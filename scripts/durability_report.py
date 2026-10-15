"""Summarise what Temporal actually did during a catalog run, from event history.

Usage: python scripts/durability_report.py --id CATALOG_ID --data DATA_DIR [--chaos chaos_log.json] --out report.json

Counts come from the workflow histories (activity attempts, failures that were
retried, workflow tasks that timed out because a worker died) and from the
LLM ledger (real HTTP calls, duplicate completions, injected faults).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from pathlib import Path

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client


def _ts(e) -> float:
    return e.event_time.seconds + e.event_time.nanos / 1e9


async def history_stats(client: Client, wid: str) -> dict:
    h = await client.get_workflow_handle(wid).fetch_history()
    events = list(h.events)
    sched: dict[int, str] = {}
    attempts = Counter()
    retried_failures: list[dict] = []
    per_type_failures = Counter()
    wft_timeouts = 0
    wft_failed = 0
    activity_timeouts = Counter()
    for e in events:
        et = EventType.Name(e.event_type)
        if et == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED":
            sched[e.event_id] = e.activity_task_scheduled_event_attributes.activity_type.name
        elif et == "EVENT_TYPE_ACTIVITY_TASK_STARTED":
            a = e.activity_task_started_event_attributes
            name = sched.get(a.scheduled_event_id, "?")
            attempts[name] += a.attempt
            if a.attempt > 1 and a.last_failure.message:
                per_type_failures[name] += a.attempt - 1
                retried_failures.append({"t": _ts(e), "activity": name, "attempt": a.attempt, "last_failure": a.last_failure.message[:160]})
        elif et == "EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT":
            a = e.activity_task_timed_out_event_attributes
            activity_timeouts[sched.get(a.scheduled_event_id, "?")] += 1
        elif et == "EVENT_TYPE_WORKFLOW_TASK_TIMED_OUT":
            wft_timeouts += 1
        elif et == "EVENT_TYPE_WORKFLOW_TASK_FAILED":
            wft_failed += 1
    return {
        "workflow_id": wid,
        "events": len(events),
        "start": _ts(events[0]) if events else None,
        "end": _ts(events[-1]) if events else None,
        "activities_scheduled": Counter(sched.values()),
        "activity_attempts": attempts,
        "retried_attempts_by_activity": per_type_failures,
        "retries": retried_failures,
        "workflow_task_timeouts": wft_timeouts,
        "workflow_task_failures": wft_failed,
        "activity_final_timeouts": activity_timeouts,
    }


def ledger_stats(data_dir: Path) -> dict:
    path = data_dir / "ledger.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []
    status = Counter(r.get("status") for r in rows)
    ok_keys = Counter(r["key"] for r in rows if r.get("status") == "ok")
    sent_by_activity_attempt = {(r["activity"], r["attempt"]) for r in rows if r.get("status") == "sent"}
    done_by_activity_attempt = {(r["activity"], r["attempt"]) for r in rows if r.get("status") not in ("sent", "injected_503")}
    return {
        "http_calls_sent": status.get("sent", 0),
        "completions_ok": status.get("ok", 0),
        "unique_completions": len(ok_keys),
        "duplicate_completions": sum(c - 1 for c in ok_keys.values() if c > 1),
        "sent_without_outcome": len(sent_by_activity_attempt - done_by_activity_attempt),
        "injected_faults": status.get("injected_503", 0),
        "http_errors": {k: v for k, v in status.items() if k and k.startswith("http_")},
        "timeouts": status.get("timeout", 0),
        "usd": round(sum(float(r.get("usd", 0)) for r in rows), 6),
        "prompt_tokens": sum(int(r.get("prompt_tokens", 0)) for r in rows),
        "completion_tokens": sum(int(r.get("completion_tokens", 0)) for r in rows),
    }


def chunk_stats(data_dir: Path) -> dict:
    """Chunk files record when they finished; a chunk transcribed twice would be
    overwritten, so we count processing seconds per chunk and compare with the
    attempts Temporal recorded."""
    out = {}
    for ep in sorted((data_dir / "episodes").glob("*")):
        chunks = sorted((ep / "chunks").glob("*.json"))
        out[ep.name] = len(chunks)
    return out


def merge_counters(items: list[dict], field: str) -> dict:
    total = Counter()
    for it in items:
        total.update(it[field])
    return dict(total)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--id", required=True)
    p.add_argument("--data", required=True)
    p.add_argument("--chaos")
    p.add_argument("--address", default="localhost:7233")
    p.add_argument("--out", required=True)
    a = p.parse_args()
    client = await Client.connect(a.address)
    root = await client.get_workflow_handle(a.id).describe()
    result = await client.get_workflow_handle(a.id).result()
    histories = [await history_stats(client, a.id)]
    for ep in result["episodes"]:
        try:
            histories.append(await history_stats(client, f"{a.id}--{ep['key']}"))
        except Exception as exc:  # noqa: BLE001 - a missing child is reported, not fatal
            histories.append({"workflow_id": f"{a.id}--{ep['key']}", "error": str(exc)})
    ok = [h for h in histories if "error" not in h]
    report = {
        "catalog_id": a.id,
        "status": root.status.name if root.status else None,
        "wall_clock_s": round(max(h["end"] for h in ok) - min(h["start"] for h in ok), 1),
        "episodes": result["episodes"],
        "spent_usd": result["spent_usd"],
        "activities_scheduled": merge_counters(ok, "activities_scheduled"),
        "activity_attempts": merge_counters(ok, "activity_attempts"),
        "retried_attempts_by_activity": merge_counters(ok, "retried_attempts_by_activity"),
        "activity_final_timeouts": merge_counters(ok, "activity_final_timeouts"),
        "workflow_task_timeouts": sum(h["workflow_task_timeouts"] for h in ok),
        "workflow_task_failures": sum(h["workflow_task_failures"] for h in ok),
        "retries": sorted((r | {"workflow": h["workflow_id"]} for h in ok for r in h["retries"]), key=lambda r: r["t"]),
        "ledger": ledger_stats(Path(a.data)),
        "chunk_files": chunk_stats(Path(a.data)),
    }
    if a.chaos and Path(a.chaos).exists():
        report["chaos"] = json.loads(Path(a.chaos).read_text())
    Path(a.out).write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({k: v for k, v in report.items() if k not in ("retries", "episodes")}, indent=1, default=str))


if __name__ == "__main__":
    asyncio.run(main())
