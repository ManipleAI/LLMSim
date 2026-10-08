#!/usr/bin/env python3
"""Phase 1: a scenario file is a test. Drive an agent, judge the run.

    python3 tests/test_kit_drive.py
"""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kit import Sim, run_cases, CHAT  # noqa: E402
from fake_agent import FakeAgent  # noqa: E402

SCRIPT = [{"name": "order-flow", "when": {"contains": "order 1182"}, "turns": [
    {"text": "Looking it up.", "tool_calls": [{"name": "lookup_order", "input": {"id": 1182}}]},
    {"text": "Order 1182 shipped, but the refund is over the limit, so I have handed it to a manager."}]}]


def agent_cfg(fa, **extra):
    return dict({"name": "support", "driver": "http", "url": fa.url + "/chat",
                 "body": {"input": "{{message}}", "session": "{{conversation}}", "run": "{{run}}", "agent": "{{agent}}"},
                 "reply": "output.text"}, **extra)


def scenario(fa, **over):
    base = {"id": "refund", "description": "an over-limit refund is handed on", "agent": agent_cfg(fa),
            "model": {"rules": SCRIPT},
            "turns": [{"say": "I want a refund for order 1182", "expect": {"contains": "manager"}}],
            "checks": [{"type": "path", "mode": "strict", "expected": [{"tool": "lookup_order", "arguments": {"id": 1182}}]},
                       {"type": "budget", "steps": {"modelCalls": 3}, "tokens": {"total": 100000}}]}
    base.update(over)
    return base


def run(sim, sc, **options):
    status, body = sim.api("POST", "/api/scenarios/run", dict({"scenario": sc}, **options))
    assert status == 200, body
    return body["result"]


def case_a_scenario_file_is_a_whole_test(sim):
    agent = sim.agent
    result = run(sim, scenario(agent))
    assert result["verdict"] == "pass", result
    assert [c["status"] for c in result["checks"]] == ["pass", "pass", "pass"], result["checks"]
    assert result["turns"][0]["reply"].endswith("handed it to a manager.")
    # the run is on the timeline: the user, the model twice, the agent's reply, the verdict
    tl = sim.api("GET", "/api/runs/%s/timeline" % result["run"])[1]
    assert [e["kind"] for e in tl["events"]] == ["user_turn", "model_call", "model_call", "agent_reply", "check"], tl["counts"]
    assert tl["agents"] == ["support"]
    # the agent was told which run and agent it was, by the driver, so its model calls landed in the run
    assert agent.seen[-1]["body"]["run"] == result["run"]
    # and what it rules installed is gone again
    status, after = sim.chat("order 1182 please", path="/run/%s/agent/support%s" % (result["run"], CHAT))
    assert "Looking it up" not in json.dumps(after), "the run's model script outlived the run"


def case_each_check_fails_when_the_thing_it_checks_is_wrong(sim):
    agent = sim.agent
    wrong_answer = run(sim, scenario(agent, turns=[{"say": "I want a refund for order 1182", "expect": {"contains": "refunded in full"}}]))
    assert wrong_answer["verdict"] == "fail" and wrong_answer["checks"][0]["status"] == "fail", wrong_answer["checks"]
    assert "refunded in full" in wrong_answer["checks"][0]["detail"]
    def only(result, kind):
        return next(c for c in result["checks"] if c["type"] == kind)
    wrong_path = run(sim, scenario(agent, checks=[{"type": "path", "mode": "strict", "expected": [{"tool": "lookup_order", "arguments": {"id": 7}}]}]))
    assert only(wrong_path, "path")["status"] == "fail" and "call 1" in only(wrong_path, "path")["detail"], wrong_path["checks"]
    over_budget = run(sim, scenario(agent, checks=[{"type": "budget", "steps": {"modelCalls": 1}}]))
    assert only(over_budget, "budget")["status"] == "fail" and "modelCalls" in only(over_budget, "budget")["detail"]
    broken_rule = run(sim, scenario(agent, checks=[{"type": "run", "cond": {"field": "usage.total", "op": "lt", "value": 1}}]))
    assert only(broken_rule, "run")["status"] == "fail"


def case_the_four_path_modes_give_four_verdicts(sim):
    agent = sim.agent
    extra = {"name": "two-calls", "when": {"contains": "two orders"}, "turns": [
        {"text": "Checking both.", "tool_calls": [{"name": "lookup_order", "input": {"id": 1182}}, {"name": "lookup_order", "input": {"id": 7}}]},
        {"text": "Both are looked up."}]}
    sc = scenario(agent, model={"rules": [extra]}, turns=[{"say": "two orders please"}], checks=[])
    verdicts = {}
    one, other = {"tool": "lookup_order", "arguments": {"id": 1182}}, {"tool": "lookup_order", "arguments": {"id": 7}}
    cases = {"strict-ordered": ("strict", [one, other]), "strict-swapped": ("strict", [other, one]),
             "unordered-swapped": ("unordered", [other, one]), "subset-too-small": ("subset", [one]),
             "subset-enough": ("subset", [one, other, {"tool": "extra"}]), "superset-small": ("superset", [one]),
             "superset-missing": ("superset", [one, other, {"tool": "extra"}])}
    for name, (mode, expected) in cases.items():
        sc["checks"] = [{"type": "path", "mode": mode, "expected": expected}]
        verdicts[name] = run(sim, sc)["checks"][0]["status"]
    assert verdicts == {"strict-ordered": "pass", "strict-swapped": "fail", "unordered-swapped": "pass", "subset-too-small": "fail",
                        "subset-enough": "pass", "superset-small": "pass", "superset-missing": "fail"}, verdicts
    sc["checks"] = [{"type": "path", "mode": "strict", "args": "subset", "expected": [{"tool": "lookup_order"}, {"tool": "lookup_order"}]}]
    assert run(sim, sc)["checks"][0]["status"] == "pass", "arguments in subset mode should have matched"
    sc["checks"] = [{"type": "path", "mode": "strict", "argsOverrides": {"lookup_order": "ignore"},
                     "expected": [{"tool": "lookup_order", "arguments": {"id": 999}}, {"tool": "lookup_order", "arguments": {"id": 7}}]}]
    assert run(sim, sc)["checks"][0]["status"] == "pass"
    sc["checks"] = [{"type": "path", "mode": "strict", "expected": [{"tool": "lookup_order", "arguments": {"id": {"$type": "integer"}}},
                                                                    {"tool": "lookup_order", "arguments": {"id": {"$any": True}}}]}]
    assert run(sim, sc)["checks"][0]["status"] == "pass"


def case_answer_checks_cover_text_schema_and_numbers(sim):
    agent = sim.agent
    rules = [{"name": "json-answer", "when": {"contains": "as json"}, "turns": [
        {"text": "Here you go:\n```json\n{\"status\": \"shipped\", \"total\": 240.0, \"items\": [1, 2]}\n```\nTotal 240.50 dollars."}]}]
    sc = scenario(agent, model={"rules": rules}, turns=[{"say": "give me order 1182 as json"}], checks=[])
    good = [{"type": "answer", "schema": {"type": "object", "required": ["status", "total"], "properties": {"status": {"enum": ["shipped"]}, "items": {"type": "array", "minItems": 2}}}},
            {"type": "answer", "number": {"regex": "Total ([\\d.]+)", "equals": 240.5, "tolerance": 0.01}},
            {"type": "answer", "regex": "shipped", "not_contains": "refunded"}]
    bad = [{"type": "answer", "schema": {"type": "object", "required": ["missing"]}},
           {"type": "answer", "schema": {"type": "object", "properties": {"status": {"enum": ["lost"]}}}},
           {"type": "answer", "number": {"regex": "Total ([\\d.]+)", "equals": 100, "tolerance": 1}},
           {"type": "answer", "number": {"regex": "Total ([\\d.]+)", "max": 200}}]
    sc["checks"] = good + bad
    got = [c["status"] for c in run(sim, sc)["checks"]]
    assert got == ["pass"] * 3 + ["fail"] * 4, got
    sc["checks"] = [{"type": "answer", "target": 5, "contains": "x"}]
    assert run(sim, sc)["checks"][0]["status"] == "fail"


def case_no_data_is_not_a_pass(sim):
    agent = sim.agent
    # an agent that answers without ever asking LLMSim's model: nothing to judge on the path
    sc = scenario(agent, agent=dict(agent_cfg(agent), url=agent.url + "/echo"), model={"rules": []},
                  turns=[{"say": "hello"}], checks=[{"type": "path", "mode": "strict", "expected": []}])
    out = run(sim, sc)
    assert out["checks"][0]["status"] == "no-data" and "pointed at LLMSim" in out["checks"][0]["detail"], out["checks"]
    assert out["verdict"] == "no-data", out
    sc["checks"] = [{"type": "path", "mode": "subset", "expected": [{"tool": "x"}]}, {"type": "answer", "contains": "echo"}]
    out = run(sim, sc)
    assert [c["status"] for c in out["checks"]] == ["no-data", "pass"] and out["verdict"] == "no-data", out
    # and a run nobody drove has no answer to judge
    assert sim.api("POST", "/api/checks/evaluate", {"run": "never-ran", "checks": [{"type": "answer", "contains": "x"}]})[1]["verdict"] == "no-data"


def case_a_scenario_that_cannot_reach_its_agent_fails_and_says_why(sim):
    sc = scenario(sim.agent, agent=dict(agent_cfg(sim.agent), url=sim.agent.url + "/boom"))
    out = run(sim, sc)
    assert out["verdict"] == "fail" and "HTTP 500" in out["error"], out
    sc = scenario(sim.agent, agent=dict(agent_cfg(sim.agent), url="http://127.0.0.1:9/nope"))
    out = run(sim, sc)
    assert out["verdict"] == "fail" and "could not reach" in out["error"], out
    sc = scenario(sim.agent, agent=dict(agent_cfg(sim.agent), reply="no.such.path"))
    assert "no.such.path" in run(sim, sc)["error"]


def case_scenarios_are_validated_when_saved(sim):
    good = scenario(sim.agent)
    assert sim.api("POST", "/api/scenarios", good)[0] == 200
    assert [s["id"] for s in sim.api("GET", "/api/scenarios")[1]["scenarios"]] == ["refund"]
    for label, mutate in {"no checks": lambda s: s.update(checks=[], turns=[{"say": "hi"}]),
                          "unknown driver": lambda s: s["agent"].update(driver="carrier-pigeon"),
                          "bad path mode": lambda s: s["checks"][0].update(mode="loose"),
                          "bad regex": lambda s: s["checks"].append({"type": "answer", "regex": "(unclosed"}),
                          "bad rule": lambda s: s["model"]["rules"].append({"name": "x"}),
                          "unknown field": lambda s: s.update(frobnicate=1),
                          "empty turns": lambda s: s.update(turns=[]),
                          "bad stop": lambda s: s.update(stop={"maxTurns": 0}),
                          "cassette name": lambda s: s["model"].update(cassette="../x"),
                          "budget with nothing": lambda s: s["checks"].append({"type": "budget"})}.items():
        s = json.loads(json.dumps(good))
        mutate(s)
        status, body = sim.api("POST", "/api/scenarios/validate", {"scenario": s})
        assert status == 200 and body["valid"] is False and body["problems"], label
    assert sim.api("DELETE", "/api/scenarios/refund")[0] == 200
    assert sim.api("GET", "/api/scenarios/refund")[0] == 404


def case_a_saved_scenario_runs_by_name_and_in_the_background(sim):
    sim.api("PUT", "/api/scenarios/refund", scenario(sim.agent))
    status, body = sim.api("POST", "/api/scenarios/refund/run", {"wait": False, "run": "bg-1"})
    assert status == 202 and body["run"] == "bg-1", body
    import time
    for _ in range(100):
        got = sim.api("GET", "/api/results/bg-1")[1]
        if got["result"]["status"] == "done":
            break
        time.sleep(0.1)
    assert got["result"]["verdict"] == "pass", got
    listed = sim.api("GET", "/api/results?scenario=refund")[1]["results"]
    assert listed and listed[0]["run"] == "bg-1" and listed[0]["verdict"] == "pass"
    assert sim.api("POST", "/api/scenarios/nope/run", {})[0] == 404
    assert sim.api("POST", "/api/scenarios/refund/run", {"tier": "weird"})[0] == 400


def case_the_report_is_junit_for_a_ci_system(sim):
    out = run(sim, scenario(sim.agent, checks=[{"type": "path", "mode": "strict", "expected": [{"tool": "nothing"}]}]))
    status, headers, text = sim.call("GET", "/api/results/%s/junit" % out["run"])
    assert status == 200 and "xml" in headers["Content-Type"], headers
    assert text.startswith("<?xml") and 'failures="1"' in text and "<failure" in text and 'name="turn 1"' in text, text


def case_a_credential_is_used_and_is_in_no_evidence(sim):
    secret = "tok-SECRET-9f8e7d6c5b4a"
    assert sim.api("PUT", "/api/credentials/agent-token", {"type": "bearer", "token": secret})[0] == 200
    listed = sim.api("GET", "/api/credentials")[1]
    assert "SECRET" not in json.dumps(listed) and listed["credentials"][0]["label"] == "agent-token", listed
    assert oct(os.stat(sim.env["LLMSIM_CREDENTIALS"]).st_mode & 0o777) == "0o600"
    sim.agent.echo = True          # an agent that repeats the header it was given into its reply
    out = run(sim, scenario(sim.agent, agent=agent_cfg(sim.agent, credential="agent-token"), turns=[{"say": "I want a refund for order 1182"}]))
    sim.agent.echo = False
    assert out["verdict"] == "pass"
    assert sim.agent.seen[-1]["headers"]["Authorization"] == "Bearer " + secret, "the driver did not present the credential"
    assert "[credential]" in out["turns"][0]["reply"] and secret not in json.dumps(out), out["turns"]
    # so a driver that echoed its header into the reply still leaves nothing behind
    db = sqlite3.connect(sim.env["LLMSIM_RUNS_DB"])
    dump = "\n".join(str(r) for t in ("events", "docs", "runs") for r in db.execute("select * from %s" % t))
    assert secret not in dump, "the credential is in what the kit stored"
    assert secret not in json.dumps(sim.api("GET", "/api/runs/%s/timeline" % out["run"])[1])
    assert sim.api("PUT", "/api/credentials/bad", {"type": "bearer"})[0] == 400
    assert sim.api("DELETE", "/api/credentials/agent-token")[0] == 200
    out = run(sim, scenario(sim.agent, agent=agent_cfg(sim.agent, credential="agent-token")))
    assert out["verdict"] == "fail" and "not stored" in out["error"], out


def case_a_recording_can_stand_in_for_the_models_script(sim):
    sc = scenario(sim.agent)
    first = run(sim, dict(sc, model={"rules": SCRIPT, "cassette": "refund-run", "mode": "record"}), run="rec-1")
    assert first["verdict"] == "pass" and first["model"]["entries"] == 2, first["model"]
    replay = run(sim, dict(sc, model={"cassette": "refund-run", "mode": "replay"}), run="rep-1")
    assert replay["verdict"] == "pass" and replay["model"]["hits"] == 2 and replay["model"]["misses"] == 0, replay
    # a run whose agent asks something the recording lacks fails, and says so
    changed = run(sim, dict(sc, model={"cassette": "refund-run", "mode": "replay"}, turns=[{"say": "a refund for order 7"}],
                            checks=[{"type": "run", "cond": {"field": "turns", "op": "gte", "value": 1}}]))
    assert changed["verdict"] == "fail" and any(c["type"] == "model" for c in changed["checks"]), changed
    # the real tier does not apply the model block at all
    real = run(sim, sc, tier="real")
    assert real["model"] == {"tier": "real"}, real["model"]


def case_the_process_driver_is_off_unless_the_operator_turns_it_on(sim):
    status, body = sim.api("POST", "/api/drivers/validate", {"driver": "process", "command": ["echo", "hi"]})
    assert status == 200 and body["valid"] is False and "LLMSIM_ALLOW_PROCESS" in " ".join(body["problems"]), body
    assert sim.api("GET", "/api/drivers")[1]["processAllowed"] is False
    sc = scenario(sim.agent, agent={"name": "p", "driver": "process", "command": ["echo", "hi"]})
    assert sim.api("POST", "/api/scenarios/validate", {"scenario": sc})[1]["valid"] is False


def case_an_openai_compatible_endpoint_is_driven_with_history(sim):
    # LLMSim is itself an OpenAI-compatible endpoint, which makes it a fine agent to drive.
    rules = [{"name": "chat", "when": {"field": "userText", "op": "contains", "value": "hello"}, "turns": [{"text": "Hi, I am here."}, {"text": "Your name is on record."}]}]
    sc = {"id": "chat", "agent": {"name": "bot", "driver": "openai", "url": sim.url("/run/oc-1/agent/bot/openai/v1"), "model": "gpt-5",
                                  "credential": "k"},
          "model": {"rules": rules}, "turns": [{"say": "hello", "expect": {"contains": "here"}}, {"say": "who am I?", "expect": {"contains": "record"}}]}
    sim.api("PUT", "/api/credentials/k", {"type": "bearer", "token": "sk-llmsim-000000000000000000000000"})
    out = run(sim, sc, run="oc-1")
    assert out["verdict"] == "pass", out
    assert sim.api("GET", "/api/runs/oc-1/timeline")[1]["counts"]["model_call"] == 2
    sim.api("DELETE", "/api/credentials/k")


def case_the_command_line_runs_a_scenario_and_exits_with_its_verdict(sim):
    import subprocess
    import tempfile
    cli = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "llmsim")
    with tempfile.TemporaryDirectory() as tmp:
        good, bad = os.path.join(tmp, "good.json"), os.path.join(tmp, "bad.json")
        json.dump(scenario(sim.agent), open(good, "w"))
        json.dump(scenario(sim.agent, turns=[{"say": "I want a refund for order 1182", "expect": {"contains": "refunded in full"}}]), open(bad, "w"))
        junit = os.path.join(tmp, "out.xml")
        ok = subprocess.run([sys.executable, cli, "--server", sim.base, "run", good, "--junit", junit], capture_output=True, text=True)
        assert ok.returncode == 0 and ok.stdout.startswith("PASS"), (ok.returncode, ok.stdout, ok.stderr)
        assert open(junit).read().startswith("<?xml")
        no = subprocess.run([sys.executable, cli, "--server", sim.base, "run", bad, "--junit", junit], capture_output=True, text=True)
        assert no.returncode == 1 and "FAIL" in no.stdout and 'failures="1"' in open(junit).read(), (no.returncode, no.stdout)
        gone = subprocess.run([sys.executable, cli, "--server", "http://127.0.0.1:9", "run", good], capture_output=True, text=True)
        assert gone.returncode == 2 and "cannot reach" in gone.stderr
        v = subprocess.run([sys.executable, cli, "--server", sim.base, "validate", good], capture_output=True, text=True)
        assert v.returncode == 0 and "valid" in v.stdout


def case_an_a2a_agent_is_driven_through_messages_late_tasks_and_questions(sim):
    from fake_platforms import FakeA2A
    peer = FakeA2A()
    try:
        sim.api("PUT", "/api/credentials/a2a", {"type": "bearer", "token": peer.token})
        base = {"id": "a2a-flow", "agent": {"name": "remote", "driver": "a2a", "url": peer.url, "credential": "a2a", "pollMs": 50}}
        # three turns: a plain answer, a task that finishes only after it has been polled, and a question answered by the next turn
        sc = dict(base, turns=[{"say": "hello there", "expect": {"contains": "Hello from"}},
                               {"say": "build the slow report", "expect": {"contains": "slow report is ready"}},
                               {"say": "which order do I have", "expect": {"contains": "Which order number"}},
                               {"say": "order 1182", "expect": {"contains": "order 1182 found"}}], checks=[])
        out = run(sim, sc)
        assert out["verdict"] == "pass", out
        assert out["turns"][2]["state"]["task"] == "input-required" and out["turns"][3]["state"]["task"] == "completed"
        methods = [c["method"] for c in peer.log]
        assert methods.count("tasks/get") >= 2, methods
        # the answer to the question went to the same task and the same conversation
        asked = [c for c in peer.log if c["method"] == "message/send"]
        assert asked[-1]["params"]["message"]["taskId"] == out["turns"][2]["state"]["taskId"]
        assert asked[-1]["params"]["message"]["contextId"], "the conversation was not carried"
        # pushed rather than polled
        peer.log.clear()
        pushed = run(sim, dict(base, agent=dict(base["agent"], wait="push"), turns=[{"say": "build the slow report", "expect": {"contains": "slow report is ready"}}], checks=[]))
        assert pushed["verdict"] == "pass" and not [c for c in peer.log if c["method"] == "tasks/get"], (pushed, [c["method"] for c in peer.log])
        # streamed
        streamed = run(sim, dict(base, agent=dict(base["agent"], wait="stream"), turns=[{"say": "stream it", "expect": {"contains": "Streamed answer"}}], checks=[]))
        assert streamed["verdict"] == "pass", streamed
        # a wrong credential is the agent saying no, reported as that
        sim.api("PUT", "/api/credentials/a2a", {"type": "bearer", "token": "wrong-token-value"})
        refused = run(sim, sc)
        assert refused["verdict"] == "fail" and "401" in refused["error"], refused
    finally:
        peer.stop()
        sim.api("DELETE", "/api/credentials/a2a")


def case_a_salesforce_agent_is_driven_with_a_session_and_the_session_is_ended(sim):
    from fake_platforms import FakeSalesforce
    sf = FakeSalesforce()
    try:
        sim.api("PUT", "/api/credentials/sf", {"type": "oauth_client_credentials", "tokenUrl": sf.url + "/services/oauth2/token",
                                                "clientId": sf.client_id, "clientSecret": sf.client_secret})
        cfg = {"name": "sf-agent", "driver": "agentforce", "domain": sf.url, "agentId": sf.GOOD, "apiBase": sf.url, "credential": "sf"}
        sc = {"id": "sf", "agent": cfg, "turns": [{"say": "where is my order", "expect": {"contains": "You said: where is my order"}},
                                                   {"say": "thanks", "expect": {"contains": "You said: thanks"}}]}
        out = run(sim, sc)
        assert out["verdict"] == "pass", out
        sends = [b for m, p, b in sf.log if p.endswith("/messages")]
        assert [s["message"]["sequenceId"] for s in sends] == [1, 2], "the sequence did not advance"
        assert sf.ended and sf.ended[0][1] == "UserRequest" and not sf.sessions, (sf.ended, sf.sessions)
        streamed = run(sim, dict(sc, agent=dict(cfg, stream=True), turns=[{"say": "stream", "expect": {"equals": "Streamed reply."}}]))
        assert streamed["verdict"] == "pass", streamed
        # an agent type the API does not support is said to be that, and nothing is left open
        gone = run(sim, dict(sc, agent=dict(cfg, agentId=sf.UNSUPPORTED)))
        assert gone["verdict"] == "fail" and "Agent Script" in gone["error"] and "not supported" in gone["error"], gone["error"]
        # the secret is in no evidence
        assert sf.client_secret not in json.dumps(out) and "sf-access-token-abc" not in json.dumps(out)
        db = sqlite3.connect(sim.env["LLMSIM_RUNS_DB"])
        dump = "\n".join(str(r) for t in ("events", "docs") for r in db.execute("select * from %s" % t))
        assert sf.client_secret not in dump and "sf-access-token-abc" not in dump
    finally:
        sf.stop()
        sim.api("DELETE", "/api/credentials/sf")


def make_sim():
    sim = Sim()
    return sim


def main():
    sim = Sim().start()
    sim.agent = FakeAgent(sim.base)
    failed = 0
    cases = [v for k, v in sorted(globals().items(), key=lambda kv: list(globals()).index(kv[0])) if k.startswith("case_")]
    try:
        for case in cases:
            try:
                case(sim)
                print("  ok    %s" % case.__name__[5:].replace("_", " "))
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print("  FAIL  %s\n        %s: %s" % (case.__name__[5:].replace("_", " "), type(exc).__name__, str(exc)[:900]))
    finally:
        sim.agent.stop()
        sim.stop()
    print("\n%d passed, %d failed" % (len(cases) - failed, failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
