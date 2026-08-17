import json
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
    load_sources,
    merge_items,
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

    run_crawl()

    stored = json.loads(items_path.read_text(encoding="utf-8"))
    assert stored == []


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

def test_canonical_url_strips_query_and_trailing_slash():
    assert canonical_url("https://Example.com/Story/?utm_source=rss") == "https://example.com/Story"
    assert canonical_url("https://example.com/story/") == "https://example.com/story"


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
    merged_twice, added2, refreshed2 = merge_items(merged_once, second_run)

    assert len(merged_twice) == 1  # still one record, not two
    assert added2 == 0
    assert refreshed2 == 0
    # collected_at should have moved forward even though nothing else changed
    assert merged_twice[0]["item_id"] == merged_once[0]["item_id"]


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
                    "region": "shenandoah_valley",
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

    run_crawl()

    stored = json.loads(items_path.read_text(encoding="utf-8"))
    assert len(stored) == 1
    assert stored[0]["title"] == "Real item"

    output = capsys.readouterr().out
    assert "bad_source" in output and "FAILED" in output
    assert "good_source" in output and "SUCCESS" in output
