"""Recent-failure recording mixin."""

from __future__ import annotations

import hashlib
import logging
import time

from ..fetcher import FetchError
from ...core.logging_utils import emit_event
from ._common import _HIT_RATE_LOGGER


class _FailureMixin:
    def _record_failure(
        self, source_name: str, url: str, error: FetchError
    ) -> None:
        document: dict[str, object] = {
            "timestamp": time.time(),
            "source": source_name,
            "url_key": hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
            "status": error.status,
            "stage": error.stage,
            "error": str(error),
            "attempts": [attempt.document() for attempt in error.attempts],
        }
        with self._failure_guard:
            self._recent_failures.append(document)
        emit_event(
            _HIT_RATE_LOGGER,
            "cache_fetch_failed",
            level=logging.ERROR,
            **{key: value for key, value in document.items() if key != "timestamp"},
        )

    def recent_failures(self, limit: int = 20) -> list[dict[str, object]]:
        if limit < 1 or limit > 100:
            raise ValueError("failure limit must be in 1..100")
        with self._failure_guard:
            return list(self._recent_failures)[-limit:][::-1]
