"""What the harness can score: anything with ``name``, ``version`` and ``tag(story) -> Tags``.

``tag`` may instead return ``TaggerOutput`` to report a cost (hosted models); a tagger that raises is
scored as ``Tags.unsure()``, the same fail-open fallback the watcher uses. Bump ``version`` whenever
the tagger's behavior changes (model, prompt, rules), because cached outputs are keyed by it.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Union

from .. import classify
from ..classify import CLASSIFIERS, Classifier, build_classifier
from ..tags import Tags
from .data import EvalStory


@dataclass(frozen=True)
class TaggerOutput:
    tags: Tags
    cost_usd: Optional[float] = None  # hook for hosted models; None = free or unknown


class Tagger(Protocol):
    name: str
    version: str

    def tag(self, story: EvalStory) -> Union[Tags, TaggerOutput]: ...


def _source_hash(module) -> str:
    """Short hash of a module's source, so editing a classifier invalidates its cached outputs."""
    path = inspect.getsourcefile(module)
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:8] if path else "unknown"


class ClassifierAdapter:
    """Scores a ``classify.py`` classifier. Classifiers already return ``Tags``, so this only adds
    the ``name`` and ``version`` the harness needs; the version includes a hash of ``classify.py``,
    so editing the classifier invalidates its cached outputs."""

    def __init__(self, classifier: Classifier, name: str):
        self._classifier = classifier
        self.name = f"classify-{name}"
        self.version = "cat-" + _source_hash(classify)

    def tag(self, story: EvalStory) -> Tags:
        return self._classifier.classify(story.item)


class UnsureBaseline:
    """Says "no idea" for everything. The extra-ping ceiling and the zero-effort comparison."""

    name = "unsure"
    version = "1"

    def tag(self, story: EvalStory) -> Tags:
        return Tags.unsure()


def load_tagger(spec: str) -> Tagger:
    """A classifier name from CLASSIFIERS, ``unsure``, or ``package.module:factory`` for a prototype.

    The factory is called with no arguments and returns a Tagger. It runs your own code, so only
    pass specs you wrote or reviewed.
    """
    if spec == "unsure":
        return UnsureBaseline()
    if spec in CLASSIFIERS:
        return ClassifierAdapter(build_classifier(spec), spec)
    module_name, sep, factory = spec.partition(":")
    if not sep or not module_name or not factory:
        known = ", ".join(sorted([*CLASSIFIERS, "unsure"]))
        raise ValueError(f"unknown tagger {spec!r}; use one of {known} or package.module:factory")
    try:
        tagger = getattr(importlib.import_module(module_name), factory)()
    except (ImportError, AttributeError) as e:
        raise ValueError(f"cannot load tagger {spec!r}: {type(e).__name__}") from None
    for attr in ("name", "version", "tag"):
        if not hasattr(tagger, attr):
            raise ValueError(f"tagger {spec!r} has no {attr!r}")
    return tagger
