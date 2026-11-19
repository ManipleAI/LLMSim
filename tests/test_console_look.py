#!/usr/bin/env python3
"""The console's look, in a real browser: every page at desktop and phone width.
Pictures go to $LLMSIM_SHOTS (default: a temp folder, printed at the end).
Needs Node and Playwright for Node; without them it says it skipped and passes.

    NODE_PATH=/path/to/node_modules python3 tests/test_console_look.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from kit import Sim  # noqa: E402


def main():
    node = shutil.which("node")
    if not node or subprocess.run([node, "-e", "require.resolve('playwright')"], capture_output=True).returncode:
        print("Console look in a browser\n  skip  Node or Playwright for Node was not found\n\n0 passed, 0 failed")
        return 0
    shots = os.environ.get("LLMSIM_SHOTS") or tempfile.mkdtemp(prefix="llmsim-look-")
    # LLMSIM_LOOK_BASE: look at a console that already has agents, scenarios and runs in it.
    given = os.environ.get("LLMSIM_LOOK_BASE")
    sim = None if given else Sim()
    try:
        if sim:
            sim.start()
        cfg = {"base": given or sim.base, "shots": shots, "deep": bool(given)}
        done = subprocess.run([node, os.path.join(HERE, "ui", "console_look.cjs"), json.dumps(cfg)], timeout=900)
    finally:
        if sim:
            sim.stop()
    print("pictures: " + shots)
    return done.returncode


if __name__ == "__main__":
    sys.exit(main())
