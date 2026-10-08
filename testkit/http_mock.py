"""A mock HTTP backend: the REST API an agent's tool calls.

Not every tool is an MCP tool. Plenty are a function in the agent's own code that calls a REST API
— a CRM, an ERP, a ticketing system. This is that API, written down in a file or read from its
OpenAPI description, served at  /run/<run>/agent/<agent>/mock/<name>/…

    {"id": "crm", "routes": [
        {"method": "GET",  "path": "/orders/{id}", "state": {"op": "get", "collection": "orders", "key": "{{params.id}}"}},
        {"method": "POST", "path": "/orders/{id}/refund",
         "state": {"op": "update", "collection": "orders", "key": "{{params.id}}", "set": {"status": "refunded"}}},
        {"method": "GET",  "path": "/status", "default": {"result": {"ok": true}}}]}

Without a contract it is exactly what is written. With one — written by hand, or imported from an
OpenAPI document — a request that is not in it is refused and says why, which is the half of a
backend an agent's tests are usually missing: it will accept anything an agent invents.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import checks, core, credentials, fakes, mcp_mock, route, rest_area, timeline
from .core import fail, ok
from .store import store

ROUTE_KEYS = {"method", "path", "results", "state", "default", "inject", "contract", "description"}
DEF_KEYS = {"id", "description", "routes", "proxy", "strict"}
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _compile(path):
    names = []

    def one(m):
        names.append(m.group(1))
        return "([^/]+)"
    return re.compile("^" + re.sub(r"\{([A-Za-z0-9_]+)\}", one, re.escape(path).replace(r"\{", "{").replace(r"\}", "}")) + "$"), names


def validate(definition):
    if not isinstance(definition, dict):
        return ["a mock backend is an object"]
    problems = ["unknown field %r" % k for k in definition if k not in DEF_KEYS]
    if not core.valid_id(definition.get("id")):
        problems.append("id is letters, digits, dot, dash, underscore or colon")
    proxy = definition.get("proxy")
    if proxy is not None and (not isinstance(proxy, dict) or proxy.get("mode") not in ("record", "replay", "passthrough")
                              or not isinstance(proxy.get("baseUrl"), str)):
        problems.append("proxy is {baseUrl, mode: record|replay|passthrough, credential?}")
    routes = definition.get("routes", [])
    if not isinstance(routes, list) or (not routes and proxy is None):
        problems.append("routes is a list of at least one route (or give a proxy)")
        return problems
    seen = set()
    for i, r in enumerate(routes):
        where = "routes[%d]" % i
        if not isinstance(r, dict):
            problems.append("%s is an object" % where)
            continue
        problems += ["%s: unknown field %r" % (where, k) for k in r if k not in ROUTE_KEYS]
        if r.get("method") not in METHODS:
            problems.append("%s.method is one of %s" % (where, ", ".join(METHODS)))
        if not isinstance(r.get("path"), str) or not r["path"].startswith("/"):
            problems.append("%s.path starts with / (use {name} for a parameter)" % where)
        elif (r.get("method"), r["path"]) in seen:
            problems.append("%s: %s %s is defined twice" % (where, r.get("method"), r["path"]))
        else:
            seen.add((r.get("method"), r["path"]))
        problems += fakes.validate_behavior({k: v for k, v in r.items() if k in {"results", "state", "default", "inject"}}, where)
    return problems


def definition(name, run=None):
    return mcp_mock._RUN_DEFS.get((run, "http:" + name)) if run and (run, "http:" + name) in mcp_mock._RUN_DEFS else store().get("http", name)


def install_for_run(run, definition_):
    mcp_mock._RUN_DEFS[(run, "http:" + definition_["id"])] = definition_
    return lambda: mcp_mock._RUN_DEFS.pop((run, "http:" + definition_["id"]), None)


# -- OpenAPI -----------------------------------------------------------------------------

def example_of(schema, depth=0):
    """A plausible value for a JSON Schema, for the answer a contract does not spell out."""
    if not isinstance(schema, dict) or depth > 5:
        return None
    for key in ("example", "default", "const"):
        if key in schema:
            return schema[key]
    if schema.get("enum"):
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        return {k: example_of(v, depth + 1) for k, v in (schema.get("properties") or {}).items()}
    if t == "array":
        item = example_of(schema.get("items"), depth + 1)
        return [] if item is None else [item]
    return {"string": "string", "integer": 0, "number": 0, "boolean": True}.get(t)


def from_openapi(doc, name):
    """A mock backend definition from an OpenAPI 3 document (JSON). $ref is followed inside the document."""
    if not isinstance(doc, dict) or not isinstance(doc.get("paths"), dict):
        raise ValueError("that is not an OpenAPI document: it has no paths")

    def deref(node, depth=0):
        if isinstance(node, dict) and "$ref" in node and depth < 10:
            target = doc
            for part in node["$ref"].lstrip("#/").split("/"):
                target = target.get(part, {}) if isinstance(target, dict) else {}
            return deref(target, depth + 1)
        return node

    base = ""
    servers = doc.get("servers") or []
    if servers and isinstance(servers[0], dict):
        base = urllib.parse.urlsplit(servers[0].get("url", "")).path.rstrip("/")
    routes = []
    for path, item in doc["paths"].items():
        item = deref(item)
        shared = [deref(p) for p in item.get("parameters") or []]
        for method in METHODS:
            op = item.get(method.lower())
            if not isinstance(op, dict):
                continue
            params = [deref(p) for p in op.get("parameters") or []] + shared
            body = deref((((deref(op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")) or {})
            answers = op.get("responses") or {}
            code = next((c for c in sorted(answers) if str(c).startswith("2")), None)
            content = ((deref(answers.get(code) or {}).get("content") or {}).get("application/json") or {}) if code else {}
            sample = content.get("example")
            if sample is None and content.get("examples"):
                sample = (deref(next(iter(content["examples"].values()))) or {}).get("value")
            if sample is None:
                sample = example_of(deref(content.get("schema") or {}))
            routes.append({"method": method, "path": base + path, "description": op.get("summary", ""),
                           "contract": {"parameters": [{"name": p.get("name"), "in": p.get("in"), "required": bool(p.get("required")),
                                                          "schema": deref(p.get("schema") or {})} for p in params if isinstance(p, dict)],
                                        "body": body or None, "required": bool(deref(op.get("requestBody") or {}).get("required"))},
                           "results": [{"when": {}, "result": sample if sample is not None else {}, "status": int(code or 200)}]})
    return {"id": name, "description": (doc.get("info") or {}).get("title", ""), "strict": True, "routes": routes}


def contract_problems(route_, params, query, body):
    c = route_.get("contract")
    if not c:
        return []
    problems = []
    for p in c.get("parameters") or []:
        src = {"path": params, "query": query}.get(p.get("in"))
        if src is None:
            continue
        if p.get("required") and p["name"] not in src:
            problems.append("the %s parameter %s is required" % (p["in"], p["name"]))
        elif p["name"] in src and p.get("schema", {}).get("type") in ("integer", "number"):
            try:
                float(src[p["name"]])
            except (TypeError, ValueError):
                problems.append("%s must be a number" % p["name"])
    if c.get("body"):
        if body is None and c.get("required"):
            problems.append("a JSON body is required")
        elif body is not None:
            problems += checks.schema_errors(body, c["body"], "body")
    return problems


# -- serving ------------------------------------------------------------------------------

def match_route(defn, method, path):
    """(route, params, path_allowed_other_methods)"""
    other = False
    ranked = sorted(defn.get("routes", []), key=lambda r: -len(r["path"].replace("{", "").split("/")))
    for r in ranked:
        rx, names = _compile(r["path"])
        m = rx.match(path)
        if not m:
            continue
        if r["method"] != method:
            other = True
            continue
        return r, dict(zip(names, (urllib.parse.unquote(v) for v in m.groups()))), other
    return None, {}, other


def cassette_key(method, path, query, body):
    return core.digest([method, path, sorted(query.items()), body])


def proxy_request(defn, method, path, query, body, raw_body):
    proxy = defn["proxy"]
    name = defn["id"]
    cas = store().get("httpcassettes", name) or {"calls": {}}
    key = cassette_key(method, path, query, body)
    if proxy["mode"] == "replay":
        hit = cas["calls"].get(key)
        if hit is None:
            return 404, {"error": "LLMSim has no recording of %s %s in cassette %s" % (method, path, name)}
        return hit["status"], hit["body"]
    url = proxy["baseUrl"].rstrip("/") + path + ("?" + urllib.parse.urlencode(query) if query else "")
    headers = {"Content-Type": "application/json"}
    if proxy.get("credential"):
        try:
            headers.update(credentials.headers(proxy["credential"]))
        except KeyError:
            return 502, {"error": "the credential %s is not stored" % proxy["credential"]}
    req = urllib.request.Request(url, raw_body or None, headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status, text = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status, text = exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return 502, {"error": "could not reach the real backend: %s" % getattr(exc, "reason", exc)}
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = text
    if proxy["mode"] == "record":
        cas["calls"][key] = {"status": status, "body": parsed}
        store().put("httpcassettes", name, cas)
    return status, parsed


def serve_call(defn, method, path, query, body, raw_body, headers, run, agent):
    """(status, body, extra_headers, special) for one request to a mock backend."""
    started = time.time()
    if defn.get("proxy") and defn["proxy"]["mode"] != "passthrough" or (defn.get("proxy") and not defn.get("routes")):
        status, out = proxy_request(defn, method, path, query, body, raw_body)
        if run:
            timeline.emit(run, "tool_call", agent, None, {"tool": "%s %s" % (method, path), "server": defn["id"], "arguments": {"query": query, "body": body},
                                                          "result": out, "status": status, "error": status >= 400, "via": "proxy:" + defn["proxy"]["mode"],
                                                          "ms": int((time.time() - started) * 1000)})
        return status, out, {}, None
    r, params, other = match_route(defn, method, path)
    if r is None:
        if defn.get("proxy"):
            status, out = proxy_request(defn, method, path, query, body, raw_body)
            return status, out, {}, None
        msg = "%s %s is not in this backend's contract" % (method, path)
        return (405 if other else 404), {"error": msg + ("; the path exists under another method" if other else "")}, {}, None
    problems = contract_problems(r, params, query, body)
    label = "%s %s" % (method, r["path"])
    arguments = dict(body if isinstance(body, dict) else {}, **query, **params)
    scope = {"method": method, "path": path, "params": params, "query": query, "body": body, "headers": headers, "arguments": arguments,
             "run": run or "", "agent": agent or "", "tool": label, "server": defn["id"], "identity": {"run": run or "", "agent": agent or ""}}
    if problems:
        status, out, fault = 400, {"error": "the request breaks the contract", "problems": problems}, None
        special = None
    else:
        rule, fault_spec = fakes.pick("tool", dict(scope, target="tool"))
        fault = fault_spec["type"] if fault_spec else None
        special = None
        if fault == "delay":
            fakes.wait(fault_spec["ms"])
        elif fault == "timeout":
            fakes.wait(fault_spec.get("ms", 5000))
            status, out = 504, {"error": "the backend timed out"}
        elif fault == "error":
            status, out = fault_spec.get("status", 500), {"error": fault_spec.get("message") or "the backend failed"}
        elif fault == "rate_limit":
            status, out, special = 429, {"error": "rate limit exceeded"}, {"Retry-After": fault_spec.get("retry_after", 30)}
        elif fault == "drop":
            return 0, None, {}, "drop:" + fault_spec.get("mode", "close")
        if fault in ("timeout", "error", "rate_limit"):
            pass
        else:
            outcome = fakes.behave({k: v for k, v in r.items() if k in {"results", "state", "default", "inject"}}, scope, run or "_", agent or "")
            if fault_spec and fault in ("partial", "stale", "wrong_shape", "empty") and not outcome.error:
                outcome = fakes.degrade(fault_spec, outcome, run, label, arguments)
            status = outcome.status or (400 if outcome.error else 200)
            out = outcome.result
            special = outcome.headers or None
            if not outcome.error and not fault:
                fakes.remember(run, label, arguments, out)
    if run:
        timeline.emit(run, "tool_call", agent, None, {"tool": label, "server": defn["id"], "arguments": arguments, "result": out, "status": status,
                                                      "error": status >= 400, "fault": fault or "", "via": "http", "ms": int((time.time() - started) * 1000)})
    return status, out, (special if isinstance(special, dict) else {}), None


@route("/mock/")
def _endpoint(handler, method, path, rest, raw):
    name, _, tail = rest.partition("/")
    ident = core.identity(handler)
    defn = definition(name, ident.get("run"))
    if defn is None:
        return core.reply_json(handler, 404, {"error": "There is no mock backend called %s." % name})
    split = urllib.parse.urlsplit(handler.path)
    query = {k: v[0] for k, v in urllib.parse.parse_qs(split.query).items()}
    sub = "/" + tail.split("?")[0]
    body = None
    if raw and raw.strip():
        try:
            body = json.loads(raw)
        except ValueError:
            return core.reply_json(handler, 400, {"error": "the body is not valid JSON"})
    status, out, extra, special = serve_call(defn, method, sub.rstrip("/") or "/", query, body, raw, {k.lower(): v for k, v in handler.headers.items()
                                                                                                      if k.lower() not in ("authorization", "cookie")},
                                             ident.get("run"), ident.get("agent"))
    if special and special.startswith("drop"):
        return core.hang_up(handler, special.split(":", 1)[1])
    core.reply_json(handler, status, out, extra)


# -- REST ------------------------------------------------------------------------------------

@rest_area("mocks")
def _rest(method, rest, body, query, host):
    st = store()
    if method == "GET" and not rest:
        return ok({"backends": [{"id": i, "routes": ["%s %s" % (r["method"], r["path"]) for r in d.get("routes", [])], "proxy": (d.get("proxy") or {}).get("mode"),
                                 "description": d.get("description", "")} for i, d, _ in st.docs("http")]})
    if method == "POST" and rest == ["validate"]:
        problems = validate((body or {}).get("definition"))
        return ok(valid=not problems, problems=problems)
    if method == "POST" and len(rest) == 2 and rest[1] == "openapi" and core.valid_id(rest[0]):
        try:
            defn = from_openapi((body or {}).get("openapi"), rest[0])
        except ValueError as exc:
            return fail(400, str(exc))
        problems = validate(defn)
        if problems:
            return fail(400, "The import has %d problem(s): %s" % (len(problems), "; ".join(problems[:5])), problems=problems)
        st.put("http", rest[0], defn)
        return ok({"backend": defn, "routes": len(defn["routes"])})
    if len(rest) >= 1 and core.valid_id(rest[0]):
        name, found = rest[0], st.get("http", rest[0])
        if method == "PUT" and len(rest) == 1:
            defn = dict(body or {}, id=name)
            problems = validate(defn)
            if problems:
                return fail(400, "That backend has %d problem(s): %s" % (len(problems), "; ".join(problems[:5])), problems=problems)
            st.put("http", name, defn)
            return ok({"backend": defn})
        if method == "GET" and len(rest) == 1:
            return ok({"backend": found}) if found else fail(404, "There is no mock backend called %s." % name)
        if method == "DELETE" and len(rest) == 1:
            return ok(removed=st.drop("http", name)) if found else fail(404, "There is no mock backend called %s." % name)
        if method == "GET" and rest[1:] == ["url"]:
            run, agent = (query.get("run") or ["RUN"])[0], (query.get("agent") or [""])[0]
            return ok({"url": "http://%s/run/%s%s/mock/%s" % (host, run, "/agent/" + agent if agent else "", name)})
    return fail(404, "No such route: %s /api/mocks/%s" % (method, "/".join(rest)))
