"""Code evaluators: a Python function in a file is a check.

    # evaluators/refund_policy.py
    def check(run):
        refunds = [c for c in run["toolCalls"] if c["tool"] == "refund"]
        return {"status": "pass" if all(c["arguments"]["amount"] <= 500 for c in refunds) else "fail", "detail": "refunds within the limit"}

    {"type": "code", "file": "refund_policy.py"}                      # function defaults to check

The function is given the run — events, replies, final answer, tool calls, state — already scrubbed of every
credential the kit holds, and returns True/False or {status, detail, evidence}. It runs in its own process with
no environment, a time limit, and an audit hook that refuses it the credential files, sockets and new processes.

Evaluators are read from a directory the OPERATOR controls (LLMSIM_EVALUATORS, default ./evaluators). They cannot
be uploaded over the API: that would be remote code execution for anyone who can reach the port.
"""

import json
import os
import re
import subprocess
import sys

from . import checks, core, credentials, rest_area
from .checks import check, result
from .core import ok
from .store import store

NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,60}\.py")


def directory():
    return os.environ.get("LLMSIM_EVALUATORS") or os.path.join(core.DATA, "evaluators")


def available():
    try:
        return sorted(f for f in os.listdir(directory()) if NAME.fullmatch(f))
    except OSError:
        return []


def validate(spec):
    problems = ["unknown field %r" % k for k in spec if k not in {"type", "name", "agent", "file", "function", "params", "timeoutS"}]
    if not isinstance(spec.get("file"), str) or not NAME.fullmatch(spec["file"]):
        problems.append("file is the name of a .py file in the evaluators directory")
    elif spec["file"] not in available():
        problems.append("there is no evaluator %s in %s" % (spec["file"], directory()))
    if "function" in spec and not (isinstance(spec["function"], str) and re.fullmatch(r"[A-Za-z_]\w*", spec["function"])):
        problems.append("function is a name")
    if "timeoutS" in spec and not (isinstance(spec["timeoutS"], (int, float)) and 0 < spec["timeoutS"] <= 120):
        problems.append("timeoutS is up to 120")
    return problems


def denied_files():
    host = core.host
    paths = [credentials.path(), store().path, store().path + "-wal", store().path + "-shm"]
    for attr in ("CONFIG_PATH", "CHATGPT_AUTH_PATH", "RULES_PATH", "SUITE_PATH"):
        if getattr(host, attr, None):
            paths.append(getattr(host, attr))
    return paths


@check("code", validate)
def check_code(spec, ctx):
    view = ctx.view()
    payload = core.redact({"run": ctx.run, "events": ctx.events, "replies": ctx.replies, "final": ctx.final, "agents": view["agents"], "counts": view["counts"],
                           "toolCalls": ctx.tool_calls() or ctx.requested_calls(), "usage": view["usage"], "state": ctx.state, "params": spec.get("params") or {}})
    raw = json.dumps(payload, default=str)
    if len(raw) > 5_000_000:
        return result(spec, "error", "the run is too large to hand to an evaluator")
    import tempfile

    def limits():
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (int(spec.get("timeoutS") or 10) + 2,) * 2)
            resource.setrlimit(resource.RLIMIT_AS, (1 << 30,) * 2)
        except (ImportError, ValueError, OSError):
            pass
    with tempfile.TemporaryDirectory() as tmp:
        try:
            proc = subprocess.run([sys.executable, "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)), "_evalchild.py"), os.path.join(directory(), spec["file"]),
                                   spec.get("function", "check"), json.dumps(denied_files())], input=raw, capture_output=True, text=True,
                                  timeout=float(spec.get("timeoutS") or 10), env={}, cwd=tmp, preexec_fn=limits)
        except subprocess.TimeoutExpired:
            return result(spec, "error", "the evaluator did not finish within %ds" % (spec.get("timeoutS") or 10))
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        return result(spec, "error", "the evaluator crashed: %s" % ((proc.stderr or proc.stdout)[-200:] or "no output"))
    if isinstance(out, bool):
        return result(spec, "pass" if out else "fail", "the evaluator said %s" % ("yes" if out else "no"))
    status = out.get("status")
    if status not in ("pass", "fail", "no-data", "error"):
        return result(spec, "error", "the evaluator returned status %r; it must be pass, fail, no-data or error" % status)
    return result(spec, status, str(out.get("detail", ""))[:500], out.get("evidence") if isinstance(out.get("evidence"), list) else [])


@rest_area("evaluators")
def _rest(method, rest, body, query, host):
    if method == "GET" and not rest:
        return ok({"evaluators": available(), "directory": directory(),
                   "note": "Evaluators are files in this directory. They are not uploaded over the API, because they run code."})
    from .core import fail
    return fail(404, "No such route: %s /api/evaluators/%s" % (method, "/".join(rest)))
