"""Two small real services, and the agent's OpenTelemetry export.

The services are what a test records once and replays without: a real MCP server and a real REST
API, served beside the agent so LLMSim's proxies have something real to stand in front of.

    POST /services/mcp                      MCP over streamable HTTP: exchange_rate, public_holidays
    GET  /services/rest/rates/{pair}        an exchange rate, e.g. EUR-USD
    GET  /services/rest/holidays/{country}  a country's public holidays this year

Their answers are fixed tables, so a recording of them is the same every time it is made.

Spans: when AGENT_OTLP_URL is set, each turn is also reported as OpenTelemetry spans (OTLP/HTTP,
JSON) — one per model call with its token usage, one per tool call with its arguments and result.
That is how an agent whose model LLMSim does not stand in front of is still seen by it.
"""

import json
import os
import time
import urllib.request
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from langchain_core.messages import AIMessage, ToolMessage

router = APIRouter()

RATES = {"EUR-USD": 1.0842, "USD-EUR": 0.9223, "GBP-USD": 1.2671, "USD-JPY": 151.37, "EUR-GBP": 0.8556}
HOLIDAYS = {"US": ["01-01 New Year's Day", "07-04 Independence Day", "11-26 Thanksgiving Day", "12-25 Christmas Day"],
            "GB": ["01-01 New Year's Day", "12-25 Christmas Day", "12-26 Boxing Day"],
            "IL": ["04-02 Pesach", "09-21 Rosh Hashanah", "09-30 Yom Kippur"]}
MCP_TOOLS = [
    {"name": "exchange_rate", "description": "The exchange rate between two currencies, e.g. EUR-USD",
     "inputSchema": {"type": "object", "required": ["pair"], "properties": {"pair": {"type": "string", "description": "FROM-TO, e.g. EUR-USD"}}}},
    {"name": "public_holidays", "description": "A country's public holidays this year, by its two-letter code",
     "inputSchema": {"type": "object", "required": ["country"], "properties": {"country": {"type": "string"}}}}]


def _rate(pair):
    pair = pair.upper().replace("/", "-")
    if pair not in RATES:
        return None
    return {"pair": pair, "rate": RATES[pair], "asOf": "2026-10-28"}


@router.get("/services/rest/rates/{pair}")
def rest_rate(pair: str):
    got = _rate(pair)
    if not got:
        raise HTTPException(404, f"no rate for {pair}")
    return got


@router.get("/services/rest/holidays/{country}")
def rest_holidays(country: str):
    days = HOLIDAYS.get(country.upper())
    if days is None:
        raise HTTPException(404, f"no holidays known for {country}")
    return {"country": country.upper(), "year": 2026, "holidays": days}


@router.post("/services/mcp")
def mcp(body: dict):
    rid, method, params = body.get("id"), body.get("method"), body.get("params") or {}
    if rid is None:                                   # a notification: nothing to answer
        return JSONResponse(None, 202)
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "rates-and-holidays", "version": "1.0.0"}}
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result}, headers={"Mcp-Session-Id": uuid.uuid4().hex})
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": MCP_TOOLS}}
    if method == "tools/call":
        args = params.get("arguments") or {}
        if params.get("name") == "exchange_rate":
            got = _rate(str(args.get("pair", "")))
        elif params.get("name") == "public_holidays":
            days = HOLIDAYS.get(str(args.get("country", "")).upper())
            got = {"country": str(args.get("country", "")).upper(), "holidays": days} if days is not None else None
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"no tool {params.get('name')}"}}
        if got is None:
            return {"jsonrpc": "2.0", "id": rid, "result": {"isError": True, "content": [{"type": "text", "text": "not found"}]}}
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(got)}], "structuredContent": got}}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"{method} is not supported"}}


# -- OpenTelemetry ----------------------------------------------------------------------

def _attr(key, value):
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": value if isinstance(value, str) else json.dumps(value, default=str)}
    return {"key": key, "value": v}


class Spans:
    """The spans of one turn, sent when it ends. Nothing is kept or sent when AGENT_OTLP_URL is unset."""

    def __init__(self, test_run):
        self.url = os.environ.get("AGENT_OTLP_URL", "")
        self.run, self.agent = (test_run or {}).get("run"), (test_run or {}).get("agent")
        self.trace, self.root, self.spans, self.calls, self.last = uuid.uuid4().hex, uuid.uuid4().hex[:16], [], {}, time.time_ns()

    def _span(self, name, attrs, start, end, error=False):
        self.spans.append({"traceId": self.trace, "spanId": uuid.uuid4().hex[:16], "parentSpanId": self.root, "name": name,
                           "kind": 3, "startTimeUnixNano": str(start), "endTimeUnixNano": str(end),
                           "attributes": [_attr(k, v) for k, v in attrs.items() if v is not None],
                           "status": {"code": 2 if error else 1}})

    def saw(self, m):
        if not self.url:
            return
        now = time.time_ns()
        if isinstance(m, AIMessage):
            usage = m.usage_metadata or {}
            self._span("chat " + os.environ.get("MODEL_NAME", "model"), {
                "gen_ai.operation.name": "chat", "gen_ai.system": "openai", "gen_ai.request.model": os.environ.get("MODEL_NAME", "model"),
                "gen_ai.usage.input_tokens": int(usage.get("input_tokens") or 0), "gen_ai.usage.output_tokens": int(usage.get("output_tokens") or 0)},
                self.last, now)
            for c in m.tool_calls or []:
                self.calls[c["id"]] = (c["name"], c["args"], now)
        elif isinstance(m, ToolMessage):
            name, args, start = self.calls.get(m.tool_call_id, (m.name, {}, self.last))
            text = m.content if isinstance(m.content, str) else json.dumps(m.content, default=str)
            self._span("execute_tool " + name, {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name,
                                                "gen_ai.tool.call.arguments": json.dumps(args), "gen_ai.tool.call.result": text[:2000]},
                       start, now, error=text.startswith("error"))
        self.last = now

    def send(self):
        if not self.url or not self.spans:
            return
        resource = [_attr("service.name", self.agent or "langgraph-assistant")] + ([_attr("llmsim.run", self.run)] if self.run else [])
        if self.agent:
            resource.append(_attr("llmsim.agent", self.agent))
        payload = {"resourceSpans": [{"resource": {"attributes": resource},
                                      "scopeSpans": [{"scope": {"name": "langgraph-agent"}, "spans": self.spans}]}]}
        req = urllib.request.Request(self.url, json.dumps(payload).encode(), {"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except OSError:
            pass                                       # telemetry never breaks a turn
