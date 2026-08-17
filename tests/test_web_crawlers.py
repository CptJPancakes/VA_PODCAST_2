import json
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

from web_crawlers import crawler as crawler_module
from web_crawlers.crawler import (
    build_item,
    canonical_url,
    clean_text,
    fetch_html,
    fetch_rss,
    item_evidence_datetime,
    load_sources,
    merge_items,
    prune_items,
    run_crawl,
)


class FakeResponse:
    def __init__(self, content=b"", text="", json_data=None):
        self.content = content
        self.text = text
        self._json_data = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_data


@pytest.fixture(autouse=True)
def isolate_live_item_store(tmp_path, monkeypatch):
    """No test in this module may write the checked-in evidence store."""
    monkeypatch.setattr(crawler_module, "ITEMS_PATH", tmp_path / "items.json")


def _sample_rss_bytes():
    now = datetime.now(timezone.utc)
    recent = format_datetime(now - timedelta(hours=2))
    old = format_datetime(now - timedelta(days=400))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel>
  <title>Sample Feed</title>
  <item>
    <title>Town Council approves new zoning text amendment</title>
    <link>https://example.com/zoning-amendment/?utm_source=rss</link>
    <pubDate>{recent}</pubDate>
    <description>&lt;p&gt;The council voted 4-1 on the &lt;b&gt;amendment&lt;/b&gt;.&lt;/p&gt;</description>
    <category>Government</category>
    <dc:creator>Jane Reporter</dc:creator>
  </item>
  <item>
    <title>Old item outside the window</title>
    <link>https://example.com/old-item/</link>
    <pubDate>{old}</pubDate>
    <description>This item is far too old to be collected with a 24 hour window.</description>
  </item>
</channel>
</rss>
""".encode("utf-8")

SAMPLE_HTML = b"""
<html><body>
<article>
  <h2><a href="/local-news/first-story">Front Royal opens new park downtown</a></h2>
</article>
<article>
  <h2><a href="/local-news/first-story">Front Royal opens new park downtown</a></h2>
</article>
<h3><a href="https://example.org/second-story">Short</a></h3>
</body></html>
"""


# ---------------------------------------------------------------------------
# sources.json
# ---------------------------------------------------------------------------

def test_sources_json_loads_and_has_expected_shape():
    sources = load_sources()
    assert sources
    for source in sources:
        assert {"id", "name", "region", "source_type", "url", "collector_type", "enabled"} <= set(source.keys())
        assert source["collector_type"] in {"rss", "html", "json"}
        assert source["source_type"] in {"news", "government", "business", "events", "schools", "roads"}

    enabled_ids = {s["id"] for s in sources if s["enabled"]}
    assert "royal_examiner" in enabled_ids
    assert "warren_county_news" in enabled_ids

    disabled = [s for s in sources if not s["enabled"]]
    assert disabled
    assert all(s.get("note") for s in disabled)


def test_requested_shenandoah_valley_sources_are_registered_once():
    sources = load_sources()
    source_ids = [source["id"] for source in sources]
    requested_ids = {
        "whsv",
        "wmra_local_news",
        "harrisonburg_citizen",
        "rocktown_now",
        "augusta_free_press",
        "royal_examiner",
        "river953",
        "page_valley_news",
        "route_11_news",
        "news_gazette",
        "news29_shenandoah_valley",
        "wdbj7_local_news",
        "valley_today",
        "shenandoah_valley_partnership_news",
        "shenandoah_valley_investments",
        "virginia_business_shenandoah_valley",
        "top_of_virginia_chamber_news",
        "harrisonburg_rockingham_chamber_calendar",
        "virginia_realtors_market_reports",
        "blue_ridge_realtors_market_reports",
        "harrisonburg_rockingham_realtors",
        "greater_augusta_realtors_statistics",
        "central_shenandoah_housing_study",
        "whsv_agriculture",
        "virginia_agriculture_press_releases",
        "virginia_farm_bureau_newsroom",
        "alliance_shenandoah_valley_news",
        "shenandoah_valley_conservancy_news",
        "shenandoah_national_park_news",
        "visit_shenandoah_valley_events",
        "visit_harrisonburg_events",
        "visit_staunton_events",
        "winchester_frederick_events",
        "shenandoah_county_events",
        "luray_page_events",
        "wmra_community_calendar",
    }

    shenandoah_ids = {
        source["id"]
        for source in sources
        if source["region"] == "shenandoah_valley"
    }
    assert requested_ids <= shenandoah_ids
    assert len(source_ids) == len(set(source_ids))


def test_disabled_sources_are_skipped_during_crawl(tmp_path, monkeypatch):
    sources_path = tmp_path / "sources.json"
    items_path = tmp_path / "items.json"
    sources_path.write_text(
        json.dumps(
            [
                {
                    "id": "disabled_source",
                    "name": "Disabled Source",
                    "region": "shenandoah_valley",
                    "source_type": "news",
                    "url": "https://example.com/feed/",
                    "collector_type": "rss",
                    "enabled": False,
                    "note": "no clean path",
                }
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(crawler_module, "SOURCES_PATH", sources_path)
    monkeypatch.setattr(crawler_module, "ITEMS_PATH", items_path)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("collect_source should not be called for a disabled source")

    monkeypatch.setattr(crawler_module, "collect_source", fail_if_called)

    summary = run_crawl()

    assert not items_path.exists()
    assert summary == {
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "regions": {},
        "added": 0,
        "refreshed": 0,
        "pruned": 0,
        "total_items": 0,
        "saved": False,
    }


# ---------------------------------------------------------------------------
# RSS normalization
# ---------------------------------------------------------------------------

def test_rss_normalization(monkeypatch):
    source = {
        "id": "sample_rss",
        "name": "Sample Feed",
        "region": "shenandoah_valley",
        "source_type": "news",
        "url": "https://example.com/feed/",
        "collector_type": "rss",
        "enabled": True,
    }
    monkeypatch.setattr(crawler_module.requests, "get", lambda *a, **k: FakeResponse(content=_sample_rss_bytes()))

    items = fetch_rss(source, window_hours=24)

    assert len(items) == 1  # the old item falls outside the 24 hour window
    item = items[0]
    assert item["title"] == "Town Council approves new zoning text amendment"
    assert item["url"] == "https://example.com/zoning-amendment/?utm_source=rss"
    assert "amendment" in item["text"]
    assert "<b>" not in item["text"]
    assert item["author"] == "Jane Reporter"
    assert item["category_hint"] == "Government"
    assert item["source_id"] == "sample_rss"
    assert item["region"] == "shenandoah_valley"
    assert item["published_at"] is not None
    assert item["collected_at"] is not None
    assert item["first_seen_at"] == item["collected_at"]
    assert item["last_seen_at"] == item["first_seen_at"]


def test_rss_normalization_allows_whitespace_before_xml_declaration(monkeypatch):
    source = {
        "id": "sample_rss",
        "name": "Sample Feed",
        "region": "shenandoah_valley",
        "source_type": "news",
        "url": "https://example.com/feed/",
        "collector_type": "rss",
        "enabled": True,
    }
    content = b"\n  \n" + _sample_rss_bytes()
    monkeypatch.setattr(crawler_module.requests, "get", lambda *a, **k: FakeResponse(content=content))

    items = fetch_rss(source, window_hours=24)

    assert len(items) == 1
    assert items[0]["title"] == "Town Council approves new zoning text amendment"


def test_rss_filters_dates_before_applying_accepted_item_cap(monkeypatch):
    source = {
        "id": "deep_feed",
        "name": "Deep Feed",
        "region": "northern_virginia",
        "source_type": "news",
        "url": "https://example.com/feed",
        "collector_type": "rss",
        "enabled": True,
    }
    old_date = format_datetime(datetime.now(timezone.utc) - timedelta(days=30))
    recent_date = format_datetime(datetime.now(timezone.utc) - timedelta(hours=1))
    old_entries = "".join(
        f"<item><title>Old {index}</title><link>https://example.com/old/{index}</link>"
        f"<pubDate>{old_date}</pubDate></item>"
        for index in range(crawler_module.MAX_ITEMS_PER_SOURCE)
    )
    recent_entries = "".join(
        f"<item><title>Recent {index}</title><link>https://example.com/recent/{index}</link>"
        f"<pubDate>{recent_date}</pubDate></item>"
        for index in range(crawler_module.MAX_ITEMS_PER_SOURCE + 5)
    )
    content = f"<rss><channel>{old_entries}{recent_entries}</channel></rss>".encode()
    monkeypatch.setattr(
        crawler_module.requests,
        "get",
        lambda *a, **k: FakeResponse(content=content),
    )

    items = fetch_rss(source, window_hours=24)

    assert len(items) == crawler_module.MAX_ITEMS_PER_SOURCE
    assert [item["title"] for item in items] == [
        f"Recent {index}" for index in range(crawler_module.MAX_ITEMS_PER_SOURCE)
    ]


def test_atom_filters_dates_before_applying_accepted_item_cap(monkeypatch):
    source = {
        "id": "deep_atom_feed",
        "name": "Deep Atom Feed",
        "region": "shenandoah_valley",
        "source_type": "government",
        "url": "https://example.com/atom",
        "collector_type": "rss",
        "enabled": True,
    }
    old_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    recent_date = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    old_entries = "".join(
        f'<entry><title>Old {index}</title><link href="https://example.com/old/{index}" />'
        f"<published>{old_date}</published></entry>"
        for index in range(crawler_module.MAX_ITEMS_PER_SOURCE)
    )
    recent_entry = (
        '<entry><title>Recent meeting</title><link href="https://example.com/recent" />'
        f"<published>{recent_date}</published></entry>"
    )
    content = f'<feed xmlns="http://www.w3.org/2005/Atom">{old_entries}{recent_entry}</feed>'.encode()
    monkeypatch.setattr(
        crawler_module.requests,
        "get",
        lambda *a, **k: FakeResponse(content=content),
    )

    items = fetch_rss(source, window_hours=24)

    assert [item["title"] for item in items] == ["Recent meeting"]


# ---------------------------------------------------------------------------
# HTML normalization
# ---------------------------------------------------------------------------

def test_html_normalization(monkeypatch):
    source = {
        "id": "sample_html",
        "name": "Sample HTML Source",
        "region": "shenandoah_valley",
        "source_type": "business",
        "url": "https://example.org/news/",
        "collector_type": "html",
        "enabled": True,
    }
    monkeypatch.setattr(crawler_module.requests, "get", lambda *a, **k: FakeResponse(text=SAMPLE_HTML.decode()))

    items = fetch_html(source, window_hours=24)

    # the duplicated "first-story" anchor should only appear once
    urls = [item["url"] for item in items]
    assert urls.count("https://example.org/local-news/first-story") == 1
    assert "https://example.org/second-story" not in urls  # title "Short" is < 10 chars, filtered out
    assert items[0]["title"] == "Front Royal opens new park downtown"
    assert items[0]["published_at"] is None  # HTML fallback has no reliable date


# ---------------------------------------------------------------------------
# canonicalization / text cleanup
# ---------------------------------------------------------------------------

def test_canonical_url_strips_tracking_query_and_trailing_slash():
    assert canonical_url("https://Example.com/Story/?utm_source=rss") == "https://example.com/Story"
    assert canonical_url("https://example.com/story/") == "https://example.com/story"


def test_canonical_url_preserves_meaningful_query_parameters_deterministically():
    first = canonical_url(
        "https://Alexandria.Granicus.com/AgendaViewer.php?"
        "utm_source=rss&event_id=1720&clip_id=6928&fbclid=tracking#agenda"
    )
    reordered = canonical_url(
        "https://alexandria.granicus.com/AgendaViewer.php?clip_id=6928&event_id=1720"
    )

    assert first == (
        "https://alexandria.granicus.com/AgendaViewer.php?clip_id=6928&event_id=1720"
    )
    assert reordered == first


def test_granicus_meetings_with_different_query_ids_do_not_collapse():
    source = {
        "id": "alexandria_dockets",
        "name": "Alexandria Dockets",
        "region": "northern_virginia",
        "source_type": "government",
    }
    first = build_item(
        source,
        "Board of Architectural Review Public Hearing",
        "https://alexandria.granicus.com/AgendaViewer.php?view_id=57&event_id=1720",
        "First agenda",
        None,
    )
    second = build_item(
        source,
        "Planning Commission Public Hearing",
        "https://alexandria.granicus.com/AgendaViewer.php?event_id=1717&view_id=57",
        "Second agenda",
        None,
    )

    merged, added, refreshed = merge_items([], [first, second])

    assert first["item_id"] != second["item_id"]
    assert len(merged) == 2
    assert added == 2
    assert refreshed == 0


def test_clean_text_strips_html_and_collapses_whitespace():
    assert clean_text("<p>Hello   <b>world</b></p>\n") == "Hello world"
    assert clean_text(None) == ""


# ---------------------------------------------------------------------------
# deduplication
# ---------------------------------------------------------------------------

def test_deduplication_does_not_duplicate_unchanged_items():
    source = {"id": "src", "name": "Src", "region": "shenandoah_valley", "source_type": "news"}
    first_run = [build_item(source, title="Same title", url="https://example.com/a", text="body", published_dt=None)]
    merged_once, added, refreshed = merge_items([], first_run)
    assert len(merged_once) == 1
    assert added == 1
    assert refreshed == 0

    second_run = [build_item(source, title="Same title", url="https://example.com/a", text="body", published_dt=None)]
    original_first_seen = merged_once[0]["first_seen_at"]
    original_collected = merged_once[0]["collected_at"]
    second_last_seen = second_run[0]["last_seen_at"]
    merged_twice, added2, refreshed2 = merge_items(merged_once, second_run)

    assert len(merged_twice) == 1  # still one record, not two
    assert added2 == 0
    assert refreshed2 == 0
    assert merged_twice[0]["item_id"] == merged_once[0]["item_id"]
    assert merged_twice[0]["first_seen_at"] == original_first_seen
    assert merged_twice[0]["collected_at"] == original_collected
    assert merged_twice[0]["last_seen_at"] == second_last_seen


def test_deduplication_updates_changed_title_and_text():
    source = {"id": "src", "name": "Src", "region": "shenandoah_valley", "source_type": "news"}
    original = [build_item(source, title="Old title", url="https://example.com/a", text="old body", published_dt=None)]
    merged, _, _ = merge_items([], original)

    updated = [build_item(source, title="Updated title", url="https://example.com/a", text="new body", published_dt=None)]
    merged2, added, refreshed = merge_items(merged, updated)

    assert len(merged2) == 1
    assert added == 0
    assert refreshed == 1
    assert merged2[0]["title"] == "Updated title"


def test_upsert_preserves_original_dates_and_advances_only_last_seen():
    source = {"id": "src", "name": "Src", "region": "northern_virginia", "source_type": "news"}
    legacy_first_seen = "2026-07-01T12:00:00+00:00"
    existing = {
        "item_id": crawler_module.make_item_id("src", "https://example.com/a", "Old title"),
        "source_id": "src",
        "source_name": "Src",
        "source_type": "news",
        "region": "northern_virginia",
        "title": "Old title",
        "url": "https://example.com/a",
        "text": "old body",
        "published_at": None,
        "collected_at": legacy_first_seen,
    }
    incoming = build_item(
        source,
        title="Updated title",
        url="https://example.com/a?utm_source=feed",
        text="new body",
        published_dt=datetime(2026, 8, 17, 12, tzinfo=timezone.utc),
    )

    merged, added, refreshed = merge_items([existing], [incoming])

    assert added == 0
    assert refreshed == 1
    assert len(merged) == 1
    assert merged[0]["published_at"] is None
    assert merged[0]["first_seen_at"] == legacy_first_seen
    assert merged[0]["collected_at"] == legacy_first_seen
    assert merged[0]["last_seen_at"] == incoming["last_seen_at"]
    assert merged[0]["title"] == "Updated title"


def test_merge_deduplicates_equivalent_urls_within_one_batch():
    source = {"id": "src", "name": "Src", "region": "shenandoah_valley", "source_type": "news"}
    first = build_item(source, "Title", "https://example.com/story/?utm_source=a", "first", None)
    duplicate = build_item(source, "Title", "https://example.com/story?utm_source=b", "second", None)

    merged, added, refreshed = merge_items([], [first, duplicate])

    assert len(merged) == 1
    assert added == 1
    assert refreshed == 1
    assert merged[0]["text"] == "second"
    assert merged[0]["first_seen_at"] == first["first_seen_at"]


def test_retention_uses_published_then_first_seen_then_legacy_collected_at():
    now = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)

    def record(item_id, **timestamps):
        return {"item_id": item_id, **timestamps}

    items = [
        # An old publication is old even if it was first/last seen today.
        record(
            "old-published",
            published_at=(now - timedelta(days=15)).isoformat(),
            first_seen_at=now.isoformat(),
            last_seen_at=now.isoformat(),
        ),
        record(
            "recent-published",
            published_at=(now - timedelta(days=2)).isoformat(),
            first_seen_at=(now - timedelta(days=1)).isoformat(),
        ),
        # Undated evidence ages from its immutable first-seen timestamp.
        record(
            "old-undated",
            published_at=None,
            first_seen_at=(now - timedelta(days=15)).isoformat(),
            last_seen_at=now.isoformat(),
        ),
        record(
            "boundary",
            published_at=None,
            first_seen_at=(now - timedelta(days=14)).isoformat(),
        ),
        # Pre-migration records remain readable.
        record("legacy-recent", published_at=None, collected_at=(now - timedelta(days=3)).isoformat()),
        # Unknown age is retained instead of being destroyed speculatively.
        record("unknown-age", published_at=None),
    ]

    retained, pruned = prune_items(items, retention_days=14, now=now)

    assert {item["item_id"] for item in retained} == {
        "recent-published",
        "boundary",
        "legacy-recent",
        "unknown-age",
    }
    assert pruned == 2
    assert item_evidence_datetime(items[4]) == now - timedelta(days=3)


def test_retention_parses_fractional_iso_timestamps_written_by_crawler():
    timestamp = "2026-08-17T12:34:56.123456+00:00"
    assert item_evidence_datetime({"first_seen_at": timestamp}).isoformat() == timestamp


# ---------------------------------------------------------------------------
# concurrent crawl / progress reporting
# ---------------------------------------------------------------------------

def test_crawl_is_bounded_concurrent_reports_progress_and_merges_once_in_source_order(
    tmp_path, monkeypatch
):
    sources_path = tmp_path / "sources.json"
    items_path = tmp_path / "items.json"
    sources = [
        {
            "id": f"source_{index}",
            "name": f"Source {index}",
            "region": "shenandoah_valley" if index % 2 == 0 else "northern_virginia",
            "source_type": "news",
            "url": f"https://example.com/{index}/feed",
            "collector_type": "rss",
            "enabled": True,
        }
        for index in range(6)
    ]
    sources_path.write_text(json.dumps(sources), encoding="utf-8")
    monkeypatch.setattr(crawler_module, "SOURCES_PATH", sources_path)
    monkeypatch.setattr(crawler_module, "ITEMS_PATH", items_path)

    lock = threading.Lock()
    release = threading.Event()
    active = 0
    maximum_active = 0

    def fake_collect(source, window_hours):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            if maximum_active == 3:
                release.set()
        assert release.wait(timeout=2), "three workers never ran concurrently"
        # Invert completion timing so configured order and completion order differ.
        source_index = int(source["id"].split("_")[1])
        time.sleep((6 - source_index) * 0.003)
        with lock:
            active -= 1
        return [
            build_item(
                source,
                title=f"Story {source_index}",
                url=f"https://example.com/story/{source_index}",
                text="body",
                published_dt=datetime.now(timezone.utc),
            )
        ]

    save_calls = []

    def capture_save(items, path=None):
        save_calls.append(list(items))

    progress_events = []
    monkeypatch.setattr(crawler_module, "collect_source", fake_collect)
    monkeypatch.setattr(crawler_module, "save_items", capture_save)

    summary = run_crawl(progress_callback=progress_events.append, max_workers=3)

    assert maximum_active == 3
    assert len(save_calls) == 1
    assert [item["source_id"] for item in save_calls[0]] == [source["id"] for source in sources]
    assert [event["completed"] for event in progress_events] == [1, 2, 3, 4, 5, 6]
    assert {event["source_id"] for event in progress_events} == {source["id"] for source in sources}
    assert all(event["total"] == 6 for event in progress_events)
    assert all(event["status"] == "success" for event in progress_events)
    assert all(event["duration_seconds"] >= 0 for event in progress_events)
    assert summary["attempted"] == 6
    assert summary["succeeded"] == 6
    assert summary["failed"] == 0
    assert summary["saved"] is True
    assert summary["regions"] == {
        "shenandoah_valley": {"attempted": 3, "succeeded": 3, "failed": 0},
        "northern_virginia": {"attempted": 3, "succeeded": 3, "failed": 0},
    }


def test_progress_callback_failure_does_not_break_a_healthy_crawl(tmp_path, monkeypatch, capsys):
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps(
            [
                {
                    "id": "good",
                    "name": "Good",
                    "region": "shenandoah_valley",
                    "source_type": "news",
                    "url": "https://example.com/feed",
                    "collector_type": "rss",
                    "enabled": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(crawler_module, "SOURCES_PATH", sources_path)
    monkeypatch.setattr(crawler_module, "collect_source", lambda *args: [])

    def broken_callback(event):
        raise RuntimeError("UI disconnected")

    summary = run_crawl(progress_callback=broken_callback)

    assert summary["succeeded"] == 1
    assert summary["saved"] is True
    assert "progress callback failed: UI disconnected" in capsys.readouterr().out


def test_crawl_rejects_invalid_worker_limit(tmp_path, monkeypatch):
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps(
            [
                {
                    "id": "good",
                    "name": "Good",
                    "region": "shenandoah_valley",
                    "source_type": "news",
                    "url": "https://example.com/feed",
                    "collector_type": "rss",
                    "enabled": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(crawler_module, "SOURCES_PATH", sources_path)

    with pytest.raises(ValueError, match="positive integer"):
        run_crawl(max_workers=0)


# ---------------------------------------------------------------------------
# source failure isolation
# ---------------------------------------------------------------------------

def test_one_bad_source_does_not_stop_the_crawl(tmp_path, monkeypatch, capsys):
    sources_path = tmp_path / "sources.json"
    items_path = tmp_path / "items.json"
    sources_path.write_text(
        json.dumps(
            [
                {
                    "id": "good_source",
                    "name": "Good Source",
                    "region": "shenandoah_valley",
                    "source_type": "news",
                    "url": "https://example.com/good/feed/",
                    "collector_type": "rss",
                    "enabled": True,
                },
                {
                    "id": "bad_source",
                    "name": "Bad Source",
                    "region": "northern_virginia",
                    "source_type": "news",
                    "url": "https://example.com/bad/feed/",
                    "collector_type": "rss",
                    "enabled": True,
                },
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(crawler_module, "SOURCES_PATH", sources_path)
    monkeypatch.setattr(crawler_module, "ITEMS_PATH", items_path)

    good_source = {"id": "good_source", "name": "Good Source", "region": "shenandoah_valley", "source_type": "news"}

    def fake_collect(source, window_hours):
        if source["id"] == "bad_source":
            raise RuntimeError("simulated network failure")
        return [build_item(good_source, title="Real item", url="https://example.com/real", text="body", published_dt=None)]

    monkeypatch.setattr(crawler_module, "collect_source", fake_collect)

    progress_events = []
    summary = run_crawl(progress_callback=progress_events.append)

    stored = json.loads(items_path.read_text(encoding="utf-8"))
    assert len(stored) == 1
    assert stored[0]["title"] == "Real item"

    output = capsys.readouterr().out
    assert "bad_source" in output and "FAILED" in output
    assert "good_source" in output and "SUCCESS" in output
    assert summary["attempted"] == 2
    assert summary["succeeded"] == 1
    assert summary["failed"] == 1
    assert summary["saved"] is True
    assert summary["regions"] == {
        "shenandoah_valley": {"attempted": 1, "succeeded": 1, "failed": 0},
        "northern_virginia": {"attempted": 1, "succeeded": 0, "failed": 1},
    }
    assert {event["status"] for event in progress_events} == {"success", "failed"}
    failed_event = next(event for event in progress_events if event["status"] == "failed")
    assert failed_event["error"] == "simulated network failure"


def test_partial_success_prunes_expired_evidence_after_single_merge(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    sources_path = tmp_path / "sources.json"
    items_path = tmp_path / "items.json"
    sources = [
        {
            "id": "good_source",
            "name": "Good Source",
            "region": "shenandoah_valley",
            "source_type": "news",
            "url": "https://example.com/good/feed/",
            "collector_type": "rss",
            "enabled": True,
        },
        {
            "id": "bad_source",
            "name": "Bad Source",
            "region": "shenandoah_valley",
            "source_type": "news",
            "url": "https://example.com/bad/feed/",
            "collector_type": "rss",
            "enabled": True,
        },
    ]
    sources_path.write_text(json.dumps(sources), encoding="utf-8")
    expired = {
        "item_id": "legacy:expired",
        "published_at": None,
        "collected_at": (now - timedelta(days=30)).isoformat(),
        "title": "Expired",
    }
    items_path.write_text(json.dumps([expired]), encoding="utf-8")
    monkeypatch.setattr(crawler_module, "SOURCES_PATH", sources_path)
    monkeypatch.setattr(crawler_module, "ITEMS_PATH", items_path)

    def fake_collect(source, window_hours):
        if source["id"] == "bad_source":
            raise RuntimeError("offline")
        return [
            build_item(
                source,
                title="Fresh story",
                url="https://example.com/fresh",
                text="body",
                published_dt=now,
            )
        ]

    monkeypatch.setattr(crawler_module, "collect_source", fake_collect)

    summary = run_crawl()

    stored = json.loads(items_path.read_text(encoding="utf-8"))
    assert [item["title"] for item in stored] == ["Fresh story"]
    assert summary["added"] == 1
    assert summary["pruned"] == 1
    assert summary["succeeded"] == 1
    assert summary["failed"] == 1


@pytest.mark.parametrize("predicate_raises", [False, True])
def test_rejected_commit_preserves_store_byte_for_byte(
    predicate_raises, tmp_path, monkeypatch
):
    sources_path = tmp_path / "sources.json"
    items_path = tmp_path / "items.json"
    sources = [
        {
            "id": "good_source",
            "name": "Good Source",
            "region": "shenandoah_valley",
            "source_type": "news",
            "url": "https://example.com/good",
            "collector_type": "rss",
            "enabled": True,
        },
        {
            "id": "failed_region_source",
            "name": "Failed Region Source",
            "region": "northern_virginia",
            "source_type": "government",
            "url": "https://example.com/failed",
            "collector_type": "rss",
            "enabled": True,
        },
    ]
    sources_path.write_text(json.dumps(sources), encoding="utf-8")
    original = '[\n  {"item_id": "old:item", "collected_at": "2020-01-01T00:00:00+00:00"}\n]\n'
    items_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(crawler_module, "SOURCES_PATH", sources_path)
    monkeypatch.setattr(crawler_module, "ITEMS_PATH", items_path)

    def fake_collect(source, window_hours):
        if source["id"] == "failed_region_source":
            raise RuntimeError("offline")
        return [
            build_item(
                source,
                title="Fresh story",
                url="https://example.com/fresh",
                text="body",
                published_dt=datetime.now(timezone.utc),
            )
        ]

    predicate_calls = []

    def reject_commit(collection_summary):
        predicate_calls.append(collection_summary)
        if predicate_raises:
            raise RuntimeError("health policy crashed")
        return False

    monkeypatch.setattr(crawler_module, "collect_source", fake_collect)

    summary = run_crawl(commit_predicate=reject_commit)

    assert items_path.read_text(encoding="utf-8") == original
    assert predicate_calls == [
        {
            "attempted": 2,
            "succeeded": 1,
            "failed": 1,
            "regions": {
                "shenandoah_valley": {"attempted": 1, "succeeded": 1, "failed": 0},
                "northern_virginia": {"attempted": 1, "succeeded": 0, "failed": 1},
            },
        }
    ]
    assert "saved" not in predicate_calls[0]
    assert summary["saved"] is False
    assert summary["commit_rejected"] is True
    assert summary["added"] == 0
    assert summary["refreshed"] == 0
    assert summary["pruned"] == 0
    assert summary["total_items"] == 1


def test_all_source_failure_preserves_store_byte_for_byte_and_does_not_prune(
    tmp_path, monkeypatch
):
    sources_path = tmp_path / "sources.json"
    items_path = tmp_path / "items.json"
    sources = [
        {
            "id": "bad_one",
            "name": "Bad One",
            "region": "shenandoah_valley",
            "source_type": "news",
            "url": "https://example.com/one",
            "collector_type": "rss",
            "enabled": True,
        },
        {
            "id": "bad_two",
            "name": "Bad Two",
            "region": "northern_virginia",
            "source_type": "government",
            "url": "https://example.com/two",
            "collector_type": "html",
            "enabled": True,
        },
    ]
    sources_path.write_text(json.dumps(sources), encoding="utf-8")
    original = '[\n  {"item_id": "old:item", "collected_at": "2020-01-01T00:00:00+00:00"}\n]\n'
    items_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(crawler_module, "SOURCES_PATH", sources_path)
    monkeypatch.setattr(crawler_module, "ITEMS_PATH", items_path)
    monkeypatch.setattr(
        crawler_module,
        "collect_source",
        lambda *args: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    summary = run_crawl(max_workers=2)

    assert items_path.read_text(encoding="utf-8") == original
    assert summary["attempted"] == 2
    assert summary["succeeded"] == 0
    assert summary["failed"] == 2
    assert summary["added"] == 0
    assert summary["refreshed"] == 0
    assert summary["pruned"] == 0
    assert summary["total_items"] == 1
    assert summary["saved"] is False
    assert summary["regions"] == {
        "shenandoah_valley": {"attempted": 1, "succeeded": 0, "failed": 1},
        "northern_virginia": {"attempted": 1, "succeeded": 0, "failed": 1},
    }
