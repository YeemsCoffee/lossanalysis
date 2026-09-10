"""
Find which part of the KDS query Square is rejecting.

Sends progressively richer queries and reports the first one that fails, so a
400 "Invalid request" becomes a specific culprit. Read-only — it never writes
to the database.

    python probe_square.py

It asks for the token if SQUARE_ACCESS_TOKEN isn't already set.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta

BASE = os.environ.get("SQUARE_API_BASE", "https://connect.squareup.com/reporting")

# Prompt rather than insist on the environment variable: `set` vs `export` vs
# `$env:` differs per shell, and getting that wrong is a pointless detour.
TOKEN = os.environ.get("SQUARE_ACCESS_TOKEN", "").strip()
if not TOKEN:
    TOKEN = input("Square access token: ").strip()

YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
TODAY     = date.today().isoformat()


def call(query):
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        f"{BASE}/v1/load", data=body, method="POST",
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode())
        rows = payload.get("data", payload if isinstance(payload, list) else [])
        return True, f"{len(rows)} rows", (rows[0] if rows else None)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}", None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", None


def probe(label, query, show_row=False):
    ok, detail, row = call(query)
    print(f"  [{'OK ' if ok else 'FAIL'}] {label:<46} {detail}")
    if ok and show_row and row:
        print("\n    a returned row looks like:")
        for k, v in list(row.items())[:14]:
            print(f"      {k:<42} {v!r}")
        print()
    return ok


print(f"\nProbing the KDS cube for {YESTERDAY} to {TODAY}\n")

# 1. Does anything at all work?
print("Baseline")
if not probe("one measure, nothing else", {"measures": ["KDS.ticket_count"]}):
    print("\n  Even the simplest query fails, so it is the request envelope or the")
    print("  token's access to KDS rather than any particular field.")
    sys.exit(1)

# 2. Time filtering
print("\nTime filtering")
probe("timeDimensions, no granularity", {
    "measures": ["KDS.ticket_count"],
    "timeDimensions": [{"dimension": "KDS.local_reporting_timestamp",
                        "dateRange": [YESTERDAY, TODAY]}]})
probe("timeDimensions with granularity", {
    "measures": ["KDS.ticket_count"],
    "timeDimensions": [{"dimension": "KDS.local_reporting_timestamp",
                        "dateRange": [YESTERDAY, TODAY], "granularity": "day"}]})

# 3. Dimensions, one at a time — a bad name shows up here
print("\nDimensions (one at a time)")
for dim in ["KDS.ticket_key", "KDS.ticket_name", "KDS.order_source",
            "KDS.location_name", "KDS.local_date", "KDS.line_item_count",
            "KDS.station_type", "KDS.device_code_name",
            "KDS.display_on_kds_at", "KDS.chit_created_at",
            "KDS.actual_completed_at"]:
    probe(dim, {"measures": ["KDS.ticket_count"], "dimensions": [dim]})

# 4. The measures we rely on
print("\nMeasures")
for measure in ["KDS.avg_ticket_time_seconds", "KDS.ticket_count"]:
    probe(measure, {"measures": [measure], "dimensions": ["KDS.ticket_key"]})

# 5. Query options that vary between Cube versions
print("\nQuery options")
base = {"measures": ["KDS.ticket_count"], "dimensions": ["KDS.ticket_key"]}
probe("filters: [] (empty list)",       dict(base, filters=[]))
probe("order as an object",             dict(base, order={"KDS.ticket_key": "asc"}))
probe("order as a list of pairs",       dict(base, order=[["KDS.ticket_key", "asc"]]))
probe("limit 5000",                     dict(base, limit=5000))
probe("limit 1000",                     dict(base, limit=1000))
probe("limit 500 + offset 0",           dict(base, limit=500, offset=0))

# 6. The real thing
print("\nThe query the app actually sends")
full = {
    "measures": ["KDS.avg_ticket_time_seconds"],
    "dimensions": ["KDS.ticket_key", "KDS.ticket_name", "KDS.order_source",
                   "KDS.location_name", "KDS.local_date", "KDS.display_on_kds_at",
                   "KDS.chit_created_at", "KDS.actual_completed_at",
                   "KDS.line_item_count", "KDS.station_type",
                   "KDS.device_code_name"],
    "timeDimensions": [{"dimension": "KDS.local_reporting_timestamp",
                        "dateRange": [YESTERDAY, TODAY]}],
    "filters": [],
    "order": {"KDS.display_on_kds_at": "asc"},
    "limit": 5000,
    "offset": 0,
}
probe("full query", full, show_row=True)

print("\nIf the full query failed but the pieces passed, drop options one at a")
print("time from the block above — filters, order, then limit — to find it.\n")
