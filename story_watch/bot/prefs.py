"""Logic behind /pings: the option list, applying a pick, warnings and the summary text.

Pure: no discord, no I/O. Values are qualified tag keys such as "role:swe"; PingPrefs does the
validation and the post-type default (docs/pings.md), this module only shapes the UI around it.
"""

from __future__ import annotations

from typing import Iterable

from ..pings import PingPrefs
from ..tags import DIMENSIONS, PostType, Sponsorship, tag_key
from .render import DIMENSION_TITLES

TAGS_DOC = "docs/tags.md"
SELECT_LIMIT = 25  # options per Discord select; the taxonomy must fit in one

# What a user who never saved anything starts from in /pings edit: "don't ping me for No
# sponsor" (docs/pings.md, open question 2). Most listings don't state sponsorship, so ping-me
# "Sponsor or Canadian" would silently skip every Unknown one.
DEFAULT_MUTE = (tag_key(Sponsorship.NO_SPONSOR),)


def all_options() -> list[tuple[str, str]]:
    """(key, label) for every value, e.g. ("role:swe", "Role: SWE"), in display order."""
    return [
        (tag_key(m), f"{DIMENSION_TITLES[name]}: {m.label}") for name, cls in DIMENSIONS.items() for m in cls
    ]


_LABEL = dict(all_options())
assert len(_LABEL) <= SELECT_LIMIT


def label(key: str) -> str:
    return _LABEL[key]


def initial_lists(saved: PingPrefs | None) -> tuple[list[str], list[str]]:
    """The lists /pings edit opens with: the saved ones, or the defaults for a new user."""
    if saved is None:
        return [], list(DEFAULT_MUTE)
    return saved.to_keys()


def normalize(ping: Iterable[str], mute: Iterable[str]) -> tuple[list[str], list[str]]:
    """Round-trip through PingPrefs: validates, adds the default post type, sorts."""
    return PingPrefs.from_keys(ping, mute).to_keys()


def pick_ping(ping: Iterable[str], mute: Iterable[str], chosen: Iterable[str]) -> tuple[list[str], list[str]]:
    """The "Ping me for" select changed to `chosen`. What was just picked leaves the other list."""
    chosen = set(chosen)
    return normalize(chosen, set(mute) - chosen)


def pick_mute(ping: Iterable[str], mute: Iterable[str], chosen: Iterable[str]) -> tuple[list[str], list[str]]:
    """The "Never ping me for" select changed to `chosen`. What was just picked leaves the other list."""
    chosen = set(chosen)
    return normalize(set(ping) - chosen, chosen)


def warnings(prefs: PingPrefs) -> list[str]:
    out: list[str] = []
    if prefs.is_empty:
        out.append("**You won't be pinged.** Pick at least one value under \"Ping me for\".")
    for name, cls in DIMENSIONS.items():
        blocked = prefs.dont_ping.get(name, frozenset())
        if len(blocked) == len(cls):
            out.append(
                f"**You blocked every {DIMENSION_TITLES[name]} value, so you will never be pinged.** "
                "Unselect one if that's not what you want."
            )
    ping_keys, mute_keys = prefs.to_keys()
    both = sorted(set(ping_keys) & set(mute_keys))
    if both and not any(w.startswith("**You blocked every") for w in out):
        names = ", ".join(label(k) for k in both)
        out.append(
            f"**{names} is on both lists, and \"never\" wins.** "
            "Job postings are the default when you pick no post type; pick a post type to change that."
        )
    return out


def _group_lines(keys: list[str]) -> list[str]:
    lines = []
    for name, cls in DIMENSIONS.items():
        values = [label(tag_key(m)).split(": ", 1)[1] for m in cls if tag_key(m) in keys]
        if values:
            lines.append(f"{DIMENSION_TITLES[name]}: {', '.join(values)}")
    return lines


def describe(prefs: PingPrefs, saved: bool = True) -> str:
    """The summary shown by /pings show and under the /pings edit selects."""
    ping, mute = prefs.to_keys()
    ping_lines = _group_lines(ping) or ["nothing"]
    mute_lines = _group_lines(mute) or ["nothing"]
    parts = ["**Ping me for**", *(f"- {x}" for x in ping_lines), "**Never ping me for**", *(f"- {x}" for x in mute_lines)]
    if ping and set(prefs.ping_me.get("post_type", ())) == {PostType.JOB_POSTING}:
        parts.append("_Job postings only. Pick other post types to hear about those too._")
    if not saved:
        parts.append("_Defaults, not saved yet. Your first change saves them._")
    warn = warnings(prefs)
    if warn:
        parts.append("")
        parts.extend(warn)
    return "\n".join(parts)


def help_footer() -> str:
    return f"What each value means: `{TAGS_DOC}` in the repo."
