"""Logic behind /nopost: the option list and the summary text. Pure: no discord, no I/O.

A rule is built from one multi-select of tag values: the picks are grouped by dimension, values
within a dimension are OR, dimensions are AND (story_watch/nopost.py has the matching rules).
"""

from __future__ import annotations

from typing import Iterable, Sequence

from ..nopost import MAX_RULES, RULE_DIMENSIONS, NoPostRule, RuleError
from ..tags import DIMENSIONS, PostType, tag_key
from .render import DIMENSION_TITLES

OPTION_LIMIT = 25
RULE_LABEL_LIMIT = 100  # characters in a Discord select option label
MESSAGE_LIMIT = 2000  # characters in a Discord message; 25 long rules can exceed it

EDIT_INTRO = (
    "**The no-post list.** Pick values for one rule, then press **Add rule**. "
    "Values of one kind are \"or\", different kinds are \"and\"."
)
NEVER_DROPPED = (
    "_Never dropped, whatever the list says: stories with a job link, stories that might be a job posting, "
    "stories where the tagger is unsure about a kind the rule uses, and stories not tagged by the Claude or "
    "local model (the placeholder `rules` tagger, or the fallback while the model is down)._"
)


def all_options() -> list[tuple[str, str]]:
    """(key, label) for every value a rule can use, in display order. Job posting and Sponsorship
    are left out: those are never dropped."""
    out = []
    for name in RULE_DIMENSIONS:
        for m in DIMENSIONS[name]:
            if m is PostType.JOB_POSTING:
                continue
            out.append((tag_key(m), f"{DIMENSION_TITLES[name]}: {m.label}"))
    return out


assert len(all_options()) <= OPTION_LIMIT


def build_rule(picks: Iterable[str]) -> tuple[NoPostRule | None, str]:
    """(rule, "") or (None, the reason, safe to show)."""
    try:
        return NoPostRule.from_keys(picks), ""
    except RuleError as e:
        return None, str(e)


def rule_label(rule_id: int, rule: NoPostRule) -> str:
    text = rule.describe()
    return text if len(text) <= RULE_LABEL_LIMIT else text[: RULE_LABEL_LIMIT - 1] + "…"


def describe(rules: Sequence[tuple[int, NoPostRule]], editable: bool) -> str:
    """The summary shown by /nopost show and under the /nopost edit controls."""
    if rules:
        lines = ["**Not posted (a story matching any of these):**", *(f"- {r.describe()}" for _, r in rules)]
    else:
        lines = ["**The list is empty.** Every story is posted."]
    lines += ["", NEVER_DROPPED, "Dropped stories are still archived and still reach the label channel."]
    if len(rules) >= MAX_RULES:
        lines.append(f"The list is full ({MAX_RULES} rules).")
    if not editable:
        lines.append("_Only labelers can change this list._")
    return "\n".join(lines)


def fit(text: str) -> str:
    """Cut a reply to Discord's message limit (a full list of long rules can pass it)."""
    return text if len(text) <= MESSAGE_LIMIT else text[: MESSAGE_LIMIT - 1] + "…"
