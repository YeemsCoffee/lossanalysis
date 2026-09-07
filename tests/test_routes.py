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
