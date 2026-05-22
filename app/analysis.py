"""
Kitchen Ticket Loss Analysis Engine
Target: 4 min 54 sec = 294 seconds
"""

import pandas as pd
import json

TARGET_SECONDS = 294  # 4 min 54 sec
MIN_CLUSTER_SIZE = 3  # consecutive over-target tickets to form a cluster


def fmt_time(seconds):
    """Format seconds as m:ss string."""
    seconds = int(round(seconds))
    sign = "-" if seconds < 0 else ""
    m, s = divmod(abs(seconds), 60)
    return f"{sign}{m}:{s:02d}"


def fmt_clock(ts):
    """Format a Timestamp as '7:05 AM' (cross-platform, no %-I)."""
    s = ts.strftime("%I:%M %p")
    return s.lstrip("0")


def fmt_hour(hour):
    """Format an integer hour as '7 AM' (cross-platform)."""
    s = pd.Timestamp(f"2000-01-01 {hour:02d}:00").strftime("%I %p")
    return s.lstrip("0")


def fmt_date(d):
    """Format a date as 'Thursday, May 21, 2026' (cross-platform)."""
    s = d.strftime("%A, %B %d, %Y")
    # strip leading zero on day-of-month
    return s.replace(" 0", " ", 1) if " 0" in s else s


def parse_report(file_obj):
    """Parse the Square kitchen report CSV."""
    df = pd.read_csv(file_obj)
    df.columns = [c.strip() for c in df.columns]

    df["Time Created"] = pd.to_datetime(df["Time Created"])
    df["Time Completed"] = pd.to_datetime(df["Time Completed"])
    df["duration"] = df["Completion Time (seconds)"].astype(float)
    df["over_target"] = df["duration"] > TARGET_SECONDS
    df["seconds_over"] = (df["duration"] - TARGET_SECONDS).clip(lower=0)
    df["hour"] = df["Time Created"].dt.hour
    df["source"] = df["Order Source"].str.strip()

    # Sort by creation time so "consecutive" is meaningful
    df = df.sort_values("Time Created").reset_index(drop=True)
    return df


CLUSTER_MERGE_GAP_MINUTES = 3  # merge clusters whose gap is ≤ this


def find_clusters(df, min_size=MIN_CLUSTER_SIZE):
    """
    Find runs of consecutive over-target tickets (sorted by Time Created).
    After building individual runs, merge any two clusters whose gap is
    <= CLUSTER_MERGE_GAP_MINUTES into one.
    """
    # Step 1: collect raw runs of over-target tickets with their raw rows
    raw_runs = []   # list of lists-of-rows
    run = []

    for _, row in df.iterrows():
        if row["over_target"]:
            run.append(row)
        else:
            if len(run) >= min_size:
                raw_runs.append(run)
            run = []
    if len(run) >= min_size:
        raw_runs.append(run)

    if not raw_runs:
        return []

    # Step 2: merge runs that are within CLUSTER_MERGE_GAP_MINUTES of each other
    # "gap" = time between last ticket of run A and first ticket of run B
    merged = [raw_runs[0]]
    for next_run in raw_runs[1:]:
        prev_end = merged[-1][-1]["Time Created"]
        next_start = next_run[0]["Time Created"]
        gap_minutes = (next_start - prev_end).total_seconds() / 60
        if gap_minutes <= CLUSTER_MERGE_GAP_MINUTES:
            merged[-1] = merged[-1] + next_run  # combine into one run
        else:
            merged.append(next_run)

    # Step 3: build cluster dicts, keep only those still ≥ min_size
    clusters = [_build_cluster(r) for r in merged if len(r) >= min_size]
    clusters.sort(key=lambda c: (-c["ticket_count"], -c["avg_seconds"]))
    return clusters


def _build_cluster(rows):
    durations = [r["duration"] for r in rows]
    return {
        "ticket_count": len(rows),
        "start_time": fmt_clock(rows[0]["Time Created"]),
        "end_time": fmt_clock(rows[-1]["Time Created"]),
        "duration_minutes": round(
            (rows[-1]["Time Created"] - rows[0]["Time Created"]).total_seconds() / 60, 1
        ),
        "avg_seconds": round(sum(durations) / len(durations)),
        "avg_fmt": fmt_time(sum(durations) / len(durations)),
        "max_seconds": round(max(durations)),
        "max_fmt": fmt_time(max(durations)),
        "total_seconds_over": round(sum(max(0, d - TARGET_SECONDS) for d in durations)),
        "tickets": [
            {
                "name": str(r["Ticket Name"]),
                "time": fmt_clock(r["Time Created"]),
                "duration": int(r["duration"]),
                "duration_fmt": fmt_time(r["duration"]),
                "over_by": fmt_time(r["duration"] - TARGET_SECONDS),
                "items": str(r["Items in Ticket"]),
                "source": str(r["Order Source"]),
            }
            for r in rows
        ],
    }


def hourly_summary(df):
    """Counts per hour for stacked bar chart."""
    rows = []
    for hour, grp in df.groupby("hour"):
        total = len(grp)
        over = int(grp["over_target"].sum())
        rows.append({
            "hour": int(hour),
            "hour_label": fmt_hour(hour),
            "total": total,
            "over": over,
            "on_time": total - over,
            "pct_over": round(over / total * 100) if total else 0,
            "avg_seconds": round(grp["duration"].mean()),
            "avg_fmt": fmt_time(grp["duration"].mean()),
        })
    rows.sort(key=lambda x: x["hour"])
    return rows


def source_summary(df):
    """On-target rate per order source."""
    rows = []
    for source, grp in df.groupby("source"):
        total = len(grp)
        over = int(grp["over_target"].sum())
        on_time = total - over
        rows.append({
            "source": source,
            "total": total,
            "over": over,
            "on_time": on_time,
            "pct_on_target": round(on_time / total * 100, 1) if total else 0,
            "pct_over": round(over / total * 100, 1) if total else 0,
            "avg_seconds": round(grp["duration"].mean()),
            "avg_fmt": fmt_time(grp["duration"].mean()),
        })
    rows.sort(key=lambda x: -x["total"])
    return rows


def top_offenders(df, n=15):
    top = df.nlargest(n, "duration")
    out = []
    for _, row in top.iterrows():
        out.append({
            "name": str(row["Ticket Name"]),
            "source": str(row["source"]),
            "time": fmt_clock(row["Time Created"]),
            "duration": int(row["duration"]),
            "duration_fmt": fmt_time(row["duration"]),
            "over_by": fmt_time(row["duration"] - TARGET_SECONDS),
            "items": str(row["Items in Ticket"]),
            "item_count": int(row["Number of Items"]),
        })
    return out


def all_over_target(df):
    """Every ticket that missed target — for the detail table."""
    over = df[df["over_target"]].sort_values("duration", ascending=False)
    out = []
    for _, row in over.iterrows():
        out.append({
            "name": str(row["Ticket Name"]),
            "source": str(row["source"]),
            "time": fmt_clock(row["Time Created"]),
            "duration": int(row["duration"]),
            "duration_fmt": fmt_time(row["duration"]),
            "over_by_seconds": int(row["duration"] - TARGET_SECONDS),
            "over_by": fmt_time(row["duration"] - TARGET_SECONDS),
            "items": str(row["Items in Ticket"]),
            "item_count": int(row["Number of Items"]),
        })
    return out


def build_timeline_chart(df):
    """Scatter plot: x=time, y=duration, color=over/on, target line at 294s."""
    over_df = df[df["over_target"]]
    ok_df = df[~df["over_target"]]

    traces = []

    if not ok_df.empty:
        traces.append({
            "x": ok_df["Time Created"].dt.strftime("%H:%M:%S").tolist(),
            "y": ok_df["duration"].tolist(),
            "text": [
                f"{r['Ticket Name']}<br>{fmt_time(r['duration'])}<br>{r['Items in Ticket']}"
                for _, r in ok_df.iterrows()
            ],
            "mode": "markers",
            "type": "scatter",
            "name": "On Target",
            "marker": {"color": "#2B4628", "size": 7, "opacity": 0.7, "line": {"width": 0}},
            "hovertemplate": "%{text}<extra></extra>",
        })

    if not over_df.empty:
        traces.append({
            "x": over_df["Time Created"].dt.strftime("%H:%M:%S").tolist(),
            "y": over_df["duration"].tolist(),
            "text": [
                f"{r['Ticket Name']}<br>{fmt_time(r['duration'])} ({fmt_time(r['duration']-TARGET_SECONDS)} over)<br>{r['Items in Ticket']}"
                for _, r in over_df.iterrows()
            ],
            "mode": "markers",
            "type": "scatter",
            "name": "Over Target",
            "marker": {"color": "#dc2626", "size": 8, "opacity": 0.85, "line": {"width": 0}},
            "hovertemplate": "%{text}<extra></extra>",
        })

    if not df.empty:
        times_sorted = sorted(df["Time Created"].dt.strftime("%H:%M:%S").tolist())
        traces.append({
            "x": [times_sorted[0], times_sorted[-1]],
            "y": [TARGET_SECONDS, TARGET_SECONDS],
            "mode": "lines",
            "type": "scatter",
            "name": "Target (4:54)",
            "line": {"color": "#d97706", "width": 2, "dash": "dash"},
            "hoverinfo": "skip",
        })

    return traces


def build_longest_chart(df, n=15):
    """Horizontal bar chart of top N longest tickets."""
    top = df.nlargest(n, "duration").copy()
    top = top.sort_values("duration")  # ascending so longest is at top of bar chart

    return {
        "x": top["duration"].tolist(),
        "y": [f"{name} · {fmt_clock(t)}" for name, t in zip(
            top["Ticket Name"].astype(str),
            top["Time Created"])],
        "text": [fmt_time(d) for d in top["duration"].tolist()],
        "textposition": "outside",
        "type": "bar",
        "orientation": "h",
        "marker": {
            "color": ["#dc2626" if d > TARGET_SECONDS else "#2B4628"
                      for d in top["duration"].tolist()],
        },
        "hovertemplate": "%{y}<br>%{text}<extra></extra>",
    }


def build_hourly_chart(hourly):
    """Stacked bar chart: on-time vs over-target per hour."""
    labels = [h["hour_label"] for h in hourly]
    return [
        {
            "x": labels,
            "y": [h["on_time"] for h in hourly],
            "name": "On Target",
            "type": "bar",
            "marker": {"color": "#2B4628"},
            "hovertemplate": "%{x}<br>On Target: %{y}<extra></extra>",
        },
        {
            "x": labels,
            "y": [h["over"] for h in hourly],
            "name": "Over Target",
            "type": "bar",
            "marker": {"color": "#dc2626"},
            "hovertemplate": "%{x}<br>Over Target: %{y}<extra></extra>",
        },
    ]


def build_source_chart(sources):
    """Grouped bar chart of source performance."""
    labels = [s["source"] for s in sources]
    return [
        {
            "x": labels,
            "y": [s["on_time"] for s in sources],
            "name": "On Target",
            "type": "bar",
            "marker": {"color": "#2B4628"},
            "text": [f"{s['pct_on_target']}%" for s in sources],
            "textposition": "inside",
        },
        {
            "x": labels,
            "y": [s["over"] for s in sources],
            "name": "Over Target",
            "type": "bar",
            "marker": {"color": "#dc2626"},
            "text": [f"{s['pct_over']}%" for s in sources],
            "textposition": "inside",
        },
    ]


def run_analysis(file_obj):
    """Main entry point — returns all analysis data for templating."""
    df = parse_report(file_obj)

    if df.empty:
        raise ValueError("CSV contains no rows.")

    total = len(df)
    over_count = int(df["over_target"].sum())
    on_time_count = total - over_count
    pct_over = round(over_count / total * 100, 1)
    pct_on_target = round(on_time_count / total * 100, 1)
    avg_seconds = round(df["duration"].mean())

    report_date = fmt_date(df["Time Created"].dt.date.iloc[0])
    max_row = df.loc[df["duration"].idxmax()]

    hourly = hourly_summary(df)
    sources = source_summary(df)

    return {
        "report_date": report_date,
        "target_seconds": TARGET_SECONDS,
        "target_fmt": fmt_time(TARGET_SECONDS),
        "total_tickets": total,
        "over_count": over_count,
        "on_time_count": on_time_count,
        "pct_over": pct_over,
        "pct_on_target": pct_on_target,
        "avg_seconds": avg_seconds,
        "avg_fmt": fmt_time(avg_seconds),
        "longest_seconds": int(max_row["duration"]),
        "longest_fmt": fmt_time(int(max_row["duration"])),
        "longest_over_by": fmt_time(int(max_row["duration"]) - TARGET_SECONDS),
        "longest_ticket_name": str(max_row["Ticket Name"]),
        "longest_ticket_items": str(max_row["Items in Ticket"]),
        "longest_ticket_time": fmt_clock(max_row["Time Created"]),
        "longest_ticket_source": str(max_row["source"]),
        "clusters": find_clusters(df),
        "hourly": hourly,
        "sources": sources,
        "top_offenders": top_offenders(df, n=15),
        "all_over": all_over_target(df),
        "timeline_chart": json.dumps(build_timeline_chart(df)),
        "longest_chart": json.dumps(build_longest_chart(df)),
        "hourly_chart": json.dumps(build_hourly_chart(hourly)),
        "source_chart": json.dumps(build_source_chart(sources)),
    }
