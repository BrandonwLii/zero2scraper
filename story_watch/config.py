"""Load and validate configuration from the environment (and .env)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import find_dotenv, load_dotenv

from .classify import CLASSIFIERS, Category

WEBHOOK_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
    "https://ptb.discord.com/api/webhooks/",
    "https://canary.discord.com/api/webhooks/",
)


class ConfigError(ValueError):
    pass


def webhook_id(url: str) -> str:
    """The numeric id in .../api/webhooks/<id>/<token>. Not secret; the token is."""
    for prefix in WEBHOOK_PREFIXES:
        if url.startswith(prefix):
            return url[len(prefix):].split("/")[0]
    raise ValueError("not a Discord webhook URL")


@dataclass(frozen=True)
class Destination:
    """One Discord server: DISCORD_WEBHOOK[_<n>] plus its ROLE_<CATEGORY>[_<n>] pings."""

    name: str  # "1" for the unsuffixed keys, else the env suffix; used in logs
    webhook: str = field(repr=False)
    role_ids: Mapping[Category, tuple[str, ...]] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable id for delivery tracking, so renumbering servers doesn't resend."""
        return webhook_id(self.webhook)


@dataclass(frozen=True)
class Config:
    ig_user: str
    ig_session_path: str | None
    targets: tuple[str, ...]
    discord_webhook: str
    # username -> numeric userid from TARGETS ("name:id"); skips the 429-prone lookup
    target_ids: Mapping[str, int] = field(default_factory=dict)
    min_wait: int = 300
    max_wait: int = 600
    fail_alert_threshold: int = 3
    heartbeat_hour: int | None = None
    db_path: Path = Path("/opt/story-watch/data/state.db")
    classifier: str = "rules"
    # Categories that get posted at all (NOTIFY_<CATEGORY>); others are recorded silently.
    notify_categories: frozenset[Category] = frozenset(Category)
    ping_roles: bool = False  # PING_ROLES master switch
    role_ids: Mapping[Category, tuple[str, ...]] = field(default_factory=dict)  # ROLE_<CATEGORY>
    # More servers: DISCORD_WEBHOOK_<n> with ROLE_<CATEGORY>_<n>. Stories and deploys; alerts go to server 1.
    extra_destinations: tuple[Destination, ...] = ()
    # ARCHIVE_DIR: keep each new story's media and a JSON sidecar here (off when unset),
    # evicting the oldest items past ARCHIVE_MAX_MB.
    archive_dir: Path | None = None
    archive_max_mb: int = 2048

    @property
    def destinations(self) -> tuple[Destination, ...]:
        return (Destination("1", self.discord_webhook, self.role_ids),) + self.extra_destinations

    def roles_for(self, category: Category, destination: Destination | None = None) -> tuple[str, ...]:
        if not self.ping_roles:
            return ()
        role_ids = destination.role_ids if destination is not None else self.role_ids
        return role_ids.get(category, ())

    def __repr__(self) -> str:  # never leak the webhook token into logs
        return (
            f"Config(ig_user={self.ig_user!r}, targets={self.targets!r}, "
            f"target_ids={dict(self.target_ids)!r}, "
            f"min_wait={self.min_wait}, max_wait={self.max_wait}, "
            f"fail_alert_threshold={self.fail_alert_threshold}, "
            f"heartbeat_hour={self.heartbeat_hour}, db_path={str(self.db_path)!r}, "
            f"archive_dir={str(self.archive_dir) if self.archive_dir else None!r}, "
            f"archive_max_mb={self.archive_max_mb}, "
            f"classifier={self.classifier!r}, notify={sorted(c.value for c in self.notify_categories)}, "
            f"ping_roles={self.ping_roles}, destinations="
            f"{ {d.name: {c.value: r for c, r in d.role_ids.items()} for d in self.destinations} })"
        )

    __str__ = __repr__


def _int(env: Mapping[str, str], key: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from None
    if not lo <= value <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}, got {value}")
    return value


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{key} must be true or false, got {raw!r}")


def _role_ids(env: Mapping[str, str], key: str) -> tuple[str, ...]:
    ids = tuple(r.strip().lstrip("<@&").rstrip(">") for r in env.get(key, "").split(",") if r.strip())
    for rid in ids:
        if not (rid.isdigit() and 15 <= len(rid) <= 21):
            raise ConfigError(f"{key}: {rid!r} is not a Discord role ID (enable Developer Mode, right-click the role)")
    return ids


def role_ids_from_env(
    env: Mapping[str, str], prefix: str = "ROLE_", suffix: str = ""
) -> dict[Category, tuple[str, ...]]:
    """<prefix><CATEGORY><suffix> role IDs, e.g. ROLE_JOB_POSTING (service), ROLE_JOB_POSTING_2
    (server 2) or TEST_ROLE_JOB_POSTING (resend script)."""
    return {c: ids for c in Category if (ids := _role_ids(env, f"{prefix}{c.value.upper()}{suffix}"))}


_EXTRA_WEBHOOK = re.compile(r"DISCORD_WEBHOOK_(\d+)")
_EXTRA_ROLE = re.compile(r"ROLE_(?:%s)_(\d+)" % "|".join(c.value.upper() for c in Category))


def _extra_destinations(env: Mapping[str, str], primary: str) -> tuple[Destination, ...]:
    """DISCORD_WEBHOOK_<n> (n >= 2) servers, in numeric order. Blank values are ignored."""
    numbers: set[int] = set()
    for key, value in env.items():
        m = _EXTRA_WEBHOOK.fullmatch(key)
        if not m or not value.strip():
            continue
        if int(m.group(1)) < 2:
            raise ConfigError(f"{key}: numbered webhooks start at 2 (server 1 is DISCORD_WEBHOOK)")
        numbers.add(int(m.group(1)))
    for key, value in env.items():
        m = _EXTRA_ROLE.fullmatch(key)
        if m and value.strip() and int(m.group(1)) not in numbers:
            raise ConfigError(f"{key} is set but DISCORD_WEBHOOK_{m.group(1)} is not")

    out: list[Destination] = []
    seen = {webhook_id(primary)}
    for n in sorted(numbers):
        key = f"DISCORD_WEBHOOK_{n}"
        webhook = env[key].strip()
        if not webhook.startswith(WEBHOOK_PREFIXES):
            raise ConfigError(f"{key} must be a Discord webhook URL")
        dest = Destination(str(n), webhook, role_ids_from_env(env, suffix=f"_{n}"))
        if dest.key in seen:
            raise ConfigError(f"{key} is the same webhook as another server")
        seen.add(dest.key)
        out.append(dest)
    return tuple(out)


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a Config from `env` (defaults to os.environ after loading .env)."""
    if env is None:
        load_dotenv(find_dotenv(usecwd=True))
        env = os.environ

    ig_user = env.get("IG_USER", "").strip()
    if not ig_user:
        raise ConfigError("IG_USER is required (the burner account's username)")

    webhook = env.get("DISCORD_WEBHOOK", "").strip()
    if not webhook.startswith(WEBHOOK_PREFIXES):
        raise ConfigError("DISCORD_WEBHOOK must be a Discord webhook URL")

    targets: list[str] = []
    target_ids: dict[str, int] = {}
    for entry in env.get("TARGETS", "zero2sudo").split(","):
        name, _, uid = entry.partition(":")
        name = name.strip().lstrip("@").lower()
        uid = uid.strip()
        if not name:
            continue
        if uid:
            if not uid.isdigit():
                raise ConfigError(f"TARGETS: userid for {name!r} must be numeric, got {uid!r}")
            target_ids[name] = int(uid)
        targets.append(name)
    if not targets:
        raise ConfigError("TARGETS must list at least one username")

    min_wait = _int(env, "MIN_WAIT", 300, 30, 86400)
    max_wait = _int(env, "MAX_WAIT", 600, 30, 86400)
    if max_wait < min_wait:
        raise ConfigError("MAX_WAIT must be >= MIN_WAIT")

    hb_raw = env.get("HEARTBEAT_HOUR", "").strip()
    heartbeat_hour = _int(env, "HEARTBEAT_HOUR", 0, 0, 23) if hb_raw else None

    classifier = env.get("CLASSIFIER", "").strip().lower() or "rules"
    if classifier not in CLASSIFIERS:
        raise ConfigError(f"CLASSIFIER must be one of {', '.join(sorted(CLASSIFIERS))}, got {classifier!r}")
    suffix = {c: c.value.upper() for c in Category}  # JOB_POSTING, INTERVIEW_INFO, MISC
    notify = frozenset(c for c in Category if _bool(env, f"NOTIFY_{suffix[c]}", True))
    role_ids = role_ids_from_env(env)
    extra_destinations = _extra_destinations(env, webhook)
    ping_roles = _bool(env, "PING_ROLES", False)
    archive_dir = env.get("ARCHIVE_DIR", "").strip()
    if archive_dir and not os.path.isabs(archive_dir):
        raise ConfigError("ARCHIVE_DIR must be an absolute path")

    return Config(
        ig_user=ig_user,
        ig_session_path=env.get("IG_SESSION_PATH", "").strip() or None,
        targets=tuple(targets),
        discord_webhook=webhook,
        target_ids=target_ids,
        min_wait=min_wait,
        max_wait=max_wait,
        fail_alert_threshold=_int(env, "FAIL_ALERT_THRESHOLD", 3, 1, 1000),
        heartbeat_hour=heartbeat_hour,
        db_path=Path(env.get("DB_PATH", "").strip() or "/opt/story-watch/data/state.db"),
        classifier=classifier,
        notify_categories=notify,
        ping_roles=ping_roles,
        role_ids=role_ids,
        extra_destinations=extra_destinations,
        archive_dir=Path(archive_dir) if archive_dir else None,
        archive_max_mb=_int(env, "ARCHIVE_MAX_MB", 2048, 1, 10_000_000),
    )
