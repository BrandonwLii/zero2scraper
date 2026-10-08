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

# Ship the local .env as the server's config, minus the TEST_* settings that
# only scripts/resend_story.py reads. install.sh installs and then deletes it.
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
env_files=()
if [[ -f .env ]]; then
    (umask 077; grep -v '^TEST_' .env > "$tmp/.env.deploy" || true)
    env_files=(-C "$tmp" .env.deploy)
fi

# install.sh posts this to Discord once the service is back up.
commit="$(git rev-parse --short HEAD)"
[[ -z "$(git status --porcelain)" ]] || commit+="-dirty"

tar czf - --exclude .git --exclude .venv --exclude .env --exclude __pycache__ \
    --exclude '*.egg-info' --exclude .pytest_cache --exclude story-cache \
    --exclude 'story-dump*' --exclude '*.har' --exclude 'session-*' --exclude data --exclude '*.db' \
    . "${env_files[@]}" \
  | ssh "$PVE_HOST" "pct exec $CTID -- bash -c '
        rm -rf /root/story-watch-src && mkdir -p /root/story-watch-src &&
        tar xzf - -C /root/story-watch-src &&
        DEPLOY_COMMIT=$commit bash /root/story-watch-src/deploy/install.sh'"
