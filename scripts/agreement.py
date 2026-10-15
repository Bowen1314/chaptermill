"""Word-level difference between our transcript and a reference transcript.

  python scripts/agreement.py --ours demo/episodes --refs /path/to/hpr-ref --out demo/agreement.json

The reference for the demo is the plain-text transcript Hacker Public Radio
publishes for each episode. How HPR produces it is not documented on the
episode pages, so the number is a *difference*, not a word error rate against
a human ground truth. Words are lower-cased and stripped of punctuation;
the distance is a plain word-level Levenshtein distance divided by the
reference length.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np

_WORD = re.compile(r"[a-z0-9']+")


def words(text: str) -> list[str]:
    return [w.strip("'") for w in _WORD.findall(text.lower()) if w.strip("'")]


def edit_distance(ref: list[str], hyp: list[str]) -> int:
    """Word Levenshtein distance, one numpy row at a time (O(len(ref)) rows)."""
    vocab: dict[str, int] = {}
    r = np.array([vocab.setdefault(w, len(vocab)) for w in ref], dtype=np.int64)
    h = np.array([vocab.setdefault(w, len(vocab)) for w in hyp], dtype=np.int64)
    m = len(h)
    idx = np.arange(m + 1, dtype=np.int64)
    prev = idx.copy()
    for i in range(1, len(r) + 1):
        sub = prev[:-1] + (h != r[i - 1])  # substitution or match
        dele = prev[1:] + 1  # ref word missing from ours
        cur = np.empty(m + 1, dtype=np.int64)
        cur[0] = i
        cur[1:] = np.minimum(sub, dele)
        # insertions: cur[j] = min_k(cur[k] + j - k), a running minimum of cur[k] - k
        cur = np.minimum.accumulate(cur - idx) + idx
        prev = cur
    return int(prev[-1])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--ours", required=True, help="folder with <key>/transcript.json")
    p.add_argument("--refs", required=True, help="folder with <key>.txt")
    p.add_argument("--out", required=True)
    a = p.parse_args()
    rows = {}
    for ref_path in sorted(Path(a.refs).glob("*.txt")):
        key = ref_path.stem
        ours = Path(a.ours) / key / "transcript.json"
        if not ours.exists():
            continue
        segs = json.loads(ours.read_text())["segments"]
        hyp = words(" ".join(s["text"] for s in segs))
        ref = words(ref_path.read_text(errors="replace"))
        d = edit_distance(ref, hyp)
        rows[key] = {"ref_words": len(ref), "our_words": len(hyp), "edits": d, "difference_pct": round(100 * d / max(1, len(ref)), 1)}
        print(key, rows[key], flush=True)
    total_ref = sum(r["ref_words"] for r in rows.values())
    total_edits = sum(r["edits"] for r in rows.values())
    out = {
        "reference": "Hacker Public Radio's published per-episode transcripts (method not documented)",
        "episodes": rows,
        "overall_difference_pct": round(100 * total_edits / max(1, total_ref), 1),
    }
    Path(a.out).write_text(json.dumps(out, indent=1))
    print("overall", out["overall_difference_pct"])


if __name__ == "__main__":
    main()
