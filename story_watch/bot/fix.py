"""Fix tags (#35): correct the tags of a story the watcher posted, from the post itself.

Pure logic plus the file/database writes; nothing here imports discord. The context-menu command
in discord_app.py asks for the new tags, and `apply_fix` then:

1. appends a label (`source: "correction"`) to labels.jsonl, so the fix joins the eval set;
2. replaces the story's stored tags in state.db (the pings already went out, nothing re-pings);
3. returns the embed footer text for the webhook post, which the caller edits.

Failures are logged by exception type only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..companies import match_companies
from ..notify import tags_text
from ..store import Store, StoryRef
from ..tags import DIMENSIONS, Tags
from .labels import Label, append_label, make_label

log = logging.getLogger(__name__)

SOURCE = "correction"
COMPANY_GROUPS = {"faang_plus": "FAANG+", "quant": "Quant"}


def selection_from_tags(tags: Tags | None) -> dict[str, list[str]]:
    """The stored tags as a picker selection. A dimension the tagger had no idea about (every
    value) is left empty, so the person must choose rather than save "anything" as the truth."""
    out: dict[str, list[str]] = {}
    if tags is None:
        return out
    for name, cls in DIMENSIONS.items():
        values = tags.values(name)
        if values is None or tags.is_unsure(name):
            continue
        out[name] = [m.value for m in cls if m in values]
    return out


def tags_from_label(label: Label) -> Tags:
    """The corrected tags in the classifier contract (a dimension that doesn't apply is None)."""
    return Tags(**{name: label.tags.get(name) for name in DIMENSIONS}, confidence=1.0, evidence=SOURCE)


def corrected_footer(tags: Tags) -> str:
    return f"{tags_text(tags)} · corrected"


_EMBED_KEEP = ("title", "url", "color", "fields", "timestamp", "description")


def embed_with_footer(embed: Mapping[str, Any], footer: str) -> dict[str, Any]:
    """A fetched embed (as `Embed.to_dict()`) rebuilt for sending back with a new footer. Only
    the fields the watcher sets are kept; read-only ones such as proxy_url are dropped."""
    out: dict[str, Any] = {k: embed[k] for k in _EMBED_KEEP if embed.get(k) is not None}
    for key, subkeys in (("image", ("url",)), ("author", ("name",))):
        sub = embed.get(key) or {}
        kept = {k: sub[k] for k in subkeys if sub.get(k)}
        if kept:
            out[key] = kept
    out["footer"] = {"text": footer[:2048]}
    return out


def company_feedback(selection: Mapping[str, Any], texts: list[str]) -> list[str]:
    """Notes for the confirmation when FAANG+ or Quant was picked but the story's company text
    doesn't match that list (docs/tags.md), meaning the list may need the company added.
    No text to go on means no note."""
    texts = [t for t in texts if t]
    if not texts:
        return []
    found = match_companies(texts)
    return [
        f"{label} was set, but no company on the {label} list matches this story. "
        f"If the company belongs there, add it to `story_watch/companies.py` and `docs/tags.md`."
        for group, label in COMPANY_GROUPS.items()
        if group in set(selection.get("company") or ()) and group not in found
    ]


@dataclass(frozen=True)
class FixResult:
    label: Label
    tags: Tags
    footer: str
    notes: list[str]


def lookup(db_path: Path, message_id: str) -> tuple[StoryRef, Tags | None] | None:
    """The story shown by a message and its stored tags, or None if the message isn't a story post."""
    store = Store(db_path)
    try:
        ref = store.story_for_message(message_id)
        return None if ref is None else (ref, store.get_tags(ref.media_id))
    finally:
        store.close()


def apply_fix(
    db_path: Path,
    labels_path: Path,
    ref: StoryRef,
    selection: Mapping[str, Any],
    labeler: int | str,
    note: str = "",
    texts: list[str] = (),  # type: ignore[assignment]
) -> FixResult:
    """Validate, append the label, then replace the stored tags. Raises LabelError for a bad
    selection (nothing written) and OSError/sqlite errors when a write fails. The label goes
    first: if the database write then fails, the correction is not lost for the eval set."""
    label = make_label(ref.media_id, ref.target, selection, labeler, note, source=SOURCE)
    tags = tags_from_label(label)
    append_label(labels_path, label)
    notes = company_feedback(selection, list(texts))
    for n in notes:
        log.info("fix: %s set for company %r, which is not on that list (media %s)",
                 n.split(" was set", 1)[0], (texts[0] if texts else "")[:80], ref.media_id)
    store = Store(db_path)
    try:
        store.replace_tags(ref.media_id, ref.taken_at, tags)
    finally:
        store.close()
    return FixResult(label, tags, corrected_footer(tags), notes)
