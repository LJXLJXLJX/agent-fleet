import asyncio
import base64
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


HARBOR_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = HARBOR_DIR / "yicloud_opensandbox.py"
sys.path.insert(0, str(HARBOR_DIR))


def install_harbor_stubs() -> None:
    harbor = types.ModuleType("harbor")
    environments = types.ModuleType("harbor.environments")
    base = types.ModuleType("harbor.environments.base")
    capabilities = types.ModuleType("harbor.environments.capabilities")

    class BaseEnvironment:
        pass

    class ExecResult:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Capability:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    base.BaseEnvironment = BaseEnvironment
    base.ExecResult = ExecResult
    capabilities.EnvironmentCapabilities = Capability
    capabilities.EnvironmentResourceCapabilities = Capability
    sys.modules.update(
        {
            "harbor": harbor,
            "harbor.environments": environments,
            "harbor.environments.base": base,
            "harbor.environments.capabilities": capabilities,
        }
    )


install_harbor_stubs()
spec = importlib.util.spec_from_file_location("yicloud_opensandbox", MODULE_PATH)
assert spec is not None and spec.loader is not None
yicloud_opensandbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(yicloud_opensandbox)


class Request:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeSandbox:
    def __init__(self, environments):
        self.environments = environments
        self.models = SimpleNamespace(
            ListSandboxEnvironmentsReq=Request,
            GetSandboxEnvironmentReq=Request,
        )

    def list_sandbox_environments(self, _context, _request):
        return SimpleNamespace(Items=self.environments)

    def get_sandbox_environment(self, _context, request):
        return next(
            (
                item
                for item in self.environments
                if item.Id == request.EnvironmentId
            ),
            SimpleNamespace(Id="", Name=""),
        )


class YiCloudOpenSandboxTest(unittest.TestCase):
    def test_s3_download_url_is_passed_as_environment_not_command_text(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._s3_download_timeout_sec = 1800
        instance._s3_downloader_ready = True
        instance.exec = AsyncMock(
            return_value=SimpleNamespace(return_code=0, stdout="", stderr="")
        )
        artifact = yicloud_opensandbox.S3UploadArtifact(
            kind="file",
            logical_digest="a" * 64,
            payload_digest="b" * 64,
            payload_size=12,
            compression="none",
            local_payload_path="/cache/payload",
            object_key="objects/payload",
            object_uri="s3://cache/objects/payload",
            signed_url="http://ceph.example/cache/object?secret=signature",
        )

        asyncio.run(instance._materialize_s3_file(artifact, "/tmp/agent.tgz"))

        call = instance.exec.await_args
        self.assertNotIn("secret=signature", call.args[0])
        self.assertEqual(
            call.kwargs["env"]["HARBOR_S3_URL"],
            artifact.signed_url,
        )

    def test_s3_bootstrap_is_uploaded_once_only_when_native_tools_are_missing(
        self,
    ) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._s3_download_timeout_sec = 1800
        instance._s3_downloader_ready = False
        instance._s3_downloader_lock = None
        instance._sandbox_id = "sbx-test"
        instance._access_token = "sandbox-token"
        instance.logger = Mock()
        instance.exec = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    return_code=0, stdout="bootstrap", stderr=""
                ),
                SimpleNamespace(return_code=0, stdout="", stderr=""),
            ]
        )
        uploaded = {}

        def capture_upload(source, target_path, _upload_url):
            uploaded["count"] = uploaded.get("count", 0) + 1
            uploaded["payload"] = source.read_bytes()
            uploaded["target_path"] = target_path

        instance._upload_file_fast_sync = capture_upload

        async def ensure_twice() -> None:
            signed_url = "http://ceph.example/cache/object?signature=test"
            await instance._ensure_s3_downloader(signed_url)
            await instance._ensure_s3_downloader(signed_url)

        async def run_inline(function, *args):
            return function(*args)

        with patch.object(
            yicloud_opensandbox.asyncio,
            "to_thread",
            side_effect=run_inline,
        ):
            asyncio.run(ensure_twice())

        self.assertEqual(uploaded["count"], 1)
        self.assertEqual(
            uploaded["target_path"],
            yicloud_opensandbox.S3_HTTP_BOOTSTRAP_PATH,
        )
        self.assertIn(b"/dev/tcp/", uploaded["payload"])
        self.assertLess(len(uploaded["payload"]), 2048)
        self.assertIn(
            yicloud_opensandbox.S3_HTTP_BOOTSTRAP_PATH,
            instance._s3_download_command(
                SimpleNamespace(payload_size=12, payload_digest="b" * 64),
                "/tmp/payload",
            ),
        )

    def test_environment_and_image_bindings_are_enforced(self) -> None:
        sandbox = FakeSandbox(
            [
                SimpleNamespace(Id="env-other", Name="other"),
                SimpleNamespace(
                    Id="env-dedicated",
                    Name="jianxiao-sandbox-0",
                ),
            ]
        )
        environment_id = yicloud_opensandbox._environment_id_by_exact_name(
            sandbox,
            "fdj-infra",
            "jianxiao-sandbox-0",
        )
        self.assertEqual(environment_id, "env-dedicated")

        running = SimpleNamespace(
            EnvironmentId=environment_id,
            Image=SimpleNamespace(Ref="project/task:image"),
        )
        yicloud_opensandbox._validate_sandbox_binding(
            running,
            "env-dedicated",
            "project/task:image",
        )
        running.EnvironmentId = "env-other"
        with self.assertRaisesRegex(RuntimeError, "environment binding mismatch"):
            yicloud_opensandbox._validate_sandbox_binding(
                running,
                "env-dedicated",
                "project/task:image",
            )

    def test_root_exec_payload_uses_uid_zero(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._command_url = "https://sandbox.example/command"
        instance._access_token = "test-token"
        instance._signed_headers = Mock(return_value={})
        captured = {}

        class StopAfterCapture(RuntimeError):
            pass

        class FakeSession:
            trust_env = True

            def post(self, _url, *, headers, data, timeout):
                captured["payload"] = json.loads(data)
                raise StopAfterCapture

        with (
            patch.object(
                yicloud_opensandbox.requests,
                "Session",
                return_value=FakeSession(),
            ),
            self.assertRaises(StopAfterCapture),
        ):
            instance._run_command_sync(
                "id -u",
                "/",
                {},
                30,
                uid=instance._resolve_exec_uid("root"),
            )

        self.assertEqual(captured["payload"]["uid"], 0)

    def test_fast_upload_keeps_access_token_out_of_argv(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._sandbox_id = "sbx-test"
        instance._access_token = "secret-sandbox-token"
        instance.logger = Mock()
        captured = {}

        def fake_run(command, **_kwargs):
            captured["command"] = command
            header_path = Path(command[command.index("--header") + 1][1:])
            captured["headers"] = header_path.read_text(encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="200", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "agent.tgz"
            source.write_bytes(b"agent-package")
            with patch.object(
                yicloud_opensandbox.subprocess,
                "run",
                side_effect=fake_run,
            ):
                instance._upload_file_fast_sync(
                    source,
                    "/opt/tb-opik/agent.tgz",
                    instance._fast_upload_url(),
                )

        self.assertNotIn(
            "secret-sandbox-token",
            " ".join(captured["command"]),
        )
        self.assertIn(
            "X-Sandbox-Access-Token: secret-sandbox-token",
            captured["headers"],
        )

    def test_execd_upload_uses_binary_multipart_metadata(self) -> None:
        instance = object.__new__(
            yicloud_opensandbox.YiCloudOpenSandboxEnvironment
        )
        instance._command_url = (
            "https://gate.example/sandbox-connect/v1/sandboxes/sbx-test/"
            "proxy/44772/command"
        )
        instance._request_timeout_sec = 180
        instance._signed_headers = Mock(
            return_value={
                "X-OGW-SIGN": "signed",
                "X-Sandbox-Access-Token": "token",
            }
        )
        sent = {}

        class FakeResponse:
            ok = True
            status_code = 200
            text = ""

        class FakeSession:
            trust_env = True

            def prepare_request(self, request):
                return (
                    yicloud_opensandbox.requests.sessions.Session()
                    .prepare_request(request)
                )

            def send(self, prepared, timeout):
                sent["prepared"] = prepared
                sent["timeout"] = timeout
                return FakeResponse()

        with patch.object(
            yicloud_opensandbox.requests,
            "Session",
            FakeSession,
        ):
            instance._upload_chunk_sync(
                b"\x00\xffagent-package",
                "/tmp/harbor-upload.chunk",
                "agent.tgz",
            )

        prepared = sent["prepared"]
        self.assertTrue(prepared.url.endswith("/files/upload"))
        self.assertIn(
            base64.b64encode(b"\x00\xffagent-package"),
            prepared.body,
        )
        self.assertIn(
            b'name="metadata"; filename="metadata.json"',
            prepared.body,
        )
        self.assertIn(b'"mode":600', prepared.body)
        self.assertEqual(prepared.headers["X-OGW-SIGN"], "signed")


if __name__ == "__main__":
    unittest.main()
