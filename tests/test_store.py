import time
from datetime import timedelta

from conftest import make_item


def test_prune_removes_rows_older_than_48h(store):
    store.mark_seen(make_item("old", age=timedelta(hours=49)))
    store.mark_seen(make_item("new", age=timedelta(hours=47)))
    assert store.prune(now=time.time()) == 1
    assert not store.is_seen("old")
    assert store.is_seen("new")


def test_prune_keeps_target_seeded(store):
    store.seed("alice", [make_item("old", age=timedelta(hours=72))])
    store.prune()
    assert store.is_seeded("alice")


def test_tags_round_trip_and_are_pruned_with_the_item():
    from story_watch.store import Store
    from story_watch.tags import Level, PostType, Tags

    store = Store(":memory:")
    old, new = make_item("old", age=timedelta(hours=49)), make_item("new")
    tags = Tags(post_type=[PostType.JOB_POSTING], level=[Level.INTERNSHIP], confidence=0.5, evidence="x")
    assert store.get_tags("new") is None
    store.save_tags(new, tags)
    store.save_tags(old, Tags.unsure())
    store.save_tags(new, Tags.unsure())  # first classification wins
    assert store.get_tags("new") == tags
    store.prune()
    assert store.get_tags("old") is None and store.get_tags("new") == tags


def test_unreadable_stored_tags_mean_reclassify(tmp_path):
    from story_watch.store import Store

    store = Store(tmp_path / "s.db")
    store._db.execute("INSERT INTO story_tags VALUES ('1', 0, '{\"bogus\": 1}')")
    assert store.get_tags("1") is None


def test_schema_migration_is_idempotent_on_an_old_database(tmp_path):
    import sqlite3

    from story_watch.store import Store

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE seen (media_id TEXT PRIMARY KEY, target TEXT NOT NULL, taken_at REAL NOT NULL, notified_at REAL)")
    db.execute("INSERT INTO seen VALUES ('1', 'a', 1, NULL)")
    db.commit()
    db.close()
    Store(path).close()
    store = Store(path)
    assert store.is_seen("1") and store.get_tags("1") is None
