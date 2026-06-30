from __future__ import annotations
import json
import sys
from typing import Any


def _emit(level: str, event: str, fields: dict[str, Any]) -> None:
    record = {"level": level, "event": event}
    for k, v in fields.items():
        record[k] = v if isinstance(v, (str, int, float, bool)) or v is None else str(v)
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def log(event: str, **fields: Any) -> None:
    _emit("info", event, fields)


def log_warn(event: str, **fields: Any) -> None:
    _emit("warn", event, fields)


def log_error(event: str, **fields: Any) -> None:
    _emit("error", event, fields)
