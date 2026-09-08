"""
Client for Square's Reporting API (the Cube-based one behind the Dashboard
reports), used to pull KDS kitchen ticket times without a manual CSV export.

The API has exactly two endpoints:
  GET  /reporting/v1/meta  — schema discovery (cubes, measures, dimensions)
  POST /reporting/v1/load  — run a query

Field names live in square_sync.KDS so they can be corrected without touching
this module; the API is in open beta and its schema is discovered at runtime.
"""

import json
import os
import time
import urllib.error
import urllib.request

API_BASE = os.environ.get("SQUARE_API_BASE", "https://connect.squareup.com/reporting")
TIMEOUT  = int(os.environ.get("SQUARE_TIMEOUT", "60"))

# Cube caps how many rows one response can carry; we page until it runs dry.
PAGE_SIZE   = int(os.environ.get("SQUARE_PAGE_SIZE", "5000"))
MAX_PAGES   = 200          # ~1M rows; a guard against an endless paging loop
RETRY_ON    = {429, 500, 502, 503, 504}
MAX_RETRIES = 4


class SquareError(RuntimeError):
    """A Square API call failed in a way the caller should see verbatim."""


class SquareAuthError(SquareError):
    """Token missing, wrong, or lacking the REPORTING_READ scope."""


def _token(token=None):
    token = token or os.environ.get("SQUARE_ACCESS_TOKEN", "")
    if not token:
        raise SquareAuthError(
            "No Square access token. Set SQUARE_ACCESS_TOKEN (a production "
            "access token with the REPORTING_READ scope) or pass token=."
        )
    return token


def _request(path, payload=None, token=None, method="GET"):
    """One HTTP call, with retries on the transient statuses."""
    url  = f"{API_BASE}{path}"
    body = json.dumps(payload).encode() if payload is not None else None
    last = None

    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Authorization": f"Bearer {_token(token)}",
            "Content-Type":  "application/json",
            "Square-Version": os.environ.get("SQUARE_VERSION", "2025-01-23"),
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:800]
            if e.code in (401, 403):
                raise SquareAuthError(
                    f"Square rejected the token ({e.code}). Check it is a "
                    f"production token with the REPORTING_READ scope, and that "
                    f"the Reporting API is enabled for your application.\n{detail}"
                ) from e
            if e.code in RETRY_ON and attempt < MAX_RETRIES - 1:
                last = SquareError(f"HTTP {e.code}: {detail}")
                time.sleep(2 ** attempt)
                continue
            raise SquareError(f"HTTP {e.code} from {path}: {detail}") from e
        except urllib.error.URLError as e:
            if attempt < MAX_RETRIES - 1:
                last = SquareError(f"Could not reach Square: {e.reason}")
                time.sleep(2 ** attempt)
                continue
            raise SquareError(f"Could not reach Square: {e.reason}") from e

    raise last or SquareError("Square request failed")


def meta(token=None) -> dict:
    """Schema discovery — what cubes, measures and dimensions this account has."""
    return _request("/v1/meta", token=token)


def load(query: dict, token=None, page_size: int = PAGE_SIZE) -> list:
    """
    Run one Cube query, following pagination until every row is fetched.

    Returns the raw rows: dicts keyed by fully-qualified field name, e.g.
    {"KDS.ticket_key": "abc", "KDS.avg_ticket_time_seconds": 312.0}.
    """
    rows, offset = [], 0

    for _ in range(MAX_PAGES):
        page_query = dict(query, limit=page_size, offset=offset)
        payload    = _request("/v1/load", {"query": page_query},
                              token=token, method="POST")

        # Cube returns {"data": [...]}; be forgiving about the envelope in case
        # the beta reshapes it.
        if isinstance(payload, list):
            page = payload
        elif isinstance(payload, dict):
            page = payload.get("data")
            if page is None:
                results = payload.get("results")
                page = (results[0].get("data", []) if results else [])
        else:
            raise SquareError(f"Unexpected /v1/load response type: {type(payload)}")

        rows.extend(page)
        if len(page) < page_size:
            return rows
        offset += page_size

    raise SquareError(
        f"Stopped after {MAX_PAGES} pages ({len(rows):,} rows) — narrow the date range."
    )


def check_access(token=None) -> dict:
    """
    Confirm the token works and the KDS cube is present.

    Returns {"ok": bool, "cubes": [...], "has_kds": bool, "message": str}.
    """
    try:
        payload = meta(token=token)
    except SquareError as e:
        return {"ok": False, "cubes": [], "has_kds": False, "message": str(e)}

    names = []
    for group in ("cubes", "views"):
        for c in payload.get(group, []) or []:
            if c.get("name"):
                names.append(c["name"])

    has_kds = "KDS" in names
    return {
        "ok": True,
        "cubes": sorted(names),
        "has_kds": has_kds,
        "message": ("KDS cube available"
                    if has_kds else
                    "Token works but this account has no KDS cube — kitchen "
                    "ticket times will not be available."),
    }
