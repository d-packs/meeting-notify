#!/usr/bin/python3
# meeting-notify — Google Calendar desktop meeting notifier
# Copyright (C) 2026 Ionut R.  —  https://github.com/d-packs/meeting-notify
# Licensed under the GNU General Public License v3.0 (see LICENSE); NO WARRANTY.

"""Interactive popup for meeting-notify.

Posts one desktop notification over D-Bus (org.freedesktop.Notifications) and
runs a GLib loop to: handle the action buttons, and — for the imminent alert —
tick a live "Meeting starts in: M:SS" countdown (updating in place via
replaces_id) and CloseNotification it as it reaches 0:00. The explicit close is
why this works even for `critical` urgency, which some servers (e.g. KDE) keep on screen
forever (ignoring expire_timeout).

Because it waits, it must run detached from the once-a-minute poller;
meeting-notify.py launches it via `systemd-run --user`.

Buttons: "Join meeting" (only when a link is given) opens the meeting with the
configured opener (--meet-opener, default the browser); "Close" dismisses.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time

from gi.repository import Gio, GLib

NOTIFY_NAME = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"
NOTIFY_IFACE = "org.freedesktop.Notifications"

# Hard cap so the helper can never linger (e.g. a normal popup that never closes).
SAFETY_CAP_SEC = 3600


REAUTH_SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                             "meeting-notify-reauth.py")


def open_meet(link, opener):
    # Open the link with the configured opener (--meet-opener), in its own
    # transient scope so it outlives this helper.
    subprocess.run(
        ["systemd-run", "--user", "--quiet", "--collect", "--", *opener, link],
        check=False,
    )


def open_reauth():
    # Launch the re-auth flow in its own transient scope (it opens the browser
    # and blocks until sign-in completes).
    subprocess.run(
        ["systemd-run", "--user", "--quiet", "--collect", "--", REAUTH_SCRIPT],
        check=False,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True)
    ap.add_argument("--urgency", required=True)  # normal | critical
    ap.add_argument("--icon", required=True)
    ap.add_argument("--summary", required=True)
    ap.add_argument("--body", default="")
    ap.add_argument("--sound", default="")
    ap.add_argument("--link", default="")
    ap.add_argument("--timeout-ms", type=int, default=0,
                    help="force-close after N ms (0 = let the server decide)")
    ap.add_argument("--countdown-until", type=int, default=0,
                    help="epoch seconds of meeting start; shows a live "
                         "'Meeting starts in: M:SS' countdown, closing at 0:00")
    ap.add_argument("--when", default="")       # meeting start, e.g. "16:00"
    ap.add_argument("--location", default="")
    ap.add_argument("--reauth", action="store_true",
                    help="add a 'Re-authorize' button that runs the re-auth flow")
    ap.add_argument("--meet-opener", default="xdg-open",
                    help="command used to open a meeting link (default: xdg-open)")
    args = ap.parse_args()

    paplay = shutil.which("paplay")
    if args.sound and paplay and os.path.exists(args.sound):
        subprocess.Popen([paplay, args.sound])

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    # Not every notification server shows action buttons well (GNOME Shell
    # under-emphasizes them). Ask what the server supports; if it can't do
    # actions, drop the buttons and surface the link / re-auth hint in the body
    # instead, so nothing is lost on GNOME/minimal daemons.
    try:
        caps = set(bus.call_sync(
            NOTIFY_NAME, NOTIFY_PATH, NOTIFY_IFACE, "GetCapabilities", None,
            GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE, -1, None
        ).unpack()[0])
    except Exception:
        caps = set()
    has_actions = "actions" in caps

    actions = []
    if has_actions:
        if args.link:
            actions += ["join", "Join meeting"]
        if args.reauth:
            actions += ["reauth", "Re-authorize"]
        actions += ["ok", "Close notification"]

    hints = {"urgency": GLib.Variant("y", 2 if args.urgency == "critical" else 1)}

    def render_body():
        if args.countdown_until > 0:
            remaining = max(0, int(args.countdown_until - time.time()))
            m, s = divmod(remaining, 60)
            lines = []
            if args.when:
                lines.append(f"at {args.when}")
            if args.location:
                lines.append(args.location)
            lines.append(f"Meeting starts in: {m}:{s:02d}")
        else:
            lines = [args.body] if args.body else []
        if not has_actions:  # no buttons -> put the actionable bits in the text
            if args.link:
                lines.append(args.link)
            if args.reauth:
                lines.append(f"Re-authorize: run {REAUTH_SCRIPT}")
        return "\n".join(lines)

    def post(replaces_id):
        # replaces_id=0 creates; reusing the id updates the popup in place
        # (no re-pop, no replayed sound) — that's how the clock ticks.
        reply = bus.call_sync(
            NOTIFY_NAME, NOTIFY_PATH, NOTIFY_IFACE, "Notify",
            GLib.Variant("(susssasa{sv}i)",
                         (args.app, replaces_id, args.icon, args.summary,
                          render_body(), actions, hints, -1)),
            GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, -1, None,
        )
        return reply.unpack()[0]

    nid = post(0)

    loop = GLib.MainLoop()

    def on_signal(_conn, _sender, _path, _iface, signal, params):
        if params.unpack()[0] != nid:
            return
        if signal == "ActionInvoked":
            key = params.unpack()[1]
            if key == "join" and args.link:
                open_meet(args.link, args.meet_opener.split())
            elif key == "reauth":
                open_reauth()
        loop.quit()

    for sig in ("ActionInvoked", "NotificationClosed"):
        bus.signal_subscribe(NOTIFY_NAME, NOTIFY_IFACE, sig, NOTIFY_PATH,
                             None, Gio.DBusSignalFlags.NONE, on_signal)

    def force_close():
        bus.call_sync(NOTIFY_NAME, NOTIFY_PATH, NOTIFY_IFACE, "CloseNotification",
                      GLib.Variant("(u)", (nid,)), None,
                      Gio.DBusCallFlags.NONE, -1, None)
        return False  # one-shot

    def tick():
        post(nid)  # update the countdown in place
        if args.countdown_until - time.time() <= 0:
            force_close()
            return False
        return True

    if args.countdown_until > 0:
        GLib.timeout_add_seconds(1, tick)
    if args.timeout_ms > 0:
        GLib.timeout_add(args.timeout_ms, force_close)
    GLib.timeout_add_seconds(SAFETY_CAP_SEC, loop.quit)

    loop.run()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"meeting-notify-popup: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(0)
