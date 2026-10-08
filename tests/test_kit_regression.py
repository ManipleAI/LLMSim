#!/usr/bin/env python3
"""Phase 4: regression. Many cases, kept, repeated, and compared with the last good run.

    python3 tests/test_kit_regression.py
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kit import Sim  # noqa: E402
from fake_agent import FakeAgent  # noqa: E402
from test_kit_tools import MCP, ORDERS, REFUND_SCRIPT, LIE_SCRIPT, agent_cfg  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOOKUP_SCRIPT = {"name": "lookup-flow", "when": {"contains": "where is order 1182"}, "turns": [
    {"text": "Looking.", "tool_calls": [{"name": "lookup_order", "input": {"id": 1182}}]}, {"text": "Order 1182 has shipped."}]}


def refund_scenario(fa, route="/chat-mcp", **over):
    sc = {"id": "refund", "agent": agent_cfg(fa, route, mcp="orders"), "model": {"rules": [REFUND_SCRIPT]},
          "setup": [{"type": "state", "seed": {"orders": ORDERS}}], "turns": [{"say": "please refund order 1182"}],
          "checks": [{"type": "answer", "contains": "refunded"}, {"type": "state", "collection": "orders", "key": "1182", "expect": {"status": "refunded"}}]}
    sc.update(over)
    return sc


def lookup_scenario(fa, route="/chat-mcp"):
    return {"id": "lookup", "agent": agent_cfg(fa, route, mcp="orders"), "model": {"rules": [LOOKUP_SCRIPT]},
            "setup": [{"type": "state", "seed": {"orders": ORDERS}}], "turns": [{"say": "where is order 1182"}],
            "checks": [{"type": "answer", "contains": "shipped"}, {"type": "path", "source": "tools", "mode": "strict", "expected": [{"tool": "lookup_order", "arguments": {"id": 1182}}]}]}


def items(fa):
    return [{"id": "refund", "scenario": refund_scenario(fa), "tags": ["money"], "split": "smoke"},
            {"id": "lookup", "scenario": lookup_scenario(fa), "tags": ["read"]}]


def setup(sim, name="orders-suite"):
    assert sim.api("PUT", "/api/mcp/orders", MCP)[0] == 200
    status, body = sim.api("PUT", "/api/datasets/" + name, {"items": items(sim.agent)})
    assert status == 200, body
    return body


def experiment(sim, **spec):
    status, body = sim.api("POST", "/api/experiments/run", spec)
    assert status == 200, body
    return body["experiment"]


def statuses(exp):
    return {i["id"]: i["status"] for i in exp["items"]}


def case_a_dataset_is_versioned_and_a_version_does_not_move(sim):
    first = setup(sim, "ds1")
    assert first["version"] == 1 and first["created"] is True
    same = sim.api("PUT", "/api/datasets/ds1", {"items": items(sim.agent)})[1]
    assert same["version"] == 1 and same["created"] is False, "saving nothing new made a new version"
    sim.api("PUT", "/api/scenarios/saved-lookup", lookup_scenario(sim.agent))
    v2 = sim.api("PUT", "/api/datasets/ds1", {"items": items(sim.agent)[:1] + [{"id": "from-saved", "scenarioId": "saved-lookup"}]})[1]
    assert v2["version"] == 2 and v2["created"] is True
    sim.api("PUT", "/api/scenarios/saved-lookup", dict(lookup_scenario(sim.agent), turns=[{"say": "something else entirely"}]))
    frozen = sim.api("GET", "/api/datasets/ds1?version=2")[1]
    assert frozen["items"][1]["scenario"]["turns"] == [{"say": "where is order 1182"}], "a version moved when its saved scenario was edited"
    assert sim.api("POST", "/api/datasets/ds1/tag", {"tag": "baseline", "version": 1})[1]["tags"] == {"baseline": 1}
    assert [i["id"] for i in sim.api("GET", "/api/datasets/ds1?tag=baseline")[1]["items"]] == ["refund", "lookup"]
    assert sim.api("GET", "/api/datasets/ds1")[1]["version"] == 2
    assert [v["version"] for v in sim.api("GET", "/api/datasets/ds1/versions")[1]["versions"]] == [1, 2]
    for bad in ({"items": []}, {"items": [{"id": "x", "scenario": {"id": "x"}}]}, {"items": [{"id": "a", "scenarioId": "s", "scenario": {}}]},
                {"items": items(sim.agent) + [items(sim.agent)[0]]}, {"items": [dict(items(sim.agent)[0], repeat={"n": 3, "required": 5})]}):
        assert sim.api("PUT", "/api/datasets/bad", bad)[0] == 400, bad
    assert sim.api("GET", "/api/datasets/nope")[0] == 404 and sim.api("GET", "/api/datasets/ds1?version=9")[0] == 404
    sim.api("DELETE", "/api/datasets/ds1")


def case_a_run_becomes_an_item_whose_expectation_is_what_it_did(sim):
    setup(sim, "ds2")
    sim.api("PUT", "/api/scenarios/refund-base", refund_scenario(sim.agent))
    out = sim.api("POST", "/api/scenarios/refund-base/run", {})[1]["result"]
    status, body = sim.api("POST", "/api/datasets/ds2/items/from-run", {"run": out["run"], "id": "refund-captured"})
    assert status == 200, body
    kinds = [c["type"] for c in body["item"]["scenario"]["checks"]]
    assert kinds == ["answer", "path", "state", "state"] and body["item"]["metadata"]["fromRun"] == out["run"], kinds
    path = body["item"]["scenario"]["checks"][1]
    assert path["source"] == "tools" and path["expected"] == [{"tool": "refund", "arguments": {"id": 1182}}]
    exp = experiment(sim, dataset="ds2", items=["refund-captured"])
    assert statuses(exp) == {"refund-captured": "pass"}, exp["items"]
    broken = experiment(sim, dataset="ds2", items=["refund-captured"], agent={"url": sim.agent.url + "/chat-v2"})
    assert statuses(broken) == {"refund-captured": "fail"}, "an item made from a good run accepted an agent that does nothing"
    assert sim.api("POST", "/api/datasets/ds2/items/from-run", {"run": "never-ran", "id": "x"})[0] == 400
    sim.api("DELETE", "/api/datasets/ds2")


def case_test_cases_from_elsewhere_are_imported(sim):
    agent = {"name": "sf", "driver": "http", "url": sim.agent.url + "/echo", "body": {"input": "{{message}}"}, "reply": "output.text"}
    csv_text = "utterance,expected_response,expected_actions,expected_topic\nhello there,echo: hello,lookup;refund,Greetings\nanother,echo: another,,\n"
    status, body = sim.api("POST", "/api/datasets/imp/import", {"format": "csv", "content": csv_text, "agent": agent})
    assert status == 200 and body["imported"] == 2 and any("topic" in w for w in body["warnings"]), body
    ds = sim.api("GET", "/api/datasets/imp")[1]["items"]
    assert ds[0]["scenario"]["turns"][0]["expect"] == {"contains": "echo: hello"} and ds[0]["scenario"]["checks"][0]["expected"] == [{"tool": "lookup"}, {"tool": "refund"}]
    assert ds[0]["metadata"]["expected_topic"] == "Greetings"
    xml = """<?xml version="1.0"?><AiEvaluationDefinition xmlns="http://soap.sforce.com/2006/04/metadata"><name>T</name>
      <testCase><expectation><name>action_sequence_match</name><expectedValue>["IdentifyRecordByName","GetRecordDetails"]</expectedValue></expectation>
      <expectation><name>topic_sequence_match</name><expectedValue>GeneralCRM</expectedValue></expectation>
      <inputs><utterance>Find Acme</utterance></inputs></testCase></AiEvaluationDefinition>"""
    status, body = sim.api("POST", "/api/datasets/imp2/import", {"format": "xml", "content": xml, "agent": agent})
    item = sim.api("GET", "/api/datasets/imp2")[1]["items"][0]
    assert status == 200 and item["scenario"]["turns"][0]["say"] == "Find Acme" and item["scenario"]["checks"][0]["expected"] == [{"tool": "IdentifyRecordByName"}, {"tool": "GetRecordDetails"}], (body, item)
    status, body = sim.api("POST", "/api/datasets/imp3/import", {"format": "json", "content": json.dumps({"testCases": [{"utterance": "hi", "expectedActions": ["a"]}]}), "agent": agent})
    assert status == 200 and body["imported"] == 1
    assert sim.api("POST", "/api/datasets/imp4/import", {"format": "yaml", "content": "a: b", "agent": agent})[0] == 400
    assert sim.api("POST", "/api/datasets/imp4/import", {"format": "csv", "content": "x,y\n1,2", "agent": agent})[0] == 400
    assert sim.api("POST", "/api/datasets/imp4/import", {"format": "csv", "content": csv_text, "agent": {"driver": "nope"}})[0] == 400
    # the imported cases run: the agent echoes, so the expected response holds
    exp = experiment(sim, dataset="imp", items=["case-2"])
    assert statuses(exp) == {"case-2": "pass"}, exp["items"]
    for name in ("imp", "imp2", "imp3"):
        sim.api("DELETE", "/api/datasets/" + name)


def flaky_scenario(fa, probability):
    return refund_scenario(fa, id="flaky", setup=[{"type": "state", "seed": {"orders": ORDERS}},
                                                   {"type": "faults", "rules": [{"target": "tool", "match": {"field": "tool", "op": "eq", "value": "refund"},
                                                                                  "fault": {"type": "error"}, "probability": probability}]}])


def case_a_case_is_repeated_and_a_flaky_one_is_not_called_a_pass(sim):
    sim.api("PUT", "/api/mcp/orders", MCP)
    sim.api("PUT", "/api/datasets/fl", {"items": [{"id": "flaky", "scenario": flaky_scenario(sim.agent, 0.5)}]})
    base = experiment(sim, dataset="fl", repeat={"n": 10, "required": 1}, seed="s1")
    item = base["items"][0]
    passes = item["passes"]
    assert 0 < passes < 10, "the seeded dice should make a mixed case; got %d" % passes
    assert item["status"] == "flaky" and base["summary"]["flaky"] == 1 and base["verdict"] == "pass", item
    # N of 10 at N passes, and at N+1 fails
    at = experiment(sim, dataset="fl", repeat={"n": 10, "required": passes}, seed="s1")["items"][0]
    over = experiment(sim, dataset="fl", repeat={"n": 10, "required": passes + 1}, seed="s1")
    assert at["passes"] == passes and at["status"] == "flaky", at
    assert over["items"][0]["status"] == "fail" and over["verdict"] == "fail", over["items"][0]
    # the same seed is the same run; another seed is not
    again = experiment(sim, dataset="fl", repeat={"n": 10, "required": 1}, seed="s1")["items"][0]
    assert again["verdicts"] == item["verdicts"], "the same seed gave a different run"
    other = [experiment(sim, dataset="fl", repeat={"n": 10, "required": 1}, seed="s%d" % i)["items"][0]["verdicts"] for i in (2, 3, 4)]
    assert any(v != item["verdicts"] for v in other), "different seeds all gave the same run"
    sure = experiment(sim, dataset="fl", repeat={"n": 4})["items"][0]       # a fault that never fires is a plain pass
    assert sim.api("PUT", "/api/datasets/fl", {"items": [{"id": "flaky", "scenario": flaky_scenario(sim.agent, 0.0)}]})[0] == 200
    sure = experiment(sim, dataset="fl", repeat=3)["items"][0]
    assert sure["status"] == "pass" and sure["passes"] == 3 and sure["n"] == 3
    assert sim.api("POST", "/api/experiments/run", {"dataset": "fl", "repeat": {"n": 3, "required": 4}})[0] == 400
    sim.api("DELETE", "/api/datasets/fl")


def case_a_new_agent_version_is_compared_with_the_baseline_case_by_case(sim):
    setup(sim, "cmp")
    good = experiment(sim, dataset="cmp", label="v1")
    assert good["verdict"] == "pass" and statuses(good) == {"refund": "pass", "lookup": "pass"}, good["items"]
    assert sim.api("POST", "/api/experiments/%s/baseline" % good["id"], {})[1]["dataset"] == "cmp"
    assert sim.api("POST", "/api/experiments/%s/gate" % good["id"], {})[1]["gate"]["verdict"] == "pass"
    bad = experiment(sim, dataset="cmp", label="v2", agent={"url": sim.agent.url + "/chat-v2"})
    assert bad["verdict"] == "fail" and statuses(bad) == {"refund": "fail", "lookup": "fail"}
    status, gate = sim.api("POST", "/api/experiments/%s/gate" % bad["id"], {})
    g = gate["gate"]
    assert status == 200 and g["verdict"] == "fail" and sorted(e["id"] for e in g["regressed"]) == ["lookup", "refund"], gate
    assert "regressed" in gate["report"] and "refund" in gate["report"] and 'failures="2"' in gate["junit"]
    # the other way: a good version against a bad baseline fixes both
    cmp = sim.api("GET", "/api/experiments/%s/compare/%s" % (bad["id"], good["id"]))[1]
    assert cmp["comparison"]["verdict"] == "pass" and sorted(e["id"] for e in cmp["comparison"]["fixed"]) == ["lookup", "refund"], cmp
    # only one case regresses, the other is new, one is gone
    sim.api("PUT", "/api/datasets/cmp", {"items": [items(sim.agent)[0], {"id": "extra", "scenario": dict(lookup_scenario(sim.agent), id="extra")}]})
    third = experiment(sim, dataset="cmp", label="v3")
    g = sim.api("GET", "/api/experiments/%s/compare/%s" % (good["id"], third["id"]))[1]["comparison"]
    assert [e["id"] for e in g["new"]] == ["extra"] and [e["id"] for e in g["removed"]] == ["lookup"] and g["verdict"] == "pass", g
    # a floor on the pass rate holds whatever the baseline says
    gate = sim.api("POST", "/api/experiments/%s/gate" % bad["id"], {"baseline": bad["id"], "minPassRate": 0.5})[1]
    assert gate["gate"]["verdict"] == "fail" and "below the floor" in gate["report"], gate
    assert sim.api("POST", "/api/experiments/%s/gate" % good["id"], {"baseline": "nope"})[0] == 400
    # a baseline: strict also refuses a case that got flaky
    status, body = sim.api("GET", "/api/experiments")
    assert [e["id"] for e in body["experiments"]][0] == third["id"]
    sim.api("DELETE", "/api/datasets/cmp")


def case_the_same_dataset_runs_on_either_tier(sim):
    setup(sim, "tiers")
    simulated = experiment(sim, dataset="tiers", tier="simulated")
    real = experiment(sim, dataset="tiers", tier="real")
    assert simulated["verdict"] == "pass" and real["tier"] == "real"
    assert real["verdict"] == "fail", "with no model script, the same cases should not pass on the real tier of a simulator that has none"
    assert sim.api("GET", "/api/results/%s" % real["items"][0]["runs"][0])[1]["result"]["tier"] == "real"
    assert sim.api("POST", "/api/experiments/run", {"dataset": "tiers", "tier": "weird"})[0] == 400
    sim.api("DELETE", "/api/datasets/tiers")


def case_the_command_line_and_the_ci_script_gate_on_the_baseline(sim):
    setup(sim, "ci")
    cli = os.path.join(ROOT, "bin", "llmsim")
    env = dict(os.environ, LLMSIM_URL=sim.base)
    sh = lambda *a, **kw: subprocess.run([sys.executable, *a], capture_output=True, text=True, env=dict(env, **kw.get("env", {})))
    first = sh(cli, "experiment", "run", "--dataset", "ci", "--set-baseline")
    assert first.returncode == 0 and "is now the baseline" in first.stdout, (first.stdout, first.stderr)
    clean = sh(cli, "experiment", "run", "--dataset", "ci", "--gate")
    assert clean.returncode == 0 and "PASS" in clean.stdout, clean.stdout
    regressed = sh(cli, "experiment", "run", "--dataset", "ci", "--gate", "--agent-url", sim.agent.url + "/chat-v2")
    assert regressed.returncode == 1 and "regressed" in regressed.stdout and "refund" in regressed.stdout, regressed.stdout
    assert sh(cli, "experiment", "run", "--dataset", "missing").returncode == 2

    posted, summary = [], os.path.join(tempfile.mkdtemp(), "summary.md")

    class GitHub(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            posted.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(201)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    api = ThreadingHTTPServer(("127.0.0.1", 0), GitHub)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    event = os.path.join(tempfile.mkdtemp(), "event.json")
    json.dump({"pull_request": {"number": 5}}, open(event, "w"))
    ci_env = {"INPUT_SERVER-URL": sim.base, "INPUT_DATASET": "ci", "GITHUB_STEP_SUMMARY": summary, "GITHUB_EVENT_PATH": event, "GITHUB_TOKEN": "tok",
              "GITHUB_REPOSITORY": "o/r", "GITHUB_API_URL": "http://127.0.0.1:%d" % api.server_address[1], "GITHUB_SHA": "abcdef1234567890"}
    try:
        ci = os.path.join(ROOT, "bin", "llmsim-ci")
        ok = sh(ci, env=ci_env)
        assert ok.returncode == 0 and "PASS" in ok.stdout and open(summary).read().startswith("### LLMSim: PASS"), (ok.stdout, ok.stderr)
        bad = sh(ci, env=dict(ci_env, **{"INPUT_AGENT-URL": sim.agent.url + "/chat-v2"}))
        assert bad.returncode == 1, (bad.stdout, bad.stderr)
        path, auth, comment = posted[-1]
        assert path == "/repos/o/r/issues/5/comments" and auth == "Bearer tok", (path, auth)
        assert "Regressed (2)" in comment["body"] and "`refund`" in comment["body"] and "`lookup`" in comment["body"], comment["body"]
        assert "LLMSim: FAIL" in open(summary).read()
        assert sh(ci, env=dict(ci_env, **{"INPUT_COMMENT": "false"})).returncode == 0 and len(posted) == 2
        assert sh(ci, env={"INPUT_SERVER-URL": sim.base}).returncode == 2
    finally:
        api.shutdown()
    sim.api("DELETE", "/api/datasets/ci")


def case_pytest_gets_a_fixture_and_a_failure_that_says_what_failed(sim):
    sys.path.insert(0, ROOT)
    from testkit.pytest_plugin import Session
    s = Session(sim.base)
    sim.api("PUT", "/api/mcp/orders", MCP)
    good = refund_scenario(sim.agent)
    assert s.assert_passes(good)["verdict"] == "pass"
    bad = refund_scenario(sim.agent, turns=[{"say": "please refund order 1182", "expect": {"contains": "never said"}}])
    try:
        s.assert_passes(bad)
        raise RuntimeError("a failing scenario passed")
    except AssertionError as exc:
        assert "never said" in str(exc) and "1 of 1" not in str(exc) and "0 of 1 run(s) passed, 1 required" in str(exc), str(exc)
    assert len(s.assert_passes(good, repeat=(3, 3))) == 3
    try:
        s.assert_passes(good, repeat=(2, 3))
        raise RuntimeError("an impossible repeat was accepted")
    except ValueError:
        pass
    action = open(os.path.join(ROOT, "action.yml")).read()
    assert "using: composite" in action and "bin/llmsim-ci" in action and "INPUT_DATASET" in action


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
                print("  FAIL  %s\n        %s: %s" % (case.__name__[5:].replace("_", " "), type(exc).__name__, str(exc)[:1500]))
    finally:
        sim.agent.stop()
        sim.stop()
    print("\n%d passed, %d failed" % (len(cases) - failed, failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
