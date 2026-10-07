# story-watch

This repo is developed in WSL and deployed to an unprivileged Debian 12 LXC (CT 120) on a Proxmox host that is reachable over Tailscale. Claude Code runs only on the workstation; it is not installed on the server.

- Tests: `.venv/bin/pytest`. Tests must never touch the network; mock instaloader and HTTP.
- Deploy: `PVE_HOST=root@<proxmox> CTID=120 ./deploy/push.sh`. It runs `deploy/install.sh` inside the CT, and that script must stay idempotent.
- Remote logs: `ssh root@<proxmox> pct exec 120 -- journalctl -u story-watch -n 100 --no-pager`
- Target is Python 3.11 (Debian 12). Don't use newer syntax or stdlib features.
- Never log or put the Discord webhook URL or the session contents in exception messages. requests exceptions include the URL, so log only their type.
- An item is marked seen only after Discord returns 2xx.
- Instagram errors back off exponentially (cap 3600 s). Session or checkpoint errors send one alert and do not crash the service.
