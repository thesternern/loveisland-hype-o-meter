"""TikTok scraper backed by the Apify actor `clockworks/tiktok-scraper`.

One actor run does both keyword/hashtag discovery AND comment retrieval.
Important quirk: comment text is written to a SEPARATE dataset referenced by
each post's `commentsDatasetUrl`, so we do a second fetch and join by video URL.
"""

from __future__ import annotations

import re
import sys

from platforms.base import PlatformScraper, ScanResult

ACTOR_ID = "clockworks/tiktok-scraper"

# BRONZE pay-per-result pricing, verified 2026-06-17. Re-verify before scans.
PRICE_START = 0.001
PRICE_PER_POST = 0.003
PRICE_PER_COMMENT = 0.001
PRICE_ADDON = 0.001  # each: date filter, search sorting

DRY_RESULTS_PER_PAGE = 5
DRY_COMMENTS_PER_POST = 3

_DATASET_ID_RE = re.compile(r"/datasets/([^/]+)/")


def _to_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _first(d: dict, *keys, default=None):
    """Return the first present, non-None key from d."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _run_attr(run, *names):
    """Read a field from an Apify run, whether it's a dict or a pydantic object."""
    for n in names:
        if isinstance(run, dict) and run.get(n) is not None:
            return run[n]
        if hasattr(run, n) and getattr(run, n) is not None:
            return getattr(run, n)
    return None


class TikTokScraper(PlatformScraper):
    platform = "tiktok"

    def __init__(self, client):
        self.client = client  # apify_client.ApifyClient

    # ---- cost ---------------------------------------------------------------

    def estimate(self, n_sources: int, scan_cfg: dict, dry_run: bool = False) -> dict:
        rpp = DRY_RESULTS_PER_PAGE if dry_run else int(scan_cfg.get("results_per_couple", 25))
        cpp = DRY_COMMENTS_PER_POST if dry_run else int(scan_cfg.get("comments_per_post", 12))
        posts = n_sources * rpp
        comments = posts * cpp
        addons = 0.0
        if scan_cfg.get("date_filter"):
            addons += PRICE_ADDON
        if scan_cfg.get("video_search_sorting"):
            addons += PRICE_ADDON
        usd = PRICE_START + posts * PRICE_PER_POST + comments * PRICE_PER_COMMENT + addons
        breakdown = (
            f"{posts} posts x ${PRICE_PER_POST} + {comments} comments x ${PRICE_PER_COMMENT} "
            f"+ ${PRICE_START} start + ${addons:.3f} add-ons"
        )
        return {"posts": posts, "comments": comments, "usd": round(usd, 2), "breakdown": breakdown}

    # ---- scan ---------------------------------------------------------------

    def scan(self, queries, hashtags, scan_cfg, dry_run=False) -> ScanResult:
        rpp = DRY_RESULTS_PER_PAGE if dry_run else int(scan_cfg.get("results_per_couple", 25))
        cpp = DRY_COMMENTS_PER_POST if dry_run else int(scan_cfg.get("comments_per_post", 12))

        run_input = {
            "searchQueries": queries,
            "searchSection": "/video",
            "hashtags": hashtags,
            "resultsPerPage": rpp,
            "commentsPerPost": cpp,
            "topLevelCommentsPerPost": cpp,
            "maxRepliesPerComment": 0,
            "proxyCountryCode": scan_cfg.get("proxy_country_code", "US"),
        }
        if scan_cfg.get("video_search_sorting"):
            run_input["videoSearchSorting"] = scan_cfg["video_search_sorting"]
        if scan_cfg.get("date_filter"):
            run_input["videoSearchDateFilter"] = scan_cfg["date_filter"]

        print(f"  [tiktok] starting {ACTOR_ID}: {len(queries)} queries, "
              f"{len(hashtags)} hashtags, {rpp}/source, {cpp} comments/post ...", file=sys.stderr)
        run = self.client.actor(ACTOR_ID).call(run_input=run_input)
        status = _run_attr(run, "status")
        if status != "SUCCEEDED":
            raise RuntimeError(f"TikTok actor run did not succeed: {status or 'no run'}")
        run_id = _run_attr(run, "id")
        print(f"  [tiktok] run {run_id} SUCCEEDED", file=sys.stderr)
        return self._collect(_run_attr(run, "defaultDatasetId", "default_dataset_id"))

    def fetch_run(self, run_id: str) -> ScanResult:
        """Recover results from an already-completed run (no new scrape / no cost)."""
        run = self.client.run(run_id).get()
        status = _run_attr(run, "status")
        print(f"  [tiktok] recovering run {run_id} (status {status})", file=sys.stderr)
        if status != "SUCCEEDED":
            raise RuntimeError(f"Run {run_id} is not SUCCEEDED (status {status}).")
        return self._collect(_run_attr(run, "defaultDatasetId", "default_dataset_id"))

    def _collect(self, dataset_id: str) -> ScanResult:
        raw = list(self.client.dataset(dataset_id).iterate_items())
        real = [p for p in raw if p.get("id") and not p.get("error")]
        print(f"  [tiktok] {len(raw)} dataset items -> {len(real)} real posts "
              f"({len(raw) - len(real)} errors skipped)", file=sys.stderr)
        posts = [self._norm_post(p) for p in real]
        comments = self._fetch_comments(real)
        print(f"  [tiktok] {len(comments)} comments", file=sys.stderr)
        return ScanResult(posts=posts, comments=comments)

    # ---- comment retrieval (the second fetch) -------------------------------

    def _fetch_comments(self, raw_posts) -> list[dict]:
        # clockworks may put comments inline OR in a separate dataset. Handle both.
        inline: list[dict] = []
        dataset_ids: set[str] = set()
        for p in raw_posts:
            url = p.get("commentsDatasetUrl") or p.get("commentsUrl")
            if url:
                m = _DATASET_ID_RE.search(url)
                if m:
                    dataset_ids.add(m.group(1))
            for key in ("comments", "topComments", "commentsList"):
                if isinstance(p.get(key), list) and p[key]:
                    for c in p[key]:
                        if isinstance(c, dict):
                            c.setdefault("_videoWebUrl", p.get("webVideoUrl"))
                            c.setdefault("_videoId", p.get("id"))
                            inline.append(c)

        fetched: list[dict] = list(inline)
        for ds_id in dataset_ids:
            try:
                fetched.extend(self.client.dataset(ds_id).iterate_items())
            except Exception as e:  # noqa: BLE001 - log and continue, don't lose the run
                print(f"  [tiktok] WARN: could not fetch comments dataset {ds_id}: {e}", file=sys.stderr)

        # Build URL->postid map to join comments lacking an explicit post id.
        url_to_id = {p.get("webVideoUrl"): p.get("id") for p in raw_posts if p.get("webVideoUrl")}

        out, seen = [], set()
        for c in fetched:
            norm = self._norm_comment(c, url_to_id)
            if not norm["text"]:
                continue
            key = norm["id"] or (norm["post_id"], norm["text"][:40], norm["author_id"])
            if key in seen:
                continue
            seen.add(key)
            out.append(norm)
        return out

    # ---- normalizers --------------------------------------------------------

    def _norm_post(self, p: dict) -> dict:
        author = p.get("authorMeta") or {}
        return {
            "platform": self.platform,
            "id": str(_first(p, "id", "postId", default="") or ""),
            "url": _first(p, "webVideoUrl", "postPage", default="") or "",
            "caption": _first(p, "text", "title", "desc", default="") or "",
            "author_id": str(_first(author, "id", "name", default="") or ""),
            "author_name": _first(author, "nickName", "name", default="") or "",
            "likes": _to_int(_first(p, "diggCount", "likes", default=0)),
            "views": _to_int(_first(p, "playCount", "views", default=0)),
            "shares": _to_int(_first(p, "shareCount", "shares", default=0)),
            "comment_count": _to_int(_first(p, "commentCount", "comments", default=0)),
            "created_at": _first(p, "createTimeISO", "createTime", default="") or "",
            "hashtags": [h.get("name") if isinstance(h, dict) else h
                         for h in (p.get("hashtags") or []) if h],
            "search_query": _first(p, "searchQuery", "input", default="") or "",
        }

    def _norm_comment(self, c: dict, url_to_id: dict) -> dict:
        user = c.get("user") if isinstance(c.get("user"), dict) else {}
        video_url = _first(c, "videoWebUrl", "videoUrl", "postUrl", "_videoWebUrl", default="") or ""
        post_id = _first(c, "videoId", "awemeId", "aweme_id", "postId", "_videoId", default="")
        if not post_id and video_url:
            post_id = url_to_id.get(video_url, "")
        return {
            "platform": self.platform,
            "id": str(_first(c, "cid", "id", default="") or ""),
            "post_id": str(post_id or ""),
            "post_url": video_url,
            "text": _first(c, "text", "comment", default="") or "",
            "author_id": str(_first(c, "uniqueId", "uid", default="")
                             or _first(user, "uniqueId", "id", "username", default="") or ""),
            "likes": _to_int(_first(c, "diggCount", "likeCount", "likesCount", default=0)),
            "created_at": _first(c, "createTimeISO", "createTime", "createdAt", default="") or "",
        }
