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
import http.cookies
import itertools
import json
import os
import threading
import random
import re
import socket
import string
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The agent test kit (scenarios, mocks, a kept timeline of every run). It is a package
# beside this file; a copy of the server without it is the simulator it always was.
try:
    import testkit
except ImportError:  # pragma: no cover — the server must run without its kit
    class testkit:  # noqa: N801
        @staticmethod
        def attach(_module): pass
        @staticmethod
        def owns(_path): return False
        @staticmethod
        def on_capture(_cap): pass
        @staticmethod
        def capture_started(_cap): pass
        @staticmethod
        def run_sub(*_args): return None
        @staticmethod
        def run_rules(): return []
        @staticmethod
        def run_faults(): return []
        @staticmethod
        def run_script(_view): return None
        @staticmethod
        def claim_for(*_args): return None
        @staticmethod
        def model_guard(*_args):
            import contextlib
            return contextlib.nullcontext()
        @staticmethod
        def store_runs(): return []
        @staticmethod
        def forget_run(_run): return 0
        class timeline:  # noqa: N801
            @staticmethod
            def trace_run(_header): return None

HERE = os.path.dirname(os.path.abspath(__file__))
# Everything LLMSim writes for itself goes in one directory: the keys and routing the console saves, your rules, the suite, the
# runs, the recordings. Beside the code by default, so a checkout keeps working
# exactly as it always has. LLMSIM_DATA moves the lot, which is what a
# container wants: point it at a volume and a pod that is replaced or restarted
# comes back as it was. A file's own variable (LLMSIM_CONFIG, ...) still wins.
DATA_DIR = os.path.abspath(os.environ.get("LLMSIM_DATA") or HERE)
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
CONFIG_PATH = os.environ.get("LLMSIM_CONFIG", os.path.join(DATA_DIR, "llmsim.config.json"))

# Test cases and plans are kept apart from the rest of the settings, in a
# file that is NOT gitignored.
#
# llmsim.config.json is machine-local by design — it holds the upstream key —
# and a suite that lives in it cannot be committed, reviewed or handed to
# anybody, which undoes most of the reason for writing one. A suite is source:
# it belongs beside the code it tests, in a diff, on a branch.
SUITE_PATH = os.environ.get("LLMSIM_SUITE", os.path.join(DATA_DIR, "llmsim.tests.json"))
SUITE_KEYS = ("tests", "plans")

# The console asks for a sign-in only once one is configured (see "access" below).
#
# On a developer's own machine a password is friction on the one path everybody
# uses, so a checkout starts open, as it always has. Anywhere else it holds what
# is worth taking: every request an agent sent, a saved key, a signed-in plan.
# There, LLMSIM_ADMIN_PASSWORD and LLMSIM_API_KEY put the console and /api behind
# a sign-in, and the installers set them.

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
    "session": {"onMiss": "fail", "strict": False},
    # What each request was, kept in memory so it can be looked at and asserted on.
    #
    #   max        how many requests are kept (the newest)
    #   maxBytes   the most of one request that is kept whole; a bigger one is
    #              reduced to what assertions read and marked truncated
    #   redact     applied on the way OUT — to the console, the API, an export —
    #              never to what assertions are run on
    "capture": {"enabled": True, "max": 300, "maxBytes": 262144,
                "redact": {"headers": ["x-forwarded-for", "x-real-ip"], "patterns": []}},
    # Where a request says who sent it. The base URL is always read
    # (/run/<id>/agent/<name>/…); these are the rest, for a gateway that carries
    # an identity through. Which header a given gateway forwards is a fact about
    # that gateway, so it is configured here and not assumed.
    "identity": {"rules": [
        {"field": "run", "from": "header", "name": "x-llmsim-run"},
        {"field": "agent", "from": "header", "name": "x-llmsim-agent"},
        {"field": "app", "from": "header", "name": "x-llmsim-app"},
    ]},
    # Which credentials the vendor routes accept, and what the ones that arrive are called.
    #   mode         published (the keys in this file), registered (those, plus
    #                the fingerprints below) or any (witness mode: accept anything
    #                and say which one came)
    #   credentials  [{label, fingerprint}] — a fingerprint, never the key
    "auth": {"mode": "published", "credentials": []},
    # Fault rules (see the Faults section): things that go wrong, on purpose, when
    # a condition holds. `paused` stops all of them at once without deleting any.
    "faults": [],
    "chaos": {"paused": False},
    # A third route, "anthropic": a real Claude model, by API key. Any client
    # format in, that format out. See "Anthropic: a real Claude model".
    "anthropic": {
        "url": os.environ.get("LLMSIM_ANTHROPIC_UPSTREAM", "https://api.anthropic.com").rstrip("/"),
        "key": os.environ.get("LLMSIM_ANTHROPIC_UPSTREAM_KEY", ""),
        # How the credential is sent: "api_key" as x-api-key, which is what
        # Anthropic's API takes, or "bearer" as Authorization: Bearer, which is
        # what a gateway in front of it usually takes.
        "auth": os.environ.get("LLMSIM_ANTHROPIC_UPSTREAM_AUTH", "api_key"),
        # Empty means "the model the client asked for", when that is a Claude model.
        "model": os.environ.get("LLMSIM_ANTHROPIC_UPSTREAM_MODEL", ""),
        "maxTokens": 4096,
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


ROUTES = ("scripted", "relay", "anthropic")


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
    # A config saved with a route this LLMSim does not have answers from the script, not with an error on every request.
    routing = config["routing"]
    if routing.get("default") not in ROUTES:
        routing["default"] = "scripted"
    routing["models"] = {m: r for m, r in routing.get("models", {}).items() if r in ROUTES}
    return config


CONFIG = load_config()


def save_config():
    settings = {k: v for k, v in CONFIG.items() if k not in SUITE_KEYS}
    _write_json(CONFIG_PATH, settings)
    # It can hold an upstream key and an Anthropic key. Nobody else on the
    # machine needs to read it, so nobody else can.
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
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
    out["anthropic"]["key"] = ""
    out["anthropic"]["keySet"] = bool(CONFIG["anthropic"]["key"])
    return out


def route_for(model):
    """scripted, relay or anthropic, for this model.

    A named model wins over the default, and a relay route with no upstream
    URL configured is scripted — saying "relay" while having nowhere to relay
    to would fail every request for a reason the table does not show.
    """
    routing = CONFIG["routing"]
    hold = route_hold()
    if hold and hold["everyModel"]:
        choice = hold["default"]
    else:
        choice = routing["models"].get(model) or (hold["default"] if hold else routing["default"])
    if choice == "relay" and not CONFIG["upstream"]["url"]:
        return "scripted"
    return choice


# ── A route held for a while ──────────────────────────────────────────────
#
# The default route is somebody's standing choice: this LLMSim answers with a
# real model, say. A test that scripts the model needs the script to answer
# while it runs, and nothing else about that choice to change. Rewriting the
# setting and writing it back afterwards is how that was done, by hand, and it
# has one way to go wrong that matters: a run that is killed between the two
# leaves a person's LLMSim answering from a script, and nothing on screen says
# why their agent has started saying "ok.".
#
# So a caller does not change the setting. It HOLDS the default route on what it
# needs, for a number of seconds it names, and releases it when it is done. The
# hold is in memory only and is never written to the configuration: releasing it,
# its time running out, and restarting LLMSim all give back exactly what was
# chosen, because what was chosen was never touched.
ROUTE_HOLD = None                 # {"id", "default", "everyModel", "by", "since", "until"}
ROUTE_HOLD_LOCK = threading.Lock()
ROUTE_HOLD_MAX_S = 3600


def route_hold():
    """The hold on the default route, if one is in force. One whose time has run out is let go here."""
    global ROUTE_HOLD
    with ROUTE_HOLD_LOCK:
        if ROUTE_HOLD and ROUTE_HOLD["until"] <= time.time():
            log("routing: the hold on %s by %s ran out; the default route is %s again"
                % (ROUTE_HOLD["default"], ROUTE_HOLD["by"], CONFIG["routing"]["default"]))
            ROUTE_HOLD = None
        return dict(ROUTE_HOLD) if ROUTE_HOLD else None


def default_route():
    """The route a model with none of its own takes right now."""
    hold = route_hold()
    return hold["default"] if hold else CONFIG["routing"]["default"]


def set_routing(routing):
    """Change the routing that is kept. Returns what is wrong with it, or None.

    Validated rather than merged blindly: a routing table is the one thing here
    that can send traffic to a paid endpoint, and a typo that silently becomes a
    new key would be discovered on an invoice.
    """
    if not isinstance(routing, dict):
        return "routing is {default?, models?}"
    default = routing.get("default")
    if default is not None and default not in ROUTES:
        return "routing.default must be one of %s" % ", ".join(ROUTES)
    table = None
    if "models" in routing:
        if not isinstance(routing["models"] or {}, dict):
            return "routing.models is {model: route}"
        for model, choice in (routing["models"] or {}).items():
            if choice not in ROUTES:
                return "%s: must be one of %s" % (model, ", ".join(ROUTES))
        # "same as the default" is not an override; storing it would
        # pin the model the next time the default changes.
        table = {m: c for m, c in (routing["models"] or {}).items() if c != (default or CONFIG["routing"]["default"])}
    if default is not None:
        CONFIG["routing"]["default"] = default
    if table is not None:
        CONFIG["routing"]["models"] = table
    return None


def routing_view():
    return {"default": CONFIG["routing"]["default"], "models": dict(CONFIG["routing"]["models"]), "effective": default_route(),
            "routes": list(ROUTES), "hold": route_hold()}


def _rest_routing(method, rest, body, query, host):
    """GET/PUT /api/routing; POST /api/routing/hold; DELETE /api/routing/hold[/<id>]."""
    global ROUTE_HOLD
    body = body if isinstance(body, dict) else {}
    if method == "GET" and not rest:
        return _ok(routing_view())
    if method == "PUT" and not rest:
        extra = sorted(set(body) - {"default", "models"})
        if extra:
            return _fail(400, "Routing is default and models. Not understood: %s." % ", ".join(extra))
        problem = set_routing(body)
        if problem:
            return _fail(400, problem)
        save_config()
        return _ok(routing_view())
    if method == "POST" and rest == ["hold"]:
        extra = sorted(set(body) - {"default", "seconds", "by", "everyModel"})
        if extra:
            return _fail(400, "A hold is default, seconds, by and everyModel. Not understood: %s." % ", ".join(extra))
        default, seconds, by = body.get("default"), body.get("seconds", 300), body.get("by")
        if default not in ROUTES:
            return _fail(400, "default must be one of %s." % ", ".join(ROUTES))
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= ROUTE_HOLD_MAX_S:
            return _fail(400, "seconds is how long the hold may last at most: 1 to %d." % ROUTE_HOLD_MAX_S)
        if not isinstance(by, str) or not 0 < len(by.strip()) <= 80:
            return _fail(400, "by says who is holding it, in up to 80 characters. It is shown to whoever looks at the console meanwhile.")
        if not isinstance(body.get("everyModel", False), bool):
            return _fail(400, "everyModel is true or false.")
        current = route_hold()
        if current and current["by"] != by.strip():
            return _fail(409, "The default route is already held on %s by %s, for another %d second(s). Two callers that each need "
                         "their own route cannot both have it." % (current["default"], current["by"], max(1, int(current["until"] - time.time()))),
                         hold=current)
        now = time.time()
        with ROUTE_HOLD_LOCK:
            ROUTE_HOLD = {"id": "h-" + uuid.uuid4().hex[:10], "default": default, "everyModel": bool(body.get("everyModel", False)),
                          "by": by.strip(), "since": now, "until": now + float(seconds)}
            held = dict(ROUTE_HOLD)
        log("routing: %s holds the default route on %s for up to %ds (it is kept as %s)" % (held["by"], default, seconds, CONFIG["routing"]["default"]))
        return _ok(routing_view(), message="Held on %s. It goes back to %s when released, or by itself in %d second(s)."
                   % (default, CONFIG["routing"]["default"], seconds))
    if method == "DELETE" and rest[:1] == ["hold"] and len(rest) <= 2:
        current = route_hold()
        if current and len(rest) == 2 and rest[1] != current["id"]:
            return _fail(409, "That is not the hold in force: %s holds the default route now." % current["by"], hold=current)
        with ROUTE_HOLD_LOCK:
            ROUTE_HOLD = None
        if current:
            log("routing: %s released the default route; it is %s again" % (current["by"], CONFIG["routing"]["default"]))
        return _ok(routing_view(), released=bool(current))
    return _fail(404, "No such route: %s /api/routing/%s" % (method, "/".join(rest)))


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
    # Sonnet 5 and 5.5 before the family row, which carries 4.5's figures: a
    # prefix match gave Sonnet 5 a 64,000-token output limit it does not have,
    # and refused clients that asked for its real 128,000. Published by
    # Anthropic: 1M context, 128K output on the Messages API.
    ("claude-sonnet-5", {"context": 1000000, "maxOutput": 128000}),
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


def fill(turn, messages, view=None):
    """The turn with its placeholders filled in. The script is not touched."""
    if "{{" not in json.dumps(turn):
        return turn
    values = {
        "{{ask}}": subject(messages).strip(),
        "{{tool_output}}": last_tool_output(messages).strip()[:TOOL_OUTPUT_LIMIT],
    }
    if view:
        ident = view.get("identity") or {}
        values.update({"{{run}}": ident.get("run", ""), "{{agent}}": ident.get("agent", ""), "{{app}}": ident.get("app", ""),
                       "{{turn}}": str(view.get("turn", 0)), "{{model}}": view.get("model", "")})

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
    # or a real model that would spend something and say something else — and
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


def choose_turn(provider, model, messages, declared=None, view=None):
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

    if view is None:
        view = build_view({"provider": provider, "shape": "openai", "model": model}, {"model": model, "messages": messages})
    name, _source, _index, turn, _checked = pick_scripted(view)
    return name, fill(turn, messages, view)


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
            "input": call["raw_arguments"] if "raw_arguments" in call else call.get("input", {}),
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
                                  turn["output_tokens"] if "output_tokens" in turn else rough_tokens(out_text),
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
            for chunk in chunked(block["input"] if isinstance(block["input"], str) else json.dumps(block["input"]), 24):
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
            "function": {"name": c["name"], "arguments": c["raw_arguments"] if "raw_arguments" in c else json.dumps(c.get("input", {}))},
        } for c in turn["tool_calls"]]
        finish = "tool_calls"

    prompt_tokens = turn.get("input_tokens", 1200)
    completion_tokens = turn["output_tokens"] if "output_tokens" in turn else rough_tokens(json.dumps(message))
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
            "name": call["name"], "arguments": call["raw_arguments"] if "raw_arguments" in call else json.dumps(call.get("input", {})), "status": "completed",
        })

    input_tokens = turn.get("input_tokens", 1200)
    output_tokens = turn["output_tokens"] if "output_tokens" in turn else rough_tokens(json.dumps(output))
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


# ── What every real model's route shares ──────────────────────────────────

class ModelRouteError(Exception):
    """A failure to report in the client's own error envelope."""

    def __init__(self, status, kind, message):
        Exception.__init__(self, message)
        self.status, self.kind, self.message = status, kind, message


def state_store():
    """Where LLMSim keeps what it writes, and whether that outlives it.

    On a machine it does. In a container it does only if the directory is a
    volume: otherwise the next restart starts from nothing, with no keys, no
    routing and none of your rules. Said out loud, because the first anybody
    hears of it otherwise is an empty console."""
    contained = os.path.exists("/.dockerenv") or bool(os.environ.get("KUBERNETES_SERVICE_HOST"))
    return {"dir": DATA_DIR, "kept": not contained or os.path.ismount(DATA_DIR)}


# ── Real models: one request and one stream, whatever the client spoke ───────
#
#     client (Chat Completions | Anthropic Messages | Responses)
#         │   canon_request()
#         ▼
#     CanonicalRequest ── the route's payload ──▶ the real model
#         ▲                                        │
#         │                                        │
#     encoders ◀── CanonicalEvents ◀── the route's stream reader
#     (the client's own wire format, streamed or whole)
#
# Every client format goes in and comes out through the same two seams, so
# another client format is one translator rather than a copy of the whole path.

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


# Sampling controls not every real model takes. Sent anyway they are a
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


# -- canonical events: what the route says, in the order it says it -----------
#
#   ("text", str)                                  a piece of the answer
#   ("call", {"id", "name", "arguments"})          a whole tool call
#   ("end",  {"reason", "usage": {...}})           finished
#
# A tool call is passed on when the route has finished it rather than
# argument-fragment by fragment. No client acts on half a call, and the
# fragments are the most format-specific thing in all three wire formats.

def _collect(events, plan, who):
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
        turn["text"] = "(%s returned no content)" % who
    if reason == "length":
        turn["stop_reason"] = "max_tokens"
    turn.update(input_tokens=usage.get("input", 0), output_tokens=usage.get("output", 0),
                cached_tokens=usage.get("cached", 0), reasoning_tokens=usage.get("reasoning", 0))
    turn["usage"] = {"input": usage.get("input", 0), "output": usage.get("output", 0),
                     "cacheRead": usage.get("cached", 0), "model": plan}
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


def model_route_encoder(shape, model, body):
    if shape == "anthropic":
        return _AnthropicEnc(model)
    if shape == "responses":
        return _ResponsesEnc(model)
    return _ChatEnc(model, bool((body.get("stream_options") or {}).get("include_usage")))


# ── Anthropic: a real Claude model, by API key ───────────────────────────
#
# This route answers with Anthropic's API and a key: two seams, a canonical
# request in and canonical events out, so a client speaking any of the three
# formats is answered in its own, tools and streaming included.
#
# It takes a key and not a sign-in, on purpose. Anthropic's terms do not permit
# a tool to offer Claude sign-in, or to route requests through Free, Pro or Max
# plan credentials on behalf of whoever is calling it — and that is exactly
# what a simulator answering other tools' requests would be doing. So there is
# no OAuth here and nothing that presents itself as Claude Code. The credential
# is an API key from the Claude Console (or a cloud provider's), sent as
# x-api-key; or, for a gateway that stands in front of the API, whatever token
# that gateway issues, sent as Authorization: Bearer.

class AnthropicError(ModelRouteError):
    pass


ANTHROPIC_VERSION = "2023-06-01"


def save_anthropic_settings(given):
    """Apply what the console sent. Returns what is wrong with it, or None."""
    cfg = CONFIG["anthropic"]
    if "url" in given:
        url = str(given["url"] or "").strip().rstrip("/") or "https://api.anthropic.com"
        if not re.match(r"https?://[^\s/]+", url):
            return "anthropic.url must start with http:// or https://"
        cfg["url"] = url
    if "auth" in given:
        if given["auth"] not in ("api_key", "bearer"):
            return "anthropic.auth is api_key (sent as x-api-key) or bearer (sent as Authorization: Bearer)"
        cfg["auth"] = given["auth"]
    if "model" in given:
        cfg["model"] = str(given["model"] or "").strip()[:100]
    if "maxTokens" in given:
        try:
            limit = int(given["maxTokens"])
        except (TypeError, ValueError):
            return "anthropic.maxTokens must be a whole number"
        if not 1 <= limit <= 128000:
            return "anthropic.maxTokens must be between 1 and 128000"
        cfg["maxTokens"] = limit
    if "fallback" in given:
        cfg["fallback"] = bool(given["fallback"])
    # An empty key means "leave the stored one alone", so a person changing
    # the model does not have to retype the secret. Forgetting it is said out loud.
    if given.get("forgetKey"):
        cfg["key"] = ""
    elif given.get("key"):
        cfg["key"] = str(given["key"]).strip()
    return None


def anthropic_model(requested):
    """The model Anthropic is asked for: the one chosen, or the client's own when it is a Claude model."""
    chosen = CONFIG["anthropic"]["model"]
    if chosen:
        return chosen
    if str(requested or "").startswith("claude"):
        return requested
    raise AnthropicError(502, "upstream_error",
                         "No Anthropic model is chosen, and %r is not one of theirs. Set a model under Routing, Anthropic API."
                         % (requested or ""))


def _anthropic_auth_headers():
    cfg = CONFIG["anthropic"]
    if not cfg["key"]:
        raise AnthropicError(502, "upstream_error",
                             "This model is routed to Anthropic and no key is set. Add one under Routing, Anthropic API.")
    auth = {"Authorization": "Bearer " + cfg["key"]} if cfg["auth"] == "bearer" else {"x-api-key": cfg["key"]}
    return dict(auth, **{"anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"})


def _anthropic_image(url):
    match = re.match(r"data:([^;,]+);base64,(.*)$", url or "", re.S)
    if match:
        return {"type": "image", "source": {"type": "base64", "media_type": match.group(1), "data": match.group(2)}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def anthropic_payload(canon, model, body):
    """Canonical request → POST /v1/messages, always streamed.

    Streamed whatever the client asked:
    one way to read the answer, and a client that wanted it whole is served by
    collecting the stream. A long answer asked for whole would otherwise be
    refused by the API for being long.
    """
    messages = []

    def add(role, block):
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].append(block)
        else:
            messages.append({"role": role, "content": [block]})

    for item in canon["items"]:
        kind = item["kind"]
        if kind == "text":
            add("assistant" if item["role"] == "assistant" else "user", {"type": "text", "text": item["text"]})
        elif kind == "image":
            add("user", _anthropic_image(item["url"]))
        elif kind == "call":
            try:
                args = json.loads(item["arguments"])
            except ValueError:
                args = {"_raw": item["arguments"]}
            add("assistant", {"type": "tool_use", "id": item["call_id"], "name": item["name"],
                              "input": args if isinstance(args, dict) else {"value": args}})
        elif kind == "result":
            add("user", {"type": "tool_result", "tool_use_id": item["call_id"], "content": item["output"]})

    limit = next((body.get(k) for k in ("max_tokens", "max_completion_tokens", "max_output_tokens")
                  if isinstance(body.get(k), int) and not isinstance(body.get(k), bool) and body.get(k) > 0), None)
    payload = {"model": model, "max_tokens": limit or CONFIG["anthropic"]["maxTokens"], "messages": messages, "stream": True}
    system = "\n\n".join(part for part in canon["system"] if part)
    if system:
        payload["system"] = system
    if canon["tools"]:
        payload["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                            for t in canon["tools"]]
        choice = canon["tool_choice"]
        chosen = ({"type": "tool", "name": choice["name"]} if isinstance(choice, dict)
                  else {"type": "any"} if choice == "required" else {"type": "none"} if choice == "none" else {"type": "auto"})
        if not canon["parallel"] and chosen["type"] != "none":
            chosen["disable_parallel_tool_use"] = True
        if chosen != {"type": "auto"}:
            payload["tool_choice"] = chosen
    # The sampling controls the API does take. One of temperature and top_p,
    # not both: newer models refuse the pair.
    if isinstance(body.get("temperature"), (int, float)) and not isinstance(body.get("temperature"), bool):
        payload["temperature"] = body["temperature"]
    elif isinstance(body.get("top_p"), (int, float)) and not isinstance(body.get("top_p"), bool):
        payload["top_p"] = body["top_p"]
    stops = body.get("stop_sequences") or body.get("stop")
    if isinstance(stops, str):
        stops = [stops]
    if isinstance(stops, list) and stops:
        payload["stop_sequences"] = [str(x) for x in stops][:4]
    return payload


def anthropic_not_sent(canon, body):
    """What the client asked for that this route has no way to pass on, for the log."""
    out = [k for k in canon["dropped"] if k.startswith("tool:")]
    out += [k for k in ("presence_penalty", "frequency_penalty", "seed", "n") if body.get(k) is not None]
    if canon["effort"]:
        out.append("reasoning_effort")
    if canon["text_format"]:
        out.append("response_format")
    return out


def _anthropic_http_error(exc):
    """An upstream HTTP failure, as the client's own."""
    raw = exc.read()[:4000]
    try:
        err = (json.loads(raw) or {}).get("error") or {}
        message = err.get("message") or raw.decode("utf-8", "replace")[:300]
    except Exception:
        message = raw.decode("utf-8", "replace")[:300]
    if exc.code == 429:
        return AnthropicError(429, "rate_limit_error", "Anthropic's rate limit was reached: " + message)
    if exc.code == 529:
        return AnthropicError(529, "overloaded_error", "Anthropic is overloaded: " + message)
    if exc.code in (400, 404, 413, 422):
        return AnthropicError(400, "invalid_request_error", "Anthropic rejected the request: " + message)
    # 401 and 403 are LLMSim's key failing, not the caller's. Passed on as a
    # 401 they would tell a gateway its own key was wrong.
    if exc.code in (401, 403):
        return AnthropicError(502, "upstream_error", "Anthropic refused the key LLMSim holds (%d): %s" % (exc.code, message))
    return AnthropicError(502, "upstream_error", "Anthropic failed (%d): %s" % (exc.code, message))


def anthropic_open(payload):
    """Open the upstream stream. Raises AnthropicError before any byte is sent
    to the client, so a failure can still be an honest HTTP status."""
    headers = dict(_anthropic_auth_headers(), accept="text/event-stream")
    req = urllib.request.Request(CONFIG["anthropic"]["url"] + "/v1/messages", data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    try:
        return urllib.request.urlopen(req, timeout=180)
    except urllib.error.HTTPError as exc:
        raise _anthropic_http_error(exc)
    except Exception as exc:
        raise AnthropicError(502, "upstream_error", "Could not reach Anthropic at %s: %s" % (CONFIG["anthropic"]["url"], exc))


def anthropic_models():
    """The model ids this key can use, newest first, as the API lists them."""
    req = urllib.request.Request(CONFIG["anthropic"]["url"] + "/v1/models?limit=100", headers=_anthropic_auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            rows = (json.loads(resp.read()) or {}).get("data") or []
    except urllib.error.HTTPError as exc:
        raise _anthropic_http_error(exc)
    except Exception as exc:
        raise AnthropicError(502, "upstream_error", "Could not reach Anthropic at %s: %s" % (CONFIG["anthropic"]["url"], exc))
    return [r.get("id") for r in rows if isinstance(r, dict) and r.get("id")]


_ANTHROPIC_STOP = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls", "max_tokens": "length"}


def anthropic_events(resp):
    """Anthropic's stream as the canonical events the encoders take.

    A tool call is passed on when its block closes, whole: the argument fragments are the most format-specific thing
    in the stream, and no client acts on half a call.
    """
    blocks, usage, reason, calls = {}, {"input": 0, "output": 0, "cached": 0, "reasoning": 0}, None, 0
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind == "message_start":
            u = (ev.get("message") or {}).get("usage") or {}
            cached = u.get("cache_read_input_tokens") or 0
            # Canonical input is everything that was read, cached or not; the
            # encoders take the cached part back out where a format wants that.
            usage["input"] = (u.get("input_tokens") or 0) + cached + (u.get("cache_creation_input_tokens") or 0)
            usage["cached"] = cached
        elif kind == "content_block_start":
            block = ev.get("content_block") or {}
            blocks[ev.get("index")] = {"type": block.get("type"), "id": block.get("id"), "name": block.get("name"), "json": []}
        elif kind == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                yield "text", delta["text"]
            elif delta.get("type") == "input_json_delta" and ev.get("index") in blocks:
                blocks[ev["index"]]["json"].append(delta.get("partial_json") or "")
        elif kind == "content_block_stop":
            block = blocks.pop(ev.get("index"), None)
            if block and block["type"] == "tool_use":
                calls += 1
                yield "call", {"id": block["id"] or rand_id("toolu_"), "name": block["name"] or "",
                               "arguments": "".join(block["json"]) or "{}"}
        elif kind == "message_delta":
            reason = (ev.get("delta") or {}).get("stop_reason") or reason
            u = ev.get("usage") or {}
            if u.get("output_tokens") is not None:
                usage["output"] = u["output_tokens"]
        elif kind == "message_stop":
            yield "end", {"reason": "tool_calls" if calls else _ANTHROPIC_STOP.get(reason, "stop"), "usage": usage}
            return
        elif kind == "error":
            err = ev.get("error") or {}
            code, message = err.get("type") or "", err.get("message") or json.dumps(err)[:300]
            if code == "overloaded_error":
                raise AnthropicError(529, "overloaded_error", "Anthropic is overloaded: " + message)
            if code == "rate_limit_error":
                raise AnthropicError(429, "rate_limit_error", "Anthropic's rate limit was reached: " + message)
            raise AnthropicError(502, "upstream_error", "Anthropic failed: " + message)
    raise AnthropicError(502, "upstream_error", "Anthropic closed the stream before the answer was finished.")


# ── Sessions: record a conversation once, replay it exactly ───────────────
#
# The scripted route answers from rules somebody wrote, and the relay and
# Anthropic routes answer from a real model that says something different each
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

CASSETTE_DIR = os.environ.get("LLMSIM_CASSETTES", os.path.join(DATA_DIR, "cassettes"))
SESSION_LOCK = threading.RLock()
_STATE_TEMPLATE = {
    "mode": "off",          # off | record | replay
    "name": None,
    "entries": [],          # what has been captured, or what is loaded
    "index": {},            # replay: key -> the entries recorded for it, one per caller
    "since": None,
    "hits": 0, "misses": 0, "diverged": 0,
    "unmatched": [],        # requests replay had no answer for, newest first
    "masks": [],            # compiled patterns removed from an ask before it is compared
    "maskSources": [],
    "onMiss": "fail", "strict": False,   # this session's own; see session_start
}
MAX_MASKS, MAX_MASK_LENGTH, MAX_UNMATCHED = 20, 200, 50


def _new_state():
    """A session that is doing nothing."""
    return {k: (v.copy() if isinstance(v, (list, dict)) else v) for k, v in _STATE_TEMPLATE.items()}


# One session for requests that name no run, and one per run that has started its
# own. A request is handled under its run's session if that run has one, else
# under the default — so two agents under two runs can be recorded or replayed at
# the same time without seeing each other, and everything that never named a run
# behaves exactly as it always did.
SESSION = _new_state()
SESSIONS = {}


def _st(run=None):
    """The session for a run, or the default. A run with none is "off"."""
    if not run:
        return SESSION
    return SESSIONS.get(run) or _new_state()


def _states():
    return [SESSION] + list(SESSIONS.values())
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


def _mask(text, st):
    for rx in st["masks"]:
        text = rx.sub("<*>", text)
    return text


def _clean(text, st):
    return _mask(" ".join(_REMINDER.sub(" ", text or "").split()), st)


def _last_text(messages, st):
    """The final message in the conversation: what changed since the last turn."""
    if not messages:
        return ""
    last = messages[-1]
    if last.get("role") == "tool":
        content = last.get("content")
        if isinstance(content, list):
            content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
        return _clean(str(content or ""), st)
    return _clean(flatten(last)[0], st)


def exchange_key(messages, st=None):
    """(ask, turn, last, key) for a request. See the note above for why."""
    st = st or SESSION
    ask = _clean(subject(messages), st)
    turn = sum(1 for m in messages if m.get("role") == "assistant")
    last = _last_text(messages, st)
    key = hashlib.sha256(("%s\0%d" % (ask, turn)).encode()).hexdigest()[:16]
    return ask, turn, last, key


def _fingerprint(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _recorded_answer(turn):
    """The part of a turn worth keeping.

    An answer with no words and no tool call is kept like any other. It is how
    an agent's last turn often looks — the work is done, the model has nothing
    to add and stops — and a recording without it replays every step of a run
    and then refuses the request that ends it.
    """
    calls = [{"id": c.get("id") or rand_id("call_", 20), "name": c.get("name", ""), "input": c.get("input") or {}}
             for c in turn.get("tool_calls") or []]
    answer = {"text": turn.get("text") or "", "tool_calls": calls}
    usage = turn.get("usage")
    if usage:
        answer["usage"] = {"input": usage.get("input", 0), "output": usage.get("output", 0),
                           "cacheRead": usage.get("cacheRead", 0), "reasoning": usage.get("reasoning", 0)}
    return answer


def _flush_session(st):
    """The recording so far, on disk. Written after every exchange, so a
    session that is killed halfway still leaves everything up to that point."""
    os.makedirs(CASSETTE_DIR, exist_ok=True)
    path = _cassette_path(st["name"])
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"version": 1, "name": st["name"], "recordedAt": st["since"],
                   "masks": st["maskSources"], "entries": st["entries"]}, fh, indent=1)
    os.replace(path + ".tmp", path)


def session_capture(ctx, route, rule, turn, ms):
    """Keep one answered exchange, when a recording is running."""
    st = (ctx or {}).get("st") or SESSION
    if st["mode"] != "record" or not ctx:
        return
    answer = _recorded_answer(turn)
    ask, n, last, key = exchange_key(ctx["messages"], st)
    entry = {
        "key": key, "ask": ask, "turn": n, "last": _fingerprint(last), "lastText": last[:160],
        "model": ctx["model"], "provider": ctx["provider"], "shape": ctx["shape"],
        "route": route, "rule": rule, "tools": ctx["declared"], "at": time.time(), "ms": ms, "answer": answer,
    }
    with SESSION_LOCK:
        if st["mode"] != "record":
            return
        # The same ask at the same point from the same caller, again: a retry, or
        # the same step of a second run. The latest is kept, because replay can
        # only give one. A caller is told apart by the tools it declares — see
        # _closest for who asks twice at the same point, and why.
        st["entries"] = [e for e in st["entries"]
                         if not (e["key"] == key and e.get("tools") == entry["tools"])] + [entry]
        _flush_session(st)


def _closest(entries, declared):
    """Of the answers recorded for one ask at one point, the one for this caller.

    A harness does not only make the request it is there to make. OpenCode asks
    for a title for the conversation alongside the agent's first real request:
    the same ask, the same point, two different questions — one wants a tool
    call that writes a file, the other eight words to show in a sidebar. Which
    is which shows in the tools each declares, so both are kept and each is
    given back to the caller that declares the same ones.

    The nearest set rather than only an identical one, because a harness that
    gains a tool is still the same caller and should not be handed the title.
    """
    if len(entries) == 1 or declared is None:
        return entries[-1]
    want = set(declared)

    def near(entry):
        have = set(entry.get("tools") or [])
        return 1.0 if not (want | have) else len(want & have) / float(len(want | have))

    best = max(near(e) for e in entries)
    return [e for e in entries if near(e) == best][-1]


def session_replay(messages, st=None, declared=None):
    """(entry, differs) for a request the recording can answer, else None."""
    st = st or SESSION
    ask, n, last, key = exchange_key(messages, st)
    with SESSION_LOCK:
        held = st["index"].get(key)
        entry = _closest(held, declared) if held else None
        differs = bool(entry) and _fingerprint(last) != entry["last"]
        if entry and differs and st["strict"]:
            entry = None
            why = "the last message differs from the recording"
        else:
            why = "nothing was recorded for it"
        if not entry:
            st["misses"] += 1
            st["unmatched"] = ([{"ask": ask[:160], "turn": n, "why": why}] + st["unmatched"])[:MAX_UNMATCHED]
            return None
        st["hits"] += 1
        st["diverged"] += 1 if differs else 0
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


def _session_brief(st):
    return {"mode": st["mode"], "name": st["name"], "entries": len(st["entries"]), "hits": st["hits"],
            "misses": st["misses"], "diverged": st["diverged"], "since": st["since"]}


def session_status(run=None):
    """One session's status. With no run, the default session's, and a brief of
    every run that has a session of its own."""
    with SESSION_LOCK:
        st = _st(run)
        recent = [{"ask": e.get("ask", "")[:140], "turn": e.get("turn", 0), "route": e.get("route", ""),
                   "rule": e.get("rule", ""), "model": e.get("model", ""),
                   "tools": [c.get("name", "") for c in e.get("answer", {}).get("tool_calls", [])]}
                  for e in st["entries"][-25:]][::-1]
        out = {
            "run": run or "", "mode": st["mode"], "name": st["name"], "since": st["since"],
            "entries": len(st["entries"]), "recent": recent,
            "hits": st["hits"], "misses": st["misses"], "diverged": st["diverged"],
            "unmatched": st["unmatched"][:10], "masks": st["maskSources"],
            "onMiss": st["onMiss"], "strict": st["strict"],
            "settings": CONFIG["session"], "cassettes": list_cassettes(),
        }
        if not run:
            out["runs"] = {r: _session_brief(s_) for r, s_ in SESSIONS.items()}
        return out


# Every lifecycle call answers (ok, message, http status). The status is what a
# caller branches on — 400 it asked wrongly, 404 there is no such cassette, 409
# the state forbids it — so a test harness need not parse a sentence to know.

def _no(code, message):
    return False, message, code


def _name_in_use(name, by_recording_only=False, except_state=None):
    """Whether another live session is using this cassette. A cassette being
    recorded is taken: nobody else may record it, replay it or delete it. One being
    replayed may be replayed by others at the same time."""
    for st in _states():
        if st is except_state or st["mode"] == "off" or st["name"] != name:
            continue
        if st["mode"] == "record" or not by_recording_only:
            return st
    return None


def session_start(mode, name, masks=None, on_miss=None, strict=None, overwrite=False, run=None):
    """Begin recording or replaying. One mode at a time per session, and never over
    somebody's recording unless they said to.

    run scopes it: only requests that name that run (/run/<id>/…) are recorded or
    replayed, so several runs can each have their own at once. With no run it is
    the default session, which covers every request that names none.

    on_miss and strict belong to THIS replay. They default to the console's
    settings but are not those settings: a test that wants strictness for one
    run must not be able to change what the next run, or the console, does.
    """
    if not _cassette_name_ok(name):
        return _no(400, "A cassette name is letters, digits, dot, dash and underscore, and starts with a letter or digit.")
    if run is not None and not (isinstance(run, str) and ID_RE.fullmatch(run)):
        return _no(400, "A run id is letters, digits, dot, dash, underscore or colon, up to 64.")
    if on_miss not in (None, "fail", "route"):
        return _no(400, "onMiss must be fail or route.")
    if strict is not None and not isinstance(strict, bool):
        return _no(400, "strict must be true or false.")
    try:
        compiled, sources = _compile_masks(masks)
    except ValueError as exc:
        return _no(400, str(exc))
    with SESSION_LOCK:
        st = SESSIONS.get(run) if run else SESSION
        if st is not None and st["mode"] != "off":
            return _no(409, "A %s is already running%s (%s). Stop it first." % (
                "recording" if st["mode"] == "record" else "replay", " for run %s" % run if run else "", st["name"]))
        fresh = st is None
        if fresh:
            st = _new_state()
        exists = os.path.exists(_cassette_path(name))
        taken = _name_in_use(name, by_recording_only=(mode == "replay"))
        if taken is not None:
            return _no(409, "%s is being %s by another session. Use another name, or wait for it." % (
                name, "recorded" if taken["mode"] == "record" else "replayed"))
        if mode == "record":
            if exists and not overwrite:
                return _no(409, "A cassette called %s already exists. Pick another name, delete it, or record with overwrite." % name)
            st.update(mode="record", name=name, entries=[], index={}, since=time.time(), hits=0, misses=0,
                      diverged=0, unmatched=[], masks=compiled, maskSources=sources, onMiss="fail", strict=False)
            if run:
                SESSIONS[run] = st
            _flush_session(st)               # the file exists from the first moment, even if nothing is said
            log("session: recording to %s%s%s" % (_cassette_path(name), " (replacing it)" if exists else "",
                                                 " for run %s" % run if run else ""))
            return True, "Recording to %s. Everything answered%s from now on is kept." % (name, " for run %s" % run if run else ""), 200
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
        st.update(mode="replay", name=name, entries=data["entries"], since=time.time(), hits=0, misses=0,
                  diverged=0, unmatched=[], masks=compiled, maskSources=joined,
                  onMiss=on_miss or CONFIG["session"]["onMiss"],
                  strict=CONFIG["session"]["strict"] if strict is None else strict)
        # Keyed under THIS session's masks, not trusted from the file: the key
        # decides which request gets which answer.
        st["index"] = {}
        for e in data["entries"]:
            key = hashlib.sha256(("%s\0%d" % (_mask(e["ask"], st), e["turn"])).encode()).hexdigest()[:16]
            st["index"].setdefault(key, []).append(e)
        if run:
            SESSIONS[run] = st
        log("session: replaying %s%s (%d exchange(s), on miss: %s%s)" % (
            name, " for run %s" % run if run else "", len(data["entries"]), st["onMiss"], ", strict" if st["strict"] else ""))
        return True, "Replaying %s%s: %d recorded exchange(s)." % (name, " for run %s" % run if run else "", len(data["entries"])), 200


def session_auto(name, masks=None, on_miss=None, strict=None, run=None):
    """Replay the cassette if there is one, else record it: the first run of a
    test makes the recording and every run after it uses it."""
    if not _cassette_name_ok(name):
        return _no(400, "A cassette name is letters, digits, dot, dash and underscore, and starts with a letter or digit.")
    mode = "replay" if os.path.exists(_cassette_path(name)) else "record"
    return session_start(mode, name, masks=masks, on_miss=on_miss, strict=strict, run=run)


def session_stop(run=None):
    """(ok, message, code, summary). Everything a harness needs to assert on."""
    with SESSION_LOCK:
        st = SESSIONS.get(run) if run else SESSION
        if st is None or st["mode"] == "off":
            return False, ("Nothing is running for run %s." % run) if run else "Nothing is running.", 409, None
        was, name = st["mode"], st["name"]
        summary = {"run": run or "", "was": was, "name": name, "entries": len(st["entries"]),
                   "hits": st["hits"], "misses": st["misses"], "diverged": st["diverged"],
                   "unmatched": list(st["unmatched"]), "masks": list(st["maskSources"]),
                   "onMiss": st["onMiss"], "strict": st["strict"]}
        if run:
            SESSIONS.pop(run, None)
        else:
            st.update(mode="off", name=None, entries=[], index={}, since=None, masks=[], maskSources=[])
    log("session: %s of %s%s stopped" % ("recording" if was == "record" else "replay", name, " for run %s" % run if run else ""))
    message = ("Saved %d exchange(s) to %s." % (summary["entries"], name) if was == "record"
               else "Stopped replaying %s: %d answered, %d with no recording." % (name, summary["hits"], summary["misses"]))
    return True, message, 200, summary


def run_session_status(run):
    """What a run's own session is doing, or None when it has none."""
    with SESSION_LOCK:
        st = SESSIONS.get(run)
        return _session_brief(st) if st else None


def run_session_stop(run):
    """Stop a run's session if it has one. True when there was one."""
    return bool(SESSIONS.get(run)) and session_stop(run)[0]


def session_delete(name):
    if not _cassette_name_ok(name):
        return _no(400, "That is not a cassette name.")
    with SESSION_LOCK:
        if _name_in_use(name) is not None:
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
        if _name_in_use(name) is not None:
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


# ── Behaviors and evidence: the machinery they share ──────────────────────
#
# Four things sit on the same ground, and building them separately would have
# built that ground four times:
#
#   rules that branch      "if the tool said granted, carry on; if it said
#                           denied, stop" — a condition on the request
#   faults                  "answer 20% of requests with a dropped stream" — a
#                           condition on the request, and a thing to do
#   identity                "this request came from agent B of run r1" — which
#                           is a condition on the request too
#   assertions              "no request of run r1 may carry a card number to the
#                           model" — a check over the requests that were made
#
# So there is ONE description of a request (a *view*), ONE language for saying
# something about it (a *condition*), and everything else is built on those.
# A view is the same whether it is live, being matched against a rule, or a
# capture read back a week later to be asserted on.

ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")
# Never kept. A credential's fingerprint says which one arrived; the credential
# itself has no business being in a log or a capture.
SENSITIVE_HEADERS = ("authorization", "x-api-key", "cookie", "set-cookie", "proxy-authorization")
IDENTITY_FIELDS = ("run", "agent", "app")


# -- settings: defaults and what a caller may change -------------------------

def _settings_check(section, value):
    """(clean, error) for one settings section. Settings come from whoever
    calls the API, and a typo that silently becomes a new key is how a
    capture limit ends up unlimited."""
    if section == "capture":
        if not isinstance(value, dict):
            return None, "capture must be an object"
        out = json.loads(json.dumps(CONFIG["capture"]))
        for key in ("enabled",):
            if key in value:
                out[key] = bool(value[key])
        for key, low, high in (("max", 1, 20000), ("maxBytes", 1024, 16 * 1024 * 1024)):
            if key in value:
                try:
                    n = int(value[key])
                except (TypeError, ValueError):
                    return None, "capture.%s must be a number" % key
                if not low <= n <= high:
                    return None, "capture.%s must be between %d and %d" % (key, low, high)
                out[key] = n
        red = value.get("redact")
        if red is not None:
            if not isinstance(red, dict):
                return None, "capture.redact must be an object"
            if "headers" in red:
                if not isinstance(red["headers"], list) or not all(isinstance(h, str) for h in red["headers"]):
                    return None, "capture.redact.headers must be a list of header names"
                out["redact"]["headers"] = [h.lower() for h in red["headers"]][:50]
            if "patterns" in red:
                pats = red["patterns"]
                if not isinstance(pats, list) or len(pats) > 50:
                    return None, "capture.redact.patterns must be a list of at most 50"
                clean = []
                for p in pats:
                    if not isinstance(p, dict) or not isinstance(p.get("pattern"), str) or not p["pattern"] or len(p["pattern"]) > 300:
                        return None, "each redaction is {pattern, replace?} with a pattern of 1 to 300 characters"
                    try:
                        re.compile(p["pattern"])
                    except re.error as exc:
                        return None, "redaction pattern %r is not valid: %s" % (p["pattern"], exc)
                    clean.append({"pattern": p["pattern"], "replace": str(p.get("replace", "[redacted]"))[:100]})
                out["redact"]["patterns"] = clean
        return out, None
    if section == "identity":
        if not isinstance(value, dict) or not isinstance(value.get("rules", []), list) or len(value.get("rules", [])) > 30:
            return None, "identity is {rules: [...]} with at most 30 rules"
        rules = []
        for r in value.get("rules", []):
            if not isinstance(r, dict) or not ID_RE.fullmatch(str(r.get("field", ""))):
                return None, "an identity rule needs a field name (letters, digits, dot, dash, underscore)"
            if r.get("from") not in ("header", "body", "query", "credential"):
                return None, "an identity rule's from is header, body, query or credential"
            if r["from"] != "credential" and not isinstance(r.get("name"), str):
                return None, "an identity rule from %s needs a name" % r["from"]
            if r.get("pattern"):
                try:
                    if re.compile(r["pattern"]).groups < 1:
                        return None, "an identity pattern needs one capture group, which is the value"
                except re.error as exc:
                    return None, "identity pattern %r is not valid: %s" % (r["pattern"], exc)
            elif r["from"] == "credential":
                return None, "an identity rule from credential needs a pattern with one capture group"
            rules.append({k: r[k] for k in ("field", "from", "name", "pattern") if k in r})
        return {"rules": rules}, None
    if section == "auth":
        if not isinstance(value, dict):
            return None, "auth must be an object"
        out = json.loads(json.dumps(CONFIG["auth"]))
        if "mode" in value:
            if value["mode"] not in ("published", "registered", "any"):
                return None, "auth.mode is published, registered or any"
            out["mode"] = value["mode"]
        return out, None
    return None, "unknown settings section %r" % section


def settings_public():
    """Everything a caller may read: credentials as labels and fingerprints, never keys."""
    return {"capture": CONFIG["capture"], "identity": CONFIG["identity"],
            "auth": {"mode": CONFIG["auth"]["mode"], "credentials": CONFIG["auth"]["credentials"]},
            "session": CONFIG["session"]}


# -- credential: which one arrived -------------------------------------------

def credential_fingerprint(key):
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def credential_of(handler):
    """What a request presented, without keeping it: which header, a fingerprint,
    and whether LLMSim knows it.

    A fingerprint is enough to say "this is the platform token" or "this is a
    minted virtual key" once somebody has registered what each one is, and it
    cannot be turned back into the key.
    """
    key = handler.presented_key()
    if not key:
        return {"kind": "none", "fingerprint": "", "label": "", "known": False, "length": 0}
    kind = "x-api-key" if handler.headers.get("x-api-key") else "bearer"
    fp = credential_fingerprint(key)
    label = ""
    for provider, published in API_KEYS.items():
        if key == published:
            label = "published:" + provider
    if not label:
        for c in CONFIG["auth"]["credentials"]:
            if c["fingerprint"] == fp:
                label = c["label"]
    return {"kind": kind, "fingerprint": fp, "label": label, "known": bool(label), "length": len(key)}


def credential_accepted(cred):
    """Whether this credential may use the vendor routes.

    published   only the keys this file publishes — a wrong one is the vendor's
                own 401, so a misconfigured client fails the way it would for real
    registered  those, and any credential somebody has registered by fingerprint
    any         anything non-empty. For witnessing: a gateway mints its own keys
                and the question is WHICH one reached here, which a strict check
                would answer by refusing every one.
    """
    if cred["kind"] == "none":
        return False
    mode = CONFIG["auth"]["mode"]
    if mode == "any":
        return True
    if mode == "registered":
        return cred["known"]
    return cred["label"].startswith("published:")


# -- access: who may use the console and the API -----------------------------
#
# Off until a password is set, so a checkout, a test and every LLMSim installed
# before this keep working as they did. Set either where LLMSim is started, which
# gives the first sign-in:
#
#   LLMSIM_ADMIN_USER       the console's user name (default admin)
#   LLMSIM_ADMIN_PASSWORD   its password: the console asks for it once, then a cookie
#   LLMSIM_API_KEY          for scripts and CI: X-LLMSim-Key, or a bearer token
#
# or on the console's Settings › Sign-in and API key page, which replaces what the
# environment gave and keeps it in llmsim.access.json: the password as a PBKDF2
# hash and the key as a SHA-256 hash, so the file is no use to whoever reads it.
# A key made there is shown once.
# The password turns the sign-in on. The key is a second way in, for what cannot
# sign in; a key with no password is not used, as it would lock the console out.
#
# What it guards is the console and /api/…: everything that reads what was sent,
# changes what is answered, or spends a signed-in plan or a saved key. What it
# does not guard is what an agent under test calls, because an agent cannot be
# made to sign in: the vendors' routes (their own keys, LLMSIM_OPENAI_KEY and the
# rest), the test kit's stand-ins (/mcp, /mock, /a2a, /a2a-proxy, /otlp,
# /callbacks), the health check and the guide.

ENV_USER = os.environ.get("LLMSIM_ADMIN_USER") or ""
ENV_PASSWORD = os.environ.get("LLMSIM_ADMIN_PASSWORD") or ""
ENV_KEY = os.environ.get("LLMSIM_API_KEY") or ""
ACCESS_PATH = os.environ.get("LLMSIM_ACCESS") or os.path.join(DATA_DIR, "llmsim.access.json")
ACCESS = {"user": "", "password": "", "key": "", "keyHint": "", "changed": 0}
ACCESS_LOCK = threading.Lock()
PBKDF2_ROUNDS = 200000
MIN_PASSWORD = 8
SESSION_SECONDS = int(float(os.environ.get("LLMSIM_SESSION_DAYS") or 7) * 86400)
# Cookies are not kept apart by port, and two LLMSims on one machine are usual,
# so each one's cookie carries its port: signing in to one does not sign out the other.
SESSION_COOKIE = "llmsim_session_%d" % PORT
SECRET_PATH = os.environ.get("LLMSIM_SECRET") or os.path.join(DATA_DIR, "llmsim.secret")
# /api/auth says only whether a sign-in is needed and who this request is, which the sign-in itself needs to know.
OPEN_PATHS = ("/", "/healthz", "/guide", "/ui", "/ui/", "/login", "/logout", "/api/auth", "/favicon.ico")
SIGN_IN_FAILS = {}                     # client address -> (failures in a row, time of the last)
SIGN_IN_LOCK = threading.Lock()
SIGN_IN_LIMIT, SIGN_IN_WAIT_S = 5, 60
_SECRET = []


def load_access():
    try:
        with open(ACCESS_PATH) as fh:
            kept = json.load(fh)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        log("access: %s could not be read (%s); its password and key are not used" % (ACCESS_PATH, exc))
        return
    for name in ACCESS:
        if isinstance(kept.get(name), type(ACCESS[name])):
            ACCESS[name] = kept[name]


def save_access():
    os.makedirs(os.path.dirname(ACCESS_PATH) or ".", exist_ok=True)
    tmp = ACCESS_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(dict(ACCESS, _="Written by LLMSim's Settings page. Hashes only; owner-only."), fh, indent=1)
    os.replace(tmp, ACCESS_PATH)


def access_user():
    return ACCESS["user"] or ENV_USER or "admin"


def password_set():
    return bool(ENV_PASSWORD or ACCESS["password"])


def key_set():
    return bool(ENV_KEY or ACCESS["key"])


def access_on():
    return password_set()


def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ROUNDS, salt.hex(), digest.hex())


def _password_matches(password, stored):
    try:
        scheme, rounds, salt, digest = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
    except ValueError:
        return False
    return scheme == "pbkdf2_sha256" and hmac.compare_digest(got.hex(), digest)


def hash_key(key):
    return "sha256$" + hashlib.sha256(key.encode()).hexdigest()


def _session_secret():
    """The key cookies are signed with, made once and kept beside the rest, so a
    restart does not sign anybody out. Owner-only: whoever has it can mint a cookie."""
    if _SECRET:
        return _SECRET[0]
    try:
        with open(SECRET_PATH, "rb") as fh:
            secret = fh.read().strip()
    except FileNotFoundError:
        secret = b""
    if len(secret) < 32:
        secret = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=")
        os.makedirs(os.path.dirname(SECRET_PATH) or ".", exist_ok=True)
        fd = os.open(SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(secret + b"\n")
    _SECRET.append(secret)
    return secret


def _session_mac(user, expires):
    # The password is part of the key, so changing it signs out every cookie made with the old one.
    # A kept password is a hash with its own salt, which changes whenever the password does.
    stamp = ("kept\0%s\0%s" % (user, ACCESS["password"])) if ACCESS["password"] else ("env\0%s\0%s" % (user, ENV_PASSWORD))
    key = _session_secret() + hashlib.sha256(stamp.encode()).digest()
    return hmac.new(key, ("%s.%d" % (user, expires)).encode(), hashlib.sha256).hexdigest()


def session_cookie_for(user):
    expires = int(time.time()) + SESSION_SECONDS
    name = base64.urlsafe_b64encode(user.encode()).decode().rstrip("=")
    return "%s.%d.%s" % (name, expires, _session_mac(user, expires))


def session_user(value):
    """The user a cookie was made for, or None when it is not one of ours, has run out,
    or was made before the password changed."""
    if not password_set() or not value:
        return None
    try:
        name, expires, mac = value.split(".")
        user, expires = base64.urlsafe_b64decode(name + "=" * (-len(name) % 4)).decode(), int(expires)
    except (ValueError, UnicodeDecodeError):
        return None
    if expires < time.time() or user != access_user():
        return None
    return user if hmac.compare_digest(mac, _session_mac(user, expires)) else None


def access_key_ok(key):
    if not key or not access_on():
        return False
    # One made in Settings replaces LLMSIM_API_KEY: the environment's is the first, not the last.
    if ACCESS["key"]:
        return hmac.compare_digest(hash_key(key), ACCESS["key"])
    return bool(ENV_KEY) and hmac.compare_digest(key.encode(), ENV_KEY.encode())


def password_ok(user, password):
    if not password_set():
        return False
    # Both compared, always, so how long a refusal takes says nothing about which was wrong.
    same_user = hmac.compare_digest(str(user).encode(), access_user().encode())
    if ACCESS["password"]:
        same_password = _password_matches(str(password), ACCESS["password"])
    else:
        same_password = hmac.compare_digest(str(password).encode(), ENV_PASSWORD.encode())
    return same_user and same_password


def access_status():
    """What the Settings page shows. Never a password, never a whole key."""
    # "from" is the one in use; "env" says whether the environment gave one, which Settings has replaced or not.
    return {"on": access_on(), "user": access_user(), "userFrom": "env" if ENV_USER and not ACCESS["user"] else "settings",
            "password": {"set": password_set(), "from": "settings" if ACCESS["password"] else "env" if ENV_PASSWORD else None,
                         "env": bool(ENV_PASSWORD)},
            "key": {"set": key_set(), "from": "settings" if ACCESS["key"] else "env" if ENV_KEY else None, "env": bool(ENV_KEY),
                    "hint": ACCESS["keyHint"] if ACCESS["key"] else ("…" + ENV_KEY[-4:]) if ENV_KEY else ""},
            "changed": ACCESS["changed"] or None, "sessionDays": SESSION_SECONDS / 86400.0}


load_access()


def sign_in_wait(address):
    """Seconds this address must wait before it may try a password again; 0 when it may."""
    with SIGN_IN_LOCK:
        fails, last = SIGN_IN_FAILS.get(address, (0, 0))
    if fails < SIGN_IN_LIMIT:
        return 0
    return max(0, int(last + SIGN_IN_WAIT_S - time.time()))


def sign_in_result(address, ok):
    with SIGN_IN_LOCK:
        if ok:
            SIGN_IN_FAILS.pop(address, None)
        else:
            fails, _ = SIGN_IN_FAILS.get(address, (0, 0))
            SIGN_IN_FAILS[address] = (fails + 1, time.time())


def access_guarded(path):
    """Whether a path needs a sign-in or the key, once access is on. The path is the
    one left after a /run/…, /agent/… or provider prefix is taken off it. The console's
    page itself is not: it holds nothing, and it is where the sign-in is asked for."""
    if path in OPEN_PATHS:
        return False
    return path.startswith("/api/") or path == "/api"


# -- identity: who sent it ----------------------------------------------------

def _dig_path(value, path):
    """A dotted path into nested dicts and lists. None when it is not there."""
    for part in str(path).split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def identity_of(handler, body):
    """{run, agent, app, …} for a request, from where the caller put it.

    The reliable way is the base URL: /run/<id>/agent/<name>/openai/v1 needs
    nothing of an agent platform except that it can be pointed at a URL, which
    is the one thing every platform has to support. Headers and body fields are
    for a gateway that carries an identity through for you; they are configured,
    not assumed, because which header a given gateway forwards is a fact about
    that gateway.
    """
    found = dict(getattr(handler, "path_identity", {}) or {})
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    key = handler.presented_key()
    for rule in CONFIG["identity"]["rules"]:
        field = rule["field"]
        if field in found:
            continue
        source = rule["from"]
        if source == "header":
            raw = handler.headers.get(rule["name"])
        elif source == "body":
            raw = _dig_path(body, rule["name"]) if isinstance(body, dict) else None
        elif source == "query":
            raw = (query.get(rule["name"]) or [None])[0]
        else:
            raw = key
        if raw is None or isinstance(raw, (dict, list)):
            continue
        raw = str(raw)
        if rule.get("pattern"):
            m = re.search(rule["pattern"], raw)
            if not m:
                continue
            raw = m.group(1)
        if ID_RE.fullmatch(raw):
            found[field] = raw
    if "run" not in found:
        run = testkit.timeline.trace_run(handler.headers.get("traceparent"))
        if run:
            found["run"] = run
    if "run" not in found and body is not None:
        claimed = testkit.claim_for(handler, body)
        if claimed:
            found["run"] = claimed["run"]
            if claimed.get("agent") and "agent" not in found:
                found["agent"] = claimed["agent"]
            found["claimed"] = "yes"
    return found


# -- the view of a request ----------------------------------------------------

def _message_parts(messages):
    """[{role, text, tool}] — tool true when a message is a tool's output."""
    out = []
    for m in messages or []:
        role = m.get("role")
        text, as_tool = flatten(m) if role != "tool" else (_plain(m.get("content")), True)
        out.append({"role": role, "text": text, "tool": bool(as_tool or role == "tool")})
    return out


def _system_text(request, shape, messages):
    if shape == "anthropic":
        return _plain(request.get("system"))
    parts = []
    if shape == "responses" and request.get("instructions"):
        parts.append(_plain(request["instructions"]))
    raw = request.get("input") if shape == "responses" else request.get("messages")
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and item.get("role") in ("system", "developer"):
            parts.append(_plain(item.get("content")))
    return "\n\n".join(p for p in parts if p)


def build_view(meta, request, response=None):
    """The one description of a request that rules, faults and assertions all read.

    meta     what was around the request: provider, shape, model, path, stream,
             identity, credential, headers, route
    request  the body the client sent
    response what was answered, once there is an answer
    """
    shape = meta.get("shape") or "openai"
    request = request if isinstance(request, dict) else {}
    messages = responses_to_messages(request) if shape == "responses" else (request.get("messages") or [])
    if not isinstance(messages, list):
        messages = []
    parts = _message_parts(messages)
    declared = declared_tools(request)
    tool_outputs = [p["text"] for p in parts if p["tool"]]
    view = {
        "provider": meta.get("provider", ""), "shape": shape, "model": meta.get("model") or request.get("model") or "",
        "path": meta.get("path", ""), "stream": bool(meta.get("stream", request.get("stream"))),
        "route": meta.get("route", ""), "rule": meta.get("rule", ""),
        "identity": dict(meta.get("identity") or {}), "credential": dict(meta.get("credential") or {}),
        "headers": {k.lower(): v for k, v in (meta.get("headers") or {}).items()},
        "turn": sum(1 for m in messages if m.get("role") == "assistant"),
        "ask": subject(messages), "lastText": parts[-1]["text"] if parts else "",
        "lastToolOutput": last_tool_output(messages), "toolOutputs": tool_outputs,
        "system": _system_text(request, shape, messages),
        "userText": "\n".join(p["text"] for p in parts if p["role"] == "user" and not p["tool"]),
        "assistantText": "\n".join(p["text"] for p in parts if p["role"] == "assistant"),
        "allText": "\n".join(p["text"] for p in parts),
        "messageCount": len(parts), "messages": parts,
        "tools": sorted(declared), "toolSpecs": request.get("tools") or [],
    }
    r = response or {}
    view["response"] = {
        "status": r.get("status"), "text": r.get("text") or "", "toolCalls": r.get("toolCalls") or [],
        "toolNames": [c.get("name") for c in r.get("toolCalls") or []], "usage": r.get("usage") or {},
        "error": r.get("error") or "", "fault": r.get("fault") or "", "ms": r.get("ms"), "present": response is not None,
    }
    view["status"], view["fault"] = r.get("status"), r.get("fault") or ""
    return view


# -- conditions: one language ------------------------------------------------

def _text_ok(spec, text, label):
    """(ok, why). A string is 'contains'; an object may say more. Case does not
    matter unless {case: true}."""
    text = "" if text is None else str(text)
    if isinstance(spec, str):
        spec = {"contains": spec}
    if not isinstance(spec, dict):
        return False, "%s: a text condition is a string or an object" % label
    case = bool(spec.get("case"))
    flags = 0 if case else re.I
    hay = text if case else text.lower()
    for key, want in spec.items():
        if key == "case":
            continue
        try:
            if key == "contains":
                ok = (str(want) if case else str(want).lower()) in hay
            elif key == "not_contains":
                ok = (str(want) if case else str(want).lower()) not in hay
            elif key == "regex":
                ok = re.search(want, text, flags) is not None
            elif key == "not_regex":
                ok = re.search(want, text, flags) is None
            elif key == "equals":
                ok = hay == (str(want) if case else str(want).lower())
            elif key == "not_equals":
                ok = hay != (str(want) if case else str(want).lower())
            elif key == "empty":
                ok = (text == "") == bool(want)
            else:
                return False, "%s: unknown text condition %r" % (label, key)
        except re.error as exc:
            return False, "%s: %r is not a valid pattern (%s)" % (label, want, exc)
        if not ok:
            return False, "%s: %s %r" % (label, key, want if not isinstance(want, str) else want[:60])
    return True, ""


def _number_ok(spec, value, label):
    if isinstance(spec, (int, float)) and not isinstance(spec, bool):
        spec = {"eq": spec}
    if not isinstance(spec, dict):
        return False, "%s: a number condition is a number or {min, max}" % label
    if value is None:
        return False, "%s: there is no value" % label
    for key, want in spec.items():
        ok = {"eq": value == want, "ne": value != want, "min": value >= want, "max": value <= want,
              "gt": value > want, "lt": value < want}.get(key)
        if ok is None:
            return False, "%s: unknown number condition %r" % (label, key)
        if not ok:
            return False, "%s is %s, wanted %s %s" % (label, value, key, want)
    return True, ""


def _field_op(view, field, op, want, case):
    """(ok, why) for {field, op, value}."""
    got = _dig_path(view, field)
    label = "%s" % field
    present = got not in (None, "", [], {})
    if op == "exists":
        want_present = True if want is None else bool(want)
        return present == want_present, "%s is %s" % (label, "present" if present else "missing")
    if op == "not_exists":
        return not present, "%s is present" % label
    if isinstance(got, list):
        low = [str(x).lower() for x in got] if not case else [str(x) for x in got]
        w = (str(want) if case else str(want).lower()) if not isinstance(want, list) else [str(x) if case else str(x).lower() for x in want]
        if op in ("includes", "contains"):
            return w in low, "%s does not include %r" % (label, want)
        if op in ("excludes", "not_contains"):
            return w not in low, "%s includes %r" % (label, want)
        if op == "includes_all":
            return all(x in low for x in w), "%s is missing some of %r" % (label, want)
        if op == "includes_any":
            return any(x in low for x in w), "%s has none of %r" % (label, want)
        if op == "equals" or op == "eq":
            return sorted(low) == sorted(w if isinstance(w, list) else [w]), "%s is %r, not %r" % (label, got, want)
        if op == "empty":
            return (not got) == bool(want if want is not None else True), "%s is %r" % (label, got)
        if op in ("len", "count"):
            return _number_ok(want, len(got), "length of " + label)
        return False, "%s is a list; %r does not apply" % (label, op)
    if op in ("gt", "gte", "lt", "lte", "min", "max"):
        try:
            number = float(got)
        except (TypeError, ValueError):
            return False, "%s is not a number" % label
        return _number_ok({{"gte": "min", "lte": "max"}.get(op, op): want}, number, label)
    if op in ("eq", "ne"):
        same = got == want or str(got).lower() == str(want).lower() if not case else got == want
        return (same if op == "eq" else not same), "%s is %r" % (label, got)
    if op in ("in", "not_in"):
        pool = want if isinstance(want, list) else [want]
        hit = got in pool or (not case and str(got).lower() in [str(x).lower() for x in pool])
        return (hit if op == "in" else not hit), "%s is %r" % (label, got)
    spec = {"contains": "contains", "not_contains": "not_contains", "regex": "regex", "not_regex": "not_regex",
            "equals": "equals", "not_equals": "not_equals", "empty": "empty"}.get(op)
    if spec is None:
        return False, "unknown operator %r" % op
    return _text_ok({spec: want, "case": case}, got if isinstance(got, str) else ("" if got is None else json.dumps(got) if isinstance(got, (dict, list)) else str(got)), label)


def cond_result(cond, view):
    """(ok, why). Every key must hold. The same function answers a rule's `when`,
    a fault's `match`, an assertion's scope and its checks, so a thing learned in
    one place is true in all of them."""
    if not isinstance(cond, dict):
        return False, "a condition is an object"
    for key, want in cond.items():
        if key in ("op", "value", "case"):
            continue
        if key == "field":
            ok, why = _field_op(view, want, cond.get("op", "eq"), cond.get("value"), bool(cond.get("case")))
        else:
            ok, why = _one_cond(key, want, view)
        if not ok:
            return False, why
    return True, ""


def _one_cond(key, want, view):
    if key == "not":
        ok, _ = cond_result(want, view)
        return (not ok), "not: the condition held"
    if key == "all":
        for sub in want if isinstance(want, list) else []:
            ok, why = cond_result(sub, view)
            if not ok:
                return False, why
        return True, ""
    if key == "any":
        reasons = []
        for sub in want if isinstance(want, list) else []:
            ok, why = cond_result(sub, view)
            if ok:
                return True, ""
            reasons.append(why)
        return False, "any: none held (%s)" % "; ".join(reasons[:3])
    if key in ("provider", "shape", "route"):
        return _text_ok(want if isinstance(want, dict) else {"equals": want}, view.get(key), key)
    if key == "path":
        return _text_ok(want if isinstance(want, dict) else {"contains": want}, view.get("path"), "path")
    if key == "stream":
        return view.get("stream") == bool(want), "stream is %s" % view.get("stream")
    if key == "model":
        # A string is a substring, which is what rules have always meant by it.
        return _text_ok(want if isinstance(want, dict) else {"contains": want}, view.get("model"), "model")
    if key in ("tool", "tools"):
        wanted = want if isinstance(want, list) else [want]
        have = {t.lower() for t in view.get("tools") or []}
        missing = [t for t in wanted if str(t).lower() not in have]
        return (not missing), "the client did not declare %s" % ", ".join(map(str, missing))
    if key == "contains":
        return _text_ok({"contains": want}, view.get("ask"), "the ask")
    if key == "regex":
        return _text_ok({"regex": want}, view.get("ask"), "the ask")
    if key == "turn":
        return _number_ok(want, view.get("turn"), "turn")
    if key == "toolOutput":
        return _text_ok(want, view.get("lastToolOutput"), "the last tool output")
    if key == "anyToolOutput":
        outputs = view.get("toolOutputs") or []
        for out in outputs:
            if _text_ok(want, out, "")[0]:
                return True, ""
        return False, "no tool output satisfied %s" % json.dumps(want)[:60]
    if key == "lastMessage":
        return _text_ok(want, view.get("lastText"), "the last message")
    if key == "anyMessage":
        # Anything said earlier in the conversation, by the user or the model: what an agent
        # that remembers carries forward, and one that forgets does not.
        for part in view.get("messages") or []:
            if not part.get("tool") and _text_ok(want, part.get("text"), "")[0]:
                return True, ""
        return False, "no message in the conversation satisfied %s" % json.dumps(want)[:60]
    if key == "system":
        return _text_ok(want, view.get("system"), "the system prompt")
    if key == "identity":
        for field, spec in (want if isinstance(want, dict) else {}).items():
            ok, why = _text_ok(spec if isinstance(spec, dict) else {"equals": spec}, (view.get("identity") or {}).get(field), "identity." + field)
            if not ok:
                return False, why
        return True, ""
    if key == "header":
        for name, spec in (want if isinstance(want, dict) else {}).items():
            ok, why = _text_ok(spec if isinstance(spec, dict) else {"equals": spec}, (view.get("headers") or {}).get(name.lower()), "header " + name)
            if not ok:
                return False, why
        return True, ""
    if key == "credential":
        cred = view.get("credential") or {}
        for field, spec in (want if isinstance(want, dict) else {}).items():
            if field == "known":
                if bool(cred.get("known")) != bool(spec):
                    return False, "credential.known is %s" % cred.get("known")
                continue
            ok, why = _text_ok(spec if isinstance(spec, dict) else {"equals": spec}, cred.get(field), "credential." + field)
            if not ok:
                return False, why
        return True, ""
    if key in ("status", "fault"):
        if not view["response"]["present"]:
            return False, "there is no response yet"
        if key == "status":
            return _number_ok(want, view.get("status"), "status")
        return _text_ok(want if isinstance(want, dict) else {"equals": want}, view.get("fault"), "fault")
    return False, "unknown condition %r" % key


COND_KEYS = {"not", "all", "any", "provider", "shape", "route", "path", "stream", "model", "tool", "tools", "contains",
             "regex", "turn", "toolOutput", "anyToolOutput", "lastMessage", "anyMessage", "system", "identity", "header", "credential",
             "status", "fault", "field", "op", "value", "case"}


def validate_cond(cond, where="when", depth=0):
    """A list of problems with a condition, found when it is saved and not when
    it first fails to match something in the middle of a run."""
    problems = []
    if not isinstance(cond, dict):
        return ["%s must be an object" % where]
    if depth > 6:
        return ["%s is nested too deeply" % where]
    for key, want in cond.items():
        if key not in COND_KEYS:
            problems.append("%s: unknown condition %r" % (where, key))
            continue
        if key in ("regex",) and isinstance(want, str):
            try:
                re.compile(want)
            except re.error as exc:
                problems.append("%s.regex is not a valid pattern: %s" % (where, exc))
        if key == "not":
            problems += validate_cond(want, where + ".not", depth + 1)
        if key in ("all", "any"):
            if not isinstance(want, list):
                problems.append("%s.%s must be a list" % (where, key))
            else:
                for i, sub in enumerate(want):
                    problems += validate_cond(sub, "%s.%s[%d]" % (where, key, i), depth + 1)
        if key in ("toolOutput", "anyToolOutput", "lastMessage", "anyMessage", "system") and isinstance(want, dict):
            for sk, sv in want.items():
                if sk in ("regex", "not_regex") and isinstance(sv, str):
                    try:
                        re.compile(sv)
                    except re.error as exc:
                        problems.append("%s.%s.%s is not a valid pattern: %s" % (where, key, sk, exc))
                elif sk not in ("contains", "not_contains", "regex", "not_regex", "equals", "not_equals", "empty", "case"):
                    problems.append("%s.%s: unknown text condition %r" % (where, key, sk))
        if key == "field":
            if not isinstance(want, str) or not want:
                problems.append("%s.field must be a path such as system or response.text" % where)
            op = cond.get("op", "eq")
            known = {"eq", "ne", "equals", "not_equals", "contains", "not_contains", "regex", "not_regex", "in", "not_in", "gt",
                     "gte", "lt", "lte", "min", "max", "exists", "not_exists", "includes", "excludes", "includes_all",
                     "includes_any", "empty", "len", "count"}
            if op not in known:
                problems.append("%s.op %r is not an operator" % (where, op))
            if op in ("regex", "not_regex") and isinstance(cond.get("value"), str):
                try:
                    re.compile(cond["value"])
                except re.error as exc:
                    problems.append("%s.value is not a valid pattern: %s" % (where, exc))
    return problems


# ── Captures: what each request was, and what answered it ─────────────────
#
# Kept in memory, bounded, and never written to disk: a capture holds what an
# agent sent to a model, which is exactly the thing worth looking at and exactly
# the thing not to leave lying around. Assertions run on what is held, so
# "no request carried a card number" is checked against the request as it was,
# not as it is shown. What is shown, exported or returned over the API has the
# redactions applied on the way out.

CAPTURES = collections.deque(maxlen=300)
CAPTURE_LOCK = threading.Lock()
CAPTURE_SEQ = itertools.count(1)


def _resize_captures():
    """Apply a new capture limit, keeping the newest."""
    global CAPTURES
    with CAPTURE_LOCK:
        CAPTURES = collections.deque(list(CAPTURES)[-CONFIG["capture"]["max"]:], maxlen=CONFIG["capture"]["max"])


def _compact_request(view, shape_budget):
    """A request too big to keep whole, reduced to what assertions read.

    Each message is cut to its share of the budget. The result is a Chat
    Completions-shaped request, so a view of it reads exactly as one of the
    original would — and it is marked, so an assertion can say it was made on a
    truncated request rather than pass or fail as if it had seen everything.
    """
    share = max(256, shape_budget // max(1, view["messageCount"] + 1))
    messages = [{"role": "system", "content": view["system"][:share]}] if view["system"] else []
    for part in view["messages"]:
        role = "tool" if part["tool"] else part["role"]
        messages.append({"role": role, "content": part["text"][:share]})
    return {"model": view["model"], "stream": view["stream"], "messages": messages,
            "tools": [{"type": "function", "function": {"name": t}} for t in view["tools"]]}


def capture_begin(handler, body, shape, model, stream):
    """Start a capture for this request. None when capture is off or the request
    is LLMSim's own runner, whose requests are no part of anything being observed."""
    if not CONFIG["capture"]["enabled"] or getattr(handler, "internal", False):
        return None
    headers = {k.lower(): v for k, v in handler.headers.items() if k.lower() not in SENSITIVE_HEADERS}
    return {"at": time.time(), "meta": {
        "provider": handler.provider, "shape": shape, "model": model, "path": handler.path.split("?")[0],
        "stream": stream, "identity": getattr(handler, "identity", {}), "credential": getattr(handler, "cred", {}),
        "headers": headers, "route": "", "rule": ""}, "request": body, "response": {}}


def _usage_of(turn):
    """The token counts a turn reports, in one shape, or {} when it reports none."""
    u = turn.get("usage") or {}
    if "input_tokens" in turn or "output_tokens" in turn:
        return {"input": turn.get("input_tokens", 0), "output": turn.get("output_tokens", 0),
                "cacheRead": turn.get("cached_tokens", 0), "reasoning": turn.get("reasoning_tokens", 0)}
    return {"input": u.get("input", 0), "output": u.get("output", 0), "cacheRead": u.get("cacheRead", 0),
            "reasoning": u.get("reasoning", 0)} if u else {}


def usage_from_body(shape, body):
    """The token counts a built answer carries, in one shape.

    Read from the answer itself rather than from the turn, because a scripted turn
    names no counts and the builders make them up as they build — and what a
    capture should hold is what was actually sent.
    """
    u = body.get("usage") or {}
    if shape == "anthropic":
        cached = u.get("cache_read_input_tokens", 0)
        return {"input": u.get("input_tokens", 0) + cached + u.get("cache_creation_input_tokens", 0),
                "output": u.get("output_tokens", 0), "cacheRead": cached, "reasoning": 0}
    if shape == "responses":
        return {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
                "cacheRead": (u.get("input_tokens_details") or {}).get("cached_tokens", 0),
                "reasoning": (u.get("output_tokens_details") or {}).get("reasoning_tokens", 0)}
    return {"input": u.get("prompt_tokens", 0), "output": u.get("completion_tokens", 0),
            "cacheRead": (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            "reasoning": (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)}


def capture_note(handler, **fields):
    """Add to what is known about the answer. Safe to call when there is no capture."""
    cap = getattr(handler, "cap", None)
    if cap is None:
        return
    for key in ("route", "rule"):
        if key in fields:
            cap["meta"][key] = fields.pop(key)
    cap["response"].update({k: v for k, v in fields.items() if v is not None})


def capture_finish(handler):
    cap = getattr(handler, "cap", None)
    handler.cap = None
    if cap is None:
        return
    limit = CONFIG["capture"]["maxBytes"]
    try:
        raw = json.dumps(cap["request"])
    except (TypeError, ValueError):
        raw = ""
    size = len(raw)
    cap["requestBytes"] = size
    cap["truncated"] = False
    if size > limit:
        view = build_view(cap["meta"], cap["request"])
        cap["request"] = _compact_request(view, limit)
        cap["meta"]["shape"] = "openai"
        cap["truncated"] = True
    cap["response"].setdefault("status", getattr(handler, "_status", None))
    cap["response"]["ms"] = int((time.time() - cap["at"]) * 1000)
    cap["n"] = next(CAPTURE_SEQ)
    with CAPTURE_LOCK:
        CAPTURES.append(cap)
    try:
        testkit.on_capture(cap)         # the run's timeline, kept on disk
    except Exception as exc:  # noqa: BLE001 — watching a request must never break it
        log("testkit: %s: %s" % (type(exc).__name__, exc))


def capture_view(cap):
    """The view of a held capture — the same shape a live request has, plus its answer."""
    view = build_view(cap["meta"], cap["request"], cap["response"])
    view["n"], view["at"], view["truncated"] = cap["n"], cap["at"], cap["truncated"]
    return view


def _redact_value(value, patterns):
    if isinstance(value, str):
        for p in patterns:
            value = re.sub(p["pattern"], p["replace"], value)
        return value
    if isinstance(value, list):
        return [_redact_value(v, patterns) for v in value]
    if isinstance(value, dict):
        return {k: _redact_value(v, patterns) for k, v in value.items()}
    return value


def capture_public(cap, redact=True):
    """A capture as it may be shown: redactions applied, and nothing a header
    list says should be kept out."""
    out = json.loads(json.dumps({k: cap[k] for k in ("n", "at", "meta", "request", "response", "requestBytes", "truncated")}))
    if redact:
        drop = set(CONFIG["capture"]["redact"]["headers"])
        out["meta"]["headers"] = {k: v for k, v in out["meta"]["headers"].items() if k not in drop}
        patterns = CONFIG["capture"]["redact"]["patterns"]
        if patterns:
            out["request"] = _redact_value(out["request"], patterns)
            out["response"] = _redact_value(out["response"], patterns)
    return out


def capture_summary(cap):
    view = capture_view(cap)
    ident = cap["meta"].get("identity") or {}
    return {"n": cap["n"], "at": cap["at"], "run": ident.get("run", ""), "agent": ident.get("agent", ""),
            "app": ident.get("app", ""), "provider": view["provider"], "model": view["model"], "route": view["route"],
            "rule": view["rule"], "status": view["status"], "ms": view["response"]["ms"], "turn": view["turn"],
            "ask": view["ask"][:200], "tools": view["tools"], "calls": view["response"]["toolNames"],
            "fault": view["fault"], "credential": cap["meta"].get("credential", {}).get("label", ""),
            "truncated": cap["truncated"]}


def captures_matching(scope=None, limit=None, since=0):
    """Captures (oldest first) whose view satisfies a condition."""
    with CAPTURE_LOCK:
        held = list(CAPTURES)
    out = []
    for cap in held:
        if cap["n"] <= since:
            continue
        if scope and not cond_result(scope, capture_view(cap))[0]:
            continue
        out.append(cap)
    return out[-limit:] if limit else out


def runs_overview():
    """The runs seen so far, derived from what was captured and what is running."""
    runs = {}
    with CAPTURE_LOCK:
        held = list(CAPTURES)
    for cap in held:
        ident = cap["meta"].get("identity") or {}
        run = ident.get("run")
        if not run:
            continue
        r = runs.setdefault(run, {"run": run, "requests": 0, "agents": set(), "apps": set(), "first": cap["at"], "last": cap["at"]})
        r["requests"] += 1
        r["first"], r["last"] = min(r["first"], cap["at"]), max(r["last"], cap["at"])
        if ident.get("agent"):
            r["agents"].add(ident["agent"])
        if ident.get("app"):
            r["apps"].add(ident["app"])
    return sorted(({**r, "agents": sorted(r["agents"]), "apps": sorted(r["apps"])} for r in runs.values()),
                  key=lambda r: -r["last"])


def run_base_urls(host, run, agents=None):
    """The base URLs to give an agent so its traffic arrives under this run.

    This is what an agent platform needs and the only thing it needs: a URL it
    can be pointed at. Identity rides in the path, so it works for a platform
    that offers no way to add a header.
    """
    def urls(prefix):
        return {"anthropic": "http://%s%s/anthropic" % (host, prefix), "openai": "http://%s%s/openai/v1" % (host, prefix),
                "deepseek": "http://%s%s/deepseek/v1" % (host, prefix)}
    out = {"run": run, "baseUrls": urls("/run/" + run)}
    if agents:
        out["agents"] = {a: urls("/run/%s/agent/%s" % (run, a)) for a in agents}
    return out


# ── The REST surface ──────────────────────────────────────────────────────
#
# One dispatcher, so the checks that every one of these routes needs — refusing
# a request that a browser made on behalf of another site, answering in one
# shape, returning a status that says what went wrong — are written once. The
# console is a client of this API like any other: whatever a person can do on a
# page, a harness can do over HTTP.
#
#   settings     GET/PUT /api/settings · POST /api/settings/credentials · DELETE …/<label>
#   captures     GET /api/captures · GET /api/captures/<n> · GET …/export · DELETE /api/captures
#   runs         GET/POST /api/runs · GET/DELETE /api/runs/<run>
#   rules        /api/rules …
#   faults       /api/faults …
#   assertions   /api/assertions/evaluate · /api/assertion-sets …

REST_PREFIXES = ("/api/settings", "/api/captures", "/api/runs", "/api/rules", "/api/routing", "/api/faults", "/api/assertions",
                 "/api/assertion-sets")


def _fail(code, message, **extra):
    return code, dict({"ok": False, "message": message}, **extra)


def _ok(payload=None, **extra):
    return 200, dict({"ok": True}, **(payload or {}), **extra)


def _int_arg(query, name, default, low, high):
    try:
        n = int((query.get(name) or [default])[0])
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


# -- settings -----------------------------------------------------------------

def _rest_settings(method, rest, body, query, host):
    if method == "GET" and not rest:
        return _ok(settings_public())
    if method == "PUT" and not rest:
        if not isinstance(body, dict):
            return _fail(400, "The body must be an object of settings sections.")
        staged = {}
        for section, value in body.items():
            if section not in ("capture", "identity", "auth"):
                return _fail(400, "Unknown settings section %r. The sections are capture, identity and auth." % section)
            clean, error = _settings_check(section, value)
            if error:
                return _fail(400, error)
            staged[section] = clean
        CONFIG.update(staged)
        if "capture" in staged:
            _resize_captures()
        save_config()
        return _ok(settings_public())
    if method == "POST" and rest == ["credentials"]:
        label, key = (body or {}).get("label"), (body or {}).get("key")
        if not isinstance(label, str) or not ID_RE.fullmatch(label) or label.startswith("published:"):
            return _fail(400, "A credential's label is letters, digits, dot, dash, underscore or colon, and not 'published:…'.")
        if not isinstance(key, str) or not key or len(key) > 1000:
            return _fail(400, "Give the credential to register as key. It is fingerprinted and not kept.")
        fp = credential_fingerprint(key)
        kept = [c for c in CONFIG["auth"]["credentials"] if c["label"] != label and c["fingerprint"] != fp]
        CONFIG["auth"]["credentials"] = kept + [{"label": label, "fingerprint": fp}]
        save_config()
        return _ok(settings_public(), message="Registered %s as %s. The key itself is not stored." % (fp, label))
    if method == "DELETE" and len(rest) == 2 and rest[0] == "credentials":
        before = len(CONFIG["auth"]["credentials"])
        CONFIG["auth"]["credentials"] = [c for c in CONFIG["auth"]["credentials"] if c["label"] != rest[1]]
        if len(CONFIG["auth"]["credentials"]) == before:
            return _fail(404, "There is no credential called %s." % rest[1])
        save_config()
        return _ok(settings_public())
    return _fail(404, "No such route: %s /api/settings/%s" % (method, "/".join(rest)))


# -- captures -----------------------------------------------------------------

def _capture_scope(query):
    """The filter a captures query asks for, as a condition — so the API filters
    with the same language everything else does."""
    scope = {}
    ident = {k: (query.get(k) or [None])[0] for k in IDENTITY_FIELDS if (query.get(k) or [None])[0]}
    if ident:
        scope["identity"] = ident
    for key in ("route", "provider"):
        if (query.get(key) or [None])[0]:
            scope[key] = (query[key])[0]
    if (query.get("model") or [None])[0]:
        scope["model"] = query["model"][0]
    if (query.get("status") or [None])[0]:
        try:
            scope["status"] = int(query["status"][0])
        except ValueError:
            pass
    q = (query.get("q") or [None])[0]
    if q:
        scope["any"] = [{"contains": q}, {"field": "response.text", "op": "contains", "value": q},
                        {"field": "allText", "op": "contains", "value": q}]
    return scope


def _rest_captures(method, rest, body, query, host):
    if method == "GET" and not rest:
        found = captures_matching(_capture_scope(query), limit=_int_arg(query, "limit", 100, 1, 1000),
                                  since=_int_arg(query, "since", 0, 0, 10 ** 12))
        with CAPTURE_LOCK:
            total = len(CAPTURES)
        return _ok({"captures": [capture_summary(c) for c in found][::-1], "seq": found[-1]["n"] if found else 0,
                    "total": total, "capacity": CONFIG["capture"]["max"], "enabled": CONFIG["capture"]["enabled"]})
    if method == "GET" and rest == ["export"]:
        found = captures_matching(_capture_scope(query))
        return 200, {"version": 1, "exportedAt": time.time(), "redacted": True,
                     "captures": [capture_public(c) for c in found]}, {"download": "llmsim-captures.json"}
    if method == "GET" and len(rest) == 1 and rest[0].isdigit():
        with CAPTURE_LOCK:
            cap = next((c for c in CAPTURES if c["n"] == int(rest[0])), None)
        if not cap:
            return _fail(404, "Capture %s is not held. It may have been pushed out by newer ones." % rest[0])
        view = capture_view(cap)
        return _ok({"capture": capture_public(cap), "view": {k: view[k] for k in (
            "system", "ask", "lastText", "lastToolOutput", "tools", "turn", "identity", "credential", "response", "messageCount")}})
    if method == "DELETE" and not rest:
        scope = _capture_scope(query)
        with CAPTURE_LOCK:
            before = len(CAPTURES)
            if scope:
                keep = [c for c in CAPTURES if not cond_result(scope, capture_view(c))[0]]
                CAPTURES.clear()
                CAPTURES.extend(keep)
            else:
                CAPTURES.clear()
            removed = before - len(CAPTURES)
        return _ok(removed=removed)
    return _fail(404, "No such route: %s /api/captures/%s" % (method, "/".join(rest)))


# -- runs ---------------------------------------------------------------------

def _rest_runs(method, rest, body, query, host):
    kit = testkit.run_sub(method, rest, body, query)
    if kit is not None:
        return kit
    if method == "GET" and not rest:
        return _ok({"runs": runs_overview(), "stored": testkit.store_runs()})
    if method == "POST" and not rest:
        body = body or {}
        run = body.get("run") or "r-" + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))
        if not isinstance(run, str) or not ID_RE.fullmatch(run):
            return _fail(400, "A run id is letters, digits, dot, dash, underscore or colon, up to 64.")
        agents = body.get("agents") or []
        if not isinstance(agents, list) or not all(isinstance(a, str) and ID_RE.fullmatch(a) for a in agents) or len(agents) > 50:
            return _fail(400, "agents is a list of names (letters, digits, dot, dash, underscore).")
        return _ok(run_base_urls(host, run, agents))
    if method == "GET" and len(rest) == 1:
        found = [c for c in captures_matching({"identity": {"run": rest[0]}})]
        overview = next((r for r in runs_overview() if r["run"] == rest[0]), None)
        if not overview and not found:
            return _fail(404, "Nothing has been seen for run %s." % rest[0])
        return _ok({"run": overview, "captures": [capture_summary(c) for c in found][::-1][:200],
                    "sessions": run_session_status(rest[0])})
    if method == "DELETE" and len(rest) == 1:
        with CAPTURE_LOCK:
            before = len(CAPTURES)
            keep = [c for c in CAPTURES if (c["meta"].get("identity") or {}).get("run") != rest[0]]
            CAPTURES.clear()
            CAPTURES.extend(keep)
            removed = before - len(CAPTURES)
        stopped = run_session_stop(rest[0])
        return _ok(removed=removed, sessionStopped=stopped, storedRemoved=testkit.forget_run(rest[0]))
    return _fail(404, "No such route: %s /api/runs/%s" % (method, "/".join(rest)))


REST_AREAS = {"settings": _rest_settings, "captures": _rest_captures, "runs": _rest_runs}


def rest_dispatch(method, path, body, query, host):
    """(status, payload[, headers]) for any of the routes above."""
    segs = [s for s in path.split("/") if s][1:]
    if not segs or segs[0] not in REST_AREAS:
        return _fail(404, "No such route: %s %s" % (method, path))
    try:
        return REST_AREAS[segs[0]](method, segs[1:], body, query, host)
    except Exception as exc:  # noqa: BLE001 — a bug here must be a message, not a dropped connection
        log("api error on %s %s: %s: %s" % (method, path, type(exc).__name__, exc))
        return _fail(500, "%s: %s" % (type(exc).__name__, exc))


# ── Rules that branch ─────────────────────────────────────────────────────
#
# A rule is `when` a request satisfies a condition, answer with these `turns`.
# That has not changed. What has: `when` is the one condition language every other
# part of this file uses, so a rule can say what it always could — the ask, the
# provider, the tools the client declared — and now also what the LAST TOOL SAID,
# which turn this is, who is asking, and what the system prompt contained.
#
# That is what makes an agent's conditional flows testable. "The grant came back
# approved, so carry on; it came back denied, so stop" is two rules that differ in
# one condition, or one turn with two branches:
#
#   {"name": "needs-capability",
#    "when": {"contains": "reconcile"},
#    "turns": [
#      {"text": "Checking what I am granted.", "tool_calls": [{"name": "granted", "input": {}}]},
#      {"branches": [
#        {"when": {"toolOutput": {"contains": "SAP.Invoice.Read"}}, "turn": {"text": "Granted — reading."}},
#        {"when": {"toolOutput": {"contains": "[]"}},               "turn": {"text": "Nothing granted. Stopping."}}]}]}
#
# Shipped rules live in responses.json, which is source and is read, not written.
# Rules made through the console or the API live in llmsim.rules.json, beside it,
# which is also meant to be committed, and are tried FIRST so one can override a
# shipped rule without editing it.

RULES_PATH = os.environ.get("LLMSIM_RULES", os.path.join(DATA_DIR, "llmsim.rules.json"))
RULES_LOCK = threading.RLock()
USER_RULES = {"rules": [], "default": None}
TURN_KEYS = {"text", "tool_calls", "stop_reason", "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens",
             "delay_ms", "error", "fault", "branches", "description"}
RULE_KEYS = {"name", "description", "when", "turns", "enabled"}
MAX_TURNS, MAX_BRANCHES = 200, 20


def load_user_rules():
    try:
        with open(RULES_PATH) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"rules": [], "default": None}
    except (ValueError, OSError) as exc:
        log("rules: cannot read %s (%s) — starting without them" % (RULES_PATH, exc))
        return {"rules": [], "default": None}
    rules = [r for r in data.get("rules", []) if isinstance(r, dict) and isinstance(r.get("turns"), list)]
    return {"rules": rules, "default": data.get("default") if isinstance(data.get("default"), dict) else None}


def save_user_rules():
    tmp = RULES_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"version": 1, "_": "LLMSim rules made in the console or over the API. Tried before responses.json. "
                                       "Commit this.", "rules": USER_RULES["rules"], "default": USER_RULES["default"]},
                  fh, indent=1)
    os.replace(tmp, RULES_PATH)


def all_rules():
    """[(rule, source)] in the order they are tried: user rules, then shipped."""
    with RULES_LOCK:
        mine = [(r, "user") for r in USER_RULES["rules"]]
    return mine + [(r, "shipped") for r in SCRIPT.get("rules", [])]


def default_turn():
    with RULES_LOCK:
        mine = USER_RULES["default"]
    return mine or SCRIPT.get("default", {"text": "LLMSim has no scripted answer for that."})


def validate_turn(turn, where="turn", depth=0):
    """Problems with one scripted turn, found when it is saved."""
    if not isinstance(turn, dict):
        return ["%s must be an object" % where]
    if depth > 4:
        return ["%s: branches are nested too deeply" % where]
    problems = ["%s: unknown field %r" % (where, k) for k in turn if k not in TURN_KEYS]
    if not any(k in turn for k in ("text", "tool_calls", "error", "fault", "branches")):
        problems.append("%s must say something, fail, inject a fault or branch" % where)
    if "text" in turn and not isinstance(turn["text"], str):
        problems.append("%s.text must be a string" % where)
    for key in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens"):
        if key in turn and (isinstance(turn[key], bool) or not isinstance(turn[key], int) or not 0 <= turn[key] <= 10 ** 9):
            problems.append("%s.%s must be a whole number from 0 to a billion" % (where, key))
    if "delay_ms" in turn and (isinstance(turn["delay_ms"], bool) or not isinstance(turn["delay_ms"], int) or not 0 <= turn["delay_ms"] <= 600000):
        problems.append("%s.delay_ms must be 0 to 600000" % where)
    if "stop_reason" in turn and not isinstance(turn["stop_reason"], str):
        problems.append("%s.stop_reason must be a string" % where)
    calls = turn.get("tool_calls")
    if calls is not None:
        if not isinstance(calls, list) or len(calls) > 50:
            problems.append("%s.tool_calls must be a list of at most 50" % where)
        else:
            for i, c in enumerate(calls):
                if not isinstance(c, dict) or not isinstance(c.get("name"), str) or not c["name"]:
                    problems.append("%s.tool_calls[%d] needs a name" % (where, i))
                elif "input" in c and not isinstance(c["input"], dict):
                    problems.append("%s.tool_calls[%d].input must be an object" % (where, i))
    err = turn.get("error")
    if err is not None:
        if not isinstance(err, dict) or isinstance(err.get("status"), bool) or not isinstance(err.get("status"), int) or not 400 <= err["status"] <= 599:
            problems.append("%s.error needs a status from 400 to 599" % where)
    if "fault" in turn:
        problems += validate_fault(turn["fault"], where + ".fault")
    branches = turn.get("branches")
    if branches is not None:
        if not isinstance(branches, list) or not branches or len(branches) > MAX_BRANCHES:
            problems.append("%s.branches must be a list of 1 to %d" % (where, MAX_BRANCHES))
        else:
            for i, b in enumerate(branches):
                w = "%s.branches[%d]" % (where, i)
                if not isinstance(b, dict) or set(b) - {"when", "turn", "description"} or "turn" not in b:
                    problems.append("%s must be {when, turn}" % w)
                    continue
                problems += validate_cond(b.get("when", {}), w + ".when")
                problems += validate_turn(b["turn"], w + ".turn", depth + 1)
    return problems


def validate_rule(rule):
    """Every problem with a rule, so a person fixes them in one go."""
    if not isinstance(rule, dict):
        return ["a rule must be an object"]
    problems = ["unknown field %r" % k for k in rule if k not in RULE_KEYS]
    name = rule.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[^\s/][^/]{0,79}", name or ""):
        problems.append("name is required: 1 to 80 characters, no slash, not starting with a space")
    if "enabled" in rule and not isinstance(rule["enabled"], bool):
        problems.append("enabled must be true or false")
    problems += validate_cond(rule.get("when", {}), "when")
    turns = rule.get("turns")
    if not isinstance(turns, list) or not turns or len(turns) > MAX_TURNS:
        problems.append("turns must be a list of 1 to %d turns" % MAX_TURNS)
    else:
        for i, t in enumerate(turns):
            problems += validate_turn(t, "turns[%d]" % i)
    return problems


def _turn_base(rule):
    """The conversation turn a rule's first scripted turn answers. Zero unless
    the rule says it only applies from a later turn on."""
    t = (rule.get("when") or {}).get("turn")
    if isinstance(t, int) and not isinstance(t, bool):
        return t
    if isinstance(t, dict):
        return t.get("min", t.get("eq", 0)) if isinstance(t.get("min", t.get("eq", 0)), int) else 0
    return 0


def resolve_branches(turn, view, depth=0):
    """The turn a request actually gets: the first branch whose condition holds,
    else the turn's own fields. A branch's turn replaces the turn whole."""
    for b in turn.get("branches") or []:
        if cond_result(b.get("when", {}), view)[0]:
            inner = b.get("turn") or {}
            return resolve_branches(inner, view, depth + 1) if depth < 4 and inner.get("branches") else {
                k: v for k, v in inner.items() if k != "branches"}
    return {k: v for k, v in turn.items() if k not in ("branches", "description")}


def pick_scripted(view, explain=False):
    """(name, source, turn_index, turn, checked) for the first rule that matches.

    `checked` says, for every rule tried and in order, whether it matched and — when
    it did not — the first reason why. That is the answer to the question that comes
    after writing any rule: why did it not fire?
    """
    checked = []
    for rule, source in testkit.run_rules() + all_rules():
        if rule.get("enabled") is False:
            if explain:
                checked.append({"rule": rule.get("name"), "source": source, "matched": False, "why": "disabled"})
            continue
        ok, why = cond_result(rule.get("when", {}), view)
        if explain:
            checked.append({"rule": rule.get("name"), "source": source, "matched": ok, "why": why})
        turns = rule.get("turns") or []
        if ok and turns:
            index = max(0, view["turn"] - _turn_base(rule))
            turn = resolve_branches(turns[min(index, len(turns) - 1)], view)
            return rule.get("name", "unnamed"), source, min(index, len(turns) - 1), turn, checked
    return "default", "default", 0, dict(default_turn()), checked


def _rule_summary(rule):
    when = rule.get("when") or {}
    parts = ["%s: %s" % (k, json.dumps(v)[:40]) for k, v in when.items()]
    return ", ".join(parts)[:140] or "every request"


def _rule_public(rule, source, index):
    return {"name": rule.get("name"), "source": source, "index": index, "enabled": rule.get("enabled") is not False,
            "summary": _rule_summary(rule), "turns": len(rule.get("turns") or []),
            "description": rule.get("description", ""), "rule": rule}


def rules_listing():
    return [_rule_public(r, src, i) for i, (r, src) in enumerate(all_rules())]


def _dry_run_view(spec):
    """(view, messages) for a request described in the fields a person has to hand."""
    messages = spec.get("messages")
    if not isinstance(messages, list):
        messages = [{"role": "user", "content": str(spec.get("ask") or "")}]
    tools = spec.get("tools") or []
    request = {"model": spec.get("model", ""), "stream": bool(spec.get("stream")), "messages": messages,
               "tools": [{"type": "function", "function": {"name": t}} for t in tools if isinstance(t, str)]}
    meta = {"provider": spec.get("provider", "openai"), "shape": "openai", "model": spec.get("model", ""),
            "path": spec.get("path", ""), "stream": bool(spec.get("stream")), "route": spec.get("route", "scripted"),
            "identity": spec.get("identity") or {}, "credential": spec.get("credential") or {}, "headers": spec.get("headers") or {}}
    return build_view(meta, request), messages


def _rest_rules(method, rest, body, query, host):
    global SCRIPT
    if method == "GET" and not rest:
        return _ok({"rules": rules_listing(), "default": default_turn(), "userDefault": USER_RULES["default"] is not None,
                    "path": RULES_PATH})
    if method == "POST" and rest == ["validate"]:
        problems = validate_rule((body or {}).get("rule"))
        return _ok(valid=not problems, problems=problems)
    if method == "POST" and rest == ["test"]:
        spec = body or {}
        view, messages = _dry_run_view(spec)
        name, source, index, turn, checked = pick_scripted(view, explain=True)
        turn = fill(turn, messages, view)
        fault = preview_fault(view)
        return _ok(matched=name, source=source, turnIndex=index, turn=turn, checked=checked, fault=fault,
                   view={k: view[k] for k in ("ask", "turn", "lastToolOutput", "tools", "identity")})
    if method == "POST" and rest == ["order"]:
        names = (body or {}).get("names")
        with RULES_LOCK:
            have = {r["name"]: r for r in USER_RULES["rules"]}
            if not isinstance(names, list) or sorted(names) != sorted(have):
                return _fail(400, "names must be every one of your rules, in the order you want them tried.")
            USER_RULES["rules"] = [have[n] for n in names]
            save_user_rules()
        return _ok({"rules": rules_listing()})
    if method == "POST" and rest == ["reload"]:
        with RULES_LOCK:
            SCRIPT = load_script()
            USER_RULES.update(load_user_rules())
        return _ok({"rules": rules_listing()}, message="Reloaded responses.json and %s." % os.path.basename(RULES_PATH))
    if method == "POST" and not rest:
        rule = (body or {}).get("rule")
        problems = validate_rule(rule)
        if problems:
            return _fail(400, "That rule is not valid.", problems=problems)
        with RULES_LOCK:
            if any(r["name"] == rule["name"] for r in USER_RULES["rules"]):
                return _fail(409, "You already have a rule called %s. Replace it with PUT, or pick another name." % rule["name"])
            if (body or {}).get("position") == "bottom":
                USER_RULES["rules"].append(rule)
            else:
                USER_RULES["rules"].insert(0, rule)
            save_user_rules()
        shadows = any(r.get("name") == rule["name"] for r in SCRIPT.get("rules", []))
        return _ok({"rules": rules_listing()}, shadowsShipped=shadows)
    if method == "PUT" and rest == ["default"]:
        turn = (body or {}).get("turn")
        problems = validate_turn(turn, "turn") if turn is not None else []
        if problems:
            return _fail(400, "That turn is not valid.", problems=problems)
        with RULES_LOCK:
            USER_RULES["default"] = turn
            save_user_rules()
        return _ok({"default": default_turn()})
    if method == "DELETE" and rest == ["default"]:
        with RULES_LOCK:
            USER_RULES["default"] = None
            save_user_rules()
        return _ok({"default": default_turn()})
    if len(rest) == 1 and rest[0] not in ("validate", "test", "order", "reload", "default"):
        name = urllib.parse.unquote(rest[0])
        with RULES_LOCK:
            mine = next((r for r in USER_RULES["rules"] if r["name"] == name), None)
        shipped = next((r for r in SCRIPT.get("rules", []) if r.get("name") == name), None)
        if method == "GET":
            if not mine and not shipped:
                return _fail(404, "There is no rule called %s." % name)
            return _ok({"rule": mine or shipped, "source": "user" if mine else "shipped"})
        if method == "PUT":
            if not mine:
                return _fail(403 if shipped else 404, ("%s is a shipped rule and is read-only. Create a rule of your own — "
                                                       "it is tried first." % name) if shipped else "There is no rule called %s." % name)
            rule = (body or {}).get("rule")
            problems = validate_rule(rule)
            if problems:
                return _fail(400, "That rule is not valid.", problems=problems)
            with RULES_LOCK:
                if rule["name"] != name and any(r["name"] == rule["name"] for r in USER_RULES["rules"]):
                    return _fail(409, "You already have a rule called %s." % rule["name"])
                USER_RULES["rules"] = [rule if r["name"] == name else r for r in USER_RULES["rules"]]
                save_user_rules()
            return _ok({"rules": rules_listing()})
        if method == "DELETE":
            if not mine:
                return _fail(403 if shipped else 404, ("%s is a shipped rule and is read-only." % name) if shipped
                             else "There is no rule called %s." % name)
            with RULES_LOCK:
                USER_RULES["rules"] = [r for r in USER_RULES["rules"] if r["name"] != name]
                save_user_rules()
            return _ok({"rules": rules_listing()})
    return _fail(404, "No such route: %s /api/rules/%s" % (method, "/".join(rest)))


REST_AREAS["rules"] = _rest_rules
REST_AREAS["routing"] = _rest_routing


# ── Faults ────────────────────────────────────────────────────────────────
#
# The paths that only ever run when something goes wrong are the ones no live
# vendor will reliably show you: a stream that dies half way, a tool call whose
# arguments are not JSON, a connection reset before a byte. An agent that has
# never met one has never been tested, whatever its happy path does.
#
# A fault is a thing to do (`{"type": "abort_stream", "after": 3}`) and there are
# two ways to say when:
#
#   on a scripted turn   {"text": "...", "fault": {...}} — this turn, every time
#   as a fault rule      {"match": <condition>, "fault": {...}, "probability": 0.2,
#                         "every": 5, "after": 10, "limit": 3, "seed": 7}
#                        — whichever requests the condition selects, as often as
#                        the gates allow
#
# Rules apply on EVERY route — scripted, relay, Anthropic, a replay — since
# a fault is about how the vendor misbehaves, not about who answered. A rule's
# randomness is seeded, so a chaotic run can be run again and do the same thing.
#
#   type                  what happens                                          when
#   status                a vendor error envelope with this status             before any byte
#   delay                 wait first                                           before any byte
#   drop                  hang up before answering (close or reset)            before any byte
#   empty                 a 200 that says nothing                              whole answers
#   malformed_tool_args   a tool call whose arguments are not valid JSON       whole answers, and streams that carry whole calls
#   unknown_tool          a tool call naming a tool nobody declared            whole answers
#   garbage               a body (or one stream frame) that is not valid       any
#   huge                  an answer padded to this many bytes                  whole answers
#   abort_stream          stop after N events, close or reset                  streams
#   stall                 pause for ms after N events                          streams
#   slow_stream           pause for ms between every event                     streams
#
# "Whole answers" means the scripted, replayed and non-streamed relay routes. A
# streamed relay or plan answer is passed through event by event, so only the
# stream faults reach it.

FAULT_TYPES = {
    "status": {"status": "int 400-599 (required)", "error_type": "the vendor's error type", "message": "text", "retry_after": "seconds"},
    "delay": {"ms": "0-600000 (required)"},
    "drop": {"mode": "close | reset"},
    "empty": {},
    "malformed_tool_args": {"mode": "truncated | not_json | array | empty"},
    "unknown_tool": {"name": "the tool name to call"},
    "garbage": {"where": "body | frame", "after": "events before the bad frame"},
    "huge": {"bytes": "1-5000000 (required)"},
    "abort_stream": {"after": "events sent first", "mode": "close | reset"},
    "stall": {"after": "events sent first", "ms": "0-600000 (required)"},
    "slow_stream": {"ms": "0-60000 (required)"},
}
STREAM_FAULTS = ("abort_stream", "stall", "slow_stream", "garbage")
FAULT_RULE_KEYS = {"id", "name", "enabled", "match", "fault", "probability", "every", "after", "limit", "seed", "description"}
FAULT_STATE = {}                 # rule id -> {seen, fired, rng}
FAULT_LOCK = threading.Lock()


class FaultAbort(BrokenPipeError):
    """A stream was cut on purpose. A BrokenPipeError, so every path that already
    copes with a client going away copes with this one too."""


def validate_fault(fault, where="fault"):
    """Problems with a fault spec, found when it is saved."""
    if not isinstance(fault, dict):
        return ["%s must be an object with a type" % where]
    kind = fault.get("type")
    if kind not in FAULT_TYPES:
        return ["%s.type must be one of: %s" % (where, ", ".join(sorted(FAULT_TYPES)))]
    problems = ["%s: %r is not a parameter of %s" % (where, k, kind) for k in fault if k != "type" and k not in FAULT_TYPES[kind]]

    def whole(key, low, high, required=False):
        if key not in fault:
            if required:
                problems.append("%s.%s is required" % (where, key))
            return
        if isinstance(fault[key], bool) or not isinstance(fault[key], int) or not low <= fault[key] <= high:
            problems.append("%s.%s must be a whole number from %d to %d" % (where, key, low, high))

    if kind == "status":
        whole("status", 400, 599, required=True)
        whole("retry_after", 0, 3600)
    elif kind == "delay":
        whole("ms", 0, 600000, required=True)
    elif kind == "huge":
        whole("bytes", 1, 5000000, required=True)
    elif kind == "slow_stream":
        whole("ms", 0, 60000, required=True)
    elif kind == "stall":
        whole("after", 0, 100000)
        whole("ms", 0, 600000, required=True)
    elif kind == "abort_stream":
        whole("after", 0, 100000)
    elif kind == "garbage":
        whole("after", 0, 100000)
    if "mode" in fault and fault["mode"] not in (("close", "reset") if kind in ("drop", "abort_stream") else
                                                 ("truncated", "not_json", "array", "empty")):
        problems.append("%s.mode is not one of the modes of %s" % (where, kind))
    if "where" in fault and fault["where"] not in ("body", "frame"):
        problems.append("%s.where is body or frame" % where)
    for key in ("error_type", "message", "name"):
        if key in fault and not isinstance(fault[key], str):
            problems.append("%s.%s must be text" % (where, key))
    return problems


def validate_fault_rule(rule):
    if not isinstance(rule, dict):
        return ["a fault rule must be an object"]
    problems = ["unknown field %r" % k for k in rule if k not in FAULT_RULE_KEYS]
    if "id" in rule and not (isinstance(rule["id"], str) and ID_RE.fullmatch(rule["id"])):
        problems.append("id is letters, digits, dot, dash, underscore or colon, up to 64")
    if "name" in rule and not isinstance(rule["name"], str):
        problems.append("name must be text")
    if "enabled" in rule and not isinstance(rule["enabled"], bool):
        problems.append("enabled must be true or false")
    problems += validate_cond(rule.get("match", {}), "match")
    problems += validate_fault(rule.get("fault"), "fault")
    p = rule.get("probability", 1)
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0 <= p <= 1:
        problems.append("probability must be from 0 to 1")
    for key in ("every", "after", "limit"):
        if key in rule and (isinstance(rule[key], bool) or not isinstance(rule[key], int) or not 0 <= rule[key] <= 10 ** 9):
            problems.append("%s must be a whole number, 0 or more" % key)
    if "seed" in rule and not isinstance(rule["seed"], (int, str, type(None))):
        problems.append("seed must be a number or text")
    return problems


def _fault_state(rule):
    st = FAULT_STATE.get(rule["id"])
    if st is None:
        seed = rule.get("seed")
        st = FAULT_STATE[rule["id"]] = {"seen": 0, "fired": 0, "rng": random.Random(rule["id"] if seed in (None, "") else seed)}
    return st


def _gate(rule, st, consume):
    """(fires, why) for a request this rule's condition selected.

    seen counts the requests the condition selected; the gates then say which of
    them the rule acts on: skip the first `after`, then every `every`th, until
    `limit` have fired, each with `probability`. With consume=False this only
    looks — nothing is counted, and a probability is reported rather than rolled.
    """
    seen = st["seen"] + 1
    if consume:
        st["seen"] = seen
    after, every, limit = rule.get("after", 0), rule.get("every", 0), rule.get("limit", 0)
    if seen <= after:
        return False, "skipping the first %d matching request(s)" % after
    if every > 1 and (seen - after) % every != 0:
        return False, "fires every %d, and this is not one" % every
    if limit and st["fired"] >= limit:
        return False, "used up its limit of %d" % limit
    p = rule.get("probability", 1)
    if p < 1:
        if not consume:
            return True, "would fire with probability %s" % p
        if st["rng"].random() >= p:
            return False, "the dice said no (probability %s)" % p
    return True, "fires"


def pick_fault(view, consume=True):
    """(rule, fault) for the first fault rule that fires on this request, else (None, None).
    With consume=False nothing is used up: this is the preview."""
    if CONFIG["chaos"]["paused"]:
        return None, None
    with FAULT_LOCK:
        for rule in testkit.run_faults() + CONFIG["faults"]:
            if rule.get("enabled") is False or not cond_result(rule.get("match", {}), view)[0]:
                continue
            st = _fault_state(rule)
            fires, _why = _gate(rule, st, consume)
            if fires:
                if consume:
                    st["fired"] += 1
                return rule, rule["fault"]
    return None, None


def preview_fault(view):
    """What would be done to this request, without using anything up."""
    rule, fault = pick_fault(view, consume=False)
    if not rule:
        return None
    return {"rule": rule["id"], "name": rule.get("name", ""), "fault": fault, "probability": rule.get("probability", 1)}


def fault_label(fault):
    return fault["type"] if fault else ""


def apply_turn_fault(turn, fault):
    """(turn, after) for the faults that change what is said. `after` is a thing
    to do once the turn is built: "garbage_body"."""
    kind = fault["type"]
    turn = dict(turn)
    after = None
    if kind == "empty":
        turn.pop("text", None)
        turn["tool_calls"] = []
        turn["text"] = ""
    elif kind == "huge":
        turn["text"] = (turn.get("text") or "") + "x" * fault["bytes"]
    elif kind == "malformed_tool_args":
        mode = fault.get("mode", "truncated")
        calls = []
        for c in turn.get("tool_calls") or []:
            whole_json = json.dumps(c.get("input") or {})
            raw = {"truncated": whole_json[:max(1, len(whole_json) // 2)], "not_json": "this is not json",
                   "array": "[1, 2, 3]", "empty": ""}[mode]
            calls.append(dict(c, raw_arguments=raw))
        turn["tool_calls"] = calls
    elif kind == "unknown_tool":
        turn["tool_calls"] = [dict(c, name=fault.get("name") or "llmsim_unknown_tool") for c in turn.get("tool_calls") or []] or \
            [{"name": fault.get("name") or "llmsim_unknown_tool", "input": {}}]
    elif kind == "garbage" and fault.get("where", "body") == "body":
        after = "garbage_body"
    elif kind == "status":
        turn["error"] = {"status": fault["status"], "type": fault.get("error_type") or _error_type(fault["status"]),
                         "message": fault.get("message") or "A fault LLMSim was told to inject.", "retry_after": fault.get("retry_after")}
    return turn, after


def fault_rules_listing():
    out = []
    for rule in CONFIG["faults"]:
        with FAULT_LOCK:
            st = FAULT_STATE.get(rule["id"], {"seen": 0, "fired": 0})
        out.append(dict(rule, seen=st["seen"], fired=st["fired"]))
    return out


def _rest_faults(method, rest, body, query, host):
    def listing(**extra):
        return _ok({"faults": fault_rules_listing(), "paused": CONFIG["chaos"]["paused"], "types": FAULT_TYPES}, **extra)

    if method == "GET" and not rest:
        return listing()
    if method == "POST" and rest in (["pause"], ["resume"]):
        CONFIG["chaos"]["paused"] = rest == ["pause"]
        save_config()
        return listing()
    if method == "POST" and rest == ["reset"]:
        with FAULT_LOCK:
            FAULT_STATE.clear()
        return listing(message="Counters reset.")
    if method == "POST" and not rest:
        # The rule itself is the body — or wrapped as {"rule": …}. Not under "fault": a
        # rule HAS a fault, and reading that as the wrapper takes the spec for the rule.
        rule = dict((body or {}).get("rule") if isinstance((body or {}).get("rule"), dict) else (body or {}))
        rule.setdefault("id", "f-" + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6)))
        problems = validate_fault_rule(rule)
        if problems:
            return _fail(400, "That fault rule is not valid.", problems=problems)
        if any(r["id"] == rule["id"] for r in CONFIG["faults"]):
            return _fail(409, "A fault rule called %s already exists." % rule["id"])
        rule.setdefault("enabled", True)
        CONFIG["faults"].append(rule)
        save_config()
        return listing(id=rule["id"])
    if len(rest) == 1 and rest[0] not in ("pause", "resume", "reset"):
        fid = rest[0]
        existing = next((r for r in CONFIG["faults"] if r["id"] == fid), None)
        if not existing:
            return _fail(404, "There is no fault rule called %s." % fid)
        if method == "GET":
            return _ok({"fault": fault_rules_listing()[CONFIG["faults"].index(existing)]})
        if method == "PUT":
            rule = dict((body or {}).get("rule") if isinstance((body or {}).get("rule"), dict) else (body or {}))
            rule["id"] = fid
            problems = validate_fault_rule(rule)
            if problems:
                return _fail(400, "That fault rule is not valid.", problems=problems)
            rule.setdefault("enabled", True)
            CONFIG["faults"] = [rule if r["id"] == fid else r for r in CONFIG["faults"]]
            with FAULT_LOCK:
                FAULT_STATE.pop(fid, None)
            save_config()
            return listing()
        if method == "DELETE":
            CONFIG["faults"] = [r for r in CONFIG["faults"] if r["id"] != fid]
            with FAULT_LOCK:
                FAULT_STATE.pop(fid, None)
            save_config()
            return listing()
    return _fail(404, "No such route: %s /api/faults/%s" % (method, "/".join(rest)))


REST_AREAS["faults"] = _rest_faults


# ── Assertions: checking what was actually sent ───────────────────────────
#
# A test of an agent has to be able to say things like
#
#     no request of run r1 carried a card number to the model
#     every request carried the safety preamble
#     the worker never called Write
#     the planner asked before the worker did
#     agent B was told what agent A answered
#
# — claims about the requests an agent made, which LLMSim is the one place that
# sees. Each is an assertion over captures:
#
#   {"id": "no-card-numbers", "name": "…",
#    "scope": {"identity": {"run": "r1"}},                   which requests this is about
#    "none":  {"field": "allText", "op": "regex", "value": "\\b\\d{13,16}\\b"}}
#
# and exactly one of
#
#   each      every scoped request satisfies the check
#   none      no scoped request satisfies the check          (the form for "must not")
#   count     {min, max, where?}: how many scoped requests (satisfying where) there are
#   sequence  [cond, cond, …]: requests matching these exist, in this order
#
# Three decisions, each from something that fails otherwise:
#
#   NO DATA IS NOT A PASS. "No request carried a card number" is true of an agent
#   that never ran. A check that selects nothing reports `no-data`, which is not a
#   pass, unless the assertion says allowEmpty.
#   RUN ON WHAT WAS SENT, SHOW WHAT IS SAFE. Assertions read the held, unredacted
#   capture — a leak check run on redacted text could never find a leak — and the
#   evidence they return has the configured redactions applied.
#   SAY WHEN IT SAW LESS THAN EVERYTHING. A request too large to hold whole is kept
#   in part. An assertion that passed over one says so, because "none" passing on
#   half a request is weaker than it looks.

ASSERTIONS_PATH = os.environ.get("LLMSIM_ASSERTIONS", os.path.join(DATA_DIR, "llmsim.assertions.json"))
ASSERTION_SETS = {"sets": []}
ASSERTION_LOCK = threading.RLock()
ASSERTION_KINDS = ("each", "none", "count", "sequence")
ASSERTION_KEYS = {"id", "name", "description", "scope", "allowEmpty", "each", "none", "count", "where", "sequence"}
VIEW_FIELDS = {
    "system": "the system prompt", "ask": "the last thing a person asked", "lastText": "the last message",
    "lastToolOutput": "what the last tool printed", "userText": "everything a person said", "assistantText": "everything the model said so far",
    "allText": "every message, concatenated", "tools": "the tools the agent declared (a list)", "turn": "turns already taken",
    "messageCount": "messages in the request", "model": "the model asked for", "provider": "anthropic, openai or deepseek",
    "route": "scripted, relay, anthropic or replay", "rule": "which rule answered", "stream": "whether it streamed",
    "status": "the HTTP status of the answer", "fault": "the fault injected, if any",
    "response.text": "what was answered", "response.toolNames": "the tools the answer called (a list)",
    "response.usage.input": "tokens in", "response.usage.output": "tokens out", "response.error": "the error, if any",
    "response.ms": "how long it took", "identity.run": "the run", "identity.agent": "the agent", "identity.app": "the app",
    "credential.label": "what the credential was registered as", "credential.fingerprint": "which credential",
    "credential.known": "whether LLMSim knows the credential", "headers.<name>": "a request header",
}
OPERATORS = ["eq", "ne", "contains", "not_contains", "regex", "not_regex", "in", "not_in", "gt", "gte", "lt", "lte", "exists",
             "not_exists", "includes", "excludes", "includes_all", "includes_any", "empty", "len"]


def load_assertion_sets():
    try:
        with open(ASSERTIONS_PATH) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"sets": []}
    except (ValueError, OSError) as exc:
        log("assertions: cannot read %s (%s) — starting without them" % (ASSERTIONS_PATH, exc))
        return {"sets": []}
    return {"sets": [s for s in data.get("sets", []) if isinstance(s, dict) and s.get("id")]}


def save_assertion_sets():
    tmp = ASSERTIONS_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"version": 1, "_": "LLMSim assertion sets. Commit this.", "sets": ASSERTION_SETS["sets"]}, fh, indent=1)
    os.replace(tmp, ASSERTIONS_PATH)


def validate_assertion(a, where="assertion"):
    if not isinstance(a, dict):
        return ["%s must be an object" % where]
    problems = ["%s: unknown field %r" % (where, k) for k in a if k not in ASSERTION_KEYS]
    if not isinstance(a.get("id"), str) or not ID_RE.fullmatch(a.get("id") or ""):
        problems.append("%s.id is required: letters, digits, dot, dash, underscore or colon" % where)
    if not isinstance(a.get("name"), str) or not a.get("name"):
        problems.append("%s.name is required" % where)
    problems += validate_cond(a.get("scope", {}), where + ".scope")
    kinds = [k for k in ASSERTION_KINDS if k in a]
    if len(kinds) != 1:
        problems.append("%s must have exactly one of each, none, count or sequence" % where)
        return problems
    kind = kinds[0]
    if kind in ("each", "none"):
        problems += validate_cond(a[kind], "%s.%s" % (where, kind))
    elif kind == "count":
        c = a["count"]
        if not isinstance(c, dict) or set(c) - {"min", "max"} or not c:
            problems.append("%s.count is {min, max} with at least one of them" % where)
        else:
            for k, v in c.items():
                if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                    problems.append("%s.count.%s must be a whole number, 0 or more" % (where, k))
            if isinstance(c.get("min"), int) and isinstance(c.get("max"), int) and c["min"] > c["max"]:
                problems.append("%s.count.min is more than max" % where)
        if "where" in a:
            problems += validate_cond(a["where"], where + ".where")
    elif kind == "sequence":
        seq = a["sequence"]
        if not isinstance(seq, list) or not 1 <= len(seq) <= 20:
            problems.append("%s.sequence must be a list of 1 to 20 conditions" % where)
        else:
            for i, c in enumerate(seq):
                problems += validate_cond(c, "%s.sequence[%d]" % (where, i))
    if kind != "count" and "where" in a:
        problems.append("%s.where only goes with count" % where)
    if "allowEmpty" in a and not isinstance(a["allowEmpty"], bool):
        problems.append("%s.allowEmpty must be true or false" % where)
    return problems


def _excerpt(check, view, width=60):
    """The text around what a check found, for evidence. Redacted by the caller."""
    if not (isinstance(check, dict) and check.get("op") in ("regex", "contains") and isinstance(check.get("field"), str)):
        return ""
    text = _dig_path(view, check["field"])
    if not isinstance(text, str):
        return ""
    want, case = str(check.get("value")), bool(check.get("case"))
    start = end = None
    try:
        if check["op"] == "regex":
            m = re.search(want, text, 0 if case else re.I)
            if m:
                start, end = m.start(), m.end()
        else:
            i = text.find(want) if case else text.lower().find(want.lower())
            if i >= 0:
                start, end = i, i + len(want)
    except re.error:
        return ""
    if start is None:
        return ""
    return ("…" if start > width else "") + text[max(0, start - width):end + width] + ("…" if end + width < len(text) else "")


def _evidence(view, why, check=None, patterns=None):
    item = {"capture": view["n"], "run": view["identity"].get("run", ""), "agent": view["identity"].get("agent", ""),
            "turn": view["turn"], "ask": view["ask"][:120], "why": why[:300]}
    ex = _excerpt(check, view)
    if ex:
        item["excerpt"] = ex
    return _redact_value(item, patterns) if patterns else item


def eval_assertion(a, views):
    """The verdict on one assertion over the given views (oldest first).

    status: pass, fail, no-data (selected nothing, so proved nothing), or error
    (the assertion itself could not be evaluated).
    """
    patterns = CONFIG["capture"]["redact"]["patterns"]
    result = {"id": a.get("id"), "name": a.get("name"), "status": "pass", "detail": "", "matched": 0, "evidence": [], "warnings": []}
    problems = validate_assertion(a)
    if problems:
        return dict(result, status="error", detail="; ".join(problems[:3]))
    scoped = [v for v in views if cond_result(a.get("scope", {}), v)[0]]
    result["matched"] = len(scoped)
    kind = [k for k in ASSERTION_KINDS if k in a][0]
    truncated = [v for v in scoped if v["truncated"]]
    if truncated:
        result["warnings"].append("%d request(s) were too large to hold whole and were reduced; a check for something absent may have "
                                  "missed it in the part that was dropped" % len(truncated))
    if not scoped and not a.get("allowEmpty"):
        if kind == "count" and a["count"].get("min", 0) >= 1:
            return dict(result, status="fail", detail="no request matched the scope, and at least %d was required" % a["count"]["min"])
        return dict(result, status="no-data", detail="no request matched the scope, so there is nothing to check. "
                    "An agent that never ran satisfies every 'must not'; set allowEmpty if that is intended.")
    if kind == "each":
        bad = []
        for v in scoped:
            ok, why = cond_result(a["each"], v)
            if not ok:
                bad.append(_evidence(v, why, a["each"], patterns))
        result["evidence"] = bad[:10]
        if bad:
            return dict(result, status="fail", detail="%d of %d request(s) did not satisfy it" % (len(bad), len(scoped)))
        return dict(result, detail="all %d request(s) satisfied it" % len(scoped))
    if kind == "none":
        hit = []
        for v in scoped:
            ok, why = cond_result(a["none"], v)
            if ok:
                hit.append(_evidence(v, "this request satisfied the forbidden condition", a["none"], patterns))
        result["evidence"] = hit[:10]
        if hit:
            return dict(result, status="fail", detail="%d of %d request(s) did what it forbids" % (len(hit), len(scoped)))
        return dict(result, detail="none of %d request(s) did" % len(scoped))
    if kind == "count":
        rows = [v for v in scoped if cond_result(a["where"], v)[0]] if "where" in a else scoped
        n, lo, hi = len(rows), a["count"].get("min"), a["count"].get("max")
        result["evidence"] = [_evidence(v, "counted", a.get("where")) for v in rows[:10]]
        result["count"] = n
        if lo is not None and n < lo:
            return dict(result, status="fail", detail="%d request(s), at least %d required" % (n, lo))
        if hi is not None and n > hi:
            return dict(result, status="fail", detail="%d request(s), at most %d allowed" % (n, hi))
        return dict(result, detail="%d request(s), within the limits" % n)
    # sequence: each condition is found in a request after the one before it
    cursor, found = 0, []
    for i, cond in enumerate(a["sequence"]):
        hit = next((j for j in range(cursor, len(scoped)) if cond_result(cond, scoped[j])[0]), None)
        if hit is None:
            result["evidence"] = [_evidence(scoped[found[-1]], "the last one found, before step %d" % (i + 1))] if found else []
            return dict(result, status="fail", detail="step %d of %d was never found%s" % (
                i + 1, len(a["sequence"]), " after step %d" % i if i else ""))
        found.append(hit)
        cursor = hit + 1
    result["evidence"] = [_evidence(scoped[j], "step %d" % (i + 1)) for i, j in enumerate(found)]
    return dict(result, detail="all %d steps were found, in order" % len(found))


def evaluate_assertions(assertions, extra_scope=None, set_scope=None):
    """A report for a list of assertions over what is held now."""
    views = [capture_view(c) for c in captures_matching(extra_scope or None)]
    results = []
    for a in assertions:
        a = dict(a)
        if set_scope:
            a["scope"] = {"all": [set_scope, a.get("scope", {})]}
        results.append(eval_assertion(a, views))
    counts = {k: sum(1 for r in results if r["status"] == k) for k in ("pass", "fail", "no-data", "error")}
    verdict = "fail" if counts["fail"] or counts["error"] else "no-data" if counts["no-data"] else "pass"
    if not results:
        verdict = "no-data"
    return {"verdict": verdict, "summary": counts, "results": results, "capturesConsidered": len(views),
            "firstCapture": views[0]["n"] if views else None, "lastCapture": views[-1]["n"] if views else None,
            "evaluatedAt": time.time(), "redacted": bool(CONFIG["capture"]["redact"]["patterns"])}


def _run_scope(body):
    scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
    ident = {k: body[k] for k in IDENTITY_FIELDS if isinstance(body.get(k), str)}
    if ident:
        scope = {"all": [scope, {"identity": ident}]} if scope else {"identity": ident}
    return scope


def _rest_assertions(method, rest, body, query, host):
    if method == "GET" and rest == ["catalog"]:
        return _ok({"fields": VIEW_FIELDS, "operators": OPERATORS, "kinds": list(ASSERTION_KINDS),
                    "conditions": sorted(COND_KEYS)})
    if method == "POST" and rest == ["validate"]:
        problems = validate_assertion((body or {}).get("assertion"))
        return _ok(valid=not problems, problems=problems)
    if method == "POST" and rest == ["evaluate"]:
        body = body or {}
        if "set" in body:
            with ASSERTION_LOCK:
                found = next((s for s in ASSERTION_SETS["sets"] if s["id"] == body["set"]), None)
            if not found:
                return _fail(404, "There is no assertion set called %s." % body["set"])
            return _ok(evaluate_assertions(found.get("assertions", []), _run_scope(body), found.get("scope")), set=found["id"])
        assertions = body.get("assertions")
        if not isinstance(assertions, list) or not assertions or len(assertions) > 200:
            return _fail(400, "Give assertions (a list of 1 to 200), or the id of a saved set.")
        return _ok(evaluate_assertions(assertions, _run_scope(body)))
    return _fail(404, "No such route: %s /api/assertions/%s" % (method, "/".join(rest)))


def _rest_assertion_sets(method, rest, body, query, host):
    def clean(sset):
        if not isinstance(sset, dict) or not isinstance(sset.get("id"), str) or not ID_RE.fullmatch(sset["id"]):
            return None, ["a set needs an id: letters, digits, dot, dash, underscore or colon"]
        problems = ["unknown field %r" % k for k in sset if k not in ("id", "name", "description", "scope", "assertions")]
        problems += validate_cond(sset.get("scope", {}), "scope")
        items = sset.get("assertions")
        if not isinstance(items, list) or len(items) > 200:
            problems.append("assertions must be a list of at most 200")
        else:
            seen = set()
            for i, a in enumerate(items):
                problems += validate_assertion(a, "assertions[%d]" % i)
                if isinstance(a, dict) and a.get("id") in seen:
                    problems.append("assertions[%d].id %r is used twice" % (i, a.get("id")))
                seen.add(a.get("id") if isinstance(a, dict) else None)
        return sset, problems

    if method == "GET" and not rest:
        return _ok({"sets": ASSERTION_SETS["sets"], "path": ASSERTIONS_PATH})
    if method == "POST" and not rest:
        sset, problems = clean((body or {}).get("set") if isinstance((body or {}).get("set"), dict) else body)
        if problems:
            return _fail(400, "That set is not valid.", problems=problems)
        with ASSERTION_LOCK:
            if any(s["id"] == sset["id"] for s in ASSERTION_SETS["sets"]):
                return _fail(409, "A set called %s already exists." % sset["id"])
            ASSERTION_SETS["sets"].append(sset)
            save_assertion_sets()
        return _ok({"sets": ASSERTION_SETS["sets"]})
    if len(rest) >= 1:
        sid = rest[0]
        with ASSERTION_LOCK:
            found = next((s for s in ASSERTION_SETS["sets"] if s["id"] == sid), None)
        if not found:
            return _fail(404, "There is no assertion set called %s." % sid)
        if method == "GET" and len(rest) == 1:
            return _ok({"set": found})
        if method == "POST" and rest[1:] == ["evaluate"]:
            return _ok(evaluate_assertions(found.get("assertions", []), _run_scope(body or {}), found.get("scope")), set=sid)
        if method == "PUT" and len(rest) == 1:
            sset, problems = clean(dict((body or {}).get("set") if isinstance((body or {}).get("set"), dict) else (body or {}), id=sid))
            if problems:
                return _fail(400, "That set is not valid.", problems=problems)
            with ASSERTION_LOCK:
                ASSERTION_SETS["sets"] = [sset if s["id"] == sid else s for s in ASSERTION_SETS["sets"]]
                save_assertion_sets()
            return _ok({"sets": ASSERTION_SETS["sets"]})
        if method == "DELETE" and len(rest) == 1:
            with ASSERTION_LOCK:
                ASSERTION_SETS["sets"] = [s for s in ASSERTION_SETS["sets"] if s["id"] != sid]
                save_assertion_sets()
            return _ok({"sets": ASSERTION_SETS["sets"]})
    return _fail(404, "No such route: %s /api/assertion-sets/%s" % (method, "/".join(rest)))


REST_AREAS["assertions"] = _rest_assertions
REST_AREAS["assertion-sets"] = _rest_assertion_sets


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
        if access_on() and not self.who:
            # The sign-in is drawn in front of the console from the first paint, not after a round trip.
            page = page.replace(b"<body>", b'<body class="signing-in">', 1)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        # Never cached: the console is read while settings are being changed,
        # and a stale one shows a routing table that is no longer true.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(page)

    def serve_guide(self):
        """The how-to guide, from the same process and with nothing to fetch.

        docs/guide.html is written as a page body — a title, its styles, its
        content — so the same file can be published elsewhere as it is. docs/guide.md
        beside it is the same guide for reading on GitHub, which shows an .html file
        as source; a test keeps the two naming the same routes. It is given a
        document around it here; without one a browser renders it in quirks mode.
        """
        try:
            with open(os.path.join(HERE, "docs", "guide.html"), "rb") as fh:
                body = fh.read()
        except FileNotFoundError:
            return self.send_json(404, {"error": "docs/guide.html is missing from this install"})
        page = (b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                b'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"></head><body>'
                + body + b"</body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
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
            "rules": [r.get("name") for r, _ in all_rules()],
            "runs": RESULTS["runs"][:5],
            "session": session_status(),
            "hold": route_hold(),
            "state": state_store(),
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

        problem = set_routing(routing)
        if problem:
            return self.send_json(400, {"error": problem})

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

        claude = body.get("anthropic")
        if isinstance(claude, dict):
            problem = save_anthropic_settings(claude)
            if problem:
                return self.send_json(400, {"error": problem})

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
        for key, value in getattr(self, "_extra", {}).items():
            self.send_header(key, value)
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
        # Unless a fault said how long: two Retry-After headers disagree, and the client
        # reads whichever it likes.
        if "Retry-After" in getattr(self, "_extra", {}):
            pass
        elif status == 429:
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

    def send_response(self, code, message=None):
        # The first status sent is the answer's; a capture records it.
        if getattr(self, "_status", None) is None:
            self._status = code
        BaseHTTPRequestHandler.send_response(self, code, message)

    def refuse(self, status, kind, message):
        capture_note(self, error=message)
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
        # Who is asking, from the base URL: /run/<id>, /agent/<name>, /app/<name>,
        # in any order, ahead of the provider. It is the one way of saying so that
        # needs nothing of an agent platform but a URL to point at.
        self.path_identity = {}
        while True:
            m = re.match(r"^/(run|agent|app)/([A-Za-z0-9._:-]{1,64})(/.*)?$", path)
            if not m:
                break
            self.path_identity.setdefault(m.group(1), m.group(2))
            path = (m.group(3) or "/").rstrip("/") or "/"
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

    def resolve_provider(self, prefix, key, path=""):
        if prefix:
            return prefix
        for name, expected in API_KEYS.items():
            if key == expected:
                return name
        # A credential LLMSim did not publish says nothing about the vendor, so
        # the route does: /v1/messages is Anthropic's, and everything else here
        # is the OpenAI shape.
        if path.startswith("/v1/messages"):
            return "anthropic"
        return None

    # -- routes --

    def rest_api(self, method, path, body, query):
        """Any /api/… route that is not the console's own. A browser page on
        another site can make this machine's browser send these, so a request
        that declares another origin is refused, exactly as the console's are."""
        if self.foreign_origin():
            return self.send_json(403, {"ok": False, "message": "That request came from another site."})
        result = rest_dispatch(method, path, body, query, self.headers.get("Host") or "localhost:%d" % PORT)
        if len(result) == 3 and result[2].get("raw") is not None:
            raw = result[2]["raw"].encode()
            self.send_response(result[0])
            self.send_header("Content-Type", result[2].get("content_type", "text/plain") + "; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if len(result) == 3 and result[2].get("download"):
            raw = json.dumps(result[1], indent=1).encode()
            self.send_response(result[0])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % result[2]["download"])
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        return self.send_json(result[0], result[1])

    def _begin(self):
        """State that belongs to one request. A handler outlives a request on a
        kept-alive connection, so none of it may be left from the last one."""
        self._status, self.cap, self.identity = None, None, {}
        self.show_trace, self.trace = False, {}
        self.fault, self.stream_fault, self._events, self._fault_done, self._extra = None, None, 0, False, {}

    def do_GET(self):
        self._begin()
        prefix, path = self.split_path()
        key = self.presented_key()
        self.provider = self.resolve_provider(prefix, key, path) or "openai"
        self.cred = credential_of(self)
        if not self.authorize("GET", path):
            return
        if path == "/api/access":
            return self.send_json(200, dict(access_status(), ok=True, you=self.who if self.who not in (None, "key") else None))
        if path == "/api/auth":
            return self.send_json(200, {"on": access_on(), "user": self.who if self.who not in (None, "key") else None,
                                        "via": "key" if self.who == "key" else "cookie" if self.who else None})

        if path.startswith(REST_PREFIXES):
            return self.rest_api("GET", path, None, urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))

        if testkit.owns(path):
            return self.kit_route("GET", path, b"")

        if path == "/ui" or path == "/ui/":
            return self.serve_ui()

        if path == "/guide":
            return self.serve_guide()

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

        if path == "/api/sessions" or path.startswith("/api/sessions/"):
            return self.session_api("GET", path, None, urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))

        if path == "/api/ui/session":
            return self.send_json(200, session_status())

        if path.startswith("/api/ui/session/"):
            return self.session_download(path[len("/api/ui/session/"):])

        if path == "/api/ui/anthropic/models":
            try:
                return self.send_json(200, {"models": anthropic_models()})
            except AnthropicError as exc:
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
            if access_on() and not self.who:
                # Alive, and nothing more: the rest says what is routed where and which rules exist.
                return self.send_json(200, {"ok": True, "service": "llmsim", "access": "sign-in required"})
            return self.send_json(200, {
                "ok": True, "service": "llmsim",
                "providers": sorted(API_KEYS),
                "mode": default_route(),
                "hold": route_hold(),
                "state": state_store(),
                "routed": len(CONFIG["routing"]["models"]),
                "upstream": ({"model": CONFIG["upstream"]["model"],
                              "format": CONFIG["upstream"]["format"],
                              "fallback": CONFIG["upstream"]["fallback"]} if relaying() else None),
                "anthropic": {"keySet": bool(CONFIG["anthropic"]["key"]), "model": CONFIG["anthropic"]["model"] or "the client's"},
                "session": {"mode": SESSION["mode"], "name": SESSION["name"]},
                "rules": [r.get("name") for r, _ in all_rules()],
            })

        if path == "/v1/models":
            if not credential_accepted(self.cred):
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

    def kit_route(self, method, path, raw):
        """A protocol route of the test kit (a mock tool server, a stub agent, a trace
        collector). Served by the kit, which is given the handler to answer through."""
        if self.foreign_origin():
            return self.send_json(403, {"ok": False, "message": "That request came from another site."})
        self.identity = identity_of(self, None)
        try:
            testkit.serve(self, method, path, raw)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as exc:  # noqa: BLE001 — a bug in a route is a message, not a dropped connection
            log("testkit route %s %s: %s: %s" % (method, path, type(exc).__name__, exc))
            if self._status is None:
                self.send_json(500, {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)})

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return None

    def do_PUT(self):
        self._begin()
        prefix, path = self.split_path()
        self.provider = self.resolve_provider(prefix, self.presented_key(), path) or "openai"
        body = self._json_body()
        if body is None:
            return self.send_json(400, {"ok": False, "message": "The body is not valid JSON."})
        if not self.authorize("PUT", path):
            return
        if path.startswith(REST_PREFIXES):
            return self.rest_api("PUT", path, body, urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))
        if testkit.owns(path):
            return self.kit_route("PUT", path, json.dumps(body).encode())
        if not path.startswith("/api/sessions/"):
            return self.send_json(404, {"error": "No such route: PUT " + path})
        return self.session_api("PUT", path, body, urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))

    def do_PATCH(self):
        # Only the test kit's own addresses take PATCH: a mock REST backend's routes can be PATCH
        # routes, and an agent's call to one must reach it rather than the server's default 501.
        self._begin()
        prefix, path = self.split_path()
        self.provider = self.resolve_provider(prefix, self.presented_key(), path) or "openai"
        body = self._json_body()
        if body is None:
            return self.send_json(400, {"ok": False, "message": "The body is not valid JSON."})
        if not self.authorize("PATCH", path):
            return
        if testkit.owns(path):
            return self.kit_route("PATCH", path, json.dumps(body).encode())
        return self.send_json(405, {"error": "PATCH is taken only by a mock backend: " + path})

    def do_DELETE(self):
        self._begin()
        prefix, path = self.split_path()
        self.provider = self.resolve_provider(prefix, self.presented_key(), path) or "openai"
        self._json_body()                       # a body, if one was sent, is read and ignored
        if not self.authorize("DELETE", path):
            return
        if path.startswith(REST_PREFIXES):
            return self.rest_api("DELETE", path, None, urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))
        if testkit.owns(path):
            return self.kit_route("DELETE", path, b"")
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
        self._begin()
        self.internal = self._is_internal()
        prefix, path = self.split_path()
        key = self.presented_key()
        self.provider = self.resolve_provider(prefix, key, path) or "openai"
        self.cred = credential_of(self)

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not self.authorize("POST", path):
            return
        if path == "/login":
            return self.login_post(raw if length else b"")
        if path == "/logout":
            return self.logout_post()
        if path == "/api/access":
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self.send_json(400, {"ok": False, "message": "The body is not valid JSON."})
            return self.access_console(body if isinstance(body, dict) else {})
        if testkit.owns(path):
            return self.kit_route("POST", path, raw)
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

        if path == "/api/ui/chat":
            return self.console_chat(body)

        if path.startswith(REST_PREFIXES):
            return self.rest_api("POST", path, body, urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))

        if path.startswith("/api/sessions"):
            return self.session_api("POST", path, body, urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))

        if path == "/api/ui/session":
            return self.session_console(body)

        if not key:
            return self.refuse(401, "authentication_error", "No API key provided.")
        if not credential_accepted(self.cred):
            return self.refuse(401, "authentication_error", "Invalid API key.")
        # Who this is, now that it is allowed in. Read from the base URL first and
        # then from wherever the configured rules look, since a gateway may carry
        # an identity through in a header or a body field.
        self.identity = identity_of(self, body)

        if path == "/v1/messages" and self.provider == "anthropic":
            return self.handle_chat(body, "anthropic")
        # DeepSeek publishes an Anthropic-compatible endpoint beside its own
        # (api.deepseek.com/anthropic), and a gateway sends any client that
        # speaks Messages there: LiteLLM's deepseek/ provider does. Without it a
        # coding agent on a DeepSeek model got "No such route" here and nowhere
        # else.
        if self.provider == "deepseek" and path in ("/anthropic/v1/messages", "/anthropic/v1/messages/count_tokens"):
            path = path[len("/anthropic"):]
            if path == "/v1/messages":
                return self.handle_chat(body, "anthropic")
        if path == "/v1/messages/count_tokens":
            text = json.dumps(body.get("messages", []))
            return self.send_json(200, {"input_tokens": rough_tokens(text)})
        if path == "/v1/chat/completions":
            return self.handle_chat(body, "openai")
        if path == "/v1/responses":
            return self.handle_chat(body, "responses")

        return self.refuse(404, "not_found_error", "No such route: " + path)




    # -- access --

    def _cookie(self, name):
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            return ""
        return jar[name].value if name in jar else ""

    def _presented_access_key(self, path):
        key = (self.headers.get("X-LLMSim-Key") or "").strip()
        if not key and path.startswith("/api"):
            # A bearer token on /api/… is never a vendor's key: no vendor route lives there.
            auth = self.headers.get("authorization") or ""
            key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        return key

    def authorize(self, method, path):
        """True when this request may go on. When it may not, the refusal has been sent.

        Who it is is kept on the handler: "key" for the API key, the user's name for a
        console cookie, None when access is off or the path is one anybody may use.
        """
        self.who = None
        if not access_on():
            return True
        if access_key_ok(self._presented_access_key(path)):
            self.who = "key"
            return True
        user = session_user(self._cookie(SESSION_COOKIE))
        if user:
            # A cookie is sent by the browser whoever made the request, so a change made
            # with one must come from the console's own page. Another site on this machine
            # (another port is the same site to a browser) says so in Origin, or in
            # Sec-Fetch-Site when it leaves Origin out.
            if method not in ("GET", "HEAD") and (
                    self.foreign_origin() or (not self.headers.get("Origin")
                                              and self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"))):
                self.send_json(403, {"ok": False, "message": "That request came from another site."})
                return False
            self.who = user
            return True
        if not access_guarded(path):
            return True
        raw = json.dumps({"ok": False, "message": "Sign in on the console (/ui), or send the API key (X-LLMSim-Key, or a bearer token)."}).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        # What the console looks for to put its sign-in in front of the page.
        self.send_header("X-LLMSim-Sign-In", "/ui")
        self.send_header("WWW-Authenticate", 'Bearer realm="llmsim"')
        self.end_headers()
        self.wfile.write(raw)
        return False

    def _access_reply(self, code, message, cookie=None, **extra):
        return self._reply(code, dict(access_status(), ok=code < 400, message=message, **extra), cookie)

    def _reply(self, code, payload, cookie=None):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def _current_password_ok(self, body):
        """A change to the sign-in itself, made with a cookie, asks for the password again:
        a console left open is not the same as knowing it. The key, or a sign-in not yet
        turned on, needs nothing more."""
        if self.who in (None, "key") or not password_set():
            return True
        address = self.client_address[0]
        if sign_in_wait(address):
            return False
        ok = password_ok(self.who, body.get("current") or "")
        sign_in_result(address, ok)
        return ok

    def access_console(self, body):
        """Settings › Sign-in and API key. One action per call:

            password      {user?, password, current?}  turn the sign-in on, or change the password
            new-key       {current?}                   make an API key (replacing one); shown once
            remove-key    {current?}
            off           {current?}                   remove the password and the key kept here

        A password or a key set here replaces the one LLMSIM_ADMIN_PASSWORD or LLMSIM_API_KEY gave;
        what the environment gave cannot be removed from here."""
        if self.foreign_origin():
            return self.send_json(403, {"ok": False, "message": "That request came from another site."})
        action = body.get("action")
        if action not in ("password", "new-key", "remove-key", "off"):
            return self._access_reply(400, "action is password, new-key, remove-key or off.")
        if not self._current_password_ok(body):
            return self._access_reply(403, "Type your current password to change the sign-in.")
        signed_in_here = self.who not in (None, "key")
        with ACCESS_LOCK:
            if action == "password":
                password, user = str(body.get("password") or ""), str(body.get("user") or "").strip()
                if len(password) < MIN_PASSWORD:
                    return self._access_reply(400, "A password is at least %d characters." % MIN_PASSWORD)
                if user and user != access_user():
                    if not re.fullmatch(r"[A-Za-z0-9._@-]{1,64}", user):
                        return self._access_reply(400, "A user name is letters, digits, dot, dash, underscore or @, up to 64.")
                    ACCESS["user"] = user
                was_on = access_on()
                ACCESS["password"], ACCESS["changed"] = hash_password(password), int(time.time())
                save_access()
                log("access: the console password was %s in Settings" % ("changed" if was_on and signed_in_here else "set"))
                # Whoever set it stays signed in: everybody else's cookie stopped working with the old password.
                cookie = None if self.who == "key" else self._cookie_header(session_cookie_for(access_user()), SESSION_SECONDS)
                return self._access_reply(200, "Saved. The console now asks for a sign-in as %s." % access_user(), cookie)
            if action == "new-key":
                if not password_set():
                    return self._access_reply(409, "Set a console password first. A key alone would lock this page out.")
                key = "llmsim-" + base64.urlsafe_b64encode(os.urandom(30)).decode().rstrip("=")
                ACCESS["key"], ACCESS["keyHint"], ACCESS["changed"] = hash_key(key), "…" + key[-4:], int(time.time())
                save_access()
                log("access: a new API key was made in Settings (%s); the one before, if any, no longer works" % ACCESS["keyHint"])
                return self._access_reply(200, "Made. Copy it now: it is not shown again.", newKey=key)
            if action == "remove-key":
                if not ACCESS["key"]:
                    return self._access_reply(409, "This key is LLMSIM_API_KEY, where LLMSim is started. Remove it there, or make a new key here to replace it.")
                ACCESS["key"], ACCESS["keyHint"], ACCESS["changed"] = "", "", int(time.time())
                save_access()
                log("access: the API key made in Settings was removed")
                return self._access_reply(200, "Removed. The key from LLMSIM_API_KEY works again." if ENV_KEY
                                          else "Removed. Scripts can no longer reach /api with it.")
            if ENV_PASSWORD:
                return self._access_reply(409, "LLMSIM_ADMIN_PASSWORD is set where LLMSim is started, so the sign-in stays on. Remove it there.")
            ACCESS.update(user="", password="", key="", keyHint="", changed=int(time.time()))
            save_access()
            log("access: the sign-in was turned off in Settings; LLMSim is OPEN")
            return self._access_reply(200, "Turned off. Anyone who can reach this port can use the console and the API.", self._cookie_header("", 0))

    def _secure_cookie(self):
        return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" or os.environ.get("LLMSIM_COOKIE_SECURE") == "1"

    def _cookie_header(self, value, max_age):
        return "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s" % (
            SESSION_COOKIE, value, max_age, "; Secure" if self._secure_cookie() else "")

    def login_post(self, raw):
        """The console's sign-in, {user, password}: a cookie when they match. JSON in and
        out, so the console asks for it in front of its own page and nothing is redirected."""
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = None
        if not isinstance(body, dict):
            return self.send_json(400, {"ok": False, "message": "Send {\"user\": …, \"password\": …} as JSON."})
        user, password = str(body.get("user") or ""), str(body.get("password") or "")
        if not access_on():
            return self.send_json(200, {"ok": True, "on": False, "user": None})
        if self.foreign_origin():
            return self.send_json(403, {"ok": False, "message": "That sign-in came from another site."})
        address = self.client_address[0]
        wait = sign_in_wait(address)
        if wait:
            return self.send_json(429, {"ok": False, "message": "Too many wrong passwords. Try again in %d seconds." % wait})
        ok = password_ok(user, password)
        sign_in_result(address, ok)
        if not ok:
            log("access: a sign-in as %r from %s was refused" % (user[:64], address))
            return self.send_json(401, {"ok": False, "message": "That user name and password do not match."})
        log("access: %s signed in from %s" % (user, address))
        return self._reply(200, {"ok": True, "on": True, "user": user},
                           self._cookie_header(session_cookie_for(user), SESSION_SECONDS))

    def logout_post(self):
        if self.foreign_origin():
            return self.send_json(403, {"ok": False, "message": "That request came from another site."})
        return self._reply(200, {"ok": True, "on": access_on(), "user": None}, self._cookie_header("", 0))

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
        validation, the script, relay, Anthropic and the Logs page all
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

    def _session_reply(self, ok, message, code, run=None, **extra):
        return self.send_json(code, dict({"ok": ok, "message": message, "status": session_status(run)}, **extra))

    def session_console(self, body):
        """The console's route, one action per call. See session_api for the one
        a test harness uses; both end in the same functions.

        Refused from another site for the reason the other console routes are:
        a replay changes what every request is answered with.
        """
        if self.foreign_origin():
            return self.send_json(403, {"error": "That request came from another site."})
        action, name, run = body.get("action"), body.get("name"), body.get("run") or None
        if run is not None and not (isinstance(run, str) and ID_RE.fullmatch(run)):
            return self._session_reply(False, "A run id is letters, digits, dot, dash, underscore or colon, up to 64.", 400)
        if action in ("record", "replay"):
            return self._session_reply(*session_start(action, name, masks=body.get("masks"), on_miss=body.get("onMiss"),
                                                      strict=body.get("strict"), overwrite=bool(body.get("overwrite")), run=run), run=run)
        if action == "auto":
            return self._session_reply(*session_auto(name, masks=body.get("masks"), on_miss=body.get("onMiss"),
                                                     strict=body.get("strict"), run=run), run=run)
        if action == "stop":
            ok, message, code, summary = session_stop(run)
            return self._session_reply(ok, message, code, summary=summary, run=run)
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
            for st_ in _states():                    # the console's controls change the replays in front of it
                if st_["mode"] == "replay":
                    st_["onMiss"], st_["strict"] = on_miss, strict
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
        run = (body or {}).get("run") if isinstance(body, dict) else None
        run = run or (query.get("run") or [None])[0]
        if run is not None and not (isinstance(run, str) and ID_RE.fullmatch(run)):
            return self.send_json(400, {"ok": False, "message": "A run id is letters, digits, dot, dash, underscore or colon, up to 64."})
        if method == "GET" and not rest:
            return self.send_json(200, session_status(run))
        if method == "GET":
            return self.session_download(rest)
        if method == "DELETE" and rest:
            return self._session_reply(*session_delete(rest))
        if method == "PUT" and rest:
            overwrite = (query.get("overwrite") or [""])[0].lower() in ("1", "true", "yes")
            return self._session_reply(*session_import(rest, body, overwrite=overwrite))
        if method == "POST" and rest in ("record", "replay"):
            return self._session_reply(*session_start(rest, body.get("name"), masks=body.get("masks"), on_miss=body.get("onMiss"),
                                                      strict=body.get("strict"), overwrite=bool(body.get("overwrite")), run=run), run=run)
        if method == "POST" and rest == "auto":
            return self._session_reply(*session_auto(body.get("name"), masks=body.get("masks"),
                                                     on_miss=body.get("onMiss"), strict=body.get("strict"), run=run), run=run)
        if method == "POST" and rest == "stop":
            ok, message, code, summary = session_stop(run)
            return self._session_reply(ok, message, code, summary=summary, run=run)
        return self.send_json(404, {"ok": False, "message": "No such route: %s %s" % (method, path)})

    def begin_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.vendor_headers(200)
        self.end_headers()
        self.close_connection = True

    def _hang_up(self, mode="close"):
        """End the connection without finishing the answer.

        close is what a vendor's load balancer does mid-answer: a clean EOF, so the
        client sees a body that stops short. reset is the harder case — a TCP reset
        that surfaces as a connection error rather than as an incomplete answer.
        """
        self.close_connection = True
        try:
            if mode == "reset":
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                self.connection.close()
            else:
                self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _send_raw(self, status, content_type, raw):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.vendor_headers(status)
        self.end_headers()
        self.wfile.write(raw)

    def _stream_fault_step(self):
        """What a stream fault does before the next event is written."""
        fault, n = self.stream_fault, self._events
        kind = fault["type"]
        if kind == "slow_stream":
            time.sleep(fault["ms"] / 1000.0)
        elif kind == "abort_stream" and n >= fault.get("after", 3):
            log("   fault: hanging up after %d event(s)" % n)
            self._hang_up(fault.get("mode", "close"))
            raise FaultAbort("a stream fault cut this stream")
        elif n == fault.get("after", 3) and not self._fault_done:
            self._fault_done = True
            if kind == "stall":
                time.sleep(fault["ms"] / 1000.0)
            elif kind == "garbage" and fault.get("where", "frame") == "frame":
                self.wfile.write(b'data: {"this frame": is not json\n\n')
                self.wfile.flush()

    def write_event(self, event, data):
        if self.stream_fault:
            self._stream_fault_step()
        self._events += 1
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
            capture_note(self, route="relay", rule="relay:" + CONFIG["upstream"]["model"], text="".join(sent))
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

    def upstream_serve(self, resp, events, route, plan, shape, model, stream, body, messages, started):
        """Answer from an open stream of a real model, in the shape the client asked.

        `route` is which real model it is (anthropic) and `plan` the
        model it was asked for; `events` is its stream as canonical events.

        Whole when the client did not ask for a stream, and otherwise piece by
        piece as the model produces it. In both the failure that matters is the
        one after the first byte, when the status is already sent: it is
        reported inside the stream in the client's own error form, since
        nothing else is left to say it with.
        """
        entry = record({"provider": self.provider, "shape": shape, "model": model, "route": route,
                        "rule": route + ":" + plan, "stream": stream, "status": 200, "ms": 0,
                        "prompt": subject(messages)[:400], "reply": "", "tools": [], "usage": None, "error": None})
        self.trace["Rule"] = route + ":" + plan
        log("%s %s model=%s stream=%s -> %s:%s" % (self.provider, shape, model, stream, route, plan))

        def settle(**fields):
            entry.update(fields, ms=int((time.time() - started) * 1000))

        begun = False
        try:
            if not stream:
                turn = _collect(events, plan, "Anthropic")
                capture_note(self, route=route, rule=route + ":" + plan, text=turn.get("text") or "", usage=_usage_of(turn),
                             toolCalls=[{"id": c.get("id"), "name": c.get("name"), "input": c.get("input")} for c in turn.get("tool_calls") or []])
                session_capture(self.ctx, route, route + ":" + plan, turn, int((time.time() - started) * 1000))
                settle(reply=(turn.get("text") or "")[:400], usage=turn["usage"],
                       tools=[c["name"] for c in turn.get("tool_calls") or []])
                build = (anthropic_body if shape == "anthropic"
                         else responses_body if shape == "responses" else openai_body)
                return self.send_json(200, build(turn, model))

            enc, said, made = model_route_encoder(shape, model, body), [], []
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
                    capture_note(self, route=route, rule=route + ":" + plan, text="".join(said), toolCalls=made,
                                 usage={"input": u["input"], "output": u["output"], "cacheRead": u["cached"], "reasoning": u["reasoning"]})
                    session_capture(self.ctx, route, route + ":" + plan, {
                        "text": "".join(said), "tool_calls": made,
                        "usage": {"input": u["input"], "output": u["output"], "cacheRead": u["cached"], "reasoning": u["reasoning"]}},
                        int((time.time() - started) * 1000))
                    settle(reply="".join(said)[:400],
                           usage={"input": u["input"], "output": u["output"], "cacheRead": u["cached"], "model": plan})
        except ModelRouteError as exc:
            log(route + " failed %s: %s" % ("mid-stream" if begun else "before the first word", exc.message))
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
        """Answer one request, and keep what it was and what answered it."""
        model = body.get("model") or (MODELS[self.provider][0])
        self.cap = capture_begin(self, body, shape, model, bool(body.get("stream")))
        testkit.capture_started(self.cap)
        try:
            with testkit.model_guard(self, body, shape):      # a test may have put this request in an order
                return self._handle_chat(body, shape)
        finally:
            capture_finish(self)

    def _view(self, body, shape, model, stream, route):
        """This request as rules, faults and assertions see it."""
        meta = {"provider": self.provider, "shape": shape, "model": model, "path": self.path.split("?")[0], "stream": stream,
                "route": route, "identity": getattr(self, "identity", {}), "credential": getattr(self, "cred", {}),
                "headers": {k.lower(): v for k, v in self.headers.items() if k.lower() not in SENSITIVE_HEADERS}}
        return build_view(meta, body)

    def _handle_chat(self, body, shape):
        model = body.get("model") or (MODELS[self.provider][0])
        messages = responses_to_messages(body) if shape == "responses" else (body.get("messages") or [])
        stream = bool(body.get("stream"))
        declared = declared_tools(body)
        # What a recording needs to know about this request, wherever in the
        # several paths below the answer ends up being produced.
        run = (getattr(self, "identity", {}) or {}).get("run")
        st = SESSIONS.get(run) if run and SESSIONS.get(run) and SESSIONS[run]["mode"] != "off" else SESSION
        self.ctx = {"shape": shape, "model": model, "messages": messages,
                    "provider": self.provider, "declared": sorted(declared), "st": st}

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
        replaying = st["mode"] == "replay" and not internal
        replayed = session_replay(messages, st, sorted(declared)) if replaying else None
        if replaying and not replayed and st["onMiss"] != "route":
            ask, n, _, _ = exchange_key(messages, st)
            message = ("LLMSim has no recording for this request. Cassette %s, asked %r, %d turn(s) in. "
                       "Record it again, or set misses to route normally." % (st["name"], ask[:80], n))
            log("replay miss: " + message)
            record({"provider": self.provider, "shape": shape, "model": model, "route": "replay",
                    "rule": None, "stream": stream, "status": 404, "ms": 0,
                    "prompt": subject(messages)[:400], "reply": None, "tools": [], "error": message})
            self.trace = {"Route": "replay"}
            capture_note(self, route="replay", rule="")
            return self.refuse(404, "not_found_error", message)
        if replayed:
            route = "replay"
        self.trace = {"Route": route}
        view = self._view(body, shape, model, stream, route)

        # A scenario that brought its own script is answered by it wherever it has a rule for
        # the request, whatever this LLMSim's default route is: a test that scripts the model
        # must not quietly get a real one because somebody's default is a real model. What
        # its script does not cover still goes where Routing says, so a run can script the
        # steps that matter and let a real model do the rest, unless it asked to be refused.
        if not internal and not replayed:
            own = testkit.run_script(view)
            if own == "scripted" and route != "scripted":
                route = view["route"] = self.trace["Route"] = "scripted"
            elif own == "refuse":
                message = ("This run's script has no rule for this request, and the scenario says such requests are refused "
                           "(model.unmatched). Asked %r, turn %d." % (subject(messages)[:80], view.get("turn", 0)))
                log("   refused: " + message)
                record({"provider": self.provider, "shape": shape, "model": model, "route": "refused", "rule": None, "stream": stream,
                        "status": 400, "ms": 0, "prompt": subject(messages)[:400], "reply": None, "tools": [], "error": message})
                self.trace["Route"] = "refused"
                capture_note(self, route="refused", rule="")
                return self.refuse(400, "invalid_request_error", message)
        # Noted now, not only once an answer is made: a request whose route fails — a plan nobody
        # is signed in to, an upstream that is down — still went there, and a run counts it there.
        capture_note(self, route=route)

        # A fault rule picks its request before anything is spent on an answer, so
        # a status or a hang-up never costs an upstream call. The runner's own
        # requests are checking a caller against the script and are left alone.
        fault_rule, fault = (None, None) if internal else pick_fault(view)
        if fault:
            self.trace["Fault"] = fault["type"]
            log("   fault: %s (%s)" % (fault["type"], fault_rule.get("name") or fault_rule["id"]))
            capture_note(self, route=route, fault=fault["type"])
            early = {"prompt": subject(messages)[:400], "reply": None, "tools": [], "provider": self.provider, "shape": shape,
                     "model": model, "route": route, "rule": None, "stream": stream, "ms": 0, "fault": fault["type"]}
            if fault["type"] == "delay":
                time.sleep(fault["ms"] / 1000.0)
            elif fault["type"] == "drop":
                record(dict(early, status=0, error="LLMSim hung up before answering."))
                self._hang_up(fault.get("mode", "close"))
                return None
            elif fault["type"] == "status":
                turn_err, _ = apply_turn_fault({}, fault)
                err = turn_err["error"]
                if err.get("retry_after") is not None:
                    self._extra["Retry-After"] = str(err["retry_after"])
                record(dict(early, status=err["status"], error=err["message"]))
                return self.refuse(err["status"], err["type"], err["message"])
            elif fault["type"] in STREAM_FAULTS:
                self.stream_fault = fault
            else:
                self.fault = fault

        if route == "replay":
            entry_hit, differs = replayed
            name = "replay:%s%s" % (st["name"], " (last message differs)" if differs else "")
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
                name, turn = choose_turn(self.provider, model, messages, declared, view)
                name = "fallback:" + name
        elif route == "anthropic":
            canon = canon_request(shape, body)
            try:
                _anthropic_auth_headers()            # no key is the first thing to say, before which model
                target = anthropic_model(model)
                resp = anthropic_open(anthropic_payload(canon, target, body))
            except AnthropicError as exc:
                log("anthropic failed before it began: %s" % exc.message)
                if not CONFIG["anthropic"]["fallback"]:
                    record({"provider": self.provider, "shape": shape, "model": model,
                            "route": route, "rule": None, "stream": stream, "status": exc.status,
                            "ms": int((time.time() - started) * 1000),
                            "prompt": subject(messages)[:400], "reply": None, "tools": [],
                            "error": exc.message})
                    return self.refuse(exc.status, exc.kind, exc.message)
                name, turn = choose_turn(self.provider, model, messages, declared, view)
                name = "fallback:" + name
            else:
                unsent = anthropic_not_sent(canon, body)
                if unsent:
                    log("   not sent to Anthropic, which this route cannot pass on: %s" % ", ".join(unsent))
                return self.upstream_serve(resp, anthropic_events(resp), "anthropic", target, shape, model, stream, body, messages, started)
        else:
            name, turn = choose_turn(self.provider, model, messages, declared, view)

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

        built = (anthropic_body if shape == "anthropic" else responses_body if shape == "responses" else openai_body)
        capture_note(self, route=route, rule=name, text=turn.get("text") or "", usage=usage_from_body(shape, built(turn, model)),
                     toolCalls=[{"id": c.get("id"), "name": c.get("name"), "input": c.get("input")} for c in turn.get("tool_calls") or []])
        if route != "replay" and not internal and not turn.get("error"):
            session_capture(self.ctx, route, name, turn, int((time.time() - started) * 1000))

        # A fault is what happens to an answer, never the answer itself — which is why
        # this comes after the answer has been recorded. A rule's fault applies
        # first; failing that, the turn's own.
        post = None
        turn_fault = turn.get("fault")
        # A garbage fault is a corrupt frame in a stream and a corrupt body in a
        # whole answer: the same fault, in whichever form the request asked for.
        if not stream and self.stream_fault and self.stream_fault["type"] == "garbage":
            self.fault, self.stream_fault = self.stream_fault, None
        fault_now = self.fault or turn_fault
        if fault_now:
            kind = fault_now["type"]
            if kind == "garbage" and not stream:
                post = "garbage_body"
            elif kind in STREAM_FAULTS:
                self.stream_fault = fault_now
            elif kind == "drop":
                capture_note(self, fault="drop")
                entry["fault"], entry["status"] = "drop", 0
                self._hang_up(fault_now.get("mode", "close"))
                return None
            elif kind == "delay":
                if turn_fault:
                    turn = dict(turn, delay_ms=turn.get("delay_ms", 0) + fault_now["ms"])
            else:
                turn, post = apply_turn_fault(turn, fault_now)
            capture_note(self, fault=kind)
            entry["fault"] = kind
            self.trace["Fault"] = kind

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
            if err.get("retry_after") is not None:
                self._extra["Retry-After"] = str(err["retry_after"])
            return self.refuse(err.get("status", 429), err.get("type", "rate_limit_error"),
                               err.get("message", "Rate limited."))

        if not stream and post == "garbage_body":
            return self._send_raw(200, "application/json",
                                  b'{"id": "chatcmpl-llmsim", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assis')

        if not stream:
            payload = (anthropic_body(turn, model) if shape == "anthropic"
                       else responses_body(turn, model) if shape == "responses"
                       else openai_body(turn, model))
            return self.send_json(200, payload)

        # Connection: close, not keep-alive, and the handler stops reusing the socket
        # (begin_stream does both). An event stream has no Content-Length and this
        # server speaks HTTP/1.1, so a kept-alive connection leaves the client with no
        # way to know the body ended — it reads the last event and then blocks until
        # it times out. Closing is what marks the end.
        self.begin_stream()
        try:
            if shape in ("anthropic", "responses"):
                events = anthropic_sse(turn, model) if shape == "anthropic" else responses_sse(turn, model)
                for event, data in events:
                    self.write_event(event, data)
            else:
                for chunk in openai_sse(turn, model):
                    self.write_event("", chunk)
                self.write_event("", "[DONE]")
        except BrokenPipeError:
            log("client closed the stream early")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    testkit.attach(sys.modules[__name__])
    log("listening on http://localhost:%d" % PORT)
    if state_store()["kept"]:
        log("state:  %s" % DATA_DIR)
    else:
        log("state:  %s is inside this container — a restart forgets keys, routing and rules;"
            " mount a volume there (LLMSIM_DATA)" % DATA_DIR)
    USER_RULES.update(load_user_rules())
    ASSERTION_SETS.update(load_assertion_sets())
    log("script: %s (%d rules%s)" % (SCRIPT_PATH, len(SCRIPT.get("rules", [])),
                                     ", and %d of yours in %s" % (len(USER_RULES["rules"]), os.path.basename(RULES_PATH)) if USER_RULES["rules"] else ""))
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
    claude = CONFIG["anthropic"]
    routed_to_claude = [m for m, r in CONFIG["routing"]["models"].items() if r == "anthropic"]
    if CONFIG["routing"]["default"] == "anthropic" or routed_to_claude:
        if claude["key"]:
            log("anthropic: answers with %s at %s%s — this is billed to the key"
                % (claude["model"] or "the model each client asks for", claude["url"],
                   " for the default route" if CONFIG["routing"]["default"] == "anthropic" else ""))
        else:
            log("anthropic: routes point at Anthropic but no key is set — those requests will fail until one is")
    if access_on():
        st = access_status()
        log("access: the console and /api need a sign-in (user %s, password from %s)%s"
            % (st["user"], "LLMSIM_ADMIN_PASSWORD" if st["password"]["from"] == "env" else
               "Settings, replacing LLMSIM_ADMIN_PASSWORD" if st["password"]["env"] else "Settings",
               ", or the API key from %s" % ("LLMSIM_API_KEY" if st["key"]["from"] == "env" else "Settings")
               if st["key"]["set"] else " — no API key is set, so scripts cannot reach /api"))
    elif key_set():
        log("access: OPEN — an API key is set but no console password, so the key is not used. Set LLMSIM_ADMIN_PASSWORD too.")
    else:
        log("access: open (no LLMSIM_ADMIN_PASSWORD set); a sign-in can be turned on at /ui#access")
    log("console: http://localhost:%d/ui   guide: http://localhost:%d/guide" % (PORT, PORT))
    for name in sorted(API_KEYS):
        log("  %-9s http://localhost:%d/%s   key %s" % (name, PORT, name, API_KEYS[name]))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
