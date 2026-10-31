#!/usr/bin/env python3
"""This repository answers from a person's rules, an upstream or Anthropic's API, never from a
ChatGPT subscription or as anybody else's client. Nothing that does may come into it.

    python3 tests/test_no_plan_route.py

LLMSim-internal, Maniple's own copy, is this repository plus the ChatGPT plan route, and is kept
in step by merging from here. INTERNAL.md marks that copy, and this check does not apply there.
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORBIDDEN = re.compile(r"chatgpt|chat\.openai|sign.in.with.openai|app_EMoam|codex_cli|REMOVED_CLIENT_ID|removed_originator|"
                       r"dynamic_agent_client|tokens\.use\.direct|backend-api", re.I)


def main():
    if os.path.exists(os.path.join(ROOT, "INTERNAL.md")):
        print("  skip  this is LLMSim-internal, which keeps the route\n\n0 passed, 0 failed")
        return
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
    found = []
    for name in files:
        path = os.path.join(ROOT, name)
        if not name or name == "tests/test_no_plan_route.py" or not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            raw = fh.read()
        if b"\0" in raw:
            continue
        for number, line in enumerate(raw.decode("utf-8", "replace").split("\n"), 1):
            if FORBIDDEN.search(line):
                found.append("%s:%d: %s" % (name, number, line.strip()[:100]))
    if found:
        print("  FAIL  this repository must not carry the ChatGPT plan route\n        " + "\n        ".join(found[:40]))
        print("\n0 passed, 1 failed")
        sys.exit(1)
    print("  ok    nothing of the ChatGPT plan route is in any tracked file\n\n1 passed, 0 failed")


if __name__ == "__main__":
    main()
