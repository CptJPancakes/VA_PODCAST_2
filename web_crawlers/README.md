# web_crawlers

Simple evidence-collection layer for normal (non-Facebook) web sources.

This subsystem does exactly five things:

1. holds an approved source list (`sources.json`)
2. fetches recent material from each enabled source
3. normalizes it into a common item shape
4. deduplicates it
5. saves it to `items.json`

It does **not** rank, score, cluster, summarize, or write to `data.json`.
That is the separate Topic Engine's job.

## Files

- `crawler.py` — the whole collector. One file, no framework.
- `sources.json` — the approved source list (see format below).
- `items.json` — normalized active evidence, written atomically by the crawler
  and retained for a rolling 14 days.
- `README.md` — this file.

## Running it

```bash
python web_crawlers/crawler.py                    # collect from all enabled sources
python web_crawlers/crawler.py --source royal_examiner   # collect from one source
python web_crawlers/crawler.py --audit             # check source health, save nothing
python web_crawlers/crawler.py --window-hours 72    # widen the collection window
python web_crawlers/crawler.py --max-workers 4      # lower the concurrency limit
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
  "first_seen_at": "2026-08-16T18:00:00+00:00",
  "last_seen_at": "2026-08-17T13:00:00+00:00",
  "author": null,
  "category_hint": null
}
```

No ranking fields, no keyword fields, no LLM fields. That comes later.

## Identity / deduplication

`item_id` is derived from the source id plus a hash of the canonicalized
article URL. Fragments, trailing slashes, and known analytics parameters are
removed; meaningful query parameters are retained because government systems
such as Granicus use values like `event_id` and `clip_id` to distinguish
meetings. One article exists once in `items.json` no matter how many times the
crawler runs. On a re-run,
`published_at`, `first_seen_at`, and the compatibility field `collected_at`
remain unchanged; only `last_seen_at` advances. If the source changes the
title/text, that content is updated in place. This prevents an old undated
page from looking new simply because it was crawled again. Pre-migration
records containing only `collected_at` remain supported.

## Collection window

`WINDOW_HOURS = 336` in `crawler.py` lets a first crawl populate as much of
the 14-day view as each source exposes. It can be overridden per run with
`--window-hours`. Items without a parsable publication date are collected,
then age from their immutable `first_seen_at` value. RSS/Atom/JSON entries are
date-filtered before the per-source accepted-item safety cap is applied, so old
feed entries cannot hide newer in-window material farther down the feed.

After at least one source succeeds, the merged store is pruned to a rolling
14 days using `published_at`, otherwise `first_seen_at`, otherwise the legacy
`collected_at`. `last_seen_at` is never used for freshness or retention. If
every source fails, the existing file is left completely untouched. The app
also supplies a `commit_predicate` source-health gate; when it rejects the
collection, merge, pruning, and saving are all skipped byte-for-byte.

## Concurrency and progress

Enabled sources are fetched concurrently with a bounded pool (eight workers
by default). Each source remains isolated, so one failure cannot cancel the
others. Results are merged once in configured source order after all fetches
finish, then saved once with an atomic file replacement.

Library callers can pass `progress_callback` to `run_crawl`; it is called as
each source completes with cumulative total/completed/succeeded/failed counts,
the current source and region, result status, item count, and duration. A
callback error is logged but cannot fail the crawl. The final summary includes
the same overall counts plus per-region attempted/succeeded/failed counts.

## Relationship to `scrape_local_sources.py`

`scrape_local_sources.py` (repo root) is an earlier, ad hoc script that
scraped a couple of pages and wrote topic-card-shaped data directly. It is
left untouched for history; the current production path is this evidence
collector followed by `topic_engine/engine.py`.

## What's deliberately not here

No ranking, no Top 3, no "Why It's Trending", no "Covered By" generation,
no LLM calls, no database, no scheduler. Ranking and presentation belong to
the separate Topic Engine, which reads this module's `items.json` output.
