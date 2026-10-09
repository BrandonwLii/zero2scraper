"""Entry point of the Discord bot (`story-watch-bot`). Labeling is its first feature; later
features (e.g. /pings commands) register on the same client.

Exit codes: 0 on a clean stop, 2 when the configuration or token is wrong. The systemd unit
doesn't restart on 2, so a misconfigured bot can't crash-loop (Discord resets a token after
1000 gateway IDENTIFYs in 24 hours).
"""

from __future__ import annotations

import argparse
import logging
import sys

import discord

from .config import BotConfigError, load_bot_config

log = logging.getLogger("story_watch.bot")


class RedactToken(logging.Filter):
    """Belt and braces: whatever a library logs, the token never reaches the journal."""

    def __init__(self, token: str):
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if self._token and self._token in record.getMessage():
            record.msg = record.getMessage().replace(self._token, "[token]")
            record.args = None
        return True


def _setup_logging(token: str = "") -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.WARNING)
    if token:
        for handler in logging.getLogger().handlers:
            handler.addFilter(RedactToken(token))


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="story-watch-bot")
    parser.add_argument("--check-config", action="store_true", help="validate the environment and exit")
    args = parser.parse_args(argv)

    _setup_logging()
    try:
        cfg = load_bot_config()
    except BotConfigError as e:
        log.error("config error: %s", e)  # messages never contain the token
        return 2
    _setup_logging(cfg.token)
    if args.check_config:
        log.info("config ok: %s", cfg)
        return 0

    from .discord_app import LabelBot

    bot = LabelBot(cfg)
    try:
        # log_handler=None: our logging setup only, and discord.py's own logs stay out of it.
        bot.run(cfg.token, log_handler=None)
    except discord.LoginFailure:
        log.error("Discord rejected the bot token; fix DISCORD_BOT_TOKEN")
        return 2
    except discord.PrivilegedIntentsRequired:
        log.error("Discord says a privileged intent is required, which this bot doesn't use")
        return 2
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.error("bot stopped (%s)", type(e).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli())
