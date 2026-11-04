"""Unit tests for scoring, dedupe and the 3/5 cap. Fixture feeds live in tests/fixtures."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 4, 4, 17, tzinfo=timezone.utc)
FRESH = NOW - timedelta(hours=5)
OLD = NOW - timedelta(hours=80)


def fixture(name: str) -> bytes:
    text = (FIX / name).read_text(encoding="utf-8")
    text = text.replace("PUBDATE_FRESH", FRESH.strftime("%a, %d %b %Y %H:%M:%S GMT"))
    text = text.replace("PUBDATE_OLD", OLD.strftime("%a, %d %b %Y %H:%M:%S GMT"))
    text = text.replace("PUBISO_FRESH", FRESH.strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    return text.encode("utf-8")


def src(name, tier=1, category="ai", **kw):
    return {"name": name, "tier": tier, "category": category, "url": "https://x.test/feed", "type": "rss", **kw}


def item(title, url, tier=2, category="ai", hours=5, **kw):
    it = build.base_item(src(f"src-{tier}-{url[-6:]}", tier, category), {}, url, title, "")
    it["published"] = (NOW - timedelta(hours=hours)).isoformat()
    it.update(kw)
    return it


EMPTY_SEEN = {"version": 1, "items": {}}


# ---------------------------------------------------------------- parsing
def test_parse_rss_strips_html_and_limits_summary():
    items = build.parse_rss(fixture("official.xml"), src("Official Lab"))
    first = items[0]
    assert first["title"] == "Introducing Claude Omega 5"
    assert "<" not in first["summary"]
    assert len(first["summary"]) <= build.CONFIG["summary_chars"]
    assert first["summary"].startswith("Today we are releasing Claude Omega 5")


def test_parse_gnews_strips_publisher_and_sets_source():
    items = build.parse_gnews(fixture("gnews.xml"), src("Google News", 3))
    assert items[0]["title"] == "Anthropic introduces Claude Omega 5 model"
    assert items[0]["source"] == "TechPaper (via Google News)"
    assert items[0]["summary"] == ""


def test_parse_youtube_sets_thumbnail():
    items = build.parse_youtube(fixture("youtube.xml"), src("Lab Channel", 1, type="youtube"))
    assert items[0]["thumbnail"] == "https://i.ytimg.com/vi/abc123XYZ/hqdefault.jpg"
    assert items[0]["kind"] == "video"


def test_github_releases_skip_patch_and_prerelease():
    items = build.parse_github_releases(fixture("releases.xml"), src("Ollama releases", 1, "mac", type="github_releases"))
    assert [i["title"] for i in items] == ["ollama v0.14.0"]
    assert items[0]["kind"] == "release"


# ---------------------------------------------------------------- window / seen
def test_filter_candidates_drops_old_and_seen():
    items = build.parse_rss(fixture("official.xml"), src("Official Lab"))
    assert len(items) == 3
    fresh = build.filter_candidates(items, EMPTY_SEEN, NOW)
    assert {i["title"] for i in fresh} == {"Introducing Claude Omega 5", "Our quarterly podcast episode 12"}
    seen = {"version": 1, "items": {fresh[0]["id"]: {"date": "2026-11-03", "url": build.normalize_url(fresh[0]["url"]), "title": fresh[0]["title"]}}}
    assert [i["title"] for i in build.filter_candidates(items, seen, NOW)] == ["Our quarterly podcast episode 12"]


def test_seen_matches_same_story_with_different_url():
    seen = {"version": 1, "items": {"abc": {"date": "2026-11-03", "url": "https://other.test/x", "title": "Anthropic introduces Claude Omega 5 model"}}}
    it = item("Anthropic introduces Claude Omega 5", "https://news.test/omega5")
    assert build.is_seen(it, seen)


def test_prune_seen_drops_old_entries():
    seen = {"version": 1, "items": {"a": {"date": "2026-09-01"}, "b": {"date": "2026-11-01"}}}
    assert list(build.prune_seen(seen, "2026-11-04")["items"]) == ["b"]


# ---------------------------------------------------------------- scoring
def test_score_boosts_and_penalties():
    rel, _ = build.score_item(item("Introducing Claude Omega 5, now available", "https://a.test/1", tier=1))
    plain, _ = build.score_item(item("A quiet note about things", "https://a.test/2", tier=1))
    pod, _ = build.score_item(item("Our quarterly podcast episode 12", "https://a.test/3", tier=1))
    deal, _ = build.score_item(item("Big launch deal: 40% off everything", "https://a.test/4", tier=1))
    patch, _ = build.score_item(item("Tool v1.4.2 released", "https://a.test/5", tier=1))
    assert plain == build.CONFIG["tier_score"][1]
    assert rel > plain + 6
    assert pod < plain
    assert deal < plain
    assert patch < plain + 3  # +3 release, -4 patch bump


def test_score_tier_order_and_hn_points():
    t1, _ = build.score_item(item("Something", "https://a.test/1", tier=1))
    t3, _ = build.score_item(item("Something", "https://a.test/2", tier=3))
    hn, _ = build.score_item(item("Something", "https://a.test/3", tier=3, hn_points=500))
    assert t1 > t3
    assert hn == pytest.approx(t3 + build.CONFIG["hn_points_cap"])


def test_multi_source_boost():
    base, _ = build.score_item(item("Something", "https://a.test/1", tier=2))
    multi, reasons = build.score_item(item("Something", "https://a.test/1", tier=2, also_in=["B", "C"]))
    assert multi == base + build.CONFIG["multi_source_boost"] + build.CONFIG["multi_source_extra_per_source"]
    assert any("3 sources" in r for r in reasons)


# ---------------------------------------------------------------- dedupe
def test_dedupe_merges_same_story_keeps_official():
    official = item("Introducing Claude Omega 5", "https://www.lab.test/news/omega?utm_source=rss", tier=1)
    gnews = item("Anthropic introduces Claude Omega 5 model", "https://news.google.com/rss/articles/abc", tier=3)
    same_url = item("Introducing Claude Omega 5", "https://lab.test/news/omega/", tier=2, hn_points=250, kind="hn")
    merged = build.dedupe([gnews, same_url, official])
    assert len(merged) == 1
    assert merged[0]["source"] == official["source"]
    assert merged[0]["hn_points"] == 250
    assert len(merged[0]["also_in"]) == 2


def test_dedupe_keeps_different_stories():
    a = item("Gemini 4 launches with new reasoning mode", "https://a.test/1")
    b = item("Ollama v0.14.0 adds Gemini 4 support", "https://b.test/2", category="mac")
    assert len(build.dedupe([a, b])) == 2


def test_normalize_url():
    assert build.normalize_url("https://www.Lab.test/news/x/?utm_source=rss&id=2#top") == "https://lab.test/news/x?id=2"


# ---------------------------------------------------------------- picking
NAMES = ["Orion", "Vega", "Lyra", "Atlas", "Nova", "Helix", "Titan", "Pulsar", "Zephyr", "Comet"]


def big(n, cat="ai", tier=1):
    """A distinct 'major' item per n, so dedupe never merges them."""
    verbs = ["Introducing", "Launching", "Releasing", "Announcing", "Meet", "Say hello to", "Unveiling", "Presenting", "Shipping", "Debuting"]
    tails = ["is here", "arrives today", "opens to everyone", "lands on Mac", "goes live", "ships now", "rolls out", "enters GA", "hits the store", "debuts"]
    return item(f"{verbs[n % 10]} {NAMES[n % 10]} {n}: new model now available, {tails[n % 10]}",
                f"https://a.test/{n}", tier=tier, category=cat)


def test_pick_top_three_by_default():
    cands = [big(1, "ai"), big(2, "code"), big(3, "mac"), big(4, "guides"), big(5, "reselling")]
    picks = build.select_picks(cands)
    assert len(picks) == build.CONFIG["base_picks"]
    assert picks[0]["score"] >= picks[-1]["score"]
    assert all(p["why"] for p in picks)


def test_pick_four_and_five_only_above_major_threshold():
    cands = [big(1, "ai"), big(2, "code"), big(3, "mac"), big(4, "guides"), big(5, "reselling")]
    for c in cands[:5]:
        c["also_in"] = ["X", "Y"]  # pushes scores over major_threshold
    picks = build.select_picks(cands)
    assert len(picks) == build.CONFIG["max_picks"]
    cands2 = [big(1, "ai"), big(2, "code"), big(3, "mac"), big(4, "guides"), big(5, "reselling"), big(6, "crypto")]
    cands2[0]["also_in"] = ["X", "Y", "Z"]
    assert len(build.select_picks(cands2)) == build.CONFIG["base_picks"]


def test_pick_max_two_per_category():
    cands = [big(n, "ai") for n in range(1, 7)] + [big(9, "code")]
    picks = build.select_picks(cands)
    assert sum(p["category"] == "ai" for p in picks) == build.CONFIG["max_per_category"]
    assert any(p["category"] == "code" for p in picks)


def test_quiet_day_has_no_filler():
    cands = [item("Some mild post about things", "https://a.test/1", tier=3),
             item("Another unremarkable item", "https://a.test/2", tier=3)]
    assert build.select_picks(cands) == []


def test_crypto_capped_at_one_and_needs_high_score():
    cands = [big(1, "crypto"), big(2, "crypto"), big(3, "ai")]
    picks = build.select_picks(cands)
    assert sum(p["category"] == "crypto" for p in picks) <= 1
    weak = [item("Bitcoin drifts sideways", "https://c.test/1", tier=2, category="crypto")]
    assert build.select_picks(weak) == []


def test_ai_picks_get_llm_tag():
    picks = build.select_picks([big(1, "ai")])
    assert picks[0]["llm"] == "Other"
    picks = build.select_picks([item("Introducing Gemini 4, now available", "https://g.test/1", tier=1)])
    assert picks[0]["llm"] == "Gemini"


# ---------------------------------------------------------------- output helpers
def test_tip_rotates_by_date(tmp_path):
    p = tmp_path / "tips.json"
    p.write_text(json.dumps([{"tag": "a", "text": "one"}, {"tag": "b", "text": "two"}]))
    t1 = build.tip_for("2026-11-04", p)
    t2 = build.tip_for("2026-11-05", p)
    assert t1 != t2
    assert build.tip_for("2026-11-06", p) == t1


def test_write_json_atomic(tmp_path):
    p = tmp_path / "out.json"
    build.write_json_atomic(p, {"ok": True})
    assert json.loads(p.read_text()) == {"ok": True}
    assert not p.with_suffix(".json.tmp").exists()


def test_sources_yaml_is_valid():
    sources = build.load_sources()
    assert len(sources) >= 40
    for s in sources:
        assert s["type"] in build.FETCHERS
        assert s["category"] in build.CATEGORIES + ["auto"]
        assert 1 <= int(s["tier"]) <= 3
