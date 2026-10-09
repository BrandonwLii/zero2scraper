"""#16: ping preference storage, the /pings logic and the discord glue (fakes only, no network)."""

import asyncio
import sqlite3
from types import SimpleNamespace

import discord
import pytest
from test_bot_logic import env

from story_watch.bot import pings_app
from story_watch.bot import prefs as P
from story_watch.bot.config import load_bot_config
from story_watch.bot.discord_app import LabelBot
from story_watch.pings import PingPrefs
from story_watch.store import Store

UID = "100000000000000001"
OTHER = "100000000000000002"


def run(coro):
    return asyncio.run(coro)


# -- storage ------------------------------------------------------------------------------------


def test_wal_and_migration_are_idempotent(tmp_path):
    path = tmp_path / "data" / "state.db"
    Store(path).close()
    Store(path).close()  # a second start changes nothing and doesn't fail
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"seen", "targets", "ping_users", "ping_prefs"} <= tables


def test_migrates_an_existing_rollback_journal_database(tmp_path):
    path = tmp_path / "state.db"
    old = sqlite3.connect(path)
    old.executescript("CREATE TABLE seen (media_id TEXT PRIMARY KEY, target TEXT NOT NULL, taken_at REAL NOT NULL, notified_at REAL);")
    old.execute("INSERT INTO seen VALUES ('m', 't', 1.0, NULL)")
    old.commit()
    old.close()
    store = Store(path)
    assert store.is_seen("m")
    assert store.get_ping_prefs(UID) is None
    store.close()


def test_roundtrip_with_post_type_default(store):
    assert store.get_ping_prefs(UID) is None
    store.set_ping_prefs(UID, PingPrefs.from_keys(["level:internship"], ["sponsorship:no_sponsor"]))
    got = store.get_ping_prefs(UID)
    assert got.to_keys() == (["level:internship", "post_type:job_posting"], ["sponsorship:no_sponsor"])


def test_set_replaces_and_users_are_separate(store):
    store.set_ping_prefs(UID, PingPrefs.from_keys(["role:swe"]))
    store.set_ping_prefs(OTHER, PingPrefs.from_keys(["role:pm"]))
    store.set_ping_prefs(UID, PingPrefs.from_keys(["role:ml"], ["company:quant"]))
    assert store.get_ping_prefs(UID).to_keys() == (["post_type:job_posting", "role:ml"], ["company:quant"])
    assert store.get_ping_prefs(OTHER).to_keys() == (["post_type:job_posting", "role:pm"], [])
    assert set(store.all_ping_prefs()) == {UID, OTHER}


def test_empty_prefs_are_saved_as_empty_not_missing(store):
    store.set_ping_prefs(UID, PingPrefs.from_keys([], ["sponsorship:no_sponsor"]))
    got = store.get_ping_prefs(UID)
    assert got is not None and got.is_empty
    assert got.to_keys() == ([], ["sponsorship:no_sponsor"])


def test_clear(store):
    store.set_ping_prefs(UID, PingPrefs.from_keys(["role:swe"]))
    assert store.clear_ping_prefs(UID) is True
    assert store.get_ping_prefs(UID) is None
    assert store._db.execute("SELECT COUNT(*) FROM ping_prefs").fetchone()[0] == 0  # cascade
    assert store.clear_ping_prefs(UID) is False


def test_unknown_stored_keys_are_ignored(store):
    store.set_ping_prefs(UID, PingPrefs.from_keys(["role:swe"]))
    with store._db:
        store._db.execute("INSERT INTO ping_prefs VALUES (?, 'ping', 'role:wizard')", (UID,))
        store._db.execute("INSERT INTO ping_prefs VALUES (?, 'mute', 'nonsense')", (UID,))
    assert store.get_ping_prefs(UID).to_keys() == (["post_type:job_posting", "role:swe"], [])


def test_two_connections_can_write_in_wal(tmp_path):
    a, b = Store(tmp_path / "s.db"), Store(tmp_path / "s.db")
    a.set_ping_prefs(UID, PingPrefs.from_keys(["role:swe"]))
    b.set_ping_prefs(OTHER, PingPrefs.from_keys(["role:pm"]))
    assert a.get_ping_prefs(OTHER) is not None and b.get_ping_prefs(UID) is not None


# -- pure logic ---------------------------------------------------------------------------------


def test_options_fit_one_select_and_are_unique():
    opts = P.all_options()
    assert len(opts) == 17 <= P.SELECT_LIMIT
    assert len({k for k, _ in opts}) == len(opts)
    assert ("role:other", "Role: Other") in opts and ("level:other", "Level: Other") in opts


def test_new_user_default_is_mute_no_sponsor():
    assert P.initial_lists(None) == ([], ["sponsorship:no_sponsor"])
    text = P.describe(PingPrefs.from_keys(*P.initial_lists(None)), saved=False)
    assert "won't be pinged" in text and "not saved yet" in text and "No sponsor" in text


def test_picking_moves_between_lists():
    ping, mute = P.pick_mute(["role:swe"], [], ["role:swe", "company:quant"])
    assert ping == [] and mute == ["company:quant", "role:swe"]
    ping, mute = P.pick_ping(ping, mute, ["company:quant", "level:internship"])
    assert mute == ["role:swe"]
    assert ping == ["company:quant", "level:internship", "post_type:job_posting"]


def test_deselecting_removes_a_value():
    ping, mute = P.pick_ping(["role:swe", "role:pm"], [], ["role:swe"])
    assert ping == ["post_type:job_posting", "role:swe"]


def test_pick_rejects_unknown_values():
    with pytest.raises(ValueError):
        P.pick_ping([], [], ["role:wizard"])


def test_warn_empty_ping_list():
    assert any("won't be pinged" in w for w in P.warnings(PingPrefs.from_keys([], ["company:quant"])))


@pytest.mark.parametrize(
    "mute",
    [
        ["sponsorship:sponsor_or_canadian", "sponsorship:no_sponsor", "sponsorship:unknown"],
        ["company:faang_plus", "company:quant", "company:other"],
        ["post_type:event", "post_type:job_posting", "post_type:process_info", "post_type:misc"],
    ],
)
def test_warn_when_a_whole_dimension_is_blocked(mute):
    warns = P.warnings(PingPrefs.from_keys(["role:swe"], mute))
    assert any("blocked every" in w for w in warns)


def test_no_blocked_warning_for_a_partial_block():
    assert P.warnings(PingPrefs.from_keys(["role:swe"], ["sponsorship:no_sponsor", "company:quant"])) == []


def test_warn_when_the_default_post_type_is_also_muted():
    warns = P.warnings(PingPrefs.from_keys(["level:internship"], ["post_type:job_posting"]))
    assert len(warns) == 1 and "both lists" in warns[0]


def test_describe_says_job_postings_only_without_post_type():
    text = P.describe(PingPrefs.from_keys(["role:swe"]))
    assert "Job postings only" in text and "Role: SWE" in text
    assert "Job postings only" not in P.describe(PingPrefs.from_keys(["post_type:event"]))


# -- discord glue (fakes) ----------------------------------------------------------------------


class Resp:
    def __init__(self):
        self.calls = []

    async def send_message(self, content=None, **kw):
        self.calls.append(("send", content, kw))

    async def edit_message(self, **kw):
        self.calls.append(("edit", kw.get("content"), kw))


def interaction(user_id=UID):
    return SimpleNamespace(user=SimpleNamespace(id=int(user_id)), response=Resp())


def select_of(view, placeholder):
    return next(c for c in view.children if c.placeholder == placeholder)


def choose(select, values):
    """Pretend the user picked `values`; Select.values reads the component's stored data."""
    select._values = list(values)


def test_edit_show_clear_flow(tmp_path):
    db = tmp_path / "state.db"
    group = pings_app.PingsGroup(db)

    async def go():
        i = interaction()
        await group.edit.callback(group, i)  # new user: defaults offered, nothing saved yet
        kind, text, kw = i.response.calls[0]
        assert kw["ephemeral"] is True and "not saved yet" in text
        view = kw["view"]
        assert [k for k in (o.value for o in select_of(view, "Never ping me for").options if o.default)] == ["sponsorship:no_sponsor"]
        assert Store(db).get_ping_prefs(UID) is None

        # pick "Role: SWE" under ping me
        choose(select_of(view, "Ping me for"), ["role:swe"])
        i2 = interaction()
        i2.message = None
        await select_of(view, "Ping me for").callback(i2)
        _, text2, kw2 = i2.response.calls[0]
        assert "Role: SWE" in text2 and "Job postings only" in text2
        new_view = kw2["view"]
        assert {o.value for o in select_of(new_view, "Ping me for").options if o.default} == {"role:swe", "post_type:job_posting"}
        saved = Store(db).get_ping_prefs(UID)
        assert saved.to_keys() == (["post_type:job_posting", "role:swe"], ["sponsorship:no_sponsor"])

        # moving swe to the mute list takes it off the ping list
        choose(select_of(new_view, "Never ping me for"), ["role:swe"])
        await select_of(new_view, "Never ping me for").callback(interaction())
        # (the mute pick replaces the mute list, so the earlier no-sponsor default is gone too;
        # the defaulted post type stays on the ping list)
        assert Store(db).get_ping_prefs(UID).to_keys() == (["post_type:job_posting"], ["role:swe"])

        i3 = interaction()
        await group.show.callback(group, i3)
        assert "Never ping me for" in i3.response.calls[0][1] and i3.response.calls[0][2]["ephemeral"]

        i4 = interaction()
        await group.clear.callback(group, i4)
        assert i4.response.calls[0][1].startswith("Cleared")
        assert Store(db).get_ping_prefs(UID) is None

    run(go())


def test_show_for_a_user_with_nothing_saved(tmp_path):
    group = pings_app.PingsGroup(tmp_path / "state.db")
    i = interaction()
    run(group.show.callback(group, i))
    assert "won't be pinged" in i.response.calls[0][1]


def test_only_the_owner_can_use_the_edit_view(tmp_path):
    view = pings_app.EditView(tmp_path / "s.db", UID, [], [])
    assert run(view.interaction_check(interaction(UID))) is True
    assert run(view.interaction_check(interaction(OTHER))) is False


def test_db_failure_is_reported_without_details(tmp_path, caplog):
    group = pings_app.PingsGroup(tmp_path / "missing" / "dir" / "x" / "state.db")
    (tmp_path / "missing").write_text("a file, so mkdir fails")
    i = interaction()
    run(group.show.callback(group, i))
    assert "Could not reach" in i.response.calls[0][1]
    assert str(tmp_path) not in caplog.text


def test_commands_are_registered_for_the_guild_only(tmp_path):
    cfg = load_bot_config(env(tmp_path, DB_PATH=str(tmp_path / "state.db")))
    assert cfg.db_path == tmp_path / "state.db"
    bot = LabelBot(cfg)
    synced = []

    async def sync(guild=None):
        synced.append(guild.id)

    bot.tree.sync = sync

    async def fetch(_id):
        return SimpleNamespace(guild=SimpleNamespace(id=424242))

    bot.fetch_channel = fetch
    run(bot._register_pings())
    assert synced == [424242]
    assert bot.tree.get_commands() == []  # nothing global
    assert [c.name for c in bot.tree.get_commands(guild=discord.Object(id=424242))] == ["pings"]
    group = bot.tree.get_commands(guild=discord.Object(id=424242))[0]
    assert {c.name for c in group.commands} == {"edit", "show", "clear"}


def test_register_failure_does_not_stop_the_bot(tmp_path):
    bot = LabelBot(load_bot_config(env(tmp_path)))

    async def fetch(_id):
        raise discord.HTTPException(SimpleNamespace(status=403, reason="x"), "nope")

    bot.fetch_channel = fetch
    run(bot._register_pings())  # logs the type only
