#!/usr/bin/env python3
"""Send one grouped factory alert to the operator's GNOME notification service."""
from __future__ import annotations
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def notify(subject, body, city, run=subprocess.run):
    state = Path(city) / 'proof' / 'desktop-notification.json'
    try:
        previous = json.loads(state.read_text()).get('notification_id', 0)
        if not isinstance(previous, int) or previous < 0:
            previous = 0
    except (OSError, ValueError, AttributeError):
        previous = 0
    env = dict(os.environ)
    env.setdefault('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/run/user/%d/bus' % os.getuid())
    command = ['gdbus', 'call', '--session', '--dest', 'org.freedesktop.Notifications',
               '--object-path', '/org/freedesktop/Notifications',
               '--method', 'org.freedesktop.Notifications.Notify', '--',
               'Gas City', str(previous), 'dialog-warning', subject[:180],
               html.escape(body[:2800]), '[]', "{'urgency': <byte 2>}", '-1']
    done = run(command, env=env, capture_output=True, text=True, timeout=15)
    if done.returncode:
        raise RuntimeError(done.stderr.strip()[-400:] or 'GNOME notification failed')
    found = re.fullmatch(r'\(uint32 (\d+),\)\s*', done.stdout)
    if not found:
        raise RuntimeError('GNOME returned an unrecognized notification receipt')
    state.parent.mkdir(parents=True, exist_ok=True)
    temp = state.with_suffix('.tmp')
    temp.write_text(json.dumps({'notification_id': int(found.group(1))}))
    os.replace(temp, state)


def main():
    city = os.environ.get('GC_CITY') or os.environ.get('GC_CITY_PATH')
    if not city:
        print('notify-gnome: GC_CITY is required', file=sys.stderr)
        return 1
    try:
        notify(os.environ.get('PROOF_SUBJECT') or 'Gas City needs attention', sys.stdin.read(), city)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print('notify-gnome: %s' % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
