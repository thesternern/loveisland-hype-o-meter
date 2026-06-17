"""Sentiment + voting-intent analysis.

PRIMARY engine: Claude Haiku reads each high-engagement comment/caption and
returns both a sentiment (-2..2) and a voting intent (save / eliminate /
neutral). It handles the sarcasm and stan slang a lexicon misses
("they ATE" = love, "not them winning" = contempt).

FALLBACK (no ANTHROPIC_API_KEY): VADER lexicon sentiment, with intent derived
from polarity. Kept only so a cloned repo runs without a key.

`analyze()` returns a per-couple dict consumed directly by scoring:
    { couple_id: {eng_weighted_sentiment, n_save, n_eliminate, n_neutral,
                  n_classified, sentiment_source} }
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import defaultdict

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
SENTIMENT_CAP = 2000   # max items sent to Haiku per scan (highest-engagement first)
BATCH_SIZE = 60
INTENT_TOP_K = 40      # per couple, for the VADER fallback intent
INTENTS = {"save", "eliminate", "neutral"}

_SYSTEM = """You analyze Love Island USA fan comments about a couple. For each item return:
- "intent": "save" (wants them to stay/win), "eliminate" (wants them gone/dumped), or "neutral" (on-topic, no voting lean).
- "sentiment": an integer -2, -1, 0, 1, or 2 = overall feeling toward the couple.
Resolve sarcasm and stan slang to TRUE feeling: "they ATE"/"mother is mothering" = +2 save;
"not them winning", "ok and?", an eye-roll, "so boring", "ick" = negative eliminate.
Output ONLY compact JSON: {"results":[{"i":0,"intent":"save","sentiment":2}, ...]}.
Include every index exactly once. No prose."""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _collect_items(config: dict, posts: list[dict], comments: list[dict]) -> list[dict]:
    ids = {c["id"] for c in config["couples"]}
    items = []
    for p in posts:
        if p.get("couple_id") in ids and (p.get("caption") or "").strip():
            items.append({"cid": p["couple_id"], "text": p["caption"], "eng": max(p.get("likes", 0), 0)})
    for c in comments:
        if c.get("couple_id") in ids and (c.get("text") or "").strip():
            items.append({"cid": c["couple_id"], "text": c["text"], "eng": max(c.get("likes", 0), 0)})
    return items


def _blank(couple_ids):
    return {cid: {"eng_weighted_sentiment": 0.0, "n_save": 0, "n_eliminate": 0,
                  "n_neutral": 0, "n_classified": 0, "sentiment_source": "none"} for cid in couple_ids}


def _aggregate(couple_ids, classified: list[dict], source: str) -> dict:
    """classified items carry cid, eng, sent01 (-1..1), intent."""
    out = _blank(couple_ids)
    num = defaultdict(float)
    den = defaultdict(float)
    for it in classified:
        cid = it["cid"]
        w = math.log1p(it["eng"])
        num[cid] += w * it["sent01"]
        den[cid] += w
        out[cid]["n_classified"] += 1
        if it["intent"] == "save":
            out[cid]["n_save"] += 1
        elif it["intent"] == "eliminate":
            out[cid]["n_eliminate"] += 1
        else:
            out[cid]["n_neutral"] += 1
    for cid in couple_ids:
        out[cid]["eng_weighted_sentiment"] = (num[cid] / den[cid]) if den[cid] > 1e-9 else 0.0
        out[cid]["sentiment_source"] = source
    return out


# ---- Haiku primary ----------------------------------------------------------

def _haiku_batch(batch: list[dict], client, model: str):
    payload = [{"i": i, "text": (it["text"] or "")[:300]} for i, it in enumerate(batch)]
    for attempt in range(2):
        try:
            msg = client.messages.create(
                model=model, max_tokens=2000, temperature=0, system=_SYSTEM,
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            )
            m = _JSON_RE.search(msg.content[0].text)
            data = json.loads(m.group(0))
            by_i = {}
            for r in data.get("results", []):
                i = int(r["i"])
                intent = r.get("intent") if r.get("intent") in INTENTS else "neutral"
                try:
                    s = max(-2, min(2, int(r.get("sentiment", 0))))
                except (TypeError, ValueError):
                    s = 0
                by_i[i] = (intent, s / 2.0)
            if by_i:
                return by_i
        except Exception as e:  # noqa: BLE001
            print(f"  [sentiment] Haiku batch attempt {attempt + 1} failed: {e}", file=sys.stderr)
    return None


def _haiku_analyze(couple_ids, items, api_key, model):
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)

    items = sorted(items, key=lambda it: it["eng"], reverse=True)[:SENTIMENT_CAP]
    classified, fell_back = [], False
    for start in range(0, len(items), BATCH_SIZE):
        batch = items[start:start + BATCH_SIZE]
        labels = _haiku_batch(batch, client, model)
        if labels is None:  # graceful per-batch VADER fallback
            fell_back = True
            for it in batch:
                comp = _vader_compound(it["text"])
                classified.append({**it, "sent01": comp, "intent": _vader_intent(comp)})
        else:
            for i, it in enumerate(batch):
                intent, sent01 = labels.get(i, ("neutral", 0.0))
                classified.append({**it, "sent01": sent01, "intent": intent})
        print(f"  [sentiment] classified {min(start + BATCH_SIZE, len(items))}/{len(items)}", file=sys.stderr)

    source = "haiku+vader_fallback" if fell_back else f"haiku:{model}"
    return _aggregate(couple_ids, classified, source)


# ---- VADER fallback ---------------------------------------------------------

_vader = None


def _vader_compound(text: str) -> float:
    global _vader
    if _vader is None:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        _vader = SentimentIntensityAnalyzer()
    return _vader.polarity_scores(text or "")["compound"]


def _vader_intent(compound: float) -> str:
    if compound >= 0.3:
        return "save"
    if compound <= -0.3:
        return "eliminate"
    return "neutral"


def _vader_analyze(couple_ids, items):
    # sentiment over everything; intent only on the top-K per couple (keeps the
    # intent signal comparable to the Haiku path, which is engagement-capped).
    by_couple = defaultdict(list)
    for it in items:
        it = {**it, "sent01": _vader_compound(it["text"])}
        by_couple[it["cid"]].append(it)
    classified = []
    for cid, cl in by_couple.items():
        cl_sorted = sorted(cl, key=lambda it: it["eng"], reverse=True)
        topk = set(id(x) for x in cl_sorted[:INTENT_TOP_K])
        for it in cl:
            intent = _vader_intent(it["sent01"]) if id(it) in topk else "neutral"
            classified.append({**it, "intent": intent})
    return _aggregate(couple_ids, classified, "vader")


# ---- entry point ------------------------------------------------------------

def analyze(config: dict, posts: list[dict], comments: list[dict],
            api_key: str | None, model: str = DEFAULT_MODEL) -> dict:
    couple_ids = [c["id"] for c in config["couples"]]
    items = _collect_items(config, posts, comments)
    if not items:
        return _blank(couple_ids)
    if api_key:
        return _haiku_analyze(couple_ids, items, api_key, model)
    print("  [sentiment] no ANTHROPIC_API_KEY — using VADER fallback", file=sys.stderr)
    return _vader_analyze(couple_ids, items)
