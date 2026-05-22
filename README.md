# Yeems Coffee — Kitchen Ticket Loss Analysis

Internal web tool for analyzing daily Square kitchen reports. Managers upload the CSV; the app surfaces over-target tickets, rush clusters, and the longest ticket of the day.

**Target:** 4 minutes 54 seconds per ticket (294 seconds).

## Setup

```bash
pip install -r requirements.txt
python wsgi.py
```

Open http://localhost:5000 and upload a `kitchenreport.csv` from Square.

## What it shows

- **Summary cards** — total tickets, on-target %, over-target count, average time, cluster count
- **Longest ticket banner** — the slowest ticket of the day with item details
- **Over-target clusters** — streaks of 3+ consecutive tickets that missed target, with the time window and ticket details
- **Top 15 longest tickets** — horizontal bar chart
- **Full-day timeline** — every ticket plotted by time and duration, with the target line drawn across
- **Hourly breakdown** — stacked bar chart, on-time vs over-target per hour
- **Order source performance** — on-target rate per channel (POS, Square Online, Uber Eats, etc.)
- **Searchable over-target table** — every ticket that missed, sorted by duration

## Replacing the logo

The header logo is currently an SVG approximation. To use the real Yeems Coffee logo:

1. Drop your logo file into `app/static/` (e.g. `logo.png`)
2. Update the path in `app/templates/base.html`
