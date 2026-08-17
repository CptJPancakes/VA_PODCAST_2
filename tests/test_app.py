import json
import threading
import time

import pytest
import app as app_module
from app import app, format_updated_at


@pytest.fixture(autouse=True)
def reset_background_hunt_state():
    """Keep background-job state isolated without ever invoking a live crawl."""
    thread = app_module._hunt_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)
    with app_module._hunt_state_lock:
        app_module._hunt_state = app_module._idle_hunt_state()
        app_module._hunt_thread = None
    yield
    thread = app_module._hunt_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)


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
    assert "Go Hunting" in html
    assert "14 Days" in html
    assert 'role="dialog"' in html
    assert "View Updated Dashboard" in html


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
# Manual background Hunt V2
# ---------------------------------------------------------------------------


def _healthy_crawl_summary(attempted=4, succeeded=3):
    return {
        "attempted": attempted,
        "succeeded": succeeded,
        "failed": attempted - succeeded,
        "added": 1,
        "refreshed": 2,
        "total_items": 10,
        "saved": True,
        "regions": {
            "shenandoah_valley": {"attempted": 2, "succeeded": 1, "failed": 1},
            "northern_virginia": {
                "attempted": max(0, attempted - 2),
                "succeeded": max(1, succeeded - 1),
                "failed": max(0, attempted - succeeded - 1),
            },
        },
    }


def _dashboard_view(timeframe):
    return {
        "updated_at": "2026-01-01T00:00:00+00:00",
        "timeframe": timeframe,
        "shenandoah_valley": {},
        "northern_virginia": {},
    }


def _wait_for_status(expected, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = app_module.get_hunt_status()
        if status["status"] == expected:
            return status
        time.sleep(0.01)
    raise AssertionError(f"hunt never reached {expected}: {app_module.get_hunt_status()}")


def _install_successful_hunt(monkeypatch, call_order=None, written=None):
    call_order = call_order if call_order is not None else []
    written = written if written is not None else {}

    def fake_run_crawl(*, progress_callback, max_workers, commit_predicate):
        call_order.append("crawler")
        assert max_workers == 8
        progress_callback({
            "total": 4,
            "completed": 1,
            "attempted": 1,
            "succeeded": 1,
            "failed": 0,
            "current_source": {"id": "one", "name": "Source One", "region": "shenandoah_valley"},
        })
        summary = _healthy_crawl_summary()
        assert commit_predicate(summary) is True
        return summary

    pipeline_times = []

    def fake_run_pipeline(*, timeframe, now):
        call_order.append(f"engine:{timeframe}")
        pipeline_times.append(now)
        return _dashboard_view(timeframe), [], {"items_read": 10}

    def fake_write_data(data, path=None):
        call_order.append("write")
        written["data"] = data
        written["path"] = path

    monkeypatch.setattr(app_module.crawler_module, "run_crawl", fake_run_crawl)
    monkeypatch.setattr(app_module.engine_module, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(app_module.engine_module, "write_data", fake_write_data)
    written["pipeline_times"] = pipeline_times
    return call_order, written


def test_getting_page_or_status_never_starts_crawl(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.crawler_module, "run_crawl", lambda *a, **k: calls.append(True))

    client = app.test_client()
    assert client.get("/").status_code == 200
    status_response = client.get("/api/refresh/status")

    assert status_response.status_code == 200
    assert status_response.get_json()["status"] == "idle"
    assert calls == []


def test_post_starts_daemon_hunt_and_returns_202_before_crawl_finishes(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocking_crawl(**kwargs):
        started.set()
        release.wait(timeout=3)
        return _healthy_crawl_summary()

    monkeypatch.setattr(app_module.crawler_module, "run_crawl", blocking_crawl)
    monkeypatch.setattr(
        app_module.engine_module,
        "run_pipeline",
        lambda *, timeframe, now: (_dashboard_view(timeframe), [], {}),
    )
    monkeypatch.setattr(app_module.engine_module, "write_data", lambda *a, **k: None)

    response = app.test_client().post("/api/refresh")

    assert response.status_code == 202
    assert response.get_json()["success"] is True
    assert response.get_json()["status"] == "running"
    assert started.wait(timeout=1)
    assert app_module._hunt_thread.daemon is True
    release.set()
    assert _wait_for_status("succeeded")["phase"] == "complete"


def test_success_builds_all_timeframes_then_publishes_one_bundle(monkeypatch):
    call_order, written = _install_successful_hunt(monkeypatch)

    response = app.test_client().post("/api/refresh")
    assert response.status_code == 202
    status = _wait_for_status("succeeded")

    assert call_order == [
        "crawler",
        "engine:today",
        "engine:24h",
        "engine:3d",
        "engine:7d",
        "engine:14d",
        "write",
    ]
    assert written["path"] == app_module.DATA_PATH
    bundle = written["data"]
    assert bundle["schema_version"] == 2
    assert bundle["default_range"] == "today"
    assert set(bundle["timeframes"]) == set(app_module.TIMEFRAME_RANGES)
    assert all("updated_at" not in view for view in bundle["timeframes"].values())
    assert len(set(written["pipeline_times"])) == 1
    assert status["completed_timeframes"] == 5
    assert status["eta_seconds"] == 0
    assert status["dashboard_unchanged"] is False


def test_progress_callback_is_visible_through_status_endpoint(monkeypatch):
    progress_reported = threading.Event()
    release = threading.Event()

    def reporting_crawl(*, progress_callback, max_workers, commit_predicate):
        progress_callback({
            "total": 10,
            "completed": 4,
            "attempted": 4,
            "succeeded": 3,
            "failed": 1,
            "source_id": "council",
            "source_name": "Town Council",
            "region": "shenandoah_valley",
        })
        progress_reported.set()
        release.wait(timeout=3)
        summary = _healthy_crawl_summary()
        assert commit_predicate(summary) is True
        return summary

    monkeypatch.setattr(app_module.crawler_module, "run_crawl", reporting_crawl)
    monkeypatch.setattr(
        app_module.engine_module,
        "run_pipeline",
        lambda *, timeframe, now: (_dashboard_view(timeframe), [], {}),
    )
    monkeypatch.setattr(app_module.engine_module, "write_data", lambda *a, **k: None)

    app.test_client().post("/api/refresh")
    assert progress_reported.wait(timeout=1)
    status = app.test_client().get("/api/refresh/status").get_json()

    assert status["phase"] == "fetching_sources"
    assert status["completed"] == 4
    assert status["total"] == 10
    assert status["succeeded"] == 3
    assert status["failed"] == 1
    assert status["current_source"]["name"] == "Town Council"
    assert status["elapsed_seconds"] >= 0
    assert status["eta_seconds"] >= 0
    release.set()
    _wait_for_status("succeeded")


def test_second_post_gets_409_with_current_running_status(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_crawl(**kwargs):
        started.set()
        release.wait(timeout=3)
        return _healthy_crawl_summary()

    monkeypatch.setattr(app_module.crawler_module, "run_crawl", slow_crawl)
    monkeypatch.setattr(
        app_module.engine_module,
        "run_pipeline",
        lambda *, timeframe, now: (_dashboard_view(timeframe), [], {}),
    )
    monkeypatch.setattr(app_module.engine_module, "write_data", lambda *a, **k: None)

    first = app.test_client().post("/api/refresh")
    assert started.wait(timeout=1)
    second = app.test_client().post("/api/refresh")

    assert first.status_code == 202
    assert second.status_code == 409
    second_body = second.get_json()
    assert second_body["success"] is False
    assert second_body["status"] == "running"
    assert second_body["job_id"] == first.get_json()["job_id"]
    assert "already in progress" in second_body["error"]
    release.set()
    _wait_for_status("succeeded")


@pytest.mark.parametrize(
    ("summary", "expected_reason"),
    [
        (
            {
                **_healthy_crawl_summary(attempted=4, succeeded=1),
                "regions": {"shenandoah_valley": {"attempted": 4, "succeeded": 1, "failed": 3}},
            },
            "only 1 of 4 sources succeeded",
        ),
        (
            {
                **_healthy_crawl_summary(attempted=4, succeeded=3),
                "regions": {
                    "shenandoah_valley": {"attempted": 2, "succeeded": 2, "failed": 0},
                    "northern_virginia": {"attempted": 2, "succeeded": 0, "failed": 2},
                },
            },
            "no sources succeeded in Northern Virginia",
        ),
        (
            {"attempted": 0, "succeeded": 0, "failed": 0, "regions": {}, "saved": False},
            "no enabled sources were attempted",
        ),
    ],
)
def test_health_gate_fails_without_running_engine_or_writing(monkeypatch, summary, expected_reason):
    engine_calls = []
    write_calls = []
    commit_decisions = []

    def staged_crawl(**kwargs):
        commit_allowed = kwargs["commit_predicate"](summary)
        commit_decisions.append(commit_allowed)
        return {**summary, "saved": commit_allowed}

    monkeypatch.setattr(app_module.crawler_module, "run_crawl", staged_crawl)
    monkeypatch.setattr(app_module.engine_module, "run_pipeline", lambda **kwargs: engine_calls.append(kwargs))
    monkeypatch.setattr(app_module.engine_module, "write_data", lambda *a, **k: write_calls.append(True))

    response = app.test_client().post("/api/refresh")
    assert response.status_code == 202
    status = _wait_for_status("failed")

    assert expected_reason in status["error"]
    assert status["dashboard_unchanged"] is True
    assert commit_decisions == [False]
    assert engine_calls == []
    assert write_calls == []


def test_health_gate_accepts_exactly_half_when_each_region_succeeds():
    summary = {
        "attempted": 4,
        "succeeded": 2,
        "failed": 2,
        "saved": True,
        "regions": {
            "shenandoah_valley": {"attempted": 2, "succeeded": 1, "failed": 1},
            "northern_virginia": {"attempted": 2, "succeeded": 1, "failed": 1},
        },
    }
    assert app_module._source_health_error(summary) is None


def test_crawler_exception_is_reported_as_failed_without_engine(monkeypatch):
    engine_called = []

    def failing_crawl(**kwargs):
        raise RuntimeError("secret network details")

    monkeypatch.setattr(app_module.crawler_module, "run_crawl", failing_crawl)
    monkeypatch.setattr(app_module.engine_module, "run_pipeline", lambda **kwargs: engine_called.append(True))

    app.test_client().post("/api/refresh")
    status = _wait_for_status("failed")

    assert status["error"] == "The hunt could not be completed."
    assert "secret" not in json.dumps(status)
    assert engine_called == []


def test_engine_failure_never_calls_dashboard_writer(monkeypatch, tmp_path):
    old_dashboard = {"updated_at": "2026-01-01T00:00:00", "old": True}
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps(old_dashboard), encoding="utf-8")
    write_calls = []

    monkeypatch.setattr(app_module, "DATA_PATH", str(data_path))
    monkeypatch.setattr(app_module.crawler_module, "run_crawl", lambda **kwargs: _healthy_crawl_summary())

    def failing_pipeline(*, timeframe, now):
        if timeframe == "3d":
            raise RuntimeError("simulated engine failure")
        return _dashboard_view(timeframe), [], {}

    monkeypatch.setattr(app_module.engine_module, "run_pipeline", failing_pipeline)
    monkeypatch.setattr(app_module.engine_module, "write_data", lambda *a, **k: write_calls.append(True))

    app.test_client().post("/api/refresh")
    status = _wait_for_status("failed")

    assert status["dashboard_unchanged"] is True
    assert write_calls == []
    assert json.loads(data_path.read_text(encoding="utf-8")) == old_dashboard


# ---------------------------------------------------------------------------
# Timeframe bundle selection and backward compatibility
# ---------------------------------------------------------------------------


def _empty_regions(marker=None):
    result = {
        region_key: {
            category_key: []
            for category_key, _title in region["categories"]
        }
        for region_key, region in app_module.REGIONS.items()
    }
    if marker is not None:
        result["marker"] = marker
    return result


def test_api_data_selects_requested_bundle_view_and_common_timestamp(monkeypatch):
    bundle = {
        "schema_version": 2,
        "updated_at": "2026-08-17T12:00:00+00:00",
        "default_range": "today",
        "timeframes": {
            timeframe: _empty_regions(marker=timeframe)
            for timeframe in app_module.TIMEFRAME_RANGES
        },
    }
    monkeypatch.setattr(app_module, "load_data", lambda: bundle)

    response = app.test_client().get("/api/data?range=14d")
    data = response.get_json()

    assert response.status_code == 200
    assert data["marker"] == "14d"
    assert data["updated_at"] == bundle["updated_at"]
    assert "timeframes" not in data


def test_invalid_timeframe_falls_back_to_today_and_today_button_is_active(monkeypatch):
    bundle = {
        "updated_at": "2026-08-17T12:00:00+00:00",
        "default_range": "today",
        "timeframes": {"today": _empty_regions(marker="today")},
    }
    monkeypatch.setattr(app_module, "load_data", lambda: bundle)

    api_data = app.test_client().get("/api/data?range=forever").get_json()
    html = app.test_client().get("/?range=forever").get_data(as_text=True)

    assert api_data["marker"] == "today"
    assert 'class="timeframe active" type="button" data-range="today"' in html


def test_legacy_root_dashboard_works_for_every_range(monkeypatch):
    legacy = {"updated_at": "2026-08-17T12:00:00+00:00", **_empty_regions(marker="legacy")}
    monkeypatch.setattr(app_module, "load_data", lambda: legacy)

    assert app.test_client().get("/api/data?range=3d").get_json() == legacy
    html = app.test_client().get("/?range=3d").get_data(as_text=True)
    assert 'class="timeframe active" type="button" data-range="3d"' in html


def test_missing_requested_bundle_view_falls_back_to_declared_default(monkeypatch):
    bundle = {
        "updated_at": "2026-08-17T12:00:00+00:00",
        "default_range": "7d",
        "timeframes": {"7d": _empty_regions(marker="fallback")},
    }
    monkeypatch.setattr(app_module, "load_data", lambda: bundle)
    assert app.test_client().get("/api/data?range=14d").get_json()["marker"] == "fallback"


def test_civic_action_and_land_use_badges_render(monkeypatch):
    data = {"updated_at": "2026-08-17T12:00:00+00:00", **_empty_regions()}
    topic = {
        "rank": 1,
        "title": "Council approves rezoning",
        "summary": "A rezoning vote passed.",
        "mentions": 1,
        "source_count": 1,
        "latest_activity": "today",
        "source_names": ["Town Council"],
        "trend": "up",
        "first_detected": "2026-08-17",
        "related_keywords": ["rezoning"],
        "article_links": [],
        "details": "Official action.",
        "civic_action": "APPROVED",
        "land_use": True,
    }
    data["shenandoah_valley"]["real_estate"] = [topic]
    monkeypatch.setattr(app_module, "load_data", lambda: data)

    html = app.test_client().get("/").get_data(as_text=True)

    # The same story appears in Timeline and its region card.
    assert html.count('<span class="civic-badge">APPROVED</span>') == 2
    assert html.count('<span class="civic-badge">LAND USE</span>') == 2


def test_api_data_contract_remains_dashboard_shaped():
    response = app.test_client().get("/api/data")
    assert response.status_code == 200
    data = response.get_json()
    for region in ("shenandoah_valley", "northern_virginia"):
        assert set(data[region].keys()) == {
            "real_estate", "community", "business_economy", "money_finance",
            "food_lifestyle", "faith_family", "news_conversation",
        }
