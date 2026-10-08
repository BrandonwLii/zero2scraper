import pytest

from story_watch import main
from story_watch.notify import COLOR_OK, DiscordError


class RecordingNotifier:
    def __init__(self):
        self.alerts: list[tuple] = []
        self.fail = False

    def alert(self, title, description, color=0):
        if self.fail:
            raise DiscordError("boom")
        self.alerts.append((title, description, color))


@pytest.fixture
def notifier(cfg, monkeypatch):
    fake = RecordingNotifier()
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    monkeypatch.setattr(main, "Notifier", lambda url: fake)
    return fake


def test_deploy_notify_sends_commit(notifier):
    assert main.cli(["--deploy-notify", "abc1234-dirty"]) == 0
    assert notifier.alerts == [("story-watch deployed", "Commit `abc1234-dirty` is running.", COLOR_OK)]


def test_deploy_notify_failure_exits_nonzero(notifier):
    notifier.fail = True
    assert main.cli(["--deploy-notify", "abc1234"]) == 1


LOG = ["c" * 40 + "\tAdd changelog\n", "b" * 40 + "\tFix embed\n", "a" * 40 + "\tOld thing\n"]


def test_deploy_message_lists_commits_since_last_deploy():
    text = main.deploy_message("ccccccc", LOG, since="a" * 40)
    assert text == ("Commit `ccccccc` is running.\n\nChanges since the last deploy:\n"
                    "- `ccccccc` Add changelog\n- `bbbbbbb` Fix embed")


@pytest.mark.parametrize("since, commit, expected", [
    ("c" * 40, "ccccccc", "No new commits since the last deploy."),
    ("c" * 40, "ccccccc-dirty", "No new commits since the last deploy.\n- plus uncommitted changes"),
    ("", "ccccccc", "No previous deploy recorded, so no changelog."),
    ("d" * 40, "ccccccc", "Previous deploy `ddddddd` isn't in the recent history, so no changelog."),
])
def test_deploy_message_edge_cases(since, commit, expected):
    text = main.deploy_message(commit, LOG, since=since)
    assert expected in text and "Old thing" not in text


def test_deploy_message_caps_long_changelogs():
    log = [f"{i:040d}\tcommit {i}" for i in range(41)]
    text = main.deploy_message("x", log, since=f"{40:040d}")
    assert text.count("\n- `") == main.CHANGELOG_MAX and text.endswith("…and 25 more")


def test_deploy_notify_reads_changelog_file(notifier, tmp_path):
    f = tmp_path / "log"
    f.write_text("".join(LOG))
    assert main.cli(["--deploy-notify", "ccccccc", "--changelog", str(f), "--since", "b" * 40]) == 0
    assert notifier.alerts[0][1].endswith("Changes since the last deploy:\n- `ccccccc` Add changelog")


def test_check_config_validates_only_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("IG_USER", "from-shell")  # must not mask a missing key in the file
    f = tmp_path / ".env"
    f.write_text("IG_USER=b\nDISCORD_WEBHOOK=https://discord.com/api/webhooks/1/s\n")
    assert main.cli(["--check-config", str(f)]) == 0
    f.write_text("DISCORD_WEBHOOK=https://discord.com/api/webhooks/1/s\n")
    assert main.cli(["--check-config", str(f)]) == 2


def test_check_config_warns_about_leftover_servers_by_name_only(tmp_path, caplog):
    f = tmp_path / ".env"
    f.write_text("IG_USER=b\nDISCORD_WEBHOOK=https://discord.com/api/webhooks/1/s\n"
                 "DISCORD_WEBHOOK_2=https://discord.com/api/webhooks/2/hidden\nROLE_MISC_2=222222222222222222\n")
    with caplog.at_level("WARNING"):
        assert main.cli(["--check-config", str(f)]) == 0
    assert "DISCORD_WEBHOOK_2" in caplog.text and "ROLE_MISC_2" in caplog.text
    assert "hidden" not in caplog.text and "222222222222222222" not in caplog.text
