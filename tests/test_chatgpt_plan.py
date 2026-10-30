#!/usr/bin/env python3
"""The ChatGPT plan route, end to end, against a stand-in for OpenAI.

    python3 tests/test_chatgpt_plan.py

No network, no account, no install. A stub plays both halves of OpenAI —
Sign in with ChatGPT (registration, token, revoke, its published configuration
and signing keys) and the plan's route (/v1/responses, /v1/models) — and is
deliberately strict where the real one is: it checks the PKCE verifier against
the challenge, signs its ID tokens with a real RSA key, issues a client id at
registration, refuses a request without store:false and stream:true or with
tools outside a namespace, refuses a stale bearer token, and rotates refresh
tokens. Anything that looks like the Codex client is refused. LLMSim is started
as a subprocess pointed at it, with its own throwaway config and token file, so
nothing here touches a real setup.

What this proves is that LLMSim speaks the contract OpenAI documents. It cannot
prove OpenAI honours that contract today; only a real sign-in does.
"""

import base64
import hashlib
import http.client
import json
import os
import random
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


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _is_prime(n):
    if n % 2 == 0:
        return n == 2
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(12):
        x = pow(random.randrange(2, n - 1), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    while True:
        n = random.getrandbits(bits) | (1 << (bits - 1)) | 1
        if _is_prime(n):
            return n


class RSA:
    """A throwaway signing key, made fresh each run, so what LLMSim verifies is a real signature."""

    def __init__(self, kid="stub-key"):
        self.kid, self.e = kid, 65537
        while True:
            p, q = _prime(1024), _prime(1024)
            phi = (p - 1) * (q - 1)
            if p != q and phi % self.e:
                break
        self.n, self.d = p * q, pow(self.e, -1, phi)

    def public(self):
        size = (self.n.bit_length() + 7) // 8
        return {"kid": self.kid, "kty": "RSA", "alg": "RS256", "use": "sig",
                "n": b64(self.n.to_bytes(size, "big")), "e": b64(self.e.to_bytes(3, "big"))}

    def sign(self, message):
        size = (self.n.bit_length() + 7) // 8
        digest = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(message).digest()
        padded = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
        return pow(int.from_bytes(padded, "big"), self.d, self.n).to_bytes(size, "big")


def jwt(claims, key=None):
    """Signed when given a key; an access token here is opaque to LLMSim, so it need not be."""
    head = {"alg": "RS256", "kid": key.kid} if key else {"alg": "none"}
    signed = b64(json.dumps(head).encode()) + "." + b64(json.dumps(claims).encode())
    return signed + "." + (b64(key.sign(signed.encode())) if key else "sig")


PLAN_SCOPE = "chatgpt.tokens.use.direct"
FULL_SCOPE = "openid profile email offline_access resource.invoke " + PLAN_SCOPE
RESOURCE = "https://api.openai.com/v1"


# ── The stand-in for OpenAI ──────────────────────────────────────────────

class Stub:
    def __init__(self):
        self.port = free_port()
        self.key = RSA()
        self.reset()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def reply(self, status, body, kind="application/json", extra=None):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                stub.handle(self, "GET")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.body = self.rfile.read(length) if length else b""
                stub.handle(self, "POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reset(self):
        self.codes = {}                 # authorization code -> (challenge, redirect_uri)
        self.refresh_token = None       # the one currently valid
        self.access_token = None
        self.access_ttl = 3600
        self.refresh_calls = 0
        self.refresh_fails = False
        self.revoked = []
        self.authorize_seen = []        # every /authorize query, as a dict
        self.clients_issued = 0
        self.known_clients = set()
        self.client_id = None           # the client the current tokens belong to
        self.subject = "user-abc"       # who is at the keyboard at OpenAI
        self.grant_scope = FULL_SCOPE   # what they allow
        self.deny = False               # they press Cancel
        self.omit_issued_client = False
        self.wrong_issued_client = None
        self.bad_id_token = None        # signature, nonce, audience, issuer or expired
        self.fail_next = None           # (status, error object) for the next /responses
        self.fail_midstream = None      # an error object for response.failed, after some text
        self.last = None                # the last /responses request: headers + json
        self.expire_next = False        # answer the next /responses with 401, once
        self.issued = 0
        self.models_calls = 0           # how often the plan's model list was asked for
        self.models_fail = False

    def mint(self, client_id, nonce=None):
        self.issued += 1
        self.client_id = client_id
        self.access_token = jwt({"exp": int(time.time()) + self.access_ttl, "n": self.issued, "aud": RESOURCE,
                                 "client_id": client_id, "https://api.openai.com/auth": {"per_user_salt": "opaque"}})
        self.refresh_token = "rt-%d" % self.issued
        now = int(time.time())
        claims = {"iss": self.issuer, "sub": self.subject, "aud": client_id, "exp": now + 3600, "iat": now,
                  "email": "eti@example.com", "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}}
        if nonce:
            claims["nonce"] = nonce
        key = self.key
        if self.bad_id_token == "nonce":
            claims["nonce"] = "not-the-one"
        elif self.bad_id_token == "audience":
            claims["aud"] = "oaiapp_somebody_else"
        elif self.bad_id_token == "issuer":
            claims["iss"] = "https://evil.example"
        elif self.bad_id_token == "expired":
            claims["exp"] = now - 60
        elif self.bad_id_token == "signature":
            key = RSA.__new__(RSA)                # the same kid, and a key that is not the published one
            key.__dict__.update(self.key.__dict__)
            key.d = self.key.d + 2
        return {"access_token": self.access_token, "refresh_token": self.refresh_token,
                "id_token": jwt(claims, key), "token_type": "Bearer", "expires_in": self.access_ttl,
                "scope": self.grant_scope}

    @property
    def issuer(self):
        return "http://127.0.0.1:%d" % self.port

    def handle(self, h, method):
        parts = urllib.parse.urlsplit(h.path)
        q = urllib.parse.parse_qs(parts.query)
        path = parts.path

        if path == "/.well-known/openid-configuration":
            return h.reply(200, {"issuer": self.issuer, "jwks_uri": self.issuer + "/.well-known/jwks.json",
                                 "revocation_endpoint": self.issuer + "/api/accounts/oauth/revoke"})
        if path == "/.well-known/jwks.json":
            return h.reply(200, {"keys": [self.key.public()]})

        if path == "/api/accounts/authorize":
            seen = {k: v[0] for k, v in q.items()}
            self.authorize_seen.append(seen)
            for wrong in ("originator", "codex_cli_simplified_flow", "id_token_add_organizations"):
                if wrong in seen:
                    return h.reply(400, {"error": "that is the Codex client's %s" % wrong})
            need = {"response_type": "code", "code_challenge_method": "S256", "scope": FULL_SCOPE, "resource": RESOURCE}
            for k, v in need.items():
                if seen.get(k) != v:
                    return h.reply(400, {"error": "bad %s" % k})
            for k in ("state", "nonce", "code_challenge", "ext_agent_host_id", "redirect_uri", "client_id"):
                if not seen.get(k):
                    return h.reply(400, {"error": "no %s" % k})
            if not seen["redirect_uri"].startswith("http://127.0.0.1:") or not seen["redirect_uri"].endswith("/auth/callback"):
                return h.reply(400, {"error": "the redirect must be a 127.0.0.1 loopback"})
            client = seen["client_id"]
            back = {"state": seen["state"]}
            if self.deny:
                back["error"] = "access_denied"
                return h.reply(302, b"", extra={"Location": seen["redirect_uri"] + "?" + urllib.parse.urlencode(back)})
            if client == "dynamic_agent_client":
                if not seen.get("agent_name_hint"):
                    return h.reply(400, {"error": "a new registration needs agent_name_hint"})
                self.clients_issued += 1
                client = "oaiapp_%d" % self.clients_issued
                self.known_clients.add(client)
                if not self.omit_issued_client:
                    back["client_id"] = client
            else:
                if client not in self.known_clients:
                    return h.reply(400, {"error": "invalid_client"})
                if seen.get("agent_name_hint"):
                    return h.reply(400, {"error": "agent_name_hint is for a first registration only"})
            if self.wrong_issued_client:
                back["client_id"] = self.wrong_issued_client
            code = "code-%d" % len(self.codes)
            self.codes[code] = (seen["code_challenge"], seen["redirect_uri"], client, seen["nonce"])
            back.update(code=code, scope=self.grant_scope)
            return h.reply(302, b"", extra={"Location": seen["redirect_uri"] + "?" + urllib.parse.urlencode(back)})

        if path == "/api/accounts/oauth/token":
            if "x-www-form-urlencoded" not in (h.headers.get("Content-Type") or ""):
                return h.reply(400, {"error": "invalid_request", "error_description": "form-encoded only"})
            body = {k: v[0] for k, v in urllib.parse.parse_qs(h.body.decode()).items()}
            if body.get("resource") != RESOURCE:
                return h.reply(400, {"error": "invalid_target"})
            if body.get("grant_type") == "refresh_token":
                self.refresh_calls += 1
                if self.refresh_fails:
                    return h.reply(400, {"error": "refresh_token_reused", "error_description": "refresh token reused"})
                if "scope" in body or body.get("client_id") != self.client_id or body.get("refresh_token") != self.refresh_token:
                    return h.reply(400, {"error": "invalid_grant"})
                return h.reply(200, self.mint(self.client_id))
            challenge, redirect, client, nonce = self.codes.pop(body.get("code"), (None, None, None, None))
            if not challenge:
                return h.reply(400, {"error": "invalid_grant"})
            if b64(hashlib.sha256(body.get("code_verifier", "").encode()).digest()) != challenge:
                return h.reply(400, {"error": "invalid_grant", "error_description": "PKCE verifier mismatch"})
            if body.get("redirect_uri") != redirect or body.get("grant_type") != "authorization_code":
                return h.reply(400, {"error": "invalid_grant", "error_description": "redirect mismatch"})
            if body.get("client_id") != client:
                return h.reply(400, {"error": "invalid_client"})
            return h.reply(200, self.mint(client, nonce))

        if path == "/api/accounts/oauth/revoke":
            if "x-www-form-urlencoded" not in (h.headers.get("Content-Type") or ""):
                return h.reply(400, {"error": "form-encoded only"})
            body = {k: v[0] for k, v in urllib.parse.parse_qs(h.body.decode()).items()}
            if body.get("client_id") != self.client_id or body.get("token_type_hint") != "refresh_token":
                return h.reply(400, {"error": "invalid_client"})
            self.revoked.append(body.get("token"))
            return h.reply(200, b"")

        if path == "/v1/models":
            self.models_calls += 1
            if self.models_fail:
                return h.reply(500, {"error": {"message": "the model list is down"}})
            # The hidden model comes first: the trap in taking the top of the list. The rest are in the
            # server's own order, which is the plan's preference, and which is not alphabetical.
            return h.reply(200, {"models": [
                {"slug": "gpt-hidden", "visibility": "hide", "display_name": "Hidden"},
                {"slug": "gpt-5.4", "visibility": "list", "display_name": "GPT-5.4"},
                {"slug": "gpt-5.4-mini", "visibility": "list", "display_name": "GPT-5.4 mini"}]})

        if path == "/v1/responses":
            return self.responses(h)
        return h.reply(404, {"error": "no " + path})

    def responses(self, h):
        if h.headers.get("Authorization") != "Bearer %s" % self.access_token:
            return h.reply(401, {"error": {"message": "stale token"}})
        if self.expire_next:
            self.expire_next = False
            return h.reply(401, {"error": {"message": "token revoked"}})
        for codex in ("ChatGPT-Account-ID", "originator", "session_id"):
            if h.headers.get(codex):
                return h.reply(400, {"error": {"message": "%s is the Codex client's header" % codex}})
        if self.fail_next:
            status, err = self.fail_next
            self.fail_next = None
            return h.reply(status, {"error": err})
        req = json.loads(h.body)
        # Lower-cased: HTTP header names are case-insensitive and urllib
        # title-cases whatever it sends, so a test must not care which.
        self.last = {"headers": {k.lower(): v for k, v in h.headers.items()}, "json": req}
        # The two the real backend insists on, and the ones the brief names.
        if req.get("store") is not False or req.get("stream") is not True or not req.get("instructions"):
            return h.reply(400, {"error": {"message": "needs store:false, stream:true and instructions"}})
        for banned in ("temperature", "max_output_tokens", "max_tokens", "top_p", "background", "conversation",
                       "max_tool_calls", "metadata", "moderation", "multi_agent", "prompt", "prompt_cache_retention",
                       "safety_identifier", "top_logprobs", "truncation", "user", "previous_response_id"):
            if banned in req:
                return h.reply(400, {"error": {"code": "subscription_sharing_unsupported_capability", "param": banned,
                                               "message": "Unsupported parameter: %s" % banned}})
        for item in req.get("input") or []:
            if item.get("role") == "system":
                return h.reply(400, {"error": {"code": "subscription_sharing_unsupported_capability", "param": "input",
                                               "message": "explicit system messages are rejected"}})
            if item.get("type") == "function_call" and item.get("namespace") != "client_tools":
                return h.reply(400, {"error": {"message": "that call's namespace was not declared: %s" % item.get("namespace")}})
        for tool in req.get("tools") or []:
            if tool.get("type") != "namespace" or not tool.get("name") or not tool.get("description") or not tool.get("tools"):
                return h.reply(400, {"error": {"code": "subscription_sharing_unsupported_capability", "param": "tools",
                                               "message": "function tools must be grouped in a namespace"}})

        # As the real route answers: a function tool_choice is looked for at the top level, where a namespace hides it.
        if isinstance(req.get("tool_choice"), dict):
            return h.reply(400, {"error": {"message": "Tool choice '%s' not found in 'tools' parameter." % req["tool_choice"].get("name")}})

        ask = " ".join(c.get("text", "") for i in req["input"] if i.get("type") == "message"
                       and i["role"] == "user" for c in i["content"]).lower()
        answered = [i for i in req["input"] if i.get("type") == "function_call_output"]

        if "limit please" in ask:
            return h.reply(429, {"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "plan limit."}})

        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Connection", "close")
        h.end_headers()
        h.close_connection = True

        def sse(kind, **data):
            h.wfile.write(("event: %s\ndata: %s\n\n" % (kind, json.dumps(dict(type=kind, **data)))).encode())
            h.wfile.flush()

        sse("response.created", response={"id": "resp_1", "status": "in_progress"})
        if "break midway" in ask:
            sse("response.output_text.delta", delta="Starting, ")
            return sse("response.failed", response={"error": self.fail_midstream or {"code": "server_error", "message": "boom"}})
        if "use the tool" in ask and not answered and req.get("tools"):
            name = req["tools"][0]["tools"][0]["name"]
            call = {"type": "function_call", "id": "fc_1", "call_id": "call_abc", "name": name, "namespace": "client_tools",
                    "arguments": json.dumps({"city": "Paris"}), "status": "completed"}
            sse("response.output_text.delta", delta="Checking. ")
            sse("response.output_item.added", item=dict(call, arguments=""))
            sse("response.function_call_arguments.delta", delta=call["arguments"])
            sse("response.output_item.done", item=call)
            return sse("response.completed", response={"usage": {
                "input_tokens": 50, "output_tokens": 9, "input_tokens_details": {"cached_tokens": 20},
                "output_tokens_details": {"reasoning_tokens": 3}}})

        if "then stop" in ask and answered:
            # The work is done and the model has nothing to add: a completed answer with nothing in it.
            return sse("response.completed", response={"usage": {
                "input_tokens": 60, "output_tokens": 4, "input_tokens_details": {"cached_tokens": 50},
                "output_tokens_details": {"reasoning_tokens": 0}}})

        said = ("The tool said: %s" % answered[-1]["output"]) if answered else "Hello from the plan: %s" % ask[:60]
        for piece in (said[:6], said[6:]):
            sse("response.output_text.delta", delta=piece)
        sse("response.completed", response={"usage": {
            "input_tokens": 40, "output_tokens": 7, "input_tokens_details": {"cached_tokens": 10},
            "output_tokens_details": {"reasoning_tokens": 0}}})


# ── LLMSim, as a subprocess, pointed at the stub ─────────────────────────

class Sim:
    def __init__(self, stub, workdir, callback_ports=None):
        self.port = free_port()
        self.callback_port = free_port()
        self.workdir = workdir
        self.auth_path = os.path.join(workdir, "auth.json")
        self.env = dict(
            os.environ, LLMSIM_PORT=str(self.port), LLMSIM_CONFIG=os.path.join(workdir, "config.json"),
            LLMSIM_SUITE=os.path.join(workdir, "suite.json"), LLMSIM_CHATGPT_AUTH=self.auth_path,
            LLMSIM_CASSETTES=os.path.join(workdir, "cassettes"),
            LLMSIM_RULES=os.path.join(workdir, "rules.json"), LLMSIM_ASSERTIONS=os.path.join(workdir, "assertions.json"),
            LLMSIM_RUNS_DB=os.path.join(workdir, "runs.db"), LLMSIM_CREDENTIALS=os.path.join(workdir, "credentials.json"),
            LLMSIM_CHATGPT_ISSUER="http://127.0.0.1:%d" % stub.port,
            LLMSIM_CHATGPT_API="http://127.0.0.1:%d/v1" % stub.port,
            LLMSIM_CHATGPT_PORTS=callback_ports or str(self.callback_port))
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

    def sign_in(self):
        status, started = self.json("POST", "/api/ui/chatgpt/login")
        assert status == 200 and started["listening"], started
        page = urllib.request.urlopen(started["url"], timeout=10).read().decode()   # the stub "confirms access"
        assert "Signed in" in page, page
        return started


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


def case_unsigned_is_an_honest_error(sim, stub):
    sim.configure(routing={"default": "chatgpt"})
    status, headers, text = sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]})
    err = json.loads(text)["error"]
    assert status == 502 and err["type"] == "upstream_error" and "not signed in" in err["message"], text


def case_sign_in_with_pkce_and_no_secret_leaks(sim, stub):
    started = sim.sign_in()
    assert started["redirectUri"].startswith("http://127.0.0.1:%d/auth/callback" % sim.callback_port)
    _, status = sim.json("GET", "/api/ui/chatgpt")
    assert status["signedIn"] and status["email"] == "eti@example.com" and status["plan"] == "plus", status
    everything = json.dumps(status) + sim.call("GET", "/api/ui/state")[2] + sim.call("GET", "/healthz")[2]
    for secret in (stub.access_token, stub.refresh_token, stub.client_id):
        assert secret not in everything, "a credential reached the console"
    assert oct(os.stat(sim.auth_path).st_mode & 0o777) == "0o600", oct(os.stat(sim.auth_path).st_mode)


def case_chat_completions_plain_and_streamed(sim, stub):
    ask = {"model": "claude-sonnet-5", "temperature": 0.2, "max_tokens": 50, "messages": [
        {"role": "user", "content": "say hello"}]}
    status, _, text = sim.post(CHAT, ask)
    body = json.loads(text)
    assert status == 200 and body["choices"][0]["message"]["content"].startswith("Hello from the plan"), text
    assert body["choices"][0]["finish_reason"] == "stop" and body["usage"]["prompt_tokens"] == 40
    assert body["usage"]["prompt_tokens_details"]["cached_tokens"] == 10, "real usage was not carried"
    sent = stub.last["json"]
    assert sent["store"] is False and sent["stream"] is True, sent
    assert sent["model"] == "gpt-5.4", "the plan's model answers, not the client's name"
    assert sent["instructions"] == "You are a helpful assistant." and "temperature" not in sent
    assert sent["input"] == [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "say hello"}]}]
    assert "llmsim" in stub.last["headers"].get("user-agent", "").lower(), stub.last["headers"]

    status, headers, text = sim.post(CHAT, dict(ask, stream=True, stream_options={"include_usage": True}))
    out = frames(text)
    assert status == 200 and out[-1][1] == "[DONE]" and headers["Content-Type"].startswith("text/event-stream")
    chunks = [d for _, d in out if d != "[DONE]"]
    assert "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"]).startswith("Hello from")
    assert [c["choices"][0]["finish_reason"] for c in chunks if c["choices"]][-1] == "stop"
    assert chunks[-1]["usage"]["completion_tokens"] == 7, "include_usage was not honoured"


def case_chat_tool_call_round_trip(sim, stub):
    ask = {"model": "gpt-5", "tools": [WEATHER], "messages": [{"role": "user", "content": "use the tool for Paris"}]}
    _, _, text = sim.post(CHAT, ask)
    choice = json.loads(text)["choices"][0]
    call = choice["message"]["tool_calls"][0]
    assert choice["finish_reason"] == "tool_calls" and call["id"] == "call_abc", text
    assert call["function"]["name"] == "get_weather" and json.loads(call["function"]["arguments"]) == {"city": "Paris"}
    assert stub.last["json"]["tools"][0]["type"] == "namespace", stub.last["json"]["tools"]
    assert stub.last["json"]["tools"][0]["tools"][0]["name"] == "get_weather" and stub.last["json"]["tools"][0]["tools"][0]["type"] == "function"

    _, _, text = sim.post(CHAT, dict(ask, stream=True))
    deltas = [d["choices"][0]["delta"] for _, d in frames(text) if d != "[DONE]" and d["choices"]]
    streamed = [t for d in deltas for t in d.get("tool_calls", [])]
    assert streamed and streamed[0]["id"] == "call_abc" and streamed[0]["function"]["name"] == "get_weather"

    follow = dict(ask, messages=ask["messages"] + [
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_abc", "content": "18C and clear"}])
    _, _, text = sim.post(CHAT, follow)
    assert json.loads(text)["choices"][0]["message"]["content"] == "The tool said: 18C and clear", text
    items = stub.last["json"]["input"]
    assert [i["type"] for i in items] == ["message", "function_call", "function_call_output"], items
    assert items[1]["call_id"] == items[2]["call_id"] == "call_abc"


def case_anthropic_messages(sim, stub):
    ask = {"model": "claude-sonnet-5", "max_tokens": 64, "system": "Be brief.",
           "tools": [{"name": "get_weather", "description": "Weather", "input_schema": {"type": "object"}}],
           "messages": [{"role": "user", "content": "use the tool for Paris"}]}
    status, _, text = sim.post("/anthropic/v1/messages", ask, key=ANTHROPIC_KEY)
    body = json.loads(text)
    kinds = [b["type"] for b in body["content"]]
    assert status == 200 and kinds == ["text", "tool_use"] and body["stop_reason"] == "tool_use", text
    assert body["content"][1]["id"] == "call_abc" and body["content"][1]["input"] == {"city": "Paris"}
    assert body["usage"]["cache_read_input_tokens"] == 20 and body["usage"]["input_tokens"] == 30
    sent = stub.last["json"]
    assert sent["instructions"] == "Be brief." and sent["tools"][0]["tools"][0]["parameters"] == {"type": "object"}

    _, _, text = sim.post("/anthropic/v1/messages", dict(ask, stream=True), key=ANTHROPIC_KEY)
    names = [e for e, _ in frames(text)]
    assert names[0] == "message_start" and names[-1] == "message_stop" and "ping" in names
    starts = [d["content_block"]["type"] for e, d in frames(text) if e == "content_block_start"]
    assert starts == ["text", "tool_use"], starts
    delta = next(d for e, d in frames(text) if e == "message_delta")
    assert delta["delta"]["stop_reason"] == "tool_use" and delta["usage"]["output_tokens"] == 9

    follow = dict(ask, messages=ask["messages"] + [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_abc", "name": "get_weather", "input": {"city": "Paris"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_abc", "content": [{"type": "text", "text": "18C"}]}]}])
    _, _, text = sim.post("/anthropic/v1/messages", follow, key=ANTHROPIC_KEY)
    assert json.loads(text)["content"][0]["text"] == "The tool said: 18C", text


def case_responses_api(sim, stub):
    ask = {"model": "gpt-5", "instructions": "Be terse.", "input": "use the tool for Paris",
           "tools": [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}]}
    status, _, text = sim.post("/openai/v1/responses", ask)
    body = json.loads(text)
    assert status == 200 and [o["type"] for o in body["output"]] == ["message", "function_call"], text
    assert body["output"][1]["call_id"] == "call_abc" and body["usage"]["input_tokens"] == 50
    assert stub.last["json"]["instructions"] == "Be terse."

    _, _, text = sim.post("/openai/v1/responses", dict(ask, stream=True))
    names = [e for e, _ in frames(text)]
    assert names[0] == "response.created" and names[-1] == "response.completed", names
    assert "response.output_text.delta" in names and "response.function_call_arguments.done" in names
    final = frames(text)[-1][1]["response"]
    assert [o["type"] for o in final["output"]] == ["message", "function_call"] and final["output_text"] == "Checking. "


def case_plan_limit_and_midstream_failure(sim, stub):
    status, headers, text = sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "limit please"}]})
    err = json.loads(text)["error"]
    assert status == 429 and err["type"] == "rate_limit_error" and "https://chatgpt.com/settings/usage" in err["message"], text
    assert headers.get("retry-after"), "a 429 with no retry-after is a dead end"

    status, _, text = sim.post(CHAT, {"model": "gpt-5", "stream": True,
                                      "messages": [{"role": "user", "content": "break midway"}]})
    out = frames(text)
    assert status == 200 and out[-1][1] == "[DONE]" and "error" in out[-2][1], text
    assert out[0][1]["choices"][0]["delta"]["content"] == ""      # it had begun, and said so inside the stream


def case_token_refresh_rotates_and_persists(sim, stub):
    stub.access_ttl = 3600
    stored = json.load(open(sim.auth_path))
    stored["expires_at"] = time.time() + 30                       # inside the five-minute window
    json.dump(stored, open(sim.auth_path, "w"))
    sim.stop(); sim.start()                                       # a restart finds the file, as a person's would
    before = stub.refresh_calls
    status, _, _ = sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]})
    assert status == 200 and stub.refresh_calls == before + 1, "an expiring token was not refreshed"
    assert json.load(open(sim.auth_path))["refresh_token"] == stub.refresh_token, "the rotated token was not saved"


def case_revoked_token_is_refreshed_once_and_retried(sim, stub):
    stub.expire_next = True
    before = stub.refresh_calls
    status, _, text = sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]})
    assert status == 200 and stub.refresh_calls == before + 1, text


def case_dead_refresh_token_stops_asking(sim, stub):
    stub.refresh_fails = True
    stored = json.load(open(sim.auth_path))
    stored["expires_at"] = time.time() - 10
    json.dump(stored, open(sim.auth_path, "w"))
    sim.stop(); sim.start()
    before = stub.refresh_calls
    for _ in range(3):
        status, _, text = sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]})
        assert status == 502 and "Sign in" in json.loads(text)["error"]["message"], text
    assert stub.refresh_calls == before + 1, "a spent refresh token was retried on every request"
    assert sim.json("GET", "/api/ui/chatgpt")[1]["needsSignIn"] is True
    stub.refresh_fails = False


def case_script_is_untouched_and_fallback_is_opt_in(sim, stub):
    sim.configure(routing={"default": "scripted"})
    _, _, text = sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hello there"}]})
    assert "Hello from the plan" not in text and json.loads(text)["choices"], text
    sim.configure(routing={"default": "chatgpt"}, chatgpt={"fallback": True})
    _, _, text = sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hello there"}]})
    assert json.loads(text)["choices"][0]["message"], "fallback did not serve the script"
    sim.configure(chatgpt={"fallback": False})


def case_models_and_per_model_routing(sim, stub):
    sim.sign_in()
    status, body = sim.json("GET", "/api/ui/chatgpt/models")
    assert status == 200 and body["models"] == ["gpt-5.4", "gpt-5.4-mini", "gpt-hidden"], body
    assert body["default"] == "gpt-5.4", "the default must be the plan's first VISIBLE model, not its lowest priority number"
    sim.configure(routing={"default": "scripted", "models": {"gpt-5": "chatgpt"}}, chatgpt={"model": "gpt-5.4-mini"})
    _, _, text = sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]})
    assert text.count("Hello from the plan") == 1 and stub.last["json"]["model"] == "gpt-5.4-mini"
    status, _ = sim.json("POST", "/api/ui/config", {"routing": {"default": "nonsense"}})
    assert status == 400


# ── Which plan model answers ─────────────────────────────────────────────

def case_the_default_model_is_the_plans_own_first_visible_choice(sim, stub):
    """gpt-5.4 was a guess, and an account without it was refused on its first request."""
    sim.configure(routing={"default": "scripted", "models": {"gpt-5": "chatgpt"}}, chatgpt={"model": ""})
    sim.json("POST", "/api/ui/chatgpt/logout")           # a new sign-in drops the cached list
    sim.sign_in()
    stub.models_calls = 0
    status, headers, text = sim.call("POST", "/api/ui/chat", {"model": "gpt-5", "stream": False,
                                                              "messages": [{"role": "user", "content": "hi"}]})
    assert status == 200, text
    assert stub.last["json"]["model"] == "gpt-5.4", "the hidden model, or none, was asked for: %s" % stub.last["json"]["model"]
    assert headers.get("X-LLMSim-Rule") == "chatgpt:gpt-5.4", headers
    sim.call("POST", "/api/ui/chat", {"model": "gpt-5", "stream": False, "messages": [{"role": "user", "content": "again"}]})
    assert stub.models_calls == 1, "the plan's list was asked for on every request"
    assert sim.json("GET", "/api/ui/chatgpt")[1]["default"] == "gpt-5.4"

    sim.configure(chatgpt={"model": "gpt-5.4-mini"})     # a choice always wins
    sim.call("POST", "/api/ui/chat", {"model": "gpt-5", "stream": False, "messages": [{"role": "user", "content": "x"}]})
    assert stub.last["json"]["model"] == "gpt-5.4-mini"
    sim.configure(chatgpt={"model": "gpt-hidden"})       # a hidden one is never picked for you, but may be named
    sim.call("POST", "/api/ui/chat", {"model": "gpt-5", "stream": False, "messages": [{"role": "user", "content": "x"}]})
    assert stub.last["json"]["model"] == "gpt-hidden"
    sim.configure(chatgpt={"model": ""})
    assert sim.json("GET", "/api/ui/state")[1]["config"]["chatgpt"]["model"] == "", "an empty model was refused or kept"


def case_an_unreadable_plan_list_fails_loudly_rather_than_guessing(sim, stub):
    sim.json("POST", "/api/ui/chatgpt/logout")
    sim.sign_in()                                        # no cached list now
    stub.models_fail, stub.last = True, None
    try:
        status, _, text = sim.call("POST", "/api/ui/chat", {"model": "gpt-5", "stream": False,
                                                            "messages": [{"role": "user", "content": "hi"}]})
        assert status == 502 and "plan" in json.loads(text)["error"]["message"].lower(), text
        assert stub.last is None, "a model name was guessed and sent"
        sim.configure(chatgpt={"model": "gpt-5.4"})      # chosen, so the list is not needed
        status, _, text = sim.call("POST", "/api/ui/chat", {"model": "gpt-5", "stream": False,
                                                            "messages": [{"role": "user", "content": "hi"}]})
        assert status == 200, text
    finally:
        stub.models_fail = False
        sim.configure(chatgpt={"model": ""})


def case_a_config_that_holds_the_old_guess_is_read_as_unchosen(sim, stub):
    path = os.path.join(sim.workdir, "config.json")
    for stored, expected in (("gpt-5.4", ""), ("gpt-6-sol", "gpt-6-sol")):
        sim.stop()
        config = json.load(open(path))
        config.setdefault("chatgpt", {})["model"] = stored
        json.dump(config, open(path, "w"))
        sim.start()
        got = sim.json("GET", "/api/ui/state")[1]["config"]["chatgpt"]["model"]
        assert got == expected, (stored, got)
    sim.configure(chatgpt={"model": ""})


def case_console_chat_goes_through_the_real_path(sim, stub):
    sim.configure(routing={"default": "scripted", "models": {"gpt-5": "chatgpt"}}, chatgpt={"model": "gpt-5.4"})
    # A model on the plan: the page is told what answered, and the plan answered.
    status, headers, text = sim.call("POST", "/api/ui/chat", {
        "model": "gpt-5", "stream": False, "system": "Be brief.", "messages": [{"role": "user", "content": "hi"}]})
    reply = json.loads(text)
    assert status == 200 and reply["choices"][0]["message"]["content"].startswith("Hello from the plan"), text
    assert headers.get("X-LLMSim-Route") == "chatgpt" and headers.get("X-LLMSim-Rule") == "chatgpt:gpt-5.4", headers
    assert stub.last["json"]["instructions"] == "Be brief.", "the system prompt did not travel"
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
    assert status == 403, "another site could make this page spend the plan"


def case_console_chat_streams_in_the_models_own_format(sim, stub):
    sim.configure(routing={"default": "scripted", "models": {"gpt-5": "chatgpt"}})
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
    sim.configure(routing={"default": "scripted", "models": {"gpt-5": "chatgpt"}}, chatgpt={"model": "gpt-5.4"})
    assert sess(sim, action="settings", onMiss="fail", strict=False)[1]["ok"]
    assert sess(sim, action="record", name="loop")[1]["ok"]
    live = conversation(sim)
    assert sim.post(CHAT, {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hello there"}]})[0] == 200
    assert sess(sim, action="stop")[1]["message"].startswith("Saved 3 exchange(s)")
    path = os.path.join(sim.workdir, "cassettes", "loop.json")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600", "a recording holds model answers and must be owner-only"
    cassette = json.load(open(path))
    assert [e["turn"] for e in cassette["entries"] if e["route"] == "chatgpt"] == [0, 1]
    assert "messages" not in json.dumps(cassette["entries"][0]), "the request itself was kept, and only the answer should be"

    # Replay: the plan is no longer asked, and the model is routed somewhere else entirely.
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
    sim.configure(routing={"default": "scripted", "models": {"gpt-5": "chatgpt"}})
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
    sim.configure(routing={"default": "scripted", "models": {"gpt-5": "chatgpt"}}, chatgpt={"model": "gpt-5.4"})
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
    sim.configure(routing={"default": "scripted", "models": {"gpt-5": "chatgpt"}}, chatgpt={"model": "gpt-5.4"})
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
    sim.configure(routing={"default": "chatgpt", "models": {}})
    stub.last = None
    try:
        assert api(sim, "POST", "/api/sessions/record", {"name": "runner"})[0] == 200
        status, run = sim.json("POST", "/api/ui/run", {"planId": "agent"})
        assert status == 200 and run["failed"] == 0 and run["passed"] == 6, run
        assert api(sim, "POST", "/api/sessions/stop")[1]["summary"]["entries"] == 0, "the runner's requests were recorded"
        assert stub.last is None, "the runner's requests reached the plan"
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


def case_another_site_cannot_drive_the_sign_in(sim, stub):
    for path in ("login", "logout", "complete"):
        status, _ = sim.json("POST", "/api/ui/chatgpt/" + path, {}, {"Origin": "http://evil.example"})
        assert status == 403, (path, status)
    assert sim.json("GET", "/api/ui/chatgpt")[1]["signedIn"], "a foreign origin signed the user out"


def case_wrong_state_is_refused(sim, stub):
    started = sim.json("POST", "/api/ui/chatgpt/login")[1]
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(started["url"]).query)["state"][0]
    status, body = sim.json("POST", "/api/ui/chatgpt/complete", {
        "redirect": "http://127.0.0.1/auth/callback?code=x&state=not-" + state})
    assert status == 400 and "not the one LLMSim started" in body["message"], body
    status, body = sim.json("POST", "/api/ui/chatgpt/complete", {"redirect": "http://127.0.0.1/?code=x&state=" + state})
    assert status == 400, "a code could be tried twice, or a bad one accepted"


def case_clicking_sign_in_twice_still_listens(sim, stub):
    sim.json("POST", "/api/ui/chatgpt/logout")
    first = sim.json("POST", "/api/ui/chatgpt/login")[1]
    second = sim.json("POST", "/api/ui/chatgpt/login")[1]       # straight away, before the first has let go
    assert first["listening"] and second["listening"], "the second click fell back to the paste flow"
    assert first["url"] != second["url"], "the abandoned sign-in's state was reused"


def case_paste_flow_when_the_callback_port_is_taken(sim, stub):
    sim.json("POST", "/api/ui/chatgpt/logout")         # cancels the abandoned sign-in that still holds the port
    time.sleep(0.5)
    taken = socket.socket()
    # What any server does. Without it the bind trips on closed connections
    # still in TIME_WAIT from the earlier sign-ins, which says nothing about
    # whether a listener is alive — and a live one still refuses this bind.
    taken.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    taken.bind(("127.0.0.1", sim.callback_port))
    taken.listen(1)
    try:
        started = sim.json("POST", "/api/ui/chatgpt/login")[1]
        assert started["listening"] is False
        no_follow = urllib.request.build_opener(type("N", (urllib.request.HTTPRedirectHandler,), {
            "redirect_request": lambda *a, **k: None}))
        try:
            no_follow.open(started["url"])
        except urllib.error.HTTPError as redirect:
            landed = redirect.headers["Location"]
        status, body = sim.json("POST", "/api/ui/chatgpt/complete", {"redirect": landed})
        assert status == 200 and body["status"]["signedIn"], body
    finally:
        taken.close()


def case_sign_out_forgets_and_revokes(sim, stub):
    status, body = sim.json("POST", "/api/ui/chatgpt/logout")
    assert status == 200 and body["revoked"] and stub.revoked[-1] == stub.refresh_token, body
    kept = json.load(open(sim.auth_path))
    assert body["status"]["signedIn"] is False and not {"access_token", "refresh_token", "id_token"} & set(kept), kept
    assert kept["client_id"] == stub.client_id and kept["ext_agent_host_id"], "the registration should outlive a sign-out"


def latest(stub):
    return stub.authorize_seen[-1]


def asked(started):
    """What LLMSim's sign-in URL asks OpenAI for, before anybody opens it."""
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(started["url"]).query).items()}


def fresh(sim, stub):
    """Signed out, with no registration on file: as if LLMSim had just been installed."""
    sim.json("POST", "/api/ui/chatgpt/logout")
    stub.grant_scope, stub.subject, stub.deny = FULL_SCOPE, "user-abc", False
    stub.omit_issued_client, stub.wrong_issued_client, stub.bad_id_token = False, None, None
    sim.stop()
    if os.path.exists(sim.auth_path):
        os.remove(sim.auth_path)
    sim.start()
    sim.configure(routing={"default": "chatgpt", "models": {}})


def case_it_signs_in_under_its_own_name_and_never_as_codex(sim, stub):
    source = open(SERVER).read()
    for codex in ("REMOVED_CLIENT_ID", "removed_originator", "codex_cli_simplified_flow", "backend-api/codex", "chatgpt.com/backend-api",
                  "ChatGPT-Account-ID"):
        assert codex not in source, "server.py still has %s" % codex
    fresh(sim, stub)
    sim.sign_in()
    first = latest(stub)
    assert first["client_id"] == "dynamic_agent_client" and first["agent_name_hint"] == "LLMSim", first
    assert first["ext_agent_host_id"].startswith("urn:uuid:"), first
    assert first["resource"] == RESOURCE and first["scope"] == FULL_SCOPE, first
    assert "id_token_hint" not in first and "login_hint" not in first and "prompt" not in first, first
    kept = json.load(open(sim.auth_path))
    assert kept["client_id"] == "oaiapp_%d" % stub.clients_issued, kept["client_id"]
    assert kept["subject"] == "user-abc" and kept["issuer"] == stub.issuer and PLAN_SCOPE in kept["scopes"], kept
    assert oct(os.stat(sim.auth_path).st_mode & 0o777) == "0o600"
    # Everything goes to the documented places, and the requests answer.
    status, _, text = sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]})
    assert status == 200 and "Hello from the plan" in text, text


def case_signing_in_again_comes_back_as_the_same_registration(sim, stub):
    before = json.load(open(sim.auth_path))
    issued = stub.clients_issued
    sim.sign_in()
    again = latest(stub)
    assert again["client_id"] == before["client_id"] and stub.clients_issued == issued, again
    assert "agent_name_hint" not in again, "the name hint is for a first registration only"
    assert again["ext_agent_host_id"] == before["ext_agent_host_id"], "this host's id changed"
    assert again["id_token_hint"] == before["id_token"] and again["login_hint"] == "eti@example.com", again
    # Signed out: the same registration, without the hint a sign-out takes away.
    sim.json("POST", "/api/ui/chatgpt/logout")
    sim.sign_in()
    back = latest(stub)
    assert back["client_id"] == before["client_id"] and back["ext_agent_host_id"] == before["ext_agent_host_id"], back
    assert "id_token_hint" not in back and back["login_hint"] == "eti@example.com", back


def case_a_different_account_is_refused_until_it_is_asked_for(sim, stub):
    before = json.load(open(sim.auth_path))
    stub.subject = "somebody-else"
    status, started = sim.json("POST", "/api/ui/chatgpt/login")
    try:
        urllib.request.urlopen(started["url"], timeout=10)
        raise AssertionError("a different account was accepted")
    except urllib.error.HTTPError as exc:
        assert "different ChatGPT account" in exc.read().decode()
    kept = json.load(open(sim.auth_path))
    assert kept["refresh_token"] == before["refresh_token"] and kept["subject"] == "user-abc", "its credentials were replaced"
    status, body = sim.json("POST", "/api/ui/chatgpt/login", {"newAccount": True})
    assert status == 409, "another account while signed in would leave this one's grant standing: %s" % status
    sim.json("POST", "/api/ui/chatgpt/logout")
    status, started = sim.json("POST", "/api/ui/chatgpt/login", {"newAccount": True})
    url = asked(started)
    assert status == 200 and url["client_id"] == "dynamic_agent_client" and url["agent_name_hint"] == "LLMSim", url
    assert url["ext_agent_host_id"] == before["ext_agent_host_id"], "the host is still the same host"
    assert "login_hint" not in url, url
    urllib.request.urlopen(started["url"], timeout=10).read()
    kept = json.load(open(sim.auth_path))
    assert kept["subject"] == "somebody-else" and kept["client_id"] != before["client_id"], kept
    stub.subject = "user-abc"
    fresh(sim, stub)
    sim.sign_in()


def _expect_refused(sim, stub, words):
    before = json.dumps(json.load(open(sim.auth_path))) if os.path.exists(sim.auth_path) else None
    _, started = sim.json("POST", "/api/ui/chatgpt/login")
    try:
        urllib.request.urlopen(started["url"], timeout=10)
        raise AssertionError("accepted: wanted %r" % words)
    except urllib.error.HTTPError as exc:
        assert words in exc.read().decode(), words
    return before


def case_an_id_token_that_does_not_check_out_is_never_kept(sim, stub):
    fresh(sim, stub)
    for bad, words in (("signature", "signature"), ("nonce", "not from the sign-in"), ("audience", "another client"),
                       ("issuer", "another issuer"), ("expired", "expired")):
        stub.bad_id_token = bad
        _expect_refused(sim, stub, words)
        status = sim.json("GET", "/api/ui/chatgpt")[1]
        assert status["signedIn"] is False, "signed in on a bad ID token: %s" % bad
        kept = json.load(open(sim.auth_path))
        assert "access_token" not in kept, "tokens were saved from a bad ID token: %s" % bad
    stub.bad_id_token = None


def case_a_first_sign_in_needs_the_client_id_openai_issues(sim, stub):
    fresh(sim, stub)
    stub.omit_issued_client = True
    _expect_refused(sim, stub, "did not finish registering")
    stub.omit_issued_client = False
    stub.wrong_issued_client = "dynamic_agent_client"
    _expect_refused(sim, stub, "did not finish registering")
    stub.wrong_issued_client = None
    sim.sign_in()
    stub.wrong_issued_client = "oaiapp_other"
    _expect_refused(sim, stub, "another registration")
    stub.wrong_issued_client = None


def case_declining_the_sign_in_stops_there(sim, stub):
    fresh(sim, stub)
    stub.deny = True
    codes_before = stub.issued
    _expect_refused(sim, stub, "access_denied")
    assert stub.issued == codes_before and sim.json("GET", "/api/ui/chatgpt")[1]["signedIn"] is False
    stub.deny = False


def case_without_the_plan_permission_nothing_is_answered_until_it_is_given(sim, stub):
    fresh(sim, stub)
    stub.grant_scope = "openid profile email offline_access"
    sim.sign_in()
    status = sim.json("GET", "/api/ui/chatgpt")[1]
    assert status["signedIn"] is True and status["planUsage"] is False, status
    stub.last = None
    code, _, text = sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]})
    assert code == 502 and "not allowed" in json.loads(text)["error"]["message"] and stub.last is None, text
    stub.grant_scope = FULL_SCOPE
    _, started = sim.json("POST", "/api/ui/chatgpt/login", {"consent": True})
    assert asked(started)["prompt"] == "consent" and asked(started)["scope"] == FULL_SCOPE, asked(started)
    urllib.request.urlopen(started["url"], timeout=10).read()
    assert sim.json("GET", "/api/ui/chatgpt")[1]["planUsage"] is True
    code, _, _ = sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]})
    assert code == 200
    sim.sign_in()
    assert "prompt" not in latest(stub), "consent must not be forced on every sign-in"


def case_a_sign_in_from_the_codex_days_is_never_used(sim, stub):
    fresh(sim, stub)
    sim.stop()
    old = {"access_token": "old-codex-access", "refresh_token": "old-codex-refresh", "id_token": "x.y.z",
           "account_id": "acct-123", "email": "eti@example.com", "plan": "plus", "expires_at": time.time() + 3600}
    fd = os.open(sim.auth_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(old, fh)
    sim.start()
    status = sim.json("GET", "/api/ui/chatgpt")[1]
    assert status["signedIn"] is False and status["needsSignIn"] is True and "Codex" in status["error"], status
    stub.last, before = None, stub.refresh_calls
    code, _, text = sim.post(CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]})
    assert code == 502 and "signing in again" in json.loads(text)["error"]["message"], text
    assert stub.last is None and stub.refresh_calls == before, "an old token was sent"
    # Signing in again replaces it. Nothing of the old sign-in is left.
    sim.sign_in()
    kept = open(sim.auth_path).read()
    assert "old-codex" not in kept and json.loads(kept)["client_id"].startswith("oaiapp_"), kept
    assert sim.json("GET", "/api/ui/chatgpt")[1]["needsSignIn"] is False
    # And signing out of one clears the file without ever showing the old token to OpenAI.
    sim.json("POST", "/api/ui/chatgpt/logout")
    sim.stop()
    fd = os.open(sim.auth_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(old, fh)
    sim.start()
    revoked = list(stub.revoked)
    sim.json("POST", "/api/ui/chatgpt/logout")
    assert not os.path.exists(sim.auth_path) and stub.revoked == revoked
    assert sim.json("GET", "/api/ui/chatgpt")[1]["needsSignIn"] is False
    sim.sign_in()


def case_forcing_one_tool_offers_only_that_tool(sim, stub):
    sim.configure(routing={"default": "chatgpt", "models": {}})
    weather = {"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}
    other = {"type": "function", "function": {"name": "get_time", "parameters": {"type": "object"}}}
    ask = {"model": "gpt-5", "tools": [other, weather], "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
           "messages": [{"role": "user", "content": "use the tool"}]}
    status, _, text = sim.post(CHAT, ask)
    assert status == 200, text
    assert json.loads(text)["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "get_weather", text
    sent = stub.last["json"]
    assert sent["tool_choice"] == "required" and [t["name"] for t in sent["tools"][0]["tools"]] == ["get_weather"], sent
    # Anthropic's way of saying the same thing takes the same path.
    status, _, text = sim.post("/anthropic/v1/messages", {
        "model": "claude-sonnet-5", "max_tokens": 50, "tool_choice": {"type": "tool", "name": "get_weather"},
        "tools": [{"name": "get_time", "input_schema": {"type": "object"}}, {"name": "get_weather", "input_schema": {"type": "object"}}],
        "messages": [{"role": "user", "content": "use the tool"}]}, key=ANTHROPIC_KEY)
    assert status == 200 and stub.last["json"]["tool_choice"] == "required", text
    assert [t["name"] for t in stub.last["json"]["tools"][0]["tools"]] == ["get_weather"], stub.last["json"]
    # A tool that was not offered is the client's mistake, and it gets the route's own answer.
    status, _, text = sim.post(CHAT, dict(ask, tool_choice={"type": "function", "function": {"name": "no_such_tool"}}))
    assert status == 400 and "no_such_tool" in text, text


def case_the_plans_own_errors_say_what_to_do(sim, stub):
    sim.configure(routing={"default": "chatgpt", "models": {}})
    ask = {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]}
    for http, code, want, words in (
            (403, "subscription_sharing_user_not_eligible", 502, "not available for this account"),
            (503, "subscription_sharing_usage_unavailable", 503, "Try again"),
            (503, "subscription_sharing_user_unavailable", 503, "Try again"),
            (400, "subscription_sharing_unsupported_capability", 400, "does not support web_search"),
            (403, "subscription_sharing_route_not_supported", 502, "subscription_sharing_route_not_supported"),
            (403, "chatpass_v2_scope_not_authorized", 502, "chatpass_v2_scope_not_authorized")):
        stub.fail_next = (http, {"code": code, "message": "said by OpenAI", "param": "web_search"})
        before = stub.refresh_calls
        status, _, text = sim.post(CHAT, ask)
        assert status == want and words in json.loads(text)["error"]["message"], (code, status, text)
        assert stub.refresh_calls == before, "%s should not have refreshed the token" % code
    # After the first word the status is gone, and the limit is reported inside the stream.
    stub.fail_midstream = {"code": "subscription_sharing_usage_limit_exceeded", "message": "plan limit."}
    status, _, text = sim.post(CHAT, {"model": "gpt-5", "stream": True,
                                      "messages": [{"role": "user", "content": "break midway"}]})
    stub.fail_midstream = None
    assert status == 200 and "settings/usage" in text, text
    # A pre-stream refusal in the other shape OpenAI documents.
    original = stub.responses
    stub.responses = lambda h: h.reply(403, {"detail": "not in a permitted region"})
    try:
        status, _, text = sim.post(CHAT, ask)
    finally:
        stub.responses = original
    assert status == 502 and "permitted region" in json.loads(text)["error"]["message"], text


def case_the_welcome_is_said_once(sim, stub):
    fresh(sim, stub)
    sim.sign_in()
    assert sim.json("GET", "/api/ui/chatgpt")[1]["welcomed"] is False
    assert sim.json("POST", "/api/ui/chatgpt/welcomed")[0] == 200
    assert sim.json("GET", "/api/ui/chatgpt")[1]["welcomed"] is True
    sim.sign_in()
    assert sim.json("GET", "/api/ui/chatgpt")[1]["welcomed"] is True, "said again on a later sign-in"
    sim.json("POST", "/api/ui/chatgpt/logout")
    sim.stop(); sim.start()
    assert sim.json("GET", "/api/ui/chatgpt")[1]["welcomed"] is True, "forgotten by a sign-out and a restart"


CASES = [v for k, v in sorted(globals().items(), key=lambda kv: list(globals()).index(kv[0])) if k.startswith("case_")]


def main():
    stub = Stub()
    failed = 0
    with tempfile.TemporaryDirectory() as work:
        sim = Sim(stub, work).start()
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
