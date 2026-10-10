"""Text rules on a job posting: sponsorship, "in or open to Canada" and level (#9, docs/tags.md).

Inputs are a posting's structured fields (title, locations, employment type) plus its plain-text
description. Every function returns the set of values it can't rule out. Pure: no I/O. Ported from
#9's research/job_pages/rules.py, plus the refusal phrasings from the user's decisions (#10).
"""

from __future__ import annotations

import re

SPONSOR, NO_SPONSOR, UNKNOWN = "sponsor_or_canadian", "no_sponsor", "unknown"
INTERN, NEW_GRAD, OTHER = "internship", "new_grad", "other"

# -- location ----------------------------------------------------------------------------------

_PROVINCES = ("ontario|british columbia|quebec|québec|alberta|manitoba|saskatchewan|nova scotia|new brunswick|"
              "newfoundland|prince edward island|yukon|nunavut|northwest territories")
_CITIES = ("toronto|vancouver|montr[eé]al|ottawa|waterloo|kitchener|calgary|edmonton|mississauga|markham|"
           "burnaby|winnipeg|halifax|victoria, bc|hamilton, on|london, on|oakville|brampton|richmond hill|"
           "vaughan|quebec city|gatineau|saskatoon|regina|surrey, bc|laval|guelph")
_NAME = r"(?-i:[A-Z])[\w.'’-]*(?:\s+(?-i:[A-Z])[\w.'’-]*){0,3}"  # a capitalised place name
_CA_ABBR = r"(?-i:ON|BC|QC|AB|MB|SK|NS|NB|NL|PE)\b"  # capitals only: ", on Tuesday" is not Ontario
# A Canadian place named anywhere (a location string, or text for the fail-open check in sponsorship()).
CANADA_PLACE = re.compile(rf"\bcanada\b|\bcanadian\b|(?-i:\bCAN\b)|\b(?:{_PROVINCES}|{_CITIES})\b|,\s*{_CA_ABBR}", re.I)
# Location strings also use the country code "CA" (but "CA" alone could be California in body text).
CANADA = re.compile(rf"{CANADA_PLACE.pattern}|^CA$|-\s*CA\b|\bCA-\w", re.I | re.M)
# Body text: only an explicit statement that the role is in or open to Canada.
CANADA_IN_TEXT = re.compile(
    rf"(?:located|based|work(?:ing)?|office|remote|hybrid|on-?site|position is|role is|open to (?:candidates|applicants) in)"
    rf"[^.\n]{{0,40}}\b(?:canada|{_CITIES})\b"
    rf"|\b(?:canada|{_CITIES})\b[^.\n]{{0,20}}\b(?:office|based|hybrid|on-?site|remote)\b"
    # "relocate throughout Canada", "Canada-based", "positions in Canada" (not "customers in Canada")
    rf"|\brelocat\w*\s+(?:throughout|to|within|across|in)\s+(?:the\s+)?(?:canada|{_CITIES})\b"
    rf"|\bcanada[- ]based\b"
    rf"|\b(?:positions?|roles?|jobs?|openings?|opportunit\w+|vacanc\w+|hiring|candidates?|applicants?|"
    rf"locations?|offices?|work|working|located|based|employed|authori[sz]ed to work|eligible to work|"
    rf"available|relocat\w+)\b[^.\n]{{0,40}}\bin (?:the )?canada\b"
    # pay in Canadian dollars: "50,405.00 CAD Annually"
    rf"|\b\d[\d,]*(?:\.\d+)?\s*(?-i:CAD)\b"
    # structured location strings: "Toronto, ON, CAN", "CAN, ON, Toronto", "Toronto, Ontario, Canada", "Vancouver, BC"
    rf"|\b{_NAME},\s*{_CA_ABBR}"
    rf"|(?-i:\bCAN),\s*{_CA_ABBR}"
    rf"|\b{_NAME},\s*(?:{_PROVINCES})\b"
    rf"|\b{_NAME},\s*canada\b",
    re.I,
)
REMOTE_NA = re.compile(r"remote[^.\n]{0,30}\b(?:north america|canada|us (?:or|and|/) canada)\b", re.I)


def in_canada(locations: list[str], text: str = "") -> bool | None:
    """True if a location (or the text) puts the job in or open to Canada; None if no location at all."""
    if any(CANADA.search(l) for l in locations):
        return True
    if REMOTE_NA.search(text) or CANADA_IN_TEXT.search(text):
        return True
    return False if locations else None


# -- sponsorship ---------------------------------------------------------------------------------

_NEG = r"(?:not|unable to|will not|won.t|cannot|can.t|do not|does not|don.t|doesn.t|no longer|is not able to|are not able to)"
NO_SPONSOR_RX = [
    re.compile(rf"\b{_NEG}\b[^.\n]{{0,60}}\b(?:sponsor|sponsorship)", re.I),
    re.compile(r"\b(?:sponsorship|sponsor)[^.\n]{0,40}\b(?:not|unavailable|isn.t)\b[^.\n]{0,20}(?:available|offered|provided|possible|eligible)", re.I),
    re.compile(r"\bwithout (?:the need for |requiring |needing )?(?:current or future |future |any )?(?:employer |company |visa )?sponsorship", re.I),
    re.compile(r"\bno (?:visa |immigration )?sponsorship\b", re.I),
    re.compile(r"\bmust be (?:a )?(?:u\.?s\.? citizen|united states citizen|citizen of the united states)", re.I),
    re.compile(r"\b(?:u\.?s\.?|united states) citizenship (?:is )?(?:required|is a requirement)", re.I),
    re.compile(r"\brequires? (?:u\.?s\.?|united states) citizenship", re.I),
    re.compile(r"\b(?:active|current|ability to obtain|eligib\w+ (?:for|to obtain)|obtain and maintain)[^.\n]{0,40}"
               r"\b(?:security clearance|secret clearance|ts/sci|top secret)", re.I),
    re.compile(r"\b(?:only (?:employ|hire|consider)|must be|must currently be)\b[^.\n]{0,40}\b(?:legally )?authori[sz]ed to work in the "
               r"(?:u\.?s\.?|united states)", re.I),
    re.compile(r"\bu\.?s\.? persons? (?:status )?(?:is |are )?required", re.I),
    re.compile(r"\b(?:itar|export control)[^.\n]{0,120}\b(?:u\.?s\.? persons?|citizens?|permanent residents?)", re.I),
]
SPONSOR_RX = [
    re.compile(r"\b(?:will|can|may|able to|happy to|do|does)\s+(?:provide\s+|offer\s+)?(?:visa\s+)?sponsor", re.I),
    re.compile(r"\b(?:visa|immigration) sponsorship (?:is |will be )?(?:available|offered|provided)", re.I),
    re.compile(r"\bsponsorship (?:is )?available\b", re.I),
]
_WORK_AUTH = re.compile(r"sponsor|work authori[sz]|authori[sz]ed to work|citizenship|clearance|permanent resident|"
                        r"\bvisa\b|\bitar\b|export control", re.I)


def sponsorship(locations: list[str], text: str, read_ok: bool = True) -> set[str]:
    if not read_ok:
        return {SPONSOR, NO_SPONSOR, UNKNOWN}
    if in_canada(locations, text):
        return {SPONSOR}
    out = _sponsorship_from_text(text)
    # Fail open: no extracted location, yet the text names a Canadian place. A missed Canadian job is
    # the worst failure (docs/tags.md), so a bare no_sponsor (or unknown) must not stand.
    if not locations and CANADA_PLACE.search(text):
        out = out | {SPONSOR}
    return out


def _sponsorship_from_text(text: str) -> set[str]:
    # A "will sponsor" match inside a negated sentence is a refusal, so test refusals first.
    no = [rx.search(text) for rx in NO_SPONSOR_RX]
    yes = [rx.search(text) for rx in SPONSOR_RX]
    if any(no):
        no_spans = [m.span() for m in no if m]
        if any(m and not any(a <= m.start() < b + 40 for a, b in no_spans) for m in yes):
            return {SPONSOR, NO_SPONSOR}  # both said, in different places: let a person decide
        return {NO_SPONSOR}
    if any(yes):
        return {SPONSOR}
    return {UNKNOWN}


def work_auth_sentences(text: str) -> list[str]:
    """Sentences that mention sponsorship or work authorization (for the model, or for review)."""
    sents = re.split(r"(?<=[.!?])\s+|\n", text)
    return [s for s in sents if _WORK_AUTH.search(s)]


# -- level ----------------------------------------------------------------------------------------

INTERN_RX = re.compile(r"\bintern(?:ship)?s?\b|\bco-?op\b|\bwork term\b|\bplacement (?:student|year)\b|"
                       r"\b(?:summer|fall|winter|spring|autumn) (?:20\d\d )?(?:analyst|associate|student)\b|"
                       r"\bstudent (?:researcher|developer|engineer)\b|\bapprentice", re.I)
NEW_GRAD_RX = re.compile(r"\bnew (?:college )?grad|\b(?:university|college|recent) grad|\bgraduate (?:program|programme|20\d\d)|"
                         r"\b20\d\d (?:graduate|grad)s?\b|\bentry[- ]level\b|\bearly[- ]career\b|\bcampus hire|"
                         r"\b(?:analyst|associate|engineer) program\b", re.I)
OTHER_TITLE_RX = re.compile(r"\b(?:senior|sr\.?|staff|principal|lead|manager|director|head of|vp|architect|"
                            r"distinguished|expert)\b|\b(?:engineer|developer|scientist)\s+(?:i{2,3}|iv|v|[2-5])\b", re.I)
# Linear on long whitespace runs: the optional range owns its leading whitespace (the same matches as
# "\s*(?:range)?\s*", which was quadratic: ~40 s on a posting of one digit and 50 KB of spaces).
_YEARS = re.compile(r"\b(\d{1,2})(?:\s*(?:\+|-\s*\d+|to \d+))?\s*(?:\+\s*)?years?\b[^.\n]{0,40}\b(?:experience|industry|professional)", re.I)
_STUDENT = re.compile(r"\b(?:currently )?(?:enrolled|pursuing)\b[^.\n]{0,60}\b(?:degree|bachelor|master|phd|program)|"
                      r"\breturn(?:ing)? to (?:school|your studies)\b", re.I)


def min_years(text: str) -> int | None:
    years = [int(m.group(1)) for m in _YEARS.finditer(text)]
    return min(years) if years else None


def level(title: str | None, text: str, employment_type: str | None = None) -> set[str]:
    title = title or ""
    if not title and not text:
        return {INTERN, NEW_GRAD, OTHER}
    if INTERN_RX.search(title) or re.search(r"intern", employment_type or "", re.I):
        return {INTERN}
    if NEW_GRAD_RX.search(title):
        return {NEW_GRAD}
    if OTHER_TITLE_RX.search(title):
        return {OTHER}
    years = min_years(text)
    if years is not None:
        return {NEW_GRAD} if years <= 2 else {OTHER}
    head = text[:3000]
    if _STUDENT.search(head) and INTERN_RX.search(head):
        return {INTERN}
    if NEW_GRAD_RX.search(head):
        return {NEW_GRAD, OTHER}
    return {NEW_GRAD, OTHER}


# Refusal phrasings #9's rules miss, from the user's decisions (#9, #10; docs/tags.md): "must be a
# U.S. person", "must be enrolled at a US school" and US-only applicant pools are no_sponsor for a
# Canadian student. sponsorship() already returns sponsor_or_canadian when the job is in or open
# to Canada, so "US-based applicants only" never overrides a Canadian location.
EXTRA_NO_SPONSOR_RX = [
    re.compile(r"\bmust be (?:a )?u\.?s\.? persons?\b", re.I),
    re.compile(r"\benrolled in (?:an? )?(?:academic )?(?:program|school|university|college)[^.\n]{0,40}\blocated in the (?:u\.?s\.?|united states)", re.I),
    re.compile(r"\b(?:u\.?s\.?|usa|united states)[- ]based (?:applicants|candidates) only\b", re.I),
    re.compile(r"\bonly (?:open to|accepting|considering) (?:applicants|candidates|residents)[^.\n]{0,30}\b(?:u\.?s\.?|united states)\b", re.I),
]


def posting_sponsorship(locations: list[str], text: str) -> set[str]:
    """sponsorship() on a posting that was read, plus the extra refusal phrasings above."""
    out = set(sponsorship(locations, text))
    if out == {UNKNOWN} and any(rx.search(text) for rx in EXTRA_NO_SPONSOR_RX):
        return {NO_SPONSOR}
    return out
