#!/usr/bin/env bash
# meeting-notify — Google Calendar desktop meeting notifier
# Copyright (C) 2026 Ionut R.  —  https://github.com/d-packs/meeting-notify
# Licensed under the GNU General Public License v3.0 (see LICENSE); NO WARRANTY.

# meeting-notify installer — run it from a clone of the repo:
#   git clone https://github.com/d-packs/meeting-notify.git
#   cd meeting-notify && ./install.sh
#
# Sets up a Google Calendar -> desktop notifier (systemd --user timer) on
# any Linux desktop with a notification daemon (KDE, GNOME, XFCE, …).
# Idempotent; safe to re-run.

set -uo pipefail

SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
APPDIR="$HOME/.local/share/meeting-notify"
CONFIG_DIR="$HOME/.config/meeting-notify"
UNIT_DIR="$HOME/.config/systemd/user"
VENV_PY="$HOME/.local/share/pipx/venvs/gcalcli/bin/python"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*"; exit 1; }

[ -f "$SRC/meeting-notify.py" ] || die "Run this from inside the cloned repo."

# --- 1. system packages ------------------------------------------------------
# notify-send (libnotify), gi (python-gobject), xdg-open (xdg-utils),
# paplay (pulseaudio/pipewire), pipx (to install gcalcli). Sounds are bundled,
# so no system sound theme is required.
say "Installing system dependencies"
if   command -v pacman >/dev/null 2>&1; then
  sudo pacman -S --needed --noconfirm python-pipx libnotify python-gobject xdg-utils libpulse \
    || warn "pacman step had issues; check above."
elif command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -qq && sudo apt-get install -y pipx libnotify-bin python3-gi xdg-utils pulseaudio-utils \
    || warn "apt step had issues; check above."
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y pipx libnotify python3-gobject xdg-utils pulseaudio-utils \
    || warn "dnf step had issues; check above."
else
  warn "Unknown distro. Install by hand: pipx, libnotify (notify-send), python-gobject (gi), xdg-utils, paplay."
fi

# --- 2. gcalcli via pipx (provides the Google API libs in an isolated venv) --
say "Installing gcalcli (Google Calendar libraries) via pipx"
PIPX="pipx"; command -v pipx >/dev/null 2>&1 || PIPX="python3 -m pipx"
$PIPX install gcalcli >/dev/null 2>&1 || $PIPX upgrade gcalcli >/dev/null 2>&1 || true
$PIPX ensurepath >/dev/null 2>&1 || true
[ -x "$VENV_PY" ] || die "gcalcli venv not found at $VENV_PY (pipx install failed?)."

# --- 3. lay down app files ---------------------------------------------------
say "Installing files to $APPDIR"
mkdir -p "$APPDIR/sounds" "$CONFIG_DIR" "$UNIT_DIR"
install -m644 "$SRC/sounds/"*.ogg "$APPDIR/sounds/"
install -m755 "$SRC/meeting-notify.py" "$SRC/meeting-notify-popup.py" \
              "$SRC/meeting-notify-reauth.py" "$APPDIR/"
# meeting-notify.py + reauth run on the gcalcli venv; popup needs system python (gi).
sed -i "1s|^#!.*|#!$VENV_PY|" "$APPDIR/meeting-notify.py" "$APPDIR/meeting-notify-reauth.py"
sed -i "1s|^#!.*|#!/usr/bin/python3|" "$APPDIR/meeting-notify-popup.py"
# Drop a config template (never clobber an existing one).
if [ -f "$CONFIG_DIR/config.ini" ]; then
  echo "Keeping existing $CONFIG_DIR/config.ini"
else
  install -m644 "$SRC/config.example.ini" "$CONFIG_DIR/config.ini"
  echo "Wrote default config to $CONFIG_DIR/config.ini"
fi

# --- 4. systemd --user units (%h + explicit interpreter = username-agnostic) -
say "Writing systemd --user units"
PY='%h/.local/share/pipx/venvs/gcalcli/bin/python'
APP='%h/.local/share/meeting-notify/meeting-notify.py'
cat > "$UNIT_DIR/meeting-notify.service" <<EOF
[Unit]
Description=Notify of upcoming Google Calendar meetings

[Service]
Type=oneshot
ExecStart=$PY $APP
EOF
cat > "$UNIT_DIR/meeting-notify.timer" <<EOF
[Unit]
Description=Check Google Calendar for upcoming meetings every minute

[Timer]
OnCalendar=*:*:00
Persistent=true
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF
cat > "$UNIT_DIR/meeting-notify-authcheck.service" <<EOF
[Unit]
Description=Re-check Google Calendar sign-in at login (re-nag if broken)
After=graphical-session.target

[Service]
Type=oneshot
ExecStart=$PY $APP --auth-check

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now meeting-notify.timer
systemctl --user enable meeting-notify-authcheck.service

# --- 5. Google authorization -------------------------------------------------
say "Google sign-in"
if [ -f "$CONFIG_DIR/token.json" ]; then
  echo "Existing token found — sign-in already set up."
else
  if [ ! -f "$CONFIG_DIR/client_secret.json" ]; then
    cat <<'MSG'
You need a free Google OAuth credential (one-time, ~3 min):
  1. https://console.cloud.google.com/  -> create a project.
  2. Enable the "Google Calendar API" for it.
  3. OAuth consent screen: User type "Internal" if you have Workspace,
     else "External" and add yourself as a Test user.
  4. Credentials -> Create credentials -> OAuth client ID
     -> Application type: "Desktop app". Copy the Client ID and Client secret.

MSG
    read -rp "Paste Client ID (or leave blank to skip and do it later): " CID
    if [ -n "$CID" ]; then
      read -rp "Paste Client secret: " CSEC
      cat > "$CONFIG_DIR/client_secret.json" <<EOF
{"installed":{"client_id":"$CID","client_secret":"$CSEC","auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token"}}
EOF
      chmod 600 "$CONFIG_DIR/client_secret.json"
    fi
  fi
  if [ -f "$CONFIG_DIR/client_secret.json" ]; then
    echo "Opening the browser to sign in…"
    "$APPDIR/meeting-notify-reauth.py" \
      || warn "Sign-in didn't finish; run it later: $APPDIR/meeting-notify-reauth.py"
  else
    warn "Skipped sign-in. When ready, put your client_secret.json in $CONFIG_DIR/ and run:"
    warn "  $APPDIR/meeting-notify-reauth.py"
  fi
fi

say "Done."
echo "Reminders come from each event's own notifications in Google Calendar."
echo "Test:     $APPDIR/meeting-notify.py"
echo "Schedule: systemctl --user list-timers meeting-notify.timer"
echo "Logs:     journalctl --user -u meeting-notify.service -f"
