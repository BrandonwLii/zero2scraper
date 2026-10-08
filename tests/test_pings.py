import pytest

from story_watch.pings import PingPrefs, should_ping
from story_watch.tags import Company, Level, PostType, Role, Sponsorship, Tags

JOB = PostType.JOB_POSTING

# Users from docs/pings.md.
A = PingPrefs.from_keys(ping_me=["post_type:job_posting", "role:swe", "role:ml", "level:internship"])
B = PingPrefs.from_keys(
    ping_me=["post_type:job_posting"], dont_ping=["sponsorship:no_sponsor", "company:other"]
)
C = PingPrefs.from_keys(
    ping_me=["post_type:job_posting"], dont_ping=["sponsorship:no_sponsor", "sponsorship:unknown"]
)
D = PingPrefs.from_keys(ping_me=["sponsorship:sponsor_or_canadian"])
E = PingPrefs.from_keys(ping_me=["level:internship"])
F = PingPrefs.from_keys(ping_me=["role:swe"], dont_ping=["company:quant"])
G = PingPrefs.from_keys(dont_ping=["company:quant"])
B2 = PingPrefs.from_keys(ping_me=["post_type:event"], dont_ping=["sponsorship:no_sponsor"])
H = PingPrefs.from_keys(
    ping_me=["post_type:job_posting"], dont_ping=["company:faang_plus", "company:quant", "company:other"]
)


def job(**kw):
    return Tags(post_type=[JOB], **kw)


# (id, prefs, tags, expected): one row per row of the table in docs/pings.md.
EXAMPLES = [
    ("1", A, job(role=[Role.SWE], level=[Level.INTERNSHIP]), True),
    ("2", A, job(role=[Role.ML], level=[Level.INTERNSHIP]), True),
    ("3", A, job(role=[Role.PM], level=[Level.INTERNSHIP]), False),
    ("4", A, job(role=[Role.SWE], level=[Level.NEW_GRAD]), False),
    ("5", A, job(role=[Role.SWE, Role.PM], level=[Level.INTERNSHIP]), True),
    ("6", A, job(level=[Level.INTERNSHIP]), True),
    ("7", A, job(role=[Role.SWE], level=[Level.INTERNSHIP, Level.NEW_GRAD]), True),
    ("8", A, Tags(post_type=[PostType.EVENT], role=[Role.SWE], level=[Level.INTERNSHIP]), False),
    ("9", A, Tags(post_type=[JOB, PostType.MISC], role=[Role.SWE], level=[Level.INTERNSHIP]), True),
    ("10", A, Tags(post_type=[PostType.MISC]), False),
    ("11", B, job(sponsorship=[Sponsorship.NO_SPONSOR], company=[Company.QUANT]), False),
    ("12", B, job(sponsorship=[Sponsorship.NO_SPONSOR, Sponsorship.UNKNOWN], company=[Company.QUANT]), True),
    ("13", B, job(sponsorship=[Sponsorship.UNKNOWN], company=[Company.QUANT]), True),
    ("14", B, job(company=[Company.QUANT]), True),
    ("15", B, job(sponsorship=[Sponsorship.UNKNOWN], company=[Company.OTHER]), False),
    ("16", B, job(sponsorship=[Sponsorship.UNKNOWN], company=[Company.FAANG_PLUS, Company.OTHER]), True),
    ("17", C, job(sponsorship=[Sponsorship.NO_SPONSOR, Sponsorship.UNKNOWN]), False),
    ("18", C, job(sponsorship=[Sponsorship.UNKNOWN, Sponsorship.SPONSOR_OR_CANADIAN]), True),
    ("19", B2, Tags(post_type=[PostType.EVENT], company=[Company.QUANT]), True),
    ("20", D, job(sponsorship=[Sponsorship.SPONSOR_OR_CANADIAN]), True),
    ("21", D, job(sponsorship=[Sponsorship.UNKNOWN]), False),
    ("22", D, job(sponsorship=[Sponsorship.NO_SPONSOR]), False),
    ("23", D, job(sponsorship=[Sponsorship.UNKNOWN, Sponsorship.SPONSOR_OR_CANADIAN]), True),
    ("24", D, Tags(post_type=[PostType.PROCESS_INFO]), True),
    ("25", E, Tags(post_type=[PostType.MISC]), True),
    ("26", PingPrefs(), job(role=[Role.SWE]), False),
    ("27", G, job(role=[Role.SWE], company=[Company.OTHER]), False),
    ("28", F, job(role=[Role.SWE], company=[Company.QUANT]), False),
    ("29", F, job(role=[Role.SWE], company=[Company.QUANT, Company.OTHER]), True),
    ("30", A, Tags.unsure(), True),
    ("31", H, Tags.unsure(), False),
    ("32", H, job(company=[Company.QUANT, Company.OTHER]), False),
]


@pytest.mark.parametrize("prefs,tags,expected", [e[1:] for e in EXAMPLES], ids=[e[0] for e in EXAMPLES])
def test_worked_examples(prefs, tags, expected):
    assert should_ping(prefs, tags) is expected


def test_example_ids_are_unique_and_sequential():
    assert [e[0] for e in EXAMPLES] == [str(i) for i in range(1, len(EXAMPLES) + 1)]


# --- uncertainty ---------------------------------------------------------

def test_unsure_dimension_matches_if_any_value_listed():
    prefs = PingPrefs.from_keys(ping_me=["role:pm"])
    assert should_ping(prefs, job())  # role unsure: every value
    assert should_ping(prefs, job(role=[Role.PM, Role.OTHER]))
    assert not should_ping(prefs, job(role=[Role.SWE, Role.OTHER]))


def test_unsure_dimension_cannot_veto_unless_every_value_listed():
    prefs = PingPrefs.from_keys(ping_me=["post_type:job_posting"], dont_ping=["role:pm", "role:other"])
    assert should_ping(prefs, job())
    assert should_ping(prefs, job(role=[Role.PM, Role.SWE]))
    assert not should_ping(prefs, job(role=[Role.PM, Role.OTHER]))


def test_unknown_is_a_real_tag_distinct_from_unsure():
    veto_unknown = PingPrefs.from_keys(ping_me=["post_type:job_posting"], dont_ping=["sponsorship:unknown"])
    assert not should_ping(veto_unknown, job(sponsorship=[Sponsorship.UNKNOWN]))
    assert should_ping(veto_unknown, job())  # unsure about sponsorship
    want_unknown = PingPrefs.from_keys(ping_me=["sponsorship:unknown"])
    assert should_ping(want_unknown, job(sponsorship=[Sponsorship.UNKNOWN]))
    assert not should_ping(want_unknown, job(sponsorship=[Sponsorship.NO_SPONSOR]))
    assert should_ping(want_unknown, job())


def test_unsure_post_type_keeps_job_dimensions_in_play():
    prefs = PingPrefs.from_keys(ping_me=["sponsorship:sponsor_or_canadian"])
    tags = Tags(post_type=[JOB, PostType.EVENT], sponsorship=[Sponsorship.NO_SPONSOR])
    assert not should_ping(prefs, tags)  # may be a job, and sponsorship applies and misses


# --- not applicable ------------------------------------------------------

def test_not_applicable_dimension_matches_and_never_vetoes():
    tags = Tags(post_type=[PostType.EVENT])
    assert tags.values("sponsorship") is None
    assert should_ping(PingPrefs.from_keys(ping_me=["sponsorship:no_sponsor"]), tags)
    every = [f"sponsorship:{s.value}" for s in Sponsorship]
    assert should_ping(PingPrefs.from_keys(ping_me=["post_type:event"], dont_ping=every), tags)


# --- opt-in --------------------------------------------------------------

def test_empty_prefs_never_ping():
    for tags in (Tags.unsure(), job(), Tags(post_type=[PostType.MISC])):
        assert not should_ping(PingPrefs(), tags)


def test_dont_ping_alone_never_pings():
    assert not should_ping(PingPrefs.from_keys(dont_ping=["role:pm"]), job(role=[Role.SWE]))


def test_empty_sets_in_a_dimension_are_ignored():
    prefs = PingPrefs({"role": set(), "level": {Level.INTERNSHIP}}, {"company": set()})
    assert prefs.ping_me == {"level": frozenset({Level.INTERNSHIP})}
    assert prefs.dont_ping == {}
    assert should_ping(prefs, job(role=[Role.PM], level=[Level.INTERNSHIP]))
    assert not should_ping(PingPrefs({"role": set()}), job())


# --- veto and AND/OR -----------------------------------------------------

def test_veto_beats_match():
    prefs = PingPrefs.from_keys(ping_me=["role:swe"], dont_ping=["level:other"])
    assert should_ping(prefs, job(role=[Role.SWE], level=[Level.INTERNSHIP]))
    assert not should_ping(prefs, job(role=[Role.SWE], level=[Level.OTHER]))


def test_values_or_within_dimension_and_across_dimensions():
    prefs = PingPrefs.from_keys(ping_me=["role:swe", "role:ml", "level:internship", "level:new_grad"])
    assert should_ping(prefs, job(role=[Role.ML], level=[Level.NEW_GRAD]))
    assert not should_ping(prefs, job(role=[Role.ML], level=[Level.OTHER]))
    assert not should_ping(prefs, job(role=[Role.PM], level=[Level.NEW_GRAD]))


def test_unset_dimension_matches_anything():
    prefs = PingPrefs.from_keys(ping_me=["role:swe"])
    for level in Level:
        assert should_ping(prefs, job(role=[Role.SWE], level=[level]))


def test_confidence_and_evidence_are_ignored():
    prefs = PingPrefs.from_keys(ping_me=["role:swe"])
    assert should_ping(prefs, job(role=[Role.SWE], confidence=0.0, evidence="x"))
    assert not should_ping(prefs, job(role=[Role.PM], confidence=1.0))


# --- prefs --------------------------------------------------------------

def test_prefs_validation():
    with pytest.raises(ValueError):
        PingPrefs({"shoe_size": {Role.SWE}})
    with pytest.raises(ValueError):
        PingPrefs({"level": {Role.SWE}})
    with pytest.raises(ValueError):
        PingPrefs.from_keys(ping_me=["role:cook"])


def test_prefs_keys_round_trip():
    prefs = PingPrefs.from_keys(ping_me=["role:swe", "level:other"], dont_ping=["company:other"])
    assert prefs.to_keys() == (["level:other", "role:swe"], ["company:other"])
    assert PingPrefs.from_keys(*prefs.to_keys()) == prefs


def test_prefs_equal_regardless_of_input_container():
    assert PingPrefs({"role": [Role.SWE]}) == PingPrefs({"role": {Role.SWE}})
