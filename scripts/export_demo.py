"""Copy a finished run into demo/ for the static site, and compare a chaos run
with the undisturbed one.

  python scripts/export_demo.py --clean-data DATA --clean-report run1.json [--clean-report run2.json] \
      [--chaos-data DATA_CHAOS --chaos-report chaos_report.json] --out demo

Copies each episode's published files (never the audio or the per-chunk files),
trims the durability reports to what the page shows, and writes compare.json:
for every episode in the chaos run, is its transcript identical, segment for
segment, to the transcript of the same episode in the clean run?
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

PUBLISH = ["notes.json", "chapters.json", "youtube-chapters.txt", "show-notes.md", "transcript.json", "transcript.srt", "transcript.vtt"]
FAULT_NAMES = {"llm": "model API calls answered with a 503", "download": "audio downloads cut off", "tool": "transcript tool calls failing"}


def faults_human(spec: str) -> str:
    parts = []
    for item in filter(None, (s.strip() for s in (spec or "").split(","))):
        name, _, rate = item.partition("=")
        parts.append(f"{float(rate) * 100:.0f}% of {FAULT_NAMES.get(name, name)} (first two attempts only)")
    return "; ".join(parts) or "none"


def trim_report(rep: dict) -> dict:
    keep = ["catalog_id", "status", "wall_clock_s", "spent_usd", "activities_scheduled", "activity_attempts",
            "retried_attempts_by_activity", "activity_final_timeouts", "workflow_task_timeouts",
            "workflow_task_failures", "ledger"]
    out = {k: rep.get(k) for k in keep}
    out["episodes"] = [{"key": e["key"], "status": e.get("status"), "error": e.get("error")} for e in rep.get("episodes", [])]
    out["retries"] = [{k: r[k] for k in ("activity", "attempt", "last_failure", "workflow")} for r in rep.get("retries", [])]
    return out


def compare(clean: Path, chaos: Path) -> dict:
    eps, total, bad = {}, 0, []
    for ep_dir in sorted((chaos / "episodes").glob("*/")):
        a, b = ep_dir / "transcript.json", clean / "episodes" / ep_dir.name / "transcript.json"
        if not a.exists() or not b.exists():
            eps[ep_dir.name] = {"identical": False, "reason": "missing transcript"}
            bad.append(ep_dir.name)
            continue
        sa, sb = json.loads(a.read_text())["segments"], json.loads(b.read_text())["segments"]
        same = sa == sb
        eps[ep_dir.name] = {"identical": same, "segments": len(sa), "clean_segments": len(sb)}
        total += len(sa)
        if not same:
            bad.append(ep_dir.name)
    return {"episodes": eps, "segments": total, "all_identical": not bad and bool(eps),
            "summary": "identical" if not bad else f"differs for {', '.join(bad)}"}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--clean-data", required=True)
    p.add_argument("--clean-report", required=True, action="append", help="one per catalog run over --clean-data")
    p.add_argument("--chaos-data")
    p.add_argument("--chaos-report")
    p.add_argument("--out", required=True)
    a = p.parse_args()
    out = Path(a.out)
    (out / "episodes").mkdir(parents=True, exist_ok=True)
    clean = Path(a.clean_data)
    for ep_dir in sorted((clean / "episodes").glob("*/")):
        if not (ep_dir / "notes.json").exists():
            continue
        dest = out / "episodes" / ep_dir.name
        dest.mkdir(exist_ok=True)
        for name in PUBLISH:
            if (ep_dir / name).exists():
                shutil.copy2(ep_dir / name, dest / name)
    reports = [json.loads(Path(r).read_text()) for r in a.clean_report]
    # The ledger is per data directory, so the last report's ledger covers every run.
    run = {"runs": [trim_report(r) for r in reports], "ledger": reports[-1].get("ledger")}
    (out / "run.json").write_text(json.dumps(run, indent=1))
    if a.chaos_report:
        rep = json.loads(Path(a.chaos_report).read_text())
        doc = trim_report(rep)
        log = rep.get("chaos") or {}
        events = log.get("events", [])
        doc.update(
            kills=log.get("kills", 0),
            server_kills=sum(1 for e in events if e.get("action") == "kill" and e.get("server_too")),
            faults=log.get("faults", ""),
            faults_human=faults_human(log.get("faults", "")),
            seed=log.get("seed"),
            events=events,
        )
        doc["wall_clock_s"] = log.get("wall_clock_s", doc["wall_clock_s"])
        (out / "chaos.json").write_text(json.dumps(doc, indent=1))
        (out / "compare.json").write_text(json.dumps(compare(clean, Path(a.chaos_data)), indent=1))
    print("exported to", out)


if __name__ == "__main__":
    main()
