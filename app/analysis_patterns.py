"""
Pattern analysis: when in the week losses happen, and how the two stores
compare.

Everything here is built from the aggregated rows the database already
returns (get_daily_summary / get_hourly_daily_summary), so adding these
views costs no extra ticket-level work.
"""

from datetime import date

from .analysis import fmt_hour, fmt_time

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday"]
SHORT    = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _weekday(iso_date):
    """0 = Monday … 6 = Sunday."""
    return date.fromisoformat(iso_date).weekday()


def _on_time_pct(total, over):
    return (total - over) / total * 100 if total else 0.0


def _colour(pct, target_pct):
    if pct < target_pct - 15:
        return "#dc2626"
    if pct < target_pct:
        return "#d97706"
    return "#2B4628"


# ---------------------------------------------------------------------------
# Day of week
# ---------------------------------------------------------------------------

def weekday_summary(daily_rows, target_pct):
    """
    On-time rate per weekday, averaged over the days in range.

    Averaging each day's rate (rather than pooling all tickets) keeps a
    single very busy Saturday from speaking for every Saturday.
    """
    buckets = {}
    for row in daily_rows:
        wd = _weekday(row["report_date"])
        b  = buckets.setdefault(wd, {"rates": [], "tickets": 0, "days": 0})
        b["rates"].append(_on_time_pct(row["total"], row["over_count"]))
        b["tickets"] += row["total"]
        b["days"]    += 1

    rows = []
    for wd in range(7):
        b = buckets.get(wd)
        if not b:
            continue
        pct = round(sum(b["rates"]) / len(b["rates"]), 1)
        rows.append({
            "weekday":       WEEKDAYS[wd],
            "short":         SHORT[wd],
            "pct_on_target": pct,
            "tickets":       b["tickets"],
            "days":          b["days"],
            "avg_tickets":   round(b["tickets"] / b["days"]),
        })

    worst = min(rows, key=lambda r: r["pct_on_target"]) if rows else None
    best  = max(rows, key=lambda r: r["pct_on_target"]) if rows else None
    chart = [{
        "x": [r["short"] for r in rows],
        "y": [r["pct_on_target"] for r in rows],
        "type": "bar",
        "text": [f"{r['tickets']:,} tix over {r['days']}d" for r in rows],
        "marker": {"color": [_colour(r["pct_on_target"], target_pct) for r in rows]},
        "hovertemplate": "%{x}<br>On-target: <b>%{y}%</b><br>%{text}<extra></extra>",
    }]
    return {"rows": rows, "worst": worst, "best": best, "chart": chart}


# ---------------------------------------------------------------------------
# Weekday x hour heatmap
# ---------------------------------------------------------------------------

def weekday_hour_heatmap(hourly_rows, target_pct, min_tickets=5):
    """
    On-time rate for each (weekday, hour) cell.

    Cells thinner than min_tickets are left blank rather than shown as a
    dramatic 0% or 100% built on two tickets.
    """
    cells = {}
    for row in hourly_rows:
        key = (_weekday(row["report_date"]), row["hour"])
        c   = cells.setdefault(key, {"rates": [], "tickets": 0})
        c["rates"].append(_on_time_pct(row["total"], row["over_count"]))
        c["tickets"] += row["total"]

    if not cells:
        return {"chart": [], "hours": [], "worst": None, "has_data": False}

    hours    = sorted({h for _, h in cells})
    weekdays = sorted({w for w, _ in cells})

    z, text, worst = [], [], None
    for wd in weekdays:
        z_row, t_row = [], []
        for hour in hours:
            c = cells.get((wd, hour))
            if not c or c["tickets"] < min_tickets:
                z_row.append(None)
                t_row.append("not enough tickets")
                continue
            pct = round(sum(c["rates"]) / len(c["rates"]), 1)
            z_row.append(pct)
            t_row.append(f"{c['tickets']:,} tickets")
            if worst is None or pct < worst["pct"]:
                worst = {"pct": pct, "weekday": WEEKDAYS[wd],
                         "hour": fmt_hour(hour), "tickets": c["tickets"]}
        z.append(z_row)
        text.append(t_row)

    chart = [{
        "type": "heatmap",
        "x": [fmt_hour(h) for h in hours],
        "y": [SHORT[w] for w in weekdays],
        "z": z,
        "text": text,
        "hovertemplate": "%{y} %{x}<br>On-target: <b>%{z}%</b><br>%{text}<extra></extra>",
        "colorscale": [[0, "#dc2626"], [0.6, "#d97706"], [1, "#2B4628"]],
        "zmin": 0, "zmax": 100,
        "hoverongaps": False,
        "colorbar": {"title": "On-target %", "ticksuffix": "%"},
    }]
    return {"chart": chart, "hours": hours, "worst": worst, "has_data": True}


# ---------------------------------------------------------------------------
# Location comparison
# ---------------------------------------------------------------------------

def location_comparison(daily_by_location, target_pct, p90_by_location=None):
    """
    Side-by-side totals per store, plus a shared on-time trend.

    daily_by_location: {location: [daily summary rows]}
    """
    stats, trend = [], []
    colours = ["#2B4628", "#9bc1cb", "#d97706", "#6b7280"]

    for i, (loc, rows) in enumerate(sorted(daily_by_location.items())):
        if not rows:
            continue
        tickets = sum(r["total"] for r in rows)
        over    = sum(r["over_count"] for r in rows)
        # Weight each day's average by its ticket count.
        avg_sec = (sum(r["avg_seconds"] * r["total"] for r in rows) / tickets
                   if tickets else 0)
        p90 = (p90_by_location or {}).get(loc, 0)
        day_rates = [{"date": r["report_date"],
                      "pct": round(_on_time_pct(r["total"], r["over_count"]), 1)}
                     for r in sorted(rows, key=lambda r: r["report_date"])]
        worst = min(day_rates, key=lambda d: d["pct"])

        stats.append({
            "location":      loc,
            "tickets":       tickets,
            "over":          over,
            "pct_on_target": round(_on_time_pct(tickets, over), 1),
            "avg_seconds":   round(avg_sec),
            "avg_fmt":       fmt_time(avg_sec),
            "p90_seconds":   p90,
            "p90_fmt":       fmt_time(p90),
            "days":          len(rows),
            "avg_tickets":   round(tickets / len(rows)),
            "worst_day":     worst,
            "meets_goal":    _on_time_pct(tickets, over) >= target_pct,
        })
        trend.append({
            "x": [d["date"] for d in day_rates],
            "y": [d["pct"] for d in day_rates],
            "name": loc,
            "type": "scatter", "mode": "lines+markers",
            "line": {"color": colours[i % len(colours)], "width": 3},
            "marker": {"size": 7},
            "hovertemplate": f"{loc}<br>%{{x}}<br><b>%{{y}}%</b> on target<extra></extra>",
        })

    stats.sort(key=lambda s: -s["pct_on_target"])
    gap = (round(stats[0]["pct_on_target"] - stats[-1]["pct_on_target"], 1)
           if len(stats) > 1 else None)

    return {"stats": stats, "chart": trend, "gap": gap,
            "leader": stats[0] if stats else None,
            "laggard": stats[-1] if len(stats) > 1 else None,
            "has_data": len(stats) > 1}
