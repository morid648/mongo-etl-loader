"""Structured JSON logging setup and the per-run summary object."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: str | None = None) -> None:
    resolved_level = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger("loader")
    root.setLevel(resolved_level)
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False


@dataclass
class RunSummary:
    source: str
    target: str
    mode: str
    started_at: str
    rows_read: int = 0
    rows_loaded: int = 0
    rows_rejected: int = 0
    ended_at: str | None = None
    duration_seconds: float | None = None
    reconciled: bool = True
    errors: list[str] = field(default_factory=list)

    @classmethod
    def start(cls, source: str, target: str, mode: str) -> "RunSummary":
        return cls(
            source=source,
            target=target,
            mode=mode,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

    def finish(self) -> "RunSummary":
        end = datetime.now(timezone.utc)
        self.ended_at = end.isoformat()
        start = datetime.fromisoformat(self.started_at)
        self.duration_seconds = (end - start).total_seconds()
        self.reconciled = self.rows_read == self.rows_loaded + self.rows_rejected
        return self

    def as_dict(self) -> dict:
        return asdict(self)

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2)
