"""Test runs: a set of scenarios, run on one agent, kept as one thing.

Running a scenario answers "does this hold". What somebody responsible for an agent asks is
wider: here is the agent, here are the tests that apply to it, run them and tell me which
failed. A test run is that: one saved agent, the scenarios chosen for it, one result each,
and a verdict over the lot. It is kept, so the question "how did this agent do last time"
has an answer.

It is deliberately less than an experiment. An experiment runs a dataset version, repeats
cases, and is compared with a baseline; it is what a release is gated on. A test run is
what a person presses a button for: these scenarios, that agent, now.
"""

import threading
import time

from . import agents, core, rest_area, runner
from .store import store

SPEC_KEYS = {"agent", "scenarios", "tags", "all", "label", "tier", "wait"}
_LOCK = threading.Lock()
_RUNNING = {}                     # test run id -> the document, while it runs


def validate(spec):
    if not isinstance(spec, dict):
        return ["a test run is {agent, scenarios | tags | all}"]
    problems = ["unknown field %r" % k for k in spec if k not in SPEC_KEYS]
    if not core.valid_id(spec.get("agent")):
        problems.append("agent is the id of a saved agent")
    elif store().get("agents", spec["agent"]) is None:
        problems.append("there is no saved agent called %s" % spec["agent"])
    chosen = [k for k in ("scenarios", "tags", "all") if spec.get(k)]
    if len(chosen) != 1:
        problems.append("say which scenarios in one way: scenarios (a list of ids), tags (any of these), or all: true")
    for key in ("scenarios", "tags"):
        if spec.get(key) is not None and not (isinstance(spec[key], list) and spec[key] and all(isinstance(x, str) and x for x in spec[key])):
            problems.append("%s is a list of names" % key)
    if spec.get("tier", "simulated") not in ("simulated", "real"):
        problems.append("tier is simulated or real")
    if "label" in spec and not isinstance(spec["label"], str):
        problems.append("label is text")
    return problems


def select(spec):
    """(scenarios, missing): the saved scenarios a spec chooses, in the order asked for."""
    saved = {sid: sc for sid, sc, _ in store().docs("scenarios")}
    if spec.get("scenarios"):
        wanted = list(dict.fromkeys(spec["scenarios"]))
        return [saved[s] for s in wanted if s in saved], [s for s in wanted if s not in saved]
    if spec.get("tags"):
        return [sc for _, sc in sorted(saved.items()) if set(sc.get("tags") or []) & set(spec["tags"])], []
    return [sc for _, sc in sorted(saved.items())], []


def summarize(doc):
    counts = {"pass": 0, "fail": 0, "no-data": 0, "error": 0, "waiting": 0}
    for item in doc["items"]:
        counts[item["verdict"] if item["verdict"] in counts else "error"] += 1
    doc["summary"] = dict(counts, total=len(doc["items"]))
    done = counts["waiting"] == 0
    doc["verdict"] = ("pass" if counts["pass"] == len(doc["items"]) and doc["items"] else "fail") if done else None
    return doc


def execute(doc, scenarios, base_host):
    """Each scenario in turn, on the agent. One after another: they are conversations with the
    same agent, and an agent that is asked five things at once is being load-tested, not tested."""
    by_id = {sc["id"]: sc for sc in scenarios}
    try:
        for item in doc["items"]:
            sc = by_id[item["scenario"]]
            if isinstance(sc.get("agents"), list):
                item.update(verdict="error", error="It drives several agents at once; run it on its own and say which of them is this agent.")
            else:
                result = runner.run_scenario(sc, {"agent": doc["agent"], "tier": doc["tier"], "label": doc["id"], "testrun": doc["id"]}, base_host)
                bad = [c for c in result.get("checks") or [] if c["status"] != "pass"]
                item.update(run=result["run"], verdict=result.get("verdict") or "error", durationS=result.get("durationS"), summary=result.get("summary"),
                            error=result.get("error"), failed=[{"name": c["name"], "status": c["status"], "detail": c.get("detail", "")} for c in bad[:3]])
            store().put("testruns", doc["id"], summarize(doc))
    finally:
        doc["status"], doc["finishedAt"] = "done", time.time()
        for item in doc["items"]:
            if item["verdict"] == "waiting":                         # the run was cut short; say so rather than leave it waiting for ever
                item.update(verdict="error", error="It was not reached: the test run stopped before it.")
        store().put("testruns", doc["id"], summarize(doc))
        with _LOCK:
            _RUNNING.pop(doc["id"], None)
    return doc


def start(spec, base_host):
    scenarios, missing = select(spec)
    if missing:
        return None, "There is no scenario called %s." % ", ".join(missing)
    if not scenarios:
        return None, "No saved scenario matches that."
    doc = {"id": core.new_id("t-", 8), "agent": spec["agent"], "label": spec.get("label", ""), "tier": spec.get("tier", "simulated"),
           "status": "running", "startedAt": time.time(),
           "items": [{"scenario": sc["id"], "description": sc.get("description", ""), "verdict": "waiting"} for sc in scenarios]}
    summarize(doc)
    store().put("testruns", doc["id"], doc)
    with _LOCK:
        _RUNNING[doc["id"]] = doc
    if spec.get("wait") is False:
        threading.Thread(target=execute, args=(doc, scenarios, base_host), daemon=True).start()
        return doc, None
    return execute(doc, scenarios, base_host), None


def get(run_id):
    with _LOCK:
        if run_id in _RUNNING:
            return dict(_RUNNING[run_id])
    doc = store().get("testruns", run_id)
    if doc and doc.get("status") == "running":
        # Kept as running by a server that is no longer running it.
        doc = dict(doc, status="interrupted")
    return doc


@rest_area("testruns")
def _rest(method, rest, body, query, host):
    st = store()
    if method == "GET" and not rest:
        on = (query.get("agent") or [None])[0]
        rows = []
        for run_id, _, _ in reversed(st.docs("testruns")):
            doc = get(run_id)
            if on and doc.get("agent") != on:
                continue
            rows.append({k: doc.get(k) for k in ("id", "agent", "label", "tier", "status", "verdict", "summary", "startedAt", "finishedAt")})
        return core.ok({"testruns": rows})
    if method == "POST" and not rest:
        problems = validate(body or {})
        if problems:
            return core.fail(400, "; ".join(problems[:5]), problems=problems)
        doc, problem = start(body, host)
        if problem:
            return core.fail(404, problem)
        if body.get("wait") is False:
            return 202, {"ok": True, "testrun": doc, "message": "Poll GET /api/testruns/%s." % doc["id"]}
        return core.ok({"testrun": doc})
    if len(rest) == 1 and core.valid_id(rest[0]):
        doc = get(rest[0])
        if doc is None:
            return core.fail(404, "There is no test run called %s." % rest[0])
        if method == "GET":
            return core.ok({"testrun": doc})
        if method == "DELETE":
            if doc.get("status") == "running":
                return core.fail(409, "It is still running.")
            return core.ok(removed=st.drop("testruns", rest[0]))
    return core.fail(404, "No such route: %s /api/testruns/%s" % (method, "/".join(rest)))
