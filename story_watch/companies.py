"""The FAANG+ and Quant company lists (docs/tags.md, "Company"): the single source of truth.

docs/tags.md shows the same tables for people to read; tests/test_companies.py fails if the two
drift, so change a list here and in the doc in the same commit. Each entry is the company name and
its aliases and subsidiaries; matching is case-insensitive except for the short aliases below.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable

FAANG_PLUS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Meta", ("Facebook", "Instagram", "WhatsApp", "Reality Labs")),
    ("Apple", ()),
    ("Amazon", ("AWS", "Amazon Web Services")),
    ("Netflix", ()),
    ("Google", ("Alphabet", "Google DeepMind", "DeepMind", "YouTube", "Wing")),
    ("Microsoft", ("LinkedIn", "GitHub")),
    ("NVIDIA", ()),
    ("OpenAI", ()),
    ("Anthropic", ()),
    ("Stripe", ()),
    ("Databricks", ()),
    ("Uber", ()),
    ("Airbnb", ()),
    ("Tesla", ()),
    ("Waymo", ()),
    ("Shopify", ()),
    ("Salesforce", ()),
    ("Adobe", ()),
    ("Snowflake", ()),
    ("Palantir", ()),
    ("Bloomberg", ()),
    ("DoorDash", ()),
    ("Pinterest", ()),
    ("Notion", ()),
)

QUANT: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Jane Street", ()),
    ("Citadel", ("Citadel Securities",)),
    ("Two Sigma", ()),
    ("Hudson River Trading", ("HRT",)),
    ("Jump Trading", ("Jump",)),
    ("D. E. Shaw", ("DE Shaw", "D.E. Shaw")),
    ("Optiver", ()),
    ("IMC Trading", ("IMC",)),
    ("Susquehanna", ("SIG", "Susquehanna International Group")),
    ("Five Rings", ()),
    ("Tower Research Capital", ("Tower Research",)),
    ("DRW", ()),
    ("Akuna Capital", ("Akuna",)),
    ("Virtu Financial", ("Virtu",)),
    ("XTX Markets", ("XTX",)),
    ("Renaissance Technologies", ("RenTech",)),
    ("Millennium", ("Millennium Management",)),
    ("Point72", ("Cubist",)),
    ("Squarepoint Capital", ("Squarepoint",)),
    ("Old Mission Capital", ("Old Mission",)),
    ("Radix Trading", ("Radix",)),
    ("Flow Traders", ()),
    ("Headlands Technologies", ("Headlands",)),
    ("G-Research", ()),
    ("Bridgewater", ("Bridgewater Associates",)),
    ("AQR", ("AQR Capital Management",)),
    ("Balyasny", ("Balyasny Asset Management", "BAM")),
    ("Man Group", ("Man AHL", "AHL")),
    ("PDT Partners", ("PDT",)),
    ("Voleon", ("The Voleon Group",)),
    ("Qube Research & Technologies", ("QRT",)),
    ("Maven Securities", ("Maven",)),
    ("Belvedere Trading", ("Belvedere",)),
    ("Chicago Trading Company", ("CTC",)),
    ("Schonfeld", ("Schonfeld Strategic Advisors",)),
    ("Marshall Wace", ()),
)

LISTS = {"faang_plus": FAANG_PLUS, "quant": QUANT}

# Known sponsors (docs/tags.md, decided 2026-10-09): a job posting at one of these is
# sponsor_or_canadian unless the posting itself refuses sponsorship.
KNOWN_SPONSORS = ("tesla",)

# Short or common-word aliases that need capitals or context to match safely.
_CASE_SENSITIVE = {"sig", "bam", "ctc", "imc", "hrt", "pdt", "xtx", "drw", "aqr", "qrt", "ahl"}
_CONTEXT = {
    "jump": r"jump trading",
    "wing": r"\bwing\b(?=[^\n]{0,30}(?:alphabet|drone|deliver))|wing\.com",
    "maven": r"maven securities",
}
# Text that Instagram's own UI puts on screenshots; never a company signal. Dropped line by line:
# the old "^\s*instagram\s*$" (re.M) was quadratic on long runs of blank lines.


def _drop_ui_noise(text: str) -> str:
    return "\n".join(" " if line.strip().lower() == "instagram" else line for line in text.split("\n"))


# "Notion" is also the notes tool that story authors link their write-ups to ("Notion / Full notes:",
# "slides in Notion"); that is not the company. Only these tool uses are dropped; "Notion intern" stays.
_NOTION_TOOL = re.compile(
    r"\bnotion\.(?:so|site)\b|"
    r"\b(?:in|on|via|use|uses|using|my|our|from|with|through|to)\s+notion\b|"
    r"\bnotion\s*(?:\n\s*)?(?:full\s+)?(?:notes?|docs?|page|pages|template|templates|link|workspace|board|database|here)\b",
    re.I)


def prompt_list(group: str) -> str:
    """'Meta (Facebook, Instagram, ...); Apple; ...' for the tagging prompt."""
    items = [f"{name} ({', '.join(aliases)})" if aliases else name for name, aliases in LISTS[group]]
    return "; ".join(items) + "."


@lru_cache(maxsize=1)
def _patterns() -> dict[str, list[re.Pattern[str]]]:
    out = {}
    for group, entries in LISTS.items():
        names = sorted({n.lower() for name, aliases in entries for n in (name, *aliases)}, key=len, reverse=True)
        rxs = []
        for name in names:
            if name in _CONTEXT:
                rxs.append(re.compile(_CONTEXT[name], re.I))
            elif name in _CASE_SENSITIVE:
                rxs.append(re.compile(rf"(?<![A-Za-z]){re.escape(name.upper())}(?![A-Za-z])"))
            else:
                rxs.append(re.compile(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", re.I))
        out[group] = rxs
    return out


def match_companies(texts: Iterable[str]) -> set[str]:
    """The groups ("faang_plus", "quant") whose names or aliases appear in any of ``texts``."""
    found = set()
    for text in texts:
        if not text:
            continue
        text = _NOTION_TOOL.sub(" ", _drop_ui_noise(text))
        for group, rxs in _patterns().items():
            if group not in found and any(rx.search(text) for rx in rxs):
                found.add(group)
    return found


def is_known_sponsor(texts: Iterable[str]) -> bool:
    return any(re.search(rf"\b{k}\b", t or "", re.I) for t in texts for k in KNOWN_SPONSORS)
