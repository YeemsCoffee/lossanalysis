"""The analysis engine — formatting, clustering, and the analyze_df summary."""
import pandas as pd
import pytest

from app.analysis import (
    analyze_df, find_clusters, fmt_date, fmt_hour, fmt_time, source_summary,
)
from conftest import TARGET, make_tickets

OVER, OK = 400, 100          # comfortably either side of the 294s target


# --- formatting --------------------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (0, "0:00"), (59, "0:59"), (60, "1:00"), (294, "4:54"),
    (605, "10:05"), (-30, "-0:30"), (294.4, "4:54"),
])
def test_fmt_time(seconds, expected):
    assert fmt_time(seconds) == expected


@pytest.mark.parametrize("hour,expected", [
    (0, "12 AM"), (9, "9 AM"), (12, "12 PM"), (13, "1 PM"), (23, "11 PM"),
])
def test_fmt_hour(hour, expected):
    assert fmt_hour(hour) == expected


def test_fmt_date_strips_leading_zero():
    assert fmt_date(pd.Timestamp("2026-09-07").date()) == "Monday, September 7, 2026"


# --- clustering --------------------------------------------------------------

def test_three_in_a_row_is_a_cluster():
    df = make_tickets("2026-09-01", [OK, OVER, OVER, OVER, OK])
    clusters = find_clusters(df)
    assert len(clusters) == 1
    assert clusters[0]["ticket_count"] == 3


def test_two_in_a_row_is_not_a_cluster():
    df = make_tickets("2026-09-01", [OK, OVER, OVER, OK])
    assert find_clusters(df) == []


def test_nearby_runs_merge_distant_ones_do_not():
    # Tickets are one minute apart, so a single on-time ticket between two
    # runs is a 2-minute gap — inside the 3-minute merge window.
    near = make_tickets("2026-09-01", [OVER] * 3 + [OK] + [OVER] * 3)
    assert [c["ticket_count"] for c in find_clusters(near)] == [6]

    # Push the second run far enough out and they stay separate.
    far = make_tickets("2026-09-01", [OVER] * 3 + [OK] * 10 + [OVER] * 3)
    assert [c["ticket_count"] for c in find_clusters(far)] == [3, 3]


def test_clusters_do_not_span_days():
    day1 = make_tickets("2026-09-01", [OVER] * 2)
    day2 = make_tickets("2026-09-02", [OVER] * 2)
    # Two over-target tickets on each of two days: no run reaches 3 on either
    # day, so nothing should be reported even though there are 4 in sequence.
    assert find_clusters(pd.concat([day1, day2])) == []


def test_cluster_carries_its_tickets_and_timings():
    df = make_tickets("2026-09-01", [OVER, OVER, OVER])
    c = find_clusters(df)[0]
    assert c["ticket_count"] == len(c["tickets"]) == 3
    assert c["date"] == "2026-09-01"
    assert c["avg_seconds"] == OVER
    assert c["max_seconds"] == OVER
    assert c["duration_minutes"] == 2          # first to last, 1 min apart
    assert c["total_seconds_over"] == 3 * (OVER - TARGET)
    assert c["tickets"][0]["over_by"] == fmt_time(OVER - TARGET)


def test_clusters_sorted_biggest_first():
    df = pd.concat([
        make_tickets("2026-09-01", [OVER] * 3, start_minute=0),
        make_tickets("2026-09-01", [OK] * 10, start_minute=10),
        make_tickets("2026-09-01", [OVER] * 5, start_minute=30),
    ])
    assert [c["ticket_count"] for c in find_clusters(df)] == [5, 3]


def test_target_is_configurable():
    df = make_tickets("2026-09-01", [200, 200, 200])
    df["over_target"] = df["duration"] > 150      # stricter target
    assert len(find_clusters(df, target_seconds=150)) == 1


# --- source summary ----------------------------------------------------------

def test_source_summary_splits_by_source_and_sorts_by_volume():
    small = make_tickets("2026-09-01", [OK, OVER], source="Online")
    big   = make_tickets("2026-09-01", [OK] * 3 + [OVER], source="Register")
    rows  = source_summary(pd.concat([small, big]))

    assert [r["source"] for r in rows] == ["Register", "Online"]
    reg = rows[0]
    assert (reg["total"], reg["over"], reg["on_time"]) == (4, 1, 3)
    assert reg["pct_on_target"] == 75.0
    assert reg["pct_over"] == 25.0


# --- analyze_df --------------------------------------------------------------

def test_analyze_df_headline_numbers():
    df = make_tickets("2026-09-01", [OK] * 8 + [OVER] * 2)
    out = analyze_df(df, target_seconds=TARGET, target_pct=85)

    assert out["total_tickets"] == 10
    assert out["over_count"] == 2
    assert out["on_time_count"] == 8
    assert out["pct_on_target"] == 80.0
    assert out["target_pct"] == 85
    assert out["report_date"] == "Tuesday, September 1, 2026"


def test_analyze_df_recomputes_against_the_passed_target():
    # Same data, two targets: the second should flip every ticket to over.
    df = make_tickets("2026-09-01", [200] * 5)
    assert analyze_df(df, target_seconds=294, target_pct=85)["over_count"] == 0
    assert analyze_df(df, target_seconds=150, target_pct=85)["over_count"] == 5


def test_analyze_df_cluster_pct_of_day():
    df = make_tickets("2026-09-01", [OVER] * 3 + [OK] * 7)
    out = analyze_df(df, target_seconds=TARGET, target_pct=85)
    assert out["clusters"][0]["pct_of_day"] == 30.0
