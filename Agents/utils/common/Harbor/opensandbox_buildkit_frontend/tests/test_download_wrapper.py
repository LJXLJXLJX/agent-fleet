import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[2] / "opensandbox_download_runtime"
WRAPPER = RUNTIME / "download-wrapper.sh"
REWRITER = RUNTIME / "url-rewriter.awk"
SOURCE = "http://third-party-source.internal/v1/cache"


def encoded(value: str) -> str:
    return value.encode("utf-8").hex()


def rewrite(original: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "awk",
            "-v",
            f"source={SOURCE}",
            "-v",
            f"original={original}",
            "-f",
            str(REWRITER),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


class DownloadWrapperTest(unittest.TestCase):
    def test_route_reversibly_encodes_origin_and_original_path(self) -> None:
        cases = {
            "https://github.com/org/repo/archive/v1.tar.gz": (
                f"{SOURCE}/download/v1/https/{encoded('github.com')}/object/"
                f"{encoded('/org/repo/archive/v1.tar.gz')}\n"
            ),
            "http://example.com:8080/a%20b": (
                f"{SOURCE}/download/v1/http/{encoded('example.com:8080')}/object/"
                f"{encoded('/a%20b')}\n"
            ),
            "https://example.com": (
                f"{SOURCE}/download/v1/https/{encoded('example.com')}/root\n"
            ),
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                result = rewrite(original)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)

    def test_leading_zero_ports_are_canonicalized(self) -> None:
        cases = {
            "http://example.com:080/a": (
                f"{SOURCE}/download/v1/http/{encoded('example.com')}/object/"
                f"{encoded('/a')}\n"
            ),
            "https://example.com:007/a": (
                f"{SOURCE}/download/v1/https/{encoded('example.com:7')}/object/"
                f"{encoded('/a')}\n"
            ),
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                result = rewrite(original)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)

    def test_root_literal_at_root_and_long_hosts_do_not_collide(self) -> None:
        urls = (
            "https://example.com",
            "https://example.com/@root",
            "https://" + "a" * 70 + ".example/one",
            "https://" + "a" * 69 + "b.example/two",
        )
        routes = []
        for original in urls:
            result = rewrite(original)
            self.assertEqual(result.returncode, 0, result.stderr)
            routes.append(result.stdout)
        self.assertEqual(len(routes), len(set(routes)))

    def test_query_and_fragment_are_not_cache_routes(self) -> None:
        original = "https://example.com/file?version=2#section"
        result = rewrite(original)
        self.assertEqual(result.returncode, 2)
        self.assertIn("event=invalid-url", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_loopback_is_not_a_cache_route(self) -> None:
        for original in (
            "http://127.0.0.1:8080/artifact",
            "http://localhost:8080/artifact",
            "http://[::1]:8080/artifact",
        ):
            with self.subTest(original=original):
                result = rewrite(original)
                self.assertEqual(result.returncode, 2)
                self.assertIn("event=invalid-url", result.stderr)

    def test_non_public_authorities_are_not_cache_routes(self) -> None:
        for original in (
            "http://10.0.0.5/pkgs/foo.deb",
            "http://192.168.1.1/file",
            "http://169.254.169.254/latest/meta-data",
            "http://8.8.8.8/file",
            "http://LOCALHOST:8080/artifact",
            "http://db:5432/schema",
            "http://host.docker.internal/x",
            "http://repo.corp.internal/x",
        ):
            with self.subTest(original=original):
                result = rewrite(original)
                self.assertEqual(result.returncode, 2)
                self.assertIn("event=invalid-url", result.stderr)

    def test_url_glob_is_not_a_cache_route(self) -> None:
        for original in (
            "https://example.com/file[1-3].txt",
            "https://example.com/file{a,b}.txt",
        ):
            with self.subTest(original=original):
                result = rewrite(original)
                self.assertEqual(result.returncode, 2)
                self.assertIn("event=invalid-url", result.stderr)

    def test_credentialed_authority_is_not_a_cache_route(self) -> None:
        result = rewrite("https://user@example.com/file")
        self.assertEqual(result.returncode, 2)
        self.assertIn("event=invalid-url", result.stderr)

    def test_wrapper_is_posix_sh_and_preserves_direct_fallback(self) -> None:
        syntax = subprocess.run(
            ["sh", "-n", str(WRAPPER)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        source = WRAPPER.read_text(encoding="utf-8")
        self.assertTrue(source.startswith("#!/bin/sh\n"))
        self.assertIn("set -eu", source)
        self.assertIn("classify_curl", source)
        self.assertIn("classify_wget", source)
        self.assertIn('exec "$real" "$@"', source)
        self.assertNotIn("value_option", source)
        self.assertNotIn("set -euo pipefail", source)
        self.assertNotIn("blocked_proxy=http://127.0.0.1:1", source)
        self.assertNotIn("WGETRC=/dev/null", source)
        self.assertIn("OPENSANDBOX_DOWNLOAD_RUNTIME_DIR", source)
        self.assertIn("OPENSANDBOX_DOWNLOAD_REAL_BIN_DIR", source)


class DownloadWrapperBehaviorTest(unittest.TestCase):
    """Exercise the wrapper end-to-end against a fake filesystem layout."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.runtime = root / "runtime"
        self.runtime.mkdir()
        (self.runtime / "source").write_text(SOURCE + "\n", encoding="utf-8")
        shutil.copy(REWRITER, self.runtime / "url-rewriter.awk")
        self.real_bin = root / "real-bin"
        self.real_bin.mkdir()
        for command in ("curl", "wget"):
            fake = self.real_bin / command
            fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
            fake.chmod(0o755)
        self.wrapper_dir = root / "wrapper-bin"
        self.wrapper_dir.mkdir()
        for command in ("curl", "wget"):
            wrapper_copy = self.wrapper_dir / command
            shutil.copy(WRAPPER, wrapper_copy)
            wrapper_copy.chmod(0o755)
        self.env = {
            **os.environ,
            "OPENSANDBOX_DOWNLOAD_RUNTIME_DIR": str(self.runtime),
            "OPENSANDBOX_DOWNLOAD_REAL_BIN_DIR": str(self.real_bin),
        }

    def run_wrapper(
        self, command: str, *arguments: str, env: dict[str, str] | None = None
    ) -> list[str]:
        result = subprocess.run(
            [str(self.wrapper_dir / command), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=env or self.env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_header_request_bypasses_cache_without_changing_arguments(
        self,
    ) -> None:
        self.assertEqual(
            self.run_wrapper(
                "curl",
                "-H",
                "Referer: https://example.com/x",
                "https://download.example.com/f.tgz",
            ),
            ["-H", "Referer: https://example.com/x", "https://download.example.com/f.tgz"],
        )

    def test_request_body_bypasses_cache_without_changing_arguments(
        self,
    ) -> None:
        self.assertEqual(
            self.run_wrapper(
                "curl",
                "-d",
                "redirect_uri=https://example.com/cb",
                "https://api.example.com/token",
            ),
            ["-d", "redirect_uri=https://example.com/cb", "https://api.example.com/token"],
        )

    def test_joined_long_header_option_is_passthrough(self) -> None:
        self.assertEqual(
            self.run_wrapper(
                "curl", "--header=X: https://y", "https://dl.example.com/a"
            ),
            ["--header=X: https://y", "https://dl.example.com/a"],
        )

    def test_wget_post_bypasses_cache_without_changing_arguments(self) -> None:
        self.assertEqual(
            self.run_wrapper(
                "wget",
                "--post-data",
                "url=https://x",
                "https://api.example.com/",
            ),
            ["--post-data", "url=https://x", "https://api.example.com/"],
        )

    def test_plain_flags_and_download_url_still_rewritten(self) -> None:
        self.assertEqual(
            self.run_wrapper("curl", "-fsSL", "https://github.com/a/b.tar.gz"),
            [
                "-fsSL",
                (
                    f"{SOURCE}/download/v1/https/"
                    f"{encoded('github.com')}/object/{encoded('/a/b.tar.gz')}"
                ),
            ],
        )

    def test_wget_with_explicit_output_is_rewritten(self) -> None:
        self.assertEqual(
            self.run_wrapper("wget", "-qO", "/tmp/a", "https://example.com/a"),
            [
                "-qO",
                "/tmp/a",
                (
                    f"{SOURCE}/download/v1/https/"
                    f"{encoded('example.com')}/object/{encoded('/a')}"
                ),
            ],
        )

    def test_wget_default_filename_and_credentialed_url_bypass(self) -> None:
        self.assertEqual(
            self.run_wrapper("wget", "https://example.com/a"),
            ["https://example.com/a"],
        )
        self.assertEqual(
            self.run_wrapper("curl", "https://user@example.com/a"),
            ["https://user@example.com/a"],
        )

    def test_proto_and_tls_constraints_bypass_verbatim(self) -> None:
        self.assertEqual(
            self.run_wrapper(
                "curl",
                "--proto",
                "=https",
                "--tlsv1.2",
                "-sSf",
                "https://sh.rustup.rs",
            ),
            ["--proto", "=https", "--tlsv1.2", "-sSf", "https://sh.rustup.rs"],
        )

    def test_proto_equals_and_redirect_proto_bypass(self) -> None:
        self.assertEqual(
            self.run_wrapper(
                "curl",
                "--proto=https",
                "--proto-redir",
                "https,http",
                "--tls-max",
                "1.3",
                "https://download.example.com/f.tgz",
            ),
            [
                "--proto=https",
                "--proto-redir",
                "https,http",
                "--tls-max",
                "1.3",
                "https://download.example.com/f.tgz",
            ],
        )
        self.assertEqual(
            self.run_wrapper(
                "curl",
                "--proto-default=https",
                "--proto-redir=https",
                "--tls-max=1.2",
                "https://download.example.com/g",
            ),
            [
                "--proto-default=https",
                "--proto-redir=https",
                "--tls-max=1.2",
                "https://download.example.com/g",
            ],
        )

    def test_tls_security_options_bypass(self) -> None:
        self.assertEqual(
            self.run_wrapper(
                "curl",
                "--tlsv1.3",
                "--tls13-ciphers",
                "TLS_AES_128_GCM_SHA256",
                "--cacert",
                "/etc/ssl/certs.pem",
                "--capath",
                "/etc/ssl/certs",
                "--ciphers",
                "DEFAULT",
                "--curves",
                "X25519",
                "--crlfile",
                "/tmp/crl",
                "--pinnedpubkey",
                "sha256//abc",
                "--cert-status",
                "--insecure",
                "https://example.com/file",
            ),
            [
                "--tlsv1.3",
                "--tls13-ciphers",
                "TLS_AES_128_GCM_SHA256",
                "--cacert",
                "/etc/ssl/certs.pem",
                "--capath",
                "/etc/ssl/certs",
                "--ciphers",
                "DEFAULT",
                "--curves",
                "X25519",
                "--crlfile",
                "/tmp/crl",
                "--pinnedpubkey",
                "sha256//abc",
                "--cert-status",
                "--insecure",
                "https://example.com/file",
            ],
        )

    def test_wget_tls_option_bypasses_instead_of_rewriting(self) -> None:
        self.assertEqual(
            self.run_wrapper(
                "wget",
                "-O",
                "/tmp/f",
                "--https-only",
                "https://example.com/a",
            ),
            ["-O", "/tmp/f", "--https-only", "https://example.com/a"],
        )
        self.assertEqual(
            self.run_wrapper(
                "wget",
                "--no-check-certificate",
                "--ca-certificate",
                "/etc/ssl/certs.pem",
                "https://example.com/b",
            ),
            [
                "--no-check-certificate",
                "--ca-certificate",
                "/etc/ssl/certs.pem",
                "https://example.com/b",
            ],
        )

    def test_plain_download_still_rewritten_and_proto_value_not_a_url(self) -> None:
        # The output option precedes the URL in the cacheable grammar; the URL
        # is rewritten while the output file passes through untouched.
        self.assertEqual(
            self.run_wrapper(
                "curl",
                "-fsSL",
                "-o",
                "/tmp/file",
                "https://example.org/file",
            ),
            [
                "-fsSL",
                "-o",
                "/tmp/file",
                (
                    f"{SOURCE}/download/v1/https/"
                    f"{encoded('example.org')}/object/{encoded('/file')}"
                ),
            ],
        )
        # Unknown long options are unsupported grammar: the whole invocation
        # bypasses, so the separated --proto value ("=https") is never treated
        # as a cache key.
        self.assertEqual(
            self.run_wrapper("curl", "--proto", "=https", "https://sh.rustup.rs"),
            ["--proto", "=https", "https://sh.rustup.rs"],
        )

    def test_cacheable_curl_shapes_are_rewritten(self) -> None:
        route = (
            f"{SOURCE}/download/v1/https/"
            f"{encoded('example.com')}/object/{encoded('/a')}"
        )
        for flags in ("-s", "-S", "-f", "-L", "-fsSL", "-sSfL"):
            with self.subTest(flags=flags):
                self.assertEqual(
                    self.run_wrapper("curl", flags, "https://example.com/a"),
                    [flags, route],
                )

    def test_cacheable_curl_output_option_shapes(self) -> None:
        route = (
            f"{SOURCE}/download/v1/https/"
            f"{encoded('example.com')}/object/{encoded('/a')}"
        )
        self.assertEqual(
            self.run_wrapper("curl", "-o", "/tmp/f", "https://example.com/a"),
            ["-o", "/tmp/f", route],
        )
        self.assertEqual(
            self.run_wrapper("curl", "-sSLo", "/tmp/f", "https://example.com/a"),
            ["-sSLo", "/tmp/f", route],
        )
        self.assertEqual(
            self.run_wrapper("curl", "-sSLo/tmp/f", "https://example.com/a"),
            ["-sSLo/tmp/f", route],
        )

    def test_unknown_curl_option_bypasses_verbatim(self) -> None:
        self.assertEqual(
            self.run_wrapper("curl", "--compressed", "https://example.com/a"),
            ["--compressed", "https://example.com/a"],
        )
        self.assertEqual(
            self.run_wrapper("curl", "-v", "https://example.com/a"),
            ["-v", "https://example.com/a"],
        )

    def test_unknown_value_taking_option_never_rewrites_its_value(self) -> None:
        self.assertEqual(
            self.run_wrapper(
                "curl",
                "--retry",
                "https://evil.example/x",
                "https://real.example/src.tgz",
            ),
            [
                "--retry",
                "https://evil.example/x",
                "https://real.example/src.tgz",
            ],
        )

    def test_curl_multiple_urls_bypass(self) -> None:
        self.assertEqual(
            self.run_wrapper(
                "curl",
                "https://example.com/a",
                "https://example.com/b",
            ),
            ["https://example.com/a", "https://example.com/b"],
        )

    def test_curl_options_after_url_bypass(self) -> None:
        self.assertEqual(
            self.run_wrapper("curl", "https://example.com/a", "-s"),
            ["https://example.com/a", "-s"],
        )
        self.assertEqual(
            self.run_wrapper("curl", "https://example.com/a", "-o", "/tmp/f"),
            ["https://example.com/a", "-o", "/tmp/f"],
        )

    def test_wget_whitelisted_shapes_are_rewritten(self) -> None:
        route = (
            f"{SOURCE}/download/v1/https/"
            f"{encoded('example.com')}/object/{encoded('/a')}"
        )
        self.assertEqual(
            self.run_wrapper("wget", "-O", "/tmp/f", "https://example.com/a"),
            ["-O", "/tmp/f", route],
        )
        self.assertEqual(
            self.run_wrapper("wget", "-q", "-O", "/tmp/f", "https://example.com/a"),
            ["-q", "-O", "/tmp/f", route],
        )
        self.assertEqual(
            self.run_wrapper("wget", "-qO", "/tmp/f", "https://example.com/a"),
            ["-qO", "/tmp/f", route],
        )
        self.assertEqual(
            self.run_wrapper("wget", "-qO-", "https://example.com/a"),
            ["-qO-", route],
        )

    def test_wget_unsupported_shapes_bypass_verbatim(self) -> None:
        self.assertEqual(
            self.run_wrapper("wget", "https://example.com/a"),
            ["https://example.com/a"],
        )
        self.assertEqual(
            self.run_wrapper(
                "wget",
                "--no-check-certificate",
                "-O",
                "/tmp/f",
                "https://example.com/a",
            ),
            ["--no-check-certificate", "-O", "/tmp/f", "https://example.com/a"],
        )
        self.assertEqual(
            self.run_wrapper("wget", "https://example.com/a", "-O", "/tmp/f"),
            ["https://example.com/a", "-O", "/tmp/f"],
        )

    def test_wget_plain_download_rewrites_and_plain_url_is_direct(self) -> None:
        self.assertEqual(
            self.run_wrapper("wget", "-O", "/tmp/file", "https://example.com/a"),
            [
                "-O",
                "/tmp/file",
                (
                    f"{SOURCE}/download/v1/https/"
                    f"{encoded('example.com')}/object/{encoded('/a')}"
                ),
            ],
        )
        self.assertEqual(
            self.run_wrapper("wget", "https://example.com/a"),
            ["https://example.com/a"],
        )

    def test_combined_remote_name_flag_bypasses_cache(self) -> None:
        self.assertEqual(
            self.run_wrapper("curl", "-LO", "https://example.com/archive.tar.gz"),
            ["-LO", "https://example.com/archive.tar.gz"],
        )
        self.assertEqual(
            self.run_wrapper("curl", "-sLO", "https://example.com/archive.tar.gz"),
            ["-sLO", "https://example.com/archive.tar.gz"],
        )

    def test_write_out_option_bypasses_cache(self) -> None:
        self.assertEqual(
            self.run_wrapper("curl", "-w", "%{size_download}", "https://example.com/a"),
            ["-w", "%{size_download}", "https://example.com/a"],
        )
        self.assertEqual(
            self.run_wrapper("curl", "-sw", "%{http_code}", "https://example.com/a"),
            ["-sw", "%{http_code}", "https://example.com/a"],
        )

    def test_implicit_curlrc_bypasses_cache(self) -> None:
        home = Path(self.tmp.name) / "home-curlrc"
        home.mkdir()
        (home / ".curlrc").write_text("silent\n", encoding="utf-8")
        self.assertEqual(
            self.run_wrapper(
                "curl",
                "https://example.com/a",
                env={**self.env, "HOME": str(home)},
            ),
            ["https://example.com/a"],
        )
        self.assertEqual(
            self.run_wrapper(
                "curl",
                "https://example.com/a",
                env={**self.env, "HOME": str(home), "CURL_HOME": str(home)},
            ),
            ["https://example.com/a"],
        )

    def test_implicit_xdg_curlrc_bypasses_cache(self) -> None:
        xdg = Path(self.tmp.name) / "xdg-config"
        xdg.mkdir()
        (xdg / "curlrc").write_text("silent\n", encoding="utf-8")
        self.assertEqual(
            self.run_wrapper(
                "curl",
                "https://example.com/a",
                env={**self.env, "XDG_CONFIG_HOME": str(xdg)},
            ),
            ["https://example.com/a"],
        )

    def test_implicit_wgetrc_bypasses_cache(self) -> None:
        home = Path(self.tmp.name) / "home-wgetrc"
        home.mkdir()
        (home / ".wgetrc").write_text("header = X-Test: 1\n", encoding="utf-8")
        self.assertEqual(
            self.run_wrapper(
                "wget",
                "-O",
                "/tmp/file",
                "https://example.com/a",
                env={**self.env, "HOME": str(home)},
            ),
            ["-O", "/tmp/file", "https://example.com/a"],
        )

    def test_system_wgetrc_with_header_or_auth_directives_bypasses(self) -> None:
        for content in (
            "header = Authorization: Bearer token\n",
            "http_user = user\nhttp_password = secret\n",
        ):
            with self.subTest(content=content):
                system_wgetrc = Path(self.tmp.name) / "system-wgetrc"
                system_wgetrc.write_text(content, encoding="utf-8")
                self.assertEqual(
                    self.run_wrapper(
                        "wget",
                        "-O",
                        "/tmp/file",
                        "https://example.com/a",
                        env={
                            **self.env,
                            "OPENSANDBOX_DOWNLOAD_SYSTEM_WGETRC": str(system_wgetrc),
                        },
                    ),
                    ["-O", "/tmp/file", "https://example.com/a"],
                )

    def test_system_wgetrc_with_benign_defaults_still_rewrites(self) -> None:
        system_wgetrc = Path(self.tmp.name) / "system-wgetrc-benign"
        system_wgetrc.write_text(
            "# system defaults\npassive_ftp = on\n", encoding="utf-8"
        )
        self.assertEqual(
            self.run_wrapper(
                "wget",
                "-O",
                "/tmp/file",
                "https://example.com/a",
                env={
                    **self.env,
                    "OPENSANDBOX_DOWNLOAD_SYSTEM_WGETRC": str(system_wgetrc),
                },
            ),
            [
                "-O",
                "/tmp/file",
                (
                    f"{SOURCE}/download/v1/https/"
                    f"{encoded('example.com')}/object/{encoded('/a')}"
                ),
            ],
        )

    def test_query_url_falls_back_to_original_command(self) -> None:
        self.assertEqual(
            self.run_wrapper("curl", "https://example.com/file?version=2"),
            ["https://example.com/file?version=2"],
        )
        self.assertEqual(
            self.run_wrapper("curl", "http://127.0.0.1:8080/artifact"),
            ["http://127.0.0.1:8080/artifact"],
        )
        self.assertEqual(
            self.run_wrapper("curl", "http://172.16.5.7/pkgs/foo.deb"),
            ["http://172.16.5.7/pkgs/foo.deb"],
        )
        self.assertEqual(
            self.run_wrapper("curl", "https://example.com/file[1-3].txt"),
            ["https://example.com/file[1-3].txt"],
        )

    def test_json_and_url_query_options_bypass_cache(self) -> None:
        self.assertEqual(
            self.run_wrapper(
                "curl",
                "--json",
                '{"token":"secret"}',
                "https://api.example/upload",
            ),
            ["--json", '{"token":"secret"}', "https://api.example/upload"],
        )
        self.assertEqual(
            self.run_wrapper(
                "curl",
                "--url-query",
                "token=secret",
                "https://example.com/object",
            ),
            ["--url-query", "token=secret", "https://example.com/object"],
        )

    def test_output_option_value_is_passthrough(self) -> None:
        self.assertEqual(
            self.run_wrapper(
                "curl",
                "-o",
                "https://example.com/out.tgz",
                "https://real.example/src.tgz",
            ),
            [
                "-o",
                "https://example.com/out.tgz",
                (
                    f"{SOURCE}/download/v1/https/"
                    f"{encoded('real.example')}/object/"
                    f"{encoded('/src.tgz')}"
                ),
            ],
        )
        self.assertEqual(
            self.run_wrapper("curl", "--output", "/tmp/out", "https://example.com/a"),
            [
                "--output",
                "/tmp/out",
                (
                    f"{SOURCE}/download/v1/https/"
                    f"{encoded('example.com')}/object/{encoded('/a')}"
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
