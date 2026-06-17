"""Offline smoke test — exercises resolution -> VADER -> intent -> scoring ->
payload with synthetic data, so the analysis chain is validated for free before
any paid Apify scan. No network, no tokens, writes nothing.

    python tests/smoke_offline.py
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import entity_resolution as er  # noqa: E402
import render as render_mod  # noqa: E402
import scoring  # noqa: E402
import sentiment  # noqa: E402

config = json.loads((ROOT / "config" / "round.json").read_text())

POS = ["I love them so much, obsessed, cutest couple ever",
       "they ATE, absolutely adorable, vote for them to win",
       "protect them at all costs, my favorite winners",
       "so happy for them, best couple, rooting hard",
       "they deserve to win, love this pairing so much"]
NEG = ["absolutely the worst couple, terrible, I hate them",
       "so boring and fake, awful, send them home now",
       "ugh I can't stand them, dump this terrible pairing",
       "worst match ever, no chemistry, get them out, hate it",
       "they are the worst, cringe and annoying, vote them off"]


def _ts(day: int) -> str:
    return f"2026-06-{day:02d}T12:00:00.000Z"


posts, comments = [], []
pid = cid = 0


def add(couple_terms: str, n_posts: int, n_comments: int, positive: bool, day: int):
    """Create posts+comments whose text contains the couple's search terms."""
    global pid, cid
    pool = POS if positive else NEG
    for i in range(n_posts):
        pid += 1
        posts.append({
            "platform": "tiktok", "id": f"p{pid}", "url": f"https://tt/p{pid}",
            "caption": f"{couple_terms} {pool[i % len(pool)]}",
            "author_id": f"author{pid % 7}", "likes": 100 + (pid * 13) % 500,
            "views": 5000, "shares": 10, "comment_count": n_comments,
            "created_at": _ts(day), "hashtags": ["LoveIslandUSA"], "search_query": couple_terms,
        })
        for j in range(n_comments):
            cid += 1
            comments.append({
                "platform": "tiktok", "id": f"c{cid}", "post_id": f"p{pid}",
                "post_url": f"https://tt/p{pid}", "text": pool[(i + j) % len(pool)],
                "author_id": f"cauthor{cid % 23}", "likes": (cid * 7) % 90, "created_at": _ts(day),
            })


# kaniya: lots of love, recent → should be strongly SAFE
add("Aniya Harvey and KC Chandler Kaniya", 14, 6, True, 16)
# trinity_bryce: moderate positive
add("Trinity Tatum and Bryce Dettloff", 8, 4, True, 14)
# kayda_zach: positive + a Zryce cross-ship post (also names Bryce)
add("Kayda Bosse Zach Georgiou", 7, 4, True, 15)
posts.append({"platform": "tiktok", "id": "pZ", "url": "https://tt/pZ",
              "caption": "Zryce being iconic, Zach and Bryce best friendship arc",
              "author_id": "authorZ", "likes": 900, "views": 9000, "shares": 50,
              "comment_count": 0, "created_at": _ts(16), "hashtags": [], "search_query": "Zryce"})
# jen_gabe: small positive
add("Jen Terry Gabriel Vasconcelos", 4, 2, True, 13)
# kenzie_corbin: small mixed-positive
add("Kenzie Annis Corbin Mims", 4, 2, True, 13)
# melanie_sincere: HIGH volume but NEGATIVE → hate-watch, should be demoted
add("Melanie Moreno Sincere Rhea", 16, 6, False, 16)
# sol_caleb: tiny + negative → VULNERABLE
add("Sol Dean Caleb McDaniel", 3, 1, False, 12)
# a few general posts (no couple) to ensure 'general' bucket works
add("just love this season of Love Island so fun", 3, 2, True, 14)

# ---- run the chain ----
matchers = er.build_matchers(config)
er.resolve_posts(posts, matchers)
er.resolve_comments(comments, posts, matchers)
sent = sentiment.analyze(config, posts, comments, api_key=None)  # VADER fallback path
rows, meta = scoring.score(config, posts, comments, sent)
payload = render_mod.build_payload(config, rows, meta,
                                   {"posts": len(posts), "comments": len(comments), "dry_run": True},
                                   {"rounds": []})

# ---- assertions ----
failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


check(len(rows) == len(config["couples"]), f"expected {len(config['couples'])} couples, got {len(rows)}")
sov_sum = sum(r["raw"]["share_of_voice"] for r in rows)
check(abs(sov_sum - 1.0) < 1e-6, f"share_of_voice should sum to 1.0, got {sov_sum}")
ranks = sorted(r["rank"] for r in rows)
check(ranks == list(range(1, len(rows) + 1)), f"ranks not unique 1..N: {ranks}")
n_safe = sum(1 for r in rows if r["status"] == "SAFE")
n_vuln = sum(1 for r in rows if r["status"] == "VULNERABLE")
check(n_safe == config["cut_line"]["n_safe"], f"expected {config['cut_line']['n_safe']} SAFE, got {n_safe}")
check(n_vuln == config["cut_line"]["n_vulnerable"], f"expected {config['cut_line']['n_vulnerable']} VULNERABLE, got {n_vuln}")
for r in rows:
    for k, v in r["raw"].items():
        if isinstance(v, float):
            check(not math.isnan(v) and not math.isinf(v), f"{r['id']}.{k} is NaN/inf")
    check(not math.isnan(r["hype_score"]), f"{r['id']} hype_score NaN")
# JSON serializable
try:
    json.dumps(payload)
except (TypeError, ValueError) as e:
    failures.append(f"payload not JSON-serializable: {e}")
# behavioral expectations
by_id = {r["id"]: r for r in rows}
check(by_id["kaniya"]["status"] == "SAFE", "kaniya should be SAFE")
check(by_id["sol_caleb"]["status"] == "VULNERABLE", "sol_caleb should be VULNERABLE")
check(by_id["melanie_sincere"]["hate_watch_flag"] is True,
      "melanie_sincere should be flagged hate-watch (loud but disliked)")
check(by_id["melanie_sincere"]["status"] != "SAFE",
      "melanie_sincere must not be SAFE despite high volume")
# Zryce post resolves to kayda_zach, with trinity_bryce visible as cross-mention
zpost = next(p for p in posts if p["id"] == "pZ")
check(zpost["couple_id"] == "kayda_zach", f"Zryce post should resolve to kayda_zach, got {zpost['couple_id']}")
check("trinity_bryce" in zpost["mentions_multiple"], "Zryce post should record trinity_bryce as cross-mention")

# ---- report ----
print("\nLeaderboard (synthetic):")
for r in rows:
    flag = " 🔥hate-watch" if r["hate_watch_flag"] else ""
    print(f"  {r['rank']}. {r['display']:<20} {r['hype_score']:>7.2f} "
          f"[{r['status']}] SoV={r['raw']['share_of_voice']:.2f} "
          f"sent={r['raw']['eng_weighted_sentiment']:+.2f} net_intent={r['raw']['net_intent']}{flag}")
print(f"\nvelocity_method={meta['velocity_method']}, general_items={meta['general_items']}")

if failures:
    print("\n❌ SMOKE TEST FAILED:")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("\n✅ SMOKE TEST PASSED")

# Optional: dump synthetic payload to docs/data.json for artifact development.
# (Real scans overwrite this; does NOT touch history.json.)
if "--write" in sys.argv:
    payload["meta"]["scan"]["sample"] = True
    payload["meta"]["accuracy"] = {"correct": 0, "scored": 0,
                                   "label": "Sample data — run a real scan to populate"}
    out = ROOT / "docs" / "data.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"Wrote sample payload to {out}")
