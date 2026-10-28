"""A scenario: one test of an agent, as a file.

    {
      "id": "refund-over-limit",
      "description": "An order over the limit is handed to a person, not refunded",
      "tags": ["refunds"],
      "agent": {"name": "support", "driver": "http", "url": "https://…", "body": {"input": "{{message}}"}, "reply": "text"},
      "model": {"rules": [ … a script for the model, for this run only … ], "cassette": "refund-run"},
      "setup": [ … seed what the agent's tools will see … ],
      "turns": [ {"say": "I want a refund for order 1182", "expect": {"contains": "manager"}} ],
      "stop": {"when": {"field": "text", "op": "contains", "value": "handed"}, "maxTurns": 6},
      "checks": [ {"type": "path", "mode": "subset", "expected": [ … ]} ],
      "timeoutS": 120
    }

A scenario is data, validated when it is saved and not when it first fails in the
middle of a run. It names its credentials, never holds them.
"""

from . import checks, core, drivers

TOP = {"id", "description", "tags", "agent", "agents", "model", "setup", "teardown", "turns", "user", "stop", "checks", "timeoutS", "notes", "ordering", "repeat"}
SETUP_TYPES = {}                  # type -> (fn(action, run_ctx), validate(action) -> [problems]); phases 2 and 3 register here


def setup_action(kind, validate=None):
    def wrap(fn):
        SETUP_TYPES[kind] = (fn, validate or (lambda action: []))
        return fn
    return wrap


def _agent_problems(agent, where):
    """An agent is described where it is used, or is a saved one named with `use`. Whether a saved
    agent of that name exists is found out when the scenario runs: a scenario is a file, and it may
    be written before the agent is added, or on another machine."""
    if "use" in agent:
        from . import agents
        return agents.validate_reference(agent, where)
    return ["%s: %s" % (where, p) for p in drivers.validate(agent)]


def validate(scenario):
    """Every problem with a scenario, so a person fixes them in one go."""
    if not isinstance(scenario, dict):
        return ["a scenario is an object"]
    problems = ["unknown field %r" % k for k in scenario if k not in TOP]
    if not core.valid_id(scenario.get("id")):
        problems.append("id is required: letters, digits, dot, dash, underscore or colon, up to 64")
    if "description" in scenario and not isinstance(scenario["description"], str):
        problems.append("description is text")
    tags = scenario.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) and 0 < len(t) <= 40 for t in tags):
        problems.append("tags is a list of short words")
    agent, agents = scenario.get("agent"), scenario.get("agents")
    if (agent is None) == (agents is None):
        problems.append("give agent (one agent, with turns) or agents (several, each with its own turns)")
    elif agent is not None:
        if not isinstance(agent, dict):
            problems.append("agent is {name?, driver, …}")
        else:
            if "name" in agent and not core.valid_id(agent["name"]):
                problems.append("agent.name is letters, digits, dot, dash, underscore or colon")
            problems += _agent_problems(agent, "agent")
    else:
        if not isinstance(agents, list) or not 2 <= len(agents) <= 20:
            problems.append("agents is a list of 2 to 20 agents")
        else:
            names = []
            for i, a in enumerate(agents):
                if not isinstance(a, dict):
                    problems.append("agents[%d] is an object" % i)
                    continue
                names.append(a.get("name"))
                if not core.valid_id(a.get("name")):
                    problems.append("agents[%d].name is required: letters, digits, dot, dash, underscore or colon" % i)
                problems += _agent_problems(a, "agents[%d]" % i)
                if not (isinstance(a.get("turns"), list) and a["turns"]) and a.get("user") is None:
                    problems.append("agents[%d] needs turns (what is said to it)" % i)
                for j, t in enumerate(a.get("turns") or []):
                    if not isinstance(t, dict) or not isinstance(t.get("say"), str) or not t["say"]:
                        problems.append("agents[%d].turns[%d] needs say" % (i, j))
            if len(set(names)) != len(names):
                problems.append("agent names must be different")
    turns, user = scenario.get("turns"), scenario.get("user")
    if agents is None and turns is None and user is None:
        problems.append("give turns (what the user says, in order) or user (a simulated user)")
    if agents is not None and (turns is not None or user is not None):
        problems.append("with agents, each agent has its own turns; leave turns and user out")
    if scenario.get("ordering") is not None:
        from . import ordering
        problems += ordering.validate(scenario["ordering"])
    if turns is not None:
        if not isinstance(turns, list) or not turns or len(turns) > 100:
            problems.append("turns is a list of 1 to 100 turns")
        else:
            for i, t in enumerate(turns):
                if not isinstance(t, dict) or not isinstance(t.get("say"), str) or not t["say"]:
                    problems.append("turns[%d] needs say (what the user says)" % i)
                    continue
                if set(t) - {"say", "expect"}:
                    problems.append("turns[%d]: only say and expect" % i)
                if "expect" in t:
                    problems += ["turns[%d].expect: %s" % (i, p) for p in checks.validate(dict(t["expect"], type="answer")) ] if isinstance(t["expect"], dict) else ["turns[%d].expect is an object" % i]
    if user is not None:
        from . import users
        problems += ["user: " + p for p in users.validate(user)]
    stop = scenario.get("stop")
    if stop is not None:
        if not isinstance(stop, dict) or set(stop) - {"when", "maxTurns"}:
            problems.append("stop is {when?, maxTurns?}")
        else:
            if "when" in stop:
                problems += core.check_cond(stop["when"], "stop.when")
            if "maxTurns" in stop and not (isinstance(stop["maxTurns"], int) and 1 <= stop["maxTurns"] <= 100):
                problems.append("stop.maxTurns is a whole number from 1 to 100")
    model = scenario.get("model")
    if model is not None:
        if not isinstance(model, dict) or set(model) - {"rules", "cassette", "mode", "allowMisses", "faults", "unmatched", "claim"}:
            problems.append("model is {rules?, cassette?, mode?, faults?, unmatched?, claim?}")
        else:
            if "rules" in model:
                if not isinstance(model["rules"], list):
                    problems.append("model.rules is a list of rules")
                else:
                    for i, r in enumerate(model["rules"]):
                        problems += ["model.rules[%d]: %s" % (i, p) for p in core.host.validate_rule(r)]
            if "cassette" in model and not (isinstance(model["cassette"], str) and core.host._cassette_name_ok(model["cassette"])):
                problems.append("model.cassette is a recording's name")
            if model.get("mode", "replay") not in ("replay", "record", "auto"):
                problems.append("model.mode is replay, record or auto")
            if model.get("unmatched", "route") not in ("route", "refuse"):
                problems.append("model.unmatched is route (what this run's script does not answer goes where Routing says) or refuse")
            if "unmatched" in model and not model.get("rules"):
                problems.append("model.unmatched says what happens to requests this run's script does not answer: it needs model.rules")
            if "claim" in model:
                if not isinstance(model["claim"], dict) or not model["claim"]:
                    problems.append("model.claim is a condition picking out the model calls that name no run this run adopts, "
                                    "such as {\"model\": \"gpt-5-nano\"}")
                else:
                    problems += core.host.validate_cond(model["claim"], "model.claim")
                if scenario.get("agents"):
                    problems.append("model.claim adopts calls for one agent: it cannot be used with agents")
            if "faults" in model:
                if not isinstance(model["faults"], list) or len(model["faults"]) > 50:
                    problems.append("model.faults is a list of up to 50 fault rules, as /api/faults takes them")
                else:
                    for i, f in enumerate(model["faults"]):
                        problems += ["model.faults[%d]: %s" % (i, p) for p in core.host.validate_fault_rule(dict(f, id=f.get("id", "f%d" % i)) if isinstance(f, dict) else f)]
    for key in ("setup", "teardown"):
        for i, action in enumerate(scenario.get(key) or []):
            if not isinstance(action, dict) or action.get("type") not in SETUP_TYPES:
                problems.append("%s[%d].type is one of %s" % (key, i, ", ".join(sorted(SETUP_TYPES)) or "(none)"))
            else:
                problems += ["%s[%d]: %s" % (key, i, p) for p in SETUP_TYPES[action["type"]][1](action)]
    specs = scenario.get("checks", [])
    if not isinstance(specs, list) or len(specs) > 100:
        problems.append("checks is a list of up to 100 checks")
    else:
        for i, spec in enumerate(specs):
            problems += ["checks[%d]: %s" % (i, p) for p in checks.validate(spec)]
    has_check = specs or any(isinstance(t, dict) and t.get("expect") for t in (turns or [])) or \
        any(isinstance(t, dict) and t.get("expect") for a in (agents or []) if isinstance(a, dict) for t in a.get("turns") or [])
    if not has_check:
        problems.append("a scenario with no checks and no expectations passes nothing: add checks, or expect on a turn")
    if "repeat" in scenario:
        from . import datasets
        problems += datasets.validate_repeat(scenario["repeat"])
    t = scenario.get("timeoutS")
    if t is not None and not (isinstance(t, (int, float)) and 1 <= t <= 3600):
        problems.append("timeoutS is from 1 to 3600")
    return problems
