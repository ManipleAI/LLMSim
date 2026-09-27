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
    http://localhost:8099/openai       (then /v1/chat/completions or /v1/responses)
    http://localhost:8099/deepseek     (then /v1/chat/completions)

Three wire formats, because agents disagree: Claude Code speaks Anthropic
Messages, OpenCode speaks Chat Completions, and Codex speaks Responses.

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
#
# WHAT THIS LIST IS, AND WHAT IT IS NOT
#
# It is the set of models the simulator ADVERTISES. The completion endpoints
# below accept any model name at all and always have — the name is echoed
# back and nothing else depends on it. So adding a model here does not make
# it "work"; it makes it discoverable, which is the part a client acts on.
#
# WHERE IT COMES FROM
#
# Every model id declared by any base the Coding Agent Router serves, plus
# the real ids in Maniple's own model catalogue. Both were read from a
# running deployment rather than typed, because a hand-kept list of seventy
# names drifts the week after it is written. To regenerate, ask the engine
# for its bases and union their model ids with ModelCatalogEntry.realModelId.
#
# HOW IT IS SPLIT
#
# By wire format, not by who owns the brand. A provider here is one of the
# three request shapes this file can answer, so:
#
#   anthropic   the Messages format — every claude-*
#   deepseek    OpenAI-shaped, kept separate for its own key and prefix
#   openai      the OpenAI format, which is everything else. Gemini, Grok,
#               Qwen, Kimi, Llama, Mistral and the rest are reached through
#               OpenAI-compatible endpoints, and the engine configures them
#               through OpenAI-shaped providers, so that is the shape a
#               request for them actually arrives in.
MODELS = {
    "anthropic": [
        "claude-fable-5", "claude-fable-5-1", "claude-haiku-4-5-20251001", "claude-haiku-4.5",
        "claude-opus-4.7", "claude-opus-4.8", "claude-opus-5", "claude-opus-5.5",
        "claude-sonnet-4.6", "claude-sonnet-5",
    ],
    "openai": [
        "gemini-3-flash-preview", "gemini-3.1-flash-lite", "gemini-3.1-pro-preview",
        "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.7-flash",
        "gemini-3.8-flash", "glm-5.3", "glm-5.3-flash", "gpt-5", "gpt-5-mini", "gpt-5.2",
        "gpt-5.3-codex", "gpt-5.4", "gpt-5.4-mini", "gpt-5.5", "gpt-5.6-luna", "gpt-5.6-sol",
        "gpt-5.6-terra", "gpt-6-astra", "grok-4.20", "grok-4.3", "grok-4.5", "grok-4.6",
        "grok-build-0.1", "hunyuan-3", "hunyuan-4-preview", "jev-1.13", "jev-latest",
        "jev-preview", "kimi-k2.7-code", "kimi-k3", "laya", "ling-3.0-flash", "llama-3.3-70b",
        "llama-4-maverick", "minimax-m3", "mistral-medium-3.5", "muse-glimmer-30b",
        "muse-spark-1.1", "muse-spark-1.2", "muse-spark-1.3", "nemotron-3-super",
        "nemotron-3-ultra", "nemotron-3.5-lightning", "openthai-systemone", "qwen3.7-flash",
        "qwen3.7-max", "qwen3.7-plus", "qwen3.8-27b", "qwen3.8-flash", "qwen3.8-max",
        "step-3.7-flash", "system-one-phase2",
    ],
    "deepseek": [
        "deepseek-chat", "deepseek-reasoner", "deepseek-v4-flash", "deepseek-v4-pro",
        "deepseek-v4.1-flash",
    ],
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
    """One message's text, and whether it is tool output rather than a person.

    Content may be a plain string or a list of typed blocks, and a tool result
    arrives as a block inside a *user* message — so "was a human speaking here"
    cannot be answered from the role alone.
    """
    content = msg.get("content")
    if isinstance(content, str):
        return content, bool(msg.get("tool_result"))
    if not isinstance(content, list):
        return "", False

    parts, saw_tool_result, saw_text = [], False, False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
            saw_text = True
        elif block.get("type") == "tool_result":
            saw_tool_result = True
            inner = block.get("content")
            if isinstance(inner, str):
                parts.append(inner)
            elif isinstance(inner, list):
                for b in inner:
                    if isinstance(b, dict) and b.get("type") == "text":
                        parts.append(b.get("text", ""))
    return "\n".join(parts), saw_tool_result and not saw_text


def subject(messages):
    """What this conversation is about: the last thing a person actually asked.

    Both obvious readings are wrong, and each was wrong in its own real run:

    - The *latest* user message is a tool result by turn two, so the
      conversation gets re-matched against whatever the tool printed. A file
      named HELLO.md dragged an agent-loop conversation into the "hello" rule,
      mid-loop.
    - The *first* user message is not the ask either. A harness injects
      governance context ahead of the prompt, so the opening text is whatever
      the platform had to say — which sent a "create a file" session into the
      SAP capability rule, because the harness guidance mentions SAP.

    What holds in both: the last user message that is not tool output. At turn
    zero that is the prompt; at turn five it is still the prompt, because
    everything since has been tool results. The turn counter advances the
    script; this only decides which script.
    """
    best = ""
    for msg in messages:
        if msg.get("role") != "user":
            continue
        text, is_tool_result = flatten(msg)
        if not is_tool_result and text.strip():
            best = text
    return best


def matches(rule, provider, model, text, declared=None):
    when = rule.get("when", {})
    if "provider" in when and when["provider"] != provider:
        return False
    if "model" in when and when["model"] not in (model or ""):
        return False
    # Match on the tools the client declared, which is the only honest way to
    # tell agents apart. Codex declares apply_patch/exec_command and no write
    # at all; OpenCode declares write; Claude Code declares Write. That is a
    # difference in vocabulary, not in spelling, so no amount of realigning
    # names bridges it — a rule has to be able to say "only when this client
    # can actually do this". `provider` cannot express it: Codex and OpenCode
    # can both sit behind the same provider.
    wanted = when.get("tools") or ([when["tool"]] if "tool" in when else [])
    if wanted:
        have = {t.lower() for t in (declared or {})}
        if not all(t.lower() in have for t in wanted):
            return False
    if "contains" in when and when["contains"].lower() not in text.lower():
        return False
    if "regex" in when and not re.search(when["regex"], text, re.I):
        return False
    return True


def choose_turn(provider, model, messages, declared=None):
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
        if matches(rule, provider, model, text, declared):
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


# ── OpenAI Responses: /v1/responses ──────────────────────────────────────
#
# A third wire format, and not an optional one: Codex talks Responses, not
# Chat Completions. Pointing HarnessRouter's Codex backend at this server got
# `404 No such route: /v1/responses` five times over while the agent retried —
# the routing was right and the format was missing.

def responses_to_messages(body):
    """The Responses request's history, as chat-shaped messages.

    Lets subject() and choose_turn() stay format-agnostic: a rule written once
    drives Chat Completions, Anthropic Messages and Responses alike.
    """
    raw = body.get("input")
    if isinstance(raw, str):
        return [{"role": "user", "content": raw}]
    if not isinstance(raw, list):
        return []

    messages = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        # A tool call the model made, and its result coming back — both count
        # as turns having happened, which is how the script advances.
        if kind == "function_call":
            messages.append({"role": "assistant", "content": ""})
            continue
        if kind == "function_call_output":
            # Flagged as tool output so subject() looks past it for the prompt.
            messages.append({"role": "user", "content": str(item.get("output") or ""), "tool_result": True})
            continue

        role = item.get("role") or "user"
        content = item.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            parts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text")]
            messages.append({"role": role, "content": "\n".join(parts)})
    return messages


def responses_body(turn, model):
    output = []
    if turn.get("text"):
        output.append({
            "type": "message", "id": rand_id("msg_"), "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": turn["text"], "annotations": []}],
        })
    for call in turn.get("tool_calls", []):
        output.append({
            "type": "function_call", "id": rand_id("fc_"), "call_id": rand_id("call_", 20),
            "name": call["name"], "arguments": json.dumps(call.get("input", {})), "status": "completed",
        })

    input_tokens = turn.get("input_tokens", 1200)
    output_tokens = rough_tokens(json.dumps(output))
    return {
        "id": rand_id("resp_"),
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": output,
        # Responses clients read output_text as the flattened convenience form;
        # omitting it makes a reply look empty to anything that trusts it.
        "output_text": turn.get("text") or "",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def responses_sse(turn, model):
    """Responses streaming: every item announced, delta'd, then closed.

    Text streams as output_text.delta; a function call streams its arguments
    as function_call_arguments.delta. A client that reads only the terminal
    response.completed still gets the whole thing, because that event carries
    the assembled response.
    """
    full = responses_body(turn, model)
    shell = dict(full, status="in_progress", output=[], output_text="")

    yield "response.created", {"type": "response.created", "response": shell}
    yield "response.in_progress", {"type": "response.in_progress", "response": shell}

    for index, item in enumerate(full["output"]):
        opening = dict(item)
        if item["type"] == "message":
            opening["content"] = []
        else:
            opening["arguments"] = ""
        yield "response.output_item.added", {
            "type": "response.output_item.added", "output_index": index, "item": opening,
        }

        if item["type"] == "message":
            yield "response.content_part.added", {
                "type": "response.content_part.added", "item_id": item["id"], "output_index": index,
                "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []},
            }
            for piece in chunked(item["content"][0]["text"], 18):
                yield "response.output_text.delta", {
                    "type": "response.output_text.delta", "item_id": item["id"],
                    "output_index": index, "content_index": 0, "delta": piece,
                }
            yield "response.output_text.done", {
                "type": "response.output_text.done", "item_id": item["id"], "output_index": index,
                "content_index": 0, "text": item["content"][0]["text"],
            }
            yield "response.content_part.done", {
                "type": "response.content_part.done", "item_id": item["id"], "output_index": index,
                "content_index": 0, "part": item["content"][0],
            }
        else:
            for piece in chunked(item["arguments"], 24):
                yield "response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta", "item_id": item["id"],
                    "output_index": index, "delta": piece,
                }
            yield "response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done", "item_id": item["id"],
                "output_index": index, "arguments": item["arguments"],
            }

        yield "response.output_item.done", {
            "type": "response.output_item.done", "output_index": index, "item": item,
        }

    yield "response.completed", {"type": "response.completed", "response": full}


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
        if path == "/v1/responses":
            return self.handle_chat(body, "responses")

        return self.refuse(404, "not_found_error", "No such route: " + path)

    def handle_chat(self, body, shape):
        model = body.get("model") or (MODELS[self.provider][0])
        messages = responses_to_messages(body) if shape == "responses" else (body.get("messages") or [])
        stream = bool(body.get("stream"))
        declared = declared_tools(body)
        name, turn = choose_turn(self.provider, model, messages, declared)

        log("%s %s model=%s stream=%s turns=%d -> rule '%s'%s" % (
            self.provider, shape, model, stream,
            sum(1 for m in messages if m.get("role") == "assistant"), name,
            " (tool_call)" if turn.get("tool_calls") else ""))
        # The text the rule was chosen from. Without this, a wrong rule is a
        # mystery: the request carries injected harness context, tool output and
        # the prompt all mixed together, and only this says which part won.
        log("   matched on: %r" % (subject(messages)[:160],))

        # A scripted tool call has to name a tool the *client* declared, and
        # nothing else will tell you when it doesn't.
        #
        # Found the hard way: a script returning "Write" — Claude Code's name
        # for it — was handed to OpenCode, which calls its own tool something
        # else. OpenCode took the unknown name, failed the call, came back for
        # another turn, and the script's next line announced success. The
        # session finished "completed" with no file and no error anywhere. A
        # simulator that can do that is worse than no simulator.
        if declared:
            log("   client declared %d tools: %s" % (len(declared), ", ".join(sorted(declared))[:220]))

        if turn.get("tool_calls"):
            resolved = []
            for call in turn["tool_calls"]:
                real, swapped = resolve_tool_name(call["name"], declared)
                if declared and real not in declared:
                    # Printed with signatures, because the next thing anyone
                    # does after seeing this is write a rule for this client,
                    # and guessing its argument names is how that goes wrong.
                    sigs = "; ".join("%s(%s)" % (t, ", ".join(declared[t])) for t in sorted(declared))
                    log("   WARNING: rule '%s' calls '%s', which this client did not declare — the call "
                        "will fail. This client offers: %s" % (name, real, sigs[:600]))
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
            payload = (anthropic_body(turn, model) if shape == "anthropic"
                       else responses_body(turn, model) if shape == "responses"
                       else openai_body(turn, model))
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
            if shape in ("anthropic", "responses"):
                events = anthropic_sse(turn, model) if shape == "anthropic" else responses_sse(turn, model)
                for event, data in events:
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
