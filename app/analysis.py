"""
Kitchen Ticket Loss Analysis Engine
Target: 4 min 54 sec = 294 seconds
"""

import pandas as pd
import json

TARGET_SECONDS = 294
MIN_CLUSTER_SIZE = 3
CLUSTER_MERGE_GAP_MINUTES = 3


# ---------------------------------------------------------------------------
# Formatters (cross-platform — no %-I / %-d)
# ---------------------------------------------------------------------------

def fmt_time(seconds):
    seconds = int(round(seconds))
    sign = "-" if seconds < 0 else ""
    m, s = divmod(abs(seconds), 60)
    return f"{sign}{m}:{s:02d}"


def fmt_clock(ts):
    return ts.strftime("%I:%M %p").lstrip("0")


def fmt_hour(hour):
    return pd.Timestamp(f"2000-01-01 {hour:02d}:00").strftime("%I %p").lstrip("0")


def fmt_date(d):
    s = d.strftime("%A, %B %d, %Y")
    return s.replace(" 0", " ", 1) if " 0" in s else s


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def parse_report(file_obj) -> pd.DataFrame:
    """Parse a Square kitchen CSV into a typed DataFrame."""
    df = pd.read_csv(file_obj)
    df.columns = [c.strip() for c in df.columns]
    df["Time Created"]   = pd.to_datetime(df["Time Created"])
    df["Time Completed"] = pd.to_datetime(df["Time Completed"])
    df["duration"]       = df["Completion Time (seconds)"].astype(float)
    df["over_target"]    = df["duration"] > TARGET_SECONDS
    df["seconds_over"]   = (df["duration"] - TARGET_SECONDS).clip(lower=0)
    df["hour"]           = df["Time Created"].dt.hour
    df["source"]         = df["Order Source"].str.strip()
    return df.sort_values("Time Created").reset_index(drop=True)


def extract_iso_date(df: pd.DataFrame) -> str:
    """Return ISO date string (YYYY-MM-DD) from the df's first Time Created."""
    return df["Time Created"].dt.date.iloc[0].strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Analysis building blocks
# ---------------------------------------------------------------------------

def find_clusters(df, min_size=MIN_CLUSTER_SIZE, target_seconds=TARGET_SECONDS):
    """
    Find consecutive over-target runs per day, then merge runs within
    CLUSTER_MERGE_GAP_MINUTES of each other.
    Works correctly on multi-day data (resets at day boundaries).
    """
    all_clusters = []

    for date_val, day_df in df.groupby(df["Time Created"].dt.date):
        day_df = day_df.sort_values("Time Created").reset_index(drop=True)
        raw_runs = []
        run = []

        for _, row in day_df.iterrows():
            if row["over_target"]:
                run.append(row)
            else:
                if len(run) >= min_size:
                    raw_runs.append(run)
                run = []
        if len(run) >= min_size:
            raw_runs.append(run)

        if not raw_runs:
            continue

        # Merge nearby runs
        merged = [raw_runs[0]]
        for next_run in raw_runs[1:]:
            gap = (next_run[0]["Time Created"] - merged[-1][-1]["Time Created"]).total_seconds() / 60
            if gap <= CLUSTER_MERGE_GAP_MINUTES:
                merged[-1] = merged[-1] + next_run
            else:
                merged.append(next_run)

        for run in merged:
            if len(run) >= min_size:
                all_clusters.append(_build_cluster(run, target_seconds=target_seconds))

    all_clusters.sort(key=lambda c: (-c["ticket_count"], -c["avg_seconds"]))
    return all_clusters


def _build_cluster(rows, target_seconds=TARGET_SECONDS):
    durations = [r["duration"] for r in rows]
    return {
        "ticket_count":    len(rows),
        "start_time":      fmt_clock(rows[0]["Time Created"]),
        "end_time":        fmt_clock(rows[-1]["Time Created"]),
        "date":            rows[0]["Time Created"].strftime("%Y-%m-%d"),
        "duration_minutes": round(
            (rows[-1]["Time Created"] - rows[0]["Time Created"]).total_seconds() / 60, 1
        ),
        "avg_seconds":     round(sum(durations) / len(durations)),
        "avg_fmt":         fmt_time(sum(durations) / len(durations)),
        "max_seconds":     round(max(durations)),
        "max_fmt":         fmt_time(max(durations)),
        "total_seconds_over": round(sum(max(0, d - target_seconds) for d in durations)),
        "tickets": [
            {
                "name":         str(r["Ticket Name"]),
                "time":         fmt_clock(r["Time Created"]),
                "duration":     int(r["duration"]),
                "duration_fmt": fmt_time(r["duration"]),
                "over_by":      fmt_time(r["duration"] - target_seconds),
                "items":        str(r["Items in Ticket"]),
                "source":       str(r["Order Source"]),
            }
            for r in rows
        ],
    }


def _clusters_with_pct(clusters, total_tickets):
    for c in clusters:
        c["pct_of_day"] = round(c["ticket_count"] / total_tickets * 100, 1) if total_tickets else 0
    return clusters


def hourly_summary(df):
    rows = []
    for hour, grp in df.groupby("hour"):
        total = len(grp)
        over  = int(grp["over_target"].sum())
        rows.append({
            "hour":       int(hour),
            "hour_label": fmt_hour(hour),
            "total":      total,
            "over":       over,
            "on_time":    total - over,
            "pct_over":   round(over / total * 100) if total else 0,
            "avg_seconds": round(grp["duration"].mean()),
            "avg_fmt":    fmt_time(grp["duration"].mean()),
        })
    return sorted(rows, key=lambda x: x["hour"])


def source_summary(df):
    rows = []
    for source, grp in df.groupby("source"):
        total  = len(grp)
        over   = int(grp["over_target"].sum())
        on_time = total - over
        rows.append({
            "source":         source,
            "total":          total,
            "over":           over,
            "on_time":        on_time,
            "pct_on_target":  round(on_time / total * 100, 1) if total else 0,
            "pct_over":       round(over / total * 100, 1) if total else 0,
            "avg_seconds":    round(grp["duration"].mean()),
            "avg_fmt":        fmt_time(grp["duration"].mean()),
        })
    return sorted(rows, key=lambda x: -x["total"])


def top_offenders(df, n=15, target_seconds=TARGET_SECONDS):
    out = []
    for _, row in df.nlargest(n, "duration").iterrows():
        out.append({
            "name":         str(row["Ticket Name"]),
            "source":       str(row["source"]),
            "time":         fmt_clock(row["Time Created"]),
            "date":         row["Time Created"].strftime("%Y-%m-%d"),
            "duration":     int(row["duration"]),
            "duration_fmt": fmt_time(row["duration"]),
            "over_by":      fmt_time(row["duration"] - target_seconds),
            "items":        str(row["Items in Ticket"]),
            "item_count":   int(row["Number of Items"]),
        })
    return out


def all_over_target(df, target_seconds=TARGET_SECONDS):
    out = []
    for _, row in df[df["over_target"]].sort_values("duration", ascending=False).iterrows():
        out.append({
            "name":            str(row["Ticket Name"]),
            "source":          str(row["source"]),
            "time":            fmt_clock(row["Time Created"]),
            "duration":        int(row["duration"]),
            "duration_fmt":    fmt_time(row["duration"]),
            "over_by_seconds": int(row["duration"] - target_seconds),
            "over_by":         fmt_time(row["duration"] - target_seconds),
            "items":           str(row["Items in Ticket"]),
            "item_count":      int(row["Number of Items"]),
        })
    return out


# ---------------------------------------------------------------------------
# Chart builders
# ---------------------------------------------------------------------------

def build_timeline_chart(df, target_seconds=TARGET_SECONDS):
    over_df = df[df["over_target"]]
    ok_df   = df[~df["over_target"]]
    traces  = []

    if not ok_df.empty:
        traces.append({
            "x": ok_df["Time Created"].dt.strftime("%H:%M:%S").tolist(),
            "y": ok_df["duration"].tolist(),
            "text": [f"{r['Ticket Name']}<br>{fmt_time(r['duration'])}<br>{r['Items in Ticket']}"
                     for _, r in ok_df.iterrows()],
            "mode": "markers", "type": "scatter", "name": "On Target",
            "marker": {"color": "#2B4628", "size": 7, "opacity": 0.7, "line": {"width": 0}},
            "hovertemplate": "%{text}<extra></extra>",
        })

    if not over_df.empty:
        traces.append({
            "x": over_df["Time Created"].dt.strftime("%H:%M:%S").tolist(),
            "y": over_df["duration"].tolist(),
            "text": [f"{r['Ticket Name']}<br>{fmt_time(r['duration'])} (+{fmt_time(r['duration']-target_seconds)})<br>{r['Items in Ticket']}"
                     for _, r in over_df.iterrows()],
            "mode": "markers", "type": "scatter", "name": "Over Target",
            "marker": {"color": "#dc2626", "size": 8, "opacity": 0.85, "line": {"width": 0}},
            "hovertemplate": "%{text}<extra></extra>",
        })

    if not df.empty:
        times = sorted(df["Time Created"].dt.strftime("%H:%M:%S").tolist())
        target_label = fmt_time(target_seconds)
        traces.append({
            "x": [times[0], times[-1]], "y": [target_seconds, target_seconds],
            "mode": "lines", "type": "scatter", "name": f"Target ({target_label})",
            "line": {"color": "#d97706", "width": 2, "dash": "dash"},
            "hoverinfo": "skip",
        })
    return traces


def build_longest_chart(df, n=15, target_seconds=TARGET_SECONDS):
    top = df.nlargest(n, "duration").sort_values("duration")
    return {
        "x": top["duration"].tolist(),
        "y": [f"{name} · {fmt_clock(t)}" for name, t in
              zip(top["Ticket Name"].astype(str), top["Time Created"])],
        "text":         [fmt_time(d) for d in top["duration"].tolist()],
        "textposition": "outside",
        "type": "bar", "orientation": "h",
        "marker": {"color": ["#dc2626" if d > target_seconds else "#2B4628"
                              for d in top["duration"].tolist()]},
        "hovertemplate": "%{y}<br>%{text}<extra></extra>",
    }


def build_hourly_chart(hourly, target_seconds=TARGET_SECONDS):
    labels = [h["hour_label"] for h in hourly]
    return [
        {"x": labels, "y": [h["on_time"] for h in hourly], "name": "On Target",
         "type": "bar", "marker": {"color": "#2B4628"},
         "hovertemplate": "%{x}<br>On Target: %{y}<extra></extra>"},
        {"x": labels, "y": [h["over"] for h in hourly], "name": "Over Target",
         "type": "bar", "marker": {"color": "#dc2626"},
         "hovertemplate": "%{x}<br>Over Target: %{y}<extra></extra>"},
    ]


def build_source_chart(sources, target_seconds=TARGET_SECONDS):
    labels = [s["source"] for s in sources]
    return [
        {"x": labels, "y": [s["on_time"] for s in sources], "name": "On Target",
         "type": "bar", "marker": {"color": "#2B4628"},
         "text": [f"{s['pct_on_target']}%" for s in sources], "textposition": "inside"},
        {"x": labels, "y": [s["over"] for s in sources], "name": "Over Target",
         "type": "bar", "marker": {"color": "#dc2626"},
         "text": [f"{s['pct_over']}%" for s in sources], "textposition": "inside"},
    ]


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def analyze_df(df: pd.DataFrame, target_seconds=None, target_pct=None) -> dict:
    """
    Full analysis on any properly-typed DataFrame.
    Works for single-day (from CSV) and single-day replay (from DB).
    """
    if df.empty:
        raise ValueError("No ticket data found.")

    if target_seconds is None:
        target_seconds = TARGET_SECONDS
    if target_pct is None:
        target_pct = 85

    # Recalculate over_target and seconds_over using the passed target_seconds
    df = df.copy()
    df["over_target"]  = df["duration"] > target_seconds
    df["seconds_over"] = (df["duration"] - target_seconds).clip(lower=0)

    total      = len(df)
    over_count = int(df["over_target"].sum())
    on_time    = total - over_count
    pct_over   = round(over_count / total * 100, 1)
    pct_on     = round(on_time / total * 100, 1)
    avg_sec    = round(df["duration"].mean())
    max_row    = df.loc[df["duration"].idxmax()]
    hourly     = hourly_summary(df)
    sources    = source_summary(df)

    return {
        "report_date":           fmt_date(df["Time Created"].dt.date.iloc[0]),
        "target_seconds":        target_seconds,
        "target_fmt":            fmt_time(target_seconds),
        "target_pct":            target_pct,
        "total_tickets":         total,
        "over_count":            over_count,
        "on_time_count":         on_time,
        "pct_over":              pct_over,
        "pct_on_target":         pct_on,
        "avg_seconds":           avg_sec,
        "avg_fmt":               fmt_time(avg_sec),
        "longest_seconds":       int(max_row["duration"]),
        "longest_fmt":           fmt_time(int(max_row["duration"])),
        "longest_over_by":       fmt_time(int(max_row["duration"]) - target_seconds),
        "longest_ticket_name":   str(max_row["Ticket Name"]),
        "longest_ticket_items":  str(max_row["Items in Ticket"]),
        "longest_ticket_time":   fmt_clock(max_row["Time Created"]),
        "longest_ticket_source": str(max_row["source"]),
        "clusters":              _clusters_with_pct(find_clusters(df, target_seconds=target_seconds), len(df)),
        "hourly":                hourly,
        "sources":               sources,
        "top_offenders":         top_offenders(df, n=15, target_seconds=target_seconds),
        "all_over":              all_over_target(df, target_seconds=target_seconds),
        "timeline_chart":        json.dumps(build_timeline_chart(df, target_seconds=target_seconds)),
        "longest_chart":         json.dumps(build_longest_chart(df, target_seconds=target_seconds)),
        "hourly_chart":          json.dumps(build_hourly_chart(hourly, target_seconds=target_seconds)),
        "source_chart":          json.dumps(build_source_chart(sources, target_seconds=target_seconds)),
    }


def run_analysis(file_obj) -> dict:
    """Parse a CSV file and run the full analysis. Backward-compatible entry point."""
    return analyze_df(parse_report(file_obj))
