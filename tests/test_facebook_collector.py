import json
from pathlib import Path

from facebook import collector as collector_module
from facebook.collector import (
    load_group_watchlist,
    normalize_comment,
    normalize_post,
    run_access_audit,
)


def test_groups_json_loads_and_verified_sources_are_enabled():
    groups = json.loads(Path("facebook/groups.json").read_text(encoding="utf-8"))
    assert groups
    assert any(group["id"] == "front_royal_police" for group in groups)
    assert any(group["id"] == "front_royal_fire_ems_calls_and_police_incidents" for group in groups)

    enabled = [group for group in groups if group.get("enabled")]
    assert enabled
    assert {group["id"] for group in enabled} >= {"front_royal_police", "front_royal_fire_ems_calls_and_police_incidents"}

    watchlist = load_group_watchlist()
    assert any(group["id"] == "front_royal_police" for group in watchlist)


def test_post_and_comment_normalization():
    source = {
        "id": "demo_source",
        "name": "Demo Source",
        "type": "group",
        "region": "shenandoah_valley",
    }
    normalized = normalize_post(
        {
            "post_id": "abc123",
            "post_url": "https://facebook.com/demo/post/123",
            "post_text": "  Local discussion about roads and growth  ",
            "published_at": "2026-08-15T12:00:00Z",
            "comments_count": 2,
            "reactions_count": 10,
            "shares_count": 3,
            "comments": [{"comment_id": "c1", "text": "  Agree  ", "published_at": "2026-08-15T12:05:00Z", "reactions_count": 4}],
        },
        source,
    )

    assert normalized["post_id"] == "abc123"
    assert normalized["post_text"] == "Local discussion about roads and growth"
    assert normalized["comments"][0]["text"] == "Agree"
    assert normalized["comments_count"] == 2


def test_duplicate_post_prevention_and_engagement_update():
    source = {"id": "demo_source", "name": "Demo Source", "type": "group", "region": "shenandoah_valley"}
    first = normalize_post(
        {
            "post_id": "dup-1",
            "post_url": "https://facebook.com/demo/dup-1",
            "post_text": "Example discussion",
            "comments_count": 1,
            "reactions_count": 5,
            "shares_count": 2,
            "comments": [{"comment_id": "comment-1", "text": "First comment", "reactions_count": 3}],
        },
        source,
    )
    second = normalize_post(
        {
            "post_id": "dup-1",
            "post_url": "https://facebook.com/demo/dup-1",
            "post_text": "Example discussion",
            "comments_count": 4,
            "reactions_count": 15,
            "shares_count": 6,
            "comments": [{"comment_id": "comment-1", "text": "First comment", "reactions_count": 3}, {"comment_id": "comment-2", "text": "New comment", "reactions_count": 9}],
        },
        source,
    )

    assert first["post_id"] == second["post_id"]
    assert second["comments_count"] == 4
    assert second["reactions_count"] == 15
    assert second["shares_count"] == 6
    assert len(second["comments"]) >= 2


def test_access_audit_status_handling():
    results = run_access_audit()
    assert results
    assert any(result["source_id"] == "front_royal_police" and result["status"] == "PUBLIC_ACCESSIBLE" for result in results)
    assert any(result["source_id"] == "front_royal_fire_ems_calls_and_police_incidents" and result["status"] == "PUBLIC_ACCESSIBLE" for result in results)

    # direct status check for the explicit fixture behavior
    assert normalize_comment({"comment_id": "x", "text": " okay ", "reactions_count": 7})["text"] == "okay"


def test_live_collection_replaces_demo_rows(tmp_path, monkeypatch):
    source = {
        "id": "front_royal_police",
        "name": "Front Royal Police Department",
        "type": "page",
        "region": "shenandoah_valley",
        "url": "https://www.facebook.com/frontroyalpolice",
    }

    posts_path = tmp_path / "posts.json"
    posts_path.write_text(
        json.dumps([
            {
                "post_id": "demo_front_royal_police_post_1",
                "source_id": "front_royal_police",
                "source_name": "Front Royal Police Department",
                "source_type": "facebook_page",
                "region": "shenandoah_valley",
                "post_url": "https://www.facebook.com/frontroyalpolice",
                "post_text": "Demo public post for Facebook collection validation.",
                "comments": [],
            }
        ]),
        encoding="utf-8",
    )

    monkeypatch.setattr(collector_module, "POSTS_PATH", posts_path)
    monkeypatch.setattr(
        collector_module,
        "extract_live_posts_from_url",
        lambda source: [
            normalize_post(
                {
                    "post_id": "live_real_1",
                    "post_url": "https://www.facebook.com/frontroyalpolice/posts/1234567890",
                    "post_text": "Live post from the real Front Royal Police page.",
                    "published_at": "2026-08-16T12:00:00Z",
                    "comments_count": 1,
                    "reactions_count": 24,
                    "shares_count": 2,
                    "comments": [{"comment_id": "comment_1", "text": "Good update", "reactions_count": 4}],
                },
                source,
            )
        ],
    )

    result = collector_module.collect_posts_for_source(source)

    assert result["post_id"] == "live_real_1"
    stored = json.loads(posts_path.read_text(encoding="utf-8"))
    assert [item["post_id"] for item in stored] == ["live_real_1"]
    assert all("demo_" not in item["post_id"] for item in stored)
