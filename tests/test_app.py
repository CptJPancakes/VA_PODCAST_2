import json
import threading

import app as app_module
from app import app, format_updated_at


def test_app_starts_and_serves_homepage():
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    # The masthead headline was intentionally emptied out (may be replaced
    # later); confirm the empty placeholder is there instead of old copy.
    assert "<h1></h1>" in html
    assert "WHAT'S WORTH TALKING ABOUT TODAY?" not in html
    assert "Timeline" in html
    assert "Shenandoah Valley" in html
    assert "Northern Virginia" in html
    assert "SHENANDOAH VALLEY" in html
    assert "NORTHERN VIRGINIA" in html


def test_updated_at_is_formatted_in_eastern_daylight_time():
    assert format_updated_at("2026-08-16T23:10:05+00:00") == (
        "Sunday, August 16, 2026, at 7:10:05 PM"
    )


def test_updated_at_is_formatted_in_eastern_standard_time():
    assert format_updated_at("2026-01-15T00:10:05+00:00") == (
        "Wednesday, January 14, 2026, at 7:10:05 PM"
    )


def test_updated_at_preserves_legacy_naive_eastern_timestamps():
    assert format_updated_at("2026-08-16T19:10:05") == (
        "Sunday, August 16, 2026, at 7:10:05 PM"
    )


def test_dashboard_renders_formatted_updated_at(monkeypatch):
    fixture_data = {
        "updated_at": "2026-08-16T23:10:05+00:00",
        **{
            region_key: {
                category_key: []
                for category_key, _category_title in region["categories"]
            }
            for region_key, region in app_module.REGIONS.items()
        },
    }
    monkeypatch.setattr(app_module, "load_data", lambda: fixture_data)

    response = app.test_client().get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Last Updated: Sunday, August 16, 2026, at 7:10:05 PM" in html
    assert "2026-08-16T23:10:05+00:00" not in html


def test_updated_at_falls_back_cleanly_for_missing_or_invalid_values():
    assert format_updated_at(None) == "Not available"
    assert format_updated_at("not-a-timestamp") == "Not available"


def test_data_json_loads_and_has_regions_and_categories():
    with open("data.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    assert "updated_at" in data
    assert "shenandoah_valley" in data
    assert "northern_virginia" in data

    for region in ("shenandoah_valley", "northern_virginia"):
        assert set(data[region].keys()) == {
            "real_estate",
            "community",
            "business_economy",
            "money_finance",
            "food_lifestyle",
            "faith_family",
            "news_conversation",
        }


def test_api_data_route_returns_json():
    client = app.test_client()
    response = client.get("/api/data")
    assert response.status_code == 200
    data = response.get_json()
    assert "updated_at" in data
    assert "shenandoah_valley" in data
    assert "northern_virginia" in data


def test_data_has_real_local_topics_without_demo_markers():
    with open("data.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    all_titles = []
    for region_key in ("shenandoah_valley", "northern_virginia"):
        for topics in data[region_key].values():
            all_titles.extend(item.get("title", "") for item in topics)

    joined = "\n".join(all_titles)
    assert "DEMO:" not in joined
    assert "example.com/demo" not in joined.lower()
    assert any("Warren" in title or "Front Royal" in title for title in all_titles)


# ---------------------------------------------------------------------------
# Real Refresh V1 — POST /api/refresh
# ---------------------------------------------------------------------------

def test_refresh_success_runs_crawler_then_topic_engine(monkeypatch):
    call_order = []
    fake_data = {"updated_at": "2026-01-01T00:00:00", "shenandoah_valley": {}, "northern_virginia": {}}
    written = {}

    def fake_run_crawl(*args, **kwargs):
        call_order.append("crawler")
        return {"attempted": 1, "succeeded": 1, "failed": 0, "added": 0, "refreshed": 0, "total_items": 1}

    def fake_run_pipeline(*args, **kwargs):
        call_order.append("engine")
        return fake_data, [], {"items_read": 1, "by_region": {}}

    def fake_write_data(data, path=None):
        call_order.append("write")
        written["data"] = data

    monkeypatch.setattr(app_module.crawler_module, "run_crawl", fake_run_crawl)
    monkeypatch.setattr(app_module.engine_module, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(app_module.engine_module, "write_data", fake_write_data)

    client = app.test_client()
    response = client.post("/api/refresh")

    assert response.status_code == 200
    assert response.get_json() == {"success": True}
    assert call_order == ["crawler", "engine", "write"]
    assert written["data"] == fake_data


def test_refresh_does_not_run_topic_engine_if_crawler_fails(monkeypatch):
    engine_called = []

    def failing_run_crawl(*args, **kwargs):
        raise RuntimeError("simulated network failure")

    def should_not_run(*args, **kwargs):
        engine_called.append(True)
        raise AssertionError("Topic Engine must not run after a crawler failure")

    monkeypatch.setattr(app_module.crawler_module, "run_crawl", failing_run_crawl)
    monkeypatch.setattr(app_module.engine_module, "run_pipeline", should_not_run)

    client = app.test_client()
    response = client.post("/api/refresh")

    assert response.status_code == 500
    body = response.get_json()
    assert body == {"success": False, "error": "Refresh failed"}
    assert "Traceback" not in response.get_data(as_text=True)
    assert engine_called == []


def test_refresh_topic_engine_failure_leaves_existing_data_json_untouched(monkeypatch, tmp_path):
    original_data_path = app_module.DATA_PATH
    fixture_data = {"updated_at": "2026-01-01T00:00:00", "shenandoah_valley": {}, "northern_virginia": {}}
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps(fixture_data), encoding="utf-8")

    write_called = []

    def fake_run_crawl(*args, **kwargs):
        return {"attempted": 1, "succeeded": 1, "failed": 0, "added": 0, "refreshed": 0, "total_items": 1}

    def failing_run_pipeline(*args, **kwargs):
        raise RuntimeError("simulated topic engine crash")

    def tracking_write_data(data, path=None):
        write_called.append(True)

    monkeypatch.setattr(app_module, "DATA_PATH", str(data_path))
    monkeypatch.setattr(app_module.crawler_module, "run_crawl", fake_run_crawl)
    monkeypatch.setattr(app_module.engine_module, "run_pipeline", failing_run_pipeline)
    monkeypatch.setattr(app_module.engine_module, "write_data", tracking_write_data)

    client = app.test_client()
    response = client.post("/api/refresh")

    assert response.status_code == 500
    assert response.get_json() == {"success": False, "error": "Refresh failed"}
    assert write_called == []  # data.json was never touched
    assert json.loads(data_path.read_text(encoding="utf-8")) == fixture_data

    monkeypatch.setattr(app_module, "DATA_PATH", original_data_path)


def test_refresh_lock_rejects_concurrent_requests(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_run_crawl(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        return {"attempted": 0, "succeeded": 0, "failed": 0, "added": 0, "refreshed": 0, "total_items": 0}

    def fake_run_pipeline(*args, **kwargs):
        return {"updated_at": "x", "shenandoah_valley": {}, "northern_virginia": {}}, [], {}

    monkeypatch.setattr(app_module.crawler_module, "run_crawl", slow_run_crawl)
    monkeypatch.setattr(app_module.engine_module, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(app_module.engine_module, "write_data", lambda *a, **k: None)

    first_response = {}

    def call_first():
        client = app.test_client()
        first_response["response"] = client.post("/api/refresh")

    first_thread = threading.Thread(target=call_first)
    first_thread.start()
    assert started.wait(timeout=5), "first refresh never started"

    # second request while the first is still holding the lock
    second_client = app.test_client()
    second_response = second_client.post("/api/refresh")

    assert second_response.status_code == 409
    assert second_response.get_json() == {"success": False, "error": "Refresh already in progress"}

    release.set()
    first_thread.join(timeout=5)
    assert first_response["response"].status_code == 200


def test_api_data_and_dashboard_contract_unaffected_by_refresh_wiring():
    # Real Refresh V1 must not change the existing /api/data contract.
    client = app.test_client()
    response = client.get("/api/data")
    assert response.status_code == 200
    data = response.get_json()
    for region in ("shenandoah_valley", "northern_virginia"):
        assert set(data[region].keys()) == {
            "real_estate", "community", "business_economy", "money_finance",
            "food_lifestyle", "faith_family", "news_conversation",
        }
