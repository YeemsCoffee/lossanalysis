"""
Database layer.
- Local dev  : SQLite (history.db in project root)
- Production : PostgreSQL via DATABASE_URL environment variable

All SQL is written with ? placeholders; _adapt() converts to %s for Postgres.
"""

import json
import os
import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager

try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

DATABASE_URL = os.environ.get("DATABASE_URL", "")
DB_PATH      = Path(__file__).resolve().parent.parent / "history.db"
TARGET_SECONDS = 294
IS_POSTGRES  = bool(DATABASE_URL)
POOL_MAX     = int(os.environ.get("DB_POOL_MAX", "4"))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _adapt(sql: str) -> str:
    """Translate ? placeholders to %s for PostgreSQL."""
    return sql.replace("?", "%s") if IS_POSTGRES else sql


_pool     = None
_pool_pid = None


def _get_pool():
    """
    Return this process's connection pool, creating it on first use.

    Gunicorn forks workers, and a pool built before the fork would hand out
    sockets shared between processes — so the pool is keyed to the pid and
    rebuilt if we find ourselves in a child that inherited one.
    """
    global _pool, _pool_pid
    pid = os.getpid()
    if _pool is None or _pool_pid != pid:
        _pool     = psycopg2.pool.ThreadedConnectionPool(1, POOL_MAX, DATABASE_URL)
        _pool_pid = pid
    return _pool


@contextmanager
def _get_cursor():
    """
    Yield a DB cursor; commit on clean exit, rollback + reraise on error.
    Both backends yield dict-like rows (sqlite3.Row / psycopg2 RealDictCursor).
    """
    if IS_POSTGRES:
        pool = _get_pool()
        conn = pool.getconn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                yield cur
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass  # already broken; the original error is the useful one
                raise
            finally:
                cur.close()
        finally:
            # Don't hand a connection that died mid-query back to the pool.
            pool.putconn(conn, close=bool(conn.closed))
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

_schema_ready = False


def _ensure_schema():
    """
    Make sure the schema exists before running a query.

    init_db() also runs at boot, but that can fail (an unreachable RDS, a
    security group not open yet) and it only logs a warning — so the first
    query in each worker retries it. Once it succeeds this is a no-op; it
    used to re-run the full DDL against RDS on every single query.
    """
    if not _schema_ready:
        init_db()


def init_db():
    """Create tables and indexes if they don't exist; seed admin if needed."""
    global _schema_ready
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
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_location ON tickets(location)"
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

        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Seed default settings
        if IS_POSTGRES:
            cur.execute("INSERT INTO settings (key, value) VALUES ('target_seconds', '294') ON CONFLICT DO NOTHING")
            cur.execute("INSERT INTO settings (key, value) VALUES ('target_pct', '85') ON CONFLICT DO NOTHING")
        else:
            cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('target_seconds', '294')")
            cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('target_pct', '85')")

    # Add location column to existing tables if it doesn't already exist
    _migrate_add_location_columns()

    _schema_ready = True


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
# Settings helpers
# ---------------------------------------------------------------------------

def get_setting(key: str, default: str = None) -> str:
    """Get a single setting value."""
    # Settings are often the first thing touched on a fresh database — the CLI
    # reads the target before it reads a ticket — so this cannot assume the
    # schema is already there the way a ticket query can.
    _ensure_schema()
    with _get_cursor() as cur:
        cur.execute(_adapt("SELECT value FROM settings WHERE key = ?"), (key,))
        row = cur.fetchone()
    return dict(row)["value"] if row else default


def set_setting(key: str, value: str):
    """Upsert a setting value."""
    _ensure_schema()
    if IS_POSTGRES:
        sql = "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        with _get_cursor() as cur:
            cur.execute(sql, (key, value))
    else:
        with _get_cursor() as cur:
            cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


SYNC_STATUS_KEY = "square_last_sync"


def record_sync_status(**fields):
    """
    Remember how the last Square sync went.

    Stored in settings rather than its own table: it is one row, and this keeps
    the sync from needing a migration. The point is that a sync which quietly
    stops working should be visible in the app, not only in a log file on the
    instance — stale data that still looks current is the failure that costs
    someone a decision.
    """
    fields.setdefault("at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    set_setting(SYNC_STATUS_KEY, json.dumps(fields))


def get_sync_status() -> dict:
    """The last recorded sync outcome, or {} if one has never run."""
    raw = get_setting(SYNC_STATUS_KEY)
    if not raw:
        return {}
    try:
        status = json.loads(raw)
    except (ValueError, TypeError):
        return {}

    at = status.get("at")
    if at:
        try:
            delta = datetime.now() - datetime.strptime(at, "%Y-%m-%d %H:%M:%S")
            status["minutes_ago"] = max(int(delta.total_seconds() // 60), 0)
        except ValueError:
            pass
    return status


def get_targets() -> dict:
    """Return current target_seconds and target_pct from settings."""
    return {
        "target_seconds": int(get_setting("target_seconds", "294")),
        "target_pct":     int(get_setting("target_pct",     "85")),
    }


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


def bulk_assign_location(location: str, overwrite: bool = False):
    """Assign a location to all tickets that have no location set.
    If overwrite=True, reassigns ALL tickets regardless of current location."""
    with _get_cursor() as cur:
        if overwrite:
            cur.execute(_adapt("UPDATE tickets SET location = ?"), (location,))
        else:
            cur.execute(
                _adapt("UPDATE tickets SET location = ? WHERE location IS NULL OR location = ''"),
                (location,)
            )
        return cur.rowcount


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
    _ensure_schema()
    uploaded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Build the tuples off the columns directly — iterrows() boxes every row
    # into a Series, which dominates the cost on a month-sized upload.
    n       = len(df)
    devices = (df["Device Name"].astype(str) if "Device Name" in df.columns
               else pd.Series([""] * n, index=df.index))
    rows = list(zip(
        [report_date] * n,
        [location or None] * n,
        df["Ticket Name"].astype(str),
        df["Order Source"].astype(str),
        df["Number of Items"].astype(int),
        df["Items in Ticket"].astype(str),
        df["duration"].astype(int),
        df["Time Created"].astype(str),
        df["Time Completed"].astype(str),
        devices,
        [uploaded_at] * n,
    ))

    insert_sql = """
        INSERT INTO tickets
           (report_date, location, ticket_name, order_source, num_items, items,
            duration, time_created, time_completed, device_name, uploaded_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """
    with _get_cursor() as cur:
        if location:
            cur.execute(
                _adapt("DELETE FROM tickets WHERE report_date = ? AND location = ?"),
                (report_date, location)
            )
        else:
            cur.execute(_adapt("DELETE FROM tickets WHERE report_date = ?"), (report_date,))
        if not rows:
            return
        if IS_POSTGRES:
            # execute_values sends one multi-row INSERT; executemany would send
            # a separate round trip per ticket.
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO tickets"
                " (report_date, location, ticket_name, order_source, num_items,"
                "  items, duration, time_created, time_completed, device_name,"
                "  uploaded_at) VALUES %s",
                rows,
                page_size=1000,
            )
        else:
            cur.executemany(insert_sql, rows)


def _ticket_filters(from_date: str = None, to_date: str = None,
                    location: str = None) -> tuple:
    """Build the shared WHERE clause for ticket queries. Returns (sql, params)."""
    conditions, params = [], []
    if from_date:
        conditions.append("report_date >= ?")
        params.append(from_date)
    if to_date:
        conditions.append("report_date <= ?")
        params.append(to_date)
    if location:
        conditions.append("location = ?")
        params.append(location)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, params


# Hour-of-day out of the stored "YYYY-MM-DD HH:MM:SS" string. substr() with
# this signature behaves identically on SQLite and Postgres.
_HOUR_EXPR = "CAST(substr(time_created, 12, 2) AS INTEGER)"

# duration > target, as a 0/1 column both backends can SUM().
_OVER_EXPR = "SUM(CASE WHEN duration > ? THEN 1 ELSE 0 END)"


def get_daily_summary(from_date: str = None, to_date: str = None,
                      location: str = None, target_seconds: int = 294) -> list:
    """
    Per-day totals, aggregated by the database.

    The History page only ever needed these numbers, not the underlying
    tickets — this returns one row per day instead of thousands per day.
    """
    _ensure_schema()
    where, params = _ticket_filters(from_date, to_date, location)
    sql = f"""
        SELECT report_date,
               COUNT(*)            AS total,
               {_OVER_EXPR}        AS over_count,
               AVG(duration * 1.0) AS avg_seconds,
               MAX(duration)       AS longest_seconds
        FROM tickets{where}
        GROUP BY report_date
        ORDER BY report_date ASC
    """
    with _get_cursor() as cur:
        cur.execute(_adapt(sql), [target_seconds] + params)
        rows = [dict(r) for r in cur.fetchall()]

    for r in rows:
        r["total"]           = int(r["total"])
        r["over_count"]      = int(r["over_count"] or 0)
        r["avg_seconds"]     = float(r["avg_seconds"] or 0)
        r["longest_seconds"] = int(r["longest_seconds"] or 0)
    return rows


def get_source_daily_summary(from_date: str = None, to_date: str = None,
                             location: str = None, target_seconds: int = 294) -> list:
    """Per (order source, day) totals — for the source trend chart."""
    _ensure_schema()
    where, params = _ticket_filters(from_date, to_date, location)
    sql = f"""
        SELECT TRIM(order_source) AS source,
               report_date,
               COUNT(*)           AS total,
               {_OVER_EXPR}       AS over_count
        FROM tickets{where}
        GROUP BY TRIM(order_source), report_date
        ORDER BY report_date ASC
    """
    with _get_cursor() as cur:
        cur.execute(_adapt(sql), [target_seconds] + params)
        rows = [dict(r) for r in cur.fetchall()]

    for r in rows:
        r["total"]      = int(r["total"])
        r["over_count"] = int(r["over_count"] or 0)
    return rows


def get_hourly_daily_summary(from_date: str = None, to_date: str = None,
                             location: str = None, target_seconds: int = 294) -> list:
    """
    Per (hour of day, day) totals — for the hourly chart.

    Grouped this far and no further: the chart averages each day's hourly
    on-time rate, which is an average of ratios rather than a ratio of sums,
    so the last step happens in Python over a few hundred rows.
    """
    _ensure_schema()
    where, params = _ticket_filters(from_date, to_date, location)
    sql = f"""
        SELECT {_HOUR_EXPR} AS hour,
               report_date,
               COUNT(*)     AS total,
               {_OVER_EXPR} AS over_count
        FROM tickets{where}
        GROUP BY {_HOUR_EXPR}, report_date
        ORDER BY hour ASC
    """
    with _get_cursor() as cur:
        cur.execute(_adapt(sql), [target_seconds] + params)
        rows = [dict(r) for r in cur.fetchall()]

    for r in rows:
        r["hour"]       = int(r["hour"])
        r["total"]      = int(r["total"])
        r["over_count"] = int(r["over_count"] or 0)
    return rows


def count_tickets(from_date: str = None, to_date: str = None,
                  location: str = None) -> int:
    """
    How many tickets a range holds, without loading any of them.

    The ticket-level pages (Loss Drivers) genuinely need every row, so this
    lets a route find out what it is about to pull and say so, rather than
    starting work that will outlive the request timeout.
    """
    _ensure_schema()
    where, params = _ticket_filters(from_date, to_date, location)
    with _get_cursor() as cur:
        cur.execute(_adapt(f"SELECT COUNT(*) AS n FROM tickets{where}"), params)
        return int(dict(cur.fetchone())["n"])


def get_tickets_df(from_date: str = None, to_date: str = None,
                   location: str = None, target_seconds: int = 294) -> pd.DataFrame:
    """
    Load tickets from the DB as a DataFrame matching the column structure
    that parse_report() produces, plus a 'report_date' column.
    """
    _ensure_schema()
    where, params = _ticket_filters(from_date, to_date, location)
    sql = "SELECT * FROM tickets" + where + " ORDER BY time_created ASC"

    with _get_cursor() as cur:
        cur.execute(_adapt(sql), params)
        rows = cur.fetchall()

    if not rows:
        return pd.DataFrame()

    # Build columnwise. pd.DataFrame(list_of_dicts) re-derives the schema from
    # every row and is ~3x slower on the row counts the drivers page reaches.
    columns = list(rows[0].keys())
    df = pd.DataFrame({k: [r[k] for r in rows] for k in columns})
    df = df.rename(columns={
        "ticket_name":  "Ticket Name",
        "order_source": "Order Source",
        "num_items":    "Number of Items",
        "items":        "Items in Ticket",
        "device_name":  "Device Name",
    })
    # We always write these ourselves as str(Timestamp) (see save_day_tickets),
    # so they're consistently ISO-like — pin the format to avoid pandas falling
    # back to a slow per-row dateutil parse on large history/drivers queries.
    df["Time Created"]   = pd.to_datetime(df["time_created"], format="ISO8601")
    df["Time Completed"] = pd.to_datetime(df["time_completed"], format="ISO8601")
    df["duration"]       = df["duration"].astype(float)
    df["over_target"]    = df["duration"] > target_seconds
    df["seconds_over"]   = (df["duration"] - target_seconds).clip(lower=0)
    df["hour"]           = df["Time Created"].dt.hour
    df["source"]         = df["Order Source"].str.strip()

    return df


def get_date_bounds() -> tuple:
    _ensure_schema()
    with _get_cursor() as cur:
        cur.execute("SELECT MIN(report_date) AS mn, MAX(report_date) AS mx FROM tickets")
        row = dict(cur.fetchone())
    return (row["mn"], row["mx"])


def get_last_date_by_location() -> dict:
    """
    The most recent day each location has tickets for.

    Per location rather than overall, because the two stores fail
    independently: a rename that stops Koreatown matching, or a KDS that
    stayed off, leaves that one location behind while the other looks
    perfectly current. An overall MAX(report_date) hides exactly that.
    """
    _ensure_schema()
    with _get_cursor() as cur:
        cur.execute("""
            SELECT location, MAX(report_date) AS mx
              FROM tickets
             WHERE location IS NOT NULL
          GROUP BY location
        """)
        return {dict(r)["location"]: dict(r)["mx"] for r in cur.fetchall()}


def get_distinct_dates() -> list:
    _ensure_schema()
    with _get_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT report_date FROM tickets ORDER BY report_date DESC"
        )
        return [dict(r)["report_date"] for r in cur.fetchall()]
