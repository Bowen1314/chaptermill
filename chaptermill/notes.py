"""Validate the agent's submission against the transcript, and render outputs.

The validator is the reason the agent loop exists: a pull quote that the guest
never said is worse than no quote, so every quote must appear word for word in
the transcript. Rejections go back to the model as a tool result and it tries
again.
"""

from __future__ import annotations

import json
from typing import Any

from .transcript import Segment, fmt_ts, locate, norm_words, parse_ts

QUOTE_SNAP_S = 30.0


def chapter_limits(duration_s: float) -> tuple[int, int]:
    lo = 2 if duration_s < 600 else 3
    hi = max(lo, min(12, int(duration_s // 150) + 2))
    return lo, hi


def validate_submission(sub: Any, segments: list[Segment], duration_s: float) -> tuple[dict[str, Any] | None, list[str], int]:
    """Returns (clean, errors, corrections). clean is None when errors is non-empty."""
    errors: list[str] = []
    corrections = 0
    if not isinstance(sub, dict):
        return None, ["submit_notes arguments must be a JSON object"], 0

    summary = str(sub.get("summary") or "").strip()
    n_words = len(summary.split())
    if not 25 <= n_words <= 140:
        errors.append(f"summary has {n_words} words; write 25-140 words.")

    # chapters
    chapters_in = sub.get("chapters") or []
    clean_chapters: list[dict[str, Any]] = []
    lo, hi = chapter_limits(duration_s)
    if not isinstance(chapters_in, list) or not lo <= len(chapters_in) <= hi:
        errors.append(f"give between {lo} and {hi} chapters for a {fmt_ts(duration_s)} episode (got {len(chapters_in) if isinstance(chapters_in, list) else 0}).")
        chapters_in = chapters_in if isinstance(chapters_in, list) else []
    prev = -1.0
    for i, ch in enumerate(chapters_in, 1):
        if not isinstance(ch, dict):
            errors.append(f"chapter {i} must be an object with start and title.")
            continue
        start = parse_ts(ch.get("start"))
        title = str(ch.get("title") or "").strip()
        if start is None:
            errors.append(f"chapter {i} start {ch.get('start')!r} is not a timestamp like 12:34.")
            continue
        if i == 1 and 0 < start <= 30:
            start = 0.0
            corrections += 1
        if i == 1 and start != 0:
            errors.append("the first chapter must start at 00:00.")
        if start >= max(0.0, duration_s - 20):
            errors.append(f"chapter {i} starts at {fmt_ts(start)}, after the episode ends ({fmt_ts(duration_s)}).")
        if start <= prev:
            errors.append(f"chapter {i} ({fmt_ts(start)}) must start after chapter {i - 1}.")
        elif prev >= 0 and start - prev < 45:
            errors.append(f"chapter {i} starts only {int(start - prev)}s after the previous one; chapters must be at least 45s apart.")
        if not 2 <= len(title) <= 70:
            errors.append(f"chapter {i} title must be 2-70 characters.")
        prev = max(prev, start)
        clean_chapters.append({"start": round(start, 1), "title": title})

    # quotes
    quotes_in = sub.get("quotes") or []
    clean_quotes: list[dict[str, Any]] = []
    if not isinstance(quotes_in, list) or not 2 <= len(quotes_in) <= 4:
        errors.append("give 2-4 pull quotes.")
        quotes_in = quotes_in if isinstance(quotes_in, list) else []
    seen: set[str] = set()
    for i, q in enumerate(quotes_in, 1):
        if not isinstance(q, dict):
            errors.append(f"quote {i} must be an object with text and start.")
            continue
        text = str(q.get("text") or "").strip().strip('"“”')
        words = norm_words(text)
        if not 8 <= len(words) <= 60:
            errors.append(f"quote {i} has {len(words)} words; use 8-60 consecutive words.")
            continue
        key = " ".join(words)
        if key in seen:
            errors.append(f"quote {i} repeats another quote.")
            continue
        seen.add(key)
        hits = locate(segments, text)
        if not hits:
            errors.append(
                f"quote {i} is not in the transcript word for word: {text[:80]!r}. "
                "Copy the exact words from read_transcript or check with search_transcript."
            )
            continue
        claimed = parse_ts(q.get("start"))
        best = min(hits, key=lambda h: abs(h - claimed)) if claimed is not None else hits[0]
        if claimed is None or abs(best - claimed) > QUOTE_SNAP_S:
            corrections += 1
        clean_quotes.append({"text": text, "start": round(best, 1)})

    topics_in = sub.get("topics") or []
    topics = [str(t).strip() for t in topics_in if str(t).strip()] if isinstance(topics_in, list) else []
    if not 3 <= len(topics) <= 8 or any(len(t) > 40 for t in topics):
        errors.append("give 3-8 topics, each at most 40 characters.")

    if errors:
        return None, errors, corrections
    return {"summary": summary, "chapters": clean_chapters, "quotes": clean_quotes, "topics": topics}, [], corrections


def to_podcast_chapters(notes: dict[str, Any], title: str = "", podcast: str = "") -> dict[str, Any]:
    """Podcasting 2.0 JSON chapters (application/json+chapters)."""
    doc: dict[str, Any] = {
        "version": "1.2.0",
        "chapters": [{"startTime": float(c["start"]), "title": c["title"]} for c in notes["chapters"]],
    }
    if title:
        doc["title"] = title
    if podcast:
        doc["podcastName"] = podcast
    return doc


def to_youtube_chapters(notes: dict[str, Any]) -> str:
    return "\n".join(f"{fmt_ts(c['start'])} {c['title']}" for c in notes["chapters"])


def to_markdown(notes: dict[str, Any], title: str, page_url: str = "", attribution: str = "") -> str:
    lines = [f"# {title}", "", notes["summary"], "", "## Chapters", ""]
    lines += [f"- {fmt_ts(c['start'])} {c['title']}" for c in notes["chapters"]]
    lines += ["", "## Pull quotes", ""]
    lines += [f"> {q['text']}\n> — at {fmt_ts(q['start'])}\n" for q in notes["quotes"]]
    lines += ["## Topics", "", ", ".join(notes["topics"]), ""]
    if page_url:
        lines += [f"Episode page: {page_url}", ""]
    if attribution:
        lines += [attribution, ""]
    return "\n".join(lines)


def parse_args(raw: Any) -> Any:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {"_unparsed": str(raw)[:200]}
