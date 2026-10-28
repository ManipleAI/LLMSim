"""One run, seen whole.

A run is what happened between a person's first message and the agents' last: the
user's turns, every model call, every tool call, every message between agents, any
state that changed. Until now LLMSim saw the first kind only as model requests,
held in memory. Here they are one ordered list, kept on disk, with who did each
thing and what caused it — which is what a multi-agent test needs, because the
failure is almost never in one agent's request. It is in how two of them met.

Events:

  user_turn      what was said to the agent under test
  agent_reply    what it said back
  model_call     a request to the model LLMSim simulates, and its answer
  tool_call      a tool asked to do something, and what it did
  peer_message   one agent talking to another
  state_change   a record in a fake backend changed
  state_read     a record in a fake backend was read
  span           a step reported by OpenTelemetry, where LLMSim is not the model
  check, note    a verdict, or a remark

`parent` says what caused an event: a tool_call's parent is the model_call that
asked for it.
"""

import json
import re
import threading

from . import core
from .store import store

KINDS = ("user_turn", "agent_reply", "model_call", "tool_call", "peer_message", "state_change", "state_read", "span", "check", "note")


def settings():
    return dict({"recordAll": False}, **(store().get("settings", "timeline") or {}))


def emit(run, kind, agent="", parent=None, data=None, at=None):
    """Add an event. Never raises: a test kit that breaks the request it is
    watching has failed at the one thing it must not do."""
    try:
        if not core.valid_id(run) or kind not in KINDS:
            return None
        return store().add_event(run, kind, agent, parent, data, at)
    except Exception as exc:  # noqa: BLE001
        core.log("timeline: could not record a %s for %s: %s" % (kind, run, exc))
        return None


# A model call reaches the timeline after its answer has gone back to the agent, so an agent can
# answer its user, and the run be judged, before its last model call is recorded. The runner
# waits for the run's calls still being recorded before it judges.
_IN_FLIGHT, _SETTLED = {}, threading.Condition()


def _run_of(cap):
    """The run a capture is part of; there is no capture at all when capturing is off."""
    return ((cap.get("meta") or {}).get("identity") or {}).get("run") if isinstance(cap, dict) else None


def started(cap):
    run = _run_of(cap)
    if run:
        with _SETTLED:
            _IN_FLIGHT[run] = _IN_FLIGHT.get(run, 0) + 1


def _finished(cap):
    run = _run_of(cap)
    if run:
        with _SETTLED:
            left = _IN_FLIGHT.get(run, 0) - 1
            if left > 0:
                _IN_FLIGHT[run] = left
            else:
                _IN_FLIGHT.pop(run, None)
            _SETTLED.notify_all()


def settle(run, seconds=5.0):
    """Wait, at most this long, until no model call of the run is still being recorded. True if none is."""
    with _SETTLED:
        return _SETTLED.wait_for(lambda: not _IN_FLIGHT.get(run), timeout=seconds)


def on_capture(cap):
    """A model call, as the server captured it. Called for every capture; only a
    request that names a run is a part of one, unless recordAll says every request is."""
    try:
        _record(cap)
    finally:
        _finished(cap)


def _record(cap):
    meta = cap["meta"]
    ident = meta.get("identity") or {}
    run = ident.get("run")
    if not run:
        if not settings()["recordAll"]:
            return
        run = "unscoped"
    view = core.host.build_view(meta, cap["request"], cap["response"])
    emit(run, "model_call", ident.get("agent", ""), data={
        "n": cap["n"], "provider": view["provider"], "model": view["model"], "route": view["route"], "rule": view["rule"],
        "status": view["status"], "ms": view["response"]["ms"], "turn": view["turn"], "ask": view["ask"][:400],
        "tools": view["tools"], "credential": (meta.get("credential") or {}).get("label", ""), "fault": view["fault"],
        "app": ident.get("app", ""), "request": cap["request"], "response": cap["response"], "truncated": cap["truncated"],
        "headers": meta.get("headers") or {}}, at=cap["at"])
    _reported_tool_results(run, ident.get("agent", ""), meta.get("shape") or view.get("shape"), cap["request"], cap["at"])


def _json_args(raw):
    if isinstance(raw, dict):
        return raw
    try:
        out = json.loads(raw or "{}")
        return out if isinstance(out, dict) else {"value": out}
    except (TypeError, ValueError):
        return {"raw": raw}


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return "" if content is None else json.dumps(content)


def tool_results_in(shape, request):
    """[{id, tool, arguments, result}] for every tool the agent ran and handed back to the model
    in this request, whatever the API: an agent whose tools run inside it reports them nowhere else."""
    asked, done = {}, []
    if shape == "responses":
        for item in request.get("input") if isinstance(request.get("input"), list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                asked[item.get("call_id")] = (item.get("name"), _json_args(item.get("arguments")))
            elif item.get("type") == "function_call_output":
                done.append((item.get("call_id"), _text_of(item.get("output"))))
    else:
        for m in request.get("messages") or []:
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            if m.get("role") == "assistant":
                for c in m.get("tool_calls") or []:
                    f = c.get("function") or {}
                    asked[c.get("id")] = (f.get("name"), _json_args(f.get("arguments")))
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        asked[b.get("id")] = (b.get("name"), b.get("input") or {})
            elif m.get("role") == "tool":
                done.append((m.get("tool_call_id"), _text_of(content)))
            elif m.get("role") == "user" and isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        done.append((b.get("tool_use_id"), _text_of(b.get("content"))))
    out = []
    for call_id, result in done:
        name, args = asked.get(call_id, (None, {}))
        out.append({"id": call_id, "tool": name, "arguments": args, "result": result})
    return out


def _reported_tool_results(run, agent, shape, request, at):
    """A tool that runs inside the agent is seen only when its result comes back to the model.
    Each one is put on the timeline once, as a tool call the agent reported, after the model
    call that asked for it. One a mock served LLMSim has already put there itself."""
    try:
        found = tool_results_in(shape, request if isinstance(request, dict) else {})
        if not found:
            return
        seen = {e["data"].get("callId") for e in store().events(run, kinds=["tool_call"]) if e["data"].get("callId")}
        for r in found:
            if not r["id"] or r["id"] in seen or not r["tool"]:
                continue
            parent = link_tool_call(run, agent, r["tool"], r["arguments"])
            if parent is None and _served_by_mock(run, agent, r["tool"]):
                continue
            # Placed when the tool started, as near as can be known: when the model's answer that
            # asked for it came back. A mock the tool then called comes after it, not before.
            asked = store().event(parent) if parent else None
            when = (asked["at"] + float((asked["data"] or {}).get("ms") or 0) / 1000.0) if asked else at
            emit(run, "tool_call", agent, parent, {"tool": r["tool"], "arguments": r["arguments"], "result": r["result"][:4000],
                                                   "error": None, "source": "agent", "callId": r["id"]}, at=min(when, at - 0.000001))
    except Exception as exc:  # noqa: BLE001 — watching must never break the request
        core.log("timeline: could not read the tool results in a request of %s: %s" % (run, exc))


def _served_by_mock(run, agent, tool):
    return any(e["data"].get("tool") == tool and e["data"].get("source") != "agent"
               for e in store().events(run, kinds=["tool_call"], agent=agent or None))


def public(event):
    return core.redact(event)


def timeline(run, kinds=None, agent=None, since=0, limit=None):
    events = store().events(run, kinds=kinds, agent=agent, since=since, limit=limit)
    rows = []
    for i, e in enumerate(events):
        row = public(e)
        row["seq"] = i + 1
        row["label"] = label(e)
        rows.append(row)
    first = events[0]["at"] if events else 0
    for row in rows:
        row["t"] = round(row["at"] - first, 3)
    counts = {}
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    return {"run": run, "events": rows, "counts": counts, "agents": sorted({e["agent"] for e in events if e["agent"]}),
            "duration": round(events[-1]["at"] - first, 3) if events else 0}


def label(event):
    """One line saying what an event was, for a list."""
    d, kind = event["data"], event["kind"]
    if kind == "model_call":
        calls = ", ".join(c.get("name", "") for c in (d.get("response") or {}).get("toolCalls") or [])
        said = ((d.get("response") or {}).get("text") or "")[:80]
        return "%s %s → %s" % (d.get("model", ""), (d.get("ask") or "")[:60], ("calls " + calls) if calls else (said or "(nothing)"))
    if kind == "tool_call":
        return "%s(%s) → %s" % (d.get("tool", ""), core.dumps(d.get("arguments", {}))[:60], "error" if d.get("error") else core.dumps(d.get("result", ""))[:60])
    if kind in ("user_turn", "agent_reply"):
        return (d.get("text") or "")[:100]
    if kind == "peer_message":
        return "%s → %s: %s" % (d.get("from", ""), d.get("to", ""), (d.get("text") or core.dumps(d.get("message", "")))[:80])
    if kind in ("state_change", "state_read"):
        return "%s %s %s" % (d.get("op", ""), d.get("collection", ""), d.get("key", ""))
    if kind == "span":
        return d.get("name", "")
    return (d.get("text") or d.get("name") or "")[:100]


def link_tool_call(run, agent, tool, arguments):
    """The model_call whose answer asked for this tool call, if one did and no
    other tool call has claimed it yet. That is what makes the two events one
    exchange: the model asked, the tool received."""
    claimed = {e["parent"] for e in store().events(run, kinds=["tool_call"]) if e["parent"]}
    for e in reversed(store().events(run, kinds=["model_call"], agent=agent or None)):
        if e["id"] in claimed:
            continue
        for call in (e["data"].get("response") or {}).get("toolCalls") or []:
            if call.get("name") == tool and (not call.get("input") or call.get("input") == arguments or _similar(call.get("input"), arguments)):
                return e["id"]
    return None


def _similar(a, b):
    return isinstance(a, dict) and isinstance(b, dict) and all(b.get(k) == v for k, v in a.items() if k in b)


TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


def trace_run(header):
    """The run a W3C traceparent names: its trace id, which is what an OpenTelemetry
    span carries too, so spans and simulated calls land together."""
    m = TRACEPARENT.match((header or "").strip())
    return m.group(1) if m and m.group(1) != "0" * 32 else None
