from flask import Blueprint, render_template, request, redirect, url_for, flash
from .analysis import run_analysis
from .db import save_report, get_history, get_date_bounds
import io
import json

bp = Blueprint("main", __name__)


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
        result = run_analysis(io.BytesIO(content))
        save_report(result)
        return render_template("results.html", **result)
    except Exception as e:
        flash(f"Could not parse report: {e}", "error")
        return redirect(url_for("main.index"))


@bp.route("/history")
def history():
    earliest, latest = get_date_bounds()

    # Date range from query params; default to all available data
    from_date = request.args.get("from", earliest)
    to_date = request.args.get("to", latest)

    rows = get_history(from_date, to_date)

    if not rows:
        return render_template("history.html",
                               rows=[], charts=None,
                               from_date=from_date, to_date=to_date,
                               earliest=earliest, latest=latest)

    # Build chart data
    dates = [r["report_date"] for r in rows]

    trend_traces = [
        {
            "x": dates,
            "y": [r["pct_on_target"] for r in rows],
            "name": "On-Target %",
            "type": "scatter",
            "mode": "lines+markers",
            "line": {"color": "#2B4628", "width": 3},
            "marker": {"size": 7},
            "hovertemplate": "%{x}<br>%{y:.1f}% on target<extra></extra>",
        },
    ]

    volume_traces = [
        {
            "x": dates,
            "y": [r["on_time_count"] for r in rows],
            "name": "On Target",
            "type": "bar",
            "marker": {"color": "#2B4628"},
            "hovertemplate": "%{x}<br>On Target: %{y}<extra></extra>",
        },
        {
            "x": dates,
            "y": [r["over_count"] for r in rows],
            "name": "Over Target",
            "type": "bar",
            "marker": {"color": "#dc2626"},
            "hovertemplate": "%{x}<br>Over Target: %{y}<extra></extra>",
        },
    ]

    avg_trace = [
        {
            "x": dates,
            "y": [r["avg_seconds"] for r in rows],
            "name": "Avg Ticket Time (sec)",
            "type": "scatter",
            "mode": "lines+markers",
            "line": {"color": "#BCD7DE", "width": 3},
            "marker": {"size": 7, "color": "#2B4628"},
            "hovertemplate": "%{x}<br>Avg: %{y}s<extra></extra>",
            "fill": "tozeroy",
            "fillcolor": "rgba(188,215,222,0.2)",
        },
        {
            "x": [dates[0], dates[-1]],
            "y": [294, 294],
            "name": "Target (294s)",
            "type": "scatter",
            "mode": "lines",
            "line": {"color": "#d97706", "width": 2, "dash": "dash"},
            "hoverinfo": "skip",
        },
    ]

    # Source performance over time — one line per source
    all_sources = sorted({s["source"] for r in rows for s in r["sources"]})
    source_colors = ["#2B4628", "#BCD7DE", "#d97706", "#6b7280"]
    source_traces = []
    for i, src in enumerate(all_sources):
        xs, ys = [], []
        for r in rows:
            match = next((s for s in r["sources"] if s["source"] == src), None)
            if match:
                xs.append(r["report_date"])
                ys.append(match["pct_on_target"])
        source_traces.append({
            "x": xs,
            "y": ys,
            "name": src,
            "type": "scatter",
            "mode": "lines+markers",
            "line": {"color": source_colors[i % len(source_colors)], "width": 2},
            "marker": {"size": 6},
            "hovertemplate": f"{src}<br>%{{x}}<br>%{{y:.1f}}% on target<extra></extra>",
        })

    charts = {
        "trend": json.dumps(trend_traces),
        "volume": json.dumps(volume_traces),
        "avg": json.dumps(avg_trace),
        "sources": json.dumps(source_traces),
    }

    # Period summary stats
    total_tickets = sum(r["total_tickets"] for r in rows)
    total_over = sum(r["over_count"] for r in rows)
    avg_on_target = round(sum(r["pct_on_target"] for r in rows) / len(rows), 1)
    worst_day = min(rows, key=lambda r: r["pct_on_target"])
    best_day = max(rows, key=lambda r: r["pct_on_target"])

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
