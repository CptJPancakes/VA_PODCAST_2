"""
topic_engine/engine.py

Topic Engine V1.

Reads collected web evidence (web_crawlers/items.json), groups related
items into topics, ranks them, assigns each to the best-fitting existing
GUI category, picks the Top 3 per category, and writes the result into the
existing data.json contract the GUI already reads.

This module does NOT use an LLM, a database, embeddings, or a scheduler.
Every decision below is a small, deterministic, readable rule — see
topic_engine/README.md for the full explanation of each one.

Usage:
    python topic_engine/engine.py              # write data.json
    python topic_engine/engine.py --dry-run     # show selections, write nothing
"""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

try:
    # Works when imported as a package, e.g. `from topic_engine import engine`
    # (how the test suite imports it, run from the repo root).
    from topic_engine import config
except ImportError:
    # Works when run directly: `python topic_engine/engine.py` puts this
    # file's own directory on sys.path, not the repo root.
    import config

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
ITEMS_PATH = REPO_ROOT / "web_crawlers" / "items.json"
SOURCES_PATH = REPO_ROOT / "web_crawlers" / "sources.json"
DATA_PATH = REPO_ROOT / "data.json"


# ---------------------------------------------------------------------------
# evidence loading
# ---------------------------------------------------------------------------

def load_items(path=None):
    path = path if path is not None else ITEMS_PATH
    path = Path(path)
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as handle:
        items = json.load(handle)
    return items if isinstance(items, list) else []


def load_disabled_source_ids(path=None):
    """Return source ids explicitly disabled in the crawler registry.

    Raw evidence is intentionally retained in items.json, so the Topic
    Engine must enforce current source status when selecting dashboard
    topics. Unknown source ids remain eligible, which keeps fixtures and
    other evidence producers independent of this registry.
    """
    path = Path(path) if path is not None else SOURCES_PATH
    if not path.exists():
        return set()
    with open(path, "r", encoding="utf-8") as handle:
        sources = json.load(handle)
    if not isinstance(sources, list):
        return set()
    return {
        source.get("id")
        for source in sources
        if source.get("id") and source.get("enabled") is False
    }


def parse_iso(value):
    """Best-effort parse of the ISO timestamps items.json already stores."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def item_datetime(item):
    """The evidence item's best-known timestamp: published_at, else collected_at."""
    return parse_iso(item.get("published_at")) or parse_iso(item.get("collected_at"))


def clean_text(value):
    return " ".join((value or "").split())


_BOILERPLATE_RE = [re.compile(pattern, re.IGNORECASE) for pattern in config.BOILERPLATE_PATTERNS]


def strip_boilerplate(text):
    """Remove generic auto-generated feed/site footers before this text is
    used for relevance/category matching or shown as a summary. Without
    this, a syndication footer like "For more news from across the
    Shenandoah Valley, click here" (present on unrelated posts too) can
    make an item look locally relevant when its actual content is not."""
    text = text or ""
    for pattern in _BOILERPLATE_RE:
        text = pattern.sub("", text)
    return clean_text(text)


def analysis_text(item):
    """Title + boilerplate-stripped body text — the text actually used for
    relevance/category/watch-term matching and for building summaries."""
    return f"{item.get('title', '')} {strip_boilerplate(item.get('text'))}"


_TERM_CACHE = {}


def contains_term(text, term):
    """Evidence Quality Cleanup V2 — Fix 1: whole-word/phrase-safe matching
    for semantic keyword checks (category keywords, permanent watch terms,
    local place terms, context-exclusion terms). Plain `term in text`
    substring checks let a keyword like "trail" match inside "trailer" —
    found live: it made a construction-worker-fatality story qualify for
    Food & Lifestyle. Case-insensitive; single-word terms get simple plural
    support ("trail" also matches "trails") without matching inside a
    longer word ("trailer", "trailing", "contrail"); multi-word phrases
    match as a whole phrase. Punctuation/whitespace around a match are
    valid boundaries (not just other letters/digits), so this needs no
    separate punctuation-stripping step. Not a general NLP/tokenizer — just
    a boundary-aware substring check."""
    if not text or not term:
        return False
    term = term.strip().lower()
    pattern = _TERM_CACHE.get(term)
    if pattern is None:
        escaped = re.escape(term)
        suffix = "s?" if " " not in term else ""  # simple plural only for single words
        pattern = re.compile(r"(?<![A-Za-z0-9])" + escaped + suffix + r"(?![A-Za-z0-9])", re.IGNORECASE)
        _TERM_CACHE[term] = pattern
    return pattern.search(text) is not None


# ---------------------------------------------------------------------------
# local relevance
# ---------------------------------------------------------------------------

def is_locally_relevant(item):
    """Government feeds ARE the town/county, so they're always relevant.
    Everything else (including hyperlocal outlets, which also run wire/
    syndicated filler) must actually mention the target geography.

    The place-name and hyperlocal-source tables are looked up by the item's
    own `region` field, so the exact same relevance rule applies to any
    region — only the geography data differs (topic_engine/config.py).

    Evidence Quality Cleanup V1: sponsored/ad content (Fix 3) is excluded
    outright, and generic non-local-news content (Fix 4, e.g. a movie
    review) only counts a place-name TITLE match, not an incidental single
    mention buried in the body — see GENERIC_CONTENT_HINTS."""
    category_hint = (item.get("category_hint") or "").strip().lower()
    if category_hint in config.EXCLUDED_CATEGORY_HINTS:
        return False

    if item.get("source_type") in config.ALWAYS_LOCAL_SOURCE_TYPES:
        return True
    hint_allowlist = config.LOCAL_CATEGORY_HINTS.get(item.get("source_id"))
    if hint_allowlist and item.get("category_hint") in hint_allowlist:
        return True

    title = clean_text(item.get("title"))
    body = strip_boilerplate(item.get("text"))
    is_generic_content = item.get("category_hint") in config.GENERIC_CONTENT_HINTS
    place_terms = config.LOCAL_PLACE_TERMS_BY_REGION.get(item.get("region"), [])
    for term in place_terms:
        if contains_term(title, term):
            return True
        if contains_term(body, term) and not is_generic_content:
            return True
    return False


# ---------------------------------------------------------------------------
# topic discovery: title-similarity clustering
# ---------------------------------------------------------------------------

def significant_words(title):
    words = re.findall(r"[A-Za-z']+", (title or "").lower())
    return {
        w for w in words
        if len(w) >= config.MIN_SIGNIFICANT_WORD_LENGTH and w not in config.STOPWORDS
    }


def clustering_words(title):
    """Evidence Quality Cleanup V1 — Fix 1: significant_words(), but with
    recurring roundup/briefing title templates ("Daily Debrief", "Morning
    Notes", ...) stripped out first. Found live: ARLnow/FFXnow/ALXnow each
    publish their own daily roundup under that shared template wording,
    which otherwise reads as strong title overlap even though they're three
    separate posts about three separate outlets' own local coverage — not
    the same underlying story. Used only for clustering; if the remaining,
    genuinely-distinguishing words in two titles still overlap enough
    (e.g. both roundups happen to headline the same specific event), they
    still merge normally."""
    text = (title or "").lower()
    for phrase in config.RECURRING_TITLE_PHRASES:
        text = text.replace(phrase, " ")
    return significant_words(text)


def title_overlap(words_a, words_b):
    """Returns (jaccard, shared_word_count)."""
    if not words_a or not words_b:
        return 0.0, 0
    shared = words_a & words_b
    union = words_a | words_b
    return (len(shared) / len(union) if union else 0.0), len(shared)


def cluster_items(items):
    """Connected-components clustering on title-word overlap (union-find).
    Two items merge only when they clear BOTH a Jaccard threshold and a
    minimum shared-word count — conservative on purpose, per the "a false
    merge is worse than two separate cards" rule.

    This merges transitively: if article A's title overlaps article B's
    enough, and B's overlaps article C's enough, A/B/C all become one topic
    even if A and C don't directly share enough words — B has already
    established they're the same underlying story. A simpler "compare each
    new item to existing clusters only" approach is order-dependent and can
    miss exactly this case (verified against real evidence: three
    independent outlets covering the same Confederate-school-names ruling,
    where only one of the three pairs directly cleared the threshold)."""
    n = len(items)
    word_sets = [clustering_words(it.get("title")) for it in items]
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            jaccard, shared_count = title_overlap(word_sets[i], word_sets[j])
            if jaccard >= config.TITLE_JACCARD_THRESHOLD and shared_count >= config.MIN_SHARED_TITLE_WORDS:
                union(i, j)

    groups = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(items[idx])
    return list(groups.values())


# ---------------------------------------------------------------------------
# category assignment
# ---------------------------------------------------------------------------

def keyword_counts_in_excluded_context(keyword, combined_text):
    """Evidence Quality Cleanup V1 — Fix 2: some category keywords are
    ambiguous — "construction" is legitimate real_estate signal for
    development/construction-project stories, but not for a construction-
    WORKER-fatality story, which is a public-safety story. If any of the
    keyword's excluded-context phrases is present, that keyword does not
    count toward the category for this topic. Every other keyword (and
    every other category) is unaffected."""
    context_terms = config.CONTEXT_SENSITIVE_KEYWORDS.get(keyword)
    return bool(context_terms) and any(contains_term(combined_text, term) for term in context_terms)


def assign_category(cluster):
    combined = " ".join(analysis_text(it) for it in cluster)
    best_category, best_count = config.DEFAULT_CATEGORY, 0
    for category in config.CATEGORIES:
        count = sum(
            1 for keyword in config.CATEGORY_KEYWORDS[category]
            if contains_term(combined, keyword) and not keyword_counts_in_excluded_context(keyword, combined)
        )
        if count > best_count:
            best_category, best_count = category, count
    return best_category


# ---------------------------------------------------------------------------
# topic facts: everything ranking / heat / explanations are derived from
# ---------------------------------------------------------------------------

def compute_freshness_tier(latest_dt):
    if latest_dt is None:
        return "older"
    age_hours = (datetime.now(timezone.utc) - latest_dt).total_seconds() / 3600
    if age_hours <= config.FRESH_TODAY_HOURS:
        return "today"
    if age_hours <= config.FRESH_THIS_WEEK_HOURS:
        return "this_week"
    if age_hours <= config.FRESH_THIS_MONTH_HOURS:
        return "this_month"
    return "older"


def build_topic_facts(cluster):
    items_sorted = sorted(
        cluster,
        key=lambda it: item_datetime(it) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    latest_item = items_sorted[0]
    latest_dt = item_datetime(latest_item)

    unique_source_ids, unique_source_names, seen = [], [], set()
    for item in items_sorted:
        sid = item.get("source_id")
        if sid not in seen:
            seen.add(sid)
            unique_source_ids.append(sid)
            unique_source_names.append(item.get("source_name"))

    has_government_source = any(it.get("source_type") == "government" for it in cluster)
    has_hyperlocal_source = any(
        it.get("source_type") == "government"
        or it.get("source_id") in config.HYPERLOCAL_SOURCE_IDS_BY_REGION.get(it.get("region"), set())
        for it in cluster
    )

    combined_text = " ".join(analysis_text(it) for it in cluster)
    matched_watch_terms = [term for term in config.PERMANENT_WATCH_TERMS if contains_term(combined_text, term)]

    representative_item = max(cluster, key=lambda it: len(it.get("text") or ""))

    dated_items = [it for it in cluster if item_datetime(it) is not None]
    earliest_dt = min((item_datetime(it) for it in dated_items), default=latest_dt)

    article_links, seen_urls = [], set()
    for item in items_sorted:
        url = item.get("url")
        if url and url not in seen_urls:
            seen_urls.add(url)
            article_links.append(url)

    return {
        "items": items_sorted,
        "latest_item": latest_item,
        "latest_dt": latest_dt,
        "earliest_dt": earliest_dt,
        "representative_item": representative_item,
        "unique_source_ids": unique_source_ids,
        "unique_source_names": unique_source_names,
        "unique_source_count": len(unique_source_ids),
        "total_items": len(cluster),
        "has_government_source": has_government_source,
        "has_hyperlocal_source": has_hyperlocal_source,
        "matched_watch_terms": matched_watch_terms,
        "freshness_tier": compute_freshness_tier(latest_dt),
        "article_links": article_links,
        "primary_source_name": latest_item.get("source_name"),
    }


# ---------------------------------------------------------------------------
# ranking (internal score used only to sort/select — never shown in the GUI)
# ---------------------------------------------------------------------------

def facebook_signal_for_topic(facts):
    """Extension point: Facebook is paused for V1, so this always
    contributes 0. Once facebook/posts.json has live data again, this is
    where post/comment/reaction counts for the same topic would be added
    into the score without changing anything else in the pipeline."""
    return 0


def compute_score(facts):
    diversity_bonus = (facts["unique_source_count"] - 1) * 3  # independent sources matter most
    repeat_same_source_bonus = min(facts["total_items"] - facts["unique_source_count"], 3) * 0.5  # capped, diminishing
    freshness_bonus = {"today": 3, "this_week": 2, "this_month": 1, "older": 0}[facts["freshness_tier"]]
    locality_bonus = 2 if facts["has_hyperlocal_source"] else 1  # already filtered to be locally relevant
    significance_bonus = 2 if (facts["has_government_source"] or facts["matched_watch_terms"]) else 0
    return (
        diversity_bonus
        + repeat_same_source_bonus
        + freshness_bonus
        + locality_bonus
        + significance_bonus
        + facebook_signal_for_topic(facts)
    )


# ---------------------------------------------------------------------------
# heat labels
# ---------------------------------------------------------------------------

def assign_heat(facts):
    """Qualitative, rule-based — see topic_engine/README.md for the
    reasoning behind each tier. Uses the same facts as ranking (source
    count, freshness, significance), not the numeric score itself."""
    sources = facts["unique_source_count"]
    fresh = facts["freshness_tier"]
    significant = facts["has_government_source"] or bool(facts["matched_watch_terms"])

    if sources >= 3 and fresh == "today":
        return "RED HOT"
    if sources >= 2 and fresh in ("today", "this_week"):
        return "HOT"
    if significant and fresh in ("today", "this_week"):
        return "HOT"
    if fresh in ("today", "this_week") or sources >= 2 or significant:
        return "WARM"
    return "WATCH"


def trend_from_heat(heat):
    if heat in ("RED HOT", "HOT"):
        return "up"
    if heat == "WARM":
        return "steady"
    return "down"


# ---------------------------------------------------------------------------
# explanations (deterministic templates, no LLM)
# ---------------------------------------------------------------------------

def build_why_trending(facts):
    n = facts["unique_source_count"]
    fresh = facts["freshness_tier"]
    terms = facts["matched_watch_terms"]
    total = facts["total_items"]
    fresh_word = "today" if fresh == "today" else ("this week" if fresh == "this_week" else None)

    if n >= 3:
        return f"Covered by {n} independent local sources" + (f" {fresh_word}." if fresh_word else ".")
    if n == 2:
        return f"Reported by {n} independent sources" + (f" {fresh_word}." if fresh_word else ", with continued coverage.")
    if facts["has_government_source"] and terms:
        return f"Official {facts['primary_source_name']} notice concerning {terms[0]}."
    if facts["has_government_source"]:
        return f"Official notice from {facts['primary_source_name']}."
    if terms:
        return f"Single-source coverage of a {terms[0]} story worth watching."
    if total > n:
        return f"Repeated coverage from {facts['primary_source_name']} ({total} items)."
    return f"Reported by {facts['primary_source_name']}."


def build_details(facts):
    names = ", ".join(facts["unique_source_names"])
    parts = [
        f"Supported by {facts['total_items']} item(s) from {facts['unique_source_count']} "
        f"source(s): {names}."
    ]
    if facts["matched_watch_terms"]:
        parts.append("Matches ongoing local watch topics: " + ", ".join(facts["matched_watch_terms"][:5]) + ".")
    if facts["has_government_source"]:
        parts.append("Includes an official local government source.")
    return " ".join(parts)


def build_related_keywords(facts):
    if facts["matched_watch_terms"]:
        return facts["matched_watch_terms"][:5]
    return sorted(significant_words(facts["latest_item"].get("title")))[:5]


def build_summary(facts):
    representative = facts["representative_item"]
    text = strip_boilerplate(representative.get("text")) or clean_text(representative.get("title") or "")
    if len(text) <= 260:
        return text
    return text[:260].rsplit(" ", 1)[0] + "…"


def latest_activity_label(facts):
    fresh = facts["freshness_tier"]
    if fresh == "today":
        return "today"
    if fresh == "this_week":
        return "this week"
    if fresh == "this_month":
        return "this month"
    return facts["latest_dt"].strftime("%Y-%m-%d") if facts["latest_dt"] else "unknown"


# ---------------------------------------------------------------------------
# assemble one topic's output row (matches the existing data.json contract)
# ---------------------------------------------------------------------------

def topic_to_output(facts, rank):
    heat = assign_heat(facts)
    return {
        "rank": rank,
        "title": clean_text(facts["latest_item"].get("title")),
        "summary": build_summary(facts),
        "mentions": facts["total_items"],
        "source_count": facts["unique_source_count"],
        "latest_activity": latest_activity_label(facts),
        "source_names": facts["unique_source_names"],
        "trend": trend_from_heat(heat),
        "first_detected": facts["earliest_dt"].strftime("%Y-%m-%d") if facts["earliest_dt"] else latest_activity_label(facts),
        "related_keywords": build_related_keywords(facts),
        "article_links": facts["article_links"][:8],
        "details": build_details(facts),
        # The two fields below are additive and backward-compatible: the GUI
        # template already guards why_trending with `is defined`, and heat
        # gets a small badge in the topic card header (see templates/index.html).
        "heat": heat,
        "why_trending": build_why_trending(facts),
    }


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

def run_pipeline_for_region(items, region):
    """Run the full discover -> cluster -> rank -> categorize -> Top 3
    pipeline for a single region's items. This is the entire region-support
    fix: the exact same clustering/ranking/heat/category logic runs for
    every region in config.REGIONS — only which items are in scope (region
    match + is_locally_relevant, which itself looks up per-region geography
    tables) differs. Nothing about grouping, scoring, or heat rules changes
    per region."""
    regional_items = [it for it in items if it.get("region") == region]
    relevant_items = [it for it in regional_items if is_locally_relevant(it)]
    relevant_items_sorted = sorted(
        relevant_items,
        key=lambda it: item_datetime(it) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    clusters = cluster_items(relevant_items_sorted)

    topics_by_category = {category: [] for category in config.CATEGORIES}
    for cluster in clusters:
        facts = build_topic_facts(cluster)
        category = assign_category(cluster)
        score = compute_score(facts)
        topics_by_category[category].append((score, facts, category))

    region_data = {}
    region_debug = []
    for category in config.CATEGORIES:
        ranked = sorted(topics_by_category[category], key=lambda triple: triple[0], reverse=True)
        top = ranked[: config.TOP_N_PER_CATEGORY]
        region_data[category] = [topic_to_output(facts, rank=i + 1) for i, (score, facts, _) in enumerate(top)]
        region_debug.extend((category, score, facts) for score, facts, _ in top)

    region_stats = {
        "regional_items": len(regional_items),
        "locally_relevant_items": len(relevant_items),
        "topics_discovered": len(clusters),
    }
    return region_data, region_debug, region_stats


def run_pipeline(items_path=None, sources_path=None):
    all_items = load_items(items_path)
    disabled_source_ids = load_disabled_source_ids(sources_path)
    items = [
        item
        for item in all_items
        if item.get("source_id") not in disabled_source_ids
    ]

    data = {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    selected_debug = []
    stats = {
        "items_read": len(all_items),
        "disabled_source_items": len(all_items) - len(items),
        "regional_items": 0,
        "locally_relevant_items": 0,
        "topics_discovered": 0,
        "by_region": {},
    }

    for region in config.REGIONS:
        region_data, region_debug, region_stats = run_pipeline_for_region(items, region)
        data[region] = region_data
        selected_debug.extend(region_debug)
        stats["by_region"][region] = region_stats
        stats["regional_items"] += region_stats["regional_items"]
        stats["locally_relevant_items"] += region_stats["locally_relevant_items"]
        stats["topics_discovered"] += region_stats["topics_discovered"]

    return data, selected_debug, stats


def write_data(data, path=None):
    """Write data.json. Writes to a temp file and renames over the target
    (atomic on POSIX) so a crash or exception mid-write can never leave
    data.json truncated — matters now that Refresh can trigger this write
    from a live server, where the existing dashboard must survive a failed
    run."""
    path = Path(path) if path is not None else DATA_PATH
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_summary(data, selected_debug, stats):
    print(f"items read from web_crawlers/items.json: {stats['items_read']}")
    print(f"items excluded from disabled sources: {stats['disabled_source_items']}")
    print()
    for region in config.REGIONS:
        region_stats = stats["by_region"][region]
        print(f"--- {region} ---")
        print(f"region items: {region_stats['regional_items']}")
        print(f"locally relevant items (after geography filter): {region_stats['locally_relevant_items']}")
        print(f"topics discovered (after grouping): {region_stats['topics_discovered']}")
        print()
        for category in config.CATEGORIES:
            topics = data[region][category]
            print(f"== {category} ({len(topics)} selected) ==")
            if not topics:
                print("  (no meaningful topics detected)")
            for topic in topics:
                print(
                    f"  #{topic['rank']} [{topic['heat']:8s}] {topic['title']} "
                    f"({topic['source_count']} source(s), {topic['mentions']} item(s))"
                )
            print()


def main():
    parser = argparse.ArgumentParser(description="VA_PODCAST_2 Topic Engine V1")
    parser.add_argument("--dry-run", action="store_true", help="Show selections without writing data.json")
    parser.add_argument("--items-path", default=None, help="Override path to items.json (mainly for testing)")
    args = parser.parse_args()

    data, selected_debug, stats = run_pipeline(args.items_path)
    print_summary(data, selected_debug, stats)

    if args.dry_run:
        print("--dry-run: data.json was NOT modified.")
        return

    write_data(data)
    print(f"Wrote {DATA_PATH}")


if __name__ == "__main__":
    main()
