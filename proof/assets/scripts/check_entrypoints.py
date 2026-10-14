#!/usr/bin/env python3
"""Refuse a pack whose direct order commands cannot execute after deployment."""
from pathlib import Path
import os
import shlex
import sys
import tomllib


def problems(pack):
    found = []
    for order in sorted((Path(pack) / 'orders').glob('*.toml')):
        command = tomllib.loads(order.read_text()).get('order', {}).get('exec')
        if not command:
            continue
        argv = shlex.split(command)
        first = argv[0].replace('$PACK_DIR', str(pack)).replace('${PACK_DIR}', str(pack))
        if argv[0].startswith('$PACK_DIR/') or argv[0].startswith('${PACK_DIR}/'):
            if not Path(first).is_file() or not os.access(first, os.X_OK):
                found.append('%s: order entry point is absent or not executable: %s' % (order.name, first))
    return found


def main():
    issues = problems(Path(sys.argv[1]).resolve())
    for issue in issues:
        print('pack-entrypoints: ' + issue, file=sys.stderr)
    if not issues:
        print('pack-entrypoints: all direct order commands are executable')
    return int(bool(issues))


if __name__ == '__main__':
    sys.exit(main())
