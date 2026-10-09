"""Offline tests for the #10 prototype rules, on invented inputs only.

    cd research/tagger && .venv/bin/python -m pytest -q test_common.py
"""

from __future__ import annotations

from common import (ALL, Listing, StoryInputs, company_lists, dims_from_model, guard, level_from_title, match_companies,
                    parse_model_json, role_from_text)
from prompting import SCHEMA, system_prompt
from taggers import rule_company, rule_sponsorship


def _inp(links=(), listing=None, company=""):
    return StoryInputs("1", None, list(links), [], "", company, listing)


def test_company_lists_come_from_docs():
    lists = company_lists()
    assert "tesla" in lists["faang_plus"] and "wing" in lists["faang_plus"]
    assert "marshall wace" in lists["quant"] and "hrt" in lists["quant"]


def test_match_companies_aliases_and_noise():
    assert match_companies(["New grad role at Amazon Web Services"]) == {"faang_plus"}
    assert match_companies(["HRT is hiring"]) == {"quant"}
    assert match_companies(["hrt emoji text"]) == set()  # short aliases need capitals
    assert match_companies(["Instagram\nAcme Corp careers"]) == set()  # in-app browser header
    assert match_companies(["Jump into summer", "Maven of style"]) == set()
    assert match_companies(["Jump Trading and Meta"]) == {"quant", "faang_plus"}


def test_role_and_level_from_titles():
    assert role_from_text(["AI Software Engineer Intern"]) == {"ml", "swe"}
    assert role_from_text(["Data Scientist, New Grad"]) == {"other"}
    assert role_from_text(["Associate Product Manager"]) == {"pm"}
    assert role_from_text([""]) == set(ALL["role"])
    assert level_from_title("Software Engineer Intern, Summer 2027") == {"internship"}
    assert level_from_title("Engineer I, Platform") == {"new_grad", "other"}
    assert level_from_title("Senior Backend Engineer") == {"other"}
    assert level_from_title("Software Engineer") is None


def test_model_reply_is_parsed_fail_open():
    obj = parse_model_json('```json\n{"post_type": ["job_posting"], "role": [], "level": ["bogus"]}\n```')
    dims = dims_from_model(obj)
    assert dims["post_type"] == {"job_posting"}
    assert dims["role"] == set(ALL["role"]) and dims["level"] == set(ALL["level"])


def test_guard_keeps_job_posting_and_widens_unread_unknown():
    dims = {**{k: set(v) for k, v in ALL.items()}, "post_type": {"misc"}, "sponsorship": {"unknown"}}
    out = guard(dims, _inp(links=["https://jobs.example.com/1"], listing=Listing("failed")))
    assert "job_posting" in out["post_type"]
    assert out["sponsorship"] == set(ALL["sponsorship"])


def test_guard_known_sponsor():
    dims = {**{k: set(v) for k, v in ALL.items()}, "sponsorship": {"unknown"}}
    out = guard(dims, _inp(links=["https://example.com/careers/1"], listing=Listing("failed", title="Tesla intern")))
    assert out["sponsorship"] == {"sponsor_or_canadian"}


def test_rule_sponsorship_from_listing_and_image():
    read = Listing("ok", locations=["Austin, TX"], description="We are unable to sponsor visas for this role. " * 10)
    assert rule_sponsorship(_inp(["https://x"], read), []) == {"no_sponsor"}
    assert rule_sponsorship(_inp(["https://x"], Listing("closed")), ["Remote across North America"]) == {"sponsor_or_canadian"}
    assert rule_sponsorship(_inp(["https://x"], Listing("closed")), ["Austin, TX"]) == set(ALL["sponsorship"])


def test_rule_company_named_but_unlisted_is_other():
    assert rule_company(_inp(company="Acme Robotics"), [], set(ALL["company"])) == {"other"}
    assert rule_company(_inp(), ["no names here"], {"faang_plus", "other"}) == {"faang_plus", "other"}
    assert rule_company(_inp(), [], set(ALL["company"]), ["Citadel Securities"]) == {"quant"}


def test_prompt_and_schema():
    text = system_prompt()
    assert "{faang_list}" not in text and "Marshall Wace" in text
    assert set(SCHEMA["required"]) >= set(ALL)
