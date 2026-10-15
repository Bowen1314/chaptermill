"""The show-notes agent: prompt and tool schema (pure, imported by the workflow)."""

from __future__ import annotations

from typing import Any

from .transcript import fmt_ts

MAX_TURNS = 12
# In the first full run two 30+ minute episodes spent all their turns reading the
# transcript window by window and never submitted. With this many turns left the
# workflow tells the model to stop reading and submit.
WARN_AT_TURNS_LEFT = 3

SYSTEM_PROMPT = """You prepare show notes for a podcast producer. You get an episode's metadata and a one-line-per-minute outline of its transcript. Use the tools to read the parts you need, then call submit_notes once.

Rules:
- Every pull quote must be copied word for word from the transcript (read_transcript shows the exact words). Pick quotes a listener would share: opinions, surprising facts, clear explanations. Not greetings, not sponsor reads, not the show's standard intro or outro.
- Chapter starts must be real moments where the topic changes. The first chapter starts at 00:00.
- The summary says what the episode covers and who would enjoy it. No hype words, no claims that are not in the transcript.
- If submit_notes is rejected, fix exactly what the rejection says and submit again.
- Be economical: every turn re-sends the whole conversation. Read the transcript in large windows (up to 15 minutes each), not many small ones, and only search when you need to confirm exact wording."""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_transcript",
            "description": "Return the timestamped transcript between start and end (mm:ss or seconds). At most 15 minutes per call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "e.g. 00:00"},
                    "end": {"type": "string", "description": "e.g. 15:00"},
                },
                "required": ["start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_transcript",
            "description": "Find where words are said. Exact phrase matches first, otherwise the closest lines.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_notes",
            "description": "Submit the final show notes. They are checked against the transcript; you will be told what to fix if anything fails.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "25-140 words"},
                    "chapters": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"start": {"type": "string"}, "title": {"type": "string"}},
                            "required": ["start", "title"],
                        },
                    },
                    "quotes": {
                        "type": "array",
                        "description": "2-4 verbatim pull quotes, 8-60 words each",
                        "items": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}, "start": {"type": "string"}},
                            "required": ["text", "start"],
                        },
                    },
                    "topics": {"type": "array", "items": {"type": "string"}, "description": "3-8 short topics"},
                },
                "required": ["summary", "chapters", "quotes", "topics"],
            },
        },
    },
]


def initial_messages(title: str, author: str, summary: str, duration_s: float, outline: str) -> list[dict[str, Any]]:
    meta = [f"Title: {title}", f"Duration: {fmt_ts(duration_s)}"]
    if author:
        meta.append(f"Host: {author}")
    if summary:
        meta.append(f"Publisher's one-line summary: {summary}")
    user = "\n".join(meta) + "\n\nOutline (first words of each minute):\n" + outline
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


NUDGE = "Use the tools. When you have what you need, call submit_notes."


def turns_left_note(left: int) -> str:
    return (
        f"You have {left} model turns left, including this one. Stop reading and call submit_notes now "
        "with what you already know; if it is rejected you can still fix it."
    )
