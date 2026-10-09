"""Shared pieces for the #10 tagger prototypes: story inputs, company lists, rules and contract guards.

Nothing here is imported by the service. Private data (the archive, saved listings, model outputs)
is read from and written to ``~/story-watch-data`` (override with the env vars below), never the repo.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit

from story_watch.jobs import is_job_link
from story_watch.tags import DIMENSIONS, Company, PostType, Sponsorship, Tags

REPO = Path(__file__).resolve().parents[2]
sys.path.append(str(REPO / "research" / "job_pages"))
import rules as job_rules  # noqa: E402  (#9's sponsorship, location and level rules)

DATA = Path(os.environ.get("STORY_WATCH_DATA", Path.home() / "story-watch-data"))
ARCHIVE = Path(os.environ.get("TAGGER_ARCHIVE", DATA / "archive"))
LISTINGS = Path(os.environ.get("TAGGER_LISTINGS", DATA / "listings"))
WORK = Path(os.environ.get("TAGGER_WORK", DATA / "eval-work" / "issue-10"))
IMAGES = WORK / "images"  # one normalised still per media id (prep_images.py)
RAW = WORK / "raw"  # model responses per run label: raw/<label>/<media id>.json
OCR = WORK / "ocr"  # RapidOCR text per media id: ocr/<media id>.json

ALL = {name: [m.value for m in cls] for name, cls in DIMENSIONS.items()}
KNOWN_SPONSORS = ("tesla",)  # docs/tags.md, decided 2026-10-09


# -- story inputs ---------------------------------------------------------------------------


@dataclass
class Listing:
    status: str  # ok | closed | failed
    title: str = ""
    company: str = ""
    locations: list[str] = field(default_factory=list)
    employment_type: str = ""
    description: str = ""
    is_event_page: bool = False

    @property
    def read_ok(self) -> bool:
        return self.status == "ok" and len(self.description) >= 200


@dataclass
class StoryInputs:
    media_id: str
    image: Optional[Path]
    links: list[str]
    mentions: list[str]
    sidecar_title: str
    sidecar_company: str
    listing: Optional[Listing]

    @property
    def has_job_link(self) -> bool:
        return any(is_job_link(u) for u in self.links)


def _s(x) -> str:
    return "" if x in (None, "None") else str(x)


def load_listing(media_id: str) -> Optional[Listing]:
    path = LISTINGS / f"{media_id}.json"
    if not path.exists():
        return None
    d = json.loads(path.read_text(encoding="utf-8"))
    locs = d.get("locations") or []
    return Listing(
        status=d.get("status") or "failed",
        title=_s(d.get("title")),
        company=_s(d.get("company")),
        locations=[str(x) for x in locs] if isinstance(locs, list) else [],
        employment_type=_s(d.get("employment_type")),
        description=_s(d.get("description")),
        is_event_page="event page" in _s(d.get("reason")),
    )


def story_inputs(story) -> StoryInputs:
    """``story`` is a harness EvalStory."""
    item = story.item
    image = IMAGES / f"{story.media_id}.jpg"
    return StoryInputs(
        media_id=story.media_id,
        image=image if image.exists() else None,
        links=list(item.links),
        mentions=list(item.mentions),
        sidecar_title=item.job_title or "",
        sidecar_company=item.company or "",
        listing=load_listing(story.media_id) if item.links else None,
    )


def listing_snippet(listing: Listing, head_chars: int = 600, auth_chars: int = 700) -> str:
    """The start of the description plus every sentence about work authorization (#9's selector)."""
    text = listing.description
    head = " ".join(text[:head_chars].split())
    auth, n = [], 0
    for s in job_rules.work_auth_sentences(text):
        s = " ".join(s.split())
        if s and s not in head and n + len(s) <= auth_chars:
            auth.append(s)
            n += len(s)
    out = head + ("…" if len(text) > head_chars else "")
    if auth:
        out += "\nWork-authorization sentences: " + " | ".join(auth)
    return out


def context_text(inp: StoryInputs, ocr_text: Optional[str] = None) -> str:
    """The text that goes into every model prompt next to (or instead of) the image."""
    lines = []
    if inp.links:
        for url in inp.links:
            p = urlsplit(url)
            lines.append(f"Link sticker: {p.hostname or ''}{p.path[:120]}")
    else:
        lines.append("Link sticker: none")
    if inp.mentions:
        lines.append("Mentions: " + ", ".join("@" + m.lstrip("@") for m in inp.mentions))
    lst = inp.listing
    if inp.links:
        if lst is None or not lst.read_ok:
            why = "closed" if lst is not None and lst.status == "closed" else "could not be read"
            lines.append(f"Linked page: {why}; nothing is known about its text.")
            title = (lst.title if lst else "") or inp.sidecar_title
            if title:
                lines.append(f"Page title from metadata: {title}")
        else:
            lines.append("Linked page" + (" (looks like an event page)" if lst.is_event_page else "") + ":")
            if lst.title or inp.sidecar_title:
                lines.append(f"  Title: {lst.title or inp.sidecar_title}")
            if lst.company or inp.sidecar_company:
                lines.append(f"  Company: {lst.company or inp.sidecar_company}")
            if lst.locations:
                lines.append("  Locations: " + "; ".join(lst.locations[:6]))
            if lst.employment_type:
                lines.append(f"  Employment type: {lst.employment_type}")
            lines.append("  Text: " + listing_snippet(lst))
    if ocr_text is not None:
        lines.append("OCR text of the story image (may be garbled or split):\n" + (ocr_text.strip() or "(none)"))
    return "\n".join(lines)


def load_ocr(media_id: str) -> Optional[str]:
    path = OCR / f"{media_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("text", "")


# -- company lists (parsed from docs/tags.md so they never drift) -----------------------------


@lru_cache(maxsize=1)
def company_lists() -> dict[str, list[str]]:
    """group -> lowercase names and aliases, from the FAANG+ and Quant tables in docs/tags.md."""
    doc = (REPO / "docs" / "tags.md").read_text(encoding="utf-8")
    out: dict[str, list[str]] = {}
    for group, heading in (("faang_plus", "### FAANG+ list"), ("quant", "### Quant list")):
        section = doc.split(heading, 1)[1].split("\n## ", 1)[0].split("\n### ", 1)[0]
        names = []
        for line in section.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2 or cells[0] in ("Company", "") or set(cells[0]) <= set("-"):
                continue
            names.append(cells[0])
            for alias in re.split(r",\s*", re.sub(r"\(.*?\)", "", cells[1])):
                if alias.strip():
                    names.append(alias.strip())
        out[group] = sorted({n.lower() for n in names}, key=len, reverse=True)
    return out


# Short or common-word aliases that need case or context to match safely.
_CASE_SENSITIVE = {"sig", "bam", "ctc", "imc", "hrt", "pdt", "xtx", "drw", "aqr", "qrt", "ahl"}
_CONTEXT = {"jump": r"jump trading", "wing": r"\bwing\b(?=[^\n]{0,30}(?:alphabet|drone|deliver))|wing\.com", "maven": r"maven securities"}
# Text that Instagram's own UI puts on screenshots; never a company signal.
_UI_NOISE = re.compile(r"^\s*instagram\s*$", re.I | re.M)


def match_companies(texts: Iterable[str]) -> set[str]:
    """The groups ("faang_plus", "quant") whose names or aliases appear in any of ``texts``."""
    found = set()
    for text in texts:
        if not text:
            continue
        text = _UI_NOISE.sub(" ", text)
        for group, names in company_lists().items():
            for name in names:
                if name in _CONTEXT:
                    rx = re.compile(_CONTEXT[name], re.I)
                elif name in _CASE_SENSITIVE:
                    rx = re.compile(rf"(?<![A-Za-z]){re.escape(name.upper())}(?![A-Za-z])")
                else:
                    rx = re.compile(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", re.I)
                if rx.search(text):
                    found.add(group)
                    break
    return found


def is_known_sponsor(texts: Iterable[str]) -> bool:
    return any(re.search(rf"\b{k}\b", t or "", re.I) for t in texts for k in KNOWN_SPONSORS)


# -- title and text rules -----------------------------------------------------------------------

ROLE_RX = {
    "ml": re.compile(r"\bmachine learning\b|\bML\b|\bAI\b|\bartificial intelligence\b|\bdeep learning\b|\bLLM|\bcomputer vision\b|\bNLP\b|\bapplied scien|\bresearch scien", re.I),
    "swe": re.compile(r"\bsoftware\b|\bSWE\b|\bSDE\b|\bdeveloper\b|\bback-?end\b|\bfront-?end\b|\bfull[- ]?stack\b|\bmobile\b|\bios\b|\bandroid\b|"
                      r"\binfrastructure\b|\bSRE\b|\bdevops\b|\bplatform engineer|\bdata engineer|\bsecurity engineer|\bquant(?:itative)? developer|\bengineering intern|\bcoding\b", re.I),
    "pm": re.compile(r"\bproduct manage|\bAPM\b|\bPM\b|\bproduct owner\b|\bprogram manage|\bTPM\b", re.I),
    "other": re.compile(r"\btrad(?:er|ing)\b|\bquant(?:itative)? research|\bhardware\b|\bdata scien|\banalyst\b|\bdesign(?:er)?\b|\bconsult|\bsales\b|\bfinance\b|"
                        r"\bmechanical\b|\belectrical\b|\bmanufactur|\bsupply chain\b|\bbusiness\b|\boperations\b|\bmarketing\b", re.I),
}
_AI_ENGINEERING = re.compile(r"\b(?:AI|ML|machine learning)\b[^\n]{0,30}\b(?:software|SWE|engineer|engineering|developer|automation)\b|"
                             r"\b(?:software|SWE)\b[^\n]{0,20}\b(?:AI|ML|machine learning)\b", re.I)


def role_from_text(texts: Iterable[str]) -> set[str]:
    """Role values named in titles; every role when nothing matches."""
    found: set[str] = set()
    for t in texts:
        if not t:
            continue
        for value, rx in ROLE_RX.items():
            if rx.search(t):
                found.add(value)
        if _AI_ENGINEERING.search(t):
            found |= {"ml", "swe"}
        if re.search(r"data scien", t, re.I) and not ROLE_RX["ml"].search(t):
            found.discard("ml")
    return found or set(ALL["role"])


def level_from_title(title: str) -> Optional[set[str]]:
    """Level from title words alone; None when the title doesn't settle it."""
    if not title:
        return None
    if job_rules.INTERN_RX.search(title):
        return {"internship"}
    if job_rules.NEW_GRAD_RX.search(title):
        return {"new_grad"}
    if job_rules.OTHER_TITLE_RX.search(title):
        return {"other"}
    if re.search(r"(?i:\b(?:engineer|developer|scientist|analyst))\s+(?:I|1)\b|(?i:\blevel 1\b)", title):
        return {"new_grad", "other"}
    return None


EVENT_RX = re.compile(r"\binfo(?:rmation)? session|\bcoffee chat|\bwebinar|\bhackathon|\bcareer fair|\bworkshop|\bconference|\bregister\b|"
                      r"\bRSVP\b|\bsign up\b|\bopen house|\bmeet (?:the|our) team|\bnetworking|\bfireside|\bday\b[^\n]{0,15}\b(?:2026|2027)\b", re.I)
PROCESS_RX = re.compile(r"\binterview|\bOA\b|\bonline assessment|\boffer\b|\brecruiter|\bsuper ?day|\bfinal round|\bphone screen|\btechnical round|"
                        r"\bbehavio(?:u)?ral\b|\bleetcode|\brejected|\btimeline", re.I)


# Refusal phrasings #9's rules miss, from the user's decisions in docs/tags.md and #9 (2026-10-08):
# "must be enrolled at a US school" and US-only applicant pools are no_sponsor for a Canadian student.
EXTRA_NO_SPONSOR_RX = [
    re.compile(r"\bmust be (?:a )?u\.?s\.? persons?\b", re.I),
    re.compile(r"\benrolled in (?:an? )?(?:academic )?(?:program|school|university|college)[^.\n]{0,40}\blocated in the (?:u\.?s\.?|united states)", re.I),
    re.compile(r"\b(?:u\.?s\.?|usa|united states)[- ]based (?:applicants|candidates) only\b", re.I),
    re.compile(r"\bonly (?:open to|accepting|considering) (?:applicants|candidates|residents)[^.\n]{0,30}\b(?:u\.?s\.?|united states)\b", re.I),
]


def posting_sponsorship(locations: list[str], text: str) -> set[str]:
    """#9's rules on a posting that was read, plus the extra refusal phrasings above."""
    out = set(job_rules.sponsorship(locations, text))
    if out == {"unknown"} and any(rx.search(text) for rx in EXTRA_NO_SPONSOR_RX):
        return {"no_sponsor"}
    return out


# -- model output -> Tags -------------------------------------------------------------------------


def parse_model_json(raw: str) -> dict:
    """The first JSON object in a model's reply (tolerates code fences and leading prose)."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    start = raw.find("{")
    if start < 0:
        raise ValueError("no JSON object")
    obj, _ = json.JSONDecoder().raw_decode(raw[start:])
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    return obj


def dims_from_model(obj: dict) -> dict[str, set[str]]:
    """Valid values per dimension; a missing, empty or invalid list becomes every value (fail open)."""
    out = {}
    for name, values in ALL.items():
        got = obj.get(name)
        if isinstance(got, str):
            got = [got]
        vals = {v for v in (got or []) if v in values} if isinstance(got, list) else set()
        out[name] = vals or set(values)
    return out


def to_tags(dims: dict[str, set[str]], evidence: str = "") -> Tags:
    return Tags(**{name: sorted(dims[name]) for name in ALL}, evidence=evidence[:200])


# -- contract guards (docs/tags.md), applied after any model ------------------------------------------


def guard(dims: dict[str, set[str]], inp: StoryInputs, image_texts: Iterable[str] = ()) -> dict[str, set[str]]:
    """Deterministic fixes the contract requires, whatever the model said.

    1. A job link keeps ``job_posting`` in the post type, unless the page was read and is an event page.
    2. Sponsorship ``{unknown}`` is a fact about a posting that was read. When the link's page was not
       read, a bare ``{unknown}`` becomes "every value" unless the image itself settles it.
    3. Known sponsors (Tesla) are ``sponsor_or_canadian`` unless the posting says it won't sponsor.
    """
    dims = {k: set(v) for k, v in dims.items()}
    lst = inp.listing
    if inp.links and "job_posting" not in dims["post_type"]:
        event_page = lst is not None and lst.read_ok and lst.is_event_page and "event" in dims["post_type"]
        if not event_page:
            dims["post_type"].add("job_posting")
    read = lst is not None and lst.read_ok
    if inp.links and not read and dims["sponsorship"] == {"unknown"}:
        dims["sponsorship"] = set(ALL["sponsorship"])
    names = [lst.company if lst else "", lst.title if lst else "", inp.sidecar_company, *image_texts]
    if is_known_sponsor(names) and not (read and posting_sponsorship(lst.locations, lst.description) == {"no_sponsor"}):
        dims["sponsorship"] = {"sponsor_or_canadian"}
    return dims
