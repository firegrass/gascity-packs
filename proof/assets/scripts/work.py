#!/usr/bin/env python3
"""Invoke the native transaction helper with this pack's trusted evaluator."""
import argparse
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--helper', type=Path, required=True)
    parser.add_argument('--request', type=Path, required=True)
    args = parser.parse_args()
    with args.request.open('rb') as stream:
        result = subprocess.run([str(args.helper.resolve()), '--workspace', str(args.workspace.resolve()),
                                 '--evaluator', str(Path(__file__).resolve().with_name('proof_work_evaluate.py'))],
                                stdin=stream, check=False)
    sys.exit(result.returncode)


if __name__ == '__main__':
    main()
