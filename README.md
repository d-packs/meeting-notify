# meeting-notify

Reliable desktop notifications for your Google Calendar meetings on Linux —
without a full PIM suite, and without depending on a browser tab staying open.

Today you either go heavy — Evolution, KDE/Akonadi or Thunderbird, which sync
your *whole* account — or you rely on Google's own notifications, which fire
only while a tab or the phone app is awake, so you miss meetings. The light
alternative is usually a personal `gcalcli` + cron + `notify-send` script that
nobody packages, partly because a shareable Google app needs per-user OAuth.
This is that middle ground, packaged.

A tiny `systemd --user` timer checks every minute and fires a native
notification (with sound) before each meeting, using the reminders you set per
event. It actually calls Google only every few minutes and caches in between,
so it's light and keeps reminding you even when the network is down. No
always-on daemon, no database, no stored password — just an OAuth token you can
revoke anytime. Runs on any freedesktop notification daemon (KDE, GNOME, XFCE,
dunst, …).

> Not affiliated with or endorsed by Google. Google Calendar and Google Meet
> are trademarks of Google LLC; used here only to describe compatibility.

## Requirements

- A Linux desktop with a notification daemon (`notify-send` works) and
  `systemd --user`.
- A Google account (personal or Workspace).
- Installed for you by `install.sh`: `pipx` + `gcalcli` (Google API libs),
  `libnotify`, `python-gobject`, `xdg-utils`, `paplay`. Sounds are bundled, so
  no system sound theme is required.

## Install

```sh
git clone https://github.com/d-packs/meeting-notify.git
cd meeting-notify
./install.sh
```

The installer pulls dependencies (pacman/apt/dnf), installs to
`~/.local/share/meeting-notify/`, writes `systemd --user` units (using `%h`, so
they're username-agnostic), enables the timer, and walks you through a one-time
Google sign-in. It's idempotent — safe to re-run. You can delete the clone
afterward; the install is self-contained.

**Arch Linux:** a `-git` PKGBUILD is in [`packaging/aur/`](packaging/aur/) — it
depends on the system Python packages (no pipx), installs to `/usr/share` with
`systemd --user` units, and prints the one-time setup steps after install.

## One-time Google authorization

A redistributable Calendar tool can't ship a shared sign-in, so you create your
own free OAuth credential (≈3 min). The installer prompts for it:

1. <https://console.cloud.google.com/> → create a project.
2. Enable the **Google Calendar API** for that project.
3. **OAuth consent screen** → User type **Internal** (if you have Workspace) or
   **External** (then add your email under *Test users*).
   *Internal avoids the 7-day token expiry — pick it if you can.*
4. **Credentials → Create credentials → OAuth client ID → Desktop app.**
   Copy the **Client ID** and **Client secret**, paste them when asked.

Your browser opens to sign in → **Allow**. The token (read-only Calendar scope)
is stored at `~/.config/meeting-notify/token.json` and refreshes itself. If it
ever breaks you get a sticky **Re-authorize** popup and a one-click fix; the
check also re-runs at every login until it's fixed.

## How reminders work

- Only meetings with a **notification attached in Google Calendar** alert. The
  calendar's *default* reminder is ignored, and meetings on shared/secondary
  calendars only alert if you add a notification to that specific event.
- Every alerting meeting also gets an automatic **2-minute** heads-up; any
  reminder set closer than 2 min is pulled to 2 min.
- **Early alerts** (e.g. 10 min, 1 h before): calm — normal urgency, soft sound,
  a *Close* button.
- **The 2-minute alert**: urgent — critical/sticky, alarm sound, a live
  `Meeting starts in: M:SS` countdown, **Join** + *Close* buttons, auto-closes
  at meeting start.

## Desktop support

Built on freedesktop standards (`org.freedesktop.Notifications`, `notify-send`,
`paplay`, `xdg-open`, `systemd --user`) — no KDE-specific APIs.

| Capability | KDE | GNOME | XFCE |
|---|---|---|---|
| Timed notifications + sound | ✅ | ✅ | ✅ (needs `xfce4-notifyd`) |
| Live `M:SS` countdown (in-place update) | ✅ | ✅ | ✅ |
| Action buttons (Join / Close / Re-authorize) | ✅ | ⚠️ shown but de-emphasized | ✅ |
| Critical/sticky + explicit close | ✅ | ✅ | ✅ |
| Join → opens default browser | ✅ | ✅ | ✅ |

Where a daemon doesn't show action buttons (e.g. GNOME Shell de-emphasizes
them), the helper detects this via `GetCapabilities` and puts the meeting link /
re-auth hint **in the notification body** instead, so nothing is lost.

## Customizing

All settings live in **`~/.config/meeting-notify/config.ini`** (the installer
seeds it from [`config.example.ini`](config.example.ini)). Every key is
optional — unset keys use the built-in default. No code editing, and it survives
re-installs. Available keys:

| `[meeting-notify]` | Default | Meaning |
|---|---|---|
| `imminent_minutes` | `2` | Imminent lead — drives the countdown length, when the urgent alert fires, the fade-at-start, and the floor for closer reminders |
| `poll_interval_minutes` | `5` | How often to actually query Google's API. The timer still checks every minute against a local cache, so reminders stay minute-accurate; this only controls network/API frequency. A failed fetch reuses the last cache (works offline). |
| `window_hours` | `26` | Calendar look-ahead (must exceed your longest reminder) |
| `skip_declined` | `true` | Skip meetings you've declined |
| `auth_alert_throttle_seconds` | `14400` | In-session re-nag interval for a broken sign-in |
| `app_name` | `Meeting Reminder` | Popup header |
| `icon` | `appointment-soon` | Themed icon name |
| `meet_opener` | `xdg-open` | Command for the **Join** action; set to a Meet PWA command to open the app instead of a browser |

| `[sounds]` | Default | Meaning |
|---|---|---|
| `heads_up` | *(bundled)* | Absolute path to your own early-alert sound (`.ogg`/`.wav`) |
| `imminent` | *(bundled)* | Absolute path to your own imminent-alert sound |

Example — use a Meet PWA and your own imminent sound:

```ini
[meeting-notify]
meet_opener = /usr/bin/chromium --profile-directory=Default --app-id=<meet-app-id>

[sounds]
imminent = /home/you/sounds/my-alert.ogg
```

## Manage

```sh
systemctl --user list-timers meeting-notify.timer    # next run
journalctl --user -u meeting-notify.service -f        # logs
systemctl --user disable --now meeting-notify.timer   # pause
```

## Uninstall

```sh
systemctl --user disable --now meeting-notify.timer
systemctl --user disable meeting-notify-authcheck.service
rm -f ~/.config/systemd/user/meeting-notify*.{service,timer}
systemctl --user daemon-reload
rm -rf ~/.local/share/meeting-notify ~/.config/meeting-notify
pipx uninstall gcalcli      # optional
```

Revoke the token anytime at <https://myaccount.google.com/permissions>.

## License

This software is licensed under the **GNU General Public License v3.0** — see
[`LICENSE`](LICENSE). You may use, modify, and redistribute it freely; derivative
and redistributed work must remain under the GPL, keep this license, and retain
the copyright/author notice (original author: Ionut R.,
<https://github.com/d-packs/meeting-notify>).

The bundled alert sounds in [`sounds/`](sounds/) are from the **KDE Oxygen**
sound theme, © the KDE community, **LGPL-2.0-or-later** — see
[`sounds/CREDITS.md`](sounds/CREDITS.md) and
[`sounds/LICENSE.LGPL-2.0`](sounds/LICENSE.LGPL-2.0). They're optional; replace
them with your own and the rest of the project is unaffected.
