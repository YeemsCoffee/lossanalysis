"""Routes: auth, authorization, CSRF, upload, and the reporting pages."""
import io
import re

import pandas as pd
import pytest

from conftest import login, make_tickets, token_from

OVER, OK = 400, 100


def csv_bytes(rows):
    """A CSV shaped like Square's kitchen export."""
    return io.BytesIO(pd.DataFrame(rows).to_csv(index=False).encode())


def square_rows(day, durations, hour=9):
    return [{
        "Ticket Name": f"T{i}",
        "Order Source": "Register",
        "Number of Items": 2,
        "Items in Ticket": "Latte, Bagel",
        "Completion Time (seconds)": d,
        "Time Created":   f"{day} {hour:02d}:{i:02d}:00",
        "Time Completed": f"{day} {hour:02d}:{i:02d}:{d % 60:02d}",
        "Device Name": "KDS1",
    } for i, d in enumerate(durations)]


# --- authentication ----------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/history", "/drivers", "/patterns",
                                  "/admin/users", "/admin/settings",
                                  "/day/2026-09-01"])
def test_pages_require_login(client, path):
    r = client.get(path)
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_login_and_logout(client):
    r = login(client)
    assert r.status_code == 200 and b"Sign Out" in r.data

    r = client.post("/logout", data={"csrf_token": token_from(client, "/")},
                    follow_redirects=True)
    assert r.status_code == 200
    assert client.get("/").status_code == 302      # back to needing a login


def test_bad_password_rejected(client):
    r = login(client, password="wrongpassword")
    assert b"Sign Out" not in r.data
    assert b"Invalid email or password" in r.data


def test_disabled_user_cannot_log_in(client, sqlite_db):
    sqlite_db.create_user("gone@yeemscoffee.com", "Gone", "pw12345678")
    u = sqlite_db.get_user_by_email("gone@yeemscoffee.com")
    sqlite_db.set_user_active(u.id, False)

    r = login(client, email="gone@yeemscoffee.com")
    assert b"Sign Out" not in r.data


# --- authorization -----------------------------------------------------------

def test_manager_cannot_reach_admin_pages(client, sqlite_db):
    sqlite_db.create_user("mgr@yeemscoffee.com", "Mgr", "pw12345678",
                          is_admin=False, location="Gardena")
    login(client, email="mgr@yeemscoffee.com")

    for path in ["/admin/users", "/admin/settings"]:
        assert client.get(path).status_code == 403, f"{path} was not blocked"


def test_manager_can_reach_reporting_pages(client, sqlite_db):
    sqlite_db.create_user("mgr@yeemscoffee.com", "Mgr", "pw12345678", is_admin=False)
    login(client, email="mgr@yeemscoffee.com")

    for path in ["/", "/history", "/drivers"]:
        assert client.get(path).status_code == 200


# --- CSRF --------------------------------------------------------------------

@pytest.mark.parametrize("path,data", [
    ("/admin/users/create", {"email": "e@x.com", "name": "E", "password": "pw12345678"}),
    ("/admin/settings", {"minutes": "1", "seconds": "0", "target_pct": "99"}),
    ("/admin/data/assign-location", {"location": "Gardena", "overwrite": "1"}),
    ("/logout", {}),
])
def test_post_without_csrf_token_is_rejected(client, path, data):
    login(client)
    assert client.post(path, data=data).status_code == 400


def test_forged_settings_post_has_no_effect(client, sqlite_db):
    login(client)
    client.post("/admin/settings", data={"minutes": "1", "seconds": "0", "target_pct": "99"})
    assert sqlite_db.get_targets() == {"target_seconds": 294, "target_pct": 85}


def test_settings_update_with_token_works(client, sqlite_db):
    login(client)
    r = client.post("/admin/settings",
                    data={"minutes": "5", "seconds": "30", "target_pct": "90",
                          "csrf_token": token_from(client, "/admin/settings")},
                    follow_redirects=True)
    assert r.status_code == 200
    assert sqlite_db.get_targets() == {"target_seconds": 330, "target_pct": 90}


def test_every_rendered_post_form_carries_a_token(client):
    login(client)
    for path in ["/", "/admin/users", "/admin/settings", "/history"]:
        html = client.get(path, follow_redirects=True).get_data(as_text=True)
        forms = len(re.findall(r'<form[^>]*method="post"', html, re.I))
        tokens = len(re.findall(r'name="csrf_token"', html))
        assert tokens >= forms, f"{path}: {forms} forms but only {tokens} tokens"


# --- upload ------------------------------------------------------------------

def test_single_day_upload_shows_results(client, sqlite_db):
    login(client)
    r = client.post("/analyze", data={
        "location": "Gardena",
        "csrf_token": token_from(client, "/"),
        "report": (csv_bytes(square_rows("2026-09-01", [OK] * 8 + [OVER] * 2)), "r.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)

    assert r.status_code == 200
    assert b"Daily Performance" in r.data
    assert len(sqlite_db.get_tickets_df("2026-09-01", "2026-09-01")) == 10


def test_multi_day_upload_splits_by_day(client, sqlite_db):
    """A month-long export must land on its own dates, not all on day one."""
    login(client)
    rows = (square_rows("2026-09-01", [OK] * 3)
            + square_rows("2026-09-02", [OK] * 4)
            + square_rows("2026-09-03", [OVER] * 5))
    r = client.post("/analyze", data={
        "location": "Gardena",
        "csrf_token": token_from(client, "/"),
        "report": (csv_bytes(rows), "month.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)

    assert r.status_code == 200
    assert [d["total"] for d in sqlite_db.get_daily_summary()] == [3, 4, 5]
    assert sqlite_db.get_distinct_dates() == ["2026-09-03", "2026-09-02", "2026-09-01"]


def test_upload_records_the_chosen_location(client, sqlite_db):
    login(client)
    client.post("/analyze", data={
        "location": "Koreatown",
        "csrf_token": token_from(client, "/"),
        "report": (csv_bytes(square_rows("2026-09-01", [OK] * 3)), "r.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)

    assert len(sqlite_db.get_tickets_df(location="Koreatown")) == 3
    assert sqlite_db.get_tickets_df(location="Gardena").empty


def test_manager_uploads_are_pinned_to_their_location(client, sqlite_db):
    """A manager assigned to Gardena can't file a report under Koreatown."""
    sqlite_db.create_user("g@yeemscoffee.com", "G", "pw12345678", location="Gardena")
    login(client, email="g@yeemscoffee.com")

    client.post("/analyze", data={
        "location": "Koreatown",                      # attempt to override
        "csrf_token": token_from(client, "/"),
        "report": (csv_bytes(square_rows("2026-09-01", [OK] * 3)), "r.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)

    assert len(sqlite_db.get_tickets_df(location="Gardena")) == 3
    assert sqlite_db.get_tickets_df(location="Koreatown").empty


def test_upload_rejects_a_non_csv(client):
    login(client)
    r = client.post("/analyze", data={
        "location": "Gardena",
        "csrf_token": token_from(client, "/"),
        "report": (io.BytesIO(b"not a csv"), "notes.txt"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert b"Please upload a .csv file" in r.data


def test_upload_without_a_location_is_rejected(client, sqlite_db):
    login(client)
    r = client.post("/analyze", data={
        "location": "",
        "csrf_token": token_from(client, "/"),
        "report": (csv_bytes(square_rows("2026-09-01", [OK])), "r.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert b"valid location" in r.data
    assert sqlite_db.get_tickets_df().empty


# --- reporting pages ---------------------------------------------------------

@pytest.fixture
def seeded(client, sqlite_db):
    login(client)
    for day in ("2026-09-01", "2026-09-02"):
        sqlite_db.save_day_tickets(day, make_tickets(day, [OK] * 6 + [OVER] * 4), "Gardena")
        sqlite_db.save_day_tickets(day, make_tickets(day, [OK] * 5), "Koreatown")
    return client


@pytest.mark.parametrize("path", [
    "/history", "/drivers", "/patterns", "/history?location=Gardena",
    "/drivers?location=Koreatown", "/patterns?location=Gardena",
    "/day/2026-09-01", "/history?from=2026-09-01&to=2026-09-01",
    "/patterns?from=2026-09-01&to=2026-09-01",
])
def test_reporting_pages_render(seeded, path):
    r = seeded.get(path, follow_redirects=True)
    assert r.status_code == 200
    assert b"Internal Server Error" not in r.data


def test_patterns_renders_every_section(seeded):
    html = seeded.get("/patterns").get_data(as_text=True)
    for heading in ("Day of the Week", "Weekly Hot Spots", "Gardena vs Koreatown"):
        assert heading in html, f"missing section: {heading}"


def test_patterns_compares_both_stores_even_when_filtered(seeded):
    """The comparison is the point of the section — a tab filter shouldn't empty it."""
    html = seeded.get("/patterns?location=Gardena").get_data(as_text=True)
    assert "Koreatown" in html
    assert "nothing to compare" not in html


def test_patterns_with_one_location_says_so(client, sqlite_db):
    login(client)
    sqlite_db.save_day_tickets("2026-09-01",
                               make_tickets("2026-09-01", [OK] * 5), "Gardena")
    html = client.get("/patterns").get_data(as_text=True)
    assert "nothing to compare" in html


def test_patterns_empty_invites_an_upload(client):
    login(client)
    assert "Upload a Report" in client.get("/patterns").get_data(as_text=True)


def test_history_shows_both_days_and_the_right_totals(seeded):
    html = seeded.get("/history").get_data(as_text=True)
    assert "2026-09-01" in html and "2026-09-02" in html
    assert "30" in html                      # 2 days x (10 Gardena + 5 Koreatown)


def test_history_location_filter_changes_the_numbers(seeded):
    both = seeded.get("/history").get_data(as_text=True)
    kt = seeded.get("/history?location=Koreatown").get_data(as_text=True)
    assert both != kt


def test_drivers_renders_every_section(seeded):
    html = seeded.get("/drivers").get_data(as_text=True)
    for heading in ("Kitchen Load Threshold", "Cascade Effect",
                    "Problem Items", "Order-Size Threshold"):
        assert heading in html, f"missing section: {heading}"


def test_day_view_includes_clusters(seeded, sqlite_db):
    sqlite_db.save_day_tickets("2026-09-04",
                               make_tickets("2026-09-04", [OVER] * 5), "Gardena")
    html = seeded.get("/day/2026-09-04").get_data(as_text=True)
    assert "tickets in a row" in html


def test_day_with_no_data_redirects_to_history(seeded):
    r = seeded.get("/day/2019-01-01", follow_redirects=True)
    assert b"No data found" in r.data


def test_empty_history_invites_an_upload(client):
    login(client)
    html = client.get("/history").get_data(as_text=True)
    assert "Upload a Report" in html


# --- Loss Drivers is bounded --------------------------------------------------
#
# Unlike History and Patterns, this page needs every ticket row, so its cost
# grows with the range. Left unbounded it eventually exceeds the request
# timeout and the browser gets a 504 with nothing useful in the log.

def test_drivers_defaults_to_a_recent_window_not_all_history(client, sqlite_db,
                                                             monkeypatch):
    import app.routes as routes
    monkeypatch.setattr(routes, "DRIVER_DEFAULT_DAYS", 30)
    login(client)

    # One old day and one recent one.
    sqlite_db.save_day_tickets("2026-01-01",
                               make_tickets("2026-01-01", [OK] * 3), "Gardena")
    sqlite_db.save_day_tickets("2026-09-01",
                               make_tickets("2026-09-01", [OK] * 4), "Gardena")

    ranges = []
    real = routes.count_tickets
    monkeypatch.setattr(routes, "count_tickets",
                        lambda f, t, location=None: ranges.append((f, t)) or real(f, t, location))

    client.get("/drivers")
    from_date, to_date = ranges[0]
    assert to_date == "2026-09-01"
    assert from_date == "2026-08-03"        # 30 days back from the latest day
    assert from_date > "2026-01-01", "still reaching back over all history"


def test_drivers_honours_an_explicit_range(client, sqlite_db, monkeypatch):
    """The default is a default, not a cap — the picker still reaches back."""
    import app.routes as routes
    login(client)
    sqlite_db.save_day_tickets("2026-01-01",
                               make_tickets("2026-01-01", [OK, OVER]), "Gardena")

    r = client.get("/drivers?from=2026-01-01&to=2026-01-01")
    assert r.status_code == 200
    assert b"Kitchen Load Threshold" in r.data


def test_drivers_asks_for_a_narrower_range_instead_of_timing_out(client, sqlite_db,
                                                                 monkeypatch):
    import app.routes as routes
    monkeypatch.setattr(routes, "DRIVER_MAX_TICKETS", 5)
    login(client)
    sqlite_db.save_day_tickets("2026-09-01",
                               make_tickets("2026-09-01", [OK] * 20), "Gardena")

    r = client.get("/drivers?from=2026-09-01&to=2026-09-01")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "That&#39;s a lot of tickets" in html or "a lot of tickets" in html
    assert "20" in html                       # tells them the actual count
    # And it must not have attempted the analysis.
    assert "Kitchen Load Threshold" not in html


def test_drivers_does_not_load_tickets_when_over_the_limit(client, sqlite_db,
                                                           monkeypatch):
    """The guard has to run before the expensive load, or it saves nothing."""
    import app.routes as routes
    monkeypatch.setattr(routes, "DRIVER_MAX_TICKETS", 5)
    login(client)
    sqlite_db.save_day_tickets("2026-09-01",
                               make_tickets("2026-09-01", [OK] * 20), "Gardena")

    loaded = []
    monkeypatch.setattr(routes, "get_tickets_df",
                        lambda *a, **k: loaded.append(1) or (_ for _ in ()).throw(
                            AssertionError("tickets were loaded despite the guard")))

    assert client.get("/drivers?from=2026-09-01&to=2026-09-01").status_code == 200
    assert loaded == []


def test_drivers_renders_normally_under_the_limit(client, sqlite_db, monkeypatch):
    import app.routes as routes
    monkeypatch.setattr(routes, "DRIVER_MAX_TICKETS", 500)
    login(client)
    sqlite_db.save_day_tickets("2026-09-01",
                               make_tickets("2026-09-01", [OK] * 6 + [OVER] * 4),
                               "Gardena")

    html = client.get("/drivers?from=2026-09-01&to=2026-09-01").get_data(as_text=True)
    assert "Kitchen Load Threshold" in html
    assert "a lot of tickets" not in html


# --- Clicking through from History keeps the location -------------------------

@pytest.fixture
def two_stores(client, sqlite_db):
    """Gardena clearly worse than Koreatown, so combining them is visible."""
    login(client)
    sqlite_db.save_day_tickets("2026-09-01",
                               make_tickets("2026-09-01", [OVER] * 8 + [OK] * 2),
                               "Gardena")          # 20% on target
    sqlite_db.save_day_tickets("2026-09-01",
                               make_tickets("2026-09-01", [OK] * 10),
                               "Koreatown")        # 100% on target
    return client


def test_day_view_honours_the_location_filter(two_stores):
    gardena = two_stores.get("/day/2026-09-01?location=Gardena").get_data(as_text=True)
    ktown   = two_stores.get("/day/2026-09-01?location=Koreatown").get_data(as_text=True)

    # 10 tickets each, not 20 — the other store must not be mixed in.
    assert ">10<" in gardena and ">10<" in ktown
    assert "20.0%" in gardena, "Gardena's own on-target rate"
    assert "100.0%" in ktown, "Koreatown's own on-target rate"
    assert gardena != ktown


def test_day_view_matches_the_history_row_that_was_clicked(two_stores, sqlite_db):
    """
    The bug this guards: History filtered to one store showed that store's
    numbers, but clicking the row landed on a page combining both, so the
    figures disagreed with the row that was clicked.
    """
    row = sqlite_db.get_daily_summary("2026-09-01", "2026-09-01",
                                      location="Gardena")[0]
    row_pct = round((row["total"] - row["over_count"]) / row["total"] * 100, 1)

    html = two_stores.get("/day/2026-09-01?location=Gardena").get_data(as_text=True)
    assert f"{row_pct}%" in html, f"day page disagrees with the {row_pct}% row"


def test_history_row_link_carries_the_location(two_stores):
    html = two_stores.get("/history?location=Gardena").get_data(as_text=True)
    assert "/day/2026-09-01?location=Gardena" in html.replace("&amp;", "&")


def test_unfiltered_history_links_to_the_combined_day(two_stores):
    html = two_stores.get("/history").get_data(as_text=True)
    assert "/day/2026-09-01'" in html or '/day/2026-09-01"' in html
    combined = two_stores.get("/day/2026-09-01").get_data(as_text=True)
    assert ">20<" in combined, "unfiltered day should cover both stores"


def test_day_view_names_the_location_it_is_showing(two_stores):
    """Otherwise there is nothing on the page saying which store it is."""
    assert "Gardena" in two_stores.get("/day/2026-09-01?location=Gardena").get_data(as_text=True)
    assert "all locations" in two_stores.get("/day/2026-09-01").get_data(as_text=True)


def test_back_link_returns_to_the_filtered_history(two_stores):
    html = two_stores.get("/day/2026-09-01?location=Gardena").get_data(as_text=True)
    assert "/history?location=Gardena" in html.replace("&amp;", "&")


def test_day_with_no_data_for_that_location_says_so(two_stores, sqlite_db):
    sqlite_db.save_day_tickets("2026-09-05",
                               make_tickets("2026-09-05", [OK] * 3), "Gardena")
    r = two_stores.get("/day/2026-09-05?location=Koreatown", follow_redirects=True)
    assert b"No data found" in r.data
    assert b"Koreatown" in r.data


def test_bogus_location_is_ignored_not_trusted(two_stores):
    """A hand-typed location must not filter to nothing or reach the query."""
    html = two_stores.get("/day/2026-09-01?location=Nowhere").get_data(as_text=True)
    assert ">20<" in html          # falls back to all locations
    assert "all locations" in html


# --- Is the automatic sync actually running? ----------------------------------
#
# The failure worth designing for isn't a loud crash, it's silence: a sync that
# quietly stops leaves the app showing the last good data as though current.

def test_no_banner_before_syncing_is_set_up(client, sqlite_db):
    login(client)
    sqlite_db.save_day_tickets("2026-09-01",
                               make_tickets("2026-09-01", [OK] * 3), "Gardena")
    html = client.get("/history").get_data(as_text=True)
    assert "Synced from Square" not in html
    assert "sync-status" not in html


def test_recent_sync_is_reported(client, sqlite_db):
    login(client)
    sqlite_db.save_day_tickets("2026-09-01",
                               make_tickets("2026-09-01", [OK] * 3), "Gardena")
    sqlite_db.record_sync_status(ok=True, tickets=120, days=2,
                                 range="2026-09-01 to 2026-09-02", unmapped=[])

    html = client.get("/history").get_data(as_text=True)
    assert "Synced from Square" in html
    assert "120" in html
    assert "sync-status ok" in html


def test_a_failing_sync_is_called_out(client, sqlite_db):
    login(client)
    sqlite_db.save_day_tickets("2026-09-01",
                               make_tickets("2026-09-01", [OK] * 3), "Gardena")
    sqlite_db.record_sync_status(ok=False,
                                 error="SquareAuthError: token rejected (401)")

    html = client.get("/history").get_data(as_text=True)
    assert "Square sync is failing" in html
    assert "may be out of date" in html
    assert "401" in html                      # the actual reason, not just "error"


def test_a_silent_gap_during_service_is_flagged(client, sqlite_db, monkeypatch):
    """A sync that stopped two hours ago mid-service is the dangerous case."""
    import app.routes as routes
    from datetime import datetime as real_datetime

    login(client)
    sqlite_db.save_day_tickets("2026-09-01",
                               make_tickets("2026-09-01", [OK] * 3), "Gardena")
    old = (real_datetime.now() - __import__("datetime").timedelta(hours=2))
    sqlite_db.record_sync_status(ok=True, tickets=10, days=1, unmapped=[],
                                 at=old.strftime("%Y-%m-%d %H:%M:%S"))

    class Midday(real_datetime):
        @classmethod
        def now(cls):
            return real_datetime.now().replace(hour=12)
    monkeypatch.setattr(routes, "datetime", Midday)

    html = client.get("/history").get_data(as_text=True)
    assert "No sync in" in html
    assert "sync-status warn" in html


def test_the_same_gap_overnight_is_not_flagged(client, sqlite_db, monkeypatch):
    """Nothing to pull from a closed kitchen — warning then trains people to ignore it."""
    import app.routes as routes
    from datetime import datetime as real_datetime

    login(client)
    sqlite_db.save_day_tickets("2026-09-01",
                               make_tickets("2026-09-01", [OK] * 3), "Gardena")
    old = (real_datetime.now() - __import__("datetime").timedelta(hours=2))
    sqlite_db.record_sync_status(ok=True, tickets=10, days=1, unmapped=[],
                                 at=old.strftime("%Y-%m-%d %H:%M:%S"))

    class Midnight(real_datetime):
        @classmethod
        def now(cls):
            return real_datetime.now().replace(hour=2)
    monkeypatch.setattr(routes, "datetime", Midnight)

    html = client.get("/history").get_data(as_text=True)
    assert "No sync in" not in html


def test_unmapped_locations_are_surfaced(client, sqlite_db):
    """Tickets silently skipped for an unknown store name need to be visible."""
    login(client)
    sqlite_db.save_day_tickets("2026-09-01",
                               make_tickets("2026-09-01", [OK] * 3), "Gardena")
    sqlite_db.record_sync_status(ok=True, tickets=50, days=1,
                                 unmapped=["Yeems Third Store"])

    html = client.get("/history").get_data(as_text=True)
    assert "Yeems Third Store" in html
    assert "SQUARE_LOCATION_MAP" in html


def test_banner_shows_even_when_there_is_no_data(client, sqlite_db):
    """An empty History plus a failing sync is exactly when you need to know."""
    login(client)
    sqlite_db.record_sync_status(ok=False, error="SquareError: HTTP 500")
    html = client.get("/history").get_data(as_text=True)
    assert "Square sync is failing" in html
