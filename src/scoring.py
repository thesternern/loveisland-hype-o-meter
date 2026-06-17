"""Turn resolved + scored records into a ranked SAFE / VULNERABLE leaderboard.

HypeScore = 100 * (0.35*z(share_of_voice) + 0.30*z(eng_weighted_sentiment)
                 + 0.20*z(velocity) + 0.15*z(unique_authors))

with a net-intent tiebreaker and a hate-watch demotion (loud but disliked
couples are pushed to the bottom regardless of raw volume).
"""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np

WEIGHTS = {"sov": 0.35, "sentiment": 0.30, "velocity": 0.20, "authors": 0.15}
RECENT_FRACTION = 1.0 / 3.0  # "latest third" of the scan window counts as recent
EPS = 1e-9


def _parse_ts(s: str) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _zscores(values: list[float]) -> list[float]:
    arr = np.array(values, dtype=float)
    sd = arr.std()
    if sd < EPS:
        return [0.0] * len(values)
    return list((arr - arr.mean()) / sd)


def _recent_cutoff(all_items: list[dict]) -> float | None:
    ts = [t for t in (_parse_ts(it.get("created_at", "")) for it in all_items) if t is not None]
    if len(ts) < 2:
        return None
    lo, hi = min(ts), max(ts)
    if hi - lo < EPS:
        return None
    return hi - (hi - lo) * RECENT_FRACTION


def _velocity(items: list[dict], cutoff: float | None) -> float:
    """Momentum: how front-loaded the couple's engagement is in the recent
    window vs the neutral expectation. 1.0 = flat, >1 = heating up."""
    if cutoff is None:
        return 1.0
    recent = total = 0.0
    have_ts = False
    for it in items:
        t = _parse_ts(it.get("created_at", ""))
        if t is None:
            continue
        have_ts = True
        eng = max(it.get("likes", 0), 0) + 1  # +1 so zero-like items still count
        total += eng
        if t >= cutoff:
            recent += eng
    if not have_ts or total < EPS:
        return 1.0
    vel = (recent / total) / RECENT_FRACTION
    return float(min(max(vel, 0.0), 3.0))


def score(config: dict, posts: list[dict], comments: list[dict],
          sent: dict[str, dict]) -> tuple[list[dict], dict]:
    """`sent` is the per-couple output of sentiment.analyze (eng_weighted_sentiment
    + save/eliminate/neutral counts)."""
    couples = config["couples"]
    couple_ids = [c["id"] for c in couples]

    posts_by = {cid: [p for p in posts if p.get("couple_id") == cid] for cid in couple_ids}
    comments_by = {cid: [c for c in comments if c.get("couple_id") == cid] for cid in couple_ids}

    total_resolved = sum(len(posts_by[c]) + len(comments_by[c]) for c in couple_ids) or 1
    cutoff = _recent_cutoff(
        [p for cid in couple_ids for p in posts_by[cid]]
        + [c for cid in couple_ids for c in comments_by[cid]]
    )
    velocity_method = "intra_pull_recency" if cutoff is not None else "unavailable_flat"

    rows: list[dict] = []
    for couple in couples:
        cid = couple["id"]
        cposts, ccomments = posts_by[cid], comments_by[cid]
        items = cposts + ccomments

        authors = {it.get("author_id") for it in items if it.get("author_id")}
        s = sent.get(cid, {})
        n_save = s.get("n_save", 0)
        n_elim = s.get("n_eliminate", 0)

        rows.append({
            "id": cid,
            "display": couple["display"],
            "ship_names": couple.get("ship_names", []),
            "islanders": [i["name"] for i in couple["islanders"]],
            "raw": {
                "share_of_voice": (len(cposts) + len(ccomments)) / total_resolved,
                "eng_weighted_sentiment": s.get("eng_weighted_sentiment", 0.0),
                "velocity": _velocity(items, cutoff),
                "unique_authors": len(authors),
                "post_count": len(cposts),
                "comment_count": len(ccomments),
                "n_save": n_save,
                "n_eliminate": n_elim,
                "n_neutral": s.get("n_neutral", 0),
                "net_intent": n_save - n_elim,
                "sentiment_source": s.get("sentiment_source", "none"),
            },
        })

    # z-score the four signals across couples
    z_sov = _zscores([r["raw"]["share_of_voice"] for r in rows])
    z_sent = _zscores([r["raw"]["eng_weighted_sentiment"] for r in rows])
    z_vel = _zscores([r["raw"]["velocity"] for r in rows])
    z_auth = _zscores([r["raw"]["unique_authors"] for r in rows])

    sov_vals = [r["raw"]["share_of_voice"] for r in rows]
    sov_q75 = float(np.percentile(sov_vals, 75)) if sov_vals else 0.0

    for i, r in enumerate(rows):
        r["z"] = {"sov": z_sov[i], "sentiment": z_sent[i], "velocity": z_vel[i], "authors": z_auth[i]}
        r["hype_score"] = round(100 * (
            WEIGHTS["sov"] * z_sov[i]
            + WEIGHTS["sentiment"] * z_sent[i]
            + WEIGHTS["velocity"] * z_vel[i]
            + WEIGHTS["authors"] * z_auth[i]
        ), 2)
        # hate-watch: loud (top-quartile SoV) but disliked
        r["hate_watch_flag"] = bool(
            r["raw"]["share_of_voice"] >= sov_q75
            and r["raw"]["net_intent"] < 0
            and r["raw"]["eng_weighted_sentiment"] < 0
        )

    # Rank: non-flagged couples first, then by hype_score, then net_intent.
    rows.sort(key=lambda r: (not r["hate_watch_flag"], r["hype_score"], r["raw"]["net_intent"]),
              reverse=True)

    n = len(rows)
    n_safe = config["cut_line"]["n_safe"]
    n_vuln = config["cut_line"]["n_vulnerable"]
    for idx, r in enumerate(rows):
        r["rank"] = idx + 1
        if idx < n_safe:
            r["status"] = "SAFE"
        elif idx >= n - n_vuln:
            r["status"] = "VULNERABLE"
        else:
            r["status"] = "BUBBLE"

    meta = {
        "total_resolved_items": total_resolved,
        "general_items": (len([p for p in posts if p.get("couple_id") == "general"])
                          + len([c for c in comments if c.get("couple_id") == "general"])),
        "velocity_method": velocity_method,
        "weights": WEIGHTS,
    }
    return rows, meta
