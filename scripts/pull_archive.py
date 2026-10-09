#!/usr/bin/env python3
"""Pull the story archive from the LXC to this workstation.

Tars /opt/story-watch/archive inside the CT over the same ssh + `pct exec` path that
deploy/push.sh uses, and unpacks it locally. Re-running just refreshes the copy; files the
server has since pruned stay here.

  PVE_HOST=root@<proxmox> CTID=120 .venv/bin/python scripts/pull_archive.py

  PVE_HOST      required, e.g. root@proxmox (your Tailscale name)
  CTID          container id (default 120)
  ARCHIVE_PULL_DIR   destination (default ~/story-watch-data/archive)
  ARCHIVE_REMOTE_DIR archive path inside the CT (default /opt/story-watch/archive)

The archive holds signed CDN URLs and story media. Keep it outside the repo.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

DEFAULT_DEST = "~/story-watch-data/archive"
DEFAULT_REMOTE = "/opt/story-watch/archive"


def pull(env: dict[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    host = env.get("PVE_HOST", "").strip()
    if not host:
        print("set PVE_HOST, e.g. PVE_HOST=root@proxmox", file=sys.stderr)
        return 2
    ctid = env.get("CTID", "120").strip()
    if not ctid.isdigit():
        print("CTID must be a number", file=sys.stderr)
        return 2
    remote_dir = env.get("ARCHIVE_REMOTE_DIR", "").strip() or DEFAULT_REMOTE
    dest = Path(env.get("ARCHIVE_PULL_DIR", "").strip() or DEFAULT_DEST).expanduser()
    dest.mkdir(parents=True, exist_ok=True)

    remote = f"pct exec {ctid} -- tar czf - -C {shlex.quote(remote_dir)} ."
    ssh = subprocess.Popen(["ssh", "--", host, remote], stdout=subprocess.PIPE)
    untar = subprocess.Popen(
        ["tar", "xzf", "-", "-C", str(dest), "--no-same-owner", "--no-same-permissions"], stdin=ssh.stdout
    )
    ssh.stdout.close()  # so ssh gets SIGPIPE if tar exits early
    untar_rc = untar.wait()
    ssh_rc = ssh.wait()
    if ssh_rc or untar_rc:
        print(f"pull failed (ssh exit {ssh_rc}, tar exit {untar_rc})", file=sys.stderr)
        return 1
    files = sum(1 for f in dest.rglob("*") if f.is_file())
    print(f"archive pulled to {dest} ({files} file(s))")
    return 0


if __name__ == "__main__":
    sys.exit(pull())
