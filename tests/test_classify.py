from dataclasses import replace

import pytest

from conftest import JOB_TAGS, MISC_TAGS, make_item

from story_watch.classify import RuleClassifier, build_classifier, legacy_category
from story_watch.tags import PostType, Tags

GH = "https://job-boards.greenhouse.io/sigmacomputing/jobs/8001295003"


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"links": (GH,), "job_title": "Software Engineering Intern"}, JOB_TAGS),
        # interview detection is left to the LLM classifier: rules never say PROCESS_INFO
        ({"links": ("https://careers.ibm.com/eventlisting/Eventdetail?jobId=131064",),
          "job_title": "Interview Readiness: Inside IBM Interviews"}, JOB_TAGS),
        ({"links": ("https://youtube.com/@zero2sudo",)}, JOB_TAGS),
        ({"mentions": ("claudeai",)}, MISC_TAGS),
        ({}, MISC_TAGS),
    ],
)
def test_rule_classifier(fields, expected):
    assert RuleClassifier().classify(replace(make_item("1"), **fields)) == expected


def test_rule_classifier_decides_only_the_post_type():
    job = RuleClassifier().classify(replace(make_item("1"), links=(GH,)))
    assert job.certain_value("post_type") is PostType.JOB_POSTING
    assert all(job.is_unsure(d) for d in ("sponsorship", "company", "role", "level"))
    misc = RuleClassifier().classify(make_item("1"))
    assert all(not misc.is_applicable(d) for d in ("sponsorship", "company", "role", "level"))


@pytest.mark.parametrize(
    "post_types, expected",
    [
        ([PostType.JOB_POSTING], "job_posting"),
        ([PostType.PROCESS_INFO], "interview_info"),
        ([PostType.MISC], "misc"),
        ([PostType.EVENT], "misc"),
        ([PostType.PROCESS_INFO, PostType.JOB_POSTING], "job_posting"),  # fail open
        (None, "job_posting"),  # unsure
    ],
)
def test_legacy_category(post_types, expected):
    assert legacy_category(Tags(post_type=post_types)) == expected


def test_build_classifier():
    assert isinstance(build_classifier("rules"), RuleClassifier)
    with pytest.raises(ValueError):
        build_classifier("nope")
