"""
SQLite persistence layer — stores every ticket row.
One DB row per ticket; re-uploading a date replaces all tickets for that date.
"""

import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).resolve().parent.parent / "history.db"
TARGET_SECONDS = 294


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date     TEXT NOT NULL,
                ticket_name     TEXT,
                order_source    TEXT,
                num_items       INTEGER,
                items           TEXT,
                duration        INTEGER,
                time_created    TEXT,
                time_completed  TEXT,
                device_name     TEXT,
                uploaded_at     TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_report_date ON tickets(report_date)"
        )


def save_day_tickets(report_date: str, df: pd.DataFrame):
    """Delete existing tickets for date, then insert the full new set."""
    init_db()
    uploaded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        (
            report_date,
            str(row["Ticket Name"]),
            str(row["Order Source"]),
            int(row["Number of Items"]),
            str(row["Items in Ticket"]),
            int(row["duration"]),
            str(row["Time Created"]),
            str(row["Time Completed"]),
            str(row.get("Device Name", "")),
            uploaded_at,
        )
        for _, row in df.iterrows()
    ]
    with _connect() as conn:
        conn.execute("DELETE FROM tickets WHERE report_date = ?", (report_date,))
        conn.executemany(
            """INSERT INTO tickets
               (report_date, ticket_name, order_source, num_items, items,
                duration, time_created, time_completed, device_name, uploaded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )


def get_tickets_df(from_date: str = None, to_date: str = None) -> pd.DataFrame:
    """
    Load tickets from DB as a DataFrame with the same computed columns
    that parse_report() produces, plus a 'report_date' column.
    """
    init_db()
    query = "SELECT * FROM tickets"
    params, conditions = [], []
    if from_date:
        conditions.append("report_date >= ?")
        params.append(from_date)
    if to_date:
        conditions.append("report_date <= ?")
        params.append(to_date)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY time_created ASC"

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(r) for r in rows])
    df = df.rename(columns={
        "ticket_name":  "Ticket Name",
        "order_source": "Order Source",
        "num_items":    "Number of Items",
        "items":        "Items in Ticket",
        "device_name":  "Device Name",
    })
    df["Time Created"]   = pd.to_datetime(df["time_created"])
    df["Time Completed"] = pd.to_datetime(df["time_completed"])
    df["duration"]       = df["duration"].astype(float)
    df["over_target"]    = df["duration"] > TARGET_SECONDS
    df["seconds_over"]   = (df["duration"] - TARGET_SECONDS).clip(lower=0)
    df["hour"]           = df["Time Created"].dt.hour
    df["source"]         = df["Order Source"].str.strip()

    return df


def get_date_bounds() -> tuple:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT MIN(report_date), MAX(report_date) FROM tickets"
        ).fetchone()
    return (row[0], row[1])


def get_distinct_dates() -> list:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT report_date FROM tickets ORDER BY report_date DESC"
        ).fetchall()
    return [r[0] for r in rows]
