"""Loss-driver analyses: WIP, cascade, item lift, order size."""
import pandas as pd
import pytest

from app.analysis_drivers import (
    _wip_per_ticket, analyze_loss_drivers, cascade_analysis,
    item_association_analysis, order_size_analysis, wip_threshold_analysis,
)
from conftest import TARGET, make_tickets

OVER, OK = 400, 100


# --- work in process ---------------------------------------------------------

def test_wip_counts_tickets_open_when_each_arrives():
    # Created a minute apart, each taking 100s, so ticket 1 is still open when
    # ticket 2 arrives but ticket 1 has closed by the time ticket 3 does.
    df = make_tickets("2026-09-01", [100, 100, 100])
    assert list(_wip_per_ticket(df)["wip"]) == [1, 2, 2]


def test_wip_is_one_when_tickets_never_overlap():
    df = make_tickets("2026-09-01", [10, 10, 10])   # done long before the next
    assert list(_wip_per_ticket(df)["wip"]) == [1, 1, 1]


def test_wip_does_not_bleed_across_days():
    df = pd.concat([make_tickets("2026-09-01", [900] * 3),
                    make_tickets("2026-09-02", [900] * 3)])
    # Each day restarts at 1 rather than continuing to 6.
    assert list(_wip_per_ticket(df)["wip"]) == [1, 2, 3, 1, 2, 3]


def test_wip_knee_needs_a_real_spike_not_just_a_high_baseline():
    # Everything is over target, so queue depth explains nothing — the knee
    # should stay unset rather than blaming the first busy bucket.
    df = make_tickets("2026-09-01", [OVER] * 30)
    out = wip_threshold_analysis(df, TARGET, 85)
    assert out["baseline_over"] == 100.0
    assert out["knee"] is None


def test_wip_knee_fires_when_deep_queues_are_worse():
    calm = make_tickets("2026-09-01", [OK] * 40, start_minute=0)
    # A burst of slow tickets arriving on top of each other later in the day.
    busy = make_tickets("2026-09-01", [OVER] * 12, start_minute=200)
    out = wip_threshold_analysis(pd.concat([calm, busy]), TARGET, 85)
    assert out["knee"] is not None
    assert out["knee"]["over_pct"] > out["baseline_over"]


# --- cascade -----------------------------------------------------------------

def test_cascade_transition_probabilities():
    df = make_tickets("2026-09-01", [OVER, OVER, OK, OK])
    out = cascade_analysis(df, TARGET)
    # over->over happens once out of two chances; ok->over never out of one.
    assert out["p_after_over"] == 50.0
    assert out["p_after_ok"] == 0.0
    assert out["avg_streak"] == 2
    assert out["max_streak"] == 2
    assert out["streak_count"] == 1


def test_cascade_recovery_time_measured_from_streak_start():
    df = make_tickets("2026-09-01", [OVER, OVER, OVER, OK])
    out = cascade_analysis(df, TARGET)
    assert out["avg_recovery"] == 3.0     # tickets are a minute apart


def test_cascade_with_no_over_target_tickets():
    out = cascade_analysis(make_tickets("2026-09-01", [OK] * 5), TARGET)
    assert out["p_after_over"] == 0
    assert out["max_streak"] == 0
    assert out["avg_recovery"] is None


def test_cascade_streaks_do_not_span_days():
    df = pd.concat([make_tickets("2026-09-01", [OK, OVER]),
                    make_tickets("2026-09-02", [OVER, OK])])
    # Two separate 1-long streaks, not one 2-long streak across midnight.
    assert cascade_analysis(df, TARGET)["max_streak"] == 1


# --- item association --------------------------------------------------------

def test_item_lift_flags_the_slow_item():
    # 12 tickets with Panini all over target; 12 with Drip all on time.
    slow = make_tickets("2026-09-01", [OVER] * 12, items="Panini", start_minute=0)
    fast = make_tickets("2026-09-01", [OK] * 12, items="Drip", start_minute=100)
    out = item_association_analysis(pd.concat([slow, fast]), TARGET)

    by_name = {i["name"]: i for i in out["items"]}
    assert out["baseline"] == 50.0
    assert by_name["Panini"]["lift"] == 2.0     # twice the baseline rate
    assert by_name["Panini"]["over_pct"] == 100.0
    assert by_name["Drip"]["lift"] == 0.0
    assert out["items"][0]["name"] == "Panini"  # sorted by lift


def test_item_quantity_prefixes_and_case_are_normalised():
    df = make_tickets("2026-09-01", [OVER] * 12,
                      items=["2x Latte"] * 4 + ["3 Latte"] * 4 + ["latte"] * 4)
    out = item_association_analysis(df, TARGET)
    assert [i["total"] for i in out["items"]] == [12]


def test_item_counted_once_per_ticket_even_if_repeated():
    df = make_tickets("2026-09-01", [OVER] * 10, items="Latte, Latte, Latte")
    out = item_association_analysis(df, TARGET)
    assert out["items"][0]["total"] == 10


def test_rare_items_are_excluded():
    df = make_tickets("2026-09-01", [OVER] * 5, items="Rare Special")
    out = item_association_analysis(df, TARGET, min_occurrences=10)
    assert out["items"] == []
    assert out["has_data"] is False


# --- order size --------------------------------------------------------------

def test_order_size_buckets_and_averages():
    small = make_tickets("2026-09-01", [OK] * 10, n_items=1, start_minute=0)
    large = make_tickets("2026-09-01", [OVER] * 10, n_items=9, start_minute=100)
    out = order_size_analysis(pd.concat([small, large]), TARGET, 85)

    rows = {r["label"]: r for r in out["rows"]}
    assert rows["1"]["over_pct"] == 0.0
    assert rows["1"]["count"] == 10
    # Sizes at or above the cap collapse into a single "N+" bucket.
    top = [r for r in out["rows"] if r["label"].endswith("+")][0]
    assert top["over_pct"] == 100.0
    assert out["knee"] is not None


def test_order_size_knee_unset_when_size_does_not_matter():
    df = pd.concat([make_tickets("2026-09-01", [OK] * 10, n_items=1),
                    make_tickets("2026-09-01", [OK] * 10, n_items=8, start_minute=50)])
    assert order_size_analysis(df, TARGET, 85)["knee"] is None


# --- entry point -------------------------------------------------------------

def test_analyze_loss_drivers_returns_every_section():
    df = make_tickets("2026-09-01", [OK, OVER] * 15)
    out = analyze_loss_drivers(df, TARGET, 85)
    assert set(out) == {"wip", "cascade", "items", "size", "total_tickets"}
    assert out["total_tickets"] == 30
    for section in ("wip", "cascade", "size"):
        assert out[section]["chart"], f"{section} produced no chart data"


def test_analyze_loss_drivers_handles_a_single_ticket():
    # Degenerate input shouldn't raise — a manager can pick a one-ticket range.
    out = analyze_loss_drivers(make_tickets("2026-09-01", [OVER]), TARGET, 85)
    assert out["total_tickets"] == 1
    assert out["cascade"]["max_streak"] == 1
