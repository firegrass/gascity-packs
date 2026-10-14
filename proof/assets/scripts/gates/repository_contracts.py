#!/usr/bin/env python3
"""Run the member-owned contracts composite locally, without a second command list."""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# A step names its base by expression, not by variable: each guard picks its
# own key (SOURCE_SIZE_BASE, LINK_TARGET_BASE, SCORECARD_BASE, ...). Locally the
# gate's merge base is that SHA. Any other expression still fails closed.
BASE_EXPRESSIONS = {
    "${{ github.event.pull_request.base.sha || github.event.before }}",
    "${{ github.event.before }}",
}


def is_base_expression(value):
    return re.sub(r"\s+", " ", str(value).strip()) in BASE_EXPRESSIONS


def commands(action, base, rust):
    if action.get("runs", {}).get("using") != "composite":
        raise ValueError("repository contracts must be a composite action")
    for step in action["runs"]["steps"]:
        if step.get("name") == "fmt" and not rust:
            continue
        if step.get("shell") != "bash" or not isinstance(step.get("run"), str) or "if" in step:
            raise ValueError("unsupported contracts step: " + str(step.get("name")))
        env = dict(os.environ)
        for key, value in step.get("env", {}).items():
            if is_base_expression(value):
                env[key] = base
            elif "${{" in str(value):
                raise ValueError("unresolved contracts environment: " + key)
            else:
                env[key] = str(value)
        if "${{" in step["run"]:
            raise ValueError("unresolved contracts command: " + step["name"])
        yield step["name"], step["run"], env


def main():
    import yaml
    action = yaml.safe_load(Path(".github/actions/repository-contracts/action.yml").read_text())
    for name, command, env in commands(action, sys.argv[1], "--rust" in sys.argv[2:]):
        print("gate: contracts: " + name, flush=True)
        # A step may append to the job summary; outside Actions it goes to the log.
        with tempfile.NamedTemporaryFile("r", suffix=".md") as summary:
            env.setdefault("GITHUB_STEP_SUMMARY", summary.name)
            subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", command],
                           env=env, check=True)
            text = summary.read()
        if text.strip():
            print(text, end="" if text.endswith("\n") else "\n", flush=True)


if __name__ == "__main__":
    main()
