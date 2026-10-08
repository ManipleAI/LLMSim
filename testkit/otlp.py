"""A collector for traces, for the agents LLMSim cannot stand in front of.

Some agents cannot be pointed at a model of your choosing — the platform decides. What they can
usually do is export OpenTelemetry. This takes OTLP/HTTP (JSON) at  /otlp/v1/traces  (or
/run/<run>/otlp/v1/traces) and puts what it receives on the same timeline as everything else:

  every span             a `span` event
  a span with gen_ai.tool.name              also a `tool_call` event
  a span with gen_ai.request.model          also a `model_call` event, with its token usage

so the budget, path and authority checks work on an agent whose model LLMSim never saw. Spans are
put in the run named by their trace id (which is what a W3C traceparent carries), by an attribute
`llmsim.run`, or by the URL. They are observed: nothing here can change what the agent does.

JSON only: OTLP's protobuf encoding needs a protobuf library, and this has none. Set
OTEL_EXPORTER_OTLP_PROTOCOL=http/json.
"""

import json

from . import core, route, timeline


def _value(v):
    if not isinstance(v, dict):
        return v
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in v:
            return int(v[key]) if key == "intValue" else v[key]
    if "arrayValue" in v:
        return [_value(x) for x in (v["arrayValue"].get("values") or [])]
    if "kvlistValue" in v:
        return {kv["key"]: _value(kv.get("value")) for kv in v["kvlistValue"].get("values") or []}
    return None


def attrs(items):
    return {kv.get("key"): _value(kv.get("value")) for kv in items or [] if isinstance(kv, dict)}


def _json(text):
    try:
        return json.loads(text) if isinstance(text, str) else text
    except ValueError:
        return text


def ingest(payload, default_run=None):
    """(spans, runs): the spans kept, and the runs they were put in."""
    kept, runs = 0, set()
    for rs in payload.get("resourceSpans") or []:
        resource = attrs((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []:
            for sp in ss.get("spans") or []:
                a = attrs(sp.get("attributes"))
                run = a.get("llmsim.run") or resource.get("llmsim.run") or default_run or sp.get("traceId")
                if not core.valid_id(run):
                    continue
                agent = a.get("llmsim.agent") or a.get("gen_ai.agent.name") or resource.get("service.name") or ""
                agent = agent if core.valid_id(agent) else ""
                start = int(sp.get("startTimeUnixNano") or 0) / 1e9 or None
                end = int(sp.get("endTimeUnixNano") or 0) / 1e9 or None
                ms = int((end - start) * 1000) if start and end else 0
                status = (sp.get("status") or {}).get("code")
                span_id = timeline.emit(run, "span", agent, None, {"name": sp.get("name", ""), "spanId": sp.get("spanId"), "parentSpanId": sp.get("parentSpanId"),
                                                                   "ms": ms, "attributes": a, "error": status in (2, "STATUS_CODE_ERROR"), "via": "otel"}, start)
                if a.get("gen_ai.tool.name"):
                    timeline.emit(run, "tool_call", agent, span_id, {"tool": a["gen_ai.tool.name"], "arguments": _json(a.get("gen_ai.tool.call.arguments")) or {},
                                                                     "result": _json(a.get("gen_ai.tool.call.result")), "error": status in (2, "STATUS_CODE_ERROR"),
                                                                     "ms": ms, "via": "otel"}, start)
                if a.get("gen_ai.request.model"):
                    usage = {"input": int(a.get("gen_ai.usage.input_tokens") or 0), "output": int(a.get("gen_ai.usage.output_tokens") or 0), "cacheRead": 0, "reasoning": 0}
                    timeline.emit(run, "model_call", agent, span_id, {"model": a["gen_ai.request.model"], "provider": a.get("gen_ai.system", ""), "route": "otel",
                                                                     "status": 500 if status in (2, "STATUS_CODE_ERROR") else 200, "ms": ms, "turn": 0, "ask": "",
                                                                     "tools": [], "request": {}, "response": {"text": "", "toolCalls": [], "usage": usage}, "via": "otel"}, start)
                kept += 1
                runs.add(run)
    return kept, sorted(runs)


@route("/otlp/")
def _traces(handler, method, path, rest, raw):
    if method != "POST" or rest.strip("/") != "v1/traces":
        return core.reply_json(handler, 404, {"error": "POST /otlp/v1/traces"})
    ctype = handler.headers.get("Content-Type", "")
    if "json" not in ctype:
        return core.reply_json(handler, 415, {"error": "LLMSim reads OTLP as JSON only; set OTEL_EXPORTER_OTLP_PROTOCOL=http/json"})
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return core.reply_json(handler, 400, {"error": "that is not JSON"})
    kept, runs = ingest(payload, core.identity(handler).get("run"))
    core.reply_json(handler, 200, {"partialSuccess": {}} if kept else {"partialSuccess": {"rejectedSpans": 0, "errorMessage": "no span carried a usable trace id"}})
