"""
web_crawlers/crawler.py

Simple evidence-collection crawler for VA_PODCAST_2.

Job: hold an approved source list, fetch recent material, normalize it,
deduplicate it, and save it to web_crawlers/items.json.

This module does NOT rank, score, or interpret anything. It does not touch
data.json or the GUI. It only collects and normalizes raw evidence for the
future Topic Engine.

Usage:
    python web_crawlers/crawler.py                 # collect from all enabled sources
    python web_crawlers/crawler.py --source ID      # collect from one source
    python web_crawlers/crawler.py --audit          # check source health, save nothing
    python web_crawlers/crawler.py --window-hours 72  # widen the collection window
"""

import argparse
import hashlib
import json
import os
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
SOURCES_PATH = BASE_DIR / "sources.json"
ITEMS_PATH = BASE_DIR / "items.json"

WINDOW_HOURS = 14 * 24  # populate the complete active-evidence window on each crawl
RETENTION_DAYS = 14
DEFAULT_MAX_WORKERS = 8
REQUEST_TIMEOUT = 20
MAX_ITEMS_PER_SOURCE = 40
# Query parameters that identify an individual article, meeting, or document
# must remain part of its identity (for example Granicus ``event_id`` and
# ``clip_id``).  Remove only parameters whose purpose is known to be tracking.
TRACKING_QUERY_PARAMS = frozenset(
    {
        "_ga",
        "_gl",
        "dclid",
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "mkt_tok",
        "msclkid",
        "twclid",
        "yclid",
    }
)
# A plain browser-style UA is used because a couple of source sites run WAFs
# (e.g. Wordfence) that 403 self-identifying "compatible; ...bot..." strings
# even for public RSS feeds meant to be machine-read.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

ATOM_NS = "{http://www.w3.org/2005/Atom}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"


def find_first(element, *tags):
    """Element.find() elements are falsy when childless (ElementTree quirk), so
    `a or b` fallback chains silently pick the wrong branch. Use explicit
    `is not None` checks instead."""
    for tag in tags:
        found = element.find(tag)
        if found is not None:
            return found
    return None


# ---------------------------------------------------------------------------
# source list / item store
# ---------------------------------------------------------------------------

def load_sources(path=None):
    # `path` resolves against the module-level constant at *call* time (not at
    # def time) so tests can monkeypatch crawler_module.SOURCES_PATH safely.
    path = path if path is not None else SOURCES_PATH
    with open(path, "r", encoding="utf-8") as handle:
        sources = json.load(handle)
    if not isinstance(sources, list):
        raise ValueError("sources.json must contain a JSON list of source records")
    return sources


def load_items(path=None):
    path = path if path is not None else ITEMS_PATH
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as handle:
        items = json.load(handle)
    return items if isinstance(items, list) else []


def save_items(items, path=None):
    path = Path(path) if path is not None else ITEMS_PATH
    ordered = sorted(items, key=_item_sort_key, reverse=True)
    # Write to a temp file and rename over the target (atomic on POSIX) so a
    # crash or exception mid-write can never leave items.json truncated —
    # matters now that Refresh can trigger this write from a live server.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(ordered, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


# ---------------------------------------------------------------------------
# normalization helpers
# ---------------------------------------------------------------------------

def clean_text(value):
    """Strip HTML tags/entities and collapse whitespace."""
    if not value:
        return ""
    value = unescape(str(value))
    if "<" in value and ">" in value:
        value = BeautifulSoup(value, "html.parser").get_text(separator=" ")
    return " ".join(value.split())


def canonical_url(raw_url):
    """Normalize a URL for stable article identity.

    Fragments and known analytics parameters are non-identifying.  Other query
    parameters are preserved because many government systems put the actual
    meeting/document id in the query string.  Sorting the retained pairs makes
    equivalent URLs independent of parameter order.
    """
    if not raw_url:
        return ""
    parts = urlsplit(raw_url.strip())
    path = parts.path.rstrip("/") or parts.path
    query_pairs = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not name.casefold().startswith("utm_")
        and name.casefold() not in TRACKING_QUERY_PARAMS
    ]
    query = urlencode(sorted(query_pairs, key=lambda pair: (pair[0], pair[1])))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def stable_hash(value):
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16]


def make_item_id(source_id, url, title):
    identity = canonical_url(url) or title or ""
    return f"{source_id}:{stable_hash(identity)}"


def parse_datetime(raw_value):
    """Best-effort parse of RSS/Atom timestamps into aware UTC ISO strings."""
    if not raw_value:
        return None
    if isinstance(raw_value, datetime):
        dt = raw_value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    raw_value = str(raw_value).strip()
    try:
        dt = parsedate_to_datetime(raw_value)
    except (TypeError, ValueError, OverflowError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            dt = None
    if dt is None:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw_value, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def within_window(published_dt, window_hours):
    """Items without a parsable date are kept (better to collect than silently drop)."""
    if published_dt is None:
        return True
    age_hours = (datetime.now(timezone.utc) - published_dt).total_seconds() / 3600
    return age_hours <= window_hours


def build_item(source, title, url, text, published_dt, category_hint=None, author=None):
    seen_at = datetime.now(timezone.utc).isoformat()
    return {
        "item_id": make_item_id(source["id"], url, title),
        "source_id": source["id"],
        "source_name": source["name"],
        "source_type": source["source_type"],
        "region": source["region"],
        "title": clean_text(title),
        "url": (url or "").strip(),
        "text": clean_text(text),
        "published_at": published_dt.isoformat() if published_dt else None,
        # collected_at remains as an immutable compatibility timestamp for
        # consumers and records created before first_seen_at existed. New code
        # should use first_seen_at/last_seen_at directly.
        "collected_at": seen_at,
        "first_seen_at": seen_at,
        "last_seen_at": seen_at,
        "author": author,
        "category_hint": category_hint,
    }


# ---------------------------------------------------------------------------
# collectors
# ---------------------------------------------------------------------------

def fetch_rss(source, window_hours):
    response = requests.get(source["url"], headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    # A few otherwise-valid WordPress feeds emit blank lines before their
    # XML declaration. ElementTree rejects declarations that are not the
    # first bytes in the document, so tolerate leading whitespace here.
    root = ET.fromstring(response.content.lstrip())

    items = []
    if root.tag.endswith("feed"):
        entries = root.findall(f"{ATOM_NS}entry")
        if not entries:
            entries = root.findall("entry")
        for entry in entries:
            title_el = find_first(entry, f"{ATOM_NS}title", "title")
            link_el = find_first(entry, f"{ATOM_NS}link", "link")
            summary_el = find_first(entry, f"{ATOM_NS}summary", f"{ATOM_NS}content", "summary")
            date_el = find_first(entry, f"{ATOM_NS}published", f"{ATOM_NS}updated", "published", "updated")
            url = link_el.get("href") if link_el is not None else None
            published_dt = parse_datetime(date_el.text if date_el is not None else None)
            if not within_window(published_dt, window_hours):
                continue
            items.append(
                build_item(
                    source,
                    title=title_el.text if title_el is not None else "",
                    url=url,
                    text=summary_el.text if summary_el is not None else "",
                    published_dt=published_dt,
                )
            )
            if len(items) >= MAX_ITEMS_PER_SOURCE:
                break
        return items

    channel = root.find("channel")
    entries = channel.findall("item") if channel is not None else root.findall("item")
    for entry in entries:
        title_el = entry.find("title")
        link_el = entry.find("link")
        date_el = find_first(entry, "pubDate", "date")
        category_el = entry.find("category")
        creator_el = entry.find(f"{{http://purl.org/dc/elements/1.1/}}creator")
        content_el = entry.find(f"{CONTENT_NS}encoded")
        description_el = entry.find("description")

        text_source = content_el.text if content_el is not None and content_el.text else (
            description_el.text if description_el is not None else ""
        )
        published_dt = parse_datetime(date_el.text if date_el is not None else None)
        if not within_window(published_dt, window_hours):
            continue

        items.append(
            build_item(
                source,
                title=title_el.text if title_el is not None else "",
                url=link_el.text if link_el is not None else None,
                text=text_source,
                published_dt=published_dt,
                category_hint=category_el.text if category_el is not None else None,
                author=creator_el.text if creator_el is not None else None,
            )
        )
        if len(items) >= MAX_ITEMS_PER_SOURCE:
            break
    return items


def fetch_html(source, window_hours):
    """Generic fallback for sources with no feed. No reliable dates are assumed,
    so items are always kept (window filtering does not apply to undated HTML)."""
    response = requests.get(source["url"], headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    items = []
    seen_urls = set()
    for heading in soup.select("h1 a[href], h2 a[href], h3 a[href], article a[href]"):
        title = clean_text(heading.get_text())
        url = heading.get("href") or ""
        if not title or len(title) < 10 or not url:
            continue
        if url.startswith("/"):
            parts = urlsplit(source["url"])
            url = urlunsplit((parts.scheme, parts.netloc, url, "", ""))
        if url in seen_urls:
            continue
        seen_urls.add(url)
        items.append(build_item(source, title=title, url=url, text=title, published_dt=None))
        if len(items) >= MAX_ITEMS_PER_SOURCE:
            break
    return items


def fetch_json(source, window_hours):
    """Generic fallback for sources exposing a JSON list of articles/events.
    Looks for a top-level list, or a list under a common wrapper key."""
    response = requests.get(source["url"], headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()

    if isinstance(payload, dict):
        for key in ("items", "results", "data", "posts", "articles"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        return []

    items = []
    for raw in payload:
        title = raw.get("title") or raw.get("name") or ""
        url = raw.get("url") or raw.get("link") or ""
        text = raw.get("text") or raw.get("summary") or raw.get("description") or ""
        published_dt = parse_datetime(raw.get("published_at") or raw.get("date") or raw.get("pubDate"))
        if not within_window(published_dt, window_hours):
            continue
        items.append(build_item(source, title=title, url=url, text=text, published_dt=published_dt))
        if len(items) >= MAX_ITEMS_PER_SOURCE:
            break
    return items


COLLECTORS = {
    "rss": fetch_rss,
    "html": fetch_html,
    "json": fetch_json,
}


def collect_source(source, window_hours):
    collector = COLLECTORS.get(source.get("collector_type"))
    if collector is None:
        raise ValueError(f"Unknown collector_type: {source.get('collector_type')!r}")
    return collector(source, window_hours)


# ---------------------------------------------------------------------------
# merge / dedupe
# ---------------------------------------------------------------------------

def item_evidence_datetime(item):
    """Return the timestamp that determines an item's age.

    Publication time is authoritative. Undated evidence falls back to when it
    was first seen, then to the legacy collected_at field. last_seen_at is
    deliberately excluded so re-crawling an old undated page cannot make it
    fresh again.
    """
    for field in ("published_at", "first_seen_at", "collected_at"):
        parsed = parse_datetime(item.get(field))
        if parsed is not None:
            return parsed
    return None


def _item_sort_key(item):
    evidence_dt = item_evidence_datetime(item)
    timestamp = evidence_dt.isoformat() if evidence_dt is not None else ""
    return timestamp, item.get("item_id") or ""


def prune_items(items, retention_days=RETENTION_DAYS, now=None):
    """Keep active evidence inside the rolling retention window.

    Records with no usable timestamp are retained because their age cannot be
    established safely. The caller decides when pruning is appropriate; the
    live crawl only calls this after at least one source succeeds.
    """
    now = parse_datetime(now) if now is not None else datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    retained = []
    pruned = 0
    for item in items:
        evidence_dt = item_evidence_datetime(item)
        if evidence_dt is not None and evidence_dt < cutoff:
            pruned += 1
        else:
            retained.append(item)
    return retained, pruned


def merge_items(existing_items, new_items):
    """Upsert new_items into existing_items, keyed by item_id. One article/page
    exists only once. Original publication/first-seen timestamps are immutable;
    only last_seen_at advances when an item is encountered again."""
    index = {item["item_id"]: item for item in existing_items}
    added, refreshed = 0, 0
    for item in new_items:
        key = item["item_id"]
        prior = index.get(key)
        if prior is None:
            first_seen_at = item.get("first_seen_at") or item.get("collected_at")
            if not first_seen_at:
                first_seen_at = datetime.now(timezone.utc).isoformat()
            normalized = dict(item)
            normalized["first_seen_at"] = first_seen_at
            normalized["last_seen_at"] = (
                item.get("last_seen_at")
                or item.get("first_seen_at")
                or item.get("collected_at")
                or first_seen_at
            )
            # Keep collected_at readable and immutable for compatibility with
            # consumers that have not migrated to first_seen_at yet.
            normalized.setdefault("collected_at", first_seen_at)
            index[key] = normalized
            added += 1
            continue

        if prior.get("title") != item.get("title") or prior.get("text") != item.get("text"):
            refreshed += 1

        updated = {**prior, **item}
        # Preserve these even when the old value is None. A feed adding a date
        # later must not make an old, formerly-undated record appear newly fresh.
        updated["published_at"] = prior.get("published_at")
        updated["first_seen_at"] = (
            prior.get("first_seen_at")
            or prior.get("collected_at")
            or item.get("first_seen_at")
            or item.get("collected_at")
        )
        updated["last_seen_at"] = (
            item.get("last_seen_at")
            or item.get("first_seen_at")
            or item.get("collected_at")
            or datetime.now(timezone.utc).isoformat()
        )
        if "collected_at" in prior:
            updated["collected_at"] = prior.get("collected_at")
        else:
            # Do not introduce a newly-current compatibility timestamp into a
            # record that already uses the first_seen_at/last_seen_at schema.
            updated.pop("collected_at", None)
        index[key] = updated
    return list(index.values()), added, refreshed


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------

def _collect_source_timed(source, window_hours):
    started_at = time.monotonic()
    try:
        found = collect_source(source, window_hours)
        return found, None, time.monotonic() - started_at
    except Exception as exc:  # returned to preserve per-source isolation
        return [], exc, time.monotonic() - started_at


def _empty_summary(total_items=0):
    return {
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "regions": {},
        "added": 0,
        "refreshed": 0,
        "pruned": 0,
        "total_items": total_items,
        "saved": False,
    }


def run_crawl(
    source_id=None,
    window_hours=WINDOW_HOURS,
    progress_callback=None,
    max_workers=DEFAULT_MAX_WORKERS,
    commit_predicate=None,
):
    """Returns a small summary dict of what happened (also used by
    app.py's /api/refresh to log/report results) — callers that only care
    about the printed CLI output (main() below) can simply ignore it.

    Collection finishes entirely in memory before ``commit_predicate`` is
    consulted.  This lets the app apply its source-health gate before any
    merge, retention pruning, or replacement of the canonical item store.
    """
    sources = load_sources()
    existing_items = load_items()
    if source_id:
        sources = [s for s in sources if s["id"] == source_id]
        if not sources:
            print(f"No source with id={source_id!r} in sources.json")
            return _empty_summary(total_items=len(existing_items))

    enabled_sources = []
    for source in sources:
        if not source.get("enabled", False):
            print(f"{source['id']:38s} SKIPPED (disabled)")
            continue
        enabled_sources.append(source)

    attempted = len(enabled_sources)
    if attempted == 0:
        summary = _empty_summary(total_items=len(existing_items))
        print()
        print("sources attempted: 0, succeeded: 0, failed: 0")
        print(f"store unchanged: {len(existing_items)} items")
        return summary

    if not isinstance(max_workers, int) or isinstance(max_workers, bool) or max_workers < 1:
        raise ValueError("max_workers must be a positive integer")

    regions = {}
    for source in enabled_sources:
        stats = regions.setdefault(
            source["region"],
            {"attempted": 0, "succeeded": 0, "failed": 0},
        )
        stats["attempted"] += 1

    completed = succeeded = failed = 0
    results_by_index = {}

    worker_count = min(max_workers, attempted)
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="source-crawl") as executor:
        futures = {
            executor.submit(_collect_source_timed, source, window_hours): (index, source)
            for index, source in enumerate(enabled_sources)
        }
        for future in as_completed(futures):
            index, source = futures[future]
            try:
                found, error, duration_seconds = future.result()
            except Exception as exc:  # defensive: also isolate wrapper failures
                found, error, duration_seconds = [], exc, 0.0

            completed += 1
            if error is None:
                status = "success"
                succeeded += 1
                regions[source["region"]]["succeeded"] += 1
                results_by_index[index] = found
                print(f"{source['id']:38s} SUCCESS  ({len(found)} items in window)")
            else:
                status = "failed"
                failed += 1
                regions[source["region"]]["failed"] += 1
                print(f"{source['id']:38s} FAILED   ({error})")

            source_result = {
                "source_id": source["id"],
                "source_name": source["name"],
                "region": source["region"],
                "status": status,
                "items_found": len(found),
                "duration_seconds": round(duration_seconds, 3),
            }
            if error is not None:
                source_result["error"] = str(error)

            if progress_callback is not None:
                progress = {
                    "total": attempted,
                    "completed": completed,
                    "attempted": attempted,
                    "succeeded": succeeded,
                    "failed": failed,
                    "current_source": {
                        "id": source["id"],
                        "name": source["name"],
                        "region": source["region"],
                    },
                    **source_result,
                }
                try:
                    progress_callback(progress)
                except Exception as exc:
                    # A presentation/UI callback is observational and must not
                    # be able to turn a healthy source crawl into a failure.
                    print(f"progress callback failed: {exc}")

    collection_summary = {
        "attempted": attempted,
        "succeeded": succeeded,
        "failed": failed,
        "regions": regions,
    }
    commit_allowed = succeeded > 0
    commit_rejected = False
    if commit_predicate is not None:
        try:
            predicate_allowed = bool(commit_predicate(collection_summary))
        except Exception as exc:
            predicate_allowed = False
            print(f"commit predicate failed: {exc}")
        commit_allowed = commit_allowed and predicate_allowed
        commit_rejected = not predicate_allowed

    # Flatten in configured source order, not completion order. This keeps the
    # merge and final file deterministic while collection remains concurrent.
    all_new_items = []
    for index in range(attempted):
        all_new_items.extend(results_by_index.get(index, []))

    added = refreshed = pruned = 0
    saved = False
    merged_items = existing_items
    if commit_allowed:
        merged_items, added, refreshed = merge_items(existing_items, all_new_items)
        merged_items, pruned = prune_items(merged_items)
        save_items(merged_items)
        saved = True

    print()
    print(f"sources attempted: {attempted}, succeeded: {succeeded}, failed: {failed}")
    if saved:
        print(
            f"new items: {added}, refreshed items: {refreshed}, pruned items: {pruned}, "
            f"total items in store: {len(merged_items)}"
        )
    elif commit_rejected:
        print(f"commit rejected; store unchanged: {len(existing_items)} items")
    else:
        print(f"all sources failed; store unchanged: {len(existing_items)} items")

    summary = {
        "attempted": attempted,
        "succeeded": succeeded,
        "failed": failed,
        "regions": regions,
        "added": added,
        "refreshed": refreshed,
        "pruned": pruned,
        "total_items": len(merged_items),
        "saved": saved,
    }
    if commit_rejected:
        summary["commit_rejected"] = True
    return summary


def run_audit():
    sources = load_sources()
    for source in sources:
        if not source.get("enabled", False):
            reason = source.get("note", "no reason given")
            print(f"{source['name']:55s} DISABLED - {reason}")
            continue
        try:
            found = collect_source(source, window_hours=24 * 3650)  # audit: don't filter by recency
            label = source["collector_type"].upper()
            print(f"{source['name']:55s} {label} OK ({len(found)} items available)")
        except Exception as exc:
            label = source["collector_type"].upper()
            print(f"{source['name']:55s} {label} FAILED - {exc}")


def main():
    parser = argparse.ArgumentParser(description="VA_PODCAST_2 web evidence crawler")
    parser.add_argument("--source", help="Collect only this source id")
    parser.add_argument("--audit", action="store_true", help="Check source health, save nothing")
    parser.add_argument(
        "--window-hours",
        type=float,
        default=WINDOW_HOURS,
        help=f"Collection window in hours (default {WINDOW_HOURS})",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help=f"Maximum concurrent source fetches (default {DEFAULT_MAX_WORKERS})",
    )
    args = parser.parse_args()

    if args.audit:
        run_audit()
        return

    run_crawl(source_id=args.source, window_hours=args.window_hours, max_workers=args.max_workers)


if __name__ == "__main__":
    main()
