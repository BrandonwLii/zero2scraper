"""Harness tests: synthetic labels, sidecars and predictions only. No network, no real media."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from story_watch.bot.labels import Label, TAXONOMY_VERSION, append_label, make_label
from story_watch.evaluation import metrics
from story_watch.evaluation.cache import CacheEntry, TagCache
from story_watch.evaluation.cli import DEFAULT_PING_CONFIGS, main
from story_watch.evaluation.data import BAD_LABEL, NO_SIDECAR, STALE_TAXONOMY, UNLABELED, EvalStory, LoadResult, gold_tags, load_stories
from story_watch.evaluation.report import build_report, render_markdown
from story_watch.evaluation.runner import Prediction, run_tagger
from story_watch.evaluation.taggers import ClassifierAdapter, TaggerOutput, UnsureBaseline, load_tagger
from story_watch.instagram import StoryItem
from story_watch.pings import PingPrefs
from story_watch.tags import Company, Level, PostType, Role, Sponsorship, Tags

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def job(role=("swe",), level=("internship",), sponsorship=("unknown",), company=("other",)):
    return Tags(post_type=["job_posting"], sponsorship=sponsorship, company=company, role=role, level=level)


def story(media_id, gold, links=()):
    item = StoryItem(media_id=media_id, target="t", taken_at=T0, is_video=False, thumbnail_url="", links=tuple(links))
    return EvalStory(media_id, "t", gold, item)


def pred(media_id, tags, latency=0.1, cost=None, error=None, cached=False):
    return Prediction(media_id, tags, latency, cost, cached, error)


def write_sidecar(root, media_id, minutes=0, links=()):
    folder = root / "t"
    folder.mkdir(parents=True, exist_ok=True)
    taken = T0 + timedelta(minutes=minutes)
    doc = {"media_id": media_id, "target": "t", "taken_at": taken.isoformat(), "is_video": False, "links": list(links),
           "mentions": [], "job_title": None, "company": None, "category": "misc", "files": [], "node": None, "page_node": None}
    (folder / f"{taken:%Y%m%dT%H%M%SZ}_{media_id}.json").write_text(json.dumps(doc))


def write_label(root, media_id, selection):
    append_label(root / "labels.jsonl", make_label(media_id, "t", selection, "1", now=T0))


JOB_SEL = {"post_type": ["job_posting"], "sponsorship": ["unknown"], "company": ["other"], "role": ["swe"], "level": ["internship"]}


# -- loading ------------------------------------------------------------------------------------


def test_load_joins_labels_and_sidecars_and_counts_skips(tmp_path):
    write_sidecar(tmp_path, "2", minutes=2)
    write_sidecar(tmp_path, "1", minutes=1, links=["https://example.com/j"])
    write_sidecar(tmp_path, "3", minutes=3)  # never labeled
    write_label(tmp_path, "1", JOB_SEL)
    write_label(tmp_path, "2", {"post_type": ["misc"]})
    write_label(tmp_path, "9", {"post_type": ["misc"]})  # no sidecar
    result = load_stories(tmp_path)
    assert [s.media_id for s in result.stories] == ["1", "2"]  # oldest first
    assert result.stories[0].item.links == ("https://example.com/j",)
    assert result.stories[1].gold.values("role") is None
    assert result.skipped[NO_SIDECAR] == 1 and result.skipped[UNLABELED] == 1


def test_last_label_wins(tmp_path):
    write_sidecar(tmp_path, "1")
    write_label(tmp_path, "1", {"post_type": ["misc"]})
    write_label(tmp_path, "1", JOB_SEL)
    assert load_stories(tmp_path).stories[0].gold.certain_value("post_type") is PostType.JOB_POSTING


def test_stale_taxonomy_and_bad_labels_are_skipped(tmp_path):
    write_sidecar(tmp_path, "1")
    write_sidecar(tmp_path, "2")
    lines = [
        {"media_id": "1", "target": "t", "post_type": ["misc"], "taxonomy": "tags-00000000"},
        {"media_id": "2", "target": "t", "post_type": ["job_posting"], "sponsorship": None, "company": None, "role": None, "level": None, "taxonomy": TAXONOMY_VERSION},
    ]
    (tmp_path / "labels.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    result = load_stories(tmp_path)
    assert not result.stories and result.skipped[STALE_TAXONOMY] == 1 and result.skipped[BAD_LABEL] == 1
    assert [s.media_id for s in load_stories(tmp_path, allow_stale_taxonomy=True).stories] == ["1"]


def test_gold_tags_rejects_unknown_value_and_missing_dimension():
    base = {n: None for n in ("sponsorship", "company", "role", "level")}
    with pytest.raises(ValueError):
        gold_tags(Label("1", "t", {**base, "post_type": ["bogus"]}, "", "", ""))
    with pytest.raises(ValueError):  # a job needs all dimensions
        gold_tags(Label("1", "t", {**base, "post_type": ["job_posting"]}, "", "", ""))


# -- cache and runner ---------------------------------------------------------------------------


def test_cache_roundtrip_and_key_isolation(tmp_path):
    cache = TagCache(tmp_path)
    entry = CacheEntry(job(), 0.25, 0.01)
    cache.put("tg", "v1", "42", entry)
    assert cache.get("tg", "v1", "42") == entry
    assert cache.get("tg", "v2", "42") is None and cache.get("other", "v1", "42") is None
    assert cache.get("tg", "v1", "43") is None


def test_cache_rejects_path_tricks_and_ignores_corrupt_files(tmp_path):
    cache = TagCache(tmp_path)
    with pytest.raises(ValueError):
        cache.put("../x", "v1", "1", CacheEntry(Tags.unsure(), 0.0))
    with pytest.raises(ValueError):
        cache.get("tg", "v1", "a/b")
    cache.put("tg", "v1", "1", CacheEntry(Tags.unsure(), 0.0))
    (tmp_path / "tg" / "v1" / "1.json").write_text("{not json")
    assert cache.get("tg", "v1", "1") is None


class Counting:
    name, version = "counting", "1"

    def __init__(self, result=None, fail=False):
        self.calls, self.result, self.fail = 0, result or Tags(post_type=["misc"]), fail

    def tag(self, s):
        self.calls += 1
        if self.fail:
            raise RuntimeError("secret story text")
        return self.result


def test_runner_uses_cache_and_refresh(tmp_path):
    stories, cache, tagger = [story("1", job()), story("2", job())], TagCache(tmp_path), Counting(TaggerOutput(Tags(post_type=["misc"]), 0.5))
    first = run_tagger(tagger, stories, cache)
    assert tagger.calls == 2 and not any(p.cached for p in first) and first[0].cost_usd == 0.5
    second = run_tagger(tagger, stories, cache)
    assert tagger.calls == 2 and all(p.cached for p in second) and second[0].cost_usd == 0.5
    assert second[0].latency_s == first[0].latency_s
    run_tagger(tagger, stories, cache, refresh=True)
    assert tagger.calls == 4


def test_runner_failure_is_unsure_uncached_and_leaks_only_the_type(tmp_path):
    cache, tagger = TagCache(tmp_path), Counting(fail=True)
    out = run_tagger(tagger, [story("1", job())], cache)
    assert out[0].tags == Tags.unsure() and out[0].error == "RuntimeError" and "secret" not in repr(out[0])
    run_tagger(tagger, [story("1", job())], cache)
    assert tagger.calls == 2  # failures are retried


def test_runner_rejects_non_tags_output():
    class Bad:
        name, version = "bad", "1"

        def tag(self, s):
            return {"post_type": ["misc"]}

    assert run_tagger(Bad(), [story("1", job())])[0].error == "BadOutput"


def test_runner_measures_latency_with_injected_clock():
    ticks = iter([10.0, 10.75])
    out = run_tagger(Counting(), [story("1", job())], clock=lambda: next(ticks))
    assert out[0].latency_s == 0.75


# -- taggers ------------------------------------------------------------------------------------


def test_classifier_adapter_returns_the_classifiers_tags():
    expected = Tags(post_type=["job_posting", "event"], role=["swe"])

    class Fixed:
        def classify(self, item):
            return expected

    tagger = ClassifierAdapter(Fixed(), "fixed")
    assert tagger.tag(story("1", job())) == expected
    assert tagger.name == "classify-fixed" and tagger.version.startswith("cat-")


def test_rules_adapter_uses_links_from_the_sidecar():
    tagger = load_tagger("rules")
    assert tagger.tag(story("1", job(), links=["https://example.com/j"])).certain_value("post_type") is PostType.JOB_POSTING
    assert tagger.tag(story("2", job())).certain_value("post_type") is PostType.MISC


def test_load_tagger_specs(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    assert isinstance(load_tagger("unsure"), UnsureBaseline)
    assert load_tagger("test_evaluation:Counting").name == "counting"
    for bad in ("nope", "test_evaluation:Missing", "no_such_module:f"):
        with pytest.raises(ValueError):
            load_tagger(bad)


# -- pings --------------------------------------------------------------------------------------


def test_missed_and_extra_pings():
    cfg = metrics.PingConfig("swe", "", PingPrefs.from_keys(["role:swe"]))
    stories = [story("hit", job(role=["swe"])), story("miss", job(role=["swe"])), story("extra", job(role=["pm"])), story("none", job(role=["pm"])), story("misc", Tags(post_type=["misc"]))]
    preds = [pred("hit", job(role=["swe"])), pred("miss", job(role=["pm"])), pred("extra", Tags.unsure()), pred("none", job(role=["pm"])), pred("misc", Tags(post_type=["misc"]))]
    (res,) = metrics.evaluate_pings([cfg], stories, preds)
    assert (res.should_ping, res.predicted, res.missed, res.extra) == (2, 2, 1, 1)
    assert res.missed_ids == ["miss"] and res.extra_ids == ["extra"] and res.recall == 0.5


def test_unsure_baseline_never_misses_a_job_ping():
    cfg = metrics.PingConfig("any", "", PingPrefs.from_keys(["role:swe", "level:internship"]))
    stories = [story("1", job()), story("2", job(role=["pm"]))]
    (res,) = metrics.evaluate_pings([cfg], stories, [pred(s.media_id, Tags.unsure()) for s in stories])
    assert res.missed == 0 and res.extra == 1


def test_recall_is_none_without_positives():
    cfg = metrics.PingConfig("pm", "", PingPrefs.from_keys(["role:pm"]))
    (res,) = metrics.evaluate_pings([cfg], [story("1", job())], [pred("1", job())])
    assert res.recall is None


def test_checked_in_ping_configs_load():
    configs = metrics.load_ping_configs(DEFAULT_PING_CONFIGS)
    assert 5 <= len(configs) <= 10 and len({c.name for c in configs}) == len(configs)
    assert all(not c.prefs.is_empty for c in configs)


def test_ping_config_errors(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[[config]]\nname = "x"\nping_me = ["role:nope"]\n')
    with pytest.raises(ValueError, match="x"):
        metrics.load_ping_configs(path)
    path.write_text("")
    with pytest.raises(ValueError):
        metrics.load_ping_configs(path)


# -- dimensions ---------------------------------------------------------------------------------


def test_dimension_stats():
    stories = [story("1", job(role=["swe"])), story("2", job(role=["swe", "pm"])), story("3", job(role=["ml"])), story("4", Tags(post_type=["misc"]))]
    preds = [
        pred("1", job(role=["swe"])),  # exact
        pred("2", job(role=["swe", "pm", "ml"])),  # covers, not exact
        pred("3", job(role=["swe"])),  # confident wrong
        pred("4", Tags(post_type=["misc"])),
    ]
    role = metrics.dimension_stats(stories, preds)["role"]
    assert (role.n, role.exact, role.covers, role.confident, role.confident_wrong) == (3, 1, 2, 2, 1)
    assert role.exact_rate == pytest.approx(1 / 3) and role.mean_size == pytest.approx((1 + 3 + 1) / 3)
    assert metrics.dimension_stats(stories, preds)["post_type"].exact == 4  # N/A labels aren't counted for role


def test_tagger_values_where_label_is_na_are_counted_separately():
    stories = [story("1", Tags(post_type=["misc"])), story("2", Tags(post_type=["misc"])), story("3", job())]
    preds = [pred("1", Tags.unsure()), pred("2", Tags(post_type=["misc"])), pred("3", job())]
    role = metrics.dimension_stats(stories, preds)["role"]
    assert (role.n, role.predicted_applicable_gold_na, role.exact) == (1, 1, 1)
    out = render_markdown(build_report("t", "1", LoadResult(stories=stories), preds, metrics.load_ping_configs(DEFAULT_PING_CONFIGS), frozenset()))
    assert "Labeled N/A, tagger gave values" in out and "`multi` row" in out and "Said N/A" in out


def test_predicted_not_applicable_counts_as_a_miss():
    st = metrics.dimension_stats([story("1", job())], [pred("1", Tags(post_type=["misc"]))])["role"]
    assert (st.n, st.predicted_na, st.exact, st.covers) == (1, 1, 0, 0) and st.mean_size is None


def test_confusion_matrix_cells():
    stories = [story("1", job(role=["swe"])), story("2", job(role=["swe"])), story("3", job(role=["swe", "pm"])), story("4", job(role=["ml"]))]
    preds = [pred("1", job(role=["swe"])), pred("2", job(role=["pm", "swe"])), pred("3", job(role=["swe"])), pred("4", Tags(post_type=["misc"]))]
    role = metrics.confusion_matrices(stories, preds)["role"]
    assert role["swe"] == {"swe": 1, "multi": 1}
    assert role["multi"] == {"swe": 1}
    assert role["ml"] == {"n/a": 1}


# -- no post ------------------------------------------------------------------------------------


def test_no_post_never_drops_a_story_that_could_be_a_job():
    no_post = metrics.parse_no_post(["post_type:misc", "post_type:event"])
    drop = metrics.stub_would_drop(no_post)
    assert drop(Tags(post_type=["misc"])) and drop(Tags(post_type=["event"]))
    assert not drop(Tags(post_type=["misc", "job_posting"]))  # could be a job
    assert not drop(Tags(post_type=["misc", "event"]))  # not certain
    assert not drop(Tags.unsure())


def test_no_post_counts_dropped_job_postings():
    no_post = metrics.parse_no_post(["post_type:misc"])
    stories = [story("job", job()), story("misc", Tags(post_type=["misc"])), story("job2", job())]
    preds = [pred("job", Tags(post_type=["misc"])), pred("misc", Tags(post_type=["misc"])), pred("job2", job())]
    res = metrics.evaluate_no_post(stories, preds, no_post)
    assert (res.dropped, res.dropped_job_postings, res.job_posting_ids, res.stub) == (2, 1, ["job"], True)


def test_no_post_accepts_a_real_drop_function():
    res = metrics.evaluate_no_post([story("job", job())], [pred("job", job())], frozenset(), drop=lambda tags: True)
    assert res.dropped_job_postings == 1 and not res.stub


def test_parse_no_post_rejects_unknown_keys():
    with pytest.raises(ValueError):
        metrics.parse_no_post(["post_type:nope"])


# -- timing and counts --------------------------------------------------------------------------


def test_timing_and_cost():
    preds = [pred("1", job(), 1.0, 0.02), pred("2", job(), 3.0, 0.04, cached=True), pred("3", Tags.unsure(), 9.0, error="X")]
    t = metrics.timing(preds)
    assert (t.n, t.cached, t.errors, t.cost_known) == (3, 1, 1, 2)
    assert t.mean_s == pytest.approx(2.0) and t.max_s == 3.0
    assert t.cost_total_usd == pytest.approx(0.06) and t.cost_per_story_usd == pytest.approx(0.03)


def test_timing_without_costs_or_predictions():
    t = metrics.timing([pred("1", job(), 1.0)])
    assert t.cost_total_usd is None and t.cost_per_story_usd is None
    assert metrics.timing([]).mean_s is None


def test_label_counts_flag_rare_values():
    stories = [story("1", job(role=["swe", "pm"])), story("2", job(role=["swe"])), story("3", Tags(post_type=["misc"]))]
    counts = metrics.label_counts(stories, min_count=2)
    assert counts.by_dimension["role"] == {"ml": 0, "swe": 2, "pm": 1, "other": 0}
    assert counts.not_applicable["role"] == 1 and counts.stories == 3
    assert "role:pm (1)" in counts.rare and "role:ml (0)" in counts.rare and "role:swe (2)" not in counts.rare
    assert "post_type:event (0)" in counts.rare


# -- report and CLI -----------------------------------------------------------------------------


def make_archive(root):
    write_sidecar(root, "1", minutes=1, links=["https://example.com/private-path"])
    write_sidecar(root, "2", minutes=2)
    write_label(root, "1", JOB_SEL)
    write_label(root, "2", {"post_type": ["misc"]})


def test_cli_markdown_report_and_cache(tmp_path, capsys):
    make_archive(tmp_path / "arch")
    cache = tmp_path / "cache"
    argv = [str(tmp_path / "arch"), "--tagger", "rules", "--cache-dir", str(cache)]
    assert main(argv) == 0
    text = capsys.readouterr().out
    for heading in ("Missed pings", "Per-dimension accuracy", "Confusion matrices", "no post", "Latency and cost", "Labeled stories per value"):
        assert heading in text
    assert "private-path" not in text and "RARE" in text
    assert list(cache.glob("classify-rules/*/*.json"))
    assert main(argv + ["--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["stories"] == 2 and doc["timing"]["cached"] == 2 and doc["missed_total"] == 0


def test_cli_writes_out_file_and_baseline(tmp_path, capsys):
    make_archive(tmp_path / "arch")
    out = tmp_path / "r" / "report.md"
    assert main([str(tmp_path / "arch"), "--tagger", "unsure", "--no-cache", "--out", str(out)]) == 0
    assert out.read_text().startswith("# Tagger evaluation: unsure")
    assert not (tmp_path / "eval-cache").exists()


def test_cli_exit_codes(tmp_path, capsys):
    assert main([str(tmp_path), "--no-cache"]) == 1  # nothing labeled
    make_archive(tmp_path)
    assert main([str(tmp_path), "--tagger", "nope"]) == 2
    assert main([str(tmp_path), "--ping-configs", str(tmp_path / "missing.toml")]) == 2
    assert main([str(tmp_path), "--no-post", "bogus:thing"]) == 2


def test_empty_report_is_json_serializable_and_renders():
    report = build_report("t", "1", LoadResult(), [], metrics.load_ping_configs(DEFAULT_PING_CONFIGS), frozenset())
    json.dumps(report)
    text = render_markdown(report)  # an empty set still renders
    assert "No labeled stories." in text and "n/a" in text
