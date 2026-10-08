"""A small agent for the kit's tests: it talks to LLMSim as its model and has one tool.

It is deliberately ordinary — an HTTP endpoint that takes a message, asks a model, runs
the tool the model asks for, asks again, and answers. Everything the kit does to an agent
it does to this one, so the tests exercise the kit on the same path a real agent takes.
"""

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kit import OPENAI_KEY

ORDERS = {1182: {"id": 1182, "status": "shipped", "total": 240.0}, 7: {"id": 7, "status": "pending", "total": 12.0}}
TOOLS = [{"type": "function", "function": {"name": "lookup_order", "description": "Look an order up",
                                           "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}}}}]


class FakeAgent:
    def __init__(self, sim_base, tool_hook=None):
        self.sim_base, self.seen, self.sessions, self.tool_hook = sim_base, [], {}, tool_hook
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
                        out = {"output": {"text": agent.answer(body)}, "session": body.get("session")}
                    except Exception as exc:  # noqa: BLE001 — an agent whose model refused it
                        return self._send(502, {"error": "the model refused: %s" % exc})
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
