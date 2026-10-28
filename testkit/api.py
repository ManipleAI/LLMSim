"""The REST surface of the kit, and the doors the server opens for it.

Every area here is a client-visible part of the same API the console uses, so
whatever a person can do on a page a harness can do over HTTP.
"""

from . import core, rest_area, run_route
from . import agents, checks, credentials, drivers, fakes, runner, scenario as scn, store, testruns, timeline  # noqa: F401
from . import a2a_peer, codeeval, datasets, experiments, http_mock, judges, mcp_mock, multi_checks, ordering, otlp, review, setup_actions, state_checks, users  # noqa: F401 — registering routes, checks and setup actions
from .core import ok, fail


def _list_arg(query, name):
    raw = (query.get(name) or [""])[0]
    return [x for x in raw.split(",") if x] or None


def _int(query, name, default):
    try:
        return int((query.get(name) or [default])[0])
    except (TypeError, ValueError):
        return default


@rest_area("kit")
def _kit(method, rest, body, query, host):
    st = store.store()
    if method == "GET" and not rest:
        return ok({"retention": st.retention(), "timeline": timeline.settings(), "store": st.path, "kinds": list(timeline.KINDS)})
    if method == "PUT" and not rest:
        body = body or {}
        if "retention" in body:
            r = body["retention"]
            if not isinstance(r, dict) or any(k not in store.DEFAULT_RETENTION or not isinstance(v, int) or v < 1 for k, v in r.items()):
                return fail(400, "retention is {maxRuns, maxEventsPerRun, days}, each a positive whole number.")
            st.put("settings", "retention", dict(st.retention(), **r))
        if "timeline" in body:
            t = body["timeline"]
            if not isinstance(t, dict) or set(t) - {"recordAll"} or not isinstance(t.get("recordAll", False), bool):
                return fail(400, "timeline is {recordAll: true|false}.")
            st.put("settings", "timeline", dict(timeline.settings(), **t))
        return _kit("GET", [], None, query, host)
    return fail(404, "No such route: %s /api/kit/%s" % (method, "/".join(rest)))


@run_route("timeline")
def _timeline(method, run, rest, body, query):
    if method != "GET":
        return fail(405, "A timeline is read, not written. Add to it with POST /api/runs/%s/events." % run)
    if not core.valid_id(run):
        return fail(400, "That is not a run id.")
    out = timeline.timeline(run, kinds=_list_arg(query, "kinds"), agent=(query.get("agent") or [None])[0],
                            since=_int(query, "since", 0), limit=_int(query, "limit", 0) or None)
    if not out["events"] and not store.store().run(run):
        return fail(404, "Nothing is kept for run %s." % run)
    return ok(out)


@run_route("events")
def _events(method, run, rest, body, query):
    if not core.valid_id(run):
        return fail(400, "That is not a run id.")
    if method == "GET":
        return _timeline("GET", run, rest, body, query)
    if method == "POST":
        body = body or {}
        kind = body.get("kind")
        if kind not in timeline.KINDS:
            return fail(400, "kind is one of %s." % ", ".join(timeline.KINDS))
        agent = body.get("agent", "")
        if agent and not core.valid_id(agent):
            return fail(400, "agent is letters, digits, dot, dash, underscore or colon.")
        if not isinstance(body.get("data", {}), dict):
            return fail(400, "data is an object.")
        parent = body.get("parent")
        if parent is not None and not isinstance(parent, int):
            return fail(400, "parent is the id of an earlier event.")
        event_id = timeline.emit(run, kind, agent, parent, body.get("data") or {}, body.get("at"))
        if event_id is None:
            return fail(409, "The event was not kept: the run is full (see /api/kit retention) or the id is not one.")
        return ok({"id": event_id})
    return fail(405, "GET lists a run's events; POST adds one.")


@run_route("stored")
def _stored(method, run, rest, body, query):
    if method == "DELETE":
        return ok({"removed": store.store().delete_run(run)})
    return fail(405, "DELETE removes what is kept for a run.")


# -- credentials ---------------------------------------------------------------

@rest_area("credentials")
def _credentials(method, rest, body, query, host):
    if method == "GET" and not rest:
        return ok({"credentials": credentials.listing(), "types": {k: list(v) for k, v in credentials.TYPES.items()}})
    if method == "PUT" and len(rest) == 1:
        problems = credentials.put(rest[0], body or {})
        if problems:
            return fail(400, "; ".join(problems))
        return ok({"credentials": credentials.listing()}, message="Stored %s. Its value is never shown again." % rest[0])
    if method == "DELETE" and len(rest) == 1:
        if not credentials.drop(rest[0]):
            return fail(404, "There is no credential called %s." % rest[0])
        return ok({"credentials": credentials.listing()})
    return fail(404, "No such route: %s /api/credentials/%s" % (method, "/".join(rest)))


# -- what exists: drivers and checks ---------------------------------------------

@rest_area("drivers")
def _drivers(method, rest, body, query, host):
    if method == "GET" and not rest:
        return ok({"drivers": [{"name": n, "required": list(d.required), "doc": (d.__doc__ or "").strip()} for n, d in sorted(drivers.DRIVERS.items())],
                   "processAllowed": drivers.process_allowed()})
    if method == "POST" and rest == ["validate"]:
        problems = drivers.validate(body or {})
        return ok(valid=not problems, problems=problems)
    return fail(404, "No such route: %s /api/drivers/%s" % (method, "/".join(rest)))


@rest_area("checks")
def _checks(method, rest, body, query, host):
    if method == "GET" and not rest:
        return ok({"types": sorted(checks.CHECKS), "statuses": ["pass", "fail", "no-data", "error"]})
    if method == "POST" and rest == ["validate"]:
        problems = checks.validate((body or {}).get("check"))
        return ok(valid=not problems, problems=problems)
    if method == "POST" and rest == ["evaluate"]:
        # Judge checks over a run that already happened — what to reach for when a rule is being written.
        body = body or {}
        run, specs = body.get("run"), body.get("checks")
        if not core.valid_id(run) or not isinstance(specs, list) or not specs or len(specs) > 100:
            return fail(400, "Give run (a run id) and checks (a list of 1 to 100).")
        problems = [p for i, s in enumerate(specs) for p in ["checks[%d]: %s" % (i, q) for q in checks.validate(s)]]
        if problems:
            return fail(400, "; ".join(problems[:5]), problems=problems)
        results = checks.evaluate(specs, checks.RunCtx(run))
        return ok({"verdict": checks.verdict(results), "results": results})
    return fail(404, "No such route: %s /api/checks/%s" % (method, "/".join(rest)))


# -- scenarios ------------------------------------------------------------------

def _scenario_listing():
    return [{"id": i, "description": b.get("description", ""), "tags": b.get("tags", []), "driver": (b.get("agent") or {}).get("driver"),
             "agent": (b.get("agent") or {}).get("name", "agent"), "uses": agents.uses(b), "several": isinstance(b.get("agents"), list),
             "checks": len(b.get("checks") or []), "updated": u}
            for i, b, u in store.store().docs("scenarios")]


def _options(body, query):
    options = {}
    for key in ("run", "tier", "label", "seed", "keepState", "agent", "order"):
        if (body or {}).get(key) is not None:
            options[key] = body[key]
    return options


def _check_options(options):
    if "run" in options and not core.valid_id(options["run"]):
        return "run is a run id"
    if options.get("tier", "simulated") not in ("simulated", "real"):
        return "tier is simulated or real"
    if "keepState" in options and not isinstance(options["keepState"], bool):
        return "keepState is true or false"
    if "order" in options and not (isinstance(options["order"], list) and options["order"] and all(isinstance(s, str) for s in options["order"])):
        return "order is a list of the scenario's ordering steps"
    agent = options.get("agent")
    if agent is not None and not (core.valid_id(agent) or (isinstance(agent, dict) and agent and all(core.valid_id(v) for v in agent.values()))):
        return "agent is the id of a saved agent, or {name: id} for a scenario that drives several"
    return None


@rest_area("scenarios")
def _scenarios(method, rest, body, query, host):
    st = store.store()
    if method == "GET" and not rest:
        return ok({"scenarios": _scenario_listing()})
    if method == "POST" and rest == ["validate"]:
        problems = scn.validate((body or {}).get("scenario"))
        return ok(valid=not problems, problems=problems)
    if method == "POST" and rest == ["run"]:
        scenario = (body or {}).get("scenario")
        problems = scn.validate(scenario)
        if problems:
            return fail(400, "That scenario has %d problem(s): %s" % (len(problems), "; ".join(problems[:5])), problems=problems)
        return _execute(scenario, body, host)
    if method == "POST" and not rest:
        scenario = body or {}
        problems = scn.validate(scenario)
        if problems:
            return fail(400, "That scenario has %d problem(s): %s" % (len(problems), "; ".join(problems[:5])), problems=problems)
        st.put("scenarios", scenario["id"], scenario)
        return ok({"scenario": scenario})
    if len(rest) >= 1 and core.valid_id(rest[0]):
        sid = rest[0]
        found = st.get("scenarios", sid)
        if method == "GET" and len(rest) == 1:
            return ok({"scenario": found}) if found else fail(404, "There is no scenario called %s." % sid)
        if method == "PUT" and len(rest) == 1:
            scenario = dict(body or {}, id=sid)
            problems = scn.validate(scenario)
            if problems:
                return fail(400, "That scenario has %d problem(s): %s" % (len(problems), "; ".join(problems[:5])), problems=problems)
            st.put("scenarios", sid, scenario)
            return ok({"scenario": scenario})
        if method == "DELETE" and len(rest) == 1:
            return ok(removed=st.drop("scenarios", sid)) if found else fail(404, "There is no scenario called %s." % sid)
        if method == "POST" and rest[1:] == ["run"]:
            if not found:
                return fail(404, "There is no scenario called %s." % sid)
            return _execute(found, body, host)
    return fail(404, "No such route: %s /api/scenarios/%s" % (method, "/".join(rest)))


def _execute(scenario, body, host):
    options = _options(body, None)
    problem = _check_options(options)
    if problem:
        return fail(400, problem)
    if (body or {}).get("wait") is False:
        run = runner.start_async(scenario, options, host)
        return 202, {"ok": True, "run": run, "status": "running", "message": "Poll GET /api/results/%s." % run}
    result = runner.run_scenario(scenario, options, host)
    return ok({"result": result})


# -- results --------------------------------------------------------------------

@rest_area("results")
def _results(method, rest, body, query, host):
    st = store.store()
    if method == "GET" and not rest:
        rows = []
        for run, doc, updated in reversed(st.docs("results")):
            rows.append({"run": run, "scenario": doc.get("scenario"), "verdict": doc.get("verdict"), "status": doc.get("status"),
                         "tier": doc.get("tier"), "summary": doc.get("summary"), "durationS": doc.get("durationS"),
                         "finishedAt": doc.get("finishedAt"), "label": doc.get("label", ""), "agents": doc.get("agentRefs") or [],
                         "testrun": doc.get("testrun", "")})
        want, on = (query.get("scenario") or [None])[0], (query.get("agent") or [None])[0]
        rows = [r for r in rows if (not want or r["scenario"] == want) and (not on or on in r["agents"])]
        if (query.get("latest") or [None])[0] == "scenario":
            # each scenario's newest result, however long ago it ran: the newest hundred of all
            # results leave out every scenario not run lately, and it shows as never run
            newest = {}
            for r in rows:
                if r["scenario"] and (r["scenario"] not in newest or (r["finishedAt"] or 0) > (newest[r["scenario"]]["finishedAt"] or 0)):
                    newest[r["scenario"]] = r
            return ok({"results": sorted(newest.values(), key=lambda r: -(r["finishedAt"] or 0))[:_int(query, "limit", 10000)]})
        return ok({"results": rows[:_int(query, "limit", 100)]})
    if method == "GET" and len(rest) >= 1:
        result = runner.result_of(rest[0])
        if not result:
            return fail(404, "There is no result for run %s." % rest[0])
        if rest[1:] == ["junit"]:
            return 200, {"ok": True, "junit": runner.junit(result)}, {"raw": runner.junit(result), "content_type": "application/xml"}
        return ok({"result": result})
    if method == "DELETE" and len(rest) == 1:
        return ok(removed=st.drop("results", rest[0]))
    return fail(404, "No such route: %s /api/results/%s" % (method, "/".join(rest)))


# -- faults on tools and peers, and what a run's fake backends hold -----------------------------

@rest_area("kitfaults")
def _kitfaults(method, rest, body, query, host):
    st = store.store()
    if method == "GET" and not rest:
        counts = fakes.counters()
        return ok({"faults": [dict(d, **counts.get(i, {"seen": 0, "fired": 0})) for i, d, _ in st.docs("faults")], "runFaults": fakes.run_fault_listing(),
                   "types": fakes.KIT_FAULTS,
                   "tool": sorted(fakes.TOOL_FAULTS), "peer": sorted(fakes.PEER_FAULTS), "paused": core.host.CONFIG["chaos"]["paused"]})
    if method == "POST" and rest == ["validate"]:
        problems = fakes.validate_rule((body or {}).get("rule"))
        return ok(valid=not problems, problems=problems)
    if method == "POST" and rest == ["reseed"]:
        fakes.reseed((body or {}).get("seed"))
        return ok(seed=(body or {}).get("seed"))
    if len(rest) == 1 and core.valid_id(rest[0]):
        if method == "PUT":
            rule = dict((body or {}).get("rule") or body or {}, id=rest[0])
            problems = fakes.validate_rule(rule)
            if problems:
                return fail(400, "That fault has %d problem(s): %s" % (len(problems), "; ".join(problems[:5])), problems=problems)
            st.put("faults", rest[0], rule)
            fakes._STATE.pop(rest[0], None)
            return ok({"rule": rule})
        if method == "DELETE":
            fakes._STATE.pop(rest[0], None)
            return ok(removed=st.drop("faults", rest[0])) if st.get("faults", rest[0]) is not None else fail(404, "There is no fault called %s." % rest[0])
    return fail(404, "No such route: %s /api/kitfaults/%s" % (method, "/".join(rest)))


@rest_area("presets")
def _presets(method, rest, body, query, host):
    if method == "GET" and not rest:
        return ok({"injections": fakes.INJECTIONS, "use": 'on a tool or route: "inject": {"preset": "<name>"}; catch it being obeyed with a leak check on its marker'})
    return fail(404, "No such route: %s /api/presets/%s" % (method, "/".join(rest)))


@run_route("state")
def _run_state(method, run, rest, body, query):
    if not core.valid_id(run):
        return fail(400, "That is not a run id.")
    if method == "GET":
        snap = fakes.snapshot(run)
        if not snap:
            held = (store.store().get("results", run) or {}).get("state")
            if held is not None:
                return ok({"state": held, "from": "the result of the run"})
        return ok({"state": snap, "from": "live"})
    if method == "PUT":
        if not isinstance(body, dict) or not all(isinstance(v, dict) for v in body.values()):
            return fail(400, "The body is {collection: {key: record}}.")
        fakes.seed(run, "", body)
        return ok({"state": fakes.snapshot(run)})
    if method == "DELETE":
        return ok(removed=fakes.purge(run))
    return fail(405, "GET reads a run's state, PUT seeds it, DELETE clears it.")


@rest_area("interleavings")
def _interleavings(method, rest, body, query, host):
    """Run one scenario in several orders and say whether the order changed the outcome."""
    if method == "POST" and len(rest) == 1 and core.valid_id(rest[0]):
        scenario = store.store().get("scenarios", rest[0])
        return _interleave(scenario, body or {}, host) if scenario else fail(404, "There is no scenario called %s." % rest[0])
    if method == "POST" and not rest:
        scenario = (body or {}).get("scenario")
        problems = scn.validate(scenario)
        if problems:
            return fail(400, "That scenario has %d problem(s): %s" % (len(problems), "; ".join(problems[:5])), problems=problems)
        return _interleave(scenario, body or {}, host)
    return fail(404, "No such route: %s /api/interleavings/%s" % (method, "/".join(rest)))


def _interleave(scenario, body, host):
    spec = scenario.get("ordering")
    orders = body.get("orders")
    if not spec:
        return fail(400, "That scenario has no ordering. Add ordering.steps, then name the orders to try.")
    if not (isinstance(orders, list) and 2 <= len(orders) <= 12 and all(isinstance(o, list) for o in orders)):
        return fail(400, "orders is a list of 2 to 12 orders, each a list of step names.")
    compare = body.get("compare") or ["state"]
    if not set(compare) <= {"state", "replies"}:
        return fail(400, "compare is state and/or replies.")
    runs = []
    for order in orders:
        result = runner.run_scenario(scenario, dict(_options(body, None), order=order, run=None), host)
        replies = {}
        for t in result.get("turns") or []:
            replies.setdefault(t.get("agent", "agent"), []).append(t["reply"])
        runs.append({"order": order, "run": result["run"], "verdict": result["verdict"], "state": result.get("state"), "replies": replies,
                     "timedOut": (result.get("model") or {}).get("orderTimedOut") or []})
    differences = []
    for key in compare:
        base = runs[0][key]
        for r in runs[1:]:
            if r[key] != base:
                differences.append({"what": key, "between": [runs[0]["order"], r["order"]], "first": base, "second": r[key]})
    race = bool(differences)
    verdict = "fail" if race or any(r["verdict"] != "pass" for r in runs) else "pass"
    return ok({"verdict": verdict, "race": race, "differences": differences[:6], "orders": runs})
