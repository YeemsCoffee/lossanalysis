"""Square Reporting API client and the KDS -> ticket mapping."""
import json
import urllib.error
import urllib.request

import pytest

from app import square_api, square_sync
from app.square_sync import KDS

LOCATIONS = ["Gardena", "Koreatown"]
TARGET = 294


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def square(monkeypatch):
    """Capture requests and serve queued responses."""
    state = {"requests": [], "responses": [], "fail_with": None}

    def fake_urlopen(req, timeout=None):
        state["requests"].append({
            "url": req.full_url,
            "method": req.get_method(),
            "headers": dict(req.header_items()),
            "body": json.loads(req.data.decode()) if req.data else None,
        })
        if state["fail_with"] is not None:
            raise state["fail_with"]
        if not state["responses"]:
            return FakeResponse({"data": []})
        return FakeResponse(state["responses"].pop(0))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("SQUARE_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(square_sync, "STATION_TYPE", "")
    monkeypatch.setattr(square_sync, "LOCATION_MAP", {})
    return state


def ticket_row(key, seconds, *, name=None, location="Yeems Coffee Gardena",
               date="2026-09-01", created="2026-09-01T09:00:00Z",
               completed="2026-09-01T09:05:00Z", items=2,
               source="Register", station="expo"):
    return {
        KDS.TICKET_KEY: key,
        KDS.TICKET_NAME: name or key,
        KDS.ORDER_SOURCE: source,
        KDS.LOCATION_NAME: location,
        KDS.LOCAL_DATE: date,
        KDS.DISPLAYED_AT: created,
        KDS.CREATED_AT: created,
        KDS.COMPLETED_AT: completed,
        KDS.LINE_ITEM_COUNT: items,
        KDS.STATION_TYPE: station,
        KDS.DEVICE: "KDS-1",
        KDS.AVG_TICKET_SECS: seconds,
    }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def test_load_sends_a_wrapped_query_with_auth(square):
    square["responses"] = [{"data": [{"KDS.ticket_key": "a"}]}]
    square_api.load({"measures": ["KDS.ticket_count"]})

    sent = square["requests"][0]
    assert sent["method"] == "POST"
    assert sent["url"].endswith("/v1/load")
    # The query must be wrapped or Square answers "Query param is required".
    assert "query" in sent["body"]
    assert sent["body"]["query"]["measures"] == ["KDS.ticket_count"]
    assert sent["headers"]["Authorization"] == "Bearer test-token"


def test_load_pages_until_a_short_page(square):
    page = square_api.PAGE_SIZE
    square["responses"] = [
        {"data": [{"i": i} for i in range(page)]},      # full -> keep going
        {"data": [{"i": i} for i in range(page)]},      # full -> keep going
        {"data": [{"i": 1}]},                           # short -> stop
    ]
    rows = square_api.load({"measures": ["KDS.ticket_count"]})

    assert len(rows) == page * 2 + 1
    assert len(square["requests"]) == 3
    assert [r["body"]["query"]["offset"] for r in square["requests"]] == [0, page, page * 2]


def test_load_accepts_a_results_envelope(square):
    """Be tolerant of the beta reshaping its response."""
    square["responses"] = [{"results": [{"data": [{"KDS.ticket_key": "a"}]}]}]
    assert square_api.load({"measures": ["KDS.ticket_count"]}) == [{"KDS.ticket_key": "a"}]


def test_missing_token_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("SQUARE_ACCESS_TOKEN", raising=False)
    with pytest.raises(square_api.SquareAuthError, match="No Square access token"):
        square_api.load({"measures": ["KDS.ticket_count"]})


def test_401_explains_the_scope(square):
    square["fail_with"] = urllib.error.HTTPError(
        "u", 401, "Unauthorized", {}, __import__("io").BytesIO(b"{}"))
    with pytest.raises(square_api.SquareAuthError, match="REPORTING_READ"):
        square_api.load({"measures": ["KDS.ticket_count"]})


def test_check_access_reports_a_missing_kds_cube(square):
    square["responses"] = [{"cubes": [{"name": "Orders"}], "views": [{"name": "Sales"}]}]
    info = square_api.check_access()
    assert info["ok"] is True and info["has_kds"] is False
    assert "no KDS cube" in info["message"]

    square["responses"] = [{"cubes": [{"name": "KDS"}, {"name": "Orders"}]}]
    assert square_api.check_access()["has_kds"] is True


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------

def test_rows_become_the_upload_shape():
    df = square_sync.rows_to_df(
        [ticket_row("t1", 250.0, items=3)],
        [{KDS.TICKET_KEY: "t1", KDS.ITEM_NAME: "Latte", KDS.QUANTITY: 2},
         {KDS.TICKET_KEY: "t1", KDS.ITEM_NAME: "Bagel", KDS.QUANTITY: 1}],
        LOCATIONS)

    row = df.iloc[0]
    assert row["Ticket Name"] == "t1"
    assert row["duration"] == 250.0
    assert row["Number of Items"] == 3
    assert row["location"] == "Gardena"          # from "Yeems Coffee Gardena"
    assert row["report_date"] == "2026-09-01"
    assert row["Items in Ticket"] == "2x Latte, Bagel"
    assert str(df["Time Created"].dtype).startswith("datetime64")
    # Naive local time, matching what save_day_tickets stores for CSV uploads.
    assert df["Time Created"].dt.tz is None


def test_location_matching_and_overrides(monkeypatch):
    assert square_sync.map_location("Yeems Coffee - Koreatown", LOCATIONS) == "Koreatown"
    assert square_sync.map_location("KOREATOWN", LOCATIONS) == "Koreatown"
    assert square_sync.map_location("Somewhere Else", LOCATIONS) is None
    assert square_sync.map_location(None, LOCATIONS) is None

    monkeypatch.setattr(square_sync, "LOCATION_MAP", {"Store 42": "Gardena"})
    assert square_sync.map_location("Store 42", LOCATIONS) == "Gardena"


def test_tickets_without_timing_are_skipped():
    """An open ticket has no completion time and must not become a 0-second one."""
    rows = [ticket_row("done", 200.0),
            ticket_row("open", None),
            dict(ticket_row("nostart", 200.0), **{KDS.DISPLAYED_AT: None,
                                                  KDS.CREATED_AT: None})]
    df = square_sync.rows_to_df(rows, [], LOCATIONS)
    assert list(df["Ticket Name"]) == ["done"]


def test_square_local_date_wins_over_the_utc_timestamp():
    """A late-night ticket belongs to the store's business day, not UTC's."""
    row = ticket_row("late", 200.0, date="2026-09-01",
                     created="2026-09-02T04:30:00Z",     # still Sep 1 locally
                     completed="2026-09-02T04:33:00Z")
    df = square_sync.rows_to_df([row], [], LOCATIONS)
    assert df.iloc[0]["report_date"] == "2026-09-01"


def test_unknown_locations_are_reported_not_written(square, monkeypatch):
    written = []
    monkeypatch.setattr(square_sync, "save_day_tickets",
                        lambda d, df, loc: written.append((d, loc, len(df))))
    square["responses"] = [
        {"data": [ticket_row("a", 200.0, location="Yeems Coffee Gardena"),
                  ticket_row("b", 200.0, location="Mystery Store")]},
        {"data": []},
    ]
    summary = square_sync.sync_range("2026-09-01", "2026-09-01", LOCATIONS)

    assert summary["unmapped_locations"] == ["Mystery Store"]
    assert written == [("2026-09-01", "Gardena", 1)]


def test_dry_run_writes_nothing(square, monkeypatch):
    written = []
    monkeypatch.setattr(square_sync, "save_day_tickets",
                        lambda *a: written.append(a))
    square["responses"] = [{"data": [ticket_row("a", 200.0)]}, {"data": []}]

    summary = square_sync.sync_range("2026-09-01", "2026-09-01", LOCATIONS,
                                     dry_run=True)
    assert written == []
    assert summary["dry_run"] is True and summary["tickets"] == 1
    assert summary["days"][0]["tickets"] == 1


def test_station_filter_is_sent_when_configured(square, monkeypatch):
    monkeypatch.setattr(square_sync, "STATION_TYPE", "expo")
    square["responses"] = [{"data": []}, {"data": []}]
    square_sync.fetch_ticket_rows("2026-09-01", "2026-09-01")

    filters = square["requests"][0]["body"]["query"]["filters"]
    assert filters == [{"member": KDS.STATION_TYPE,
                        "operator": "equals", "values": ["expo"]}]


def test_no_station_filter_when_unset(square):
    square["responses"] = [{"data": []}]
    square_sync.fetch_ticket_rows("2026-09-01", "2026-09-01")
    assert square["requests"][0]["body"]["query"]["filters"] == []


def test_date_range_is_sent_as_a_time_dimension(square):
    square["responses"] = [{"data": []}]
    square_sync.fetch_ticket_rows("2026-09-01", "2026-09-07")
    td = square["requests"][0]["body"]["query"]["timeDimensions"]
    assert td == [{"dimension": KDS.TIME_FILTER,
                   "dateRange": ["2026-09-01", "2026-09-07"]}]


def test_order_is_sent_as_a_list_of_pairs(square):
    """Square rejects the {field: direction} object form Cube also documents.

    It answers a bare 400 "Invalid request" with no hint as to which part of
    the query it disliked, so keep the pair form.
    """
    square["responses"] = [{"data": []}]
    square_sync.fetch_ticket_rows("2026-09-01", "2026-09-01")
    assert square["requests"][0]["body"]["query"]["order"] == [
        [KDS.DISPLAYED_AT, "asc"]]


def test_ticket_key_is_grouped_so_measures_are_per_ticket(square):
    """Grouping by ticket key is what makes the aggregate a single ticket's time."""
    square["responses"] = [{"data": []}]
    square_sync.fetch_ticket_rows("2026-09-01", "2026-09-01")
    q = square["requests"][0]["body"]["query"]
    assert KDS.TICKET_KEY in q["dimensions"]
    assert q["measures"] == [KDS.AVG_TICKET_SECS]


# ---------------------------------------------------------------------------
# The headline metric
# ---------------------------------------------------------------------------

def test_on_time_pct_counts_at_or_under_target():
    df = square_sync.rows_to_df(
        [ticket_row("a", 100.0), ticket_row("b", TARGET),      # exactly on target
         ticket_row("c", TARGET + 1), ticket_row("d", 500.0)],
        [], LOCATIONS)
    # 2 of 4 at or under 294s.
    assert square_sync._on_time_pct(df, TARGET) == 50.0


def test_validate_reports_a_match(square, monkeypatch):
    stored = square_sync.rows_to_df(
        [ticket_row("a", 100.0), ticket_row("b", 400.0)], [], LOCATIONS)
    monkeypatch.setattr(square_sync, "get_tickets_df", lambda *a, **k: stored)
    square["responses"] = [
        {"data": [ticket_row("a", 100.0), ticket_row("b", 400.0)]},
        {"data": []},
    ]
    res = square_sync.validate_against_csv("2026-09-01", LOCATIONS,
                                           target_seconds=TARGET)
    assert res["verdict"] == "match"
    assert res["stored_on_time_pct"] == res["api_on_time_pct"] == 50.0
    assert res["on_time_difference"] == 0.0


def test_validate_flags_a_shifted_start_point(square, monkeypatch):
    """Same tickets, every duration inflated — the metric must not silently pass."""
    stored = square_sync.rows_to_df(
        [ticket_row(k, 200.0) for k in "abcd"], [], LOCATIONS)
    monkeypatch.setattr(square_sync, "get_tickets_df", lambda *a, **k: stored)
    square["responses"] = [
        {"data": [ticket_row(k, 400.0) for k in "abcd"]},   # all now over target
        {"data": []},
    ]
    res = square_sync.validate_against_csv("2026-09-01", LOCATIONS,
                                           target_seconds=TARGET)
    assert res["verdict"] == "counts-match-metric-differs"
    assert res["stored_on_time_pct"] == 100.0
    assert res["api_on_time_pct"] == 0.0
    assert res["flipped_tickets"] == 4
    assert "different start point" in " ".join(res["notes"])


def test_validate_flags_double_counted_stations(square, monkeypatch):
    stored = square_sync.rows_to_df(
        [ticket_row(k, 100.0) for k in "abcd"], [], LOCATIONS)
    monkeypatch.setattr(square_sync, "get_tickets_df", lambda *a, **k: stored)
    # Same tickets twice, once per station type.
    doubled = ([ticket_row(k, 100.0, station="prep") for k in "abcd"] +
               [ticket_row(k, 100.0, station="expo") for k in "abcd"])
    square["responses"] = [{"data": doubled}, {"data": []}]

    res = square_sync.validate_against_csv("2026-09-01", LOCATIONS,
                                           target_seconds=TARGET)
    assert res["verdict"] == "mismatch"
    assert res["api_tickets"] == 8 and res["stored_tickets"] == 4
    assert sorted(res["station_types"]) == ["expo", "prep"]
    assert "SQUARE_STATION_TYPE" in " ".join(res["notes"])


def test_validate_needs_something_to_compare(square, monkeypatch):
    import pandas as pd
    monkeypatch.setattr(square_sync, "get_tickets_df",
                        lambda *a, **k: pd.DataFrame())
    res = square_sync.validate_against_csv("2019-01-01", LOCATIONS,
                                           target_seconds=TARGET)
    assert res["verdict"] == "no-csv-data"


# ---------------------------------------------------------------------------
# Frequent syncing
# ---------------------------------------------------------------------------

def test_recent_excludes_today_by_default(square, monkeypatch):
    """The nightly-style run should not pull a day still in progress."""
    seen = {}
    monkeypatch.setattr(square_sync, "sync_range",
                        lambda f, t, *a, **k: seen.update(from_=f, to=t) or {})
    import datetime as dt

    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 8)
    monkeypatch.setattr(square_sync, "date", FixedDate)

    square_sync.sync_recent(2, LOCATIONS)
    assert seen == {"from_": "2026-09-06", "to": "2026-09-07"}


def test_recent_includes_today_when_asked(square, monkeypatch):
    """
    The 15-minute job must cover today, and reach back a day.

    This box runs UTC while the stores run Pacific, so during a Pacific evening
    the server's "today" is already the store's tomorrow; a one-day window
    would miss the dinner rush entirely.
    """
    seen = {}
    monkeypatch.setattr(square_sync, "sync_range",
                        lambda f, t, *a, **k: seen.update(from_=f, to=t) or {})
    import datetime as dt

    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 8)
    monkeypatch.setattr(square_sync, "date", FixedDate)

    square_sync.sync_recent(2, LOCATIONS, include_today=True)
    assert seen == {"from_": "2026-09-07", "to": "2026-09-08"}


@pytest.fixture
def fixed_today(monkeypatch):
    """Pin date.today() to 2026-09-08 and capture the range sync_range gets."""
    import datetime as dt

    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 8)

    monkeypatch.setattr(square_sync, "date", FixedDate)
    seen = {}
    monkeypatch.setattr(square_sync, "sync_range",
                        lambda f, t, *a, **k: seen.update(from_=f, to=t) or {})
    return seen


def _last_seen(monkeypatch, mapping):
    monkeypatch.setattr(square_sync, "get_last_date_by_location",
                        lambda: mapping)


def test_catch_up_reaches_back_to_the_furthest_behind_location(
        square, monkeypatch, fixed_today):
    """One location stalling must widen the window, even if the other is current."""
    _last_seen(monkeypatch, {"Gardena": "2026-09-08", "Koreatown": "2026-09-04"})
    square_sync.sync_recent(2, LOCATIONS, include_today=True, catch_up=True)
    assert fixed_today == {"from_": "2026-09-04", "to": "2026-09-08"}


def test_catch_up_never_narrows_the_requested_window(
        square, monkeypatch, fixed_today):
    """Everything current still syncs the normal two days, not just today."""
    _last_seen(monkeypatch, {"Gardena": "2026-09-08", "Koreatown": "2026-09-08"})
    square_sync.sync_recent(2, LOCATIONS, include_today=True, catch_up=True)
    assert fixed_today == {"from_": "2026-09-07", "to": "2026-09-08"}


def test_a_location_with_no_data_gets_the_cap_not_all_of_history(
        square, monkeypatch, fixed_today):
    """
    A cold start must not try to pull the ~2 years of KDS history in one
    query on a 15-minute schedule.
    """
    _last_seen(monkeypatch, {"Gardena": "2026-09-08"})   # Koreatown unknown
    monkeypatch.setattr(square_sync, "CATCH_UP_MAX_DAYS", 14)
    square_sync.sync_recent(2, LOCATIONS, include_today=True, catch_up=True)
    assert fixed_today == {"from_": "2026-08-26", "to": "2026-09-08"}


def test_a_gap_beyond_the_cap_is_reported_not_silently_shortened(
        square, monkeypatch, fixed_today):
    """Covering less than asked must be visible, or the hole looks filled."""
    _last_seen(monkeypatch, {"Gardena": "2026-09-08", "Koreatown": "2026-01-02"})
    monkeypatch.setattr(square_sync, "CATCH_UP_MAX_DAYS", 14)
    summary = square_sync.sync_recent(2, LOCATIONS, include_today=True,
                                      catch_up=True)
    assert fixed_today["from_"] == "2026-08-26"
    assert summary["gap_beyond_reach"] == "2026-01-02"


def test_catch_up_is_off_unless_asked(square, monkeypatch, fixed_today):
    _last_seen(monkeypatch, {"Gardena": "2026-01-02"})
    square_sync.sync_recent(2, LOCATIONS, include_today=True)
    assert fixed_today == {"from_": "2026-09-07", "to": "2026-09-08"}


def test_a_malformed_stored_date_does_not_break_the_sync(
        square, monkeypatch, fixed_today):
    """A junk report_date should fall back to the cap, not raise."""
    _last_seen(monkeypatch, {"Gardena": "not-a-date", "Koreatown": "2026-09-08"})
    monkeypatch.setattr(square_sync, "CATCH_UP_MAX_DAYS", 14)
    square_sync.sync_recent(2, LOCATIONS, include_today=True, catch_up=True)
    assert fixed_today == {"from_": "2026-08-26", "to": "2026-09-08"}


# ---------------------------------------------------------------------------
# Time zones
# ---------------------------------------------------------------------------

def test_utc_timestamps_become_store_local_clock_times():
    """
    Square reports UTC; every page reads these as store time.

    Dropping the zone without converting kept the UTC reading, so a 9am
    ticket showed as 4pm — a time that had not happened yet.
    """
    row = ticket_row("morning", 300.0, date="2026-09-10",
                     created="2026-09-10T16:00:00Z",       # 9:00 PDT
                     completed="2026-09-10T16:05:00Z")
    df = square_sync.rows_to_df([row], [], LOCATIONS)
    assert str(df.iloc[0]["Time Created"])   == "2026-09-10 09:00:00"
    assert str(df.iloc[0]["Time Completed"]) == "2026-09-10 09:05:00"


def test_the_hour_used_by_the_charts_is_the_store_hour():
    """The hour-of-day charts were shifted by the whole UTC offset."""
    row = ticket_row("rush", 300.0, date="2026-09-10",
                     created="2026-09-10T15:30:00Z",       # 8:30 PDT
                     completed="2026-09-10T15:35:00Z")
    df = square_sync.rows_to_df([row], [], LOCATIONS)
    assert df.iloc[0]["Time Created"].hour == 8


def test_daylight_saving_offset_is_not_hardcoded():
    """Pacific is UTC-7 in September and UTC-8 in January."""
    summer = square_sync.rows_to_df(
        [ticket_row("s", 300.0, date="2026-09-10",
                    created="2026-09-10T17:00:00Z",
                    completed="2026-09-10T17:05:00Z")], [], LOCATIONS)
    winter = square_sync.rows_to_df(
        [ticket_row("w", 300.0, date="2026-01-10",
                    created="2026-01-10T17:00:00Z",
                    completed="2026-01-10T17:05:00Z")], [], LOCATIONS)
    assert summer.iloc[0]["Time Created"].hour == 10   # PDT, UTC-7
    assert winter.iloc[0]["Time Created"].hour == 9    # PST, UTC-8


def test_store_timezone_is_configurable(monkeypatch):
    monkeypatch.setattr(square_sync, "STORE_TZ", "America/New_York")
    df = square_sync.rows_to_df(
        [ticket_row("e", 300.0, date="2026-09-10",
                    created="2026-09-10T16:00:00Z",
                    completed="2026-09-10T16:05:00Z")], [], LOCATIONS)
    assert df.iloc[0]["Time Created"].hour == 12       # EDT, UTC-4


def test_a_synced_ticket_is_never_in_the_future():
    """The symptom that started this: today's tickets dated hours ahead."""
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    df = square_sync.rows_to_df(
        [ticket_row("now", 300.0, date=now.date().isoformat(),
                    created=stamp, completed=stamp)], [], LOCATIONS)

    from zoneinfo import ZoneInfo
    store_now = now.astimezone(ZoneInfo(square_sync.STORE_TZ)).replace(tzinfo=None)
    assert df.iloc[0]["Time Created"] <= store_now


def test_evening_pacific_ticket_lands_on_the_right_business_day():
    """
    18:30 Pacific on Sep 7 is 01:30 UTC on Sep 8. Square's local_date says
    Sep 7, and that must win — otherwise a dinner rush is filed under tomorrow.
    """
    row = ticket_row("dinner", 320.0, date="2026-09-07",
                     created="2026-09-08T01:30:00Z",
                     completed="2026-09-08T01:35:20Z")
    df = square_sync.rows_to_df([row], [], LOCATIONS)
    assert df.iloc[0]["report_date"] == "2026-09-07"


def test_resyncing_a_day_replaces_rather_than_appends(square, monkeypatch):
    """Re-running every 15 minutes must not pile up duplicate rows."""
    calls = []
    monkeypatch.setattr(square_sync, "save_day_tickets",
                        lambda d, df, loc: calls.append((d, loc, len(df))))
    for tickets in (2, 5):
        square["responses"] = [
            {"data": [ticket_row(f"t{i}", 200.0) for i in range(tickets)]},
            {"data": []},
        ]
        square_sync.sync_range("2026-09-01", "2026-09-01", LOCATIONS)

    # save_day_tickets is delete-then-insert per (date, location), so the
    # second run supersedes the first rather than adding to it.
    assert calls == [("2026-09-01", "Gardena", 2), ("2026-09-01", "Gardena", 5)]


def test_open_tickets_are_excluded_until_they_complete():
    """Mid-service there are always tickets still on the pass."""
    rows = [ticket_row("closed", 250.0), ticket_row("still-cooking", None)]
    df = square_sync.rows_to_df(rows, [], LOCATIONS)
    assert list(df["Ticket Name"]) == ["closed"]


# ---------------------------------------------------------------------------
# The write gate
# ---------------------------------------------------------------------------

def test_sync_refuses_to_write_without_the_enable_flag(square, monkeypatch, capsys):
    monkeypatch.delenv("SQUARE_SYNC_ENABLED", raising=False)
    called = []
    monkeypatch.setattr(square_sync, "sync_recent",
                        lambda *a, **k: called.append(1) or {})

    assert square_sync.main(["recent", "--days", "2"]) == 0
    assert called == []
    assert "SQUARE_SYNC_ENABLED is not set" in capsys.readouterr().out


def test_dry_run_works_without_the_enable_flag(square, monkeypatch):
    monkeypatch.delenv("SQUARE_SYNC_ENABLED", raising=False)
    called = []
    monkeypatch.setattr(square_sync, "sync_recent", lambda *a, **k: called.append(1) or {
        "dry_run": True, "tickets": 0, "from": "x", "to": "y",
        "days": [], "unmapped_locations": []})

    assert square_sync.main(["recent", "--dry-run"]) == 0
    assert called == [1]


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on"])
def test_enable_flag_accepts_the_usual_spellings(square, monkeypatch, flag):
    monkeypatch.setenv("SQUARE_SYNC_ENABLED", flag)
    called = []
    monkeypatch.setattr(square_sync, "sync_recent", lambda *a, **k: called.append(1) or {
        "dry_run": False, "tickets": 0, "from": "x", "to": "y",
        "days": [], "unmapped_locations": []})

    assert square_sync.main(["recent"]) == 0
    assert called == [1]


def test_validate_does_not_need_the_enable_flag(square, monkeypatch):
    """Read-only commands must stay usable while the gate is shut."""
    monkeypatch.delenv("SQUARE_SYNC_ENABLED", raising=False)
    called = []
    monkeypatch.setattr(square_sync, "validate_against_csv",
                        lambda *a, **k: called.append(1) or {
                            "verdict": "no-api-data", "date": "2026-09-01",
                            "notes": ["x"], "station_types": [],
                            "square_locations": []})

    square_sync.main(["validate", "2026-09-01"])
    assert called == [1]


def test_a_bad_timezone_name_fails_loudly(monkeypatch):
    """Silently falling back to UTC would reintroduce the future-times bug."""
    monkeypatch.setattr(square_sync, "STORE_TZ", "Mars/Olympus_Mons")
    with pytest.raises(ValueError, match="STORE_TIMEZONE"):
        square_sync.rows_to_df([ticket_row("x", 300.0)], [], LOCATIONS)
