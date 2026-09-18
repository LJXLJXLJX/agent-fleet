"""AptProvider: the _ProbeMixin plus _WarmMixin."""

from __future__ import annotations

from .probe import _ProbeMixin
from .resolver import (
    CommandRunner,
    _run_container,
)
from .warm import _WarmMixin


class AptProvider(_ProbeMixin, _WarmMixin):
    manager = "apt"


    def __init__(self, command_runner: CommandRunner = _run_container):
        self.command_runner = command_runner
