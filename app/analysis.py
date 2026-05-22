"""
Kitchen Ticket Loss Analysis Engine
Target: 4 min 54 sec = 294 seconds
"""

import pandas as pd
import json
from datetime import timedelta

TARGET_SECONDS = 294  # 4 min 54 sec
CLUSTER_GAP_MINUTES = 5  # tickets within 5 min of each other count as a cluster


def fmt_time(seconds):
    """Format seconds as m:ss string."""
    seconds = int(seconds)
    m, s = divmod(abs(seconds), 60)
    sign = "-" if seconds < 0 else ""
    return f"{sign}{m}:{s:02d}"


def parse_report(file_obj):
    """Parse the Square kitchen report CSV."""
    df = pd.read_csv(file_obj)

    # Normalize column names
    df.columns = [c.strip() for c in df.columns]

    # Parse timestamps
    df["Time Created"] = pd.to_datetime(df["Time Created"])
    df["Time Completed"] = pd.to_datetime(df["Time Completed"])

    # Completion time in seconds (use provided column)
    df["duration"] = df["Completion Time (seconds)"].astype(float)

    # Derived fields
    df["over_target"] = df["duration"] > TARGET_SECONDS
    df["seconds_over"] = (df["duration"] - TARGET_SECONDS).clip(lower=0)
    df["hour"] = df["Time Created"].dt.hour
    df["minute"] = df["Time Created"].dt.minute
    df["time_label"] = df["Time Created"].dt.strftime("%-I:%M %p")
    df["device"] = df["Device Name"].str.strip()
    df["source"] = df["Order Source"].str.strip()

    return df


def find_clusters(df, min_cluster_size=3):
    """
    Find consecutive runs of over-target tickets per device.
    A cluster is a group of ≥ min_cluster_size over-target tickets
    where each ticket starts within CLUSTER_GAP_MINUTES of the previous one.
    """
    clusters = []

    for device, grp in df.groupby("device"):
        grp = grp.sort_values("Time Created").copy()
        over = grp[grp["over_target"]].copy()
        if over.empty:
            continue

        # Build runs using time gaps
        over = over.reset_index(drop=True)
        run = [over.iloc[0]]

        for i in range(1, len(over)):
            gap = (over.at[i, "Time Created"] - run[-1]["Time Created"]).total_seconds() / 60
            if gap <= CLUSTER_GAP_MINUTES:
                run.append(over.iloc[i])
            else:
                if len(run) >= min_cluster_size:
                    clusters.append(_build_cluster(device, run))
                run = [over.iloc[i]]

        if len(run) >= min_cluster_size:
            clusters.append(_build_cluster(device, run))

    # Sort clusters by size descending, then by avg time descending
    clusters.sort(key=lambda c: (-c["ticket_count"], -c["avg_seconds"]))
    return clusters


def _build_cluster(device, rows):
    durations = [r["duration"] for r in rows]
    return {
        "device": device,
        "ticket_count": len(rows),
        "start_time": rows[0]["Time Created"].strftime("%-I:%M %p"),
        "end_time": rows[-1]["Time Created"].strftime("%-I:%M %p"),
        "avg_seconds": round(sum(durations) / len(durations)),
        "avg_fmt": fmt_time(round(sum(durations) / len(durations))),
        "max_seconds": round(max(durations)),
        "max_fmt": fmt_time(round(max(durations))),
        "total_seconds_over": round(sum(max(0, d - TARGET_SECONDS) for d in durations)),
        "tickets": [
            {
                "name": r["Ticket Name"],
                "time": r["Time Created"].strftime("%-I:%M %p"),
                "duration": r["duration"],
                "duration_fmt": fmt_time(r["duration"]),
                "over_by": fmt_time(r["duration"] - TARGET_SECONDS),
                "items": r["Items in Ticket"],
                "source": r["Order Source"],
            }
            for r in rows
        ],
    }


def hourly_summary(df):
    """Over-target ticket count and % by hour, per device."""
    result = []
    for (hour, device), grp in df.groupby(["hour", "device"]):
        total = len(grp)
        over = grp["over_target"].sum()
        result.append({
            "hour": hour,
            "hour_label": pd.Timestamp(f"2000-01-01 {hour:02d}:00").strftime("%-I %p"),
            "device": device,
            "total": int(total),
            "over": int(over),
            "pct": round(over / total * 100) if total else 0,
            "avg_seconds": round(grp["duration"].mean()),
            "avg_fmt": fmt_time(round(grp["duration"].mean())),
        })
    result.sort(key=lambda x: (x["hour"], x["device"]))
    return result


def top_offenders(df, n=10):
    """Top N longest tickets of the day."""
    top = df.nlargest(n, "duration")[
        ["Ticket Name", "device", "source", "Time Created", "duration",
         "Items in Ticket", "Number of Items"]
    ].copy()
    records = []
    for _, row in top.iterrows():
        records.append({
            "name": row["Ticket Name"],
            "device": row["device"],
            "source": row["source"],
            "time": row["Time Created"].strftime("%-I:%M %p"),
            "duration": int(row["duration"]),
            "duration_fmt": fmt_time(int(row["duration"])),
            "over_by": fmt_time(int(row["duration"]) - TARGET_SECONDS),
            "items": row["Items in Ticket"],
            "item_count": int(row["Number of Items"]),
        })
    return records


def device_summary(df):
    """Per-device summary stats."""
    result = []
    for device, grp in df.groupby("device"):
        total = len(grp)
        over = grp["over_target"].sum()
        result.append({
            "device": device,
            "total": int(total),
            "over": int(over),
            "on_time": int(total - over),
            "pct_over": round(over / total * 100) if total else 0,
            "avg_seconds": round(grp["duration"].mean()),
            "avg_fmt": fmt_time(round(grp["duration"].mean())),
            "max_seconds": int(grp["duration"].max()),
            "max_fmt": fmt_time(int(grp["duration"].max())),
            "median_seconds": round(grp["duration"].median()),
            "median_fmt": fmt_time(round(grp["duration"].median())),
        })
    return result


def build_timeline_chart_data(df):
    """
    Build data for the full-day scatter chart:
    x = time created, y = duration (seconds), color = over/on-time, per device.
    Returns a JSON-serializable dict for Plotly.
    """
    traces = []
    colors = {"over": "#ef4444", "ok": "#22c55e"}

    for device, grp in df.groupby("device"):
        for status, subgrp in grp.groupby("over_target"):
            label = "Over Target" if status else "On Time"
            color = colors["over"] if status else colors["ok"]
            traces.append({
                "x": subgrp["Time Created"].dt.strftime("%H:%M:%S").tolist(),
                "y": subgrp["duration"].tolist(),
                "text": [
                    f"{row['Ticket Name']}<br>{fmt_time(int(row['duration']))} "
                    f"({'over' if row['over_target'] else 'ok'})<br>{row['Items in Ticket']}"
                    for _, row in subgrp.iterrows()
                ],
                "mode": "markers",
                "type": "scatter",
                "name": f"{device} — {label}",
                "marker": {
                    "color": color,
                    "size": 7,
                    "opacity": 0.75,
                    "line": {"width": 0},
                },
                "hovertemplate": "%{text}<extra></extra>",
                "legendgroup": device,
            })

    # Target line
    if not df.empty:
        times = df["Time Created"].dt.strftime("%H:%M:%S").tolist()
        times_sorted = sorted(times)
        traces.append({
            "x": [times_sorted[0], times_sorted[-1]],
            "y": [TARGET_SECONDS, TARGET_SECONDS],
            "mode": "lines",
            "type": "scatter",
            "name": "Target (4:54)",
            "line": {"color": "#f59e0b", "width": 2, "dash": "dash"},
            "hoverinfo": "skip",
        })

    return traces


def build_longest_ticket_chart(df):
    """Bar chart of the top 15 longest tickets."""
    top = df.nlargest(15, "duration").copy()
    top = top.sort_values("duration")

    bars = {
        "x": top["duration"].tolist(),
        "y": (top["Ticket Name"] + " · " + top["device"]).tolist(),
        "text": [fmt_time(int(d)) for d in top["duration"].tolist()],
        "type": "bar",
        "orientation": "h",
        "marker": {
            "color": [
                "#ef4444" if d > TARGET_SECONDS else "#22c55e"
                for d in top["duration"].tolist()
            ]
        },
        "hovertemplate": "%{y}<br>%{text}<extra></extra>",
    }
    return bars


def run_analysis(file_obj):
    """Main entry point — returns all analysis as a dict."""
    df = parse_report(file_obj)

    total = len(df)
    over_count = int(df["over_target"].sum())
    on_time_count = total - over_count
    pct_over = round(over_count / total * 100, 1) if total else 0

    report_date = df["Time Created"].dt.date.iloc[0].strftime("%B %-d, %Y")
    max_row = df.loc[df["duration"].idxmax()]

    return {
        "report_date": report_date,
        "target_fmt": fmt_time(TARGET_SECONDS),
        "total_tickets": total,
        "over_count": over_count,
        "on_time_count": on_time_count,
        "pct_over": pct_over,
        "avg_seconds": round(df["duration"].mean()),
        "avg_fmt": fmt_time(round(df["duration"].mean())),
        "longest_seconds": int(max_row["duration"]),
        "longest_fmt": fmt_time(int(max_row["duration"])),
        "longest_ticket_name": max_row["Ticket Name"],
        "longest_ticket_items": max_row["Items in Ticket"],
        "longest_ticket_device": max_row["device"],
        "longest_ticket_time": max_row["Time Created"].strftime("%-I:%M %p"),
        "clusters": find_clusters(df),
        "hourly": hourly_summary(df),
        "devices": device_summary(df),
        "top_offenders": top_offenders(df, n=15),
        "timeline_traces": json.dumps(build_timeline_chart_data(df)),
        "longest_chart": json.dumps(build_longest_ticket_chart(df)),
    }
