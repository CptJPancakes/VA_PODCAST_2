import json

from app import app


def test_app_starts_and_serves_homepage():
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "WHAT'S WORTH TALKING ABOUT TODAY?" in html
    assert "Timeline" in html
    assert "Shenandoah Valley" in html
    assert "Northern Virginia" in html
    assert "SHENANDOAH VALLEY" in html
    assert "NORTHERN VIRGINIA" in html


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
