"""Optional fault injection for durability demos.

CHAPTERMILL_FAULTS="llm=0.3,download=0.5,tool=0.1" makes those activities fail
on purpose, before any network call (so injected faults cost nothing). Faults
only fire on the first `max_attempt` attempts so every injected failure is
recoverable by Temporal's retry policy. Leave it empty in normal use.
"""

from __future__ import annotations

import hashlib


class InjectedFault(RuntimeError):
    pass


def parse(spec: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in (spec or "").split(","):
        if "=" in part:
            name, _, rate = part.partition("=")
            try:
                out[name.strip()] = max(0.0, min(1.0, float(rate)))
            except ValueError:
                pass
    return out


def should_fail(spec: str, kind: str, activity_id: str, attempt: int, max_attempt: int = 2) -> bool:
    rate = parse(spec).get(kind, 0.0)
    if rate <= 0 or attempt > max_attempt:
        return False
    h = hashlib.sha256(f"{kind}|{activity_id}|{attempt}".encode()).digest()
    return (int.from_bytes(h[:4], "big") / 2**32) < rate


def maybe_fail(spec: str, kind: str, activity_id: str, attempt: int, message: str) -> None:
    if should_fail(spec, kind, activity_id, attempt):
        raise InjectedFault(f"[injected fault] {message}")
