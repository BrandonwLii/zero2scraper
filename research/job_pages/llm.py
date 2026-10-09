"""Ask a local model (llama.cpp's llama-server, OpenAI-compatible API) for sponsorship, level and location.

Only ever pointed at a server on this machine: posting text never leaves it. Output is
constrained to a JSON schema, and sets follow docs/tags.md (values it can't rule out).
"""

from __future__ import annotations

import json
import re

import requests

import rules

TIMINGS: list[dict] = []  # llama-server's per-request timings, for the benchmark

SPONSORSHIP = ["sponsor_or_canadian", "no_sponsor", "unknown"]
LEVEL = ["internship", "new_grad", "other"]
NO_SPONSOR_ID = "no_sponsor"

SCHEMA = {
    "type": "object",
    "properties": {
        "work_location": {"type": "string"},
        "in_or_open_to_canada": {"type": "boolean"},
        "work_authorization_evidence": {"type": "string"},
        "sponsorship": {"type": "array", "items": {"enum": SPONSORSHIP}, "minItems": 1, "uniqueItems": True},
        "level": {"type": "array", "items": {"enum": LEVEL}, "minItems": 1, "uniqueItems": True},
    },
    "required": ["work_location", "in_or_open_to_canada", "work_authorization_evidence", "sponsorship", "level"],
}

PROMPT = """You tag job postings for Canadian students. Read the posting and answer in JSON.

sponsorship (from a Canadian student's point of view):
- "sponsor_or_canadian": the job is located in Canada, is open to people in Canada (e.g. remote in Canada or North America), or the posting says it sponsors work visas. A job in Canada is this value even if it says it won't sponsor.
- "no_sponsor": the posting says it won't sponsor, or requires citizenship, permanent residence, a security clearance, "U.S. person" status, or existing work authorization in another country, and the job isn't in or open to Canada.
- "unknown": the posting says nothing about sponsorship or work authorization and isn't in Canada. Application-form questions ("Will you require sponsorship?") are not a policy.
Do not infer from what the company usually does. If unsure between values, list each one you can't rule out.
A location in the United States or elsewhere outside Canada is NOT a reason for "no_sponsor" on its own: "no_sponsor" needs a sentence that refuses sponsorship or requires citizenship, clearance or existing authorization. If work_authorization_evidence is "" and the job isn't in Canada, sponsorship is ["unknown"].

level:
- "internship": internship, co-op, work term, placement, student researcher.
- "new_grad": full-time role for recent or upcoming graduates, entry level, early career, or 0-2 years of experience.
- "other": more than about 2 years of experience, senior, staff, manager.
A full-time title with no level and no experience requirement is ["new_grad", "other"].

work_location: the location(s) as written. work_authorization_evidence: quote the sentence about sponsorship, citizenship or work authorization, or "" if none.

Title: {title}
Locations: {locations}
Employment type: {etype}

Posting:
{text}
"""

MAX_CHARS = 9000  # keeps prompts around 2-3k tokens on a CPU


def ask(base: str, rec: dict, text: str | None = None, temperature: float = 0.0) -> dict:
    body = (text if text is not None else rec["text"])[:MAX_CHARS]
    prompt = PROMPT.format(title=rec.get("title") or "?", locations="; ".join(rec.get("locations") or []) or "?",
                           etype=rec.get("employment_type") or "?", text=body)
    resp = requests.post(f"{base}/v1/chat/completions", timeout=600, json={
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": 400,
        "response_format": {"type": "json_schema", "json_schema": {"name": "tags", "schema": SCHEMA}},
    })
    resp.raise_for_status()
    TIMINGS.append(resp.json().get("timings") or {})
    return json.loads(resp.json()["choices"][0]["message"]["content"])


_LEVEL_LINES = re.compile(r"years?|graduat|enrolled|pursuing|student|intern|co-?op|entry|junior|senior|location|based|office|"
                          r"remote|hybrid|on-?site|canada", re.I)


def snippets(text: str, limit: int = 2500) -> str:
    """Only the sentences that bear on sponsorship, location or level: a short prompt for a small CPU."""
    sents = [s for s in re.split(r"(?<=[.!?])\s+|\n", text) if s.strip()]
    keep = [s for s in sents if rules._WORK_AUTH.search(s) or _LEVEL_LINES.search(s)]
    return "\n".join(dict.fromkeys(keep))[:limit]


_MEMO: dict = {}


def tags(base: str, rec: dict, runs: int = 1, mode: str = "full") -> dict:
    key = (rec["url"], runs, mode)
    if key not in _MEMO:
        _MEMO[key] = _tags(base, rec, runs, mode)
    return _MEMO[key]


def _tags(base: str, rec: dict, runs: int, mode: str) -> dict:
    out = {"sponsorship": set(), "level": set(), "canada": False}
    text = snippets(rec["text"]) if mode == "snippets" else None
    for i in range(runs):
        a = ask(base, rec, text=text, temperature=0.0 if i == 0 else 0.7)
        spons = set(a["sponsorship"])
        quote = " ".join(a["work_authorization_evidence"].split()).lower()[:80]
        if NO_SPONSOR_ID in spons and (not quote or quote not in " ".join(rec["text"].split()).lower()):
            spons.add("unknown")  # a refusal the model can't quote from the posting: don't let it veto alone
        out["sponsorship"] |= spons
        out["level"] |= set(a["level"])
        out["canada"] = out["canada"] or bool(a["in_or_open_to_canada"])
    return out


def hybrid(base: str, rec: dict, rule: dict, runs: int = 1, mode: str = "snippets") -> dict:
    """Fail-open union: rules and model must both rule a value out for it to go."""
    model = tags(base, rec, runs, mode)
    return {"sponsorship": rule["sponsorship"] | model["sponsorship"], "level": rule["level"] | model["level"],
            "canada": rule["canada"] or model["canada"]}
