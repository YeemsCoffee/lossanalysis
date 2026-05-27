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
Short version: RDS PostgreSQL + AWS App Runner, configured via env vars.

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

## Environment variables

| Variable | Required in prod | Description |
|----------|-----------------|-------------|
| `SECRET_KEY` | ✅ | Flask session signing key — use a random 64-char hex string |
| `DATABASE_URL` | ✅ | PostgreSQL connection string (`postgresql://user:pass@host:5432/db`) |
| `INITIAL_ADMIN_EMAIL` | First deploy only | Email for the auto-seeded admin account |
| `INITIAL_ADMIN_PASSWORD` | First deploy only | Password for the auto-seeded admin account |

Leave `DATABASE_URL` unset (or empty) to use local SQLite (`history.db`).

## Replacing the logo

The header logo is an SVG approximation. To use the real Yeems Coffee logo:

1. Drop your logo file into `app/static/` (e.g. `logo.png`)
2. Update the `<img>` path in `app/templates/base.html`
