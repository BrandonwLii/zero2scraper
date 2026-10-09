"""Load the labeled set: labels.jsonl joined with the archive's JSON sidecars.

The archive keeps ``<target>/<time>_<media id>.json`` sidecars (story_watch/archive.py) and the
labeling bot keeps ``labels.jsonl`` at the archive's top level (story_watch/bot/labels.py; the last
line per media id wins). A story is scored only when it has both.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..bot.labels import TAXONOMY_VERSION, Label, read_labels
from ..instagram import StoryItem
from ..tags import APPLICABLE, DIMENSIONS, PostType, Tags

log = logging.getLogger(__name__)

LABELS_NAME = "labels.jsonl"

# Reasons a label or archived story is left out; keys of LoadResult.skipped.
NO_SIDECAR = "labeled but not in the archive"
BAD_LABEL = "label does not parse"
STALE_TAXONOMY = "label follows another taxonomy version"
UNLABELED = "archived but not labeled"


@dataclass(frozen=True)
class EvalStory:
    media_id: str
    target: str
    gold: Tags
    item: StoryItem
    taxonomy: str = ""


@dataclass
class LoadResult:
    stories: list[EvalStory] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)


def gold_tags(label: Label) -> Tags:
    """The label as ``Tags``. Raises ValueError if a value is unknown or a dimension is missing.

    A dimension that applies to the labeled post types must have values; ``Tags`` would otherwise
    turn a missing one into "every value", which would make a broken label look like a tolerant one.
    """
    post_types = label.tags.get("post_type")
    if not post_types:
        raise ValueError("no post type")
    applicable = frozenset().union(*(APPLICABLE[PostType.parse(p)] for p in post_types))
    for name in DIMENSIONS:
        if name in applicable and not label.tags.get(name):
            raise ValueError(f"{name} missing")
    return Tags(**{name: label.tags.get(name) for name in DIMENSIONS})


def story_item(doc: Mapping[str, Any]) -> StoryItem:
    """Rebuild the watcher's ``StoryItem`` from a sidecar (the thumbnail URL is not kept)."""
    taken_at = datetime.fromisoformat(doc["taken_at"])
    if taken_at.tzinfo is None:
        taken_at = taken_at.replace(tzinfo=timezone.utc)
    return StoryItem(
        media_id=str(doc["media_id"]),
        target=str(doc["target"]),
        taken_at=taken_at,
        is_video=bool(doc.get("is_video")),
        thumbnail_url="",
        links=tuple(str(x) for x in doc.get("links") or ()),
        mentions=tuple(str(x) for x in doc.get("mentions") or ()),
        job_title=doc.get("job_title") or None,
        company=doc.get("company") or None,
        node=doc.get("node"),
        page_node=doc.get("page_node"),
    )


def read_sidecars(archive: Path) -> dict[str, StoryItem]:
    """media id -> story, for every readable sidecar in the per-target folders."""
    out: dict[str, StoryItem] = {}
    for path in sorted(Path(archive).glob("*/*.json")):
        try:
            item = story_item(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("eval: skipping an unreadable sidecar")
            continue
        out[item.media_id] = item
    return out


def load_stories(archive: Path, labels_path: Path | None = None, allow_stale_taxonomy: bool = False) -> LoadResult:
    """The labeled stories, oldest first, plus counts of what was left out and why."""
    archive = Path(archive)
    labels = read_labels(labels_path or archive / LABELS_NAME)
    items = read_sidecars(archive)
    result = LoadResult()
    for media_id, label in labels.items():
        item = items.get(media_id)
        if item is None:
            result.skipped[NO_SIDECAR] += 1
            continue
        if label.taxonomy != TAXONOMY_VERSION and not allow_stale_taxonomy:
            result.skipped[STALE_TAXONOMY] += 1
            continue
        try:
            gold = gold_tags(label)
        except ValueError:
            result.skipped[BAD_LABEL] += 1
            continue
        result.stories.append(EvalStory(media_id, item.target, gold, item, label.taxonomy))
    result.skipped[UNLABELED] = sum(1 for m in items if m not in labels)
    result.stories.sort(key=lambda s: (s.item.taken_at, s.media_id))
    return result
