from datetime import datetime, timedelta, timezone

import pytest

from story_watch.config import Config
from story_watch.instagram import StoryItem
from story_watch.notify import DiscordError
from story_watch.store import Store


def make_item(media_id: str, target: str = "alice", age: timedelta = timedelta(minutes=5)) -> StoryItem:
    return StoryItem(
        media_id=media_id,
        target=target,
        taken_at=datetime.now(timezone.utc) - age,
        is_video=False,
        thumbnail_url=f"https://cdn.example/{media_id}.jpg",
    )


class FakeIG:
    def __init__(self):
        self.items: dict[str, list[StoryItem]] = {}
        self.error: Exception | None = None

    def fetch_story_items(self, target):
        if self.error:
            raise self.error
        return list(self.items.get(target, []))


class FakeNotifier:
    def __init__(self):
        self.stories: list[StoryItem] = []
        self.alerts: list[str] = []
        self.fail = False

    def story(self, item):
        if self.fail:
            raise DiscordError("boom")
        self.stories.append(item)

    def alert(self, title, description, color=0):
        if self.fail:
            raise DiscordError("boom")
        self.alerts.append(title)


@pytest.fixture
def cfg(tmp_path):
    return Config(
        ig_user="burner",
        ig_session_path=None,
        targets=("alice",),
        discord_webhook="https://discord.com/api/webhooks/1/x",
        min_wait=300,
        max_wait=600,
        fail_alert_threshold=3,
        db_path=tmp_path / "state.db",
    )


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()
