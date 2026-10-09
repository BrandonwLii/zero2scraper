import json

import pytest

from story_watch.tags import (
    APPLICABLE,
    DIMENSION_NAME,
    DIMENSIONS,
    Company,
    Level,
    PostType,
    Role,
    Sponsorship,
    Tags,
    applicable_dimensions,
    parse_tag_key,
    tag_key,
)

ALL_VALUES = [m for cls in DIMENSIONS.values() for m in cls]


def test_stable_strings():
    # These strings live in env vars, the DB and user config: changing one is a migration.
    assert {name: [m.value for m in cls] for name, cls in DIMENSIONS.items()} == {
        "post_type": ["event", "job_posting", "process_info", "misc"],
        "sponsorship": ["sponsor_or_canadian", "no_sponsor", "unknown"],
        "company": ["faang_plus", "quant", "other"],
        "role": ["ml", "swe", "pm", "other"],
        "level": ["internship", "new_grad", "other"],
    }


@pytest.mark.parametrize("member", ALL_VALUES, ids=tag_key)
def test_round_trip(member):
    cls = type(member)
    assert member.value == member.value.lower().strip()
    assert cls.parse(member.value) is member
    assert cls.parse(f"  {member.value.upper()} ") is member
    assert parse_tag_key(tag_key(member)) is member
    assert member.label


def test_strings_unique():
    for cls in DIMENSIONS.values():
        values = [m.value for m in cls]
        assert len(values) == len(set(values))
    keys = [tag_key(m) for m in ALL_VALUES]
    assert len(keys) == len(set(keys))
    assert DIMENSION_NAME == {cls: name for name, cls in DIMENSIONS.items()}


@pytest.mark.parametrize("text", ["swe", "role:", "role:faang_plus", "seniority:intern", ":swe", ""])
def test_parse_tag_key_rejects(text):
    with pytest.raises(ValueError):
        parse_tag_key(text)


def test_parse_rejects_unknown():
    with pytest.raises(ValueError, match="known: ml, swe, pm, other"):
        Role.parse("devops")


def test_applicable_table():
    assert set(APPLICABLE) == set(PostType)
    assert all("post_type" in dims for dims in APPLICABLE.values())
    assert APPLICABLE[PostType.JOB_POSTING] == set(DIMENSIONS)
    assert applicable_dimensions([PostType.MISC]) == {"post_type"}
    # fail open: a dimension applies if any possible post type uses it
    assert applicable_dimensions([PostType.MISC, PostType.JOB_POSTING]) == set(DIMENSIONS)


def test_confident_job_posting():
    tags = Tags(
        post_type=[PostType.JOB_POSTING],
        sponsorship=["unknown"],
        company={Company.QUANT},
        role=("swe", "ml"),
        level=[Level.INTERNSHIP],
        confidence=0.9,
        evidence="title says intern",
    )
    assert tags.post_type == {PostType.JOB_POSTING}
    assert tags.role == {Role.SWE, Role.ML}
    assert tags.is_certain("level") and tags.certain_value("level") is Level.INTERNSHIP
    assert tags.certain_value("sponsorship") is Sponsorship.UNKNOWN  # a fact, not unsure
    assert not tags.is_unsure("sponsorship")
    assert not tags.is_certain("role") and tags.certain_value("role") is None
    assert not tags.is_unsure("role")


def test_unsure():
    tags = Tags.unsure()
    for name, cls in DIMENSIONS.items():
        assert tags.values(name) == frozenset(cls)
        assert tags.is_applicable(name) and tags.is_unsure(name) and not tags.is_certain(name)


def test_missing_applicable_dimension_is_unsure():
    tags = Tags(post_type=["event"], company=["faang_plus"])
    assert tags.is_unsure("role") and tags.is_unsure("level")
    assert tags.certain_value("company") is Company.FAANG_PLUS


def test_not_applicable_is_none():
    tags = Tags(post_type=["misc"], role=["swe"])  # role dropped: doesn't apply to Misc
    for name in ("sponsorship", "company", "role", "level"):
        assert tags.values(name) is None
        assert not tags.is_applicable(name)
        assert not tags.is_certain(name) and not tags.is_unsure(name)
        assert tags.certain_value(name) is None
    assert Tags(post_type=["event"]).sponsorship is None


def test_unsure_post_type_keeps_dimensions():
    tags = Tags(post_type=["misc", "job_posting"], sponsorship=["no_sponsor"])
    assert tags.sponsorship == {Sponsorship.NO_SPONSOR}
    assert tags.is_unsure("level")


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"role": []}, ValueError),
        ({"role": ["ninja"]}, ValueError),
        ({"role": [Company.OTHER]}, ValueError),  # same string, wrong dimension
        ({"role": "swe"}, TypeError),
        ({"post_type": []}, ValueError),
        ({"confidence": 1.5}, ValueError),
        ({"confidence": -0.1}, ValueError),
    ],
)
def test_invalid(kwargs, error):
    with pytest.raises(error):
        Tags(**kwargs)


def test_values_rejects_unknown_dimension():
    with pytest.raises(ValueError):
        Tags().values("seniority")


@pytest.mark.parametrize(
    "tags",
    [
        Tags.unsure(),
        Tags(post_type=["misc"], evidence="meme"),
        Tags(post_type=["job_posting"], role=["pm", "swe"], level=["new_grad"], confidence=0.5),
    ],
)
def test_dict_round_trip(tags):
    data = json.loads(json.dumps(tags.to_dict()))
    assert Tags.from_dict(data) == tags


def test_to_dict_shape():
    assert Tags(post_type=["misc"]).to_dict() == {
        "post_type": ["misc"],
        "sponsorship": None,
        "company": None,
        "role": None,
        "level": None,
        "confidence": None,
        "evidence": "",
    }
    assert Tags(post_type=["job_posting"], role=["swe", "ml"]).to_dict()["role"] == ["ml", "swe"]


def test_from_dict_rejects_unknown_field():
    with pytest.raises(ValueError):
        Tags.from_dict({"post_type": ["misc"], "seniority": ["intern"]})
