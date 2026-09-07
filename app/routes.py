from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort
)
from flask_login import login_required, login_user, logout_user, current_user
from werkzeug.security import check_password_hash

from .analysis import (
    parse_report, analyze_df,
    fmt_time, fmt_hour,
)
from .db import (
    save_day_tickets, get_tickets_df, get_date_bounds, get_distinct_dates,
    get_daily_summary, get_source_daily_summary, get_hourly_daily_summary,
    get_user_by_email, update_last_login,
    create_user, list_users, set_user_active, update_user_location,
    create_reset_token, get_valid_reset_token, mark_token_used,
    update_user_password, bulk_assign_location,
    get_targets, get_setting, set_setting,
)
import io
import json

bp = Blueprint("main", __name__)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))

        user = get_user_by_email(email)
        if user and user.is_active and check_password_hash(user.password_hash, password):
            login_user(user, remember=remember)
            update_last_login(user.id)
            next_page = request.args.get("next")
            return redirect(next_page or url_for("main.index"))

        flash("Invalid email or password.", "error")

    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("main.login"))


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user  = get_user_by_email(email)
        if user and user.is_active:
            try:
                from .email import send_reset_email
                token     = create_reset_token(user.id)
                reset_url = url_for("main.reset_password", token=token, _external=True)
                send_reset_email(user.email, reset_url, user.name)
            except Exception as e:
                # Log but don't reveal the error to the user
                import logging
                logging.getLogger(__name__).error(f"Reset email failed: {e}")
        # Always show the same message — don't reveal whether the email exists
        flash("If that email is in our system, a reset link is on its way.", "info")
        return redirect(url_for("main.login"))

    return render_template("forgot_password.html")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    token_row = get_valid_reset_token(token)
    if not token_row:
        flash("This reset link is invalid or has expired. Please request a new one.", "error")
        return redirect(url_for("main.forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("reset_password.html", token=token)
        if password != confirm:
            flash("Passwords don't match.", "error")
            return render_template("reset_password.html", token=token)

        update_user_password(token_row["user_id"], password)
        mark_token_used(token)
        flash("Password updated — please sign in with your new password.", "success")
        return redirect(url_for("main.login"))

    return render_template("reset_password.html", token=token)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

LOCATIONS = ["Gardena", "Koreatown"]


@bp.route("/", methods=["GET"])
@login_required
def index():
    earliest, latest = get_date_bounds()
    user_location = current_user.location if hasattr(current_user, "location") else None
    targets = get_targets()
    return render_template(
        "upload.html",
        has_history=bool(earliest),
        user_location=user_location,
        locations=LOCATIONS,
        target_fmt=fmt_time(targets["target_seconds"]),
        target_pct=targets["target_pct"],
    )


@bp.route("/analyze", methods=["POST"])
@login_required
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

        # Determine location
        user_location = current_user.location if hasattr(current_user, "location") else None
        if user_location:
            # User has a fixed location — use it regardless of form input
            location = user_location
        else:
            location = request.form.get("location", "")

        if location not in LOCATIONS:
            flash("Please select a valid location.", "error")
            return redirect(url_for("main.index"))

        # Group by date to handle multi-day CSVs
        date_groups = list(df.groupby(df["Time Created"].dt.date))

        targets = get_targets()
        if len(date_groups) == 1:
            # Single day — existing behavior: show results
            date_val, day_df = date_groups[0]
            iso = date_val.strftime("%Y-%m-%d")
            save_day_tickets(iso, day_df, location)
            result = analyze_df(day_df, target_seconds=targets["target_seconds"], target_pct=targets["target_pct"])
            return render_template("results.html", **result, iso_date=iso)
        else:
            # Multiple days — save each group and redirect to history
            for date_val, day_df in date_groups:
                iso = date_val.strftime("%Y-%m-%d")
                save_day_tickets(iso, day_df, location)

            dates = sorted(dv.strftime("%Y-%m-%d") for dv, _ in date_groups)
            flash(
                f"{len(date_groups)} days of data uploaded "
                f"({dates[0]} to {dates[-1]})",
                "success",
            )
            return redirect(url_for("main.history"))

    except Exception as e:
        flash(f"Could not parse report: {e}", "error")
        return redirect(url_for("main.index"))


# ---------------------------------------------------------------------------
# Day drill-down (replay from DB)
# ---------------------------------------------------------------------------

@bp.route("/day/<date>")
@login_required
def day(date):
    targets = get_targets()
    df = get_tickets_df(from_date=date, to_date=date, target_seconds=targets["target_seconds"])
    if df.empty:
        flash(f"No data found for {date}.", "error")
        return redirect(url_for("main.history"))
    result = analyze_df(df, target_seconds=targets["target_seconds"], target_pct=targets["target_pct"])
    return render_template("results.html", **result, iso_date=date, from_history=True)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

@bp.route("/history")
@login_required
def history():
    earliest, latest = get_date_bounds()
    location = request.args.get("location", "")
    targets = get_targets()
    target_fmt = fmt_time(targets["target_seconds"])
    target_pct = targets["target_pct"]

    if not earliest:
        return render_template("history.html", rows=[], charts=None,
                               from_date=None, to_date=None,
                               earliest=None, latest=None,
                               location=location, locations=LOCATIONS,
                               target_fmt=target_fmt, target_pct=target_pct,
                               targets=targets)

    from_date = request.args.get("from", earliest)
    to_date   = request.args.get("to",   latest)

    loc = location if location else None
    tgt = targets["target_seconds"]

    # Every number on this page is an aggregate, so let the database do the
    # grouping and return a row per day instead of a row per ticket.
    daily = get_daily_summary(from_date, to_date, location=loc, target_seconds=tgt)
    if not daily:
        return render_template("history.html", rows=[], charts=None,
                               from_date=from_date, to_date=to_date,
                               earliest=earliest, latest=latest,
                               location=location, locations=LOCATIONS,
                               target_fmt=target_fmt, target_pct=target_pct,
                               targets=targets)

    # ---- Per-day summary rows ------------------------------------------------
    rows = []
    for d in daily:
        total   = d["total"]
        over    = d["over_count"]
        on_time = total - over
        avg     = round(d["avg_seconds"])
        longest = d["longest_seconds"]
        rows.append({
            "report_date":     d["report_date"],
            "total_tickets":   total,
            "over_count":      over,
            "on_time_count":   on_time,
            "pct_on_target":   round(on_time / total * 100, 1) if total else 0,
            "avg_seconds":     avg,
            "avg_fmt":         fmt_time(avg),
            "longest_seconds": longest,
            "longest_fmt":     fmt_time(longest),
        })
    rows.sort(key=lambda r: r["report_date"], reverse=True)

    # ---- Period-wide stats ---------------------------------------------------
    total_tickets = sum(r["total_tickets"] for r in rows)
    total_over    = sum(r["over_count"]    for r in rows)
    avg_on_target = round(sum(r["pct_on_target"] for r in rows) / len(rows), 1)
    worst_day     = min(rows, key=lambda r: r["pct_on_target"])
    best_day      = max(rows, key=lambda r: r["pct_on_target"])

    # ---- Chart data ----------------------------------------------------------
    dates_asc   = sorted(r["report_date"] for r in rows)
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
         "y": [targets["target_seconds"], targets["target_seconds"]],
         "name": f"Target ({target_fmt})", "type": "scatter", "mode": "lines",
         "line": {"color": "#d97706", "width": 2, "dash": "dash"},
         "hoverinfo": "skip"},
    ]

    # Source on-target % trend
    by_source = {}
    for r in get_source_daily_summary(from_date, to_date, location=loc,
                                      target_seconds=tgt):
        by_source.setdefault(r["source"], []).append(r)

    all_sources   = sorted(by_source)
    source_colors = ["#2B4628", "#9bc1cb", "#d97706", "#6b7280"]
    source_traces = []
    for i, src in enumerate(all_sources):
        entries = sorted(by_source[src], key=lambda r: r["report_date"])
        xs = [e["report_date"] for e in entries]
        ys = [round((e["total"] - e["over_count"]) / e["total"] * 100, 1)
              for e in entries]
        source_traces.append({
            "x": xs, "y": ys, "name": src,
            "type": "scatter", "mode": "lines+markers",
            "line": {"color": source_colors[i % len(source_colors)], "width": 2},
            "marker": {"size": 6},
            "hovertemplate": f"{src}<br>%{{x}}<br><b>%{{y:.1f}}%</b> on target<extra></extra>",
        })

    # Hour-of-day bar chart — average of each day's on-time rate for that hour
    by_hour = {}
    for r in get_hourly_daily_summary(from_date, to_date, location=loc,
                                      target_seconds=tgt):
        by_hour.setdefault(r["hour"], []).append(r)

    hour_rows = []
    for hour in sorted(by_hour):
        entries     = by_hour[hour]
        daily_rates = [(e["total"] - e["over_count"]) / e["total"] * 100
                       for e in entries]
        hour_rows.append({
            "hour":          hour,
            "hour_label":    fmt_hour(hour),
            "pct_on_target": round(sum(daily_rates) / len(daily_rates), 1),
            "total":         sum(e["total"] for e in entries),
        })

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
        location=location,
        locations=LOCATIONS,
        target_fmt=target_fmt,
        target_pct=target_pct,
        targets=targets,
    )


# ---------------------------------------------------------------------------
# Loss Drivers (correlation / root-cause analysis)
# ---------------------------------------------------------------------------

@bp.route("/drivers")
@login_required
def drivers():
    from .analysis_drivers import analyze_loss_drivers

    earliest, latest = get_date_bounds()
    location = request.args.get("location", "")
    targets  = get_targets()
    target_fmt = fmt_time(targets["target_seconds"])
    target_pct = targets["target_pct"]

    if not earliest:
        return render_template("drivers.html", drivers=None,
                               from_date=None, to_date=None,
                               earliest=None, latest=None,
                               location=location, locations=LOCATIONS,
                               target_fmt=target_fmt, target_pct=target_pct)

    from_date = request.args.get("from", earliest)
    to_date   = request.args.get("to",   latest)

    df = get_tickets_df(from_date, to_date, location=location if location else None,
                        target_seconds=targets["target_seconds"])
    if df.empty:
        return render_template("drivers.html", drivers=None,
                               from_date=from_date, to_date=to_date,
                               earliest=earliest, latest=latest,
                               location=location, locations=LOCATIONS,
                               target_fmt=target_fmt, target_pct=target_pct)

    result = analyze_loss_drivers(df, targets["target_seconds"], target_pct)
    charts = {
        "wip":     json.dumps(result["wip"]["chart"]),
        "cascade": json.dumps(result["cascade"]["chart"]),
        "size":    json.dumps(result["size"]["chart"]),
    }

    return render_template(
        "drivers.html",
        drivers=result,
        charts=charts,
        from_date=from_date,
        to_date=to_date,
        earliest=earliest,
        latest=latest,
        location=location,
        locations=LOCATIONS,
        target_fmt=target_fmt,
        target_pct=target_pct,
    )


# ---------------------------------------------------------------------------
# Patterns (day of week, weekday x hour, location comparison)
# ---------------------------------------------------------------------------

@bp.route("/patterns")
@login_required
def patterns():
    from .analysis_patterns import (
        location_comparison, weekday_hour_heatmap, weekday_summary,
    )

    earliest, latest = get_date_bounds()
    location   = request.args.get("location", "")
    targets    = get_targets()
    target_fmt = fmt_time(targets["target_seconds"])
    target_pct = targets["target_pct"]

    base = dict(from_date=None, to_date=None, earliest=None, latest=None,
                location=location, locations=LOCATIONS,
                target_fmt=target_fmt, target_pct=target_pct)

    if not earliest:
        return render_template("patterns.html", patterns=None, **base)

    from_date = request.args.get("from", earliest)
    to_date   = request.args.get("to",   latest)
    loc       = location if location else None
    tgt       = targets["target_seconds"]

    base.update(from_date=from_date, to_date=to_date,
                earliest=earliest, latest=latest)

    daily = get_daily_summary(from_date, to_date, location=loc, target_seconds=tgt)
    if not daily:
        return render_template("patterns.html", patterns=None, **base)

    hourly = get_hourly_daily_summary(from_date, to_date, location=loc,
                                      target_seconds=tgt)

    # The comparison always spans both stores, whatever the tab filter is.
    daily_by_location = {
        name: get_daily_summary(from_date, to_date, location=name, target_seconds=tgt)
        for name in LOCATIONS
    }

    result = {
        "weekday":    weekday_summary(daily, target_pct),
        "heatmap":    weekday_hour_heatmap(hourly, target_pct),
        "comparison": location_comparison(daily_by_location, target_pct),
        "total_tickets": sum(d["total"] for d in daily),
        "days": len(daily),
    }
    charts = {
        "weekday":    json.dumps(result["weekday"]["chart"]),
        "heatmap":    json.dumps(result["heatmap"]["chart"]),
        "comparison": json.dumps(result["comparison"]["chart"]),
    }
    return render_template("patterns.html", patterns=result, charts=charts, **base)


# ---------------------------------------------------------------------------
# Admin — Manage Managers
# ---------------------------------------------------------------------------

def _require_admin():
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)


@bp.route("/admin/users")
@login_required
def admin_users():
    _require_admin()
    users = list_users()
    return render_template("admin_users.html", users=users)


@bp.route("/admin/users/create", methods=["POST"])
@login_required
def admin_create_user():
    _require_admin()
    email    = request.form.get("email", "").strip()
    name     = request.form.get("name", "").strip()
    password = request.form.get("password", "")
    is_admin = bool(request.form.get("is_admin"))
    location = request.form.get("location", "") or None

    if not email or not name or not password:
        flash("Email, name, and password are all required.", "error")
        return redirect(url_for("main.admin_users"))

    try:
        create_user(email, name, password, is_admin=is_admin, location=location)
        flash(f"Account created for {name} ({email}).", "success")
    except Exception as e:
        if "UNIQUE" in str(e).upper() or "unique" in str(e).lower():
            flash(f"An account with that email already exists.", "error")
        else:
            flash(f"Could not create account: {e}", "error")

    return redirect(url_for("main.admin_users"))


@bp.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@login_required
def admin_toggle_user(user_id):
    _require_admin()
    if user_id == current_user.id:
        flash("You cannot disable your own account.", "error")
        return redirect(url_for("main.admin_users"))

    action = request.form.get("action", "disable")
    set_user_active(user_id, active=(action == "enable"))
    flash(f"Account {'enabled' if action == 'enable' else 'disabled'}.", "success")
    return redirect(url_for("main.admin_users"))


@bp.route("/admin/users/<int:user_id>/location", methods=["POST"])
@login_required
def admin_update_location(user_id):
    _require_admin()
    location = request.form.get("location", "") or None
    update_user_location(user_id, location)
    flash("Location updated.", "success")
    return redirect(url_for("main.admin_users"))


@bp.route("/admin/data/assign-location", methods=["POST"])
@login_required
def admin_assign_location():
    _require_admin()
    location  = request.form.get("location", "")
    overwrite = request.form.get("overwrite") == "1"

    if location not in LOCATIONS:
        flash("Invalid location.", "error")
        return redirect(url_for("main.admin_users"))

    count = bulk_assign_location(location, overwrite=overwrite)
    scope = "all tickets" if overwrite else "unassigned tickets"
    flash(f"{count} {scope} assigned to {location}.", "success")
    return redirect(url_for("main.admin_users"))


@bp.route("/admin/settings", methods=["GET", "POST"])
@login_required
def admin_settings():
    _require_admin()
    if request.method == "POST":
        minutes = int(request.form.get("minutes", 4))
        seconds = int(request.form.get("seconds", 54))
        target_pct = int(request.form.get("target_pct", 85))
        target_seconds = minutes * 60 + seconds
        set_setting("target_seconds", str(target_seconds))
        set_setting("target_pct", str(target_pct))
        flash(f"Target updated to {minutes}m {seconds}s at {target_pct}% goal.", "success")
        return redirect(url_for("main.admin_settings"))
    targets = get_targets()
    ts = targets["target_seconds"]
    return render_template("admin_settings.html",
        target_minutes=ts // 60,
        target_seconds_rem=ts % 60,
        target_pct=targets["target_pct"],
    )
