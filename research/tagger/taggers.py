"""Tagger prototypes for #10, loadable by the #6 harness as ``--tagger taggers:<factory>``.

Run with ``PYTHONPATH=research/tagger``. Model taggers read the replies saved by ``run_requests.py``
(or ``claude_ref.py``) under ``$TAGGER_WORK/raw/<label>/``, so scoring never calls a model and every
pipeline is scored on the same replies that were timed in CT 120. A story without a saved reply
raises, and the harness scores it as ``Tags.unsure()`` (the watcher's fail-open fallback).

Pipelines (docs/research/tagger-comparison.md):
1. ``vlm_*``: one Qwen3-VL call per image, plus the contract guards (``*_raw`` without them).
2. ``ocr_rules``: RapidOCR text, then text rules. ``ocrtext_*``: RapidOCR text, then the 2B as a text model.
3. ``hybrid_*``: rules for company, sponsorship and level; the model for post type and role.
4. ``claude_*``: Claude Sonnet through headless Claude Code, the accuracy reference.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Optional

from story_watch.classify import build_classifier
from story_watch.evaluation.taggers import ClassifierAdapter, UnsureBaseline
from story_watch.tags import Tags

from common import (ALL, RAW, StoryInputs, dims_from_model, guard, is_known_sponsor, job_rules, level_from_title,
                    load_ocr, match_companies, posting_sponsorship, parse_model_json, role_from_text, story_inputs, to_tags, EVENT_RX, PROCESS_RX)

_HERE = Path(__file__).resolve().parent
PREFIX = os.environ.get("TAGGER_RAW_PREFIX", "ct")  # "ct" = replies produced in CT 120, "ws" = workstation


def _code_version(*extra: str) -> str:
    h = hashlib.sha256()
    for name in ("common.py", "taggers.py", "prompting.py"):
        h.update((_HERE / name).read_bytes())
    for e in extra:
        h.update(e.encode())
    return h.hexdigest()[:8]


def load_reply(label: str, media_id: str) -> dict:
    """The saved model reply as a dict; raises when missing or failed (scored as unsure)."""
    path = RAW / label / f"{media_id}.json"
    rec = json.loads(path.read_text(encoding="utf-8"))
    if rec.get("error"):
        raise RuntimeError(rec["error"])
    if "structured_output" in rec and isinstance(rec["structured_output"], dict):  # claude -p --json-schema
        return rec["structured_output"]
    return parse_model_json(rec.get("content") or rec.get("result") or "")


# -- rules shared by the OCR and hybrid pipelines ---------------------------------------------------


def _link_texts(inp: StoryInputs) -> list[str]:
    from urllib.parse import urlsplit

    return [f"{urlsplit(u).hostname or ''} {urlsplit(u).path}" for u in inp.links]


# Event, form and link-in-bio hosts: their page title and company name say nothing about the employer.
PLATFORM_HOSTS = ("teams.microsoft.com", "zoom.us", "eventbrite.", "lu.ma", "linktr.ee", "forms.gle", "docs.google.com",
                  "forms.office.com", "youtube.com", "youtu.be", "luma.com", "calendly.com", "typeform.com")


def _platform_link(inp: StoryInputs) -> bool:
    from urllib.parse import urlsplit

    hosts = [(urlsplit(u).hostname or "").lower() for u in inp.links]
    return bool(hosts) and all(any(p in h for p in PLATFORM_HOSTS) for h in hosts)


def rule_sponsorship(inp: StoryInputs, image_texts: list[str]) -> set[str]:
    """#9's rules on a posting that was read; otherwise only what the image itself says."""
    lst = inp.listing
    if lst is not None and lst.read_ok:
        out = posting_sponsorship(lst.locations, lst.description)
    else:
        text = "\n".join(t for t in image_texts if t)
        said = job_rules.sponsorship([], text) if text else {"unknown"}
        out = said if said != {"unknown"} and (job_rules.in_canada([], text) or said == {"no_sponsor"}) else set(ALL["sponsorship"])
    if is_known_sponsor([lst.company if lst else "", lst.title if lst else "", inp.sidecar_company, *image_texts]):
        if not (lst is not None and lst.read_ok and out == {"no_sponsor"}):
            out = {"sponsor_or_canadian"}
    return out


def rule_company(inp: StoryInputs, image_texts: list[str], fallback: set[str], image_names: list[str] = ()) -> set[str]:
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


def rule_level(inp: StoryInputs, titles: list[str], fallback: set[str], post_type: set[str] = frozenset({"job_posting"})) -> set[str]:
    """Title words first. The posting-text fallback (years of experience, else "new grad or other")
    is for job postings only; for a possible event or process post the model's (or fallback) set stays."""
    lst = inp.listing
    for title in [lst.title if lst else "", inp.sidecar_title, *titles]:
        got = level_from_title(title or "")
        if got:
            return got
    if lst is not None and lst.read_ok and post_type == {"job_posting"}:
        return set(job_rules.level(lst.title or inp.sidecar_title, lst.description, lst.employment_type))
    return set(fallback)


# -- taggers ----------------------------------------------------------------------------------------------


class ModelTagger:
    """Pipelines 1, 2 (text model) and 4: the model's lists, then (optionally) the contract guards."""

    def __init__(self, label: str, name: str, guarded: bool = True):
        self.label, self.guarded = label, guarded
        self.name = name + ("" if guarded else "-raw")
        self.version = _code_version(label, str(guarded))

    def dims(self, story) -> tuple[dict[str, set[str]], dict, StoryInputs]:
        inp = story_inputs(story)
        obj = load_reply(self.label, story.media_id)
        dims = dims_from_model(obj)
        if self.guarded:
            dims = guard(dims, inp, [*obj.get("companies", []), *obj.get("job_titles", [])])
        return dims, obj, inp

    def tag(self, story) -> Tags:
        dims, obj, _ = self.dims(story)
        return to_tags(dims, str(obj.get("evidence", "")))


class HybridTagger:
    """Pipeline 3: company, sponsorship and level from rules; post type and role from the model."""

    def __init__(self, base: ModelTagger, name: str, use_ocr: bool = True, role_rules: bool = False):
        self.base, self.use_ocr, self.role_rules = base, use_ocr, role_rules
        self.name = name + ("-rolerules" if role_rules else "")
        self.version = _code_version(base.label, "hybrid", str(use_ocr), str(role_rules))

    def tag(self, story) -> Tags:
        dims, obj, inp = self.base.dims(story)
        companies = [str(x) for x in obj.get("companies", [])]
        titles = [str(x) for x in obj.get("job_titles", [])]
        ocr = (load_ocr(story.media_id) or "") if self.use_ocr else ""
        image_texts = [", ".join(companies), *titles, ocr]
        dims["company"] = rule_company(inp, [ocr], dims["company"], companies)
        # No link sticker: a story the model calls a job posting or misc may well be an interview report
        # or tips. Keep process_info when the image text has hiring-process words (fail open).
        if not inp.links and dims["post_type"] & {"job_posting", "misc"} and PROCESS_RX.search(f"{ocr}\n{obj.get('evidence', '')}"):
            dims["post_type"].add("process_info")
        dims["sponsorship"] = rule_sponsorship(inp, image_texts)
        dims["level"] = rule_level(inp, titles, dims["level"], dims["post_type"])
        # docs/tags.md: an event or process post for students in general is {internship, new_grad}.
        # Small models usually pick one of the two, so widen (fail open) unless a title settled it.
        if "job_posting" not in dims["post_type"] and dims["level"] < {"internship", "new_grad"} and not any(level_from_title(t) for t in titles):
            dims["level"] = {"internship", "new_grad"}
        if self.role_rules:
            title = (inp.listing.title if inp.listing else "") or inp.sidecar_title
            ruled = role_from_text([title] if title else titles)
            if ruled != set(ALL["role"]):
                dims["role"] = ruled
        return to_tags(dims, str(obj.get("evidence", "")))


class OcrRulesTagger:
    """Pipeline 2a: RapidOCR text plus link and listing, through keyword and title rules only."""

    def __init__(self, use_ocr: bool = True):
        self.use_ocr = use_ocr
        self.name = "ocr-rules" if use_ocr else "listing-rules"
        self.version = _code_version(self.name)

    def tag(self, story) -> Tags:
        inp = story_inputs(story)
        ocr = load_ocr(story.media_id) if self.use_ocr else ""
        if ocr is None:
            raise FileNotFoundError("no OCR text")
        if not self.use_ocr and not inp.links:
            return Tags.unsure()  # nothing to read without the image
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
            if job_rules.INTERN_RX.search(ocr) or job_rules.NEW_GRAD_RX.search(ocr) or "hiring" in ocr.lower():
                post.add("job_posting")
            post = post or {"misc"}
        level_text = ocr if not title else title
        lv = set()
        if job_rules.INTERN_RX.search(level_text):
            lv.add("internship")
        if job_rules.NEW_GRAD_RX.search(level_text):
            lv.add("new_grad")
        dims = {
            "post_type": post,
            "company": rule_company(inp, [ocr], set(ALL["company"])),
            "role": role_from_text([title] if title else [ocr]),
            "level": rule_level(inp, [], lv or set(ALL["level"]), post),
            "sponsorship": rule_sponsorship(inp, [ocr]),
        }
        return to_tags(dims)


class UnionTagger:
    """Per-dimension union of two taggers (fails open: a value either keeps is kept)."""

    def __init__(self, a, b, name: str):
        self.a, self.b, self.name = a, b, name
        self.version = _code_version(a.name, a.version, b.name, b.version)

    def tag(self, story) -> Tags:
        ta, tb = self.a.tag(story), self.b.tag(story)
        post = set(ta.values("post_type")) | set(tb.values("post_type"))
        dims = {"post_type": post}
        for name in ALL:
            if name == "post_type":
                continue
            va, vb = ta.values(name), tb.values(name)
            got = (set(va) if va else set()) | (set(vb) if vb else set())
            dims[name] = got or set(ALL[name])
        return to_tags(dims)


# -- factories (``--tagger taggers:<name>``) ------------------------------------------------------------------

RUNS = {
    "q2b4_i512": "Qwen3-VL-2B Q4_K_M, 512 image tokens",
    "q2b4_i1024": "Qwen3-VL-2B Q4_K_M, 1024 image tokens",
    "q2b8_i512": "Qwen3-VL-2B Q8_0, 512 image tokens",
    "q4b4_i512": "Qwen3-VL-4B Q4_K_M, 512 image tokens",
}
PROMPT_VERSION = os.environ.get("TAGGER_PROMPT_VERSION", "v1")


def _label(run: str) -> str:
    return f"{PREFIX}-{run.replace('_', '-')}-{PROMPT_VERSION}"


def _register() -> dict[str, Callable[[], object]]:
    out: dict[str, Callable[[], object]] = {}
    for run in RUNS:
        lab = _label(run)
        out[f"vlm_{run}"] = (lambda lab=lab, run=run: ModelTagger(lab, f"vlm-{run}"))
        out[f"vlm_{run}_raw"] = (lambda lab=lab, run=run: ModelTagger(lab, f"vlm-{run}", guarded=False))
        out[f"hybrid_{run}"] = (lambda lab=lab, run=run: HybridTagger(ModelTagger(lab, f"vlm-{run}"), f"hybrid-{run}"))
        out[f"hybrid_{run}_rolerules"] = (lambda lab=lab, run=run: HybridTagger(ModelTagger(lab, f"vlm-{run}"), f"hybrid-{run}", role_rules=True))
        out[f"hybrid_{run}_rolerules_noocr"] = (lambda lab=lab, run=run: HybridTagger(ModelTagger(lab, f"vlm-{run}"), f"hybrid-{run}-noocr", use_ocr=False, role_rules=True))
        out[f"union_{run}_ocr"] = (lambda lab=lab, run=run: UnionTagger(HybridTagger(ModelTagger(lab, f"vlm-{run}"), "h"), OcrRulesTagger(), f"union-hybrid-{run}-ocr-rules"))
    ocr_lab = f"{PREFIX}-ocrtext-q2b4-{PROMPT_VERSION}"
    out["ocrtext_q2b4"] = lambda: ModelTagger(ocr_lab, "ocrtext-q2b4")
    out["hybrid_ocrtext_q2b4_rolerules"] = lambda: HybridTagger(ModelTagger(ocr_lab, "ocrtext-q2b4"), "hybrid-ocrtext-q2b4", role_rules=True)
    out["hybrid_ocrtext_q2b4"] = lambda: HybridTagger(ModelTagger(ocr_lab, "ocrtext-q2b4"), "hybrid-ocrtext-q2b4")
    out["ocr_rules"] = OcrRulesTagger
    out["listing_rules"] = lambda: OcrRulesTagger(use_ocr=False)
    out["unsure"] = UnsureBaseline
    out["rules"] = lambda: ClassifierAdapter(build_classifier("rules"), "rules")
    for run in ("sonnet-v1", "sonnet-v1b"):
        key = run.replace("-", "_")
        out[f"claude_{key}"] = (lambda run=run: ModelTagger(f"claude-{run}", f"claude-{run}"))
        out[f"claude_{key}_raw"] = (lambda run=run: ModelTagger(f"claude-{run}", f"claude-{run}", guarded=False))
        out[f"hybrid_claude_{key}"] = (lambda run=run: HybridTagger(ModelTagger(f"claude-{run}", f"claude-{run}"), f"hybrid-claude-{run}"))
        out[f"hybrid_claude_{key}_rolerules"] = (lambda run=run: HybridTagger(ModelTagger(f"claude-{run}", f"claude-{run}"), f"hybrid-claude-{run}", role_rules=True))
    return out


FACTORIES = _register()
globals().update(FACTORIES)


def build(name: str):
    return FACTORIES[name]()
