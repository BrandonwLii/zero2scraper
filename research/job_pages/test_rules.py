"""Offline tests for the prototype rules and extractors. Every phrase here is invented.

    research/job_pages/.venv/bin/python -m pytest research/job_pages -q
"""

from __future__ import annotations

import json
import socket

import pytest

import extract
import rules
from fetch import Blocked, check_public

S, N, U = rules.SPONSOR, rules.NO_SPONSOR, rules.UNKNOWN
US = ["Austin, TX"]


@pytest.mark.parametrize("text, want", [
    ("We are unable to provide visa sponsorship for this role.", {N}),
    ("Applicants must be authorized to work in the U.S. without sponsorship now or in the future.", {N}),
    ("The company will only employ those who are legally authorized to work in the United States.", {N}),
    ("Must be a U.S. citizen.", {N}),
    ("This role requires an active Secret security clearance.", {N}),
    ("Due to export control rules, a U.S. person is required.", {N}),
    ("H1B sponsorship is available for this position.", {S}),
    ("We will sponsor visas for strong candidates.", {S}),
    ("Visa sponsorship is not available.", {N}),
    ("Will you require immigration sponsorship to work for us? Yes / No", {U}),  # a form question, not a policy
    ("Build services in Python and Go.", {U}),
])
def test_sponsorship_phrases(text, want):
    assert rules.sponsorship(US, text) == want


def test_canada_location_wins_over_no_sponsorship():
    assert rules.sponsorship(["Toronto, ON"], "We do not sponsor visas.") == {S}
    assert rules.sponsorship(["Remote"], "This is a remote role open across North America.") == {S}
    assert rules.sponsorship(["New York, NY", "Waterloo, Ontario"], "") == {S}


def test_unread_posting_is_unsure_not_unknown():
    assert rules.sponsorship([], "", read_ok=False) == {S, N, U}


def test_canada_needs_more_than_a_bare_mention():
    assert rules.in_canada(US, "We have customers in Canada and Mexico.") is False
    assert rules.in_canada([], "") is None


@pytest.mark.parametrize("title, text, want", [
    ("Software Engineer Intern, Summer 2027", "", {rules.INTERN}),
    ("Embedded Co-op (8 months)", "", {rules.INTERN}),
    ("Software Engineer, New Grad 2027", "", {rules.NEW_GRAD}),
    ("Senior Backend Engineer", "", {rules.OTHER}),
    ("Software Engineer", "Requires 5+ years of professional experience.", {rules.OTHER}),
    ("Software Engineer", "0-2 years of industry experience.", {rules.NEW_GRAD}),
    ("Engineer I", "Bachelor's degree in CS.", {rules.NEW_GRAD, rules.OTHER}),
    (None, "", {rules.INTERN, rules.NEW_GRAD, rules.OTHER}),
])
def test_level(title, text, want):
    assert rules.level(title, text) == want


def test_jsonld_description_and_double_escaped_html():
    ld = {"@type": "JobPosting", "title": "Data Intern", "hiringOrganization": {"name": "Example Co"},
          "jobLocation": {"address": {"addressLocality": "Ottawa", "addressRegion": "ON", "addressCountry": "CA"}},
          "description": "&lt;p&gt;Build things.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;"}
    page = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    p = extract.posting_from_jsonld(page)
    assert p.title == "Data Intern" and p.company == "Example Co"
    assert p.locations == ["Ottawa, ON, CA"]
    assert p.text == "Build things.\nPython"


def test_redirect_to_listing_counts_as_closed():
    assert extract._redirected_away("https://jobs.example.com/job/123456", "https://jobs.example.com/search")
    assert not extract._redirected_away("https://jobs.example.com/job/123456", "https://jobs.example.com/job/123456/x")


def _resolve_to(ip):
    return lambda host, port, proto=0: [(socket.AF_INET, socket.SOCK_STREAM, proto, "", (ip, port))]


def test_guards():
    with pytest.raises(Blocked):
        check_public("http://jobs.example.com/1", _resolve_to("93.184.216.34"))
    with pytest.raises(Blocked):
        check_public("https://jobs.example.com/1", _resolve_to("192.168.1.10"))
    check_public("https://jobs.example.com/1", _resolve_to("93.184.216.34"))
