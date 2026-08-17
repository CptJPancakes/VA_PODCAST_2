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
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
SOURCES_PATH = BASE_DIR / "sources.json"
ITEMS_PATH = BASE_DIR / "items.json"

WINDOW_HOURS = 24  # default collection window; widen with --window-hours for sparse sources
REQUEST_TIMEOUT = 20
MAX_ITEMS_PER_SOURCE = 40
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
    path = path if path is not None else ITEMS_PATH
    ordered = sorted(items, key=lambda item: item.get("published_at") or "", reverse=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(ordered, handle, ensure_ascii=False, indent=2)


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
    """Normalize a URL for identity: strip query string, fragment, trailing slash."""
    if not raw_url:
        return ""
    parts = urlsplit(raw_url.strip())
    path = parts.path.rstrip("/") or parts.path
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def stable_hash(value):
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16]


def make_item_id(source_id, url, title):
    identity = canonical_url(url) or title or ""
    return f"{source_id}:{stable_hash(identity)}"


def parse_datetime(raw_value):
    """Best-effort parse of RSS/Atom timestamps into aware UTC ISO strings."""
    if not raw_value:
        return None
    raw_value = raw_value.strip()
    try:
        dt = parsedate_to_datetime(raw_value)
    except (TypeError, ValueError):
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
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "author": author,
        "category_hint": category_hint,
    }


# ---------------------------------------------------------------------------
# collectors
# ---------------------------------------------------------------------------

def fetch_rss(source, window_hours):
    response = requests.get(source["url"], headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    root = ET.fromstring(response.content)

    items = []
    if root.tag.endswith("feed"):
        entries = root.findall(f"{ATOM_NS}entry")
        if not entries:
            entries = root.findall("entry")
        for entry in entries[:MAX_ITEMS_PER_SOURCE]:
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
        return items

    channel = root.find("channel")
    entries = channel.findall("item") if channel is not None else root.findall("item")
    for entry in entries[:MAX_ITEMS_PER_SOURCE]:
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
    for raw in payload[:MAX_ITEMS_PER_SOURCE]:
        title = raw.get("title") or raw.get("name") or ""
        url = raw.get("url") or raw.get("link") or ""
        text = raw.get("text") or raw.get("summary") or raw.get("description") or ""
        published_dt = parse_datetime(raw.get("published_at") or raw.get("date") or raw.get("pubDate"))
        if not within_window(published_dt, window_hours):
            continue
        items.append(build_item(source, title=title, url=url, text=text, published_dt=published_dt))
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

def merge_items(existing_items, new_items):
    """Upsert new_items into existing_items, keyed by item_id. One article/page
    exists only once; unchanged items are not duplicated, only collected_at moves."""
    index = {item["item_id"]: item for item in existing_items}
    added, refreshed = 0, 0
    for item in new_items:
        key = item["item_id"]
        prior = index.get(key)
        if prior is None:
            added += 1
        elif prior.get("title") != item.get("title") or prior.get("text") != item.get("text"):
            refreshed += 1
        index[key] = item
    return list(index.values()), added, refreshed


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------

def run_crawl(source_id=None, window_hours=WINDOW_HOURS):
    sources = load_sources()
    if source_id:
        sources = [s for s in sources if s["id"] == source_id]
        if not sources:
            print(f"No source with id={source_id!r} in sources.json")
            return

    existing_items = load_items()
    all_new_items = []
    attempted, succeeded, failed = 0, 0, 0

    for source in sources:
        if not source.get("enabled", False):
            print(f"{source['id']:38s} SKIPPED (disabled)")
            continue
        attempted += 1
        try:
            found = collect_source(source, window_hours)
            all_new_items.extend(found)
            succeeded += 1
            print(f"{source['id']:38s} SUCCESS  ({len(found)} items in window)")
        except Exception as exc:  # keep one bad source from stopping the whole crawl
            failed += 1
            print(f"{source['id']:38s} FAILED   ({exc})")

    merged_items, added, refreshed = merge_items(existing_items, all_new_items)
    save_items(merged_items)

    print()
    print(f"sources attempted: {attempted}, succeeded: {succeeded}, failed: {failed}")
    print(f"new items: {added}, refreshed items: {refreshed}, total items in store: {len(merged_items)}")


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
    args = parser.parse_args()

    if args.audit:
        run_audit()
        return

    run_crawl(source_id=args.source, window_hours=args.window_hours)


if __name__ == "__main__":
    main()
