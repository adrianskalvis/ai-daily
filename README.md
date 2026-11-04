# AI Daily

One page, three items a day (hard max 5). New models, major releases, big coding and
automation tool updates, a crypto price strip, reselling news with an EU/Baltics angle,
and beginner guides. Quiet days show fewer items. No filler, no AI, no API keys.

Live URL format: `https://<user>.github.io/<repo>/`
For this repo: **https://adrianskalvis.github.io/ai-daily/**

## How it works

| Piece | Job |
| --- | --- |
| `sources.yaml` | every feed, with name, url, type, category, tier (1 to 3) and optional llm tag |
| `scripts/build.py` | fetches the feeds, scores, dedupes, picks, writes `data/` |
| `data/latest.json` | what the page reads: picks, crypto prices, tip of the day, last 7 days |
| `data/days/YYYY-MM-DD.json` | one file per day, 30 days kept |
| `data/seen.json` | stories already shown, so nothing repeats within 30 days |
| `data/tips.json` | about 60 evergreen reselling, coding and automation tips, one per day |
| `index.html`, `app.js`, `style.css` | the static page, vanilla JS, no build step |
| `.github/workflows/daily.yml` | runs the build every day, commits `data/`, deploys GitHub Pages |

Picking, in short (all thresholds sit in the `CONFIG` block at the top of `scripts/build.py`):

1. Candidates: items published in the last 36 hours and not in `data/seen.json`.
2. Score: tier (6 / 4 / 2) + keyword boosts (release, launch, introducing, new model,
   now available, open weights, GA) - penalties (rumor, opinion, podcast, sponsored,
   deals, patch bumps) + Hacker News points + a boost when 2+ sources carry the same story.
3. Dedupe by normalized URL and title similarity. Official source wins, then tier, then
   original article over the HN post.
4. Top 3 above `min_score`. Items 4 and 5 only above `major_threshold`.
   Max 2 per category, max 1 crypto story.
5. Summary: feed text, HTML stripped, 160 chars max, plus a "why" label
   ("Official release", "In 3 sources", "HN 250 points", "Major release").

If a source fails it is logged and skipped. If every source fails, yesterday's data is
kept and marked `status: stale`. Files are written atomically, so the page never sees a
half-written JSON.

## Run locally (Mac)

```bash
make run
```

That installs the three dependencies, builds `data/`, and serves the site on
http://localhost:8000. Other targets: `make build`, `make serve`, `make test`.

A dry run that fetches and scores but writes nothing:

```bash
python3 scripts/build.py --dry-run
```

## Add or edit a source

Edit `sources.yaml`. One entry looks like this:

```yaml
  - name: Mistral AI
    url: https://mistral.ai/rss.xml
    type: rss            # rss | youtube | github_releases | gnews | hn | x
    category: ai         # ai | code | mac | crypto | reselling | guides | auto (hn only)
    tier: 1              # 1 official, 2 solid secondary, 3 search feed / filler
    llm: Mistral         # optional: Claude GPT Gemini Grok Llama DeepSeek Mistral Qwen Other
```

Source types:

- `rss`: any RSS or Atom feed.
- `youtube`: `https://www.youtube.com/feeds/videos.xml?channel_id=UC...`
  Find the channel id in the page source of `youtube.com/@handle` (search for `externalId`).
- `github_releases`: `https://github.com/<owner>/<repo>/releases.atom`.
  Only `x.0` and `x.y.0` releases count. Patch and pre-releases are skipped.
- `gnews`: a Google News search feed, e.g.
  `https://news.google.com/rss/search?q=Vinted&hl=en-GB&gl=GB&ceid=GB:en`.
- `hn`: Hacker News via Algolia. `min_points` filters; `category: auto` sorts stories
  into ai / code / mac / crypto by keyword, see `CATEGORY_KEYWORDS` in `build.py`.
- `x`: a stub. X has no free keyless feed. To add it later, implement `fetch_x()` in
  `scripts/build.py` (return a list of items shaped like `base_item()`) and keep the
  `FETCHERS` entry. Nothing else changes.

Test a feed quickly:

```bash
python3 -c "import feedparser,sys; f=feedparser.parse(sys.argv[1]); print(len(f.entries), f.entries[0].title)" "https://example.com/feed.xml"
```

## Change the daily time

Edit the cron line in `.github/workflows/daily.yml`. It is UTC.
`17 4 * * *` is 04:17 UTC, which is 06:17 in Riga in winter and 07:17 in summer.
Use an odd minute, the `:00` slots are crowded and start late.

GitHub pauses scheduled workflows in public repos after 60 days without a commit. The
daily `data/` commit from the workflow counts as activity, so the schedule stays alive as
long as the build keeps running.

## One-time setup on GitHub

1. Settings → Pages → Source: **GitHub Actions**.
2. Actions → `daily` → **Run workflow** once. This builds the data and publishes the site.
3. Optional Telegram digest: add repo secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
   When both exist the build sends one message with the day's picks. Otherwise it skips.

## Tests

```bash
make test
```

`tests/test_build.py` covers parsing of fixture feeds, scoring, dedupe, the seen filter,
the 3/5 cap, the per-category cap and the quiet-day case.

## Add to Home Screen (iPhone)

Open the live URL in Safari, tap Share → Add to Home Screen. The manifest and icon make it
open full screen like an app.
