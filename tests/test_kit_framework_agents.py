#!/usr/bin/env python3
"""Testing an agent built on a framework (LangGraph, LangChain and the like) through LLMSim.

    python3 tests/test_kit_framework_agents.py

Such an agent calls its model with whatever LLMSim it is pointed at, runs its tools inside
itself, and is often run on an LLMSim whose default route is somebody's real model. Each case
here is something that went wrong testing a real LangGraph agent on such an LLMSim:

- the scenario's script was ignored, because the default route was a ChatGPT plan, and the
  run still called itself simulated;
- a model that fails once could only be had by changing every run's faults;
- what the agent's own tools returned was seen nowhere;
- a memory test could not be scripted, a check could not name what the run made, a path with
  "this tool, any arguments" could not be written, and the number in a real model's answer
  was the wrong one;
- an agent that cannot be changed names no run, so its model calls could not be in one.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kit import Sim, run_cases  # noqa: E402
from fake_agent import FakeAgent  # noqa: E402

SCRIPT = [{"name": "order-flow", "when": {"contains": "order 1182"}, "turns": [
    {"text": "Looking it up.", "tool_calls": [{"name": "lookup_order", "input": {"id": 1182}}]},
    {"text": "Order 1182 has {{tool_output}}."}]}]


def agent_cfg(sim, plain=False, model=None):
    body = {"input": "{{message}}", "session": "{{conversation}}"}
    if not plain:
        body.update(run="{{run}}", agent="{{agent}}")
    if model:
        body["model"] = model
    return {"name": "support", "driver": "http", "url": sim.agent.url + "/chat", "body": body, "reply": "output.text"}


def scenario(sim, **over):
    sc = {"id": "framework", "agent": agent_cfg(sim), "model": {"rules": SCRIPT},
          "turns": [{"say": "Where is order 1182?"}], "checks": [{"type": "answer", "contains": "shipped"}]}
    sc.update(over)
    return sc


def run(sim, sc, **options):
    status, body = sim.api("POST", "/api/scenarios/run", dict({"scenario": sc}, **options))
    assert status == 200, body
    return body["result"]


def default_route(sim, route):
    sim.api("DELETE", "/api/routing/hold")
    assert sim.api("PUT", "/api/routing", {"default": route, "models": {}})[0] == 200


def timeline(sim, result):
    return sim.api("GET", "/api/runs/%s/timeline" % result["run"])[1]["events"]


# The default route in these cases is the ChatGPT plan with nobody signed in: a request that
# reaches it fails, which is how a case tells the script from a real model without a network.

def case_a_runs_own_script_answers_it_whatever_the_default_route(sim):
    default_route(sim, "chatgpt")
    try:
        result = run(sim, scenario(sim))
        assert result["verdict"] == "pass", result
        assert result["model"]["answered"] == {"routes": {"scripted": 2}, "by": "the script"}, result["model"]
        assert "warnings" not in result, result.get("warnings")
        # and nothing else is answered by it: a request of no run still goes where Routing says
        assert sim.chat("Where is order 1182?")[0] != 200
    finally:
        default_route(sim, "scripted")


def case_what_the_script_does_not_answer_goes_where_routing_says_and_the_run_says_so(sim):
    default_route(sim, "chatgpt")
    try:
        sc = scenario(sim, turns=[{"say": "Something the script has no rule for"}], checks=[{"type": "answer", "contains": "x"}])
        result = run(sim, sc)
        assert result["model"]["answered"]["routes"] == {"chatgpt": 1}, result["model"]
        assert result["model"]["answered"]["by"] == "a real model"
        assert any("answered by a real model (chatgpt), not the script" in w for w in result.get("warnings", [])), result.get("warnings")
        # asked to, the run refuses such a request instead, and says why
        refused = run(sim, dict(sc, model={"rules": SCRIPT, "unmatched": "refuse"}))
        assert refused["verdict"] == "fail" and refused["model"]["answered"] == {"routes": {"refused": 1}, "by": "nothing"}, refused["model"]
        calls = [e for e in timeline(sim, refused) if e["kind"] == "model_call"]
        assert [(e["data"]["route"], e["data"]["status"]) for e in calls] == [("refused", 400)], calls
        assert "no rule for this request" in str(calls[0]["data"]["response"]), calls[0]["data"]["response"]
    finally:
        default_route(sim, "scripted")


def case_unmatched_needs_a_script_and_is_route_or_refuse(sim):
    for model, said in (({"unmatched": "refuse"}, "it needs model.rules"), ({"rules": SCRIPT, "unmatched": "maybe"}, "model.unmatched is route")):
        status, body = sim.api("POST", "/api/scenarios/validate", {"scenario": scenario(sim, model=model)})
        assert status == 200 and any(said in p for p in body["problems"]), body


def case_a_scenario_brings_model_faults_for_its_run_only(sim):
    before = sim.api("GET", "/api/faults")[1]
    sc = scenario(sim, model={"rules": SCRIPT, "faults": [{"name": "down-once", "fault": {"type": "status", "status": 503}, "limit": 1}]})
    faulted = run(sim, sc)
    assert faulted["verdict"] == "fail" and "503" in faulted.get("error", ""), faulted
    calls = [e for e in timeline(sim, faulted) if e["kind"] == "model_call"]
    assert calls and calls[0]["data"]["status"] == 503 and calls[0]["data"]["fault"] == "status", calls
    # gone with the run: the saved faults are untouched, the next run is not faulted, nor is a request of no run
    assert sim.api("GET", "/api/faults")[1] == before
    assert run(sim, scenario(sim))["verdict"] == "pass"
    assert sim.chat("hello there")[0] == 200
    bad = sim.api("POST", "/api/scenarios/validate", {"scenario": scenario(sim, model={"rules": SCRIPT, "faults": [{"fault": {"type": "nonsense"}}]})})[1]
    assert any(p.startswith("model.faults[0]") for p in bad["problems"]), bad


def case_what_an_agents_own_tool_returned_is_on_the_timeline(sim):
    result = run(sim, scenario(sim, checks=[
        {"type": "path", "mode": "strict", "expected": [{"tool": "lookup_order", "arguments": {"id": 1182}}], "source": "agent"},
        {"type": "budget", "steps": {"toolCalls": 1}},
        {"type": "answer", "contains": "shipped"}]))
    assert result["verdict"] == "pass", result["checks"]
    events = timeline(sim, result)
    kinds = [e["kind"] for e in events]
    assert kinds[:4] == ["user_turn", "model_call", "tool_call", "model_call"], kinds
    # what LLMSim's own mocks received is a separate layer, and this agent reached none
    served = run(sim, scenario(sim, checks=[{"type": "path", "mode": "strict", "expected": [], "source": "tools"}]))
    assert served["verdict"] == "pass", served["checks"]
    tool = events[2]
    assert tool["parent"] == events[1]["id"] and tool["data"]["source"] == "agent" and '"shipped"' in tool["data"]["result"], tool
    # seen once, however many later requests carry the same result back
    # seen once, however many later requests carry the same result back: the second turn's request
    # holds the first turn's tool result again, and it is not counted twice
    two = run(sim, scenario(sim, turns=[{"say": "Where is order 1182?"}, {"say": "Thanks, is there anything else?"}]))
    later = [e for e in timeline(sim, two) if e["kind"] == "model_call"][-1]
    assert "lookup_order" in str(later["data"]["request"]), "the second turn should carry the first turn's tool result"
    assert sum(1 for e in timeline(sim, two) if e["kind"] == "tool_call") == 1, [e["kind"] for e in timeline(sim, two)]


def case_a_rule_can_depend_on_anything_said_earlier(sim):
    rules = [{"name": "intro", "when": {"contains": "My name is"}, "turns": [{"text": "Nice to meet you."}]},
             {"name": "recall", "when": {"all": [{"contains": "my name"}, {"anyMessage": {"contains": "Ada Lovelace"}}]},
              "turns": [{"text": "You are Ada Lovelace."}]},
             {"name": "forgot", "when": {"contains": "my name"}, "turns": [{"text": "I do not know."}]}]
    remembers = run(sim, scenario(sim, model={"rules": rules}, turns=[{"say": "My name is Ada Lovelace."}, {"say": "What is my name?"}],
                                  checks=[{"type": "answer", "contains": "Ada Lovelace"}]))
    assert remembers["verdict"] == "pass", remembers["turns"]
    # an agent that forgets is told apart: the second request has nothing earlier in it
    forgets = run(sim, scenario(sim, model={"rules": rules}, turns=[{"say": "What is my name?"}], checks=[{"type": "answer", "contains": "Ada"}]))
    assert forgets["verdict"] == "fail" and forgets["turns"][0]["reply"] == "I do not know."
    status, body = sim.api("POST", "/api/rules", {"rule": {"name": "r", "when": {"anyMessage": {"nonsense": 1}}, "turns": [{"text": "x"}]}})
    assert status == 400 and any("unknown text condition 'nonsense'" in p for p in body["problems"]), body


def case_a_check_can_name_what_the_run_made(sim):
    rules = [{"name": "named", "when": {"contains": "order 1182"}, "turns": [{"text": "Filed as {{run}}."}]}]
    result = run(sim, scenario(sim, model={"rules": rules}, checks=[{"type": "answer", "equals": "Filed as {{run}}."}]))
    assert result["verdict"] == "pass", result["checks"]


def case_a_reference_call_without_arguments_takes_any(sim):
    result = run(sim, scenario(sim, checks=[{"type": "path", "mode": "strict", "expected": [{"tool": "lookup_order"}]}]))
    assert result["verdict"] == "pass", result["checks"]
    # arguments given are still compared, exactly unless said otherwise
    wrong = run(sim, scenario(sim, checks=[{"type": "path", "mode": "strict", "expected": [{"tool": "lookup_order", "arguments": {"id": 7}}]}]))
    assert wrong["verdict"] == "fail" and "lookup_order" in wrong["checks"][0]["detail"], wrong["checks"]


def case_the_number_judged_can_be_any_or_the_last(sim):
    rules = [{"name": "fib", "when": {"contains": "order 1182"}, "turns": [{"text": "The 20th Fibonacci number is 6,765."}]}]
    one = lambda n: run(sim, scenario(sim, model={"rules": rules}, checks=[{"type": "answer", "number": n}]))["checks"][0]
    assert one({"equals": 6765})["status"] == "fail"                        # the first number is the 20
    assert one({"equals": 6765, "pick": "any"})["status"] == "pass"
    assert one({"equals": 6765, "pick": "last"})["status"] == "pass"
    missed = one({"equals": 42, "pick": "any"})
    assert missed["status"] == "fail" and "none of the 2 numbers" in missed["detail"], missed
    bad = sim.api("POST", "/api/scenarios/validate", {"scenario": scenario(sim, checks=[{"type": "answer", "number": {"equals": 1, "pick": "middle"}}])})[1]
    assert any("number.pick is first, last or any" in p for p in bad["problems"]), bad


def case_an_agent_nobody_changed_has_its_model_calls_adopted(sim):
    """It calls LLMSim's plain address and names no run. A run that claims calls by their model adopts them."""
    plain = agent_cfg(sim, plain=True, model="gpt-5-nano")
    unclaimed = run(sim, scenario(sim, agent=plain))
    assert unclaimed["verdict"] == "fail" and unclaimed["model"]["answered"]["by"] == "nothing", unclaimed["model"]
    claimed = run(sim, scenario(sim, agent=plain, model={"rules": SCRIPT, "claim": {"model": "gpt-5-nano"}},
                                checks=[{"type": "answer", "contains": "shipped"}, {"type": "path", "mode": "strict", "expected": [{"tool": "lookup_order"}]}]))
    assert claimed["verdict"] == "pass", claimed
    calls = [e for e in timeline(sim, claimed) if e["kind"] == "model_call"]
    assert len(calls) == 2 and all(e["agent"] == "support" for e in calls), calls
    # only what the condition picks out: another model's unlabelled call, made during no run, is nobody's
    assert run(sim, scenario(sim, agent=agent_cfg(sim, plain=True, model="gpt-5"), model={"rules": SCRIPT, "claim": {"model": "gpt-5-nano"}}))["verdict"] == "fail"
    # and the claim ends with the run
    sim.chat("Where is order 1182?")
    assert sim.api("GET", "/api/runs/%s/timeline" % claimed["run"])[1]["counts"].get("model_call") == 2
    bad = sim.api("POST", "/api/scenarios/validate", {"scenario": scenario(sim, model={"rules": SCRIPT, "claim": "everything"})})[1]
    assert any("model.claim is a condition" in p for p in bad["problems"]), bad


def make_sim():
    sim = Sim()
    sim.start_orig = sim.start

    def start():
        sim.start_orig()
        sim.agent = FakeAgent(sim.base)
        return sim
    sim.start = start
    return sim


if __name__ == "__main__":
    run_cases([v for k, v in list(globals().items()) if k.startswith("case_")], make_sim=make_sim)
