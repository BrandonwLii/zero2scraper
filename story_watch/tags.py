"""Story tags: the five dimensions from docs/tags.md and the classifier output contract.

Nothing here does I/O or is wired into the service yet (#11 does that).

Values. Each dimension is an enum whose ``.value`` is a stable lowercase string,
used in env vars, the database and user config. Never rename one. ``"other"``
exists in several dimensions, so config that mixes dimensions should use the
qualified form ``"<dimension>:<value>"`` (``tag_key`` / ``parse_tag_key``)::

    Role.parse("SWE")            -> Role.SWE
    tag_key(Role.SWE)            -> "role:swe"
    parse_tag_key("level:other") -> Level.OTHER

Contract. A classifier returns ``Tags``. Each dimension holds the frozenset of
values the classifier can't rule out:

- one value: confident (or the story genuinely has one value);
- several values: unsure between them, or the story covers several (e.g. SWE
  and PM interns). Matching treats both the same way;
- every value: no idea;
- ``None``: not applicable, decided only by the post type (``APPLICABLE``).
  A dimension applies when it applies to *any* possible post type.

An empty set is never valid, so "not applicable" can't be mistaken for "matches
nothing". Pass ``None`` (or leave a field out) for a dimension the classifier
didn't decide: if it applies, it becomes every value (unsure); if it doesn't,
it stays ``None``, and any set given for it is dropped. ``Tags.unsure()`` is
the fallback when a classifier fails.

``Sponsorship.UNKNOWN`` is a fact (the posting says nothing about sponsorship),
not the classifier being unsure; unsure is a set with several values.

``confidence`` (0..1, overall) and ``evidence`` (short, for logs) are optional
and informational. Matching must use only the value sets.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, TypeVar, Union

E = TypeVar("E", bound="TagValue")


class TagValue(str, enum.Enum):
    """Base for the dimension enums: parsing and display labels."""

    @classmethod
    def parse(cls: type[E], text: str) -> E:
        """Stable string (case and surrounding spaces ignored) -> member."""
        try:
            return cls(text.strip().lower())
        except ValueError:
            known = ", ".join(m.value for m in cls)
            raise ValueError(f"unknown {DIMENSION_NAME[cls]} value {text!r}; known: {known}") from None

    @property
    def label(self) -> str:
        return _LABELS[self]


class Sponsorship(TagValue):
    SPONSOR_OR_CANADIAN = "sponsor_or_canadian"
    NO_SPONSOR = "no_sponsor"
    UNKNOWN = "unknown"


class PostType(TagValue):
    EVENT = "event"
    JOB_POSTING = "job_posting"
    PROCESS_INFO = "process_info"
    MISC = "misc"


class Company(TagValue):
    FAANG_PLUS = "faang_plus"
    QUANT = "quant"
    OTHER = "other"


class Role(TagValue):
    ML = "ml"
    SWE = "swe"
    PM = "pm"
    OTHER = "other"


class Level(TagValue):
    INTERNSHIP = "internship"
    NEW_GRAD = "new_grad"
    OTHER = "other"


_LABELS: dict[TagValue, str] = {
    Sponsorship.SPONSOR_OR_CANADIAN: "Sponsor or Canadian",
    Sponsorship.NO_SPONSOR: "No sponsor",
    Sponsorship.UNKNOWN: "Unknown",
    PostType.EVENT: "Event",
    PostType.JOB_POSTING: "Job posting",
    PostType.PROCESS_INFO: "Process info",
    PostType.MISC: "Misc",
    Company.FAANG_PLUS: "FAANG+",
    Company.QUANT: "Quant",
    Company.OTHER: "Other",
    Role.ML: "ML",
    Role.SWE: "SWE",
    Role.PM: "PM",
    Role.OTHER: "Other",
    Level.INTERNSHIP: "Internship",
    Level.NEW_GRAD: "New grad",
    Level.OTHER: "Other",
}

# Stable dimension names, in display order. Also the Tags field names.
DIMENSIONS: dict[str, type[TagValue]] = {
    "post_type": PostType,
    "sponsorship": Sponsorship,
    "company": Company,
    "role": Role,
    "level": Level,
}
DIMENSION_NAME: dict[type[TagValue], str] = {cls: name for name, cls in DIMENSIONS.items()}

# Which dimensions apply to each post type. post_type always applies.
APPLICABLE: dict[PostType, frozenset[str]] = {
    PostType.JOB_POSTING: frozenset(DIMENSIONS),
    PostType.EVENT: frozenset({"post_type", "company", "role", "level"}),
    PostType.PROCESS_INFO: frozenset({"post_type", "company", "role", "level"}),
    PostType.MISC: frozenset({"post_type"}),
}


def tag_key(value: TagValue) -> str:
    """Role.SWE -> "role:swe"."""
    return f"{DIMENSION_NAME[type(value)]}:{value.value}"


def parse_tag_key(text: str) -> TagValue:
    """"role:swe" -> Role.SWE."""
    dimension, sep, value = text.partition(":")
    dimension = dimension.strip().lower()
    if not sep or dimension not in DIMENSIONS:
        raise ValueError(f"expected <dimension>:<value> with dimension in {', '.join(DIMENSIONS)}, got {text!r}")
    return DIMENSIONS[dimension].parse(value)


def applicable_dimensions(post_types: Iterable[PostType]) -> frozenset[str]:
    """Dimensions that apply to at least one of the possible post types."""
    return frozenset().union(*(APPLICABLE[p] for p in post_types))


ValuesIn = Optional[Iterable[Union[TagValue, str]]]


@dataclass(frozen=True)
class Tags:
    """Classifier output. See the module docstring for the contract.

    Fields accept any iterable of members or stable strings and are normalized
    to frozensets (or None when not applicable).
    """

    post_type: ValuesIn = None
    sponsorship: ValuesIn = None
    company: ValuesIn = None
    role: ValuesIn = None
    level: ValuesIn = None
    confidence: Optional[float] = None
    evidence: str = ""

    def __post_init__(self) -> None:
        post_types = _normalize("post_type", self.post_type)
        object.__setattr__(self, "post_type", post_types)
        applicable = applicable_dimensions(post_types)
        for name in DIMENSIONS:
            if name == "post_type":
                continue
            value = _normalize(name, getattr(self, name)) if name in applicable else None
            object.__setattr__(self, name, value)
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence!r}")

    @classmethod
    def unsure(cls) -> Tags:
        """Every dimension unsure: the fallback when a classifier fails."""
        return cls()

    def values(self, dimension: str) -> Optional[frozenset[TagValue]]:
        """The possible values of a dimension, or None if it doesn't apply."""
        _check_dimension(dimension)
        return getattr(self, dimension)

    def is_applicable(self, dimension: str) -> bool:
        return self.values(dimension) is not None

    def is_certain(self, dimension: str) -> bool:
        """True when the dimension applies and has exactly one value."""
        values = self.values(dimension)
        return values is not None and len(values) == 1

    def certain_value(self, dimension: str) -> Optional[TagValue]:
        """The single value if the dimension is certain, else None."""
        values = self.values(dimension)
        return next(iter(values)) if values is not None and len(values) == 1 else None

    def is_unsure(self, dimension: str) -> bool:
        """True when the dimension applies and has every value (no idea)."""
        values = self.values(dimension)
        return values is not None and len(values) == len(DIMENSIONS[dimension])

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form with values in enum order; from_dict() reverses it."""
        out: dict[str, Any] = {}
        for name, cls in DIMENSIONS.items():
            values = getattr(self, name)
            out[name] = None if values is None else [m.value for m in cls if m in values]
        out["confidence"] = self.confidence
        out["evidence"] = self.evidence
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Tags:
        unknown = set(data) - set(DIMENSIONS) - {"confidence", "evidence"}
        if unknown:
            raise ValueError(f"unknown tag fields: {', '.join(sorted(unknown))}")
        return cls(**data)


def _check_dimension(dimension: str) -> None:
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown dimension {dimension!r}; known: {', '.join(DIMENSIONS)}")


def _normalize(dimension: str, values: ValuesIn) -> frozenset[TagValue]:
    cls = DIMENSIONS[dimension]
    if values is None:
        return frozenset(cls)
    if isinstance(values, str):
        raise TypeError(f"{dimension}: pass a collection of values, not a single string")
    out = set()
    for v in values:
        if isinstance(v, TagValue) and not isinstance(v, cls):
            raise ValueError(f"{dimension}: {tag_key(v)} belongs to another dimension")
        out.add(v if isinstance(v, cls) else cls.parse(v))
    if not out:
        raise ValueError(f"{dimension}: empty value set; use None for not applicable or unsure")
    return frozenset(out)
