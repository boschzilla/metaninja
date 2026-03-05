"""
seed_fake_history.py

Inserts 24 hours of fake hourly price snapshots into price_history.db for
every item currently in the database.

For each item:
  - A random total delta of ±1–100 chaos is chosen.
  - 24 data points are inserted, one per hour going back from now,
    linearly interpolating from (current_price - delta) → current_price.

This lets you test the Trend Watcher without waiting days for real data.

Usage:
    python seed_fake_history.py
"""
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(r"C:\poe\price_history.db")
HOURS   = 24


def main() -> None:
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found. Run trend_watcher.py and fetch at least once first.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Get the most-recent price for every item.
    items = conn.execute("""
        SELECT p.name, p.category, p.chaos_value
        FROM price_history p
        WHERE p.fetched_at = (
            SELECT MAX(p2.fetched_at) FROM price_history p2 WHERE p2.name = p.name
        )
        GROUP BY p.name
    """).fetchall()

    if not items:
        print("No items found in database. Fetch real data first, then re-run this script.")
        conn.close()
        return

    print(f"Found {len(items)} distinct items.  Inserting {HOURS} fake hourly snapshots each…")

    now = datetime.utcnow()
    # Build 24 timestamps: 24h ago, 23h ago, ..., 1h ago  (not "now", so real data stays newest)
    timestamps = [
        (now - timedelta(hours=HOURS - h)).isoformat()
        for h in range(HOURS)
    ]

    rows = []
    for item in items:
        name      = item["name"]
        category  = item["category"]
        end_price = item["chaos_value"]          # current price is the "end" of the series

        delta     = random.randint(1, 100) * random.choice([-1, 1])
        start_price = max(0.01, end_price - delta)  # clamp so price never goes negative

        for i, ts in enumerate(timestamps):
            # Linear interpolation from start_price → end_price over HOURS steps
            t = i / max(HOURS - 1, 1)
            price = round(start_price + t * (end_price - start_price), 2)
            rows.append((category, name, price, ts))

    conn.executemany(
        "INSERT INTO price_history (category, name, chaos_value, fetched_at) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()

    total_snapshots = len(set(ts for _, _, _, ts in rows))
    print(f"Done.  Inserted {len(rows):,} rows across {total_snapshots} fake snapshots.")
    print(f"DB: {DB_PATH}")


if __name__ == "__main__":
    main()
