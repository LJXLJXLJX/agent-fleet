from __future__ import annotations

import re
import shlex
from textwrap import dedent, indent

from swe_rebench_v2.instance_spec import InstanceSpec

# Test execution is adapted from the official SWE-rebench-V2 builder's
# scripts/eval.py at commit c71902a8cf8d2b725f63d51f199f4d3e56f68d2d:
# https://github.com/SWE-rebench/SWE-rebench-V2/blob/c71902a8cf8d2b725f63d51f199f4d3e56f68d2d/scripts/eval.py

_SHELL_CONTROL_TOKENS = ("&&", "||", ";", "|", ">", "<")


def _extract_patch_paths(patch: str) -> list[str]:
    preimage = [
        path
        for path in re.findall(r"^--- a/(.*)$", patch, re.MULTILINE)
        if path != "/dev/null"
    ]
    postimage = [
        path
        for path in re.findall(r"^\+\+\+ b/(.*)$", patch, re.MULTILINE)
        if path != "/dev/null"
    ]
    return list(dict.fromkeys(preimage + postimage))


def _extract_test_directives(patch: str) -> list[str]:
    paths = [
        path
        for path in re.findall(r"^\+\+\+ b/(.*)$", patch, re.MULTILINE)
        if path != "/dev/null"
    ]
    return [path for path in dict.fromkeys(paths) if path.endswith(".py")]


def _pytest_cmd_has_explicit_targets(test_cmd: str) -> bool:
    try:
        tokens = shlex.split(test_cmd)
    except ValueError:
        return False
    start_index = next(
        (
            index + 1
            for index, token in enumerate(tokens)
            if token == "pytest" or token.endswith("/pytest")
        ),
        None,
    )
    if start_index is None:
        return False
    return any(".py" in token for token in tokens[start_index:])


def _render_patch_cleanup(base_commit: str, all_paths: list[str]) -> str:
    quoted_paths = " ".join(shlex.quote(path) for path in all_paths)
    if not quoted_paths:
        return ":"
    return dedent(f"""\
        for path in {quoted_paths}; do
            git checkout {base_commit} -- "$path" 2>/dev/null || git rm -f -- "$path" 2>/dev/null || rm -rf -- "$path"
        done""")


def _render_patch_apply(test_patch: str) -> str:
    if not test_patch:
        return ":"
    patch_content = test_patch.rstrip("\n")
    return (
        "cat > /tmp/test_patch.diff << '__HARBOR_TEST_PATCH__'\n"
        + patch_content
        + "\n__HARBOR_TEST_PATCH__\n"
        "git apply -v --3way --recount --ignore-space-change "
        "--whitespace=nowarn /tmp/test_patch.diff"
    )


def _render_resolve_repo_dir(spec: InstanceSpec) -> str:
    project_dir = shlex.quote(spec.project_dir)
    return dedent(f"""\
        resolve_repo_dir() {{
            local repo_dir={project_dir}
            if [ -d "$repo_dir/.git" ]; then
                printf '%s\n' "$repo_dir"
                return 0
            fi
            printf 'Expected repository is missing: %s\n' "$repo_dir" >&2
            return 1
        }}""")


def _render_test_invocation(spec: InstanceSpec, test_directives: list[str]) -> str:
    commands = list(spec.test_commands)
    if len(commands) == 1 and "pytest" in commands[0].lower():
        test_cmd = commands[0]
        directive_string = " ".join(shlex.quote(item) for item in test_directives)
        if (
            directive_string
            and not any(operator in test_cmd for operator in _SHELL_CONTROL_TOKENS)
            and not _pytest_cmd_has_explicit_targets(test_cmd)
        ):
            test_cmd = f"{test_cmd} {directive_string}"
        if not any(operator in test_cmd for operator in _SHELL_CONTROL_TOKENS):
            test_cmd += " -v"
        commands = [test_cmd]

    command_block = "\n".join(commands)
    return "(\n" + indent(command_block, "    ") + "\n) 2>&1 || true"


def render_test_commands(spec: InstanceSpec) -> str:
    test_patch = spec.test_patch or ""
    all_paths = _extract_patch_paths(test_patch)
    test_directives = _extract_test_directives(test_patch)
    cleanup = _render_patch_cleanup(spec.base_commit, all_paths)

    parts = [
        _render_resolve_repo_dir(spec),
        "",
        'if ! REPO_DIR="$(resolve_repo_dir)"; then',
        "    rm -f /logs/verifier/reward.txt /logs/verifier/report.json",
        "    exit 127",
        "fi",
        'cd "$REPO_DIR"',
        "",
        cleanup,
        "",
        _render_patch_apply(test_patch),
        "",
        "exec 3>&1 4>&2",
        'exec > >(tee "$LOG_FILE") 2>&1',
        'tee_pid="$!"',
        "",
        "set +x",
        _render_test_invocation(spec, test_directives),
        "exec 1>&3 2>&4",
        'wait "$tee_pid"',
        "",
        "set +e",
        cleanup,
        "set -e",
    ]
    return "\n".join(parts)
