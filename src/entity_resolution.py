"""Assign each post / comment to a couple id (or 'general').

Deliberately simple and explainable — no ML. A text's score for a couple is the
sum of the weights of the distinct roster terms it matches (handles, full names
and ship-names weigh 2; bare first names weigh 1, since they're noisier). The
highest-scoring couple wins; a tie at the top, or no match, resolves to
'general'. Other matched couples are recorded in `mentions_multiple` so
cross-mentions stay visible (e.g. the "Zryce" bromance meme).
"""

from __future__ import annotations

import re

GENERAL = "general"

W_STRONG = 2  # handle, full (multi-word) name, ship-name
W_WEAK = 1    # bare first name


def _boundary(term: str) -> re.Pattern:
    """Case-insensitive matcher. Allows an optional leading '@' (handles) and
    forbids alphanumerics immediately adjacent so 'Sol' won't hit 'solution'
    and 'Jen' won't hit 'Jennifer'."""
    esc = re.escape(term.lower().lstrip("@"))
    return re.compile(r"(?<![a-z0-9_])@?" + esc + r"(?![a-z0-9_])", re.IGNORECASE)


def build_matchers(config: dict) -> dict[str, list[tuple[re.Pattern, int, str]]]:
    """couple_id -> list of (regex, weight, term). Built from structured roster
    fields, then any extra `search_terms` not already covered."""
    matchers: dict[str, list[tuple[re.Pattern, int, str]]] = {}
    for couple in config["couples"]:
        terms: dict[str, int] = {}  # term(lower) -> weight (keep the strongest)

        def add(term: str, weight: int):
            t = (term or "").strip()
            if not t:
                return
            key = t.lower().lstrip("@")
            terms[key] = max(terms.get(key, 0), weight)

        for islander in couple["islanders"]:
            if islander.get("name"):
                add(islander["name"], W_STRONG)      # full name
            if islander.get("handle"):
                add(islander["handle"], W_STRONG)    # handle
            if islander.get("first"):
                add(islander["first"], W_WEAK)       # bare first name
        for ship in couple.get("ship_names", []):
            add(ship, W_STRONG)
        # Fold in any extra free-text search_terms (multi-word -> strong).
        for term in couple.get("search_terms", []):
            add(term, W_STRONG if " " in term.strip() else W_WEAK)

        matchers[couple["id"]] = [(_boundary(t), w, t) for t, w in terms.items()]
    return matchers


def resolve_text(text: str, matchers) -> tuple[str, list[str], dict[str, int]]:
    """Return (couple_id, mentions_multiple, scores)."""
    if not text:
        return GENERAL, [], {}
    scores: dict[str, int] = {}
    for cid, terms in matchers.items():
        s = sum(w for rx, w, _ in terms if rx.search(text))
        if s > 0:
            scores[cid] = s
    if not scores:
        return GENERAL, [], {}
    top = max(scores.values())
    winners = [cid for cid, s in scores.items() if s == top]
    matched = sorted(scores, key=lambda c: -scores[c])
    if len(winners) == 1:
        return winners[0], matched, scores
    return GENERAL, matched, scores  # ambiguous top → general (still visible via mentions)


def resolve_posts(posts: list[dict], matchers) -> None:
    """Annotate posts in place with couple_id + mentions_multiple."""
    for p in posts:
        cid, multiple, _ = resolve_text(p.get("caption", ""), matchers)
        p["couple_id"] = cid
        p["mentions_multiple"] = multiple


def resolve_comments(comments: list[dict], posts: list[dict], matchers) -> None:
    """Annotate comments in place. A comment that names a couple is assigned to
    it; an otherwise-generic comment inherits the couple of its parent video."""
    post_couple = {p["id"]: p.get("couple_id", GENERAL) for p in posts if p.get("id")}
    url_couple = {p["url"]: p.get("couple_id", GENERAL) for p in posts if p.get("url")}
    for c in comments:
        cid, multiple, _ = resolve_text(c.get("text", ""), matchers)
        if cid == GENERAL:
            inherited = post_couple.get(c.get("post_id")) or url_couple.get(c.get("post_url"))
            if inherited:
                cid = inherited
        c["couple_id"] = cid
        c["mentions_multiple"] = multiple


def summarize(posts: list[dict], comments: list[dict]) -> dict[str, dict[str, int]]:
    """Per-couple {posts, comments} counts, for logging / sanity checks."""
    out: dict[str, dict[str, int]] = {}
    for p in posts:
        out.setdefault(p["couple_id"], {"posts": 0, "comments": 0})["posts"] += 1
    for c in comments:
        out.setdefault(c["couple_id"], {"posts": 0, "comments": 0})["comments"] += 1
    return out
