from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from log_parsers import NAME_TO_PARSER

# Evaluation semantics are adapted from the official SWE-rebench-V2 builder's
# scripts/eval.py at commit c71902a8cf8d2b725f63d51f199f4d3e56f68d2d:
# https://github.com/SWE-rebench/SWE-rebench-V2/blob/c71902a8cf8d2b725f63d51f199f4d3e56f68d2d/scripts/eval.py

_TIMING_NORMALIZE_RES = [
    re.compile(r"\s*\[\s*\d+(?:\.\d+)?\s*(?:ms|s)\s*\]\s*$", re.IGNORECASE),
    re.compile(r"\s+in\s+\d+(?:\.\d+)?\s+(?:msec|sec)\b", re.IGNORECASE),
    re.compile(r"\s*\(\s*\d+(?:\.\d+)?\s*(?:ms|s)\s*\)\s*$", re.IGNORECASE),
]

CONFIG_PATH = Path("/tests/config.json")
REPORT_PATH = Path("/logs/verifier/report.json")
REWARD_PATH = Path("/logs/verifier/reward.txt")


def normalize_test_name(name: str) -> str:
    for pattern in _TIMING_NORMALIZE_RES:
        name = pattern.sub("", name)
    return name.strip()


def expected_tests(config: dict[str, object], field: str) -> set[str]:
    value = config.get(field) or []
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a list")
    return {normalize_test_name(item) for item in value if isinstance(item, str)}


def grade(config: dict[str, object], log_content: str) -> dict[str, object]:
    install_config = config.get("install_config")
    if not isinstance(install_config, dict):
        raise TypeError("install_config must be an object")
    parser_name = install_config.get("log_parser")
    if not isinstance(parser_name, str):
        raise TypeError("install_config.log_parser must be a string")
    parser = NAME_TO_PARSER.get(parser_name)
    if parser is None:
        raise ValueError(f"Unsupported SWE-rebench-V2 log parser: {parser_name!r}")
    parsed = {
        normalize_test_name(name): status
        for name, status in parser(log_content).items()
    }
    passed_actual = {name for name, status in parsed.items() if status == "PASSED"}
    fail_to_pass = expected_tests(config, "FAIL_TO_PASS")
    pass_to_pass = expected_tests(config, "PASS_TO_PASS")
    passed_expected = fail_to_pass | pass_to_pass

    # Match scripts/eval.py from the pinned V2 builder: the parsed PASSED set
    # must exactly equal FAIL_TO_PASS + PASS_TO_PASS after timing normalization.
    resolved = passed_actual == passed_expected
    return {
        "parser": parser_name,
        "fail_to_pass_expected": sorted(fail_to_pass),
        "pass_to_pass_expected": sorted(pass_to_pass),
        "passed_expected": sorted(passed_expected),
        "passed_actual": sorted(passed_actual),
        "missing_expected": sorted(passed_expected - passed_actual),
        "unexpected_passed": sorted(passed_actual - passed_expected),
        "resolved": resolved,
    }


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    log_content = Path(os.environ["LOG_FILE"]).read_text(
        encoding="utf-8", errors="replace"
    )
    try:
        report = grade(config, log_content)
    except (TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    resolved = bool(report["resolved"])
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    REWARD_PATH.write_text(f"{int(resolved)}\n", encoding="utf-8")
    print("SWE-rebench-V2 results:")
    print("PASSED" if resolved else "FAILED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
