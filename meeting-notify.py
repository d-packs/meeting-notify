#!/usr/bin/env python3
# meeting-notify — Google Calendar desktop meeting notifier
# Copyright (C) 2026 Ionut R.  —  https://github.com/d-packs/meeting-notify
# Licensed under the GNU General Public License v3.0 (see LICENSE); NO WARRANTY.

"""Desktop notifier for upcoming Google Calendar meetings.

Fires a desktop notification (with sound) before meetings, based ONLY on
the notifications you explicitly add to an event in Google Calendar. The
calendar's inherited default reminder is ignored, so meetings you never
customized stay silent — on every calendar, personal or shared alike.

Rules:
  * No notification added to a meeting -> no alert.
  * A meeting with >=1 notification always gets an AUTO_LEAD_MINUTES (2 min)
    heads-up. That lead is also the floor: a reminder set closer than it
    (e.g. "at start" = 0 min) is pulled up to 2 min, so 0-, 1- and 2-min
    notifications all fire exactly 2 minutes before the meeting.
  * No inherited/default reminder is ever applied.

Run once per minute by a systemd --user timer; it polls, notifies, exits.
State (already-fired reminders) lives in STATE_FILE so you are not spammed.
"""

import configparser
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_DIR = Path.home() / ".config" / "meeting-notify"
TOKEN_FILE = CONFIG_DIR / "token.json"
STATE_FILE = CONFIG_DIR / "state.json"
CONFIG_FILE = CONFIG_DIR / "config.ini"
# Bundle dir — sounds and sibling scripts resolve relative to this file, so the
# whole install is relocatable.
HERE = Path(__file__).resolve().parent

# --- User configuration ------------------------------------------------------
# All settings live in ~/.config/meeting-notify/config.ini (see
# config.example.ini for the documented template). Every key is optional;
# anything unset uses the built-in default below.
_cfg = configparser.ConfigParser()
try:
    _cfg.read(CONFIG_FILE)
except configparser.Error:
    pass  # malformed config -> ignore and use defaults


def _cget(section, key, default):
    return _cfg.get(section, key, fallback=default)


# The "imminent" lead, in minutes: the urgent alert fires this far before a
# meeting, the live countdown runs from here down to 0, and the popup fades at
# meeting start. Also the floor — reminders set closer than this are pulled up.
AUTO_LEAD_MINUTES = _cfg.getint("meeting-notify", "imminent_minutes", fallback=2)

# How far ahead to scan the calendar (must exceed your longest reminder).
WINDOW_HOURS = _cfg.getint("meeting-notify", "window_hours", fallback=26)

# How often to actually query Google's API, in minutes. The timer still runs
# every minute (so reminders/countdowns stay minute-accurate); between fetches
# each run evaluates a local cache. A failed fetch reuses the last cache.
POLL_INTERVAL_SEC = _cfg.getint("meeting-notify", "poll_interval_minutes", fallback=5) * 60

# Skip meetings you've explicitly declined.
SKIP_DECLINED = _cfg.getboolean("meeting-notify", "skip_declined", fallback=True)

# Re-nag about broken sign-in at most this often, in seconds (startup always
# fires regardless, so each login re-alerts until fixed).
AUTH_ALERT_THROTTLE_SEC = _cfg.getint(
    "meeting-notify", "auth_alert_throttle_seconds", fallback=4 * 3600)

# Notification appearance.
APP_NAME = _cget("meeting-notify", "app_name", "Meeting Reminder")
ICON = _cget("meeting-notify", "icon", "appointment-soon")

# How to open a meeting link (handed to the popup's Join action).
MEET_OPENER = _cget("meeting-notify", "meet_opener", "xdg-open")

# Alert sounds: a user-supplied absolute path wins, else the bundled file.
SOUND_SOON = _cget("sounds", "heads_up", "").strip() or str(HERE / "sounds" / "heads-up.ogg")
SOUND_NOW = _cget("sounds", "imminent", "").strip() or str(HERE / "sounds" / "imminent.ogg")

# Helper that renders the interactive popup. Launched detached via systemd-run
# so its wait-for-click never stalls the poller.
POPUP_HELPER = str(HERE / "meeting-notify-popup.py")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def human(seconds):
    """Human-friendly remaining time, e.g. '1 h 5 min', '4 min', 'now'."""
    m = round(seconds / 60)
    if m <= 0:
        return "now"
    if m < 60:
        return f"{m} min"
    h, mm = divmod(m, 60)
    return f"{h} h {mm} min" if mm else f"{h} h"


def get_service():
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(creds.to_json())
        os.chmod(TOKEN_FILE, 0o600)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def popup_minutes(event):
    """Lead times (minutes before) to alert for this event.

    Only popup reminders you explicitly added to the event count; the
    calendar's inherited default reminder is ignored. A meeting with no such
    reminder returns [] and gets no alert. One that has any always also gets
    the AUTO_LEAD_MINUTES heads-up, and every reminder is floored to that lead
    (so a 0-min "at start" reminder fires AUTO_LEAD_MINUTES before instead).
    """
    overrides = event.get("reminders", {}).get("overrides") or []
    mins = [o["minutes"] for o in overrides if o.get("method") == "popup"]
    if mins and AUTO_LEAD_MINUTES is not None:
        mins = [max(m, AUTO_LEAD_MINUTES) for m in mins] + [AUTO_LEAD_MINUTES]
    return sorted(set(mins))


def declined(event):
    for a in event.get("attendees", []):
        if a.get("self") and a.get("responseStatus") == "declined":
            return True
    return False


def notify(title, remaining_sec, when_local, link, location, lead, start_ts):
    # The alert at the auto/floor lead is the "imminent" one: urgent, with the
    # Join button and a live countdown that closes at meeting start. Earlier
    # alerts are calm heads-ups.
    imminent = AUTO_LEAD_MINUTES is not None and lead <= AUTO_LEAD_MINUTES
    lead_text = "starting now" if remaining_sec <= 45 else f"in {human(remaining_sec)}"
    body_lines = [f"{lead_text}  •  {when_local}"]
    if location:
        body_lines.append(location)
    body = "\n".join(body_lines)

    # Fire the interactive popup detached, in its own transient scope, so its
    # wait-for-click survives this run exiting and never stalls it.
    cmd = [
        "systemd-run", "--user", "--quiet", "--collect", "--",
        POPUP_HELPER,
        "--app", APP_NAME,
        "--urgency", "critical" if imminent else "normal",
        "--icon", ICON,
        "--summary", title,
        "--body", body,
        "--sound", SOUND_NOW if imminent else SOUND_SOON,
    ]
    if imminent:
        # Live "Meeting starts in: M:SS" countdown; the popup closes at 0:00.
        cmd += ["--countdown-until", str(int(start_ts)), "--when", when_local]
        if location:
            cmd += ["--location", location]
        if link:  # Join button only on the imminent alert
            cmd += ["--link", link, "--meet-opener", MEET_OPENER]
    subprocess.run(cmd, check=False)


def alert_auth(force=False):
    """Fail loud when the Google sign-in is broken: a sticky critical popup
    with a Re-authorize button. Throttled in-session unless force=True (the
    startup check forces it so every login re-nags until it's fixed)."""
    state = load_state()
    now_ts = datetime.now(timezone.utc).timestamp()
    if not force and now_ts - state.get("auth_alert_ts", 0) < AUTH_ALERT_THROTTLE_SEC:
        return
    state["auth_alert_ts"] = now_ts
    save_state(state)
    subprocess.run(
        ["systemd-run", "--user", "--quiet", "--collect", "--",
         POPUP_HELPER,
         "--app", APP_NAME,
         "--urgency", "critical",
         "--icon", "dialog-warning",
         "--summary", "Meeting alerts paused",
         "--body", "Google sign-in expired — you will not be reminded of "
                   "meetings until you re-authorize.",
         "--sound", SOUND_NOW,
         "--reauth"],
        check=False,
    )


# ---------------------------------------------------------------------------
# Calendar fetch (cached)
# ---------------------------------------------------------------------------
class AuthBroken(Exception):
    """The Google sign-in is unusable (expired / revoked / missing token)."""


def _trim(ev):
    """Keep only the fields the reminder loop needs — small and JSON-cacheable."""
    return {
        "iCalUID": ev.get("iCalUID"),
        "summary": ev.get("summary", "(no title)"),
        "start": {"dateTime": ev["start"]["dateTime"]},
        "hangoutLink": ev.get("hangoutLink", ""),
        "location": ev.get("location", ""),
        "reminders": {"overrides": ev.get("reminders", {}).get("overrides") or []},
    }


def fetch_events(now):
    """Upcoming timed events across all calendars, deduped and trimmed.

    Raises AuthBroken on a credential problem (caller fails loud). Lets network
    / transient API errors propagate (caller falls back to the cache).
    """
    try:
        service = get_service()
        calendars = service.calendarList().list().execute().get("items", [])
    except (RefreshError, FileNotFoundError, ValueError) as e:
        raise AuthBroken() from e
    except HttpError as e:
        status = getattr(e, "status_code", None) or getattr(e.resp, "status", None)
        if status == 401:
            raise AuthBroken() from e
        raise  # quota / 5xx / etc. -> transient

    time_min = now.isoformat()
    time_max = (now + timedelta(hours=WINDOW_HOURS)).isoformat()

    # Dedupe the same meeting across calendars by iCalUID: prefer the copy with
    # explicit reminder overrides, then the primary calendar's copy.
    chosen = {}  # uid -> (trimmed_event, rank)  (lower rank wins)
    for cal in calendars:
        is_primary = cal.get("primary", False)
        try:
            items = service.events().list(
                calendarId=cal["id"],
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,          # expand recurrences into instances
                orderBy="startTime",
                fields="items(iCalUID,summary,start,reminders,"
                "hangoutLink,location,status,attendees)",
            ).execute().get("items", [])
        except Exception as e:  # one bad calendar shouldn't kill the run
            print(f"warn: calendar {cal['id']}: {e}", file=sys.stderr)
            continue
        for ev in items:
            if ev.get("status") == "cancelled":
                continue
            if "dateTime" not in ev.get("start", {}):
                continue  # all-day event -> no meeting time
            if SKIP_DECLINED and declined(ev):
                continue
            uid = ev.get("iCalUID") or ev.get("summary", "?") + ev["start"]["dateTime"]
            has_overrides = bool(ev.get("reminders", {}).get("overrides"))
            rank = (0 if has_overrides else 1, 0 if is_primary else 1)
            if uid not in chosen or rank < chosen[uid][1]:
                chosen[uid] = (_trim(ev), rank)
    return [ev for ev, _rank in chosen.values()]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(auth_check_only=False):
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()

    # Login-time check: only verify the sign-in works (no caching, no notifying).
    if auth_check_only:
        try:
            fetch_events(now)
        except AuthBroken:
            alert_auth(force=True)        # re-nag every login until fixed
        except Exception:
            pass                          # network/transient -> not an auth issue
        else:
            st = load_state()
            if st.pop("auth_alert_ts", None) is not None:
                save_state(st)
        return

    state = load_state()
    cache = state.get("cache") or {}

    # Call Google only every POLL_INTERVAL_SEC; otherwise reuse the cached
    # events. On ANY fetch failure fall back to the last cache regardless of its
    # age — better to keep alerting for known meetings than to go dark (offline).
    if "events" in cache and now_ts - cache.get("fetched_at", 0) < POLL_INTERVAL_SEC:
        events = cache["events"]
    else:
        try:
            events = fetch_events(now)
        except AuthBroken:
            alert_auth()                  # throttled re-auth popup
            events = cache.get("events", [])
        except Exception as e:
            print(f"warn: fetch failed, using cache: {e}", file=sys.stderr)
            events = cache.get("events", [])
        else:
            state["cache"] = {"fetched_at": now_ts, "events": events}
            state.pop("auth_alert_ts", None)   # auth healthy -> clear

    fired = state.setdefault("fired", {})
    for ev in events:
        start = datetime.fromisoformat(ev["start"]["dateTime"])
        start_ts = start.timestamp()
        remaining = start_ts - now_ts
        if remaining < -120:  # meeting already well underway
            continue
        title = ev.get("summary", "(no title)")
        link = ev.get("hangoutLink", "")
        location = ev.get("location", "")
        when_local = start.astimezone().strftime("%H:%M")
        uid = ev.get("iCalUID") or title + ev["start"]["dateTime"]
        for m in popup_minutes(ev):
            if remaining <= m * 60:  # this reminder's moment has arrived
                key = f"{uid}|{ev['start']['dateTime']}|{m}"
                if key not in fired:
                    notify(title, remaining, when_local, link, location, m, start_ts)
                    fired[key] = start_ts
                break  # fire only the largest due reminder this run

    # Prune fired entries for meetings that ended > 3h ago.
    cutoff = now_ts - 3 * 3600
    state["fired"] = {k: v for k, v in fired.items() if v > cutoff}
    save_state(state)


if __name__ == "__main__":
    # --auth-check: only verify the sign-in (used by the startup service) and,
    # if broken, force the re-auth popup; skip the normal meeting poll.
    auth_check = "--auth-check" in sys.argv
    try:
        main(auth_check_only=auth_check)
    except Exception as e:
        # Transient (offline, API hiccup) — log and exit cleanly so the
        # timer just retries next minute instead of flagging a failed unit.
        print(f"meeting-notify: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(0)
