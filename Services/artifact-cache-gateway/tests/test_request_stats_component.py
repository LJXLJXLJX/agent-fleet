"""Key behavioral tests for the standalone request-stats component.

`RequestStats` (`gateway/engine/_stats.py`) is an internal stats component that was
factored out of the Gateway by composition: it owns its own state, stats lock,
checkpoint thread and shutdown flow, and does not hold the full Gateway. This file
constructs the component directly (paired with a temporary FileStorage or a minimal
fake storage), bypassing the Gateway/Fetcher/HTTP server, and verifies recording,
querying, persistence and shutdown through its public behavior.

Covers: cache/Git/upstream-attempt counting and grouping, historical-session recovery,
persistence failure states, save-on-close and thread exit, and concurrent recording that
does not lose counts (verified with controlled synchronization, not long sleeps).
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from artifact_cache_gateway.core.config import GatewayConfig, SourceConfig
from artifact_cache_gateway.gateway.engine._stats import RequestStats
from artifact_cache_gateway.storage.base import StorageError
from artifact_cache_gateway.storage.gpfs import FileStorage
from artifact_cache_gateway.storage.request_stats import RequestStatsSession


def make_source(name: str, ecosystem: str = "pip", base_url: str = "https://example.com/") -> SourceConfig:
    return SourceConfig.from_dict(
        {
            "name": name,
            "base_url": base_url,
            "ecosystem": ecosystem,
            "proxy_mode": "configured",
        }
    )


def display_name(source: SourceConfig, request_url: str | None = None) -> str:
    return source.name


class RequestStatsComponentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.storage = FileStorage(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build_config(self, *sources: SourceConfig) -> GatewayConfig:
        return GatewayConfig(sources={source.name: source for source in sources})

    # ---- Standalone construction (no Gateway/Fetcher/server) ----

    def test_component_constructs_without_gateway(self) -> None:
        stats = RequestStats(
            self.build_config(make_source("pypi", "pip")),
            self.storage,
            display_name,
            persist_request_stats=False,
            stats_checkpoint_seconds=5.0,
            cache_mode="proxy-only",
        )
        document = stats.stats()
        self.assertEqual(document["scope"], "current-process")
        self.assertEqual(document["historical_sessions"], 0)
        self.assertEqual(document["request_total"], 0)
        self.assertEqual(document["cache_mode"], "proxy-only")
        stats.close()

    def test_display_name_callback_without_default_argument(self) -> None:
        # The component's source_display_name callback signature is (SourceConfig, str | None).
        # Passing a two-argument callback with no defaults must cover all four call sites
        # (query, historical recovery, upstream recording and normal recording); any call site
        # that omits an argument raises TypeError here (regression guard).
        source = make_source("pypi", "pip")
        # A two-argument callback with no defaults: it must receive exactly 2 positional
        # arguments, otherwise TypeError.
        both_args = lambda cfg, request_url: cfg.name
        stats = RequestStats(
            self.build_config(source),
            self.storage,
            both_args,
            persist_request_stats=True,
            stats_checkpoint_seconds=5.0,
            cache_mode="proxy-only",
        )
        # Normal recording path: record() passes source + url.
        stats.record("MISS", source, "https://example.com/pkg", cache_fill_route="configured_proxy", cache_fill_bytes=1)
        # Upstream recording path: record_upstream_attempts() passes source only.
        stats.record_upstream_attempts(source, (FakeAttempt("configured", True),))
        # Stats query path: stats() passes source only via _module_stats_document_locked().
        document = stats.stats()
        self.assertEqual(document["modules"]["pypi"]["miss"], 1)
        stats.close()
        # Historical recovery path: _load_persistent_stats() passes source only.
        reloaded = RequestStats(
            self.build_config(source),
            self.storage,
            both_args,
            persist_request_stats=True,
            stats_checkpoint_seconds=5.0,
            cache_mode="proxy-only",
        )
        reloaded_pypi = reloaded.stats()["modules"]["pypi"]
        self.assertIn("pypi", reloaded_pypi["sources"])
        reloaded.close()

    # ---- Counting, grouping and querying ----

    def test_record_counts_and_source_grouping(self) -> None:
        pypi = make_source("pypi-simple", "pip")
        stats = RequestStats(
            self.build_config(pypi),
            self.storage,
            display_name,
            persist_request_stats=False,
            stats_checkpoint_seconds=5.0,
            cache_mode="all",
        )
        url = pypi.build_url("simple/project/")
        stats.record("HIT", pypi, url, cache_fill_route="configured_proxy", cache_fill_bytes=10)
        stats.record("MISS", pypi, url, cache_fill_route="configured_proxy", cache_fill_bytes=20)
        document = stats.stats()
        pypi_module = document["modules"]["pypi"]
        self.assertEqual((pypi_module["hit"], pypi_module["miss"]), (1, 1))
        self.assertEqual(
            (
                pypi_module["sources"]["pypi-simple"]["hit"],
                pypi_module["sources"]["pypi-simple"]["miss"],
            ),
            (1, 1),
        )
        self.assertEqual(
            pypi_module["sources"]["pypi-simple"]["cache_fills"]["configured_proxy"],
            {"objects": 2, "bytes": 30},
        )
        self.assertEqual(document["request_total"], 2)
        self.assertEqual(document["hit_rate"], 0.5)
        stats.close()

    def test_git_mirror_state_grouping_and_validation(self) -> None:
        stats = RequestStats(
            self.build_config(),
            self.storage,
            display_name,
            persist_request_stats=False,
            stats_checkpoint_seconds=5.0,
            cache_mode="proxy-only",
        )
        stats.record_git_mirror_state("fill")
        stats.record_git_mirror_state("hit")
        with self.assertRaises(ValueError):
            stats.record_git_mirror_state("bogus")
        git = stats.stats()["modules"]["git_clone"]
        self.assertEqual((git["hit"], git["miss"]), (1, 1))
        self.assertEqual(
            (git["sources"]["github"]["hit"], git["sources"]["github"]["miss"]),
            (1, 1),
        )
        stats.close()

    def test_upstream_attempts_routes(self) -> None:
        source = make_source("direct-src", "pip", "https://direct.example.com/")
        stats = RequestStats(
            self.build_config(source),
            self.storage,
            display_name,
            persist_request_stats=False,
            stats_checkpoint_seconds=5.0,
            cache_mode="proxy-only",
        )
        failed = [FakeAttempt("direct", False), FakeAttempt("configured", True)]
        stats.record_upstream_attempts(source, failed)
        stats.record_upstream_attempts(source, (), FakeAttempt("configured", True))
        document = stats.stats()
        self.assertEqual(document["upstream_attempts"]["direct"], {"success": 0, "failure": 1})
        self.assertEqual(
            document["upstream_attempts"]["configured_proxy"],
            {"success": 1, "failure": 1},
        )
        self.assertEqual(document["upstream_attempts"]["configured_direct"], {"success": 0, "failure": 0})
        stats.close()

    # ---- Historical session recovery and persistence ----

    def test_historical_session_recovery_and_persist_on_close(self) -> None:
        source = make_source("pypi", "pip")
        stats = RequestStats(
            self.build_config(source),
            self.storage,
            display_name,
            persist_request_stats=True,
            stats_checkpoint_seconds=5.0,
            cache_mode="proxy-only",
        )
        stats.record("MISS", source, "https://example.com/a", cache_fill_route="configured_proxy", cache_fill_bytes=5)
        stats.record_git_mirror_state("fill")
        # close() forces the final checkpoint to be written back.
        stats.close()

        reloaded = RequestStats(
            self.build_config(source),
            self.storage,
            display_name,
            persist_request_stats=True,
            stats_checkpoint_seconds=5.0,
            cache_mode="proxy-only",
        )
        document = reloaded.stats()
        self.assertEqual(document["scope"], "persistent-cumulative")
        self.assertEqual(document["historical_sessions"], 1)
        pypi = document["modules"]["pypi"]
        self.assertEqual(pypi["miss"], 1)
        self.assertEqual(
            pypi["sources"]["pypi"]["cache_fills"]["configured_proxy"],
            {"objects": 1, "bytes": 5},
        )
        self.assertEqual(document["modules"]["git_clone"]["miss"], 1)
        reloaded.close()

    def test_persistence_failure_surfaces_error_without_data_loss(self) -> None:
        source = make_source("pypi", "pip")
        failing = FailingStorage(self.storage, fail_save=True)
        stats = RequestStats(
            self.build_config(source),
            failing,
            display_name,
            persist_request_stats=True,
            stats_checkpoint_seconds=5.0,
            cache_mode="proxy-only",
        )
        stats.record("MISS", source, "https://example.com/a")
        document = stats.stats()
        self.assertIsNotNone(document["persistence_error"])
        self.assertEqual(document["modules"]["pypi"]["miss"], 1)
        stats.close()
        self.assertIsNotNone(document["persistence_error"])

    def test_load_failure_surfaces_error_and_starts_empty(self) -> None:
        failing = FailingStorage(self.storage, fail_load=True)
        stats = RequestStats(
            self.build_config(make_source("pypi", "pip")),
            failing,
            display_name,
            persist_request_stats=True,
            stats_checkpoint_seconds=5.0,
            cache_mode="proxy-only",
        )
        document = stats.stats()
        self.assertIsNotNone(document["persistence_error"])
        self.assertEqual(document["historical_sessions"], 0)
        stats.close()

    # ---- Save on close, thread exit and concurrency ----

    def test_close_joins_thread_and_flushes(self) -> None:
        baseline_threads = len(threading.enumerate())
        source = make_source("pypi", "pip")
        stats = RequestStats(
            self.build_config(source),
            self.storage,
            display_name,
            persist_request_stats=True,
            stats_checkpoint_seconds=60.0,
            cache_mode="proxy-only",
        )
        # The persistent component starts a background checkpoint thread (owned by the component).
        self.assertEqual(len(threading.enumerate()), baseline_threads + 1)
        stats.record("MISS", source, "https://example.com/b")
        stats.close()
        # After close, the checkpoint thread exits, returning to the baseline thread count.
        self.assertEqual(len(threading.enumerate()), baseline_threads)
        # The final checkpoint from close has been written to disk.
        sessions = self.storage.load_request_stats()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].modules["pypi"]["counts"]["miss"], 1)

    def test_concurrent_records_do_not_lose_counts(self) -> None:
        workers = 8
        per_worker = 50
        source = make_source("pypi", "pip")
        stats = RequestStats(
            self.build_config(source),
            self.storage,
            display_name,
            persist_request_stats=False,
            stats_checkpoint_seconds=5.0,
            cache_mode="proxy-only",
        )
        barrier = threading.Barrier(workers)

        def worker() -> None:
            barrier.wait()  # Controlled synchronization: all threads start at once.
            for _ in range(per_worker):
                stats.record_git_mirror_state("hit")

        threads = [
            threading.Thread(target=worker) for _ in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        git = stats.stats()["modules"]["git_clone"]
        self.assertEqual(git["hit"], workers * per_worker)
        self.assertEqual(git["sources"]["github"]["hit"], workers * per_worker)
        # Git mirror state counts only toward the git_clone module, not the global
        # request_total count (consistent with the Gateway's original record_git_mirror_state).
        self.assertEqual(stats.stats()["current_process"]["hit"], 0)
        stats.close()


class FakeAttempt:
    def __init__(self, proxy_mode: str, used_proxy: bool) -> None:
        self.proxy_mode = proxy_mode
        self.used_proxy = used_proxy


class FailingStorage:
    """Fake storage that raises when saving/loading request stats, used for persistence failure paths."""

    def __init__(self, inner: FileStorage, *, fail_save: bool = False, fail_load: bool = False):
        self._inner = inner
        self._fail_save = fail_save
        self._fail_load = fail_load

    def load_request_stats(self) -> tuple[RequestStatsSession, ...]:
        if self._fail_load:
            raise StorageError("load fails")
        return self._inner.load_request_stats()

    def save_request_stats(self, session: RequestStatsSession) -> None:
        if self._fail_save:
            raise StorageError("save fails")
        self._inner.save_request_stats(session)


if __name__ == "__main__":
    unittest.main()