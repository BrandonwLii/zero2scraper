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
