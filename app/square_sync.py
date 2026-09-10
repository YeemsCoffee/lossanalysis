"""
Pull KDS kitchen tickets from Square's Reporting API and store them in the
same shape a CSV upload produces, so every existing page keeps working.

Two queries per date range:

  1. ticket level — one row per KDS.ticket_key. Grouping by the ticket key
     turns the cube's aggregate measures into that single ticket's values,
     so avg_ticket_time_seconds is the ticket's own time.
  2. item level  — ticket key x item name, collapsed into the comma-separated
     "Items in Ticket" string the item-lift analysis reads.

On duration: we take Square's avg_ticket_time_seconds rather than computing
completed - created ourselves. It is the number the Kitchen Performance
report shows, which is where the CSVs came from, so history stays on one
definition. validate_against_csv() checks that claim against real data.

Note that the KDS cube also ships its own on-time measures (percent_late,
tickets_completed_on_time). Those are scored against Square's per-ticket
"time due" setting, not against this app's configurable target, so they are
deliberately not used: the headline metric stays "percent of tickets closed
at or under target_seconds", computed from the durations, and stays correct
when an admin changes the target.
"""

import os
from datetime import date, timedelta

import pandas as pd

from . import square_api
from .db import (get_last_date_by_location, get_tickets_df, record_sync_status,
                 save_day_tickets)

# ---------------------------------------------------------------------------
# Field names, all in one place.
#
# The Reporting API is in open beta and discovers its schema at runtime, so if
# Square renames something this is the only block that needs editing. Verify
# against `python -m app.square_sync schema`.
# ---------------------------------------------------------------------------

class KDS:
    CUBE            = "KDS"
    TICKET_KEY      = "KDS.ticket_key"
    TICKET_NAME     = "KDS.ticket_name"
    ORDER_SOURCE    = "KDS.order_source"
    LOCATION_NAME   = "KDS.location_name"
    LOCAL_DATE      = "KDS.local_date"
    DISPLAYED_AT    = "KDS.display_on_kds_at"
    CREATED_AT      = "KDS.chit_created_at"
    COMPLETED_AT    = "KDS.actual_completed_at"
    LINE_ITEM_COUNT = "KDS.line_item_count"
    STATION_TYPE    = "KDS.station_type"
    DEVICE          = "KDS.device_code_name"
    ITEM_NAME       = "KDS.item_name"
    QUANTITY        = "KDS.quantity"
    TIME_FILTER     = "KDS.local_reporting_timestamp"

    AVG_TICKET_SECS = "KDS.avg_ticket_time_seconds"
    TICKET_COUNT    = "KDS.ticket_count"


# A ticket can pass a prep station and an expo station; counting both would
# double it. Square's Kitchen Performance report is per-station, and "expo" is
# the customer-facing completion. Override with SQUARE_STATION_TYPE if your
# CSV turns out to match prep instead — validate_against_csv() will say.
STATION_TYPE = os.environ.get("SQUARE_STATION_TYPE", "").strip()

# Square reports timestamps in UTC. Everything downstream — the clock times on
# the report, the hour-of-day charts, the business day a ticket falls on — is
# in store time, and the database columns are naive strings with no zone to
# say which. So convert here, once, and store store-time throughout.
STORE_TZ = os.environ.get("STORE_TIMEZONE", "America/Los_Angeles").strip()

# How far back a catch-up run may reach. There is roughly two years of KDS
# history, and a cold start that tried to pull all of it on a 15-minute
# schedule would time out and retry forever. Past this the run says so
# rather than silently covering less than it should.
CATCH_UP_MAX_DAYS = int(os.environ.get("SQUARE_CATCH_UP_DAYS", "14"))

# Square's location names may not equal the app's ("Yeems Coffee - Gardena"
# vs "Gardena"). Anything unmatched falls back to substring matching.
LOCATION_MAP = {}
for pair in os.environ.get("SQUARE_LOCATION_MAP", "").split(","):
    if ":" in pair:
        square_name, app_name = pair.split(":", 1)
        LOCATION_MAP[square_name.strip()] = app_name.strip()


def map_location(square_name, known_locations):
    """Translate a Square location name into one of the app's locations."""
    if not square_name:
        return None
    if square_name in LOCATION_MAP:
        return LOCATION_MAP[square_name]
    for loc in known_locations:
        if loc.lower() in square_name.lower():
            return loc
    return None


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def _date_filter(from_date, to_date):
    return [{"dimension": KDS.TIME_FILTER, "dateRange": [from_date, to_date]}]


def _station_filter():
    if not STATION_TYPE:
        return []
    return [{"member": KDS.STATION_TYPE, "operator": "equals",
             "values": [STATION_TYPE]}]


def fetch_ticket_rows(from_date, to_date, token=None):
    """One row per KDS ticket."""
    return square_api.load({
        "measures": [KDS.AVG_TICKET_SECS],
        "dimensions": [
            KDS.TICKET_KEY, KDS.TICKET_NAME, KDS.ORDER_SOURCE,
            KDS.LOCATION_NAME, KDS.LOCAL_DATE, KDS.DISPLAYED_AT,
            KDS.CREATED_AT, KDS.COMPLETED_AT, KDS.LINE_ITEM_COUNT,
            KDS.STATION_TYPE, KDS.DEVICE,
        ],
        "timeDimensions": _date_filter(from_date, to_date),
        "filters": _station_filter(),
        # A list of pairs, not the {field: direction} object Cube also accepts:
        # Square rejects the object form with a bare 400 "Invalid request".
        "order": [[KDS.DISPLAYED_AT, "asc"]],
    }, token=token)


def fetch_item_rows(from_date, to_date, token=None):
    """Ticket key x item name, for the 'Items in Ticket' string."""
    return square_api.load({
        "measures": [KDS.TICKET_COUNT],
        "dimensions": [KDS.TICKET_KEY, KDS.ITEM_NAME, KDS.QUANTITY],
        "timeDimensions": _date_filter(from_date, to_date),
        "filters": _station_filter(),
    }, token=token)


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------

def _items_by_ticket(item_rows):
    """{ticket_key: "2x Latte, Bagel"} — matching how the CSV lists items."""
    grouped = {}
    for r in item_rows:
        key  = r.get(KDS.TICKET_KEY)
        name = (r.get(KDS.ITEM_NAME) or "").strip()
        if not key or not name:
            continue
        try:
            qty = int(float(r.get(KDS.QUANTITY) or 1))
        except (TypeError, ValueError):
            qty = 1
        grouped.setdefault(key, []).append(f"{qty}x {name}" if qty > 1 else name)
    return {k: ", ".join(v) for k, v in grouped.items()}


def _to_store_time(values):
    """
    Parse Square's UTC timestamps and return them as naive store-local times.

    tz_convert before tz_localize(None) is the whole point. Dropping the zone
    straight off a UTC timestamp keeps the UTC clock reading, which during
    Pacific daylight time puts a 9am ticket at 4pm — times that have not
    happened yet — and shifts every hour-of-day chart by seven hours.

    Naive rather than tz-aware because the tickets table stores plain strings
    and the CSV path has always written store time; a tz-aware column here
    would make Square rows and uploaded rows incomparable.
    """
    parsed = pd.to_datetime(values, format="ISO8601", utc=True)
    try:
        converted = parsed.dt.tz_convert(STORE_TZ)
    except Exception as e:
        # Storing UTC as though it were store time is the bug this function
        # exists to prevent, so refuse rather than fall back to it.
        raise ValueError(
            f"STORE_TIMEZONE={STORE_TZ!r} is not a timezone this system knows "
            f"({type(e).__name__}). Use a name like 'America/Los_Angeles'.") from e
    return converted.dt.tz_localize(None)


def rows_to_df(ticket_rows, item_rows, known_locations) -> pd.DataFrame:
    """
    Turn raw KDS rows into the DataFrame shape parse_report() produces, so it
    can go straight through save_day_tickets().
    """
    if not ticket_rows:
        return pd.DataFrame()

    items = _items_by_ticket(item_rows or [])
    records = []

    for r in ticket_rows:
        duration = r.get(KDS.AVG_TICKET_SECS)
        created  = r.get(KDS.DISPLAYED_AT) or r.get(KDS.CREATED_AT)
        finished = r.get(KDS.COMPLETED_AT)
        if duration is None or created is None:
            continue  # an open ticket, or one Square has no timing for

        key = r.get(KDS.TICKET_KEY)
        records.append({
            "Ticket Name":     str(r.get(KDS.TICKET_NAME) or key or ""),
            "Order Source":    str(r.get(KDS.ORDER_SOURCE) or "Unknown").strip(),
            "Number of Items": int(float(r.get(KDS.LINE_ITEM_COUNT) or 0)),
            "Items in Ticket": items.get(key, ""),
            "duration":        float(duration),
            "Time Created":    created,
            "Time Completed":  finished if finished is not None else created,
            "Device Name":     str(r.get(KDS.DEVICE) or ""),
            "square_location": r.get(KDS.LOCATION_NAME),
            "square_date":     r.get(KDS.LOCAL_DATE),
        })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["Time Created"]   = _to_store_time(df["Time Created"])
    df["Time Completed"] = _to_store_time(df["Time Completed"])
    df["location"] = df["square_location"].map(
        lambda n: map_location(n, known_locations))
    # Prefer Square's local_date: it already accounts for the store's timezone
    # and its business day, which a UTC timestamp would not.
    df["report_date"] = df["square_date"].fillna(
        df["Time Created"].dt.strftime("%Y-%m-%d"))

    return df.sort_values("Time Created").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_range(from_date, to_date, known_locations, token=None, dry_run=False):
    """
    Pull a date range from Square and store it, one (date, location) at a time.

    Returns a summary dict; nothing is written when dry_run is set.
    """
    ticket_rows = fetch_ticket_rows(from_date, to_date, token=token)
    item_rows   = fetch_item_rows(from_date, to_date, token=token)
    df          = rows_to_df(ticket_rows, item_rows, known_locations)

    summary = {
        "from": from_date, "to": to_date,
        "rows_fetched": len(ticket_rows),
        "tickets": int(len(df)),
        "days": [], "unmapped_locations": [], "written": 0, "dry_run": dry_run,
    }
    if df.empty:
        # Still a successful run — a closed day legitimately has no tickets,
        # and treating that as "no sync" would raise a false alarm.
        if not dry_run:
            record_sync_status(ok=True, tickets=0, days=0,
                               range=f"{from_date} to {to_date}", unmapped=[])
        return summary

    unmapped = sorted({str(n) for n in
                       df.loc[df["location"].isna(), "square_location"].unique()})
    summary["unmapped_locations"] = unmapped

    known = df[df["location"].notna()]
    for (report_date, location), group in known.groupby(["report_date", "location"]):
        summary["days"].append({
            "date": report_date, "location": location, "tickets": int(len(group)),
        })
        if not dry_run:
            save_day_tickets(report_date, group, location)
            summary["written"] += len(group)

    summary["days"].sort(key=lambda d: (d["date"], d["location"]))

    if not dry_run:
        record_sync_status(
            ok=True,
            tickets=summary["written"],
            days=len(summary["days"]),
            range=f"{from_date} to {to_date}",
            unmapped=summary["unmapped_locations"],
        )
    return summary


def sync_recent(days, known_locations, token=None, dry_run=False,
                include_today=False, catch_up=False):
    """
    Sync the last N days.

    With catch_up, the window also stretches back to the last day each
    location actually has data for, so a gap left by an outage, a stopped
    KDS or a location that stopped matching gets filled by the next
    scheduled run instead of staying a hole forever.

    include_today matters more than it looks. This process runs on UTC while
    the stores run on Pacific time, so from late afternoon onwards the server's
    "today" is already the store's tomorrow. Reaching back a couple of days and
    letting Square's own local_date place each ticket on its business day covers
    the boundary in both directions — which is why the frequent sync asks for
    two days rather than one.
    """
    end   = date.today() if include_today else date.today() - timedelta(days=1)
    start = end - timedelta(days=max(days, 1) - 1)

    if catch_up:
        start, capped = catch_up_start(known_locations, start, end)
    else:
        capped = None

    summary = sync_range(start.isoformat(), end.isoformat(),
                         known_locations, token=token, dry_run=dry_run)
    if capped:
        summary["gap_beyond_reach"] = capped
    return summary


def catch_up_start(known_locations, default_start, end):
    """
    Move the window back to cover any location that has fallen behind.

    Returns (start, gap_beyond_reach). The second is the date a location was
    last seen when that is further back than CATCH_UP_MAX_DAYS, meaning the
    scheduled run cannot close the gap on its own and a manual `sync --from`
    is needed. It is None when nothing is out of reach.

    Never returns a start later than default_start: catching up widens the
    window, it never narrows the one the caller asked for.
    """
    last_seen = get_last_date_by_location()

    # A location with no tickets at all is behind by definition — but by an
    # unknown amount, so it gets the cap rather than the whole of history.
    floor  = end - timedelta(days=max(CATCH_UP_MAX_DAYS, 1) - 1)
    wanted = [_parse_date(last_seen.get(loc)) or floor for loc in known_locations]
    if not wanted:
        return default_start, None

    # Re-sync the last day a location has rather than the day after it: an
    # afternoon sync stored a partial day, and sync_range replaces what it
    # covers, so redoing it is both safe and the point.
    oldest = min(wanted)
    return min(default_start, max(oldest, floor)), (
        oldest.isoformat() if oldest < floor else None)


def _parse_date(value):
    """A YYYY-MM-DD string as a date, or None if it is missing or malformed."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Validation — does the API agree with the CSVs already uploaded?
# ---------------------------------------------------------------------------

def _on_time_pct(df, target_seconds):
    """The headline metric: share of tickets closed at or under target."""
    if df.empty:
        return 0.0
    return float((df["duration"] <= target_seconds).mean() * 100)


def validate_against_csv(report_date, known_locations, token=None,
                         target_seconds=None, point_tolerance=1.0):
    """
    Compare a day already loaded from CSV against the same day from the API.

    Judged on the metric that actually matters — percent of tickets closed at
    or under target — because that is a threshold: a one-second systematic
    offset moves it barely at all, while a wrong start point moves it a lot.
    Average time is reported too, but only as a diagnostic. Nothing is written.
    """
    if target_seconds is None:
        from .db import get_targets
        target_seconds = get_targets()["target_seconds"]

    stored = get_tickets_df(report_date, report_date,
                            target_seconds=target_seconds)
    rows   = fetch_ticket_rows(report_date, report_date, token=token)
    items  = fetch_item_rows(report_date, report_date, token=token)
    api_df = rows_to_df(rows, items, known_locations)

    result = {
        "date": report_date,
        "target_seconds": target_seconds,
        "stored_tickets": int(len(stored)),
        "api_tickets": int(len(api_df)),
        "station_types": sorted({str(r.get(KDS.STATION_TYPE)) for r in rows
                                 if r.get(KDS.STATION_TYPE)}),
        "square_locations": sorted({str(r.get(KDS.LOCATION_NAME)) for r in rows
                                    if r.get(KDS.LOCATION_NAME)}),
        "verdict": "",
        "notes": [],
    }

    if stored.empty:
        result["verdict"] = "no-csv-data"
        result["notes"].append(
            f"Nothing stored for {report_date}; pick a date you have already "
            f"uploaded so there is something to compare against.")
        return result

    if api_df.empty:
        result["verdict"] = "no-api-data"
        result["notes"].append(
            "Square returned no KDS tickets for this date. Check the date is "
            "within KDS history and that the token covers both locations.")
        return result

    # --- the headline metric ---
    result["stored_on_time_pct"] = round(_on_time_pct(stored, target_seconds), 1)
    result["api_on_time_pct"]    = round(_on_time_pct(api_df, target_seconds), 1)
    result["on_time_difference"] = round(
        result["api_on_time_pct"] - result["stored_on_time_pct"], 1)

    # --- diagnostics ---
    result["stored_avg_seconds"] = round(float(stored["duration"].mean()), 1)
    result["api_avg_seconds"]    = round(float(api_df["duration"].mean()), 1)
    result["avg_difference"]     = round(
        result["api_avg_seconds"] - result["stored_avg_seconds"], 1)

    count_gap = result["api_tickets"] - result["stored_tickets"]
    if count_gap:
        pct = abs(count_gap) / max(result["stored_tickets"], 1) * 100
        result["notes"].append(
            f"Ticket count differs by {count_gap:+d} ({pct:.1f}%). "
            + ("More from the API usually means both prep and expo stations are "
               "counted — set SQUARE_STATION_TYPE to just one."
               if count_gap > 0 else
               "Fewer from the API can mean tickets sent to printers rather "
               "than a KDS, which Square does not track."))

    # Per-ticket comparison, matched on name where we can.
    matched = stored.merge(
        api_df[["Ticket Name", "duration"]], on="Ticket Name",
        how="inner", suffixes=("_csv", "_api"))
    if not matched.empty:
        diff = (matched["duration_api"] - matched["duration_csv"]).abs()
        result["matched_tickets"] = int(len(matched))
        result["max_difference"]  = round(float(diff.max()), 1)
        result["mean_difference"] = round(float(diff.mean()), 1)
        # Tickets that would land on opposite sides of the target — the only
        # per-ticket differences that can move the headline number.
        flipped = ((matched["duration_csv"] <= target_seconds) !=
                   (matched["duration_api"] <= target_seconds))
        result["flipped_tickets"] = int(flipped.sum())
        if result["flipped_tickets"]:
            result["notes"].append(
                f"{result['flipped_tickets']} of {len(matched)} matched tickets "
                f"fall on opposite sides of the {target_seconds}s target between "
                f"the two sources.")
    else:
        result["notes"].append(
            "No tickets matched by name, so durations could not be compared one "
            "to one; judge by the on-time percentages instead.")

    close_metric = abs(result["on_time_difference"]) <= point_tolerance
    close_count  = abs(count_gap) <= max(1, result["stored_tickets"] * 0.02)

    if close_metric and close_count:
        result["verdict"] = "match"
        result["notes"].insert(0,
            f"On-time rate agrees ({result['api_on_time_pct']}% via API vs "
            f"{result['stored_on_time_pct']}% from CSV) on the same ticket "
            f"count — safe to sync automatically.")
    elif close_count:
        result["verdict"] = "counts-match-metric-differs"
        result["notes"].insert(0,
            f"Same tickets, but on-time rate differs by "
            f"{result['on_time_difference']:+.1f} points "
            f"({result['api_on_time_pct']}% vs {result['stored_on_time_pct']}%). "
            f"The API is likely timing from a different start point than your "
            f"CSV column — try KDS.chit_created_at instead of display_on_kds_at.")
    else:
        result["verdict"] = "mismatch"
        result["notes"].insert(0,
            "API and CSV disagree on both ticket count and on-time rate. Do not "
            "sync automatically until this is understood — check the station "
            "type and location mapping below.")

    return result


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _print(title, lines):
    print(f"\n{title}\n" + "-" * len(title))
    for line in lines:
        print(line)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m app.square_sync",
        description="Pull KDS kitchen ticket times from Square.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="verify the token and that the KDS cube exists")
    sub.add_parser("schema", help="print the KDS measures and dimensions")

    v = sub.add_parser("validate",
                       help="compare an already-uploaded day against the API")
    v.add_argument("date", help="YYYY-MM-DD, a date you have uploaded by CSV")

    s = sub.add_parser("sync", help="pull a date range into the database")
    s.add_argument("--from", dest="from_date", required=True)
    s.add_argument("--to", dest="to_date", required=True)
    s.add_argument("--dry-run", action="store_true",
                   help="show what would be written without writing it")

    r = sub.add_parser("recent", help="pull the last N days (ending yesterday)")
    r.add_argument("--days", type=int, default=2)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--include-today", action="store_true",
                   help="include the in-progress day, for frequent syncing")
    r.add_argument("--catch-up", action="store_true",
                   help="also reach back to the last day each location has "
                        f"data for, up to {CATCH_UP_MAX_DAYS} days")

    args = parser.parse_args(argv)

    from .routes import LOCATIONS

    # A scheduled run on a deployment without a token should be a quiet no-op,
    # not a failure email every quarter of an hour.
    if not os.environ.get("SQUARE_ACCESS_TOKEN"):
        print("SQUARE_ACCESS_TOKEN is not set — skipping. Set it to enable "
              "Square syncing.")
        return 0

    # Writing needs a second, deliberate switch. Syncing replaces the days it
    # covers, so an unvalidated station type or location mapping would
    # overwrite good CSV data — and on a 15-minute schedule that starts
    # happening minutes after deploy rather than overnight. Reading commands
    # (check, schema, validate) are unaffected.
    if args.command in ("sync", "recent") and not args.dry_run:
        if os.environ.get("SQUARE_SYNC_ENABLED", "").strip().lower() not in (
                "1", "true", "yes", "on"):
            print("SQUARE_SYNC_ENABLED is not set — refusing to write.\n"
                  "Run `validate <date>` against a day you uploaded by CSV "
                  "first, then set SQUARE_SYNC_ENABLED=1 to arm the sync.\n"
                  "(`--dry-run` works without it.)")
            return 0

    if args.command == "check":
        info = square_api.check_access()
        print(info["message"])
        if info["cubes"]:
            print(f"\n{len(info['cubes'])} cubes/views available:")
            print("  " + ", ".join(info["cubes"]))
        return 0 if info["ok"] and info["has_kds"] else 1

    if args.command == "schema":
        payload = square_api.meta()
        for group in ("cubes", "views"):
            for cube in payload.get(group, []) or []:
                if cube.get("name") != KDS.CUBE:
                    continue
                for kind in ("measures", "dimensions", "segments"):
                    _print(f"KDS {kind}", [
                        f"  {f.get('name'):45} {f.get('type', '')}"
                        for f in cube.get(kind, [])
                    ])
        return 0

    if args.command == "validate":
        res = validate_against_csv(args.date, LOCATIONS)
        verdict = {
            "match": "MATCH",
            "counts-match-metric-differs": "PARTIAL MATCH",
            "mismatch": "MISMATCH",
            "no-csv-data": "NO CSV DATA",
            "no-api-data": "NO API DATA",
        }.get(res["verdict"], res["verdict"].upper())

        print(f"\n{verdict} for {res['date']}")
        print("=" * (len(verdict) + len(res["date"]) + 5))
        if "stored_on_time_pct" in res:
            print(f"  on-time at {res['target_seconds']}s"
                  f"    CSV {res['stored_on_time_pct']:>6.1f}%"
                  f"   API {res['api_on_time_pct']:>6.1f}%"
                  f"   ({res['on_time_difference']:+.1f} pts)")
            print(f"  tickets             "
                  f"CSV {res['stored_tickets']:>6}   API {res['api_tickets']:>6}")
            print(f"  average seconds     "
                  f"CSV {res['stored_avg_seconds']:>6.1f}   "
                  f"API {res['api_avg_seconds']:>6.1f}")
        if res.get("station_types"):
            print(f"  station types seen: {', '.join(res['station_types'])}")
        if res.get("square_locations"):
            print(f"  Square locations:   {', '.join(res['square_locations'])}")
        for note in res["notes"]:
            print(f"\n  - {note}")
        print()
        return 0 if res["verdict"] == "match" else 1

    if args.command in ("sync", "recent"):
        try:
            if args.command == "sync":
                summary = sync_range(args.from_date, args.to_date, LOCATIONS,
                                     dry_run=args.dry_run)
            else:
                summary = sync_recent(args.days, LOCATIONS, dry_run=args.dry_run,
                                      include_today=args.include_today,
                                      catch_up=args.catch_up)
        except Exception as e:
            # Record the failure before re-raising. A scheduled sync that starts
            # failing is invisible otherwise: the app would go on showing the
            # last good data as though it were current.
            if not args.dry_run:
                try:
                    from .db import record_sync_status
                    record_sync_status(ok=False, error=f"{type(e).__name__}: {e}"[:300])
                except Exception:
                    pass  # never let the bookkeeping hide the real error
            raise

        head = ("Would sync" if summary["dry_run"] else "Synced")
        print(f"\n{head} {summary['tickets']:,} tickets "
              f"({summary['from']} to {summary['to']})")
        for day in summary["days"]:
            print(f"  {day['date']}  {day['location']:<12} {day['tickets']:>6} tickets")
        if summary.get("gap_beyond_reach"):
            print(f"\n  WARNING — a location was last seen on "
                  f"{summary['gap_beyond_reach']}, further back than "
                  f"{CATCH_UP_MAX_DAYS} days. Close the rest by hand:\n"
                  f"    python -m app.square_sync sync --from "
                  f"{summary['gap_beyond_reach']} --to {summary['from']}")
        if summary["unmapped_locations"]:
            print("\n  WARNING — these Square locations did not map to a known "
                  "location and were skipped:")
            for name in summary["unmapped_locations"]:
                print(f"    {name!r}")
            print("  Set SQUARE_LOCATION_MAP, e.g. "
                  '"Yeems Coffee Gardena:Gardena,Yeems KTown:Koreatown"')
        print()
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
