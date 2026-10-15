"""Build the static demo site from committed run outputs.

  python scripts/build_site.py --demo demo --out _site

Reads demo/episodes/<key>/ (the files chaptermill wrote), demo/run.json (the
durability report of the clean run), and, when present, demo/chaos.json,
demo/compare.json and demo/agreement.json. Writes _site/ with the page,
a data.json the page reads, and every episode's downloadable files.
No network, no model, no key.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EPISODE_FILES = [
    ("show-notes.md", "Show notes (Markdown)"),
    ("chapters.json", "Podcasting 2.0 chapters"),
    ("youtube-chapters.txt", "YouTube chapters"),
    ("transcript.srt", "Transcript (SRT)"),
    ("transcript.vtt", "Transcript (VTT)"),
    ("notes.json", "Everything (JSON)"),
]

# Read from the vendors' public pricing pages on 2026-10-15. Hosted tools do more
# than chaptermill (editors, clips, social posts); this compares only the cost of
# getting a back catalog transcribed and written up.
PRICES = [
    {
        "name": "Castmagic Hobby",
        "price": "$19/mo, billed annually ($239/yr)",
        "allowance": "30 transcribed hours, library total",
        "url": "https://www.castmagic.io/pricing",
    },
    {
        "name": "Castmagic Team",
        "price": "$139/mo, billed annually ($1,669/yr)",
        "allowance": "400 transcribed hours, library total",
        "url": "https://www.castmagic.io/pricing",
    },
    {
        "name": "Podsqueeze Starter",
        "price": "$8.99/mo",
        "allowance": "120 minutes of podcast time per month",
        "url": "https://podsqueeze.com/pricing",
    },
    {
        "name": "Podsqueeze Agency Lite",
        "price": "$89/mo",
        "allowance": "600 minutes per month",
        "url": "https://podsqueeze.com/pricing",
    },
    {
        "name": "OpenAI Whisper API",
        "price": "$0.006/minute ($0.36 per audio hour)",
        "allowance": "transcription only, no notes (listed as estimated cost)",
        "url": "https://developers.openai.com/api/docs/pricing",
    },
]
NEBIUS_PRICE = {
    "model": "Qwen/Qwen3-235B-A22B-Instruct-2507",
    "input_per_m": 0.20,
    "output_per_m": 0.60,
    "source": "Nebius Token Factory GET /v1/models?verbose=true, read 2026-10-15",
}


def load(path: Path):
    return json.loads(path.read_text()) if path.exists() else None


def episode_entry(ep_dir: Path) -> dict:
    doc = json.loads((ep_dir / "notes.json").read_text())
    ep = doc["episode"]
    files = [{"name": n, "label": label} for n, label in EPISODE_FILES if (ep_dir / n).exists()]
    return {
        "key": ep["key"],
        "title": ep["title"],
        "page_url": ep["page_url"],
        "audio_url": ep["audio_url"],
        "published": ep["published"],
        "author": ep["author"],
        "notes": doc["notes"],
        "stats": doc["stats"],
        "attribution": doc["attribution"],
        "files": files,
    }


def run_summary(run: dict | None, episodes: list[dict]) -> dict:
    stats = [e["stats"] for e in episodes]
    audio_s = sum(s["duration_s"] for s in stats)
    out = {
        "episodes": len(episodes),
        "audio_s": round(audio_s, 1),
        "words": sum(s["words"] for s in stats),
        "transcribe_s": round(sum(s["processing_s"] for s in stats), 1),
        "turns": sum(s["turns"] for s in stats),
        "tool_calls": sum(s["tool_calls"] for s in stats),
        "rejections": sum(s["rejections"] for s in stats),
        "timestamp_corrections": sum(s["timestamp_corrections"] for s in stats),
        # what the accepted notes cost at list price (cached replies included)
        "usd_list": round(sum(s.get("usd_list", s["usd_spent"]) for s in stats), 5),
    }
    runs = (run or {}).get("runs") or []
    ledger = (run or {}).get("ledger") or {}
    # what was actually paid: every completion in the data directory's ledger,
    # including the turns of the two first attempts that ran out of turns
    out["usd"] = round(ledger.get("usd", out["usd_list"]), 5)
    out["usd_per_audio_hour"] = round(out["usd"] / (audio_s / 3600), 4) if audio_s else None
    out["prompt_tokens"] = ledger.get("prompt_tokens")
    out["completion_tokens"] = ledger.get("completion_tokens")
    out["completions"] = ledger.get("completions_ok")
    out["duplicate_completions"] = ledger.get("duplicate_completions")
    if runs:
        retried: dict[str, int] = {}
        for r in runs:
            for k, v in (r.get("retried_attempts_by_activity") or {}).items():
                retried[k] = retried.get(k, 0) + v
        out.update(
            catalog_ids=[r.get("catalog_id") for r in runs],
            wall_clock_s=round(sum(r.get("wall_clock_s") or 0 for r in runs), 1),
            workflow_task_timeouts=sum(r.get("workflow_task_timeouts") or 0 for r in runs),
            workflow_task_failures=sum(r.get("workflow_task_failures") or 0 for r in runs),
            retried_attempts_by_activity=retried,
            first_run_failed=[e["key"] for e in runs[0].get("episodes", []) if e.get("status") != "done"],
        )
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--demo", default=str(ROOT / "demo"))
    p.add_argument("--site", default=str(ROOT / "site"))
    p.add_argument("--out", default=str(ROOT / "_site"))
    a = p.parse_args()
    demo, out = Path(a.demo), Path(a.out)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(a.site, out)
    episodes = []
    for ep_dir in sorted((demo / "episodes").glob("*/"), reverse=True):
        if not (ep_dir / "notes.json").exists():
            continue
        episodes.append(episode_entry(ep_dir))
        dest = out / "episodes" / ep_dir.name
        dest.mkdir(parents=True)
        for name in [n for n, _ in EPISODE_FILES] + ["transcript.json"]:
            if (ep_dir / name).exists():
                shutil.copy2(ep_dir / name, dest / name)
    data = {
        "run": run_summary(load(demo / "run.json"), episodes),
        "episodes": episodes,
        "chaos": load(demo / "chaos.json"),
        "compare": load(demo / "compare.json"),
        "agreement": load(demo / "agreement.json"),
        "prices": PRICES,
        "nebius_price": NEBIUS_PRICE,
    }
    (out / "data.json").write_text(json.dumps(data, separators=(",", ":")))
    print(f"built {out} with {len(episodes)} episodes")


if __name__ == "__main__":
    main()
