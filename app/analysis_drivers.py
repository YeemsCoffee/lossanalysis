"""
Loss-driver / correlation analysis for the Loss Analysis tool.

Moves beyond "what happened" to "what is driving the over-target tickets":
  1. WIP / concurrent-load threshold  — the capacity ceiling
  2. Cascade / domino effect           — how being over-target propagates
  3. Item association (lift)           — which menu items drag times down
  4. Order-size threshold              — over-target rate by ticket size

All analyses run over a multi-day / multi-location DataFrame and compute
concurrency *within* each (location, report_date) group so two stores or two
days never bleed into one another.
"""

import re
import numpy as np
import pandas as pd

from .analysis import fmt_time

# Items column may carry quantity prefixes like "2x Latte", "2 × Latte", "3 Latte"
_QTY_PREFIX = re.compile(r"^\s*\d+\s*[x×]?\s*", flags=re.IGNORECASE)


def _groups(df):
    """Yield per (location, date) sub-frames, sorted by creation time."""
    keys = []
    if "location" in df.columns:
        keys.append("location")
    keys.append(df["Time Created"].dt.date)
    for _, g in df.groupby(keys, dropna=False, sort=False):
        yield g.sort_values("Time Created").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 1. WIP / concurrent-load threshold
# ---------------------------------------------------------------------------

def _wip_per_ticket(df):
    """Return a copy of df with a 'wip' column = tickets open when each was created."""
    parts = []
    for g in _groups(df):
        created = g["Time Created"].values
        done    = np.sort(g["Time Completed"].values)
        # opened so far (incl. self) minus already-completed at this instant
        opened = np.searchsorted(np.sort(created), created, side="right")
        closed = np.searchsorted(done, created, side="right")
        g = g.copy()
        g["wip"] = (opened - closed).clip(min=1)
        parts.append(g)
    return pd.concat(parts) if parts else df.assign(wip=1)


def wip_threshold_analysis(df, target_seconds, target_pct):
    d = _wip_per_ticket(df)
    baseline_over = round(d["over_target"].mean() * 100, 1)
    over_goal     = 100 - target_pct  # acceptable over-rate implied by the goal

    cap = int(min(max(d["wip"].quantile(0.97), 4), 12))
    rows = []
    knee = None
    for v in range(1, cap + 1):
        if v < cap:
            sub = d[d["wip"] == v]
            label = str(v)
        else:
            sub = d[d["wip"] >= v]
            label = f"{v}+"
        if sub.empty:
            continue
        over_pct = round(sub["over_target"].mean() * 100, 1)
        rows.append({"label": label, "wip": v, "count": int(len(sub)),
                     "over_pct": over_pct})
        # Flag the ceiling: first queue depth that is both past the acceptable
        # line AND materially worse than the overall baseline (a real spike).
        if (knee is None and v > 1
                and over_pct > over_goal
                and over_pct >= baseline_over + 15):
            knee = {"wip": v, "over_pct": over_pct}

    chart = [{
        "x": [r["label"] for r in rows],
        "y": [r["over_pct"] for r in rows],
        "type": "bar",
        "text": [f"{r['count']} tix" for r in rows],
        "marker": {"color": [
            "#dc2626" if r["over_pct"] > over_goal
            else "#d97706" if r["over_pct"] > over_goal * 0.6
            else "#2B4628" for r in rows]},
        "hovertemplate": "WIP %{x}<br>Over: <b>%{y}%</b><br>%{text}<extra></extra>",
    }]

    return {"rows": rows, "baseline_over": baseline_over, "knee": knee,
            "over_goal": over_goal, "chart": chart}


# ---------------------------------------------------------------------------
# 2. Cascade / domino effect
# ---------------------------------------------------------------------------

def cascade_analysis(df, target_seconds):
    after_over, after_over_total = 0, 0
    after_ok,   after_ok_total   = 0, 0
    streak_lengths   = []
    recovery_minutes = []

    for g in _groups(df):
        over = g["over_target"].tolist()
        times = g["Time Created"].tolist()
        n = len(over)
        for i in range(n - 1):
            if over[i]:
                after_over_total += 1
                if over[i + 1]:
                    after_over += 1
            else:
                after_ok_total += 1
                if over[i + 1]:
                    after_ok += 1
        # streaks of consecutive over-target tickets
        i = 0
        while i < n:
            if over[i]:
                j = i
                while j < n and over[j]:
                    j += 1
                streak_lengths.append(j - i)
                # recovery = start of streak -> first on-time ticket after it
                if j < n:
                    mins = (times[j] - times[i]).total_seconds() / 60
                    recovery_minutes.append(mins)
                i = j
            else:
                i += 1

    p_after_over = round(after_over / after_over_total * 100, 1) if after_over_total else 0
    p_after_ok   = round(after_ok   / after_ok_total   * 100, 1) if after_ok_total else 0
    baseline     = round(df["over_target"].mean() * 100, 1)
    lift = round(p_after_over / p_after_ok, 1) if p_after_ok else None

    return {
        "p_after_over":   p_after_over,
        "p_after_ok":     p_after_ok,
        "baseline":       baseline,
        "lift":           lift,
        "avg_streak":     round(np.mean(streak_lengths), 1) if streak_lengths else 0,
        "max_streak":     int(max(streak_lengths)) if streak_lengths else 0,
        "streak_count":   len(streak_lengths),
        "avg_recovery":   round(np.mean(recovery_minutes), 1) if recovery_minutes else None,
        "chart": [{
            "x": ["After an on-time ticket", "After an over-target ticket"],
            "y": [p_after_ok, p_after_over],
            "type": "bar",
            "text": [f"{p_after_ok}%", f"{p_after_over}%"],
            "textposition": "outside",
            "marker": {"color": ["#2B4628", "#dc2626"]},
            "hovertemplate": "%{x}<br>Next ticket over: <b>%{y}%</b><extra></extra>",
        }],
    }


# ---------------------------------------------------------------------------
# 3. Item association (lift)
# ---------------------------------------------------------------------------

def _explode_items(df):
    rows = []
    for items, over in zip(df["Items in Ticket"].astype(str), df["over_target"]):
        seen = set()
        for piece in items.split(","):
            name = _QTY_PREFIX.sub("", piece).strip()
            if not name or name.lower() in ("nan", "none"):
                continue
            key = name.lower()
            if key in seen:           # count each item once per ticket
                continue
            seen.add(key)
            rows.append((name, bool(over)))
    return rows


def item_association_analysis(df, target_seconds, min_occurrences=10, top_n=15):
    baseline = df["over_target"].mean()
    if baseline == 0:
        baseline = 1e-9

    agg = {}
    for name, over in _explode_items(df):
        a = agg.setdefault(name.lower(), {"name": name, "total": 0, "over": 0})
        a["total"] += 1
        a["over"]  += 1 if over else 0

    items = []
    for a in agg.values():
        if a["total"] < min_occurrences:
            continue
        over_rate = a["over"] / a["total"]
        items.append({
            "name":      a["name"],
            "total":     a["total"],
            "over":      a["over"],
            "over_pct":  round(over_rate * 100, 1),
            "lift":      round(over_rate / baseline, 2),
        })
    items.sort(key=lambda x: (-x["lift"], -x["total"]))
    return {
        "items":    items[:top_n],
        "baseline": round(baseline * 100, 1),
        "min_occurrences": min_occurrences,
        "has_data": len(items) > 0,
    }


# ---------------------------------------------------------------------------
# 4. Order-size threshold
# ---------------------------------------------------------------------------

def order_size_analysis(df, target_seconds, target_pct):
    over_goal = 100 - target_pct
    baseline_over = round(df["over_target"].mean() * 100, 1)
    d = df.copy()
    d["nitems"] = pd.to_numeric(d["Number of Items"], errors="coerce").fillna(0).astype(int)

    cap = int(min(max(d["nitems"].quantile(0.97), 4), 10))
    rows = []
    knee = None
    for v in range(1, cap + 1):
        if v < cap:
            sub = d[d["nitems"] == v]
            label = str(v)
        else:
            sub = d[d["nitems"] >= v]
            label = f"{v}+"
        if sub.empty:
            continue
        over_pct = round(sub["over_target"].mean() * 100, 1)
        avg_sec  = round(sub["duration"].mean())
        rows.append({"label": label, "size": v, "count": int(len(sub)),
                     "over_pct": over_pct, "avg_fmt": fmt_time(avg_sec)})
        if (knee is None and v > 1
                and over_pct > over_goal
                and over_pct >= baseline_over + 15):
            knee = {"size": v, "over_pct": over_pct}

    chart = [{
        "x": [r["label"] for r in rows],
        "y": [r["over_pct"] for r in rows],
        "type": "bar",
        "text": [f"{r['count']} tix" for r in rows],
        "marker": {"color": [
            "#dc2626" if r["over_pct"] > over_goal
            else "#d97706" if r["over_pct"] > over_goal * 0.6
            else "#2B4628" for r in rows]},
        "hovertemplate": "%{x} items<br>Over: <b>%{y}%</b><br>%{text}<extra></extra>",
    }]
    return {"rows": rows, "knee": knee, "over_goal": over_goal, "chart": chart}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def analyze_loss_drivers(df, target_seconds, target_pct):
    return {
        "wip":      wip_threshold_analysis(df, target_seconds, target_pct),
        "cascade":  cascade_analysis(df, target_seconds),
        "items":    item_association_analysis(df, target_seconds),
        "size":     order_size_analysis(df, target_seconds, target_pct),
        "total_tickets": int(len(df)),
    }
