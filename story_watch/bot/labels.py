"""Label records: validation against story_watch.tags, the JSONL file, last-wins reading.

Format (one JSON object per line, appended on every save; the last line per media_id wins):

    {"media_id": "...", "target": "...",
     "post_type": ["job_posting"], "sponsorship": ["unknown"], "company": ["other"],
     "role": ["swe", "pm"], "level": ["internship"],
     "labeler": "<Discord user id>", "note": "", "labeled_at": "<UTC ISO time>",
     "taxonomy": "<version string>"}

Each dimension is a list of stable value strings (enum order, no duplicates), or null when the
dimension doesn't apply to the post type (APPLICABLE in tags.py). A dimension that applies
always has at least one value.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from ..tags import APPLICABLE, DIMENSIONS, PostType, applicable_dimensions

log = logging.getLogger(__name__)

NOTE_MAX = 500


def _taxonomy_version() -> str:
    """Changes whenever a dimension, a value string or APPLICABLE changes."""
    spec = {
        "dimensions": {n: [m.value for m in cls] for n, cls in DIMENSIONS.items()},
        "applicable": {p.value: sorted(d) for p, d in APPLICABLE.items()},
    }
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    return f"tags-{digest[:8]}"


TAXONOMY_VERSION = _taxonomy_version()


class LabelError(ValueError):
    """The selection can't be saved; the message says why, for the labeler."""


Selection = Mapping[str, Iterable[str]]  # dimension -> chosen value strings


def _clean(dimension: str, values: Iterable[str]) -> list[str]:
    cls = DIMENSIONS[dimension]
    known = [m.value for m in cls]
    chosen = set(values)
    bad = chosen - set(known)
    if bad:
        raise LabelError(f"unknown {dimension} value(s): {', '.join(sorted(bad))}")
    return [v for v in known if v in chosen]  # enum order


def validate(selection: Selection) -> dict[str, list[str] | None]:
    """Selection -> the stored form. Raises LabelError.

    Dimensions that don't apply to the chosen post types become None, whatever was selected for
    them; dimensions that apply must have at least one value.
    """
    unknown = set(selection) - set(DIMENSIONS)
    if unknown:
        raise LabelError(f"unknown dimension(s): {', '.join(sorted(unknown))}")
    post_types = _clean("post_type", selection.get("post_type", ()))
    if not post_types:
        raise LabelError("choose at least one post type")
    applicable = applicable_dimensions(PostType(v) for v in post_types)
    out: dict[str, list[str] | None] = {"post_type": post_types}
    missing = []
    for name in DIMENSIONS:
        if name == "post_type":
            continue
        if name not in applicable:
            out[name] = None
            continue
        values = _clean(name, selection.get(name, ()))
        if not values:
            missing.append(name)
        out[name] = values
    if missing:
        raise LabelError("choose at least one value for: " + ", ".join(missing))
    return out


def ignored_dimensions(selection: Selection) -> list[str]:
    """Dimensions with selected values that will be dropped because they don't apply."""
    post_types = _clean("post_type", selection.get("post_type", ()))
    applicable = applicable_dimensions(PostType(v) for v in post_types) if post_types else frozenset()
    return [n for n in DIMENSIONS if n not in applicable and n != "post_type" and list(selection.get(n, ()))]


def is_allowed(user_id: int, allowed: frozenset[int]) -> bool:
    return user_id in allowed


@dataclass(frozen=True)
class Label:
    media_id: str
    target: str
    tags: Mapping[str, list[str] | None]
    labeler: str
    note: str
    labeled_at: str
    taxonomy: str = TAXONOMY_VERSION

    def to_json(self) -> str:
        doc = {"media_id": self.media_id, "target": self.target}
        doc.update({name: self.tags.get(name) for name in DIMENSIONS})
        doc.update(labeler=self.labeler, note=self.note, labeled_at=self.labeled_at, taxonomy=self.taxonomy)
        return json.dumps(doc, ensure_ascii=False, sort_keys=False)

    @classmethod
    def from_doc(cls, doc: Mapping) -> "Label":
        if not isinstance(doc, Mapping) or not isinstance(doc.get("media_id"), str):
            raise ValueError("not a label")
        return cls(
            media_id=doc["media_id"],
            target=str(doc.get("target", "")),
            tags={n: (list(doc[n]) if doc.get(n) is not None else None) for n in DIMENSIONS},
            labeler=str(doc.get("labeler", "")),
            note=str(doc.get("note", "")),
            labeled_at=str(doc.get("labeled_at", "")),
            taxonomy=str(doc.get("taxonomy", "")),
        )


def make_label(
    media_id: str, target: str, selection: Selection, labeler: int | str, note: str = "", now: datetime | None = None
) -> Label:
    """Validate and build the record to save. Raises LabelError."""
    note = note.strip()
    if len(note) > NOTE_MAX:
        raise LabelError(f"note is longer than {NOTE_MAX} characters")
    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return Label(
        media_id=media_id,
        target=target,
        tags=validate(selection),
        labeler=str(labeler),
        note=note,
        labeled_at=when.isoformat(timespec="seconds"),
    )


def append_label(path: Path, label: Label) -> None:
    """Append one line durably (a single write, then fsync)."""
    line = (label.to_json() + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def read_labels(path: Path) -> dict[str, Label]:
    """media_id -> its latest label. Missing file = none; bad lines are skipped."""
    out: dict[str, Label] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            label = Label.from_doc(json.loads(line))
        except (ValueError, TypeError):
            log.warning("labels: skipping line %d (not a label)", n)
            continue
        out[label.media_id] = label
    return out
