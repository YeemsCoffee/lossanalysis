# Yeems Coffee — Kitchen Ticket Loss Analysis

Internal web tool for analyzing daily Square kitchen reports. Managers
upload the CSV; the app surfaces over-target tickets, rush clusters, and
the longest ticket of the day.

**Target:** 4 minutes 54 seconds per ticket (294 seconds).

---

## Running locally

```bash
pip install -r requirements.txt
python wsgi.py
```

Open http://localhost:5000 — you'll be prompted to sign in.  
On first boot, create a `.env` file from `.env.example` and set
`INITIAL_ADMIN_EMAIL` + `INITIAL_ADMIN_PASSWORD` to seed the first account.

Or skip auth entirely for local testing by temporarily commenting out
`@login_required` in `routes.py` — don't do this in production.

## Deploying to AWS

See **[DEPLOY.md](DEPLOY.md)** for the full step-by-step guide.  
Short version: RDS PostgreSQL + AWS Elastic Beanstalk, configured via env vars.

## What it shows

- **Summary cards** — total tickets, on-target %, over-target count, average time, cluster count
- **Longest ticket banner** — the slowest ticket of the day with item details
- **Over-target clusters** — streaks of 3+ consecutive tickets that missed target, merged if ≤ 3 min apart
- **Top 15 longest tickets** — horizontal bar chart
- **Full-day timeline** — every ticket plotted by time and duration, with the target line
- **Hourly breakdown** — stacked bar chart, on-time vs over-target per hour
- **Order source performance** — on-target rate per channel (POS, Square Online, Uber Eats, etc.)
- **Searchable over-target table** — every ticket that missed, sorted by duration
- **Historical dashboard** — trends, heatmaps, and day-by-day table across any date range
- **Loss Drivers** — kitchen load threshold, cascade effect, problem items, order-size threshold
- **Patterns** — day-of-week performance, weekday x hour hot spots, and store comparison

## Environment variables

| Variable | Required in prod | Description |
|----------|-----------------|-------------|
| `SECRET_KEY` | ✅ | Flask session signing key — use a random 64-char hex string |
| `DATABASE_URL` | ✅ | PostgreSQL connection string (`postgresql://user:pass@host:5432/db`) |
| `INITIAL_ADMIN_EMAIL` | First deploy only | Email for the auto-seeded admin account |
| `INITIAL_ADMIN_PASSWORD` | First deploy only | Password for the auto-seeded admin account |
| `MAX_UPLOAD_MB` | | Upload size cap, default 64 — keep in step with nginx |
| `DB_POOL_MAX` | | Postgres connections per worker, default 4 |

Leave `DATABASE_URL` unset (or empty) to use local SQLite (`history.db`).

Without `SECRET_KEY`, the app refuses to start when `DATABASE_URL` is set —
session cookies signed with a known key would be forgeable.

## Automatic sync from Square

Kitchen ticket times can be pulled from Square's Reporting API instead of
exporting a CSV. The `KDS` cube carries per-ticket prep times — the same
figures behind the Kitchen Performance report the CSVs come from.

**Set this up in order. Do not enable the scheduled job until step 3 passes.**

1. **Get a token.** developer.squareup.com → your application → Credentials →
   production access token. It needs the `REPORTING_READ` scope. Then:

   ```bash
   export SQUARE_ACCESS_TOKEN=...        # Windows: set SQUARE_ACCESS_TOKEN=...
   python -m app.square_sync check
   ```

   This confirms the token works and that your account has the `KDS` cube.

2. **Map the locations.** Square's location names may not match this app's.
   A dry run shows what came back and what didn't map:

   ```bash
   python -m app.square_sync sync --from 2026-09-01 --to 2026-09-01 --dry-run
   ```

   If it reports unmapped locations, set:

   ```bash
   export SQUARE_LOCATION_MAP="Yeems Coffee Gardena:Gardena,Yeems KTown:Koreatown"
   ```

3. **Prove the numbers match.** Pick a date already uploaded by CSV and
   compare the two sources on the metric that matters — percent of tickets
   closed at or under target:

   ```bash
   python -m app.square_sync validate 2026-08-15
   ```

   A `MATCH` means the API is measuring the same thing and history stays
   continuous. Anything else is explained in the output; the two usual causes
   are both prep and expo stations being counted (fix with
   `SQUARE_STATION_TYPE=expo`) or the API timing from a different start point.
   Re-run until it matches.

4. **Backfill and arm it.** Once validated, set `SQUARE_SYNC_ENABLED=1` and:

   ```bash
   python -m app.square_sync sync --from 2026-06-01 --to 2026-09-07
   ```

   Then set `SQUARE_ACCESS_TOKEN`, `SQUARE_SYNC_ENABLED=1` and any mapping
   variables as Elastic Beanstalk environment properties.

Syncing writes through the same path as a CSV upload — re-running a date
replaces it rather than duplicating, so backfills and repeated syncs are safe.

### The scheduled job

`.ebextensions/cron-square-sync.config` runs the sync **every 15 minutes**,
which is as fresh as the Reporting API gets — most cubes lag about that long.
Today's numbers appear on the dashboard during service rather than the next
morning.

Two switches guard it, both off by default:

- without `SQUARE_ACCESS_TOKEN` the command is a no-op
- without `SQUARE_SYNC_ENABLED=1` it refuses to write

That second gate exists because syncing *replaces* the days it covers. An
unvalidated station type or location mapping would overwrite good CSV data,
and on a 15-minute schedule that starts minutes after deploy. `--dry-run`
and the read-only commands work regardless.

The job syncs yesterday **and** today. That is deliberate: the server runs
UTC while the stores run Pacific, so from late afternoon the server's "today"
is already the store's tomorrow. Square's own `local_date` files each ticket
under the right business day.

Note that today will be a partial day until close, so it shows fewer tickets
than a finished one — worth remembering when comparing it on History or
Patterns.

### Square environment variables

| Variable | Description |
|----------|-------------|
| `SQUARE_ACCESS_TOKEN` | Production token with `REPORTING_READ`. Unset = syncing disabled |
| `SQUARE_SYNC_ENABLED` | Set to `1` to allow writes. Unset = read-only, dry runs still work |
| `SQUARE_LOCATION_MAP` | `Square name:App name` pairs, comma separated |
| `SQUARE_STATION_TYPE` | Restrict to one KDS station (e.g. `expo`) to avoid double counting |
| `SQUARE_API_BASE` | Defaults to `https://connect.squareup.com/reporting` |

### A note on the on-time metric

The KDS cube ships its own `percent_late` and `tickets_completed_on_time`
measures, but those score against Square's per-ticket "time due" setting — not
this app's target. The headline number stays "percent of tickets closed at or
under `target_seconds`", computed here from the durations, so it remains
correct when an admin changes the target in Settings.

## Running the tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

That runs everything against SQLite. To also exercise the production Postgres
path — connection pooling, batched inserts, the SQL aggregation — point it at a
scratch database:

```bash
TEST_DATABASE_URL=postgresql://user@localhost/lossanalysis_test pytest
```

## Replacing the logo

The header logo is an SVG approximation. To use the real Yeems Coffee logo:

1. Drop your logo file into `app/static/` (e.g. `logo.png`)
2. Update the `<img>` path in `app/templates/base.html`
