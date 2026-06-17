"""Write docs/data.json, inline it into docs/index.html, update history, and
surface the running accuracy badge.

Can also run standalone (`python src/render.py`) to re-emit the artifact from an
existing data.json — useful after editing config/history.json with a real result.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
CONFIG = ROOT / "config"
DATA_JSON = DOCS / "data.json"
INDEX_HTML = DOCS / "index.html"
HISTORY = CONFIG / "history.json"

DISCLAIMER = ("Entertainment only: predictions come from public TikTok buzz + sentiment, "
              "NOT real vote data. Social volume skews younger and more online than the actual "
              "voting base, and can be brigaded. A fun model, not a poll.")

_MARKER_RE = re.compile(
    r"(<!--HYPE_DATA_START-->)(.*?)(<!--HYPE_DATA_END-->)", re.DOTALL)


def _load_history() -> dict:
    if HISTORY.exists():
        return json.loads(HISTORY.read_text())
    return {"rounds": []}


def _update_history(history: dict, config: dict, rows: list[dict]) -> dict:
    """Write current-round predictions; compute correctness on any scored round."""
    vote = config["vote"]
    pred_safe = [r["id"] for r in rows if r["status"] == "SAFE"]
    pred_vuln = [r["id"] for r in rows if r["status"] == "VULNERABLE"]

    rounds = history.setdefault("rounds", [])
    cur = next((r for r in rounds
                if r.get("episode") == vote.get("episode")
                and r.get("results_air") == vote.get("results_air")), None)
    if cur is None:
        cur = {"vote": vote.get("title"), "episode": vote.get("episode"),
               "results_air": vote.get("results_air"), "actual_eliminated": [],
               "correct": None, "scored": False}
        rounds.append(cur)
    cur["predicted_safe"] = pred_safe
    cur["predicted_vulnerable"] = pred_vuln

    for rnd in rounds:
        if rnd.get("scored") and rnd.get("actual_eliminated") is not None:
            actual = set(rnd.get("actual_eliminated") or [])
            pv = set(rnd.get("predicted_vulnerable") or [])
            rnd["correct"] = bool(actual & pv)
    return history


def _accuracy(history: dict) -> dict:
    scored = [r for r in history.get("rounds", []) if r.get("scored")]
    hits = sum(1 for r in scored if r.get("correct"))
    label = f"Called it: {hits} of {len(scored)}" if scored else "Track record starts at the next result"
    return {"correct": hits, "scored": len(scored), "label": label}


def build_payload(config: dict, rows: list[dict], score_meta: dict,
                  scan_stats: dict, history: dict) -> dict:
    return {
        "meta": {
            "show": config.get("show"),
            "season": config.get("season"),
            "vote": config.get("vote"),
            "cut_line": config.get("cut_line"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scan": {
                "platform": config.get("scan", {}).get("platform", "tiktok"),
                "posts": scan_stats.get("posts", 0),
                "comments": scan_stats.get("comments", 0),
                "general_items": score_meta.get("general_items", 0),
                "dry_run": scan_stats.get("dry_run", False),
            },
            "velocity_method": score_meta.get("velocity_method"),
            "weights": score_meta.get("weights"),
            "accuracy": _accuracy(history),
            "disclaimer": DISCLAIMER,
        },
        "couples": rows,
    }


def render(config: dict, rows: list[dict], score_meta: dict, scan_stats: dict) -> dict:
    history = _update_history(_load_history(), config, rows)
    payload = build_payload(config, rows, score_meta, scan_stats, history)

    DOCS.mkdir(parents=True, exist_ok=True)
    DATA_JSON.write_text(json.dumps(payload, indent=2))
    HISTORY.write_text(json.dumps(history, indent=2))
    _inline_into_html(payload)

    print(f"  [render] wrote {DATA_JSON.relative_to(ROOT)} and updated history", file=sys.stderr)
    return payload


def _inline_into_html(payload: dict) -> None:
    if not INDEX_HTML.exists():
        print("  [render] NOTE: docs/index.html not built yet — wrote data.json only.", file=sys.stderr)
        return
    html = INDEX_HTML.read_text()
    if not _MARKER_RE.search(html):
        print("  [render] WARN: data markers not found in index.html — left HTML untouched.", file=sys.stderr)
        return
    block = ('<!--HYPE_DATA_START--><script id="hype-data" type="application/json">'
             + json.dumps(payload) + '</script><!--HYPE_DATA_END-->')
    INDEX_HTML.write_text(_MARKER_RE.sub(lambda m: block, html))
    print("  [render] inlined data into docs/index.html", file=sys.stderr)


def _standalone() -> None:
    """Re-emit the artifact from existing data.json (e.g. after editing history)."""
    if not DATA_JSON.exists():
        sys.exit("No docs/data.json yet — run the pipeline first.")
    payload = json.loads(DATA_JSON.read_text())
    payload["meta"]["accuracy"] = _accuracy(_load_history())
    DATA_JSON.write_text(json.dumps(payload, indent=2))
    _inline_into_html(payload)
    print("Re-rendered artifact from existing data.json.", file=sys.stderr)


if __name__ == "__main__":
    _standalone()
