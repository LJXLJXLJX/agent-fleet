from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from artifact_cache_gateway.core.config import GatewayConfig, SourceConfig
from artifact_cache_gateway.gateway.engine import Gateway
from artifact_cache_gateway.gateway.inventory import cache_object
from artifact_cache_gateway.gateway.request_stats import empty_modules
from artifact_cache_gateway.storage.base import CacheEntry, StorageError
from artifact_cache_gateway.storage.gpfs import FileStorage
from artifact_cache_gateway.storage.request_stats import (
    RequestStatsSession,
    empty_cache_fills,
    empty_source_stats,
    empty_upstream_attempts,
)
from artifact_cache_gateway.storage.s3 import S3Settings, S3Storage


class FakeS3Error(Exception):
    def __init__(self, code: str = "NoSuchKey", status: int = 404):
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class FakeBody(io.BytesIO):
    pass


class FakeS3Client:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.content_types: dict[tuple[str, str], str] = {}
        self.upload_file_calls = 0

    def head_bucket(self, *, Bucket: str):
        if Bucket != "test-bucket":
            raise FakeS3Error("Forbidden", 403)
        return {}

    def get_object(self, *, Bucket: str, Key: str, Range: str | None = None):
        try:
            content = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise FakeS3Error() from exc
        if Range:
            first, last = Range.removeprefix("bytes=").split("-", 1)
            content = content[int(first) : int(last) + 1]
        return {"Body": FakeBody(content), "ContentLength": len(content)}

    def head_object(self, *, Bucket: str, Key: str):
        try:
            content = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise FakeS3Error() from exc
        return {"ContentLength": len(content)}

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
    ):
        content = Body if isinstance(Body, bytes) else Body.read()
        self.objects[(Bucket, Key)] = content
        self.content_types[(Bucket, Key)] = ContentType
        return {"ETag": '"fake"'}

    def upload_file(self, **kwargs):
        self.upload_file_calls += 1
        content = Path(kwargs["Filename"]).read_bytes()
        key = (kwargs["Bucket"], kwargs["Key"])
        self.objects[key] = content
        self.content_types[key] = kwargs["ExtraArgs"]["ContentType"]

    def list_objects_v2(
        self,
        *,
        Bucket: str,
        Prefix: str,
        MaxKeys: int,
        Delimiter: str | None = None,
        ContinuationToken: str | None = None,
    ):
        keys = sorted(
            key for bucket, key in self.objects
            if bucket == Bucket and key.startswith(Prefix)
        )
        if Delimiter:
            grouped = []
            seen = set()
            for key in keys:
                remainder = key[len(Prefix) :]
                if Delimiter in remainder:
                    child = Prefix + remainder.split(Delimiter, 1)[0] + Delimiter
                    if child not in seen:
                        seen.add(child)
                        grouped.append(("prefix", child))
                else:
                    grouped.append(("object", key))
        else:
            grouped = [("object", key) for key in keys]
        offset = int(ContinuationToken or "0")
        page = grouped[offset : offset + MaxKeys]
        response = {
            "CommonPrefixes": [
                {"Prefix": value} for kind, value in page if kind == "prefix"
            ],
            "Contents": [
                {"Key": value} for kind, value in page if kind == "object"
            ],
        }
        if offset + MaxKeys < len(grouped):
            response["NextContinuationToken"] = str(offset + MaxKeys)
        return response


class FlakyReadS3Client(FakeS3Client):
    def __init__(self, failures: list[Exception]):
        super().__init__()
        self.failures = failures
        self.get_object_calls = 0

    def get_object(self, *, Bucket: str, Key: str, Range: str | None = None):
        self.get_object_calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return super().get_object(Bucket=Bucket, Key=Key, Range=Range)


def settings() -> S3Settings:
    return S3Settings(
        endpoint="http://s3.example.internal",
        region="test-region",
        bucket="test-bucket",
        prefix="artifact-cache",
        access_key_id="access",
        secret_access_key="secret",
    )


class S3SettingsTest(unittest.TestCase):
    def test_requires_all_production_settings(self) -> None:
        values = {
            "ARTIFACT_CACHE_S3_ENDPOINT": "http://s3.example.internal",
            "ARTIFACT_CACHE_S3_REGION": "test-region",
            "ARTIFACT_CACHE_S3_BUCKET": "test-bucket",
            "ARTIFACT_CACHE_S3_PREFIX": "/artifact-cache/",
            "ARTIFACT_CACHE_S3_ACCESS_KEY_ID": "access",
            "ARTIFACT_CACHE_S3_SECRET_ACCESS_KEY": "secret",
        }
        parsed = S3Settings.from_env(values)
        self.assertEqual(parsed.prefix, "artifact-cache")
        with self.assertRaises(StorageError):
            S3Settings.from_env({})
        invalid = dict(values, ARTIFACT_CACHE_S3_PREFIX="../escape")
        with self.assertRaises(StorageError):
            S3Settings.from_env(invalid)


class S3StorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.client = FakeS3Client()
        self.storage = S3Storage(
            settings(),
            Path(self.temp.name),
            client=self.client,
            stale_temp_seconds=3600,
        )
        self.content = b"0123456789-pytorch-wheel"
        self.entry = CacheEntry(
            url="https://download.pytorch.org/whl/cpu/example.whl",
            digest=hashlib.sha256(self.content).hexdigest(),
            size=len(self.content),
            content_type="application/octet-stream",
            fetched_at=time.time(),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _temp_with_content(self) -> Path:
        stream, path = self.storage.create_temp()
        with stream:
            stream.write(self.content)
        return path

    def test_publish_load_and_range_read(self) -> None:
        temp_path = self._temp_with_content()
        self.storage.publish(self.entry, temp_path)
        self.assertFalse(temp_path.exists())
        self.assertEqual(self.client.upload_file_calls, 1)
        self.assertEqual(self.storage.load(self.entry.url), self.entry)
        with self.storage.open_blob(self.entry, start=3, end=8) as stream:
            self.assertEqual(stream.read(), self.content[3:9])

        second = replace(self.entry, url=self.entry.url + "?variant=2")
        self.storage.publish(second, self._temp_with_content())
        self.assertEqual(self.client.upload_file_calls, 1)
        self.assertEqual(self.storage.load(second.url), second)

    @patch("artifact_cache_gateway.storage.s3.time.sleep", return_value=None)
    def test_transient_metadata_read_is_retried(self, _sleep) -> None:
        client = FlakyReadS3Client(
            [FakeS3Error("InternalError", 500), FakeS3Error("SlowDown", 503)]
        )
        storage = S3Storage(settings(), Path(self.temp.name), client=client)
        storage.publish(self.entry, self._temp_with_content())

        self.assertEqual(storage.load(self.entry.url), self.entry)
        self.assertEqual(client.get_object_calls, 3)

    @patch("artifact_cache_gateway.storage.s3.time.sleep", return_value=None)
    def test_transient_blob_read_is_retried(self, _sleep) -> None:
        client = FlakyReadS3Client([FakeS3Error("ServiceUnavailable", 503)])
        storage = S3Storage(settings(), Path(self.temp.name), client=client)
        storage.publish(self.entry, self._temp_with_content())

        with storage.open_blob(self.entry) as stream:
            self.assertEqual(stream.read(), self.content)
        self.assertEqual(client.get_object_calls, 2)

    @patch("artifact_cache_gateway.storage.s3.time.sleep", return_value=None)
    def test_missing_metadata_is_not_retried(self, _sleep) -> None:
        client = FlakyReadS3Client([FakeS3Error()])
        storage = S3Storage(settings(), Path(self.temp.name), client=client)

        self.assertIsNone(storage.load(self.entry.url))
        self.assertEqual(client.get_object_calls, 1)

    @patch("artifact_cache_gateway.storage.s3.time.sleep", return_value=None)
    def test_permanent_read_error_is_not_retried(self, _sleep) -> None:
        client = FlakyReadS3Client([FakeS3Error("Forbidden", 403)])
        storage = S3Storage(settings(), Path(self.temp.name), client=client)

        with self.assertRaisesRegex(StorageError, "failed to read S3 metadata"):
            storage.load(self.entry.url)
        self.assertEqual(client.get_object_calls, 1)

    def test_stale_temp_is_removed_on_startup(self) -> None:
        stream, path = self.storage.create_temp()
        stream.close()
        old = time.time() - 100
        path.touch()

        os.utime(path, (old, old))
        removed = self.storage.cleanup_stale_temp(10)
        self.assertEqual(removed, 1)
        self.assertFalse(path.exists())

    def test_request_stats_sessions_round_trip_and_overwrite_idempotently(self) -> None:
        fills = empty_cache_fills()
        fills["configured_proxy"] = {"objects": 2, "bytes": 1234}
        attempts = empty_upstream_attempts()
        attempts["direct"] = {"success": 18, "failure": 2}
        attempts["configured_proxy"] = {"success": 2, "failure": 1}
        modules = empty_modules()
        modules["pypi"]["counts"]["hit"] = 138099
        modules["pypi"]["counts"]["miss"] = 1466
        modules["pypi"]["counts"]["refresh"] = 6
        modules["pypi"]["counts"]["revalidated"] = 1240
        modules["pypi"]["counts"]["error"] = 1
        modules["pypi"]["cache_fills"] = fills
        modules["pypi"]["upstream_attempts"] = attempts
        source_stats = empty_source_stats()
        source_stats["counts"]["hit"] = 138099
        source_stats["counts"]["miss"] = 1466
        source_stats["counts"]["refresh"] = 6
        source_stats["counts"]["revalidated"] = 1240
        source_stats["counts"]["error"] = 1
        source_stats["cache_fills"] = fills
        source_stats["upstream_attempts"] = attempts
        modules["pypi"]["sources"]["pypi-simple"] = source_stats
        modules["apt"]["sources"][
            "https://packages.example/repository"
        ] = empty_source_stats()
        session = RequestStatsSession(
            session_id="tmax-2026-08-24",
            started_at=1,
            updated_at=2,
            counts={
                "hit": 138099,
                "bypass": 0,
                "miss": 1466,
                "refresh": 6,
                "revalidated": 1240,
                "stale": 0,
                "error": 1,
            },
            cache_fills=fills,
            upstream_attempts=attempts,
            modules=modules,
            label="TMax prebuild before workstation restart",
        )
        self.storage.save_request_stats(session)
        self.assertEqual(self.storage.load_request_stats(), (session,))
        self.assertEqual(session.document()["schema_version"], 4)
        self.assertIn(
            "https://packages.example/repository",
            session.document()["modules"]["apt"]["sources"],
        )
        self.storage.save_request_stats(session)
        self.assertEqual(self.storage.load_request_stats(), (session,))

        schema_three = session.document()
        schema_three["schema_version"] = 3
        for counters in schema_three["modules"].values():
            counters.pop("sources")
        loaded = RequestStatsSession.from_document(schema_three)
        self.assertEqual(loaded.modules["pypi"]["sources"], {})

    def test_request_stats_schema_one_loads_with_zero_upstream_attempts(self) -> None:
        document = {
            "schema_version": 1,
            "session_id": "legacy",
            "started_at": 1,
            "updated_at": 2,
            "counts": {
                "hit": 1,
                "bypass": 0,
                "miss": 1,
                "refresh": 0,
                "revalidated": 0,
                "stale": 0,
                "error": 0,
            },
            "cache_fills": empty_cache_fills(),
            "label": "schema-one",
        }
        session = RequestStatsSession.from_document(document)
        self.assertEqual(session.upstream_attempts, empty_upstream_attempts())

    def test_persistent_gateway_stats_survive_new_process(self) -> None:
        fills = empty_cache_fills()
        fills["direct"] = {"objects": 1, "bytes": len(self.content)}
        attempts = empty_upstream_attempts()
        attempts["direct"] = {"success": 1, "failure": 2}
        baseline = RequestStatsSession(
            session_id="historical",
            started_at=1,
            updated_at=2,
            counts={
                "hit": 4,
                "bypass": 0,
                "miss": 1,
                "refresh": 0,
                "revalidated": 0,
                "stale": 0,
                "error": 0,
            },
            cache_fills=fills,
            upstream_attempts=attempts,
            label="historical",
        )
        self.storage.save_request_stats(baseline)
        source = SourceConfig.from_dict(
            {"name": "example", "base_url": "https://example.com/"}
        )
        gateway = Gateway(
            GatewayConfig(sources={source.name: source}),
            self.storage,
            None,  # type: ignore[arg-type]
            300,
            persist_request_stats=True,
        )
        stats = gateway.stats()
        self.assertEqual(stats["lookup_total"], 5)
        self.assertEqual(stats["scope"], "persistent-cumulative")
        self.assertEqual(stats["historical_sessions"], 1)
        self.assertEqual(stats["cache_fills"]["direct"], fills["direct"])
        self.assertEqual(stats["upstream_attempts"]["direct"], attempts["direct"])
        self.assertEqual(stats["modules"]["unclassified"]["lookup_total"], 5)
        legacy_source = stats["modules"]["unclassified"][
            "historical_unattributed"
        ]
        self.assertEqual(legacy_source["lookup_total"], 5)
        self.assertEqual(stats["modules"]["unclassified"]["sources"], {})
        gateway.record_git_mirror_state("fill")
        stats = gateway.stats()
        self.assertEqual(stats["lookup_total"], 5)
        self.assertEqual(stats["modules"]["git_clone"]["miss"], 1)
        self.assertEqual(
            set(stats["modules"]["git_clone"]["sources"]), {"github"}
        )
        self.assertEqual(
            stats["modules"]["git_clone"]["sources"]["github"]["miss"], 1
        )
        gateway.close()

        reloaded = Gateway(
            GatewayConfig(sources={source.name: source}),
            self.storage,
            None,  # type: ignore[arg-type]
            300,
            persist_request_stats=True,
        )
        self.assertEqual(reloaded.stats()["modules"]["git_clone"]["miss"], 1)
        self.assertEqual(
            reloaded.stats()["modules"]["git_clone"]["sources"]["github"][
                "miss"
            ],
            1,
        )
        reloaded.close()

    def test_legacy_hashed_apt_stats_merge_into_original_url(self) -> None:
        modules = empty_modules()
        modules["apt"]["counts"]["miss"] = 2
        source_stats = empty_source_stats()
        source_stats["counts"]["miss"] = 2
        modules["apt"]["sources"][
            "apt-objects-example-com-1234abcd"
        ] = source_stats
        session = RequestStatsSession(
            session_id="legacy-hashed-source",
            started_at=1,
            updated_at=2,
            counts={**modules["apt"]["counts"]},
            cache_fills=empty_cache_fills(),
            upstream_attempts=empty_upstream_attempts(),
            modules=modules,
        )
        self.storage.save_request_stats(session)
        source = SourceConfig.from_dict(
            {
                "name": "apt-objects-example-com",
                "kind": "static-objects",
                "ecosystem": "apt",
                "base_url": "https://example.com/",
                "allowed_exact_paths": ["key.asc"],
            }
        )
        gateway = Gateway(
            GatewayConfig(sources={source.name: source}),
            self.storage,
            None,  # type: ignore[arg-type]
            300,
            persist_request_stats=True,
        )
        apt_sources = gateway.stats()["modules"]["apt"]["sources"]
        original_url = "https://example.com/key.asc"
        self.assertEqual(set(apt_sources), {original_url})
        self.assertEqual(apt_sources[original_url]["miss"], 2)
        gateway.close()

    def test_removed_legacy_apt_source_is_unattributed_not_internal_name(self) -> None:
        modules = empty_modules()
        modules["apt"]["counts"]["hit"] = 1
        source_stats = empty_source_stats()
        source_stats["counts"]["hit"] = 1
        modules["apt"]["sources"]["apt-objects-removed-example"] = source_stats
        session = RequestStatsSession(
            session_id="removed-legacy-apt-source",
            started_at=1,
            updated_at=2,
            counts={**modules["apt"]["counts"]},
            cache_fills=empty_cache_fills(),
            upstream_attempts=empty_upstream_attempts(),
            modules=modules,
        )
        self.storage.save_request_stats(session)
        gateway = Gateway(
            GatewayConfig(sources={}),
            self.storage,
            None,  # type: ignore[arg-type]
            300,
            persist_request_stats=True,
        )

        apt_stats = gateway.stats()["modules"]["apt"]
        self.assertEqual(apt_stats["sources"], {})
        self.assertEqual(apt_stats["hit"], 1)
        self.assertEqual(apt_stats["historical_unattributed"]["hit"], 1)
        gateway.close()

    def test_shared_inventory_is_hierarchical_and_paginated(self) -> None:
        source = SourceConfig.from_dict(
            {
                "name": "pypi-files",
                "base_url": "https://pypi.example/",
                "ecosystem": "pip",
            }
        )
        filenames = (
            "torch-2.13.0-cp310-cp310-manylinux_x86_64.whl",
            "torch-2.13.0-cp311-cp311-manylinux_x86_64.whl",
        )
        for index, filename in enumerate(filenames):
            path = f"packages/aa/{filename}"
            entry = replace(
                self.entry,
                url=source.build_url(path),
                digest=str(index + 1) * 64,
            )
            self.storage.ensure_inventory(cache_object(source, path, entry))

        ecosystems = self.storage.list_inventory((), None, 10)
        self.assertEqual(ecosystems.children, ("pip",))
        sources = self.storage.list_inventory(("pip",), None, 10)
        self.assertEqual(sources.children, ("pypi-files",))
        packages = self.storage.list_inventory(("pip", "pypi-files"), None, 10)
        self.assertEqual(packages.children, ("torch",))
        versions = self.storage.list_inventory(
            ("pip", "pypi-files", "torch"), None, 10
        )
        self.assertEqual(versions.children, ("2.13.0",))

        first = self.storage.list_inventory(
            ("pip", "pypi-files", "torch", "2.13.0"), None, 1
        )
        self.assertEqual(len(first.objects), 1)
        self.assertIsNotNone(first.next_cursor)
        second = self.storage.list_inventory(
            ("pip", "pypi-files", "torch", "2.13.0"),
            first.next_cursor,
            1,
        )
        self.assertEqual(len(second.objects), 1)
        self.assertIsNone(second.next_cursor)
        self.assertNotEqual(first.objects[0].python_tag, second.objects[0].python_tag)


class HitRateLogTest(unittest.TestCase):
    def test_structured_log_has_rates_and_no_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = FileStorage(Path(directory))
            source = SourceConfig.from_dict(
                {"name": "example", "base_url": "https://example.com/"}
            )
            url = source.build_url("artifact.whl", "token=do-not-log")
            content = b"artifact"
            entry = CacheEntry(
                url=url,
                digest=hashlib.sha256(content).hexdigest(),
                size=len(content),
                content_type="application/octet-stream",
                fetched_at=time.time(),
            )
            stream, temp_path = storage.create_temp()
            with stream:
                stream.write(content)
            storage.publish(entry, temp_path)
            gateway = Gateway(
                config=GatewayConfig(sources={source.name: source}),
                storage=storage,
                fetcher=None,  # type: ignore[arg-type]
                index_ttl_seconds=300,
            )
            with self.assertLogs(
                "artifact_cache_gateway.hit_rate", level="INFO"
            ) as captured:
                result = gateway.resolve("example", "artifact.whl", "token=do-not-log")
            self.assertEqual(result.state, "HIT")
            payload = json.loads(captured.records[-1].getMessage())
            self.assertEqual(payload["event"], "cache_hit_rate")
            self.assertEqual(payload["state"], "HIT")
            self.assertEqual(payload["hit_rate"], 1.0)
            self.assertEqual(payload["fresh_hit_rate"], 1.0)
            self.assertEqual(len(payload["url_key"]), 16)
            self.assertNotIn("do-not-log", captured.records[-1].getMessage())
            self.assertNotIn("https://", captured.records[-1].getMessage())


if __name__ == "__main__":
    unittest.main()
