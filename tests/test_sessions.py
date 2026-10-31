#!/usr/bin/env python3
"""Recording and replay, and the console's chat, on a real model's route, against a stand-in.

    python3 tests/test_sessions.py

No network, no account, no install. A stub plays Anthropic's Messages API and LLMSim is
started as a subprocess pointed at it, with its own throwaway config, so nothing here touches
a real setup. tests/test_anthropic_route.py checks that route itself; this checks what is
built on any real model: what is recorded from it, and what is replayed without it.
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "server.py")
OPENAI_KEY = "sk-llmsim-000000000000000000000000"
ANTHROPIC_KEY = "sk-ant-llmsim-000000000000000000000000"
REAL_KEY = "sk-ant-api03-STANDIN-KEY-0000"
MODEL = "claude-standin"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── The stand-in for a real model ────────────────────────────────────────

class Stub:
    def __init__(self):
        self.port = free_port()
        self.reset()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def reply(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self.reply(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.body = self.rfile.read(length) if length else b"{}"
                stub.messages(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reset(self):
        self.last = None                # the last request: headers + json

    def messages(self, h):
        if h.path != "/v1/messages" or h.headers.get("x-api-key") != REAL_KEY:
            return h.reply(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
        req = json.loads(h.body)
        self.last = {"headers": {k.lower(): v for k, v in h.headers.items()}, "json": req}
        if req.get("stream") is not True or not req.get("max_tokens"):
            return h.reply(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "stream and max_tokens are required"}})
        said_by_user, answered = [], []
        for msg in req["messages"]:
            content = msg["content"] if isinstance(msg["content"], list) else [{"type": "text", "text": msg["content"]}]
            for block in content:
                if block.get("type") == "tool_result":
                    out = block.get("content")
                    answered.append(out if isinstance(out, str) else "".join(b.get("text", "") for b in out or []))
                elif block.get("type") == "text" and msg["role"] == "user":
                    said_by_user.append(block["text"])
        ask = " ".join(said_by_user).lower()

        if "limit please" in ask:
            return h.reply(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "limit reached"}})

        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Connection", "close")
        h.end_headers()
        h.close_connection = True

        def sse(kind, **data):
            h.wfile.write(("event: %s\ndata: %s\n\n" % (kind, json.dumps(dict(type=kind, **data)))).encode())
            h.wfile.flush()

        def text_block(index, pieces):
            sse("content_block_start", index=index, content_block={"type": "text", "text": ""})
            for piece in pieces:
                sse("content_block_delta", index=index, delta={"type": "text_delta", "text": piece})
            sse("content_block_stop", index=index)

        def finish(reason, output):
            sse("message_delta", delta={"stop_reason": reason}, usage={"output_tokens": output})
            sse("message_stop")

        sse("message_start", message={"id": "msg_1", "type": "message", "role": "assistant", "model": req["model"], "content": [],
                                      "usage": {"input_tokens": 40, "cache_read_input_tokens": 10, "output_tokens": 1}})
        if "break midway" in ask:
            sse("content_block_start", index=0, content_block={"type": "text", "text": ""})
            sse("content_block_delta", index=0, delta={"type": "text_delta", "text": "Starting, "})
            return sse("error", error={"type": "overloaded_error", "message": "boom"})
        if "use the tool" in ask and not answered and req.get("tools"):
            text_block(0, ["Checking. "])
            sse("content_block_start", index=1, content_block={"type": "tool_use", "id": "call_abc", "name": req["tools"][0]["name"], "input": {}})
            sse("content_block_delta", index=1, delta={"type": "input_json_delta", "partial_json": json.dumps({"city": "Paris"})})
            sse("content_block_stop", index=1)
            return finish("tool_use", 9)
        if "then stop" in ask and answered:
            # The work is done and the model has nothing to add: a completed answer with nothing in it.
            return finish("end_turn", 4)
        said = ("The tool said: %s" % answered[-1]) if answered else "Hello from the model: %s" % ask[:60]
        text_block(0, [said[:6], said[6:]])
        finish("end_turn", 7)


# ── LLMSim, as a subprocess, pointed at the stub ─────────────────────────

class Sim:
    def __init__(self, stub, workdir):
        self.port = free_port()
        self.stub = stub
        self.workdir = workdir
        self.env = dict(
            os.environ, LLMSIM_PORT=str(self.port), LLMSIM_CONFIG=os.path.join(workdir, "config.json"),
            LLMSIM_SUITE=os.path.join(workdir, "suite.json"),
            LLMSIM_CASSETTES=os.path.join(workdir, "cassettes"),
            LLMSIM_RULES=os.path.join(workdir, "rules.json"), LLMSIM_ASSERTIONS=os.path.join(workdir, "assertions.json"),
            LLMSIM_RUNS_DB=os.path.join(workdir, "runs.db"), LLMSIM_CREDENTIALS=os.path.join(workdir, "credentials.json"))
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen([sys.executable, "-u", SERVER], env=self.env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(100):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/healthz" % self.port, timeout=1).read()
                return self
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("LLMSim did not start")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(5)

    def call(self, method, path, body=None, headers=None):
        """(status, headers, text). Never raises on an HTTP error."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, path, json.dumps(body) if body is not None else None,
                     dict({"Content-Type": "application/json"}, **(headers or {})))
        resp = conn.getresponse()
        text = resp.read().decode()
        conn.close()
        return resp.status, dict(resp.getheaders()), text

    def post(self, path, body, key=OPENAI_KEY):
        header = {"x-api-key": key} if key.startswith("sk-ant") else {"Authorization": "Bearer " + key}
        status, headers, text = self.call("POST", path, body, header)
        return status, headers, text

    def json(self, method, path, body=None, headers=None):
        status, _, text = self.call(method, path, body, headers)
        return status, json.loads(text)

    def configure(self, **body):
        status, _ = self.json("POST", "/api/ui/config", body)
        assert status == 200, status

    def connect(self):
        """Give it a real model to route to: the stub."""
        self.configure(anthropic={"url": "http://127.0.0.1:%d" % self.stub.port, "key": REAL_KEY, "auth": "api_key", "model": MODEL})


def frames(text):
    """SSE text as [(event, data)], data parsed unless it is [DONE]."""
    out, event = [], ""
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data = line[5:].strip()
            out.append((event, data if data == "[DONE]" else json.loads(data)))
            event = ""
    return out


# ── The cases ────────────────────────────────────────────────────────────

CHAT = "/openai/v1/chat/completions"
WEATHER = {"type": "function", "function": {"name": "get_weather", "description": "Weather",
                                           "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}


CHAT = "/openai/v1/chat/completions"

WEATHER = {"type": "function", "function": {"name": "get_weather", "description": "Weather",
                                           "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}


def case_console_chat_goes_through_the_real_path(sim, stub):
    sim.configure(routing={"default": "scripted", "models": {"gpt-5": "anthropic"}})
    # A model on a real route: the page is told what answered, and that model answered.
    status, headers, text = sim.call("POST", "/api/ui/chat", {
        "model": "gpt-5", "stream": False, "system": "Be brief.", "messages": [{"role": "user", "content": "hi"}]})
    reply = json.loads(text)
    assert status == 200 and reply["choices"][0]["message"]["content"].startswith("Hello from the model"), text
    assert headers.get("X-LLMSim-Route") == "anthropic" and headers.get("X-LLMSim-Rule") == "anthropic:" + MODEL, headers
    assert "Be brief." in json.dumps(stub.last["json"]["system"]), "the system prompt did not travel"
    # An Anthropic-provider model is asked in Anthropic's format, with the field that vendor requires.
    status, headers, text = sim.call("POST", "/api/ui/chat", {
        "model": "claude-sonnet-5", "stream": False, "messages": [{"role": "user", "content": "hello there"}]})
    assert status == 200 and json.loads(text)["type"] == "message", text
    assert headers.get("X-LLMSim-Route") == "scripted" and headers.get("X-LLMSim-Rule") == "hello", headers
    # And it shows up in the Logs like any other caller.
    assert any(r["model"] == "claude-sonnet-5" for r in json.loads(sim.call("GET", "/api/ui/state")[2])["requests"])
    # A vendor client never sees the trace.
    _, headers, _ = sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]})
    assert not [h for h in headers if h.lower().startswith("x-llmsim")], "the trace leaked to a vendor route"


def case_console_chat_refuses_what_it_should(sim, stub):
    assert sim.call("POST", "/api/ui/chat", {"model": "no-such", "messages": [{"role": "user", "content": "x"}]})[0] == 404
    assert sim.call("POST", "/api/ui/chat", {"model": "gpt-5", "messages": []})[0] == 400
    status, _, _ = sim.call("POST", "/api/ui/chat", {"model": "gpt-5", "messages": [{"role": "user", "content": "x"}]},
                            {"Origin": "http://evil.example"})
    assert status == 403, "another site could make this page spend a real model"


def case_console_chat_streams_in_the_models_own_format(sim, stub):
    sim.configure(routing={"default": "scripted", "models": {"gpt-5": "anthropic"}})
    status, _, text = sim.call("POST", "/api/ui/chat", {
        "model": "gpt-5", "stream": True, "messages": [{"role": "user", "content": "say hello"}]})
    out = frames(text)
    assert status == 200 and out[-1][1] == "[DONE]", text
    assert "".join(d["choices"][0]["delta"].get("content") or "" for _, d in out if d != "[DONE]" and d["choices"]).startswith("Hello from")
    usage = [d["usage"] for _, d in out if d != "[DONE]" and d.get("usage")]
    assert usage and usage[0]["completion_tokens"] == 7, "a streamed chat answer reported no tokens"
    sim.configure(routing={"default": "scripted", "models": {}})


# ── Sessions: record once, replay exactly ────────────────────────────────

def sess(sim, **body):
    return sim.json("POST", "/api/ui/session", body)


def conversation(sim, ask="use the tool for Paris", model="gpt-5", tool_result=None, **extra):
    """Two turns of an agent loop, in Chat Completions: the ask, then the tool's answer."""
    first = {"model": model, "tools": [WEATHER], "messages": [{"role": "user", "content": ask}], **extra}
    status, _, text = sim.post(CHAT, first)
    out = [(status, json.loads(text))]
    call = out[0][1]["choices"][0]["message"].get("tool_calls", [None])[0] if status == 200 else None
    if call:
        second = dict(first, messages=first["messages"] + [
            {"role": "assistant", "content": None, "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call["id"], "content": tool_result or "18C and clear"}])
        status, _, text = sim.post(CHAT, second)
        out.append((status, json.loads(text)))
    return out


def case_a_recorded_session_is_replayed_exactly(sim, stub):
    sim.configure(routing={"default": "scripted", "models": {"gpt-5": "anthropic"}})
    assert sess(sim, action="settings", onMiss="fail", strict=False)[1]["ok"]
    assert sess(sim, action="record", name="loop")[1]["ok"]
    live = conversation(sim)
    assert sim.post(CHAT, {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hello there"}]})[0] == 200
    assert sess(sim, action="stop")[1]["message"].startswith("Saved 3 exchange(s)")
    path = os.path.join(sim.workdir, "cassettes", "loop.json")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600", "a recording holds model answers and must be owner-only"
    cassette = json.load(open(path))
    assert [e["turn"] for e in cassette["entries"] if e["route"] == "anthropic"] == [0, 1]
    assert "messages" not in json.dumps(cassette["entries"][0]), "the request itself was kept, and only the answer should be"

    # Replay: the real model is no longer asked, and the model is routed somewhere else entirely.
    sim.configure(routing={"default": "scripted", "models": {}})
    stub.last = None
    assert sess(sim, action="replay", name="loop")[1]["ok"]
    again = conversation(sim)
    assert stub.last is None, "replay asked the upstream"
    for (_, was), (_, now) in zip(live, again):
        assert now["choices"][0]["message"] == was["choices"][0]["message"], (was, now)
        assert now["choices"][0]["finish_reason"] == was["choices"][0]["finish_reason"]
        assert now["usage"]["prompt_tokens"] == was["usage"]["prompt_tokens"], "the recorded numbers were not kept"
        assert now["usage"]["completion_tokens"] == was["usage"]["completion_tokens"]
    assert again[0][1]["choices"][0]["message"]["tool_calls"][0]["id"] == "call_abc", "the call id is part of the answer"

    # The same run, asked in another vendor's format, streamed: still the recorded answer.
    ask = {"model": "claude-sonnet-5", "max_tokens": 64, "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
           "messages": [{"role": "user", "content": "use the tool for Paris"}]}
    _, headers, text = sim.post("/anthropic/v1/messages", ask, key=ANTHROPIC_KEY)
    body = json.loads(text)
    assert [b["type"] for b in body["content"]] == ["text", "tool_use"] and body["content"][1]["id"] == "call_abc", text
    assert headers.get("x-llmsim-route") is None, "the trace leaked to a vendor route"
    _, _, text = sim.post("/anthropic/v1/messages", dict(ask, stream=True), key=ANTHROPIC_KEY)
    assert [d["content_block"]["type"] for e, d in frames(text) if e == "content_block_start"] == ["text", "tool_use"]
    follow = dict(ask, messages=ask["messages"] + [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_abc", "name": "get_weather", "input": {"city": "Paris"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_abc", "content": "18C and clear"}]}])
    assert json.loads(sim.post("/anthropic/v1/messages", follow, key=ANTHROPIC_KEY)[2])["content"][0]["text"] == "The tool said: 18C and clear"
    status = sim.json("GET", "/api/ui/session")[1]
    assert status["mode"] == "replay" and status["hits"] == 5 and status["misses"] == 0, status
    assert sess(sim, action="stop")[1]["ok"]


def case_replay_is_lenient_about_tool_output_and_strict_when_told(sim, stub):
    sess(sim, action="replay", name="loop")
    differing = conversation(sim, tool_result="a different, timestamped result 2026-10-06T13:00")
    assert differing[1][0] == 200 and differing[1][1]["choices"][0]["message"]["content"] == "The tool said: 18C and clear"
    assert sim.json("GET", "/api/ui/session")[1]["diverged"] == 1, "the difference was swallowed, not counted"
    assert "last message differs" in sim.json("GET", "/api/ui/state")[1]["requests"][-1]["rule"]

    sess(sim, action="settings", strict=True)
    status, body = conversation(sim, tool_result="a different, timestamped result")[1]
    assert status == 404 and "no recording" in body["error"]["message"], body
    sess(sim, action="settings", strict=False)
    sess(sim, action="stop")


def case_a_request_the_recording_cannot_answer_is_loud_or_routed(sim, stub):
    sess(sim, action="replay", name="loop")
    status, headers, text = sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "something never asked"}]})
    err = json.loads(text)["error"]
    assert status == 404 and err["type"] == "not_found_error" and "something never asked" in err["message"], text
    assert sim.json("GET", "/api/ui/session")[1]["unmatched"][0]["ask"] == "something never asked"
    sess(sim, action="settings", onMiss="route")
    status, _, text = sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "something never asked"}]})
    assert status == 200 and "no rule matching" in text, "a miss set to route did not take its normal route"
    sess(sim, action="settings", onMiss="fail")
    sess(sim, action="stop")


def case_harness_reminders_are_not_part_of_the_ask(sim, stub):
    sess(sim, action="replay", name="loop")
    noisy = "<system-reminder>\nToday's date is 2026-10-06.\n</system-reminder>\n\nhello   there"
    status, _, text = sim.post(CHAT, {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": noisy}]})
    assert status == 200 and "Hello from LLMSim" in text, text
    sess(sim, action="stop")


def case_errors_are_never_recorded(sim, stub):
    sim.configure(routing={"default": "scripted", "models": {"gpt-5": "anthropic"}})
    sess(sim, action="record", name="refused")
    assert sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "limit please"}]})[0] == 429
    assert sess(sim, action="stop")[1]["message"].startswith("Saved 0 exchange(s)"), "a refusal was kept as an answer"
    sim.configure(routing={"default": "scripted", "models": {}})


def case_session_guards(sim, stub):
    for bad in ("../escape", "a/b", "", ".hidden", "x" * 80, "a..b"):
        status, body = sim.json("POST", "/api/ui/session", {"action": "record", "name": bad})
        assert status == 400 and not body["ok"], (bad, body)
    assert sess(sim, action="record", name="loop")[0] == 409, "an existing recording was overwritten"
    assert sess(sim, action="replay", name="nope")[0] == 404, "a missing cassette is a 404, not a conflict"
    assert sess(sim, action="record", name="one")[1]["ok"]
    assert sess(sim, action="replay", name="loop")[0] == 409, "two modes ran at once"
    assert sess(sim, action="delete", name="one")[0] == 409, "the recording in progress was deleted"
    assert sess(sim, action="stop")[1]["ok"] and sess(sim, action="stop")[0] == 409
    assert sess(sim, action="delete", name="one")[1]["ok"]
    for action in ("record", "stop", "delete"):
        assert sim.json("POST", "/api/ui/session", {"action": action, "name": "x"}, {"Origin": "http://evil.example"})[0] == 403
    assert sess(sim, action="settings", onMiss="nonsense")[0] == 400
    assert sim.call("GET", "/api/ui/session/..%2F..%2Fetc%2Fpasswd")[0] == 404


def case_a_scripted_tool_call_keeps_its_id_through_a_replay(sim, stub):
    """The id was invented at send time, so the recording invented another. A replay is not exact if it changes."""
    sim.configure(routing={"default": "scripted", "models": {}})
    write = {"name": "Write", "input_schema": {"type": "object"}}
    anthropic = {"model": "claude-sonnet-5", "max_tokens": 64, "tools": [write],
                 "messages": [{"role": "user", "content": "please create a file"}]}
    # A different ask: a recording keeps one answer per (ask, turn), so the same ask
    # through two formats would be one entry, not two.
    chat = {"model": "gpt-5", "messages": [{"role": "user", "content": "please write a file"}],
            "tools": [{"type": "function", "function": {"name": "Write", "parameters": {"type": "object"}}}]}
    try:
        assert sess(sim, action="record", name="ids")[1]["ok"]
        said = json.loads(sim.post("/anthropic/v1/messages", anthropic, key=ANTHROPIC_KEY)[2])["content"][1]["id"]
        said_chat = json.loads(sim.post(CHAT, chat)[2])["choices"][0]["message"]["tool_calls"][0]["id"]
        assert sess(sim, action="stop")[1]["ok"]
        assert said.startswith("toolu_") and said_chat.startswith("call_"), (said, said_chat)

        assert sess(sim, action="replay", name="ids")[1]["ok"]
        assert json.loads(sim.post("/anthropic/v1/messages", anthropic, key=ANTHROPIC_KEY)[2])["content"][1]["id"] == said
        assert json.loads(sim.post(CHAT, chat)[2])["choices"][0]["message"]["tool_calls"][0]["id"] == said_chat
    finally:
        if sim.json("GET", "/api/ui/session")[1]["mode"] != "off":
            sess(sim, action="stop")
        sess(sim, action="delete", name="ids")


def case_a_harness_that_asks_twice_at_the_same_point_gets_both_answers_back(sim, stub):
    """OpenCode asks for a title alongside the agent's first request: the same ask, the same turn. One answer
    per ask and turn kept whichever finished last, and a replay then handed the title request a tool call."""
    sim.configure(routing={"default": "scripted", "models": {"gpt-5": "anthropic"}})
    ask = [{"role": "user", "content": "use the tool for Paris"}]
    agent = {"model": "gpt-5", "tools": [WEATHER], "messages": ask}
    title = {"model": "gpt-5", "messages": [{"role": "system", "content": "You are a title generator."}] + ask}
    message = lambda request: json.loads(sim.post(CHAT, request)[2])["choices"][0]["message"]
    try:
        assert sess(sim, action="record", name="twice")[1]["ok"]
        # The title is kept last, so "whichever came last" would give it to everybody below.
        called, titled = message(agent), message(title)
        assert titled["content"] and not titled.get("tool_calls") and called["tool_calls"], (titled, called)
        assert sess(sim, action="stop")[1]["message"].startswith("Saved 2 exchange(s)"), "one answer replaced the other"

        sim.configure(routing={"default": "scripted", "models": {}})
        stub.last = None
        assert sess(sim, action="replay", name="twice")[1]["ok"]
        assert message(title) == titled, "the title request was not given the title"
        assert message(agent) == called, "the agent's request was not given its tool call"
        # A harness that gains a tool is still the agent, not the title generator.
        grown = dict(agent, tools=[WEATHER, {"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}])
        assert message(grown) == called, "a caller with one more tool was handed the other caller's answer"
        status = sim.json("GET", "/api/ui/session")[1]
        assert stub.last is None and status["hits"] == 3 and status["misses"] == 0, status
    finally:
        if sim.json("GET", "/api/ui/session")[1]["mode"] != "off":
            sess(sim, action="stop")
        sess(sim, action="delete", name="twice")
        sim.configure(routing={"default": "scripted", "models": {}})


def case_an_answer_that_says_nothing_is_recorded_and_replayed(sim, stub):
    """An agent's last turn is often empty: the file is written, the model stops. Not keeping it made a
    recording that replayed every step of a run and then refused the request that ends it."""
    sim.configure(routing={"default": "scripted", "models": {"gpt-5": "anthropic"}})
    ask = "use the tool for Paris, then stop"
    try:
        assert sess(sim, action="record", name="quiet")[1]["ok"]
        live = conversation(sim, ask=ask, stream=False)
        assert sess(sim, action="stop")[1]["message"].startswith("Saved 2 exchange(s)"), "the empty last turn was dropped"

        assert sess(sim, action="record", name="quiet-streamed")[1]["ok"]
        first = {"model": "gpt-5", "tools": [WEATHER], "messages": [{"role": "user", "content": ask}]}
        done = dict(first, stream=True, messages=first["messages"] + [
            {"role": "assistant", "content": None, "tool_calls": [live[0][1]["choices"][0]["message"]["tool_calls"][0]]},
            {"role": "tool", "tool_call_id": "call_abc", "content": "18C and clear"}])
        assert sim.post(CHAT, done)[0] == 200
        kept = sess(sim, action="stop")[1]
        assert kept["message"].startswith("Saved 1 exchange(s)"), kept
        path = os.path.join(sim.workdir, "cassettes", "quiet-streamed.json")
        assert json.load(open(path))["entries"][0]["answer"]["text"] == "", "a streamed empty answer was not kept as empty"

        sim.configure(routing={"default": "scripted", "models": {}})
        stub.last = None
        assert sess(sim, action="replay", name="quiet-streamed")[1]["ok"]
        status, _, text = sim.post(CHAT, done)
        out = [d for _, d in frames(text) if isinstance(d, dict)]
        assert status == 200 and out[-1]["choices"][0]["finish_reason"] == "stop", text
        assert "".join(d["choices"][0]["delta"].get("content") or "" for d in out if d.get("choices")) == "", text
        # The same empty answer in the other formats: an answer, not an error and not a crash.
        status, _, text = sim.post(CHAT, dict(done, stream=False))
        assert status == 200 and not json.loads(text)["choices"][0]["message"].get("content"), text
        claude = {"model": "claude-sonnet-5", "max_tokens": 64, "messages": [
            {"role": "user", "content": ask},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "call_abc", "name": "get_weather", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_abc", "content": "18C and clear"}]}]}
        status, _, text = sim.post("/anthropic/v1/messages", claude, key=ANTHROPIC_KEY)
        assert status == 200 and json.loads(text)["stop_reason"] == "end_turn", text
        assert stub.last is None and sim.json("GET", "/api/ui/session")[1]["misses"] == 0
    finally:
        if sim.json("GET", "/api/ui/session")[1]["mode"] != "off":
            sess(sim, action="stop")
        for name in ("quiet", "quiet-streamed"):
            sess(sim, action="delete", name=name)
        sim.configure(routing={"default": "scripted", "models": {}})


def case_the_shipped_functional_plan_still_passes(sim, stub):
    """LLMSim's own test runner. A same-named helper added for recording once shadowed the runner's
    and made every run drop the connection — and nothing here ran the shipped plan to notice."""
    sim.configure(routing={"default": "scripted", "models": {}})
    status, run = sim.json("POST", "/api/ui/run", {"planId": "agent"})
    assert status == 200 and run["failed"] == 0 and run["passed"] == 6, run


def case_a_cassette_can_leave_and_come_back(sim, stub):
    status, headers, text = sim.call("GET", "/api/ui/session/loop")
    assert status == 200 and "attachment" in headers["Content-Disposition"] and len(json.loads(text)["entries"]) == 3
    assert sess(sim, action="import", name="loop")[0] == 409 and sess(sim, action="import", name="bad", cassette={"entries": []})[0] == 400
    # A key in an imported file is not believed: it decides who gets which answer, so it is worked out here.
    forged = {"entries": [{"key": "ffffffffffffffff", "ask": "imported ask", "turn": 0, "model": "gpt-5", "last": "",
                           "answer": {"text": "from another machine", "tool_calls": []}}]}
    assert sess(sim, action="import", name="shared", cassette=forged)[1]["ok"]
    sess(sim, action="replay", name="shared")
    status, _, text = sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "imported ask"}]})
    assert status == 200 and json.loads(text)["choices"][0]["message"]["content"] == "from another machine", text
    sess(sim, action="stop")
    for name in ("shared", "loop"):
        assert sess(sim, action="delete", name=name)[1]["ok"]
    left = [c["name"] for c in sim.json("GET", "/api/ui/session")[1]["cassettes"]]
    assert "loop" not in left and "shared" not in left, left
    assert sess(sim, action="delete", name="refused")[1]["ok"]       # the earlier case's empty recording



# ── The API a test harness drives ────────────────────────────────────────

def api(sim, method, path, body=None, headers=None):
    status, _, text = sim.call(method, path, body, headers)
    return status, (json.loads(text) if text.startswith(("{", "[")) else text)


def case_the_rest_api_records_and_replays_a_run_that_has_a_different_run_id(sim, stub):
    """The shape of Maniple's own use: a prompt carrying a run id from the clock, recorded once and replayed
    on a later run with another id. Without a mask the two asks never meet."""
    sim.configure(routing={"default": "scripted", "models": {}})
    masks = [r"run-\d{6}"]

    def ask(run):
        return json.loads(sim.post(CHAT, {"model": "gpt-5", "messages": [
            {"role": "user", "content": "please write a file for run-%s" % run}]})[2])["choices"][0]["message"]

    status, body = api(sim, "GET", "/api/sessions")
    assert status == 200 and body["mode"] == "off" and "cassettes" in body, body
    status, body = api(sim, "POST", "/api/sessions/record", {"name": "rest", "masks": masks})
    assert status == 200 and body["ok"] and body["status"]["mode"] == "record", body
    recorded = ask("111111")
    status, body = api(sim, "POST", "/api/sessions/stop")
    assert status == 200 and body["summary"]["entries"] == 1 and body["summary"]["masks"] == masks, body

    status, cassette = api(sim, "GET", "/api/sessions/rest")
    assert status == 200 and cassette["masks"] == masks, cassette
    assert cassette["entries"][0]["ask"] == "please write a file for <*>", "the run id was kept in the ask"
    assert api(sim, "DELETE", "/api/sessions/rest")[0] == 200 and api(sim, "GET", "/api/sessions/rest")[0] == 404
    assert api(sim, "PUT", "/api/sessions/rest", cassette)[0] == 200           # the repo's copy, put back
    assert api(sim, "PUT", "/api/sessions/rest", cassette)[0] == 409, "an upload replaced a cassette without being told to"
    assert api(sim, "PUT", "/api/sessions/rest?overwrite=1", cassette)[0] == 200

    status, body = api(sim, "POST", "/api/sessions/replay", {"name": "rest"})
    assert status == 200 and body["status"]["masks"] == masks, "the replay did not use the recording's masks"
    replayed = ask("999999")                                                   # another run, another id
    assert replayed == recorded, (recorded, replayed)
    assert replayed["tool_calls"][0]["id"] == recorded["tool_calls"][0]["id"]
    status, body = api(sim, "POST", "/api/sessions/stop")
    assert body["summary"]["hits"] == 1 and body["summary"]["misses"] == 0 and body["summary"]["unmatched"] == [], body

    assert api(sim, "POST", "/api/sessions/record", {"name": "rest"})[0] == 409
    assert api(sim, "POST", "/api/sessions/record", {"name": "rest", "overwrite": True})[0] == 200
    assert api(sim, "POST", "/api/sessions/stop")[1]["summary"]["entries"] == 0, "overwrite kept the old recording"
    assert api(sim, "DELETE", "/api/sessions/rest")[0] == 200


def case_auto_records_the_first_time_and_replays_after(sim, stub):
    status, body = api(sim, "POST", "/api/sessions/auto", {"name": "once"})
    assert status == 200 and body["status"]["mode"] == "record", body
    sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hello there"}]})
    assert api(sim, "POST", "/api/sessions/stop")[1]["summary"]["entries"] == 1
    status, body = api(sim, "POST", "/api/sessions/auto", {"name": "once"})
    assert status == 200 and body["status"]["mode"] == "replay", body
    api(sim, "POST", "/api/sessions/stop")
    assert api(sim, "POST", "/api/sessions/auto", {"name": "../x"})[0] == 400
    api(sim, "DELETE", "/api/sessions/once")


def case_a_replays_settings_are_its_own(sim, stub):
    api(sim, "POST", "/api/sessions/record", {"name": "own"})
    sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hello there"}]})
    api(sim, "POST", "/api/sessions/stop")
    before = api(sim, "GET", "/api/sessions")[1]["settings"]
    status, body = api(sim, "POST", "/api/sessions/replay", {"name": "own", "onMiss": "route", "strict": True})
    assert body["status"]["onMiss"] == "route" and body["status"]["strict"] is True, body
    # a miss, taken on its normal route because this replay said so
    status, _, text = sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "never asked"}]})
    assert status == 200, text
    summary = api(sim, "POST", "/api/sessions/stop")[1]["summary"]
    assert summary["misses"] == 1 and summary["unmatched"][0]["ask"] == "never asked" and summary["onMiss"] == "route", summary
    assert api(sim, "GET", "/api/sessions")[1]["settings"] == before, "one replay's settings leaked into the next, or the console's"
    body = api(sim, "POST", "/api/sessions/replay", {"name": "own"})[1]
    assert body["status"]["onMiss"] == "fail" and body["status"]["strict"] is False, body
    api(sim, "POST", "/api/sessions/stop")
    assert api(sim, "POST", "/api/sessions/replay", {"name": "own", "onMiss": "sometimes"})[0] == 400
    assert api(sim, "POST", "/api/sessions/replay", {"name": "own", "strict": "yes"})[0] == 400
    api(sim, "DELETE", "/api/sessions/own")


def case_masks_are_checked(sim, stub):
    for bad in ("(unclosed", "", "x" * 300, 5):
        assert api(sim, "POST", "/api/sessions/record", {"name": "m", "masks": [bad]})[0] == 400, bad
    assert api(sim, "POST", "/api/sessions/record", {"name": "m", "masks": ["a"] * 21})[0] == 400
    assert api(sim, "POST", "/api/sessions/record", {"name": "m", "masks": "not-a-list"})[0] == 400
    assert api(sim, "GET", "/api/sessions")[1]["mode"] == "off", "a refused start left something running"


def case_the_functional_runner_is_neither_routed_nor_recorded(sim, stub):
    """It checks a caller against the script. A route that answered it would spend something and fail it, and a
    recording that took it in would hold requests that are not part of any session."""
    sim.configure(routing={"default": "anthropic", "models": {}})
    stub.last = None
    try:
        assert api(sim, "POST", "/api/sessions/record", {"name": "runner"})[0] == 200
        status, run = sim.json("POST", "/api/ui/run", {"planId": "agent"})
        assert status == 200 and run["failed"] == 0 and run["passed"] == 6, run
        assert api(sim, "POST", "/api/sessions/stop")[1]["summary"]["entries"] == 0, "the runner's requests were recorded"
        assert stub.last is None, "the runner's requests reached a real model"
        assert api(sim, "POST", "/api/sessions/replay", {"name": "runner"})[0] == 200
        run = sim.json("POST", "/api/ui/run", {"planId": "agent"})[1]
        assert run["failed"] == 0, "a replay with nothing recorded broke the functional runner"
        api(sim, "POST", "/api/sessions/stop")
    finally:
        sim.configure(routing={"default": "scripted", "models": {}})
        api(sim, "DELETE", "/api/sessions/runner")
    # And the marker is honoured only from this machine's own runner: a caller asking by header is still routed.
    # (Loopback is the only source here, so this proves the header works for the runner, not that it is refused elsewhere.)


def case_the_api_refuses_what_it_should(sim, stub):
    for method, path in (("POST", "/api/sessions/record"), ("POST", "/api/sessions/stop"), ("DELETE", "/api/sessions/x"),
                         ("PUT", "/api/sessions/x"), ("GET", "/api/sessions")):
        assert api(sim, method, path, {"name": "x"}, {"Origin": "http://evil.example"})[0] == 403, (method, path)
    assert api(sim, "POST", "/api/sessions/nonsense")[0] == 404
    assert api(sim, "POST", "/api/sessions/stop")[0] == 409, "stopping nothing was not a conflict"
    conn = http.client.HTTPConnection("127.0.0.1", sim.port, timeout=10)
    conn.request("PUT", "/api/sessions/x", "{not json", {"Content-Type": "application/json"})
    assert conn.getresponse().status == 400, "a body that is not JSON was not refused"
    assert api(sim, "PUT", "/api/sessions/x", {"entries": []})[0] == 400
    assert api(sim, "PUT", "/api/sessions/..%2Fx", {"entries": [{"ask": "a", "answer": {"text": "b"}}]})[0] == 400


CASES = [v for k, v in sorted(globals().items(), key=lambda kv: list(globals()).index(kv[0])) if k.startswith("case_")]


def main():
    stub = Stub()
    failed = 0
    with tempfile.TemporaryDirectory() as work:
        sim = Sim(stub, work).start()
        sim.connect()
        try:
            for case in CASES:
                try:
                    case(sim, stub)
                    print("  ok    %s" % case.__name__[5:].replace("_", " "))
                except Exception as exc:
                    failed += 1
                    print("  FAIL  %s\n        %s: %s" % (case.__name__[5:].replace("_", " "), type(exc).__name__, exc))
        finally:
            sim.stop()
    print("\n%d passed, %d failed" % (len(CASES) - failed, failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
