"""Build the report (a plain dict, JSON-friendly) and render it as Markdown.

The report holds aggregates and media ids only: no story text, links or account names.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional, Sequence

from ..tags import DIMENSIONS, TagValue
from . import metrics
from .data import LoadResult
from .metrics import PingConfig
from .runner import Prediction


def build_report(
    tagger_name: str,
    tagger_version: str,
    load: LoadResult,
    preds: Sequence[Prediction],
    configs: Sequence[PingConfig],
    no_post: frozenset[TagValue],
    min_count: int = metrics.DEFAULT_MIN_COUNT,
) -> dict[str, Any]:
    stories = load.stories
    pings = metrics.evaluate_pings(configs, stories, preds)
    matrices = metrics.confusion_matrices(stories, preds)
    return {
        "tagger": tagger_name,
        "version": tagger_version,
        "stories": len(stories),
        "skipped": dict(load.skipped),
        "pings": [
            {**asdict(p), "recall": p.recall, "missed_ids": p.missed_ids[: metrics.MAX_IDS], "extra_ids": p.extra_ids[: metrics.MAX_IDS]}
            for p in pings
        ],
        "missed_total": sum(p.missed for p in pings),
        "extra_total": sum(p.extra for p in pings),
        "dimensions": {n: {**asdict(s), "exact_rate": s.exact_rate, "covers_rate": s.covers_rate, "mean_size": s.mean_size} for n, s in metrics.dimension_stats(stories, preds).items()},
        "confusion": {n: {row: dict(cols) for row, cols in rows.items()} for n, rows in matrices.items()},
        "no_post": asdict(metrics.evaluate_no_post(stories, preds, no_post)),
        "timing": asdict(metrics.timing(preds)),
        "label_counts": asdict(metrics.label_counts(stories, min_count)),
    }


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _num(x: Optional[float], fmt: str = "{:.3f}") -> str:
    return "n/a" if x is None else fmt.format(x)


def _table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return lines + [""]


def render_markdown(report: dict[str, Any]) -> str:
    out = [f"# Tagger evaluation: {report['tagger']} ({report['version']})", ""]
    out.append(f"Labeled stories scored: **{report['stories']}**.")
    skipped = {k: v for k, v in report["skipped"].items() if v}
    if skipped:
        out.append("Left out: " + "; ".join(f"{v} {k}" for k, v in skipped.items()) + ".")
    out.append("")

    out += ["## Missed pings (headline)", "", f"**{report['missed_total']} missed** and {report['extra_total']} extra across {len(report['pings'])} configs. "
            "Missed = the true tags would ping the user and the predicted tags would not; extra is the reverse.", ""]
    rows = [(p["name"], p["should_ping"], p["predicted"], p["missed"], _pct(p["recall"]), p["extra"]) for p in report["pings"]]
    out += _table(["Config", "Should ping", "Predicted", "Missed", "Recall", "Extra"], rows)
    for p in report["pings"]:
        for label, key in (("missed", "missed_ids"), ("extra", "extra_ids")):
            if p[key]:
                more = p[label] - len(p[key])
                out.append(f"- {p['name']} {label}: " + ", ".join(p[key]) + (f" (+{more} more)" if more > 0 else ""))
    out.append("")

    out += ["## Per-dimension accuracy", "",
            "Exact = predicted set equals the label. Covers = no labeled value was ruled out (fail-open correct). "
            "Confident wrong = one value given and a labeled value is missing from it. "
            "N counts only stories whose label applies to the dimension: a story labeled N/A for it (for example "
            "sponsorship on an event) is left out of that dimension's accuracy. A tagger that says N/A where the label "
            "applies (\"Said N/A\") counts as a miss: it is neither exact nor covers. The reverse case, the tagger gave "
            "values where the label is N/A, is not scored as accuracy and is shown as \"Labeled N/A, tagger gave values\".", ""]
    rows = []
    for name, d in report["dimensions"].items():
        rows.append((name, d["n"], _pct(d["exact_rate"]), _pct(d["covers_rate"]), d["confident"], d["confident_wrong"], d["all_values"], d["predicted_na"], d["predicted_applicable_gold_na"], _num(d["mean_size"], "{:.2f}")))
    out += _table(["Dimension", "N", "Exact", "Covers", "Confident", "Confident wrong", "No idea", "Said N/A", "Labeled N/A, tagger gave values", "Mean set size"], rows)

    out += ["## Confusion matrices", "", "Rows are the labeled value, columns the predicted one. `multi` row = the story was labeled with several values (e.g. SWE and PM); `multi` column = the tagger gave several values, which includes \"no idea\" (every value); `n/a` column = the tagger said the dimension does not apply. Stories labeled N/A for the dimension are not in the matrix.", ""]
    for name in DIMENSIONS:
        rows_axis, cols_axis = metrics.matrix_axes(name)
        data = report["confusion"][name]
        out += [f"### {name}", ""]
        body = [(r, *[data.get(r, {}).get(c, 0) for c in cols_axis]) for r in rows_axis if r in data]
        out += _table(["labeled \\ predicted", *cols_axis], body) if body else ["No labeled stories.", ""]

    nop = report["no_post"]
    out += ["## Job postings the \"no post\" list would drop", ""]
    out.append(f"**{nop['dropped_job_postings']}** (must be 0). List used: {', '.join(nop['listed']) or 'empty'}; {nop['dropped']} stories would be dropped in all."
               + (" This is a stub until #13 exists." if nop["stub"] else ""))
    if nop["job_posting_ids"]:
        out.append("Dropped job postings: " + ", ".join(nop["job_posting_ids"][: metrics.MAX_IDS]))
    out.append("")

    t = report["timing"]
    out += ["## Latency and cost", ""]
    out.append(f"{t['n']} stories, {t['cached']} from cache (their latency was measured when the cache was filled), {t['errors']} failed (scored as unsure).")
    out += [""] + _table(["Mean s", "p50 s", "p95 s", "Max s"], [(_num(t["mean_s"]), _num(t["p50_s"]), _num(t["p95_s"]), _num(t["max_s"]))])
    if t["cost_known"]:
        out.append(f"Cost: ${t['cost_total_usd']:.4f} total, ${t['cost_per_story_usd']:.4f} per story ({t['cost_known']} stories reported a cost).")
    else:
        out.append("Cost: not reported by this tagger.")
    out.append("")

    lc = report["label_counts"]
    out += ["## Labeled stories per value", "", f"Values with fewer than {lc['min_count']} stories are flagged as rare. A multi-value label counts once for each value.", ""]
    rows = []
    for name, counts in lc["by_dimension"].items():
        for value, n in counts.items():
            rows.append((name, value, n, "RARE" if n < lc["min_count"] else ""))
    out += _table(["Dimension", "Value", "Stories", ""], rows)
    out.append("Not applicable: " + ", ".join(f"{n} {k}" for k, n in lc["not_applicable"].items()) + ".")
    out.append("")
    return "\n".join(out)
