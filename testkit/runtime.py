"""What a run changes in the server while it is running, and puts back.

A scenario can bring its own model script. It must apply to that run and no other,
and it must be gone when the run is — a rule left behind would answer somebody
else's request. So run rules live here, in memory, never in the file the console
saves, and the server asks for them first.
"""

import threading

from . import core

_LOCK = threading.Lock()
_RULES = {}                    # run -> [rule]
_FAULTS = {}                   # run -> [fault rule]
_OPTIONS = {}                  # run -> {"unmatched": "route" | "refuse"}
_CLAIM = {}                    # {"run", "agent", "when"} while a run adopts model calls that name no run


def set_run_rules(run, rules):
    """Install a run's rules. A list of problems, empty when they were installed."""
    problems = []
    clean = []
    for i, rule in enumerate(rules or []):
        for p in core.host.validate_rule(rule):
            problems.append("model.rules[%d]: %s" % (i, p))
        if not problems:
            when = dict(rule.get("when") or {})
            ident = dict(when.get("identity") or {}) if isinstance(when.get("identity"), dict) else {}
            ident["run"] = run
            clean.append(dict(rule, when=dict(when, identity=ident)))
    if problems:
        return problems
    with _LOCK:
        _RULES[run] = clean
    return []


def clear_run_rules(run):
    with _LOCK:
        _OPTIONS.pop(run, None)
        return len(_RULES.pop(run, []))


def has_run_rules(run):
    with _LOCK:
        return bool(_RULES.get(run))


def own_rules(run):
    """This run's rules only: what decides whether its script answers a request."""
    with _LOCK:
        return list(_RULES.get(run) or [])


def set_run_options(run, options):
    with _LOCK:
        _OPTIONS[run] = dict(options or {})


def run_option(run, key, default=None):
    with _LOCK:
        return (_OPTIONS.get(run) or {}).get(key, default)


def set_run_faults(run, faults):
    """Model faults for this run only: the same rules /api/faults takes, matched only by
    this run's requests, and gone with it. A list of problems, empty when they were installed."""
    problems, clean = [], []
    for i, rule in enumerate(faults or []):
        rule = dict(rule)
        rule.setdefault("id", "%s:f%d" % (run[:48], i))
        problems += ["model.faults[%d]: %s" % (i, p) for p in core.host.validate_fault_rule(rule)]
        match = dict(rule.get("match") or {})
        ident = dict(match.get("identity") or {}) if isinstance(match.get("identity"), dict) else {}
        ident["run"] = run
        clean.append(dict(rule, match=dict(match, identity=ident)))
    if problems:
        return problems
    with _LOCK:
        _FAULTS[run] = clean
    return []


def clear_run_faults(run):
    with _LOCK:
        gone = _FAULTS.pop(run, [])
    for rule in gone:
        core.host.FAULT_STATE.pop(rule["id"], None)
    return len(gone)


def run_faults():
    """Every run's model faults, tried before the saved ones."""
    with _LOCK:
        return [r for faults in _FAULTS.values() for r in faults]


def run_rules():
    """[(rule, source)] — tried before every other rule."""
    with _LOCK:
        return [(r, "scenario") for rules in _RULES.values() for r in rules]


# -- model calls that name no run --------------------------------------------
#
# An agent that cannot be changed sends its model calls to LLMSim's plain address and says
# nothing about which test they belong to. A run that claims them adopts the ones its condition
# picks out, for as long as it runs. One run at a time: two claiming the same calls could not be
# told apart. The condition matters on a shared LLMSim, where other people's unlabelled calls
# arrive too: {"model": "gpt-5-nano"} adopts only the agent's own.

def set_claim(run, agent, when):
    with _LOCK:
        held = _CLAIM.get("run")
        if held and held != run:
            return ["another run, %s, is adopting model calls that name no run; only one run can at a time" % held]
        _CLAIM.clear()
        _CLAIM.update({"run": run, "agent": agent, "when": dict(when or {})})
    return []


def clear_claim(run):
    with _LOCK:
        if _CLAIM.get("run") == run:
            _CLAIM.clear()


def claim_for(view):
    """(run, agent) of the run that adopts this request, or None."""
    with _LOCK:
        claim = dict(_CLAIM)
    if not claim or not core.host.cond_result(claim["when"], view)[0]:
        return None
    return claim["run"], claim["agent"]
