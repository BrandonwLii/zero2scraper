"""#13: the server-wide no-post list: rules, storage, the watcher's use of it and /nopost (fakes only)."""

import asyncio
import logging
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import FakeIG, FakeNotifier, make_item
from test_ping_prefs import interaction, run, select_of
from test_watcher import FixedClassifier

from story_watch.bot import nopost as N
from story_watch.bot import nopost_app
from story_watch.main import Watcher
from story_watch.nopost import MAX_RULES, NoPostRule, RuleError, describe_tags, tagged_by_model
from story_watch import nopost
from story_watch.store import Store
from story_watch.tags import PostType, Tags

LAB = 100000000000000001
OTHER = 100000000000000002
JOB_URL = "https://boards.greenhouse.io/acme/jobs/123"


def rule(*keys):
    return NoPostRule.from_keys(keys)


def would_drop(rules, tags, links=(), tagger="claude"):
    return nopost.would_drop(rules, tags, links, tagger=tagger)


def matching_rule(rules, tags, links=(), tagger="claude"):
    return nopost.matching_rule(rules, tags, links, tagger=tagger)


@pytest.fixture
def cfg(cfg):
    """The list only acts on a real model tagger's output; these tests use claude."""
    return replace(cfg, classifier="claude")


MISC = Tags(post_type=["misc"])
PROCESS = Tags(post_type=["process_info"], company=["other"], role=["swe"], level=["other"])
JOB = Tags(post_type=["job_posting"], sponsorship=["unknown"], company=["other"], role=["swe"], level=["internship"])


# -- rules and the safety rules ------------------------------------------------------------------


def test_single_dimension_rule_drops_a_confident_story():
    assert would_drop([rule("post_type:misc")], MISC)
    assert not would_drop([rule("post_type:event")], MISC)


def test_rule_is_a_conjunction_across_dimensions_and_or_within_one():
    r = rule("post_type:process_info", "level:other", "level:new_grad")
    assert r.matches(PROCESS)
    assert not r.matches(Tags(post_type=["process_info"], company=["other"], role=["swe"], level=["internship"]))
    assert not rule("post_type:process_info", "level:other").matches(Tags(post_type=["event"], level=["other"]))


def test_any_rule_in_the_list_can_drop():
    rules = [rule("post_type:event"), rule("post_type:misc")]
    assert would_drop(rules, MISC) and would_drop(rules, Tags(post_type=["event"]))
    assert not would_drop([], MISC)


def test_unsure_dimension_never_matches():
    # post type confident, level unsure (every value): the rule uses level, so no drop
    assert not rule("post_type:process_info", "level:other").matches(
        Tags(post_type=["process_info"], company=["other"], role=["swe"])
    )
    # several values, one outside the rule: not confident that it is covered
    assert not rule("post_type:process_info", "level:other").matches(
        Tags(post_type=["process_info"], level=["other", "internship"])
    )
    assert not rule("post_type:misc").matches(Tags.unsure())


def test_not_applicable_never_matches():
    # Misc has no company, so a rule on company can't fire for it
    assert not rule("company:other").matches(MISC)
    assert not rule("post_type:misc", "company:other").matches(MISC)


def test_a_rule_that_lists_several_values_covers_a_multi_value_story():
    assert rule("post_type:misc", "post_type:event").matches(Tags(post_type=["misc", "event"]))


def test_job_link_is_always_posted_whatever_the_list_says():
    rules = [rule("post_type:misc")]
    assert would_drop(rules, MISC, ["https://example.com/page"])
    assert not would_drop(rules, MISC, [JOB_URL])
    assert not would_drop(rules, MISC, ["https://example.com/page", JOB_URL])


def test_a_story_that_could_be_a_job_posting_is_always_posted():
    rules = [rule("post_type:misc"), rule("post_type:event"), rule("company:other")]
    assert not would_drop(rules, Tags(post_type=["misc", "job_posting"]))
    assert not would_drop(rules, JOB)
    assert not would_drop(rules, Tags(post_type=["event", "job_posting"], company=["other"]))


def test_fallback_tags_are_always_posted():
    assert not would_drop([rule("post_type:misc"), rule("company:other"), rule("level:other")], Tags.unsure())


def test_rules_that_could_never_fire_or_never_hold_are_refused():
    with pytest.raises(RuleError, match="always posted"):
        rule("post_type:job_posting")
    with pytest.raises(RuleError, match="Sponsorship"):
        rule("sponsorship:no_sponsor")
    with pytest.raises(RuleError, match="Every Level"):
        rule("level:internship", "level:new_grad", "level:other")
    with pytest.raises(RuleError, match="at least one"):
        NoPostRule({})
    with pytest.raises(ValueError):
        rule("post_type:nope")


def test_matching_rule_returns_the_rule_and_describe_is_readable():
    r = rule("post_type:process_info", "level:other")
    assert matching_rule([rule("post_type:event"), r], PROCESS) == r
    assert r.describe() == "Post type: Process info AND Level: Other"
    assert r.to_keys() == ["level:other", "post_type:process_info"]


def test_describe_tags_has_values_only():
    text = describe_tags(Tags(post_type=["misc"], evidence="secret story text"))
    assert text.startswith("post_type=misc sponsorship=n/a") and "secret" not in text


# -- storage -------------------------------------------------------------------------------------


def test_rules_roundtrip_in_order(store):
    assert store.get_nopost_rules() == []
    a = store.add_nopost_rule(rule("post_type:misc"))
    b = store.add_nopost_rule(rule("post_type:process_info", "level:other"))
    assert [(i, r.to_keys()) for i, r in store.get_nopost_rules()] == [
        (a, ["post_type:misc"]),
        (b, ["level:other", "post_type:process_info"]),
    ]


def test_duplicate_rule_is_not_added_twice(store):
    assert store.add_nopost_rule(rule("post_type:misc")) is not None
    assert store.add_nopost_rule(rule("post_type:misc")) is None
    assert len(store.get_nopost_rules()) == 1


def test_remove_and_reset(store):
    a = store.add_nopost_rule(rule("post_type:misc"))
    store.add_nopost_rule(rule("post_type:event"))
    assert store.remove_nopost_rule(a) and not store.remove_nopost_rule(a)
    assert store.clear_nopost_rules() == 1
    assert store.get_nopost_rules() == []
    assert store._db.execute("SELECT COUNT(*) FROM nopost_tags").fetchone()[0] == 0  # cascade


def test_invalid_stored_rule_is_ignored(store):
    store._db.execute("INSERT INTO nopost_rules (updated_at) VALUES (1)")
    store._db.execute("INSERT INTO nopost_tags VALUES (1, 'post_type:job_posting')")
    store._db.execute("INSERT INTO nopost_rules (updated_at) VALUES (1)")
    store._db.execute("INSERT INTO nopost_tags VALUES (2, 'role:gone')")
    store._db.commit()
    assert store.get_nopost_rules() == []


def test_migration_is_idempotent_on_an_existing_database(tmp_path):
    path = tmp_path / "state.db"
    old = sqlite3.connect(path)
    old.executescript("CREATE TABLE seen (media_id TEXT PRIMARY KEY, target TEXT NOT NULL, taken_at REAL NOT NULL, notified_at REAL);")
    old.close()
    for _ in range(2):
        s = Store(path)
        s.add_nopost_rule(rule("post_type:misc"))
        s.close()
    s = Store(path)
    assert len(s.get_nopost_rules()) == 1
    s.close()


def test_bot_and_watcher_share_the_list_through_the_file(tmp_path):
    path = tmp_path / "state.db"
    bot_side, watcher_side = Store(path), Store(path)
    bot_side.add_nopost_rule(rule("post_type:misc"))
    assert [r.to_keys() for _, r in watcher_side.get_nopost_rules()] == [["post_type:misc"]]
    bot_side.close()
    watcher_side.close()


# -- the watcher ---------------------------------------------------------------------------------


def run_story(cfg, store, tags, links=(), item_id="1", logger_note=None):
    ig, n = FakeIG(), FakeNotifier()
    w = Watcher(cfg, ig, store, n, classifier=FixedClassifier({item_id: tags}))
    w.step()
    ig.items["alice"] = [make_item(item_id)]
    ig.links[item_id] = tuple(links)
    w.step()
    return w, n


def test_dropped_story_is_marked_seen_unnotified_logged_and_archived(cfg, store, caplog, tmp_path):
    from story_watch.archive import Archive

    store.add_nopost_rule(rule("post_type:misc"))
    archive = Archive(tmp_path / "arch", 50)
    ig, n = FakeIG(), FakeNotifier()
    w = Watcher(cfg, ig, store, n, classifier=FixedClassifier({"1": replace(MISC, evidence="private words")}), archive=archive)
    w.step()
    ig.items["alice"] = [make_item("1")]
    with caplog.at_level(logging.INFO):
        w.step()
        w.step()
    assert n.stories == [] and store.is_seen("1")
    assert store._db.execute("SELECT notified_at FROM seen WHERE media_id='1'").fetchone() == (None,)
    assert "dropped by the no-post list" in caplog.text and "post_type=misc" in caplog.text
    assert "private words" not in caplog.text
    assert archive.has(make_item("1"))  # still archived, so the label channel still sees it
    assert store.get_tags("1") is not None


def test_list_rules_are_applied_on_top_of_notify(cfg, store):
    # NOTIFY_MISC=false already filters misc; the list adds events.
    cfg = replace(cfg, notify_post_types=frozenset({PostType.JOB_POSTING, PostType.EVENT, PostType.PROCESS_INFO}))
    store.add_nopost_rule(rule("post_type:event"))
    ig, n = FakeIG(), FakeNotifier()
    w = Watcher(cfg, ig, store, n, classifier=FixedClassifier({"1": Tags(post_type=["event"]), "2": MISC, "3": PROCESS}))
    w.step()
    ig.items["alice"] = [make_item("1"), make_item("2"), make_item("3")]
    w.step()
    assert [i.media_id for i in n.stories] == ["3"]
    assert all(store.is_seen(i) for i in "123")


def test_job_link_story_is_posted_even_if_the_tagger_says_misc(cfg, store):
    store.add_nopost_rule(rule("post_type:misc"))
    _, n = run_story(cfg, store, MISC, links=[JOB_URL])
    assert [i.media_id for i in n.stories] == ["1"]


def test_possible_job_and_fallback_tags_are_posted(cfg, store):
    store.add_nopost_rule(rule("post_type:misc"))
    store.add_nopost_rule(rule("company:other"))
    _, n = run_story(cfg, store, Tags(post_type=["misc", "job_posting"]))
    assert len(n.stories) == 1
    store2 = Store(":memory:")
    store2.add_nopost_rule(rule("post_type:misc"))
    ig, n2 = FakeIG(), FakeNotifier()
    w = Watcher(cfg, ig, store2, n2, classifier=FixedClassifier({}, error=RuntimeError("model down")))
    w.step()
    ig.items["alice"] = [make_item("9")]
    w.step()
    assert len(n2.stories) == 1  # classifier failure -> Tags.unsure() -> posted


def test_unreadable_list_fails_open(cfg, store, caplog, monkeypatch):
    store.add_nopost_rule(rule("post_type:misc"))

    def boom():
        raise sqlite3.OperationalError("database is locked: /secret/path")

    monkeypatch.setattr(store, "get_nopost_rules", boom)
    with caplog.at_level(logging.WARNING):
        _, n = run_story(cfg, store, MISC)
    assert len(n.stories) == 1
    assert "OperationalError" in caplog.text and "/secret/path" not in caplog.text


def test_list_is_read_every_cycle(cfg, store):
    ig, n = FakeIG(), FakeNotifier()
    w = Watcher(cfg, ig, store, n, classifier=FixedClassifier({}))
    w.step()
    ig.items["alice"] = [make_item("1")]
    store.add_nopost_rule(rule("post_type:misc"))
    w.step()
    assert n.stories == []
    store.clear_nopost_rules()
    ig.items["alice"] = [make_item("2")]
    w.step()
    assert [i.media_id for i in n.stories] == ["2"]


def test_dropped_story_pings_nobody_and_reads_no_prefs(cfg, store, monkeypatch):
    store.add_nopost_rule(rule("post_type:misc"))
    monkeypatch.setattr(store, "all_ping_prefs", lambda: pytest.fail("prefs read for a dropped story"))
    _, n = run_story(cfg, store, MISC)
    assert n.sent == []


def test_tags_are_stored_so_a_later_list_change_does_not_reclassify(cfg, store):
    store.add_nopost_rule(rule("post_type:misc"))
    run_story(cfg, store, MISC)
    assert store.get_tags("1").post_type == frozenset({PostType.MISC})


# -- /nopost -------------------------------------------------------------------------------------


def test_options_exclude_job_posting_and_sponsorship():
    keys = [k for k, _ in N.all_options()]
    assert "post_type:job_posting" not in keys and not any(k.startswith("sponsorship:") for k in keys)
    assert "post_type:misc" in keys and "level:other" in keys


def test_describe_empty_and_with_rules():
    assert "empty" in N.describe([], editable=False) and "Only labelers" in N.describe([], editable=False)
    text = N.describe([(1, rule("post_type:misc"))], editable=True)
    assert "- Post type: Misc" in text and "job link" in text and "Only labelers" not in text


def choose(select, values):
    select._values = list(values)


def button_of(view, label):
    return next(c for c in view.children if getattr(c, "label", None) == label)


def make_group(tmp_path):
    db = tmp_path / "state.db"
    return nopost_app.NoPostGroup(db, frozenset({LAB})), db


def test_labeler_adds_and_removes_rules_and_resets(tmp_path):
    group, db = make_group(tmp_path)

    async def go():
        i = interaction(LAB)
        await group.edit.callback(group, i)
        kind, text, kw = i.response.calls[0]
        assert kw["ephemeral"] and "empty" in text
        view = kw["view"]

        choose(select_of(view, "Values for a new rule"), ["post_type:process_info", "level:other"])
        i2 = interaction(LAB)
        await select_of(view, "Values for a new rule").callback(i2)
        view = i2.response.calls[0][2]["view"]
        assert "Post type: Process info AND Level: Other" in i2.response.calls[0][1]

        i3 = interaction(LAB)
        await button_of(view, "Add rule").callback(i3)
        assert "Added" in i3.response.calls[0][1]
        view = i3.response.calls[0][2]["view"]
        assert [r.to_keys() for _, r in Store(db).get_nopost_rules()] == [["level:other", "post_type:process_info"]]

        # a second identical rule is reported, not duplicated
        choose(select_of(view, "Values for a new rule"), ["post_type:process_info", "level:other"])
        i4 = interaction(LAB)
        await select_of(view, "Values for a new rule").callback(i4)
        view = i4.response.calls[0][2]["view"]
        i5 = interaction(LAB)
        await button_of(view, "Add rule").callback(i5)
        assert "already on the list" in i5.response.calls[0][1]
        view = i5.response.calls[0][2]["view"]
        assert len(Store(db).get_nopost_rules()) == 1

        # remove it
        rid = str(Store(db).get_nopost_rules()[0][0])
        rm = select_of(view, "Remove a rule")
        choose(rm, [rid])
        i6 = interaction(LAB)
        await rm.callback(i6)
        assert "Removed" in i6.response.calls[0][1] and Store(db).get_nopost_rules() == []

    run(go())


def test_reset_needs_two_presses(tmp_path):
    group, db = make_group(tmp_path)
    s = Store(db)
    s.add_nopost_rule(rule("post_type:misc"))
    s.add_nopost_rule(rule("post_type:event"))
    s.close()

    async def go():
        i = interaction(LAB)
        await group.edit.callback(group, i)
        view = i.response.calls[0][2]["view"]
        i2 = interaction(LAB)
        await button_of(view, "Reset list").callback(i2)
        assert len(Store(db).get_nopost_rules()) == 2  # first press only arms it
        view = i2.response.calls[0][2]["view"]
        i3 = interaction(LAB)
        await button_of(view, "Press again to reset the list").callback(i3)
        assert Store(db).get_nopost_rules() == [] and "Removed 2" in i3.response.calls[0][1]

    run(go())


def test_bad_rules_are_explained_not_saved(tmp_path):
    group, db = make_group(tmp_path)

    async def go():
        view = nopost_app.EditView(db, frozenset({LAB}), [])
        i = interaction(LAB)
        await button_of(view, "Add rule").callback(i)  # nothing picked
        assert "Pick at least one" in i.response.calls[0][1]
        view.picks = ["level:internship", "level:new_grad", "level:other"]
        i2 = interaction(LAB)
        await button_of(view, "Add rule").callback(i2)
        assert "Every Level" in i2.response.calls[0][1]
        assert Store(db).get_nopost_rules() == []

    run(go())


def test_non_labelers_can_view_but_not_edit(tmp_path):
    group, db = make_group(tmp_path)
    s = Store(db)
    s.add_nopost_rule(rule("post_type:misc"))
    s.close()

    async def go():
        i = interaction(OTHER)
        await group.show.callback(group, i)
        _, text, kw = i.response.calls[0]
        assert kw["ephemeral"] and "Post type: Misc" in text and "Only labelers" in text

        i2 = interaction(OTHER)
        await group.edit.callback(group, i2)
        _, text2, kw2 = i2.response.calls[0]
        assert kw2["ephemeral"] and "Only labelers" in text2 and "view" not in kw2

        # a view that leaked to someone else refuses every press
        view = nopost_app.EditView(db, frozenset({LAB}), [])
        i3 = interaction(OTHER)
        assert await view.interaction_check(i3) is False
        assert "Only labelers" in i3.response.calls[0][1]
        assert await view.interaction_check(interaction(LAB)) is True
        assert len(Store(db).get_nopost_rules()) == 1

    run(go())


def test_labeler_show_points_to_edit(tmp_path):
    group, _ = make_group(tmp_path)
    i = interaction(LAB)
    run(group.show.callback(group, i))
    assert "/nopost edit" in i.response.calls[0][1]


def test_db_failure_is_reported_without_details(tmp_path, caplog):
    (tmp_path / "missing").write_text("a file, so mkdir fails")
    group = nopost_app.NoPostGroup(tmp_path / "missing" / "dir" / "x" / "state.db", frozenset({LAB}))
    i = interaction(LAB)
    run(group.show.callback(group, i))
    assert "Could not reach" in i.response.calls[0][1]
    assert str(tmp_path) not in caplog.text


# -- only a real model tagger's output can drop a story (coordinator decision) -------------------


def test_only_claude_or_hybrid_model_output_can_drop():
    rules = [rule("post_type:misc")]
    assert would_drop(rules, MISC, tagger="claude") and would_drop(rules, MISC, tagger="hybrid")
    for tagger in ("rules", "classify-rules", "unsure", "", "Claude", "pkg.mod:factory"):
        assert not would_drop(rules, MISC, tagger=tagger), tagger
    # the claude/hybrid fallback (listing_rules + OCR) is marked by its evidence
    for tagger in ("claude", "hybrid"):
        assert not would_drop(rules, replace(MISC, evidence="fallback: no image"), tagger=tagger)
        assert not tagged_by_model(tagger, replace(PROCESS, evidence="fallback: LlmTimeout"))
    assert would_drop(rules, replace(MISC, evidence="a selfie at the gym"), tagger="claude")


def test_the_tagger_is_a_required_argument():
    with pytest.raises(TypeError):
        nopost.would_drop([rule("post_type:misc")], MISC)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        nopost.matching_rule([rule("post_type:misc")], MISC)  # type: ignore[call-arg]


def test_real_fallback_tags_are_marked_and_never_drop():
    """Both production taggers mark their fallback the way nopost checks it."""
    from story_watch import claude, hybrid

    for mod in (claude, hybrid):
        src = open(mod.__file__).read()
        assert 'evidence=f"fallback: {why}"' in src, mod.__name__
    assert nopost.FALLBACK_EVIDENCE == "fallback:"


def test_rules_classifier_never_drops_and_logs_the_skipped_rule(cfg, store, caplog):
    store.add_nopost_rule(rule("post_type:misc"))
    with caplog.at_level(logging.INFO):
        _, n = run_story(replace(cfg, classifier="rules"), store, replace(MISC, evidence="private words"))
    assert [i.media_id for i in n.stories] == ["1"]
    assert "no-post rule post_type:misc matched, but" in caplog.text and "CLASSIFIER=rules" in caplog.text
    assert "post_type=misc" in caplog.text and "private words" not in caplog.text
    assert "dropped by the no-post list" not in caplog.text


def test_model_fallback_tags_are_posted_and_logged(cfg, store, caplog):
    store.add_nopost_rule(rule("post_type:process_info", "level:other"))
    with caplog.at_level(logging.INFO):
        _, n = run_story(cfg, store, replace(PROCESS, evidence="fallback: ClaudeLimited"))
    assert len(n.stories) == 1
    assert "matched, but the tags are not a model tagger's" in caplog.text
    assert "dropped by the no-post list" not in caplog.text


def test_eval_counts_only_model_output():
    from test_evaluation import pred, story

    rules = [rule("post_type:misc")]
    gold_job = Tags(post_type=["job_posting"], sponsorship=["unknown"], company=["other"], role=["swe"], level=["internship"])
    stories = [story("a", gold_job), story("b", MISC)]
    preds = [pred("a", MISC), pred("b", replace(MISC, evidence="fallback: no image"))]
    from story_watch.evaluation import metrics

    res = metrics.evaluate_no_post(stories, preds, rules, "claude")
    assert (res.dropped, res.dropped_job_postings, res.job_posting_ids) == (1, 1, ["a"])
    res = metrics.evaluate_no_post(stories, preds, rules, "classify-rules")
    assert (res.dropped, res.dropped_job_postings) == (0, 0)


# -- storage edge cases --------------------------------------------------------------------------


def test_list_is_capped_in_the_store(store):
    from itertools import combinations

    keys = [k for k, _ in N.all_options()]
    added = 0
    for a, b in combinations(keys, 2):
        try:
            if store.add_nopost_rule(NoPostRule.from_keys([a, b])) is not None:
                added += 1
        except RuleError as e:
            assert "full" in str(e)
            break
    assert added == MAX_RULES == len(store.get_nopost_rules())


def test_rule_rows_without_tags_or_orphan_tags_are_not_rules(store):
    store._db.execute("PRAGMA foreign_keys = OFF")
    store._db.execute("INSERT INTO nopost_rules (rule_id, updated_at) VALUES (7, 1)")  # no tags
    store._db.execute("INSERT INTO nopost_tags VALUES (8, 'post_type:misc')")  # no rule
    store._db.commit()
    assert store.get_nopost_rules() == []


def test_full_list_is_explained_in_the_bot(tmp_path):
    from itertools import combinations

    group, db = make_group(tmp_path)
    s = Store(db)
    keys = [k for k, _ in N.all_options()]
    for a, b in list(combinations(keys, 2))[:MAX_RULES]:
        s.add_nopost_rule(NoPostRule.from_keys([a, b]))
    s.close()

    async def go():
        view = nopost_app.EditView(db, frozenset({LAB}), [])  # drawn while the list was shorter
        view.picks = ["post_type:misc"]
        i = interaction(LAB)
        await button_of(view, "Add rule").callback(i)
        assert "full" in i.response.calls[0][1]
        assert len(Store(db).get_nopost_rules()) == MAX_RULES

    run(go())


def test_a_full_list_of_long_rules_fits_one_message(tmp_path):
    from itertools import combinations

    group, db = make_group(tmp_path)
    s = Store(db)
    longest = ["post_type:event", "post_type:process_info", "company:faang_plus", "company:quant", "role:ml", "role:swe", "role:pm"]
    levels = ["level:internship", "level:new_grad", "level:other"]
    for extra in list(combinations(levels, 2)) + [(lv,) for lv in levels]:
        s.add_nopost_rule(NoPostRule.from_keys(longest + list(extra)))
    for a, b in combinations([k for k, _ in N.all_options()], 2):
        try:
            s.add_nopost_rule(NoPostRule.from_keys(longest[:5] + [a, b]))
        except RuleError as e:
            if "full" in str(e):
                break
    assert len(s.get_nopost_rules()) == MAX_RULES
    s.close()
    i = interaction(LAB)
    run(group.show.callback(group, i))
    i2 = interaction(LAB)
    run(group.edit.callback(group, i2))
    assert len(i.response.calls[0][1]) <= 2000 and len(i2.response.calls[0][1]) <= 2000
