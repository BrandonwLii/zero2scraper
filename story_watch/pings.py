"""Ping matching rules (docs/pings.md): should this user be pinged for these tags?

Pure and I/O-free. Storage, Discord and config wiring belong to #16 and #17.

The rules, in short (the doc has the table of worked examples):

- opt-in: a user with no "ping me" values is never pinged;
- a "ping me" list with no post type means job postings only (explicit after construction);
- a dimension in the "ping me" list must match (OR within a dimension, AND across
  dimensions); an unset dimension matches anything;
- an unsure or multi-valued dimension matches if *any* possible value is listed;
- a not-applicable dimension (``None``) always matches and never vetoes;
- "don't ping me" vetoes only when *every* possible value is on the list, i.e.
  the tag is effectively confident. A veto beats a match.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Union

from story_watch.tags import DIMENSIONS, DIMENSION_NAME, PostType, Tags, TagValue, parse_tag_key, tag_key

Values = Mapping[str, Iterable[TagValue]]

# Post types a non-empty "ping me" list gets when it names none.
DEFAULT_PING_POST_TYPES = frozenset({PostType.JOB_POSTING})


def _normalize(label: str, values: Values) -> dict[str, frozenset[TagValue]]:
    """Validate dimension names and value types; drop dimensions with no values."""
    out: dict[str, frozenset[TagValue]] = {}
    for dimension, items in values.items():
        if dimension not in DIMENSIONS:
            raise ValueError(f"{label}: unknown dimension {dimension!r}; known: {', '.join(DIMENSIONS)}")
        members = frozenset(items)
        for v in members:
            if not isinstance(v, DIMENSIONS[dimension]):
                raise ValueError(f"{label}: {v!r} is not a {dimension} value")
        if members:
            out[dimension] = members
    return out


@dataclass(frozen=True)
class PingPrefs:
    """One user's lists: value sets per dimension.

    A dimension missing or empty in ``ping_me`` matches anything, except that a
    non-empty ``ping_me`` with no post type defaults to ``{JOB_POSTING}`` (so a user
    who lists only ``level:internship`` isn't pinged for every Misc post). The default
    is written into ``ping_me``, so ``to_keys()`` shows it. In ``dont_ping`` a missing
    dimension never vetoes and nothing is defaulted. Both are normalized to dicts of
    frozensets without empty entries.
    """

    ping_me: Values = field(default_factory=dict)
    dont_ping: Values = field(default_factory=dict)

    def __post_init__(self) -> None:
        ping_me = _normalize("ping_me", self.ping_me)
        if ping_me and "post_type" not in ping_me:
            ping_me["post_type"] = DEFAULT_PING_POST_TYPES
        object.__setattr__(self, "ping_me", ping_me)
        object.__setattr__(self, "dont_ping", _normalize("dont_ping", self.dont_ping))

    @classmethod
    def from_keys(cls, ping_me: Iterable[Union[str, TagValue]] = (), dont_ping: Iterable[Union[str, TagValue]] = ()) -> PingPrefs:
        """Build from qualified tag keys such as "role:swe" (or members)."""
        return cls(_group(ping_me), _group(dont_ping))

    def to_keys(self) -> tuple[list[str], list[str]]:
        """(ping_me, dont_ping) as sorted qualified keys; from_keys() reverses it."""
        return (_keys(self.ping_me), _keys(self.dont_ping))

    @property
    def is_empty(self) -> bool:
        return not self.ping_me


def _group(items: Iterable[Union[str, TagValue]]) -> dict[str, set[TagValue]]:
    out: dict[str, set[TagValue]] = {}
    for item in items:
        value = parse_tag_key(item) if isinstance(item, str) else item
        out.setdefault(DIMENSION_NAME[type(value)], set()).add(value)
    return out


def _keys(values: Values) -> list[str]:
    return sorted(tag_key(v) for members in values.values() for v in members)


def should_ping(prefs: PingPrefs, tags: Tags) -> bool:
    """True when the user should be pinged for a story with these tags."""
    if not prefs.ping_me:  # opt-in
        return False
    for dimension in DIMENSIONS:
        possible = tags.values(dimension)
        if possible is None:  # not applicable: matches, never vetoes
            continue
        wanted = prefs.ping_me.get(dimension)
        if wanted and possible.isdisjoint(wanted):
            return False
        unwanted = prefs.dont_ping.get(dimension)
        if unwanted and possible <= unwanted:  # every possible value is unwanted
            return False
    return True
