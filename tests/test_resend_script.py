import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "resend_story.py"


@pytest.fixture(scope="module")
def resend():
    spec = importlib.util.spec_from_file_location("resend_story", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_ping_user_ids(resend):
    assert resend.parse_ping_user_ids("") == []
    assert resend.parse_ping_user_ids("111111111111111111, <@222222222222222222>") == [
        "111111111111111111", "222222222222222222"]
    for bad in ("everyone", "123", "<@&111111111111111111>"):
        with pytest.raises(SystemExit):
            resend.parse_ping_user_ids(bad)


def test_script_never_reads_real_preferences_or_old_role_settings():
    src = SCRIPT.read_text()
    for word in ("Store", "all_ping_prefs", "state.db", "DB_PATH", "TEST_ROLE", "role_ids"):
        assert word not in src.replace("TEST_ROLE_*", "")


def test_dry_run_with_the_claude_classifier(resend, tmp_path, monkeypatch, capsys):
    """Rollout step 2: CLASSIFIER=claude in --dry-run, with claude -p and the still mocked."""
    import json
    from dataclasses import asdict
    from unittest import mock

    from conftest import make_item
    from test_claude import TOKEN, stream

    from story_watch import claude, hybrid

    item = dict(asdict(make_item("1")), taken_at=make_item("1").taken_at.isoformat())
    (tmp_path / "alice.json").write_text(json.dumps({"fetched_at": "2026-10-10T00:00:00+00:00", "items": [item], "page": {}}))
    monkeypatch.chdir(tmp_path)  # no .env from the checkout
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for k, v in {"TARGETS": "alice", "CLASSIFIER": "claude", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN}.items():
        monkeypatch.setenv(k, v)
    calls = []

    def run(self, message, timeout):
        calls.append((self.home, self.env()["CLAUDE_CODE_OAUTH_TOKEN"]))
        return stream()

    monkeypatch.setattr(claude.ClaudeCli, "run", run)
    monkeypatch.setattr(hybrid.CdnImages, "__call__", lambda self, it: b"\xff\xd8\xff fake")
    monkeypatch.setattr("sys.argv", ["resend_story.py", "-n", "1", "--dry-run", "--no-ping", "--cache-dir", str(tmp_path)])
    with mock.patch.object(resend, "JobTitles"):
        assert resend.main() == 0
    out = capsys.readouterr().out
    assert calls == [(tmp_path / ".cache" / "story-watch" / "claude-home", TOKEN)]
    assert "Job posting" in out and "fallback" not in out and TOKEN not in out
