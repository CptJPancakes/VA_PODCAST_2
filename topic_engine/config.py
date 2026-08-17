"""
topic_engine/config.py

Centralized, readable constant tables for Topic Engine V1. Nothing in this
file is executable logic beyond simple lookups — it exists so the keyword
lists and thresholds that drive engine.py can be read and tuned in one
place without hunting through the pipeline code.
"""

# ---------------------------------------------------------------------------
# region / categories (must match app.py's REGIONS contract exactly)
# ---------------------------------------------------------------------------

# The engine runs the identical pipeline once per region below (same
# clustering/ranking/heat/category logic — only the per-region place-name
# and hyperlocal-source tables differ, see LOCAL_PLACE_TERMS_BY_REGION and
# HYPERLOCAL_SOURCE_IDS_BY_REGION). A region with no qualifying evidence
# simply comes out of the real pipeline with empty category lists — nothing
# is hardcoded to stay empty.
REGIONS = ["shenandoah_valley", "northern_virginia"]

CATEGORIES = [
    "real_estate",
    "community",
    "business_economy",
    "money_finance",
    "food_lifestyle",
    "faith_family",
    "news_conversation",
]

TOP_N_PER_CATEGORY = 3

# ---------------------------------------------------------------------------
# dashboard timeframes
# ---------------------------------------------------------------------------

# Kept as an insertion-ordered mapping so the API and GUI can share one
# canonical tab order.  ``today`` is a calendar day in Eastern Time; all
# other windows are rolling durations measured back from the supplied
# clock.  ``hours`` is intentionally None for today because DST can make an
# Eastern calendar day 23 or 25 hours long.
EASTERN_TIMEZONE = "America/New_York"
DEFAULT_TIMEFRAME = "today"
TIMEFRAMES = {
    "today": {"label": "Today", "kind": "calendar_day", "hours": None},
    "24h": {"label": "24 Hours", "kind": "rolling", "hours": 24},
    "3d": {"label": "3 Days", "kind": "rolling", "hours": 72},
    "7d": {"label": "7 Days", "kind": "rolling", "hours": 168},
    "14d": {"label": "14 Days", "kind": "rolling", "hours": 336},
}

# ---------------------------------------------------------------------------
# local relevance
# ---------------------------------------------------------------------------

# Items from these source_ids are hyperlocal by construction. They still
# pass through the normal relevance filter (which protects against
# syndicated filler), but count as a stronger locality signal once they do.
# Keyed by region since "hyperlocal" only means something relative to a
# specific geography.
HYPERLOCAL_SOURCE_IDS_BY_REGION = {
    "shenandoah_valley": {
        "royal_examiner",
        "river953",
        "downtown_front_royal",
        "nvdaily_front_royal",
        "wmra_local_news",
        "harrisonburg_citizen",
        "rocktown_now",
        "page_valley_news",
        "route_11_news",
        "news29_shenandoah_valley",
        "valley_today",
        "alliance_shenandoah_valley_news",
        "shenandoah_valley_conservancy_news",
    },
    "northern_virginia": {
        "arlnow",
        "alxnow",
        "ffxnow",
        "falls_church_news_press",
        "loudoun_now",
        "insidenova_arlington",
        "insidenova_fairfax",
        "insidenova_loudoun",
        "insidenova_prince_william",
    },
}

# Any item whose source_type is "government" is, by definition, the town's
# or county's own feed — always locally relevant regardless of wording.
# Region-agnostic on purpose: this is true for any jurisdiction.
ALWAYS_LOCAL_SOURCE_TYPES = {"government"}

# For everything else (e.g. WHSV's broader regional feed, or syndicated
# filler content that happens to run on a hyperlocal outlet), the item's
# title/text must mention the target geography to count as locally relevant.
# This is what keeps national/regional wire content and generic syndicated
# columns (recipes, career advice, travel pieces) from crowding out real
# local stories. Keyed by region — each region has its own geography.
LOCAL_PLACE_TERMS_BY_REGION = {
    "shenandoah_valley": [
        "front royal",
        "warren county",
        "shenandoah valley",
        "shenandoah river",
        "skyline drive",
        "blue ridge",
        "downtown front royal",
        "riverton",
        "bentonville",
        "browntown",
        "linden, va",
        "linden, virginia",
        # neighboring Shenandoah Valley localities the same hyperlocal
        # outlets cover, per "surrounding Shenandoah Valley where relevant"
        "shenandoah county",
        "clarke county",
        "strasburg",
        "woodstock, va",
        "woodstock virginia",
        "page county",
        "luray",
        "toms brook",
        "edinburg, va",
        "shenandoah national park",
        "harrisonburg",
        "rockingham county",
        "augusta county",
        "staunton",
        "waynesboro",
        "rockbridge county",
        "lexington, va",
        "lexington, virginia",
        "lexington virginia",
        "buena vista, va",
        "buena vista, virginia",
        "buena vista virginia",
        "frederick county, va",
        "frederick county, virginia",
        "middletown, va",
        "middletown, virginia",
        "middletown virginia",
        "winchester, va",
        "winchester, virginia",
        "winchester virginia",
    ],
    "northern_virginia": [
        # Arlington / Alexandria
        "arlington", "alexandria",
        # Fairfax area
        "fairfax county", "fairfax", "falls church", "vienna", "mclean",
        "tysons", "reston", "herndon", "chantilly", "centreville",
        "springfield", "burke",
        # Loudoun area
        "loudoun county", "leesburg", "ashburn", "sterling", "purcellville",
        # Prince William area
        "prince william county", "prince william", "manassas",
        "gainesville", "haymarket", "woodbridge",
    ],
}

# A hyperlocal outlet's own RSS <category> is a real, human-assigned signal
# — some of its posts (ribbon cuttings, chamber news, community events) are
# clearly local even though the article text never spells out "Front
# Royal"/"Warren County" (it assumes a local reader). Scoped per source_id
# since the taxonomy is specific to that site, not a generalization about
# what those words mean everywhere.
LOCAL_CATEGORY_HINTS = {
    "royal_examiner": {
        "Local News", "Chamber News", "Community Events", "Crime/Court", "Legal Notices",
    },
}

# Evidence Quality Cleanup V1 — Fix 3: category_hint values that mark
# sponsored/advertising/promotional publisher content. Excluded from
# locality (and therefore from ever becoming a topic candidate) regardless
# of source or place-name mentions — an ad is not evidence of what's
# actually being reported. Matched case-insensitively. The item stays in
# web_crawlers/items.json untouched; it's just never considered by the
# Topic Engine.
EXCLUDED_CATEGORY_HINTS = {
    "sponsored",
    "advertisement",
    "advertorial",
    "partner content",
    "paid content",
}

# ---------------------------------------------------------------------------
# approved primary-subject exclusions
# ---------------------------------------------------------------------------

# These tables drive a deliberately conservative classifier in engine.py.
# A word appearing incidentally in an article body is not enough: strong
# title/category evidence must show that the discarded material is the
# story's primary subject.
SERIOUS_WEATHER_TERMS = [
    "warning", "warnings", "watch", "watches", "advisory", "advisories",
    "alert", "alerts", "weather alert", "emergency", "severe", "tornado",
    "flash flood", "flooding", "hurricane", "tropical storm", "ice storm",
    "winter storm", "blizzard", "extreme heat", "dangerous heat",
]

WEATHER_FORECAST_TERMS = [
    "weather forecast", "forecast", "weather outlook", "weekend weather",
    "today's weather", "tomorrow's weather", "chance of rain",
]

SPORTS_CONTEXT_TERMS = [
    "sports", "football", "basketball", "baseball", "softball", "soccer",
    "lacrosse", "hockey", "volleyball", "wrestling", "game", "match",
    "tournament", "playoff", "championship",
]

SPORTS_RECAP_TERMS = [
    "recap", "final score", "game highlights", "defeats", "defeated",
    "beats", "beat", "falls to", "fell to", "tops", "edges", "shuts out",
    "wins over", "victory over",
]

ROADWORK_TERMS = [
    "roadwork", "road work", "paving", "milling", "resurfacing",
    "lane closure", "lane closures", "lanes closed", "road closure",
    "road closures", "traffic shift", "traffic shifts",
    "bridge maintenance", "utility work",
]

NON_ROUTINE_TRANSPORTATION_TERMS = [
    "bridge collapse", "infrastructure failure", "transit change",
    "safety plan", "safety improvements", "funding", "budget",
    "approved", "adopted", "public hearing", "town council",
    "city council", "board of supervisors", "planning commission",
]

PROPERTY_LISTING_TERMS = [
    "home of the week", "house of the week", "property of the week",
    "featured home", "just listed", "property listing", "real estate listing",
    "homes for sale", "house for sale", "condo for sale", "land for sale",
]

GENERIC_ADVICE_TERMS = [
    "ask amy", "dear abby", "hints from heloise", "horoscope", "recipe",
    "cooking tips", "career advice", "relationship advice",
]

PUBLIC_OFFICIAL_TERMS = [
    "mayor", "town manager", "city manager", "county administrator",
    "council member", "councilmember", "supervisor", "school board member",
    "public official", "police chief", "sheriff",
]

BROADER_CRIME_SIGNIFICANCE_TERMS = [
    "public corruption", "election fraud", "systemic", "policy", "reform",
    "crime trend", "crime statistics", "public safety plan",
]

VEHICLE_INCIDENT_POLICY_TERMS = [
    "safety plan", "safety improvements", "road design", "redesign",
    "vision zero", "policy", "legislation", "study", "crash data",
    "crash statistics", "dangerous road", "dangerous intersection",
    "infrastructure failure", "bridge collapse", "transit change",
]

# ---------------------------------------------------------------------------
# civic / land-use priority
# ---------------------------------------------------------------------------

CIVIC_BODY_TERMS = [
    "town council", "city council", "planning commission",
    "board of supervisors", "board of zoning appeals",
    "zoning appeals board", "county board", "bza",
]

CIVIC_LAND_USE_TERMS = [
    "land use", "land purchase", "land acquisition", "land sale", "land",
    "rezoning", "zoning", "development", "subdivision", "data center",
    "annexation", "easement", "housing project", "housing development",
    "planned unit development", "special use permit", "comprehensive plan",
]

# Decision-specific statuses are checked before generic VOTED.  Future vote
# language is handled separately in engine.py and checked first, preventing
# "will vote on a plan previously approved ..." from being mislabeled as a
# completed decision.
CIVIC_FINAL_ACTION_TERMS = [
    ("DENIED", ["denied", "denies"]),
    ("DEFERRED", ["deferred", "defers", "postponed", "tabled"]),
    ("ADOPTED", ["adopted", "adopts"]),
    ("APPROVED", ["approved", "approves"]),
    ("PASSED", ["passed", "passes"]),
    ("VOTED", ["voted"]),
]

CIVIC_UPCOMING_VOTE_TERMS = [
    "will vote", "to vote", "set to vote", "scheduled to vote",
    "vote scheduled", "scheduled vote", "upcoming vote",
]

CIVIC_PUBLIC_HEARING_TERMS = ["public hearing"]
CIVIC_SCHEDULED_ACTION_TERMS = [
    "scheduled action", "meeting agenda", "meeting agendas", "agenda", "agendas", "will consider",
    "scheduled to consider", "set to consider", "on the agenda",
]

CIVIC_FINAL_ACTION_BONUS = 5
CIVIC_UPCOMING_ACTION_BONUS = 4
CIVIC_LAND_USE_BONUS = 3
CIVIC_PRIORITY_BONUS_CAP = 8

# Evidence Quality Cleanup V1 — Fix 4: category_hint values that mark
# clearly generic, non-local-news content (movie/book reviews, etc). Found
# live: a Spider-Man movie review passed local relevance solely because it
# mentioned, once, which local theater it was playing at — a venue aside,
# not the story's subject. Items tagged with one of these hints only pass
# the generic place-term relevance check when the place name is in the
# TITLE (a strong signal the piece is actually about that place); a single
# incidental body mention is not enough for this content type. Government
# sources and the LOCAL_CATEGORY_HINTS allowlist above are unaffected —
# this only narrows the generic text-matching fallback.
GENERIC_CONTENT_HINTS = {
    "Arts & Entertainment",
}

# Evidence Quality Cleanup V1 — Fix 1: recurring roundup/briefing title
# templates. Found live: ARLnow, FFXnow, and ALXnow each publish their own
# "<Outlet> Daily Debrief for <date>" post daily — three different sister
# publications' own separate roundups, not the same underlying story — but
# the shared template wording ("daily", "debrief") was enough generic title
# overlap to falsely cluster them into one topic. Stripped before computing
# title-similarity for clustering only; if the actual remaining content
# words still overlap enough, normal clustering still merges them.
RECURRING_TITLE_PHRASES = [
    "daily debrief",
    "morning notes",
    "daily roundup",
    "morning roundup",
]

# Evidence Quality Cleanup V1 — Fix 2: "construction" is a real_estate
# keyword (legitimate for development/construction-project stories), but a
# construction-WORKER-fatality story is a public-safety story, not a real
# estate story. If any of these context phrases is present alongside
# "construction", the "construction" keyword does not count toward
# real_estate for that topic — every other real_estate keyword (and every
# other category's keywords) is unaffected.
CONTEXT_SENSITIVE_KEYWORDS = {
    "construction": [
        "worker dies", "worker died", "worker killed", "worker injured",
        "workplace accident", "construction accident", "job site accident",
    ],
}

# Generic auto-generated feed/site footers that add noise to relevance and
# category matching if left in (e.g. a WordPress "appeared first on X"
# footer, or a syndication CTA that happens to mention "Shenandoah Valley"
# on every single post regardless of the post's actual subject). Stripped
# before any keyword matching or display text is built. Case-insensitive,
# matched with re.sub.
BOILERPLATE_PATTERNS = [
    # WordPress themes vary in word order for this exact footer ("appeared
    # first on" vs "first appeared on") — found live on Northern Virginia
    # evidence: Falls Church News-Press uses the reversed order, which let
    # its own byline ("...Falls Church News-Press Online.") leak "Falls
    # Church" into an otherwise entirely national opinion column and make
    # it look locally relevant. Both orders are matched here.
    r"the post .*? (?:appeared first on|first appeared on) .*?\.",
    r"continue reading .*? at .*?\.",
    r"for more news from across the shenandoah valley,? click here\.?",
    r"for more information,? click here\.?",
    # Evidence Quality Cleanup V2 — Fix 2: publisher/byline attribution is
    # not story content. Found live: "A Penny for Your Thoughts" (an
    # entirely national political commentary column) passed Northern
    # Virginia local relevance solely because its byline read "...Exclusive
    # to the Falls Church News-Press..." — the publisher being local doesn't
    # make the story local. Bounded (max ~60 chars of publication name) so
    # this can never consume real article content beyond the attribution
    # clause itself.
    r"\bexclusive to the [a-z][a-z .'-]{0,60}?news-press\b",
    r"\bfor the [a-z][a-z .'-]{0,60}?news-press\b",
    r"\boriginally published by [a-z][a-z .'-]{0,60}\b",
    r"\bstaff report\b",
    r"\bcopyright \d{4}\b",
]

# ---------------------------------------------------------------------------
# topic grouping (title-similarity clustering)
# ---------------------------------------------------------------------------

# Words shorter than this are dropped before comparing titles (cuts noise
# like "a", "of", "to" without needing a huge stopword list).
MIN_SIGNIFICANT_WORD_LENGTH = 4

STOPWORDS = {
    "this", "that", "these", "those", "with", "from", "have", "will",
    "were", "been", "their", "about", "after", "before", "over", "under",
    "into", "than", "then", "when", "what", "which", "while", "where",
    "says", "said", "your", "their", "each", "more", "most", "some",
    "such", "only", "also", "just", "being", "amid", "near",
}

# Two items merge into one topic when the Jaccard overlap of their
# significant title words is at least this high, AND they share at least
# MIN_SHARED_TITLE_WORDS words. Both conditions exist so a couple of
# generic overlapping words (e.g. "warren county") on otherwise short
# titles can't force a false merge — a false merge is worse than leaving
# two related topics as separate cards.
TITLE_JACCARD_THRESHOLD = 0.34
MIN_SHARED_TITLE_WORDS = 2

# ---------------------------------------------------------------------------
# freshness buckets (hours since the topic's latest evidence item)
# ---------------------------------------------------------------------------

FRESH_TODAY_HOURS = 24
FRESH_THIS_WEEK_HOURS = 24 * 7
FRESH_THIS_MONTH_HOURS = 24 * 30
# anything older falls into "older"

# ---------------------------------------------------------------------------
# permanent watch priorities (supplemental signal, not the discovery engine)
# ---------------------------------------------------------------------------

# Matching one of these terms gives a topic a small, flat significance
# bonus, and lets a topic with only one source still be worth a Top 3 slot
# (a major Planning Commission action shouldn't lose to three unrelated
# lifestyle pieces just because only one outlet has covered it yet).
PERMANENT_WATCH_TERMS = [
    "data center",
    "zoning",
    "rezoning",
    "land use",
    "development",
    "construction",
    "planning commission",
    "town council",
    "board of supervisors",
    "school board",
    "public hearing",
    "public notice",
    "real estate",
    "new restaurant",
    "grand opening",
    "ribbon cutting",
    "opens",
    "closing",
    "closes",
    "ownership change",
    "community event",
]

# ---------------------------------------------------------------------------
# category assignment (simple keyword scoring, highest count wins)
# ---------------------------------------------------------------------------

# Order matters only as a tie-break when two categories score equally —
# earlier categories win ties, and "news_conversation" is last so it acts
# as the general-interest fallback when nothing else matches.
CATEGORY_KEYWORDS = {
    "real_estate": [
        "real estate", "housing", "mortgage rate", "zoning", "rezoning",
        "land use", "property value", "home sales", "homeownership",
        "rental", "apartment", "construction", "development",
        "subdivision", "data center", "planning commission",
        "board of zoning appeals", "property",
    ],
    "community": [
        "community", "neighborhood", "volunteer", "nonprofit", "naacp",
        "school board", "public school", "students", "town council",
        "board of supervisors", "meeting agenda", "public notice",
        "public hearing", "library", "parks and recreation", "sheriff",
        "police", "fire and rescue", "emergency", "festival", "fair",
        "ffa", "4-h", "humane society", "adoptable", "shelter",
    ],
    "business_economy": [
        "business", "economic development", "chamber of commerce",
        "entrepreneur", "grand opening", "ribbon cutting", "closing",
        "closed", "jobs", "employment", "tourism", "retail",
        "downtown front royal", "economic", "workforce", "industry",
        "warehouse", "manufacturing", "anniversary",
    ],
    "money_finance": [
        "tax", "taxes", "budget", "finance", "financial", "mortgage",
        "interest rate", "investment", "insurance", "credit",
        "retirement", "audit", "banking", "federal reserve",
    ],
    "food_lifestyle": [
        "restaurant", "winery", "brewery", "cafe", "food truck", "dining",
        "outdoor", "hiking", "recreation", "park", "trail",
        "skyline drive", "things to do", "farmers market", "recipe",
        "open house", "arts center", "cubs", "panda",
    ],
    "faith_family": [
        "church", "faith", "family", "parenting", "charity",
        "charitable", "youth", "ministry", "veteran", "scouting",
    ],
    "news_conversation": [
        "crime", "arrest", "investigation", "shooting", "court",
        "indictment", "accident", "weather", "alert", "opinion",
        "editorial", "politics", "election", "virginia", "lawsuit",
        "ruling", "traffic",
    ],
}

DEFAULT_CATEGORY = "news_conversation"
