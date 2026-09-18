from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any


def emit_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": event,
        **fields,
    }
    logger.log(
        level,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
