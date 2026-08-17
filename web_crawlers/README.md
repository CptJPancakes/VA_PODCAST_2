# web_crawlers

Simple evidence-collection layer for normal (non-Facebook) web sources.

This subsystem does exactly five things:

1. holds an approved source list (`sources.json`)
2. fetches recent material from each enabled source
3. normalizes it into a common item shape
4. deduplicates it
5. saves it to `items.json`

It does **not** rank, score, cluster, summarize, or write to `data.json`.
That is the job of the future Topic Engine (not built yet). The GUI is
untouched by this subsystem.

## Files

- `crawler.py` — the whole collector. One file, no framework.
- `sources.json` — the approved source list (see format below).
- `items.json` — normalized evidence, written by the crawler. Grows over
  time; existing items are never deleted, only refreshed on re-collection.
- `README.md` — this file.

## Running it

```bash
python web_crawlers/crawler.py                    # collect from all enabled sources
python web_crawlers/crawler.py --source royal_examiner   # collect from one source
python web_crawlers/crawler.py --audit             # check source health, save nothing
python web_crawlers/crawler.py --window-hours 72    # widen the collection window
```

## Source list format (`sources.json`)

```json
{
  "id": "royal_examiner",
  "name": "Royal Examiner",
  "region": "shenandoah_valley",
  "source_type": "news",
  "url": "https://royalexaminer.com/feed/",
  "collector_type": "rss",
  "enabled": true,
  "note": "why it's enabled/disabled"
}
```

`source_type`: `news`, `government`, `business`, `events`, `schools`, `roads`.

`collector_type`: `rss`, `html`, `json`.

Disabled sources are kept in the file (not deleted) with a `note` explaining
why no clean collection path was found. A disabled entry is fine. A faked
"working" source is not — every enabled source in this file was verified
live before being turned on.

## Access order

RSS/Atom is always preferred. HTML scraping (`BeautifulSoup`) is only used
when no feed exists. Browser automation is not used at all in this
subsystem — if a source needs JavaScript rendering to expose its content, it
is disabled here with a note, not forced.

## Normalized item shape (`items.json`)

```json
{
  "item_id": "royal_examiner:1a2b3c4d5e6f7890",
  "source_id": "royal_examiner",
  "source_name": "Royal Examiner",
  "source_type": "news",
  "region": "shenandoah_valley",
  "title": "...",
  "url": "...",
  "text": "...",
  "published_at": "2026-08-16T12:00:00+00:00",
  "collected_at": "2026-08-16T18:00:00+00:00",
  "author": null,
  "category_hint": null
}
```

No ranking fields, no keyword fields, no LLM fields. That comes later.

## Identity / deduplication

`item_id` is derived from the source id plus a hash of the canonicalized
article URL (query string and trailing slash stripped). One article exists
once in `items.json` no matter how many times the crawler runs. On a
re-run, an unchanged item's `collected_at` is refreshed but no duplicate
record is created; if the source changed the title/text, that is updated in
place.

## Collection window

`WINDOW_HOURS = 24` in `crawler.py` is the single constant controlling how
far back the crawler looks for new items. It can be overridden per run with
`--window-hours` (used during validation for sparse sources, e.g. `--window-
hours 72`). Items without a parsable date are always kept rather than
silently dropped. This is deliberately simple — no timeline filtering logic
beyond this one cutoff.

## Relationship to `scrape_local_sources.py`

`scrape_local_sources.py` (repo root) was an earlier, ad hoc script that
scraped a couple of pages and hand-wrote demo-adjacent topic cards directly
into `data.json`'s shape. It is left untouched. `web_crawlers/` is the
subsystem that is meant to take over real evidence collection going
forward; `scrape_local_sources.py` is not deleted or modified by this
mission, but its future role is expected to be replaced once the Topic
Engine (which will read `web_crawlers/items.json`) exists.

## What's deliberately not here

No ranking, no Top 3, no "Why It's Trending", no "Covered By" generation,
no LLM calls, no database, no scheduler. Those all belong to Topic Engine
V1, which is a separate, future mission that reads this module's
`items.json` (plus `facebook/posts.json`) as input.
