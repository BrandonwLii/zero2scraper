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
