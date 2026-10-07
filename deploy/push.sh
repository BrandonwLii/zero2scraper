#!/usr/bin/env bash
# Deploy from your workstation: stream this repo into the LXC via the Proxmox
# host (over Tailscale) and run install.sh there. The LXC itself needs no
# Tailscale or SSH.
#
#   PVE_HOST=root@proxmox CTID=120 ./deploy/push.sh
set -euo pipefail

PVE_HOST="${PVE_HOST:?set PVE_HOST, e.g. root@proxmox (your Tailscale name)}"
CTID="${CTID:-120}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

tar czf - --exclude .git --exclude .venv --exclude .env --exclude __pycache__ \
    --exclude '*.egg-info' --exclude .pytest_cache . \
  | ssh "$PVE_HOST" "pct exec $CTID -- bash -c '
        rm -rf /root/story-watch-src && mkdir -p /root/story-watch-src &&
        tar xzf - -C /root/story-watch-src &&
        bash /root/story-watch-src/deploy/install.sh'"
