# Prototypes for issue #14 (throwaway)

Findings are in [`docs/research/ping-config-ui.md`](../../docs/research/ping-config-ui.md).
Nothing here is imported by the service, and these dependencies stay out of `pyproject.toml`.

| File | What it is | Was it run? |
|---|---|---|
| `sqlite_two_processes.py` | Two processes (a fake watcher and a fake `/pings` handler) share one SQLite file. It compares the rollback journal with and without a busy timeout, and WAL. Stdlib only, no network. | Yes, offline: `.venv/bin/python research/ping-config-ui/sqlite_two_processes.py` |
| `bot_sketch.py` | discord.py gateway bot with `/pings edit`, `/pings show` and `/pings clear`. `edit` is an ephemeral message with two multi-selects. | **No.** There were no Discord credentials or test server, and discord.py isn't installed. It was only checked with `python -m py_compile`. |
| `requirements.txt` | discord.py, for the sketch only. | |

To try the sketch later against a **test** server, use a separate test application and its token, never production webhooks or roles:

    python -m venv /tmp/ping-sketch && /tmp/ping-sketch/bin/pip install -r research/ping-config-ui/requirements.txt
    DISCORD_BOT_TOKEN=<test bot token> /tmp/ping-sketch/bin/python research/ping-config-ui/bot_sketch.py \
        --db /tmp/ping-sketch/prefs.db --sync-guild <test server id>
