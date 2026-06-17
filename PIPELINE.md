# Pipeline internals

The pipeline is five stages, orchestrated by `src/pipeline.py`:

1. **scrape** (`src/scrape.py` + `src/platforms/tiktok.py`)
2. **entity resolution** (`src/entity_resolution.py`)
3. **sentiment** (`src/sentiment.py`)
4. **scoring** (`src/scoring.py`)
5. **render** (`src/render.py`)

```bash
python src/pipeline.py --dry-run        # tiny scan (~cents), validates the whole chain
python src/pipeline.py --real --yes     # full scan at config volumes; --yes skips the cost prompt
python src/pipeline.py --rescore        # re-run resolve→render on the most recent raw dump (free)
python src/render.py                     # re-emit artifact from existing data.json (e.g. after editing history.json)
```

---

## 1. Scrape

**Actor:** [`clockworks/tiktok-scraper`](https://apify.com/clockworks/tiktok-scraper) — one run does both keyword discovery *and* comments.

Queries are built from each couple's `search_terms` in `round.json`: `"<first1> <first2> Love Island"`, every handle, and every ship-name, de-duplicated across couples. `general_tags` are passed as `hashtags`.

Key inputs (set from `config/round.json → scan`):

| Input | Source | Note |
|---|---|---|
| `searchQueries` | built from roster | keyword discovery |
| `searchSection` | `"/video"` | search videos, not users |
| `hashtags` | `scan.general_tags` | no `#` prefix |
| `resultsPerPage` | `scan.results_per_couple` | **per query** — the main volume/budget knob |
| `commentsPerPost` | `scan.comments_per_post` | comments dominate cost |
| `videoSearchSorting` | `scan.video_search_sorting` | charged add-on |
| `videoSearchDateFilter` | `scan.date_filter` | charged add-on |
| `proxyCountryCode` | `scan.proxy_country_code` | `"US"` |

**Comment text lands in a *separate* dataset.** Each post object carries a `commentsDatasetUrl`; the scraper collects those URLs and does a **second fetch** to pull comment text, joining comments back to posts by post id / `webVideoUrl`.

Raw output is written to `data/raw/raw_posts_<ts>.json` and `data/raw/raw_comments_<ts>.json` (gitignored). Keeping raw lets you re-score for free with `--rescore`.

### Pricing (BRONZE, verified 2026-06-17)

Pay-per-result: `$0.001` actor start + `$0.003` per post + `$0.001` per comment add-on (+`$0.001` each for the date-filter and sorting add-ons). **Re-verify before each scan — clockworks changed its pricing model recently.**

### Cheaper fallback (~3.5× less)

Swap `scrape.py`'s actor calls for a two-step apidojo path:
- `apidojo/tiktok-scraper` — discovery, `$0.0003/post`, output `title/likes/comments(count)/views/channel`.
- `apidojo/tiktok-comments-scraper` — comment text, `$0.0003/comment`, has a handy `commentLanguage` field.

`maxItems` is **global** on these (not per-query), so to keep balanced per-couple volume you run discovery once per couple. Only `scrape.py` changes; everything downstream is identical.

---

## 2. Entity resolution

Assigns every post and comment to a couple `id` or `"general"`. Deliberately simple and explainable — no ML.

- A per-couple matcher is compiled from `search_terms` (word-boundary regex; handles match with or without `@`).
- A text's score for a couple = number of distinct matched terms, weighting handles / full names / ship-names **×2** and bare first names **×1** (first names are noisier).
- Highest-scoring couple wins. When several couples match, the others are recorded in `mentions_multiple` (so cross-mentions stay visible but a post is counted once for share-of-voice). Ties or no match → `"general"`.
- **"Zryce" rule:** `Zryce` (the Zach + Bryce bromance meme) is a `search_term` only on `kayda_zach` — the couple the vote is actually on. A Zryce post that also names *Bryce* records both couples in `mentions_multiple` and resolves by score, so the meme doesn't silently inflate one couple. The artifact shows a "cross-ship buzz" footnote when this is significant.

---

## 3. Sentiment

**Pass 1 — VADER (free), over all captions + comments.** Produces a compound polarity in [-1, 1] for every text. Aggregated per couple into a mean polarity and an **engagement-weighted** polarity (each text weighted by `log1p(likes)`), plus raw counts.

**Pass 2 — Claude Haiku intent, over the top ~30 comments per couple by likes.** Catches the sarcasm and stan-slang VADER misses ("they ATE" = positive; "not them winning 🙄" = negative). One JSON-in / JSON-out batch call per couple at `temperature 0`, classifying each comment as:

- `save` — wants them to stay / win
- `eliminate` — wants them gone ("send them home", "boring")
- `neutral` — on-topic, no voting intent

On a parse failure it retries once, then falls back to VADER-only for that couple (recorded in metadata). Model id `claude-haiku-4-5-20251001` (confirm the current Haiku id + pricing via the `claude-api` reference before changing). Cost is a few cents per full run.

---

## 4. Scoring

Per-couple raw signals:

- `share_of_voice` — couple's (posts + comments) ÷ total resolved
- `eng_weighted_sentiment` — from sentiment pass 1
- `velocity` — momentum (see fallback below)
- `unique_authors` — distinct author ids across posts + comments, capped at 1 per author (anti-bot)
- `net_intent` — `n_save − n_eliminate` from pass 2

Each of the four scored signals is **z-scored across the current couples**, then combined:

```
HypeScore = 100 × (0.35·z(share_of_voice) + 0.30·z(eng_weighted_sentiment)
                 + 0.20·z(velocity) + 0.15·z(unique_authors))
```

- **Tiebreaker:** within an epsilon of HypeScore, higher `net_intent` ranks higher.
- **Hate-watch demotion:** a couple in the top quartile of share-of-voice but with `net_intent < 0` *and* `eng_weighted_sentiment < 0` is pushed below the safe line and flagged `hate_watch_flag`. This guards against the classic "loud = safe" overprediction error.
- **Split:** rank descending → top `n_safe` = **SAFE**, bottom `n_vulnerable` = **VULNERABLE**, the rest neutral.

**Velocity, single-scan fallback:** with only one scan available, items are split by `createTimeISO` into a recent window (latest third of the date range) vs an older window; `velocity = recent_eng_per_item / (older_eng_per_item + ε)` (> 1 = heating up). When ≥ 2 historical scans exist, a true cross-scan delta is preferred. The method used is recorded in `meta.velocity_method`.

---

## 5. Render

`src/render.py` writes the per-couple **raw** signal values (so the browser can recompute) plus computed scores into:

- `docs/data.json` — the data file, fetched by the page when served.
- `docs/index.html` — the same JSON is also inlined between `<!--HYPE_DATA_START-->` and `<!--HYPE_DATA_END-->` markers (regex-replaced), so a downloaded single file works offline.

It also writes the round's `predicted_safe` / `predicted_vulnerable` back into `config/history.json`, and surfaces the running accuracy ("Called it: X of Y") into `meta`.

The HTML/CSS/JS shell is authored once; only the inlined data block changes per run. The page re-z-scores and re-weights entirely client-side, so the sliders are live.
