"""
Database layer.
- Local dev  : SQLite (history.db in project root)
- Production : PostgreSQL via DATABASE_URL environment variable

All SQL is written with ? placeholders; _adapt() converts to %s for Postgres.
"""

import os
import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager

try:
    import psycopg2
    import psycopg2.extras
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

DATABASE_URL = os.environ.get("DATABASE_URL", "")
DB_PATH      = Path(__file__).resolve().parent.parent / "history.db"
TARGET_SECONDS = 294
IS_POSTGRES  = bool(DATABASE_URL)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _adapt(sql: str) -> str:
    """Translate ? placeholders to %s for PostgreSQL."""
    return sql.replace("?", "%s") if IS_POSTGRES else sql


@contextmanager
def _get_cursor():
    """
    Yield a DB cursor; commit on clean exit, rollback + reraise on error.
    Both backends yield dict-like rows (sqlite3.Row / psycopg2 RealDictCursor).
    """
    if IS_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cur  = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db():
    """Create tables and indexes if they don't exist; seed admin if needed."""
    if IS_POSTGRES:
        pk = "SERIAL PRIMARY KEY"
    else:
        pk = "INTEGER PRIMARY KEY AUTOINCREMENT"

    with _get_cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS tickets (
                id              {pk},
                report_date     TEXT    NOT NULL,
                location        TEXT,
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
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_report_date ON tickets(report_date)"
        )

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
                id            {pk},
                email         TEXT    NOT NULL UNIQUE,
                name          TEXT    NOT NULL,
                password_hash TEXT    NOT NULL,
                is_admin      INTEGER NOT NULL DEFAULT 0,
                is_active     INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT    NOT NULL,
                last_login_at TEXT,
                location      TEXT
            )
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id         {pk},
                user_id    INTEGER NOT NULL,
                token      TEXT    NOT NULL UNIQUE,
                expires_at TEXT    NOT NULL,
                used       INTEGER NOT NULL DEFAULT 0
            )
        """)

    # Add location column to existing tables if it doesn't already exist
    _migrate_add_location_columns()


def _migrate_add_location_columns():
    """Add location column to tickets and users tables if not present (migration)."""
    if IS_POSTGRES:
        with _get_cursor() as cur:
            cur.execute(
                "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS location TEXT"
            )
            cur.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS location TEXT"
            )
    else:
        # SQLite: no IF NOT EXISTS for ALTER TABLE — catch OperationalError if already exists
        for table in ("tickets", "users"):
            try:
                with _get_cursor() as cur:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN location TEXT")
            except Exception as e:
                if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                    pass  # Column already exists — that's fine
                else:
                    raise


# ---------------------------------------------------------------------------
# User model (Flask-Login compatible)
# ---------------------------------------------------------------------------

class User:
    def __init__(self, id, email, name, password_hash,
                 is_admin, is_active, created_at, last_login_at, location=None):
        self.id            = id
        self.email         = email
        self.name          = name
        self.password_hash = password_hash
        self.is_admin      = bool(is_admin)
        self._is_active    = bool(is_active)
        self.created_at    = created_at
        self.last_login_at = last_login_at
        self.location      = location or None

    # --- Flask-Login interface ---
    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return self._is_active

    @property
    def is_anonymous(self):
        return False

    def get_id(self):
        return str(self.id)

    # --- Factory ---
    @classmethod
    def from_row(cls, row):
        if row is None:
            return None
        d = dict(row)
        return cls(
            id            = d["id"],
            email         = d["email"],
            name          = d["name"],
            password_hash = d["password_hash"],
            is_admin      = d["is_admin"],
            is_active     = d["is_active"],
            created_at    = d["created_at"],
            last_login_at = d.get("last_login_at"),
            location      = d.get("location"),
        )


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------

def get_user_by_id(user_id: int):
    with _get_cursor() as cur:
        cur.execute(_adapt("SELECT * FROM users WHERE id = ?"), (user_id,))
        return User.from_row(cur.fetchone())


def get_user_by_email(email: str):
    with _get_cursor() as cur:
        cur.execute(_adapt("SELECT * FROM users WHERE email = ?"), (email.lower().strip(),))
        return User.from_row(cur.fetchone())


def create_user(email: str, name: str, password: str, is_admin: bool = False,
                location: str = None):
    from werkzeug.security import generate_password_hash
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _get_cursor() as cur:
        cur.execute(_adapt("""
            INSERT INTO users (email, name, password_hash, is_admin, is_active, created_at, location)
            VALUES (?, ?, ?, ?, 1, ?, ?)
        """), (email.lower().strip(), name.strip(),
               generate_password_hash(password),
               1 if is_admin else 0, now, location or None))


def update_last_login(user_id: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _get_cursor() as cur:
        cur.execute(_adapt("UPDATE users SET last_login_at = ? WHERE id = ?"),
                    (now, user_id))


def set_user_active(user_id: int, active: bool):
    with _get_cursor() as cur:
        cur.execute(_adapt("UPDATE users SET is_active = ? WHERE id = ?"),
                    (1 if active else 0, user_id))


def update_user_location(user_id: int, location):
    with _get_cursor() as cur:
        cur.execute(_adapt("UPDATE users SET location = ? WHERE id = ?"),
                    (location or None, user_id))


def list_users():
    with _get_cursor() as cur:
        cur.execute("SELECT * FROM users ORDER BY created_at ASC")
        return [User.from_row(r) for r in cur.fetchall()]


def update_user_password(user_id: int, new_password: str):
    from werkzeug.security import generate_password_hash
    with _get_cursor() as cur:
        cur.execute(_adapt("UPDATE users SET password_hash = ? WHERE id = ?"),
                    (generate_password_hash(new_password), user_id))


# ---------------------------------------------------------------------------
# Password reset tokens
# ---------------------------------------------------------------------------

def create_reset_token(user_id: int) -> str:
    import secrets
    from datetime import timedelta
    token      = secrets.token_urlsafe(32)
    expires_at = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    with _get_cursor() as cur:
        cur.execute(_adapt("""
            INSERT INTO password_reset_tokens (user_id, token, expires_at, used)
            VALUES (?, ?, ?, 0)
        """), (user_id, token, expires_at))
    return token


def get_valid_reset_token(token: str):
    """Return the token row if it exists, is unused, and hasn't expired."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _get_cursor() as cur:
        cur.execute(_adapt("""
            SELECT * FROM password_reset_tokens
            WHERE token = ? AND used = 0 AND expires_at > ?
        """), (token, now))
        row = cur.fetchone()
    return dict(row) if row else None


def mark_token_used(token: str):
    with _get_cursor() as cur:
        cur.execute(_adapt("UPDATE password_reset_tokens SET used = 1 WHERE token = ?"),
                    (token,))


def seed_admin_if_needed():
    """
    On first boot, if no users exist and INITIAL_ADMIN_EMAIL + _PASSWORD are
    set in the environment, create the first admin account automatically.
    """
    email    = os.environ.get("INITIAL_ADMIN_EMAIL", "")
    password = os.environ.get("INITIAL_ADMIN_PASSWORD", "")
    if not email or not password:
        return
    try:
        with _get_cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM users")
            row = dict(cur.fetchone())
            count = row.get("cnt", 0)
        if count == 0:
            create_user(email, "Admin", password, is_admin=True)
    except Exception:
        pass  # Don't crash startup if seeding fails


# ---------------------------------------------------------------------------
# Ticket persistence
# ---------------------------------------------------------------------------

def save_day_tickets(report_date: str, df: pd.DataFrame, location: str = None):
    """Delete existing tickets for date (and location if provided), then insert the full new set."""
    init_db()
    uploaded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        (
            report_date,
            location or None,
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
    with _get_cursor() as cur:
        if location:
            cur.execute(
                _adapt("DELETE FROM tickets WHERE report_date = ? AND location = ?"),
                (report_date, location)
            )
        else:
            cur.execute(_adapt("DELETE FROM tickets WHERE report_date = ?"), (report_date,))
        cur.executemany(_adapt("""
            INSERT INTO tickets
               (report_date, location, ticket_name, order_source, num_items, items,
                duration, time_created, time_completed, device_name, uploaded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """), rows)


def get_tickets_df(from_date: str = None, to_date: str = None,
                   location: str = None) -> pd.DataFrame:
    """
    Load tickets from the DB as a DataFrame matching the column structure
    that parse_report() produces, plus a 'report_date' column.
    """
    init_db()
    sql        = "SELECT * FROM tickets"
    params     = []
    conditions = []
    if from_date:
        conditions.append("report_date >= ?")
        params.append(from_date)
    if to_date:
        conditions.append("report_date <= ?")
        params.append(to_date)
    if location:
        conditions.append("location = ?")
        params.append(location)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY time_created ASC"

    with _get_cursor() as cur:
        cur.execute(_adapt(sql), params)
        rows = cur.fetchall()

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
    with _get_cursor() as cur:
        cur.execute("SELECT MIN(report_date) AS mn, MAX(report_date) AS mx FROM tickets")
        row = dict(cur.fetchone())
    return (row["mn"], row["mx"])


def get_distinct_dates() -> list:
    init_db()
    with _get_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT report_date FROM tickets ORDER BY report_date DESC"
        )
        return [dict(r)["report_date"] for r in cur.fetchall()]
