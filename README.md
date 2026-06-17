# 🏝️ Love Island USA — Hype-O-Meter

A fun social-listening experiment that predicts which Love Island USA couples are **SAFE** (top of the vote) vs **VULNERABLE** (bottom) for America's couple votes — scored entirely from **public TikTok buzz + sentiment**.

It scrapes TikTok for posts and comments about each couple, measures how *much* and how *positively* fans are talking, and ranks them with a transparent **HypeScore**. The result ships as a single self-contained web page (the leaderboard) that you can re-generate every time a new vote drops.

> ⚠️ **Entertainment only — not real vote data.** Predictions come from public TikTok buzz and sentiment, which skew younger and more online than the actual Peacock voting base, and can be brigaded. This is a fun model, not a poll.

**Live artifact:** https://thesternern.github.io/loveisland-hype-o-meter/

---

## How it works

```
TikTok (Apify)  →  entity resolution  →  sentiment (VADER + Claude Haiku)  →  HypeScore  →  leaderboard
   posts +          which couple is        polarity + save/eliminate          z-scored        SAFE / VULNERABLE
   comments         each post about?        voting intent                     composite       split
```

The **HypeScore** is a weighted blend of four z-scored signals across the current couples:

```
HypeScore = 100 × (0.35·ShareOfVoice + 0.30·EngagementWeightedSentiment
                 + 0.20·Velocity + 0.15·UniqueAuthors)
```

…with a **save-vs-eliminate intent** signal as a tiebreaker, and a **hate-watch demotion** so a couple that is *loud but disliked* doesn't get falsely ranked safe. The published page lets you drag sliders to re-weight the signals and watch the ranking re-shuffle live.

See [PIPELINE.md](PIPELINE.md) for the full internals.

---

## Setup (once)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then paste your APIFY_TOKEN and ANTHROPIC_API_KEY into .env
```

`.env` is gitignored. Never commit it.

---

## Refresh for a new vote (the runbook)

Every time America votes, do this:

1. **Edit `config/round.json`** — update the `vote` block (type, title, stakes, episode, dates), the `cut_line` (`n_safe` / `n_vulnerable`), and the `couples` roster (remove dumped islanders, add new ones, keep each couple's `search_terms` accurate).
2. **Dry-run first** (a few cents) to confirm everything is wired:
   ```bash
   python src/pipeline.py --dry-run
   ```
3. **Run the real scan** (prints the estimated Apify cost and waits for confirmation):
   ```bash
   python src/pipeline.py --real --yes
   ```
4. **Publish:**
   ```bash
   git add docs/ config/ && git commit -m "Round: <vote name>" && git push
   ```
   GitHub Pages rebuilds in about a minute.
5. **After the result airs**, record it in `config/history.json` (`actual_eliminated`, `scored: true`), then re-run `python src/render.py` and push. The "Called it: X of Y" accuracy badge updates.

---

## Cost

TikTok scraping runs on your Apify subscription. The pipeline always prints an estimate and refuses to spend without `--yes`. A typical full scan (7 couples, ~25 posts each, ~12 comments per post) is **roughly $9** at Apify's BRONZE tier. A cheaper actor path (~$4) is documented in [PIPELINE.md](PIPELINE.md). The Claude Haiku sentiment pass costs pennies.

## Project layout

```
config/round.json     the editable per-vote config (roster, cut line, stakes)
config/history.json   results log → accuracy badge
src/                  the pipeline (scrape → resolve → sentiment → score → render)
docs/                 GitHub Pages root — index.html (the artifact) + data.json
data/raw/             gitignored raw scrape dumps (re-score without re-paying)
```
