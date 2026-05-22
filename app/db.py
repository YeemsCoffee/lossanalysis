"""
SQLite persistence layer for daily kitchen performance history.
One row per report date — re-uploading the same date overwrites.
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime

# DB file lives next to wsgi.py (project root)
DB_PATH = Path(__file__).resolve().parent.parent / "history.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the table if it doesn't exist yet."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_performance (
                report_date     TEXT PRIMARY KEY,
                total_tickets   INTEGER,
                over_count      INTEGER,
                on_time_count   INTEGER,
                pct_over        REAL,
                pct_on_target   REAL,
                avg_seconds     INTEGER,
                longest_seconds INTEGER,
                cluster_count   INTEGER,
                sources_json    TEXT,
                hourly_json     TEXT,
                uploaded_at     TEXT
            )
        """)


def save_report(result: dict):
    """Upsert a day's analysis results. Same date → overwrites."""
    init_db()

    # Parse display date back to ISO (YYYY-MM-DD) for consistent sorting
    # result["report_date"] is e.g. "Thursday, May 21, 2026"
    try:
        iso_date = datetime.strptime(result["report_date"], "%A, %B %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        # fallback: strip day name and try
        parts = result["report_date"].split(", ", 1)
        iso_date = datetime.strptime(parts[-1], "%B %d, %Y").strftime("%Y-%m-%d")

    sources = [
        {k: v for k, v in s.items() if k not in ("avg_fmt",)}
        for s in result.get("sources", [])
    ]
    hourly = [
        {k: v for k, v in h.items() if k not in ("avg_fmt",)}
        for h in result.get("hourly", [])
    ]

    with _connect() as conn:
        conn.execute("""
            INSERT INTO daily_performance
              (report_date, total_tickets, over_count, on_time_count,
               pct_over, pct_on_target, avg_seconds, longest_seconds,
               cluster_count, sources_json, hourly_json, uploaded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(report_date) DO UPDATE SET
              total_tickets   = excluded.total_tickets,
              over_count      = excluded.over_count,
              on_time_count   = excluded.on_time_count,
              pct_over        = excluded.pct_over,
              pct_on_target   = excluded.pct_on_target,
              avg_seconds     = excluded.avg_seconds,
              longest_seconds = excluded.longest_seconds,
              cluster_count   = excluded.cluster_count,
              sources_json    = excluded.sources_json,
              hourly_json     = excluded.hourly_json,
              uploaded_at     = excluded.uploaded_at
        """, (
            iso_date,
            result["total_tickets"],
            result["over_count"],
            result["on_time_count"],
            result["pct_over"],
            result["pct_on_target"],
            result["avg_seconds"],
            result["longest_seconds"],
            len(result.get("clusters", [])),
            json.dumps(sources),
            json.dumps(hourly),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ))
    return iso_date


def get_history(from_date: str = None, to_date: str = None) -> list[dict]:
    """
    Return daily rows sorted oldest-first.
    from_date / to_date: ISO strings "YYYY-MM-DD" (optional).
    """
    init_db()
    query = "SELECT * FROM daily_performance"
    params = []
    conditions = []

    if from_date:
        conditions.append("report_date >= ?")
        params.append(from_date)
    if to_date:
        conditions.append("report_date <= ?")
        params.append(to_date)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY report_date ASC"

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["sources"] = json.loads(d.pop("sources_json") or "[]")
        d["hourly"] = json.loads(d.pop("hourly_json") or "[]")
        out.append(d)
    return out


def get_date_bounds() -> tuple[str | None, str | None]:
    """Return (earliest_date, latest_date) in the DB, or (None, None)."""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT MIN(report_date), MAX(report_date) FROM daily_performance"
        ).fetchone()
    return (row[0], row[1])
