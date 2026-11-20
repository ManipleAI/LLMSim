#!/usr/bin/env python3
"""The relay to a real model, against a stand-in upstream that behaves like the one that exposed the gaps:
it refuses a request that declares tools unless `reasoning_effort` is set, and says why.

    python3 tests/test_relay.py

What is checked: the upstream's own words reach the caller (they were hidden behind "400 Bad Request");
extra request fields can be set (environment or API) and fix that refusal; an empty saved upstream does
not switch off one the environment sets; and the routing view says what really answers.
"""

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kit import Sim, free_port, run_cases  # noqa: E402

REFUSAL = ("Function tools with reasoning_effort are not supported for the-model in /v1/chat/completions. "
           "To use function tools, set reasoning_effort to 'none'.")
TOOLS = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}}}]
SEEN = []


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        SEEN.append(body)
        if body.get("tools") and "reasoning_effort" not in body:
            out, status = {"error": {"message": REFUSAL, "type": "invalid_request_error"}}, 400
        else:
            out, status = {"choices": [{"message": {"role": "assistant", "content": "from the stand-in"}}]}, 200
        raw = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def stand_in():
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % port


UP, UP_URL = stand_in()
ENV = {"LLMSIM_UPSTREAM": UP_URL, "LLMSIM_UPSTREAM_KEY": "k", "LLMSIM_UPSTREAM_MODEL": "the-model"}


def case_an_upstream_refusal_reaches_the_caller_in_its_own_words(sim):
    status, body = sim.chat("hello there", tools=TOOLS)
    text = json.dumps(body)
    assert status == 502, (status, text)
    assert "answered 400" in text and "Function tools with reasoning_effort" in text, text
    assert "could not reach" not in text, text


def case_the_same_refusal_is_reported_when_streaming(sim):
    status, body = sim.chat("hello there", tools=TOOLS, stream=True)
    text = body if isinstance(body, str) else json.dumps(body)
    assert status == 502 and "Function tools with reasoning_effort" in text, (status, text[:400])


def case_extra_fields_set_through_the_api_fix_the_refusal(sim):
    status, body = sim.api("POST", "/api/ui/config", {"upstream": {"params": {"reasoning_effort": "none"}}})
    assert status == 200 and body["config"]["upstream"]["params"] == {"reasoning_effort": "none"}, (status, body)
    SEEN.clear()
    status, body = sim.chat("hello there", tools=TOOLS)
    assert status == 200 and "from the stand-in" in json.dumps(body), (status, body)
    assert SEEN[-1]["reasoning_effort"] == "none" and SEEN[-1]["model"] == "the-model", SEEN[-1]


def case_extra_fields_never_override_what_llmsim_builds(sim):
    sim.api("POST", "/api/ui/config", {"upstream": {"params": {"reasoning_effort": "none", "model": "other", "messages": []}}})
    SEEN.clear()
    sim.chat("hello there", tools=TOOLS)
    assert SEEN[-1]["model"] == "the-model" and SEEN[-1]["messages"], SEEN[-1]


def case_extra_fields_that_are_not_an_object_are_refused(sim):
    for bad in ([1, 2], "{not json", "[1]", {"x": "y" * 3000}):
        status, body = sim.api("POST", "/api/ui/config", {"upstream": {"params": bad}})
        assert status == 400 and "JSON object" in json.dumps(body), (bad, status, body)


def case_extra_fields_can_come_from_the_environment():
    sim = Sim(env=dict(ENV, LLMSIM_UPSTREAM_PARAMS='{"reasoning_effort": "none"}')).start()
    try:
        SEEN.clear()
        status, body = sim.chat("hello there", tools=TOOLS)
        assert status == 200 and SEEN[-1]["reasoning_effort"] == "none", (status, body)
    finally:
        sim.stop()


def case_an_empty_saved_upstream_does_not_switch_off_the_environments():
    work = tempfile.mkdtemp()
    with open(os.path.join(work, "config.json"), "w") as fh:
        json.dump({"routing": {"default": "relay", "models": {}},
                   "upstream": {"url": "", "key": "", "model": "gpt-4o-mini", "format": "openai", "fallback": False}}, fh)
    sim = Sim(workdir=work, env=dict(ENV, LLMSIM_UPSTREAM_PARAMS='{"reasoning_effort": "none"}')).start()
    try:
        status, body = sim.chat("hello there")
        assert status == 200 and "from the stand-in" in json.dumps(body), (status, body)
        shown = sim.api("GET", "/api/ui/state")[1]["config"]
        assert shown["upstream"]["url"] == UP_URL and shown["upstream"]["keySet"], shown["upstream"]
    finally:
        sim.stop()


def case_the_routing_view_says_what_really_answers():
    sim = Sim().start()                      # no upstream at all
    try:
        status, body = sim.api("PUT", "/api/routing", {"default": "relay"})
        assert status == 200, body
        view = sim.api("GET", "/api/routing")[1]
        assert (view["default"], view["effective"]) == ("relay", "scripted"), view
        assert "no upstream" in view["warning"], view
        assert sim.chat("hello there")[0] == 200
    finally:
        sim.stop()


def make_sim():
    return Sim(env=ENV)


if __name__ == "__main__":
    # Cases that start their own LLMSim take no argument; the others share one with the stand-in as upstream.
    shared = [case_an_upstream_refusal_reaches_the_caller_in_its_own_words, case_the_same_refusal_is_reported_when_streaming,
              case_extra_fields_set_through_the_api_fix_the_refusal, case_extra_fields_never_override_what_llmsim_builds,
              case_extra_fields_that_are_not_an_object_are_refused]
    own = [case_extra_fields_can_come_from_the_environment, case_an_empty_saved_upstream_does_not_switch_off_the_environments,
           case_the_routing_view_says_what_really_answers]
    failed = 0
    sim = make_sim().start()
    try:
        for case in shared + own:
            try:
                case(sim) if case in shared else case()
                print("  ok    %s" % case.__name__[5:].replace("_", " "))
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print("  FAIL  %s\n        %s: %s" % (case.__name__[5:].replace("_", " "), type(exc).__name__, str(exc)[:700]))
    finally:
        sim.stop()
    print("\n%d passed, %d failed" % (len(shared) + len(own) - failed, failed))
    sys.exit(1 if failed else 0)
