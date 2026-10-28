#!/usr/bin/env python3
"""The process driver's program: the message on stdin, the agent's reply on stdout.

LLMSim runs it once per turn with LLMSIM_RUN and LLMSIM_AGENT set; it passes them on, so the
agent's model calls are the run's."""
import json
import os
import sys
import urllib.request

url = os.environ.get("AGENT_URL", "http://host.docker.internal:8771/api/chat")
body = {"input": sys.stdin.read().strip(), "run": os.environ.get("LLMSIM_RUN", ""), "agent": os.environ.get("LLMSIM_AGENT", ""),
        "session": "cli-" + os.environ.get("LLMSIM_RUN", "local")}
req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=300) as r:
    print(json.load(r)["reply"])
