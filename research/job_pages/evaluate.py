"""Score the text rules (and optionally a local model) against hand labels.

    research/job_pages/.venv/bin/python research/job_pages/evaluate.py [--llm http://127.0.0.1:8080]

Labels live in cache/labels.json (gitignored, like everything derived from real links):
{"<cache key prefix>": {"sponsorship": [...], "level": [...], "canada": bool}}.
Prints aggregates only. "risky" means the prediction rules out a labeled value, which is
what can turn into a missed ping under the #15 matching rules; "wider" is fail-open.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rules  # noqa: E402

CACHE = Path(__file__).parent / "cache"


def score(pred: set[str], gold: set[str]) -> str:
    if pred == gold:
        return "exact"
    if gold <= pred:
        return "wider"
    return "risky"


def rule_tags(rec: dict) -> dict:
    return {
        "sponsorship": rules.sponsorship(rec["locations"], rec["text"], read_ok=rec["has_description"]),
        "level": rules.level(rec["title"], rec["text"], rec["employment_type"]),
        "canada": bool(rules.in_canada(rec["locations"], rec["text"])),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", help="llama-server base URL (local only)")
    ap.add_argument("--runs", type=int, default=1, help="model samples per posting (union of answers)")
    ap.add_argument("--modes", default="full,snippets", help="model prompt modes to score: full, snippets")
    args = ap.parse_args()
    labels = json.loads((CACHE / "labels.json").read_text())
    recs = {}
    for p in CACHE.glob("*.json"):
        if p.name != "labels.json":
            recs[p.stem[:8]] = json.loads(p.read_text())
    systems = {"rules": rule_tags}
    if args.llm:
        import llm
        for mode in args.modes.split(","):
            systems[f"llm-{mode}"] = lambda rec, m=mode: llm.tags(args.llm, rec, runs=args.runs, mode=m)
            systems[f"hybrid-{mode}"] = lambda rec, m=mode: llm.hybrid(args.llm, rec, rule_tags(rec), args.runs, m)
    for name, fn in systems.items():
        if args.llm:
            llm.TIMINGS.clear()
        tally = collections.defaultdict(collections.Counter)
        secs = []
        for key, gold in sorted(labels.items()):
            t0 = time.monotonic()
            pred = fn(recs[key])
            secs.append(time.monotonic() - t0)
            for dim in ("sponsorship", "level"):
                verdict = score(set(pred[dim]), set(gold[dim]))
                tally[dim][verdict] += 1
                if verdict != "exact":
                    print(f"  {name} {dim} {key}: pred={sorted(pred[dim])} gold={sorted(gold[dim])}", file=sys.stderr)
            tally["canada"]["exact" if pred["canada"] == gold["canada"] else "risky"] += 1
        print(f"{name}: n={len(labels)}  " + "  ".join(f"{d}: {dict(c)}" for d, c in tally.items())
              + f"  sec/posting median={sorted(secs)[len(secs) // 2]:.1f} max={max(secs):.1f}", flush=True)
        if args.llm and llm.TIMINGS:
            t = llm.TIMINGS
            print(f"  prompt tokens median={sorted(x.get('prompt_n', 0) for x in t)[len(t) // 2]}"
                  f"  prompt tok/s median={sorted(x.get('prompt_per_second', 0) for x in t)[len(t) // 2]:.0f}"
                  f"  gen tok/s median={sorted(x.get('predicted_per_second', 0) for x in t)[len(t) // 2]:.1f}", flush=True)


if __name__ == "__main__":
    main()
