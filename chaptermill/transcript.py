"""Transcript helpers (pure functions over a list of segments).

A segment is {"start": float, "end": float, "text": str}, times in seconds
from the start of the episode audio.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

Segment = dict[str, Any]

_WORD = re.compile(r"[a-z0-9']+")


def norm_words(text: str) -> list[str]:
    text = text.lower().replace("’", "'").replace("-", " ")
    return [w.strip("'") for w in _WORD.findall(text) if w.strip("'")]


def fmt_ts(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def parse_ts(value: Any) -> Optional[float]:
    """Accept 75, 75.5, "75", "01:15", "1:01:15". Returns None if unparseable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    parts = text.split(":")
    if not 2 <= len(parts) <= 3 or not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def merge_chunks(chunks: Iterable[tuple[float, list[Segment]]]) -> list[Segment]:
    """chunks: (offset_seconds, segments relative to the chunk). Returns absolute segments."""
    out: list[Segment] = []
    for offset, segs in chunks:
        for s in segs:
            text = str(s.get("text", "")).strip()
            if not text:
                continue
            out.append(
                {
                    "start": round(offset + float(s["start"]), 2),
                    "end": round(offset + float(s["end"]), 2),
                    "text": text,
                }
            )
    out.sort(key=lambda s: s["start"])
    return out


def word_count(segments: list[Segment]) -> int:
    return sum(len(norm_words(s["text"])) for s in segments)


def outline(segments: list[Segment], block_s: float = 60.0, words_per_line: int = 14) -> str:
    """One line per block: '[mm:ss] first words …'. A cheap map of the episode."""
    lines: list[str] = []
    block_start = None
    words: list[str] = []
    for s in segments:
        if block_start is None or s["start"] >= block_start + block_s:
            if block_start is not None:
                lines.append(f"[{fmt_ts(block_start)}] {' '.join(words[:words_per_line])} …")
            block_start = s["start"]
            words = []
        words.extend(s["text"].split())
    if block_start is not None:
        lines.append(f"[{fmt_ts(block_start)}] {' '.join(words[:words_per_line])} …")
    return "\n".join(lines)


def window(segments: list[Segment], start: float, end: float, max_chars: int = 9000) -> str:
    """Timestamped transcript text between start and end (seconds)."""
    if end <= start:
        end = start + 300
    lines: list[str] = []
    used = 0
    for s in segments:
        if s["end"] < start or s["start"] > end:
            continue
        line = f"[{fmt_ts(s['start'])}] {s['text']}"
        if used + len(line) > max_chars:
            lines.append(f"[truncated at {fmt_ts(s['start'])}; ask for a later window]")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) if lines else "(no speech in that range)"


def _flat(segments: list[Segment]) -> tuple[list[str], list[float]]:
    words: list[str] = []
    times: list[float] = []
    for s in segments:
        ws = norm_words(s["text"])
        if not ws:
            continue
        span = max(0.01, s["end"] - s["start"])
        for i, w in enumerate(ws):
            words.append(w)
            times.append(s["start"] + span * i / len(ws))
    return words, times


def locate(segments: list[Segment], phrase: str) -> list[float]:
    """Start times of every exact (normalised) occurrence of phrase."""
    target = norm_words(phrase)
    if not target:
        return []
    words, times = _flat(segments)
    n = len(target)
    hits: list[float] = []
    first = target[0]
    for i in range(len(words) - n + 1):
        if words[i] == first and words[i : i + n] == target:
            hits.append(times[i])
    return hits


def search(segments: list[Segment], query: str, max_hits: int = 8, context_words: int = 18) -> list[dict[str, Any]]:
    """Exact phrase hits first; if none, segments sharing the most query words."""
    words, times = _flat(segments)
    target = norm_words(query)
    if not target:
        return []
    results: list[dict[str, Any]] = []
    n = len(target)
    for i in range(len(words) - n + 1):
        if words[i : i + n] == target:
            lo = max(0, i - context_words // 3)
            hi = min(len(words), i + n + context_words)
            results.append({"start": round(times[i], 1), "ts": fmt_ts(times[i]), "exact": True, "text": " ".join(words[lo:hi])})
            if len(results) >= max_hits:
                return results
    if results:
        return results
    qset = set(target)
    scored = []
    for s in segments:
        sw = set(norm_words(s["text"]))
        overlap = len(qset & sw)
        if overlap:
            scored.append((overlap, s))
    scored.sort(key=lambda t: (-t[0], t[1]["start"]))
    for overlap, s in scored[:max_hits]:
        results.append({"start": round(s["start"], 1), "ts": fmt_ts(s["start"]), "exact": False, "text": s["text"]})
    return results


def _srt_ts(seconds: float, sep: str = ",") -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(segments: list[Segment]) -> str:
    blocks = []
    for i, s in enumerate(segments, 1):
        blocks.append(f"{i}\n{_srt_ts(s['start'])} --> {_srt_ts(s['end'])}\n{s['text']}\n")
    return "\n".join(blocks)


def to_vtt(segments: list[Segment]) -> str:
    out = ["WEBVTT", ""]
    for s in segments:
        out.append(f"{_srt_ts(s['start'], '.')} --> {_srt_ts(s['end'], '.')}")
        out.append(s["text"])
        out.append("")
    return "\n".join(out)


def chunk_bounds(duration_s: float, chunk_s: float = 300.0, min_tail_s: float = 30.0) -> list[tuple[float, float]]:
    """Split [0, duration) into (start, length) chunks; a short tail joins the previous chunk."""
    out: list[tuple[float, float]] = []
    t = 0.0
    while t < duration_s - 0.5:
        out.append((round(t, 2), round(min(chunk_s, duration_s - t), 2)))
        t += chunk_s
    if len(out) >= 2 and out[-1][1] < min_tail_s:
        start, length = out[-2]
        out[-2] = (start, round(length + out[-1][1], 2))
        out.pop()
    return out
