"""Story categories and the classifiers that assign them.

The watcher only depends on the Classifier protocol, so a smarter classifier
(e.g. an LLM looking at the image and links) plugs in by implementing
classify() and registering a factory in CLASSIFIERS; CLASSIFIER=<name> in the
env selects it. Classifiers should be pure decisions: the watcher handles
failures (an exception falls back to MISC) and everything after classification
(filtering, role pings, sending).
"""

from __future__ import annotations

import enum
from typing import Callable, Protocol

from .instagram import StoryItem


class Category(str, enum.Enum):
    JOB_POSTING = "job_posting"
    INTERVIEW_INFO = "interview_info"
    MISC = "misc"

    @property
    def label(self) -> str:
        return {"job_posting": "Job posting", "interview_info": "Interview process", "misc": "Misc"}[self.value]


class Classifier(Protocol):
    def classify(self, item: StoryItem) -> Category: ...


class RuleClassifier:
    """Placeholder until the LLM classifier: any link sticker means a job posting.

    It never returns INTERVIEW_INFO. With no caption to read, keyword rules for
    interview content only produced false positives, so that category is left to
    a classifier that looks at the media.
    """

    def classify(self, item: StoryItem) -> Category:
        return Category.JOB_POSTING if item.links else Category.MISC


CLASSIFIERS: dict[str, Callable[[], Classifier]] = {
    "rules": RuleClassifier,
}


def build_classifier(name: str) -> Classifier:
    try:
        return CLASSIFIERS[name]()
    except KeyError:
        raise ValueError(f"unknown classifier {name!r}; known: {', '.join(sorted(CLASSIFIERS))}") from None
