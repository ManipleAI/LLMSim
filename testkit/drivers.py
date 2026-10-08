"""Drivers: how a test starts a conversation with an agent and reads what it says.

A driver is the one place a platform enters the kit. Everything else — scenarios,
checks, the timeline — is the same whichever platform the agent runs on. A driver
does four things: start a conversation, send a turn, return what the agent said,
end the conversation.

  http         any endpoint that takes a message and returns a reply, described by a template
  openai       an OpenAI-compatible chat endpoint
  a2a          an agent that speaks Agent2Agent (message/send, tasks, push or polling)
  agentforce   Salesforce's Agent API (session, messages, end)
  process      a local program (off unless the operator turns it on: see below)

A driver never writes a credential anywhere. It asks the credential store for the
headers at the moment it sends, and the store's secrets are scrubbed from anything
the kit keeps or shows.
"""

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import callbacks, core, credentials


class DriverError(Exception):
    """The agent could not be reached or did not answer. A test result, not a bug."""


class Reply:
    def __init__(self, text="", raw=None, ms=0, state=None, input_required=False):
        self.text, self.raw, self.ms, self.state, self.input_required = text, raw, ms, state or {}, input_required


class Ctx:
    """What a driver may use of the run it is part of."""

    def __init__(self, run, agent, base_urls=None, emit=None, timeout=60):
        self.run, self.agent, self.base_urls, self.timeout = run, agent, base_urls or {}, timeout
        self.emit = emit or (lambda kind, data: None)


# -- HTTP helpers -------------------------------------------------------------

def http(method, url, headers=None, body=None, timeout=60, stream_to=None):
    """(status, headers, text). Never raises on an HTTP error status; raises DriverError on no connection."""
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data, hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if stream_to:
                return resp.status, dict(resp.getheaders()), stream_to(resp)
            return resp.status, dict(resp.getheaders()), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        raise DriverError("could not reach %s: %s" % (url, getattr(exc, "reason", exc)))


def sse_events(resp):
    """[(event, data_text)] from a server-sent-events body, read to its end."""
    out, event, data = [], "", []
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                out.append((event, "\n".join(data)))
            event, data = "", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        out.append((event, "\n".join(data)))
    return out


def fill(template, values):
    """A JSON template with {{name}} replaced. A string that is only a placeholder takes the value's own type."""
    if isinstance(template, str):
        for key, value in values.items():
            token = "{{%s}}" % key
            if template == token:
                return value
            template = template.replace(token, str(value))
        return template
    if isinstance(template, list):
        return [fill(t, values) for t in template]
    if isinstance(template, dict):
        return {k: fill(v, values) for k, v in template.items()}
    return template


def _auth_headers(cfg, ctx):
    label = cfg.get("credential")
    if not label:
        return {}
    try:
        return credentials.headers(label, timeout=min(ctx.timeout, 30))
    except KeyError:
        raise DriverError("the credential %r is not stored (see /api/credentials)" % label)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DriverError("could not get a token for credential %r: %s" % (label, getattr(exc, "reason", exc)))


class Driver:
    name = ""
    required = ()

    def __init__(self, cfg, ctx):
        self.cfg, self.ctx = cfg, ctx
        self.turn = 0

    @classmethod
    def validate(cls, cfg):
        problems = ["%s needs %s" % (cls.name, k) for k in cls.required if not cfg.get(k)]
        if cfg.get("credential") is not None and not core.valid_id(cfg["credential"]):
            problems.append("credential is a label from /api/credentials")
        return problems

    def start(self):
        pass

    def ident(self, url):
        """Headers that say which run and agent this is, when the target is LLMSim itself — so a
        stub or a proxy it serves puts what it sees in this run. Never sent to anyone else."""
        mine = self.ctx.base_urls.get("self")
        if mine and url.startswith(mine):
            return {"X-LLMSim-Run": self.ctx.run, "X-LLMSim-Agent": self.ctx.agent}
        return {}

    def send(self, text):
        raise NotImplementedError

    def end(self):
        pass

    def timeout(self):
        return float(self.cfg.get("timeoutS") or self.ctx.timeout)


# -- plain HTTP ---------------------------------------------------------------

class HttpDriver(Driver):
    """POST a template to a URL and read the reply from a path in the answer.

        {"driver": "http", "url": "https://agent.example/chat",
         "body": {"input": "{{message}}", "session": "{{conversation}}"},
         "reply": "output.text", "conversation": "session"}
    """
    name, required = "http", ("url",)

    def start(self):
        self.conversation = uuid.uuid4().hex[:12]

    def send(self, text):
        self.turn += 1
        values = {"message": text, "turn": self.turn, "conversation": self.conversation, "run": self.ctx.run, "agent": self.ctx.agent}
        body = fill(self.cfg.get("body") or {"message": "{{message}}"}, values)
        headers = dict(fill(self.cfg.get("headers") or {}, values), **_auth_headers(self.cfg, self.ctx), **self.ident(self.cfg["url"]))
        started = time.time()
        stream = self.cfg.get("replyFrom") == "sse"
        status, _, out = http(self.cfg.get("method", "POST"), fill(self.cfg["url"], values), headers, body, self.timeout(),
                              stream_to=sse_events if stream else None)
        ms = int((time.time() - started) * 1000)
        if status >= 400:
            raise DriverError("the agent answered HTTP %d: %s" % (status, str(out)[:300]))
        if stream:
            path = self.cfg.get("reply") or "delta"
            parts = []
            for _, data in out:
                try:
                    v = core.dig(json.loads(data), path)
                except ValueError:
                    continue
                if isinstance(v, str):
                    parts.append(v)
            return Reply("".join(parts), raw=[d for _, d in out][:50], ms=ms)
        if self.cfg.get("replyFrom") == "text":
            return Reply(out.strip(), ms=ms)
        try:
            data = json.loads(out)
        except ValueError:
            raise DriverError("the agent's answer is not JSON (set replyFrom to text if it should not be): %s" % out[:200])
        reply = core.dig(data, self.cfg.get("reply") or "reply")
        if reply is None:
            raise DriverError("there is no %r in the agent's answer: %s" % (self.cfg.get("reply") or "reply", out[:300]))
        if self.cfg.get("conversation") and core.dig(data, self.cfg["conversation"]):
            self.conversation = str(core.dig(data, self.cfg["conversation"]))
        return Reply(reply if isinstance(reply, str) else json.dumps(reply), raw=data, ms=ms)


# -- OpenAI-compatible --------------------------------------------------------

class OpenAIDriver(Driver):
    name, required = "openai", ("url",)

    def start(self):
        self.messages = [{"role": "system", "content": self.cfg["system"]}] if self.cfg.get("system") else []

    def send(self, text):
        self.turn += 1
        self.messages.append({"role": "user", "content": text})
        body = {"model": self.cfg.get("model", "gpt-5"), "messages": self.messages, "stream": bool(self.cfg.get("stream"))}
        body.update(self.cfg.get("extra") or {})
        started = time.time()
        url = self.cfg["url"].rstrip("/") + "/chat/completions"
        stream = body["stream"]
        status, _, out = http("POST", url, dict(_auth_headers(self.cfg, self.ctx), **self.ident(url)), body, self.timeout(), stream_to=sse_events if stream else None)
        ms = int((time.time() - started) * 1000)
        if status >= 400:
            raise DriverError("the endpoint answered HTTP %d: %s" % (status, str(out)[:300]))
        if stream:
            parts = []
            for _, data in out:
                if data.strip() == "[DONE]":
                    continue
                try:
                    delta = (json.loads(data).get("choices") or [{}])[0].get("delta") or {}
                except ValueError:
                    continue
                if isinstance(delta.get("content"), str):
                    parts.append(delta["content"])
            reply, raw = "".join(parts), None
        else:
            try:
                raw = json.loads(out)
                reply = ((raw.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            except (ValueError, AttributeError):
                raise DriverError("that is not a chat completion: %s" % out[:200])
        self.messages.append({"role": "assistant", "content": reply})
        return Reply(reply, raw=raw, ms=ms)


# -- a local process ----------------------------------------------------------

def process_allowed():
    """A process driver runs a command. Anyone who can reach this port could name any
    command, so it exists only when whoever started LLMSim said so, in its environment
    — never through a setting the API can change."""
    return os.environ.get("LLMSIM_ALLOW_PROCESS") == "1"


class ProcessDriver(Driver):
    """Run a program for each turn: the message on stdin (and in {message}), the reply on stdout.
    The program learns where LLMSim is from LLMSIM_RUN, LLMSIM_AGENT and LLMSIM_URL_<VENDOR>."""
    name, required = "process", ("command",)

    @classmethod
    def validate(cls, cfg):
        problems = super().validate(cfg)
        if not isinstance(cfg.get("command"), list) or not all(isinstance(c, str) for c in cfg.get("command") or []):
            problems.append("command is a list of strings, one per argument")
        if not process_allowed():
            problems.append("the process driver is off: start LLMSim with LLMSIM_ALLOW_PROCESS=1 to allow it")
        return problems

    def send(self, text):
        self.turn += 1
        env = dict(os.environ, LLMSIM_RUN=self.ctx.run, LLMSIM_AGENT=self.ctx.agent, LLMSIM_TURN=str(self.turn))
        for vendor, url in self.ctx.base_urls.items():
            if isinstance(url, str):                      # the run's own base URLs are a dict, and an environment holds text
                env["LLMSIM_URL_" + vendor.upper()] = url
        env.update({k: str(v) for k, v in (self.cfg.get("env") or {}).items()})
        argv = [a.replace("{message}", text) for a in self.cfg["command"]]
        started = time.time()
        try:
            proc = subprocess.run(argv, input=text, capture_output=True, text=True, timeout=self.timeout(), env=env,
                                  cwd=self.cfg.get("cwd") or None)
        except subprocess.TimeoutExpired:
            raise DriverError("the program did not finish within %ds" % self.timeout())
        except OSError as exc:
            raise DriverError("could not run %s: %s" % (argv[0], exc))
        if proc.returncode != 0:
            raise DriverError("the program exited %d: %s" % (proc.returncode, (proc.stderr or proc.stdout)[:300]))
        return Reply(proc.stdout.strip(), ms=int((time.time() - started) * 1000))


# -- Agent2Agent --------------------------------------------------------------

TERMINAL = ("completed", "failed", "canceled", "rejected")
WAITING_ON_US = ("input-required", "auth-required")


def parts_text(parts):
    return "\n".join(p.get("text", "") for p in parts or [] if isinstance(p, dict) and p.get("kind", "text") == "text").strip()


class A2ADriver(Driver):
    """Agent2Agent over JSON-RPC: message/send, then tasks/get or a push callback until the task is done.

        {"driver": "a2a", "url": "https://agent.example", "credential": "agent-token",
         "wait": "poll" | "push" | "stream", "pollMs": 500}
    """
    name, required = "a2a", ("url",)

    def start(self):
        self.context_id, self.task_id, self.rpc_id = None, None, 0
        self.endpoint = self.cfg.get("rpcUrl") or self._endpoint()

    def _endpoint(self):
        base = self.cfg["url"].rstrip("/")
        for suffix in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            try:
                status, _, text = http("GET", base + suffix, dict(_auth_headers(self.cfg, self.ctx), **self.ident(base)), None, 15)
            except DriverError:
                break
            if status == 200:
                try:
                    card = json.loads(text)
                except ValueError:
                    continue
                self.card = card
                return card.get("url") or base
        return base

    def _rpc(self, method, params, stream=False):
        self.rpc_id += 1
        body = {"jsonrpc": "2.0", "id": self.rpc_id, "method": method, "params": params}
        headers = dict(_auth_headers(self.cfg, self.ctx), **self.ident(self.endpoint))
        if stream:
            headers["Accept"] = "text/event-stream"
        status, _, out = http("POST", self.endpoint, headers, body, self.timeout(), stream_to=sse_events if stream else None)
        if status >= 400:
            raise DriverError("the agent answered HTTP %d to %s: %s" % (status, method, str(out)[:300]))
        return out

    @staticmethod
    def _result(text):
        try:
            data = json.loads(text)
        except ValueError:
            raise DriverError("the agent's answer is not JSON-RPC: %s" % text[:200])
        if data.get("error"):
            raise DriverError("the agent returned an error: %s (code %s)" % (data["error"].get("message"), data["error"].get("code")))
        return data.get("result")

    def send(self, text):
        self.turn += 1
        started = time.time()
        message = {"kind": "message", "role": "user", "messageId": uuid.uuid4().hex, "parts": [{"kind": "text", "text": text}]}
        if self.context_id:
            message["contextId"] = self.context_id
        if self.task_id:
            message["taskId"] = self.task_id
        wait = self.cfg.get("wait", "poll")
        config = {"acceptedOutputModes": ["text/plain"], "blocking": wait == "poll" and bool(self.cfg.get("blocking", True))}
        token = None
        if wait == "push":
            token = uuid.uuid4().hex
            waiter = callbacks.register(token)
            base = self.cfg.get("callbackBase") or self.ctx.base_urls.get("self", "")
            config["blocking"] = False
            config["pushNotificationConfig"] = {"url": "%s/callbacks/%s" % (base.rstrip("/"), token), "token": token}
        params = {"message": message, "configuration": config}
        try:
            if wait == "stream":
                result = self._stream(params)
            else:
                result = self._result(self._rpc("message/send", params))
                result = self._settle(result, waiter if token else None, started)
        finally:
            if token:
                callbacks.forget(token)
        return self._reply(result, started)

    def _stream(self, params):
        events = self._rpc("message/stream", params, stream=True)
        task, final = {}, None
        for _, data in events:
            try:
                result = self._result(data)
            except DriverError:
                raise
            if not isinstance(result, dict):
                continue
            kind = result.get("kind")
            if kind == "message":
                final = result
            elif kind == "task":
                task = result
            elif kind == "status-update":
                task = dict(task, id=result.get("taskId") or task.get("id"), contextId=result.get("contextId"), status=result.get("status"))
            elif kind == "artifact-update":
                task.setdefault("artifacts", []).append(result.get("artifact") or {})
        return final or task

    def _settle(self, result, waiter, started):
        """A task may not be finished when the agent first answers. Wait for it to be."""
        deadline = started + self.timeout()
        while isinstance(result, dict) and result.get("kind") == "task":
            state = ((result.get("status") or {}).get("state") or "").lower()
            if state in TERMINAL or state in WAITING_ON_US:
                return result
            if time.time() > deadline:
                raise DriverError("the task was still %r after %ds" % (state, self.timeout()))
            if waiter is not None:
                got = waiter.wait(min(2.0, max(0.05, deadline - time.time())))
                if got:
                    waiter.event.clear()
                    pushed = got[-1]["body"]
                    if isinstance(pushed, dict) and pushed.get("kind") == "task":
                        result = pushed
                        continue
                    waiter.payloads.clear()
            else:
                time.sleep(float(self.cfg.get("pollMs") or 300) / 1000.0)
            result = self._result(self._rpc("tasks/get", {"id": result.get("id")}))
        return result

    def _reply(self, result, started):
        ms = int((time.time() - started) * 1000)
        if not isinstance(result, dict):
            raise DriverError("the agent returned neither a message nor a task")
        if result.get("kind") == "message":
            self.context_id = result.get("contextId") or self.context_id
            return Reply(parts_text(result.get("parts")), raw=result, ms=ms)
        self.context_id = result.get("contextId") or self.context_id
        state = ((result.get("status") or {}).get("state") or "").lower()
        said = parts_text(((result.get("status") or {}).get("message") or {}).get("parts"))
        artifact = "\n".join(parts_text(a.get("parts")) for a in result.get("artifacts") or [])
        self.task_id = result.get("id") if state in WAITING_ON_US else None
        if state in ("failed", "rejected", "canceled"):
            return Reply(said or artifact, raw=result, ms=ms, state={"task": state, "taskId": result.get("id")})
        return Reply("\n".join(x for x in (said, artifact) if x), raw=result, ms=ms,
                     state={"task": state, "taskId": result.get("id")}, input_required=state in WAITING_ON_US)


# -- Salesforce Agentforce ----------------------------------------------------

class AgentforceDriver(Driver):
    """Salesforce's Agent API: create a session, send messages, end it.

        {"driver": "agentforce", "domain": "https://acme.my.salesforce.com", "agentId": "0Xx…",
         "credential": "sf-client", "stream": false}

    The credential is an oauth_client_credentials one whose tokenUrl is
    <domain>/services/oauth2/token. The API is not available for every agent type;
    Salesforce's documentation says agents built with Agent Script and the default
    Agentforce agent are not supported, and a session that cannot be created says so.
    """
    name, required = "agentforce", ("domain", "agentId")

    def start(self):
        self.base = (self.cfg.get("apiBase") or "https://api.salesforce.com").rstrip("/")
        self.sequence, self.session = 0, None
        body = {"externalSessionKey": str(uuid.uuid4()), "instanceConfig": {"endpoint": self.cfg["domain"].rstrip("/")},
                "streamingCapabilities": {"chunkTypes": ["Text"]}, "bypassUser": bool(self.cfg.get("bypassUser", True))}
        status, _, out = http("POST", "%s/einstein/ai-agent/v1/agents/%s/sessions" % (self.base, self.cfg["agentId"]),
                              _auth_headers(self.cfg, self.ctx), body, self.timeout())
        if status >= 400:
            hint = (" The Agent API does not support every agent type — agents built with Agent Script and the default "
                    "Agentforce agent are not supported." if status in (400, 404, 405, 409, 422) else "")
            raise DriverError("could not start a session: HTTP %d %s.%s" % (status, str(out)[:200], hint))
        try:
            data = json.loads(out)
            self.session = data["sessionId"]
        except (ValueError, KeyError, TypeError):
            raise DriverError("the session answer had no sessionId: %s" % str(out)[:200])
        self.greeting = " ".join(m.get("message", "") for m in data.get("messages") or [] if isinstance(m, dict)).strip()

    def send(self, text):
        self.turn += 1
        self.sequence += 1
        body = {"message": {"sequenceId": self.sequence, "type": "Text", "text": text}, "variables": self.cfg.get("variables") or []}
        started = time.time()
        stream = bool(self.cfg.get("stream"))
        url = "%s/einstein/ai-agent/v1/sessions/%s/messages%s" % (self.base, self.session, "/stream" if stream else "")
        headers = _auth_headers(self.cfg, self.ctx)
        if stream:
            headers["Accept"] = "text/event-stream"
        status, _, out = http("POST", url, headers, body, self.timeout(), stream_to=sse_events if stream else None)
        ms = int((time.time() - started) * 1000)
        if status >= 400:
            raise DriverError("the agent answered HTTP %d: %s" % (status, str(out)[:300]))
        if stream:
            chunks, final = [], []
            for event, data in out:
                try:
                    d = json.loads(data)
                except ValueError:
                    continue
                m = d.get("message") if isinstance(d.get("message"), dict) else d
                kind = m.get("type") or event
                if kind == "TextChunk" and isinstance(m.get("message"), str):
                    chunks.append(m["message"])
                elif kind == "Inform" and isinstance(m.get("message"), str):
                    final.append(m["message"])
            return Reply("".join(chunks) if chunks else "\n".join(final), raw=None, ms=ms)
        try:
            data = json.loads(out)
        except ValueError:
            raise DriverError("that is not a messages answer: %s" % out[:200])
        said = []
        for m in data.get("messages") or []:
            if isinstance(m, dict) and m.get("type") in ("Failure", "Error"):
                raise DriverError("the agent reported a failure: %s" % (m.get("message") or m.get("errors") or m)[:300] if isinstance(m.get("message") or "", str) else m)
            if isinstance(m, dict) and isinstance(m.get("message"), str):
                said.append(m["message"])
        return Reply("\n".join(said), raw=data, ms=ms)

    def end(self):
        if not getattr(self, "session", None):
            return
        try:
            http("DELETE", "%s/einstein/ai-agent/v1/sessions/%s" % (self.base, self.session),
                 dict(_auth_headers(self.cfg, self.ctx), **{"x-session-end-reason": "UserRequest"}), None, 15)
        except DriverError:
            pass


DRIVERS = {d.name: d for d in (HttpDriver, OpenAIDriver, A2ADriver, AgentforceDriver, ProcessDriver)}


def validate(cfg):
    if not isinstance(cfg, dict) or cfg.get("driver") not in DRIVERS:
        return ["driver is one of %s" % ", ".join(sorted(DRIVERS))]
    return DRIVERS[cfg["driver"]].validate(cfg)


def make(cfg, ctx):
    return DRIVERS[cfg["driver"]](cfg, ctx)
