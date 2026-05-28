from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort
)
from flask_login import login_required, login_user, logout_user, current_user
from werkzeug.security import check_password_hash

from .analysis import (
    parse_report, analyze_df,
    fmt_time, fmt_hour, source_summary,
    TARGET_SECONDS,
)
from .db import (
    save_day_tickets, get_tickets_df, get_date_bounds, get_distinct_dates,
    get_user_by_email, update_last_login,
    create_user, list_users, set_user_active,
    create_reset_token, get_valid_reset_token, mark_token_used,
    update_user_password,
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

@bp.route("/", methods=["GET"])
@login_required
def index():
    earliest, latest = get_date_bounds()
    return render_template("upload.html", has_history=bool(earliest))


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
@login_required
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
@login_required
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
        total   = len(day_df)
        over    = int(day_df["over_target"].sum())
        on_time = total - over
        avg     = round(day_df["duration"].mean())
        longest = int(day_df["duration"].max())
        rows.append({
            "report_date":     date_val,
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
         "y": [TARGET_SECONDS, TARGET_SECONDS],
         "name": "Target (294s)", "type": "scatter", "mode": "lines",
         "line": {"color": "#d97706", "width": 2, "dash": "dash"},
         "hoverinfo": "skip"},
    ]

    # Source on-target % trend
    all_sources   = sorted({s["source"] for s in source_summary(df)})
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

    # Hour-of-day bar chart
    hour_rows = []
    for hour, h_df in df.groupby("hour"):
        daily_rates = []
        for _, day_h_df in h_df.groupby("report_date"):
            t    = len(day_h_df)
            on_t = int((~day_h_df["over_target"]).sum())
            daily_rates.append(on_t / t * 100 if t else 0)
        avg_rate  = round(sum(daily_rates) / len(daily_rates), 1)
        total_tix = int(len(h_df))
        hour_rows.append({
            "hour":          int(hour),
            "hour_label":    fmt_hour(int(hour)),
            "pct_on_target": avg_rate,
            "total":         total_tix,
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

    if not email or not name or not password:
        flash("Email, name, and password are all required.", "error")
        return redirect(url_for("main.admin_users"))

    try:
        create_user(email, name, password, is_admin=is_admin)
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
