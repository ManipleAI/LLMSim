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
        return len(_RULES.pop(run, []))


def run_rules():
    """[(rule, source)] — tried before every other rule."""
    with _LOCK:
        return [(r, "scenario") for rules in _RULES.values() for r in rules]
