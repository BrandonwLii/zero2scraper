"""The numbers: pings, per-dimension accuracy, confusion matrices, "no post" drops, label counts.

All functions are pure and take parallel sequences of stories and predictions.
"""

from __future__ import annotations

import statistics
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from ..pings import PingPrefs, should_ping
from ..tags import DIMENSIONS, PostType, Tags, TagValue, parse_tag_key, tag_key
from .data import EvalStory
from .runner import Prediction

MULTI = "multi"  # confusion matrix: a set with several values
NA = "n/a"  # confusion matrix: the dimension does not apply
DEFAULT_MIN_COUNT = 10
MAX_IDS = 20  # media ids listed per ping config


# -- ping configs ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PingConfig:
    name: str
    description: str
    prefs: PingPrefs


def load_ping_configs(path: Path) -> list[PingConfig]:
    """``[[config]]`` tables with ``name``, ``description``, ``ping_me`` and ``dont_ping`` (tag keys)."""
    doc = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    configs = []
    for entry in doc.get("config", []):
        name = entry["name"]
        try:
            prefs = PingPrefs.from_keys(entry.get("ping_me", []), entry.get("dont_ping", []))
        except ValueError as e:
            raise ValueError(f"ping config {name!r}: {e}") from None
        configs.append(PingConfig(name, entry.get("description", ""), prefs))
    if not configs:
        raise ValueError("no [[config]] tables in the ping config file")
    return configs


@dataclass
class PingResult:
    name: str
    description: str
    should_ping: int = 0  # true tags would ping this user
    predicted: int = 0  # predicted tags ping this user
    missed: int = 0  # should ping, predicted not to: the headline number
    extra: int = 0  # should not ping, predicted to
    missed_ids: list[str] = field(default_factory=list)
    extra_ids: list[str] = field(default_factory=list)

    @property
    def recall(self) -> Optional[float]:
        return None if not self.should_ping else (self.should_ping - self.missed) / self.should_ping


def evaluate_pings(configs: Iterable[PingConfig], stories: Sequence[EvalStory], preds: Sequence[Prediction]) -> list[PingResult]:
    """Per config: missed pings (true yes, predicted no) and extra pings (true no, predicted yes).

    Gold tags with several values (a story about several jobs) go through ``should_ping`` like any
    other tags, so they ping a user if any listed value matches.
    """
    results = []
    for cfg in configs:
        res = PingResult(cfg.name, cfg.description)
        for story, pred in zip(stories, preds):
            truth = should_ping(cfg.prefs, story.gold)
            guess = should_ping(cfg.prefs, pred.tags)
            res.should_ping += truth
            res.predicted += guess
            if truth and not guess:
                res.missed += 1
                res.missed_ids.append(story.media_id)
            elif guess and not truth:
                res.extra += 1
                res.extra_ids.append(story.media_id)
        results.append(res)
    return results


# -- per-dimension accuracy and confusion ---------------------------------------------------


@dataclass
class DimensionStats:
    n: int = 0  # stories where the label applies to this dimension
    exact: int = 0  # predicted set equals the labeled set
    covers: int = 0  # labeled set is inside the predicted set: no true value was ruled out
    predicted_na: int = 0  # tagger said the dimension does not apply
    confident: int = 0  # tagger gave exactly one value
    confident_wrong: int = 0  # ... and it left out a labeled value
    all_values: int = 0  # tagger gave every value ("no idea")
    size_sum: int = 0
    predicted_applicable_gold_na: int = 0  # labeled N/A (not in n), but the tagger gave values

    @property
    def exact_rate(self) -> Optional[float]:
        return self.exact / self.n if self.n else None

    @property
    def covers_rate(self) -> Optional[float]:
        return self.covers / self.n if self.n else None

    @property
    def mean_size(self) -> Optional[float]:
        counted = self.n - self.predicted_na
        return self.size_sum / counted if counted else None


def dimension_stats(stories: Sequence[EvalStory], preds: Sequence[Prediction]) -> dict[str, DimensionStats]:
    out = {name: DimensionStats() for name in DIMENSIONS}
    for story, pred in zip(stories, preds):
        for name, cls in DIMENSIONS.items():
            gold = story.gold.values(name)
            st = out[name]
            if gold is None:
                st.predicted_applicable_gold_na += pred.tags.values(name) is not None
                continue
            st.n += 1
            guess = pred.tags.values(name)
            if guess is None:
                st.predicted_na += 1
                continue
            st.size_sum += len(guess)
            st.exact += guess == gold
            st.covers += gold <= guess
            if len(guess) == 1:
                st.confident += 1
                st.confident_wrong += not gold <= guess
            if len(guess) == len(cls):
                st.all_values += 1
    return out


def _cell(values: Optional[frozenset[TagValue]]) -> str:
    if values is None:
        return NA
    return next(iter(values)).value if len(values) == 1 else MULTI


def confusion_matrices(stories: Sequence[EvalStory], preds: Sequence[Prediction]) -> dict[str, dict[str, Counter]]:
    """dimension -> labeled row -> Counter of predicted columns.

    Rows are the labeled single values plus ``multi`` (a story labeled with several values). Columns
    are the predicted single values plus ``multi`` (the tagger gave several, which includes "no idea")
    and ``n/a``. Only stories where the label applies to the dimension are counted.
    """
    out: dict[str, dict[str, Counter]] = {name: {} for name in DIMENSIONS}
    for story, pred in zip(stories, preds):
        for name in DIMENSIONS:
            gold = story.gold.values(name)
            if gold is None:
                continue
            out[name].setdefault(_cell(gold), Counter())[_cell(pred.tags.values(name))] += 1
    return out


def matrix_axes(dimension: str) -> tuple[list[str], list[str]]:
    values = [m.value for m in DIMENSIONS[dimension]]
    return values + [MULTI], values + [MULTI, NA]


# -- "no post" list (#13) -------------------------------------------------------------------

DropFn = Callable[[Tags], bool]


def parse_no_post(keys: Iterable[str]) -> frozenset[TagValue]:
    return frozenset(parse_tag_key(k) for k in keys if k.strip())


def stub_would_drop(no_post: frozenset[TagValue]) -> DropFn:
    """STUB until #13 exists: drop a story when a dimension is confidently a listed value.

    Follows the rules in the #13 issue (drop only when certain; never when the post type could be
    a job posting). Replace this with the real function from #13 when it lands; the report reads
    only the ``Tags -> bool`` signature.
    """

    def would_drop(tags: Tags) -> bool:
        if PostType.JOB_POSTING in tags.values("post_type"):
            return False
        for name in DIMENSIONS:
            value = tags.certain_value(name)
            if value is not None and value in no_post:
                return True
        return False

    return would_drop


@dataclass
class NoPostResult:
    stub: bool
    listed: list[str]
    dropped: int = 0
    dropped_job_postings: int = 0  # labeled as (or including) a job posting: must be 0
    job_posting_ids: list[str] = field(default_factory=list)


def evaluate_no_post(
    stories: Sequence[EvalStory], preds: Sequence[Prediction], no_post: frozenset[TagValue], drop: Optional[DropFn] = None
) -> NoPostResult:
    res = NoPostResult(stub=drop is None, listed=sorted(tag_key(v) for v in no_post))
    would_drop = drop or stub_would_drop(no_post)
    for story, pred in zip(stories, preds):
        if would_drop(pred.tags):
            res.dropped += 1
            if PostType.JOB_POSTING in story.gold.values("post_type"):
                res.dropped_job_postings += 1
                res.job_posting_ids.append(story.media_id)
    return res


# -- latency and cost -----------------------------------------------------------------------


@dataclass
class Timing:
    n: int
    cached: int
    errors: int
    mean_s: Optional[float]
    p50_s: Optional[float]
    p95_s: Optional[float]
    max_s: Optional[float]
    cost_total_usd: Optional[float]  # None when no prediction reported a cost
    cost_per_story_usd: Optional[float]
    cost_known: int


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def timing(preds: Sequence[Prediction]) -> Timing:
    times = [p.latency_s for p in preds if p.error is None]
    costs = [p.cost_usd for p in preds if p.cost_usd is not None]
    total = sum(costs) if costs else None
    return Timing(
        n=len(preds),
        cached=sum(p.cached for p in preds),
        errors=sum(p.error is not None for p in preds),
        mean_s=statistics.fmean(times) if times else None,
        p50_s=_percentile(times, 0.5) if times else None,
        p95_s=_percentile(times, 0.95) if times else None,
        max_s=max(times) if times else None,
        cost_total_usd=total,
        cost_per_story_usd=(total / len(costs)) if costs else None,
        cost_known=len(costs),
    )


# -- label counts ---------------------------------------------------------------------------


@dataclass
class LabelCounts:
    stories: int
    by_dimension: dict[str, dict[str, int]]  # dimension -> value -> stories labeled with it
    not_applicable: dict[str, int]
    rare: list[str]  # "dimension:value (n)" below min_count
    min_count: int


def label_counts(stories: Sequence[EvalStory], min_count: int = DEFAULT_MIN_COUNT) -> LabelCounts:
    by_dim: dict[str, dict[str, int]] = {}
    na: dict[str, int] = {}
    rare = []
    for name, cls in DIMENSIONS.items():
        counts = {m.value: 0 for m in cls}
        na[name] = 0
        for story in stories:
            values = story.gold.values(name)
            if values is None:
                na[name] += 1
                continue
            for v in values:
                counts[v.value] += 1
        by_dim[name] = counts
        rare += [f"{name}:{v} ({n})" for v, n in counts.items() if n < min_count]
    return LabelCounts(len(stories), by_dim, na, rare, min_count)
