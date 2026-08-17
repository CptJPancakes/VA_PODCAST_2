# VA_PODCAST

VA_PODCAST is a simple local media dashboard designed to answer: "What's worth talking about today?"

The project is intentionally started as a GUI-first application. The goal is to establish the exact dashboard structure the future collection and ranking logic will feed.

## What this project includes

- A Flask app that serves the dashboard locally
- A simple JSON data contract for future collection logic
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
- Demo data that is clearly labeled as demo content, not live information

## What is intentionally not built yet

This repo does not include:

- crawlers
- RSS readers
- Facebook collection
- topic ranking
- keyword scoring
- clustering
- LLM workflow
- article research
- database schema
- cloud or Docker infrastructure

That work is intentionally deferred until the GUI contract is fully proven.

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

## Current GUI architecture

The app is deliberately simple:

- one Flask app
- one JSON file as the data source
- plain HTML
- plain CSS
- minimal vanilla JavaScript
- no backend service layers or unnecessary abstractions

## JSON contract

The dashboard expects this shape:

```json
{
  "updated_at": "2026-08-15T17:00:00",
  "shenandoah_valley": {
    "real_estate": [],
    "community": [],
    "business_economy": [],
    "money_finance": [],
    "food_lifestyle": [],
    "faith_family": [],
    "news_conversation": []
  },
  "northern_virginia": {
    "real_estate": [],
    "community": [],
    "business_economy": [],
    "money_finance": [],
    "food_lifestyle": [],
    "faith_family": [],
    "news_conversation": []
  }
}
```

Each topic item is expected to contain a title, summary, mention count, source count, latest activity, source names, and optional details for the expandable topic panel.

## Future direction

The planned pipeline is:

```text
online sources -> collection -> keywords/topics -> topic frequency -> related articles/posts -> write-up -> data.json -> GUI
```

The GUI is built to reflect the eventual contract, but the intelligence pipeline itself is not part of this initial mission.
