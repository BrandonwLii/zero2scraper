"""The server-wide "no post" list (#13): stories the watcher records but never posts.

The list is edited from Discord (``/nopost``, labelers only) and stored in the state DB; the
watcher reads it every cycle. Pure and I/O-free, like ``pings.py``.

A rule is a conjunction: for each dimension it names, a set of values. "Post type = Misc" is one
rule; "Post type = Process info AND Level = Other" is another. The list is a set of rules, and a
story is dropped when any rule matches (OR across rules, AND across a rule's dimensions).

Hard safety rules (tests/test_nopost.py):

- a dimension matches only when the tagger is confident: the story's value set is a subset of the
  rule's values and is not "every value" (no idea). Not applicable (``None``) never matches;
- a story with a job link, or whose post type could be Job posting, is always posted;
- only a real model tagger's output can drop a story (coordinator decision, 2026-10-11): the
  tagger must be ``claude`` or ``hybrid`` and the tags must not be its fallback (``evidence``
  starting with "fallback:", set by claude.py and hybrid.py). The ``rules`` placeholder and the
  OCR+rules fallback say "confident" Misc / Process info by construction, so they never drop.
  ``Tags.unsure()`` (a classifier that raised) has Job posting among its post types anyway.

Rules use only the post type, company, role and level. Sponsorship exists only on job postings,
which are never dropped, so a sponsorship rule could never fire and is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence, Union

from .jobs import is_job_link
from .tags import DIMENSIONS, DIMENSION_NAME, PostType, Tags, TagValue, parse_tag_key, tag_key

RULE_DIMENSIONS = ("post_type", "company", "role", "level")
MAX_RULES = 25  # fits one Discord select
TITLES = {"post_type": "Post type", "company": "Company", "role": "Role", "level": "Level"}
# Only these taggers' own (non-fallback) output can drop a story; see the module docstring.
MODEL_TAGGERS = frozenset({"claude", "hybrid"})
FALLBACK_EVIDENCE = "fallback:"  # claude.py and hybrid.py: replace(tags, evidence=f"fallback: {why}")


class RuleError(ValueError):
    """A rule that can't be saved; the message is safe to show to the labeler."""


@dataclass(frozen=True)
class NoPostRule:
    """Dimension name -> the values it covers. All named dimensions must match."""

    values: Mapping[str, frozenset[TagValue]]

    def __post_init__(self) -> None:
        out: dict[str, frozenset[TagValue]] = {}
        for dimension, items in self.values.items():
            if dimension not in DIMENSIONS:
                raise RuleError(f"unknown dimension {dimension!r}")
            if dimension not in RULE_DIMENSIONS:
                raise RuleError(
                    "Sponsorship can't be used: it only exists on job postings, which are always posted."
                )
            members = frozenset(items)
            if not members:
                continue
            for v in members:
                if not isinstance(v, DIMENSIONS[dimension]):
                    raise RuleError(f"{v!r} is not a {dimension} value")
            if dimension == "post_type" and PostType.JOB_POSTING in members:
                raise RuleError("Job posting can't be on the list: job postings are always posted.")
            if len(members) == len(DIMENSIONS[dimension]):
                raise RuleError(
                    f"Every {TITLES[dimension]} value is picked, which is the same as not picking {TITLES[dimension]}. "
                    "Unselect at least one."
                )
            out[dimension] = members
        if not out:
            raise RuleError("Pick at least one value.")
        object.__setattr__(self, "values", out)

    @classmethod
    def from_keys(cls, keys: Iterable[Union[str, TagValue]]) -> NoPostRule:
        """Build from qualified keys such as "post_type:misc"."""
        grouped: dict[str, set[TagValue]] = {}
        for key in keys:
            value = parse_tag_key(key) if isinstance(key, str) else key
            grouped.setdefault(DIMENSION_NAME[type(value)], set()).add(value)
        return cls({d: frozenset(v) for d, v in grouped.items()})

    def to_keys(self) -> list[str]:
        return sorted(tag_key(v) for members in self.values.values() for v in members)

    def matches(self, tags: Tags) -> bool:
        """True when the tagger is confident on every dimension this rule uses and all of those
        values are covered. Says nothing about the safety rules (see ``would_drop``)."""
        for dimension, wanted in self.values.items():
            possible = tags.values(dimension)
            if possible is None or tags.is_unsure(dimension) or not possible <= wanted:
                return False
        return True

    def describe(self) -> str:
        """"Post type: Misc AND Level: Other" (labels as in /pings)."""
        parts = []
        for dimension in RULE_DIMENSIONS:
            members = self.values.get(dimension)
            if members:
                labels = ", ".join(m.label for m in DIMENSIONS[dimension] if m in members)
                parts.append(f"{TITLES[dimension]}: {labels}")
        return " AND ".join(parts)


def could_be_job(tags: Tags, links: Sequence[str] = ()) -> bool:
    """The "always post" test: a job link, or Job posting among the possible post types."""
    return PostType.JOB_POSTING in tags.post_type or any(is_job_link(u) for u in links)


def tagged_by_model(tagger: str, tags: Tags) -> bool:
    """True when ``tags`` are a real model tagger's output: ``tagger`` (the CLASSIFIER name, or the
    eval harness's tagger name) is claude or hybrid, and the tags are not that tagger's fallback."""
    return tagger in MODEL_TAGGERS and not tags.evidence.startswith(FALLBACK_EVIDENCE)


def list_match(rules: Iterable[NoPostRule], tags: Tags, links: Sequence[str] = ()) -> Optional[NoPostRule]:
    """The first rule that matches, with the job guards but WITHOUT the model-tagger rule. Only for
    logging a rule that was skipped; use ``matching_rule`` to decide."""
    if could_be_job(tags, links):
        return None
    return next((r for r in rules if r.matches(tags)), None)


def matching_rule(
    rules: Iterable[NoPostRule], tags: Tags, links: Sequence[str] = (), *, tagger: str
) -> Optional[NoPostRule]:
    """The first rule that drops this story, or None (post it). ``tagger`` is required: tags that
    did not come from a real model tagger never drop a story."""
    if not tagged_by_model(tagger, tags):
        return None
    return list_match(rules, tags, links)


def would_drop(rules: Iterable[NoPostRule], tags: Tags, links: Sequence[str] = (), *, tagger: str) -> bool:
    return matching_rule(rules, tags, links, tagger=tagger) is not None


def describe_tags(tags: Tags) -> str:
    """Tag values only, for logs: "post_type=misc level=n/a ...". No evidence or text."""
    parts = []
    for name, cls in DIMENSIONS.items():
        values = tags.values(name)
        parts.append(f"{name}=" + ("n/a" if values is None else "/".join(m.value for m in cls if m in values)))
    return " ".join(parts)
