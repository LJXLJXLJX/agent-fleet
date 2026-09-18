from __future__ import annotations

import gzip
import http.client
import io
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from artifact_cache_gateway.gateway.git_http import (
    GitRequestBodyError,
    decode_git_request_body,
    read_chunked_git_request_body,
)
from artifact_cache_gateway.gateway.server import GatewayHTTPServer
from artifact_cache_gateway.gateway.services.git import (
    GitMirrorError,
    GitMirrorResult,
    GitMirrorStore,
    compile_git_mirror_plan,
    read_cgi_headers,
    warm_git_mirrors,
)


def plan_for(repository: str = "example/project") -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "artifact-cache-git-mirror-plan",
        "repositories": [
            {
                "repository": repository,
                "upstream_url": f"https://github.com/{repository}.git",
            }
        ],
    }


class GitMirrorPlanTests(unittest.TestCase):
    def test_compiler_deduplicates_github_repository_and_rejects_other_hosts(self) -> None:
        report = {
            "kind": "artifact-cache-external-build-input-report",
            "dataset": "/data/rebench",
            "cache_candidates": [
                {
                    "kind": "git-repository",
                    "action": "mirror",
                    "url": "https://github.com/Example/Project.git",
                    "occurrences": 2,
                    "tasks": 2,
                    "reference": "main",
                    "examples": [{"task": "one"}],
                },
                {
                    "kind": "git-repository",
                    "action": "mirror",
                    "url": "https://github.com/example/project",
                    "occurrences": 3,
                    "tasks": 3,
                    "reference": None,
                    "examples": [{"task": "two"}],
                },
                {
                    "kind": "git-repository",
                    "action": "mirror",
                    "url": "https://gitlab.com/example/project.git",
                },
            ],
        }

        plan = compile_git_mirror_plan(report)

        self.assertEqual(plan["summary"], {"repositories": 1, "rejected": 1})
        row = plan["repositories"][0]
        self.assertEqual(row["repository"], "Example/Project")
        self.assertEqual(row["upstream_url"], "https://github.com/Example/Project.git")
        self.assertEqual(row["occurrences"], 5)
        self.assertEqual(row["references"], ["main"])

    def test_plan_rejects_path_traversal(self) -> None:
        with self.assertRaises(GitMirrorError):
            GitMirrorStore(
                plan={
                    "kind": "artifact-cache-git-mirror-plan",
                    "repositories": [
                        {
                            "repository": "example/../project",
                            "upstream_url": "https://github.com/example/../project.git",
                        }
                    ],
                },
                root=Path("/tmp/git-mirror-test"),
            )

    def test_public_github_repository_does_not_require_plan_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = GitMirrorStore(plan=None, root=Path(temporary))

            self.assertEqual(
                store.canonical_repository("Example/Unplanned.git"),
                "Example/Unplanned",
            )
            self.assertEqual(
                store.canonical_repository("example/unplanned"),
                "Example/Unplanned",
            )
            self.assertEqual(store.status()["planned_repositories"], 0)
            self.assertEqual(store.status()["on_demand_repositories"], 1)

    def test_on_demand_route_keeps_protocol_safety_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = GitMirrorStore(plan=None, root=Path(temporary))

            for repository in (
                "example/project/extra",
                "example/../project",
                "example/project?token=secret",
                "example/project#fragment",
                "example/pro%2Fject",
            ):
                with self.subTest(repository=repository):
                    with self.assertRaises(GitMirrorError):
                        store.canonical_repository(repository)

    def test_default_warm_uses_plan_not_observed_on_demand_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = GitMirrorStore(plan=plan_for(), root=Path(temporary))
            store.canonical_repository("other/on-demand")

            with mock.patch.object(
                store,
                "ensure",
                return_value=GitMirrorResult("example/project", "HIT", 0.0),
            ) as ensure:
                result = warm_git_mirrors(store, concurrency=1)

            self.assertEqual(result["summary"]["repositories"], 1)
            ensure.assert_called_once_with("example/project", refresh=False)

    def test_cgi_header_parser_preserves_body(self) -> None:
        stream = io.BytesIO(b"Status: 200 OK\r\nContent-Type: test/plain\r\n\r\nbody")
        status, headers = read_cgi_headers(stream)
        self.assertEqual(status, 200)
        self.assertEqual(headers, [("Content-Type", "test/plain")])
        self.assertEqual(stream.read(), b"body")

    def test_git_request_gzip_is_decoded_and_other_encodings_are_rejected(self) -> None:
        body = b"0032want 0123456789abcdef0123456789abcdef01234567\n0000"
        self.assertEqual(decode_git_request_body(gzip.compress(body), "gzip"), body)
        with self.assertRaises(GitMirrorError):
            decode_git_request_body(body, "br")

    def test_chunked_git_request_is_decoded_with_extensions_and_trailers(self) -> None:
        encoded = io.BytesIO(
            b"4;extension=value\r\nwant\r\n"
            b"5\r\n body\r\n"
            b"0\r\nX-Trace: ignored\r\n\r\n"
        )

        self.assertEqual(read_chunked_git_request_body(encoded), b"want body")

    def test_chunked_git_request_rejects_malformed_and_oversized_bodies(self) -> None:
        for encoded, status in (
            (b"not-hex\r\n", 400),
            (b"4\r\ndataXX", 400),
            (b"1000001\r\n", 413),
        ):
            with self.subTest(encoded=encoded):
                with self.assertRaises(GitRequestBodyError) as raised:
                    read_chunked_git_request_body(io.BytesIO(encoded))
                self.assertEqual(raised.exception.status, status)

        with mock.patch("artifact_cache_gateway.gateway.git_http._MAX_GIT_CHUNKS", 1):
            with self.assertRaises(GitRequestBodyError) as raised:
                read_chunked_git_request_body(
                    io.BytesIO(b"1\r\na\r\n1\r\nb\r\n0\r\n\r\n")
                )
            self.assertEqual(raised.exception.status, 400)

    def test_git_mirror_reports_each_cache_result_to_module_metrics(self) -> None:
        events = []
        with tempfile.TemporaryDirectory() as temporary:
            store = GitMirrorStore(
                plan=None,
                root=Path(temporary),
                metrics_callback=events.append,
            )
            with mock.patch.object(
                store, "_ensure_locked", return_value="fill"
            ), mock.patch.object(store, "_discover_submodules"):
                result = store.ensure("example/project")
        self.assertEqual(result.state, "FILL")
        self.assertEqual(events, ["fill"])


@unittest.skipUnless(shutil.which("git"), "git executable is required")
class GitMirrorSmartHTTPTests(unittest.TestCase):
    def run_git(self, *arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=30,
        )

    def test_existing_gpfs_style_bare_repository_is_cloned_over_read_only_http(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "github_mirrors"
            bare = root / "example" / "project.git"
            bare.parent.mkdir(parents=True)
            self.run_git("init", "--bare", str(bare))
            work = base / "upstream-work"
            self.run_git("init", str(work))
            self.run_git("config", "user.name", "Gateway Test", cwd=work)
            self.run_git("config", "user.email", "gateway@example.invalid", cwd=work)
            (work / "README.md").write_text("cached git mirror\n", encoding="utf-8")
            self.run_git("add", "README.md", cwd=work)
            self.run_git("commit", "-m", "fixture", cwd=work)
            self.run_git("remote", "add", "origin", str(bare), cwd=work)
            self.run_git("push", "origin", "HEAD:refs/heads/main", cwd=work)
            self.run_git("--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main")

            store = GitMirrorStore(plan=None, root=root)
            hit = store.ensure("example/project.git")
            self.assertEqual(hit.state, "HIT")
            status = store.status()
            self.assertEqual(status["planned_repositories"], 0)
            self.assertEqual(status["on_demand_repositories"], 1)
            self.assertEqual(status["ready_repositories"], 1)

            server = GatewayHTTPServer(("127.0.0.1", 0), object(), store)  # type: ignore[arg-type]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                destination = base / "clone"
                self.run_git(
                    "clone",
                    f"http://127.0.0.1:{server.server_port}/v1/git/github/example/project.git",
                    str(destination),
                )
                self.assertEqual(
                    (destination / "README.md").read_text(encoding="utf-8"),
                    "cached git mirror\n",
                )
                destination_without_suffix = base / "clone-without-dot-git"
                self.run_git(
                    "clone",
                    f"http://127.0.0.1:{server.server_port}/v1/git/github/example/project",
                    str(destination_without_suffix),
                )
                self.assertEqual(
                    (destination_without_suffix / "README.md").read_text(
                        encoding="utf-8"
                    ),
                    "cached git mirror\n",
                )

                commit = self.run_git("rev-parse", "HEAD", cwd=work).stdout.strip()
                upload_request = (
                    f"0032want {commit}\n00000009done\n".encode("ascii")
                )
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=10
                )
                connection.request(
                    "POST",
                    "/v1/git/github/example/project.git/git-upload-pack",
                    body=[upload_request[:17], upload_request[17:]],
                    headers={
                        "Content-Type": "application/x-git-upload-pack-request",
                    },
                    encode_chunked=True,
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    response.getheader("Content-Type"),
                    "application/x-git-upload-pack-result",
                )
                self.assertTrue(response.read())
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_planned_commit_records_exact_github_submodule_for_prewarm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "github_mirrors"
            owner = root / "example"
            owner.mkdir(parents=True)

            child_bare = owner / "dependency.git"
            self.run_git("init", "--bare", str(child_bare))
            child_work = base / "child-work"
            self.run_git("init", str(child_work))
            self.run_git("config", "user.name", "Gateway Test", cwd=child_work)
            self.run_git("config", "user.email", "gateway@example.invalid", cwd=child_work)
            (child_work / "child.txt").write_text("child\n", encoding="utf-8")
            self.run_git("add", "child.txt", cwd=child_work)
            self.run_git("commit", "-m", "child", cwd=child_work)
            child_commit = self.run_git("rev-parse", "HEAD", cwd=child_work).stdout.strip()
            self.run_git("remote", "add", "origin", str(child_bare), cwd=child_work)
            self.run_git("push", "origin", "HEAD:refs/heads/main", cwd=child_work)

            parent_bare = owner / "project.git"
            self.run_git("init", "--bare", str(parent_bare))
            parent_work = base / "parent-work"
            self.run_git("init", str(parent_work))
            self.run_git("config", "user.name", "Gateway Test", cwd=parent_work)
            self.run_git("config", "user.email", "gateway@example.invalid", cwd=parent_work)
            (parent_work / ".gitmodules").write_text(
                '[submodule "dependency"]\n'
                "\tpath = deps/dependency\n"
                "\turl = https://github.com/example/dependency.git\n",
                encoding="utf-8",
            )
            self.run_git("add", ".gitmodules", cwd=parent_work)
            self.run_git(
                "update-index", "--add", "--cacheinfo",
                "160000", child_commit, "deps/dependency", cwd=parent_work,
            )
            self.run_git("commit", "-m", "parent with submodule", cwd=parent_work)
            parent_commit = self.run_git("rev-parse", "HEAD", cwd=parent_work).stdout.strip()
            self.run_git("remote", "add", "origin", str(parent_bare), cwd=parent_work)
            self.run_git("push", "origin", "HEAD:refs/heads/main", cwd=parent_work)

            plan = plan_for()
            plan["repositories"][0]["references"] = [parent_commit]
            store = GitMirrorStore(plan=plan, root=root)

            result = store.ensure("example/project")

            self.assertEqual(result.state, "HIT")
            self.assertEqual(
                store.canonical_repository("example/dependency.git"),
                "example/dependency",
            )
            self.assertEqual(store.configured_count, 2)
            self.assertTrue((root / ".derived-allowlist.json").is_file())
            self.assertEqual(store.ensure("example/dependency").state, "HIT")

    def test_existing_mirror_fetches_unadvertised_planned_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            upstream = base / "upstream.git"
            self.run_git("init", "--bare", str(upstream))
            work = base / "work"
            self.run_git("init", str(work))
            self.run_git("config", "user.name", "Gateway Test", cwd=work)
            self.run_git("config", "user.email", "gateway@example.invalid", cwd=work)
            (work / "content.txt").write_text("main\n", encoding="utf-8")
            self.run_git("add", "content.txt", cwd=work)
            self.run_git("commit", "-m", "main", cwd=work)
            self.run_git("remote", "add", "origin", str(upstream), cwd=work)
            self.run_git("push", "origin", "HEAD:refs/heads/main", cwd=work)

            root = base / "github_mirrors"
            mirror = root / "example" / "project.git"
            mirror.parent.mkdir(parents=True)
            self.run_git("clone", "--mirror", str(upstream), str(mirror))

            (work / "content.txt").write_text("hidden\n", encoding="utf-8")
            self.run_git("commit", "-am", "hidden submodule pin", cwd=work)
            hidden_commit = self.run_git("rev-parse", "HEAD", cwd=work).stdout.strip()
            self.run_git("push", "origin", "HEAD:refs/heads/temporary", cwd=work)
            self.run_git(
                "--git-dir", str(upstream), "update-ref", "-d", "refs/heads/temporary"
            )
            self.run_git(
                "--git-dir", str(upstream), "config", "uploadpack.allowAnySHA1InWant", "true"
            )

            plan = plan_for()
            plan["repositories"][0]["references"] = [hidden_commit]
            store = GitMirrorStore(plan=plan, root=root)

            result = store.ensure("example/project")

            self.assertEqual(result.state, "HIT")
            self.assertEqual(
                self.run_git(
                    "--git-dir",
                    str(mirror),
                    "rev-parse",
                    f"refs/artifact-cache/pins/{hidden_commit}",
                ).stdout.strip(),
                hidden_commit,
            )


if __name__ == "__main__":
    unittest.main()
