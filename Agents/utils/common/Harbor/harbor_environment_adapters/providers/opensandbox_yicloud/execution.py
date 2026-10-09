"""YiCloud execution: extracted from the legacy Harbor provider without behavior changes."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import os
import re
import shlex
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path
from typing import Any

import requests
from harbor.environments.base import ExecResult

from .config import _positive_int


@cache
def _native_command_pool() -> ThreadPoolExecutor:
    # Long agent commands must not exhaust the default pool used by file I/O
    # and control-plane calls. All trials share this bounded, lazy pool.
    return ThreadPoolExecutor(
        max_workers=int(os.environ["HARBOR_N_CONCURRENT"]),
        thread_name_prefix="sandbox-command",
    )


EXIT_MARKER = "__HARBOR_YICLOUD_OPENSANDBOX_EXIT_CODE__="


DEFAULT_EXECD_REQUEST_ATTEMPTS = 4


EXECD_DETACHED_COMMAND_MIN_TIMEOUT_SEC = 300


EXECD_DETACHED_POLL_INTERVAL_SEC = 5


DETACHED_PENDING_MARKER = "__HARBOR_YICLOUD_DETACHED_PENDING__"


VERIFIER_PATH_PREPEND_ENV = "HARBOR_VERIFIER_PATH_PREPEND"


VERIFIER_RUNTIME_BUNDLE_ID_ENV = "HARBOR_VERIFIER_RUNTIME_BUNDLE_ID"


VERIFIER_RUNTIME_BUNDLE_ARCHIVE_ENV = "HARBOR_VERIFIER_RUNTIME_BUNDLE_ARCHIVE"


VERIFIER_RUNTIME_BUNDLE_ROOT_ENV = "HARBOR_VERIFIER_RUNTIME_BUNDLE_ROOT"


def _execd_request_attempts() -> int:
    return _positive_int(
        os.environ.get(
            "YICLOUD_EXECD_REQUEST_ATTEMPTS",
            str(DEFAULT_EXECD_REQUEST_ATTEMPTS),
        ),
        "YICLOUD_EXECD_REQUEST_ATTEMPTS",
    )


def _retryable_execd_error(error: requests.RequestException) -> bool:
    if isinstance(error, requests.HTTPError):
        return error.response is not None and error.response.status_code in {
            502,
            503,
            504,
        }
    return isinstance(
        error,
        (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    )


def _prepare_exec_runtime(
    command: str,
    env: dict[str, str] | None,
) -> tuple[str, dict[str, str] | None]:
    """Expand verifier-only runtime settings inside the Sandbox shell.

    Execd environment values are literal and cannot express ``prefix:$PATH``
    without replacing the image's configured PATH. Remove the private control
    variables from the payload and render the expansion in the command shell
    instead. A selected runtime bundle is an opaque filesystem bundle: the
    provider only materializes it, runs its standard self-check entrypoint,
    and prepends the bundle's ``bin`` directory. Runtime-specific activation
    belongs in wrappers shipped by the bundle itself.
    """
    if env is None:
        return command, None
    effective_env = dict(env)
    path_prepend = effective_env.pop(VERIFIER_PATH_PREPEND_ENV, "").strip()
    bundle_id = effective_env.pop(VERIFIER_RUNTIME_BUNDLE_ID_ENV, "").strip()
    bundle_archive = effective_env.pop(VERIFIER_RUNTIME_BUNDLE_ARCHIVE_ENV, "").strip()
    bundle_root = effective_env.pop(VERIFIER_RUNTIME_BUNDLE_ROOT_ENV, "").strip()
    bundle_settings = (bundle_id, bundle_archive, bundle_root)

    if any(bundle_settings) and not all(bundle_settings):
        raise ValueError(
            "verifier runtime bundle id, archive, and root must be configured together"
        )
    if bundle_archive and (
        not Path(bundle_archive).is_absolute()
        or not Path(bundle_root).is_absolute()
        or Path(bundle_root) == Path("/")
    ):
        raise ValueError("verifier runtime bundle paths must be safe absolute paths")
    if not path_prepend and not bundle_archive:
        return command, effective_env

    bootstrap = ["set -e"]
    if bundle_archive:
        archive = shlex.quote(bundle_archive)
        root = shlex.quote(bundle_root)
        parent = shlex.quote(str(Path(bundle_root).parent))
        check = f"{root}/bin/harbor-verifier-bundle-check"
        bootstrap_log = "/logs/verifier/runtime-bootstrap.log"
        ready_message = shlex.quote(f"verifier runtime bundle ready: {bundle_id}")
        bootstrap.extend(
            [
                (
                    "harbor_verifier_runtime_fail() { "
                    "message=$1; printf '%s\\n' \"$message\" >&2; "
                    f"printf '%s\\n' \"$message\" >> {bootstrap_log} "
                    "2>/dev/null || true; exit 127; }"
                ),
                (
                    f"[ -f {archive} ] || harbor_verifier_runtime_fail "
                    "'verifier runtime bundle archive is missing'"
                ),
                (
                    "command -v tar >/dev/null 2>&1 || "
                    "harbor_verifier_runtime_fail "
                    "'tar is required to materialize verifier runtime bundle'"
                ),
                (
                    f"rm -rf {root} || harbor_verifier_runtime_fail "
                    "'failed to clear verifier runtime bundle root'"
                ),
                (
                    f"mkdir -p {parent} || harbor_verifier_runtime_fail "
                    "'failed to create verifier runtime bundle parent'"
                ),
                (
                    f"tar -xzf {archive} -C {parent} || "
                    "harbor_verifier_runtime_fail "
                    "'failed to extract verifier runtime bundle'"
                ),
                (
                    f"[ -x {check} ] || harbor_verifier_runtime_fail "
                    "'verifier runtime bundle is missing its self-check'"
                ),
                (
                    f"if runtime_check_output=$({check} 2>&1); then "
                    f"printf '%s\\n' \"$runtime_check_output\" >> {bootstrap_log} 2>/dev/null || true; "
                    "else printf '%s\\n' \"$runtime_check_output\" >&2; "
                    f"printf '%s\\n' \"$runtime_check_output\" >> {bootstrap_log} 2>/dev/null || true; "
                    "harbor_verifier_runtime_fail 'verifier runtime bundle failed its self-check'; fi"
                ),
                (
                    f"printf '%s\\n' {ready_message} "
                    f">> {bootstrap_log} 2>/dev/null || true"
                ),
            ]
        )
    if path_prepend:
        bootstrap.append(f'export PATH={shlex.quote(path_prepend)}:"$PATH"')
    bootstrap.append("set +e")
    return "\n".join([*bootstrap, command]), effective_env


def _parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[len("data:") :].strip()
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            events.append({"type": "unparsed", "text": line})
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


class YiCloudExecutionMixin:
    """Internal execution methods sharing the adapter instance state."""

    def _cleanup_remote_exec_paths_sync(self, *paths: str) -> None:
        if not paths or not self._command_url or not self._access_token:
            return
        command = "rm -rf " + " ".join(shlex.quote(path) for path in paths)
        payload = {
            "command": command,
            "background": False,
            "timeout": 30_000,
            "uid": 0,
        }
        body = json.dumps(payload, separators=(",", ":"))
        try:
            session = requests.Session()
            session.trust_env = False
            response = session.post(
                self._command_url,
                headers=self._signed_headers(body),
                data=body,
                timeout=30,
            )
            response.raise_for_status()
        except Exception as error:  # noqa: BLE001 - cleanup is best effort
            self.logger.warning(
                "YiCloud failed to remove remote exec temporary files: %s",
                error,
            )

    def _run_command_direct_sync(
        self,
        command: str,
        cwd: str | None,
        env: dict[str, str] | None,
        timeout_sec: int | None,
        uid: int | None = None,
    ) -> ExecResult:
        if not self._command_url or not self._access_token:
            raise RuntimeError("YiCloud Sandbox is not running")
        command_id = uuid.uuid4().hex
        prefix = f"/tmp/harbor-execd-{command_id}"
        stdout_path = f"{prefix}.stdout"
        stderr_path = f"{prefix}.stderr"
        status_path = f"{prefix}.status"
        lock_path = f"{prefix}.lock"
        lock_dir = f"{lock_path}.d"
        cleanup_paths = (
            lock_path,
            lock_dir,
            stdout_path,
            stderr_path,
            status_path,
            f"{status_path}.tmp",
        )
        wrapped = (
            "set +e\n"
            f"harbor_lock_dir={shlex.quote(lock_dir)}\n"
            "if command -v flock >/dev/null 2>&1; then\n"
            f"  exec 9>{shlex.quote(lock_path)}\n"
            "  flock -x 9\n"
            "else\n"
            '  while ! mkdir "$harbor_lock_dir" 2>/dev/null; do\n'
            f"    [ -f {shlex.quote(status_path)} ] && break\n"
            '    if [ -s "$harbor_lock_dir/pid" ]; then\n'
            '      harbor_lock_pid=$(cat "$harbor_lock_dir/pid")\n'
            '      if ! kill -0 "$harbor_lock_pid" 2>/dev/null; then\n'
            '        rm -rf "$harbor_lock_dir"\n'
            "        continue\n"
            "      fi\n"
            "    fi\n"
            "    sleep 1\n"
            "  done\n"
            f"  if [ ! -f {shlex.quote(status_path)} ] "
            '&& [ -d "$harbor_lock_dir" ]; then\n'
            '    printf \'%s\\n\' "$$" > "$harbor_lock_dir/pid"\n'
            "    trap 'rm -rf \"$harbor_lock_dir\"' EXIT\n"
            "    trap 'exit 129' HUP\n"
            "    trap 'exit 130' INT\n"
            "    trap 'exit 143' TERM\n"
            "  fi\n"
            "fi\n"
            f"if [ ! -f {shlex.quote(status_path)} ]; then\n"
            "  (\n"
            f"{command}\n"
            f"  ) > {shlex.quote(stdout_path)} 2> {shlex.quote(stderr_path)}\n"
            "  harbor_rc=$?\n"
            f"  printf '%s\\n' \"$harbor_rc\" > {shlex.quote(status_path)}.tmp\n"
            f"  mv -f {shlex.quote(status_path)}.tmp {shlex.quote(status_path)}\n"
            "fi\n"
            f"cat {shlex.quote(stdout_path)} 2>/dev/null\n"
            f"cat {shlex.quote(stderr_path)} >&2 2>/dev/null\n"
            f"harbor_rc=$(cat {shlex.quote(status_path)})\n"
            f"printf '\\n{EXIT_MARKER}%s\\n' \"$harbor_rc\"\n"
            'exit "$harbor_rc"\n'
        )
        effective_timeout_sec = timeout_sec or 3600
        payload = {
            "command": wrapped,
            "background": False,
            "timeout": effective_timeout_sec * 1000,
            "envs": env,
        }
        if cwd is not None:
            payload["cwd"] = cwd
        if uid is not None:
            payload["uid"] = uid
        body = json.dumps(payload, separators=(",", ":"))
        request_timeout_sec = effective_timeout_sec + 60
        attempts = _execd_request_attempts()
        for attempt in range(attempts):
            try:
                session = requests.Session()
                session.trust_env = False
                response = session.post(
                    self._command_url,
                    headers=self._signed_headers(body),
                    data=body,
                    timeout=request_timeout_sec,
                )
                response.raise_for_status()
                response_text = response.text
                break
            except requests.RequestException as error:
                if not _retryable_execd_error(error) or attempt + 1 == attempts:
                    raise
                delay = min(2**attempt, 5)
                self.logger.warning(
                    "YiCloud execd command request failed transiently; "
                    "retrying in %ss (attempt %s/%s): %s",
                    delay,
                    attempt + 1,
                    attempts,
                    error,
                )
                time.sleep(delay)
        else:
            raise AssertionError("unreachable")
        try:
            events = _parse_sse(response_text)
            stdout = "".join(
                str(event.get("text", ""))
                for event in events
                if event.get("type") == "stdout"
            )
            stderr = "".join(
                str(event.get("text", ""))
                for event in events
                if event.get("type") == "stderr"
            )
            matches = re.findall(rf"{re.escape(EXIT_MARKER)}(\d+)", stdout)
            return_code = int(matches[-1]) if matches else 1
            stdout = re.sub(rf"\n?{re.escape(EXIT_MARKER)}\d+\n?", "", stdout)
            return ExecResult(
                stdout=stdout,
                stderr=stderr,
                return_code=return_code,
            )
        finally:
            self._cleanup_remote_exec_paths_sync(*cleanup_paths)

    def _run_command_detached_sync(
        self,
        command: str,
        cwd: str | None,
        env: dict[str, str] | None,
        timeout_sec: int,
        uid: int | None = None,
    ) -> ExecResult:
        command_id = uuid.uuid4().hex
        prefix = f"/tmp/harbor-detached-{command_id}"
        script_path = f"{prefix}.sh"
        stdout_path = f"{prefix}.stdout"
        stderr_path = f"{prefix}.stderr"
        status_path = f"{prefix}.status"
        pid_path = f"{prefix}.pid"
        launch_dir = f"{prefix}.launch"
        cleanup_paths = (
            script_path,
            stdout_path,
            stderr_path,
            status_path,
            f"{status_path}.tmp",
            pid_path,
            f"{pid_path}.tmp",
            launch_dir,
        )
        script = (
            "#!/bin/sh\n"
            "set +e\n"
            f"printf '%s\\n' \"$$\" > {shlex.quote(pid_path)}.tmp\n"
            f"mv -f {shlex.quote(pid_path)}.tmp {shlex.quote(pid_path)}\n"
            "(\n"
            f"{command}\n"
            f") > {shlex.quote(stdout_path)} 2> {shlex.quote(stderr_path)}\n"
            "harbor_rc=$?\n"
            f"printf '%s\\n' \"$harbor_rc\" > {shlex.quote(status_path)}.tmp\n"
            f"mv -f {shlex.quote(status_path)}.tmp {shlex.quote(status_path)}\n"
            'exit "$harbor_rc"\n'
        )
        encoded_script = base64.b64encode(script.encode()).decode()
        launch_command = (
            f"harbor_pid_path={shlex.quote(pid_path)}\n"
            f"harbor_launch_dir={shlex.quote(launch_dir)}\n"
            "harbor_detached_started() {\n"
            f"  [ -f {shlex.quote(status_path)} ] && return 0\n"
            '  [ -s "$harbor_pid_path" ] || return 1\n'
            '  harbor_pid=$(cat "$harbor_pid_path")\n'
            "  case \"$harbor_pid\" in ''|*[!0-9]*) return 1 ;; esac\n"
            '  kill -0 "$harbor_pid" 2>/dev/null\n'
            "}\n"
            'if [ -d "$harbor_launch_dir" ] '
            "&& ! harbor_detached_started; then\n"
            "  for harbor_wait in 1 2 3 4 5; do\n"
            "    sleep 1\n"
            "    harbor_detached_started && break\n"
            "  done\n"
            "fi\n"
            "if ! harbor_detached_started; then\n"
            '  rm -rf "$harbor_launch_dir"\n'
            '  mkdir "$harbor_launch_dir" || exit 75\n'
            f"  printf %s {shlex.quote(encoded_script)} | base64 -d > "
            f"{shlex.quote(script_path)} && chmod 700 {shlex.quote(script_path)} "
            '|| { rm -rf "$harbor_launch_dir"; exit 75; }\n'
            "  if command -v bash >/dev/null 2>&1; then\n"
            "    harbor_shell=$(command -v bash)\n"
            "  else\n"
            "    harbor_shell=/bin/sh\n"
            "  fi\n"
            "  if command -v setsid >/dev/null 2>&1; then\n"
            f'    nohup setsid "$harbor_shell" {shlex.quote(script_path)} '
            "</dev/null >/dev/null 2>&1 9>&- &\n"
            "  else\n"
            f'    nohup "$harbor_shell" {shlex.quote(script_path)} '
            "</dev/null >/dev/null 2>&1 9>&- &\n"
            "  fi\n"
            "  for harbor_wait in 1 2 3 4 5; do\n"
            "    harbor_detached_started && break\n"
            "    sleep 1\n"
            "  done\n"
            '  rm -rf "$harbor_launch_dir"\n'
            "  harbor_detached_started || exit 75\n"
            "fi\n"
        )
        launched = self._run_command_direct_sync(
            launch_command,
            cwd,
            env,
            60,
            uid,
        )
        if launched.return_code != 0:
            self._cleanup_remote_exec_paths_sync(*cleanup_paths)
            return launched

        poll_command = (
            f"if [ ! -f {shlex.quote(status_path)} ]; then\n"
            f"  if [ -f {shlex.quote(pid_path)} ] "
            f'&& ! kill -0 "$(cat {shlex.quote(pid_path)})" 2>/dev/null; then\n'
            "    printf 'detached command exited without status\\n' >&2\n"
            "    exit 125\n"
            "  fi\n"
            f"  printf %s {shlex.quote(DETACHED_PENDING_MARKER)}\n"
            "  exit 0\n"
            "fi\n"
            f"cat {shlex.quote(stdout_path)} 2>/dev/null\n"
            f"cat {shlex.quote(stderr_path)} >&2 2>/dev/null\n"
            f"harbor_rc=$(cat {shlex.quote(status_path)})\n"
            'exit "$harbor_rc"\n'
        )
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            result = self._run_command_direct_sync(
                poll_command,
                "/",
                {},
                60,
                uid,
            )
            if result.stdout != DETACHED_PENDING_MARKER:
                self._cleanup_remote_exec_paths_sync(*cleanup_paths)
                return result
            time.sleep(
                min(
                    EXECD_DETACHED_POLL_INTERVAL_SEC,
                    max(deadline - time.monotonic(), 0),
                )
            )

        terminate_command = (
            f"if [ -s {shlex.quote(pid_path)} ]; then\n"
            f"  harbor_pid=$(cat {shlex.quote(pid_path)})\n"
            '  if kill -TERM -- "-$harbor_pid" 2>/dev/null; then\n'
            "    sleep 2\n"
            '    kill -KILL -- "-$harbor_pid" 2>/dev/null || true\n'
            "  else\n"
            '    harbor_process_tree="$harbor_pid"\n'
            '    harbor_frontier="$harbor_pid"\n'
            '    while [ -n "$harbor_frontier" ]; do\n'
            '      harbor_next=""\n'
            "      for harbor_parent in $harbor_frontier; do\n"
            "        for harbor_status in /proc/[0-9]*/status; do\n"
            '          [ -r "$harbor_status" ] || continue\n'
            "          harbor_child=${harbor_status#/proc/}\n"
            "          harbor_child=${harbor_child%/status}\n"
            '          harbor_ppid=""\n'
            "          while IFS=: read -r harbor_key harbor_value; do\n"
            '            if [ "$harbor_key" = PPid ]; then\n'
            '              harbor_ppid="${harbor_value#"${harbor_value%%[![:space:]]*}"}"\n'
            "              break\n"
            "            fi\n"
            '          done < "$harbor_status"\n'
            '          if [ "$harbor_ppid" = "$harbor_parent" ]; then\n'
            '            harbor_next="$harbor_next $harbor_child"\n'
            "          fi\n"
            "        done\n"
            "      done\n"
            '      [ -n "$harbor_next" ] || break\n'
            '      harbor_process_tree="$harbor_next $harbor_process_tree"\n'
            '      harbor_frontier="$harbor_next"\n'
            "    done\n"
            "    for harbor_tree_pid in $harbor_process_tree; do\n"
            '      kill -TERM "$harbor_tree_pid" 2>/dev/null || true\n'
            "    done\n"
            "    sleep 2\n"
            "    for harbor_tree_pid in $harbor_process_tree; do\n"
            '      kill -KILL "$harbor_tree_pid" 2>/dev/null || true\n'
            "    done\n"
            "  fi\n"
            "fi\n"
        )
        self._run_command_direct_sync(
            terminate_command,
            "/",
            {},
            60,
            uid,
        )
        self._cleanup_remote_exec_paths_sync(*cleanup_paths)
        return ExecResult(
            stdout="",
            stderr=f"command timed out after {timeout_sec}s",
            return_code=124,
        )

    def _run_command_sync(
        self,
        command: str,
        cwd: str | None,
        env: dict[str, str] | None,
        timeout_sec: int | None,
        uid: int | None = None,
    ) -> ExecResult:
        if (
            timeout_sec is not None
            and timeout_sec > EXECD_DETACHED_COMMAND_MIN_TIMEOUT_SEC
        ):
            return self._run_command_detached_sync(
                command,
                cwd,
                env,
                timeout_sec,
                uid,
            )
        return self._run_command_direct_sync(
            command,
            cwd,
            env,
            timeout_sec,
            uid,
        )

    def _resolve_exec_uid(self, user: str | int | None) -> int | None:
        if user is None:
            return None
        if isinstance(user, bool):
            raise TypeError(f"invalid exec user: {user!r}")
        if isinstance(user, int):
            if user < 0:
                raise ValueError(f"exec uid must be non-negative: {user}")
            return user
        if user == "root":
            return 0
        if user.isdigit():
            return int(user)
        self.logger.debug(
            "YiCloud execd only supports numeric UIDs (or 'root'); "
            "got username %r, using the sandbox default user",
            user,
        )
        return None

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        uid = self._resolve_exec_uid(self._resolve_user(user))
        effective_cwd = cwd or self.task_env_config.workdir
        command, effective_env = _prepare_exec_runtime(
            command,
            self._merge_env(env),
        )
        args = (command, effective_cwd, effective_env, timeout_sec, uid)
        if os.environ.get("HARBOR_NATIVE_CONCURRENCY") == "1":
            result = await asyncio.get_running_loop().run_in_executor(
                _native_command_pool(),
                contextvars.copy_context().run,
                self._run_command_sync,
                *args,
            )
        else:
            result = await asyncio.to_thread(self._run_command_sync, *args)
        callback = self._output_callback()
        if callback is not None:
            if result.stdout:
                await callback(result.stdout, "stdout")
            if result.stderr:
                await callback(result.stderr, "stderr")
        return result
