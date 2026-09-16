"""CSV extraction: single file or glob over a directory, with a processed-file manifest."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from loader.config import CsvSourceConfig, OnMalformedRow

logger = logging.getLogger("loader.csv_extractor")


@dataclass
class ExtractResult:
    rows: list[dict]
    row_numbers: list[int] = field(default_factory=list)
    malformed_count: int = 0
    malformed_reasons: list[str] = field(default_factory=list)


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class ProcessedFilesManifest:
    """Tracks filename + content hash + timestamp of files already loaded."""

    def __init__(self, manifest_path: str | Path):
        self.path = Path(manifest_path)
        self._entries: dict[str, dict] = {}
        if self.path.exists():
            self._entries = json.loads(self.path.read_text(encoding="utf-8"))

    def is_processed(self, file_path: Path) -> bool:
        entry = self._entries.get(str(file_path.name))
        if entry is None:
            return False
        return entry.get("hash") == _file_hash(file_path)

    def mark_processed(self, file_path: Path) -> None:
        import datetime

        self._entries[file_path.name] = {
            "hash": _file_hash(file_path),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._entries, indent=2), encoding="utf-8")


def _iter_rows_in_file(file_path: Path, config: CsvSourceConfig) -> ExtractResult:
    rows: list[dict] = []
    row_numbers: list[int] = []
    malformed_reasons: list[str] = []

    with file_path.open("r", encoding=config.encoding, newline="") as f:
        reader = csv.reader(f, delimiter=config.delimiter)
        header: list[str] | None = None

        if config.has_header:
            header = next(reader, None)
            if header is None:
                return ExtractResult(rows=[], malformed_count=0)

        for line_no, raw_row in enumerate(reader, start=2 if config.has_header else 1):
            if not raw_row:
                continue

            if header is not None and len(raw_row) != len(header):
                reason = (
                    f"{file_path.name} row {line_no}: expected {len(header)} columns, "
                    f"got {len(raw_row)}"
                )
                if config.on_malformed_row == OnMalformedRow.FAIL:
                    raise ValueError(reason)
                logger.warning(reason)
                malformed_reasons.append(reason)
                continue

            if header is not None:
                rows.append(dict(zip(header, raw_row)))
            else:
                rows.append({str(i): v for i, v in enumerate(raw_row)})
            row_numbers.append(line_no)

    return ExtractResult(
        rows=rows,
        row_numbers=row_numbers,
        malformed_count=len(malformed_reasons),
        malformed_reasons=malformed_reasons,
    )


def _discover_files(config: CsvSourceConfig) -> list[Path]:
    source_path = Path(config.path)

    if source_path.is_file():
        return [source_path]

    if source_path.is_dir():
        pattern = config.glob or "*.csv"
        return sorted(source_path.glob(pattern))

    if config.glob:
        parent = source_path if source_path.exists() else source_path.parent
        return sorted(parent.glob(config.glob))

    raise FileNotFoundError(f"CSV source path not found: {config.path}")


def extract(config: CsvSourceConfig, dry_run: bool = False) -> Iterator[tuple[Path, ExtractResult]]:
    """Yield (file_path, ExtractResult) for each unprocessed file matching the source config.

    dry_run=True (used by `loader validate`) reads and reports without marking files
    processed or moving them — no side effects on re-runnability state.
    """
    manifest = ProcessedFilesManifest(config.manifest_path)
    files = _discover_files(config)

    for file_path in files:
        if manifest.is_processed(file_path):
            logger.info("skipping already-processed file: %s", file_path)
            continue

        result = _iter_rows_in_file(file_path, config)
        yield file_path, result

        if dry_run:
            continue

        manifest.mark_processed(file_path)

        if config.move_processed_to:
            dest_dir = file_path.parent / config.move_processed_to
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(file_path), str(dest_dir / file_path.name))
