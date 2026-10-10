"""The hybrid tagger's rules (#10, docs/research/tagger-comparison.md): pure functions, no I/O.

The model decides the post type and the role; these rules decide company (the lists in
``companies.py``), sponsorship (#9's posting rules plus the user's decisions) and level (title
words), add the contract guards from docs/tags.md, and give the fallback tags when no model is
available (``listing_rules``). Ported from #10's research/tagger/common.py and taggers.py; the
behaviour is frozen with the #10 scores, so re-score (scripts/eval_tagger.py) after any change.

Sets are plain ``set[str]`` of the stable value strings until ``to_tags``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from . import posting_rules
from .companies import is_known_sponsor, match_companies
from .jobs import is_job_link
from .tags import DIMENSIONS, Tags

ALL: dict[str, list[str]] = {name: [m.value for m in cls] for name, cls in DIMENSIONS.items()}

# Bump with any change to these rules, the prompt or the request; part of the tagger version.
RULES_VERSION = "hybrid-1"


# -- inputs ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Listing:
    """The posting behind the link, as the tagger sees it (from jobs.JobPosting)."""

    status: str  # ok | closed | failed
    title: str = ""
    company: str = ""
    locations: tuple[str, ...] = ()
    employment_type: str = ""
    description: str = ""
    is_event_page: bool = False

    @property
    def read_ok(self) -> bool:
        return self.status == "ok" and len(self.description) >= 200

    @classmethod
    def from_posting(cls, posting: Any) -> Optional["Listing"]:
        if posting is None:
            return None
        return cls(
            status=posting.status,
            title=posting.title or "",
            company=posting.company or "",
            locations=tuple(posting.locations or ()),
            employment_type=posting.employment_type or "",
            description=posting.description or "",
            is_event_page=bool(posting.is_event_page),
        )


@dataclass(frozen=True)
class StoryInputs:
    links: tuple[str, ...] = ()
    mentions: tuple[str, ...] = ()
    sidecar_title: str = ""
    sidecar_company: str = ""
    listing: Optional[Listing] = None

    @property
    def has_job_link(self) -> bool:
        return any(is_job_link(u) for u in self.links)

    @classmethod
    def from_item(cls, item: Any) -> "StoryInputs":
        return cls(
            links=tuple(item.links),
            mentions=tuple(item.mentions),
            sidecar_title=item.job_title or "",
            sidecar_company=item.company or "",
            listing=Listing.from_posting(item.posting) if item.links else None,
        )


def listing_snippet(listing: Listing, head_chars: int = 600, auth_chars: int = 700) -> str:
    """The start of the description plus every sentence about work authorization (#9's selector)."""
    text = listing.description
    head = " ".join(text[:head_chars].split())
    auth, n = [], 0
    for s in posting_rules.work_auth_sentences(text):
        s = " ".join(s.split())
        if s and s not in head and n + len(s) <= auth_chars:
            auth.append(s)
            n += len(s)
    out = head + ("…" if len(text) > head_chars else "")
    if auth:
        out += "\nWork-authorization sentences: " + " | ".join(auth)
    return out


def context_text(inp: StoryInputs, ocr_text: Optional[str] = None) -> str:
    """The text that goes into the prompt next to the image. It holds story text: never log it."""
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


# -- model output ----------------------------------------------------------------------------------


def parse_model_json(raw: str) -> dict:
    """The first JSON object in a model's reply (tolerates code fences and leading prose)."""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
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


def _strings(v: Any) -> list[str]:
    return [str(x) for x in v if isinstance(x, (str, int, float))] if isinstance(v, list) else []


def to_tags(dims: dict[str, set[str]], evidence: str = "") -> Tags:
    return Tags(**{name: sorted(dims[name]) for name in ALL}, evidence=evidence[:200])


# -- title and text rules --------------------------------------------------------------------------

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
    if posting_rules.INTERN_RX.search(title):
        return {"internship"}
    if posting_rules.NEW_GRAD_RX.search(title):
        return {"new_grad"}
    if posting_rules.OTHER_TITLE_RX.search(title):
        return {"other"}
    if re.search(r"(?i:\b(?:engineer|developer|scientist|analyst))\s+(?:I|1)\b|(?i:\blevel 1\b)", title):
        return {"new_grad", "other"}
    return None


EVENT_RX = re.compile(r"\binfo(?:rmation)? session|\bcoffee chat|\bwebinar|\bhackathon|\bcareer fair|\bworkshop|\bconference|\bregister\b|"
                      r"\bRSVP\b|\bsign up\b|\bopen house|\bmeet (?:the|our) team|\bnetworking|\bfireside|\bday\b[^\n]{0,15}\b(?:2026|2027)\b", re.I)
PROCESS_RX = re.compile(r"\binterview|\bOA\b|\bonline assessment|\boffer\b|\brecruiter|\bsuper ?day|\bfinal round|\bphone screen|\btechnical round|"
                        r"\bbehavio(?:u)?ral\b|\bleetcode|\brejected|\btimeline", re.I)

# Event, form and link-in-bio hosts: their page title and company name say nothing about the employer.
PLATFORM_HOSTS = ("teams.microsoft.com", "zoom.us", "eventbrite.", "lu.ma", "linktr.ee", "forms.gle", "docs.google.com",
                  "forms.office.com", "youtube.com", "youtu.be", "luma.com", "calendly.com", "typeform.com")


def _link_texts(inp: StoryInputs) -> list[str]:
    return [f"{urlsplit(u).hostname or ''} {urlsplit(u).path}" for u in inp.links]


def _platform_link(inp: StoryInputs) -> bool:
    hosts = [(urlsplit(u).hostname or "").lower() for u in inp.links]
    return bool(hosts) and all(any(p in h for p in PLATFORM_HOSTS) for h in hosts)


def rule_sponsorship(inp: StoryInputs, image_texts: list[str]) -> set[str]:
    """#9's rules on a posting that was read; otherwise only what the image itself says."""
    lst = inp.listing
    if lst is not None and lst.read_ok:
        out = posting_rules.posting_sponsorship(list(lst.locations), lst.description)
    else:
        text = "\n".join(t for t in image_texts if t)
        said = posting_rules.sponsorship([], text) if text else {"unknown"}
        settled = said != {"unknown"} and (posting_rules.in_canada([], text) or said == {"no_sponsor"})
        out = said if settled else set(ALL["sponsorship"])
    if is_known_sponsor([lst.company if lst else "", lst.title if lst else "", inp.sidecar_company, *image_texts]):
        if not (lst is not None and lst.read_ok and out == {"no_sponsor"}):
            out = {"sponsor_or_canadian"}
    return out


def rule_company(inp: StoryInputs, image_texts: list[str], fallback: set[str], image_names: Iterable[str] = ()) -> set[str]:
    """List match on the listing company, the link and the image text. A company that is named (by the
    listing, or by the model reading the image) but is on neither list is other; otherwise ``fallback``."""
    lst = inp.listing
    if _platform_link(inp):  # the page belongs to the tool hosting it, not the hiring company
        named, links = [*image_names], []
    else:
        named, links = [lst.company if lst else "", inp.sidecar_company, *image_names], _link_texts(inp)
    groups = match_companies([*named, *links, *image_texts])
    if groups:
        return groups
    if any(n.strip() for n in named):
        return {"other"}
    return set(fallback)


def rule_level(inp: StoryInputs, titles: list[str], fallback: set[str], post_type: Iterable[str] = frozenset({"job_posting"})) -> set[str]:
    """Title words first. The posting-text fallback (years of experience, else "new grad or other")
    is for job postings only; for a possible event or process post the model's (or fallback) set stays."""
    lst = inp.listing
    for title in [lst.title if lst else "", inp.sidecar_title, *titles]:
        got = level_from_title(title or "")
        if got:
            return got
    if lst is not None and lst.read_ok and set(post_type) == {"job_posting"}:
        return set(posting_rules.level(lst.title or inp.sidecar_title, lst.description, lst.employment_type))
    return set(fallback)


# -- the contract guards (docs/tags.md), applied after the model -------------------------------------


def guard(dims: dict[str, set[str]], inp: StoryInputs, image_texts: Iterable[str] = ()) -> dict[str, set[str]]:
    """Deterministic fixes the contract requires, whatever the model said.

    1. A link keeps ``job_posting`` in the post type, unless the page was read and is an event page.
    2. Sponsorship ``{unknown}`` is a fact about a posting that was read. When the link's page was not
       read, a bare ``{unknown}`` becomes "every value".
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
    if is_known_sponsor(names) and not (read and posting_rules.posting_sponsorship(list(lst.locations), lst.description) == {"no_sponsor"}):
        dims["sponsorship"] = {"sponsor_or_canadian"}
    return dims


# -- the two taggers ---------------------------------------------------------------------------------


def hybrid_tags(reply: dict, inp: StoryInputs, ocr: Optional[str]) -> Tags:
    """Pipeline 3 with role rules (#10's recommended ``hybrid_q4b4_i512_rolerules``): the model's
    post type and role, rules for company, sponsorship and level, then the guards."""
    companies = _strings(reply.get("companies"))
    titles = _strings(reply.get("job_titles"))
    evidence = str(reply.get("evidence", ""))
    dims = guard(dims_from_model(reply), inp, [*companies, *titles])
    ocr = ocr or ""
    image_texts = [", ".join(companies), *titles, ocr]
    dims["company"] = rule_company(inp, [ocr], dims["company"], companies)
    # No link sticker: a story the model calls a job posting or misc may well be an interview report
    # or tips. Keep process_info when the image text has hiring-process words (fail open).
    if not inp.links and dims["post_type"] & {"job_posting", "misc"} and PROCESS_RX.search(f"{ocr}\n{evidence}"):
        dims["post_type"].add("process_info")
    dims["sponsorship"] = rule_sponsorship(inp, image_texts)
    dims["level"] = rule_level(inp, titles, dims["level"], dims["post_type"])
    # docs/tags.md: an event or process post for students in general is {internship, new_grad}.
    # Small models usually pick one of the two, so widen (fail open) unless a title settled it.
    if "job_posting" not in dims["post_type"] and dims["level"] < {"internship", "new_grad"} and not any(level_from_title(t) for t in titles):
        dims["level"] = {"internship", "new_grad"}
    title = (inp.listing.title if inp.listing else "") or inp.sidecar_title
    ruled = role_from_text([title] if title else titles)
    if ruled != set(ALL["role"]):
        dims["role"] = ruled
    return to_tags(dims, evidence)


def listing_rules(inp: StoryInputs, ocr: Optional[str]) -> Tags:
    """The fallback when the model is unavailable (#10's ``ocr_rules``, or ``listing_rules`` when
    there is no OCR text): link and posting rules, plus keyword rules on the OCR text."""
    if ocr is None and not inp.links:
        return Tags.unsure()  # nothing to read without the image
    ocr = ocr or ""
    lst = inp.listing
    title = (lst.title if lst else "") or inp.sidecar_title
    page = f"{title}\n{lst.description[:400] if lst and lst.read_ok else ''}"
    post: set[str] = set()
    if inp.links:
        post.add("job_posting")
        if EVENT_RX.search(page) or (lst is not None and lst.is_event_page):
            post.add("event")
    else:
        if PROCESS_RX.search(ocr):
            post.add("process_info")
        if EVENT_RX.search(ocr):
            post.add("event")
        if posting_rules.INTERN_RX.search(ocr) or posting_rules.NEW_GRAD_RX.search(ocr) or "hiring" in ocr.lower():
            post.add("job_posting")
        post = post or {"misc"}
    level_text = ocr if not title else title
    lv = set()
    if posting_rules.INTERN_RX.search(level_text):
        lv.add("internship")
    if posting_rules.NEW_GRAD_RX.search(level_text):
        lv.add("new_grad")
    dims = {
        "post_type": post,
        "company": rule_company(inp, [ocr], set(ALL["company"])),
        "role": role_from_text([title] if title else [ocr]),
        "level": rule_level(inp, [], lv or set(ALL["level"]), post),
        "sponsorship": rule_sponsorship(inp, [ocr]),
    }
    return to_tags(dims)
