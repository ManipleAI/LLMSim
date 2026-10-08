"""A small agent for the kit's tests: it talks to LLMSim as its model and has one tool.

It is deliberately ordinary — an HTTP endpoint that takes a message, asks a model, runs
the tool the model asks for, asks again, and answers. Everything the kit does to an agent
it does to this one, so the tests exercise the kit on the same path a real agent takes.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kit import OPENAI_KEY

ORDERS = {1182: {"id": 1182, "status": "shipped", "total": 240.0}, 7: {"id": 7, "status": "pending", "total": 12.0}}
TOOLS = [{"type": "function", "function": {"name": "lookup_order", "description": "Look an order up",
                                           "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}}}}]


class FakeAgent:
    def __init__(self, sim_base, tool_hook=None):
        self.sim_base, self.seen, self.sessions, self.tool_hook, self.echo = sim_base, [], {}, tool_hook, False
        agent = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                agent.seen.append({"path": self.path, "headers": dict(self.headers.items()), "body": body})
                if self.path == "/chat":
                    try:
                        out = {"output": {"text": agent.answer(body) + (" [auth=%s]" % self.headers.get("Authorization") if agent.echo else "")},
                               "session": body.get("session")}
                    except Exception as exc:  # noqa: BLE001 — an agent whose model refused it
                        return self._send(502, {"error": "the model refused: %s" % exc})
                elif self.path == "/rw":
                    try:
                        out = {"output": {"text": agent.read_modify_write(body)}}
                    except Exception as exc:  # noqa: BLE001
                        return self._send(502, {"error": "the agent failed: %s" % exc})
                elif self.path in ("/chat-mcp", "/chat-http", "/chat-a2a"):
                    try:
                        out = {"output": {"text": agent.answer_with_tools(body, self.path)}}
                    except Exception as exc:  # noqa: BLE001
                        return self._send(502, {"error": "the agent failed: %s" % exc})
                elif self.path == "/chat-v2":
                    out = {"output": {"text": "I cannot help with that."}}        # a version of the agent that regressed: it does nothing
                elif self.path == "/echo":
                    out = {"output": {"text": "echo: " + body.get("input", "")}}
                elif self.path == "/boom":
                    return self._send(500, {"error": "the agent fell over"})
                else:
                    return self._send(404, {"error": "no such route"})
                self._send(200, out)

            def _send(self, status, payload):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        self.httpd.shutdown()

    def model(self, run, name, messages):
        req = urllib.request.Request("%s/run/%s/agent/%s/openai/v1/chat/completions" % (self.sim_base, run, name),
                                     json.dumps({"model": "gpt-5", "messages": messages, "tools": TOOLS}).encode(),
                                     {"Content-Type": "application/json", "Authorization": "Bearer " + OPENAI_KEY})
        return json.loads(urllib.request.urlopen(req, timeout=30).read())["choices"][0]["message"]

    def rpc(self, url, method, params=None, notify=False):
        message = {"jsonrpc": "2.0", "method": method}
        if not notify:
            message.update(id=1, params=params or {})
        req = urllib.request.Request(url, json.dumps(message).encode(), {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode()
        return json.loads(text)["result"] if text and not notify else None

    def http_tool(self, base, name, args):
        """The two REST calls the http agent's tools make."""
        if name == "get_order":
            req = urllib.request.Request("%s/orders/%s" % (base, args["id"]))
        else:
            req = urllib.request.Request("%s/orders/%s/refund" % (base, args["id"]), b"{}", {"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return {"error": "HTTP %d" % exc.code, "body": exc.read().decode()[:200]}

    def read_modify_write(self, body):
        """Read a counter and write it back plus one — through a tool, without asking any model. A race waiting to happen."""
        url = "%s/run/%s/agent/%s/mcp/%s" % (self.sim_base, body["run"], body["agent"], body["mcp"])
        got = self.rpc(url, "tools/call", {"name": "read_counter", "arguments": {}})
        value = got["structuredContent"]["value"]
        self.rpc(url, "tools/call", {"name": "write_counter", "arguments": {"value": value + 1}})
        return "%s read %d and wrote %d" % (body["agent"], value, value + 1)

    def delegate(self, base, run, name, to, task):
        """Ask another agent, over A2A, and wait for it."""
        url = "%s/run/%s/agent/%s/a2a/%s" % (base, run, name, to)
        message = {"kind": "message", "role": "user", "messageId": "m1", "parts": [{"kind": "text", "text": task}]}
        req = urllib.request.Request(url, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": message}}).encode(),
                                     {"Content-Type": "application/json"})
        try:
            result = json.loads(urllib.request.urlopen(req, timeout=15).read())
        except urllib.error.HTTPError as exc:
            return "error: %s is unavailable (HTTP %d)" % (to, exc.code)
        if "error" in result:
            return "error: %s" % result["error"]["message"]
        result = result["result"]
        for _ in range(8):
            if result.get("kind") != "task" or result["status"]["state"] not in ("working", "submitted"):
                break
            time.sleep(0.05)
            req = urllib.request.Request(url, json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": result["id"]}}).encode(),
                                         {"Content-Type": "application/json"})
            result = json.loads(urllib.request.urlopen(req, timeout=15).read())["result"]
        if result.get("kind") == "message":
            return " ".join(p.get("text", "") for p in result.get("parts", []))
        said = " ".join(p.get("text", "") for p in ((result.get("status") or {}).get("message") or {}).get("parts", []))
        said += " ".join(p.get("text", "") for a in result.get("artifacts", []) for p in a.get("parts", []))
        return said if result["status"]["state"] in ("completed", "input-required") else "error: peer state %s %s" % (result["status"]["state"], said)

    def answer_with_tools(self, body, path):
        """An agent whose tools are an MCP server (/chat-mcp) or a REST backend (/chat-http)."""
        run, name, sim = body["run"], body["agent"], self.sim_base
        if path == "/chat-mcp":
            url = "%s/run/%s/agent/%s/mcp/%s" % (sim, run, name, body["mcp"])
            self.rpc(url, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake", "version": "1"}})
            self.rpc(url, "notifications/initialized", notify=True)
            tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["inputSchema"]}}
                     for t in self.rpc(url, "tools/list")["tools"]]
        elif path == "/chat-a2a":
            tools = [{"type": "function", "function": {"name": "delegate", "description": "Hand a task to another agent",
                                                       "parameters": {"type": "object", "required": ["to", "task"],
                                                                      "properties": {"to": {"type": "string"}, "task": {"type": "string"}}}}}]
        else:
            tools = [{"type": "function", "function": {"name": n, "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}}}}
                     for n in ("get_order", "refund_order")]
        history = self.sessions.setdefault(body.get("session"), [])
        history.append({"role": "user", "content": body["input"]})
        for _ in range(6):
            req = urllib.request.Request("%s/run/%s/agent/%s/openai/v1/chat/completions" % (sim, run, name),
                                         json.dumps({"model": "gpt-5", "messages": history, "tools": tools}).encode(),
                                         {"Content-Type": "application/json", "Authorization": "Bearer " + OPENAI_KEY})
            msg = json.loads(urllib.request.urlopen(req, timeout=30).read())["choices"][0]["message"]
            history.append(msg)
            if not msg.get("tool_calls"):
                return msg.get("content") or ""
            for call in msg["tool_calls"]:
                args = json.loads(call["function"]["arguments"] or "{}")
                if path == "/chat-a2a":
                    content = self.delegate(sim, run, name, args.get("to", ""), args.get("task", ""))
                elif path == "/chat-mcp":
                    got = self.rpc(url, "tools/call", {"name": call["function"]["name"], "arguments": args})
                    content = got["content"][0]["text"]
                else:
                    content = json.dumps(self.http_tool("%s/run/%s/agent/%s/mock/%s" % (sim, run, name, body["mock"]), call["function"]["name"], args))
                history.append({"role": "tool", "tool_call_id": call["id"], "content": content})
        return "I could not finish."

    def answer(self, body):
        run, name = body["run"], body["agent"]
        history = self.sessions.setdefault(body.get("session"), [])
        history.append({"role": "user", "content": body["input"]})
        for _ in range(5):
            msg = self.model(run, name, history)
            history.append(msg)
            if not msg.get("tool_calls"):
                return msg.get("content") or ""
            for call in msg["tool_calls"]:
                args = json.loads(call["function"]["arguments"] or "{}")
                result = ORDERS.get(args.get("id"), {"error": "no such order"})
                if self.tool_hook:
                    self.tool_hook(run, name, call["function"]["name"], args, result)
                history.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)})
        return "I could not finish."
