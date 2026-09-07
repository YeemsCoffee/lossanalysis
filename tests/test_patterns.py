"""Weekday patterns, the weekday x hour heatmap, and location comparison."""
import pytest

from app.analysis_patterns import (
    location_comparison, weekday_hour_heatmap, weekday_summary,
)


def daily(report_date, total, over_count, avg_seconds=200):
    return {"report_date": report_date, "total": total, "over_count": over_count,
            "avg_seconds": avg_seconds, "longest_seconds": 900}


def hourly(report_date, hour, total, over_count):
    return {"report_date": report_date, "hour": hour,
            "total": total, "over_count": over_count}


# --- weekday summary ---------------------------------------------------------

def test_weekday_names_line_up_with_the_dates():
    # 2026-09-07 is a Monday.
    out = weekday_summary([daily("2026-09-07", 10, 0)], 85)
    assert out["rows"][0]["weekday"] == "Monday"
    assert out["rows"][0]["short"] == "Mon"


def test_weekday_rows_come_back_in_week_order():
    rows = weekday_summary([daily("2026-09-12", 10, 0),   # Saturday
                            daily("2026-09-07", 10, 0),   # Monday
                            daily("2026-09-09", 10, 0)],  # Wednesday
                           85)["rows"]
    assert [r["short"] for r in rows] == ["Mon", "Wed", "Sat"]


def test_same_weekday_across_weeks_is_averaged_not_pooled():
    """A huge bad Saturday shouldn't outvote a small good one."""
    out = weekday_summary([daily("2026-09-05", 1000, 1000),  # Sat, 0% on time
                           daily("2026-09-12", 10, 0)],      # Sat, 100% on time
                          85)
    sat = out["rows"][0]
    assert sat["pct_on_target"] == 50.0     # mean of the two days, not 1/1010
    assert sat["days"] == 2
    assert sat["tickets"] == 1010


def test_weekday_identifies_worst_and_best():
    out = weekday_summary([daily("2026-09-07", 10, 8),    # Mon, 20%
                           daily("2026-09-08", 10, 1)],   # Tue, 90%
                          85)
    assert out["worst"]["weekday"] == "Monday"
    assert out["best"]["weekday"] == "Tuesday"


def test_weekday_handles_no_rows():
    out = weekday_summary([], 85)
    assert out["rows"] == [] and out["worst"] is None


# --- heatmap -----------------------------------------------------------------

def test_heatmap_grid_shape_matches_days_and_hours():
    rows = [hourly("2026-09-07", 9, 10, 0), hourly("2026-09-07", 12, 10, 5),
            hourly("2026-09-08", 9, 10, 0), hourly("2026-09-08", 12, 10, 0)]
    out = weekday_hour_heatmap(rows, 85)
    trace = out["chart"][0]
    assert trace["y"] == ["Mon", "Tue"]
    assert trace["x"] == ["9 AM", "12 PM"]
    assert len(trace["z"]) == 2 and len(trace["z"][0]) == 2


def test_heatmap_blanks_thin_cells():
    """Two tickets shouldn't paint a cell bright red."""
    rows = [hourly("2026-09-07", 9, 2, 2), hourly("2026-09-07", 12, 20, 0)]
    out = weekday_hour_heatmap(rows, 85, min_tickets=5)
    z = out["chart"][0]["z"][0]
    assert z[0] is None            # the 2-ticket cell is left blank
    assert z[1] == 100.0
    # ...and the thin cell must not be reported as the worst window.
    assert out["worst"]["pct"] == 100.0


def test_heatmap_finds_the_worst_window():
    rows = [hourly("2026-09-07", 9, 20, 0), hourly("2026-09-12", 13, 20, 18)]
    out = weekday_hour_heatmap(rows, 85)
    assert out["worst"]["weekday"] == "Saturday"
    assert out["worst"]["hour"] == "1 PM"
    assert out["worst"]["pct"] == 10.0


def test_heatmap_averages_the_same_slot_across_weeks():
    rows = [hourly("2026-09-05", 10, 10, 10),   # Sat 10am, 0%
            hourly("2026-09-12", 10, 10, 0)]    # Sat 10am, 100%
    out = weekday_hour_heatmap(rows, 85)
    assert out["chart"][0]["z"][0][0] == 50.0


def test_heatmap_with_no_data():
    out = weekday_hour_heatmap([], 85)
    assert out["has_data"] is False and out["chart"] == []


# --- location comparison -----------------------------------------------------

def test_comparison_ranks_stores_and_reports_the_gap():
    out = location_comparison({
        "Gardena":   [daily("2026-09-01", 100, 10)],   # 90%
        "Koreatown": [daily("2026-09-01", 100, 40)],   # 60%
    }, 85)
    assert [s["location"] for s in out["stats"]] == ["Gardena", "Koreatown"]
    assert out["leader"]["location"] == "Gardena"
    assert out["laggard"]["location"] == "Koreatown"
    assert out["gap"] == 30.0
    assert out["has_data"] is True


def test_comparison_average_time_is_weighted_by_volume():
    """A quiet day at 600s shouldn't drag the mean like a busy day would."""
    out = location_comparison({
        "Gardena": [daily("2026-09-01", 90, 0, avg_seconds=100),
                    daily("2026-09-02", 10, 0, avg_seconds=600)],
    }, 85)
    # (90*100 + 10*600) / 100 = 150, not the unweighted 350.
    assert out["stats"][0]["avg_seconds"] == 150


def test_comparison_flags_whether_each_store_meets_the_goal():
    out = location_comparison({
        "Gardena":   [daily("2026-09-01", 100, 5)],    # 95%
        "Koreatown": [daily("2026-09-01", 100, 30)],   # 70%
    }, 85)
    by_loc = {s["location"]: s for s in out["stats"]}
    assert by_loc["Gardena"]["meets_goal"] is True
    assert by_loc["Koreatown"]["meets_goal"] is False


def test_comparison_reports_each_stores_worst_day():
    out = location_comparison({
        "Gardena": [daily("2026-09-01", 100, 5), daily("2026-09-02", 100, 50)],
    }, 85)
    assert out["stats"][0]["worst_day"]["date"] == "2026-09-02"


def test_comparison_with_a_single_location_has_nothing_to_compare():
    out = location_comparison({"Gardena": [daily("2026-09-01", 10, 1)],
                               "Koreatown": []}, 85)
    assert out["has_data"] is False
    assert out["gap"] is None
    assert len(out["stats"]) == 1


def test_comparison_with_no_data_at_all():
    out = location_comparison({"Gardena": [], "Koreatown": []}, 85)
    assert out["stats"] == [] and out["leader"] is None
