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

import collections
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
import urllib.request
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
    """scripted or relay, for this model.

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


def _anthropic_usage(input_tokens, output_tokens):
    """input/output, with the cache split a real response carries."""
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
        # The cache fields are not decoration. Claude Code reports them, and
        # Maniple's own extractUsage reads cacheReadInputTokens and
        # cacheCreationInputTokens — so a usage path that handles them could
        # never be exercised while this returned two numbers.
        #
        # Split from the same synthetic total rather than invented on top of
        # it, so the arithmetic a caller does still adds up.
        "usage": _anthropic_usage(turn.get("input_tokens", 1200), rough_tokens(out_text)),
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
            "prompt_tokens_details": {"cached_tokens": int(prompt_tokens * 0.8),
                                      "audio_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0, "audio_tokens": 0,
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
            "input_tokens_details": {"cached_tokens": int(input_tokens * 0.8)},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
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
            if default not in ("scripted", "relay"):
                return self.send_json(400, {"error": "routing.default must be scripted or relay"})
            CONFIG["routing"]["default"] = default

        if "models" in routing:
            table = {}
            for model, choice in (routing["models"] or {}).items():
                if choice not in ("scripted", "relay"):
                    return self.send_json(400, {"error": "%s: must be scripted or relay" % model})
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

        # The console writes settings here. Checked before the vendor key
        # below, because this route is not a vendor route and has no API key.
        if path == "/api/ui/config":
            return self.save_ui_config(body)

        if path == "/api/ui/run":
            return self.run_tests(body)

        if path == "/api/ui/suite":
            return self.import_suite(body)

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
        except Exception as exc:
            log("relay stream broke mid-flight: %s" % exc)
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

        bad = self.validate(body, shape, model, messages)
        if bad:
            return bad

        # Per model, not per process. A demo can point one model at a real
        # cheap one and leave every other model offline and free, which is
        # the routing table's whole reason for existing.
        route = route_for(model)
        started = time.time()
        if route == "relay":
            try:
                # A streamed relay is passed through token by token rather
                # than fetched whole and replayed. Replaying is the right
                # shape and the wrong experience: a demo of an agent typing
                # is the thing being demonstrated, and one long pause
                # followed by everything at once is not that.
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
        else:
            name, turn = choose_turn(self.provider, model, messages, declared)

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
    log("console: http://localhost:%d/ui" % PORT)
    for name in sorted(API_KEYS):
        log("  %-9s http://localhost:%d/%s   key %s" % (name, PORT, name, API_KEYS[name]))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
