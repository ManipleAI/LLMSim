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

import base64
import collections
import hashlib
import hmac
import itertools
import json
import os
import threading
import random
import re
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

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


# ── Where answers come from ──────────────────────────────────────────────
#
# Two modes, and the default is the one this file has always had.
#
#   scripted   responses.json decides. Deterministic, free, offline, and it
#              proves a pipeline carries a response of a given shape.
#   relay      one real model answers everything. Every request, whatever
#              model it named, is served by LLMSIM_UPSTREAM_MODEL.
#
# Relay exists for the demo problem: scripted answers are the same every
# time and visibly canned, while wiring seventy models to real vendors costs
# seventy configurations and a real bill. This gives genuinely generated,
# varied answers for any model name, from one cheap model, with nothing
# downstream reconfigured — Maniple, the gateway and the agent still ask for
# claude-opus-5 or gpt-5.4 and still get an answer in that vendor's format.
#
# Unset LLMSIM_UPSTREAM and nothing changes: no request leaves the machine.
# Env seeds the settings; the file wins once written. So a container starts
# configured the way its environment says, and anything changed in the UI
# afterwards survives a restart without the environment having to change.
CONFIG_PATH = os.environ.get("LLMSIM_CONFIG", os.path.join(HERE, "llmsim.config.json"))

# Test cases and plans are kept apart from the rest of the settings, in a
# file that is NOT gitignored.
#
# llmsim.config.json is machine-local by design — it holds the upstream key —
# and a suite that lives in it cannot be committed, reviewed or handed to
# anybody, which undoes most of the reason for writing one. A suite is source:
# it belongs beside the code it tests, in a diff, on a branch.
SUITE_PATH = os.environ.get("LLMSIM_SUITE", os.path.join(HERE, "llmsim.tests.json"))
SUITE_KEYS = ("tests", "plans")

# The console has no sign-in.
#
# This is a simulator: it runs on a developer's machine or a test box, it
# holds no data worth taking, and a password in front of it was friction on
# the one path everybody uses. Bind the port somewhere reachable and the
# console is reachable too — which is the same posture the vendor endpoints
# below already have, since their keys are published in this file.

DEFAULTS = {
    "routing": {
        # What a model does when nothing names it specifically.
        "default": "relay" if os.environ.get("LLMSIM_UPSTREAM") else "scripted",
        # Per model: "scripted" or "relay". Anything not listed follows the
        # default above. This is the whole point of the routing table — a
        # demo can send one expensive-looking model to a real cheap one and
        # keep every other model offline and free.
        "models": {},
    },
    # What a named model answers, instead of whatever the script would say.
    #
    # The script matches on the prompt, which is right for a scenario and
    # wrong for "this model is always slow" or "this one is over its quota".
    # Those are properties of the model, so they live per model:
    #
    #   {"gpt-5.4": {"text": "...", "status": 429, "delay_ms": 800}}
    #
    # status turns it into that vendor's error; text answers with those
    # words; delay_ms waits first. Any of the three may be set alone.
    "responses": {},
    # ── How closely the vendors are imitated ─────────────────────────────
    #
    # Each of these makes the simulator stricter, and each is separate
    # because they carry different risk to a working setup.
    #
    #   validate      reject a malformed request the way the vendor does.
    #                 Safe: real clients send valid requests, and one that
    #                 does not is exactly what this is for.
    #   unknownModel  404 a model that is not in the catalogue. OFF by
    #                 default, and not an oversight: a gateway may route an
    #                 upstream name this list has never heard of — Maniple's
    #                 own does, sending claude-sonnet-4-5 — and turning this
    #                 on without checking breaks a working deployment.
    #   headers       request-id, rate-limit and retry-after headers.
    #   limits        refuse a request past the model's context window.
    "fidelity": {"validate": True, "unknownModel": False, "headers": True, "limits": True},
    # ── Functional tests ─────────────────────────────────────────────────
    #
    # Test cases that drive this simulator to check the CALLER, not the
    # model. A case is an ordered list of steps, each one a prompt in and an
    # expectation about what comes back, so a multi-step exchange — the shape
    # a card applet conversation has, select then verify then authorise — is
    # written as the sequence it actually is rather than as one assertion.
    #
    #   {"id": "...", "name": "...", "provider": "anthropic", "model": "...",
    #    "steps": [{"input": "...", "expect": "...", "match": "contains",
    #               "expectTool": "Write", "expectToolArgs": {"file_path": "x"},
    #               "reject": "...", "rejectTool": "Bash",
    #               "expectStatus": 200, "maxMs": 2000}]}
    #
    # expectToolArgs reaches into the call's own arguments by dotted path, so
    # a test can prove the agent wrote the right file rather than merely that
    # it called Write. reject and rejectTool are the negatives — "never
    # invents a figure it could not read" is only sayable as something that
    # must not appear. maxMs is a deadline.
    #
    # A plan is a named list of case ids, run together and reported together.
    # Shipped with real cases rather than empty, because an empty harness
    # teaches nothing about how to write one. Every case below asserts
    # something responses.json actually does, so a fresh install opens on a
    # suite that passes — and each one is a thing a coding agent is expected
    # to get right.
    "tests": [
        {
            "id": "codegen", "name": "Code generation: writes the file it was asked for",
            "provider": "anthropic", "model": "claude-sonnet-5",
            "tools": [{"name": "Write", "input_schema": {"type": "object", "properties": {
                "file_path": {"type": "string"}, "content": {"type": "string"}}}}],
            "steps": [
                {"input": "Create a file called HELLO.md with a one-line heading.",
                 "expect": "create", "match": "contains", "expectTool": "Write",
                 # The point of the case. "Write was called" passes just as
                 # happily when the agent writes to /tmp.
                 "expectToolArgs": {"file_path": "HELLO.md", "content": "#"}},
                # Sent as tool output, because that is what comes back next
                # in a real loop — the file was written, and the agent reads
                # the result rather than being asked a fresh question.
                {"input": "HELLO.md written, 1 line.", "asToolResult": True,
                 "expect": "HELLO.md is written", "match": "contains"},
            ],
        },
        {
            "id": "codegen-openai", "name": "Code generation: the same ask, an OpenAI-shaped agent",
            "provider": "openai", "model": "gpt-5.4",
            # A different agent declares a different tool for the same job.
            # The case exists to prove the harness follows the caller rather
            # than one vendor's spelling.
            "tools": [{"type": "function", "function": {"name": "exec_command", "parameters": {
                "type": "object", "properties": {"cmd": {"type": "string"}}}}}],
            "steps": [
                {"input": "Create a file called HELLO.md with a one-line heading.",
                 "expect": "file", "match": "contains", "expectTool": "exec_command",
                 "expectToolArgs": {"cmd": "HELLO.md"}},
            ],
        },
        {
            "id": "plan-first", "name": "Planning: asks for a capability instead of inventing data",
            "provider": "anthropic", "model": "claude-sonnet-5",
            "tools": [{"name": "Bash", "input_schema": {"type": "object", "properties": {
                "command": {"type": "string"}}}}],
            # The one worth having. An agent that fabricates invoice figures
            # when it cannot reach SAP looks successful and is the worst
            # outcome the whole platform exists to prevent.
            "steps": [
                {"input": "Reconcile this invoice against the SAP three-way match.",
                 "expect": "SAP.Invoice.Read", "match": "contains", "expectTool": "Bash",
                 "expectToolArgs": {"command": "granted-capabilities"},
                 # The assertion that matters, and it can only be a negative:
                 # an agent that answers with a total it could not have read
                 # looks like success.
                 "reject": r"[$€£]\s*[0-9]", "rejectMatch": "regex"},
                {"input": "granted-capabilities.json: []", "asToolResult": True,
                 "expect": "file the request", "match": "contains",
                 "reject": r"[$€£]\s*[0-9]", "rejectMatch": "regex"},
                {"input": "request filed: DEP-118", "asToolResult": True,
                 "expect": "rather than invent", "match": "contains",
                 # Blocked on a capability it does not hold, it must stop —
                 # not reach for the shell again and find another way in.
                 "rejectTool": "Bash", "reject": r"[$€£]\s*[0-9]", "rejectMatch": "regex"},
            ],
        },
        {
            "id": "rate-limited", "name": "Back-pressure: the caller is handed a 429",
            "provider": "anthropic", "model": "claude-sonnet-5",
            # A refusal has no model work behind it, so it has no excuse to
            # be slow. The deadline is the half the slow case cannot cover:
            # a fast path quietly becoming slow is what regresses.
            "steps": [{"input": "llmsim:429 summarise this repository",
                       "expectStatus": 429, "expect": "rate limit", "match": "contains",
                       "maxMs": 2000}],
        },
        {
            "id": "overloaded", "name": "Back-pressure: the vendor is overloaded (529)",
            "provider": "anthropic", "model": "claude-sonnet-5",
            "steps": [{"input": "llmsim:529 summarise this repository", "expectStatus": 529}],
        },
        {
            "id": "slow", "name": "Patience: a slow answer must not be abandoned",
            "provider": "anthropic", "model": "claude-sonnet-5",
            # Eight seconds, on purpose. A caller that gives up early passes
            # every other case here and fails a real session.
            "steps": [{"input": "llmsim:slow refactor this module",
                       "expect": "eight seconds", "match": "contains"}],
        },
    ],
    "plans": [
        {"id": "agent", "name": "Coding agent — functional",
         "testIds": ["codegen", "codegen-openai", "plan-first", "rate-limited", "overloaded", "slow"]},
        {"id": "quick", "name": "Quick check (no slow case)",
         "testIds": ["codegen", "plan-first", "rate-limited"]},
    ],
    # A third route, "chatgpt": a real model answered by a ChatGPT subscription
    # the person signs in with. Nothing secret lives here — the tokens are in
    # their own owner-only file — so this block is safe to show the console.
    #
    #   model         the one plan model that answers every model routed here,
    #                 whatever name the client asked for (as relay does)
    #   instructions  sent when the client supplied no system prompt; the plan
    #                 backend refuses a request that has none
    #   fallback      answer from the script when the plan cannot be reached
    # How a replay treats a request the recording cannot answer.
    #
    #   onMiss   "fail" answers 404 and says what was missing, so a run that
    #            has wandered off its recording is loud rather than quietly
    #            talking to something else; "route" lets that request take its
    #            normal route instead.
    #   strict   also require the last message to match the recording. Off by
    #            default because it is usually tool output, which carries
    #            timestamps and never repeats.
    "session": {"onMiss": "fail", "strict": False},
    "chatgpt": {
        "model": os.environ.get("LLMSIM_CHATGPT_MODEL", "gpt-5.4"),
        "instructions": "You are a helpful assistant.",
        "fallback": False,
    },
    "upstream": {
        "url": os.environ.get("LLMSIM_UPSTREAM", "").rstrip("/"),
        "key": os.environ.get("LLMSIM_UPSTREAM_KEY", ""),
        "model": os.environ.get("LLMSIM_UPSTREAM_MODEL", "gpt-4o-mini"),
        "format": os.environ.get("LLMSIM_UPSTREAM_FORMAT", "openai").lower(),
        "fallback": os.environ.get("LLMSIM_UPSTREAM_FALLBACK", "") == "1",
    },
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def _read_json(path, what):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as exc:
        # A broken file is worth saying out loud rather than silently
        # starting with defaults that are not what anyone configured.
        log("%s: %s is not readable (%s) — using defaults" % (what, path, exc))
        return None


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)          # atomic, so a crash never leaves half a file


def load_config():
    config = _merge(DEFAULTS, _read_json(CONFIG_PATH, "config") or {})
    # The suite file wins where it exists. An older config that still carries
    # tests keeps working and is migrated on the next save, so nobody loses a
    # suite to this change.
    suite = _read_json(SUITE_PATH, "suite")
    if isinstance(suite, dict):
        for key in SUITE_KEYS:
            if isinstance(suite.get(key), list):
                config[key] = suite[key]
    return config


CONFIG = load_config()


def save_config():
    settings = {k: v for k, v in CONFIG.items() if k not in SUITE_KEYS}
    _write_json(CONFIG_PATH, settings)
    save_suite()


def ensure_suite_file():
    """Write the suite out on first run, so there is something to commit.

    A file that only appears after someone edits a case in the console is a
    file most people never learn exists.
    """
    if os.path.exists(SUITE_PATH):
        return
    try:
        save_suite()
    except OSError as exc:              # a read-only checkout is not fatal
        log("suite: cannot write %s (%s)" % (SUITE_PATH, exc))


def save_suite():
    """The suite, in a file meant to be committed."""
    _write_json(SUITE_PATH, {
        "version": 1,
        # Said in the file itself, because the first person to open it in a
        # diff will not have read this source.
        "_": "LLMSim test cases and plans. Commit this; llmsim.config.json is machine-local.",
        "tests": CONFIG.get("tests", []),
        "plans": CONFIG.get("plans", []),
    })



def public_config():
    """CONFIG with the upstream key replaced by whether there is one.

    The console never receives the secret. It does not need it to show a
    routing table, and a key that reaches a browser is a key in a screenshot,
    a bug report and a page cache.
    """
    out = json.loads(json.dumps(CONFIG))
    out["upstream"]["key"] = ""
    out["upstream"]["keySet"] = bool(CONFIG["upstream"]["key"])
    return out


def route_for(model):
    """scripted, relay or chatgpt, for this model.

    A named model wins over the default, and a relay route with no upstream
    URL configured is scripted — saying "relay" while having nowhere to relay
    to would fail every request for a reason the table does not show.
    """
    routing = CONFIG["routing"]
    choice = routing["models"].get(model) or routing["default"]
    if choice == "relay" and not CONFIG["upstream"]["url"]:
        return "scripted"
    return choice


def relaying():
    """Whether any route can reach an upstream at all."""
    return bool(CONFIG["upstream"]["url"])



# ── What a model is, beyond its name ─────────────────────────────────────
#
# Keyed by family prefix rather than by the seventy names, because the names
# change every release and the families do not. A model that matches nothing
# gets the last row, which is deliberately unremarkable: a wrong-but-plausible
# limit is better than pretending there is none, and a limit nobody set is the
# reason a context-window test cannot exist.
#
# The numbers are the vendors' published ones where they are published, and
# sane approximations where they are not. They are here to be *exceeded* — a
# caller that never checks a context window is the thing this catches.
MODEL_FAMILIES = [
    ("claude-opus",   {"context": 200000, "maxOutput": 32000}),
    ("claude-sonnet", {"context": 200000, "maxOutput": 64000}),
    ("claude-haiku",  {"context": 200000, "maxOutput": 8192}),
    ("claude-fable",  {"context": 200000, "maxOutput": 32000}),
    ("gpt-6",         {"context": 400000, "maxOutput": 128000}),
    ("gpt-5",         {"context": 400000, "maxOutput": 128000}),
    ("gemini",        {"context": 1000000, "maxOutput": 65536}),
    ("deepseek",      {"context": 128000, "maxOutput": 8192}),
    ("grok",          {"context": 256000, "maxOutput": 32000}),
    ("qwen",          {"context": 262144, "maxOutput": 32768}),
    ("kimi",          {"context": 256000, "maxOutput": 32000}),
    ("llama",         {"context": 128000, "maxOutput": 8192}),
    ("mistral",       {"context": 128000, "maxOutput": 8192}),
    ("",              {"context": 128000, "maxOutput": 8192}),
]


def model_limits(model):
    name = (model or "").lower()
    for prefix, limits in MODEL_FAMILIES:
        if name.startswith(prefix):
            return limits
    return MODEL_FAMILIES[-1][1]


def known_model(provider, model):
    return model in MODELS.get(provider, [])


def log(*parts):
    sys.stderr.write("llmsim: " + " ".join(str(p) for p in parts) + "\n")
    sys.stderr.flush()


def rand_id(prefix, n=24):
    body = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))
    return prefix + body


def _call_id(shape):
    """A tool call's id, in the form the caller's vendor uses.

    A scripted call has none, and the body builders used to invent one while
    sending. Made here instead, before the answer is sent *and* before it is
    recorded, so the id the client saw is the id a replay gives back — an
    agent that echoes it in its tool result sees the same one twice.
    """
    return rand_id("toolu_") if shape == "anthropic" else rand_id("call_", 20)


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


# ── Placeholders ─────────────────────────────────────────────────────────
#
# A scripted turn is fixed text, and two things about a conversation cannot
# be written down in advance: what was asked, and what a tool printed. A
# retrieval rule needs both — the search has to be *for the question*, and
# the answer has to carry what came back, or a test of a grounded agent can
# only assert that a tool was called, never that its result reached the
# reader. So two placeholders, and no more than two:
#
#   {{ask}}          the opening ask — the same text the rule was matched on
#   {{tool_output}}  what the most recent tool call printed
#
# Substituted in a turn's text and in the string arguments of its tool calls.

TOOL_OUTPUT_LIMIT = 1200


def last_tool_output(messages):
    """What the most recent tool call printed, or "" if none has run."""
    for msg in reversed(messages):
        role = msg.get("role")
        if role == "tool":
            content = msg.get("content")
            if isinstance(content, list):
                content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
            return str(content or "")
        if role == "user":
            text, is_tool_result = flatten(msg)
            if is_tool_result:
                return text
    return ""


def fill(turn, messages):
    """The turn with its placeholders filled in. The script is not touched."""
    if "{{" not in json.dumps(turn):
        return turn
    values = {
        "{{ask}}": subject(messages).strip(),
        "{{tool_output}}": last_tool_output(messages).strip()[:TOOL_OUTPUT_LIMIT],
    }

    def walk(value):
        if isinstance(value, str):
            for key, replacement in values.items():
                value = value.replace(key, replacement)
            return value
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        return value

    return walk(turn)


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





# ── Running a functional test ────────────────────────────────────────────
#
# A case is executed against this server over HTTP, on the real vendor
# routes, with the real keys. It is not a shortcut into choose_turn: the
# point is to exercise the whole path the caller uses — auth, the wire
# format, the routing table, the per-model answer and the relay — so that a
# test which passes here means the caller's own request would have worked.
#
# Synchronous by design. A step can depend on the one before it (select,
# then verify, then authorise), so they run in order and stop at the first
# failure; a plan runs its cases in order for the same reason.

RESULTS = {"runs": []}          # newest first, bounded below
RESULTS_LOCK = threading.Lock()


def _self_request(provider, model, messages, tools=None, timeout=180):
    """One call to this server, in that provider's own wire format."""
    if provider == "anthropic":
        path, headers = "/anthropic/v1/messages", {"x-api-key": API_KEYS["anthropic"]}
        payload = {"model": model, "max_tokens": 1024, "messages": messages}
    else:
        path = "/%s/v1/chat/completions" % provider
        headers = {"Authorization": "Bearer " + API_KEYS[provider]}
        payload = {"model": model, "messages": messages}
    if tools:
        payload["tools"] = tools
    headers["content-type"] = "application/json"
    # Says whose request this is. The functional cases check a CALLER against
    # the script's rules, so they must be answered by the script — not by a relay
    # or a ChatGPT plan that would spend something and say something else — and
    # they are no part of any session being recorded or replayed.
    headers["X-LLMSim-Internal"] = "1"

    url = "http://127.0.0.1:%d%s" % (PORT, path)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, {}
    except Exception as exc:
        return 0, {"error": {"message": "%s: %s" % (type(exc).__name__, exc)}}


def _answer_of(body):
    """The assistant's words and tool calls, whichever format answered.

    Calls carry their arguments, not just their names. "Write was called" is
    half an answer: the question a test is really asking is whether the agent
    wrote the right file, and a run that proves only the name passes happily
    while the agent writes to /tmp.
    """
    text, calls = [], []
    for block in body.get("content") or []:
        if block.get("type") == "text":
            text.append(block.get("text") or "")
        elif block.get("type") == "tool_use":
            calls.append({"name": block.get("name") or "", "input": block.get("input") or {}})
    for choice in body.get("choices") or []:
        msg = choice.get("message") or {}
        if msg.get("content"):
            text.append(msg["content"])
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            # OpenAI sends arguments as a JSON string. A caller that emitted
            # something unparseable is itself worth asserting on, so the raw
            # text is kept rather than dropped.
            raw = fn.get("arguments")
            try:
                args = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except ValueError:
                args = {"_raw": raw}
            calls.append({"name": fn.get("name") or "", "input": args})
    if not text and body.get("error"):
        text.append(body["error"].get("message") or "")
    return "\n".join(t for t in text if t), [c for c in calls if c["name"]]


def _dig(value, path):
    """A dotted path into a tool's arguments: file_path, edits.0.new_string."""
    for part in str(path).split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
        if value is None:
            return None
    return value


def _as_text(value):
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def _matches(how, want, text):
    """One comparison, used by every expectation so they all behave alike.

    A bad regex raises, and the caller turns that into a failed step naming
    the pattern — a test that silently never matches is worse than one that
    says the pattern is wrong.
    """
    if how == "regex":
        return bool(re.search(want, text, re.I | re.S))
    if how == "equals":
        return text.strip() == want
    return want.lower() in text.lower()


def _step_verdict(step, status, answer, calls, ms):
    """Why this step passed or failed, in words worth reading.

    Every expectation is optional and they compose: a step can require a
    status, a tool, an argument on that tool, words in the answer, words that
    must NOT be in it, a tool that must NOT have been called, and a deadline.
    The negatives matter as much as the positives — "never invents an invoice
    number" is only expressible as something that must not appear, and it is
    exactly the failure the whole platform exists to catch.
    """
    names = [c["name"] for c in calls]
    met = []

    want_status = step.get("expectStatus")
    if want_status and int(want_status) != status:
        return False, "expected HTTP %s, got %s" % (want_status, status)
    if not want_status and status >= 400:
        return False, "HTTP %s: %s" % (status, answer[:120])
    if want_status:
        met.append("HTTP %s" % want_status)

    # A deadline. The slow case proves a late answer still arrives; this is
    # the other half — a request that is meant to be quick staying quick,
    # which is the thing that regresses silently.
    max_ms = step.get("maxMs")
    if max_ms:
        if ms > int(max_ms):
            return False, "expected an answer within %sms, took %dms" % (max_ms, ms)
        met.append("within %sms" % max_ms)

    want_tool = (step.get("expectTool") or "").strip()
    if want_tool:
        if want_tool not in names:
            return False, "expected a %s tool call, got %s" % (want_tool, names or "none")
        met.append("%s called" % want_tool)

    reject_tool = (step.get("rejectTool") or "").strip()
    if reject_tool:
        if reject_tool in names:
            return False, "%s must not have been called, and was" % reject_tool
        met.append("no %s call" % reject_tool)

    # Arguments, on the tool the step named. Checked against every call to
    # that tool, because an agent may call it more than once and one of them
    # being right is what was asked.
    wanted_args = step.get("expectToolArgs") or {}
    if wanted_args:
        if not want_tool:
            return False, "an argument expectation needs a tool to assert on"
        candidates = [c for c in calls if c["name"] == want_tool]
        for path, want in wanted_args.items():
            hit = False
            seen = []
            for call in candidates:
                found = _dig(call["input"], path)
                if found is None:
                    continue
                text = _as_text(found)
                seen.append(text[:80])
                try:
                    if _matches(step.get("argMatch") or "contains", str(want), text):
                        hit = True
                        break
                except re.error as exc:
                    return False, "%s is not a valid regex: %s" % (path, exc)
            if not hit:
                return False, "%s.%s expected %r, got %s" % (
                    want_tool, path, want, (", ".join(seen) or "nothing"))
        met.append("%s args" % want_tool)

    # What must not appear — checked against the words AND the arguments of
    # every tool call, which is where the dangerous version of this lives: an
    # agent that cannot reach SAP and puts a made-up total into a Bash command
    # has fabricated it just as surely as if it had said it out loud.
    # Deliberately wider than `expect`, which stays on the answer: a positive
    # match cares where it appeared, a prohibition does not.
    #
    # Run before the positive, so a step that asserts both reports the more
    # alarming failure.
    reject = (step.get("reject") or "").strip()
    if reject:
        how = step.get("rejectMatch") or "contains"
        places = [("the answer", answer)]
        for call in calls:
            for key, value in (call["input"] or {}).items():
                places.append(("%s.%s" % (call["name"], key), _as_text(value)))
        for where, text in places:
            try:
                if _matches(how, reject, text):
                    return False, "%s must not %s %r, and does" % (
                        where, how.rstrip("s") if how == "contains" else how, reject[:80])
            except re.error as exc:
                return False, "the rejection is not a valid regex: %s" % exc
        met.append("no %r anywhere" % reject[:40])

    want = (step.get("expect") or "").strip()
    if not want:
        # What was actually checked, named. "no expectation set" on a step
        # that asserted a status or a tool call reads like the step proved
        # nothing, which is the opposite of true and the sort of thing that
        # gets a passing suite mistrusted.
        return True, (", ".join(met) if met else "no expectation set — reached the caller and answered")
    how = step.get("match") or "contains"
    try:
        ok = _matches(how, want, answer)
    except re.error as exc:
        return False, "the expectation is not a valid regex: %s" % exc
    return ok, (", ".join(met + ["matched"]) if ok else "expected %s %r" % (how, want[:80]))


def run_case(case):
    started = time.time()
    provider = case.get("provider") or "anthropic"
    if provider not in API_KEYS:
        provider = "anthropic"
    model = case.get("model") or MODELS[provider][0]
    tools = case.get("tools") or None

    # Carried between steps, so step two is a reply in the same conversation
    # rather than a fresh one — which is the whole point of a multi-step case.
    messages, steps = [], []
    for step in case.get("steps") or []:
        # A step is either a person speaking or a tool reporting back, and the
        # difference decides which rule answers it.
        #
        # subject() deliberately ignores tool output when it works out what a
        # conversation is about, so that an agent loop keeps the rule its
        # opening ask chose rather than being re-matched against whatever a
        # tool printed. A harness that could only send plain user text
        # therefore could not model a loop at all: step two would look like a
        # new question and pull the conversation into a different rule. Found
        # exactly that way — a two-step code-generation case failed on its
        # second step for no reason visible in the case.
        if step.get("asToolResult"):
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "content": step.get("input") or ""}]})
        else:
            messages.append({"role": "user", "content": step.get("input") or ""})
        at = time.time()
        status, body = _self_request(provider, model, messages, tools)
        # Measured before it is judged, because a deadline is one of the
        # things that can be judged.
        ms = int((time.time() - at) * 1000)
        answer, calls = _answer_of(body)
        ok, why = _step_verdict(step, status, answer, calls, ms)
        steps.append({
            "input": step.get("input") or "", "expect": step.get("expect") or "",
            "match": step.get("match") or "contains", "status": status,
            "answer": answer[:600], "tools": [c["name"] for c in calls],
            "calls": calls, "ok": ok, "why": why, "ms": ms,
        })
        messages.append({"role": "assistant", "content": answer})
        if not ok:
            break      # a later step assumes this one worked

    ran = len(steps)
    passed = all(s["ok"] for s in steps) and ran > 0
    return {
        "id": case.get("id"), "name": case.get("name") or case.get("id"),
        "provider": provider, "model": model,
        "status": "passed" if passed else ("failed" if ran else "empty"),
        "steps": steps, "ran": ran, "total": len(case.get("steps") or []),
        "ms": int((time.time() - started) * 1000),
    }


def run_cases(cases, label):
    results = [run_case(c) for c in cases]
    run = {
        "at": time.time(), "label": label, "results": results,
        "passed": sum(1 for r in results if r["status"] == "passed"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "empty": sum(1 for r in results if r["status"] == "empty"),
        "total": len(results),
        "ms": sum(r["ms"] for r in results),
    }
    with RESULTS_LOCK:
        RESULTS["runs"].insert(0, run)
        del RESULTS["runs"][20:]     # the last twenty runs is plenty to compare
    return run


# ── What came in ─────────────────────────────────────────────────────────
#
# A bounded, in-memory log of the last few hundred requests, for the UI to
# show. Deliberately not written to disk: this is a simulator, the log is a
# window rather than a record, and a file would be one more thing to rotate.
#
# Bounded on purpose too. An agent loop can be thousands of turns, and a list
# that grows without limit is a memory leak with a nice name.
REQUESTS = collections.deque(maxlen=int(os.environ.get("LLMSIM_LOG_SIZE", "300")))
REQUEST_SEQ = itertools.count(1)
REQUEST_SEQ_PEEK_COUNTER = itertools.count(1)
_SEQ_SEEN = [0]


def REQUEST_SEQ_PEEK():
    return _SEQ_SEEN[0]
REQUESTS_LOCK = threading.Lock()


def record(entry):
    entry["n"] = next(REQUEST_SEQ)
    _SEQ_SEEN[0] = entry["n"]
    entry["at"] = time.time()
    with REQUESTS_LOCK:
        REQUESTS.append(entry)
    return entry



# ── Live stream framing, for the relay ───────────────────────────────────
#
# The scripted path builds a whole turn and hands it to the *_sse generators,
# which is right when the answer is already known. A relayed answer is not:
# it arrives a piece at a time and the pauses between pieces are the point,
# so these emit the same events the generators do, split so the middle can be
# driven by whatever the upstream sends.

def _delta_text(event):
    """The text in one upstream SSE event, whichever format sent it."""
    # Anthropic
    if event.get("type") == "content_block_delta":
        return (event.get("delta") or {}).get("text") or ""
    # OpenAI chat
    for choice in event.get("choices") or []:
        piece = (choice.get("delta") or {}).get("content")
        if piece:
            return piece
    # Responses
    if event.get("type") == "response.output_text.delta":
        return event.get("delta") or ""
    return ""


def _stream_open(shape, model):
    if shape == "anthropic":
        shell = anthropic_body({"text": ""}, model)
        shell["content"], shell["stop_reason"] = [], None
        shell["usage"] = {"input_tokens": shell["usage"]["input_tokens"], "output_tokens": 0}
        return [("message_start", {"type": "message_start", "message": shell}),
                ("ping", {"type": "ping"}),
                ("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "text", "text": ""}})]
    if shape == "responses":
        return [("response.created", {"type": "response.created",
                                      "response": responses_body({"text": ""}, model)})]
    return []


def _stream_delta(shape, piece):
    if shape == "anthropic":
        return [("content_block_delta", {"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "text_delta", "text": piece}})]
    if shape == "responses":
        return [("response.output_text.delta", {"type": "response.output_text.delta", "delta": piece})]
    return [("", {"id": rand_id("chatcmpl-", 20), "object": "chat.completion.chunk",
                  "created": int(time.time()), "model": "relay",
                  "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]})]


def _stream_close(shape, text):
    if shape == "anthropic":
        return [("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta",
                                   "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                   "usage": {"output_tokens": rough_tokens(text)}}),
                ("message_stop", {"type": "message_stop"})]
    if shape == "responses":
        return [("response.completed", {"type": "response.completed",
                                        "response": responses_body({"text": text}, "relay")})]
    return [("", {"id": rand_id("chatcmpl-", 20), "object": "chat.completion.chunk",
                  "created": int(time.time()), "model": "relay",
                  "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            ("", "[DONE]")]


# ── Relay: one real model answers for every model ────────────────────────
#
# The whole mode fits in one function because of a decision made earlier in
# this file: a scripted rule produces a plain {text, tool_calls} dict, and
# every format builder below consumes that and nothing else. So a real
# model's answer converted into the same dict is served by the same three
# body builders and the same three SSE builders, in whichever format the
# client asked. Relay never touches them.

class UpstreamError(Exception):
    pass


def upstream_turn(messages, declared, tools_body):
    """Ask the one configured model, and return a turn in the script's shape.

    The client's own tool declarations are forwarded, so an agent loop is
    real: the model can call the tools that client actually has, and the call
    comes back through the same path a scripted one does — including the
    name and argument realignment, which a real model needs just as much as
    a hand-written rule does.
    """
    up = CONFIG["upstream"]
    if up["format"] == "anthropic":
        url = up["url"] + "/v1/messages"
        headers = {"x-api-key": up["key"], "anthropic-version": "2023-06-01"}
        payload = {
            "model": up["model"],
            "max_tokens": 2048,
            "messages": [m for m in messages if m.get("role") in ("user", "assistant")],
        }
        if tools_body:
            payload["tools"] = tools_body
    else:
        url = up["url"] + "/v1/chat/completions"
        headers = {"Authorization": "Bearer " + up["key"]}
        payload = {"model": up["model"], "messages": messages}
        if tools_body:
            payload["tools"] = tools_body

    headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise UpstreamError("%s %s: %s" % (exc.code, exc.reason, exc.read()[:200].decode("utf-8", "replace")))
    except Exception as exc:
        raise UpstreamError("%s: %s" % (type(exc).__name__, exc))

    return _turn_from_upstream(body)


def _turn_from_upstream(body):
    """Whatever the upstream sent, as {text, tool_calls}."""
    text_parts, calls = [], []

    # Anthropic shape
    if isinstance(body.get("content"), list):
        for block in body["content"]:
            if block.get("type") == "text" and block.get("text"):
                text_parts.append(block["text"])
            elif block.get("type") == "tool_use":
                calls.append({"name": block.get("name", ""), "input": block.get("input") or {}})
    # OpenAI shape
    for choice in body.get("choices") or []:
        msg = choice.get("message") or {}
        if msg.get("content"):
            text_parts.append(msg["content"])
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"_raw": args}   # keep it rather than drop it
            calls.append({"name": fn.get("name", ""), "input": args or {}})

    turn = {}
    if text_parts:
        turn["text"] = "\n".join(text_parts)
    if calls:
        turn["tool_calls"] = calls

    # The upstream's own numbers, carried through instead of discarded.
    #
    # Relay exists so a demo has real answers; a demo that cannot say what it
    # cost is missing the question everyone asks second. Synthetic counts are
    # right for a scripted answer and wrong here — this one really was billed.
    usage = body.get("usage") or {}
    if usage:
        turn["usage"] = {
            "input": usage.get("input_tokens", usage.get("prompt_tokens", 0)),
            "output": usage.get("output_tokens", usage.get("completion_tokens", 0)),
            "cacheRead": usage.get("cache_read_input_tokens",
                                   (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)),
            "model": CONFIG["upstream"]["model"],
        }
    # A model that answered with neither is still an answer; saying so beats
    # an empty body the client has to guess about.
    if not turn:
        turn["text"] = "(the upstream model returned no content)"
    return turn



def _error_type(status):
    """The vendor's own name for a status, so a client's handling matches."""
    return {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        413: "request_too_large",
        429: "rate_limit_error",
        500: "api_error",
        529: "overloaded_error",
    }.get(status, "api_error")


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
    # A model's own answer wins over the script. It is a statement about the
    # model rather than about the prompt, so nothing about the conversation
    # should be able to talk it out of applying.
    fixed = CONFIG.get("responses", {}).get(model)
    if fixed:
        turn = {}
        if fixed.get("status"):
            turn["error"] = {
                "status": int(fixed["status"]),
                "type": fixed.get("type") or _error_type(int(fixed["status"])),
                "message": fixed.get("text") or "Set for %s in the console." % model,
            }
        else:
            turn["text"] = fixed.get("text") or "(no answer set for %s)" % model
        if fixed.get("delay_ms"):
            turn["delay_ms"] = int(fixed["delay_ms"])
        return "model:" + model, turn

    text = subject(messages)
    turns_taken = sum(1 for m in messages if m.get("role") == "assistant")

    for rule in SCRIPT.get("rules", []):
        if matches(rule, provider, model, text, declared):
            turns = rule.get("turns") or []
            if not turns:
                continue
            turn = turns[min(turns_taken, len(turns) - 1)]
            return rule.get("name", "unnamed"), fill(turn, messages)

    return "default", SCRIPT.get("default", {"text": "LLMSim has no scripted answer for that."})


# ── Anthropic: /v1/messages ──────────────────────────────────────────────


def _anthropic_usage(input_tokens, output_tokens, cached=None):
    """input/output, with the cache split a real response carries.

    cached is given when the numbers are the vendor's own, and then they are
    used as they are; otherwise the split is synthetic, from the total.
    """
    if cached is not None:
        return {"input_tokens": max(0, input_tokens - cached), "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": cached, "output_tokens": output_tokens, "service_tier": "standard"}
    cached = int(input_tokens * 0.8)        # most of a harness prompt is reused
    created = int(input_tokens * 0.1)
    return {
        "input_tokens": input_tokens - cached - created,
        "cache_creation_input_tokens": created,
        "cache_read_input_tokens": cached,
        "output_tokens": output_tokens,
        "service_tier": "standard",
    }


def anthropic_body(turn, model):
    content = []
    if turn.get("text"):
        content.append({"type": "text", "text": turn["text"]})
    for call in turn.get("tool_calls", []):
        content.append({
            "type": "tool_use",
            "id": call.get("id") or rand_id("toolu_"),
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
        # The cache fields are not decoration. Claude Code reports them, and
        # Maniple's own extractUsage reads cacheReadInputTokens and
        # cacheCreationInputTokens — so a usage path that handles them could
        # never be exercised while this returned two numbers.
        #
        # Split from the same synthetic total rather than invented on top of
        # it, so the arithmetic a caller does still adds up.
        "usage": _anthropic_usage(turn.get("input_tokens", 1200),
                                  turn.get("output_tokens") or rough_tokens(out_text),
                                  turn.get("cached_tokens")),
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
    message = {"role": "assistant", "content": turn.get("text") or None, "refusal": None}
    finish = turn.get("stop_reason", "stop")

    if turn.get("tool_calls"):
        message["tool_calls"] = [{
            "id": c.get("id") or rand_id("call_", 20),
            "type": "function",
            "function": {"name": c["name"], "arguments": json.dumps(c.get("input", {}))},
        } for c in turn["tool_calls"]]
        finish = "tool_calls"

    prompt_tokens = turn.get("input_tokens", 1200)
    completion_tokens = turn.get("output_tokens") or rough_tokens(json.dumps(message))
    cached = turn.get("cached_tokens")
    return {
        "id": rand_id("chatcmpl-", 20),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        # system_fingerprint and service_tier are read by clients that pin
        # behaviour to a backend build; logprobs and refusal are absent from
        # a real answer as null rather than missing, and a client that tests
        # for the key rather than the value can tell the difference.
        "system_fingerprint": "fp_llmsim0001",
        "service_tier": "default",
        "choices": [{"index": 0, "message": message, "finish_reason": finish,
                     "logprobs": None}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {"cached_tokens": int(prompt_tokens * 0.8) if cached is None else cached,
                                      "audio_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": turn.get("reasoning_tokens", 0), "audio_tokens": 0,
                                          "accepted_prediction_tokens": 0,
                                          "rejected_prediction_tokens": 0},
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
            "type": "function_call", "id": rand_id("fc_"), "call_id": call.get("id") or rand_id("call_", 20),
            "name": call["name"], "arguments": json.dumps(call.get("input", {})), "status": "completed",
        })

    input_tokens = turn.get("input_tokens", 1200)
    output_tokens = turn.get("output_tokens") or rough_tokens(json.dumps(output))
    cached = turn.get("cached_tokens")
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
        # The rest of the envelope a Responses client reads back. Most are
        # null or a default in a plain answer — which is the point: a client
        # that branches on `reasoning` or resumes from `previous_response_id`
        # cannot be tested against a body where the key is simply absent.
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "previous_response_id": None,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "temperature": 1.0,
        "top_p": 1.0,
        "truncation": "disabled",
        "store": True,
        "metadata": {},
        "reasoning": {"effort": None, "summary": None},
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": int(input_tokens * 0.8) if cached is None else cached},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": turn.get("reasoning_tokens", 0)},
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


# ── ChatGPT plan: a third route ──────────────────────────────────────────
#
# Scripted answers here, one real model through an API key (relay), and now a
# real model through a ChatGPT *subscription*: the person signs in with
# OpenAI once, and every model routed to "chatgpt" is answered by their plan.
#
#     client (Chat Completions | Anthropic Messages | Responses)
#         │   canon_request()
#         ▼
#     CanonicalRequest ── chatgpt_payload() ──▶ POST {backend}/responses
#         ▲                                        store:false, stream:true
#         │                                        │
#     encoders ◀── CanonicalEvents ◀── chatgpt_events()
#     (the client's own wire format, streamed or whole)
#
# The middle is the one format the plan backend speaks. Every client format
# goes in and comes out through the same two seams, so a fourth client format
# is one translator rather than another copy of the whole path.
#
# How the sign-in works is not invented here. It is the OAuth + PKCE flow
# OpenAI's own open-source Codex client uses, with that client's registered
# id — which is why the redirect has to land on 127.0.0.1:1455 (or 1457) and
# why there is a paste-the-URL fallback for a LLMSim that is not on the same
# machine as the browser. Endpoints and the client id can be overridden by
# environment variable, and the tests do exactly that to run against a stub.

CHATGPT_ISSUER = os.environ.get("LLMSIM_CHATGPT_ISSUER", "https://auth.openai.com").rstrip("/")
CHATGPT_BACKEND = os.environ.get("LLMSIM_CHATGPT_BACKEND", "https://chatgpt.com/backend-api/codex").rstrip("/")
CHATGPT_CLIENT_ID = os.environ.get("LLMSIM_CHATGPT_CLIENT_ID", "REMOVED_CLIENT_ID")
CHATGPT_ORIGINATOR = os.environ.get("LLMSIM_CHATGPT_ORIGINATOR", "removed_originator")
CHATGPT_SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
CHATGPT_PORTS = tuple(int(p) for p in os.environ.get("LLMSIM_CHATGPT_PORTS", "1455,1457").split(","))
CHATGPT_AUTH_PATH = os.environ.get("LLMSIM_CHATGPT_AUTH", os.path.join(HERE, "llmsim.chatgpt-auth.json"))
CHATGPT_CLIENT_VERSION = os.environ.get("LLMSIM_CHATGPT_CLIENT_VERSION", "1.0.0")
CHATGPT_USER_AGENT = "LLMSim/0.1 (+https://github.com/ManipleAI/LLMSim)"
CHATGPT_SESSION = str(uuid.uuid4())
CHATGPT_REFRESH_WINDOW = 300          # refresh when the access token has under five minutes left
CHATGPT_LOGIN_TTL = 600               # a started sign-in is abandoned after ten minutes

CHATGPT_LOCK = threading.RLock()
CHATGPT = {
    "auth": None,       # the stored tokens; never leaves this process
    "dead": False,      # the refresh token was refused for good — sign in again
    "pending": None,    # a sign-in that has been started and not finished
    "error": None,      # the last thing that went wrong, for the console to say
}


class ChatGPTError(Exception):
    """A failure to report in the client's own error envelope."""

    def __init__(self, status, kind, message):
        Exception.__init__(self, message)
        self.status, self.kind, self.message = status, kind, message


class ChatGPTAuthError(ChatGPTError):
    def __init__(self, message, permanent=False):
        ChatGPTError.__init__(self, 502, "upstream_error", message)
        self.permanent = permanent


# -- tokens: where they live, and how they are read --------------------------

def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt_claims(token):
    """A JWT's payload. Not verified — it came straight from the issuer over
    TLS a moment ago, and the only use here is to read an expiry and the
    account it is for, never to decide whether to trust anybody."""
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def _load_chatgpt_auth():
    try:
        with open(CHATGPT_AUTH_PATH) as fh:
            data = json.load(fh)
        return data if data.get("access_token") and data.get("refresh_token") else None
    except FileNotFoundError:
        return None
    except Exception as exc:
        log("could not read %s: %s" % (CHATGPT_AUTH_PATH, exc))
        return None


def _save_chatgpt_auth(auth):
    """Owner-only, and written whole or not at all.

    These are the keys to a person's subscription. The file is created 0600
    before anything is written to it, and replaced atomically so a refresh
    that is interrupted cannot leave half a token behind.
    """
    tmp = CHATGPT_AUTH_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(auth, fh)
    os.replace(tmp, CHATGPT_AUTH_PATH)


def _forget_chatgpt_auth():
    try:
        os.remove(CHATGPT_AUTH_PATH)
    except FileNotFoundError:
        pass


def _auth_from_tokens(tokens, previous=None):
    """What to keep from a token response. A refresh may omit the id_token and
    may rotate the refresh token; whatever is missing is carried over."""
    previous = previous or {}
    access = tokens.get("access_token") or previous.get("access_token")
    id_token = tokens.get("id_token") or previous.get("id_token") or ""
    if not access:
        raise ChatGPTAuthError("OpenAI answered without an access token.")

    claims = _jwt_claims(id_token)
    ours = claims.get("https://api.openai.com/auth") or _jwt_claims(access).get("https://api.openai.com/auth") or {}
    account_id = ours.get("chatgpt_account_id") or previous.get("account_id")
    if not account_id:
        # An ordinary API-platform login has no ChatGPT account behind it, and
        # would only fail later at the backend with something less clear.
        raise ChatGPTAuthError("That sign-in has no ChatGPT account on it. Sign in with the account that holds the ChatGPT plan.")

    exp = _jwt_claims(access).get("exp")
    if not exp:
        exp = time.time() + int(tokens.get("expires_in") or 3600)
    profile = claims.get("https://api.openai.com/profile") or {}
    return {
        "access_token": access,
        "refresh_token": tokens.get("refresh_token") or previous.get("refresh_token"),
        "id_token": id_token,
        "account_id": account_id,
        "email": claims.get("email") or profile.get("email") or previous.get("email") or "",
        "plan": ours.get("chatgpt_plan_type") or previous.get("plan") or "",
        "expires_at": float(exp),
        "signed_in_at": previous.get("signed_in_at") or time.time(),
    }


def _oauth_error_text(raw):
    """What OpenAI said went wrong, in a sentence, from whatever shape it used."""
    try:
        body = json.loads(raw)
    except Exception:
        return raw[:200].decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)[:200]
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("message") or err.get("code") or json.dumps(err)[:200]
    if err:
        return "%s%s" % (err, ": " + body["error_description"] if body.get("error_description") else "")
    return body.get("message") or body.get("detail") or json.dumps(body)[:200]


def _chatgpt_token_request(payload, as_json):
    if as_json:
        data, kind = json.dumps(payload).encode(), "application/json"
    else:
        data, kind = urllib.parse.urlencode(payload).encode(), "application/x-www-form-urlencoded"
    req = urllib.request.Request(
        CHATGPT_ISSUER + "/oauth/token", data=data,
        headers={"Content-Type": kind, "Accept": "application/json", "User-Agent": CHATGPT_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        text = _oauth_error_text(exc.read())
        # 400/401/403 from the token endpoint are an answer, not an outage: the
        # grant is spent, revoked or wrong, and asking again will not change it.
        raise ChatGPTAuthError("OpenAI refused the token request (%d): %s" % (exc.code, text),
                               permanent=exc.code in (400, 401, 403))
    except Exception as exc:
        raise ChatGPTAuthError("Could not reach OpenAI to sign in: %s" % exc)


def chatgpt_access(force=False):
    """(access token, account id), refreshed first if it is about to lapse."""
    with CHATGPT_LOCK:
        auth = CHATGPT["auth"]
        if not auth:
            raise ChatGPTAuthError("The ChatGPT plan is not signed in. Sign in on the Routing page.")
        if CHATGPT["dead"]:
            raise ChatGPTAuthError("The ChatGPT sign-in has expired. Sign in again on the Routing page.", permanent=True)
        if force or auth["expires_at"] - time.time() < CHATGPT_REFRESH_WINDOW:
            try:
                tokens = _chatgpt_token_request({
                    "client_id": CHATGPT_CLIENT_ID, "grant_type": "refresh_token",
                    "refresh_token": auth["refresh_token"], "scope": "openid profile email"}, as_json=True)
                CHATGPT["auth"] = auth = _auth_from_tokens(tokens, auth)
                _save_chatgpt_auth(auth)
                CHATGPT["error"] = None
                log("chatgpt: access token refreshed")
            except ChatGPTAuthError as exc:
                if exc.permanent:
                    # Stop asking. A spent refresh token asked for again on
                    # every request is a lockout dressed up as a retry.
                    CHATGPT["dead"] = True
                    exc = ChatGPTAuthError("The ChatGPT sign-in could not be renewed (%s). Sign in again on the Routing page."
                                           % exc.message.split(": ", 1)[-1], permanent=True)
                CHATGPT["error"] = exc.message
                raise exc
        return auth["access_token"], auth["account_id"]


# -- signing in ---------------------------------------------------------------

def _stop_pending_login():
    """Abandon a started sign-in.

    Releasing the listening port is asynchronous on purpose: this is also
    called from inside the callback handler, which runs on the listener's own
    thread, and waiting for that thread to finish from inside it would wait
    forever. Whoever wants the port next waits for it instead.
    """
    pending = CHATGPT["pending"]
    CHATGPT["pending"] = None
    if pending:
        if pending.get("timer"):
            pending["timer"].cancel()
        server = pending.get("listener")
        if server:
            def stop():
                # shutdown() waits for the serve loop to leave; closing the
                # socket before it has is closing it under a select().
                server.shutdown()
                server.server_close()
            threading.Thread(target=stop, daemon=True).start()


def _callback_page(ok, message):
    colour, title = ("#1a7f4b", "Signed in") if ok else ("#b42318", "Sign-in did not finish")
    return ("<!doctype html><meta charset=utf-8><title>LLMSim</title><body style=\"font:16px -apple-system,system-ui,sans-serif;"
            "max-width:30rem;margin:15vh auto;padding:0 1rem\"><h1 style=\"color:%s\">%s</h1><p>%s</p>"
            "<p style=\"color:#666\">You can close this tab and go back to LLMSim.</p>" % (colour, title, message)).encode()


class _CallbackHandler(BaseHTTPRequestHandler):
    """The one URL OpenAI sends the browser back to."""

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != "/auth/callback":
            self.send_response(404)
            self.end_headers()
            return
        query = urllib.parse.parse_qs(parts.query)
        ok, message = chatgpt_finish_login(
            (query.get("code") or [""])[0], (query.get("state") or [""])[0],
            (query.get("error_description") or query.get("error") or [""])[0])
        page = _callback_page(ok, message)
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(page)


def _start_callback_listener(release_wait=1.5):
    """Listen on the port OpenAI will redirect to, on loopback only.

    Returns (server, port). The server is None when neither port could be had
    — Codex itself may be signing in on one of them — in which case the
    console falls back to asking for the redirected URL to be pasted.

    release_wait is how long to keep trying before deciding the port belongs
    to somebody else. It is always allowed, not only after an abandoned
    sign-in, because the ways of getting here — clicking Sign in twice, or
    signing out and straight back in — all find the port still held for a
    moment, and without the wait each quietly downgrades itself to the paste
    flow. The cost is that a port held by something else, Codex itself say,
    shows the paste box a second and a half later.
    """
    deadline = time.time() + release_wait
    while True:
        for port in CHATGPT_PORTS:
            try:
                server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
            except OSError:
                continue
            threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True).start()
            return server, port
        if time.time() >= deadline:
            return None, CHATGPT_PORTS[0]
        time.sleep(0.05)


def chatgpt_begin_login():
    with CHATGPT_LOCK:
        _stop_pending_login()
        verifier = _b64url(os.urandom(64))
        state = _b64url(os.urandom(32))
        listener, port = _start_callback_listener()
        redirect_uri = "http://127.0.0.1:%d/auth/callback" % port
        url = CHATGPT_ISSUER + "/oauth/authorize?" + urllib.parse.urlencode({
            "response_type": "code",
            "client_id": CHATGPT_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": CHATGPT_SCOPE,
            "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state,
            "originator": CHATGPT_ORIGINATOR,
        })
        timer = threading.Timer(CHATGPT_LOGIN_TTL, lambda: _expire_login(state))
        timer.daemon = True
        timer.start()
        CHATGPT["pending"] = {"state": state, "verifier": verifier, "redirect_uri": redirect_uri,
                              "listener": listener, "timer": timer, "started": time.time()}
        CHATGPT["error"] = None
        log("chatgpt: sign-in started (%s)" % ("listening on 127.0.0.1:%d" % port if listener else "no callback port free — paste flow"))
        return {"url": url, "listening": listener is not None, "redirectUri": redirect_uri}


def _expire_login(state):
    with CHATGPT_LOCK:
        pending = CHATGPT["pending"]
        if pending and pending["state"] == state:
            _stop_pending_login()
            CHATGPT["error"] = "The sign-in was not finished in time."


def chatgpt_finish_login(code, state, error=""):
    """Turn what the browser brought back into stored tokens. (ok, message)"""
    with CHATGPT_LOCK:
        pending = CHATGPT["pending"]
        # Compared in constant time and only against a sign-in this process
        # started: the state is what stops somebody else's redirect being
        # accepted as ours.
        if not pending or not hmac.compare_digest(pending["state"], state or ""):
            return False, "That sign-in is not the one LLMSim started, or it has expired. Start again from the console."
        _stop_pending_login()          # a code is single-use; never let it be replayed
    if error:
        CHATGPT["error"] = "OpenAI declined the sign-in: %s" % error
        return False, CHATGPT["error"]
    if not code:
        CHATGPT["error"] = "The redirect carried no code."
        return False, CHATGPT["error"]
    try:
        tokens = _chatgpt_token_request({
            "grant_type": "authorization_code", "client_id": CHATGPT_CLIENT_ID, "code": code,
            "redirect_uri": pending["redirect_uri"], "code_verifier": pending["verifier"]}, as_json=False)
        auth = _auth_from_tokens(tokens)
    except ChatGPTAuthError as exc:
        CHATGPT["error"] = exc.message
        return False, exc.message
    with CHATGPT_LOCK:
        CHATGPT["auth"], CHATGPT["dead"], CHATGPT["error"] = auth, False, None
        _save_chatgpt_auth(auth)
    log("chatgpt: signed in as %s (%s)" % (auth["email"] or "unknown", auth["plan"] or "unknown plan"))
    return True, "LLMSim can now answer with %s's ChatGPT plan." % (auth["email"] or "your")


def chatgpt_complete_by_paste(text):
    """The same sign-in, finished from a pasted redirect URL, for when the
    browser is not on the machine LLMSim is running on."""
    text = (text or "").strip()
    query = urllib.parse.urlsplit(text).query if "://" in text else text.lstrip("?")
    found = urllib.parse.parse_qs(query)
    return chatgpt_finish_login((found.get("code") or [""])[0], (found.get("state") or [""])[0],
                                (found.get("error_description") or found.get("error") or [""])[0])


def chatgpt_logout():
    """Forget the tokens here, and ask OpenAI to revoke the refresh token.

    The revoke is best effort and says so: if it fails the person is told the
    grant may still stand at OpenAI, where it can be removed from settings.
    """
    with CHATGPT_LOCK:
        auth, CHATGPT["auth"], CHATGPT["dead"], CHATGPT["error"] = CHATGPT["auth"], None, False, None
        _stop_pending_login()
        _forget_chatgpt_auth()
    revoked = False
    if auth:
        try:
            req = urllib.request.Request(
                CHATGPT_ISSUER + "/oauth/revoke",
                data=json.dumps({"token": auth["refresh_token"], "token_type_hint": "refresh_token",
                                 "client_id": CHATGPT_CLIENT_ID}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": CHATGPT_USER_AGENT})
            with urllib.request.urlopen(req, timeout=15):
                revoked = True
        except Exception as exc:
            log("chatgpt: revoke did not go through: %s" % exc)
    log("chatgpt: signed out (revoked at OpenAI: %s)" % revoked)
    return {"ok": True, "revoked": revoked}


def chatgpt_status():
    """What the console may know. Never a token, never the account id."""
    with CHATGPT_LOCK:
        auth, pending = CHATGPT["auth"], CHATGPT["pending"]
        return {
            "signedIn": bool(auth) and not CHATGPT["dead"],
            "needsSignIn": bool(auth) and CHATGPT["dead"],
            "email": (auth or {}).get("email") or "",
            "plan": (auth or {}).get("plan") or "",
            "expiresAt": (auth or {}).get("expires_at"),
            "pending": bool(pending),
            "listening": bool(pending and pending.get("listener")),
            "redirectUri": pending["redirect_uri"] if pending else None,
            "error": CHATGPT["error"],
        }


def chatgpt_models():
    """The models this plan offers, as the backend lists them."""
    token, account = chatgpt_access()
    req = urllib.request.Request(
        "%s/models?client_version=%s" % (CHATGPT_BACKEND, urllib.parse.quote(CHATGPT_CLIENT_VERSION)),
        headers=_chatgpt_headers(token, account, stream=False))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise _chatgpt_http_error(exc)
    except Exception as exc:
        raise ChatGPTError(502, "upstream_error", "Could not list the plan's models: %s" % exc)
    rows = body.get("models") if isinstance(body, dict) else body
    return sorted({m.get("slug") or m.get("id") for m in rows or [] if isinstance(m, dict) and (m.get("slug") or m.get("id"))})


# -- the canonical request ----------------------------------------------------
#
# {"system": [str], "items": [...], "tools": [...], "tool_choice": ..., ...}
#
# items is flat and in order, and each one is one of
#     {"kind": "text",   "role": "user"|"assistant", "text": str}
#     {"kind": "image",  "url": str}                       (user only)
#     {"kind": "call",   "call_id": str, "name": str, "arguments": str}
#     {"kind": "result", "call_id": str, "output": str}
# which is the Responses API's own `input` with the sugar taken off. The
# three client formats nest the same four things three different ways —
# Anthropic puts a tool result inside a user message, Chat Completions gives
# it a role of its own — and flattening is what makes them one problem.

def _plain(content):
    """Text from content that is a string or a list of typed parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text"))
    return "" if content is None else str(content)


def _args_text(value):
    if isinstance(value, str):
        return value or "{}"
    return json.dumps(value if value is not None else {})


def _canon_blank():
    return {"system": [], "items": [], "tools": [], "tool_choice": "auto", "parallel": True,
            "effort": None, "text_format": None, "dropped": []}


def _canon_tool(name, description, parameters):
    return {"name": name, "description": description or "",
            "parameters": parameters if isinstance(parameters, dict) else {"type": "object", "properties": {}}}


# Sampling controls the plan backend does not take. Sent anyway they are a
# 400, and a client that always sends temperature would never get an answer.
_UNSUPPORTED = ("max_tokens", "max_completion_tokens", "max_output_tokens", "temperature",
                "top_p", "stop", "stop_sequences", "presence_penalty", "frequency_penalty", "seed", "n")


def canon_from_chat(body):
    out = _canon_blank()
    for msg in body.get("messages") or []:
        role, content = msg.get("role"), msg.get("content")
        if role in ("system", "developer"):
            out["system"].append(_plain(content))
        elif role == "tool":
            out["items"].append({"kind": "result", "call_id": msg.get("tool_call_id") or "", "output": _plain(content)})
        elif role == "assistant":
            if _plain(content):
                out["items"].append({"kind": "text", "role": "assistant", "text": _plain(content)})
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                out["items"].append({"kind": "call", "call_id": call.get("id") or rand_id("call_", 20),
                                     "name": fn.get("name") or "", "arguments": _args_text(fn.get("arguments"))})
        else:
            for part in content if isinstance(content, list) else [{"type": "text", "text": _plain(content)}]:
                if part.get("type") == "image_url":
                    image = part.get("image_url")
                    out["items"].append({"kind": "image", "url": image.get("url") if isinstance(image, dict) else image})
                elif _plain([part]):
                    out["items"].append({"kind": "text", "role": "user", "text": _plain([part])})
    for tool in body.get("tools") or []:
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
        if tool.get("type") == "function" and fn:
            out["tools"].append(_canon_tool(fn.get("name"), fn.get("description"), fn.get("parameters")))
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        out["tool_choice"] = {"name": (choice.get("function") or {}).get("name")}
    elif choice in ("none", "required", "auto"):
        out["tool_choice"] = choice
    if body.get("parallel_tool_calls") is False:
        out["parallel"] = False
    out["effort"] = body.get("reasoning_effort")
    fmt = body.get("response_format") or {}
    if fmt.get("type") == "json_schema" and isinstance(fmt.get("json_schema"), dict):
        spec = fmt["json_schema"]
        out["text_format"] = {"type": "json_schema", "name": spec.get("name") or "response",
                              "schema": spec.get("schema") or {}, "strict": bool(spec.get("strict"))}
    elif fmt.get("type") == "json_object":
        out["text_format"] = {"type": "json_object"}
    out["dropped"] = [k for k in _UNSUPPORTED if body.get(k) is not None]
    return out


def canon_from_anthropic(body):
    out = _canon_blank()
    system = body.get("system")
    if system:
        out["system"].append(_plain(system))
    for msg in body.get("messages") or []:
        role, content = msg.get("role"), msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content or []:
            kind = block.get("type")
            if kind == "text" and block.get("text"):
                out["items"].append({"kind": "text", "role": role, "text": block["text"]})
            elif kind == "tool_use":
                out["items"].append({"kind": "call", "call_id": block.get("id") or rand_id("toolu_"),
                                     "name": block.get("name") or "", "arguments": _args_text(block.get("input"))})
            elif kind == "tool_result":
                text = _plain(block.get("content"))
                out["items"].append({"kind": "result", "call_id": block.get("tool_use_id") or "",
                                     "output": ("Error: " + text) if block.get("is_error") else text})
            elif kind == "image":
                src = block.get("source") or {}
                url = src.get("url") if src.get("type") == "url" else (
                    "data:%s;base64,%s" % (src.get("media_type", "image/png"), src.get("data", "")))
                out["items"].append({"kind": "image", "url": url})
            # "thinking" and "redacted_thinking" are the previous model's own
            # reasoning, signed for Anthropic. They mean nothing to another model.
    for tool in body.get("tools") or []:
        if tool.get("type") in (None, "custom") and tool.get("name"):
            out["tools"].append(_canon_tool(tool["name"], tool.get("description"), tool.get("input_schema")))
        else:
            out["dropped"].append("tool:%s" % (tool.get("type") or tool.get("name")))
    choice = body.get("tool_choice") or {}
    if choice.get("type") == "any":
        out["tool_choice"] = "required"
    elif choice.get("type") == "tool":
        out["tool_choice"] = {"name": choice.get("name")}
    elif choice.get("type") == "none":
        out["tool_choice"] = "none"
    if choice.get("disable_parallel_tool_use"):
        out["parallel"] = False
    out["dropped"] += [k for k in _UNSUPPORTED if body.get(k) is not None]
    return out


def canon_from_responses(body):
    out = _canon_blank()
    if body.get("instructions"):
        out["system"].append(_plain(body["instructions"]))
    raw = body.get("input")
    if isinstance(raw, str):
        raw = [{"role": "user", "content": raw}]
    for item in raw or []:
        kind = item.get("type") or ("message" if item.get("role") else None)
        if kind == "message":
            role, content = item.get("role"), item.get("content")
            if role in ("system", "developer"):
                out["system"].append(_plain(content))
                continue
            for part in content if isinstance(content, list) else [{"type": "text", "text": _plain(content)}]:
                if part.get("type") == "input_image":
                    out["items"].append({"kind": "image", "url": part.get("image_url")})
                elif _plain([part]):
                    out["items"].append({"kind": "text", "role": role, "text": _plain([part])})
        elif kind == "function_call":
            out["items"].append({"kind": "call", "call_id": item.get("call_id") or rand_id("call_", 20),
                                 "name": item.get("name") or "", "arguments": _args_text(item.get("arguments"))})
        elif kind == "function_call_output":
            output = item.get("output")
            out["items"].append({"kind": "result", "call_id": item.get("call_id") or "",
                                 "output": output if isinstance(output, str) else json.dumps(output)})
        # reasoning items are encrypted for the model that wrote them, and
        # with store:false there is nothing for them to refer back to.
    for tool in body.get("tools") or []:
        if tool.get("type") == "function" and tool.get("name"):
            out["tools"].append(_canon_tool(tool["name"], tool.get("description"), tool.get("parameters")))
        else:
            out["dropped"].append("tool:%s" % tool.get("type"))
    choice = body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("name"):
        out["tool_choice"] = {"name": choice["name"]}
    elif choice in ("none", "required", "auto"):
        out["tool_choice"] = choice
    if body.get("parallel_tool_calls") is False:
        out["parallel"] = False
    out["effort"] = (body.get("reasoning") or {}).get("effort")
    fmt = (body.get("text") or {}).get("format")
    if isinstance(fmt, dict) and fmt.get("type") in ("json_schema", "json_object"):
        out["text_format"] = fmt
    out["dropped"] += [k for k in _UNSUPPORTED if body.get(k) is not None]
    return out


def canon_request(shape, body):
    return {"anthropic": canon_from_anthropic, "responses": canon_from_responses}.get(shape, canon_from_chat)(body)


# -- the adapter: canonical request in, the plan backend's request out --------

def _responses_input(items):
    """Canonical items as Responses `input`, runs of user text and images
    merged into one message the way a person sent them."""
    out = []
    for item in items:
        kind = item["kind"]
        if kind in ("text", "image") and (kind == "image" or item["role"] == "user"):
            part = ({"type": "input_text", "text": item["text"]} if kind == "text"
                    else {"type": "input_image", "image_url": item["url"]})
            last = out[-1] if out else None
            if last and last.get("role") == "user" and last.get("type") == "message":
                last["content"].append(part)
            else:
                out.append({"type": "message", "role": "user", "content": [part]})
        elif kind == "text":
            out.append({"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": item["text"]}]})
        elif kind == "call":
            out.append({"type": "function_call", "call_id": item["call_id"],
                        "name": item["name"], "arguments": item["arguments"]})
        elif kind == "result":
            out.append({"type": "function_call_output", "call_id": item["call_id"], "output": item["output"]})
    return out


def chatgpt_payload(canon, model):
    """POST {backend}/responses, with store:false and stream:true.

    store:false because the plan backend keeps no conversation for this
    caller to resume — every request carries its whole history, which is also
    what all three client formats already do. stream:true because that is the
    only mode the backend serves; a non-streaming client is answered by
    collecting the stream, not by asking for something else.
    """
    payload = {
        "model": model,
        "instructions": "\n\n".join(s for s in canon["system"] if s) or CONFIG["chatgpt"]["instructions"],
        "input": _responses_input(canon["items"]),
        "store": False,
        "stream": True,
        "include": [],
    }
    if canon["tools"]:
        payload["tools"] = [dict(type="function", strict=False, **t) for t in canon["tools"]]
        choice = canon["tool_choice"]
        payload["tool_choice"] = ({"type": "function", "name": choice["name"]}
                                  if isinstance(choice, dict) else choice)
        payload["parallel_tool_calls"] = canon["parallel"]
    if canon["effort"]:
        payload["reasoning"] = {"effort": canon["effort"]}
    if canon["text_format"]:
        payload["text"] = {"format": canon["text_format"]}
    return payload


def _chatgpt_headers(token, account_id, stream=True):
    return {
        "Authorization": "Bearer " + token,
        "ChatGPT-Account-ID": account_id,
        "originator": CHATGPT_ORIGINATOR,
        "session_id": CHATGPT_SESSION,
        "x-client-request-id": str(uuid.uuid4()),
        "User-Agent": CHATGPT_USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }


def _chatgpt_http_error(exc):
    """An upstream HTTP failure, as the client's own."""
    raw = exc.read()[:4000]
    message = _oauth_error_text(raw)
    try:
        err = (json.loads(raw) or {}).get("error")
        if isinstance(err, dict) and err.get("resets_in_seconds"):
            message += " (resets in %ds)" % int(err["resets_in_seconds"])
    except Exception:
        pass
    if exc.code == 429:
        return ChatGPTError(429, "rate_limit_error", "The ChatGPT plan's limit was reached: " + message)
    if exc.code in (400, 404, 413, 422):
        return ChatGPTError(400, "invalid_request_error", "ChatGPT rejected the request: " + message)
    # 401 and 403 are LLMSim's credential failing, not the caller's, so they
    # are not passed on as one: a 401 here would tell a gateway its own key
    # was wrong and send somebody off to fix the wrong thing.
    return ChatGPTError(502, "upstream_error", "ChatGPT refused the request (%d): %s" % (exc.code, message))


def chatgpt_open(payload):
    """Open the upstream stream. Raises ChatGPTError before any byte is sent
    to the client, so a failure can still be an honest HTTP status."""
    body = json.dumps(payload).encode()
    for attempt in (0, 1):
        token, account = chatgpt_access(force=attempt == 1)
        req = urllib.request.Request(CHATGPT_BACKEND + "/responses", data=body,
                                     headers=_chatgpt_headers(token, account), method="POST")
        try:
            return urllib.request.urlopen(req, timeout=180)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0:
                continue          # the token was revoked or rotated under us: refresh once and retry
            raise _chatgpt_http_error(exc)
        except Exception as exc:
            raise ChatGPTError(502, "upstream_error", "Could not reach ChatGPT: %s" % exc)


# -- canonical events: what the backend says, in the order it says it ---------
#
#   ("text", str)                                  a piece of the answer
#   ("call", {"id", "name", "arguments"})          a whole tool call
#   ("end",  {"reason", "usage": {...}})           finished
#
# A tool call is passed on when the backend has finished it rather than
# argument-fragment by fragment. No client acts on half a call, and the
# fragments are the most format-specific thing in all three wire formats.

def _failure_from(ev):
    resp = ev.get("response") or {}
    err = resp.get("error") or ev.get("error") or {}
    code = err.get("code") or err.get("type") or ""
    message = err.get("message") or json.dumps(err or ev)[:300]
    if code in ("rate_limit_exceeded", "usage_limit_reached", "insufficient_quota"):
        return ChatGPTError(429, "rate_limit_error", "The ChatGPT plan's limit was reached: " + message)
    if code in ("context_length_exceeded", "invalid_prompt", "invalid_request_error"):
        return ChatGPTError(400, "invalid_request_error", message)
    return ChatGPTError(502, "upstream_error", "ChatGPT failed: " + message)


def chatgpt_events(resp):
    saw_text, calls = False, 0
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            ev = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind in ("response.output_text.delta", "response.refusal.delta"):
            if ev.get("delta"):
                saw_text = True
                yield "text", ev["delta"]
        elif kind == "response.output_item.done":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                calls += 1
                yield "call", {"id": item.get("call_id") or rand_id("call_", 20), "name": item.get("name") or "",
                               "arguments": _args_text(item.get("arguments"))}
            elif item.get("type") == "message" and not saw_text:
                text = "".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict))
                if text:
                    saw_text = True
                    yield "text", text
        elif kind in ("response.completed", "response.incomplete"):
            done = ev.get("response") or {}
            usage = done.get("usage") or {}
            cut = (done.get("incomplete_details") or {}).get("reason") == "max_output_tokens"
            yield "end", {
                "reason": "tool_calls" if calls else ("length" if cut else "stop"),
                "usage": {
                    "input": usage.get("input_tokens", 0),
                    "output": usage.get("output_tokens", 0),
                    "cached": (usage.get("input_tokens_details") or {}).get("cached_tokens", 0),
                    "reasoning": (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0),
                },
            }
            return
        elif kind in ("response.failed", "error"):
            raise _failure_from(ev)
    raise ChatGPTError(502, "upstream_error", "ChatGPT closed the stream before the answer was finished.")


def _collect(events):
    """The whole answer as the {text, tool_calls} turn the script produces, so
    a client that did not ask for a stream is served by the builders every
    other route already uses."""
    text, calls, usage, reason = [], [], {}, "stop"
    for kind, data in events:
        if kind == "text":
            text.append(data)
        elif kind == "call":
            try:
                args = json.loads(data["arguments"])
            except ValueError:
                args = {"_raw": data["arguments"]}     # keep what was said rather than drop it
            calls.append({"id": data["id"], "name": data["name"], "input": args})
        elif kind == "end":
            usage = data["usage"]
            reason = data["reason"]
    turn = {"text": "".join(text)} if text else {}
    if calls:
        turn["tool_calls"] = calls
    if not turn:
        turn["text"] = "(ChatGPT returned no content)"
    if reason == "length":
        turn["stop_reason"] = "max_tokens"
    turn.update(input_tokens=usage.get("input", 0), output_tokens=usage.get("output", 0),
                cached_tokens=usage.get("cached", 0), reasoning_tokens=usage.get("reasoning", 0))
    turn["usage"] = {"input": usage.get("input", 0), "output": usage.get("output", 0),
                     "cacheRead": usage.get("cached", 0), "model": CONFIG["chatgpt"]["model"]}
    return turn


# -- encoders: canonical events out, in the client's own wire format ----------
#
# Each takes events one at a time and returns the frames to write, in the
# (event name, payload) form write_event() already understands. The opening
# frames are held back until there is something to say, so a stream that
# fails before its first word is still an HTTP error rather than a half-built
# response.

class _ChatEnc:
    def __init__(self, model, include_usage):
        self.model, self.include_usage = model, include_usage
        self.id, self.created = rand_id("chatcmpl-", 20), int(time.time())
        self.started, self.calls = False, 0

    def _chunk(self, delta, finish=None, usage=None):
        body = {"id": self.id, "object": "chat.completion.chunk", "created": self.created,
                "model": self.model, "system_fingerprint": "fp_llmsim0001",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish, "logprobs": None}]}
        if usage is not None:
            body["choices"], body["usage"] = [], usage
        return ("", body)

    def _open(self):
        if self.started:
            return []
        self.started = True
        return [self._chunk({"role": "assistant", "content": ""})]

    def text(self, piece):
        return self._open() + [self._chunk({"content": piece})]

    def call(self, c):
        index, self.calls = self.calls, self.calls + 1
        return self._open() + [self._chunk({"tool_calls": [{
            "index": index, "id": c["id"], "type": "function",
            "function": {"name": c["name"], "arguments": c["arguments"]}}]})]

    def end(self, e):
        u = e["usage"]
        frames = self._open() + [self._chunk({}, finish=e["reason"])]
        if self.include_usage:
            frames.append(self._chunk(None, usage={
                "prompt_tokens": u["input"], "completion_tokens": u["output"],
                "total_tokens": u["input"] + u["output"],
                "prompt_tokens_details": {"cached_tokens": u["cached"], "audio_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": u["reasoning"], "audio_tokens": 0,
                                              "accepted_prediction_tokens": 0, "rejected_prediction_tokens": 0}}))
        return frames + [("", "[DONE]")]

    def error(self, exc):
        return self._open() + [("", {"error": {"message": exc.message, "type": exc.kind, "code": None}}),
                               ("", "[DONE]")]


class _AnthropicEnc:
    STOP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}

    def __init__(self, model):
        self.model, self.id = model, rand_id("msg_")
        self.started, self.index, self.open_text = False, -1, False

    def _open(self):
        if self.started:
            return []
        self.started = True
        return [("message_start", {"type": "message_start", "message": {
            "id": self.id, "type": "message", "role": "assistant", "model": self.model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            # The input count is not known until the backend finishes; the
            # real figures arrive on message_delta, which current clients read.
            "usage": {"input_tokens": 0, "output_tokens": 0}}}),
            ("ping", {"type": "ping"})]

    def _close_text(self):
        if not self.open_text:
            return []
        self.open_text = False
        return [("content_block_stop", {"type": "content_block_stop", "index": self.index})]

    def text(self, piece):
        frames = self._open()
        if not self.open_text:
            self.index, self.open_text = self.index + 1, True
            frames.append(("content_block_start", {"type": "content_block_start", "index": self.index,
                                                   "content_block": {"type": "text", "text": ""}}))
        return frames + [("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                  "delta": {"type": "text_delta", "text": piece}})]

    def call(self, c):
        frames = self._open() + self._close_text()
        self.index += 1
        return frames + [
            ("content_block_start", {"type": "content_block_start", "index": self.index, "content_block": {
                "type": "tool_use", "id": c["id"], "name": c["name"], "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                     "delta": {"type": "input_json_delta", "partial_json": c["arguments"]}}),
            ("content_block_stop", {"type": "content_block_stop", "index": self.index})]

    def end(self, e):
        u = e["usage"]
        return self._open() + self._close_text() + [
            ("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": self.STOP.get(e["reason"], "end_turn"), "stop_sequence": None},
                               "usage": {"input_tokens": max(0, u["input"] - u["cached"]),
                                         "cache_creation_input_tokens": 0, "cache_read_input_tokens": u["cached"],
                                         "output_tokens": u["output"]}}),
            ("message_stop", {"type": "message_stop"})]

    def error(self, exc):
        return self._open() + [("error", {"type": "error", "error": {"type": exc.kind, "message": exc.message}})]


class _ResponsesEnc:
    def __init__(self, model):
        self.model, self.id, self.created = model, rand_id("resp_"), int(time.time())
        self.started, self.output, self.msg = False, [], None

    def _shell(self, status, output, usage=None, error=None):
        body = responses_body({}, self.model)
        body.update(id=self.id, created_at=self.created, status=status, output=output, error=error,
                    output_text="".join(c.get("text", "") for o in output if o["type"] == "message"
                                        for c in o["content"]))
        if usage is not None:
            body["usage"] = usage
        return body

    def _open(self):
        if self.started:
            return []
        self.started = True
        shell = self._shell("in_progress", [])
        return [("response.created", {"type": "response.created", "response": shell}),
                ("response.in_progress", {"type": "response.in_progress", "response": shell})]

    def _close_msg(self):
        if not self.msg:
            return []
        m, self.msg = self.msg, None
        part = {"type": "output_text", "text": m["text"], "annotations": []}
        item = {"type": "message", "id": m["id"], "role": "assistant", "status": "completed", "content": [part]}
        self.output.append(item)
        return [("response.output_text.done", {"type": "response.output_text.done", "item_id": m["id"],
                                               "output_index": m["index"], "content_index": 0, "text": m["text"]}),
                ("response.content_part.done", {"type": "response.content_part.done", "item_id": m["id"],
                                               "output_index": m["index"], "content_index": 0, "part": part}),
                ("response.output_item.done", {"type": "response.output_item.done",
                                               "output_index": m["index"], "item": item})]

    def text(self, piece):
        frames = self._open()
        if not self.msg:
            self.msg = {"id": rand_id("msg_"), "index": len(self.output), "text": ""}
            frames += [("response.output_item.added", {"type": "response.output_item.added",
                        "output_index": self.msg["index"], "item": {
                            "type": "message", "id": self.msg["id"], "role": "assistant",
                            "status": "in_progress", "content": []}}),
                       ("response.content_part.added", {"type": "response.content_part.added",
                        "item_id": self.msg["id"], "output_index": self.msg["index"], "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []}})]
        self.msg["text"] += piece
        return frames + [("response.output_text.delta", {"type": "response.output_text.delta",
                         "item_id": self.msg["id"], "output_index": self.msg["index"],
                         "content_index": 0, "delta": piece})]

    def call(self, c):
        frames = self._open() + self._close_msg()
        index, item_id = len(self.output), rand_id("fc_")
        item = {"type": "function_call", "id": item_id, "call_id": c["id"], "name": c["name"],
                "arguments": c["arguments"], "status": "completed"}
        self.output.append(item)
        return frames + [
            ("response.output_item.added", {"type": "response.output_item.added", "output_index": index,
                                            "item": dict(item, arguments="", status="in_progress")}),
            ("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta",
                                                        "item_id": item_id, "output_index": index,
                                                        "delta": c["arguments"]}),
            ("response.function_call_arguments.done", {"type": "response.function_call_arguments.done",
                                                       "item_id": item_id, "output_index": index,
                                                       "arguments": c["arguments"]}),
            ("response.output_item.done", {"type": "response.output_item.done",
                                           "output_index": index, "item": item})]

    def end(self, e):
        u = e["usage"]
        frames = self._open() + self._close_msg()
        usage = {"input_tokens": u["input"], "input_tokens_details": {"cached_tokens": u["cached"]},
                 "output_tokens": u["output"], "output_tokens_details": {"reasoning_tokens": u["reasoning"]},
                 "total_tokens": u["input"] + u["output"]}
        return frames + [("response.completed", {"type": "response.completed",
                                                 "response": self._shell("completed", self.output, usage)})]

    def error(self, exc):
        return self._open() + [("response.failed", {"type": "response.failed", "response": self._shell(
            "failed", self.output, error={"code": exc.kind, "message": exc.message})})]


def chatgpt_encoder(shape, model, body):
    if shape == "anthropic":
        return _AnthropicEnc(model)
    if shape == "responses":
        return _ResponsesEnc(model)
    return _ChatEnc(model, bool((body.get("stream_options") or {}).get("include_usage")))


# ── Sessions: record a conversation once, replay it exactly ───────────────
#
# The scripted route answers from rules somebody wrote, and the relay and
# plan routes answer from a real model that says something different each
# time. A session recording is the third thing: capture what a real run of an
# agent was told, once, and from then on answer that same run with exactly
# those words — offline, free, identical every time. It is the cassette idea
# the README has always pointed at, and a cassette here is the `turns` shape
# the script already uses, which is why replay needs no new machinery on the
# way out: a recorded answer is served by the same body and stream builders as
# a scripted one, in whatever wire format the caller speaks.
#
# What is recorded is the ANSWER, not the request. A harness request is
# megabytes of system prompt, tool schemas and file contents, and none of it
# is needed to answer the next identical one.
#
# How a request is matched is the part that was decided by what fails:
#
#   - Hashing the whole request cannot work. Turn three carries turns one and
#     two inside it, tool output included — timestamps, ids, directory
#     listings — so it never repeats, which is why the script advances by
#     counting assistant turns rather than comparing bodies.
#   - So a request is its ASK, the last thing a person said that was not tool
#     output (the same subject() the script is matched on), plus HOW FAR INTO
#     THE CONVERSATION it is, the count of assistant turns already taken.
#   - Harnesses prepend <system-reminder> blocks to the user message, with the
#     date and the like in them. They are not the ask and they change every
#     day, so they are removed before the ask is compared.
#
# That is a lenient match, on purpose: the same ask at the same point gets
# the same answer even when a tool printed something different this time. The
# difference is counted and shown rather than swallowed, and a strict setting
# makes it a miss for the cases where it matters.

CASSETTE_DIR = os.environ.get("LLMSIM_CASSETTES", os.path.join(HERE, "cassettes"))
SESSION_LOCK = threading.RLock()
SESSION = {
    "mode": "off",          # off | record | replay
    "name": None,
    "entries": [],          # what has been captured, or what is loaded
    "index": {},            # replay: key -> entry
    "since": None,
    "hits": 0, "misses": 0, "diverged": 0,
    "unmatched": [],        # requests replay had no answer for, newest first
    "masks": [],            # compiled patterns removed from an ask before it is compared
    "maskSources": [],
    "onMiss": "fail", "strict": False,   # this session's own; see session_start
}
MAX_MASKS, MAX_MASK_LENGTH, MAX_UNMATCHED = 20, 200, 50
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_CASSETTES_SEEN = {}        # path -> (mtime, size, summary), so the console poll does not re-read every file


def _cassette_name_ok(name):
    # A cassette is a file under one directory, named by a person. Anything
    # that could reach outside it — a slash, dots, an absolute path — is not a
    # name.
    return isinstance(name, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name)) and ".." not in name


def _cassette_path(name):
    return os.path.join(CASSETTE_DIR, name + ".json")


def _compile_masks(patterns):
    """(compiled, sources) from a list of regular expressions, or ValueError.

    A mask is text that changes from run to run and is not what was asked:
    Maniple's prompts carry a run id made from the clock, so without one the
    ask a replay sees is never the ask the recording held. Each mask is
    removed from the ask — in the recording and again in the replay — before
    anything is compared. They are bounded and checked here, because they come
    from whoever calls the API.
    """
    if patterns is None:
        return [], []
    if not isinstance(patterns, list) or len(patterns) > MAX_MASKS:
        raise ValueError("masks must be a list of at most %d patterns" % MAX_MASKS)
    compiled, sources = [], []
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_MASK_LENGTH:
            raise ValueError("a mask must be a pattern of 1 to %d characters" % MAX_MASK_LENGTH)
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise ValueError("mask %r is not a valid pattern: %s" % (pattern, exc))
        sources.append(pattern)
    return compiled, sources


def _mask(text):
    for rx in SESSION["masks"]:
        text = rx.sub("<*>", text)
    return text


def _clean(text):
    return _mask(" ".join(_REMINDER.sub(" ", text or "").split()))


def _last_text(messages):
    """The final message in the conversation: what changed since the last turn."""
    if not messages:
        return ""
    last = messages[-1]
    if last.get("role") == "tool":
        content = last.get("content")
        if isinstance(content, list):
            content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
        return _clean(str(content or ""))
    return _clean(flatten(last)[0])


def exchange_key(messages):
    """(ask, turn, last, key) for a request. See the note above for why."""
    ask = _clean(subject(messages))
    turn = sum(1 for m in messages if m.get("role") == "assistant")
    last = _last_text(messages)
    key = hashlib.sha256(("%s\0%d" % (ask, turn)).encode()).hexdigest()[:16]
    return ask, turn, last, key


def _fingerprint(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _recorded_answer(turn):
    """The part of a turn worth keeping, or None when it said nothing."""
    calls = [{"id": c.get("id") or rand_id("call_", 20), "name": c.get("name", ""), "input": c.get("input") or {}}
             for c in turn.get("tool_calls") or []]
    text = turn.get("text") or ""
    if not text and not calls:
        return None
    answer = {"text": text, "tool_calls": calls}
    usage = turn.get("usage")
    if usage:
        answer["usage"] = {"input": usage.get("input", 0), "output": usage.get("output", 0),
                           "cacheRead": usage.get("cacheRead", 0), "reasoning": usage.get("reasoning", 0)}
    return answer


def _flush_session():
    """The recording so far, on disk. Written after every exchange, so a
    session that is killed halfway still leaves everything up to that point."""
    os.makedirs(CASSETTE_DIR, exist_ok=True)
    path = _cassette_path(SESSION["name"])
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"version": 1, "name": SESSION["name"], "recordedAt": SESSION["since"],
                   "masks": SESSION["maskSources"], "entries": SESSION["entries"]}, fh, indent=1)
    os.replace(path + ".tmp", path)


def session_capture(ctx, route, rule, turn, ms):
    """Keep one answered exchange, when a recording is running."""
    if SESSION["mode"] != "record" or not ctx:
        return
    answer = _recorded_answer(turn)
    if not answer:
        return
    ask, n, last, key = exchange_key(ctx["messages"])
    entry = {
        "key": key, "ask": ask, "turn": n, "last": _fingerprint(last), "lastText": last[:160],
        "model": ctx["model"], "provider": ctx["provider"], "shape": ctx["shape"],
        "route": route, "rule": rule, "tools": ctx["declared"], "at": time.time(), "ms": ms, "answer": answer,
    }
    with SESSION_LOCK:
        if SESSION["mode"] != "record":
            return
        # The same ask at the same point, again: a retry, or the same step of a
        # second run. The latest is kept — there is one answer per key, because
        # replay can only give one.
        SESSION["entries"] = [e for e in SESSION["entries"] if e["key"] != key] + [entry]
        _flush_session()


def session_replay(messages):
    """(entry, differs) for a request the recording can answer, else None."""
    ask, n, last, key = exchange_key(messages)
    with SESSION_LOCK:
        entry = SESSION["index"].get(key)
        differs = bool(entry) and _fingerprint(last) != entry["last"]
        if entry and differs and SESSION["strict"]:
            entry = None
            why = "the last message differs from the recording"
        else:
            why = "nothing was recorded for it"
        if not entry:
            SESSION["misses"] += 1
            SESSION["unmatched"] = ([{"ask": ask[:160], "turn": n, "why": why}] + SESSION["unmatched"])[:MAX_UNMATCHED]
            return None
        SESSION["hits"] += 1
        SESSION["diverged"] += 1 if differs else 0
        return entry, differs


def turn_from_entry(entry):
    """A recorded answer as the {text, tool_calls} turn every builder takes,
    with the numbers the original reported rather than invented ones."""
    answer = entry["answer"]
    turn = {}
    if answer.get("text"):
        turn["text"] = answer["text"]
    if answer.get("tool_calls"):
        turn["tool_calls"] = [dict(c) for c in answer["tool_calls"]]
    usage = answer.get("usage")
    if usage:
        turn.update(input_tokens=usage["input"], output_tokens=usage["output"],
                    cached_tokens=usage["cacheRead"], reasoning_tokens=usage.get("reasoning", 0))
        turn["usage"] = dict(usage, model=entry["model"])
    return turn


def _normalize_entry(entry):
    """One cassette entry, made complete and safe to use. ValueError if it is not one.

    A cassette does not only come from this recorder: it can be imported from
    another machine or written by hand, and carry only the two things replay
    needs — what was asked, and the answer. Everything else is filled in, so
    nothing downstream has to wonder whether a field is there.

    The key is worked out here from the ask and the turn and never read from
    the file. It decides which request is given which answer, so a file that
    can set it can answer any request it likes.
    """
    if not isinstance(entry, dict):
        raise ValueError("an entry is not an object")
    answer = entry.get("answer")
    if not isinstance(answer, dict):
        raise ValueError("an entry has no answer")
    calls = [{"id": str(c.get("id") or rand_id("call_", 20)), "name": str(c.get("name", "")),
              "input": c.get("input") if isinstance(c.get("input"), dict) else {}}
             for c in answer.get("tool_calls") or [] if isinstance(c, dict)]
    text = str(answer.get("text") or "")
    if not text and not calls:
        raise ValueError("an entry says nothing")
    try:
        n = int(entry.get("turn", 0))
    except (TypeError, ValueError):
        raise ValueError("an entry's turn is not a number")
    ask = " ".join(str(entry.get("ask", "")).split())
    usage = answer.get("usage")
    out = {
        "key": hashlib.sha256(("%s\0%d" % (ask, n)).encode()).hexdigest()[:16],
        "ask": ask, "turn": n, "last": str(entry.get("last", "")), "lastText": str(entry.get("lastText", ""))[:160],
        "model": str(entry.get("model", "")), "provider": str(entry.get("provider", "")), "shape": str(entry.get("shape", "")),
        "route": str(entry.get("route", "")), "rule": str(entry.get("rule", "")),
        "tools": [str(t) for t in entry.get("tools") or []], "at": entry.get("at"), "ms": entry.get("ms"),
        "answer": {"text": text, "tool_calls": calls},
    }
    if isinstance(usage, dict):
        out["answer"]["usage"] = {k: int(usage.get(k) or 0) for k in ("input", "output", "cacheRead", "reasoning")}
    return out


def _read_cassette(name):
    with open(_cassette_path(name)) as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError("that file is not a cassette")
    data["entries"] = [_normalize_entry(e) for e in data["entries"]]
    _compile_masks(data.get("masks"))        # a cassette with a bad mask is refused here, not at the first request
    data["masks"] = list(data.get("masks") or [])
    return data


def list_cassettes():
    out = []
    try:
        files = sorted(os.listdir(CASSETTE_DIR))
    except FileNotFoundError:
        return out
    for fname in files:
        if not fname.endswith(".json"):
            continue
        name, path = fname[:-5], os.path.join(CASSETTE_DIR, fname)
        if not _cassette_name_ok(name):
            continue
        try:
            stat = os.stat(path)
            seen = _CASSETTES_SEEN.get(path)
            if not seen or seen[:2] != (stat.st_mtime, stat.st_size):
                data = _read_cassette(name)
                seen = (stat.st_mtime, stat.st_size, {
                    "name": name, "entries": len(data["entries"]), "recordedAt": data.get("recordedAt"),
                    "bytes": stat.st_size, "masks": data["masks"],
                    "models": sorted({e.get("model") for e in data["entries"] if e.get("model")})})
                _CASSETTES_SEEN[path] = seen
            out.append(seen[2])
        except Exception:
            out.append({"name": name, "entries": 0, "recordedAt": None, "bytes": 0, "models": [], "masks": [], "broken": True})
    return out


def session_status():
    with SESSION_LOCK:
        recent = [{"ask": e.get("ask", "")[:140], "turn": e.get("turn", 0), "route": e.get("route", ""),
                   "rule": e.get("rule", ""), "model": e.get("model", ""),
                   "tools": [c.get("name", "") for c in e.get("answer", {}).get("tool_calls", [])]}
                  for e in SESSION["entries"][-25:]][::-1]
        return {
            "mode": SESSION["mode"], "name": SESSION["name"], "since": SESSION["since"],
            "entries": len(SESSION["entries"]), "recent": recent,
            "hits": SESSION["hits"], "misses": SESSION["misses"], "diverged": SESSION["diverged"],
            "unmatched": SESSION["unmatched"][:10], "masks": SESSION["maskSources"],
            "onMiss": SESSION["onMiss"], "strict": SESSION["strict"],
            "settings": CONFIG["session"], "cassettes": list_cassettes(),
        }


# Every lifecycle call answers (ok, message, http status). The status is what a
# caller branches on — 400 it asked wrongly, 404 there is no such cassette, 409
# the state forbids it — so a test harness need not parse a sentence to know.

def _no(code, message):
    return False, message, code


def session_start(mode, name, masks=None, on_miss=None, strict=None, overwrite=False):
    """Begin recording or replaying. One mode at a time, and never over somebody's
    recording unless they said to.

    on_miss and strict belong to THIS replay. They default to the console's
    settings but are not those settings: a test that wants strictness for one
    run must not be able to change what the next run, or the console, does.
    """
    if not _cassette_name_ok(name):
        return _no(400, "A cassette name is letters, digits, dot, dash and underscore, and starts with a letter or digit.")
    if on_miss not in (None, "fail", "route"):
        return _no(400, "onMiss must be fail or route.")
    if strict is not None and not isinstance(strict, bool):
        return _no(400, "strict must be true or false.")
    try:
        compiled, sources = _compile_masks(masks)
    except ValueError as exc:
        return _no(400, str(exc))
    with SESSION_LOCK:
        if SESSION["mode"] != "off":
            return _no(409, "A %s is already running (%s). Stop it first." % (
                "recording" if SESSION["mode"] == "record" else "replay", SESSION["name"]))
        exists = os.path.exists(_cassette_path(name))
        if mode == "record":
            if exists and not overwrite:
                return _no(409, "A cassette called %s already exists. Pick another name, delete it, or record with overwrite." % name)
            SESSION.update(mode="record", name=name, entries=[], index={}, since=time.time(), hits=0, misses=0,
                           diverged=0, unmatched=[], masks=compiled, maskSources=sources, onMiss="fail", strict=False)
            _flush_session()               # the file exists from the first moment, even if nothing is said
            log("session: recording to %s%s" % (_cassette_path(name), " (replacing it)" if exists else ""))
            return True, "Recording to %s. Everything answered from now on is kept." % name, 200
        if not exists:
            return _no(404, "There is no cassette called %s." % name)
        try:
            data = _read_cassette(name)
            # The recording's own masks, then any the caller adds: the recording
            # was keyed on what its masks left, so a replay must leave the same.
            joined = list(dict.fromkeys(data["masks"] + sources))
            compiled, joined = _compile_masks(joined)
        except Exception as exc:
            return _no(409, "Could not read %s: %s" % (name, exc))
        SESSION.update(mode="replay", name=name, entries=data["entries"], since=time.time(), hits=0, misses=0,
                       diverged=0, unmatched=[], masks=compiled, maskSources=joined,
                       onMiss=on_miss or CONFIG["session"]["onMiss"],
                       strict=CONFIG["session"]["strict"] if strict is None else strict)
        # Keyed under THIS session's masks, not trusted from the file: the key
        # decides which request gets which answer.
        SESSION["index"] = {hashlib.sha256(("%s\0%d" % (_mask(e["ask"]), e["turn"])).encode()).hexdigest()[:16]: e
                            for e in data["entries"]}
        log("session: replaying %s (%d exchange(s), on miss: %s%s)" % (
            name, len(data["entries"]), SESSION["onMiss"], ", strict" if SESSION["strict"] else ""))
        return True, "Replaying %s: %d recorded exchange(s)." % (name, len(data["entries"])), 200


def session_auto(name, masks=None, on_miss=None, strict=None):
    """Replay the cassette if there is one, else record it: the first run of a
    test makes the recording and every run after it uses it."""
    if not _cassette_name_ok(name):
        return _no(400, "A cassette name is letters, digits, dot, dash and underscore, and starts with a letter or digit.")
    mode = "replay" if os.path.exists(_cassette_path(name)) else "record"
    ok, message, code = session_start(mode, name, masks=masks, on_miss=on_miss, strict=strict)
    return ok, message, code


def session_stop():
    """(ok, message, code, summary). Everything a harness needs to assert on."""
    with SESSION_LOCK:
        if SESSION["mode"] == "off":
            return False, "Nothing is running.", 409, None
        was, name = SESSION["mode"], SESSION["name"]
        summary = {"was": was, "name": name, "entries": len(SESSION["entries"]),
                   "hits": SESSION["hits"], "misses": SESSION["misses"], "diverged": SESSION["diverged"],
                   "unmatched": list(SESSION["unmatched"]), "masks": list(SESSION["maskSources"]),
                   "onMiss": SESSION["onMiss"], "strict": SESSION["strict"]}
        SESSION.update(mode="off", name=None, entries=[], index={}, since=None, masks=[], maskSources=[])
    log("session: %s of %s stopped" % ("recording" if was == "record" else "replay", name))
    message = ("Saved %d exchange(s) to %s." % (summary["entries"], name) if was == "record"
               else "Stopped replaying %s: %d answered, %d with no recording." % (name, summary["hits"], summary["misses"]))
    return True, message, 200, summary


def session_delete(name):
    if not _cassette_name_ok(name):
        return _no(400, "That is not a cassette name.")
    with SESSION_LOCK:
        if SESSION["name"] == name and SESSION["mode"] != "off":
            return _no(409, "%s is in use. Stop it first." % name)
        try:
            os.remove(_cassette_path(name))
        except FileNotFoundError:
            return _no(404, "There is no cassette called %s." % name)
    return True, "Deleted %s." % name, 200


def session_import(name, data, overwrite=False):
    """A cassette somebody else recorded, checked before it is trusted."""
    if not _cassette_name_ok(name):
        return _no(400, "A cassette name is letters, digits, dot, dash and underscore.")
    with SESSION_LOCK:
        if SESSION["name"] == name and SESSION["mode"] != "off":
            return _no(409, "%s is in use. Stop it first." % name)
        exists = os.path.exists(_cassette_path(name))
        if exists and not overwrite:
            return _no(409, "A cassette called %s already exists. Pick another name, delete it, or import with overwrite." % name)
        try:
            if not isinstance(data, dict) or not isinstance(data.get("entries"), list) or not data["entries"]:
                raise ValueError("it holds no recorded exchanges")
            clean = [_normalize_entry(e) for e in data["entries"]]
            _compile_masks(data.get("masks"))
        except (ValueError, TypeError) as exc:
            return _no(400, "That is not a usable cassette: %s." % exc)
        os.makedirs(CASSETTE_DIR, exist_ok=True)
        path = _cassette_path(name)
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"version": 1, "name": name, "recordedAt": data.get("recordedAt"),
                       "masks": list(data.get("masks") or []), "entries": clean}, fh, indent=1)
        os.replace(path + ".tmp", path)
    return True, "%s %s: %d exchange(s)." % ("Replaced" if exists else "Imported", name, len(clean)), 200


# ── HTTP ─────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "LLMSim/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # logged in one line at the end of handle_chat instead

    # -- helpers --


    # ── The console ──────────────────────────────────────────────────────
    #
    # Served by this process, from one file, with no build step and no
    # dependency — the same promise the rest of this makes.

    def serve_ui(self):
        try:
            with open(os.path.join(HERE, "ui.html"), "rb") as fh:
                page = fh.read()
        except FileNotFoundError:
            return self.send_json(500, {"error": "ui.html is missing from the image"})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        # Never cached: the console is read while settings are being changed,
        # and a stale one shows a routing table that is no longer true.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(page)

    def ui_state(self, since):
        with REQUESTS_LOCK:
            rows = [r for r in REQUESTS if r["n"] > since]
        known = sorted({m for models in MODELS.values() for m in models})
        return {
            "config": public_config(),
            "models": {p: MODELS[p] for p in sorted(MODELS)},
            "allModels": known,
            "rules": [r.get("name") for r in SCRIPT.get("rules", [])],
            "runs": RESULTS["runs"][:5],
            "chatgpt": chatgpt_status(),
            "session": session_status(),
            "requests": rows,
            "seq": REQUEST_SEQ_PEEK(),
        }



    def run_tests(self, body):
        """Run named cases, or a whole plan, and report the way a suite does.

        Synchronous on purpose. A step can depend on the one before it, and a
        caller watching a run wants the answer now rather than a job id to
        poll — this is a test harness on a developer's machine, not a queue.
        """
        cases = {c.get("id"): c for c in CONFIG.get("tests", [])}
        plan_id = body.get("planId")
        if plan_id:
            plan = next((p for p in CONFIG.get("plans", []) if p.get("id") == plan_id), None)
            if not plan:
                return self.send_json(404, {"error": "no plan %r" % plan_id})
            wanted, label = plan.get("testIds") or [], plan.get("name") or plan_id
        else:
            wanted = body.get("testIds")
            if wanted is None:
                wanted, label = list(cases), "All test cases"
            else:
                label = body.get("label") or ("%d case(s)" % len(wanted))

        chosen = [cases[i] for i in wanted if i in cases]
        if not chosen:
            return self.send_json(400, {"error": "none of those test cases exist"})

        log("running %d case(s): %s" % (len(chosen), label))
        run = run_cases(chosen, label)
        log("  %d passed, %d failed in %dms" % (run["passed"], run["failed"], run["ms"]))
        return self.send_json(200, run)

    def import_suite(self, body):
        """Take a suite someone else wrote, and say what it did.

        Merge by default rather than replace: the common case is picking up a
        colleague's cases, and losing your own to that is a bad surprise. An
        id that already exists is overwritten, because re-importing a suite
        you edited should update it rather than double it.
        """
        mode = body.get("mode") or "merge"
        if mode not in ("merge", "replace"):
            return self.send_json(400, {"error": "mode must be merge or replace"})

        incoming = {}
        for key in SUITE_KEYS:
            rows = body.get(key)
            if rows is None:
                rows = []
            if not isinstance(rows, list):
                return self.send_json(400, {"error": "%s must be a list" % key})
            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    return self.send_json(400, {"error": "every %s entry needs an id" % key[:-1]})
            incoming[key] = rows
        if not incoming["tests"] and not incoming["plans"]:
            return self.send_json(400, {"error": "that file holds no cases or plans"})

        added, updated = 0, 0
        for key in SUITE_KEYS:
            if mode == "replace":
                CONFIG[key] = incoming[key]
                continue
            existing = {r.get("id"): i for i, r in enumerate(CONFIG.get(key, []))}
            merged = list(CONFIG.get(key, []))
            for row in incoming[key]:
                if row["id"] in existing:
                    merged[existing[row["id"]]] = row
                    updated += 1
                else:
                    merged.append(row)
                    added += 1
            CONFIG[key] = merged

        save_config()
        log("suite imported (%s): %d case(s), %d plan(s)"
            % (mode, len(incoming["tests"]), len(incoming["plans"])))
        return self.send_json(200, {
            "ok": True, "mode": mode, "added": added, "updated": updated,
            "tests": len(CONFIG.get("tests", [])), "plans": len(CONFIG.get("plans", [])),
        })

    def save_ui_config(self, body):
        """Accept only the settings that exist, and say what changed.

        Validated rather than merged blindly: a routing table is the one
        thing here that can send traffic to a paid endpoint, and a typo that
        silently becomes a new key would be discovered on an invoice.
        """
        routing = body.get("routing") or {}
        upstream = body.get("upstream") or {}

        default = routing.get("default")
        if default is not None:
            if default not in ("scripted", "relay", "chatgpt"):
                return self.send_json(400, {"error": "routing.default must be scripted, relay or chatgpt"})
            CONFIG["routing"]["default"] = default

        if "models" in routing:
            table = {}
            for model, choice in (routing["models"] or {}).items():
                if choice not in ("scripted", "relay", "chatgpt"):
                    return self.send_json(400, {"error": "%s: must be scripted, relay or chatgpt" % model})
                # "same as the default" is not an override; storing it would
                # pin the model the next time the default changes.
                if choice != CONFIG["routing"]["default"]:
                    table[model] = choice
            CONFIG["routing"]["models"] = table

        if "responses" in body:
            table = {}
            for model, spec in (body["responses"] or {}).items():
                if not isinstance(spec, dict):
                    return self.send_json(400, {"error": "%s: response must be an object" % model})
                entry = {}
                if spec.get("text"):
                    entry["text"] = str(spec["text"])[:4000]
                if spec.get("status"):
                    try:
                        status = int(spec["status"])
                    except (TypeError, ValueError):
                        return self.send_json(400, {"error": "%s: status must be a number" % model})
                    if not 200 <= status <= 599:
                        return self.send_json(400, {"error": "%s: status out of range" % model})
                    entry["status"] = status
                if spec.get("delay_ms"):
                    try:
                        entry["delay_ms"] = max(0, min(60000, int(spec["delay_ms"])))
                    except (TypeError, ValueError):
                        return self.send_json(400, {"error": "%s: delay must be a number" % model})
                # An empty entry is a removal, not a model that answers with
                # nothing — otherwise clearing a field would leave a rule
                # behind that silently answers "(no answer set)".
                if entry:
                    table[model] = entry
            CONFIG["responses"] = table

        for key in ("tests", "plans"):
            if key not in body:
                continue
            rows = body[key]
            if not isinstance(rows, list):
                return self.send_json(400, {"error": "%s must be a list" % key})
            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    return self.send_json(400, {"error": "every %s entry needs an id" % key[:-1]})
            CONFIG[key] = rows

        if "fidelity" in body:
            for k, v in (body["fidelity"] or {}).items():
                if k in CONFIG["fidelity"]:
                    CONFIG["fidelity"][k] = bool(v)

        plan = body.get("chatgpt") or {}
        for field, limit in (("model", 100), ("instructions", 2000)):
            if field in plan:
                value = str(plan[field] or "").strip()[:limit]
                if field == "model" and not value:
                    return self.send_json(400, {"error": "chatgpt.model cannot be empty"})
                CONFIG["chatgpt"][field] = value or CONFIG["chatgpt"][field]
        if "fallback" in plan:
            CONFIG["chatgpt"]["fallback"] = bool(plan["fallback"])

        for field in ("url", "key", "model", "format", "fallback"):
            if field not in upstream:
                continue
            value = upstream[field]
            if field == "format" and value not in ("openai", "anthropic"):
                return self.send_json(400, {"error": "upstream.format must be openai or anthropic"})
            if field == "fallback":
                value = bool(value)
            if field == "url":
                value = str(value).rstrip("/")
            # An empty key means "leave the stored one alone", so a person
            # editing the model name does not have to retype the secret.
            if field == "key" and not value:
                continue
            CONFIG["upstream"][field] = value

        save_config()
        log("config saved: default=%s routed=%d answers=%d upstream=%s"
            % (CONFIG["routing"]["default"], len(CONFIG["routing"]["models"]),
               len(CONFIG.get("responses") or {}), CONFIG["upstream"]["url"] or "none"))
        return self.send_json(200, {"ok": True, "config": public_config()})

    def vendor_headers(self, status=200):
        """The headers a real answer carries, which clients read.

        A client that logs a request id, honours retry-after, or backs off as
        a rate-limit budget runs down cannot be tested against a response
        that has none of them — and "we return a 429" is not the same claim
        as "we return a 429 a client can recover from".

        The numbers walk downward across a process's life rather than being
        fixed, so a caller watching the budget sees it move.
        """
        # Not a vendor header, so never sent to a vendor's client: only the
        # console's Chat page asks for it, to say what really answered.
        if getattr(self, "show_trace", False):
            for key, value in self.trace.items():
                self.send_header("X-LLMSim-" + key, str(value))
        if not CONFIG.get("fidelity", {}).get("headers", True):
            return
        rid = rand_id("req_", 24)
        remaining = max(0, 1000 - (next(REQUEST_SEQ_PEEK_COUNTER) % 1000))
        reset = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60))
        if self.provider == "anthropic":
            self.send_header("request-id", rid)
            self.send_header("anthropic-ratelimit-requests-limit", "1000")
            self.send_header("anthropic-ratelimit-requests-remaining", str(remaining))
            self.send_header("anthropic-ratelimit-requests-reset", reset)
        else:
            self.send_header("x-request-id", rid)
            self.send_header("x-ratelimit-limit-requests", "1000")
            self.send_header("x-ratelimit-remaining-requests", str(remaining))
            self.send_header("x-ratelimit-reset-requests", "60s")
        # The one a client actually acts on. Without it a 429 is a dead end:
        # nothing says when to try again, so every backoff is a guess.
        if status == 429:
            self.send_header("retry-after", "2")
        elif status == 529 or status == 503:
            self.send_header("retry-after", "5")

    def send_json(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.vendor_headers(status)
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

        if path == "/ui" or path == "/ui/":
            return self.serve_ui()

        if path == "/api/ui/state":
            since = 0
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            for part in query.split("&"):
                if part.startswith("since="):
                    try:
                        since = int(part[6:])
                    except ValueError:
                        since = 0
            return self.send_json(200, self.ui_state(since))

        if path == "/api/ui/chatgpt":
            return self.send_json(200, chatgpt_status())

        if path == "/api/sessions" or path.startswith("/api/sessions/"):
            return self.session_api("GET", path, None, urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))

        if path == "/api/ui/session":
            return self.send_json(200, session_status())

        if path.startswith("/api/ui/session/"):
            return self.session_download(path[len("/api/ui/session/"):])

        if path == "/api/ui/chatgpt/models":
            try:
                return self.send_json(200, {"models": chatgpt_models()})
            except ChatGPTError as exc:
                return self.send_json(exc.status, {"error": exc.message})

        if path == "/api/ui/suite":
            # Served as a download so a suite can leave this machine at all.
            payload = json.dumps({
                "version": 1,
                "_": "LLMSim test cases and plans. Commit this; llmsim.config.json is machine-local.",
                "tests": CONFIG.get("tests", []),
                "plans": CONFIG.get("plans", []),
            }, indent=2).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-disposition", 'attachment; filename="llmsim.tests.json"')
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            return self.wfile.write(payload)

        if path in ("/", "/healthz"):
            return self.send_json(200, {
                "ok": True, "service": "llmsim",
                "providers": sorted(API_KEYS),
                "mode": CONFIG["routing"]["default"],
                "routed": len(CONFIG["routing"]["models"]),
                "upstream": ({"model": CONFIG["upstream"]["model"],
                              "format": CONFIG["upstream"]["format"],
                              "fallback": CONFIG["upstream"]["fallback"]} if relaying() else None),
                "chatgpt": {"signedIn": chatgpt_status()["signedIn"], "model": CONFIG["chatgpt"]["model"]},
                "session": {"mode": SESSION["mode"], "name": SESSION["name"]},
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

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return None

    def do_PUT(self):
        self.show_trace, self.trace = False, {}
        prefix, path = self.split_path()
        self.provider = self.resolve_provider(prefix, self.presented_key()) or "openai"
        body = self._json_body()
        if not path.startswith("/api/sessions/"):
            return self.send_json(404, {"error": "No such route: PUT " + path})
        if body is None:
            return self.send_json(400, {"ok": False, "message": "The body is not valid JSON."})
        return self.session_api("PUT", path, body, urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))

    def do_DELETE(self):
        self.show_trace, self.trace = False, {}
        prefix, path = self.split_path()
        self.provider = self.resolve_provider(prefix, self.presented_key()) or "openai"
        self._json_body()                       # a body, if one was sent, is read and ignored
        if not path.startswith("/api/sessions/"):
            return self.send_json(404, {"error": "No such route: DELETE " + path})
        return self.session_api("DELETE", path, None, {})

    def _is_internal(self):
        # Only from this machine: the marker exists for the runner calling
        # itself, and honouring it from elsewhere would let a caller step round
        # a recording or a route by asking nicely.
        return (self.headers.get("X-LLMSim-Internal") == "1"
                and self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1"))

    def do_POST(self):
        self.show_trace, self.trace = False, {}
        self.internal = self._is_internal()
        prefix, path = self.split_path()
        key = self.presented_key()
        self.provider = self.resolve_provider(prefix, key) or "openai"

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self.refuse(400, "invalid_request_error", "Request body is not valid JSON.")

        # The console writes settings here. Checked before the vendor key
        # below, because this route is not a vendor route and has no API key.
        if path == "/api/ui/config":
            return self.save_ui_config(body)

        if path == "/api/ui/run":
            return self.run_tests(body)

        if path == "/api/ui/suite":
            return self.import_suite(body)

        if path.startswith("/api/ui/chatgpt/"):
            return self.chatgpt_console(path, body)

        if path == "/api/ui/chat":
            return self.console_chat(body)

        if path.startswith("/api/sessions"):
            return self.session_api("POST", path, body, urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))

        if path == "/api/ui/session":
            return self.session_console(body)

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




    def foreign_origin(self):
        """True when a browser says the request came from some other site.

        A caller that sends no Origin — curl, a script — is not a browser and
        is let through, which is also how every test here talks to it.
        """
        origin = self.headers.get("Origin")
        return bool(origin) and urllib.parse.urlsplit(origin).netloc != (self.headers.get("Host") or "")

    def console_chat(self, body):
        """One chat turn from the console's Chat page, through the real path.

        Not a shortcut into the internals: the request is shaped like the one
        the model's own vendor would take and handed to handle_chat, so routing,
        validation, the script, relay, the ChatGPT plan and the Logs page all
        behave exactly as they do for any client. Which provider and which wire
        format is decided here, from the model, so the page does not need the
        keys or the formats.
        """
        if self.foreign_origin():
            return self.send_json(403, {"error": "That request came from another site."})
        model, turns = body.get("model"), body.get("messages")
        provider = next((p for p, names in MODELS.items() if model in names), None)
        if not provider:
            return self.send_json(404, {"error": "No such model: %r" % (model,)})
        if not isinstance(turns, list) or not turns:
            return self.send_json(400, {"error": "Say something first."})

        self.provider = provider
        self.show_trace = True        # the page shows what answered, so it is told
        shape = "anthropic" if provider == "anthropic" else "openai"
        request = {"model": model, "stream": bool(body.get("stream", True)),
                   "messages": [{"role": m.get("role"), "content": str(m.get("content") or "")} for m in turns]}
        system = str(body.get("system") or "").strip()
        if system and shape == "anthropic":
            request["system"] = system
        elif system:
            request["messages"].insert(0, {"role": "system", "content": system})
        if shape == "anthropic":
            request["max_tokens"] = 1024         # required by this vendor and optional elsewhere
        elif request["stream"]:
            request["stream_options"] = {"include_usage": True}    # else a streamed answer reports no tokens
        if isinstance(body.get("tools"), list) and body["tools"]:
            request["tools"] = body["tools"]
        return self.handle_chat(request, shape)

    def session_download(self, name):
        """A cassette as a file, so it can leave this machine."""
        if not _cassette_name_ok(name) or not os.path.exists(_cassette_path(name)):
            return self.send_json(404, {"error": "There is no cassette called %s." % name})
        with open(_cassette_path(name), "rb") as fh:
            raw = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Disposition", 'attachment; filename="%s.json"' % name)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _session_reply(self, ok, message, code, **extra):
        return self.send_json(code, dict({"ok": ok, "message": message, "status": session_status()}, **extra))

    def session_console(self, body):
        """The console's route, one action per call. See session_api for the one
        a test harness uses; both end in the same functions.

        Refused from another site for the reason the other console routes are:
        a replay changes what every request is answered with.
        """
        if self.foreign_origin():
            return self.send_json(403, {"error": "That request came from another site."})
        action, name = body.get("action"), body.get("name")
        if action in ("record", "replay"):
            return self._session_reply(*session_start(action, name, masks=body.get("masks"), on_miss=body.get("onMiss"),
                                                      strict=body.get("strict"), overwrite=bool(body.get("overwrite"))))
        if action == "auto":
            return self._session_reply(*session_auto(name, masks=body.get("masks"), on_miss=body.get("onMiss"),
                                                     strict=body.get("strict")))
        if action == "stop":
            ok, message, code, summary = session_stop()
            return self._session_reply(ok, message, code, summary=summary)
        if action == "delete":
            return self._session_reply(*session_delete(name))
        if action == "import":
            return self._session_reply(*session_import(name, body.get("cassette"), overwrite=bool(body.get("overwrite"))))
        if action == "settings":
            on_miss, strict = body.get("onMiss", CONFIG["session"]["onMiss"]), body.get("strict", CONFIG["session"]["strict"])
            if on_miss not in ("fail", "route") or not isinstance(strict, bool):
                return self._session_reply(False, "onMiss must be fail or route, and strict true or false.", 400)
            CONFIG["session"] = {"onMiss": on_miss, "strict": strict}
            save_config()
            if SESSION["mode"] == "replay":          # the console's controls change the replay in front of it
                SESSION["onMiss"], SESSION["strict"] = on_miss, strict
            return self._session_reply(True, "Saved.", 200)
        return self._session_reply(False, "Unknown action.", 400)

    def session_api(self, method, path, body, query):
        """The API a test harness drives. Plain REST, so Maniple's suite needs
        nothing but an HTTP client:

            GET    /api/sessions                status, and every cassette
            POST   /api/sessions/record         {name, masks?, overwrite?}
            POST   /api/sessions/replay         {name, masks?, onMiss?, strict?}
            POST   /api/sessions/auto           {name, masks?, onMiss?, strict?}  replay if it exists, else record
            POST   /api/sessions/stop           -> summary: entries, hits, misses, diverged, unmatched
            GET    /api/sessions/<name>         the cassette, as a file
            PUT    /api/sessions/<name>         upload one (the same JSON); ?overwrite=1 replaces
            DELETE /api/sessions/<name>

        The cassette is a file the caller can keep wherever it likes — in its
        own repository, say — and PUT back before a replay: LLMSim's own
        directory is machine-local, and a test should not depend on what an
        earlier run left on this one.
        """
        if self.foreign_origin():
            return self.send_json(403, {"error": "That request came from another site."})
        rest = path[len("/api/sessions"):].strip("/")
        if method == "GET" and not rest:
            return self.send_json(200, session_status())
        if method == "GET":
            return self.session_download(rest)
        if method == "DELETE" and rest:
            return self._session_reply(*session_delete(rest))
        if method == "PUT" and rest:
            overwrite = (query.get("overwrite") or [""])[0].lower() in ("1", "true", "yes")
            return self._session_reply(*session_import(rest, body, overwrite=overwrite))
        if method == "POST" and rest in ("record", "replay"):
            return self._session_reply(*session_start(rest, body.get("name"), masks=body.get("masks"), on_miss=body.get("onMiss"),
                                                      strict=body.get("strict"), overwrite=bool(body.get("overwrite"))))
        if method == "POST" and rest == "auto":
            return self._session_reply(*session_auto(body.get("name"), masks=body.get("masks"),
                                                     on_miss=body.get("onMiss"), strict=body.get("strict")))
        if method == "POST" and rest == "stop":
            ok, message, code, summary = session_stop()
            return self._session_reply(ok, message, code, summary=summary)
        return self.send_json(404, {"ok": False, "message": "No such route: %s %s" % (method, path)})

    def chatgpt_console(self, path, body):
        """Sign in, finish signing in, sign out.

        A page on any other site can make a browser POST to this port, and
        text/plain is enough to get past the browser's own checks because this
        server reads the body as JSON whatever its type. The routes below hold
        the keys to somebody's subscription, so a request that declares an
        origin other than this server's own is refused. A caller that sends no
        Origin — curl, a script — is not a browser and is let through.
        """
        if self.foreign_origin():
            return self.send_json(403, {"error": "That request came from another site."})

        if path == "/api/ui/chatgpt/login":
            return self.send_json(200, chatgpt_begin_login())
        if path == "/api/ui/chatgpt/complete":
            ok, message = chatgpt_complete_by_paste(body.get("redirect"))
            return self.send_json(200 if ok else 400, {"ok": ok, "message": message, "status": chatgpt_status()})
        if path == "/api/ui/chatgpt/logout":
            return self.send_json(200, dict(chatgpt_logout(), status=chatgpt_status()))
        return self.send_json(404, {"error": "No such route: " + path})

    def begin_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.vendor_headers(200)
        self.end_headers()
        self.close_connection = True

    def write_event(self, event, data):
        """One SSE frame, in the form this shape uses.

        Anthropic and Responses name their events; OpenAI does not, and sends
        a bare [DONE] sentinel at the end — so an empty name means "data
        only", and a string payload is written verbatim.
        """
        try:
            if isinstance(data, str):
                self.wfile.write(("data: %s\n\n" % data).encode())
            elif event:
                self.wfile.write(("event: %s\ndata: %s\n\n" % (event, json.dumps(data))).encode())
            else:
                self.wfile.write(("data: %s\n\n" % json.dumps(data)).encode())
            self.wfile.flush()
        except BrokenPipeError:
            log("client closed the stream early")
            raise

    def relay_stream(self, shape, model, messages, tools_body, entry_started):
        """Forward the upstream's own stream, re-framed for this client.

        Two streams, not one: the upstream speaks its format and the client
        asked for theirs, so every delta is unwrapped and rewritten. What is
        preserved is the timing — the pauses are the model's, which is the
        only part of a streamed demo that cannot be faked convincingly.

        On a failure before the first byte this falls back to the buffered
        path, which can still answer with a proper error. After the first
        byte it cannot: the status is already sent, so the stream is ended
        and the reason goes to the log.
        """
        up = CONFIG["upstream"]
        if up["format"] == "anthropic":
            url, headers = up["url"] + "/v1/messages", {"x-api-key": up["key"], "anthropic-version": "2023-06-01"}
            payload = {"model": up["model"], "max_tokens": 2048, "stream": True,
                       "messages": [m for m in messages if m.get("role") in ("user", "assistant")]}
        else:
            url, headers = up["url"] + "/v1/chat/completions", {"Authorization": "Bearer " + up["key"]}
            payload = {"model": up["model"], "messages": messages, "stream": True}
        if tools_body:
            payload["tools"] = tools_body
        headers["content-type"] = "application/json"

        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=180)
        except Exception as exc:
            log("relay stream failed before it began: %s" % exc)
            if CONFIG["upstream"]["fallback"]:
                name, turn = choose_turn(self.provider, model, messages, None)
                self.begin_stream()
                for ev in _stream_open(shape, model):
                    self.write_event(ev[0], ev[1])
                for ev in _stream_delta(shape, turn.get("text") or ""):
                    self.write_event(ev[0], ev[1])
                for ev in _stream_close(shape, turn.get("text") or ""):
                    self.write_event(ev[0], ev[1])
                return None
            return self.refuse(502, "upstream_error",
                               "LLMSim could not reach its upstream model: %s" % exc)

        started, sent = False, []
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk in ("", "[DONE]"):
                    continue
                try:
                    event = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                piece = _delta_text(event)
                if not piece:
                    continue
                if not started:
                    self.begin_stream()
                    for head in _stream_open(shape, model):
                        self.write_event(head[0], head[1])
                    started = True
                sent.append(piece)
                for ev in _stream_delta(shape, piece):
                    self.write_event(ev[0], ev[1])
            if not started:
                # The upstream answered with nothing streamable. Emit an
                # empty-but-well-formed stream rather than nothing, so the
                # client sees a completed response instead of a dropped
                # connection it has to guess about.
                self.begin_stream()
                for ev in _stream_open(shape, model):
                    self.write_event(ev[0], ev[1])
                for ev in _stream_close(shape, ""):
                    self.write_event(ev[0], ev[1])
                return None
            for ev in _stream_close(shape, "".join(sent)):
                self.write_event(ev[0], ev[1])
            session_capture(self.ctx, "relay", "relay:" + CONFIG["upstream"]["model"],
                            {"text": "".join(sent)}, int((time.time() - entry_started) * 1000))
        except Exception as exc:
            log("relay stream broke mid-flight: %s" % exc)
        finally:
            try:
                resp.close()
            except Exception:
                pass
        return None

    def chatgpt_serve(self, resp, shape, model, stream, body, messages, started):
        """Answer from an open ChatGPT stream, in the shape the client asked.

        Whole when the client did not ask for a stream, and otherwise piece by
        piece as the plan produces it. In both the failure that matters is the
        one after the first byte, when the status is already sent: it is
        reported inside the stream in the client's own error form, since
        nothing else is left to say it with.
        """
        cg = CONFIG["chatgpt"]
        entry = record({"provider": self.provider, "shape": shape, "model": model, "route": "chatgpt",
                        "rule": "chatgpt:" + cg["model"], "stream": stream, "status": 200, "ms": 0,
                        "prompt": subject(messages)[:400], "reply": "", "tools": [], "usage": None, "error": None})
        self.trace["Rule"] = "chatgpt:" + cg["model"]
        log("%s %s model=%s stream=%s -> chatgpt:%s" % (self.provider, shape, model, stream, cg["model"]))

        def settle(**fields):
            entry.update(fields, ms=int((time.time() - started) * 1000))

        events = chatgpt_events(resp)
        begun = False
        try:
            if not stream:
                turn = _collect(events)
                session_capture(self.ctx, "chatgpt", "chatgpt:" + cg["model"], turn, int((time.time() - started) * 1000))
                settle(reply=(turn.get("text") or "")[:400], usage=turn["usage"],
                       tools=[c["name"] for c in turn.get("tool_calls") or []])
                build = (anthropic_body if shape == "anthropic"
                         else responses_body if shape == "responses" else openai_body)
                return self.send_json(200, build(turn, model))

            enc, said, made = chatgpt_encoder(shape, model, body), [], []
            for kind, data in events:
                frames = (enc.text(data) if kind == "text" else enc.call(data) if kind == "call" else enc.end(data))
                if not begun:
                    self.begin_stream()
                    begun = True
                for frame in frames:
                    self.write_event(*frame)
                if kind == "text":
                    said.append(data)
                elif kind == "call":
                    entry["tools"].append(data["name"])
                    try:
                        made.append({"id": data["id"], "name": data["name"], "input": json.loads(data["arguments"])})
                    except ValueError:
                        made.append({"id": data["id"], "name": data["name"], "input": {"_raw": data["arguments"]}})
                elif kind == "end":
                    u = data["usage"]
                    session_capture(self.ctx, "chatgpt", "chatgpt:" + cg["model"], {
                        "text": "".join(said), "tool_calls": made,
                        "usage": {"input": u["input"], "output": u["output"], "cacheRead": u["cached"], "reasoning": u["reasoning"]}},
                        int((time.time() - started) * 1000))
                    settle(reply="".join(said)[:400],
                           usage={"input": u["input"], "output": u["output"], "cacheRead": u["cached"], "model": cg["model"]})
        except ChatGPTError as exc:
            log("chatgpt failed %s: %s" % ("mid-stream" if begun else "before the first word", exc.message))
            settle(status=exc.status, error=exc.message)
            if not begun:
                return self.refuse(exc.status, exc.kind, exc.message)
            try:
                for frame in enc.error(exc):
                    self.write_event(*frame)
            except BrokenPipeError:
                pass
        except BrokenPipeError:
            settle()
        finally:
            try:
                resp.close()
            except Exception:
                pass
        return None

    def validate(self, body, shape, model, messages):
        """Refuse what the vendor would refuse, in the vendor's own words.

        A simulator that answers 200 to a malformed request is worse than one
        that answers nothing: the client passes here and fails in production,
        and the test that should have caught it is the one that gave false
        comfort. Each check below is a real 4xx from a real API.

        All of it is switchable, because strictness that cannot be turned off
        is a simulator that eventually blocks a setup somebody needs working.
        """
        fid = CONFIG.get("fidelity", {})
        if not fid.get("validate", True):
            return None

        if not isinstance(messages, list) or not messages:
            # Anthropic and OpenAI both reject an empty conversation.
            return self.refuse(400, "invalid_request_error",
                               "messages: at least one message is required.")

        for i, msg in enumerate(messages):
            if not isinstance(msg, dict) or "role" not in msg:
                return self.refuse(400, "invalid_request_error",
                                   "messages.%d: role is required." % i)
            if msg["role"] not in ("user", "assistant", "system", "tool", "developer"):
                return self.refuse(400, "invalid_request_error",
                                   "messages.%d.role: %r is not a valid role." % (i, msg["role"]))

        # max_tokens is required by Anthropic and optional everywhere else —
        # one of the few places the vendors genuinely disagree.
        if shape == "anthropic":
            if body.get("max_tokens") is None:
                return self.refuse(400, "invalid_request_error",
                                   "max_tokens: field required.")
            try:
                want = int(body["max_tokens"])
            except (TypeError, ValueError):
                return self.refuse(400, "invalid_request_error",
                                   "max_tokens: must be an integer.")
            if want < 1:
                return self.refuse(400, "invalid_request_error",
                                   "max_tokens: must be greater than 0.")
            if fid.get("limits", True) and want > model_limits(model)["maxOutput"]:
                return self.refuse(400, "invalid_request_error",
                                   "max_tokens: %d is more than %s can return (%d)."
                                   % (want, model, model_limits(model)["maxOutput"]))

        if fid.get("unknownModel", False) and not known_model(self.provider, model):
            return self.refuse(404, "not_found_error", "model: %r does not exist." % model)

        if fid.get("limits", True):
            # Rough, and honest about it: the token count is synthetic, so
            # this catches a caller that sends a novel, not one that is a
            # hundred tokens over.
            size = rough_tokens(json.dumps(messages))
            window = model_limits(model)["context"]
            if size > window:
                return self.refuse(400, "invalid_request_error",
                                   "prompt is too long: %d tokens > %d maximum for %s."
                                   % (size, window, model))
        return None

    def handle_chat(self, body, shape):
        model = body.get("model") or (MODELS[self.provider][0])
        messages = responses_to_messages(body) if shape == "responses" else (body.get("messages") or [])
        stream = bool(body.get("stream"))
        declared = declared_tools(body)
        # What a recording needs to know about this request, wherever in the
        # several paths below the answer ends up being produced.
        self.ctx = {"shape": shape, "model": model, "messages": messages,
                    "provider": self.provider, "declared": sorted(declared)}

        bad = self.validate(body, shape, model, messages)
        if bad:
            return bad

        # Per model, not per process. A demo can point one model at a real
        # cheap one and leave every other model offline and free, which is
        # the routing table's whole reason for existing.
        internal = getattr(self, "internal", False)
        route = "scripted" if internal else route_for(model)
        started = time.time()

        # A replay comes before routing: a request the recording can answer is
        # answered from it whatever the model is routed to, and that is the
        # point — the run no longer depends on anything that route reaches.
        replaying = SESSION["mode"] == "replay" and not internal
        replayed = session_replay(messages) if replaying else None
        if replaying and not replayed and SESSION["onMiss"] != "route":
            ask, n, _, _ = exchange_key(messages)
            message = ("LLMSim has no recording for this request. Cassette %s, asked %r, %d turn(s) in. "
                       "Record it again, or set misses to route normally." % (SESSION["name"], ask[:80], n))
            log("replay miss: " + message)
            record({"provider": self.provider, "shape": shape, "model": model, "route": "replay",
                    "rule": None, "stream": stream, "status": 404, "ms": 0,
                    "prompt": subject(messages)[:400], "reply": None, "tools": [], "error": message})
            self.trace = {"Route": "replay"}
            return self.refuse(404, "not_found_error", message)
        if replayed:
            route = "replay"
        self.trace = {"Route": route}
        if route == "replay":
            entry_hit, differs = replayed
            name = "replay:%s%s" % (SESSION["name"], " (last message differs)" if differs else "")
            turn = turn_from_entry(entry_hit)
        elif route == "relay":
            try:
                # A streamed relay is passed through token by token rather
                # than fetched whole and replayed. Replaying is the right
                # shape and the wrong experience: a demo of an agent typing
                # is the thing being demonstrated, and one long pause
                # followed by everything at once is not that.
                self.trace["Rule"] = "relay:" + CONFIG["upstream"]["model"]
                if stream:
                    return self.relay_stream(shape, model, messages, body.get("tools"), entry_started=started)
                turn = upstream_turn(messages, declared, body.get("tools"))
                name = "relay:" + CONFIG["upstream"]["model"]
            except UpstreamError as exc:
                log("relay failed: %s" % exc)
                if not CONFIG["upstream"]["fallback"]:
                    # Reported rather than hidden. A demo that silently falls
                    # back to canned answers looks like it is working and
                    # proves nothing — which is the one outcome worth
                    # refusing.
                    record({"provider": self.provider, "shape": shape, "model": model,
                            "route": route, "rule": None, "stream": stream, "status": 502,
                            "ms": int((time.time() - started) * 1000),
                            "prompt": subject(messages)[:400], "reply": None, "tools": [],
                            "error": str(exc)})
                    return self.refuse(502, "upstream_error",
                                       "LLMSim could not reach its upstream model: %s" % exc)
                name, turn = choose_turn(self.provider, model, messages, declared)
                name = "fallback:" + name
        elif route == "chatgpt":
            canon = canon_request(shape, body)
            payload = chatgpt_payload(canon, CONFIG["chatgpt"]["model"])
            try:
                resp = chatgpt_open(payload)
            except ChatGPTError as exc:
                log("chatgpt failed before it began: %s" % exc.message)
                if not CONFIG["chatgpt"]["fallback"]:
                    record({"provider": self.provider, "shape": shape, "model": model,
                            "route": route, "rule": None, "stream": stream, "status": exc.status,
                            "ms": int((time.time() - started) * 1000),
                            "prompt": subject(messages)[:400], "reply": None, "tools": [],
                            "error": exc.message})
                    return self.refuse(exc.status, exc.kind, exc.message)
                name, turn = choose_turn(self.provider, model, messages, declared)
                name = "fallback:" + name
            else:
                if canon["dropped"]:
                    log("   not sent to ChatGPT, which does not take them: %s" % ", ".join(canon["dropped"]))
                return self.chatgpt_serve(resp, shape, model, stream, body, messages, started)
        else:
            name, turn = choose_turn(self.provider, model, messages, declared)

        self.trace["Rule"] = name
        entry = record({"provider": self.provider, "shape": shape, "model": model, "route": route,
                "rule": name, "stream": stream, "status": turn.get("error", {}).get("status", 200),
                "ms": int((time.time() - started) * 1000),
                "prompt": subject(messages)[:400],
                "reply": (turn.get("text") or "")[:400],
                "tools": [c.get("name") for c in (turn.get("tool_calls") or [])],
                # Real when it came from an upstream, absent when the answer
                # was scripted — a demo should never be able to mistake a
                # synthetic number for a bill.
                "usage": turn.get("usage"),
                "error": None})

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
                    resolved.append(dict(call, name=real, id=call.get("id") or _call_id(shape)))
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

                resolved.append(dict(call, name=real, input=aligned, id=call.get("id") or _call_id(shape)))
            turn = dict(turn, tool_calls=resolved)

        if route != "replay" and not internal and not turn.get("error"):
            session_capture(self.ctx, route, name, turn, int((time.time() - started) * 1000))

        delay = turn.get("delay_ms")
        if delay:
            time.sleep(delay / 1000.0)
        # Stamped after the wait, not before it. The entry is the same object
        # the log holds, so updating it here is what the console shows.
        entry["ms"] = int((time.time() - started) * 1000)

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
        self.vendor_headers(200)
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
    CHATGPT["auth"] = _load_chatgpt_auth()
    log("listening on http://localhost:%d" % PORT)
    log("script: %s (%d rules)" % (SCRIPT_PATH, len(SCRIPT.get("rules", []))))
    ensure_suite_file()
    log("suite:  %s (%d case(s), %d plan(s))"
        % (SUITE_PATH, len(CONFIG.get("tests", [])), len(CONFIG.get("plans", []))))
    up, routing = CONFIG["upstream"], CONFIG["routing"]
    log("default: %s%s" % (routing["default"],
        ("  (%d model(s) routed individually)" % len(routing["models"])) if routing["models"] else ""))
    if relaying():
        log("upstream: %s at %s (%s format)%s"
            % (up["model"], up["url"], up["format"],
               ", falling back to the script on failure" if up["fallback"] else ""))
        if not up["key"]:
            log("          no upstream key set — the upstream will almost certainly refuse")
    else:
        log("upstream: none — scripted answers only, nothing leaves this machine")
    record_as, replay_as = os.environ.get("LLMSIM_RECORD"), os.environ.get("LLMSIM_REPLAY")
    if record_as and replay_as:
        log("session: LLMSIM_RECORD and LLMSIM_REPLAY are both set — neither is used. Pick one.")
    elif record_as or replay_as:
        ok, message = session_start("record" if record_as else "replay", record_as or replay_as)
        log("session: " + message if ok else "session: could not start — " + message)
    plan = CONFIG["chatgpt"]
    routed_to_plan = [m for m, r in CONFIG["routing"]["models"].items() if r == "chatgpt"]
    if CHATGPT["auth"]:
        log("chatgpt: signed in as %s%s — answers with %s%s"
            % (CHATGPT["auth"]["email"] or "an account", " (%s)" % CHATGPT["auth"]["plan"] if CHATGPT["auth"]["plan"] else "",
               plan["model"], " for the default route" if CONFIG["routing"]["default"] == "chatgpt" else ""))
        # Said out loud because the README's old promise — nothing here is
        # worth taking — stops being true once a subscription is signed in.
        log("          anyone who can reach this port can spend that plan; keep it off a shared network")
    elif CONFIG["routing"]["default"] == "chatgpt" or routed_to_plan:
        log("chatgpt: routes point at the plan but nobody is signed in — those requests will fail until the console signs in")
    log("console: http://localhost:%d/ui" % PORT)
    for name in sorted(API_KEYS):
        log("  %-9s http://localhost:%d/%s   key %s" % (name, PORT, name, API_KEYS[name]))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
