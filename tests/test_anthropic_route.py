#!/usr/bin/env python3
"""The Anthropic route, end to end, against a stand-in for Anthropic's API.

    python3 tests/test_anthropic_route.py

No network, no account, no key. A stub plays /v1/messages and /v1/models and is
strict where the real API is: it refuses a request with no credential, with the
wrong one, with no anthropic-version, with no max_tokens, that is not streamed,
whose conversation does not start with the user, or whose tool result names a
call that was never made. LLMSim is started as a subprocess pointed at it with
its own throwaway directory, so nothing here touches a real setup.

What this proves is that LLMSim sends Anthropic a request it would accept, in
all three client formats, and reads the stream that comes back. It cannot prove
Anthropic still answers that way today; only a real key does.
"""

import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kit import Sim, free_port, run_cases, OPENAI_KEY, ANTHROPIC_KEY, CHAT  # noqa: E402

REAL_KEY = "sk-ant-api03-STANDIN-KEY-0000"
GATEWAY_TOKEN = "gw-token-STANDIN-0000"


class Stub:
    """Anthropic's Messages API, as far as a client can tell."""

    def __init__(self):
        self.port = free_port()
        self.seen, self.plan = [], {"mode": "text"}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _refuse(self, status, kind, message):
                raw = json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _credential(self):
                want = stub.plan.get("auth", "api_key")
                if want == "bearer":
                    return self.headers.get("authorization") == "Bearer " + GATEWAY_TOKEN and not self.headers.get("x-api-key")
                return self.headers.get("x-api-key") == REAL_KEY and not self.headers.get("authorization")

            def do_GET(self):
                if not self._credential():
                    return self._refuse(401, "authentication_error", "invalid x-api-key")
                if self.path.startswith("/v1/models"):
                    raw = json.dumps({"data": [{"id": "claude-sonnet-5-5", "type": "model"}, {"id": "claude-haiku-5-5", "type": "model"}]}).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(raw)))
                    self.end_headers()
                    return self.wfile.write(raw)
                self._refuse(404, "not_found_error", "no such route")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                stub.seen.append({"path": self.path, "body": body, "headers": {k.lower(): v for k, v in self.headers.items()}})
                if self.path != "/v1/messages":
                    return self._refuse(404, "not_found_error", "no such route")
                if not self._credential():
                    return self._refuse(401, "authentication_error", "invalid x-api-key")
                if self.headers.get("anthropic-version") != "2023-06-01":
                    return self._refuse(400, "invalid_request_error", "anthropic-version: header is required")
                problem = stub.problem_with(body)
                if problem:
                    return self._refuse(400, "invalid_request_error", problem)
                mode = stub.plan["mode"]
                if mode == "rate_limited":
                    return self._refuse(429, "rate_limit_error", "Number of request tokens has exceeded your rate limit")
                if mode == "overloaded":
                    return self._refuse(529, "overloaded_error", "Overloaded")
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()

                def send(event, data):
                    self.wfile.write(("event: %s\ndata: %s\n\n" % (event, json.dumps(data))).encode())
                    self.wfile.flush()

                send("message_start", {"type": "message_start", "message": {
                    "id": "msg_stub", "type": "message", "role": "assistant", "model": body["model"], "content": [],
                    "usage": {"input_tokens": 40, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10, "output_tokens": 1}}})
                send("ping", {"type": "ping"})
                if mode == "broken":
                    send("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
                    send("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Half an "}})
                    return send("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
                index = 0
                if mode in ("text", "tool"):
                    send("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
                    for piece in stub.plan.get("text", ["Paris is the capital", " of France."]):
                        send("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": piece}})
                    send("content_block_stop", {"type": "content_block_stop", "index": index})
                    index += 1
                if mode == "tool":
                    send("content_block_start", {"type": "content_block_start", "index": index, "content_block": {
                        "type": "tool_use", "id": "toolu_stub01", "name": "get_weather", "input": {}}})
                    for piece in ('{"city": ', '"Paris"}'):
                        send("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": piece}})
                    send("content_block_stop", {"type": "content_block_stop", "index": index})
                send("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use" if mode == "tool" else stub.plan.get("stop", "end_turn"), "stop_sequence": None},
                                       "usage": {"output_tokens": 25}})
                send("message_stop", {"type": "message_stop"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.port

    @staticmethod
    def problem_with(body):
        """What the real API would refuse this request for, or None."""
        if not isinstance(body.get("max_tokens"), int) or body["max_tokens"] < 1:
            return "max_tokens: Field required"
        if body.get("stream") is not True:
            return "this stand-in only streams, as LLMSim is meant to ask"
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return "messages: at least one message is required"
        if messages[0].get("role") != "user":
            return "messages: first message must use the user role"
        made = set()
        for i, msg in enumerate(messages):
            if msg.get("role") not in ("user", "assistant"):
                return "messages.%d.role: must be user or assistant" % i
            if i and messages[i - 1].get("role") == msg.get("role"):
                return "messages.%d: roles must alternate" % i
            for block in msg.get("content") if isinstance(msg.get("content"), list) else []:
                if block.get("type") == "tool_use":
                    if not isinstance(block.get("input"), dict):
                        return "tool_use.input: must be an object"
                    made.add(block.get("id"))
                if block.get("type") == "tool_result" and block.get("tool_use_id") not in made:
                    return "tool_result.tool_use_id %r has no tool_use before it" % block.get("tool_use_id")
        if "system" in body and not isinstance(body["system"], (str, list)):
            return "system: must be a string or a list"
        for tool in body.get("tools") or []:
            if not tool.get("name") or not isinstance(tool.get("input_schema"), dict):
                return "tools: each needs name and input_schema"
        if "temperature" in body and "top_p" in body:
            return "temperature and top_p cannot both be specified"
        return None

    def last(self):
        return self.seen[-1]


STUB = Stub()
TOOL = {"type": "function", "function": {"name": "get_weather", "description": "Weather for a city",
                                         "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}


def configure(sim, **settings):
    status, body = sim.api("POST", "/api/ui/config", settings)
    assert status == 200, body
    return body


def setup(sim, mode="text", **plan):
    STUB.plan = dict({"mode": mode}, **plan)
    STUB.seen.clear()


def use_anthropic(sim, **anthropic):
    configure(sim, routing={"default": "anthropic"},
              anthropic=dict({"url": STUB.url, "key": REAL_KEY, "auth": "api_key", "model": "claude-sonnet-5-5"}, **anthropic))


def sse(text):
    return [json.loads(line[5:]) for line in text.split("\n") if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]")]


# ── cases ─────────────────────────────────────────────────────────────────

def case_with_no_key_it_says_so_and_names_where_to_set_one(sim):
    setup(sim)
    configure(sim, routing={"default": "anthropic"}, anthropic={"url": STUB.url, "forgetKey": True})
    status, body = sim.chat("hello")
    assert status == 502 and "no key is set" in json.dumps(body), (status, body)
    assert not STUB.seen, "a request with no key reached Anthropic"


def case_an_openai_client_is_answered_by_claude_in_its_own_format(sim):
    setup(sim)
    use_anthropic(sim)
    status, body = sim.chat("What is the capital of France?", messages=[
        {"role": "system", "content": "Be brief."}, {"role": "user", "content": "What is the capital of France?"}],
        max_tokens=300, temperature=0.2, top_p=0.9)
    assert status == 200, body
    assert body["choices"][0]["message"]["content"] == "Paris is the capital of France.", body
    assert body["choices"][0]["finish_reason"] == "stop"
    sent = STUB.last()
    assert sent["headers"]["x-api-key"] == REAL_KEY and "authorization" not in sent["headers"], sent["headers"]
    assert sent["body"]["model"] == "claude-sonnet-5-5" and sent["body"]["system"] == "Be brief.", sent["body"]
    assert sent["body"]["max_tokens"] == 300 and sent["body"]["stream"] is True
    assert sent["body"]["messages"] == [{"role": "user", "content": [{"type": "text", "text": "What is the capital of France?"}]}]
    # one of the two sampling controls, never the pair the API refuses
    assert sent["body"].get("temperature") == 0.2 and "top_p" not in sent["body"]
    # what was billed is what is reported: 40 fresh + 100 read from cache + 10 written to it
    usage = body["usage"]
    assert usage["prompt_tokens"] == 150 and usage["completion_tokens"] == 25, usage
    assert usage["prompt_tokens_details"]["cached_tokens"] == 100, usage


def case_a_stream_is_passed_on_piece_by_piece(sim):
    setup(sim)
    use_anthropic(sim)
    status, _, text = sim.call("POST", CHAT, {"model": "gpt-5", "stream": True, "stream_options": {"include_usage": True},
                                              "messages": [{"role": "user", "content": "capital of France?"}]},
                               {"Authorization": "Bearer " + OPENAI_KEY})
    assert status == 200, text
    chunks = sse(text)
    pieces = [c["choices"][0]["delta"].get("content") for c in chunks if c.get("choices") and c["choices"][0]["delta"].get("content")]
    assert pieces == ["Paris is the capital", " of France."], pieces
    assert chunks[-1]["usage"]["prompt_tokens"] == 150, chunks[-1]


def case_a_tool_call_goes_out_and_its_result_comes_back(sim):
    setup(sim, "tool", text=["Let me check."])
    use_anthropic(sim)
    status, body = sim.chat("weather in Paris?", tools=[TOOL], tool_choice="required", parallel_tool_calls=False)
    assert status == 200, body
    msg = body["choices"][0]["message"]
    call = msg["tool_calls"][0]
    assert call["function"]["name"] == "get_weather" and json.loads(call["function"]["arguments"]) == {"city": "Paris"}, msg
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    sent = STUB.last()["body"]
    assert sent["tools"] == [{"name": "get_weather", "description": "Weather for a city",
                              "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}], sent["tools"]
    assert sent["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}, sent.get("tool_choice")

    # the agent runs the tool and reports back, the way an agent loop does
    setup(sim, "text", text=["It is sunny in Paris."])
    status, body = sim.chat("weather in Paris?", messages=[
        {"role": "user", "content": "weather in Paris?"},
        {"role": "assistant", "content": "Let me check.", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call["id"], "content": "sunny, 21C"}], tools=[TOOL])
    assert status == 200 and body["choices"][0]["message"]["content"] == "It is sunny in Paris.", body
    turns = STUB.last()["body"]["messages"]
    assert [m["role"] for m in turns] == ["user", "assistant", "user"], turns
    assert turns[1]["content"][1] == {"type": "tool_use", "id": call["id"], "name": "get_weather", "input": {"city": "Paris"}}, turns[1]
    assert turns[2]["content"] == [{"type": "tool_result", "tool_use_id": call["id"], "content": "sunny, 21C"}], turns[2]


def case_an_anthropic_client_gets_anthropic_back_whole_and_streamed(sim):
    setup(sim, "tool", text=["Checking."])
    use_anthropic(sim, model="")
    request = {"model": "claude-haiku-5-5", "max_tokens": 64, "system": "Be brief.",
               "tools": [{"name": "get_weather", "description": "Weather", "input_schema": {"type": "object", "properties": {}}}],
               "messages": [{"role": "user", "content": "weather?"}]}
    status, body = sim.api("POST", "/anthropic/v1/messages", request, {"x-api-key": ANTHROPIC_KEY})
    assert status == 200, body
    assert [b["type"] for b in body["content"]] == ["text", "tool_use"] and body["stop_reason"] == "tool_use", body
    assert body["content"][1]["input"] == {"city": "Paris"}
    # no model was chosen, so the Claude model the client asked for is the one used
    assert STUB.last()["body"]["model"] == "claude-haiku-5-5" and STUB.last()["body"]["max_tokens"] == 64
    # LLMSim's own published key never travels upstream
    assert ANTHROPIC_KEY not in json.dumps(STUB.last()["headers"])
    assert body["usage"]["input_tokens"] == 50 and body["usage"]["cache_read_input_tokens"] == 100, body["usage"]

    status, _, text = sim.call("POST", "/anthropic/v1/messages", dict(request, stream=True), {"x-api-key": ANTHROPIC_KEY})
    kinds = [e["type"] for e in sse(text)]
    assert kinds[0] == "message_start" and kinds[-1] == "message_stop" and "content_block_delta" in kinds, kinds
    stops = [e["delta"]["stop_reason"] for e in sse(text) if e["type"] == "message_delta"]
    assert stops == ["tool_use"], stops


def case_a_model_that_is_not_claudes_needs_one_chosen(sim):
    setup(sim)
    use_anthropic(sim, model="")
    status, body = sim.chat("hello", model="gpt-5")
    assert status == 502 and "No Anthropic model is chosen" in json.dumps(body), (status, body)
    assert not STUB.seen, "a request with no model to ask for reached Anthropic"


def case_a_gateways_token_is_sent_as_a_bearer_and_never_as_a_key(sim):
    setup(sim, auth="bearer")
    use_anthropic(sim, key=GATEWAY_TOKEN, auth="bearer")
    status, body = sim.chat("hello")
    assert status == 200, body
    headers = STUB.last()["headers"]
    assert headers["authorization"] == "Bearer " + GATEWAY_TOKEN and "x-api-key" not in headers, headers
    # nothing is added that would make it pass for another client
    assert "anthropic-beta" not in headers and "claude" not in headers.get("user-agent", "").lower(), headers
    # there is no sign-in to choose: a key, or a gateway's token
    status, body = sim.api("POST", "/api/ui/config", {"anthropic": {"auth": "oauth"}})
    assert status == 400 and "api_key" in json.dumps(body), (status, body)


def case_the_vendors_failures_arrive_as_the_clients_own(sim):
    use_anthropic(sim)
    setup(sim, "rate_limited")
    status, body = sim.chat("hello")
    assert status == 429 and "rate limit" in json.dumps(body), (status, body)
    setup(sim, "overloaded")
    status, body = sim.chat("hello")
    assert status == 529, (status, body)
    # a key Anthropic refuses is LLMSim's problem, not the caller's: never a 401, which would blame their key
    setup(sim)
    use_anthropic(sim, key="sk-ant-wrong")
    status, body = sim.chat("hello")
    assert status == 502 and "refused the key" in json.dumps(body), (status, body)
    # a failure after the first word is said inside the stream, in the client's form
    setup(sim, "broken")
    use_anthropic(sim)
    status, _, text = sim.call("POST", CHAT, {"model": "gpt-5", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
                               {"Authorization": "Bearer " + OPENAI_KEY})
    assert status == 200 and "Half an " in text and "overloaded" in text.lower(), text[-400:]


def case_the_key_is_kept_and_never_given_back(sim):
    setup(sim)
    use_anthropic(sim)
    state = sim.api("GET", "/api/ui/state")[1]
    assert state["config"]["anthropic"]["keySet"] is True and state["config"]["anthropic"]["key"] == "", state["config"]["anthropic"]
    assert REAL_KEY not in json.dumps(state) and REAL_KEY not in json.dumps(sim.api("GET", "/healthz")[1])
    # changing the model without retyping the key keeps it
    configure(sim, anthropic={"model": "claude-haiku-5-5", "key": ""})
    status, body = sim.chat("hello")
    assert status == 200 and STUB.last()["body"]["model"] == "claude-haiku-5-5", (status, body)
    # the file that holds it is the owner's alone
    path = sim.env["LLMSIM_CONFIG"]
    assert REAL_KEY in open(path).read()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, oct(stat.S_IMODE(os.stat(path).st_mode))
    # and it is in no captured request
    assert REAL_KEY not in json.dumps(sim.api("GET", "/api/captures")[1])


def case_the_models_a_key_can_use_are_listed(sim):
    setup(sim)
    use_anthropic(sim)
    status, body = sim.api("GET", "/api/ui/anthropic/models")
    assert status == 200 and body["models"] == ["claude-sonnet-5-5", "claude-haiku-5-5"], (status, body)


def case_one_model_can_go_to_claude_while_the_rest_stay_scripted(sim):
    setup(sim)
    configure(sim, routing={"default": "scripted", "models": {"gpt-5.4": "anthropic"}},
              anthropic={"url": STUB.url, "key": REAL_KEY, "auth": "api_key", "model": "claude-sonnet-5-5"})
    status, body = sim.chat("hello", model="gpt-5")
    assert status == 200 and not STUB.seen, "a scripted model reached Anthropic"
    status, body = sim.chat("hello", model="gpt-5.4")
    assert status == 200 and len(STUB.seen) == 1 and body["choices"][0]["message"]["content"].startswith("Paris"), body
    row = sim.api("GET", "/api/captures?limit=1")[1]["captures"][0]
    assert row["route"] == "anthropic" and row["rule"] == "anthropic:claude-sonnet-5-5", row
    status, body = sim.api("POST", "/api/ui/config", {"routing": {"default": "claude"}})
    assert status == 400 and "anthropic" in json.dumps(body), (status, body)


def case_a_run_through_claude_can_be_recorded_and_replayed_without_it(sim):
    setup(sim)
    use_anthropic(sim)
    assert sim.api("POST", "/api/sessions/record", {"name": "claude-once"})[0] == 200
    status, first = sim.chat("capital of France?")
    assert status == 200 and sim.api("POST", "/api/sessions/stop", {})[0] == 200
    asked = len(STUB.seen)
    assert sim.api("POST", "/api/sessions/replay", {"name": "claude-once"})[0] == 200
    status, again = sim.chat("capital of France?")
    sim.api("POST", "/api/sessions/stop", {})
    assert status == 200 and again["choices"][0]["message"]["content"] == first["choices"][0]["message"]["content"], again
    assert len(STUB.seen) == asked, "the replay asked Anthropic again"


def case_falling_back_to_the_script_is_something_you_turn_on(sim):
    setup(sim, "overloaded")
    use_anthropic(sim)
    assert sim.chat("hello")[0] == 529
    use_anthropic(sim, fallback=True)
    status, body = sim.chat("hello")
    assert status == 200 and body["choices"][0]["message"]["content"], body
    use_anthropic(sim, fallback=False)


def make_sim():
    return Sim(env={"LLMSIM_ANTHROPIC_UPSTREAM": STUB.url})


if __name__ == "__main__":
    run_cases([v for k, v in list(globals().items()) if k.startswith("case_")], make_sim)
