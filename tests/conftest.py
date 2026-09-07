"""
Shared fixtures.

Tests run against SQLite by default — no setup needed. To also exercise the
production Postgres path (connection pool, execute_values, the ISO8601 parse),
point TEST_DATABASE_URL at a scratch database:

    TEST_DATABASE_URL=postgresql://postgres@localhost/lossanalysis_test pytest
"""
import importlib
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TARGET = 294


def _fresh_db(database_url, tmp_path):
    """Reimport app.db bound to the given backend, with empty tables."""
    if database_url:
        os.environ["DATABASE_URL"] = database_url
    else:
        os.environ.pop("DATABASE_URL", None)

    import app.db as db
    importlib.reload(db)
    if not database_url:
        db.DB_PATH = tmp_path / "test.db"     # isolate the SQLite file
    db._schema_ready = False
    db.init_db()
    with db._get_cursor() as cur:
        cur.execute("DELETE FROM tickets")
        cur.execute("DELETE FROM password_reset_tokens")
        cur.execute("DELETE FROM users")
        cur.execute("DELETE FROM settings")
    db.init_db()                               # re-seed default settings
    return db


@pytest.fixture(params=["sqlite", "postgres"])
def db(request, tmp_path):
    """The db module, once per available backend."""
    if request.param == "postgres":
        url = os.environ.get("TEST_DATABASE_URL")
        if not url:
            pytest.skip("set TEST_DATABASE_URL to test the Postgres path")
        return _fresh_db(url, tmp_path)
    return _fresh_db(None, tmp_path)


@pytest.fixture
def sqlite_db(tmp_path):
    """SQLite only — for tests where the backend is irrelevant."""
    return _fresh_db(None, tmp_path)


def make_tickets(day, durations, *, source="Register", hour=9, items="Latte, Bagel",
                 n_items=2, start_minute=0):
    """A DataFrame shaped like parse_report() output."""
    n = len(durations)
    created = [pd.Timestamp(f"{day} {hour:02d}:00:00") + pd.Timedelta(minutes=start_minute + i)
               for i in range(n)]
    return pd.DataFrame({
        "Ticket Name":     [f"T{i}" for i in range(n)],
        "Order Source":    [source] * n if isinstance(source, str) else source,
        "Number of Items": [n_items] * n if isinstance(n_items, int) else n_items,
        "Items in Ticket": [items] * n if isinstance(items, str) else items,
        "duration":        durations,
        "Time Created":    created,
        "Time Completed":  [c + pd.Timedelta(seconds=d) for c, d in zip(created, durations)],
        "Device Name":     ["KDS1"] * n,
        "over_target":     [d > TARGET for d in durations],
        "seconds_over":    [max(0, d - TARGET) for d in durations],
        "hour":            [hour] * n,
        "source":          [source] * n if isinstance(source, str) else source,
    })


@pytest.fixture
def client(sqlite_db, tmp_path, monkeypatch):
    """Logged-in test client backed by SQLite, CSRF left on."""
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    import app as app_pkg
    importlib.reload(app_pkg)
    application = app_pkg.create_app()
    application.config["TESTING"] = True

    sqlite_db.create_user("admin@yeemscoffee.com", "Admin", "pw12345678", is_admin=True)

    c = application.test_client()
    c._app = application
    return c


def login(client, email="admin@yeemscoffee.com", password="pw12345678"):
    """Log in, honouring the CSRF token on the form."""
    import re
    page = client.get("/login").get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', page)
    data = {"email": email, "password": password}
    if m:
        data["csrf_token"] = m.group(1)
    return client.post("/login", data=data, follow_redirects=True)


def token_from(client, path):
    """Pull a CSRF token out of a rendered page."""
    import re
    html = client.get(path, follow_redirects=True).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None
