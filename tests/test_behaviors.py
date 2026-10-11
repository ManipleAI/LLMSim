#!/usr/bin/env python3
"""Behaviors and evidence: identity, captures, rules that branch, faults, assertions, runs.

    python3 tests/test_behaviors.py

Everything runs against a LLMSim subprocess with its own throwaway config. Nothing
here needs a network, an account or a model: LLMSim is both the thing under test and
the vendor the requests are sent to, which is the point — these are the tools for
testing an agent, so they are tested with agents that are written to misbehave.
"""

import http.client
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_chatgpt_plan import Sim, Stub, frames, OPENAI_KEY, ANTHROPIC_KEY  # noqa: E402

CHAT = "/openai/v1/chat/completions"


def api(sim, method, path, body=None, headers=None):
    status, _, text = sim.call(method, path, body, headers)
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def chat(sim, content="hello there", path=CHAT, key=OPENAI_KEY, headers=None, **extra):
    """One Chat Completions request, as an agent would send it. (status, body)"""
    body = dict({"model": "gpt-5", "messages": [{"role": "user", "content": content}]}, **extra)
    hdrs = dict({"Authorization": "Bearer " + key}, **(headers or {}))
    status, _, text = sim.call("POST", path, body, hdrs)
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def reset(sim):
    """Back to a known state between cases: nothing captured, defaults restored."""
    api(sim, "DELETE", "/api/captures")
    api(sim, "PUT", "/api/settings", {"auth": {"mode": "published"}, "capture": {"enabled": True, "max": 300, "maxBytes": 262144,
                                                                          "redact": {"headers": [], "patterns": []}}})


# ── Identity ─────────────────────────────────────────────────────────────

def case_identity_comes_from_the_base_url(sim):
    reset(sim)
    status, _ = chat(sim, path="/run/r1/agent/planner" + CHAT)
    assert status == 200
    chat(sim, path="/run/r1/agent/worker-2" + CHAT)
    chat(sim, path="/run/r2" + CHAT)
    chat(sim, path="/agent/solo/app/billing" + CHAT)          # any order, any subset
    got = api(sim, "GET", "/api/captures")[1]["captures"]
    seen = {(c["run"], c["agent"], c["app"]) for c in got}
    assert seen == {("r1", "planner", ""), ("r1", "worker-2", ""), ("r2", "", ""), ("", "solo", "billing")}, seen
    assert [c["agent"] for c in api(sim, "GET", "/api/captures?run=r1")[1]["captures"]] == ["worker-2", "planner"]
    assert len(api(sim, "GET", "/api/captures?agent=solo&app=billing")[1]["captures"]) == 1
    runs = {r["run"]: r for r in api(sim, "GET", "/api/runs")[1]["runs"]}
    assert runs["r1"]["agents"] == ["planner", "worker-2"] and runs["r1"]["requests"] == 2, runs
    # an identity that is not an identifier is ignored, not trusted
    status, _ = chat(sim, path="/run/has%20space" + CHAT)
    assert status == 404, "a malformed identity segment was read as one"
    assert not [c for c in api(sim, "GET", "/api/captures")[1]["captures"] if c["run"] == "has space"]


def case_identity_comes_from_a_header_and_from_configured_rules(sim):
    reset(sim)
    chat(sim, headers={"X-LLMSim-Run": "hdr-run", "X-LLMSim-Agent": "a"})
    cap = api(sim, "GET", "/api/captures")[1]["captures"][0]
    assert (cap["run"], cap["agent"]) == ("hdr-run", "a"), cap

    # A gateway that carries an identity through the body, and a key that embeds one: configured, not assumed.
    # (Witness mode, since a key that embeds an identity is not one LLMSim published.)
    api(sim, "PUT", "/api/settings", {"auth": {"mode": "any"}})
    status, body = api(sim, "PUT", "/api/settings", {"identity": {"rules": [
        {"field": "app", "from": "body", "name": "metadata.app_id"},
        {"field": "agent", "from": "header", "name": "x-agent-name", "pattern": "^agent-(.+)$"},
        {"field": "run", "from": "credential", "pattern": "sk-run-([a-z0-9]+)"}]}})
    assert status == 200, body
    chat(sim, headers={"X-Agent-Name": "agent-writer"}, key="sk-run-abc123", metadata={"app_id": "APP-42"})
    cap = api(sim, "GET", "/api/captures")[1]["captures"][0]
    assert (cap["run"], cap["agent"], cap["app"]) == ("abc123", "writer", "APP-42"), cap
    # the base URL wins over a rule: it is the one the caller chose on purpose
    chat(sim, path="/run/explicit" + CHAT, key="sk-run-abc123")
    assert api(sim, "GET", "/api/captures")[1]["captures"][0]["run"] == "explicit"

    for bad in ({"rules": [{"field": "a", "from": "cookie", "name": "x"}]},
                {"rules": [{"field": "a", "from": "header", "name": "x", "pattern": "(unclosed"}]},
                {"rules": [{"field": "a", "from": "credential"}]},
                {"rules": [{"field": "a b", "from": "header", "name": "x"}]},
                {"rules": "no"}):
        assert api(sim, "PUT", "/api/settings", {"identity": bad})[0] == 400, bad
    api(sim, "PUT", "/api/settings", {"identity": {"rules": [
        {"field": "run", "from": "header", "name": "x-llmsim-run"}, {"field": "agent", "from": "header", "name": "x-llmsim-agent"},
        {"field": "app", "from": "header", "name": "x-llmsim-app"}]}})


def case_a_run_can_be_given_its_base_urls(sim):
    status, body = api(sim, "POST", "/api/runs", {"run": "demo", "agents": ["planner", "worker"]})
    assert status == 200 and body["baseUrls"]["openai"].endswith("/run/demo/openai/v1"), body
    assert body["agents"]["worker"]["anthropic"].endswith("/run/demo/agent/worker/anthropic"), body
    assert api(sim, "POST", "/api/runs", {})[1]["run"].startswith("r-")
    assert api(sim, "POST", "/api/runs", {"run": "no good"})[0] == 400
    assert api(sim, "POST", "/api/runs", {"agents": ["a b"]})[0] == 400
    # the URL it hands out is one an agent can use
    path = urllib.parse.urlsplit(body["agents"]["worker"]["openai"]).path + "/chat/completions"
    reset(sim)
    assert chat(sim, path=path)[0] == 200
    cap = api(sim, "GET", "/api/captures")[1]["captures"][0]
    assert (cap["run"], cap["agent"]) == ("demo", "worker"), cap


# ── Which credential arrived ─────────────────────────────────────────────

def case_the_credential_modes(sim):
    reset(sim)
    assert chat(sim, key="some-gateway-minted-key")[0] == 401, "published mode accepted a key it does not know"

    api(sim, "PUT", "/api/settings", {"auth": {"mode": "any"}})
    status, _ = chat(sim, key="some-gateway-minted-key")
    assert status == 200, "witness mode refused a credential"
    cap = api(sim, "GET", "/api/captures")[1]["captures"][0]
    assert cap["credential"] == "" and cap["status"] == 200, cap
    assert chat(sim, key="")[0] == 401, "an empty credential was accepted"
    status, _, _ = sim.call("POST", CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "x"}]}, {})
    assert status == 401, "no credential at all was accepted"
    # provider is inferred from the route when the key says nothing: /v1/messages is Anthropic's
    status, _, text = sim.call("POST", "/v1/messages", {"model": "claude-sonnet-5", "max_tokens": 16,
                                                         "messages": [{"role": "user", "content": "hello there"}]},
                               {"x-api-key": "unpublished-anthropic-key"})
    assert status == 200 and json.loads(text)["type"] == "message", text

    secret = "minted-virtual-key-7f3a9c"
    status, body = api(sim, "POST", "/api/settings/credentials", {"label": "platform-token", "key": secret})
    assert status == 200, body
    assert secret not in json.dumps(api(sim, "GET", "/api/settings")[1]), "the key itself was stored or returned"
    assert secret not in open(os.path.join(sim.workdir, "config.json")).read(), "the key itself reached the config file"
    chat(sim, key=secret)
    cap = api(sim, "GET", "/api/captures")[1]["captures"][0]
    assert cap["credential"] == "platform-token", cap
    assert secret not in json.dumps(api(sim, "GET", "/api/captures/export")[1]), "the key reached an export"

    api(sim, "PUT", "/api/settings", {"auth": {"mode": "registered"}})
    assert chat(sim, key=secret)[0] == 200 and chat(sim, key="never-registered")[0] == 401
    assert chat(sim, key=OPENAI_KEY)[0] == 200, "a published key stopped working in registered mode"

    assert api(sim, "DELETE", "/api/settings/credentials/platform-token")[0] == 200
    assert api(sim, "DELETE", "/api/settings/credentials/platform-token")[0] == 404
    assert chat(sim, key=secret)[0] == 401
    assert api(sim, "POST", "/api/settings/credentials", {"label": "published:x", "key": "k"})[0] == 400
    assert api(sim, "POST", "/api/settings/credentials", {"label": "ok", "key": ""})[0] == 400
    assert api(sim, "PUT", "/api/settings", {"auth": {"mode": "whatever"}})[0] == 400
    reset(sim)


# ── Captures ─────────────────────────────────────────────────────────────

def case_a_capture_holds_what_was_sent_and_what_answered(sim):
    reset(sim)
    chat(sim, "please write a file", messages=[{"role": "system", "content": "You are a careful assistant."},
                                               {"role": "user", "content": "please write a file"}],
         tools=[{"type": "function", "function": {"name": "Write", "parameters": {"type": "object", "properties": {"file_path": {}}}}}])
    listed = api(sim, "GET", "/api/captures")[1]["captures"][0]
    assert listed["tools"] == ["Write"] and listed["calls"] == ["Write"] and listed["route"] == "scripted", listed
    assert listed["rule"] == "write-a-file" and listed["status"] == 200 and listed["ms"] is not None, listed
    full = api(sim, "GET", "/api/captures/%d" % listed["n"])[1]
    assert full["capture"]["request"]["messages"][0]["content"] == "You are a careful assistant."
    assert full["view"]["system"] == "You are a careful assistant." and full["view"]["ask"] == "please write a file"
    assert full["capture"]["response"]["toolCalls"][0]["name"] == "Write"
    assert full["capture"]["response"]["usage"], "the answer's token counts were not kept"
    assert "authorization" not in full["capture"]["meta"]["headers"] and "x-api-key" not in full["capture"]["meta"]["headers"]
    assert OPENAI_KEY not in json.dumps(full), "a credential was kept in a capture"
    assert api(sim, "GET", "/api/captures/999999")[0] == 404

    # the answer, whichever way it was asked for
    chat(sim, "hello there", stream=True)
    sim.call("POST", "/anthropic/v1/messages", {"model": "claude-sonnet-5", "max_tokens": 32, "system": "Be terse.",
                                                "messages": [{"role": "user", "content": "hello there"}]},
             {"x-api-key": ANTHROPIC_KEY})
    got = api(sim, "GET", "/api/captures")[1]["captures"]
    assert [c["provider"] for c in got[:2]] == ["anthropic", "openai"], got
    anth = api(sim, "GET", "/api/captures/%d" % got[0]["n"])[1]
    assert anth["view"]["system"] == "Be terse.", "an Anthropic system prompt was not read"


def case_captures_are_bounded_and_can_be_switched_off(sim):
    reset(sim)
    api(sim, "PUT", "/api/settings", {"capture": {"max": 3}})
    for i in range(6):
        chat(sim, "request %d" % i)
    got = api(sim, "GET", "/api/captures")[1]
    assert [c["ask"] for c in got["captures"]] == ["request 5", "request 4", "request 3"] and got["capacity"] == 3, got

    api(sim, "PUT", "/api/settings", {"capture": {"max": 300, "maxBytes": 1024}})
    api(sim, "DELETE", "/api/captures")
    chat(sim, "padding " * 2000, messages=[{"role": "system", "content": "SYSTEM-MARKER " + "x" * 5000},
                                          {"role": "user", "content": "padding " * 2000}])
    cap = api(sim, "GET", "/api/captures")[1]["captures"][0]
    assert cap["truncated"] is True, "an oversize request was kept whole"
    full = api(sim, "GET", "/api/captures/%d" % cap["n"])[1]
    assert len(json.dumps(full["capture"]["request"])) < 4096 and full["view"]["system"].startswith("SYSTEM-MARKER")
    assert api(sim, "PUT", "/api/settings", {"capture": {"maxBytes": 10}})[0] == 400
    assert api(sim, "PUT", "/api/settings", {"capture": {"max": 0}})[0] == 400

    api(sim, "PUT", "/api/settings", {"capture": {"enabled": False, "maxBytes": 262144}})
    api(sim, "DELETE", "/api/captures")
    chat(sim, "not kept")
    got = api(sim, "GET", "/api/captures")[1]
    assert got["captures"] == [] and got["enabled"] is False
    reset(sim)


def case_redaction_is_applied_on_the_way_out(sim):
    reset(sim)
    api(sim, "PUT", "/api/settings", {"capture": {"redact": {"headers": ["x-secret-header"],
                                                              "patterns": [{"pattern": r"\b\d{4}-\d{4}-\d{4}-\d{4}\b", "replace": "[card]"}]}}})
    chat(sim, "my card is 4111-1111-1111-1111", headers={"X-Secret-Header": "shh", "X-Other": "fine"})
    n = api(sim, "GET", "/api/captures")[1]["captures"][0]["n"]
    shown = api(sim, "GET", "/api/captures/%d" % n)[1]["capture"]
    assert "4111-1111-1111-1111" not in json.dumps(shown) and "[card]" in json.dumps(shown), "a redaction was not applied"
    assert "x-secret-header" not in shown["meta"]["headers"] and shown["meta"]["headers"]["x-other"] == "fine"
    assert "4111-1111-1111-1111" not in json.dumps(api(sim, "GET", "/api/captures/export")[1])
    for bad in ([{"pattern": "(unclosed"}], [{"replace": "x"}], "no", [{"pattern": ""}]):
        assert api(sim, "PUT", "/api/settings", {"capture": {"redact": {"patterns": bad}}})[0] == 400, bad
    reset(sim)


def case_the_functional_runner_is_not_captured(sim):
    reset(sim)
    sim.json("POST", "/api/ui/run", {"planId": "quick"})
    assert api(sim, "GET", "/api/captures")[1]["captures"] == [], "the runner's own requests were captured"


def case_captures_can_be_cleared_by_run(sim):
    reset(sim)
    chat(sim, path="/run/keep" + CHAT)
    chat(sim, path="/run/drop" + CHAT)
    chat(sim, path="/run/drop" + CHAT)
    status, body = api(sim, "DELETE", "/api/captures?run=drop")
    assert status == 200 and body["removed"] == 2, body
    assert [c["run"] for c in api(sim, "GET", "/api/captures")[1]["captures"]] == ["keep"]
    status, body = api(sim, "DELETE", "/api/runs/keep")
    assert status == 200 and body["removed"] == 1 and api(sim, "GET", "/api/runs/keep")[0] == 404


def case_the_api_refuses_what_it_should_for_every_area(sim):
    for method, path in (("GET", "/api/settings"), ("PUT", "/api/settings"), ("POST", "/api/settings/credentials"),
                         ("GET", "/api/captures"), ("DELETE", "/api/captures"), ("POST", "/api/runs"), ("DELETE", "/api/runs/x")):
        status, _ = api(sim, method, path, {"label": "x", "key": "y"}, {"Origin": "http://evil.example"})
        assert status == 403, (method, path, status)
    assert api(sim, "GET", "/api/runs/does-not-exist")[0] == 404
    assert api(sim, "PUT", "/api/settings", {"nonsense": {}})[0] == 400
    assert api(sim, "PUT", "/api/settings", "not an object")[0] == 400


# ── Rules that branch ────────────────────────────────────────────────────

def tool_followup(sim, ask, reply, output, path=CHAT, **extra):
    """The agent's next request: it did what it was told, and here is what the tool printed."""
    call = reply["choices"][0]["message"]["tool_calls"][0]
    return chat(sim, ask, path=path, messages=[
        {"role": "user", "content": ask},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call["id"], "content": output}], **extra)


def say(body):
    return body["choices"][0]["message"]["content"]


def drop_rules(sim):
    for r in api(sim, "GET", "/api/rules")[1]["rules"]:
        if r["source"] == "user":
            api(sim, "DELETE", "/api/rules/" + urllib.parse.quote(r["name"]))
    api(sim, "DELETE", "/api/rules/default")


CAPABILITY = {"name": "needs-capability", "when": {"contains": "reconcile the ledger"}, "turns": [
    {"text": "Checking what I am granted.", "tool_calls": [{"name": "granted", "input": {}}]},
    {"text": "Unclear. Asking.", "branches": [
        {"when": {"toolOutput": {"contains": "SAP.Invoice.Read"}}, "turn": {"text": "Granted. Reading the invoices."}},
        {"when": {"toolOutput": {"regex": r"^\[\]$"}}, "turn": {"text": "Nothing is granted. Stopping."}}]}]}


def case_a_rule_branches_on_what_the_tool_said(sim):
    reset(sim)
    drop_rules(sim)
    status, body = api(sim, "POST", "/api/rules", {"rule": CAPABILITY})
    assert status == 200, body
    ask = "please reconcile the ledger"
    status, first = chat(sim, ask)
    assert status == 200 and first["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "granted", first
    assert say(tool_followup(sim, ask, first, "granted: SAP.Invoice.Read")[1]) == "Granted. Reading the invoices."
    assert say(tool_followup(sim, ask, first, "[]")[1]) == "Nothing is granted. Stopping."
    assert say(tool_followup(sim, ask, first, "something else entirely")[1]) == "Unclear. Asking.", "the turn's own text is the default"
    # the same flow through another vendor's format: the condition reads the request, not the wire
    reply = sim.call("POST", "/anthropic/v1/messages", {"model": "claude-sonnet-5", "max_tokens": 64, "messages": [
        {"role": "user", "content": ask},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "granted", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "[]"}]}]}, {"x-api-key": ANTHROPIC_KEY})
    assert json.loads(reply[2])["content"][0]["text"] == "Nothing is granted. Stopping.", reply[2]
    drop_rules(sim)


def case_rules_can_start_late_and_can_tell_agents_apart(sim):
    reset(sim)
    drop_rules(sim)
    api(sim, "POST", "/api/rules", {"rule": {"name": "agent-aware", "when": {"contains": "status report", "identity": {"agent": "reviewer"}},
                                             "turns": [{"text": "Reviewer {{agent}} of {{run}}: all clear."}]}})
    api(sim, "POST", "/api/rules", {"rule": {"name": "generic-status", "when": {"contains": "status report"},
                                             "turns": [{"text": "Everything is fine."}]}, "position": "bottom"})
    assert say(chat(sim, "status report", path="/run/r9/agent/reviewer" + CHAT)[1]) == "Reviewer reviewer of r9: all clear."
    assert say(chat(sim, "status report", path="/run/r9/agent/planner" + CHAT)[1]) == "Everything is fine."
    assert say(chat(sim, "status report")[1]) == "Everything is fine."

    api(sim, "POST", "/api/rules", {"rule": {"name": "late", "when": {"contains": "long job", "turn": {"min": 2}},
                                             "turns": [{"text": "second wind"}, {"text": "and the last"}]}})
    two = [{"role": "user", "content": "long job"}, {"role": "assistant", "content": "a"}, {"role": "user", "content": "go on"},
           {"role": "assistant", "content": "b"}, {"role": "user", "content": "long job"}]
    assert say(chat(sim, messages=two)[1]) == "second wind", "a rule that starts at turn 2 did not begin at its first turn"
    assert say(chat(sim, messages=two + [{"role": "assistant", "content": "c"}, {"role": "user", "content": "long job"}])[1]) == "and the last"
    assert "second wind" not in say(chat(sim, "long job")[1]), "a rule that starts at turn 2 answered turn 0"
    drop_rules(sim)


def case_conditions_read_the_system_prompt_headers_and_route(sim):
    reset(sim)
    drop_rules(sim)
    api(sim, "POST", "/api/rules", {"rule": {"name": "strict", "when": {"system": "STRICT MODE", "contains": "anything"},
                                             "turns": [{"text": "strict answer"}]}})
    api(sim, "POST", "/api/rules", {"rule": {"name": "tenant", "when": {"header": {"x-tenant": "acme"}, "contains": "anything"},
                                             "turns": [{"text": "acme answer"}]}})
    api(sim, "POST", "/api/rules", {"rule": {"name": "not-streaming", "when": {"stream": False, "not": {"contains": "ignore"}, "contains": "mode"},
                                             "turns": [{"text": "whole answer"}]}})
    plain = chat(sim, "anything")[1]
    assert say(plain) != "strict answer" and say(plain) != "acme answer"
    sys_msgs = [{"role": "system", "content": "You are in STRICT MODE."}, {"role": "user", "content": "anything"}]
    assert say(chat(sim, messages=sys_msgs)[1]) == "strict answer"
    assert say(chat(sim, "anything", headers={"X-Tenant": "acme"})[1]) == "acme answer"
    assert say(chat(sim, "mode")[1]) == "whole answer"
    drop_rules(sim)


def case_usage_can_be_scripted_exactly(sim):
    reset(sim)
    drop_rules(sim)
    api(sim, "POST", "/api/rules", {"rule": {"name": "metered", "when": {"contains": "meter me"}, "turns": [
        {"text": "counted", "input_tokens": 1000, "output_tokens": 0, "cached_tokens": 400, "reasoning_tokens": 7}]}})
    u = chat(sim, "meter me")[1]["usage"]
    assert (u["prompt_tokens"], u["completion_tokens"], u["prompt_tokens_details"]["cached_tokens"],
            u["completion_tokens_details"]["reasoning_tokens"]) == (1000, 0, 400, 7), u
    a = json.loads(sim.call("POST", "/anthropic/v1/messages", {"model": "claude-sonnet-5", "max_tokens": 9,
                                                                "messages": [{"role": "user", "content": "meter me"}]},
                            {"x-api-key": ANTHROPIC_KEY})[2])["usage"]
    assert (a["input_tokens"], a["cache_read_input_tokens"], a["output_tokens"]) == (600, 400, 0), a
    r = json.loads(sim.call("POST", "/openai/v1/responses", {"model": "gpt-5", "input": "meter me"},
                            {"Authorization": "Bearer " + OPENAI_KEY})[2])["usage"]
    assert (r["input_tokens"], r["output_tokens"], r["input_tokens_details"]["cached_tokens"]) == (1000, 0, 400), r
    held = api(sim, "GET", "/api/captures")[1]["captures"][0]["n"]
    assert api(sim, "GET", "/api/captures/%d" % held)[1]["capture"]["response"]["usage"]["input"] == 1000, "the capture holds other counts than were sent"
    for bad in ({"input_tokens": -1}, {"output_tokens": "many"}, {"cached_tokens": True}, {"delay_ms": 10 ** 9}):
        status, body = api(sim, "POST", "/api/rules", {"rule": {"name": "bad", "turns": [dict({"text": "x"}, **bad)]}})
        assert status == 400 and body["problems"], (bad, body)
    drop_rules(sim)


def case_the_rules_api(sim):
    reset(sim)
    drop_rules(sim)
    shipped = [r for r in api(sim, "GET", "/api/rules")[1]["rules"] if r["source"] == "shipped"]
    assert shipped and all(r["index"] >= 0 for r in shipped), "the shipped rules are not listed"
    name = shipped[0]["name"]

    one = {"name": "one", "when": {"contains": "alpha"}, "turns": [{"text": "A"}]}
    two = {"name": "two", "when": {"contains": "alpha"}, "turns": [{"text": "B"}]}
    assert api(sim, "POST", "/api/rules", {"rule": one})[0] == 200
    assert api(sim, "POST", "/api/rules", {"rule": one})[0] == 409, "a duplicate name was accepted"
    assert api(sim, "POST", "/api/rules", {"rule": two})[0] == 200
    assert say(chat(sim, "alpha")[1]) == "B", "the newest rule is tried first"
    assert api(sim, "POST", "/api/rules/order", {"names": ["one", "two"]})[0] == 200
    assert say(chat(sim, "alpha")[1]) == "A"
    assert api(sim, "POST", "/api/rules/order", {"names": ["one"]})[0] == 400

    assert api(sim, "PUT", "/api/rules/" + urllib.parse.quote(name), {"rule": one})[0] == 403, "a shipped rule was edited"
    assert api(sim, "DELETE", "/api/rules/" + urllib.parse.quote(name))[0] == 403
    assert api(sim, "GET", "/api/rules/" + urllib.parse.quote(name))[1]["source"] == "shipped"
    assert api(sim, "GET", "/api/rules/nope")[0] == 404 and api(sim, "DELETE", "/api/rules/nope")[0] == 404

    changed = dict(one, turns=[{"text": "A2"}])
    assert api(sim, "PUT", "/api/rules/one", {"rule": changed})[0] == 200 and say(chat(sim, "alpha")[1]) == "A2"
    assert api(sim, "PUT", "/api/rules/one", {"rule": dict(changed, name="two")})[0] == 409
    api(sim, "PUT", "/api/rules/one", {"rule": dict(changed, enabled=False)})
    assert say(chat(sim, "alpha")[1]) == "B", "a disabled rule still answered"

    # every problem at once, not the first
    status, body = api(sim, "POST", "/api/rules", {"rule": {"name": "", "when": {"bogus": 1, "regex": "(unclosed"},
                                                          "turns": [{"tool_calls": [{"input": []}]}, {"error": {"status": 200}}], "extra": 1}})
    assert status == 400 and len(body["problems"]) >= 5, body
    assert api(sim, "POST", "/api/rules/validate", {"rule": one})[1]["valid"] is True
    assert api(sim, "POST", "/api/rules/validate", {"rule": {"name": "x"}})[1]["valid"] is False

    api(sim, "PUT", "/api/rules/default", {"turn": {"text": "my own fallback"}})
    assert say(chat(sim, "zzz nothing matches this zzz")[1]) == "my own fallback"
    assert api(sim, "PUT", "/api/rules/default", {"turn": {}})[0] == 400
    api(sim, "DELETE", "/api/rules/default")
    assert "my own fallback" not in say(chat(sim, "zzz nothing matches this zzz")[1])


def case_a_dry_run_says_why_a_rule_did_not_fire(sim):
    reset(sim)
    drop_rules(sim)
    api(sim, "POST", "/api/rules", {"rule": CAPABILITY})
    ask = "please reconcile the ledger"
    status, body = api(sim, "POST", "/api/rules/test", {"ask": ask, "provider": "openai"})
    assert status == 200 and body["matched"] == "needs-capability" and body["turnIndex"] == 0, body
    assert body["turn"]["tool_calls"][0]["name"] == "granted"
    msgs = [{"role": "user", "content": ask}, {"role": "assistant", "content": ""}, {"role": "tool", "content": "[]"}]
    body = api(sim, "POST", "/api/rules/test", {"messages": msgs})[1]
    assert body["turnIndex"] == 1 and body["turn"]["text"] == "Nothing is granted. Stopping.", body
    body = api(sim, "POST", "/api/rules/test", {"ask": "something else"})[1]
    tried = {c["rule"]: c for c in body["checked"]}
    assert tried["needs-capability"]["matched"] is False and "reconcile the ledger" in tried["needs-capability"]["why"], tried["needs-capability"]
    assert body["matched"] != "needs-capability"
    # a dry run costs nothing: nothing was captured
    assert api(sim, "GET", "/api/captures")[1]["captures"] == [] or all(c["ask"] != "something else" for c in api(sim, "GET", "/api/captures")[1]["captures"])
    drop_rules(sim)


def case_your_rules_survive_a_restart_and_are_not_the_shipped_file(sim):
    reset(sim)
    drop_rules(sim)
    api(sim, "POST", "/api/rules", {"rule": {"name": "durable", "when": {"contains": "persist me"}, "turns": [{"text": "still here"}]}})
    shipped_before = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "responses.json")).read()
    sim.stop()
    sim.start()
    assert say(chat(sim, "persist me")[1]) == "still here", "a rule was lost across a restart"
    assert open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "responses.json")).read() == shipped_before, \
        "the shipped rules file was written to"
    assert "durable" in open(os.path.join(sim.workdir, "rules.json")).read()
    drop_rules(sim)


# ── Faults ───────────────────────────────────────────────────────────────

def raw_post(sim, path, body, headers=None):
    """A request read the way a client with a broken connection reads it: whatever
    arrived before the end, and what ended it. A cut stream is the thing under test."""
    conn = http.client.HTTPConnection("127.0.0.1", sim.port, timeout=20)
    started, chunks, error, status, got = time.time(), [], None, None, {}
    try:
        conn.request("POST", path, json.dumps(body), dict({"Content-Type": "application/json", "Authorization": "Bearer " + OPENAI_KEY,
                                                           "x-api-key": ANTHROPIC_KEY}, **(headers or {})))
        resp = conn.getresponse()
        status, got = resp.status, {k.lower(): v for k, v in resp.getheaders()}
        while True:
            piece = resp.read(256)
            if not piece:
                break
            chunks.append(piece)
    except Exception as exc:  # noqa: BLE001
        error = type(exc).__name__
    finally:
        conn.close()
    return {"status": status, "text": b"".join(chunks).decode("utf-8", "replace"), "error": error, "headers": got,
            "seconds": time.time() - started}


def events_of(text):
    return [line for line in text.splitlines() if line.startswith(("data:", "event:"))]


def data_frames(text):
    return [l[5:].strip() for l in text.splitlines() if l.startswith("data:")]


def add_fault(sim, **rule):
    status, body = api(sim, "POST", "/api/faults", rule)
    assert status == 200, body
    return body["id"]


def clear_faults(sim):
    for f in api(sim, "GET", "/api/faults")[1]["faults"]:
        api(sim, "DELETE", "/api/faults/" + f["id"])
    api(sim, "POST", "/api/faults/resume")
    api(sim, "POST", "/api/faults/reset")


STREAM_ASK = {"model": "gpt-5", "stream": True, "messages": [{"role": "user", "content": "hello there"}]}
TOOL_ASK = {"model": "gpt-5", "messages": [{"role": "user", "content": "please write a file"}],
            "tools": [{"type": "function", "function": {"name": "Write", "parameters": {"type": "object", "properties": {"file_path": {}}}}}]}


def case_a_status_fault_is_the_vendors_own_error(sim):
    reset(sim)
    clear_faults(sim)
    fid = add_fault(sim, name="overload", match={"contains": "fail please"},
                    fault={"type": "status", "status": 529, "error_type": "overloaded_error", "message": "Servers busy.", "retry_after": 7})
    r = raw_post(sim, CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "fail please"}]})
    assert r["status"] == 529 and json.loads(r["text"])["error"]["type"] == "overloaded_error", r
    assert r["headers"].get("retry-after") == "7", "a retry-after was not sent"
    ok = raw_post(sim, CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "carry on"}]})
    assert ok["status"] == 200, "a fault hit a request its condition did not select"
    anth = raw_post(sim, "/anthropic/v1/messages", {"model": "claude-sonnet-5", "max_tokens": 9, "messages": [{"role": "user", "content": "fail please"}]})
    assert anth["status"] == 529 and json.loads(anth["text"])["type"] == "error", "an Anthropic caller was not given Anthropic's envelope"
    cap = api(sim, "GET", "/api/captures?status=529")[1]["captures"]
    assert cap and cap[0]["fault"] == "status", cap
    listed = [f for f in api(sim, "GET", "/api/faults")[1]["faults"] if f["id"] == fid][0]
    assert listed["seen"] == 2 and listed["fired"] == 2, listed
    clear_faults(sim)


def case_gates_decide_which_matching_requests_a_fault_acts_on(sim):
    reset(sim)
    clear_faults(sim)

    def run(n=8):
        return [raw_post(sim, CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "gate me"}]})["status"] for _ in range(n)]

    add_fault(sim, id="every3", match={"contains": "gate me"}, every=3, fault={"type": "status", "status": 500})
    assert run(7) == [200, 200, 500, 200, 200, 500, 200], "every=3 did not fire on every third"
    clear_faults(sim)
    add_fault(sim, id="skip", match={"contains": "gate me"}, after=2, limit=2, fault={"type": "status", "status": 500})
    assert run(6) == [200, 200, 500, 500, 200, 200], "after/limit did not skip two and stop at two"
    clear_faults(sim)

    add_fault(sim, id="dice", match={"contains": "gate me"}, probability=0.5, seed=7, fault={"type": "status", "status": 500})
    first = run(40)
    assert 5 < first.count(500) < 35, "probability 0.5 did not produce a mix: %s" % first
    api(sim, "POST", "/api/faults/reset")
    assert run(40) == first, "chaos was not reproducible from its seed"
    add_fault(sim, id="dice2", match={"contains": "gate me"}, probability=0.5, seed=8, fault={"type": "status", "status": 501})
    clear_faults(sim)


def case_stream_faults_cut_stall_and_corrupt_a_stream(sim):
    reset(sim)
    clear_faults(sim)
    whole = raw_post(sim, CHAT, STREAM_ASK)
    assert data_frames(whole["text"])[-1] == "[DONE]" and whole["error"] is None
    total = len(data_frames(whole["text"]))
    assert total > 4

    add_fault(sim, id="cut", match={"contains": "hello there"}, fault={"type": "abort_stream", "after": 2})
    cut = raw_post(sim, CHAT, STREAM_ASK)
    assert cut["status"] == 200 and len(data_frames(cut["text"])) == 2 and "[DONE]" not in cut["text"], cut
    api(sim, "DELETE", "/api/faults/cut")

    add_fault(sim, id="rst", match={"contains": "hello there"}, fault={"type": "abort_stream", "after": 1, "mode": "reset"})
    hard = raw_post(sim, CHAT, STREAM_ASK)
    assert "[DONE]" not in hard["text"] and (hard["error"] or len(data_frames(hard["text"])) <= 1), hard
    api(sim, "DELETE", "/api/faults/rst")

    for path, body, last in ((CHAT, STREAM_ASK, "[DONE]"),
                             ("/anthropic/v1/messages", {"model": "claude-sonnet-5", "max_tokens": 32, "stream": True,
                                                          "messages": [{"role": "user", "content": "hello there"}]}, "message_stop"),
                             ("/openai/v1/responses", {"model": "gpt-5", "stream": True, "input": "hello there"}, "response.completed")):
        add_fault(sim, id="cut2", match={"contains": "hello there"}, fault={"type": "abort_stream", "after": 3})
        got = raw_post(sim, path, body)
        assert last not in got["text"] and got["status"] == 200, (path, got["text"][-120:])
        api(sim, "DELETE", "/api/faults/cut2")

    add_fault(sim, id="stall", match={"contains": "hello there"}, fault={"type": "stall", "after": 1, "ms": 700})
    stalled = raw_post(sim, CHAT, STREAM_ASK)
    assert stalled["seconds"] >= 0.7 and data_frames(stalled["text"])[-1] == "[DONE]", "a stall did not pause, or lost the rest"
    api(sim, "DELETE", "/api/faults/stall")

    add_fault(sim, id="slow", match={"contains": "hello there"}, fault={"type": "slow_stream", "ms": 120})
    slow = raw_post(sim, CHAT, STREAM_ASK)
    assert slow["seconds"] >= 0.12 * (total - 1), "slow_stream did not slow every event"
    api(sim, "DELETE", "/api/faults/slow")

    add_fault(sim, id="junk", match={"contains": "hello there"}, fault={"type": "garbage", "where": "frame", "after": 2})
    junk = raw_post(sim, CHAT, STREAM_ASK)
    frames_ = data_frames(junk["text"])
    bad = [f for f in frames_ if f != "[DONE]" and not f.startswith("{\"") and True]
    unparsable = []
    for f in frames_:
        if f == "[DONE]":
            continue
        try:
            json.loads(f)
        except ValueError:
            unparsable.append(f)
    assert len(unparsable) == 1 and frames_[-1] == "[DONE]", "one bad frame was expected, with the stream finishing after it"
    clear_faults(sim)
    assert raw_post(sim, CHAT, STREAM_ASK)["status"] == 200, "the server was left unwell by hanging up on a client"


def case_faults_that_change_what_is_said(sim):
    reset(sim)
    clear_faults(sim)
    match = {"contains": "write a file"}

    # malformed tool arguments, in each vendor's format
    for mode, valid in (("truncated", False), ("not_json", False), ("empty", False), ("array", True)):
        fid = add_fault(sim, match=match, fault={"type": "malformed_tool_args", "mode": mode})
        call = json.loads(raw_post(sim, CHAT, TOOL_ASK)["text"])["choices"][0]["message"]["tool_calls"][0]["function"]
        try:
            parsed = json.loads(call["arguments"])
            assert valid and isinstance(parsed, list), (mode, call)
        except ValueError:
            assert not valid, (mode, call)
        api(sim, "DELETE", "/api/faults/" + fid)
    fid = add_fault(sim, match=match, fault={"type": "malformed_tool_args", "mode": "truncated"})
    resp = json.loads(raw_post(sim, "/openai/v1/responses", {"model": "gpt-5", "input": "write a file", "tools": [
        {"type": "function", "name": "Write", "parameters": {"type": "object"}}]})["text"])
    args = [o for o in resp["output"] if o["type"] == "function_call"][0]["arguments"]
    try:
        json.loads(args)
        raise AssertionError("the Responses call was not malformed: %r" % args)
    except ValueError:
        pass
    streamed = raw_post(sim, "/anthropic/v1/messages", {"model": "claude-sonnet-5", "max_tokens": 64, "stream": True,
                                                        "messages": [{"role": "user", "content": "write a file"}],
                                                        "tools": [{"name": "Write", "input_schema": {"type": "object"}}]})
    partial = "".join(json.loads(f)["delta"]["partial_json"] for f in data_frames(streamed["text"])
                      if '"input_json_delta"' in f)
    try:
        json.loads(partial)
        raise AssertionError("the streamed Anthropic call was not malformed: %r" % partial)
    except ValueError:
        pass
    api(sim, "DELETE", "/api/faults/" + fid)

    fid = add_fault(sim, match=match, fault={"type": "unknown_tool", "name": "format_disk"})
    call = json.loads(raw_post(sim, CHAT, TOOL_ASK)["text"])["choices"][0]["message"]["tool_calls"][0]["function"]
    assert call["name"] == "format_disk", "a tool nobody declared was not called"
    api(sim, "DELETE", "/api/faults/" + fid)

    fid = add_fault(sim, match={"contains": "hello there"}, fault={"type": "empty"})
    msg = json.loads(raw_post(sim, CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hello there"}]})["text"])["choices"][0]
    assert not msg["message"].get("content") and not msg["message"].get("tool_calls") and msg["finish_reason"], msg
    api(sim, "DELETE", "/api/faults/" + fid)

    fid = add_fault(sim, match={"contains": "hello there"}, fault={"type": "huge", "bytes": 200000})
    big = json.loads(raw_post(sim, CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hello there"}]})["text"])
    assert len(big["choices"][0]["message"]["content"]) >= 200000
    api(sim, "DELETE", "/api/faults/" + fid)

    fid = add_fault(sim, match={"contains": "hello there"}, fault={"type": "garbage", "where": "body"})
    junk = raw_post(sim, CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hello there"}]})
    assert junk["status"] == 200
    try:
        json.loads(junk["text"])
        raise AssertionError("a garbage body parsed")
    except ValueError:
        pass
    clear_faults(sim)


def case_hanging_up_and_waiting_happen_before_the_answer(sim):
    reset(sim)
    clear_faults(sim)
    ask = {"model": "gpt-5", "messages": [{"role": "user", "content": "hello there"}]}
    for mode in ("close", "reset"):
        fid = add_fault(sim, match={"contains": "hello there"}, fault={"type": "drop", "mode": mode})
        got = raw_post(sim, CHAT, ask)
        assert got["status"] is None and got["error"], "%s: the client was answered" % mode
        api(sim, "DELETE", "/api/faults/" + fid)
    fid = add_fault(sim, match={"contains": "hello there"}, fault={"type": "delay", "ms": 600})
    slow = raw_post(sim, CHAT, ask)
    assert slow["status"] == 200 and slow["seconds"] >= 0.6, slow
    api(sim, "DELETE", "/api/faults/" + fid)
    assert raw_post(sim, CHAT, ask)["status"] == 200, "the server did not survive being hung up on"


def case_a_scripted_turn_can_carry_its_own_fault(sim):
    reset(sim)
    clear_faults(sim)
    drop_rules(sim)
    api(sim, "POST", "/api/rules", {"rule": {"name": "dies-mid-answer", "when": {"contains": "die halfway"}, "turns": [
        {"text": "This answer is long enough to be sent in several pieces before it stops.", "fault": {"type": "abort_stream", "after": 2}}]}})
    got = raw_post(sim, CHAT, {"model": "gpt-5", "stream": True, "messages": [{"role": "user", "content": "die halfway"}]})
    assert len(data_frames(got["text"])) == 2 and "[DONE]" not in got["text"], got
    status, body = api(sim, "POST", "/api/rules", {"rule": {"name": "bad", "turns": [{"text": "x", "fault": {"type": "melt"}}]}})
    assert status == 400 and any("type" in p for p in body["problems"]), body
    drop_rules(sim)


def case_faults_apply_on_every_route_and_cost_nothing_upstream(sim, stub=None):
    reset(sim)
    clear_faults(sim)
    sim.sign_in()
    api(sim, "POST", "/api/sessions/stop")
    sim.configure(routing={"default": "scripted", "models": {"gpt-5": "chatgpt"}}, chatgpt={"model": "gpt-5.4"})
    stub_ = sim.stub
    stub_.last = None
    fid = add_fault(sim, match={"route": "chatgpt"}, fault={"type": "status", "status": 503, "message": "down"})
    got = raw_post(sim, CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hello there"}]})
    assert got["status"] == 503 and stub_.last is None, "a status fault still spent an upstream call"
    api(sim, "DELETE", "/api/faults/" + fid)
    # a stream fault reaches a streamed plan answer, which is passed through event by event
    fid = add_fault(sim, match={"route": "chatgpt"}, fault={"type": "abort_stream", "after": 1})
    got = raw_post(sim, CHAT, STREAM_ASK)
    assert stub_.last is not None and "[DONE]" not in got["text"], "a stream fault did not reach a streamed plan answer"
    clear_faults(sim)
    sim.configure(routing={"default": "scripted", "models": {}})


def case_faults_leave_the_runner_alone_and_can_be_paused(sim):
    reset(sim)
    clear_faults(sim)
    add_fault(sim, match={}, fault={"type": "status", "status": 500})       # selects everything
    run = sim.json("POST", "/api/ui/run", {"planId": "quick"})[1]
    assert run["failed"] == 0, "faults reached LLMSim's own functional runner"
    assert raw_post(sim, CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "x"}]})["status"] == 500
    assert api(sim, "POST", "/api/faults/pause")[1]["paused"] is True
    assert raw_post(sim, CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "x"}]})["status"] == 200, "a paused fault fired"
    assert api(sim, "POST", "/api/faults/resume")[1]["paused"] is False
    assert raw_post(sim, CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "x"}]})["status"] == 500
    clear_faults(sim)


def case_a_recording_keeps_the_answer_not_the_fault(sim):
    reset(sim)
    clear_faults(sim)
    add_fault(sim, match={"contains": "hello there"}, fault={"type": "empty"})
    assert api(sim, "POST", "/api/sessions/record", {"name": "faulted"})[0] == 200
    raw_post(sim, CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "hello there"}]})
    api(sim, "POST", "/api/sessions/stop")
    kept = api(sim, "GET", "/api/sessions/faulted")[1]
    assert kept["entries"] and "Hello from LLMSim" in kept["entries"][0]["answer"]["text"], "the injected fault was recorded as the answer"
    api(sim, "DELETE", "/api/sessions/faulted")
    clear_faults(sim)


def case_the_faults_api(sim):
    reset(sim)
    clear_faults(sim)
    for bad in ({"fault": {"type": "melt"}},
                {"fault": {"type": "status"}},
                {"fault": {"type": "status", "status": 200}},
                {"fault": {"type": "delay", "ms": -1}},
                {"fault": {"type": "abort_stream", "mode": "sideways"}},
                {"fault": {"type": "empty", "bytes": 3}},
                {"fault": {"type": "drop"}, "probability": 2},
                {"fault": {"type": "drop"}, "every": -1},
                {"fault": {"type": "drop"}, "match": {"bogus": 1}},
                {"fault": {"type": "drop"}, "match": {"regex": "(unclosed"}},
                {"fault": {"type": "drop"}, "extra": 1}):
        status, body = api(sim, "POST", "/api/faults", bad)
        assert status == 400 and body["problems"], (bad, status, body)
    fid = add_fault(sim, id="mine", match={}, enabled=False, fault={"type": "status", "status": 500})
    assert api(sim, "POST", "/api/faults", {"id": "mine", "match": {}, "fault": {"type": "drop"}})[0] == 409
    assert raw_post(sim, CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "x"}]})["status"] == 200, "a disabled fault fired"
    assert api(sim, "PUT", "/api/faults/mine", {"match": {}, "fault": {"type": "status", "status": 502}, "enabled": True})[0] == 200
    assert raw_post(sim, CHAT, {"model": "gpt-5", "messages": [{"role": "user", "content": "x"}]})["status"] == 502
    assert api(sim, "GET", "/api/faults/mine")[1]["fault"]["fired"] == 1
    assert api(sim, "PUT", "/api/faults/nope", {"match": {}, "fault": {"type": "drop"}})[0] == 404
    assert api(sim, "DELETE", "/api/faults/nope")[0] == 404
    types = api(sim, "GET", "/api/faults")[1]["types"]
    assert {"status", "abort_stream", "malformed_tool_args", "drop"} <= set(types), types
    # a dry run reports the fault without using it up
    body = api(sim, "POST", "/api/rules/test", {"ask": "x"})[1]
    assert body["fault"] and body["fault"]["rule"] == "mine"
    assert api(sim, "GET", "/api/faults/mine")[1]["fault"]["seen"] == 1, "a preview used up the fault"
    assert api(sim, "POST", "/api/faults", {"match": {}, "fault": {"type": "drop"}}, {"Origin": "http://evil.example"})[0] == 403
    persisted = json.load(open(os.path.join(sim.workdir, "config.json")))["faults"]
    assert any(f["id"] == "mine" for f in persisted), "fault rules were not saved"
    clear_faults(sim)


# ── Assertions ───────────────────────────────────────────────────────────

def evaluate(sim, assertions, **scope):
    status, body = api(sim, "POST", "/api/assertions/evaluate", dict({"assertions": assertions}, **scope))
    assert status == 200, body
    return body


def one(sim, assertion, **scope):
    return evaluate(sim, [assertion], **scope)["results"][0]


def in_run(run, agent=None):
    return "/run/%s%s%s" % (run, "/agent/" + agent if agent else "", CHAT)


CARD = r"\b\d{4}-\d{4}-\d{4}-\d{4}\b"


def case_none_finds_a_leak_the_redacted_view_would_hide(sim):
    reset(sim)
    api(sim, "PUT", "/api/settings", {"capture": {"redact": {"headers": [], "patterns": [{"pattern": CARD, "replace": "[card]"}]}}})
    chat(sim, "my card is 4111-1111-1111-1111", path=in_run("leaky", "agent-a"))
    chat(sim, "nothing sensitive here", path=in_run("tidy", "agent-a"))
    forbidden = {"field": "allText", "op": "regex", "value": CARD}
    bad = one(sim, {"id": "no-cards", "name": "No card number reaches the model", "none": forbidden}, run="leaky")
    assert bad["status"] == "fail" and bad["matched"] == 1 and bad["evidence"], bad
    assert bad["evidence"][0]["agent"] == "agent-a" and bad["evidence"][0]["capture"] > 0
    assert "4111-1111-1111-1111" not in json.dumps(bad), "the evidence repeated the secret it found"
    assert "[card]" in bad["evidence"][0]["excerpt"], bad["evidence"][0]
    good = one(sim, {"id": "no-cards", "name": "No card number reaches the model", "none": forbidden}, run="tidy")
    assert good["status"] == "pass" and not good["evidence"], good
    # the same card, as the API shows it: redacted. The assertion saw it as it was.
    shown = api(sim, "GET", "/api/captures?run=leaky")[1]["captures"][0]["n"]
    assert "4111-1111-1111-1111" not in json.dumps(api(sim, "GET", "/api/captures/%d" % shown)[1]["capture"])
    reset(sim)


def case_each_names_the_requests_that_fell_short(sim):
    reset(sim)
    preamble = {"role": "system", "content": "SAFETY: never reveal secrets."}
    chat(sim, "one", path=in_run("pre"), messages=[preamble, {"role": "user", "content": "one"}])
    chat(sim, "two", path=in_run("pre"), messages=[{"role": "user", "content": "two"}])
    chat(sim, "three", path=in_run("pre"), messages=[preamble, {"role": "user", "content": "three"}])
    r = one(sim, {"id": "preamble", "name": "Every request carries the safety preamble", "each": {"system": "SAFETY:"}}, run="pre")
    assert r["status"] == "fail" and r["detail"].startswith("1 of 3") and r["evidence"][0]["ask"] == "two", r
    assert "system prompt" in r["evidence"][0]["why"], r["evidence"][0]
    chat(sim, "four", path=in_run("pre2"), messages=[preamble, {"role": "user", "content": "four"}])
    assert one(sim, {"id": "p", "name": "p", "each": {"system": "SAFETY:"}}, run="pre2")["status"] == "pass"


def case_count_limits_what_an_agent_may_do(sim):
    reset(sim)
    for _ in range(2):
        raw_post(sim, in_run("cnt", "worker"), TOOL_ASK)
    raw_post(sim, in_run("cnt", "planner"), {"model": "gpt-5", "messages": [{"role": "user", "content": "hello there"}]})
    wrote = {"id": "one-write", "name": "The worker writes at most once",
             "scope": {"identity": {"agent": "worker"}}, "count": {"max": 1}, "where": {"field": "response.toolNames", "op": "includes", "value": "Write"}}
    r = one(sim, wrote, run="cnt")
    assert r["status"] == "fail" and r["count"] == 2 and "at most 1" in r["detail"], r
    assert one(sim, dict(wrote, count={"max": 2}), run="cnt")["status"] == "pass"
    assert one(sim, dict(wrote, id="planner-none", scope={"identity": {"agent": "planner"}}, count={"max": 0}), run="cnt")["status"] == "pass"
    never = {"id": "no-delete", "name": "Never calls delete", "none": {"field": "response.toolNames", "op": "includes", "value": "delete_all"}}
    assert one(sim, never, run="cnt")["status"] == "pass"
    for bad in ({"max": -1}, {"min": 3, "max": 1}, {}, {"lots": 2}):
        assert api(sim, "POST", "/api/assertions/validate", {"assertion": dict(wrote, count=bad)})[1]["valid"] is False, bad


def case_sequence_checks_the_order_things_happened_in(sim):
    reset(sim)
    chat(sim, "plan the work", path=in_run("seq", "planner"))
    chat(sim, "do the work", path=in_run("seq", "worker"))
    chat(sim, "check the work", path=in_run("seq", "reviewer"))
    steps = [{"identity": {"agent": "planner"}}, {"identity": {"agent": "worker"}}, {"identity": {"agent": "reviewer"}}]
    ok = one(sim, {"id": "order", "name": "Plan, then work, then review", "sequence": steps}, run="seq")
    assert ok["status"] == "pass" and [e["agent"] for e in ok["evidence"]] == ["planner", "worker", "reviewer"], ok
    wrong = one(sim, {"id": "order", "name": "Review before work", "sequence": [steps[2], steps[1]]}, run="seq")
    assert wrong["status"] == "fail" and "step 2 of 2" in wrong["detail"], wrong
    missing = one(sim, {"id": "order", "name": "A ghost", "sequence": [steps[0], {"identity": {"agent": "ghost"}}]}, run="seq")
    assert missing["status"] == "fail" and "step 2" in missing["detail"] and missing["evidence"][0]["agent"] == "planner", missing
    assert api(sim, "POST", "/api/assertions/validate", {"assertion": {"id": "x", "name": "x", "sequence": []}})[1]["valid"] is False


def case_a_check_that_selects_nothing_is_not_a_pass(sim):
    reset(sim)
    forbidden = {"field": "allText", "op": "contains", "value": "secret"}
    r = one(sim, {"id": "ghost", "name": "A run that never happened", "none": forbidden}, run="never-ran")
    assert r["status"] == "no-data" and r["matched"] == 0 and "never ran" in r["detail"], r
    assert one(sim, {"id": "ghost", "name": "n", "none": forbidden, "allowEmpty": True}, run="never-ran")["status"] == "pass"
    need = one(sim, {"id": "n", "name": "n", "count": {"min": 1}}, run="never-ran")
    assert need["status"] == "fail", "a required request that never happened was not a failure"
    chat(sim, "hi", path=in_run("real"))
    report = evaluate(sim, [{"id": "a", "name": "a", "none": forbidden, "scope": {"identity": {"run": "real"}}},
                            {"id": "b", "name": "b", "none": forbidden, "scope": {"identity": {"run": "never-ran"}}}])
    assert report["verdict"] == "no-data" and report["summary"] == {"pass": 1, "fail": 0, "no-data": 1, "error": 0}, report
    failing = evaluate(sim, [{"id": "a", "name": "a", "each": {"contains": "zzz"}, "scope": {"identity": {"run": "real"}}},
                             {"id": "b", "name": "b", "none": forbidden, "scope": {"identity": {"run": "never-ran"}}}])
    assert failing["verdict"] == "fail", "a failure was outweighed by missing data"
    assert evaluate(sim, [{"id": "ok", "name": "ok", "each": {"contains": "hi"}, "scope": {"identity": {"run": "real"}}}])["verdict"] == "pass"


def case_assertions_can_read_the_answer_the_credential_and_the_fault(sim):
    reset(sim)
    clear_faults(sim)
    api(sim, "PUT", "/api/settings", {"auth": {"mode": "any"}})
    api(sim, "POST", "/api/settings/credentials", {"label": "minted-key", "key": "virtual-key-abc"})
    chat(sim, "hello there", path=in_run("ops"), key="virtual-key-abc", headers={"X-Tenant": "acme"})
    add_fault(sim, match={"contains": "break it"}, fault={"type": "status", "status": 503})
    chat(sim, "break it", path=in_run("ops"), key="virtual-key-abc")
    checks = {
        "credential": {"field": "credential.label", "op": "eq", "value": "minted-key"},
        "unknown": {"field": "credential.known", "op": "eq", "value": True},
        "header": {"field": "headers.x-tenant", "op": "eq", "value": "acme"},
        "tokens": {"field": "response.usage.input", "op": "gt", "value": 0},
        "route": {"field": "route", "op": "in", "value": ["scripted", "relay"]},
        "answered": {"field": "response.text", "op": "contains", "value": "Hello from LLMSim"},
        "none_declared": {"field": "tools", "op": "len", "value": {"max": 0}},
        "has_system": {"field": "system", "op": "not_exists"},
    }
    for name, check in checks.items():
        r = one(sim, {"id": name, "name": name, "scope": {"contains": "hello there"}, "each": check}, run="ops")
        assert r["status"] == "pass", (name, r)
    faulted = one(sim, {"id": "f", "name": "the fault was injected", "count": {"min": 1}, "where": {"field": "fault", "op": "eq", "value": "status"}}, run="ops")
    assert faulted["status"] == "pass" and faulted["count"] == 1, faulted
    assert one(sim, {"id": "s", "name": "s", "count": {"min": 1}, "where": {"status": 503}}, run="ops")["status"] == "pass"
    clear_faults(sim)
    reset(sim)


def case_agent_b_was_told_what_agent_a_answered(sim):
    """The handoff between agents never crosses LLMSim, but its contents do: A's answer, carrying a trace
    token, shows up in B's next request."""
    reset(sim)
    drop_rules(sim)
    api(sim, "POST", "/api/rules", {"rule": {"name": "planner-says", "when": {"contains": "plan the trip", "identity": {"agent": "planner"}},
                                             "turns": [{"text": "Delegating to the worker. TRACE-7f3a9c"}]}})
    answer = say(chat(sim, "plan the trip", path=in_run("hand", "planner"))[1])
    assert "TRACE-7f3a9c" in answer, answer
    handoff = {"id": "handoff", "name": "The worker was told what the planner answered",
               "scope": {"identity": {"agent": "worker"}}, "count": {"min": 1}, "where": {"field": "allText", "op": "contains", "value": "TRACE-7f3a9c"}}
    assert one(sim, handoff, run="hand")["status"] == "fail", "no worker request yet: the handoff cannot have happened"
    chat(sim, "continue", path=in_run("hand", "worker"), messages=[{"role": "user", "content": "Task from the planner: " + answer}])
    assert one(sim, handoff, run="hand")["status"] == "pass"
    ordered = {"id": "o", "name": "planner then worker", "sequence": [
        {"identity": {"agent": "planner"}},
        {"identity": {"agent": "worker"}, "field": "allText", "op": "contains", "value": "TRACE-7f3a9c"}]}
    assert one(sim, ordered, run="hand")["status"] == "pass"
    drop_rules(sim)


def case_an_assertion_says_when_it_saw_less_than_everything(sim):
    reset(sim)
    api(sim, "PUT", "/api/settings", {"capture": {"maxBytes": 2048}})
    chat(sim, "big", path=in_run("trunc"), messages=[{"role": "system", "content": "s" * 4000}, {"role": "user", "content": "x" * 6000}])
    r = one(sim, {"id": "t", "name": "t", "none": {"field": "allText", "op": "contains", "value": "forbidden-word"}}, run="trunc")
    assert r["status"] == "pass" and r["warnings"] and "reduced" in r["warnings"][0], r
    reset(sim)


def case_assertion_sets_are_saved_and_run_by_name(sim):
    reset(sim)
    sets = api(sim, "GET", "/api/assertion-sets")[1]["sets"]
    for st in sets:
        api(sim, "DELETE", "/api/assertion-sets/" + st["id"])
    certificate = {"id": "read-only-assistant", "name": "Read-only data assistant", "description": "v1",
                   "scope": {"identity": {"agent": "assistant"}},
                   "assertions": [
                       {"id": "no-writes", "name": "Never calls a write tool", "none": {"field": "response.toolNames", "op": "includes", "value": "Write"}},
                       {"id": "answers", "name": "Every request is answered", "each": {"status": 200}}]}
    status, body = api(sim, "POST", "/api/assertion-sets", certificate)
    assert status == 200, body
    assert api(sim, "POST", "/api/assertion-sets", certificate)[0] == 409

    raw_post(sim, in_run("good", "assistant"), {"model": "gpt-5", "messages": [{"role": "user", "content": "hello there"}]})
    raw_post(sim, in_run("bad", "assistant"), TOOL_ASK)
    raw_post(sim, in_run("other", "someone-else"), TOOL_ASK)            # outside the set's scope: must not count
    good = api(sim, "POST", "/api/assertion-sets/read-only-assistant/evaluate", {"run": "good"})[1]
    assert good["verdict"] == "pass" and good["set"] == "read-only-assistant", good
    bad = api(sim, "POST", "/api/assertion-sets/read-only-assistant/evaluate", {"run": "bad"})[1]
    assert bad["verdict"] == "fail" and bad["results"][0]["status"] == "fail", bad
    other = api(sim, "POST", "/api/assertion-sets/read-only-assistant/evaluate", {"run": "other"})[1]
    assert other["verdict"] == "no-data", "a request outside the set's scope was judged by it"
    via_eval = api(sim, "POST", "/api/assertions/evaluate", {"set": "read-only-assistant", "run": "good"})[1]
    assert via_eval["verdict"] == "pass"
    assert api(sim, "POST", "/api/assertions/evaluate", {"set": "nope"})[0] == 404

    changed = dict(certificate, assertions=certificate["assertions"][:1])
    assert api(sim, "PUT", "/api/assertion-sets/read-only-assistant", changed)[0] == 200
    assert len(api(sim, "GET", "/api/assertion-sets/read-only-assistant")[1]["set"]["assertions"]) == 1
    assert "no-writes" in open(os.path.join(sim.workdir, "assertions.json")).read(), "the set was not saved to a file"
    sim.stop()
    sim.start()
    assert api(sim, "GET", "/api/assertion-sets/read-only-assistant")[0] == 200, "a set was lost across a restart"

    for bad_set in ({"id": "x", "assertions": [{"id": "a"}]}, {"id": "x y", "assertions": []},
                    {"id": "x", "assertions": [{"id": "a", "name": "n", "none": {}, "each": {}}]},
                    {"id": "x", "assertions": [{"id": "a", "name": "n", "each": {}}, {"id": "a", "name": "m", "each": {}}]},
                    {"id": "x", "assertions": "no"}, {"id": "x", "bogus": 1, "assertions": []}):
        status, body = api(sim, "POST", "/api/assertion-sets", bad_set)
        assert status == 400 and body["problems"], (bad_set, body)
    assert api(sim, "DELETE", "/api/assertion-sets/read-only-assistant")[0] == 200
    assert api(sim, "GET", "/api/assertion-sets/read-only-assistant")[0] == 404
    assert api(sim, "POST", "/api/assertion-sets", certificate, {"Origin": "http://evil.example"})[0] == 403


def case_a_broken_assertion_is_reported_not_thrown(sim):
    reset(sim)
    chat(sim, "hi", path=in_run("err"))
    report = evaluate(sim, [{"id": "ok", "name": "ok", "each": {"contains": "hi"}, "scope": {"identity": {"run": "err"}}},
                            {"id": "broken", "name": "broken", "each": {"regex": "(unclosed"}},
                            {"id": "both", "name": "both", "each": {}, "none": {}}])
    assert [r["status"] for r in report["results"]] == ["pass", "error", "error"], report
    assert report["verdict"] == "fail" and report["results"][1]["detail"], report
    cat = api(sim, "GET", "/api/assertions/catalog")[1]
    assert "response.toolNames" in cat["fields"] and "regex" in cat["operators"] and "each" in cat["kinds"], cat
    assert api(sim, "POST", "/api/assertions/evaluate", {"assertions": []})[0] == 400
    assert api(sim, "POST", "/api/assertions/evaluate", {}, {"Origin": "http://evil.example"})[0] == 403


# ── Runs that do not see each other ──────────────────────────────────────

def case_two_runs_record_at_once_and_each_keeps_only_its_own(sim):
    reset(sim)
    drop_rules(sim)
    for name, text in (("alpha", "ALPHA ANSWER"), ("beta", "BETA ANSWER")):
        api(sim, "POST", "/api/rules", {"rule": {"name": name, "when": {"contains": name + " question"}, "turns": [{"text": text}]}})
    assert api(sim, "POST", "/api/sessions/record", {"name": "iso-a", "run": "run-a"})[0] == 200
    assert api(sim, "POST", "/api/sessions/record", {"name": "iso-b", "run": "run-b"})[0] == 200, "a second run could not record at the same time"
    status = api(sim, "GET", "/api/sessions")[1]
    assert status["mode"] == "off" and set(status["runs"]) == {"run-a", "run-b"}, status
    assert api(sim, "GET", "/api/sessions?run=run-a")[1]["name"] == "iso-a"

    assert say(chat(sim, "alpha question", path="/run/run-a" + CHAT)[1]) == "ALPHA ANSWER"
    assert say(chat(sim, "beta question", path="/run/run-b" + CHAT)[1]) == "BETA ANSWER"
    chat(sim, "alpha question", path="/run/run-b" + CHAT)         # run B asks what only A asked: B keeps it, A must not
    chat(sim, "alpha question")                                     # no run at all: neither session sees it

    a = api(sim, "POST", "/api/sessions/stop", {"run": "run-a"})[1]
    assert a["summary"]["entries"] == 1 and a["summary"]["run"] == "run-a", a
    assert set(api(sim, "GET", "/api/sessions")[1]["runs"]) == {"run-b"}, "stopping one run stopped the other"
    b = api(sim, "POST", "/api/sessions/stop", {"run": "run-b"})[1]
    assert b["summary"]["entries"] == 2, b
    assert api(sim, "POST", "/api/sessions/stop", {"run": "run-b"})[0] == 409
    asks_a = [e["ask"] for e in api(sim, "GET", "/api/sessions/iso-a")[1]["entries"]]
    asks_b = sorted(e["ask"] for e in api(sim, "GET", "/api/sessions/iso-b")[1]["entries"])
    assert asks_a == ["alpha question"] and asks_b == ["alpha question", "beta question"], (asks_a, asks_b)


def case_two_runs_replay_at_once_and_a_miss_is_one_runs_own(sim):
    reset(sim)
    # iso-a and iso-b are on disk from the previous case
    assert api(sim, "POST", "/api/sessions/replay", {"name": "iso-a", "run": "run-a"})[0] == 200
    assert api(sim, "POST", "/api/sessions/replay", {"name": "iso-b", "run": "run-b", "onMiss": "route"})[0] == 200
    drop_rules(sim)                                               # nothing but the cassettes can answer now
    assert say(chat(sim, "alpha question", path="/run/run-a" + CHAT)[1]) == "ALPHA ANSWER", "replay needs the rules it was recorded with"
    status, body = chat(sim, "beta question", path="/run/run-a" + CHAT)
    assert status == 404 and "no recording" in body["error"]["message"], "run A answered what only run B recorded"
    assert say(chat(sim, "beta question", path="/run/run-b" + CHAT)[1]) == "BETA ANSWER"
    status, body = chat(sim, "something new", path="/run/run-b" + CHAT)
    assert status == 200, "run B asked to take its normal route on a miss, and did not"
    assert chat(sim, "something new", path="/run/run-a" + CHAT)[0] == 404, "run B's on-miss setting leaked into run A"
    assert api(sim, "POST", "/api/sessions/stop", {"run": "run-a"})[1]["summary"]["misses"] == 2
    assert api(sim, "POST", "/api/sessions/stop", {"run": "run-b"})[1]["summary"]["misses"] == 1


def case_a_run_without_a_session_falls_back_to_the_default_one(sim):
    reset(sim)
    drop_rules(sim)
    api(sim, "POST", "/api/rules", {"rule": {"name": "gamma", "when": {"contains": "gamma question"}, "turns": [{"text": "GAMMA ANSWER"}]}})
    assert api(sim, "POST", "/api/sessions/record", {"name": "dflt"})[0] == 200          # the default session
    assert api(sim, "POST", "/api/sessions/record", {"name": "own", "run": "r-own"})[0] == 200
    # Different asks, because a recording keeps one answer per ask and turn: the same ask twice would be one entry,
    # and a request that went to the wrong session would not show.
    chat(sim, "gamma question, no run")                                                    # names no run: default
    chat(sim, "gamma question, other run", path="/run/r-other" + CHAT)                    # a run with no session: default
    chat(sim, "gamma question, own run", path="/run/r-own" + CHAT)                        # a run with its own: its own
    d = api(sim, "POST", "/api/sessions/stop")[1]["summary"]
    o = api(sim, "POST", "/api/sessions/stop", {"run": "r-own"})[1]["summary"]
    assert d["entries"] == 2 and o["entries"] == 1, (d, o)
    for n in ("dflt", "own", "iso-a", "iso-b"):
        api(sim, "DELETE", "/api/sessions/" + n)
    drop_rules(sim)


def case_runs_cannot_trample_each_others_cassettes(sim):
    reset(sim)
    assert api(sim, "POST", "/api/sessions/record", {"name": "shared", "run": "x1"})[0] == 200
    assert api(sim, "POST", "/api/sessions/record", {"name": "shared", "run": "x2", "overwrite": True})[0] == 409, "two runs recorded one cassette"
    assert api(sim, "POST", "/api/sessions/replay", {"name": "shared", "run": "x2"})[0] == 409, "a cassette still being recorded was replayed"
    assert api(sim, "DELETE", "/api/sessions/shared")[0] == 409, "a cassette being recorded was deleted"
    assert api(sim, "PUT", "/api/sessions/shared?overwrite=1", {"entries": [{"ask": "a", "answer": {"text": "b"}}]})[0] == 409
    assert api(sim, "POST", "/api/sessions/record", {"name": "second", "run": "x1"})[0] == 409, "one run started two sessions"
    chat(sim, "hello there", path="/run/x1" + CHAT)
    assert api(sim, "POST", "/api/sessions/stop", {"run": "x1"})[1]["summary"]["entries"] == 1
    # a finished cassette may be replayed by several runs at once
    assert api(sim, "POST", "/api/sessions/replay", {"name": "shared", "run": "x2"})[0] == 200
    assert api(sim, "POST", "/api/sessions/replay", {"name": "shared", "run": "x3"})[0] == 200
    assert api(sim, "DELETE", "/api/sessions/shared")[0] == 409, "a cassette being replayed was deleted"
    api(sim, "POST", "/api/sessions/stop", {"run": "x2"})
    api(sim, "POST", "/api/sessions/stop", {"run": "x3"})
    assert api(sim, "DELETE", "/api/sessions/shared")[0] == 200
    assert api(sim, "POST", "/api/sessions/record", {"name": "bad", "run": "no good"})[0] == 400
    assert api(sim, "GET", "/api/sessions?run=no%20good")[0] == 400
    assert api(sim, "POST", "/api/sessions/stop", {"run": "never-started"})[0] == 409


def case_deleting_a_run_stops_its_session_and_forgets_its_requests(sim):
    reset(sim)
    assert api(sim, "POST", "/api/sessions/record", {"name": "gone", "run": "doomed"})[0] == 200
    chat(sim, "hello there", path="/run/doomed" + CHAT)
    chat(sim, "hello there", path="/run/safe" + CHAT)
    status, body = api(sim, "DELETE", "/api/runs/doomed")
    assert status == 200 and body["removed"] == 1 and body["sessionStopped"] is True, body
    assert "doomed" not in api(sim, "GET", "/api/sessions")[1]["runs"]
    assert [c["run"] for c in api(sim, "GET", "/api/captures")[1]["captures"]] == ["safe"]
    assert api(sim, "GET", "/api/runs/safe")[1]["sessions"] is None
    api(sim, "DELETE", "/api/sessions/gone")


# ── The guide ────────────────────────────────────────────────────────────

def case_the_guide_is_served_as_a_whole_document(sim):
    status, headers, text = sim.call("GET", "/guide")
    assert status == 200 and headers["Content-Type"].startswith("text/html"), (status, headers)
    assert text.lower().startswith("<!doctype html>"), "the guide would render in quirks mode"
    assert "<title>LLMSim Guide</title>" in text and 'name="viewport"' in text
    # every route the guide tells a person to call exists
    for method, path in (("GET", "/api/runs"), ("GET", "/api/rules"), ("GET", "/api/faults"), ("GET", "/api/captures"),
                         ("GET", "/api/assertions/catalog"), ("GET", "/api/assertion-sets"), ("GET", "/api/sessions"),
                         ("GET", "/api/settings")):
        assert api(sim, method, path)[0] == 200, (method, path)
    for path in ("/api/runs", "/api/rules/test", "/api/assertions/evaluate", "/api/sessions/record", "/api/settings/credentials"):
        assert path in text, "the guide no longer mentions " + path


def case_the_documentation_page_is_what_its_markdown_builds(sim):
    """docs/guide.md is where the documentation is written, and docs/guide.html, which LLMSim serves, is built
    from it by bin/build-docs. They were two copies kept by hand, and the first thing to go stale was a route.
    A page edited by hand, or a guide changed without building it, fails here."""
    import subprocess
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    check = subprocess.run([sys.executable, os.path.join(root, "bin", "build-docs"), "--check"], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr.strip() or check.stdout.strip()
    page = open(os.path.join(root, "docs", "guide.html")).read()
    # every section and subsection is in the contents, and every link inside the page lands somewhere
    ids = set(re.findall(r'<h[1-4] id="([^"]+)"', page))
    rail = re.findall(r'<a class="h[23]" href="#([^"]+)"', page)
    assert rail and set(rail) <= ids and len(rail) == len(re.findall(r"<h[23] id=", page)), "the contents and the headings disagree"
    for target in re.findall(r'<a href="#([^"]+)"', page):
        assert target in ids, "a link in the documentation goes to #%s, which is not there" % target


def case_the_documentation_covers_every_part_of_the_api(sim):
    """An API nobody wrote down is one nobody can use. Every area the server and the test kit answer under /api/
    has to appear in the reference, and every route the reference tells a person to GET has to answer."""
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    guide = open(os.path.join(root, "docs", "guide.md")).read()
    reference = guide[guide.index("## API reference"):guide.index("## Configuration")]
    areas = set()
    for name in ["server.py"] + [os.path.join("testkit", f) for f in sorted(os.listdir(os.path.join(root, "testkit"))) if f.endswith(".py")]:
        source = open(os.path.join(root, name)).read()
        areas |= set(re.findall(r'@rest_area\("([\w-]+)"\)', source)) | set(re.findall(r'rest_area\("([\w-]+)"\)\(', source))
        areas |= set(re.findall(r'REST_AREAS\["([\w-]+)"\]', source)) | set(re.findall(r'^REST_AREAS = \{([^}]*)\}', source, re.M) and
                                                                               re.findall(r'"([\w-]+)":', re.findall(r'^REST_AREAS = \{([^}]*)\}', source, re.M)[0]))
    areas.add("sessions")
    assert len(areas) >= 25, sorted(areas)
    missing = sorted(a for a in areas if "/api/%s" % a not in reference)
    assert not missing, "the API reference says nothing about /api/%s" % ", /api/".join(missing)
    # what it tells a person to read is really there
    for path in sorted(set(re.findall(r"`GET (/api/[a-z-]+)`", reference))):
        assert api(sim, "GET", path)[0] == 200, "the reference lists GET %s and it does not answer" % path
    assert sim.call("GET", "/healthz")[0] == 200



# ── The runner ───────────────────────────────────────────────────────────

CASES = [v for k, v in sorted(globals().items(), key=lambda kv: list(globals()).index(kv[0])) if k.startswith("case_")]


def main():
    stub = Stub()
    failed = 0
    with tempfile.TemporaryDirectory() as work:
        sim = Sim(stub, work).start()
        sim.stub = stub
        try:
            for case in CASES:
                try:
                    case(sim)
                    print("  ok    %s" % case.__name__[5:].replace("_", " "))
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    print("  FAIL  %s\n        %s: %s" % (case.__name__[5:].replace("_", " "), type(exc).__name__, str(exc)[:600]))
        finally:
            sim.stop()
    print("\n%d passed, %d failed" % (len(CASES) - failed, failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
