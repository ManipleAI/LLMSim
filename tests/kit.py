"""A LLMSim subprocess for the kit's tests, and the small helpers they share.

Not a test file: tests/test_kit_*.py import this. Everything runs against a
throwaway directory and a free port; the live instance is never touched.
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(HERE, "server.py")
OPENAI_KEY = "sk-llmsim-000000000000000000000000"
ANTHROPIC_KEY = "sk-ant-llmsim-000000000000000000000000"
CHAT = "/openai/v1/chat/completions"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Sim:
    def __init__(self, workdir=None, env=None):
        self.port = free_port()
        self.tmp = None if workdir else tempfile.TemporaryDirectory()
        self.workdir = workdir or self.tmp.name
        self.env = dict(os.environ, LLMSIM_PORT=str(self.port), LLMSIM_CONFIG=os.path.join(self.workdir, "config.json"),
                        LLMSIM_SUITE=os.path.join(self.workdir, "suite.json"), LLMSIM_CASSETTES=os.path.join(self.workdir, "cassettes"),
                        LLMSIM_CHATGPT_AUTH=os.path.join(self.workdir, "auth.json"),
                        LLMSIM_RULES=os.path.join(self.workdir, "rules.json"), LLMSIM_ASSERTIONS=os.path.join(self.workdir, "assertions.json"),
                        LLMSIM_RUNS_DB=os.path.join(self.workdir, "runs.db"),
                        LLMSIM_CREDENTIALS=os.path.join(self.workdir, "credentials.json"), **(env or {}))
        self.proc = None
        self.base = "http://127.0.0.1:%d" % self.port

    def start(self):
        self.proc = subprocess.Popen([sys.executable, "-u", SERVER], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(100):
            try:
                urllib.request.urlopen(self.base + "/healthz", timeout=1).read()
                return self
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("LLMSim did not start: " + (self.proc.stderr.read(2000).decode() if self.proc.poll() is not None else ""))

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def restart(self):
        self.stop()
        return self.start()

    def call(self, method, path, body=None, headers=None, raw=None):
        """(status, headers, text). Never raises on an HTTP error."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        data = raw if raw is not None else (json.dumps(body) if body is not None else None)
        conn.request(method, path, data, dict({"Content-Type": "application/json"}, **(headers or {})))
        resp = conn.getresponse()
        text = resp.read().decode()
        conn.close()
        return resp.status, dict(resp.getheaders()), text

    def api(self, method, path, body=None, headers=None):
        status, _, text = self.call(method, path, body, headers)
        try:
            return status, json.loads(text)
        except ValueError:
            return status, text

    def chat(self, content="hello there", path=CHAT, headers=None, **extra):
        body = dict({"model": "gpt-5", "messages": [{"role": "user", "content": content}]}, **extra)
        return self.api("POST", path, body, dict({"Authorization": "Bearer " + OPENAI_KEY}, **(headers or {})))

    def url(self, path=""):
        return self.base + path


def run_cases(cases, make_sim=None):
    """Run each case(sim); print the way the other test files do; exit non-zero on a failure."""
    failed = 0
    sim = (make_sim or Sim)().start()
    try:
        for case in cases:
            try:
                case(sim)
                print("  ok    %s" % case.__name__[5:].replace("_", " "))
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print("  FAIL  %s\n        %s: %s" % (case.__name__[5:].replace("_", " "), type(exc).__name__, str(exc)[:700]))
    finally:
        sim.stop()
    print("\n%d passed, %d failed" % (len(cases) - failed, failed))
    sys.exit(1 if failed else 0)
