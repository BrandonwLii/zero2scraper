"""Score text readers on archived (or synthetic) stories. Prints aggregates only.

    .venv/bin/python bench.py run --archive DIR --engines rapidocr,vlm:qwen3-vl:4b
    .venv/bin/python bench.py truth-template --archive DIR [--n 30]

DIR has the service archive's layout (story_watch/archive.py): `<target>/<stem>.json` sidecars
next to their media, plus optional top-level files:

- `labels.jsonl` from the labeling bot, used for the per-post-type breakdown and to pick a
  varied sample for hand transcription;
- `media_text_truth.jsonl`, the hand-made reference, one object per line:
  {"media_id": "...", "text": "...", "facts": {"company": [...], "role": [...], "level": [...],
   "location": [...], "sponsorship": [...]}}
  Each fact is the wording as it appears in the story. Only items with a line here are scored.

Per-item outputs contain story text, so they go to --results (default outside the repo),
never to stdout. Run one engine per process when you want the memory numbers to be clean.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import resource
import statistics
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image
from rapidfuzz import fuzz

import engines

DEFAULT_RESULTS = Path("~/story-watch-data/media-text-research/runs").expanduser()
FACT_FIELDS = {"company": "companies", "role": "roles", "level": "levels",
               "location": "locations", "sponsorship": "work_authorization"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}


# -- data ---------------------------------------------------------------------------------


def load_items(archive: Path) -> list[dict]:
    items = []
    for sidecar in sorted(archive.glob("*/*.json")):
        try:
            doc = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        files = [sidecar.parent / f for f in doc.get("files") or [] if (sidecar.parent / f).exists()]
        image = next((f for f in files if f.suffix.lower() in IMAGE_EXTS), None)
        if not image:
            continue
        items.append({"media_id": str(doc.get("media_id")), "image": image, "has_links": bool(doc.get("links")),
                      "has_mentions": bool(doc.get("mentions"))})
    return items


def read_jsonl_last_wins(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(doc, dict) and doc.get("media_id"):
                out[str(doc["media_id"])] = doc
    return out


def item_images(item: dict) -> list[Image.Image]:
    """Stories are classified from a single still image."""
    return [Image.open(item["image"]).convert("RGB")]


# -- scoring ------------------------------------------------------------------------------


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def in_text(fact: str, text: str) -> bool:
    f, t = norm(fact), norm(text)
    if not f:
        return True
    if f in t:
        return True
    return len(f) >= 5 and fuzz.partial_ratio(f, t) >= 88


def in_list(fact: str, preds: list[str]) -> bool:
    f = norm(fact)
    for p in preds:
        q = norm(p)
        if q and (f in q or q in f and len(q) >= 0.6 * len(f) or fuzz.token_set_ratio(f, q) >= 85):
            return True
    return False


def score(truth: dict, res: engines.Result) -> dict:
    facts = truth.get("facts") or {}
    out = {"facts": 0, "text_hits": 0, "field_hits": 0, "by_field": {},
           "text_sim": fuzz.token_set_ratio(norm(truth.get("text", "")), norm(res.text))}
    for key, values in facts.items():
        for v in values:
            hit_t = in_text(v, res.text)
            hit_f = res.facts is not None and in_list(v, res.facts.get(FACT_FIELDS.get(key, key), []))
            out["facts"] += 1
            out["text_hits"] += hit_t
            out["field_hits"] += hit_f
            b = out["by_field"].setdefault(key, [0, 0, 0])
            b[0] += 1
            b[1] += hit_t
            b[2] += hit_f
    if res.facts is not None:
        truth_text = truth.get("text", "")
        out["invented_companies"] = sum(
            1 for c in res.facts.get("companies", [])
            if not in_list(c, facts.get("company", [])) and not in_text(c, truth_text))
    return out


# -- memory -------------------------------------------------------------------------------


class OllamaRSS(threading.Thread):
    """Peak RSS of the model processes our Ollama server spawned while the bench runs.

    Ollama 0.40 runs each model as a `llama-server` child of `ollama serve` (older releases used
    `ollama runner`). Only children of an `ollama serve` are counted, so another llama-server on
    the same machine doesn't leak into the number. RSS includes the memory-mapped weights."""

    def __init__(self):
        super().__init__(daemon=True)
        self.peak = 0
        self.stop = threading.Event()

    def run(self):
        import psutil

        while not self.stop.is_set():
            total = 0
            for p in psutil.process_iter(["name", "cmdline"]):
                try:
                    if p.info["name"] == "ollama" and "serve" in (p.info["cmdline"] or []):
                        total += sum(c.memory_info().rss for c in p.children(recursive=True))
                except (psutil.Error, TypeError):
                    pass
            self.peak = max(self.peak, total)
            time.sleep(0.25)


# -- commands -----------------------------------------------------------------------------


def cmd_run(args) -> None:
    items = load_items(args.archive)
    truth = read_jsonl_last_wins(args.archive / "media_text_truth.jsonl")
    labels = read_jsonl_last_wins(args.archive / "labels.jsonl")
    if args.only_truth:
        items = [i for i in items if i["media_id"] in truth]
    if args.limit:
        items = items[: args.limit]
    args.results.mkdir(parents=True, exist_ok=True)
    for spec in args.engines.split(","):
        engines.unload_all()  # cold start, and a clean memory reading
        eng = engines.build(spec)
        rss = OllamaRSS()
        rss.start()
        base_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        t0 = time.perf_counter()
        eng.load()
        load_s = time.perf_counter() - t0
        rows, lat, errors = [], [], Counter()
        for item in items:
            imgs = item_images(item)
            t = time.perf_counter()
            try:
                res = eng.read(imgs)
            except Exception as e:  # noqa: BLE001 -- a failed item is a data point
                errors[type(e).__name__] += 1
                res = engines.Result("")
            dt = time.perf_counter() - t
            lat.append(dt)
            row = {"media_id": item["media_id"], "secs": round(dt, 2), "text": res.text, "facts": res.facts}
            if item["media_id"] in truth:
                row["score"] = score(truth[item["media_id"]], res)
            rows.append(row)
        rss.stop.set()
        peak_mb = max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - base_rss, 0) / 1024 + rss.peak / 2**20
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", spec)
        with open(args.results / f"{args.archive.name}.{safe}.jsonl", "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        summary = summarize(spec, rows, truth, labels, load_s, lat, peak_mb, errors)
        with open(args.results / "summary.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"archive": args.archive.name, **summary}) + "\n")
        print(json.dumps(summary))


def summarize(spec, rows, truth, labels, load_s, lat, peak_mb, errors) -> dict:
    scored = [r for r in rows if "score" in r]
    agg = {"engine": spec, "threads": engines.THREADS or "default", "items": len(rows), "scored": len(scored),
           "load_s": round(load_s, 1), "first_s": round(lat[0], 2) if lat else None,
           "p50_s": round(statistics.median(lat), 2) if lat else None,
           "p90_s": round(sorted(lat)[max(math.ceil(0.9 * len(lat)) - 1, 0)], 2) if lat else None,
           "peak_rss_mb": round(peak_mb), "errors": dict(errors)}
    if not scored:
        return agg
    tot = sum(r["score"]["facts"] for r in scored)
    agg["facts"] = tot
    agg["text_recall"] = round(sum(r["score"]["text_hits"] for r in scored) / max(tot, 1), 3)
    if any(r["facts"] is not None for r in scored):
        agg["field_recall"] = round(sum(r["score"]["field_hits"] for r in scored) / max(tot, 1), 3)
        agg["invented_companies"] = sum(r["score"].get("invented_companies", 0) for r in scored)
    agg["text_sim"] = round(statistics.mean(r["score"]["text_sim"] for r in scored), 1)
    by_field: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    by_kind: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for r in scored:
        t = truth[r["media_id"]]
        kind = t.get("kind") or ",".join((labels.get(r["media_id"]) or {}).get("post_type") or ["unlabeled"])
        for f, (n, ht, hf) in r["score"]["by_field"].items():
            for bucket in (by_field[f], by_kind[kind]):
                bucket[0] += n
                bucket[1] += ht
                bucket[2] += hf
    agg["recall_by_field"] = {f: [n, round(ht / n, 2), round(hf / n, 2)] for f, (n, ht, hf) in sorted(by_field.items())}
    agg["recall_by_kind"] = {k: [n, round(ht / n, 2), round(hf / n, 2)] for k, (n, ht, hf) in sorted(by_kind.items())}
    return agg


def cmd_truth_template(args) -> None:
    """Pick a varied sample (round-robin over labeled post types) to transcribe."""
    items = load_items(args.archive)
    labels = read_jsonl_last_wins(args.archive / "labels.jsonl")
    done = read_jsonl_last_wins(args.archive / "media_text_truth.jsonl")
    groups: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        if it["media_id"] in done:
            continue
        pt = ",".join((labels.get(it["media_id"]) or {}).get("post_type") or ["unlabeled"])
        groups[pt].append(it)
    picked = []
    while len(picked) < args.n and any(groups.values()):
        for g in sorted(groups):
            if groups[g] and len(picked) < args.n:
                picked.append(groups[g].pop())
    out = args.archive / "media_text_truth.todo.jsonl"
    with open(out, "w", encoding="utf-8") as fh:
        for it in picked:
            fh.write(json.dumps({"media_id": it["media_id"], "file": str(it["image"]), "text": "",
                                 "facts": {k: [] for k in FACT_FIELDS}}) + "\n")
    print(f"{len(picked)} items to transcribe in {out}; when filled, append them to media_text_truth.jsonl")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--archive", type=Path, required=True)
    r.add_argument("--engines", required=True, help="comma-separated, see engines.build()")
    r.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--all", dest="only_truth", action="store_false", help="also run items without a reference")
    t = sub.add_parser("truth-template")
    t.add_argument("--archive", type=Path, required=True)
    t.add_argument("--n", type=int, default=30)
    args = ap.parse_args()
    if hasattr(args, "archive"):
        args.archive = args.archive.expanduser()
    {"run": cmd_run, "truth-template": cmd_truth_template}[args.cmd](args)


if __name__ == "__main__":
    main()
