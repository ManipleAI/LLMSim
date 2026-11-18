"""The other agents, as LLMSim sees them.

A multi-agent system fails between its agents: the wrong one gets the task, a handoff drops what
the next needs, two of them hand a task back and forth. To test that, LLMSim has to be on the
wire between them. It can be in two ways.

  a stub      LLMSim IS the peer. An A2A agent whose replies are written down, so the agent under
              test delegates to something whose every answer is known — and can be made to fail,
              stall, lie or attack.
  a proxy     LLMSim stands between the agent under test and a REAL peer. It forwards, keeps
              every message in the run's timeline, and can record the real peer once and answer
              from the recording after.

Both speak Agent2Agent 0.3 over JSON-RPC: message/send, message/stream, tasks/get, tasks/cancel,
tasks/pushNotificationConfig/set, and the agent card. Every message in either direction is a
`peer_message` event, so a check can say who talked to whom, in what order, and what was said.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import core, credentials, drivers, fakes, ordering, route, rest_area, timeline
from .core import fail, ok
from .store import store

STUB_KEYS = {"id", "description", "skills", "results", "default", "inject", "name"}
TASKS = {}                        # (kind, id, task id) -> task bookkeeping
_LOCK = threading.RLock()
_CALLS = {}                       # (run, id) -> number of messages received
_POS = {}                         # (run, proxy, key) -> how many times a recorded answer has been served
_RUN_DEFS = {}                    # (area, run, id) -> a stub or proxy a scenario brought for that run only


def definition(area, name, run=None):
    """The stub or proxy of that name: the one a scenario brought for this run, else the saved one.

    The run's own comes first, so a scenario can stand a different billing agent in for the shared
    one without touching it."""
    with _LOCK:
        if run and (area, run, name) in _RUN_DEFS:
            return _RUN_DEFS[(area, run, name)]
    return store().get(area, name)


def install_for_run(run, area, defn):
    with _LOCK:
        _RUN_DEFS[(area, run, defn["id"])] = defn
    return lambda: _RUN_DEFS.pop((area, run, defn["id"]), None)


def validate_stub(defn):
    if not isinstance(defn, dict):
        return ["a stub agent is an object"]
    problems = ["unknown field %r" % k for k in defn if k not in STUB_KEYS]
    if not core.valid_id(defn.get("id")):
        problems.append("id is letters, digits, dot, dash, underscore or colon")
    if not isinstance(defn.get("skills", []), list):
        problems.append("skills is a list of {id, name, description}")
    problems += fakes.validate_behavior({k: v for k, v in defn.items() if k in {"results", "default", "inject"}}, "stub")
    for i, r in enumerate(defn.get("results") or []):
        res = r.get("result")
        if isinstance(res, dict) and res.get("state") not in (None, "completed", "working", "input-required", "failed", "rejected", "canceled", "auth-required"):
            problems.append("stub.results[%d].result.state is an A2A task state" % i)
    return problems


def validate_proxy(defn):
    if not isinstance(defn, dict):
        return ["a proxy is an object"]
    problems = ["unknown field %r" % k for k in defn if k not in {"id", "url", "mode", "credential", "description"}]
    if not core.valid_id(defn.get("id")):
        problems.append("id is letters, digits, dot, dash, underscore or colon")
    if not isinstance(defn.get("url"), str) or not defn["url"].startswith("http"):
        problems.append("url is the real agent's base URL")
    if defn.get("mode") not in ("record", "replay", "passthrough"):
        problems.append("mode is record, replay or passthrough")
    return problems


def _text(parts):
    return drivers.parts_text(parts)


def _identity(handler):
    ident = core.identity(handler)
    return ident.get("run"), ident.get("agent") or "client"


def _event(run, sender, to, text, parent=None, **extra):
    if not run:
        return None
    return timeline.emit(run, "peer_message", sender, parent, dict({"from": sender, "to": to, "text": text}, **extra))


def _rpc_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _task_json(t):
    return {k: v for k, v in t.items() if not k.startswith("_")}


# -- the stub -----------------------------------------------------------------------------------

def stub_card(defn, url):
    return {"name": defn.get("name") or defn["id"], "description": defn.get("description", ""), "url": url, "version": "1.0.0", "protocolVersion": "0.3.0",
            "capabilities": {"streaming": True, "pushNotifications": True}, "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
            "skills": defn.get("skills") or []}


def _finish(task, text, artifacts):
    task["status"] = {"state": "completed", "message": {"kind": "message", "role": "agent", "messageId": uuid.uuid4().hex, "parts": [{"kind": "text", "text": text}]} if text else None}
    if task["status"]["message"] is None:
        task["status"].pop("message")
    if artifacts:
        task["artifacts"] = [{"artifactId": uuid.uuid4().hex[:8], "parts": [{"kind": "text", "text": a}]} for a in artifacts]


def _push(task, push):
    try:
        req = urllib.request.Request(push["url"], json.dumps(_task_json(task)).encode(),
                                     {"Content-Type": "application/json", "X-A2A-Notification-Token": push.get("token", "")})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as exc:  # noqa: BLE001
        core.log("a2a stub: push to %s failed: %s" % (push.get("url"), exc))


def _later(task, push, ms, text, artifacts):
    time.sleep(ms / 1000.0)
    if task["status"]["state"] == "working" and not task.get("_stuck"):
        _finish(task, text, artifacts)
        if push:
            _push(task, push)


def stub_send(defn, params, run, caller, handler_scope):
    """(result, error) for message/send to a stub."""
    message = params.get("message") or {}
    text = _text(message.get("parts"))
    with _LOCK:
        n = _CALLS[(run, defn["id"])] = _CALLS.get((run, defn["id"]), 0) + 1
    scope = {"text": text, "turn": n - 1, "call": n, "from": caller, "run": run or "", "agent": caller, "taskId": message.get("taskId"),
             "contextId": message.get("contextId"), "identity": {"run": run or "", "agent": caller}, "method": "message/send", "peer": defn["id"]}
    inbound = _event(run, caller, defn["id"], text, method="message/send", taskId=message.get("taskId"), contextId=message.get("contextId"), message=message)
    rule, fault = fakes.pick("peer", dict(scope, target="peer"))
    kind = fault["type"] if fault else None
    if kind == "delay":
        fakes.wait(fault["ms"])
    elif kind == "timeout":
        fakes.wait(fault.get("ms", 5000))
        return None, ("error", -32000, "the peer timed out"), kind, inbound
    elif kind == "down":
        return None, ("http", fault.get("status", 503), "the peer is down"), kind, inbound
    elif kind == "error":
        return None, ("error", -32000, fault.get("message") or "the peer failed"), kind, inbound
    elif kind == "drop":
        return None, ("drop", fault.get("mode", "close"), ""), kind, inbound
    outcome = fakes.behave({k: v for k, v in defn.items() if k in {"results", "default", "inject"}}, scope, run or "_", caller)
    result = outcome.result
    spec = result if isinstance(result, dict) and ({"text", "state", "after", "artifacts"} & set(result)) else {"text": result if isinstance(result, str) else json.dumps(result)}
    text_out, artifacts = spec.get("text", ""), [a if isinstance(a, str) else json.dumps(a) for a in spec.get("artifacts") or []]
    if kind == "wrong_answer":
        text_out, spec = fault.get("text", "That is not what was asked."), dict(spec, state=spec.get("state"))
    elif kind == "contradict" and n > 1:
        text_out = fault.get("text", "Actually, the opposite is true.")
    elif kind == "empty":
        text_out, artifacts = "", []
    elif kind == "refuse":
        spec = dict(spec, state="rejected")
        text_out = fault.get("message") or "I cannot help with that."
    elif kind == "never_complete":
        spec = dict(spec, state="working", after=None)
    context = message.get("contextId") or uuid.uuid4().hex
    state = spec.get("state")
    if state is None and not spec.get("after") and not artifacts:
        msg = {"kind": "message", "role": "agent", "messageId": uuid.uuid4().hex, "contextId": context, "parts": [{"kind": "text", "text": text_out}]}
        _event(run, defn["id"], caller, text_out, inbound, method="reply", fault=kind or "")
        return msg, None, kind, inbound
    task_id = message.get("taskId") if message.get("taskId") and ("stub", defn["id"], message["taskId"]) in TASKS else uuid.uuid4().hex
    task = {"kind": "task", "id": task_id, "contextId": context, "status": {"state": "working"}, "_polls": 0, "_created": time.time(),
            "_after": spec.get("after") or {}, "_text": text_out, "_artifacts": artifacts}
    push = (params.get("configuration") or {}).get("pushNotificationConfig")
    final_state = state or "completed"
    if final_state == "completed":
        if spec.get("after"):
            if "ms" in spec["after"] or push:
                threading.Thread(target=_later, args=(task, push, (spec["after"] or {}).get("ms", 200), text_out, artifacts), daemon=True).start()
        else:
            _finish(task, text_out, artifacts)
    elif final_state == "working":
        task["_stuck"] = True
    else:
        task["status"] = {"state": final_state, "message": {"kind": "message", "role": "agent", "messageId": uuid.uuid4().hex, "parts": [{"kind": "text", "text": text_out}]}}
    with _LOCK:
        TASKS[("stub", defn["id"], task_id)] = task
    _event(run, defn["id"], caller, text_out, inbound, method="reply", state=task["status"]["state"], fault=kind or "")
    return _task_json(task), None, kind, inbound


def stub_get(defn, params):
    with _LOCK:
        task = TASKS.get(("stub", defn["id"], params.get("id")))
    if task is None:
        return None, ("error", -32001, "Task not found")
    task["_polls"] += 1
    after = task.get("_after") or {}
    if task["status"]["state"] == "working" and not task.get("_stuck") and "polls" in after and task["_polls"] >= after["polls"]:
        _finish(task, task["_text"], task["_artifacts"])
    return _task_json(task), None


def stub_rpc(defn, message, handler, run, caller):
    method, params, rid = message.get("method"), message.get("params") or {}, message.get("id")
    if method == "message/send":
        result, err, _, _ = stub_send(defn, params, run, caller, None)
        if err:
            return err[0] == "http" and ("http", err[1], err[2]) or ("drop", err[1]) if err[0] in ("http", "drop") else _rpc_error(rid, err[1], err[2])
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    if method == "tasks/get":
        result, err = stub_get(defn, params)
        return _rpc_error(rid, err[1], err[2]) if err else {"jsonrpc": "2.0", "id": rid, "result": result}
    if method == "tasks/cancel":
        with _LOCK:
            task = TASKS.get(("stub", defn["id"], params.get("id")))
        if task is None:
            return _rpc_error(rid, -32001, "Task not found")
        task["status"], task["_stuck"] = {"state": "canceled"}, True
        return {"jsonrpc": "2.0", "id": rid, "result": _task_json(task)}
    if method == "tasks/pushNotificationConfig/set":
        return {"jsonrpc": "2.0", "id": rid, "result": params}
    return _rpc_error(rid, -32601, "Method not found: %s" % method)


def _sse(handler, events):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True
    for e in events:
        handler.wfile.write(("data: %s\n\n" % json.dumps(e)).encode())
        handler.wfile.flush()


def stub_stream(defn, message, handler, run, caller):
    params, rid = message.get("params") or {}, message.get("id")
    result, err, _, _ = stub_send(defn, params, run, caller, None)
    if err:
        return core.reply_json(handler, 200, _rpc_error(rid, err[1] if isinstance(err[1], int) else -32000, err[2]))
    if result.get("kind") == "message":
        return _sse(handler, [{"jsonrpc": "2.0", "id": rid, "result": result}])
    tid, ctx = result["id"], result.get("contextId")
    events = [{"jsonrpc": "2.0", "id": rid, "result": dict(result, status={"state": "submitted"})}]
    for a in result.get("artifacts") or []:
        events.append({"jsonrpc": "2.0", "id": rid, "result": {"kind": "artifact-update", "taskId": tid, "contextId": ctx, "artifact": a}})
    events.append({"jsonrpc": "2.0", "id": rid, "result": {"kind": "status-update", "taskId": tid, "contextId": ctx, "status": result["status"], "final": True}})
    _sse(handler, events)


@route("/a2a/")
def _stub_endpoint(handler, method, path, rest, raw):
    name, _, tail = rest.partition("/")
    run, caller = _identity(handler)
    defn = definition("a2a", name, run)
    if defn is None:
        return core.reply_json(handler, 404, {"error": "There is no stub agent called %s." % name})
    tail = tail.strip("/")
    host = handler.headers.get("Host") or "localhost"
    base = "http://%s%s" % (host, handler.path.split("?")[0].split("/a2a/")[0] + "/a2a/" + name)
    if method == "GET" and tail in (".well-known/agent-card.json", ".well-known/agent.json"):
        return core.reply_json(handler, 200, stub_card(defn, base))
    if method != "POST":
        return core.reply_json(handler, 405, {"error": "POST JSON-RPC"})
    try:
        message = json.loads(raw or b"{}")
    except ValueError:
        return core.reply_json(handler, 400, _rpc_error(None, -32700, "Parse error"))
    text = _text(((message.get("params") or {}).get("message") or {}).get("parts"))
    with ordering.guard(run, {"kind": "peer", "agent": caller, "run": run or "", "to": name, "text": text}):
        if message.get("method") == "message/stream":
            return stub_stream(defn, message, handler, run, caller)
        out = stub_rpc(defn, message, handler, run, caller)
    if isinstance(out, tuple):
        if out[0] == "drop":
            return core.hang_up(handler, out[1])
        return core.reply_json(handler, out[1], {"error": out[2]})
    core.reply_json(handler, 200, out)


# -- the proxy ------------------------------------------------------------------------------------

def _real(defn, method, url, body=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if defn.get("credential"):
        headers.update(credentials.headers(defn["credential"]))
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), ""
    except (urllib.error.URLError, OSError) as exc:
        return 502, json.dumps({"error": "could not reach the real agent: %s" % getattr(exc, "reason", exc)}), ""


def _proxy_key(message):
    params, method = message.get("params") or {}, message.get("method")
    if method in ("message/send", "message/stream"):
        m = params.get("message") or {}
        return core.digest([method, _text(m.get("parts")), m.get("taskId") is not None])
    return core.digest([method, params.get("id")])


def _sse_events(text):
    out = []
    for block in text.split("\n\n"):
        data = "\n".join(l[5:].lstrip() for l in block.splitlines() if l.startswith("data:"))
        if data:
            try:
                out.append(json.loads(data))
            except ValueError:
                pass
    return out


def _retext(events, text):
    """The same answer events with what the peer said replaced by `text`: a message's parts, a task's status message
    and artifacts. A fault that changes the answer must change what the caller receives, not only what is logged."""
    part = [{"kind": "text", "text": text}]
    out = []
    for e in events:
        res = e.get("result")
        if not isinstance(res, dict):
            out.append(e)
            continue
        res = dict(res)
        if res.get("kind") == "message":
            res["parts"] = part
        else:
            status = dict(res.get("status") or {})
            if status.get("message") or text:
                status["message"] = dict(status.get("message") or {"kind": "message", "role": "agent", "messageId": uuid.uuid4().hex}, parts=part)
            res["status"] = status
            if res.get("artifacts"):
                res["artifacts"] = [dict(a, parts=part) for a in res["artifacts"]]
        out.append(dict(e, result=res))
    return out


@route("/a2a-proxy/")
def _proxy_endpoint(handler, method, path, rest, raw):
    name, _, tail = rest.partition("/")
    run, caller = _identity(handler)
    defn = definition("a2aproxy", name, run)
    if defn is None:
        return core.reply_json(handler, 404, {"error": "There is no proxy called %s." % name})
    tail = tail.strip("/")
    host = handler.headers.get("Host") or "localhost"
    me = "http://%s%s" % (host, handler.path.split("?")[0].split("/a2a-proxy/")[0] + "/a2a-proxy/" + name)
    cas = store().get("a2acassettes", name) or {"card": None, "calls": {}}
    if method == "GET" and tail in (".well-known/agent-card.json", ".well-known/agent.json"):
        if defn["mode"] == "replay":
            card = cas.get("card")
            if card is None:
                return core.reply_json(handler, 404, {"error": "LLMSim has no recorded agent card for %s" % name})
        else:
            status, text, _ = _real(defn, "GET", defn["url"].rstrip("/") + "/" + tail)
            if status != 200:
                return core.reply_json(handler, status, {"error": "the real agent's card: HTTP %d" % status})
            card = json.loads(text)
            if defn["mode"] == "record":
                cas["card"] = card
                store().put("a2acassettes", name, cas)
        return core.reply_json(handler, 200, dict(card, url=me))
    if method != "POST":
        return core.reply_json(handler, 405, {"error": "POST JSON-RPC"})
    try:
        message = json.loads(raw or b"{}")
    except ValueError:
        return core.reply_json(handler, 400, _rpc_error(None, -32700, "Parse error"))
    params, rpc = message.get("params") or {}, message.get("method")
    text = _text((params.get("message") or {}).get("parts"))
    inbound = _event(run, caller, name, text, method=rpc, taskId=(params.get("message") or {}).get("taskId") or params.get("id"), message=params.get("message"))
    with _LOCK:
        n = _CALLS[(run, "proxy:" + name)] = _CALLS.get((run, "proxy:" + name), 0) + 1
    rule, fault = fakes.pick("peer", {"target": "peer", "peer": name, "method": rpc, "text": text, "call": n, "turn": n - 1,
                                      "identity": {"run": run or "", "agent": caller}, "from": caller, "run": run or ""})
    kind = fault["type"] if fault else None
    if kind == "delay":
        fakes.wait(fault["ms"])
    elif kind in ("down", "drop", "timeout", "error", "refuse"):
        if kind == "drop":
            return core.hang_up(handler, fault.get("mode", "close"))
        if kind == "down":
            return core.reply_json(handler, fault.get("status", 503), {"error": "the peer is down"})
        if kind == "timeout":
            fakes.wait(fault.get("ms", 5000))
        return core.reply_json(handler, 200, _rpc_error(message.get("id"), -32000, fault.get("message") or "the peer failed"))
    key = _proxy_key(message)
    if defn["mode"] == "replay":
        seen = cas["calls"].get(key)
        if not seen:
            reply = _rpc_error(message.get("id"), -32000, "LLMSim has no recording of this %s in %s" % (rpc, name))
            _event(run, name, caller, reply["error"]["message"], inbound, method="reply", error=True)
            return core.reply_json(handler, 200, reply)
        with _LOCK:
            idx = _POS[(run, name, key)] = _POS.get((run, name, key), -1) + 1
        got = seen[min(idx, len(seen) - 1)]          # the k-th time this was asked, the k-th answer it got (the last, after that)
        events = [dict(e, id=message.get("id")) for e in got]
    else:
        status, body, ctype = _real(defn, "POST", defn["url"].rstrip("/"), message)
        if status >= 500 and not body.strip().startswith("{"):
            return core.reply_json(handler, status, {"error": "the real agent answered HTTP %d" % status})
        events = _sse_events(body) if "event-stream" in ctype else [json.loads(body)]
        if defn["mode"] == "record":
            cas["calls"].setdefault(key, []).append(events)
            store().put("a2acassettes", name, cas)
    last = events[-1] if events else {}
    result = last.get("result") or {}
    reply_text = _text(result.get("parts")) or _text(((result.get("status") or {}).get("message") or {}).get("parts")) or \
        "\n".join(_text(a.get("parts")) for a in result.get("artifacts") or [])
    if kind == "wrong_answer":
        reply_text = fault.get("text", "Wrong.")
        events = _retext(events, reply_text)
    elif kind == "contradict" and n > 1:
        reply_text = fault.get("text", "Actually, the opposite is true.")
        events = _retext(events, reply_text)
    elif kind == "empty":
        reply_text = ""
        events = _retext(events, "")
    elif kind == "never_complete":
        reply_text = ""
        events = [dict(e, result={"kind": "task", "id": uuid.uuid4().hex, "contextId": (e.get("result") or {}).get("contextId") or uuid.uuid4().hex,
                                  "status": {"state": "working"}}) if e.get("result") else e for e in events[-1:]]
    _event(run, name, caller, reply_text, inbound, method="reply", state=(result.get("status") or {}).get("state", ""), fault=kind or "", error=bool(last.get("error")))
    if rpc == "message/stream":
        return _sse(handler, events)
    core.reply_json(handler, 200, events[-1] if events else _rpc_error(message.get("id"), -32000, "no answer"))


# -- REST -------------------------------------------------------------------------------------------

def _crud(area, noun, validator, listing_fn):
    def rest(method, rest_, body, query, host):
        st = store()
        if method == "GET" and not rest_:
            return ok({noun + "s": [listing_fn(i, d) for i, d, _ in st.docs(area)]})
        if method == "POST" and rest_ == ["validate"]:
            problems = validator((body or {}).get("definition"))
            return ok(valid=not problems, problems=problems)
        if len(rest_) >= 1 and core.valid_id(rest_[0]):
            name, found = rest_[0], st.get(area, rest_[0])
            if method == "PUT" and len(rest_) == 1:
                defn = dict(body or {}, id=name)
                problems = validator(defn)
                if problems:
                    return fail(400, "That %s has %d problem(s): %s" % (noun, len(problems), "; ".join(problems[:5])), problems=problems)
                st.put(area, name, defn)
                return ok({noun: defn})
            if method == "GET" and len(rest_) == 1:
                return ok({noun: found}) if found else fail(404, "There is no %s called %s." % (noun, name))
            if method == "DELETE" and len(rest_) == 1:
                return ok(removed=st.drop(area, name)) if found else fail(404, "There is no %s called %s." % (noun, name))
            if method == "GET" and rest_[1:] == ["url"]:
                run, agent = (query.get("run") or ["RUN"])[0], (query.get("agent") or [""])[0]
                prefix = "a2a" if area == "a2a" else "a2a-proxy"
                return ok({"url": "http://%s/run/%s%s/%s/%s" % (host, run, "/agent/" + agent if agent else "", prefix, name)})
            if method == "DELETE" and rest_[1:] == ["cassette"] and area == "a2aproxy":
                return ok(removed=st.drop("a2acassettes", name))
        return fail(404, "No such route: %s /api/%s/%s" % (method, "a2a" if area == "a2a" else "a2aproxies", "/".join(rest_)))
    return rest


rest_area("a2a")(_crud("a2a", "stub", validate_stub, lambda i, d: {"id": i, "description": d.get("description", ""), "rules": len(d.get("results") or [])}))
rest_area("a2aproxies")(_crud("a2aproxy", "proxy", validate_proxy, lambda i, d: {"id": i, "url": d["url"], "mode": d["mode"]}))
