# VA_PODCAST

VA_PODCAST is a simple local media dashboard designed to answer: "What's worth talking about today?"

## What this project includes

- A Flask app that serves the dashboard locally
- A manually triggered, concurrent local-source crawler
- Deterministic filtering, clustering, civic-priority ranking, and exclusions
- Rolling Today, 24 Hours, 3 Days, 7 Days, and 14 Days views
- A fail-safe **Go Hunting** workflow with live progress, elapsed time, and ETA
- A responsive two-region dashboard for:
  - Shenandoah Valley
  - Northern Virginia
- Seven topic categories in each region:
  1. Real Estate
  2. Community
  3. Business & Economy
  4. Money & Finance
  5. Food & Lifestyle
  6. Faith & Family
  7. News & Conversation
- A 14-day evidence store with canonical timestamps and URL deduplication

## What is intentionally not built yet

This repo intentionally does not use an LLM, database, scheduler, cloud
service, or Docker infrastructure. Facebook collection remains paused.

## Install dependencies

```bash
python3 -m pip install -r requirements.txt
```

## Run locally

```bash
./run.sh
```

or directly:

```bash
python3 app.py
```

Then open the one canonical local URL:

```text
http://127.0.0.1:5050/
```

Port 5050 is used instead of Flask's default 5000 because 5000 is macOS's
AirPlay Receiver port and gets intermittently reclaimed by the OS — see
`docs/runtime-reliability.md` for the full audit.

The dev server only stays available for as long as its terminal/process is
running — there is no background daemon or auto-restart yet. If the GUI
becomes unreachable, check whether `python app.py` is still running before
assuming something else is wrong.

Starting the app never crawls. It immediately serves the last successful
dashboard from `data.json`. A network crawl occurs only when **Go Hunting** is
clicked. The progress dialog can be closed without stopping the background
hunt; click **Hunt in Progress…** to reopen it.

## Current GUI architecture

The app is deliberately simple:

- one Flask app
- one JSON file as the data source
- plain HTML
- plain CSS
- vanilla JavaScript progress/status polling
- no backend service layers or unnecessary abstractions

## JSON contract

After the first successful hunt, `data.json` uses one atomically published
bundle containing every timeframe:

```json
{
  "schema_version": 2,
  "updated_at": "2026-08-17T21:00:00+00:00",
  "default_range": "today",
  "timeframes": {
    "today": {
      "shenandoah_valley": { "real_estate": [], "community": [] },
      "northern_virginia": { "real_estate": [], "community": [] }
    },
    "24h": {},
    "3d": {},
    "7d": {},
    "14d": {}
  }
}
```

The abbreviated region objects above also contain all seven categories listed
earlier. The server continues to read the legacy root-level dashboard shape,
so existing data remains usable until the first successful hunt. Each topic
can include `civic_action` and `land_use`; the GUI renders those as priority
badges.

## Manual hunt lifecycle

The hunt pipeline is:

```text
click Go Hunting -> collect sources concurrently -> source health gate -> build five views -> atomically replace data.json
```

At least half of attempted sources must succeed, and every attempted region
must have a success. The collected results are staged until that check passes;
an unhealthy hunt cannot prune or replace either `items.json` or the last
successful dashboard. Timeframe buttons only select stored, pre-ranked views;
they never start a crawl.
