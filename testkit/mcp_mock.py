"""A mock MCP server.

Most agents reach their tools through MCP, and an agent's tool is where an agent's
work actually happens. This is an MCP server whose tools are written down in a file:
what each is called, what arguments it takes, and what it returns for which call.

    {"id": "orders",
     "tools": [
        {"name": "lookup_order", "description": "Look an order up",
         "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}},
         "results": [{"when": {"field": "arguments.id", "op": "eq", "value": 1182}, "result": {"id": 1182, "status": "shipped"}}],
         "default": {"error": "no such order"}},
        {"name": "refund", "state": {"op": "update", "collection": "orders", "key": "{{arguments.id}}", "set": {"status": "refunded"}}}]}

Point the agent at  /run/<run>/agent/<agent>/mcp/orders  and its calls land in the run's timeline, each
linked to the model call that asked for it. The transport is MCP's streamable HTTP: JSON-RPC over POST,
answered as JSON or, if that is all the client accepts, as one event.

With a `proxy`, the same server is a go-between: `record` forwards to a real server and keeps what came
back; `replay` answers from what was kept and refuses a call that was not.
"""

import json
import threading
import time
import urllib.error
import urllib.request

from . import checks, core, credentials, fakes, route, rest_area, timeline
from .core import fail, ok
from .store import store

SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
TOOL_KEYS = {"name", "description", "inputSchema", "results", "state", "default", "inject"}
DEF_KEYS = {"id", "description", "tools", "proxy"}
_RUN_DEFS = {}                    # (run, id) -> definition, for a scenario that brings its own
_LOCK = threading.Lock()
_CALLS = {}                       # (run, tool) -> count


def validate(definition):
    if not isinstance(definition, dict):
        return ["a mock MCP server is an object"]
    problems = ["unknown field %r" % k for k in definition if k not in DEF_KEYS]
    if not core.valid_id(definition.get("id")):
        problems.append("id is letters, digits, dot, dash, underscore or colon")
    proxy = definition.get("proxy")
    if proxy is not None:
        if not isinstance(proxy, dict) or proxy.get("mode") not in ("record", "replay", "passthrough") or not isinstance(proxy.get("url"), str):
            problems.append("proxy is {url, mode: record|replay|passthrough, credential?}")
        elif proxy["mode"] != "passthrough" and not core.valid_id(proxy.get("cassette", definition.get("id"))):
            problems.append("proxy.cassette is a name")
    tools = definition.get("tools", [])
    if not isinstance(tools, list) or (not tools and proxy is None):
        problems.append("tools is a list of at least one tool (or give a proxy)")
        return problems
    names = set()
    for i, t in enumerate(tools):
        where = "tools[%d]" % i
        if not isinstance(t, dict) or not core.valid_id(t.get("name")):
            problems.append("%s.name is a tool name" % where)
            continue
        if t["name"] in names:
            problems.append("%s: %s is defined twice" % (where, t["name"]))
        names.add(t["name"])
        problems += ["%s: unknown field %r" % (where, k) for k in t if k not in TOOL_KEYS]
        if "inputSchema" in t and not isinstance(t["inputSchema"], dict):
            problems.append("%s.inputSchema is an object" % where)
        problems += fakes.validate_behavior({k: v for k, v in t.items() if k in {"results", "state", "default", "inject"}}, where)
    return problems


def definition(name, run=None):
    with _LOCK:
        if run and (run, name) in _RUN_DEFS:
            return _RUN_DEFS[(run, name)]
    return store().get("mcp", name)


def install_for_run(run, definition_):
    with _LOCK:
        _RUN_DEFS[(run, definition_["id"])] = definition_
    return lambda: _RUN_DEFS.pop((run, definition_["id"]), None)


# -- calls --------------------------------------------------------------------------------

def call_tool(defn, name, arguments, run, agent, ctx_extra=None):
    """(result, is_error, fault) for one tool call, including every fault that may apply."""
    tool = next((t for t in defn.get("tools", []) if t["name"] == name), None)
    if tool is None:
        return {"error": "unknown tool %s" % name}, True, None, "unknown"
    schema = tool.get("inputSchema")
    if schema:
        errors = checks.schema_errors(arguments, schema)
        if errors:
            return {"error": "invalid arguments: " + "; ".join(errors[:3])}, True, None, "invalid"
    with _LOCK:
        n = _CALLS[(run, name)] = _CALLS.get((run, name), 0) + 1
    scope = {"arguments": arguments, "run": run or "", "agent": agent or "", "tool": name, "server": defn["id"], "call": n,
             "identity": {"run": run or "", "agent": agent or ""}}
    rule, fault = fakes.pick("tool", dict(scope, target="tool"))
    kind = fault["type"] if fault else None
    if kind == "delay":
        fakes.wait(fault["ms"])
    elif kind == "timeout":
        fakes.wait(fault.get("ms", 5000))
        return {"error": "the tool timed out"}, True, kind, None
    elif kind == "error":
        return {"error": fault.get("message") or "the tool failed"}, True, kind, None
    elif kind == "rate_limit":
        return {"error": "rate limit exceeded; retry after %ds" % fault.get("retry_after", 30), "retryAfter": fault.get("retry_after", 30)}, True, kind, None
    elif kind == "drop":
        return None, True, kind, "drop:" + fault.get("mode", "close")
    outcome = fakes.behave({k: v for k, v in tool.items() if k in {"results", "state", "default", "inject"}}, scope, run or "_", agent or "")
    if fault and kind in ("partial", "stale", "wrong_shape", "empty") and not outcome.error:
        outcome = fakes.degrade(fault, outcome, run, name, arguments)
    if not outcome.error and not fault:
        fakes.remember(run, name, arguments, outcome.result)
    return outcome.result, outcome.error, kind, None


def _text(result):
    return result if isinstance(result, str) else json.dumps(result)


def tool_result_body(result, is_error):
    body = {"content": [{"type": "text", "text": _text(result)}], "isError": bool(is_error)}
    if isinstance(result, (dict, list)) and not is_error:
        body["structuredContent"] = result if isinstance(result, dict) else {"result": result}
    return body


# -- proxy ----------------------------------------------------------------------------------

class Upstream:
    """A real MCP server, spoken to over streamable HTTP."""

    def __init__(self, proxy):
        self.url, self.credential, self.session, self.lock, self.ready = proxy["url"], proxy.get("credential"), None, threading.Lock(), False
        self.id = 0

    def _send(self, message, expect):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.credential:
            headers.update(credentials.headers(self.credential))
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, json.dumps(message).encode(), headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                self.session = resp.headers.get("Mcp-Session-Id") or self.session
                text = resp.read().decode("utf-8", "replace")
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raise ConnectionError("the real server answered HTTP %d: %s" % (exc.code, exc.read().decode("utf-8", "replace")[:200]))
        except (urllib.error.URLError, OSError) as exc:
            raise ConnectionError("could not reach the real server: %s" % getattr(exc, "reason", exc))
        if not expect or not text.strip():
            return None
        if "event-stream" in ctype:
            for block in text.split("\n\n"):
                data = "\n".join(l[5:].lstrip() for l in block.splitlines() if l.startswith("data:"))
                if data:
                    try:
                        msg = json.loads(data)
                    except ValueError:
                        continue
                    if msg.get("id") == message.get("id"):
                        return msg
            raise ConnectionError("the real server's stream held no answer")
        return json.loads(text)

    def rpc(self, method, params):
        with self.lock:
            if not self.ready:
                self.id += 1
                self._send({"jsonrpc": "2.0", "id": self.id, "method": "initialize",
                            "params": {"protocolVersion": SUPPORTED[0], "capabilities": {}, "clientInfo": {"name": "llmsim", "version": "1"}}}, True)
                self._send({"jsonrpc": "2.0", "method": "notifications/initialized"}, False)
                self.ready = True
            self.id += 1
            return self._send({"jsonrpc": "2.0", "id": self.id, "method": method, "params": params}, True)


_UPSTREAM = {}


def upstream(defn):
    with _LOCK:
        key = defn["id"]
        if key not in _UPSTREAM or _UPSTREAM[key].url != defn["proxy"]["url"]:
            _UPSTREAM[key] = Upstream(defn["proxy"])
        return _UPSTREAM[key]


def cassette(defn):
    name = defn["proxy"].get("cassette") or defn["id"]
    return name, store().get("toolcassettes", name) or {"listing": None, "calls": {}}


def proxied(defn, method, params, rid):
    """A JSON-RPC response for a request to a proxying server, or None to serve it as a mock."""
    mode = defn["proxy"]["mode"]
    if mode == "replay":
        name, cas = cassette(defn)
        if method == "tools/list":
            if cas["listing"] is None:
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "LLMSim has no recorded tool list in cassette %s" % name}}
            return {"jsonrpc": "2.0", "id": rid, "result": cas["listing"]}
        if method == "tools/call":
            hit = cas["calls"].get(core.digest([params.get("name"), params.get("arguments") or {}]))
            if hit is None:
                return {"jsonrpc": "2.0", "id": rid, "result": tool_result_body(
                    {"error": "LLMSim has no recording of %s(%s) in cassette %s" % (params.get("name"), core.dumps(params.get("arguments") or {})[:120], name)}, True)}
            return {"jsonrpc": "2.0", "id": rid, "result": hit}
        return None
    try:
        got = upstream(defn).rpc(method, params)
    except (ConnectionError, ValueError) as exc:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(exc)}}
    if mode == "record" and got and "result" in got:
        name, cas = cassette(defn)
        if method == "tools/list":
            cas["listing"] = got["result"]
        elif method == "tools/call":
            cas["calls"][core.digest([params.get("name"), params.get("arguments") or {}])] = got["result"]
        store().put("toolcassettes", name, cas)
    return dict(got or {}, id=rid)


# -- the endpoint ------------------------------------------------------------------------------

def rpc_response(defn, message, run, agent):
    method, params, rid = message.get("method"), message.get("params") or {}, message.get("id")
    if rid is None:
        return None                                      # a notification has no answer
    if defn.get("proxy") and method in ("tools/list", "tools/call"):
        got = proxied(defn, method, params, rid)
        if got is not None:
            if method == "tools/call" and run:
                result = got.get("result") or {}
                timeline.emit(run, "tool_call", agent, timeline.link_tool_call(run, agent, params.get("name"), params.get("arguments") or {}),
                              {"tool": params.get("name"), "server": defn["id"], "arguments": params.get("arguments") or {},
                               "result": result.get("structuredContent") or (result.get("content") or [{}])[0].get("text"),
                               "error": bool(result.get("isError") or got.get("error")), "via": "proxy:" + defn["proxy"]["mode"]})
            return got
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": asked if asked in SUPPORTED else SUPPORTED[0],
                                                         "capabilities": {"tools": {"listChanged": False}},
                                                         "serverInfo": {"name": "llmsim-mock-" + defn["id"], "version": "1.0.0"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": t["name"], "description": t.get("description", ""), "inputSchema": t.get("inputSchema") or {"type": "object", "properties": {}}}
            for t in defn.get("tools", [])]}}
    if method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments") or {}
        started = time.time()
        if not any(t["name"] == name for t in defn.get("tools", [])):
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Unknown tool: %s" % name}}
        result, is_error, fault, special = call_tool(defn, name, arguments, run, agent)
        if special and special.startswith("drop"):
            return ("drop", special.split(":", 1)[1])
        if run:
            timeline.emit(run, "tool_call", agent, timeline.link_tool_call(run, agent, name, arguments),
                          {"tool": name, "server": defn["id"], "arguments": arguments, "result": result, "error": bool(is_error),
                           "fault": fault or "", "ms": int((time.time() - started) * 1000)})
        if special == "invalid":
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": result["error"]}}
        return {"jsonrpc": "2.0", "id": rid, "result": tool_result_body(result, is_error)}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found: %s" % method}}


@route("/mcp/")
def _endpoint(handler, method, path, rest, raw):
    name = rest.split("/")[0]
    ident = core.identity(handler)
    defn = definition(name, ident.get("run"))
    if defn is None:
        return core.reply_json(handler, 404, {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "There is no mock MCP server called %s." % name}})
    if method == "GET":
        return core.reply_json(handler, 405, {"error": "this server does not offer a stream; POST JSON-RPC to it"}, {"Allow": "POST"})
    if method == "DELETE":
        return core.reply_json(handler, 200, {})
    if method != "POST":
        return core.reply_json(handler, 405, {"error": "POST JSON-RPC"})
    try:
        message = json.loads(raw or b"{}")
    except ValueError:
        return core.reply_json(handler, 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
    if isinstance(message, list):
        answers = [a for a in (rpc_response(defn, m, ident.get("run"), ident.get("agent")) for m in message) if a]
        return core.reply_json(handler, 200, answers) if answers else core.reply_raw(handler, 202, b"", "application/json")
    out = rpc_response(defn, message, ident.get("run"), ident.get("agent"))
    if out is None:
        return core.reply_raw(handler, 202, b"", "application/json")
    if isinstance(out, tuple) and out[0] == "drop":
        return core.hang_up(handler, out[1])
    headers = {"Mcp-Session-Id": "llmsim-" + defn["id"]} if message.get("method") == "initialize" else {}
    accept = handler.headers.get("Accept", "")
    if "text/event-stream" in accept and "application/json" not in accept:
        return core.reply_raw(handler, 200, ("event: message\ndata: %s\n\n" % json.dumps(out)).encode(), "text/event-stream", headers)
    core.reply_json(handler, 200, out, headers)


# -- REST ---------------------------------------------------------------------------------------------

@rest_area("mcp")
def _rest(method, rest, body, query, host):
    st = store()
    if method == "GET" and not rest:
        return ok({"servers": [{"id": i, "tools": [t["name"] for t in d.get("tools", [])], "proxy": (d.get("proxy") or {}).get("mode"),
                                "description": d.get("description", "")} for i, d, _ in st.docs("mcp")]})
    if method == "POST" and rest == ["validate"]:
        problems = validate((body or {}).get("definition"))
        return ok(valid=not problems, problems=problems)
    if len(rest) >= 1 and core.valid_id(rest[0]):
        name, found = rest[0], st.get("mcp", rest[0])
        if method == "PUT" and len(rest) == 1:
            defn = dict(body or {}, id=name)
            problems = validate(defn)
            if problems:
                return fail(400, "That server has %d problem(s): %s" % (len(problems), "; ".join(problems[:5])), problems=problems)
            st.put("mcp", name, defn)
            return ok({"server": defn})
        if method == "GET" and len(rest) == 1:
            return ok({"server": found}) if found else fail(404, "There is no mock MCP server called %s." % name)
        if method == "DELETE" and len(rest) == 1:
            return ok(removed=st.drop("mcp", name)) if found else fail(404, "There is no mock MCP server called %s." % name)
        if method == "GET" and rest[1:] == ["url"]:
            run, agent = (query.get("run") or ["RUN"])[0], (query.get("agent") or [""])[0]
            return ok({"url": "http://%s/run/%s%s/mcp/%s" % (host, run, "/agent/" + agent if agent else "", name)})
        if method == "POST" and rest[1:] == ["call"]:
            if not found:
                return fail(404, "There is no mock MCP server called %s." % name)
            body = body or {}
            result, is_error, fault, special = call_tool(found, body.get("tool"), body.get("arguments") or {}, body.get("run"), body.get("agent"))
            return ok({"result": result, "isError": bool(is_error), "fault": fault, "problem": special})
        if method == "DELETE" and rest[1:] == ["cassette"] and found and found.get("proxy"):
            return ok(removed=st.drop("toolcassettes", found["proxy"].get("cassette") or name))
    return fail(404, "No such route: %s /api/mcp/%s" % (method, "/".join(rest)))
