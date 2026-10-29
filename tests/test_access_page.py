#!/usr/bin/env python3
"""The Settings › Sign-in and API key page, in a real browser: set and change the
password, make, replace and remove the API key, sign out and in, turn it off.
tests/test_access.py checks the same through the API; this checks the page.

It needs Node and Playwright for Node, which LLMSim itself does not. Without them it
says it skipped and passes. Point NODE_PATH at a node_modules that has Playwright:

    NODE_PATH=/path/to/node_modules python3 tests/test_access_page.py
"""

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from kit import Sim  # noqa: E402

PASSWORD, KEY = "started-with-this", "llmsim-env-key-0123456789abcdef"


def skip(why):
    print("Settings page in a browser\n  skip  %s" % why)
    print("\n0 passed, 0 failed")
    sys.exit(0)


def main():
    node = shutil.which("node")
    if not node:
        skip("Node is not installed")
    found = subprocess.run([node, "-e", "require.resolve('playwright')"], capture_output=True)
    if found.returncode:
        skip("Playwright for Node was not found; set NODE_PATH to a node_modules that has it")
    sims = {"open": Sim(), "env": Sim(env={"LLMSIM_ADMIN_PASSWORD": PASSWORD, "LLMSIM_API_KEY": KEY})}
    try:
        for sim in sims.values():
            sim.start()
        cfg = {"open": sims["open"].base, "env": sims["env"].base, "envPassword": PASSWORD, "envKey": KEY}
        done = subprocess.run([node, os.path.join(HERE, "ui", "access_page.cjs"), json.dumps(cfg)], timeout=300)
    finally:
        for sim in sims.values():
            sim.stop()
    sys.exit(done.returncode)


if __name__ == "__main__":
    main()
