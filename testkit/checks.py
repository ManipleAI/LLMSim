"""Checks: what a run must have done, said and cost.

A check reads a run — its timeline, the replies, the calls — and returns a verdict:

  pass       it held
  fail       it did not, and the detail says where
  no-data    there was nothing to judge. An agent that never ran satisfies every
             "must not"; saying so would be a lie, so it is not a pass
  error      the check itself could not be evaluated

The checks here are about the answer, the path the agent took, and the budget it
spent. Later phases add the state a run left behind and what passed between agents;
they register into the same table, so one scenario mixes them freely.

A check that LLMSim already has a language for is not a new language: `run` and
`assertions` hand the run to the server's own condition language and its assertions.
"""

import json
import re

from . import core
from .store import store

CHECKS = {}              # type -> (evaluate(spec, ctx) -> dict, validate(spec) -> [problems])


def check(kind, validate=None):
    def wrap(fn):
        CHECKS[kind] = (fn, validate or (lambda spec: []))
        return fn
    return wrap


def result(spec, status, detail, evidence=None, **extra):
    return dict({"name": spec.get("name") or spec.get("type"), "type": spec.get("type"), "status": status, "detail": detail,
                 "evidence": evidence or []}, **extra)


class RunCtx:
    """A run, read once and offered to every check in the shapes checks want it."""

    def __init__(self, run, scenario=None, results=None):
        self.run, self.scenario, self.events = run, scenario or {}, store().events(run)
        self.results = results or {}

    def of(self, *kinds, agent=None):
        return [e for e in self.events if e["kind"] in kinds and (not agent or e["agent"] == agent)]

    @property
    def replies(self):
        return [e["data"].get("text", "") for e in self.of("agent_reply")]

    @property
    def final(self):
        r = self.replies
        return r[-1] if r else None

    def requested_calls(self, agent=None):
        out = []
        for e in self.of("model_call", agent=agent):
            for c in (e["data"].get("response") or {}).get("toolCalls") or []:
                out.append({"tool": c.get("name"), "arguments": c.get("input") or {}, "agent": e["agent"], "event": e["id"]})
        return out

    def tool_calls(self, agent=None):
        return [{"tool": e["data"].get("tool"), "arguments": e["data"].get("arguments") or {}, "agent": e["agent"], "event": e["id"],
                 "error": e["data"].get("error")} for e in self.of("tool_call", agent=agent)]

    def usage(self, agent=None):
        total = {"input": 0, "output": 0, "cacheRead": 0, "reasoning": 0}
        for e in self.of("model_call", agent=agent):
            u = (e["data"].get("response") or {}).get("usage") or {}
            for k in total:
                total[k] += int(u.get(k) or 0)
        total["total"] = total["input"] + total["output"]
        return total

    @property
    def state(self):
        """The records the run's fake backends held when it was judged, {collection: {key: record}}."""
        if "state" not in self.__dict__:
            from . import fakes
            self.__dict__["state"] = fakes.snapshot(self.run)
        return self.__dict__["state"]

    def view(self):
        counts = {}
        for e in self.events:
            counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        span = (self.events[-1]["at"] - self.events[0]["at"]) if len(self.events) > 1 else 0
        return {"run": self.run, "agents": sorted({e["agent"] for e in self.events if e["agent"]}), "counts": counts,
                "replies": self.replies, "final": self.final or "", "turns": len(self.of("user_turn")),
                "modelCalls": counts.get("model_call", 0), "toolsCalled": [c["tool"] for c in self.requested_calls()],
                "toolEvents": [c["tool"] for c in self.tool_calls()], "usage": self.usage(), "durationS": round(span, 3),
                "peerMessages": counts.get("peer_message", 0), "states": counts.get("state_change", 0)}


# -- the answer -----------------------------------------------------------------

def _type_ok(value, want):
    kinds = {"string": str, "boolean": bool, "array": list, "object": dict, "null": type(None)}
    if want == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if want == "integer":
        return isinstance(value, int) and not isinstance(value, bool) or (isinstance(value, float) and value.is_integer())
    return isinstance(value, kinds[want])


def schema_errors(value, schema, path="$"):
    """Problems with a value against the part of JSON Schema that agents' answers use."""
    if not isinstance(schema, dict):
        return ["%s: the schema is not an object" % path]
    errors = []
    t = schema.get("type")
    if t is not None:
        wanted = t if isinstance(t, list) else [t]
        if not any(_type_ok(value, w) for w in wanted):
            return ["%s: expected %s, got %s" % (path, "/".join(wanted), type(value).__name__)]
    if "enum" in schema and value not in schema["enum"]:
        errors.append("%s: %r is not one of %r" % (path, value, schema["enum"]))
    if "const" in schema and value != schema["const"]:
        errors.append("%s: %r is not %r" % (path, value, schema["const"]))
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append("%s: shorter than %d" % (path, schema["minLength"]))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append("%s: longer than %d" % (path, schema["maxLength"]))
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append("%s: does not match %s" % (path, schema["pattern"]))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for key, op in (("minimum", lambda a, b: a >= b), ("maximum", lambda a, b: a <= b),
                        ("exclusiveMinimum", lambda a, b: a > b), ("exclusiveMaximum", lambda a, b: a < b)):
            if key in schema and not op(value, schema[key]):
                errors.append("%s: %s violates %s %s" % (path, value, key, schema[key]))
    if isinstance(value, dict):
        for key in schema.get("required") or []:
            if key not in value:
                errors.append("%s: missing %s" % (path, key))
        props = schema.get("properties") or {}
        for key, sub in props.items():
            if key in value:
                errors += schema_errors(value[key], sub, "%s.%s" % (path, key))
        if schema.get("additionalProperties") is False:
            errors += ["%s: unexpected %s" % (path, k) for k in value if k not in props]
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append("%s: fewer than %d items" % (path, schema["minItems"]))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append("%s: more than %d items" % (path, schema["maxItems"]))
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                errors += schema_errors(item, schema["items"], "%s[%d]" % (path, i))
    for sub in schema.get("allOf") or []:
        errors += schema_errors(value, sub, path)
    if schema.get("anyOf") and not any(not schema_errors(value, s, path) for s in schema["anyOf"]):
        errors.append("%s: matches none of anyOf" % path)
    if schema.get("oneOf") and sum(1 for s in schema["oneOf"] if not schema_errors(value, s, path)) != 1:
        errors.append("%s: does not match exactly one of oneOf" % path)
    if "not" in schema and not schema_errors(value, schema["not"], path):
        errors.append("%s: matches what 'not' forbids" % path)
    return errors


def json_in(text):
    """The JSON an answer holds: the whole text, a fenced block, or the first {...}/[...]."""
    text = (text or "").strip()
    for candidate in (text, *re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)):
        try:
            return json.loads(candidate.strip())
        except ValueError:
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        a, b = text.find(opener), text.rfind(closer)
        if 0 <= a < b:
            try:
                return json.loads(text[a:b + 1])
            except ValueError:
                pass
    raise ValueError("no JSON in the answer")


TEXT_KEYS = ("contains", "not_contains", "regex", "not_regex", "equals", "not_equals", "empty", "case")


def validate_answer(spec):
    problems = []
    target = spec.get("target", "final")
    if target not in ("final", "any", "every") and not (isinstance(target, int) and not isinstance(target, bool) and target >= 1):
        problems.append("target is final, any, every, or a turn number from 1")
    if not any(k in spec for k in TEXT_KEYS + ("schema", "number")):
        problems.append("say what the answer must be: contains, equals, regex, schema, number …")
    for k in ("regex", "not_regex"):
        if isinstance(spec.get(k), str):
            try:
                re.compile(spec[k])
            except re.error as exc:
                problems.append("%s is not a valid pattern: %s" % (k, exc))
    if "number" in spec:
        n = spec["number"]
        if not isinstance(n, dict) or not set(n) <= {"regex", "group", "equals", "tolerance", "min", "max"}:
            problems.append("number is {regex?, group?, equals?, tolerance?, min?, max?}")
    return problems


def _answer_one(spec, text):
    """None when one reply satisfies the spec, else why not."""
    text_spec = {k: spec[k] for k in TEXT_KEYS if k in spec}
    if text_spec:
        ok, why = core.host._text_ok(text_spec, text, "the answer")
        if not ok:
            return why
    if "schema" in spec:
        try:
            value = json_in(text)
        except ValueError as exc:
            return "the answer is not JSON: %s" % exc
        errors = schema_errors(value, spec["schema"])
        if errors:
            return "; ".join(errors[:3])
    if "number" in spec:
        n = spec["number"]
        pattern = n.get("regex") or r"-?\d[\d,]*(?:\.\d+)?"
        m = re.search(pattern, text)
        if not m:
            return "no number matching %s in the answer" % pattern
        try:
            got = float(m.group(n.get("group", 1 if m.groups() else 0)).replace(",", ""))
        except (ValueError, IndexError):
            return "the match is not a number: %r" % m.group(0)
        if "equals" in n and abs(got - n["equals"]) > n.get("tolerance", 0):
            return "the number is %s, wanted %s ± %s" % (got, n["equals"], n.get("tolerance", 0))
        if "min" in n and got < n["min"]:
            return "the number is %s, below %s" % (got, n["min"])
        if "max" in n and got > n["max"]:
            return "the number is %s, above %s" % (got, n["max"])
    return None


@check("answer", validate_answer)
def check_answer(spec, ctx):
    replies = [e for e in ctx.of("agent_reply", agent=spec.get("agent"))]
    if not replies:
        return result(spec, "no-data", "the agent never replied, so there is no answer to judge")
    target = spec.get("target", "final")
    if target == "final":
        picked = [replies[-1]]
    elif isinstance(target, int):
        if target > len(replies):
            return result(spec, "fail", "there were %d replies; turn %d never got one" % (len(replies), target))
        picked = [replies[target - 1]]
    else:
        picked = replies
    bad = [(e, _answer_one(spec, e["data"].get("text", ""))) for e in picked]
    if target == "any":
        if any(why is None for _, why in bad):
            return result(spec, "pass", "a reply satisfied it")
        return result(spec, "fail", "none of %d reply(ies) satisfied it; the last: %s" % (len(bad), bad[-1][1]),
                      [{"reply": bad[-1][0]["data"].get("text", "")[:300]}])
    fails = [(e, why) for e, why in bad if why]
    if fails:
        e, why = fails[0]
        return result(spec, "fail", why, [{"reply": e["data"].get("text", "")[:300], "turn": e["data"].get("turn")}])
    return result(spec, "pass", "%d reply(ies) satisfied it" % len(picked))


# -- the path ---------------------------------------------------------------------

def _value_ok(want, got):
    if isinstance(want, dict) and len(want) == 1:
        (k, v), = want.items()
        if k == "$any":
            return True
        if k == "$regex":
            return isinstance(got, str) and re.search(v, got) is not None
        if k == "$type":
            return _type_ok(got, v)
    if isinstance(want, dict) and isinstance(got, dict):
        return set(want) == set(got) and all(_value_ok(want[k], got[k]) for k in want)
    if isinstance(want, list) and isinstance(got, list):
        return len(want) == len(got) and all(_value_ok(a, b) for a, b in zip(want, got))
    return want == got


def args_match(expected, actual, mode):
    if mode == "ignore":
        return True
    exp, act = expected or {}, actual or {}
    if mode == "subset":
        return all(k in act and _value_ok(v, act[k]) for k, v in exp.items())
    return set(exp) == set(act) and all(_value_ok(exp[k], act[k]) for k in exp)


def call_matches(exp, got, spec):
    if exp.get("tool") != got.get("tool"):
        return False
    mode = exp.get("match") or (spec.get("argsOverrides") or {}).get(exp["tool"]) or spec.get("args", "exact")
    return args_match(exp.get("arguments"), got.get("arguments"), mode)


def describe_call(c):
    return "%s(%s)" % (c.get("tool"), json.dumps(c.get("arguments") or {}, sort_keys=True)[:80])


def validate_path(spec):
    problems = []
    if spec.get("mode", "strict") not in ("strict", "unordered", "subset", "superset"):
        problems.append("mode is strict, unordered, subset or superset")
    if spec.get("source", "model") not in ("model", "tools"):
        problems.append("source is model (calls the model asked for) or tools (calls a tool received)")
    exp = spec.get("expected")
    if not isinstance(exp, list) or not all(isinstance(c, dict) and isinstance(c.get("tool"), str) for c in exp):
        problems.append("expected is a list of {tool, arguments?, match?}")
    for mode in [spec.get("args", "exact")] + list((spec.get("argsOverrides") or {}).values()):
        if mode not in ("exact", "subset", "ignore"):
            problems.append("an arguments mode is exact, subset or ignore")
    return problems


def compare_paths(mode, expected, actual, spec):
    """(ok, why) for the four ways of comparing what an agent did with a reference."""
    if mode == "strict":
        for i in range(max(len(expected), len(actual))):
            if i >= len(actual):
                return False, "the agent stopped after %d call(s); call %d should have been %s" % (len(actual), i + 1, describe_call(expected[i]))
            if i >= len(expected):
                return False, "call %d, %s, was not expected" % (i + 1, describe_call(actual[i]))
            if not call_matches(expected[i], actual[i], spec):
                return False, "call %d was %s, expected %s" % (i + 1, describe_call(actual[i]), describe_call(expected[i]))
        return True, "%d call(s), in the expected order" % len(actual)
    if mode == "unordered":
        pool = list(actual)
        for e in expected:
            hit = next((a for a in pool if call_matches(e, a, spec)), None)
            if hit is None:
                return False, "%s was never called" % describe_call(e)
            pool.remove(hit)
        if pool:
            return False, "%s was called and was not expected" % describe_call(pool[0])
        return True, "the same %d call(s), in any order" % len(actual)
    if mode == "subset":
        for a in actual:
            if not any(call_matches(e, a, spec) for e in expected):
                return False, "%s is outside the reference" % describe_call(a)
        return True, "every call is in the reference"
    for e in expected:
        if not any(call_matches(e, a, spec) for a in actual):
            return False, "%s was never called" % describe_call(e)
    return True, "every reference call was made"


@check("path", validate_path)
def check_path(spec, ctx):
    source = spec.get("source", "model")
    actual = ctx.tool_calls(spec.get("agent")) if source == "tools" else ctx.requested_calls(spec.get("agent"))
    if not actual and not ctx.of("model_call", "tool_call", agent=spec.get("agent")):
        return result(spec, "no-data", "LLMSim saw nothing from this agent, so no path can be judged — is it pointed at LLMSim?")
    ok, why = compare_paths(spec.get("mode", "strict"), spec["expected"], actual, spec)
    return result(spec, "pass" if ok else "fail", why, [{"actual": [describe_call(c) for c in actual][:20]}] if not ok else [])


# -- the budget -------------------------------------------------------------------

def validate_budget(spec):
    problems = []
    known = {"tokens": {"input", "output", "total"}, "latencyMs": {"run", "modelCall"}, "steps": {"modelCalls", "toolCalls", "total"},
             "cost": {"max", "prices"}}
    for key, sub in known.items():
        if key in spec:
            if not isinstance(spec[key], dict) or set(spec[key]) - sub:
                problems.append("%s is {%s}" % (key, ", ".join(sorted(sub))))
    if not any(k in spec for k in known):
        problems.append("a budget limits one of: tokens, cost, latencyMs, steps")
    if "cost" in spec and isinstance(spec["cost"], dict) and not spec["cost"].get("prices"):
        problems.append("cost needs prices: {model: {input, output}} in currency per million tokens")
    if spec.get("scope", "run") not in ("run", "agent"):
        problems.append("scope is run or agent")
    return problems


def _cost(ctx, agent, prices):
    total, missing = 0.0, set()
    for e in ctx.of("model_call", agent=agent):
        u = (e["data"].get("response") or {}).get("usage") or {}
        p = prices.get(e["data"].get("model")) or prices.get("*")
        if not p:
            missing.add(e["data"].get("model"))
            continue
        total += (u.get("input", 0) * p.get("input", 0) + u.get("output", 0) * p.get("output", 0)) / 1e6
    return total, missing


@check("budget", validate_budget)
def check_budget(spec, ctx):
    if not ctx.events:
        return result(spec, "no-data", "the run has no events, so nothing was spent")
    scopes = [None] if spec.get("scope", "run") == "run" else sorted({e["agent"] for e in ctx.events if e["agent"]})
    if not scopes:
        return result(spec, "no-data", "no agent is named in the run's events, so a per-agent budget has nothing to read")
    problems = []
    for agent in scopes:
        who = ("agent %s: " % agent) if agent else ""
        events = [e for e in ctx.events if not agent or e["agent"] == agent]
        for key, limit in (spec.get("tokens") or {}).items():
            used = ctx.usage(agent)[key]
            if used > limit:
                problems.append("%s%s tokens %d over the limit of %d" % (who, key, used, limit))
        mc = ctx.of("model_call", agent=agent)
        for key, limit in (spec.get("steps") or {}).items():
            n = {"modelCalls": len(mc), "toolCalls": len(ctx.of("tool_call", agent=agent))}.get(key, None)
            if key == "total":
                n = len(mc) + len(ctx.of("tool_call", agent=agent))
            if n > limit:
                problems.append("%s%d %s over the limit of %d" % (who, n, key, limit))
        lat = spec.get("latencyMs") or {}
        if "run" in lat and events and (events[-1]["at"] - events[0]["at"]) * 1000 > lat["run"]:
            problems.append("%sthe run took %dms, limit %d" % (who, (events[-1]["at"] - events[0]["at"]) * 1000, lat["run"]))
        if "modelCall" in lat:
            slow = [e["data"].get("ms") or 0 for e in mc if (e["data"].get("ms") or 0) > lat["modelCall"]]
            if slow:
                problems.append("%s%d model call(s) slower than %dms (slowest %dms)" % (who, len(slow), lat["modelCall"], max(slow)))
        if spec.get("cost"):
            spent, missing = _cost(ctx, agent, spec["cost"]["prices"])
            if missing:
                return result(spec, "error", "no price for model(s) %s; add them to cost.prices (or \"*\")" % ", ".join(sorted(map(str, missing))))
            if spent > spec["cost"].get("max", float("inf")):
                problems.append("%scost %.4f over the limit of %s" % (who, spent, spec["cost"]["max"]))
    if problems:
        return result(spec, "fail", "; ".join(problems[:4]))
    return result(spec, "pass", "within every limit")


# -- anything the condition language can say ---------------------------------------

def validate_run_check(spec):
    if not isinstance(spec.get("cond"), dict):
        return ["cond is a condition over the run (see GET /api/assertions/catalog; fields: counts.model_call, final, usage.total, …)"]
    return core.check_cond(spec["cond"], "cond")


@check("run", validate_run_check)
def check_run(spec, ctx):
    if not ctx.events:
        return result(spec, "no-data", "the run has no events to judge")
    ok, why = core.cond(spec["cond"], ctx.view())
    return result(spec, "pass" if ok else "fail", "it held" if ok else why)


def validate_assertions_check(spec):
    if not (spec.get("set") or isinstance(spec.get("assertions"), list)):
        return ["give set (a saved assertion set) or assertions (a list)"]
    return []


@check("assertions", validate_assertions_check)
def check_assertions(spec, ctx):
    host = core.host
    scope = {"identity": {"run": ctx.run}}
    if spec.get("set"):
        found = next((s for s in host.ASSERTION_SETS["sets"] if s["id"] == spec["set"]), None)
        if not found:
            return result(spec, "error", "there is no assertion set called %s" % spec["set"])
        report = host.evaluate_assertions(found.get("assertions", []), scope, found.get("scope"))
    else:
        report = host.evaluate_assertions(spec["assertions"], scope)
    status = {"pass": "pass", "fail": "fail", "no-data": "no-data"}.get(report["verdict"], "error")
    bad = [r for r in report["results"] if r["status"] != "pass"]
    return result(spec, status, "%d assertion(s): %s" % (len(report["results"]), ", ".join("%s %d" % kv for kv in report["summary"].items() if kv[1])),
                  [{"assertion": r.get("name") or r.get("id"), "status": r["status"], "detail": r.get("detail")} for r in bad[:5]])


# -- running them -------------------------------------------------------------------

def validate(spec):
    if not isinstance(spec, dict) or spec.get("type") not in CHECKS:
        return ["type is one of %s" % ", ".join(sorted(CHECKS))]
    problems = list(CHECKS[spec["type"]][1](spec))
    if "name" in spec and not (isinstance(spec["name"], str) and 1 <= len(spec["name"]) <= 80):
        problems.append("name is 1 to 80 characters")
    if "agent" in spec and not core.valid_id(spec["agent"]):
        problems.append("agent is a name")
    return problems


def evaluate(specs, ctx):
    out = []
    for spec in specs:
        try:
            r = CHECKS[spec["type"]][0](spec, ctx)
        except Exception as exc:  # noqa: BLE001 — a broken check is an error verdict, not a crashed run
            r = result(spec, "error", "%s: %s" % (type(exc).__name__, exc))
        out.append(core.redact(r))
    return out


def verdict(results):
    if any(r["status"] in ("fail", "error") for r in results):
        return "fail"
    if not results or any(r["status"] == "no-data" for r in results):
        return "no-data"
    return "pass"
