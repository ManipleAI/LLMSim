"""What a scenario can set up before a run, and take down after it.

  state    seed the records the run's fake backends start with
  mock     bring a mock MCP server or backend for this run only
  faults   bring tool or peer faults for this run only

Everything installed is installed for ONE run: it is found by the run's id, and removed with it.
"""

from . import core, fakes, http_mock, mcp_mock
from .scenario import setup_action


def _validate_state(action):
    seeds = action.get("seed")
    if not isinstance(seeds, dict) or not all(isinstance(v, dict) for v in seeds.values()):
        return ["seed is {collection: {key: record}}"]
    return []


@setup_action("state", _validate_state)
def _state(action, ctx):
    fakes.seed(ctx.run, ctx.agent, action["seed"])
    return []


def _validate_mock(action):
    kind, defn = action.get("kind"), action.get("definition")
    if kind not in ("mcp", "http"):
        return ["kind is mcp or http"]
    return (mcp_mock if kind == "mcp" else http_mock).validate(defn)


@setup_action("mock", _validate_mock)
def _mock(action, ctx):
    module = mcp_mock if action["kind"] == "mcp" else http_mock
    ctx.cleanup.append(module.install_for_run(ctx.run, action["definition"]))
    return []


def _validate_faults(action):
    problems = []
    for i, r in enumerate(action.get("rules") if isinstance(action.get("rules"), list) else []):
        problems += ["rules[%d]: %s" % (i, p) for p in fakes.validate_rule(dict(r, id=r.get("id") or "x%d" % i))]
    if not isinstance(action.get("rules"), list):
        problems.append("rules is a list of fault rules")
    return problems


@setup_action("faults", _validate_faults)
def _faults(action, ctx):
    problems = fakes.set_run_faults(ctx.run, action["rules"])
    if not problems:
        ctx.cleanup.append(lambda: fakes.clear_run_faults(ctx.run))
    return problems
