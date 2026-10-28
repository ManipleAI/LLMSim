"""How the team's agents reach what is outside them: MCP tool servers, REST APIs, other agents.

Each is a LangChain tool built for one conversation, because where it points depends on the
conversation: under test, LLMSim serves the MCP server, the REST backend and the other agents
at addresses that carry the test run (/run/<run>/agent/<name>/mcp/<id>, …/mock/<id>,
…/a2a/<id>, …/a2a-proxy/<id>), so whatever they see is part of that run.

    mcp_tools(url)          the tools an MCP server lists, one LangChain tool each
    rest_tool(base)         call_api(method, path, body): a REST API under one base address
    peer_tool(peers)        ask_agent(agent, task): another agent, over A2A (JSON-RPC 0.3)

They speak the wire protocols themselves, with the standard library, so the agent has no
client library between it and what a test is checking.
"""

import json
import time
import urllib.error
import urllib.request
import uuid

from langchain_core.tools import StructuredTool

TIMEOUT_S = 60


def _post(url, body, headers=None, timeout=TIMEOUT_S):
    """(status, headers, text). An HTTP error status is returned, not raised."""
    req = urllib.request.Request(url, json.dumps(body).encode(), dict({"Content-Type": "application/json",
                                 "Accept": "application/json, text/event-stream"}, **(headers or {})), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers.items()), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read().decode("utf-8", "replace")


def _rpc_answer(text, headers, want_id):
    """A JSON-RPC answer, sent as JSON or as one event of a stream."""
    if "event-stream" in (headers.get("Content-Type") or headers.get("content-type") or ""):
        for block in text.split("\n\n"):
            data = "\n".join(l[5:].lstrip() for l in block.splitlines() if l.startswith("data:"))
            if data:
                msg = json.loads(data)
                if msg.get("id") == want_id:
                    return msg
        raise ValueError("the stream held no answer")
    return json.loads(text)


# -- MCP ---------------------------------------------------------------------------------

class MCPClient:
    """Streamable HTTP, the part a tool-using agent needs: initialize, tools/list, tools/call."""

    def __init__(self, url, headers=None):
        self.url, self.headers, self.session, self.id = url, dict(headers or {}), None, 0

    def rpc(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if not notify:
            self.id += 1
            msg.update(id=self.id, params=params or {})
        headers = dict(self.headers, **({"Mcp-Session-Id": self.session} if self.session else {}))
        status, hdrs, text = _post(self.url, msg, headers)
        self.session = hdrs.get("Mcp-Session-Id") or hdrs.get("mcp-session-id") or self.session
        if notify:
            return None
        if status >= 400:
            raise ConnectionError(f"the tool server answered HTTP {status}: {text[:200]}")
        answer = _rpc_answer(text, hdrs, msg["id"])
        if answer.get("error"):
            raise ConnectionError(answer["error"].get("message") or json.dumps(answer["error"]))
        return answer.get("result") or {}

    def start(self):
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "langgraph-team", "version": "1"}})
        self.rpc("notifications/initialized", notify=True)
        return self


def _content_text(result):
    parts = [c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text"]
    if not parts and result.get("structuredContent") is not None:
        parts = [json.dumps(result["structuredContent"])]
    text = "\n".join(parts)
    return f"error: {text}" if result.get("isError") else text


def mcp_tools(url, headers=None):
    """One LangChain tool per tool the server lists. A server that cannot be reached gives no
    tools, and the agent goes on without them rather than failing the whole conversation."""
    try:
        client = MCPClient(url, headers).start()
        listed = client.rpc("tools/list").get("tools") or []
    except (ConnectionError, OSError, ValueError):
        return []

    def make(t):
        def run(**arguments):
            try:
                return _content_text(client.rpc("tools/call", {"name": t["name"], "arguments": arguments}))
            except (ConnectionError, OSError, ValueError) as exc:
                return f"error: {t['name']} failed: {exc}"
        schema = dict(t.get("inputSchema") or {"type": "object", "properties": {}})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema["title"], schema["description"] = t["name"], t.get("description") or t["name"]
        return StructuredTool(name=t["name"], description=t.get("description") or t["name"], args_schema=schema, func=run)

    return [make(t) for t in listed]


# -- REST --------------------------------------------------------------------------------

def rest_tool(base, name="call_api", about="the company's REST API"):
    def call_api(method: str, path: str, body: dict | None = None) -> str:
        """method is GET, POST, PUT, PATCH or DELETE; path starts with /; body is the JSON to send, if any."""
        url = base.rstrip("/") + "/" + path.lstrip("/")
        req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None,
                                     {"Content-Type": "application/json", "Accept": "application/json"}, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                return f"HTTP {r.status}\n{r.read().decode('utf-8', 'replace')[:4000]}"
        except urllib.error.HTTPError as exc:
            return f"error: HTTP {exc.code}\n{exc.read().decode('utf-8', 'replace')[:1000]}"
        except (urllib.error.URLError, OSError) as exc:
            return f"error: could not reach the API: {getattr(exc, 'reason', exc)}"

    return StructuredTool.from_function(call_api, name=name,
                                        description=f"Call {about}. Say the method, the path, and a JSON body when the call needs one.")


# -- A2A ---------------------------------------------------------------------------------

POLLS, POLL_S = 20, 0.3


def a2a_text(parts):
    return "".join(p.get("text", "") for p in parts or [] if p.get("kind") == "text" or "text" in p)


def ask(url, task, headers=None, context=None):
    """Send one message to an A2A agent and wait for what it says: a message, or a task that
    finishes, fails or asks a question. Every way it can go wrong is said as text the model reads."""
    msg = {"kind": "message", "role": "user", "messageId": str(uuid.uuid4()), "parts": [{"kind": "text", "text": task}]}
    if context:
        msg["contextId"] = context
    rpc = lambda method, params: _post(url, {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}, headers)
    try:
        status, hdrs, text = rpc("message/send", {"message": msg})
    except (urllib.error.URLError, OSError) as exc:
        return f"error: the agent is unavailable ({getattr(exc, 'reason', exc)})"
    if status >= 400:
        return f"error: the agent is unavailable (HTTP {status})"
    try:
        answer = json.loads(text)
    except ValueError:
        return "error: the agent's answer is not JSON"
    if answer.get("error"):
        return f"error: the agent refused: {answer['error'].get('message')}"
    result = answer.get("result") or {}
    for _ in range(POLLS):
        if result.get("kind") == "message":
            return a2a_text(result.get("parts")) or "error: the agent answered with nothing"
        state = (result.get("status") or {}).get("state")
        said = a2a_text(((result.get("status") or {}).get("message") or {}).get("parts"))
        if not said and result.get("artifacts"):
            said = "".join(a2a_text(a.get("parts")) for a in result["artifacts"])
        if state == "completed":
            return said or "error: the agent finished and said nothing"
        if state in ("input-required", "auth-required"):
            return f"the agent asks: {said}"
        if state in ("failed", "rejected", "canceled"):
            return f"error: the agent's task {state}{': ' + said if said else ''}"
        time.sleep(POLL_S)
        status, hdrs, text = rpc("tasks/get", {"id": result.get("id")})
        if status >= 400:
            return f"error: the agent is unavailable (HTTP {status})"
        result = (json.loads(text).get("result")) or {}
    return "error: the agent did not finish in time"


def peer_tool(peers, headers=None):
    """ask_agent(agent, task) over the agents this conversation was given: {name: url}."""
    names = ", ".join(sorted(peers)) or "none"

    def ask_agent(agent: str, task: str) -> str:
        """agent is one of the agents you can ask; task is what you need from it, with every detail it needs."""
        if agent not in peers:
            return f"error: there is no agent called {agent!r}; you can ask {names}"
        return ask(peers[agent], task, headers)

    return StructuredTool.from_function(ask_agent, name="ask_agent",
                                        description=f"Hand a task to another agent and get its answer. The agents: {names}.")
