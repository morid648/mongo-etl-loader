"""SQL extraction: full or incremental (watermark-based), chunked reads with retry/backoff."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from tenacity import Retrying, stop_after_attempt, wait_exponential

from loader.config import SqlSourceConfig, resolve_env
from loader.state import WatermarkStore

logger = logging.getLogger("loader.sql_extractor")


def build_engine(config: SqlSourceConfig, min_wait: float = 1, max_wait: float = 30) -> Engine:
    """Connect with retry + exponential backoff (PRD §7.1). min_wait/max_wait are
    overridable so tests don't have to sit through real backoff delays."""
    conn_string = resolve_env(config.connection_env)

    for attempt in Retrying(
        stop=stop_after_attempt(config.max_retries),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        reraise=True,
    ):
        with attempt:
            engine = create_engine(conn_string)
            with engine.connect():
                pass
            return engine

    raise RuntimeError("unreachable")  # pragma: no cover


def _coerce_watermark(value) -> datetime:
    """Normalize a raw watermark column value to a datetime.

    Drivers vary: psycopg2 returns native datetime for TIMESTAMP columns,
    sqlite3 returns plain strings for untyped raw-SQL reads.
    """
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _from_clause(config: SqlSourceConfig) -> str:
    if config.table:
        return config.table
    return f"({config.query}) AS _loader_subquery"


def _build_query(config: SqlSourceConfig, incremental: bool) -> str:
    sql = f"SELECT * FROM {_from_clause(config)}"

    if incremental and config.watermark_column:
        sql += f" WHERE {config.watermark_column} > :last_watermark"

    if config.watermark_column:
        sql += f" ORDER BY {config.watermark_column} ASC"

    sql += " LIMIT :limit OFFSET :offset"
    return sql


def extract(
    config: SqlSourceConfig,
    incremental: bool = True,
    min_wait: float = 1,
    max_wait: float = 30,
    dry_run: bool = False,
) -> Iterator[list[dict]]:
    """Yield chunked lists of row dicts. Advances and persists the watermark as it reads.

    dry_run=True (used by `loader validate`) reports without advancing the persisted
    watermark, so a real run afterwards still sees all the same rows as new.
    """
    engine = build_engine(config, min_wait=min_wait, max_wait=max_wait)
    watermark_store = WatermarkStore(config.state_path)

    do_incremental = incremental and bool(config.watermark_column)
    last_watermark = watermark_store.get() if do_incremental else None
    max_seen_watermark = last_watermark

    sql = text(_build_query(config, do_incremental))
    offset = 0

    try:
        with engine.connect() as conn:
            while True:
                params: dict = {"limit": config.chunk_size, "offset": offset}
                if do_incremental:
                    params["last_watermark"] = last_watermark or datetime.min

                result = conn.execute(sql, params)
                rows = [dict(row._mapping) for row in result]

                if not rows:
                    break

                if config.watermark_column:
                    for row in rows:
                        raw_wm = row.get(config.watermark_column)
                        if raw_wm is None:
                            continue
                        wm_value = _coerce_watermark(raw_wm)
                        if max_seen_watermark is None or wm_value > max_seen_watermark:
                            max_seen_watermark = wm_value

                yield rows
                offset += len(rows)

                if len(rows) < config.chunk_size:
                    break
    finally:
        engine.dispose()

    if do_incremental and max_seen_watermark is not None and not dry_run:
        watermark_store.set(max_seen_watermark)
