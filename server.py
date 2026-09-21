#!/usr/bin/env python3
"""LLMSim — the model vendors' own APIs, simulated.

Sits where api.anthropic.com, api.openai.com and api.deepseek.com sit, and
answers in each vendor's native wire format. Anything that can be pointed at
a base URL — a gateway like LiteLLM, a coding agent, a flow editor — talks to
this instead, unchanged.

Simulating the vendors rather than the gateway is deliberate. A gateway
already translates between formats; simulating it would only ever prove the
simulator agrees with itself, and would leave the gateway's own translation
untested. Sitting upstream keeps the whole real path in the picture.

What this exists to do that a gateway's built-in mock cannot: **emit tool
calls**. A canned string proves routing, budgets and streaming; it can never
make an agent write a file, run a command, or take a second turn. Scripted
tool calls can, and that is the difference between testing the plumbing and
testing the loop.

Stdlib only, single file, no install. Run it:

    python3 server.py                  # port 8099
    LLMSIM_PORT=9000 python3 server.py

Point a client at one of:

    http://localhost:8099/anthropic    (then /v1/messages)
    http://localhost:8099/openai       (then /v1/chat/completions)
    http://localhost:8099/deepseek     (then /v1/chat/completions)

Or omit the prefix and the provider is inferred from the key presented.
"""

import json
import os
import random
import re
import string
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("LLMSIM_PORT", "8099"))
SCRIPT_PATH = os.environ.get("LLMSIM_SCRIPT", os.path.join(HERE, "responses.json"))

# Fake by construction, and meant to be. These are what a client must
# present, so a misconfigured client fails the way it would against the real
# vendor — a 401 — instead of quietly getting an answer it should not have.
# Nothing here is a real credential and nothing here should ever become one:
# if you find yourself wanting to put a live key in this file, you want a
# different tool.
API_KEYS = {
    "anthropic": os.environ.get("LLMSIM_ANTHROPIC_KEY", "sk-ant-llmsim-000000000000000000000000"),
    "openai": os.environ.get("LLMSIM_OPENAI_KEY", "sk-llmsim-000000000000000000000000"),
    "deepseek": os.environ.get("LLMSIM_DEEPSEEK_KEY", "sk-llmsim-deepseek-0000000000000000"),
}

# Advertised by /v1/models. Claude Code 2.1.126+ queries {base_url}/v1/models
# on startup to discover what a gateway offers, and shows an empty model list
# if it 404s — so this is not decoration.
MODELS = {
    "anthropic": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"],
    "openai": ["gpt-5", "gpt-5-mini"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
}


def log(*parts):
    sys.stderr.write("llmsim: " + " ".join(str(p) for p in parts) + "\n")
    sys.stderr.flush()


def rand_id(prefix, n=24):
    body = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))
    return prefix + body


def rough_tokens(text):
    # Synthetic, and flagged as such in the README. Real accounting is the
    # one thing a simulator cannot honestly provide, and pretending
    # otherwise would make cost tests lie rather than fail.
    return max(1, len(text) // 4)


# ── The script ───────────────────────────────────────────────────────────

def load_script():
    with open(SCRIPT_PATH) as fh:
        return json.load(fh)


SCRIPT = load_script()


def flatten(msg):
    """One message's text, whatever shape it arrived in.

    Both vendors allow content to be either a plain string or a list of typed
    blocks, and a tool result arrives as a block inside a user message.
    """
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("type") == "tool_result":
            inner = block.get("content")
            if isinstance(inner, str):
                parts.append(inner)
            elif isinstance(inner, list):
                for b in inner:
                    if isinstance(b, dict) and b.get("type") == "text":
                        parts.append(b.get("text", ""))
    return "\n".join(parts)


def subject(messages):
    """What this conversation is about: its opening ask.

    Deliberately the *first* user message, not the latest. Matching on the
    latest looks right and is wrong — by turn two the latest user message is
    a tool result, so the conversation gets re-matched against whatever the
    tool happened to print. Caught exactly that way: a file named HELLO.md
    dragged an agent-loop conversation into the "hello" rule, mid-loop. The
    opening ask identifies the scenario; the turn counter advances it.
    """
    for msg in messages:
        if msg.get("role") == "user":
            return flatten(msg)
    return ""


def matches(rule, provider, model, text):
    when = rule.get("when", {})
    if "provider" in when and when["provider"] != provider:
        return False
    if "model" in when and when["model"] not in (model or ""):
        return False
    if "contains" in when and when["contains"].lower() not in text.lower():
        return False
    if "regex" in when and not re.search(when["regex"], text, re.I):
        return False
    return True


def choose_turn(provider, model, messages):
    """Which scripted turn to serve.

    Two independent decisions, and keeping them apart is the whole trick.
    *Which rule* comes from the conversation's opening ask. *Which turn
    within it* comes from how many assistant turns have already happened —
    never from a hash of the request, because turn three's request carries
    turns one and two inside it, tool output included: timestamps, ids,
    directory listings. A content hash misses on the second turn, every
    time. A count of assistant messages is stable under all of it.
    """
    text = subject(messages)
    turns_taken = sum(1 for m in messages if m.get("role") == "assistant")

    for rule in SCRIPT.get("rules", []):
        if matches(rule, provider, model, text):
            turns = rule.get("turns") or []
            if not turns:
                continue
            turn = turns[min(turns_taken, len(turns) - 1)]
            return rule.get("name", "unnamed"), turn

    return "default", SCRIPT.get("default", {"text": "LLMSim has no scripted answer for that."})


# ── Anthropic: /v1/messages ──────────────────────────────────────────────

def anthropic_body(turn, model):
    content = []
    if turn.get("text"):
        content.append({"type": "text", "text": turn["text"]})
    for call in turn.get("tool_calls", []):
        content.append({
            "type": "tool_use",
            "id": rand_id("toolu_"),
            "name": call["name"],
            "input": call.get("input", {}),
        })

    stop = "tool_use" if turn.get("tool_calls") else turn.get("stop_reason", "end_turn")
    out_text = json.dumps(content)
    return {
        "id": rand_id("msg_"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": turn.get("input_tokens", 1200), "output_tokens": rough_tokens(out_text)},
    }


def anthropic_sse(turn, model):
    """The real event order, because clients depend on it.

    message_start, then a start/delta*/stop trio per content block, then
    message_delta carrying stop_reason, then message_stop. A text block
    streams as text_delta; a tool call streams its arguments as
    input_json_delta with partial_json — which is the part naive mocks get
    wrong, and the part an agent needs in order to act.
    """
    msg = anthropic_body(turn, model)
    shell = dict(msg)
    shell["content"] = []
    shell["stop_reason"] = None
    shell["usage"] = {"input_tokens": msg["usage"]["input_tokens"], "output_tokens": 0}

    yield "message_start", {"type": "message_start", "message": shell}
    yield "ping", {"type": "ping"}

    for index, block in enumerate(msg["content"]):
        if block["type"] == "text":
            yield "content_block_start", {
                "type": "content_block_start", "index": index,
                "content_block": {"type": "text", "text": ""},
            }
            for chunk in chunked(block["text"], 18):
                yield "content_block_delta", {
                    "type": "content_block_delta", "index": index,
                    "delta": {"type": "text_delta", "text": chunk},
                }
        else:
            yield "content_block_start", {
                "type": "content_block_start", "index": index,
                "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}},
            }
            for chunk in chunked(json.dumps(block["input"]), 24):
                yield "content_block_delta", {
                    "type": "content_block_delta", "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": chunk},
                }
        yield "content_block_stop", {"type": "content_block_stop", "index": index}

    yield "message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None},
        "usage": {"output_tokens": msg["usage"]["output_tokens"]},
    }
    yield "message_stop", {"type": "message_stop"}


# ── OpenAI / DeepSeek: /v1/chat/completions ──────────────────────────────

def openai_body(turn, model):
    message = {"role": "assistant", "content": turn.get("text") or None}
    finish = turn.get("stop_reason", "stop")

    if turn.get("tool_calls"):
        message["tool_calls"] = [{
            "id": rand_id("call_", 20),
            "type": "function",
            "function": {"name": c["name"], "arguments": json.dumps(c.get("input", {}))},
        } for c in turn["tool_calls"]]
        finish = "tool_calls"

    prompt_tokens = turn.get("input_tokens", 1200)
    completion_tokens = rough_tokens(json.dumps(message))
    return {
        "id": rand_id("chatcmpl-", 20),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def openai_sse(turn, model):
    full = openai_body(turn, model)
    cid, created = full["id"], full["created"]

    def chunk(delta, finish=None):
        return {
            "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    yield chunk({"role": "assistant", "content": ""})

    text = turn.get("text") or ""
    for piece in chunked(text, 18):
        yield chunk({"content": piece})

    for i, call in enumerate(full["choices"][0]["message"].get("tool_calls", [])):
        yield chunk({"tool_calls": [{
            "index": i, "id": call["id"], "type": "function",
            "function": {"name": call["function"]["name"], "arguments": ""},
        }]})
        for piece in chunked(call["function"]["arguments"], 24):
            yield chunk({"tool_calls": [{"index": i, "function": {"arguments": piece}}]})

    yield chunk({}, full["choices"][0]["finish_reason"])


def declared_tools(body):
    """What the caller says it can do: {name: [parameter names]}.

    Anthropic sends {name, input_schema}; OpenAI wraps the same thing as
    {type: "function", function: {name, parameters}}.
    """
    found = {}
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        inner = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = inner.get("name")
        if not name:
            continue
        schema = inner.get("parameters") or inner.get("input_schema") or {}
        props = schema.get("properties") if isinstance(schema, dict) else None
        found[name] = sorted(props) if isinstance(props, dict) else []
    return found


def align_arguments(args, params):
    """Rename scripted arguments to the spellings the client declared.

    The same argument is spelled differently by different agents: Claude
    Code's Write takes `file_path`, OpenCode's write takes `filePath`. This was
    the one mismatch left after tool names were resolved, and it was the
    quietest of the lot — the right tool, called with arguments it ignores,
    which fails somewhere inside the agent with nothing in any log.

    Comparing names with separators and case removed maps file_path onto
    filePath, and camelCase onto snake_case, in either direction. Anything
    with no counterpart is passed through untouched, because a declared
    schema is not always complete and dropping an argument would be a worse
    failure than sending one the tool does not know.
    """
    if not params:
        return args

    def norm(name):
        return re.sub(r"[^a-z0-9]", "", name.lower())

    by_norm = {norm(p): p for p in params}
    out = {}
    for key, value in args.items():
        out[by_norm.get(norm(key), key)] = value
    return out


def resolve_tool_name(scripted, declared):
    """The declared spelling of a scripted tool, when one exists.

    Agents disagree about capitalisation for the same tool: Claude Code
    declares `Write`, OpenCode declares `write`. A script naming one is
    useless to the other, and the failure is silent — the agent gets a tool
    it has never heard of and errors into a tool_result nobody reads.

    Matching case-insensitively lets a single script drive both. It does not
    fix differing *argument* names, which is a real limit and why recording
    real sessions beats hand-writing them.
    """
    if not declared or scripted in declared:
        return scripted, False
    for name in declared:
        if name.lower() == scripted.lower():
            return name, True
    return scripted, False


def chunked(text, size):
    for i in range(0, len(text), size):
        yield text[i:i + size]
    if not text:
        return


# ── HTTP ─────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "LLMSim/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # logged in one line at the end of handle_chat instead

    # -- helpers --

    def send_json(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def refuse(self, status, kind, message):
        # Each vendor's own error envelope, so a client's error handling is
        # exercised rather than bypassed.
        if self.provider == "anthropic":
            self.send_json(status, {"type": "error", "error": {"type": kind, "message": message}})
        else:
            self.send_json(status, {"error": {"message": message, "type": kind, "code": None}})

    def split_path(self):
        """Strip a provider prefix off the path, if there is one.

        A prefix is how one process serves three vendors whose paths would
        otherwise collide — OpenAI and DeepSeek both want
        /v1/chat/completions. With no prefix, the key decides.
        """
        path = self.path.split("?")[0].rstrip("/") or "/"
        for name in ("anthropic", "openai", "deepseek"):
            if path == "/" + name or path.startswith("/" + name + "/"):
                return name, path[len(name) + 1:] or "/"
        return None, path

    def presented_key(self):
        # Anthropic uses x-api-key; the OpenAI-shaped vendors use a bearer
        # token. Accept either, whichever the client sent.
        key = self.headers.get("x-api-key")
        if key:
            return key.strip()
        auth = self.headers.get("authorization") or ""
        return auth[7:].strip() if auth.lower().startswith("bearer ") else ""

    def resolve_provider(self, prefix, key):
        if prefix:
            return prefix
        for name, expected in API_KEYS.items():
            if key == expected:
                return name
        return None

    # -- routes --

    def do_GET(self):
        prefix, path = self.split_path()
        key = self.presented_key()
        self.provider = self.resolve_provider(prefix, key) or "openai"

        if path in ("/", "/healthz"):
            return self.send_json(200, {
                "ok": True, "service": "llmsim",
                "providers": sorted(API_KEYS),
                "rules": [r.get("name") for r in SCRIPT.get("rules", [])],
            })

        if path == "/v1/models":
            if key != API_KEYS.get(self.provider):
                return self.refuse(401, "authentication_error", "Invalid API key.")
            names = MODELS.get(self.provider, [])
            if self.provider == "anthropic":
                return self.send_json(200, {
                    "data": [{"id": m, "type": "model", "display_name": m,
                              "created_at": "2026-01-01T00:00:00Z"} for m in names],
                    "has_more": False, "first_id": names[0] if names else None,
                    "last_id": names[-1] if names else None,
                })
            return self.send_json(200, {
                "object": "list",
                "data": [{"id": m, "object": "model", "created": 1767225600, "owned_by": self.provider}
                         for m in names],
            })

        return self.send_json(404, {"error": {"message": "No such route: " + path}})

    def do_POST(self):
        prefix, path = self.split_path()
        key = self.presented_key()
        self.provider = self.resolve_provider(prefix, key) or "openai"

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self.refuse(400, "invalid_request_error", "Request body is not valid JSON.")

        expected = API_KEYS.get(self.provider)
        if not key:
            return self.refuse(401, "authentication_error", "No API key provided.")
        if key != expected:
            return self.refuse(401, "authentication_error", "Invalid API key.")

        if path == "/v1/messages" and self.provider == "anthropic":
            return self.handle_chat(body, "anthropic")
        if path == "/v1/messages/count_tokens":
            text = json.dumps(body.get("messages", []))
            return self.send_json(200, {"input_tokens": rough_tokens(text)})
        if path == "/v1/chat/completions":
            return self.handle_chat(body, "openai")

        return self.refuse(404, "not_found_error", "No such route: " + path)

    def handle_chat(self, body, shape):
        model = body.get("model") or (MODELS[self.provider][0])
        messages = body.get("messages") or []
        stream = bool(body.get("stream"))
        name, turn = choose_turn(self.provider, model, messages)

        log("%s %s model=%s stream=%s turns=%d -> rule '%s'%s" % (
            self.provider, shape, model, stream,
            sum(1 for m in messages if m.get("role") == "assistant"), name,
            " (tool_call)" if turn.get("tool_calls") else ""))

        # A scripted tool call has to name a tool the *client* declared, and
        # nothing else will tell you when it doesn't.
        #
        # Found the hard way: a script returning "Write" — Claude Code's name
        # for it — was handed to OpenCode, which calls its own tool something
        # else. OpenCode took the unknown name, failed the call, came back for
        # another turn, and the script's next line announced success. The
        # session finished "completed" with no file and no error anywhere. A
        # simulator that can do that is worse than no simulator.
        declared = declared_tools(body)
        if declared:
            log("   client declared %d tools: %s" % (len(declared), ", ".join(sorted(declared))[:220]))

        if turn.get("tool_calls"):
            resolved = []
            for call in turn["tool_calls"]:
                real, swapped = resolve_tool_name(call["name"], declared)
                if declared and real not in declared:
                    log("   WARNING: rule '%s' calls '%s', which this client did not declare. It will "
                        "fail the call. Declared: %s" % (name, real, ", ".join(sorted(declared))[:160]))
                    resolved.append(dict(call, name=real))
                    continue

                args = dict(call.get("input") or {})
                params = declared.get(real, []) if declared else []
                aligned = align_arguments(args, params)
                renamed = {k: v for k, v in zip(sorted(args), sorted(aligned)) if k != v}

                if swapped:
                    log("   '%s' -> '%s' (this client's spelling), parameters: %s"
                        % (call["name"], real, ", ".join(params) or "(none declared)"))
                elif declared:
                    log("   '%s' parameters: %s" % (real, ", ".join(params) or "(none declared)"))
                if renamed:
                    log("   arguments realigned: %s" % ", ".join("%s -> %s" % kv for kv in sorted(renamed.items())))

                resolved.append(dict(call, name=real, input=aligned))
            turn = dict(turn, tool_calls=resolved)

        delay = turn.get("delay_ms")
        if delay:
            time.sleep(delay / 1000.0)

        # A scripted failure, in the vendor's own envelope — the paths that
        # only ever run when something goes wrong are the ones no live key
        # will reliably show you.
        if turn.get("error"):
            err = turn["error"]
            return self.refuse(err.get("status", 429), err.get("type", "rate_limit_error"),
                               err.get("message", "Rate limited."))

        if not stream:
            payload = anthropic_body(turn, model) if shape == "anthropic" else openai_body(turn, model)
            return self.send_json(200, payload)

        # Connection: close, not keep-alive, and the handler stops reusing
        # the socket. An event stream has no Content-Length and this server
        # speaks HTTP/1.1, so a kept-alive connection leaves the client with
        # no way to know the body ended — it reads the last event and then
        # blocks until it times out. Closing is what marks the end.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        try:
            if shape == "anthropic":
                for event, data in anthropic_sse(turn, model):
                    self.wfile.write(("event: %s\ndata: %s\n\n" % (event, json.dumps(data))).encode())
                    self.wfile.flush()
            else:
                for chunk in openai_sse(turn, model):
                    self.wfile.write(("data: %s\n\n" % json.dumps(chunk)).encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        except BrokenPipeError:
            log("client closed the stream early")


def main():
    log("listening on http://localhost:%d" % PORT)
    log("script: %s (%d rules)" % (SCRIPT_PATH, len(SCRIPT.get("rules", []))))
    for name in sorted(API_KEYS):
        log("  %-9s http://localhost:%d/%s   key %s" % (name, PORT, name, API_KEYS[name]))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
