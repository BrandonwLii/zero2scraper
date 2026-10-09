import logging
from dataclasses import replace

import pytest
from conftest import MISC_TAGS, FakeIG, make_item

from story_watch import main
from story_watch.archive import Archive
from story_watch.instagram import InstagramError, SessionError
from story_watch.jobs import JobTitles
from story_watch.tags import Tags


class ShutHTTP:
    """Stands in for requests.Session: any request is a test failure."""

    headers: dict = {}
    cookies = type("C", (), {"clear": lambda self: None})()

    def get(self, *a, **kw):
        raise AssertionError("network used")


class RecordingArchive(Archive):
    def __init__(self, root):
        super().__init__(root, session=ShutHTTP())
        self.saved: list[tuple] = []

    def save(self, item, tags, classifier=""):
        self.saved.append((item, tags, classifier))
        return super().save(item, tags, classifier)


class Classifier:
    def __init__(self, tags=MISC_TAGS, error=None):
        self.tags, self.error = tags, error

    def classify(self, item):
        if self.error:
            raise self.error
        return self.tags


@pytest.fixture
def cfg(cfg, tmp_path):
    return replace(cfg, archive_dir=tmp_path / "archive", classifier="rules")


@pytest.fixture
def archive(cfg):
    return RecordingArchive(cfg.archive_dir)


def run(cfg, ig, archive, classifier=None):
    return main.backfill_archive(cfg, ig, archive, JobTitles(session=ShutHTTP()), classifier or Classifier())


def test_archives_only_items_not_already_archived(cfg, archive, caplog):
    ig = FakeIG()
    old, new = make_item("1001"), make_item("1002")
    ig.items["alice"] = [old, new]
    archive.root.joinpath("alice").mkdir(parents=True)
    Archive.save(archive, old, MISC_TAGS)  # already has a sidecar
    archive.saved.clear()
    with caplog.at_level(logging.INFO):
        assert run(cfg, ig, archive) == 0
    assert [i.media_id for i, _, _ in archive.saved] == ["1002"]
    assert archive.has(new)
    assert "@alice: 2 live, 1 already archived, 1 archived now" in caplog.text


def test_nothing_to_do_skips_extras(cfg, archive):
    ig = FakeIG()
    ig.items["alice"] = [make_item("1001")]
    assert run(cfg, ig, archive) == 0
    assert run(cfg, ig, archive) == 0
    assert ig.extras_calls == 1


def test_cli_posts_nothing_and_writes_no_store(cfg, monkeypatch, archive):
    ig = FakeIG()
    ig.items["alice"] = [make_item("1001")]
    boom = lambda *a, **kw: pytest.fail("must not be used")  # noqa: E731
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    monkeypatch.setattr(main, "Notifier", boom)
    monkeypatch.setattr(main, "Store", boom)
    monkeypatch.setattr(main, "InstagramClient", lambda *a, **kw: ig)
    monkeypatch.setattr(main, "Archive", lambda root, mb: archive)
    monkeypatch.setattr(main, "JobTitles", lambda: JobTitles(session=ShutHTTP()))
    monkeypatch.setattr(main, "build_classifier", lambda name: Classifier())
    assert main.cli(["--backfill-archive"]) == 0
    assert [i.media_id for i, _, _ in archive.saved] == ["1001"]


def test_failed_extras_still_archives(cfg, archive):
    ig = FakeIG()
    ig.extras_error = InstagramError("page failed")
    ig.items["alice"] = [make_item("1001")]
    assert run(cfg, ig, archive) == 0
    assert [i.media_id for i, _, _ in archive.saved] == ["1001"]


def test_extras_links_are_archived(cfg, archive):
    ig = FakeIG()
    ig.items["alice"] = [make_item("1001")]
    ig.links["1001"] = ("https://example.com/page",)  # not a job link, so no lookup
    assert run(cfg, ig, archive) == 0
    assert archive.saved[0][0].links == ("https://example.com/page",)


def test_classifier_exception_gives_unsure_tags(cfg, archive):
    ig = FakeIG()
    ig.items["alice"] = [make_item("1001")]
    assert run(cfg, ig, archive, Classifier(error=RuntimeError("secret"))) == 0
    _, tags, name = archive.saved[0]
    assert tags == Tags.unsure() and name == "rules"


def test_missing_archive_dir_is_an_error(cfg, monkeypatch, caplog):
    monkeypatch.setattr(main, "load_config", lambda: replace(cfg, archive_dir=None))
    monkeypatch.setattr(main, "InstagramClient", lambda *a, **kw: pytest.fail("must not start"))
    assert main.cli(["--backfill-archive"]) == 2
    assert "ARCHIVE_DIR" in caplog.text


@pytest.mark.parametrize("err", [InstagramError("tok3n"), SessionError("tok3n")])
def test_instagram_error_exits_1_and_logs_type_only(cfg, archive, caplog, err):
    ig = FakeIG()
    ig.error = err
    assert run(cfg, ig, archive) == 1
    assert type(err).__name__ in caplog.text and "tok3n" not in caplog.text
    assert archive.saved == []


def test_archive_has(tmp_path):
    a = Archive(tmp_path, session=ShutHTTP())
    item = make_item("1001")
    assert not a.has(item)
    assert a.save(item, MISC_TAGS)
    assert a.has(item)
    assert not a.has(make_item("1002"))
    assert not a.has(replace(item, target="bob"))
