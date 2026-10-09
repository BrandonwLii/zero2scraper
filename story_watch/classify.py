"""Classifiers: they turn a story into Tags (docs/tags.md).

The watcher only depends on the Classifier protocol, so a smarter classifier
(e.g. an LLM looking at the image and links) plugs in by implementing
classify() and registering a factory in CLASSIFIERS; CLASSIFIER=<name> in the
env selects it. Classifiers should be pure decisions: the watcher handles
failures (an exception falls back to Tags.unsure(), which fails open) and
everything after classification (filtering, role pings, sending).
"""

from __future__ import annotations

from typing import Callable, Protocol

from .instagram import StoryItem
from .tags import PostType, Tags


class Classifier(Protocol):
    def classify(self, item: StoryItem) -> Tags: ...


class RuleClassifier:
    """Placeholder until the LLM classifier: any link sticker means a job posting.

    The post type is the only dimension it decides; every other dimension is left
    unsure. It never returns PROCESS_INFO (or EVENT): with no caption to read,
    keyword rules for interview content only produced false positives, so those
    types are left to a classifier that looks at the media.
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


CLASSIFIERS: dict[str, Callable[[], Classifier]] = {
    "rules": RuleClassifier,
}


def build_classifier(name: str) -> Classifier:
    try:
        return CLASSIFIERS[name]()
    except KeyError:
        raise ValueError(f"unknown classifier {name!r}; known: {', '.join(sorted(CLASSIFIERS))}") from None
