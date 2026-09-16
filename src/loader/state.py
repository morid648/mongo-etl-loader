"""Watermark persistence for incremental SQL extraction."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class WatermarkStore:
    """Persists the last-seen watermark value for one source to a local JSON file."""

    def __init__(self, state_path: str | Path):
        self.path = Path(state_path)

    def get(self) -> datetime | None:
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        value = data.get("watermark")
        return datetime.fromisoformat(value) if value else None

    def set(self, value: datetime) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"watermark": value.isoformat()}), encoding="utf-8")
