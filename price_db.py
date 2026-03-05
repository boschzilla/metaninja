"""
SQLite storage for PoE price history.

DB location: C:\\poe\\price_history.db

Schema:
    price_history(id, category, name, chaos_value, fetched_at)

One row per item per fetch.  Multiple fetches build up a time series
that the trend viewer uses to compute movers/risers/fallers.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(r"C:\poe\price_history.db")

# ── SQL template shared by get_movers / get_risers / get_fallers ───────────────
# Uses window functions (SQLite >= 3.25, shipped 2018) to find the first and
# last recorded price per item, then computes absolute and % change.
_TREND_SQL = """
WITH snaps AS (
    SELECT
        name, category, chaos_value, fetched_at,
        ROW_NUMBER() OVER (PARTITION BY name ORDER BY fetched_at ASC)  AS rn_asc,
        ROW_NUMBER() OVER (PARTITION BY name ORDER BY fetched_at DESC) AS rn_desc,
        COUNT(*)     OVER (PARTITION BY name)                          AS snap_count
    FROM price_history
),
bounds AS (
    SELECT
        name,
        MAX(category)                                                AS category,
        MAX(CASE WHEN rn_asc  = 1 THEN chaos_value END)             AS first_price,
        MAX(CASE WHEN rn_desc = 1 THEN chaos_value END)             AS last_price,
        MAX(CASE WHEN rn_asc  = 1 THEN fetched_at  END)             AS first_time,
        MAX(CASE WHEN rn_desc = 1 THEN fetched_at  END)             AS last_time,
        MAX(snap_count)                                             AS snap_count
    FROM snaps
    GROUP BY name
    HAVING MAX(snap_count) >= :min_snaps
),
calcs AS (
    SELECT
        name, category, first_price, last_price, first_time, last_time, snap_count,
        (last_price - first_price)                                  AS abs_change,
        CASE WHEN first_price > 0
             THEN (last_price - first_price) * 100.0 / first_price
             ELSE 0 END                                             AS pct_change
    FROM bounds
    WHERE first_price > 0
)
SELECT * FROM calcs
{extra_where}
ORDER BY {order}
LIMIT :limit
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


# ── Schema ─────────────────────────────────────────────────────────────────────

def init_db() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                category    TEXT    NOT NULL,
                name        TEXT    NOT NULL,
                chaos_value REAL    NOT NULL,
                fetched_at  TEXT    NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_ph_name      ON price_history(name)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ph_time      ON price_history(fetched_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ph_name_time ON price_history(name, fetched_at)")


# ── Write ──────────────────────────────────────────────────────────────────────

def insert_snapshot(category: str, items: list, fetched_at: str) -> int:
    """
    Insert a batch of priced items for one category at one point in time.
    Items must have 'name' and 'chaosValue' keys (as returned by fetch_ninja_prices).
    Returns the number of rows inserted.
    """
    rows = [
        (category, item.get("name", "").strip(), float(item.get("chaosValue") or 0), fetched_at)
        for item in items
        if item.get("name", "").strip() and (item.get("chaosValue") or 0) > 0
    ]
    if not rows:
        return 0
    with _conn() as c:
        c.executemany(
            "INSERT INTO price_history (category, name, chaos_value, fetched_at) VALUES (?,?,?,?)",
            rows,
        )
    return len(rows)


# ── Read ───────────────────────────────────────────────────────────────────────

def get_history(name: str) -> list[dict]:
    """Full price history for one item, oldest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT fetched_at, chaos_value FROM price_history"
            " WHERE name=? ORDER BY fetched_at ASC",
            (name,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_snapshot_times() -> list[str]:
    """All distinct snapshot timestamps, newest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT fetched_at FROM price_history ORDER BY fetched_at DESC"
        ).fetchall()
    return [r["fetched_at"] for r in rows]


def snapshot_count() -> int:
    """Number of distinct fetch timestamps stored."""
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(DISTINCT fetched_at) AS n FROM price_history"
        ).fetchone()
    return row["n"] if row else 0


def search_items(query: str, limit: int = 50) -> list[dict]:
    """
    Items whose names contain *query*, returning the most-recent chaos value.
    """
    with _conn() as c:
        rows = c.execute(
            """
            SELECT p1.name, p1.category, p1.chaos_value AS last_price
            FROM price_history p1
            WHERE p1.name LIKE ?
              AND p1.fetched_at = (
                  SELECT MAX(p2.fetched_at) FROM price_history p2 WHERE p2.name = p1.name
              )
            GROUP BY p1.name
            ORDER BY p1.name
            LIMIT ?
            """,
            (f"%{query}%", limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Trend queries ──────────────────────────────────────────────────────────────

def _query_trend(extra_where: str, order: str, min_snaps: int, limit: int) -> list[dict]:
    sql = _TREND_SQL.format(extra_where=extra_where, order=order)
    with _conn() as c:
        rows = c.execute(sql, {"min_snaps": min_snaps, "limit": limit}).fetchall()
    return [dict(r) for r in rows]


def get_movers(min_snaps: int = 2, limit: int = 100) -> list[dict]:
    """Items with the largest absolute % change (up or down)."""
    return _query_trend("", "ABS(pct_change) DESC", min_snaps, limit)


def get_risers(min_snaps: int = 2, limit: int = 100) -> list[dict]:
    """Items with the largest positive % change."""
    return _query_trend("WHERE pct_change > 0", "pct_change DESC", min_snaps, limit)


def get_fallers(min_snaps: int = 2, limit: int = 100) -> list[dict]:
    """Items with the largest negative % change."""
    return _query_trend("WHERE pct_change < 0", "pct_change ASC", min_snaps, limit)
