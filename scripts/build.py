#!/usr/bin/env python3
"""Daily build: fetch sources.yaml feeds, pick today's items, write data/.

Run:  python scripts/build.py            (from the repo root)
      python scripts/build.py --dry-run  (fetch and score, write nothing)
No API keys. Every network failure is logged and skipped.
"""
from __future__ import annotations

import calendar
import difflib
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

# ============================================================================
# CONFIG: every threshold lives here.
# ============================================================================
CONFIG = {
    # Candidate window: items older than this are ignored.
    "window_hours": 36,
    # Picks: always try to fill BASE_PICKS, go up to MAX_PICKS only for
    # items whose score clears MAJOR_THRESHOLD.
    "base_picks": 3,
    "max_picks": 5,
    "major_threshold": 14.0,
    # Any pick needs at least this score. Below it the day stays quiet.
    "min_score": 7.0,
    # Per-category cap for one day. Crypto stories are capped at 1.
    "max_per_category": 2,
    "max_crypto_stories": 1,
    # Crypto stories need a higher bar: price moves are already on the strip.
    "crypto_min_score": 10.0,
    # Base score by source tier.
    "tier_score": {1: 6.0, 2: 4.0, 3: 2.0},
    # Title/summary keyword boosts (regex, case-insensitive).
    "boosts": {
        r"\b(release[sd]?|releasing)\b": 3.0,
        r"\blaunch(es|ed|ing)?\b": 3.0,
        r"\bintroducing\b": 4.0,
        r"\bnew model\b": 4.0,
        r"\bnow available\b": 3.0,
        r"\bopen[- ]weights?\b": 4.0,
        r"\bgenerally available\b|\bGA\b": 3.0,
        r"\bannounc(es|ed|ing)\b": 2.0,
        r"\b(gpt|claude|gemini|grok|llama|deepseek|mistral|qwen)[ -]?\d": 3.0,
        r"\bv?\d+\.0(\.0)?\b": 1.5,  # x.0 style major versions
    },
    # Category-specific boosts (practical reselling / beginner guides).
    "category_boosts": {
        "reselling": {r"\b(tips?|how to|guide|pricing|shipping|photos?|bundl)": 3.0,
                      r"\b(latvia|lithuania|estonia|baltic|eu|europe)\b": 2.0},
        "guides": {r"\b(beginner|tutorial|guide|how to|step[- ]by[- ]step|getting started)\b": 3.0},
    },
    # Penalties (regex, case-insensitive).
    "penalties": {
        r"\brumou?r": 4.0,
        r"\breportedly\b": 2.0,
        r"\bopinion\b|\bop-ed\b": 4.0,
        r"\bpodcast\b|\bepisode\b": 4.0,
        r"\bsponsored\b": 6.0,
        r"\bdeal\b|\d+\s?% off|\bdiscount\b|\bcoupon\b|\bblack friday\b": 5.0,
        r"\bv?\d+\.\d+\.[1-9]\d*\b": 4.0,  # patch bumps like 1.4.2
        r"#shorts\b": 10.0,
        r"\bpaper\b|\barxiv\b": 3.0,
    },
    # Guides category: skip research papers and heavy math entirely.
    "guides_skip": r"\b(paper|arxiv|theorem|proof|benchmark)\b",
    # Hacker News: points / divisor, capped.
    "hn_points_divisor": 50.0,
    "hn_points_cap": 5.0,
    # Same story in 2+ sources.
    "multi_source_boost": 5.0,
    "multi_source_extra_per_source": 1.0,
    # Dedupe: title similarity ratio (difflib) above this means same story.
    "title_similarity": 0.90,
    # Dedupe: share of stemmed title words two stories must have in common.
    "title_jaccard": 0.6,
    # Seen items: never show the same story again within this many days.
    "seen_days": 30,
    # History kept in data/days and shown in "Earlier this week".
    "history_days": 30,
    "week_days": 7,
    # Summary length.
    "summary_chars": 160,
    # Network.
    "timeout": 20,
    "user_agent": "ai-daily/1.0 (+https://github.com/adrianskalvis/ai-daily)",
    "timezone": "Europe/Riga",
}
# ============================================================================

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DAYS = DATA / "days"
SEEN_FILE = DATA / "seen.json"
LATEST_FILE = DATA / "latest.json"
TIPS_FILE = DATA / "tips.json"
SOURCES_FILE = ROOT / "sources.yaml"

CATEGORIES = ["ai", "code", "mac", "crypto", "reselling", "guides"]

LLM_PATTERNS = [
    ("Claude", r"\bclaude\b|\banthropic\b"),
    ("GPT", r"\bgpt\b|\bopenai\b|\bchatgpt\b|\bo[1-9]\b"),
    ("Gemini", r"\bgemini\b|\bdeepmind\b|\bgemma\b"),
    ("Grok", r"\bgrok\b|\bxai\b"),
    ("Llama", r"\bllama\b(?!\.cpp)|\bmeta ai\b"),
    ("DeepSeek", r"\bdeepseek\b"),
    ("Mistral", r"\bmistral\b|\bmixtral\b|\ble chat\b"),
    ("Qwen", r"\bqwen\b|\balibaba\b"),
]

# Used for hn sources with category: auto.
CATEGORY_KEYWORDS = {
    "code": r"\bclaude code\b|\bcodex\b|\bcursor\b|\bcopilot\b|\bmcp\b|\bn8n\b|\bagents?\b|\bcoding agent",
    "mac": r"\bollama\b|\blm studio\b|\bllama\.cpp\b|\bmlx\b|\bon[- ]device\b|\blocal llm",
    "ai": r"\bclaude\b|\banthropic\b|\bopenai\b|\bgpt\b|\bgemini\b|\bdeepmind\b|\bgrok\b|\bxai\b|\bllama\b|\bdeepseek\b|\bmistral\b|\bqwen\b|\bllm\b",
    "crypto": r"\bbitcoin\b|\bethereum\b|\bsolana\b|\bcrypto\b",
}

LOG_LINES: list[str] = []


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}"
    LOG_LINES.append(line)
    print(line, flush=True)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return WS_RE.sub(" ", text).strip()


def make_summary(text: str, title: str = "", limit: int | None = None) -> str:
    """1-2 clean sentences, at most `limit` chars, cut on a sentence or word."""
    limit = limit or CONFIG["summary_chars"]
    text = strip_html(text)
    if not text:
        return ""
    if title and text.lower().startswith(title.lower()):
        text = text[len(title):].lstrip(" :-–—.")
    if not text or text.lower() == title.lower():
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "! ", "? "):
        idx = cut.rfind(sep)
        if idx >= limit // 2:
            return cut[: idx + 1]
    idx = cut.rfind(" ")
    return (cut[:idx] if idx > 0 else cut).rstrip(",;:") + "…"


def normalize_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = [(k, v) for k, v in parse_qsl(parts.query)
             if not k.lower().startswith(("utm_", "ref", "fbclid", "gclid", "si", "feature"))]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", host, path, urlencode(sorted(query)), ""))


def normalize_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"\s+[-|–—]\s+[^-|–—]{2,40}$", "", t)  # drop " - Publisher"
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return WS_RE.sub(" ", t).strip()


STOPWORDS = {"a", "an", "the", "of", "to", "for", "with", "and", "in", "on", "is", "its", "by", "at",
             "from", "as", "it", "this", "that", "your", "you", "we", "our", "vs"}


def title_tokens(title: str) -> set[str]:
    """Lower-case content words with a light stem, so 'introduces' == 'introducing'."""
    out = set()
    for w in normalize_title(title).split():
        if w in STOPWORDS:
            continue
        for suf in ("ing", "ed", "es", "s"):
            if len(w) > len(suf) + 3 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.add(w)
    return out


def titles_similar(a: str, b: str) -> bool:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if difflib.SequenceMatcher(None, na, nb).ratio() >= CONFIG["title_similarity"]:
        return True
    ta, tb = title_tokens(a), title_tokens(b)
    if len(ta) >= 3 and len(tb) >= 3:
        if ta <= tb or tb <= ta:  # one title is contained in the other
            return True
        return len(ta & tb) / len(ta | tb) >= CONFIG["title_jaccard"]
    return False


def item_key(url: str, title: str) -> str:
    base = normalize_url(url) if url else normalize_title(title)
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def detect_llm(text: str) -> str:
    low = text.lower()
    for name, pat in LLM_PATTERNS:
        if re.search(pat, low):
            return name
    return "Other"


def detect_category(text: str) -> str | None:
    low = text.lower()
    for cat in ("code", "mac", "crypto", "ai"):
        if re.search(CATEGORY_KEYWORDS[cat], low):
            return cat
    return None


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def http_get(url: str, **kw) -> requests.Response:
    headers = {"User-Agent": CONFIG["user_agent"], "Accept": "*/*"}
    resp = requests.get(url, headers=headers, timeout=CONFIG["timeout"], **kw)
    resp.raise_for_status()
    return resp


def entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
            except (OverflowError, ValueError):
                continue
    return None


def base_item(source: dict, entry, url: str, title: str, summary_src: str) -> dict:
    return {
        "id": item_key(url, title),
        "title": WS_RE.sub(" ", strip_html(title)).strip(),
        "url": url,
        "source": source["name"],
        "tier": int(source.get("tier", 3)),
        "category": source.get("category", "ai"),
        "llm": source.get("llm"),
        "published": None,
        "summary": make_summary(summary_src, title),
        "thumbnail": None,
        "hn_points": 0,
        "also_in": [],
    }


def parse_rss(content: bytes, source: dict) -> list[dict]:
    feed = feedparser.parse(content)
    items = []
    for e in feed.entries:
        title = e.get("title") or ""
        url = e.get("link") or ""
        if not title or not url:
            continue
        summary_src = e.get("summary") or ""
        if e.get("content"):
            summary_src = e["content"][0].get("value") or summary_src
        it = base_item(source, e, url, title, summary_src)
        ts = entry_time(e)
        it["published"] = ts.isoformat() if ts else None
        items.append(it)
    return items


def parse_youtube(content: bytes, source: dict) -> list[dict]:
    feed = feedparser.parse(content)
    items = []
    for e in feed.entries:
        title = e.get("title") or ""
        url = e.get("link") or ""
        vid = e.get("yt_videoid")
        if not title or not url:
            continue
        desc = ""
        if e.get("media_group") is not None:
            desc = e.get("media_description") or ""
        it = base_item(source, e, url, title, desc)
        if vid:
            it["thumbnail"] = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        elif e.get("media_thumbnail"):
            it["thumbnail"] = e["media_thumbnail"][0].get("url")
        ts = entry_time(e)
        it["published"] = ts.isoformat() if ts else None
        it["kind"] = "video"
        items.append(it)
    return items


VERSION_RE = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?", re.I)
PRERELEASE_RE = re.compile(r"\b(rc|beta|alpha|pre|nightly|dev)\b|-rc|-beta|-alpha", re.I)


def is_major_release(title: str) -> bool:
    """x.0 or x.y.0 counts, patch versions and prereleases do not."""
    if PRERELEASE_RE.search(title):
        return False
    m = VERSION_RE.search(title)
    if not m:
        return False  # llama.cpp style "b1234" builds are not major
    patch = m.group(3)
    return patch is None or int(patch) == 0


def parse_github_releases(content: bytes, source: dict) -> list[dict]:
    items = []
    for it in parse_rss(content, source):
        if not is_major_release(it["title"]):
            continue
        repo = re.sub(r"https?://github\.com/", "", it["url"]).split("/releases")[0]
        it["title"] = f"{repo.split('/')[-1]} {it['title']}"
        it["kind"] = "release"
        items.append(it)
    return items


def parse_gnews(content: bytes, source: dict) -> list[dict]:
    items = []
    feed = feedparser.parse(content)
    for e in feed.entries:
        title = e.get("title") or ""
        url = e.get("link") or ""
        if not title or not url:
            continue
        publisher = ""
        src = e.get("source")
        if src is not None:
            publisher = src.get("title") or ""
        if publisher and title.endswith(" - " + publisher):
            title = title[: -len(publisher) - 3]
        it = base_item(source, e, url, title, "")
        it["summary"] = ""  # Google News descriptions are link lists
        if publisher:
            it["source"] = f"{publisher} (via Google News)"
        ts = entry_time(e)
        it["published"] = ts.isoformat() if ts else None
        items.append(it)
    return items


def fetch_feed(source: dict) -> list[dict]:
    content = http_get(source["url"]).content
    kind = source["type"]
    if kind == "youtube":
        return parse_youtube(content, source)
    if kind == "github_releases":
        return parse_github_releases(content, source)
    if kind == "gnews":
        return parse_gnews(content, source)
    return parse_rss(content, source)


def fetch_hn(source: dict, now: datetime) -> list[dict]:
    cutoff = int((now - timedelta(hours=CONFIG["window_hours"])).timestamp())
    min_points = int(source.get("min_points", 50))
    params = {
        "tags": "story",
        "hitsPerPage": 100,
        "numericFilters": f"points>{min_points},created_at_i>{cutoff}",
    }
    if source.get("query"):
        params["query"] = source["query"]
    data = http_get(source["url"], params=params).json()
    items = []
    for hit in data.get("hits", []):
        title = hit.get("title") or ""
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        if not title:
            continue
        text = title
        cat = source.get("category", "auto")
        if cat == "auto":
            cat = detect_category(text)
            if not cat:
                continue
        src = dict(source, category=cat)
        it = base_item(src, hit, url, title, hit.get("story_text") or "")
        it["hn_points"] = int(hit.get("points") or 0)
        it["kind"] = "hn"
        it["hn_url"] = f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        created = hit.get("created_at_i")
        it["published"] = datetime.fromtimestamp(created, tz=timezone.utc).isoformat() if created else None
        items.append(it)
    return items


def fetch_x(source: dict, now: datetime) -> list[dict]:
    """Placeholder. X/Twitter has no free keyless feed. Implement and register here."""
    raise NotImplementedError("type 'x' is not implemented yet")


FETCHERS = {
    "rss": lambda s, now: fetch_feed(s),
    "youtube": lambda s, now: fetch_feed(s),
    "github_releases": lambda s, now: fetch_feed(s),
    "gnews": lambda s, now: fetch_feed(s),
    "hn": fetch_hn,
    "x": fetch_x,
}


def load_sources(path: Path = SOURCES_FILE) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    sources = doc.get("sources") or []
    out = []
    for s in sources:
        if not s.get("url") or not s.get("name"):
            continue
        s.setdefault("type", "rss")
        s.setdefault("tier", 3)
        s.setdefault("category", "ai")
        out.append(s)
    return out


def fetch_all(sources: list[dict], now: datetime) -> tuple[list[dict], list[str], list[str]]:
    items, ok, failed = [], [], []
    for s in sources:
        fetcher = FETCHERS.get(s["type"])
        if not fetcher:
            log(f"[skip] {s['name']}: unknown type {s['type']}")
            failed.append(s["name"])
            continue
        try:
            got = fetcher(s, now)
            log(f"[ok]   {s['name']}: {len(got)} entries")
            items.extend(got)
            ok.append(s["name"])
        except NotImplementedError as exc:
            log(f"[skip] {s['name']}: {exc}")
        except Exception as exc:  # noqa: BLE001 - every source failure is non-fatal
            log(f"[fail] {s['name']}: {type(exc).__name__}: {str(exc)[:120]}")
            failed.append(s["name"])
    return items, ok, failed


# ---------------------------------------------------------------------------
# Crypto prices (CoinGecko, free, keyless)
# ---------------------------------------------------------------------------
COINS = [("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL")]


def fetch_crypto() -> dict | None:
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": ",".join(c for c, _ in COINS), "vs_currencies": "usd", "include_24hr_change": "true"}
    try:
        data = http_get(url, params=params).json()
    except Exception as exc:  # noqa: BLE001
        log(f"[fail] CoinGecko: {type(exc).__name__}: {str(exc)[:100]}")
        return None
    prices = []
    for cid, sym in COINS:
        row = data.get(cid) or {}
        if "usd" not in row:
            continue
        prices.append({"symbol": sym, "usd": round(float(row["usd"]), 2),
                       "change_24h": round(float(row.get("usd_24h_change") or 0.0), 2)})
    if not prices:
        return None
    log(f"[ok]   CoinGecko: {', '.join(f'{p['symbol']} {p['usd']}' for p in prices)}")
    return {"prices": prices, "updated": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# Scoring, dedupe, picking
# ---------------------------------------------------------------------------
def score_item(it: dict) -> tuple[float, list[str]]:
    text = f"{it['title']} {it.get('summary') or ''}"
    reasons = []
    score = CONFIG["tier_score"].get(int(it.get("tier", 3)), 2.0)
    for pat, val in CONFIG["boosts"].items():
        if re.search(pat, text, re.I):
            score += val
            reasons.append(f"+{val:g} {pat}")
    for pat, val in CONFIG["category_boosts"].get(it["category"], {}).items():
        if re.search(pat, text, re.I):
            score += val
            reasons.append(f"+{val:g} {pat}")
    for pat, val in CONFIG["penalties"].items():
        if re.search(pat, text, re.I):
            score -= val
            reasons.append(f"-{val:g} {pat}")
    if it.get("hn_points"):
        bonus = min(it["hn_points"] / CONFIG["hn_points_divisor"], CONFIG["hn_points_cap"])
        score += bonus
        reasons.append(f"+{bonus:.1f} hn")
    extra = len(it.get("also_in") or [])
    if extra:
        bonus = CONFIG["multi_source_boost"] + CONFIG["multi_source_extra_per_source"] * (extra - 1)
        score += bonus
        reasons.append(f"+{bonus:g} in {extra + 1} sources")
    return round(score, 2), reasons


def dedupe(items: list[dict]) -> list[dict]:
    """Merge the same story across sources. Keep the best source, official first."""
    def rank(it):
        kind_rank = {"release": 0, None: 0, "video": 1, "hn": 2}.get(it.get("kind"), 0)
        return (int(it.get("tier", 3)), kind_rank, -(it.get("hn_points") or 0))

    ordered = sorted(items, key=rank)
    merged: list[dict] = []
    by_url: dict[str, dict] = {}
    for it in ordered:
        key = normalize_url(it["url"])
        target = by_url.get(key)
        if target is None:
            for m in merged:
                if m["category"] == it["category"] or it["category"] == "ai" or m["category"] == "ai":
                    if titles_similar(m["title"], it["title"]):
                        target = m
                        break
        if target is None:
            it = dict(it, also_in=list(it.get("also_in") or []))
            merged.append(it)
            by_url[key] = it
            continue
        if it["source"] != target["source"] and it["source"] not in target["also_in"]:
            target["also_in"].append(it["source"])
        target["hn_points"] = max(target.get("hn_points") or 0, it.get("hn_points") or 0)
        if it.get("hn_url") and not target.get("hn_url"):
            target["hn_url"] = it["hn_url"]
        if not target.get("summary") and it.get("summary"):
            target["summary"] = it["summary"]
        if not target.get("thumbnail") and it.get("thumbnail"):
            target["thumbnail"] = it["thumbnail"]
        if (not target.get("published")) and it.get("published"):
            target["published"] = it["published"]
    return merged


def why_label(it: dict) -> str:
    n = len(it.get("also_in") or []) + 1
    text = it["title"].lower()
    if n >= 2:
        return f"In {n} sources"
    if it.get("hn_points", 0) >= 100:
        return f"HN {it['hn_points']} points"
    if it.get("kind") == "release":
        return "Major release"
    if it.get("tier") == 1:
        if re.search(r"release|launch|introducing|now available|open[- ]weights|announc", text):
            return "Official release"
        return "Official post"
    if it["category"] == "guides":
        return "Beginner guide"
    if it["category"] == "reselling":
        return "Selling tip" if re.search(r"tip|how to|guide", text) else "Market update"
    if it["category"] == "crypto":
        return "Big market story"
    if it.get("kind") == "video":
        return "Video"
    return "Notable"


def load_seen(path: Path = SEEN_FILE) -> dict:
    if path.exists():
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            if isinstance(doc, dict) and isinstance(doc.get("items"), dict):
                return doc
        except (OSError, ValueError) as exc:
            log(f"[warn] seen.json unreadable, starting fresh: {exc}")
    return {"version": 1, "items": {}}


def prune_seen(seen: dict, today: str) -> dict:
    keep_after = (datetime.fromisoformat(today) - timedelta(days=CONFIG["seen_days"])).date().isoformat()
    seen["items"] = {k: v for k, v in seen["items"].items() if v.get("date", "") >= keep_after}
    return seen


def is_seen(it: dict, seen: dict) -> bool:
    if it["id"] in seen["items"]:
        return True
    key = normalize_url(it["url"])
    for v in seen["items"].values():
        if v.get("url") == key or titles_similar(v.get("title", ""), it["title"]):
            return True
    return False


def filter_candidates(items: list[dict], seen: dict, now: datetime) -> list[dict]:
    cutoff = now - timedelta(hours=CONFIG["window_hours"])
    out = []
    for it in items:
        if not it.get("published"):
            continue
        try:
            ts = datetime.fromisoformat(it["published"])
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff or ts > now + timedelta(hours=1):
            continue
        if it["category"] not in CATEGORIES:
            continue
        if it["category"] == "guides" and re.search(CONFIG["guides_skip"], it["title"], re.I):
            continue
        if is_seen(it, seen):
            continue
        out.append(it)
    return out


def select_picks(candidates: list[dict]) -> list[dict]:
    """Top 3, up to 5 for major news, max 2 per category, crypto max 1."""
    scored = []
    for it in dedupe(candidates):
        sc, reasons = score_item(it)
        it = dict(it, score=sc, score_reasons=reasons)
        scored.append(it)
    scored.sort(key=lambda x: (-x["score"], x.get("tier", 3), x["title"]))

    picks: list[dict] = []
    per_cat: dict[str, int] = {}
    for it in scored:
        cat = it["category"]
        cap = CONFIG["max_crypto_stories"] if cat == "crypto" else CONFIG["max_per_category"]
        if per_cat.get(cat, 0) >= cap:
            continue
        min_needed = CONFIG["crypto_min_score"] if cat == "crypto" else CONFIG["min_score"]
        if it["score"] < min_needed:
            continue
        if len(picks) >= CONFIG["base_picks"] and it["score"] < CONFIG["major_threshold"]:
            continue
        if len(picks) >= CONFIG["max_picks"]:
            break
        it["why"] = why_label(it)
        if it["category"] == "ai" and not it.get("llm"):
            it["llm"] = detect_llm(it["title"] + " " + it["source"])
        picks.append(it)
        per_cat[cat] = per_cat.get(cat, 0) + 1
    return picks


# ---------------------------------------------------------------------------
# Tips, history, output
# ---------------------------------------------------------------------------
def tip_for(date_str: str, path: Path = TIPS_FILE) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            tips = json.load(fh)
    except (OSError, ValueError) as exc:
        log(f"[warn] tips.json: {exc}")
        return None
    if not tips:
        return None
    idx = datetime.fromisoformat(date_str).toordinal() % len(tips)
    tip = tips[idx]
    if isinstance(tip, str):
        tip = {"text": tip, "tag": ""}
    return tip


def public_pick(it: dict) -> dict:
    keys = ["id", "title", "url", "source", "category", "llm", "published", "summary",
            "thumbnail", "why", "score", "also_in", "hn_points", "hn_url", "kind"]
    return {k: it.get(k) for k in keys if it.get(k) not in (None, [], 0, "")} | {"score": it.get("score")}


def write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=1)
    json.loads(text)  # never write something that does not parse back
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_days(today: str) -> list[dict]:
    """Last `week_days` day files, newest first, compact."""
    out = []
    if not DAYS.exists():
        return out
    cutoff = (datetime.fromisoformat(today) - timedelta(days=CONFIG["week_days"] - 1)).date().isoformat()
    for p in sorted(DAYS.glob("*.json"), reverse=True):
        d = p.stem
        if d < cutoff or d > today:
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"date": d, "picks": [
            {k: it.get(k) for k in ("title", "url", "source", "category", "why", "llm") if it.get(k)}
            for it in doc.get("picks", [])]})
    return out


def prune_days(today: str) -> int:
    if not DAYS.exists():
        return 0
    cutoff = (datetime.fromisoformat(today) - timedelta(days=CONFIG["history_days"])).date().isoformat()
    removed = 0
    for p in DAYS.glob("*.json"):
        if p.stem < cutoff:
            p.unlink()
            removed += 1
    return removed


def send_telegram(day: dict) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return
    lines = [f"<b>AI Daily · {day['date']}</b>"]
    for it in day["picks"]:
        lines.append(f"• <a href=\"{html.escape(it['url'], quote=True)}\">{html.escape(it['title'])}</a>"
                     f" — {html.escape(it['source'])} ({html.escape(it.get('why', ''))})")
    if not day["picks"]:
        lines.append("Quiet day, nothing big.")
    if day.get("tip"):
        lines.append(f"\n💡 {html.escape(day['tip']['text'])}")
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=CONFIG["timeout"],
                          json={"chat_id": chat, "text": "\n".join(lines), "parse_mode": "HTML",
                                "disable_web_page_preview": True})
        log(f"[{'ok' if r.ok else 'fail'}] Telegram: HTTP {r.status_code}")
    except Exception as exc:  # noqa: BLE001
        log(f"[fail] Telegram: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(CONFIG["timezone"]))
    today = local.date().isoformat()
    log(f"build start {now.isoformat()} ({today} {CONFIG['timezone']}), dry_run={dry}")

    sources = load_sources()
    items, ok, failed = fetch_all(sources, now)
    crypto = fetch_crypto()
    log(f"sources ok={len(ok)} failed={len(failed)} raw_items={len(items)}")

    previous = None
    if LATEST_FILE.exists():
        try:
            previous = json.loads(LATEST_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None

    if not ok:
        log("[fail] every source failed")
        if previous is None:
            log("no previous data either; writing a minimal valid file")
            previous = {"date": today, "picks": [], "crypto": None, "week": []}
        day = dict(previous, updated=now.isoformat(), status="stale",
                   crypto=crypto or previous.get("crypto"), tip=tip_for(today))
        if not dry:
            write_json_atomic(LATEST_FILE, day)
        return 0

    seen = prune_seen(load_seen(), today)
    candidates = filter_candidates(items, seen, now)
    log(f"candidates in window and unseen: {len(candidates)}")
    picks = select_picks(candidates)
    for it in picks:
        log(f"PICK {it['score']:>5} [{it['category']}] {it['title'][:70]} <{it['source']}> {it['why']}"
            f"  ({'; '.join(it['score_reasons'])})")
    if not picks:
        log("quiet day: no pick cleared min_score")

    tip = tip_for(today)
    day_doc = {
        "date": today,
        "updated": now.isoformat(),
        "status": "ok",
        "picks": [public_pick(it) for it in picks],
        "crypto": crypto or (previous or {}).get("crypto"),
        "tip": tip,
        "sources_ok": len(ok),
        "sources_failed": failed,
    }
    if dry:
        print(json.dumps(day_doc, ensure_ascii=False, indent=1))
        return 0

    for it in picks:
        seen["items"][it["id"]] = {"date": today, "url": normalize_url(it["url"]), "title": it["title"]}
    write_json_atomic(DAYS / f"{today}.json", day_doc)
    write_json_atomic(SEEN_FILE, seen)
    removed = prune_days(today)
    latest = dict(day_doc, week=load_days(today))
    write_json_atomic(LATEST_FILE, latest)
    log(f"wrote {LATEST_FILE.relative_to(ROOT)} with {len(picks)} picks, pruned {removed} old days")
    send_telegram(day_doc)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
