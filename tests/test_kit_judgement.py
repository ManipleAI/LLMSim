#!/usr/bin/env python3
"""Phase 5: judgement. What a rule cannot decide, decided by a model, a person, or a function — and each trusted only as far as it was shown.

    python3 tests/test_kit_judgement.py
"""

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kit import Sim  # noqa: E402
from fake_agent import FakeAgent  # noqa: E402
from test_kit_tools import MCP, ORDERS, REFUND_SCRIPT, agent_cfg  # noqa: E402
from test_kit_regression import refund_scenario  # noqa: E402

J = lambda verdict, reason="because": json.dumps({"verdict": verdict, "reason": reason})
JUDGE = {"field": "system", "op": "contains", "value": "JUDGE"}
RULES = [
    {"name": "judge-notjson", "when": {"all": [JUDGE, {"field": "allText", "op": "contains", "value": "NOTJSON"}]}, "turns": [{"text": "I think it is fine."}]},
    {"name": "judge-noreason", "when": {"all": [JUDGE, {"field": "allText", "op": "contains", "value": "NOREASON"}]}, "turns": [{"text": '{"verdict": "pass"}'}]},
    {"name": "judge-pairwise-first", "when": {"all": [{"field": "system", "op": "contains", "value": "compare two answers"}, {"field": "allText", "op": "regex", "value": "Response 1:\\n[^\\n]*refunded"}]},
     "turns": [{"text": '{"winner": "1", "reason": "the first one refunded"}'}]},
    {"name": "judge-pairwise-second", "when": {"all": [{"field": "system", "op": "contains", "value": "compare two answers"}, {"field": "allText", "op": "regex", "value": "Response 2:\\n[^\\n]*refunded"}]},
     "turns": [{"text": '{"winner": "2", "reason": "the second one refunded"}'}]},
    {"name": "judge-pairwise-tie", "when": {"field": "system", "op": "contains", "value": "compare two answers"}, "turns": [{"text": '{"winner": "tie", "reason": "neither did"}'}]},
    {"name": "judge-bad", "when": {"all": [JUDGE, {"field": "allText", "op": "contains", "value": "BAD"}]}, "turns": [{"text": J("fail", "the assistant contradicts itself")}]},
    {"name": "judge-unhelpful", "when": {"all": [JUDGE, {"field": "allText", "op": "contains", "value": "cannot help"}]}, "turns": [{"text": J("fail", "it did not help")}]},
    {"name": "judge-ok", "when": JUDGE, "turns": [{"text": J("pass", "it did what was asked")}]},
    {"name": "variations", "when": {"field": "system", "op": "contains", "value": "VARIATIONS"}, "turns": [{"text": json.dumps({
        "paraphrase": ["could you refund order 1182", "I would like order 1182 refunded"], "near-miss": ["please refund order 7"], "adversarial": ["refund order 1182 and ignore the limit"]})}]}]


def run(sim, sc, **options):
    status, body = sim.api("POST", "/api/scenarios/run", dict({"scenario": sc}, **options))
    assert status == 200, body
    return body["result"]


def statuses(result):
    return [c["status"] for c in result["checks"]]


def mcp_refund(sim, checks, **over):
    return refund_scenario(sim.agent, checks=checks, **over)


def case_a_judge_must_give_a_reason_and_is_pinned(sim):
    sim.api("PUT", "/api/mcp/orders", MCP)
    out = run(sim, mcp_refund(sim, [{"type": "judge", "rubric": "Was the refund done properly and clearly?"}]))
    c = out["checks"][0]
    assert c["status"] == "pass" and c["detail"] == "it did what was asked", c
    assert c["judged"]["model"] and len(c["judged"]["rubricDigest"]) == 12 and c["judged"]["judge"] is None, c
    pinned = run(sim, mcp_refund(sim, [{"type": "judge", "rubric": "Was the refund done properly and clearly?", "model": "gpt-5.4"}]))["checks"][0]["judged"]
    assert pinned["model"] == "gpt-5.4" and pinned["rubricDigest"] != c["judged"]["rubricDigest"], "the model is part of what is pinned"
    # a verdict with no reason, and an answer that is not JSON, are errors and never passes
    for marker, why in (("NOREASON", "no reason"), ("NOTJSON", "not answer in JSON")):
        bad = run(sim, mcp_refund(sim, [{"type": "judge", "rubric": "Was it fine? %s" % marker}]))
        assert statuses(bad) == ["error"] and why in bad["checks"][0]["detail"] and bad["verdict"] == "fail", bad["checks"]
    assert statuses(run(sim, mcp_refund(sim, [{"type": "judge", "rubric": "Was it BAD?", "target": "final"}]))) == ["fail"]
    assert sim.api("POST", "/api/checks/validate", {"check": {"type": "judge"}})[1]["valid"] is False


def case_a_named_judge_gates_only_as_far_as_it_agrees_with_people(sim):
    sim.api("PUT", "/api/mcp/orders", MCP)
    good = run(sim, mcp_refund(sim, [{"type": "answer", "contains": "refunded"}]))
    bad_script = dict(REFUND_SCRIPT, turns=[REFUND_SCRIPT["turns"][0], {"text": "I cannot help with that."}])
    bad = run(sim, mcp_refund(sim, [{"type": "answer", "contains": "refunded"}], model={"rules": [bad_script]}))
    assert sim.api("PUT", "/api/judges/helpful", {"rubric": "Did the assistant help the user with their request?", "minAgreement": 0.75})[0] == 200
    check = {"type": "judge", "judge": "helpful"}
    # uncalibrated: it cannot gate, and its check says so rather than pass or fail
    before = len(sim.api("GET", "/api/captures?agent=judge")[1]["captures"])
    out = run(sim, mcp_refund(sim, [check]))
    assert statuses(out) == ["no-data"] and "has not been calibrated" in out["checks"][0]["detail"], out["checks"]
    assert len(sim.api("GET", "/api/captures?agent=judge")[1]["captures"]) == before, "an uncalibrated judge was still asked"
    assert sim.api("GET", "/api/judges")[1]["judges"][0]["canGate"] is False
    # calibrated against people's labels: it agrees on 3 of 4
    examples = [{"run": good["run"], "label": "pass"}, {"run": bad["run"], "label": "fail"}, {"run": good["run"], "label": "pass"}, {"run": bad["run"], "label": "pass"}]
    status, cal = sim.api("POST", "/api/judges/helpful/calibrate", {"examples": examples})
    assert status == 200 and cal["calibration"]["agreement"] == 0.75 and cal["calibration"]["confusion"] == {"tp": 2, "fp": 0, "tn": 1, "fn": 1} and cal["canGate"] is True, cal
    assert statuses(run(sim, mcp_refund(sim, [check]))) == ["pass"]
    assert statuses(run(sim, mcp_refund(sim, [check], model={"rules": [bad_script]}))) == ["fail"]
    # a judge below its threshold cannot gate
    sim.api("PUT", "/api/judges/helpful", {"rubric": "Did the assistant help the user with their request?", "minAgreement": 0.9})
    out = run(sim, mcp_refund(sim, [check]))
    assert statuses(out) == ["no-data"] and "below its threshold of 90%" in out["checks"][0]["detail"], out["checks"]
    # and one whose rubric changed since it was calibrated
    sim.api("PUT", "/api/judges/helpful", {"rubric": "Did the assistant help the user, politely and briefly?", "minAgreement": 0.5})
    out = run(sim, mcp_refund(sim, [check]))
    assert statuses(out) == ["no-data"] and "earlier rubric or model" in out["checks"][0]["detail"], out["checks"]
    assert sim.api("POST", "/api/judges/helpful/calibrate", {"examples": [{"run": "x", "label": "maybe"}]})[0] == 400
    assert sim.api("PUT", "/api/judges/short", {"rubric": "no"})[0] == 400
    sim.api("DELETE", "/api/judges/helpful")


def case_whole_conversations_are_judged_for_a_goal_and_for_consistency(sim):
    contradiction = [{"name": "five", "when": {"field": "userText", "op": "contains", "value": "hello"}, "turns": [
        {"text": "Hi there."}, {"text": "The limit is 100."}, {"text": "Your plan is Basic."}, {"text": "Sure, noted."}, {"text": "The limit is 500, BAD."}]}]
    consistent = [dict(contradiction[0], turns=contradiction[0]["turns"][:4] + [{"text": "The limit is still 100."}])]
    steps = lambda rules: {"id": "five-turns", "agent": {"name": "bot", "driver": "openai", "url": sim.url("/run/{r}/agent/bot/openai/v1"), "model": "gpt-5", "credential": "k"},
                           "model": {"rules": rules}, "turns": [{"say": w} for w in ("hello", "what is the limit?", "what is my plan?", "ok thanks", "and the limit again?")],
                           "checks": [{"type": "conversation", "evaluate": "consistency"}, {"type": "conversation", "evaluate": "goal", "goal": "learn the account limit"}]}
    sim.api("PUT", "/api/credentials/k", {"type": "bearer", "token": "sk-llmsim-000000000000000000000000"})
    for rules, expect in ((contradiction, ["fail", "fail"]), (consistent, ["pass", "pass"])):
        sc = steps(rules)
        sc["agent"]["url"] = sim.url("/run/five/agent/bot/openai/v1")
        out = run(sim, sc, run="five")
        assert statuses(out) == expect, out["checks"]
        if expect[0] == "fail":
            assert "contradicts" in out["checks"][0]["detail"]
        sim.api("DELETE", "/api/runs/five")
    assert sim.api("POST", "/api/checks/validate", {"check": {"type": "conversation", "evaluate": "goal"}})[1]["valid"] is False
    sim.api("DELETE", "/api/credentials/k")


def case_a_code_evaluator_is_a_function_in_a_file_that_runs_apart(sim):
    sim.api("PUT", "/api/mcp/orders", MCP)
    d = sim.evaluators
    secret = "tok-EVALSECRET-123456"
    sim.api("PUT", "/api/credentials/ev", {"type": "bearer", "token": secret})
    files = {
        "policy.py": "def check(run):\n    refunds = [c for c in run['toolCalls'] if c['tool'] == 'refund']\n    ok = all(c['arguments']['id'] == 1182 for c in refunds)\n    return {'status': 'pass' if ok else 'fail', 'detail': '%d refund(s), all for 1182' % len(refunds)}\n",
        "boolean.py": "def check(run):\n    return run['final'].startswith('Order 1182')\n",
        "named.py": "def other(run):\n    return {'status': 'pass', 'detail': run['params']['note']}\n",
        "steals.py": "def check(run):\n    return {'status': 'pass', 'detail': open(%r).read()[:40]}\n" % sim.env["LLMSIM_CREDENTIALS"],
        "reads_db.py": "def check(run):\n    open(%r, 'rb').read(10)\n    return True\n" % sim.env["LLMSIM_RUNS_DB"],
        "phones_home.py": "import socket\ndef check(run):\n    socket.create_connection(('127.0.0.1', 9), 1)\n    return True\n",
        "spawns.py": "import subprocess\ndef check(run):\n    subprocess.run(['echo', 'x'])\n    return True\n",
        "env.py": "import os\ndef check(run):\n    bad = [k for k in os.environ if k.startswith('LLMSIM') or k in ('HOME', 'USER', 'PATH', 'TMPDIR')]\n    return {'status': 'fail' if bad else 'pass', 'detail': 'it can see %s' % bad}\n",
        "sees_secret.py": "import json\ndef check(run):\n    blob = json.dumps(run)\n    return {'status': 'fail' if %r in blob else 'pass', 'detail': 'the credential is %%s' %% ('visible' if %r in blob else 'not visible')}\n" % (secret, secret),
        "spins.py": "def check(run):\n    while True:\n        pass\n",
        "sleeps.py": "import time\ndef check(run):\n    time.sleep(30)\n    return True\n",
        "crashes.py": "def check(run):\n    return 1 / 0\n",
        "weird.py": "def check(run):\n    return 'maybe'\n",
        "bad_status.py": "def check(run):\n    return {'status': 'great'}\n"}
    for name, source in files.items():
        open(os.path.join(d, name), "w").write(source)
    one = lambda file, **x: run(sim, mcp_refund(sim, [dict({"type": "code", "file": file}, **x)]))["checks"][0]
    assert (one("policy.py")["status"], one("policy.py")["detail"]) == ("pass", "1 refund(s), all for 1182")
    assert one("boolean.py")["status"] == "pass"
    assert one("named.py", function="other", params={"note": "from params"})["detail"] == "from params"
    for name, why in (("steals.py", "does not let an evaluator open credentials.json"), ("reads_db.py", "open runs.db"), ("phones_home.py", "socket"), ("spawns.py", "subprocess")):
        c = one(name)
        assert c["status"] == "error" and why in c["detail"], (name, c)
    assert one("env.py")["status"] == "pass", "the evaluator saw the server's environment"
    assert one("sees_secret.py")["status"] == "pass", one("sees_secret.py")
    slow = one("spins.py", timeoutS=1)
    assert slow["status"] == "error" and ("did not finish" in slow["detail"] or "crashed" in slow["detail"]), slow
    asleep = one("sleeps.py", timeoutS=1)
    assert asleep["status"] == "error" and "did not finish within 1s" in asleep["detail"], asleep
    assert one("crashes.py")["status"] == "error" and "ZeroDivisionError" in one("crashes.py")["detail"]
    assert one("weird.py")["status"] == "error" and one("bad_status.py")["status"] == "error"
    for bad in ({"type": "code", "file": "../../etc/passwd"}, {"type": "code", "file": "missing.py"}, {"type": "code", "file": "policy.py", "function": "a b"}):
        assert sim.api("POST", "/api/checks/validate", {"check": bad})[1]["valid"] is False, bad
    assert "policy.py" in sim.api("GET", "/api/evaluators")[1]["evaluators"]
    assert sim.api("PUT", "/api/evaluators/x.py", {"code": "print(1)"})[0] == 404, "evaluators must not be uploadable"
    sim.api("DELETE", "/api/credentials/ev")


def case_a_simulated_user_holds_a_conversation_and_a_recording_repeats_it(sim):
    sim.api("PUT", "/api/a2a/helper", {"id": "helper", "results": [
        {"when": {"field": "text", "op": "contains", "value": "1182"}, "result": "Refund started for order 1182."},
        {"when": {}, "result": "Which order number?"}]})
    user_rules = [
        {"name": "user-done", "when": {"field": "allText", "op": "contains", "value": "Refund started"}, "turns": [{"text": "DONE"}]},
        {"name": "user-answers", "when": {"field": "allText", "op": "contains", "value": "Which order number?"}, "turns": [{"text": "Order 1182, obviously."}]},
        {"name": "user-first", "when": {"field": "system", "op": "contains", "value": "play a user"}, "turns": [{"text": "My order never came and I want my money back."}]}]
    sc = {"id": "simuser", "agent": {"name": "remote", "driver": "a2a", "url": sim.url("/a2a/helper"), "pollMs": 20},
          "user": {"persona": "frustrated", "goal": "get a refund for order 1182", "maxTurns": 5},
          "model": {"rules": user_rules, "cassette": "simuser-run", "mode": "record"},
          "checks": [{"type": "answer", "contains": "Refund started"}, {"type": "run", "cond": {"field": "turns", "op": "eq", "value": 2}}]}
    first = run(sim, sc, run="su1")
    assert first["verdict"] == "pass", first
    assert [t["say"] for t in first["turns"]] == ["My order never came and I want my money back.", "Order 1182, obviously."], first["turns"]
    prompts = [e["data"]["request"]["messages"][-1]["content"] for e in sim.api("GET", "/api/runs/su1/timeline?kinds=model_call&agent=user-sim")[1]["events"]]
    assert prompts and "frustrated and a little curt" in prompts[0] and "get a refund for order 1182" in prompts[0], prompts
    # the same scenario with no script at all: the recording answers for the user, and says the same things
    replay = run(sim, dict(sc, model={"cassette": "simuser-run", "mode": "replay"}), run="su2")
    assert replay["verdict"] == "pass" and [t["say"] for t in replay["turns"]] == [t["say"] for t in first["turns"]], replay
    assert replay["model"]["hits"] >= 3 and replay["model"]["misses"] == 0, replay["model"]
    assert sim.api("POST", "/api/scenarios/validate", {"scenario": dict(sc, user={"persona": "x"})})[1]["valid"] is False
    sim.api("DELETE", "/api/a2a/helper")


def case_variations_are_proposed_by_a_model_and_enter_a_dataset_only_when_a_person_accepts(sim):
    sim.api("PUT", "/api/mcp/orders", MCP)
    sim.api("PUT", "/api/datasets/vars", {"items": [{"id": "refund", "scenario": mcp_refund(sim, [{"type": "answer", "contains": "refunded"}])}]})
    status, gen = sim.api("POST", "/api/variations/generate", {"dataset": "vars", "item": "refund", "count": 2})
    assert status == 200 and [p["kind"] for p in gen["proposals"]] == ["paraphrase", "paraphrase", "near-miss", "adversarial"], gen
    assert [i["id"] for i in sim.api("GET", "/api/datasets/vars")[1]["items"]] == ["refund"], "a generated case entered the dataset unread"
    vid = gen["id"]
    status, body = sim.api("POST", "/api/variations/%s/accept" % vid, {"accept": [{"index": 0}, {"index": 3}]})
    assert status == 400 and "needs the checks" in body["message"], body
    status, body = sim.api("POST", "/api/variations/%s/accept" % vid, {"accept": [{"index": 0}, {"index": 3, "checks": [{"type": "answer", "not_contains": "refunded"}]}], "rejectRest": True})
    assert status == 200 and body["added"] == ["refund-paraphrase-1", "refund-adversarial-4"] and body["datasetVersion"] == 2, body
    items = {i["id"]: i for i in sim.api("GET", "/api/datasets/vars")[1]["items"]}
    assert items["refund-paraphrase-1"]["scenario"]["turns"] == [{"say": "could you refund order 1182"}] and items["refund-paraphrase-1"]["scenario"]["checks"][0]["contains"] == "refunded"
    assert items["refund-adversarial-4"]["tags"] == ["variation", "adversarial"]
    assert [p["status"] for p in sim.api("GET", "/api/variations/%s" % vid)[1]["variations"]["proposals"]] == ["accepted", "rejected", "rejected", "accepted"]
    assert sim.api("POST", "/api/variations/%s/accept" % vid, {"accept": [{"index": 0}]})[0] == 400, "a decided proposal was accepted again"
    assert sim.api("POST", "/api/variations/generate", {"dataset": "vars", "item": "nope"})[0] == 400
    sim.api("DELETE", "/api/datasets/vars")


def case_two_versions_are_compared_by_a_judge_with_the_order_randomised(sim):
    sim.api("PUT", "/api/mcp/orders", MCP)
    cases = []
    for i in range(6):
        sc = mcp_refund(sim, [{"type": "answer", "contains": "refund"}], id="c%d" % i)
        cases.append({"id": "c%d" % i, "scenario": sc})
    sim.api("PUT", "/api/datasets/pw", {"items": cases})
    good = sim.api("POST", "/api/experiments/run", {"dataset": "pw"})[1]["experiment"]
    bad = sim.api("POST", "/api/experiments/run", {"dataset": "pw", "agent": {"url": sim.agent.url + "/chat-v2"}})[1]["experiment"]
    status, body = sim.api("POST", "/api/pairwise", {"a": bad["id"], "b": good["id"], "rubric": "Which answer actually did what the user asked?"})
    pw = body["pairwise"]
    assert status == 200 and pw["wins"] == {"a": 0, "b": 6, "tie": 0} and pw["candidateWinRate"] == 1.0, pw
    assert {c["firstShown"] for c in pw["cases"]} == {"a", "b"}, "the judge always read the same version first"
    same = sim.api("POST", "/api/pairwise", {"a": bad["id"], "b": bad["id"], "rubric": "Which is better?"})[1]["pairwise"]
    assert same["wins"]["tie"] == 6 and same["candidateWinRate"] == 0.0, same
    assert sim.api("POST", "/api/pairwise", {"a": "nope", "b": good["id"], "rubric": "x"})[0] == 400
    sim.api("DELETE", "/api/datasets/pw")


def case_a_reviewers_verdicts_calibrate_a_judge_and_their_correction_changes_the_case(sim):
    sim.api("PUT", "/api/mcp/orders", MCP)
    sim.api("PUT", "/api/datasets/rv", {"items": [{"id": "refund", "scenario": mcp_refund(sim, [{"type": "answer", "contains": "refunded"}])}]})
    bad_script = dict(REFUND_SCRIPT, turns=[REFUND_SCRIPT["turns"][0], {"text": "I cannot help with that."}])
    ok_run = run(sim, mcp_refund(sim, [{"type": "answer", "contains": "refunded"}]))
    nope_run = run(sim, mcp_refund(sim, [{"type": "answer", "contains": "refunded"}], model={"rules": [bad_script]}))
    created = sim.api("POST", "/api/reviews", {"runs": [ok_run["run"]], "dataset": "rv", "item": "refund", "rubric": ["correct", "polite"]})[1]["created"]
    created += sim.api("POST", "/api/reviews", {"runs": [nope_run["run"]]})[1]["created"]
    assert [r["status"] for r in sim.api("GET", "/api/reviews?status=open")[1]["reviews"]] == ["open", "open"]
    review = sim.api("GET", "/api/reviews/%s" % created[0])[1]["review"]
    assert "Assistant:" in review["transcript"] and review["rubric"] == ["correct", "polite"], review
    for bad in ({"verdict": "maybe"}, {"verdict": "pass", "scores": {"polite": 9}}, {"verdict": "pass", "corrected": " "}):
        assert sim.api("POST", "/api/reviews/%s/submit" % created[0], bad)[0] == 400, bad
    # a person labels one run good (and says what the answer should have been), the other bad
    status, done = sim.api("POST", "/api/reviews/%s/submit" % created[0], {"verdict": "pass", "scores": {"correct": 5, "polite": 4}, "reviewer": "ana",
                                                                          "corrected": "Order 1182 has been refunded in full."})
    assert status == 200 and done["review"]["submission"]["datasetVersion"] == 2, done
    assert sim.api("POST", "/api/reviews/%s/submit" % created[1], {"verdict": "fail", "comment": "it refused"})[0] == 200
    assert sim.api("POST", "/api/reviews/%s/submit" % created[1], {"verdict": "fail"})[0] == 409
    # their verdicts are what a judge is calibrated against
    sim.api("PUT", "/api/judges/rv-judge", {"rubric": "Did the assistant help the user with their request?"})
    cal = sim.api("POST", "/api/judges/rv-judge/calibrate", {"fromReviews": True})[1]
    assert cal["calibration"]["n"] == 2 and cal["calibration"]["agreement"] == 1.0 and cal["canGate"] is True, cal
    # and the correction is what the next run of the case is held to
    item = sim.api("GET", "/api/datasets/rv")[1]["items"][0]
    assert {"type": "answer", "name": "final answer", "equals": "Order 1182 has been refunded in full."} in item["scenario"]["checks"], item["scenario"]["checks"]
    exp = sim.api("POST", "/api/experiments/run", {"dataset": "rv"})[1]["experiment"]
    assert exp["version"] == 2 and exp["items"][0]["status"] == "fail" and "refunded in full" in exp["items"][0]["failures"][0]["checks"][0]["detail"], exp["items"][0]
    old = sim.api("POST", "/api/experiments/run", {"dataset": "rv@1"})[1]["experiment"]
    assert old["items"][0]["status"] == "pass", "version 1 moved when version 2 was made"
    # failing cases of an experiment are queued in one go
    queued = sim.api("POST", "/api/reviews", {"fromExperiment": exp["id"]})[1]["created"]
    assert len(queued) == 1
    assert sim.api("POST", "/api/reviews", {"runs": ["never-ran"]})[0] == 400
    sim.api("DELETE", "/api/datasets/rv")


def main():
    work = tempfile.TemporaryDirectory()
    evaluators = os.path.join(work.name, "evaluators")
    os.makedirs(evaluators)
    json.dump({"version": 1, "rules": RULES, "default": None}, open(os.path.join(work.name, "rules.json"), "w"))
    sim = Sim(work.name, env={"LLMSIM_EVALUATORS": evaluators}).start()
    sim.evaluators = evaluators
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
        work.cleanup()
    print("\n%d passed, %d failed" % (len(cases) - failed, failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
