"""Checks on how agents work together.

The questions that matter in a system of agents are mostly about the seams:

  routing       did the right agent get the task, and not the wrong one
  handoff       did what was passed on carry what the next agent needs, and nothing it must not
  authority     did an agent keep to the tools, data and records it is allowed — including when a PEER asked it to
  termination   did it end: no loops, no ping-pong, bounded depth and chatter
  duplication   did two agents do the same work, or work that conflicts
  degradation   when a peer or a tool failed, did the system say so, and not claim success
  memory        what one agent wrote to shared memory, did the other act on it as written

Each reads the same timeline as every other check: peer messages (who said what to whom), tool calls
and the state they changed, and the agents' replies.
"""

import json
import re

from . import checks, core
from .checks import check, result


def messages(ctx, to=None, sender=None):
    out = []
    for e in ctx.of("peer_message"):
        d = e["data"]
        if d.get("method") == "reply" or d.get("direction") == "reply":
            continue
        if (to is None or d.get("to") == to) and (sender is None or d.get("from") == sender):
            out.append(e)
    return out


def replies(ctx):
    return [e for e in ctx.of("peer_message") if e["data"].get("method") == "reply"]


def _payload(e):
    d = e["data"]
    return (d.get("text") or "") + "\n" + json.dumps(d.get("message") or {}, sort_keys=True)


def _cond_ok(spec, text):
    ok, why = core.host._text_ok(spec if isinstance(spec, dict) else {"contains": spec}, text, "the message")
    return ok, why


def _no_peer_data(spec):
    return result(spec, "no-data", "no message between agents was seen in this run — is the delegation going through LLMSim's A2A stub or proxy?")


# -- routing --------------------------------------------------------------------------------------------

def validate_routing(spec):
    problems = ["unknown field %r" % k for k in spec if k not in {"type", "name", "agent", "expect", "forbid", "from"}]
    for key in ("expect", "forbid"):
        for i, r in enumerate(spec.get(key) or []):
            if not (isinstance(r, dict) and isinstance(r.get("to"), str) and "when" in r):
                problems.append("%s[%d] is {when: <text condition>, to: <agent>}" % (key, i))
    if not (spec.get("expect") or spec.get("forbid")):
        problems.append("give expect and/or forbid")
    return problems


@check("routing", validate_routing)
def check_routing(spec, ctx):
    msgs = messages(ctx, sender=spec.get("from"))
    if not msgs:
        return _no_peer_data(spec)
    problems = []
    for r in spec.get("expect") or []:
        relevant = [e for e in msgs if _cond_ok(r["when"], _payload(e))[0]]
        if not relevant:
            problems.append("no task matching %s was delegated at all" % json.dumps(r["when"])[:60])
        elif not any(e["data"]["to"] == r["to"] for e in relevant):
            problems.append("a task matching %s went to %s, not %s" % (json.dumps(r["when"])[:50], ", ".join(sorted({e["data"]["to"] for e in relevant})), r["to"]))
    for r in spec.get("forbid") or []:
        wrong = [e for e in msgs if e["data"]["to"] == r["to"] and _cond_ok(r["when"], _payload(e))[0]]
        if wrong:
            problems.append("a task matching %s went to %s, which it must not" % (json.dumps(r["when"])[:50], r["to"]))
    if problems:
        return result(spec, "fail", "; ".join(problems[:3]), [{"delegated": ["%s → %s: %s" % (e["data"]["from"], e["data"]["to"], (e["data"].get("text") or "")[:60]) for e in msgs[:8]]}])
    return result(spec, "pass", "%d delegation(s), all routed as required" % len(msgs))


# -- handoff content --------------------------------------------------------------------------------------

def validate_handoff(spec):
    problems = ["unknown field %r" % k for k in spec if k not in {"type", "name", "agent", "to", "from", "required", "forbidden", "which"}]
    if not isinstance(spec.get("to"), str):
        problems.append("to is the agent that receives the handoff")
    if not (spec.get("required") or spec.get("forbidden")):
        problems.append("give required and/or forbidden: lists of text conditions")
    if spec.get("which", "all") not in ("all", "first", "last"):
        problems.append("which is all, first or last")
    for key in ("required", "forbidden"):
        for c in spec.get(key) or []:
            if not isinstance(c, (str, dict)):
                problems.append("%s is a list of strings or text conditions" % key)
    return problems


@check("handoff", validate_handoff)
def check_handoff(spec, ctx):
    msgs = messages(ctx, to=spec["to"], sender=spec.get("from"))
    if not msgs:
        if not messages(ctx):
            return _no_peer_data(spec)
        return result(spec, "fail", "nothing was handed to %s; the run's handoffs went to %s" % (spec["to"], ", ".join(sorted({e["data"]["to"] for e in messages(ctx)}))))
    which = spec.get("which", "all")
    picked = msgs[:1] if which == "first" else msgs[-1:] if which == "last" else msgs
    problems = []
    for e in picked:
        text = _payload(e)
        for c in spec.get("required") or []:
            if not _cond_ok(c, text)[0]:
                problems.append("the handoff to %s lacks %s" % (spec["to"], json.dumps(c)[:60]))
        for c in spec.get("forbidden") or []:
            if _cond_ok(c, text)[0]:
                problems.append("the handoff to %s carries %s, which it must not" % (spec["to"], json.dumps(c)[:60]))
    if problems:
        return result(spec, "fail", "; ".join(dict.fromkeys(problems)), [{"message": (picked[0]["data"].get("text") or "")[:200]}])
    return result(spec, "pass", "%d handoff(s) to %s carried what is required and nothing forbidden" % (len(picked), spec["to"]))


# -- authority --------------------------------------------------------------------------------------------

def validate_authority(spec):
    problems = ["unknown field %r" % k for k in spec if k not in {"type", "name", "agent", "tools", "collections", "viaPeer"}]
    if not core.valid_id(spec.get("agent")):
        problems.append("agent is the agent whose authority is checked")
    for key in ("tools", "collections"):
        v = spec.get(key)
        if v is not None and not (isinstance(v, dict) and set(v) <= {"allow", "deny"} and all(isinstance(x, list) for x in v.values())):
            problems.append("%s is {allow?: [names], deny?: [names]}" % key)
    if not (spec.get("tools") or spec.get("collections")):
        problems.append("give tools and/or collections")
    return problems


@check("authority", validate_authority)
def check_authority(spec, ctx):
    agent = spec["agent"]
    events = [e for e in ctx.events if e["agent"] == agent]
    if not events:
        return result(spec, "no-data", "agent %s did nothing in this run, so its authority was not exercised" % agent)
    cutoff = None
    if spec.get("viaPeer"):
        # what a peer asked of it, and what a peer answered it: an instruction planted in an answer
        # to the agent's own question is the usual way one arrives
        inbound = [e for e in ctx.of("peer_message") if e["data"].get("to") == agent]
        if not inbound:
            return result(spec, "no-data", "no peer said anything to %s" % agent)
        cutoff = inbound[0]["at"]
    used = []
    for e in events:
        if cutoff is not None and e["at"] < cutoff:
            continue
        if e["kind"] == "tool_call":
            used.append(("tools", e["data"].get("tool"), e))
        elif e["kind"] == "model_call":
            used += [("tools", c.get("name"), e) for c in (e["data"].get("response") or {}).get("toolCalls") or []]
        elif e["kind"] in ("state_change", "state_read") and e["data"].get("op") != "seed":
            used.append(("collections", e["data"].get("collection"), e))
    problems = []
    for kind, name, e in used:
        rule = spec.get(kind) or {}
        if name in (rule.get("deny") or []) or (rule.get("allow") is not None and name not in rule["allow"]):
            problems.append({"used": name, "kind": kind, "event": e["id"], "viaPeer": bool(cutoff)})
    if problems:
        first = problems[0]
        return result(spec, "fail", "%s used %s %s, which it is not allowed%s" % (agent, "the tool" if first["kind"] == "tools" else "the collection", first["used"],
                                                                                " when a peer asked" if spec.get("viaPeer") else ""), problems[:10])
    return result(spec, "pass", "%s stayed within its authority across %d action(s)" % (agent, len(used)))


# -- termination --------------------------------------------------------------------------------------------

def validate_termination(spec):
    problems = ["unknown field %r" % k for k in spec if k not in {"type", "name", "agent", "maxMessages", "maxRepeats", "maxDepth", "maxMessagesPerPair"}]
    for k in ("maxMessages", "maxRepeats", "maxDepth", "maxMessagesPerPair"):
        if k in spec and not (isinstance(spec[k], int) and not isinstance(spec[k], bool) and spec[k] >= 1):
            problems.append("%s is a whole number, 1 or more" % k)
    if not any(k in spec for k in ("maxMessages", "maxRepeats", "maxDepth", "maxMessagesPerPair")):
        problems.append("give maxMessages, maxRepeats, maxDepth or maxMessagesPerPair")
    return problems


def _norm(text):
    return re.sub(r"\W+", " ", (text or "").lower()).strip()[:120]


def delegation_depth(edges):
    graph = {}
    for a, b in edges:
        graph.setdefault(a, set()).add(b)
    best = 0

    def walk(node, seen):
        nonlocal best
        best = max(best, len(seen) - 1)
        for nxt in graph.get(node, ()):
            if nxt not in seen:
                walk(nxt, seen | {nxt})
    for start in graph:
        walk(start, {start})
    return best


@check("termination", validate_termination)
def check_termination(spec, ctx):
    msgs = messages(ctx)
    if not msgs:
        return _no_peer_data(spec)
    problems = []
    if "maxMessages" in spec and len(msgs) > spec["maxMessages"]:
        problems.append("%d messages between agents, limit %d" % (len(msgs), spec["maxMessages"]))
    if "maxMessagesPerPair" in spec:
        pairs = {}
        for e in msgs:
            pairs[tuple(sorted((e["data"]["from"], e["data"]["to"])))] = pairs.get(tuple(sorted((e["data"]["from"], e["data"]["to"]))), 0) + 1
        worst = max(pairs.items(), key=lambda kv: kv[1])
        if worst[1] > spec["maxMessagesPerPair"]:
            problems.append("%s and %s exchanged %d messages, limit %d" % (worst[0][0], worst[0][1], worst[1], spec["maxMessagesPerPair"]))
    if "maxRepeats" in spec:
        counts = {}
        for e in msgs:
            k = (e["data"]["from"], e["data"]["to"], _norm(e["data"].get("text")))
            counts[k] = counts.get(k, 0) + 1
        loop = max(counts.items(), key=lambda kv: kv[1])
        if loop[1] > spec["maxRepeats"]:
            problems.append("%s told %s the same thing %d times (%r) — a loop" % (loop[0][0], loop[0][1], loop[1], loop[0][2][:40]))
    if "maxDepth" in spec:
        depth = delegation_depth({(e["data"]["from"], e["data"]["to"]) for e in msgs})
        if depth > spec["maxDepth"]:
            problems.append("delegation went %d agents deep, limit %d" % (depth, spec["maxDepth"]))
    if problems:
        return result(spec, "fail", "; ".join(problems))
    return result(spec, "pass", "%d message(s) between agents, within every limit" % len(msgs))


# -- duplication and conflict ----------------------------------------------------------------------------------

def validate_duplication(spec):
    problems = ["unknown field %r" % k for k in spec if k not in {"type", "name", "agent", "tools", "allowRepeat", "conflicts"}]
    if "tools" in spec and not (isinstance(spec["tools"], list) and all(isinstance(t, str) for t in spec["tools"])):
        problems.append("tools is a list of tool names")
    if "allowRepeat" in spec and not (isinstance(spec["allowRepeat"], int) and spec["allowRepeat"] >= 1):
        problems.append("allowRepeat is a whole number, 1 or more")
    return problems


@check("duplication", validate_duplication)
def check_duplication(spec, ctx):
    calls = ctx.tool_calls() or [dict(c, error=None) for c in ctx.requested_calls()]
    writes = [e for e in ctx.of("state_change") if e["data"].get("op") not in ("seed", None) and e["data"].get("before") != e["data"].get("after")]
    if not calls and not writes:
        return result(spec, "no-data", "no tool call or state change was seen, so there is no work to compare")
    problems = []
    seen = {}
    for c in calls:
        if spec.get("tools") and c["tool"] not in spec["tools"]:
            continue
        seen.setdefault((c["tool"], core.dumps(c["arguments"])), []).append(c["agent"])
    for (tool, args), who in seen.items():
        if len(who) > spec.get("allowRepeat", 1):
            problems.append("%s(%s) was done %d times%s" % (tool, args[:50], len(who), " by %s" % " and ".join(sorted(set(who))) if len(set(who)) > 1 else ""))
    if spec.get("conflicts", True):
        by_record = {}
        for e in writes:
            by_record.setdefault((e["data"]["collection"], e["data"]["key"]), []).append((e["agent"], core.dumps(e["data"].get("after"))))
        for rec, ws in by_record.items():
            if len({a for a, _ in ws}) > 1 and len({v for _, v in ws}) > 1:
                problems.append("%s/%s was written by %s with different values" % (rec[0], rec[1], " and ".join(sorted({a or "?" for a, _ in ws}))))
    if problems:
        return result(spec, "fail", "; ".join(problems[:4]))
    return result(spec, "pass", "no duplicated or conflicting work across %d call(s) and %d write(s)" % (len(calls), len(writes)))


# -- degradation -------------------------------------------------------------------------------------------------

def validate_degradation(spec):
    problems = ["unknown field %r" % k for k in spec if k not in {"type", "name", "agent", "failure", "mustSay", "mustNotSay"}]
    f = spec.get("failure")
    if not (isinstance(f, dict) and (isinstance(f.get("peer"), str) or isinstance(f.get("tool"), str))):
        problems.append("failure is {peer: name} or {tool: name}: the thing that was made to fail")
    if not (spec.get("mustSay") or spec.get("mustNotSay")):
        problems.append("give mustSay and/or mustNotSay: text conditions on the final answer")
    return problems


@check("degradation", validate_degradation)
def check_degradation(spec, ctx):
    f = spec["failure"]
    if f.get("peer"):
        failed = [e for e in replies(ctx) if e["data"].get("from") == f["peer"] and (e["data"].get("error") or e["data"].get("fault") or e["data"].get("state") in ("failed", "rejected"))]
        failed += [e for e in ctx.of("peer_message") if e["data"].get("to") == f["peer"] and e["data"].get("method") != "reply"
                   and not any(r["data"].get("from") == f["peer"] and r["parent"] == e["id"] for r in replies(ctx))]
    else:
        failed = [e for e in ctx.of("tool_call") if e["data"].get("tool") == f["tool"] and (e["data"].get("error") or e["data"].get("fault"))]
    if not failed:
        return result(spec, "no-data", "%s did not fail in this run, so how the system copes was not exercised" % (f.get("peer") or f.get("tool")))
    final = ctx.final
    if final is None:
        return result(spec, "fail", "%s failed and the system never answered the user" % (f.get("peer") or f.get("tool")))
    problems = []
    if spec.get("mustSay") and not _cond_ok(spec["mustSay"], final)[0]:
        problems.append("after %s failed, the answer does not say %s: %r" % (f.get("peer") or f.get("tool"), json.dumps(spec["mustSay"])[:50], final[:100]))
    if spec.get("mustNotSay") and _cond_ok(spec["mustNotSay"], final)[0]:
        problems.append("after %s failed, the answer claims %s: %r" % (f.get("peer") or f.get("tool"), json.dumps(spec["mustNotSay"])[:50], final[:100]))
    if problems:
        return result(spec, "fail", "; ".join(problems), [{"answer": final[:300]}])
    return result(spec, "pass", "%s failed and the answer handled it" % (f.get("peer") or f.get("tool")))


# -- shared memory --------------------------------------------------------------------------------------------------

def validate_memory(spec):
    problems = ["unknown field %r" % k for k in spec if k not in {"type", "name", "agent", "collection", "key", "field", "readBy", "writtenBy", "actsOn"}]
    for k in ("collection", "key", "field", "readBy"):
        if not isinstance(spec.get(k), str):
            problems.append("%s is required" % k)
    a = spec.get("actsOn")
    if not (isinstance(a, dict) and isinstance(a.get("tool"), str) and isinstance(a.get("argument"), str)):
        problems.append("actsOn is {tool, argument}: the call whose argument must be what was read")
    return problems


@check("memory", validate_memory)
def check_memory(spec, ctx):
    reads = [e for e in ctx.of("state_read") if e["agent"] == spec["readBy"] and e["data"].get("collection") == spec["collection"] and e["data"].get("key") == str(spec["key"])]
    if not reads:
        return result(spec, "no-data", "%s never read %s/%s" % (spec["readBy"], spec["collection"], spec["key"]))
    problems = []
    for read in reads:
        value = core.dig(read["data"].get("value"), spec["field"])
        if spec.get("writtenBy"):
            writers = [w for w in ctx.of("state_change") if w["at"] <= read["at"] and w["data"].get("collection") == spec["collection"]
                       and w["data"].get("key") == str(spec["key"]) and w["data"].get("op") != "seed"]
            if not writers or writers[-1]["agent"] != spec["writtenBy"]:
                problems.append("the value %s read was not last written by %s" % (spec["readBy"], spec["writtenBy"]))
                continue
        later = [c for c in ctx.tool_calls(spec["readBy"]) if c["tool"] == spec["actsOn"]["tool"] and c["event"] > read["id"]]
        if not later:
            continue
        used = later[0]["arguments"].get(spec["actsOn"]["argument"])
        if used != value:
            problems.append("%s read %s=%r from %s/%s and acted on %r" % (spec["readBy"], spec["field"], value, spec["collection"], spec["key"], used))
    if problems:
        return result(spec, "fail", "; ".join(dict.fromkeys(problems)))
    return result(spec, "pass", "%s acted on what it read, as written" % spec["readBy"])
