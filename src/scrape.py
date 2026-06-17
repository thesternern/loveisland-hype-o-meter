"""Scrape orchestration: build discovery queries from the roster, run the
platform scraper, and persist raw output for free re-scoring.

Run via the pipeline; not usually invoked directly.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from platforms.base import ScanResult
from platforms.tiktok import TikTokScraper

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def build_queries(config: dict) -> tuple[list[str], list[str]]:
    """Couple-centric discovery queries + general hashtags.

    Queries are per couple: "<first1> <first2> Love Island", each handle, each
    ship-name. Bare first names are intentionally NOT used as standalone queries
    (too noisy); they only matter for entity-resolution matching downstream.
    """
    queries: list[str] = []
    seen: set[str] = set()

    def add(q: str):
        q = (q or "").strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            queries.append(q)

    for couple in config["couples"]:
        firsts = [i.get("first", "").strip() for i in couple["islanders"] if i.get("first")]
        if len(firsts) >= 2:
            add(f"{firsts[0]} {firsts[1]} Love Island")
        for islander in couple["islanders"]:
            if islander.get("handle"):
                add(islander["handle"])
        for ship in couple.get("ship_names", []):
            add(ship)

    hashtags = [t.lstrip("#") for t in config.get("scan", {}).get("general_tags", [])]
    return queries, hashtags


def _make_scraper(platform: str, token: str):
    if platform == "tiktok":
        from apify_client import ApifyClient
        return TikTokScraper(ApifyClient(token))
    raise ValueError(f"Unsupported platform: {platform!r} (only 'tiktok' for now)")


def estimate_cost(config: dict, dry_run: bool) -> dict:
    queries, hashtags = build_queries(config)
    scan_cfg = config["scan"]
    # Estimation needs no network/token.
    scraper = TikTokScraper(client=None) if scan_cfg.get("platform", "tiktok") == "tiktok" else None
    return scraper.estimate(len(queries) + len(hashtags), scan_cfg, dry_run=dry_run)


def run_scan(config: dict, token: str, dry_run: bool, from_run_id: str | None = None) -> ScanResult:
    scan_cfg = config["scan"]
    platform = scan_cfg.get("platform", "tiktok")
    scraper = _make_scraper(platform, token)

    if from_run_id:
        result = scraper.fetch_run(from_run_id)
    else:
        queries, hashtags = build_queries(config)
        result = scraper.scan(queries, hashtags, scan_cfg, dry_run=dry_run)
    _dump_raw(result, dry_run)
    return result


def _dump_raw(result: ScanResult, dry_run: bool) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = "dry_" if dry_run else ""
    (RAW_DIR / f"raw_{tag}posts_{ts}.json").write_text(json.dumps(result.posts, indent=2))
    (RAW_DIR / f"raw_{tag}comments_{ts}.json").write_text(json.dumps(result.comments, indent=2))
    print(f"  [scrape] raw dumped to {RAW_DIR} (ts={ts})", file=sys.stderr)


def load_latest_raw() -> ScanResult:
    """Load the most recent (non-dry) raw dump for free re-scoring."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    posts_files = sorted(RAW_DIR.glob("raw_posts_*.json"))
    comments_files = sorted(RAW_DIR.glob("raw_comments_*.json"))
    if not posts_files:
        raise FileNotFoundError("No raw dumps found. Run a scan first (--dry-run or --real).")
    posts = json.loads(posts_files[-1].read_text())
    comments = json.loads(comments_files[-1].read_text()) if comments_files else []
    print(f"  [scrape] re-scoring from {posts_files[-1].name} "
          f"({len(posts)} posts, {len(comments)} comments)", file=sys.stderr)
    return ScanResult(posts=posts, comments=comments)
