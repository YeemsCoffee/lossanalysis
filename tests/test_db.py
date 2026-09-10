"""Database layer — runs against SQLite, and Postgres when TEST_DATABASE_URL is set."""
import pandas as pd
import pytest

from conftest import TARGET, make_tickets

OVER, OK = 400, 100


# --- schema ------------------------------------------------------------------

def test_ensure_schema_is_a_noop_once_ready(db):
    """The hot path must not re-run DDL on every query."""
    calls = []
    real = db._get_cursor

    from contextlib import contextmanager

    @contextmanager
    def counting():
        calls.append(1)
        with real() as cur:
            yield cur

    db._get_cursor = counting
    try:
        db._ensure_schema()
        assert calls == [], f"_ensure_schema hit the database {len(calls)}x"
    finally:
        db._get_cursor = real


def test_ensure_schema_recreates_when_boot_init_failed(db):
    """If init_db() failed at boot, the first query should still fix it."""
    db._schema_ready = False
    db.get_date_bounds()          # must not raise
    assert db._schema_ready is True


def test_settings_work_on_a_database_with_no_tables(db):
    """A settings read can be the first thing that ever touches the database.

    The sync CLI reads the target before it reads a ticket, so get_setting
    cannot assume some earlier query already built the schema.
    """
    with db._get_cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS settings")
    db._schema_ready = False

    assert db.get_targets()["target_seconds"] == 294   # must not raise

    db._schema_ready = False
    with db._get_cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS settings")
    db.set_setting("target_seconds", "300")            # nor on the write path
    assert db.get_targets()["target_seconds"] == 300


# --- users -------------------------------------------------------------------

def test_user_round_trip(db):
    db.create_user("mgr@yeemscoffee.com", "Manager", "pw12345678",
                   is_admin=True, location="Gardena")
    u = db.get_user_by_email("mgr@yeemscoffee.com")
    assert (u.name, u.location, u.is_admin, u.is_active) == ("Manager", "Gardena", True, True)
    assert db.get_user_by_id(u.id).email == "mgr@yeemscoffee.com"


def test_email_is_normalised(db):
    db.create_user("  MiXeD@Yeems.com ", "M", "pw12345678")
    assert db.get_user_by_email("mixed@yeems.com") is not None


def test_duplicate_email_rejected(db):
    db.create_user("dupe@yeemscoffee.com", "A", "pw12345678")
    with pytest.raises(Exception):
        db.create_user("dupe@yeemscoffee.com", "B", "pw12345678")


def test_password_is_hashed_not_stored(db):
    db.create_user("pw@yeemscoffee.com", "P", "sup3rsecret")
    u = db.get_user_by_email("pw@yeemscoffee.com")
    assert "sup3rsecret" not in u.password_hash

    from werkzeug.security import check_password_hash
    assert check_password_hash(u.password_hash, "sup3rsecret")

    db.update_user_password(u.id, "newpassword1")
    assert check_password_hash(
        db.get_user_by_email("pw@yeemscoffee.com").password_hash, "newpassword1")


def test_disable_and_relocate_user(db):
    db.create_user("t@yeemscoffee.com", "T", "pw12345678", location="Gardena")
    u = db.get_user_by_email("t@yeemscoffee.com")

    db.set_user_active(u.id, False)
    assert db.get_user_by_id(u.id).is_active is False

    db.update_user_location(u.id, "Koreatown")
    assert db.get_user_by_id(u.id).location == "Koreatown"
    db.update_user_location(u.id, None)
    assert db.get_user_by_id(u.id).location is None


# --- settings ----------------------------------------------------------------

def test_targets_default_and_update(db):
    assert db.get_targets() == {"target_seconds": 294, "target_pct": 85}
    db.set_setting("target_seconds", "300")
    db.set_setting("target_pct", "90")
    assert db.get_targets() == {"target_seconds": 300, "target_pct": 90}


# --- reset tokens ------------------------------------------------------------

def test_reset_token_lifecycle(db):
    db.create_user("r@yeemscoffee.com", "R", "pw12345678")
    uid = db.get_user_by_email("r@yeemscoffee.com").id

    token = db.create_reset_token(uid)
    assert db.get_valid_reset_token(token)["user_id"] == uid

    db.mark_token_used(token)
    assert db.get_valid_reset_token(token) is None
    assert db.get_valid_reset_token("never-issued") is None


# --- tickets -----------------------------------------------------------------

def test_ticket_round_trip_preserves_values(db):
    src = make_tickets("2026-09-01", [120, 400, 300])
    db.save_day_tickets("2026-09-01", src, "Gardena")

    got = db.get_tickets_df("2026-09-01", "2026-09-01", target_seconds=TARGET)
    got = got.sort_values("Time Created").reset_index(drop=True)
    assert list(got["Ticket Name"]) == list(src["Ticket Name"])
    assert list(got["duration"].astype(int)) == list(src["duration"])
    assert list(got["Time Created"]) == list(src["Time Created"])
    assert list(got["Time Completed"]) == list(src["Time Completed"])
    assert str(got["Time Created"].dtype).startswith("datetime64")
    assert list(got["over_target"]) == [False, True, True]
    assert list(got["hour"]) == [9, 9, 9]


def test_reupload_replaces_that_day_only(db):
    db.save_day_tickets("2026-09-01", make_tickets("2026-09-01", [OK] * 5), "Gardena")
    db.save_day_tickets("2026-09-02", make_tickets("2026-09-02", [OK] * 4), "Gardena")
    db.save_day_tickets("2026-09-01", make_tickets("2026-09-01", [OK] * 2), "Gardena")

    assert len(db.get_tickets_df("2026-09-01", "2026-09-01")) == 2
    assert len(db.get_tickets_df("2026-09-02", "2026-09-02")) == 4


def test_reupload_of_one_location_leaves_the_other(db):
    db.save_day_tickets("2026-09-01", make_tickets("2026-09-01", [OK] * 5), "Gardena")
    db.save_day_tickets("2026-09-01", make_tickets("2026-09-01", [OK] * 3), "Koreatown")
    db.save_day_tickets("2026-09-01", make_tickets("2026-09-01", [OK] * 1), "Gardena")

    assert len(db.get_tickets_df("2026-09-01", "2026-09-01", location="Gardena")) == 1
    assert len(db.get_tickets_df("2026-09-01", "2026-09-01", location="Koreatown")) == 3


def test_empty_upload_clears_the_day(db):
    df = make_tickets("2026-09-01", [OK] * 3)
    db.save_day_tickets("2026-09-01", df, "Gardena")
    db.save_day_tickets("2026-09-01", df.iloc[0:0], "Gardena")
    assert db.get_tickets_df("2026-09-01", "2026-09-01").empty


def test_missing_device_column_is_tolerated(db):
    df = make_tickets("2026-09-01", [OK] * 3).drop(columns=["Device Name"])
    db.save_day_tickets("2026-09-01", df, "Gardena")
    assert len(db.get_tickets_df("2026-09-01", "2026-09-01")) == 3


def test_date_bounds_and_distinct_dates(db):
    for day in ("2026-09-03", "2026-09-01", "2026-09-02"):
        db.save_day_tickets(day, make_tickets(day, [OK]), "Gardena")
    assert db.get_date_bounds() == ("2026-09-01", "2026-09-03")
    assert db.get_distinct_dates() == ["2026-09-03", "2026-09-02", "2026-09-01"]


def test_empty_database_returns_empty_not_error(db):
    assert db.get_tickets_df("2026-01-01", "2026-01-02").empty
    assert db.get_date_bounds() == (None, None)
    assert db.get_daily_summary("2026-01-01", "2026-01-02") == []


def test_bulk_assign_location(db):
    db.save_day_tickets("2026-09-01", make_tickets("2026-09-01", [OK] * 4), None)
    assert db.bulk_assign_location("Gardena") == 4
    assert len(db.get_tickets_df(location="Gardena")) == 4

    # Without overwrite, already-assigned rows are left alone.
    assert db.bulk_assign_location("Koreatown") == 0
    assert db.bulk_assign_location("Koreatown", overwrite=True) == 4
    assert len(db.get_tickets_df(location="Koreatown")) == 4


# --- SQL aggregation ---------------------------------------------------------

def _pandas_daily(df, target):
    """The aggregation History used to do in pandas, for comparison."""
    out = []
    for date_val, day in df.groupby("report_date"):
        out.append({
            "report_date": date_val,
            "total": len(day),
            "over_count": int((day["duration"] > target).sum()),
            "avg_seconds": round(day["duration"].mean()),
            "longest_seconds": int(day["duration"].max()),
        })
    return sorted(out, key=lambda r: r["report_date"])


@pytest.mark.parametrize("target", [294, 150, 600])
def test_daily_summary_matches_pandas(db, target):
    for day in ("2026-09-01", "2026-09-02", "2026-09-03"):
        db.save_day_tickets(day, make_tickets(day, [90, 300, 500, 294, 295]), "Gardena")

    df = db.get_tickets_df(target_seconds=target)
    sql = [{**d, "avg_seconds": round(d["avg_seconds"])}
           for d in db.get_daily_summary(target_seconds=target)]
    assert sql == _pandas_daily(df, target)


def test_daily_summary_respects_date_and_location_filters(db):
    db.save_day_tickets("2026-09-01", make_tickets("2026-09-01", [OK] * 5), "Gardena")
    db.save_day_tickets("2026-09-01", make_tickets("2026-09-01", [OK] * 3), "Koreatown")
    db.save_day_tickets("2026-09-05", make_tickets("2026-09-05", [OK] * 2), "Gardena")

    assert [d["total"] for d in db.get_daily_summary(location="Gardena")] == [5, 2]
    assert [d["total"] for d in db.get_daily_summary("2026-09-01", "2026-09-01")] == [8]


def test_source_summary_trims_and_groups(db):
    df = make_tickets("2026-09-01", [OK, OVER, OK, OVER],
                      source=["Register", " Register", "Online ", "Online"])
    db.save_day_tickets("2026-09-01", df, "Gardena")

    rows = {r["source"]: r for r in db.get_source_daily_summary(target_seconds=TARGET)}
    assert set(rows) == {"Register", "Online"}      # whitespace trimmed, not split
    assert rows["Register"]["total"] == 2
    assert rows["Register"]["over_count"] == 1


def test_hourly_summary_extracts_hour_from_stored_timestamp(db):
    morning = make_tickets("2026-09-01", [OK] * 3, hour=8)
    lunch   = make_tickets("2026-09-01", [OVER] * 2, hour=12)
    db.save_day_tickets("2026-09-01", pd.concat([morning, lunch]), "Gardena")

    rows = {r["hour"]: r for r in db.get_hourly_daily_summary(target_seconds=TARGET)}
    assert set(rows) == {8, 12}
    assert (rows[8]["total"], rows[8]["over_count"]) == (3, 0)
    assert (rows[12]["total"], rows[12]["over_count"]) == (2, 2)


def test_hourly_summary_keeps_days_separate(db):
    for day in ("2026-09-01", "2026-09-02"):
        db.save_day_tickets(day, make_tickets(day, [OK] * 2, hour=9), "Gardena")
    rows = db.get_hourly_daily_summary(target_seconds=TARGET)
    # One row per (hour, day) so the chart can average across days.
    assert sorted(r["report_date"] for r in rows) == ["2026-09-01", "2026-09-02"]


# --- Sync status -------------------------------------------------------------

def test_sync_status_round_trip(db):
    assert db.get_sync_status() == {}       # nothing recorded yet

    db.record_sync_status(ok=True, tickets=42, days=2, unmapped=[])
    status = db.get_sync_status()
    assert status["ok"] is True
    assert status["tickets"] == 42
    assert status["minutes_ago"] == 0       # just written
    assert "at" in status


def test_sync_status_keeps_only_the_latest(db):
    db.record_sync_status(ok=True, tickets=1)
    db.record_sync_status(ok=False, error="boom")
    status = db.get_sync_status()
    assert status["ok"] is False and status["error"] == "boom"
    assert "tickets" not in status          # fully replaced, not merged


def test_sync_status_survives_a_corrupt_value(db):
    """A bad value must not take the whole page down with it."""
    db.set_setting(db.SYNC_STATUS_KEY, "not json{")
    assert db.get_sync_status() == {}


# --- percentiles -------------------------------------------------------------

def test_p90_is_the_nearest_rank_value(db):
    """
    10 tickets, so the 90th percentile is the 9th smallest.

    Nearest rank rather than an interpolated one: the number on the dashboard
    should be a time some ticket actually took.
    """
    db.save_day_tickets("2026-09-01",
                        make_tickets("2026-09-01", list(range(10, 110, 10))),
                        "Gardena")
    assert db.get_duration_percentile("2026-09-01", "2026-09-01", q=90) == 90


def test_p90_matches_the_python_definition(db):
    """
    The single-day page computes p90 in pandas, History gets it from SQL.
    Two definitions of the same statistic would show two numbers for one day.
    """
    from app.analysis import percentile_seconds

    durations = [12, 480, 301, 44, 295, 78, 600, 130, 294, 210, 55, 900, 61]
    db.save_day_tickets("2026-09-01", make_tickets("2026-09-01", durations),
                        "Gardena")

    for q in (50, 75, 90, 95, 99, 100):
        assert (db.get_duration_percentile("2026-09-01", "2026-09-01", q=q)
                == percentile_seconds(durations, q)), f"q={q}"


def test_p90_groups_per_day(db):
    db.save_day_tickets("2026-09-01", make_tickets("2026-09-01", [100] * 9 + [900]),
                        "Gardena")
    db.save_day_tickets("2026-09-02", make_tickets("2026-09-02", [200] * 9 + [800]),
                        "Gardena")
    by_day = db.get_duration_percentile(group_by="report_date", q=90)
    assert by_day == {"2026-09-01": 100, "2026-09-02": 200}


def test_p90_groups_per_location(db):
    db.save_day_tickets("2026-09-01", make_tickets("2026-09-01", [100] * 10), "Gardena")
    db.save_day_tickets("2026-09-01", make_tickets("2026-09-01", [500] * 10), "Koreatown")
    assert db.get_duration_percentile(group_by="location", q=90) == {
        "Gardena": 100, "Koreatown": 500}


def test_p90_respects_the_date_and_location_filters(db):
    db.save_day_tickets("2026-09-01", make_tickets("2026-09-01", [900] * 10), "Koreatown")
    db.save_day_tickets("2026-09-02", make_tickets("2026-09-02", [100] * 10), "Gardena")
    assert db.get_duration_percentile("2026-09-02", "2026-09-02") == 100
    assert db.get_duration_percentile(location="Gardena") == 100


def test_p90_of_a_single_ticket_is_that_ticket(db):
    db.save_day_tickets("2026-09-01", make_tickets("2026-09-01", [123]), "Gardena")
    assert db.get_duration_percentile(q=90) == 123


def test_p90_of_nothing_is_zero_not_an_error(db):
    assert db.get_duration_percentile("2019-01-01", "2019-01-02", q=90) == 0
    assert db.get_duration_percentile("2019-01-01", "2019-01-02",
                                      group_by="report_date", q=90) == {}


def test_percentile_group_by_is_whitelisted(db):
    """It goes straight into the SQL string, so it cannot be caller-supplied."""
    with pytest.raises(ValueError):
        db.get_duration_percentile(group_by="duration; DROP TABLE tickets")
