#!/usr/bin/env bash
# Mirrors liskov-skills validate.yml in full. Structural and deterministic checks
# only: live-agent evaluations, release tags and publication are separate work.
set -euo pipefail
python3 scripts/validate.py
python3 -m unittest discover -s tests -p 'test_*.py'
git diff --check
