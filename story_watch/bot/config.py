"""Bot configuration from the environment. Pure parsing; nothing here talks to Discord.

The token is never part of repr/str and never put in an error message.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from dotenv import find_dotenv, load_dotenv

DEFAULT_POLL_SECONDS = 30
DEFAULT_BACKLOG_MAX = 10
DEFAULT_DB_PATH = "/opt/story-watch/data/state.db"
LABELS_FILENAME = "labels.jsonl"
POSTS_FILENAME = "label-posts.jsonl"


class BotConfigError(ValueError):
    pass


@dataclass(frozen=True)
class BotConfig:
    token: str = field(repr=False)
    channel_id: int
    labeler_ids: frozenset[int]
    archive_dir: Path
    labels_path: Path
    posts_path: Path  # which stories are already in the channel (message ids)
    poll_seconds: int = DEFAULT_POLL_SECONDS
    backlog_max: int = DEFAULT_BACKLOG_MAX  # first start only: newest N stories get posted
    since: datetime | None = None  # ignore stories older than this
    db_path: Path = Path(DEFAULT_DB_PATH)  # the watcher's state.db; /pings preferences live there

    def __repr__(self) -> str:
        return (
            f"BotConfig(channel_id={self.channel_id}, labelers={len(self.labeler_ids)}, "
            f"archive_dir={str(self.archive_dir)!r}, labels_path={str(self.labels_path)!r}, "
            f"poll_seconds={self.poll_seconds}, backlog_max={self.backlog_max}, since={self.since}, db_path={str(self.db_path)!r})"
        )

    __str__ = __repr__


def parse_snowflake(raw: str, key: str) -> int:
    raw = raw.strip()
    if not (raw.isdigit() and 15 <= len(raw) <= 21):
        raise BotConfigError(f"{key}: {raw!r} is not a Discord id (enable Developer Mode, right-click, Copy ID)")
    return int(raw)


def parse_user_ids(raw: str, key: str = "LABELER_USER_IDS") -> frozenset[int]:
    ids = frozenset(parse_snowflake(p.strip().lstrip("<@").rstrip(">"), key) for p in raw.split(",") if p.strip())
    return ids


def _int(env: Mapping[str, str], key: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise BotConfigError(f"{key} must be an integer, got {raw!r}") from None
    if not lo <= value <= hi:
        raise BotConfigError(f"{key} must be between {lo} and {hi}, got {value}")
    return value


def _path(env: Mapping[str, str], key: str) -> Path | None:
    raw = env.get(key, "").strip()
    if not raw:
        return None
    if not os.path.isabs(raw):
        raise BotConfigError(f"{key} must be an absolute path")
    return Path(raw)


def parse_since(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise BotConfigError("LABEL_SINCE must be an ISO date or time, e.g. 2026-10-01") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_bot_config(env: Mapping[str, str] | None = None) -> BotConfig:
    """Build a BotConfig from `env` (defaults to os.environ after loading .env)."""
    if env is None:
        load_dotenv(find_dotenv(usecwd=True))
        env = os.environ

    token = env.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise BotConfigError("DISCORD_BOT_TOKEN is required")
    channel_id = parse_snowflake(env.get("LABEL_CHANNEL_ID", ""), "LABEL_CHANNEL_ID") if env.get(
        "LABEL_CHANNEL_ID", ""
    ).strip() else None
    if channel_id is None:
        raise BotConfigError("LABEL_CHANNEL_ID is required (the private channel stories are posted in)")
    labelers = parse_user_ids(env.get("LABELER_USER_IDS", ""))
    if not labelers:
        raise BotConfigError("LABELER_USER_IDS is required (comma-separated Discord user ids allowed to label)")
    archive_dir = _path(env, "ARCHIVE_DIR")
    if archive_dir is None:
        raise BotConfigError("ARCHIVE_DIR is required (the archive the watcher writes)")

    # Anything the bot writes must stay at the archive's top level: Archive.prune() evicts
    # files in <archive>/*/*.
    labels_path = _path(env, "LABELS_FILE") or archive_dir / LABELS_FILENAME
    for path in (labels_path,):
        try:
            depth = len(path.relative_to(archive_dir).parts)
        except ValueError:
            depth = 0  # outside the archive entirely is fine
        if depth > 1:
            raise BotConfigError("LABELS_FILE must not be inside a subfolder of ARCHIVE_DIR (the archive prunes those)")

    return BotConfig(
        token=token,
        channel_id=channel_id,
        labeler_ids=labelers,
        archive_dir=archive_dir,
        labels_path=labels_path,
        posts_path=archive_dir / POSTS_FILENAME,
        poll_seconds=_int(env, "LABEL_POLL_SECONDS", DEFAULT_POLL_SECONDS, 5, 3600),
        backlog_max=_int(env, "LABEL_BACKLOG_MAX", DEFAULT_BACKLOG_MAX, 0, 1000),
        since=parse_since(env.get("LABEL_SINCE", "")),
        db_path=_path(env, "DB_PATH") or Path(DEFAULT_DB_PATH),
    )
