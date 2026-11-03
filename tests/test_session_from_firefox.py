import importlib.util
import os
import pickle
import sqlite3
import stat
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "session_from_firefox", Path(__file__).resolve().parent.parent / "scripts" / "session_from_firefox.py"
)
sff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sff)

SECRET = "ZZsecretvalue123"


def make_db(path, rows, wal=False):
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, name TEXT, value TEXT, host TEXT, expiry INTEGER)")
    con.executemany("INSERT INTO moz_cookies (name, value, host, expiry) VALUES (?,?,?,?)", rows)
    con.commit()
    return con


GOOD = [
    ("sessionid", SECRET + "a", ".instagram.com", 0),
    ("ds_user_id", "42", ".instagram.com", 0),
    ("csrftoken", "tok", "www.instagram.com", 0),
    ("other", "x", ".example.com", 0),
]


def test_host_filter_and_expiry(tmp_path):
    db = tmp_path / "cookies.sqlite"
    make_db(db, GOOD + [("old", "v", ".instagram.com", 1000), ("oldms", "v", ".instagram.com", 1000000)]).close()
    c = sff.cookies_from_db(db)
    assert set(c) == {"sessionid", "ds_user_id", "csrftoken"}


def test_later_rows_win(tmp_path):
    db = tmp_path / "cookies.sqlite"
    make_db(db, [("a", "1", ".instagram.com", 0), ("a", "2", "instagram.com", 0)]).close()
    assert sff.cookies_from_db(db) == {"a": "2"}


def test_missing_sessionid_exits_2(tmp_path, capsys):
    db = tmp_path / "cookies.sqlite"
    make_db(db, [("ds_user_id", "42", ".instagram.com", 0)]).close()
    out = tmp_path / "out"
    assert sff.main([str(db), str(out)]) == 2
    assert "log in on instagram.com first" in capsys.readouterr().err
    assert not out.exists()


def test_output_pickle_mode_prev_and_no_values(tmp_path, capsys):
    db = tmp_path / "cookies.sqlite"
    make_db(db, GOOD).close()
    out = tmp_path / "sub" / "session-x"
    assert sff.main([str(db), str(out)]) == 0
    assert pickle.loads(out.read_bytes()) == {"sessionid": SECRET + "a", "ds_user_id": "42", "csrftoken": "tok"}
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    first = capsys.readouterr()
    assert SECRET not in first.out + first.err

    assert sff.main([str(db), str(out)]) == 0
    assert "sessionid: same" in capsys.readouterr().out

    con = sqlite3.connect(db)
    con.execute("UPDATE moz_cookies SET value=? WHERE name='sessionid'", (SECRET + "b",))
    con.commit()
    con.close()
    assert sff.main([str(db), str(out)]) == 0
    cap = capsys.readouterr()
    assert "sessionid: changed" in cap.out
    assert SECRET not in cap.out + cap.err
    prev = Path(str(out) + ".prev")
    assert pickle.loads(prev.read_bytes())["sessionid"] == SECRET + "a"
    assert pickle.loads(out.read_bytes())["sessionid"] == SECRET + "b"
    assert stat.S_IMODE(os.stat(prev).st_mode) == 0o600
    assert not [p for p in out.parent.iterdir() if p.name.endswith(".tmp")]


def test_wal_contents_are_read(tmp_path):
    db = tmp_path / "cookies.sqlite"
    con = make_db(db, [("ds_user_id", "42", ".instagram.com", 0)], wal=True)
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("INSERT INTO moz_cookies (name, value, host, expiry) VALUES ('sessionid','walval','.instagram.com',0)")
    con.commit()
    try:
        assert (tmp_path / "cookies.sqlite-wal").exists()
        assert sff.cookies_from_db(db)["sessionid"] == "walval"
    finally:
        con.close()


def test_changed_mode(tmp_path, capsys):
    db = tmp_path / "cookies.sqlite"
    make_db(db, GOOD).close()
    out = tmp_path / "session-x"
    assert sff.main(["--changed", str(db), str(out)]) == 0  # no session file yet
    assert sff.main([str(db), str(out)]) == 0
    capsys.readouterr()
    before = out.read_bytes()
    assert sff.main(["--changed", str(db), str(out)]) == 1  # same sessionid
    assert capsys.readouterr().out == ""
    assert out.read_bytes() == before
    out.write_bytes(pickle.dumps({"sessionid": "other"}))
    assert sff.main(["--changed", str(db), str(out)]) == 0
    out.write_bytes(b"garbage")
    assert sff.main(["--changed", str(db), str(out)]) == 0
    assert SECRET not in capsys.readouterr().out
    assert not (tmp_path / "session-x.prev").exists()


def test_changed_mode_no_login_or_unreadable(tmp_path):
    db = tmp_path / "cookies.sqlite"
    make_db(db, [("csrftoken", "t", ".instagram.com", 0)]).close()
    assert sff.main(["--changed", str(db), str(tmp_path / "o")]) == 2
    assert sff.main(["--changed", str(tmp_path / "missing.sqlite"), str(tmp_path / "o")]) == 2
    assert sff.main(["--changed", str(db)]) == 2
