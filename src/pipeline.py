"""Love Island Hype-O-Meter — pipeline orchestrator.

    python src/pipeline.py --dry-run        # tiny scan (~cents), validate the chain
    python src/pipeline.py --real --yes     # full scan at config volumes
    python src/pipeline.py --rescore        # re-run resolve->render on last raw dump (free)

Scrape -> entity resolution -> sentiment (VADER + Haiku) -> HypeScore -> render.
Any real spend is quoted first and gated behind --yes (or an interactive y/N).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Make sibling modules importable when run as `python src/pipeline.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

import entity_resolution as er
import render as render_mod
import scoring
import sentiment
from scrape import estimate_cost, load_latest_raw, run_scan

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "round.json"


def _load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def _filter_since(posts, comments, since_iso):
    """Keep only items created at/after `since_iso`. Applied AFTER resolution so
    recent comments on older videos still inherit the right couple."""
    cut = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))

    def keep(it):
        s = it.get("created_at")
        if not s:
            return False
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")) >= cut
        except (ValueError, TypeError):
            return False

    return [p for p in posts if keep(p)], [c for c in comments if keep(c)]


def _confirm_spend(estimate: dict, dry_run: bool, assume_yes: bool) -> None:
    kind = "DRY-RUN" if dry_run else "REAL"
    print("\n" + "=" * 60)
    print(f"  Apify {kind} scan cost estimate")
    print(f"    ~{estimate['posts']} posts, ~{estimate['comments']} comments")
    print(f"    {estimate['breakdown']}")
    print(f"    Estimated cost: ${estimate['usd']:.2f} (BRONZE tier)")
    print("=" * 60)
    if dry_run or assume_yes:
        print(f"  Proceeding ({'dry-run' if dry_run else '--yes'}).\n")
        return
    if not sys.stdin.isatty():
        sys.exit("  Refusing to spend without --yes (non-interactive). Re-run with --real --yes.")
    resp = input("  Proceed with the real scan? [y/N] ").strip().lower()
    if resp not in ("y", "yes"):
        sys.exit("  Aborted by user.")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Love Island Hype-O-Meter pipeline")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="tiny scan to validate the chain")
    g.add_argument("--real", action="store_true", help="full scan at config volumes")
    g.add_argument("--rescore", action="store_true", help="re-score the latest raw dump (no spend)")
    ap.add_argument("--from-run", metavar="RUN_ID", help="fetch an already-completed Apify run instead of scraping (no spend)")
    ap.add_argument("--yes", action="store_true", help="skip the spend confirmation prompt")
    args = ap.parse_args()

    if not (args.dry_run or args.real or args.rescore):
        ap.error("choose one of --dry-run, --real, or --rescore")

    load_dotenv(ROOT / ".env")
    config = _load_config()
    dry_run = args.dry_run

    # 1. Acquire data (scan, recover a prior run, or reuse local raw)
    if args.rescore:
        result = load_latest_raw()
    else:
        token = os.getenv("APIFY_TOKEN")
        if not token:
            sys.exit("APIFY_TOKEN not set. Copy .env.example to .env and fill it in.")
        if args.from_run:
            print(f"  [pipeline] recovering Apify run {args.from_run} (no new spend)")
            result = run_scan(config, token, dry_run, from_run_id=args.from_run)
        else:
            _confirm_spend(estimate_cost(config, dry_run), dry_run, args.yes)
            result = run_scan(config, token, dry_run)

    posts, comments = result.posts, result.comments
    print(f"  [pipeline] {len(posts)} posts, {len(comments)} comments", file=sys.stderr)
    if not posts:
        sys.exit("  No posts returned — check queries/actor; nothing to score.")

    # 2. Entity resolution (on the full pull, so comment->couple inheritance is intact)
    matchers = er.build_matchers(config)
    er.resolve_posts(posts, matchers)
    er.resolve_comments(comments, posts, matchers)

    # 2b. Optional recency window (e.g. only buzz since the last episode aired)
    since = config.get("scan", {}).get("since")
    if since:
        before = (len(posts), len(comments))
        posts, comments = _filter_since(posts, comments, since)
        print(f"  [pipeline] recency filter since {since}: "
              f"{before[0]}->{len(posts)} posts, {before[1]}->{len(comments)} comments", file=sys.stderr)
        if not posts:
            sys.exit("  No posts remain after the recency filter. Loosen scan.since.")

    summary = er.summarize(posts, comments)
    print("  [pipeline] resolution by couple:", file=sys.stderr)
    for cid, counts in sorted(summary.items()):
        print(f"      {cid:>16}: {counts['posts']} posts, {counts['comments']} comments", file=sys.stderr)

    # 3. Sentiment + intent (Claude Haiku primary, VADER fallback)
    sent = sentiment.analyze(config, posts, comments, os.getenv("ANTHROPIC_API_KEY"))
    sources = sorted({v.get("sentiment_source", "none") for v in sent.values()})
    print(f"  [pipeline] sentiment source(s): {', '.join(sources)}", file=sys.stderr)

    # 4. Scoring
    rows, score_meta = scoring.score(config, posts, comments, sent)

    # 5. Render
    scan_stats = {"posts": len(posts), "comments": len(comments), "dry_run": dry_run,
                  "since": since, "window_label": config.get("scan", {}).get("window_label")}
    render_mod.render(config, rows, score_meta, scan_stats)

    # Console leaderboard
    print("\n  HYPE-O-METER  (HypeScore desc)")
    print("  " + "-" * 56)
    for r in rows:
        flag = "  🔥hate-watch" if r["hate_watch_flag"] else ""
        print(f"  {r['rank']}. {r['display']:<20} {r['hype_score']:>7.2f}  "
              f"[{r['status']}]{flag}")
    print()


if __name__ == "__main__":
    main()
