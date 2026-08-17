# topic_engine

Topic Engine V2: turns collected web evidence into the Top 3 topics per
GUI category, written into the existing `data.json` contract.

```
web_crawlers/items.json  →  topic_engine/engine.py  →  data.json  →  GUI
```

No LLM, no database, no scheduler, no embeddings. Every decision below is a
small, deterministic rule. Facebook (`facebook/posts.json`) is not read at
all in V2 — it's paused, and the engine runs fine without it.

## Running it

```bash
python topic_engine/engine.py              # discover, rank, write data.json
python topic_engine/engine.py --dry-run     # same, but data.json is not touched
python topic_engine/engine.py --timeframe 14d
```

Both print a per-category summary: how many items were read, how many
survived the local-relevance filter, how many topics were discovered, and
the selected Top 3 (title, heat, source count) per category.

## Pipeline

1. **Load evidence** — `web_crawlers/items.json`. Items from source ids
   currently marked disabled in `web_crawlers/sources.json` remain
   archived but are excluded from topic selection.
2. **Timeframe filter** — use the stable evidence timestamp (`published_at`,
   else `first_seen_at`, else legacy `collected_at`) to select Today, 24
   Hours, 3 Days, 7 Days, or 14 Days. This happens before clustering, so
   mention/source counts and ranks belong to the selected window.
3. **Regional, local-relevance, and subject filters** — see below. The same pipeline
   runs per region; items that fail that region's geography check are
   dropped before topic discovery. This keeps national material and
   generic syndicated columns from crowding out real local stories.
4. **Topic discovery (clustering)** — group items whose titles clearly
   describe the same underlying story into one topic.
5. **Category assignment** — simple keyword scoring against each item's
   title + text.
6. **Ranking** — an internal score (never shown in the GUI or written to
   `data.json`) orders topics within each category.
7. **Heat labels** — RED HOT / HOT / WARM / WATCH, from the same facts used
   for ranking.
8. **Top 3 per category** — fewer if fewer than 3 meaningful topics exist.
   No filler is ever invented.
9. **Write `data.json`** — each configured region gets the same category
   contract, with empty lists only when no evidence qualifies.

## Timeframes and timestamp stability

`run_pipeline(items_path=None, sources_path=None, timeframe="today",
now=None)` accepts these ordered keys from `config.TIMEFRAMES`:

| key | meaning |
|---|---|
| `today` | Since midnight today in `America/New_York` (calendar-based, including DST) |
| `24h` | Rolling 24 hours |
| `3d` | Rolling 72 hours |
| `7d` | Rolling 168 hours |
| `14d` | Rolling 336 hours |

Both the cutoff and current-time boundaries are inclusive. Future-dated and
undated records are omitted. Pass one timezone-aware `now` value when
building several views so every boundary and freshness calculation uses the
same clock; naive injected values are treated as UTC. `SUPPORTED_TIMEFRAMES`
and `get_timeframe_metadata()` are the app-facing tab contract.

Freshness never uses `last_seen_at`. Re-crawling an old undated page updates
its observation history without making the story new again.

## Local relevance

An item counts as locally relevant if **any** of these is true:

- its `source_type` is `"government"` (it's literally the Town of Front
  Royal's or Warren County's own feed — always relevant by definition), or
- it comes from a hyperlocal outlet (Royal Examiner, River 95.3, Downtown
  Front Royal, NVDaily's Front Royal section) **and** its RSS category is
  one the outlet itself tags as local (`Local News`, `Chamber News`,
  `Community Events`, `Crime/Court`, `Legal Notices` for Royal Examiner —
  this catches stories like a Chamber-tagged ribbon-cutting that never
  spells out "Front Royal" in the text because it assumes a local reader),
  or
- its title/text (after stripping known feed boilerplate — see below)
  mentions the target geography: Front Royal, Warren County, Shenandoah
  Valley, or a defined list of Valley localities (Shenandoah, Page,
  Rockingham, Augusta, Rockbridge, and Frederick counties; Harrisonburg,
  Staunton, Waynesboro, Winchester, Luray, etc.).

Everything else — recipes, generic career-advice columns, travel pieces,
national wire stories that happen to run on a local outlet's feed, WHSV
segments about places outside the Valley — is dropped before topic
discovery even starts.

After local relevance, a conservative title/category classifier also removes
approved low-value primary subjects: road-vehicle accidents, routine
crime-blotter or individual-arrest items, non-serious generic weather
forecasts, sports scores/recaps, obituaries, routine roadwork notices,
property listings, generic syndicated advice/recipes, and sponsored content.
Broader road-safety policy, infrastructure failures, serious weather alerts,
public-official/systemic cases, housing/development reporting, and civic
actions remain eligible. `primary_subject_exclusion_reason()` returns a
stable reason code so hunt diagnostics can explain what was discarded.

**Boilerplate stripping**: found live, and worth calling out — a WordPress
auto-footer ("The post X appeared first on Y") and a syndication CTA ("For
more news from across the Shenandoah Valley, click here") were making an
unrelated Manassas National Battlefield Park story look locally relevant,
because that CTA runs on every post from that feed regardless of subject.
`engine.py` strips a short list of known boilerplate patterns
(`config.BOILERPLATE_PATTERNS`) before using an item's text for relevance,
category, or watch-term matching, and before building its summary.

## Topic discovery (clustering)

Two items merge into one topic when their *title* words overlap enough:

- Jaccard similarity of "significant words" (length ≥ 4, common words
  filtered) is at least `TITLE_JACCARD_THRESHOLD` (0.34), **and**
- they share at least `MIN_SHARED_TITLE_WORDS` (2) words.

Both conditions exist because a false merge is worse than leaving two
related topics as separate cards — a couple of generic overlapping words on
short titles shouldn't be enough on their own.

Merging is **transitive** (connected components / union-find, not a
one-pass greedy walk): if article A's title clears the threshold against
article B's, and B's clears it against C's, all three become one topic even
if A and C don't directly share enough words. This was verified against
real evidence: three independent outlets (River 95.3, WHSV, Royal Examiner)
covered the same Confederate-school-names court ruling with different
enough headlines that only 2 of the 3 pairs directly cleared the
threshold — a simple order-dependent greedy pass merged only two of them
into one topic and left the third as a duplicate card for the same story.
Switching to connected components fixed it: all three now correctly become
one RED HOT topic with 3 independent sources.

No embeddings, no LLM, only title text is compared (article bodies are
noisier and not used for clustering in V2).

## One item = one mention

`mentions` is `len(cluster)` — a count of evidence items, never a count of
keyword occurrences inside an article. An article that says "data center"
twenty times still contributes exactly 1 mention.

## Ranking (internal only — never displayed)

A topic's position within its category comes from a simple additive score.
It is **only used to sort/select** — it is never written to `data.json` or
shown in the GUI:

| signal | contribution |
|---|---|
| independent source diversity | `(unique_source_count - 1) * 3` — this dominates; more independent outlets is the strongest trending signal |
| repeated same-source coverage | `min(total_items - unique_source_count, 3) * 0.5` — capped and small, so ten posts from one outlet can't out-rank two independent outlets |
| freshness | today = 3, this week = 2, this month = 1, older = 0 |
| locality strength | 2 if any evidence item is from a hyperlocal/government source, else 1 (everything reaching this point already passed the relevance filter) |
| significance (permanent watch priorities) | +2 if a government source is present or the topic matches a permanent watch term |
| civic priority | up to +8: +5 for a completed formal action, +4 for an upcoming vote/public hearing/scheduled action, and +3 for land use; final land decisions stack to the +8 cap |
| future Facebook/community signal | always 0 in V2 — `facebook_signal_for_topic()` is the documented extension point; Facebook is paused and not read at all in this version |

`compute_score_breakdown()` exposes every normal component, the normal-score
subtotal, the capped civic boost, and the total for deterministic tests and
debugging. The numeric score remains an internal sorting value.

### Civic action and land-use priority

The civic boost is scoped to Town/City Councils, Planning Commissions,
Boards of Supervisors/County Boards, and Boards of Zoning Appeals. General
votes receive the action boost; rezoning, land use, development,
subdivisions, data centers, annexations, easements, land purchases, and
housing projects add the land-use boost.

Each output topic always includes `civic_action` (a normalized string or
`null`) and `land_use` (boolean). Supported action strings are `PASSED`,
`APPROVED`, `ADOPTED`, `DENIED`, `DEFERRED`, `VOTED`, `UPCOMING VOTE`,
`PUBLIC HEARING`, `SCHEDULED ACTION`, and `LAND USE`. Future, negated, and
conditional wording is checked conservatively so an expected or not-yet
approved result is never presented as a completed decision.

## Heat labels

Qualitative, rule-based, using the same facts as ranking (source count,
freshness tier, significance) rather than the numeric score:

- **RED HOT** — 3+ independent sources, freshest evidence is from today.
- **HOT** — 2+ independent sources with fresh (today/this week) coverage,
  **or** a significant single-source item (government source, or matches a
  permanent watch term) that's fresh. This is what lets a major Planning
  Commission notice compete even with only one source.
- **WARM** — fresh, or has some source diversity, or is significant, but
  not both/strongly enough for HOT.
- **WATCH** — none of the above; the topic is real and locally relevant but
  currently has the weakest supporting evidence of anything selected.

`trend` (the existing GUI field, `up`/`steady`/`down`) is derived directly
from heat: RED HOT/HOT → `up`, WARM → `steady`, WATCH → `down`.

## Significant single-source items

A topic with only one supporting item is not discarded. If that item is
from a government source (an official Town/County feed) or matches a
permanent watch term (zoning, data center, Planning Commission, etc.), it
gets the same +2 significance bonus a multi-source topic would need
diversity to earn, and can reach HOT even with one source. Verified against
real evidence: the Front Royal Planning Commission's single RSS entry for
its August 19 meeting agenda ranks HOT in Real Estate despite being the
only item covering it.

## Category assignment

Simple keyword scoring (`config.CATEGORY_KEYWORDS`): count how many
distinct keywords from each category's list appear in the topic's combined
title+text, and take the category with the highest count. Ties go to
whichever category is listed first in `config.CATEGORIES`;
`news_conversation` is both last in that list and the default when nothing
matches at all, so it acts as the general-interest fallback.

A story is assigned to exactly one category — no duplication across
category lists.

## Why It's Trending / Covered By / Summary

- **why_trending** — one deterministic sentence built from the same facts
  as ranking/heat (e.g. `"Covered by 3 independent local sources today."`,
  `"Official notice from Town of Front Royal - Planning Commission."`). No
  LLM, no prose generation.
- **Covered By** — reuses the existing `source_names` / `article_links` /
  `mentions` / `source_count` / `latest_activity` fields already in the GUI
  contract. No fabricated sources; every URL comes directly from a
  supporting evidence item.
- **summary** — the longest (most complete) supporting item's text,
  boilerplate-stripped, truncated to ~260 characters at a word boundary. No
  synthesis — this may be improved by a limited LLM pass on finalists later,
  but V2 remains deterministic.

## Data written

Every configured region is produced by the same evidence pipeline. A
single-view result includes top-level `updated_at` and `timeframe`, followed
by each region's category mapping. Topic rows retain the existing contract
and add `heat`, `why_trending`, `civic_action`, and `land_use`.

## Config

Every keyword list, threshold, and term table lives in
`topic_engine/config.py` — nothing here is buried inside `engine.py`'s
logic. Tune there first.
