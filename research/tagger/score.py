"""Score several taggers on the labelled set with #10's adjustments, using the #6 harness's metrics.

Adjustments (docs/research/tagger-comparison.md, "Method"):
- Stories whose sponsorship the label review marked **unverifiable** (closed or unreadable listing;
  ``$STORY_WATCH_DATA/reports/label-review.json``) are left out of sponsorship scoring: their
  sponsorship accuracy isn't counted, and for pings the tagger's sponsorship is replaced by the label's,
  so that dimension can neither miss nor add a ping.
- A job posting with no link labelled ``{unknown}`` means "the image doesn't say", so it is scored as
  unsure (all three values). (No story in the current set has this case.)

Prints aggregates only (no media ids). Per-story detail for failure analysis goes to the private
work dir (``$TAGGER_WORK/scores/``).

    PYTHONPATH=. .venv/bin/python score.py vlm_q2b4_i512 hybrid_q2b4_i512 ocr_rules --md
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import replace
from pathlib import Path

from story_watch.evaluation import metrics
from story_watch.evaluation.data import load_stories
from story_watch.evaluation.runner import Prediction
from story_watch.tags import DIMENSIONS, PostType, Tags

import taggers
from common import ALL, ARCHIVE, DATA, RAW, WORK

PING_CONFIGS = Path(__file__).resolve().parents[2] / "eval" / "ping_configs.toml"
REVIEW = DATA / "reports" / "label-review.json"
SINGLE = metrics.load_ping_configs(Path(__file__).resolve().with_name("ping_configs_single.toml"))


def unverifiable_ids() -> set[str]:
    try:
        review = json.loads(REVIEW.read_text(encoding="utf-8"))
    except OSError:
        return set()
    return {mid for mid, entry in review.items()
            if any(f["dimension"] == "sponsorship" and f["severity"] == "unverifiable" for f in entry.get("flags", []))}


def adjusted_stories():
    stories = load_stories(ARCHIVE).stories
    out = []
    for s in stories:
        gold = s.gold
        if (PostType.JOB_POSTING in gold.values("post_type") and not s.item.links
                and gold.values("sponsorship") == frozenset({DIMENSIONS["sponsorship"]("unknown")})):
            gold = replace(gold, sponsorship=ALL["sponsorship"])
        out.append(replace(s, gold=gold))
    return out


def _with_sponsorship(tags: Tags, gold: Tags) -> Tags:
    if tags.values("sponsorship") is None or gold.values("sponsorship") is None:
        return tags
    return replace(tags, sponsorship=gold.values("sponsorship"))


def latency_of(tagger, media_id: str):
    base = getattr(tagger, "base", tagger)
    label = getattr(base, "label", None)
    if not label:
        return None
    try:
        rec = json.loads((RAW / label / f"{media_id}.json").read_text(encoding="utf-8"))
    except OSError:
        return None
    if "duration_ms" in rec:  # claude -p
        return rec["duration_ms"] / 1000
    return rec.get("latency_s")


def score(name: str, stories, configs, skip_sponsor: set[str]):
    tagger = taggers.build(name)
    preds, scored_preds, errors, lat = [], [], 0, []
    for s in stories:
        try:
            tags = tagger.tag(s)
            err = None
        except Exception as e:  # noqa: BLE001 - fail open like the watcher
            tags, err = Tags.unsure(), type(e).__name__
            errors += 1
        preds.append(Prediction(s.media_id, tags, 0.0, error=err))
        scored = _with_sponsorship(tags, s.gold) if s.media_id in skip_sponsor else tags
        scored_preds.append(Prediction(s.media_id, scored, 0.0, error=err))
        t = latency_of(tagger, s.media_id)
        if t is not None:
            lat.append(t)
    pings = metrics.evaluate_pings(configs, stories, scored_preds)
    dims = metrics.dimension_stats(stories, preds)
    keep = [i for i, s in enumerate(stories) if s.media_id not in skip_sponsor]
    dims["sponsorship"] = metrics.dimension_stats([stories[i] for i in keep], [preds[i] for i in keep])["sponsorship"]
    no_post = metrics.evaluate_no_post(stories, preds, metrics.parse_no_post(["post_type:misc"]))
    risky = 0
    for i, (s, pr) in enumerate(zip(stories, preds)):
        for n in DIMENSIONS:
            g = s.gold.values(n)
            if g is None or (n == "sponsorship" and s.media_id in skip_sponsor):
                continue
            v = pr.tags.values(n)
            if v is None or not g <= v:
                risky += 1
                break
    single = metrics.evaluate_pings(SINGLE, stories, scored_preds)
    halves = {"dev": 0, "test": 0}
    for p in pings:
        for mid in p.missed_ids:
            idx = next(i for i, s in enumerate(stories) if s.media_id == mid)
            halves["dev" if idx % 2 == 0 else "test"] += 1
    lat.sort()
    return {
        "tagger": name,
        "stories": len(stories),
        "errors": errors,
        "missed_total": sum(p.missed for p in pings),
        "extra_total": sum(p.extra for p in pings),
        "should_total": sum(p.should_ping for p in pings),
        "missed_by_half": halves,
        "stories_ruling_out_a_true_value": risky,
        "single_missed": sum(p.missed for p in single), "single_extra": sum(p.extra for p in single), "single_should": sum(p.should_ping for p in single),
        "single_missed_nomisc": sum(p.missed for p in single if p.name != "only-post_type-misc"),
        "pings": [{"name": p.name, "should": p.should_ping, "predicted": p.predicted, "missed": p.missed, "extra": p.extra} for p in pings],
        "dims": {n: {"n": d.n, "exact": d.exact, "covers": d.covers, "confident": d.confident, "confident_wrong": d.confident_wrong,
                     "all_values": d.all_values, "said_na": d.predicted_na, "mean_size": d.mean_size} for n, d in dims.items()},
        "dropped_job_postings": no_post.dropped_job_postings,
        "latency": None if not lat else {"n": len(lat), "median": statistics.median(lat), "p95": lat[min(len(lat) - 1, int(0.95 * len(lat)))], "max": lat[-1]},
        "_private": {
            "missed": {p.name: p.missed_ids for p in pings if p.missed_ids},
            "extra": {p.name: p.extra_ids for p in pings if p.extra_ids},
            "tags": {pr.media_id: pr.tags.to_dict() for pr in preds},
        },
    }


def render(results) -> str:
    lines = ["| Tagger | Missed | Extra | Missed dev/test | 1-value users missed / extra | Stories ruling out a true value | Errors | post covers | spons covers | comp covers | role covers | level covers | mean set size (p/s/c/r/l) | Median s |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        d = r["dims"]
        cov = [f"{d[n]['covers']}/{d[n]['n']}" for n in DIMENSIONS]
        size = "/".join("-" if d[n]["mean_size"] is None else f"{d[n]['mean_size']:.2f}" for n in DIMENSIONS)
        lat = "-" if not r["latency"] else f"{r['latency']['median']:.1f}"
        lines.append(f"| {r['tagger']} | {r['missed_total']} | {r['extra_total']} | {r['missed_by_half']['dev']}/{r['missed_by_half']['test']} | {r['single_missed']} / {r['single_extra']} | {r['stories_ruling_out_a_true_value']} | {r['errors']} | "
                     + " | ".join(cov) + f" | {size} | {lat} |")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("taggers", nargs="+")
    p.add_argument("--configs", default=str(PING_CONFIGS))
    args = p.parse_args()
    stories = adjusted_stories()
    configs = metrics.load_ping_configs(Path(args.configs))
    skip = unverifiable_ids() & {s.media_id for s in stories}
    out_dir = WORK / "scores"
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    results = []
    for name in args.taggers:
        r = score(name, stories, configs, skip)
        (out_dir / f"{name}.json").write_text(json.dumps(r, indent=1), encoding="utf-8")
        results.append(r)
    print(f"{len(stories)} stories; sponsorship excluded on {len(skip)} unverifiable; should-ping total {results[0]['should_total']} over {len(configs)} configs; {results[0]['single_should']} over {len(SINGLE)} one-value users")
    print(render(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
