# Adapted from the official SWE-rebench-V2 builder at commit
# c71902a8cf8d2b725f63d51f199f4d3e56f68d2d:
# https://github.com/SWE-rebench/SWE-rebench-V2/blob/c71902a8cf8d2b725f63d51f199f4d3e56f68d2d/lib/agent/swe_constants.py

from enum import Enum


class TestStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"
