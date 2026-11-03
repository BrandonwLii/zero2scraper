#!/usr/bin/env bash
# Provision CT 121 (the login browser). Run inside the CT as root; idempotent.
# deploy/host/install.sh pushes and runs it. Keep the browser a stock Firefox: no spoofing,
# no automation, no fingerprint settings.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
TZNAME=America/Toronto
HOME_DIR=/home/browser

echo "==> packages"
apt-get install -y --no-install-recommends \
    firefox-esr tigervnc-standalone-server tigervnc-tools novnc websockify openbox \
    locales tzdata fonts-dejavu-core fonts-liberation2 fonts-noto-core >/dev/null

echo "==> timezone and locale"
ln -sf "/usr/share/zoneinfo/$TZNAME" /etc/localtime
echo "$TZNAME" > /etc/timezone
if ! locale -a | grep -qi '^en_US\.utf-\?8$'; then
    sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
    locale-gen >/dev/null
fi
echo 'LANG=en_US.UTF-8' > /etc/default/locale

echo "==> user and VNC password"
id browser >/dev/null 2>&1 || useradd --create-home --shell /usr/sbin/nologin browser
install -d -o browser -g browser -m 0700 "$HOME_DIR/.vnc" "$HOME_DIR/profile"
if [[ ! -s "$HOME_DIR/.vnc/passwd" ]]; then
    pw="$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 12 || true)"
    (umask 077; printf '%s\n' "$pw" > /root/vnc-password)
    printf '%s\n' "$pw" | vncpasswd -f > "$HOME_DIR/.vnc/passwd"
    chown browser:browser "$HOME_DIR/.vnc/passwd"
    chmod 0600 "$HOME_DIR/.vnc/passwd"
    echo "    VNC password saved in /root/vnc-password (read it once, then it stays there)"
fi

echo "==> Firefox prefs"
cat > "$HOME_DIR/profile/user.js" <<'JS'
user_pref("toolkit.telemetry.enabled", false);
user_pref("toolkit.telemetry.unified", false);
user_pref("datareporting.healthreport.uploadEnabled", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.startup.page", 0);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.tabs.warnOnClose", false);
user_pref("intl.accept_languages", "en-US, en");
JS
chown browser:browser "$HOME_DIR/profile/user.js"

echo "==> openbox: Firefox maximized and undecorated"
# noVNC's resize=remote shrinks the screen to the phone's size; a normal window would end up
# off-screen (a black view). A maximized window follows every screen resize.
install -d -o browser -g browser "$HOME_DIR/.config" "$HOME_DIR/.config/openbox"
sed 's|<applications>|<applications>\n  <application class="firefox-esr"><decor>no</decor><maximized>yes</maximized><focus>yes</focus></application>\n  <application class="Firefox-esr"><decor>no</decor><maximized>yes</maximized><focus>yes</focus></application>|' \
    /etc/xdg/openbox/rc.xml > "$HOME_DIR/.config/openbox/rc.xml"
chown browser:browser "$HOME_DIR/.config/openbox/rc.xml"

echo "==> ig-browser service"
cat > /usr/local/bin/ig-browser-run <<'SH'
#!/bin/bash
# On stop, Firefox gets SIGTERM first and the display stays up until it has flushed its cookies.
set -u
export DISPLAY=:1
Xtigervnc :1 -localhost yes -rfbauth /home/browser/.vnc/passwd -geometry 1920x1080 \
    -SecurityTypes VncAuth -AlwaysShared &
X=$!
for _ in $(seq 1 50); do [ -S /tmp/.X11-unix/X1 ] && break; sleep 0.2; done
openbox &
O=$!
websockify --web /usr/share/novnc 6080 localhost:5901 &
W=$!
firefox-esr --profile /home/browser/profile https://www.instagram.com/accounts/login/ &
F=$!
trap 'kill -TERM $F 2>/dev/null; wait $F 2>/dev/null; kill $W $O $X 2>/dev/null; exit 0' TERM INT
wait -n
kill $F $W $O $X 2>/dev/null
exit 1
SH
chmod 0755 /usr/local/bin/ig-browser-run
cat > /etc/systemd/system/ig-browser.service <<'UNIT'
[Unit]
Description=Login browser for the Instagram burner (VNC + noVNC + Firefox)

[Service]
User=browser
Environment=HOME=/home/browser
Environment=TZ=America/Toronto
Environment=LANG=en_US.UTF-8
ExecStart=/usr/local/bin/ig-browser-run
KillMode=mixed
# stop must give Firefox time to flush its cookies on exit
TimeoutStopSec=30
Restart=no
UNIT
systemctl daemon-reload
systemctl disable ig-browser >/dev/null 2>&1 || true  # started on demand by the host only
echo "==> done"
