from dataclasses import replace

import pytest

from conftest import make_item

from story_watch.classify import Category, RuleClassifier, build_classifier

GH = "https://job-boards.greenhouse.io/sigmacomputing/jobs/8001295003"


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"links": (GH,), "job_title": "Software Engineering Intern"}, Category.JOB_POSTING),
        # interview detection is left to the LLM classifier: rules never say INTERVIEW_INFO
        ({"links": ("https://careers.ibm.com/eventlisting/Eventdetail?jobId=131064",),
          "job_title": "Interview Readiness: Inside IBM Interviews"}, Category.JOB_POSTING),
        ({"links": ("https://youtube.com/@zero2sudo",)}, Category.JOB_POSTING),
        ({"mentions": ("claudeai",)}, Category.MISC),
        ({}, Category.MISC),
    ],
)
def test_rule_classifier(fields, expected):
    assert RuleClassifier().classify(replace(make_item("1"), **fields)) == expected


def test_build_classifier():
    assert isinstance(build_classifier("rules"), RuleClassifier)
    with pytest.raises(ValueError):
        build_classifier("nope")
