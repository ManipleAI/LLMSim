"""Run a scenario and say what happened.

One function does the whole thing, in the same order every time:

  1. open a run, and give the agent's model its script (or its recording)
  2. set up what the agent's tools will see
  3. start the conversation with the agent, through its driver
  4. say each turn, record what comes back, stop when told to
  5. end the conversation and undo everything done in 1 and 2
  6. judge the run, with every check, over the timeline LLMSim kept

Step 5 happens whatever went wrong in 3 and 4, because a rule or a recording left
installed after a failed run answers the next run's requests.

The verdict is the checks'. A run that could not reach its agent has no verdict to
give — it failed, and says why.
"""

import os
import threading
import time
import traceback

from . import checks, core, drivers, fakes, ordering, runtime, scenario as scn, timeline
from .store import store

JOBS = {}                       # run -> result, while it is running
_LOCK = threading.Lock()


def new_run_id(scenario_id):
    return ("%s-%s" % (scenario_id[:40], core.new_id("", 6))).strip(".:-") or core.new_id("r-", 8)


def participants_of(scenario):
    """The agents a scenario drives: one `agent` with the scenario's turns, or each of `agents` with its own."""
    if "agents" in scenario:
        return [dict(a, name=a.get("name") or "agent%d" % i) for i, a in enumerate(scenario["agents"])]
    return [dict(scenario["agent"], name=scenario["agent"].get("name") or "agent", turns=scenario.get("turns"), user=scenario.get("user"))]


class RunCtx:
    """What setup actions and checks may use of the run in progress."""

    def __init__(self, run, scenario, tier, base_host, options):
        self.run, self.scenario, self.tier, self.base_host, self.options = run, scenario, tier, base_host, options
        self.participants = participants_of(scenario)
        self.agent = self.participants[0].get("name") or "agent"
        self.lock = threading.Lock()
        self.cleanup = []                # callables, run in reverse at the end
        self.seed = options.get("seed")

    def base_urls(self, agent=None):
        agent = agent or self.agent
        urls = core.host.run_base_urls(self.base_host, self.run, [agent])
        out = dict(urls["agents"][agent])
        out["self"] = "http://" + self.base_host
        out["run"] = urls["baseUrls"]
        return out


def _finish_model(ctx, result):
    model = ctx.scenario.get("model") or {}
    if model.get("cassette") and ctx.tier == "simulated":
        try:
            ok, message, _, summary = core.host.session_stop(ctx.run)
        except Exception as exc:  # noqa: BLE001
            result["model"]["error"] = str(exc)
            return
        if summary:
            result["model"].update({k: summary[k] for k in ("hits", "misses", "diverged", "entries") if k in summary})
            result["model"]["unmatched"] = summary.get("unmatched", [])[:5]


def _start_model(ctx, result):
    model = ctx.scenario.get("model") or {}
    result["model"] = {"tier": ctx.tier}
    if ctx.tier != "simulated":
        return []
    problems = runtime.set_run_rules(ctx.run, model.get("rules") or [])
    if problems:
        return problems
    ctx.cleanup.append(lambda: runtime.clear_run_rules(ctx.run))
    if model.get("cassette"):
        mode = model.get("mode", "replay")
        if mode == "auto":
            mode = "replay" if os.path.exists(core.host._cassette_path(model["cassette"])) else "record"
        ok, message, *_ = core.host.session_start(mode, model["cassette"], run=ctx.run, overwrite=(mode == "record"))
        if not ok:
            return ["the model's recording could not be started: %s" % message]
        result["model"].update({"cassette": model["cassette"], "mode": mode})
    return []


def run_scenario(scenario, options=None, base_host="localhost:8099"):
    """The result of one run of a scenario. Never raises: a failure to run is a result."""
    options = options or {}
    tier = options.get("tier", "simulated")
    run = options.get("run") or new_run_id(scenario["id"])
    ctx = RunCtx(run, scenario, tier, base_host, options)
    started = time.time()
    result = {"run": run, "scenario": scenario["id"], "tier": tier, "status": "running", "startedAt": started, "turns": [],
              "checks": [], "model": {}, "agents": [{"name": p["name"], "driver": p.get("driver")} for p in ctx.participants],
              "label": options.get("label", "")}
    with _LOCK:
        JOBS[run] = result
    store().touch_run(run, label=scenario["id"], meta={"scenario": scenario["id"], "tier": tier, "label": options.get("label", "")})
    store().put("results", run, result)
    driver = []
    if options.get("seed") is not None:
        fakes.reseed(options["seed"])
    try:
        problems = _start_model(ctx, result)
        for action in scenario.get("setup") or []:
            problems += scn.SETUP_TYPES[action["type"]][0](action, ctx) or []
        if problems:
            result["error"] = "; ".join(problems)
        else:
            deadline = started + float(scenario.get("timeoutS") or 120)
            driver = []
            for part in ctx.participants:
                name = part["name"]
                dctx = drivers.Ctx(run, name, ctx.base_urls(name), emit=(lambda n: lambda kind, data: timeline.emit(run, kind, n, None, data))(name),
                                   timeout=float(scenario.get("timeoutS") or 120))
                d = drivers.make(part, dctx)
                d.start()
                driver.append((d, part))
            if scenario.get("ordering") and options.get("order"):
                bad = ordering.install(run, scenario["ordering"], options["order"])
                if bad:
                    raise ValueError("; ".join(bad))
                ctx.cleanup.append(lambda: result.setdefault("model", {}).__setitem__("orderTimedOut", ordering.remove(run)))
            _converse_all(driver, ctx, result, deadline)
    except drivers.DriverError as exc:
        result["error"] = "the agent could not be driven: %s" % exc
    except Exception as exc:  # noqa: BLE001
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        core.log("testkit: run %s: %s" % (run, traceback.format_exc(limit=3).replace("\n", " | ")))
    finally:
        for d, _ in driver or []:
            try:
                d.end()
            except Exception:  # noqa: BLE001
                pass
        _finish_model(ctx, result)
        for action in scenario.get("teardown") or []:
            try:
                scn.SETUP_TYPES[action["type"]][0](action, ctx)
            except Exception as exc:  # noqa: BLE001
                result.setdefault("warnings", []).append("teardown %s: %s" % (action.get("type"), exc))
        for undo in reversed(ctx.cleanup):
            try:
                undo()
            except Exception as exc:  # noqa: BLE001
                result.setdefault("warnings", []).append("cleanup: %s" % exc)
    _judge(ctx, result)
    result["state"] = fakes.snapshot(run)
    if not options.get("keepState"):
        fakes.purge(run)
    result["finishedAt"] = time.time()
    result["durationS"] = round(result["finishedAt"] - started, 3)
    result["status"] = "done"
    result = core.redact(result)
    store().put("results", run, result)
    with _LOCK:
        JOBS.pop(run, None)
    return result


def _converse_all(drivers_, ctx, result, deadline):
    """Every agent has its conversation at the same time. One agent is the usual case and runs in this thread."""
    if len(drivers_) == 1:
        d, part = drivers_[0]
        return _converse(d, ctx, result, deadline, part)
    errors = []

    def one(d, part):
        try:
            _converse(d, ctx, result, deadline, part)
        except drivers.DriverError as exc:
            errors.append("%s: %s" % (part["name"], exc))
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s: %s" % (part["name"], type(exc).__name__, exc))
    threads = [threading.Thread(target=one, args=pair, daemon=True) for pair in drivers_]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(1.0, deadline - time.time() + 5))
    if errors:
        raise drivers.DriverError("; ".join(errors))


def _converse(driver, ctx, result, deadline, part):
    scenario, run, agent = ctx.scenario, ctx.run, part["name"]
    turns = part.get("turns")
    limit = (scenario.get("stop") or {}).get("maxTurns") or 100
    simulated = None
    if turns is None:
        from . import users
        simulated = users.make(part["user"], ctx)
    multi = "agents" in scenario
    mine = []
    n = 0
    while n < limit:
        if time.time() > deadline:
            result["error"] = "the scenario's time limit (%ds) passed after %d turn(s)" % (scenario.get("timeoutS") or 120, n)
            return
        if turns is not None:
            if n >= len(turns):
                return
            say, expect = turns[n]["say"], turns[n].get("expect")
        else:
            say, expect = simulated.next(mine), None
            if say is None:
                return
        n += 1
        said = timeline.emit(run, "user_turn", agent, None, {"text": say, "turn": n})
        reply = driver.send(say)
        timeline.emit(run, "agent_reply", agent, said, {"text": reply.text, "turn": n, "ms": reply.ms, "state": reply.state,
                                                         "inputRequired": reply.input_required})
        row = {"turn": n, "say": say, "reply": reply.text, "ms": reply.ms, "state": reply.state}
        mine.append(row)
        with ctx.lock:
            result["turns"].append(dict(row, agent=agent) if multi else row)
            if expect:
                ctx.scenario.setdefault("_expect", []).append(dict(expect, type="answer", target=n, name="%sturn %d" % (agent + " " if multi else "", n),
                                                                    **({"agent": agent} if multi else {})))
        stop = (scenario.get("stop") or {}).get("when")
        if stop and core.cond(stop, {"text": reply.text, "turn": n, "state": reply.state, "inputRequired": reply.input_required})[0]:
            return


def _judge(ctx, result):
    specs = list(ctx.scenario.get("_expect") or []) + list(ctx.scenario.get("checks") or [])
    ctx.scenario.pop("_expect", None)
    rctx = checks.RunCtx(ctx.run, ctx.scenario)
    result["checks"] = checks.evaluate(specs, rctx)
    model = result.get("model") or {}
    if model.get("mode") == "replay" and model.get("misses") and not (ctx.scenario.get("model") or {}).get("allowMisses"):
        result["checks"].append(checks.result({"name": "model: every request answered from the recording", "type": "model"}, "fail",
                                              "%d request(s) were not in the recording %s" % (model["misses"], model.get("cassette"))))
    result["summary"] = {k: sum(1 for c in result["checks"] if c["status"] == k) for k in ("pass", "fail", "no-data", "error")}
    if result.get("error"):
        result["verdict"] = "fail"
    else:
        result["verdict"] = checks.verdict(result["checks"])
    timeline.emit(ctx.run, "check", "", None, {"text": "verdict: %s" % result["verdict"], "summary": result["summary"]})


def start_async(scenario, options, base_host):
    """Run in the background; the caller polls the result by run id."""
    options = dict(options or {})
    options["run"] = options.get("run") or new_run_id(scenario["id"])
    t = threading.Thread(target=run_scenario, args=(scenario, options, base_host), daemon=True)
    t.start()
    return options["run"]


def result_of(run):
    with _LOCK:
        if run in JOBS:
            return dict(JOBS[run], status="running")
    return store().get("results", run)


def junit(result):
    """A JUnit XML report: one testcase per check, which is what a CI system shows."""
    from xml.sax.saxutils import escape, quoteattr
    cases, failures, errors = [], 0, 0
    items = list(result.get("checks") or [])
    if result.get("error"):
        items.insert(0, {"name": "run completed", "status": "error", "detail": result["error"]})
    for c in items:
        name = quoteattr(c.get("name") or c.get("type") or "check")
        body = ""
        if c["status"] == "fail":
            failures += 1
            body = "<failure message=%s>%s</failure>" % (quoteattr(c.get("detail", "")[:300]), escape(str(c.get("evidence") or "")[:2000]))
        elif c["status"] == "no-data":
            failures += 1
            body = "<failure type=\"no-data\" message=%s>no data is not a pass</failure>" % quoteattr(c.get("detail", "")[:300])
        elif c["status"] == "error":
            errors += 1
            body = "<error message=%s/>" % quoteattr(c.get("detail", "")[:300])
        cases.append("<testcase classname=%s name=%s>%s</testcase>" % (quoteattr(result["scenario"]), name, body))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<testsuite name=%s tests="%d" failures="%d" errors="%d" time="%s">\n%s\n</testsuite>\n'
            % (quoteattr(result["scenario"]), len(items), failures, errors, result.get("durationS", 0), "\n".join(cases)))
