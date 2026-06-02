#!/usr/bin/env python3
# meeting-notify — Google Calendar desktop meeting notifier
# Copyright (C) 2026 Ionut R.  —  https://github.com/d-packs/meeting-notify
# Licensed under the GNU General Public License v3.0 (see LICENSE); NO WARRANTY.

"""Re-authorize meeting-notify against Google Calendar.

Runs the OAuth installed-app flow using the saved client credentials, opens the
browser for sign-in, writes a fresh token to TOKEN_FILE, and pops a confirmation
notification. Triggered by the "Re-authorize" button on the auth-failure alert
(or run by hand). Read-only Calendar scope — the notifier only reads events.
"""

import os
import subprocess
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

CONFIG_DIR = Path.home() / ".config" / "meeting-notify"
CLIENT_FILE = CONFIG_DIR / "client_secret.json"
TOKEN_FILE = CONFIG_DIR / "token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def notify(summary, body, urgency="normal"):
    subprocess.run(
        ["notify-send", "--app-name", "Meeting Reminder",
         "-u", urgency, "-i", "appointment-soon", summary, body],
        check=False,
    )


def main():
    if not CLIENT_FILE.exists():
        notify("Re-authorization failed",
               f"Missing {CLIENT_FILE}. Cannot re-authorize.", "critical")
        return
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    # access_type=offline + prompt=consent guarantee a long-lived refresh token
    # is issued, so the saved credentials can self-refresh indefinitely.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json())
    os.chmod(TOKEN_FILE, 0o600)
    notify("Meeting alerts resumed", "Google sign-in renewed — you're all set.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        subprocess.run(
            ["notify-send", "--app-name", "Meeting Reminder", "-u", "critical",
             "-i", "dialog-warning", "Re-authorization failed",
             f"{type(e).__name__}: {e}"],
            check=False,
        )
        sys.exit(1)
