"""Classifiers: they turn a story into Tags (docs/tags.md).

The watcher only depends on the Classifier protocol, so a classifier plugs in by
implementing classify() and registering a factory in CLASSIFIERS (called with the
Config, or None); CLASSIFIER=<name> in the env selects it. The watcher handles
failures (an exception falls back to Tags.unsure(), which fails open) and
everything after classification (filtering, pings, sending). A classifier may also
have start_cycle() -> bool, called before each watcher cycle; it reports whether its
model is reachable, and the watcher alerts once when it has been down for a while.

- ``rules`` (the default): the placeholder below.
- ``hybrid``: #10's local vision model plus rules (story_watch/hybrid.py).
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol

from .instagram import StoryItem
from .tags import PostType, Tags


class Classifier(Protocol):
    def classify(self, item: StoryItem) -> Tags: ...


class RuleClassifier:
    """Placeholder: any link sticker means a job posting.

    The post type is the only dimension it decides; every other dimension is left
    unsure. It never returns PROCESS_INFO (or EVENT): with no caption to read,
    keyword rules for interview content only produced false positives, so those
    types are left to a classifier that looks at the media (``hybrid``).
    """

    def classify(self, item: StoryItem) -> Tags:
        return Tags(post_type=[PostType.JOB_POSTING if item.links else PostType.MISC])


def legacy_category(tags: Tags) -> str:
    """The old single `category` string (job_posting / interview_info / misc), kept in
    archive sidecars for readers that predate tags. Fails open: a post type that could
    be a job posting is "job_posting"."""
    post_types = tags.post_type
    if PostType.JOB_POSTING in post_types:
        return "job_posting"
    if PostType.PROCESS_INFO in post_types:
        return "interview_info"
    return "misc"


def _hybrid(cfg: Optional[Any] = None) -> Classifier:
    from .hybrid import from_config  # imported only when selected: it pulls in the model client

    return from_config(cfg)


CLASSIFIERS: dict[str, Callable[[Optional[Any]], Classifier]] = {
    "rules": lambda cfg=None: RuleClassifier(),
    "hybrid": _hybrid,
}


def build_classifier(name: str, cfg: Optional[Any] = None) -> Classifier:
    try:
        factory = CLASSIFIERS[name]
    except KeyError:
        raise ValueError(f"unknown classifier {name!r}; known: {', '.join(sorted(CLASSIFIERS))}") from None
    return factory(cfg)
