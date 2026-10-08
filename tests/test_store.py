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


def test_stale_partial_deliveries_are_pruned(store):
    store.mark_delivered("1", "hook")
    assert store.delivered_to("1") == {"hook"}
    store.prune(now=time.time() + 49 * 3600)
    assert store.delivered_to("1") == set()
