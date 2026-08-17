"""
topic_engine/engine.py

Topic Engine V2.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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

# Public, ordered interface shared with app.py and the dashboard tabs.
SUPPORTED_TIMEFRAMES = tuple(config.TIMEFRAMES)
EASTERN_TIMEZONE = ZoneInfo(config.EASTERN_TIMEZONE)


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
        # ``fromisoformat`` understands Z on current Python versions, while
        # the replacement also keeps this compatible with older runtimes.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def item_datetime(item):
    """Return the stable timestamp used for freshness and time filtering.

    Publication time is authoritative.  Undated evidence falls back to the
    first time it was observed, and only legacy records without either use
    ``collected_at``.  Deliberately never use ``last_seen_at``: seeing an old
    undated page again must not make it look newly published.
    """
    return (
        parse_iso(item.get("published_at"))
        or parse_iso(item.get("first_seen_at"))
        or parse_iso(item.get("collected_at"))
    )


def normalize_now(now=None):
    """Return one UTC-aware clock value for a complete deterministic run."""
    value = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise TypeError("now must be a datetime or None")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def get_timeframe_metadata():
    """Return UI-ready metadata in canonical tab order."""
    return [
        {"key": key, **metadata}
        for key, metadata in config.TIMEFRAMES.items()
    ]


def timeframe_cutoff(timeframe=config.DEFAULT_TIMEFRAME, now=None):
    """Return the inclusive UTC cutoff for a supported timeframe."""
    if timeframe not in config.TIMEFRAMES:
        supported = ", ".join(SUPPORTED_TIMEFRAMES)
        raise ValueError(f"unsupported timeframe {timeframe!r}; choose one of: {supported}")

    current = normalize_now(now)
    metadata = config.TIMEFRAMES[timeframe]
    if metadata["kind"] == "calendar_day":
        eastern_now = current.astimezone(EASTERN_TIMEZONE)
        return eastern_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return current - timedelta(hours=metadata["hours"])


def filter_items_by_timeframe(items, timeframe=config.DEFAULT_TIMEFRAME, now=None):
    """Filter raw evidence before clustering/ranking.

    Both boundaries are inclusive.  Future-dated records are omitted rather
    than allowed to dominate freshness, and records with no usable timestamp
    cannot safely be assigned to a time window.
    """
    current = normalize_now(now)
    cutoff = timeframe_cutoff(timeframe, current)
    return [
        item for item in items
        if (item_dt := item_datetime(item)) is not None and cutoff <= item_dt <= current
    ]


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
# approved primary-subject exclusions
# ---------------------------------------------------------------------------

_VEHICLE_STRIKE_RE = re.compile(
    r"\b(?:pedestrian|cyclist|bicyclist|bike rider|motorcyclist)\b.{0,35}"
    r"\b(?:struck|hit|killed|injured)\b|"
    r"\b(?:strikes|hits)\b.{0,35}\b(?:pedestrian|cyclist|bicyclist|motorcyclist)\b",
    re.IGNORECASE,
)
_SPORTS_SCORE_RE = re.compile(r"\b\d{1,3}\s*[-–]\s*\d{1,3}\b")
_PROPERTY_PRICE_RE = re.compile(
    r"(?:\$\s?\d[\d,.]*|\b\d[\d,.]*\s+dollars?\b).{0,45}"
    r"\b(?:home|house|condo|townhome|property|estate)\b",
    re.IGNORECASE,
)
_INDIVIDUAL_ARREST_RE = re.compile(
    r"\b(?:man|woman|teen|resident|driver|suspect|employee|teacher|coach|doctor|"
    r"deputy|officer|two|three|four|\d+)\b.{0,55}\b(?:arrested|charged with|indicted)\b|"
    r"\b(?:arrested|charged with|indicted)\b.{0,55}\b(?:man|woman|teen|resident|driver|suspect)\b",
    re.IGNORECASE,
)


def matches_any_term(text, terms):
    return any(contains_term(text, term) for term in terms)


def title_has_local_place(item):
    title = clean_text(item.get("title"))
    return matches_any_term(title, config.LOCAL_PLACE_TERMS_BY_REGION.get(item.get("region"), []))


def primary_subject_exclusion_reason(item):
    """Return a stable reason code when an approved discard rule applies.

    The title and publisher category are treated as primary-subject signals;
    incidental body mentions are not.  Exceptions preserve broader policy,
    civic, serious-warning, and infrastructure stories.  Returning a reason
    instead of only a boolean keeps refresh diagnostics auditable.
    """
    title = clean_text(item.get("title"))
    title_lower = title.lower()
    category_hint = clean_text(item.get("category_hint")).lower()

    if category_hint in config.EXCLUDED_CATEGORY_HINTS:
        return "sponsored_content"

    # Vehicle collision events, including collision-only traffic delays.
    # Broader safety/policy/design reporting remains eligible.
    if not matches_any_term(title, config.VEHICLE_INCIDENT_POLICY_TERMS):
        struck = _VEHICLE_STRIKE_RE.search(title) is not None
        explicit_vehicle_event = matches_any_term(
            title,
            ["hit-and-run", "hit and run", "dui crash", "car crash", "truck crash",
             "motorcycle crash", "vehicle crash", "traffic collision", "vehicle collision"],
        )
        generic_crash = matches_any_term(title, ["crash", "crashes", "collision", "wreck"])
        clearly_non_vehicle = matches_any_term(
            title,
            ["plane crash", "aircraft crash", "train crash", "rail crash", "market crash",
             "boat crash", "boating crash", "marine crash", "computer crash", "workplace accident",
             "industrial accident", "construction accident"],
        )
        vehicle_accident = contains_term(title, "accident") and matches_any_term(
            title, ["car", "truck", "motorcycle", "vehicle", "traffic", "road", "highway", "driver"]
        )
        if struck or explicit_vehicle_event or vehicle_accident or (generic_crash and not clearly_non_vehicle):
            return "vehicle_accident"

    # Routine blotters and individual arrest/charge items.  Public-official
    # and broader systemic/policy cases are retained because their local
    # civic significance can be materially different from a blotter item.
    if matches_any_term(title, ["police blotter", "crime blotter", "sheriff blotter", "arrest report"]):
        return "routine_crime"
    if (
        _INDIVIDUAL_ARREST_RE.search(title)
        and not matches_any_term(title, config.PUBLIC_OFFICIAL_TERMS)
        and not matches_any_term(title, config.BROADER_CRIME_SIGNIFICANCE_TERMS)
    ):
        return "individual_arrest"

    # Forecasts are discarded only when the title does not itself signal a
    # serious warning/watch/advisory or dangerous conditions.
    weather_category = "weather" in category_hint
    if (weather_category or matches_any_term(title, config.WEATHER_FORECAST_TERMS)) and not matches_any_term(
        title, config.SERIOUS_WEATHER_TERMS
    ):
        return "generic_weather_forecast"

    # Only score/recap-style sports posts are removed; facilities, funding,
    # team policy, and other community-impact sports reporting remains.
    sports_category = "sport" in category_hint
    sports_context = sports_category or matches_any_term(title, config.SPORTS_CONTEXT_TERMS)
    recap_signal = matches_any_term(title, config.SPORTS_RECAP_TERMS) or _SPORTS_SCORE_RE.search(title)
    if sports_context and recap_signal:
        return "sports_score_or_recap"

    if (
        "obituar" in category_hint
        or re.match(r"^\s*(?:obituary|in memoriam|remembering)\b", title, re.IGNORECASE)
        or re.search(r"\b(?:dies|died)\s+at\s+(?:age\s+)?\d{1,3}\b", title, re.IGNORECASE)
    ):
        return "obituary"

    # Routine closure/paving notices are noise; major failures, policy,
    # funding, safety projects, and council/commission action are preserved.
    if matches_any_term(title, config.ROADWORK_TERMS) and not matches_any_term(
        title, config.NON_ROUTINE_TRANSPORTATION_TERMS
    ):
        return "routine_roadwork"

    # Ordinary listings are distinct from development, housing policy, land
    # use, and market reporting.  Never classify a government-source item as
    # a listing based only on phrasing such as "land for sale."
    listing_category = category_hint in {
        "property listings", "real estate listings", "homes for sale", "open houses",
    }
    listing_title = matches_any_term(title, config.PROPERTY_LISTING_TERMS) or _PROPERTY_PRICE_RE.search(title)
    if (listing_category or listing_title) and item.get("source_type") != "government":
        return "property_listing"

    # Generic syndicated advice and recipes normally reveal themselves in
    # the headline/category.  A locally named how-to/service explainer is
    # retained; e.g. "How to comment on Front Royal's rezoning proposal."
    generic_category = category_hint in {
        "advice", "recipes", "recipe", "syndicated", "horoscope",
    }
    generic_title = matches_any_term(title, config.GENERIC_ADVICE_TERMS) or re.match(
        r"^\s*(?:how to|tips for|ways to)\b", title, re.IGNORECASE
    )
    if (generic_category or generic_title) and not title_has_local_place(item):
        return "generic_advice_or_recipe"

    return None


def is_primary_subject_excluded(item):
    return primary_subject_exclusion_reason(item) is not None


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

_UNCERTAIN_CIVIC_RESULT_RE = re.compile(
    r"\b(?:not|never|may|might|could|would|whether|expected|proposed)\b"
    r"(?:\s+[a-z]+){0,5}\s+"
    r"(?:passed|approved|adopted|denied|voted|deferred|postponed|tabled)\b",
    re.IGNORECASE,
)


def detect_civic_action(text):
    """Return (badge, action_bonus) for explicit council/board action text."""
    # Future vote language wins over historical action words that often
    # appear in the same story as background ("will vote on a plan the
    # commission previously approved").
    if matches_any_term(text, config.CIVIC_UPCOMING_VOTE_TERMS):
        return "UPCOMING VOTE", config.CIVIC_UPCOMING_ACTION_BONUS

    # Do not turn a negated, conditional, or merely expected result into a
    # factual badge ("has not yet been approved", "may be denied").
    asserted_text = _UNCERTAIN_CIVIC_RESULT_RE.sub(" ", text or "")
    for badge, terms in config.CIVIC_FINAL_ACTION_TERMS:
        if matches_any_term(asserted_text, terms):
            return badge, config.CIVIC_FINAL_ACTION_BONUS

    if matches_any_term(text, config.CIVIC_PUBLIC_HEARING_TERMS):
        return "PUBLIC HEARING", config.CIVIC_UPCOMING_ACTION_BONUS
    if matches_any_term(text, config.CIVIC_SCHEDULED_ACTION_TERMS):
        return "SCHEDULED ACTION", config.CIVIC_UPCOMING_ACTION_BONUS
    return None, 0


def detect_civic_signals(cluster):
    """Detect scoped civic actions and land-use decisions for one topic.

    Bonuses apply only when the topic names one of the accepted governing
    bodies.  The action displayed comes from the newest evidence item with
    an explicit signal, rather than mixing verbs across a cluster into a
    result no individual article actually reported.
    """
    civic_scope_text = " ".join(
        f"{analysis_text(item)} {item.get('source_name', '')} "
        f"{str(item.get('source_id') or '').replace('_', ' ')}"
        for item in cluster
    )
    matched_bodies = [
        term for term in config.CIVIC_BODY_TERMS
        if contains_term(civic_scope_text, term)
    ]
    if not matched_bodies:
        return {
            "civic_action": None,
            "land_use": False,
            "civic_body_terms": [],
            "civic_action_bonus": 0,
            "civic_land_use_bonus": 0,
            "civic_priority_bonus": 0,
        }

    land_use = matches_any_term(civic_scope_text, config.CIVIC_LAND_USE_TERMS)
    action, action_bonus = None, 0
    newest_first = sorted(
        cluster,
        key=lambda item: item_datetime(item) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for item in newest_first:
        action, action_bonus = detect_civic_action(analysis_text(item))
        if action:
            break

    land_bonus = config.CIVIC_LAND_USE_BONUS if land_use else 0
    civic_bonus = min(
        action_bonus + land_bonus,
        config.CIVIC_PRIORITY_BONUS_CAP,
    )
    if action is None and land_use:
        action = "LAND USE"

    return {
        "civic_action": action,
        "land_use": land_use,
        "civic_body_terms": matched_bodies,
        "civic_action_bonus": action_bonus,
        "civic_land_use_bonus": land_bonus,
        "civic_priority_bonus": civic_bonus,
    }


def compute_freshness_tier(latest_dt, now=None):
    if latest_dt is None:
        return "older"
    age_hours = (normalize_now(now) - latest_dt).total_seconds() / 3600
    if age_hours <= config.FRESH_TODAY_HOURS:
        return "today"
    if age_hours <= config.FRESH_THIS_WEEK_HOURS:
        return "this_week"
    if age_hours <= config.FRESH_THIS_MONTH_HOURS:
        return "this_month"
    return "older"


def build_topic_facts(cluster, now=None):
    current = normalize_now(now)
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

    civic_signals = detect_civic_signals(cluster)

    facts = {
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
        "freshness_tier": compute_freshness_tier(latest_dt, current),
        "article_links": article_links,
        "primary_source_name": latest_item.get("source_name"),
    }
    facts.update(civic_signals)
    return facts


# ---------------------------------------------------------------------------
# ranking (internal score used only to sort/select — never shown in the GUI)
# ---------------------------------------------------------------------------

def facebook_signal_for_topic(facts):
    """Extension point: Facebook is paused for V2, so this always
    contributes 0. Once facebook/posts.json has live data again, this is
    where post/comment/reaction counts for the same topic would be added
    into the score without changing anything else in the pipeline."""
    return 0


def compute_score_breakdown(facts):
    """Return every additive ranking component, including both subtotals.

    Keeping this as a public helper makes the normal ranking and the new
    capped civic boost independently testable and inspectable even though
    the numeric score is not displayed on topic cards.
    """
    diversity_bonus = (facts["unique_source_count"] - 1) * 3  # independent sources matter most
    repeat_same_source_bonus = min(facts["total_items"] - facts["unique_source_count"], 3) * 0.5  # capped, diminishing
    freshness_bonus = {"today": 3, "this_week": 2, "this_month": 1, "older": 0}[facts["freshness_tier"]]
    locality_bonus = 2 if facts["has_hyperlocal_source"] else 1  # already filtered to be locally relevant
    significance_bonus = 2 if (facts["has_government_source"] or facts["matched_watch_terms"]) else 0
    facebook_bonus = facebook_signal_for_topic(facts)
    civic_priority_bonus = min(
        facts.get("civic_priority_bonus", 0),
        config.CIVIC_PRIORITY_BONUS_CAP,
    )
    normal_score = (
        diversity_bonus
        + repeat_same_source_bonus
        + freshness_bonus
        + locality_bonus
        + significance_bonus
        + facebook_bonus
    )
    return {
        "source_diversity": diversity_bonus,
        "repeat_coverage": repeat_same_source_bonus,
        "freshness": freshness_bonus,
        "locality": locality_bonus,
        "significance": significance_bonus,
        "facebook": facebook_bonus,
        "normal_score": normal_score,
        "civic_priority": civic_priority_bonus,
        "total_score": normal_score + civic_priority_bonus,
    }


def compute_score(facts):
    return compute_score_breakdown(facts)["total_score"]


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
    if facts.get("civic_action") and facts["civic_action"] != "LAND USE":
        action = facts["civic_action"].lower().replace("voted", "formal vote")
        if facts.get("land_use"):
            return f"Official local land-use action: {action}."
        return f"Official local government action: {action}."
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
    if facts.get("civic_action"):
        parts.append(f"Civic priority: {facts['civic_action'].lower()}.")
    if facts.get("land_use"):
        parts.append("Concerns land use or development.")
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
        # Stable badge contract: the UI can check one nullable normalized
        # action and independently add a LAND USE badge when appropriate.
        "civic_action": facts.get("civic_action"),
        "land_use": bool(facts.get("land_use")),
    }


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

def run_pipeline_for_region(items, region, now=None):
    """Run the full discover -> cluster -> rank -> categorize -> Top 3
    pipeline for a single region's items. This is the entire region-support
    fix: the exact same clustering/ranking/heat/category logic runs for
    every region in config.REGIONS — only which items are in scope (region
    match + is_locally_relevant, which itself looks up per-region geography
    tables) differs. Nothing about grouping, scoring, or heat rules changes
    per region."""
    regional_items = [it for it in items if it.get("region") == region]
    relevant_items = [it for it in regional_items if is_locally_relevant(it)]
    eligible_items = []
    exclusions_by_reason = {}
    for item in relevant_items:
        reason = primary_subject_exclusion_reason(item)
        if reason:
            exclusions_by_reason[reason] = exclusions_by_reason.get(reason, 0) + 1
        else:
            eligible_items.append(item)
    relevant_items_sorted = sorted(
        eligible_items,
        key=lambda it: item_datetime(it) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    clusters = cluster_items(relevant_items_sorted)

    topics_by_category = {category: [] for category in config.CATEGORIES}
    for cluster in clusters:
        facts = build_topic_facts(cluster, now=now)
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
        "eligible_items": len(eligible_items),
        "excluded_items": len(relevant_items) - len(eligible_items),
        "exclusions_by_reason": exclusions_by_reason,
        "topics_discovered": len(clusters),
    }
    return region_data, region_debug, region_stats


def run_pipeline(
    items_path=None,
    sources_path=None,
    timeframe=config.DEFAULT_TIMEFRAME,
    now=None,
):
    """Build one timeframe's complete dashboard data.

    Time filtering happens once, before any regional relevance filtering,
    clustering, category assignment, or ranking.  Supplying ``now`` makes
    window boundaries, freshness, and ``updated_at`` deterministic.
    """
    current = normalize_now(now)
    all_items = load_items(items_path)
    disabled_source_ids = load_disabled_source_ids(sources_path)
    enabled_items = [
        item
        for item in all_items
        if item.get("source_id") not in disabled_source_ids
    ]
    items = filter_items_by_timeframe(enabled_items, timeframe=timeframe, now=current)

    data = {
        "updated_at": current.isoformat(timespec="seconds"),
        "timeframe": timeframe,
    }
    selected_debug = []
    stats = {
        "items_read": len(all_items),
        "disabled_source_items": len(all_items) - len(enabled_items),
        "timeframe": timeframe,
        "timeframe_items": len(items),
        "outside_timeframe_items": len(enabled_items) - len(items),
        "undated_items": sum(item_datetime(item) is None for item in enabled_items),
        "regional_items": 0,
        "locally_relevant_items": 0,
        "eligible_items": 0,
        "excluded_items": 0,
        "topics_discovered": 0,
        "by_region": {},
    }

    for region in config.REGIONS:
        region_data, region_debug, region_stats = run_pipeline_for_region(items, region, now=current)
        data[region] = region_data
        selected_debug.extend(region_debug)
        stats["by_region"][region] = region_stats
        stats["regional_items"] += region_stats["regional_items"]
        stats["locally_relevant_items"] += region_stats["locally_relevant_items"]
        stats["eligible_items"] += region_stats["eligible_items"]
        stats["excluded_items"] += region_stats["excluded_items"]
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
    print(
        f"items in {config.TIMEFRAMES[stats['timeframe']]['label']} timeframe: "
        f"{stats['timeframe_items']}"
    )
    print(f"items excluded by approved subject filters: {stats['excluded_items']}")
    print()
    for region in config.REGIONS:
        region_stats = stats["by_region"][region]
        print(f"--- {region} ---")
        print(f"region items: {region_stats['regional_items']}")
        print(f"locally relevant items (after geography filter): {region_stats['locally_relevant_items']}")
        print(f"eligible items (after subject filters): {region_stats['eligible_items']}")
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
    parser = argparse.ArgumentParser(description="VA_PODCAST_2 Topic Engine V2")
    parser.add_argument("--dry-run", action="store_true", help="Show selections without writing data.json")
    parser.add_argument("--items-path", default=None, help="Override path to items.json (mainly for testing)")
    parser.add_argument(
        "--timeframe",
        choices=SUPPORTED_TIMEFRAMES,
        default=config.DEFAULT_TIMEFRAME,
        help="Evidence window to filter before clustering/ranking (default: today)",
    )
    args = parser.parse_args()

    data, selected_debug, stats = run_pipeline(args.items_path, timeframe=args.timeframe)
    print_summary(data, selected_debug, stats)

    if args.dry_run:
        print("--dry-run: data.json was NOT modified.")
        return

    write_data(data)
    print(f"Wrote {DATA_PATH}")


if __name__ == "__main__":
    main()
