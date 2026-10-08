"""Stand-ins for two platforms, written from their published protocols, to test the drivers.

They are not the platforms. A driver tested against a stand-in written by the same
hands as the driver agrees with its author's reading of the protocol and with nothing
else; what these prove is that the driver does what it means to. Real agents are the
other half, and need an account.
"""

import json
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Server:
    def __init__(self, handler_cls):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        self.httpd.shutdown()


def _reply(h, status, payload, raw=None, ctype="application/json"):
    data = raw if raw is not None else json.dumps(payload).encode()
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


class FakeA2A(_Server):
    """An A2A agent. It answers a message at once, makes a task that finishes after a poll or two, or asks for input."""

    def __init__(self, token="a2a-secret-token"):
        self.token, self.log, self.tasks, self.push = token, [], {}, {}
        me = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.endswith("agent-card.json"):
                    return _reply(self, 200, {"name": "Fake", "url": me.url + "/rpc", "version": "0.3.0", "protocolVersion": "0.3.0",
                                              "capabilities": {"streaming": True, "pushNotifications": True}, "skills": []})
                _reply(self, 404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                me.log.append({"auth": self.headers.get("Authorization"), "method": body.get("method"), "params": body.get("params")})
                if self.headers.get("Authorization") != "Bearer " + me.token:
                    return _reply(self, 401, {"error": "unauthorised"})
                method, params, rid = body["method"], body.get("params") or {}, body.get("id")
                if method == "message/send":
                    return _reply(self, 200, me.send(params, rid))
                if method == "message/stream":
                    return me.stream(self, params, rid)
                if method == "tasks/get":
                    return _reply(self, 200, {"jsonrpc": "2.0", "id": rid, "result": me.poll(params["id"])})
                _reply(self, 200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no such method"}})

        super().__init__(H)

    @staticmethod
    def text(message):
        return " ".join(p.get("text", "") for p in message.get("parts", []))

    def send(self, params, rid):
        message = params["message"]
        said = self.text(message).lower()
        context = message.get("contextId") or uuid.uuid4().hex
        if "hello" in said:
            return {"jsonrpc": "2.0", "id": rid, "result": {"kind": "message", "role": "agent", "contextId": context, "messageId": uuid.uuid4().hex,
                                                            "parts": [{"kind": "text", "text": "Hello from the fake agent."}]}}
        if "which order" in said or message.get("taskId"):
            if message.get("taskId") and message["taskId"] in self.tasks:
                task = self.tasks[message["taskId"]]
                task.update(status={"state": "completed", "message": {"role": "agent", "parts": [{"kind": "text", "text": "Thanks, order %s found." % said.split()[-1]}]}})
                return {"jsonrpc": "2.0", "id": rid, "result": task}
            task = {"kind": "task", "id": uuid.uuid4().hex, "contextId": context,
                    "status": {"state": "input-required", "message": {"role": "agent", "parts": [{"kind": "text", "text": "Which order number?"}]}}}
            self.tasks[task["id"]] = task
            return {"jsonrpc": "2.0", "id": rid, "result": task}
        # a task that takes a while: working now, completed after the caller has asked twice (or been pushed)
        task = {"kind": "task", "id": uuid.uuid4().hex, "contextId": context, "status": {"state": "working"}, "_polls": 0}
        self.tasks[task["id"]] = task
        push = (params.get("configuration") or {}).get("pushNotificationConfig")
        if push:
            self.push[task["id"]] = push
            threading.Thread(target=self._finish_later, args=(task["id"], push), daemon=True).start()
        return {"jsonrpc": "2.0", "id": rid, "result": {k: v for k, v in task.items() if k != "_polls"}}

    def _done(self, task):
        task["status"] = {"state": "completed"}
        task["artifacts"] = [{"artifactId": "a1", "parts": [{"kind": "text", "text": "The slow report is ready."}]}]

    def _finish_later(self, task_id, push):
        time.sleep(0.4)
        task = self.tasks[task_id]
        self._done(task)
        req = urllib.request.Request(push["url"], json.dumps({k: v for k, v in task.items() if k != "_polls"}).encode(),
                                     {"Content-Type": "application/json", "X-A2A-Notification-Token": push.get("token", "")})
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:  # noqa: BLE001
            pass

    def poll(self, task_id):
        task = self.tasks[task_id]
        task["_polls"] = task.get("_polls", 0) + 1
        if task["_polls"] >= 2 and task["status"]["state"] == "working":
            self._done(task)
        return {k: v for k, v in task.items() if k != "_polls"}

    def stream(self, h, params, rid):
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Connection", "close")
        h.end_headers()
        h.close_connection = True
        tid, ctx = uuid.uuid4().hex, uuid.uuid4().hex
        for result in ({"kind": "task", "id": tid, "contextId": ctx, "status": {"state": "submitted"}},
                       {"kind": "status-update", "taskId": tid, "contextId": ctx, "status": {"state": "working"}, "final": False},
                       {"kind": "artifact-update", "taskId": tid, "contextId": ctx, "artifact": {"artifactId": "a", "parts": [{"kind": "text", "text": "Streamed answer."}]}},
                       {"kind": "status-update", "taskId": tid, "contextId": ctx, "status": {"state": "completed"}, "final": True}):
            h.wfile.write(("data: %s\n\n" % json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})).encode())
            h.wfile.flush()


class FakeSalesforce(_Server):
    """The OAuth token endpoint and the Agent API, with one agent that works and one the API does not support."""

    GOOD, UNSUPPORTED = "0XxGOOD000000001", "0XxSCRIPT00000001"

    def __init__(self, client_id="cid", client_secret="csecret-value-123"):
        self.client_id, self.client_secret, self.sessions, self.log, self.ended = client_id, client_secret, {}, [], []
        me = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_POST(self):
                raw = self.body()
                if self.path == "/services/oauth2/token":
                    form = dict(p.split("=", 1) for p in raw.decode().split("&"))
                    if form.get("client_id") != me.client_id or form.get("client_secret") != me.client_secret:
                        return _reply(self, 400, {"error": "invalid_client"})
                    return _reply(self, 200, {"access_token": "sf-access-token-abc", "expires_in": 1800, "token_type": "Bearer"})
                if self.headers.get("Authorization") != "Bearer sf-access-token-abc":
                    return _reply(self, 401, [{"errorCode": "INVALID_SESSION_ID"}])
                body = json.loads(raw or b"{}")
                parts = self.path.strip("/").split("/")
                me.log.append((self.command, self.path, body))
                if parts[-1] == "sessions":
                    agent = parts[-2]
                    if agent == me.UNSUPPORTED:
                        return _reply(self, 400, {"message": "This agent type is not supported by the Agent API"})
                    sid = str(uuid.uuid4())
                    me.sessions[sid] = 0
                    return _reply(self, 200, {"sessionId": sid, "_links": {}, "messages": [{"type": "Inform", "id": "g", "message": "Hi, how can I help?"}]})
                if parts[-1] == "messages" and parts[-2] in me.sessions:
                    me.sessions[parts[-2]] += 1
                    text = body["message"]["text"]
                    return _reply(self, 200, {"messages": [{"type": "Inform", "id": "m%d" % body["message"]["sequenceId"], "message": "You said: " + text}]})
                if parts[-1] == "stream" and parts[-3] in me.sessions:
                    data = b"".join(("event: %s\ndata: %s\n\n" % (e, json.dumps(d))).encode() for e, d in (
                        ("ProgressIndicator", {"message": {"type": "ProgressIndicator", "message": "thinking"}}),
                        ("TextChunk", {"message": {"type": "TextChunk", "offset": 0, "message": "Stream"}}),
                        ("TextChunk", {"message": {"type": "TextChunk", "offset": 6, "message": "ed reply."}}),
                        ("EndOfTurn", {"message": {"type": "EndOfTurn"}})))
                    return _reply(self, 200, None, raw=data, ctype="text/event-stream")
                _reply(self, 404, {"message": "no such route"})

            def do_DELETE(self):
                sid = self.path.strip("/").split("/")[-1]
                me.ended.append((sid, self.headers.get("x-session-end-reason")))
                me.sessions.pop(sid, None)
                _reply(self, 200, {})

        super().__init__(H)
