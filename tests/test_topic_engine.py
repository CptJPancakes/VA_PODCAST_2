import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from topic_engine import config, engine


def iso(hours_ago=0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def make_item(item_id, source_id, source_name, source_type, title, text="", hours_ago=1,
              url=None, category_hint=None, region="shenandoah_valley"):
    return {
        "item_id": item_id,
        "source_id": source_id,
        "source_name": source_name,
        "source_type": source_type,
        "region": region,
        "title": title,
        "url": url or f"https://example.com/{item_id}",
        "text": text,
        "published_at": iso(hours_ago),
        "collected_at": iso(hours_ago),
        "author": None,
        "category_hint": category_hint,
    }


def write_items(tmp_path, items):
    path = tmp_path / "items.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    return path


GOV = "government"


# ---------------------------------------------------------------------------
# evidence loading
# ---------------------------------------------------------------------------

def test_load_items_from_fixture_file(tmp_path):
    items = [make_item("a", "front_royal_town_news", "Town of Front Royal", GOV, "Town Council votes on zoning")]
    path = write_items(tmp_path, items)
    loaded = engine.load_items(path)
    assert len(loaded) == 1
    assert loaded[0]["item_id"] == "a"


def test_load_items_missing_file_returns_empty_list(tmp_path):
    assert engine.load_items(tmp_path / "does_not_exist.json") == []


# ---------------------------------------------------------------------------
# one item = one mention
# ---------------------------------------------------------------------------

def test_one_item_equals_one_mention_regardless_of_keyword_frequency():
    text = "data center data center data center data center data center " * 4
    cluster = [make_item("a", "warren_county_news", "Warren County", GOV, "Data center zoning notice", text=text)]
    facts = engine.build_topic_facts(cluster)
    output = engine.topic_to_output(facts, rank=1)
    assert output["mentions"] == 1


# ---------------------------------------------------------------------------
# topic discovery: related items group, unrelated items stay separate
# ---------------------------------------------------------------------------

def test_related_items_from_different_sources_group_into_one_topic():
    items = [
        make_item("a", "river953", "The River 95.3", "news",
                   "Shenandoah County School appeals the court's ruling regarding school names",
                   text="Front Royal area schools react.", hours_ago=2),
        make_item("b", "whsv", "WHSV", "news",
                   "Shenandoah County School Board votes to appeal federal ruling over Confederate school names",
                   text="Front Royal area schools react.", hours_ago=3),
        make_item("c", "royal_examiner", "Royal Examiner", "news",
                   "Virginia School Board to Appeal Federal Court Order to Remove Confederate School Names",
                   text="Front Royal area schools react.", hours_ago=4),
    ]
    clusters = engine.cluster_items(items)
    assert len(clusters) == 1
    assert len(clusters[0]) == 3


def test_unrelated_topics_stay_separate_even_when_sharing_a_place_name():
    items = [
        make_item("a", "warren_county_news", "Warren County", GOV,
                   "Warren County Grand Jury Returns 15 Indictments"),
        make_item("b", "warren_county_news", "Warren County", GOV,
                   "Warren County Middle School FFA Earns National Gold Rating"),
    ]
    clusters = engine.cluster_items(items)
    assert len(clusters) == 2


# ---------------------------------------------------------------------------
# ranking: independent-source diversity beats repeated same-source coverage
# ---------------------------------------------------------------------------

def test_independent_source_diversity_outranks_repeated_same_source_coverage():
    diverse_cluster = [
        make_item("a", "royal_examiner", "Royal Examiner", "news", "Front Royal development plan advances", hours_ago=2),
        make_item("b", "river953", "The River 95.3", "news", "Front Royal development plan advances", hours_ago=3),
    ]
    repeated_cluster = [
        make_item("c", "royal_examiner", "Royal Examiner", "news", "Front Royal festival returns downtown", hours_ago=1),
        make_item("d", "royal_examiner", "Royal Examiner", "news", "Front Royal festival returns downtown", hours_ago=2),
        make_item("e", "royal_examiner", "Royal Examiner", "news", "Front Royal festival returns downtown", hours_ago=3),
        make_item("f", "royal_examiner", "Royal Examiner", "news", "Front Royal festival returns downtown", hours_ago=4),
    ]
    diverse_facts = engine.build_topic_facts(diverse_cluster)
    repeated_facts = engine.build_topic_facts(repeated_cluster)

    assert engine.compute_score(diverse_facts) > engine.compute_score(repeated_facts)


# ---------------------------------------------------------------------------
# local relevance
# ---------------------------------------------------------------------------

def test_local_evidence_beats_unrelated_regional_material(tmp_path):
    items = [
        make_item("local", "whsv", "WHSV", "news",
                   "Front Royal students head back to class this week",
                   text="Warren County Public Schools welcomed students back Monday."),
        make_item("national", "whsv", "WHSV", "news",
                   "Mass shooting investigation continues in Hartford",
                   text="Officers in Connecticut are investigating a shooting overnight."),
    ]
    assert engine.is_locally_relevant(items[0]) is True
    assert engine.is_locally_relevant(items[1]) is False

    path = write_items(tmp_path, items)
    data, _, stats = engine.run_pipeline(path, timeframe="14d")
    assert stats["locally_relevant_items"] == 1
    all_titles = [t["title"] for cat in data["shenandoah_valley"].values() for t in cat]
    assert any("back to class" in t for t in all_titles)
    assert not any("Hartford" in t for t in all_titles)


@pytest.mark.parametrize(
    "place_name",
    [
        "Harrisonburg",
        "Rockingham County",
        "Augusta County",
        "Staunton",
        "Waynesboro",
        "Rockbridge County",
        "Lexington, Virginia",
        "Buena Vista, Virginia",
        "Frederick County, Virginia",
        "Middletown, Virginia",
        "Shenandoah National Park",
    ],
)
def test_expanded_shenandoah_valley_places_are_locally_relevant(place_name):
    item = make_item(
        "local",
        "regional_source",
        "Regional Source",
        "news",
        f"{place_name} community update",
    )

    assert engine.is_locally_relevant(item) is True


def test_boilerplate_footer_does_not_create_false_local_relevance():
    item = make_item(
        "a", "river953", "The River 95.3", "news",
        "Manassas Civil War soldier remains prepared for Arlington National Cemetery",
        text=(
            "A Union soldier recovered at Manassas National Battlefield Park will be interred "
            "at Arlington National Cemetery. For more news from across the Shenandoah Valley, click here."
        ),
    )
    assert engine.is_locally_relevant(item) is False


# ---------------------------------------------------------------------------
# significant single-source civic items
# ---------------------------------------------------------------------------

def test_significant_single_source_government_item_can_reach_hot():
    cluster = [
        make_item("a", "front_royal_planning_commission", "Town of Front Royal - Planning Commission", GOV,
                   "Planning Commission Regular Meeting Agenda",
                   text="The commission will consider a zoning text amendment.", hours_ago=2)
    ]
    facts = engine.build_topic_facts(cluster)
    assert facts["unique_source_count"] == 1
    assert engine.assign_heat(facts) in ("HOT", "RED HOT")


def test_generic_single_source_item_without_significance_is_not_hot():
    cluster = [
        make_item("a", "royal_examiner", "Royal Examiner", "news",
                   "Butterflied Roast Chicken Delivers Crispy Skin",
                   text="A recipe for roast chicken with seasonal vegetables.", hours_ago=200 * 24)
    ]
    facts = engine.build_topic_facts(cluster)
    assert engine.assign_heat(facts) == "WATCH"


# ---------------------------------------------------------------------------
# category assignment
# ---------------------------------------------------------------------------

def test_category_assignment_picks_the_strongest_keyword_match():
    real_estate_cluster = [
        make_item("a", "front_royal_planning_commission", "Town of Front Royal - Planning Commission", GOV,
                   "Planning Commission reviews zoning and land use rezoning request",
                   text="Development proposal covers land use and property zoning changes.")
    ]
    finance_cluster = [
        make_item("b", "royal_examiner", "Royal Examiner", "news",
                   "County budget audit finds tax and finance issues",
                   text="The audit reviewed county budget, taxes, and financial planning.")
    ]
    assert engine.assign_category(real_estate_cluster) == "real_estate"
    assert engine.assign_category(finance_cluster) == "money_finance"


def test_category_assignment_defaults_to_news_conversation_when_nothing_matches():
    cluster = [make_item("a", "royal_examiner", "Royal Examiner", "news", "Xyzzy plugh frotz")]
    assert engine.assign_category(cluster) == config.DEFAULT_CATEGORY


# ---------------------------------------------------------------------------
# Top 3 / no filler
# ---------------------------------------------------------------------------

def test_top_3_maximum_per_category(tmp_path):
    # Five genuinely distinct subjects, each nudged (via a shared trailer
    # phrase) into the "community" category, so all 5 survive as separate
    # topics but only 3 may be selected.
    subjects = [
        ("Board of Supervisors approves new county budget", "roads"),
        ("Sheriff warns residents about a phone scam", "safety"),
        ("Library announces summer reading program", "books"),
        ("Volunteer fire crew completes rescue training", "training"),
        ("Parks department schedules trail maintenance closures", "trails"),
    ]
    items = []
    for i, (title, tag) in enumerate(subjects):
        text = f"{title}. This is a community update about {tag}. Board of Supervisors community item."
        items.append(
            make_item(f"item{i}", "front_royal_town_news", "Town of Front Royal - News Flash", GOV,
                       title, text=text, hours_ago=i)
        )
    path = write_items(tmp_path, items)
    data, _, stats = engine.run_pipeline(path, timeframe="14d")

    assert stats["topics_discovered"] == 5  # all five stayed distinct topics
    community = data["shenandoah_valley"]["community"]
    assert len(community) == config.TOP_N_PER_CATEGORY  # capped at 3, not 5
    assert [t["rank"] for t in community] == [1, 2, 3]


def test_fewer_than_3_allowed_no_fake_filler(tmp_path):
    items = [make_item("a", "front_royal_town_news", "Town of Front Royal - News Flash", GOV, "Single town notice")]
    path = write_items(tmp_path, items)
    data, _, _ = engine.run_pipeline(path, timeframe="14d")
    total_selected = sum(len(v) for v in data["shenandoah_valley"].values())
    assert total_selected == 1


# ---------------------------------------------------------------------------
# Covered By uses real supporting sources
# ---------------------------------------------------------------------------

def test_covered_by_fields_only_contain_real_supporting_sources(tmp_path):
    items = [
        make_item("a", "royal_examiner", "Royal Examiner", "news", "Front Royal downtown event draws crowd",
                   url="https://royalexaminer.com/a", hours_ago=1),
        make_item("b", "river953", "The River 95.3", "news", "Front Royal downtown event draws crowd",
                   url="https://theriver953.com/b", hours_ago=2),
    ]
    path = write_items(tmp_path, items)
    data, _, _ = engine.run_pipeline(path, timeframe="14d")
    topic = next(t for cat in data["shenandoah_valley"].values() for t in cat if t["mentions"] == 2)
    assert set(topic["source_names"]) == {"Royal Examiner", "The River 95.3"}
    assert set(topic["article_links"]) == {"https://royalexaminer.com/a", "https://theriver953.com/b"}
    assert topic["source_count"] == 2


# ---------------------------------------------------------------------------
# Northern Virginia stays empty
# ---------------------------------------------------------------------------

def test_northern_virginia_remains_empty_without_evidence(tmp_path):
    items = [make_item("a", "front_royal_town_news", "Town of Front Royal - News Flash", GOV, "Town notice")]
    path = write_items(tmp_path, items)
    data, _, _ = engine.run_pipeline(path, timeframe="14d")
    assert set(data["northern_virginia"].keys()) == set(config.CATEGORIES)
    assert all(data["northern_virginia"][cat] == [] for cat in config.CATEGORIES)


# ---------------------------------------------------------------------------
# Facebook absence does not break the engine
# ---------------------------------------------------------------------------

def test_engine_runs_fine_without_any_facebook_data(tmp_path):
    # engine.py never imports or opens anything under facebook/ — this is a
    # smoke test that the whole pipeline still completes normally, whether
    # or not facebook/posts.json exists. The Facebook signal hook always
    # contributes 0 without reading any file.
    assert engine.facebook_signal_for_topic({}) == 0

    items = [make_item("a", "front_royal_town_news", "Town of Front Royal - News Flash", GOV, "Town notice")]
    path = write_items(tmp_path, items)
    data, _, stats = engine.run_pipeline(path, timeframe="14d")
    assert stats["items_read"] == 1
    assert sum(len(v) for v in data["shenandoah_valley"].values()) == 1


def test_pipeline_excludes_evidence_from_explicitly_disabled_sources(tmp_path):
    items = [
        make_item(
            "disabled",
            "disabled_source",
            "Disabled Source",
            "news",
            "Front Royal stale archive item",
        ),
        make_item(
            "active",
            "active_source",
            "Active Source",
            "news",
            "Front Royal current local item",
        ),
    ]
    items_path = write_items(tmp_path, items)
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps(
            [
                {"id": "disabled_source", "enabled": False},
                {"id": "active_source", "enabled": True},
            ]
        ),
        encoding="utf-8",
    )

    data, _, stats = engine.run_pipeline(items_path, sources_path, timeframe="14d")

    titles = [
        topic["title"]
        for topics in data["shenandoah_valley"].values()
        for topic in topics
    ]
    assert stats["items_read"] == 2
    assert stats["disabled_source_items"] == 1
    assert any("current local item" in title for title in titles)
    assert not any("stale archive item" in title for title in titles)


# ---------------------------------------------------------------------------
# deterministic output
# ---------------------------------------------------------------------------

def test_output_is_deterministic_across_runs(tmp_path):
    items = [
        make_item("a", "royal_examiner", "Royal Examiner", "news", "Front Royal downtown event draws crowd", hours_ago=1),
        make_item("b", "river953", "The River 95.3", "news", "Front Royal downtown event draws crowd", hours_ago=2),
        make_item("c", "front_royal_town_news", "Town of Front Royal - News Flash", GOV, "Town notice about roads", hours_ago=1),
    ]
    path = write_items(tmp_path, items)
    data1, _, _ = engine.run_pipeline(path, timeframe="14d")
    data2, _, _ = engine.run_pipeline(path, timeframe="14d")
    data1.pop("updated_at")
    data2.pop("updated_at")
    assert data1 == data2


# ---------------------------------------------------------------------------
# existing GUI data contract remains valid
# ---------------------------------------------------------------------------

def test_output_matches_existing_gui_data_contract(tmp_path):
    items = [
        make_item("a", "royal_examiner", "Royal Examiner", "news", "Front Royal downtown event draws crowd", hours_ago=1),
        make_item("b", "front_royal_town_news", "Town of Front Royal - News Flash", GOV, "Town council notice", hours_ago=1),
    ]
    path = write_items(tmp_path, items)
    data, _, _ = engine.run_pipeline(path, timeframe="14d")

    assert set(data.keys()) == {"updated_at", "timeframe", "shenandoah_valley", "northern_virginia"}
    assert data["timeframe"] == "14d"
    assert datetime.fromisoformat(data["updated_at"]).tzinfo is not None
    for region in ("shenandoah_valley", "northern_virginia"):
        assert set(data[region].keys()) == set(config.CATEGORIES)

    required_fields = {
        "rank", "title", "summary", "mentions", "source_count", "latest_activity",
        "source_names", "trend", "first_detected", "related_keywords",
        "article_links", "details", "heat", "why_trending", "civic_action", "land_use",
    }
    any_topic = next(t for cat in data["shenandoah_valley"].values() for t in cat)
    assert required_fields <= set(any_topic.keys())
    assert any_topic["trend"] in ("up", "steady", "down")
    assert any_topic["heat"] in ("RED HOT", "HOT", "WARM", "WATCH")


# ---------------------------------------------------------------------------
# Northern Virginia: sources load, region tagging, cross-region isolation
# ---------------------------------------------------------------------------

def test_northern_virginia_sources_load_and_are_well_formed():
    sources = json.loads((Path("web_crawlers") / "sources.json").read_text(encoding="utf-8"))
    nova_sources = [s for s in sources if s["region"] == "northern_virginia"]
    assert nova_sources

    enabled_nova = [s for s in nova_sources if s["enabled"]]
    assert len(enabled_nova) >= 10
    for source in enabled_nova:
        assert source["collector_type"] in {"rss", "html", "json"}
        assert source["source_type"] in {"news", "government", "business", "events", "schools", "roads"}
        assert source["url"]

    # geographic spread: Arlington/Alexandria, Fairfax, Loudoun, and Prince
    # William should each have at least one enabled source
    enabled_ids = {s["id"] for s in enabled_nova}
    assert enabled_ids & {"arlnow", "insidenova_arlington"}
    assert enabled_ids & {"alxnow", "alexandria_dockets"}
    assert enabled_ids & {"ffxnow", "insidenova_fairfax"}
    assert enabled_ids & {"loudoun_now", "insidenova_loudoun", "loudoun_county_news"}
    assert enabled_ids & {"insidenova_prince_william", "prince_william_board_supervisors"}


def test_topic_engine_consumes_nova_evidence_without_breaking_shenandoah(tmp_path):
    items = [
        # Shenandoah evidence
        make_item("sv1", "royal_examiner", "Royal Examiner", "news",
                   "Front Royal downtown event draws crowd", hours_ago=1, region="shenandoah_valley"),
        make_item("sv2", "river953", "The River 95.3", "news",
                   "Front Royal downtown event draws crowd", hours_ago=2, region="shenandoah_valley"),
        # Northern Virginia evidence covering a different, unrelated subject
        make_item("nv1", "arlnow", "ARLnow", "news",
                   "Arlington County Board approves new zoning text amendment",
                   text="The Arlington County Board voted on a zoning amendment for Rosslyn.",
                   hours_ago=1, region="northern_virginia"),
        make_item("nv2", "insidenova_arlington", "InsideNoVa - Arlington section", "news",
                   "Arlington Board approves zoning text amendment for Rosslyn",
                   text="Arlington's zoning amendment passed unanimously.",
                   hours_ago=2, region="northern_virginia"),
    ]
    path = write_items(tmp_path, items)
    data, _, stats = engine.run_pipeline(path, timeframe="14d")

    assert stats["by_region"]["shenandoah_valley"]["regional_items"] == 2
    assert stats["by_region"]["northern_virginia"]["regional_items"] == 2

    sv_titles = [t["title"] for cat in data["shenandoah_valley"].values() for t in cat]
    nv_titles = [t["title"] for cat in data["northern_virginia"].values() for t in cat]
    assert any("Front Royal" in t for t in sv_titles)
    assert any("Arlington" in t or "Rosslyn" in t for t in nv_titles) or any(
        "zoning" in t.lower() for t in nv_titles
    )

    # the two Arlington items about the same zoning vote should merge into one topic
    nv_topic_with_two_sources = [
        t for cat in data["northern_virginia"].values() for t in cat if t["source_count"] == 2
    ]
    assert nv_topic_with_two_sources


def test_shenandoah_items_do_not_leak_into_northern_virginia_output(tmp_path):
    items = [
        make_item("sv1", "royal_examiner", "Royal Examiner", "news",
                   "Front Royal downtown event draws crowd", hours_ago=1, region="shenandoah_valley"),
    ]
    path = write_items(tmp_path, items)
    data, _, _ = engine.run_pipeline(path, timeframe="14d")
    nv_titles = [t["title"] for cat in data["northern_virginia"].values() for t in cat]
    assert "Front Royal downtown event draws crowd" not in nv_titles
    assert sum(len(v) for v in data["northern_virginia"].values()) == 0


def test_northern_virginia_items_do_not_leak_into_shenandoah_output(tmp_path):
    items = [
        make_item("nv1", "arlnow", "ARLnow", "news",
                   "Arlington restaurant opens downtown", hours_ago=1, region="northern_virginia"),
    ]
    path = write_items(tmp_path, items)
    data, _, _ = engine.run_pipeline(path, timeframe="14d")
    sv_titles = [t["title"] for cat in data["shenandoah_valley"].values() for t in cat]
    assert "Arlington restaurant opens downtown" not in sv_titles
    assert sum(len(v) for v in data["shenandoah_valley"].values()) == 0


def test_government_source_details_text_is_region_agnostic(tmp_path):
    items = [
        make_item("nv1", "loudoun_county_news", "Loudoun County - County News", GOV,
                   "Loudoun County announces new park land acquisition",
                   text="The county finalized a land purchase.", hours_ago=1, region="northern_virginia"),
    ]
    path = write_items(tmp_path, items)
    data, _, _ = engine.run_pipeline(path, timeframe="14d")
    topic = next(t for cat in data["northern_virginia"].values() for t in cat)
    assert "Front Royal" not in topic["details"]
    assert "Warren County" not in topic["details"]
    assert "official local government source" in topic["details"]


def test_northern_virginia_gui_data_contract_valid(tmp_path):
    items = [
        make_item("nv1", "alexandria_dockets", "City of Alexandria - City Dockets", GOV,
                   "Planning Commission Public Hearing", text="A hearing on a zoning matter.",
                   hours_ago=1, region="northern_virginia"),
    ]
    path = write_items(tmp_path, items)
    data, _, _ = engine.run_pipeline(path, timeframe="14d")
    required_fields = {
        "rank", "title", "summary", "mentions", "source_count", "latest_activity",
        "source_names", "trend", "first_detected", "related_keywords",
        "article_links", "details", "heat", "why_trending", "civic_action", "land_use",
    }
    topic = next(t for cat in data["northern_virginia"].values() for t in cat)
    assert required_fields <= set(topic.keys())
    assert topic["heat"] in ("RED HOT", "HOT", "WARM", "WATCH")


# ---------------------------------------------------------------------------
# Evidence Quality Cleanup V1
# ---------------------------------------------------------------------------

# --- Fix 1: recurring roundup/briefing titles must not false-merge ---------

def test_recurring_daily_debrief_posts_stay_separate():
    items = [
        make_item("a", "arlnow", "ARLnow", "news", "ARLnow Daily Debrief for Aug 14, 2026",
                   text="Arlington news roundup for today."),
        make_item("b", "ffxnow", "FFXnow", "news", "FFXnow Daily Debrief for Aug 14, 2026",
                   text="Fairfax news roundup for today."),
        make_item("c", "alxnow", "ALXnow", "news", "ALXnow Daily Debrief for Aug 14, 2026",
                   text="Alexandria news roundup for today."),
    ]
    clusters = engine.cluster_items(items)
    assert len(clusters) == 3  # three sister publications' own separate roundups, not one story


def test_recurring_morning_notes_posts_stay_separate():
    items = [
        make_item("a", "arlnow", "ARLnow", "news", "Morning Notes for August 14, 2026"),
        make_item("b", "alxnow", "ALXnow", "news", "Morning Notes for August 14, 2026"),
    ]
    clusters = engine.cluster_items(items)
    assert len(clusters) == 2


def test_recurring_titles_still_merge_when_real_story_content_overlaps():
    # same recurring template, but this time the titles also share a
    # specific, genuine news event beyond the boilerplate wording
    items = [
        make_item("a", "arlnow", "ARLnow", "news",
                   "ARLnow Daily Debrief: Metro station closure snarls Rosslyn traffic"),
        make_item("b", "ffxnow", "FFXnow", "news",
                   "FFXnow Daily Debrief: Metro station closure snarls Rosslyn traffic"),
    ]
    clusters = engine.cluster_items(items)
    assert len(clusters) == 1  # the real, shared content still clusters normally


# --- Fix 2: "construction" is not automatically real_estate ----------------

def test_construction_worker_fatality_is_not_real_estate():
    cluster = [
        make_item("a", "insidenova_prince_william", "InsideNoVa - Prince William section", "news",
                   "Construction worker dies in Woodbridge incident",
                   text="A 26-year-old construction worker died Wednesday morning after a workplace accident at a job site in Woodbridge.")
    ]
    assert engine.assign_category(cluster) != "real_estate"


def test_genuine_construction_project_story_is_still_real_estate():
    cluster = [
        make_item("a", "ffxnow", "FFXnow", "news",
                   "New apartment construction project breaks ground in Tysons",
                   text="Developers broke ground this week on a new construction project bringing housing and retail to Tysons.")
    ]
    assert engine.assign_category(cluster) == "real_estate"


# --- Fix 3: sponsored/advertising content is excluded -----------------------

def test_sponsored_content_is_not_locally_relevant():
    item = make_item("a", "arlnow", "ARLnow", "news",
                       "Your next customer is reading this right now",
                       text="Every day, Arlingtonians open ARLnow to catch what's happening.",
                       category_hint="Sponsored", region="northern_virginia")
    assert engine.is_locally_relevant(item) is False


def test_normal_business_opening_article_remains_eligible():
    item = make_item("a", "arlnow", "ARLnow", "news",
                       "New restaurant opens in downtown Arlington",
                       text="A new restaurant celebrated its grand opening in Arlington this week.",
                       category_hint="Around Town", region="northern_virginia")
    assert engine.is_locally_relevant(item) is True


# --- Fix 4: incidental locality mention is not enough -----------------------

def test_national_review_with_incidental_local_venue_mention_is_not_relevant():
    item = make_item("a", "falls_church_news_press", "Falls Church News-Press", "news",
                       "Spider-Man: Brand New Day by Lisa Sinrod",
                       text=(
                           "The latest Spider-Man offering is a direct sequel to 2021's smash hit. "
                           "It is currently playing on several screens at Falls Church's own Paragon "
                           "Founders Row theaters."
                       ),
                       category_hint="Arts & Entertainment", region="northern_virginia")
    assert engine.is_locally_relevant(item) is False


def test_genuine_falls_church_arts_story_remains_relevant():
    item = make_item("a", "falls_church_news_press", "Falls Church News-Press", "news",
                       "Falls Church art gallery reopens after renovation",
                       text="The gallery, a Falls Church institution for decades, reopened this weekend.",
                       category_hint="Arts & Entertainment", region="northern_virginia")
    assert engine.is_locally_relevant(item) is True  # place name is in the title


# ---------------------------------------------------------------------------
# Evidence Quality Cleanup V2
# ---------------------------------------------------------------------------

# --- Fix 1: whole-word/phrase-safe matching ---------------------------------

def test_contains_term_whole_word_matches_and_simple_plural():
    assert engine.contains_term("A new hiking trail opens this weekend", "trail") is True
    assert engine.contains_term("Several new trails were added to the park", "trail") is True


def test_contains_term_does_not_match_inside_a_longer_word():
    assert engine.contains_term("A worker was trapped inside a trailer", "trail") is False
    assert engine.contains_term("A trailing edge sensor failed", "trail") is False
    assert engine.contains_term("A contrail was visible in the sky", "trail") is False


def test_contains_term_land_does_not_match_maryland():
    assert engine.contains_term("The suspect fled toward Maryland", "land") is False
    assert engine.contains_term("The county bought new land for a park", "land") is True


def test_contains_term_bar_does_not_match_barn_or_embargo():
    assert engine.contains_term("The old barn was renovated", "bar") is False
    assert engine.contains_term("The trade embargo continued", "bar") is False
    assert engine.contains_term("The new bar opened downtown", "bar") is True


def test_contains_term_multi_word_phrases_still_match():
    text = "The Planning Commission discussed a new data center proposal at the public hearing."
    assert engine.contains_term(text, "planning commission") is True
    assert engine.contains_term(text, "data center") is True
    assert engine.contains_term(text, "public hearing") is True
    assert engine.contains_term(text, "school board") is False


def test_construction_worker_fatality_no_longer_qualifies_for_food_lifestyle():
    cluster = [
        make_item("a", "insidenova_prince_william", "InsideNoVa - Prince William section", "news",
                   "Construction worker dies in Woodbridge trailer collapse",
                   text="A 26-year-old construction worker died Wednesday morning after a mobile "
                        "trailer support collapsed at a job site in Woodbridge.")
    ]
    assert engine.assign_category(cluster) != "food_lifestyle"
    assert engine.assign_category(cluster) != "real_estate"  # V1 fix still holds too


def test_genuine_trail_story_still_qualifies_for_food_lifestyle():
    cluster = [
        make_item("a", "ffxnow", "FFXnow", "news",
                   "New hiking trail opens in Fairfax County park",
                   text="The county celebrated the opening of a new hiking trail this weekend, "
                        "part of an expanding outdoor recreation trail network.")
    ]
    assert engine.assign_category(cluster) == "food_lifestyle"


# --- Fix 2: publisher/byline locality leakage -------------------------------

def test_publisher_byline_alone_does_not_create_local_relevance():
    item = make_item(
        "a", "falls_church_news_press", "Falls Church News-Press", "news",
        "A Penny for Your Thoughts",
        text=(
            "by Penny Gross Exclusive to the Falls Church News-Press Senator Ted Cruz should be "
            "ashamed of himself. National politics dominated this week's Senate hearings."
        ),
        category_hint="Commentary", region="northern_virginia",
    )
    assert engine.is_locally_relevant(item) is False


def test_genuine_commentary_about_local_subject_remains_eligible():
    item = make_item(
        "a", "falls_church_news_press", "Falls Church News-Press", "news",
        "Falls Church zoning proposal deserves scrutiny",
        text="by Penny Gross Exclusive to the Falls Church News-Press The city's zoning proposal "
             "for downtown Falls Church raises real questions residents should ask at Tuesday's hearing.",
        category_hint="Commentary", region="northern_virginia",
    )
    assert engine.is_locally_relevant(item) is True  # place name is in the title, independent of the byline


# ---------------------------------------------------------------------------
# Timeframes V2: stable timestamps and pre-cluster filtering
# ---------------------------------------------------------------------------

FIXED_NOW = datetime(2026, 8, 17, 16, 0, tzinfo=timezone.utc)  # noon Eastern


def set_item_times(item, *, published_at=None, first_seen_at=None, collected_at=None, last_seen_at=None):
    item["published_at"] = published_at
    item["first_seen_at"] = first_seen_at
    item["collected_at"] = collected_at
    item["last_seen_at"] = last_seen_at
    return item


def fixed_item(item_id, title, timestamp, **overrides):
    item = make_item(
        item_id,
        overrides.pop("source_id", "front_royal_town_news"),
        overrides.pop("source_name", "Town of Front Royal - News Flash"),
        overrides.pop("source_type", GOV),
        title,
        **overrides,
    )
    return set_item_times(item, published_at=timestamp.isoformat(), collected_at=timestamp.isoformat())


def test_supported_timeframe_metadata_has_canonical_order_and_durations():
    assert engine.SUPPORTED_TIMEFRAMES == ("today", "24h", "3d", "7d", "14d")
    metadata = engine.get_timeframe_metadata()
    assert [entry["key"] for entry in metadata] == list(engine.SUPPORTED_TIMEFRAMES)
    assert [entry["label"] for entry in metadata] == [
        "Today", "24 Hours", "3 Days", "7 Days", "14 Days",
    ]
    assert [entry["hours"] for entry in metadata] == [None, 24, 72, 168, 336]


def test_today_uses_eastern_calendar_midnight_not_rolling_24_hours():
    just_before_midnight = fixed_item(
        "before", "Front Royal prior evening update", datetime(2026, 8, 17, 3, 59, 59, tzinfo=timezone.utc)
    )
    at_midnight = fixed_item(
        "at", "Front Royal midnight update", datetime(2026, 8, 17, 4, 0, tzinfo=timezone.utc)
    )
    morning = fixed_item(
        "morning", "Front Royal morning update", datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    )

    today_ids = {
        item["item_id"]
        for item in engine.filter_items_by_timeframe(
            [just_before_midnight, at_midnight, morning], "today", now=FIXED_NOW
        )
    }
    rolling_ids = {
        item["item_id"]
        for item in engine.filter_items_by_timeframe(
            [just_before_midnight, at_midnight, morning], "24h", now=FIXED_NOW
        )
    }

    assert today_ids == {"at", "morning"}
    assert rolling_ids == {"before", "at", "morning"}


@pytest.mark.parametrize(
    ("timeframe", "hours"),
    [("24h", 24), ("3d", 72), ("7d", 168), ("14d", 336)],
)
def test_rolling_timeframe_boundaries_are_inclusive(timeframe, hours):
    at_cutoff = fixed_item("at", "Front Royal cutoff item", FIXED_NOW - timedelta(hours=hours))
    too_old = fixed_item(
        "old", "Front Royal older item", FIXED_NOW - timedelta(hours=hours, seconds=1)
    )
    future = fixed_item("future", "Front Royal future item", FIXED_NOW + timedelta(seconds=1))

    kept = engine.filter_items_by_timeframe([at_cutoff, too_old, future], timeframe, now=FIXED_NOW)

    assert [item["item_id"] for item in kept] == ["at"]


def test_today_cutoff_respects_eastern_dst_offset_at_midnight():
    assert engine.timeframe_cutoff(
        "today", datetime(2026, 3, 8, 16, 0, tzinfo=timezone.utc)
    ) == datetime(2026, 3, 8, 5, 0, tzinfo=timezone.utc)
    assert engine.timeframe_cutoff(
        "today", datetime(2026, 11, 1, 17, 0, tzinfo=timezone.utc)
    ) == datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)


def test_item_datetime_prefers_publication_then_first_seen_then_legacy_collected():
    item = set_item_times(
        make_item("a", "source", "Source", "news", "Front Royal item"),
        published_at="2026-08-10T10:00:00+00:00",
        first_seen_at="2026-08-11T10:00:00+00:00",
        collected_at="2026-08-12T10:00:00+00:00",
        last_seen_at="2026-08-17T10:00:00+00:00",
    )
    assert engine.item_datetime(item) == datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)

    item["published_at"] = None
    assert engine.item_datetime(item) == datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)

    item["first_seen_at"] = None
    assert engine.item_datetime(item) == datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)

    item["collected_at"] = None
    assert engine.item_datetime(item) is None  # last_seen_at must never refresh an old story


def test_invalid_publication_timestamp_falls_back_to_first_seen():
    item = set_item_times(
        make_item("a", "source", "Source", "news", "Front Royal item"),
        published_at="not-a-date",
        first_seen_at="2026-08-17T10:00:00Z",
        collected_at="2026-08-16T10:00:00Z",
    )
    assert engine.item_datetime(item) == datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)


def test_pipeline_filters_evidence_before_clustering_and_ranking(tmp_path):
    inside = fixed_item(
        "inside", "Front Royal council approves zoning plan", FIXED_NOW - timedelta(hours=2)
    )
    outside = fixed_item(
        "outside", "Front Royal council approves zoning plan", FIXED_NOW - timedelta(hours=30)
    )
    path = write_items(tmp_path, [inside, outside])

    day_data, _, day_stats = engine.run_pipeline(path, timeframe="24h", now=FIXED_NOW)
    three_day_data, _, _ = engine.run_pipeline(path, timeframe="3d", now=FIXED_NOW)

    day_topic = next(topic for topics in day_data["shenandoah_valley"].values() for topic in topics)
    three_day_topic = next(topic for topics in three_day_data["shenandoah_valley"].values() for topic in topics)
    assert day_stats["timeframe_items"] == 1
    assert day_topic["mentions"] == 1
    assert three_day_topic["mentions"] == 2
    assert day_data["updated_at"] == FIXED_NOW.isoformat(timespec="seconds")


def test_unknown_timeframe_is_rejected_clearly():
    with pytest.raises(ValueError, match="unsupported timeframe"):
        engine.filter_items_by_timeframe([], "30d", now=FIXED_NOW)


# ---------------------------------------------------------------------------
# Approved primary-subject exclusions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("title", "category_hint", "expected_reason"),
    [
        ("Two hurt in Front Royal car crash", None, "vehicle_accident"),
        ("Traffic delays after crash on I-66 in Fairfax", None, "vehicle_accident"),
        ("Pedestrian struck by vehicle in Fairfax", None, "vehicle_accident"),
        ("Warren County police blotter for August 17", None, "routine_crime"),
        ("Front Royal man arrested after shoplifting report", None, "individual_arrest"),
        ("Front Royal weekend weather forecast calls for sunshine", "Weather", "generic_weather_forecast"),
        ("Front Royal football: Wildcats beat Tigers 28-14", "Sports", "sports_score_or_recap"),
        ("Obituary: Jane Doe of Front Royal", "Obituaries", "obituary"),
        ("Lane closures for paving announced in Front Royal", None, "routine_roadwork"),
        ("Front Royal home of the week listed for $850,000", None, "property_listing"),
        ("Recipe: easy summer chicken", "Recipes", "generic_advice_or_recipe"),
        ("Your Front Royal customer is reading this", "Sponsored", "sponsored_content"),
    ],
)
def test_approved_primary_subject_exclusions(title, category_hint, expected_reason):
    item = make_item(
        "x", "royal_examiner", "Royal Examiner", "news", title,
        category_hint=category_hint,
    )
    assert engine.primary_subject_exclusion_reason(item) == expected_reason


@pytest.mark.parametrize(
    ("title", "category_hint"),
    [
        ("Front Royal adopts Vision Zero safety plan after rise in crashes", None),
        ("Three injured in Smith Mountain Lake boat crash", None),
        ("Tornado warning issued for Warren County", "Weather"),
        ("Weather alert: flood watches issued for Arlington", "Weather"),
        ("Town Council approves bridge repair funding in Front Royal", None),
        ("Planning Commission public hearing on Front Royal housing development", None),
        ("Warren County funds new high school football field", "Sports"),
        ("How to comment on Front Royal's rezoning proposal", "Advice"),
    ],
)
def test_exclusion_rules_preserve_broader_local_policy_and_safety_stories(title, category_hint):
    item = make_item(
        "x", "royal_examiner", "Royal Examiner", "news", title,
        category_hint=category_hint,
    )
    assert engine.primary_subject_exclusion_reason(item) is None


def test_excluded_subjects_are_removed_before_topic_discovery(tmp_path):
    crash = fixed_item(
        "crash", "Two hurt in Front Royal car crash", FIXED_NOW - timedelta(hours=1),
        source_id="royal_examiner", source_name="Royal Examiner", source_type="news",
    )
    council = fixed_item(
        "council", "Town Council approves Front Royal zoning plan", FIXED_NOW - timedelta(hours=1)
    )
    path = write_items(tmp_path, [crash, council])

    data, _, stats = engine.run_pipeline(path, timeframe="24h", now=FIXED_NOW)
    titles = [topic["title"] for topics in data["shenandoah_valley"].values() for topic in topics]

    assert stats["excluded_items"] == 1
    assert stats["by_region"]["shenandoah_valley"]["exclusions_by_reason"] == {
        "vehicle_accident": 1,
    }
    assert not any("crash" in title.lower() for title in titles)
    assert any("zoning" in title.lower() for title in titles)


# ---------------------------------------------------------------------------
# Civic priority scoring and normalized badges
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("verb", "badge"),
    [
        ("passed", "PASSED"),
        ("approved", "APPROVED"),
        ("adopted", "ADOPTED"),
        ("denied", "DENIED"),
        ("voted on", "VOTED"),
        ("deferred", "DEFERRED"),
    ],
)
def test_formal_civic_actions_receive_plus_five_and_normalized_badge(verb, badge):
    cluster = [
        fixed_item(
            "civic", f"Town Council {verb} the annual budget", FIXED_NOW - timedelta(hours=1)
        )
    ]
    facts = engine.build_topic_facts(cluster, now=FIXED_NOW)

    assert facts["civic_action"] == badge
    assert facts["land_use"] is False
    assert facts["civic_action_bonus"] == 5
    assert facts["civic_priority_bonus"] == 5


def test_final_land_use_action_stacks_to_capped_plus_eight():
    cluster = [
        fixed_item(
            "land", "Planning Commission approved Front Royal rezoning request",
            FIXED_NOW - timedelta(hours=1),
        )
    ]
    facts = engine.build_topic_facts(cluster, now=FIXED_NOW)
    breakdown = engine.compute_score_breakdown(facts)
    output = engine.topic_to_output(facts, rank=1)

    assert facts["civic_action"] == "APPROVED"
    assert facts["land_use"] is True
    assert facts["civic_action_bonus"] == 5
    assert facts["civic_land_use_bonus"] == 3
    assert facts["civic_priority_bonus"] == 8
    assert breakdown["civic_priority"] == 8
    assert breakdown["total_score"] == breakdown["normal_score"] + 8
    assert output["civic_action"] == "APPROVED"
    assert output["land_use"] is True


@pytest.mark.parametrize(
    ("title", "badge", "expected_bonus"),
    [
        ("Town Council will vote on Front Royal rezoning", "UPCOMING VOTE", 7),
        ("Planning Commission public hearing on Front Royal subdivision", "PUBLIC HEARING", 7),
        ("Board of Supervisors meeting agenda includes a Front Royal easement", "SCHEDULED ACTION", 7),
        ("Board of Zoning Appeals reviews Front Royal land use", "LAND USE", 3),
    ],
)
def test_upcoming_and_land_only_civic_priorities(title, badge, expected_bonus):
    facts = engine.build_topic_facts(
        [fixed_item("civic", title, FIXED_NOW - timedelta(hours=1))],
        now=FIXED_NOW,
    )
    assert facts["civic_action"] == badge
    assert facts["land_use"] is True
    assert facts["civic_priority_bonus"] == expected_bonus


def test_future_vote_language_does_not_claim_background_approval_as_result():
    cluster = [
        fixed_item(
            "future",
            "Town Council will vote on rezoning plan previously approved by Planning Commission",
            FIXED_NOW - timedelta(hours=1),
        )
    ]
    facts = engine.build_topic_facts(cluster, now=FIXED_NOW)
    assert facts["civic_action"] == "UPCOMING VOTE"
    assert facts["civic_priority_bonus"] == 7


def test_negated_or_uncertain_civic_result_does_not_claim_final_action():
    for phrase in (
        "Town Council rezoning plan has not yet been approved",
        "Town Council rezoning plan may be denied",
        "Town Council rezoning plan is expected to be adopted",
    ):
        facts = engine.build_topic_facts(
            [fixed_item("uncertain", phrase, FIXED_NOW - timedelta(hours=1))],
            now=FIXED_NOW,
        )
        assert facts["civic_action"] == "LAND USE"
        assert facts["civic_priority_bonus"] == 3


def test_land_story_without_scoped_civic_body_gets_no_civic_bonus_or_badge():
    cluster = [
        fixed_item(
            "private", "Developer announces Front Royal housing project",
            FIXED_NOW - timedelta(hours=1),
            source_id="royal_examiner", source_name="Royal Examiner", source_type="news",
        )
    ]
    facts = engine.build_topic_facts(cluster, now=FIXED_NOW)
    assert facts["civic_action"] is None
    assert facts["land_use"] is False
    assert facts["civic_priority_bonus"] == 0
