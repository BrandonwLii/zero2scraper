"""The bot's own env file (#45): only the allowlisted keys of .env reach the bot's user."""

import io
import os
from contextlib import redirect_stderr, redirect_stdout

import pytest
from dotenv import dotenv_values

from story_watch.bot import envfile
from story_watch.bot.config import BOT_ENV_KEYS, load_bot_config

CHANNEL = "100000000000000001"
USER = "100000000000000002"

# Fake values; the watcher-only ones must never come out.
SECRETS = {
    "CLAUDE_CODE_OAUTH_TOKEN": "fake-claude-oauth-value",
    "IG_USER": "fake-ig-user",
    "IG_SESSION_PATH": "/fake/session-file",
    "TARGETS": "fake-target:1",
    "TEST_DISCORD_WEBHOOK": "https://discord.com/api/webhooks/2/testsecret",
    "LLM_URL": "http://127.0.0.1:1/fake",
}
BOT = {
    "DISCORD_BOT_TOKEN": "fake-bot-token",
    "LABEL_CHANNEL_ID": CHANNEL,
    "LABELER_USER_IDS": f"<@{USER}>, {USER}",
    "ARCHIVE_DIR": "/fake/archive",
    "DB_PATH": "/fake/data/state.db",
    "DISCORD_WEBHOOK": "https://discord.com/api/webhooks/1/secret",
    "LABEL_SINCE": "2026-10-01",
}

DOTENV = """\
# a comment
IG_USER=fake-ig-user
export CLAUDE_CODE_OAUTH_TOKEN=fake-claude-oauth-value
IG_SESSION_PATH = /fake/session-file
TARGETS='fake-target:1'
TEST_DISCORD_WEBHOOK="https://discord.com/api/webhooks/2/testsecret"
LLM_URL=http://127.0.0.1:1/fake
DISCORD_BOT_TOKEN=fake-bot-token
LABEL_CHANNEL_ID=100000000000000001
LABELER_USER_IDS="<@100000000000000002>, 100000000000000002"
ARCHIVE_DIR=/fake/archive
export DB_PATH=/fake/data/state.db
DISCORD_WEBHOOK=https://discord.com/api/webhooks/1/secret
LABEL_SINCE=2026-10-01
LABELS_FILE
"""


def run_main(path):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = envfile.main([str(path)])
    return code, out.getvalue(), err.getvalue()


def test_only_allowlisted_keys_pass():
    assert envfile.bot_env({**SECRETS, **BOT, "LABELS_FILE": None}) == BOT
    assert set(BOT) <= set(BOT_ENV_KEYS)
    assert not set(SECRETS) & set(BOT_ENV_KEYS)


def test_derived_file_round_trips_and_holds_no_watcher_secret(tmp_path):
    src = tmp_path / ".env"
    src.write_text(DOTENV)
    code, out, err = run_main(src)
    assert code == 0 and err == ""
    for value in SECRETS.values():
        assert value not in out
    for key in SECRETS:
        assert key not in out
    assert dotenv_values(stream=io.StringIO(out), interpolate=False) == BOT


@pytest.mark.parametrize("value", ['has "quotes" and \\back\\slash', "spaces and $DOLLAR", "trailing\\"])
def test_awkward_values_are_quoted(value):
    out = envfile.render({"LABELS_FILE": value})
    assert dotenv_values(stream=io.StringIO(out), interpolate=False) == {"LABELS_FILE": value}


def test_line_breaks_are_refused_without_echoing_the_value(tmp_path):
    src = tmp_path / ".env"
    src.write_text('DISCORD_BOT_TOKEN="fake-line\nbreak-token"\n')
    code, out, err = run_main(src)
    assert code == 1 and out == ""
    assert "DISCORD_BOT_TOKEN" in err and "fake-line" not in err and "break-token" not in err


def test_unreadable_source_names_no_value(tmp_path):
    code, out, err = run_main(tmp_path / "missing")
    assert code == 1 and out == "" and "FileNotFoundError" in err


def test_bot_config_reads_only_allowlisted_keys(tmp_path):
    seen = []

    class Recording(dict):
        def get(self, key, default=None):
            seen.append(key)
            return super().get(key, default)

        def __getitem__(self, key):
            seen.append(key)
            return super().__getitem__(key)

    load_bot_config(Recording({**SECRETS, **BOT, "ARCHIVE_DIR": str(tmp_path), "LABELS_FILE": str(tmp_path / "l.jsonl"),
                               "LABEL_POLL_SECONDS": "60", "LABEL_BACKLOG_MAX": "0"}))
    assert seen and set(seen) <= set(BOT_ENV_KEYS)
    assert set(BOT_ENV_KEYS) <= set(seen)  # and the allowlist has no stale key


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_bot_config_tolerates_an_unreadable_dotenv(tmp_path, monkeypatch):
    # Running as its own user, the bot may find the watcher's .env it can't read: not an error.
    dotenv = tmp_path / ".env"
    dotenv.write_text("DISCORD_BOT_TOKEN=from-dotenv\n")
    dotenv.chmod(0)
    monkeypatch.chdir(tmp_path)
    for key in BOT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in {**BOT, "ARCHIVE_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(key, value)
    try:
        cfg = load_bot_config()
    finally:
        dotenv.chmod(0o600)
    assert cfg.token == "fake-bot-token"


def unit(name):
    from pathlib import Path

    text = (Path(__file__).parent.parent / "deploy" / name).read_text()
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def test_bot_unit_runs_as_its_own_user_with_only_bot_env():
    lines = unit("story-watch-bot.service")
    assert "User=storywatch-bot" in lines and "SupplementaryGroups=storywatch-shared" in lines
    assert [l for l in lines if l.startswith("EnvironmentFile=")] == ["EnvironmentFile=/opt/story-watch/bot.env"]
    # .env's lookup walks up from the working directory: nowhere near the watcher's .env.
    assert "WorkingDirectory=/" in lines
    blocked = next(l for l in lines if l.startswith("InaccessiblePaths=")).split("=", 1)[1].split()
    for path in ("/opt/story-watch/.env", "/opt/story-watch/.config", "/opt/story-watch/data/claude-home"):
        assert "-" + path in blocked
    assert "UMask=0007" in lines  # the watcher must be able to write the WAL files the bot creates
    # Both need a fresh /proc mount, which an unprivileged LXC may refuse: 226/NAMESPACE, bot down.
    assert not any(l.startswith(("ProtectProc=", "ProcSubset=")) for l in lines)


def test_watcher_unit_shares_the_group_and_keeps_its_env():
    lines = unit("story-watch.service")
    assert "User=storywatch" in lines and "SupplementaryGroups=storywatch-shared" in lines
    assert "EnvironmentFile=/opt/story-watch/.env" in lines and "UMask=0007" in lines
