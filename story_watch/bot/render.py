"""Text for the label messages. Pure functions, so the discord glue stays thin."""

from __future__ import annotations

from typing import Mapping

from ..tags import DIMENSIONS, PostType
from .queue import Sidecar

DIMENSION_TITLES = {
    "post_type": "Post type",
    "sponsorship": "Sponsorship",
    "company": "Company",
    "role": "Role",
    "level": "Level",
}

# The watcher's current categories -> the tag taxonomy, only to pre-fill the post type.
_GUESS = {
    "job_posting": [PostType.JOB_POSTING.value],
    "interview_info": [PostType.PROCESS_INFO.value],
    "misc": [PostType.MISC.value],
}


def guess_selection(sc: Sidecar) -> dict[str, list[str]]:
    """What the classifier thinks, as a (partial) selection. Never saved without confirmation."""
    guess = _GUESS.get(sc.category)
    return {"post_type": list(guess)} if guess else {}


def value_label(dimension: str, value: str) -> str:
    return DIMENSIONS[dimension](value).label


def describe_tags(tags: Mapping[str, list[str] | None]) -> str:
    lines = []
    for name in DIMENSIONS:
        values = tags.get(name)
        text = "not applicable" if values is None else ", ".join(value_label(name, v) for v in values) or "none yet"
        lines.append(f"**{DIMENSION_TITLES[name]}:** {text}")
    return "\n".join(lines)


def describe_selection(selection: Mapping[str, list[str]]) -> str:
    return describe_tags({name: list(selection.get(name, ())) for name in DIMENSIONS})


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def story_description(sc: Sidecar) -> str:
    """Embed description: what the watcher found in the story."""
    parts = [f"**Account:** {sc.target}", f"**Posted:** <t:{int(sc.taken_at.timestamp())}:f>"]
    if sc.job_title or sc.company:
        parts.append("**Job:** " + " at ".join(x for x in (sc.job_title, sc.company) if x))
    if sc.links:
        parts.append("**Links:**\n" + "\n".join(f"<{link}>" for link in sc.links[:5]))
    if sc.mentions:
        parts.append("**Mentions:** " + ", ".join(f"@{m}".replace("@@", "@") for m in sc.mentions[:10]))
    guess = _GUESS.get(sc.category)
    who = f" ({sc.classifier})" if sc.classifier else ""
    parts.append(
        "**Classifier guess:** " + (value_label("post_type", guess[0]) if guess else "none") + who
    )
    return _clip("\n".join(parts), 3900)
