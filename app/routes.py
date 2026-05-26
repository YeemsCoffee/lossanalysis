from flask import Blueprint, render_template, request, redirect, url_for, flash
from .analysis import (
    parse_report, analyze_df, run_analysis,
    fmt_time, fmt_hour, source_summary, hourly_summary,
    TARGET_SECONDS,
)
from .db import (
    save_day_tickets, get_tickets_df, get_date_bounds,
    get_distinct_dates,
)
import io
import json

bp = Blueprint("main", __name__)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

@bp.route("/", methods=["GET"])
def index():
    earliest, latest = get_date_bounds()
    return render_template("upload.html", has_history=bool(earliest))


@bp.route("/analyze", methods=["POST"])
def analyze():
    if "report" not in request.files:
        flash("No file selected.", "error")
        return redirect(url_for("main.index"))

    f = request.files["report"]
    if not f.filename or not f.filename.lower().endswith(".csv"):
        flash("Please upload a .csv file.", "error")
        return redirect(url_for("main.index"))

    try:
        content = f.read()
        df      = parse_report(io.BytesIO(content))
        iso     = df["Time Created"].dt.date.iloc[0].strftime("%Y-%m-%d")
        save_day_tickets(iso, df)
        result  = analyze_df(df)
        return render_template("results.html", **result, iso_date=iso)
    except Exception as e:
        flash(f"Could not parse report: {e}", "error")
        return redirect(url_for("main.index"))


# ---------------------------------------------------------------------------
# Day drill-down (replay from DB)
# ---------------------------------------------------------------------------

@bp.route("/day/<date>")
def day(date):
    df = get_tickets_df(from_date=date, to_date=date)
    if df.empty:
        flash(f"No data found for {date}.", "error")
        return redirect(url_for("main.history"))
    result = analyze_df(df)
    return render_template("results.html", **result, iso_date=date, from_history=True)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

@bp.route("/history")
def history():
    earliest, latest = get_date_bounds()

    if not earliest:
        return render_template("history.html", rows=[], charts=None,
                               from_date=None, to_date=None,
                               earliest=None, latest=None)

    from_date = request.args.get("from", earliest)
    to_date   = request.args.get("to",   latest)

    df = get_tickets_df(from_date, to_date)
    if df.empty:
        return render_template("history.html", rows=[], charts=None,
                               from_date=from_date, to_date=to_date,
                               earliest=earliest, latest=latest)

    # ---- Per-day summary rows ------------------------------------------------
    rows = []
    for date_val, day_df in df.groupby("report_date"):
        total  = len(day_df)
        over   = int(day_df["over_target"].sum())
        on_time = total - over
        avg    = round(day_df["duration"].mean())
        longest = int(day_df["duration"].max())
        rows.append({
            "report_date":    date_val,
            "total_tickets":  total,
            "over_count":     over,
            "on_time_count":  on_time,
            "pct_on_target":  round(on_time / total * 100, 1) if total else 0,
            "avg_seconds":    avg,
            "avg_fmt":        fmt_time(avg),
            "longest_seconds": longest,
            "longest_fmt":    fmt_time(longest),
        })
    rows.sort(key=lambda r: r["report_date"], reverse=True)

    # ---- Period-wide stats ---------------------------------------------------
    total_tickets   = sum(r["total_tickets"] for r in rows)
    total_over      = sum(r["over_count"]    for r in rows)
    avg_on_target   = round(sum(r["pct_on_target"] for r in rows) / len(rows), 1)
    worst_day       = min(rows, key=lambda r: r["pct_on_target"])
    best_day        = max(rows, key=lambda r: r["pct_on_target"])

    # ---- Chart data ----------------------------------------------------------
    dates_asc = sorted(r["report_date"] for r in rows)
    row_by_date = {r["report_date"]: r for r in rows}

    # On-target % trend
    trend_traces = [{
        "x":    dates_asc,
        "y":    [row_by_date[d]["pct_on_target"] for d in dates_asc],
        "name": "On-Target %",
        "type": "scatter", "mode": "lines+markers",
        "line": {"color": "#2B4628", "width": 3},
        "marker": {"size": 8, "color": "#2B4628"},
        "hovertemplate": "%{x}<br><b>%{y:.1f}%</b> on target<extra></extra>",
    }]

    # Stacked volume bars
    volume_traces = [
        {"x": dates_asc,
         "y": [row_by_date[d]["on_time_count"] for d in dates_asc],
         "name": "On Target", "type": "bar", "marker": {"color": "#2B4628"},
         "hovertemplate": "%{x}<br>On Target: %{y}<extra></extra>"},
        {"x": dates_asc,
         "y": [row_by_date[d]["over_count"] for d in dates_asc],
         "name": "Over Target", "type": "bar", "marker": {"color": "#dc2626"},
         "hovertemplate": "%{x}<br>Over Target: %{y}<extra></extra>"},
    ]

    # Avg time trend with target line
    avg_traces = [
        {"x": dates_asc,
         "y": [row_by_date[d]["avg_seconds"] for d in dates_asc],
         "name": "Avg Ticket Time (sec)", "type": "scatter", "mode": "lines+markers",
         "line": {"color": "#BCD7DE", "width": 3},
         "marker": {"size": 7, "color": "#2B4628"},
         "fill": "tozeroy", "fillcolor": "rgba(188,215,222,0.2)",
         "hovertemplate": "%{x}<br>Avg: %{y}s<extra></extra>"},
        {"x": [dates_asc[0], dates_asc[-1]],
         "y": [TARGET_SECONDS, TARGET_SECONDS],
         "name": "Target (294s)", "type": "scatter", "mode": "lines",
         "line": {"color": "#d97706", "width": 2, "dash": "dash"},
         "hoverinfo": "skip"},
    ]

    # Source on-target % trend
    all_sources = sorted({s["source"] for s in source_summary(df)})
    source_colors = ["#2B4628", "#9bc1cb", "#d97706", "#6b7280"]
    source_traces = []
    for i, src in enumerate(all_sources):
        xs, ys = [], []
        for date_val, day_df in df.groupby("report_date"):
            src_df = day_df[day_df["source"] == src]
            if not src_df.empty:
                total = len(src_df)
                on_t  = int((~src_df["over_target"]).sum())
                xs.append(date_val)
                ys.append(round(on_t / total * 100, 1))
        source_traces.append({
            "x": xs, "y": ys, "name": src,
            "type": "scatter", "mode": "lines+markers",
            "line": {"color": source_colors[i % len(source_colors)], "width": 2},
            "marker": {"size": 6},
            "hovertemplate": f"{src}<br>%{{x}}<br><b>%{{y:.1f}}%</b> on target<extra></extra>",
        })

    # Hour-of-day heatmap: avg on-target % per hour across all days in range
    hour_rows = []
    for hour, h_df in df.groupby("hour"):
        # Per-day on-target rates for this hour, then average them
        daily_rates = []
        for _, day_h_df in h_df.groupby("report_date"):
            t = len(day_h_df)
            on_t = int((~day_h_df["over_target"]).sum())
            daily_rates.append(on_t / t * 100 if t else 0)
        avg_rate  = round(sum(daily_rates) / len(daily_rates), 1)
        total_tix = int(len(h_df))
        hour_rows.append({
            "hour":       int(hour),
            "hour_label": fmt_hour(int(hour)),
            "pct_on_target": avg_rate,
            "total":      total_tix,
        })
    hour_rows.sort(key=lambda x: x["hour"])

    hour_trace = [{
        "x":    [h["hour_label"] for h in hour_rows],
        "y":    [h["pct_on_target"] for h in hour_rows],
        "text": [f"{h['total']} tickets" for h in hour_rows],
        "type": "bar",
        "marker": {"color": [
            "#dc2626" if h["pct_on_target"] < 70
            else "#d97706" if h["pct_on_target"] < 85
            else "#2B4628"
            for h in hour_rows
        ]},
        "hovertemplate": "%{x}<br>On-Target: <b>%{y}%</b><br>%{text}<extra></extra>",
    }]

    charts = {
        "trend":   json.dumps(trend_traces),
        "volume":  json.dumps(volume_traces),
        "avg":     json.dumps(avg_traces),
        "sources": json.dumps(source_traces),
        "hour":    json.dumps(hour_trace),
    }

    return render_template(
        "history.html",
        rows=rows,
        charts=charts,
        from_date=from_date,
        to_date=to_date,
        earliest=earliest,
        latest=latest,
        period_days=len(rows),
        total_tickets=total_tickets,
        total_over=total_over,
        pct_on_target_avg=avg_on_target,
        worst_day=worst_day,
        best_day=best_day,
    )
