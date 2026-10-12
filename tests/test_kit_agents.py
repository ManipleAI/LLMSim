#!/usr/bin/env python3
"""Agents under test: saved once, reached as their platform is reached, and run against.

    python3 tests/test_kit_agents.py

Nothing here reaches Salesforce, SAP, ServiceNow or Azure. Each platform is a stand-in written
from its vendor's documentation and strict where that documentation is, which is the same
bargain the Agentforce driver was made on: the wire is tested, the tenant is not.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kit import Sim  # noqa: E402
from fake_agent import FakeAgent  # noqa: E402
from fake_platforms import FakeA2A, FakeFoundry  # noqa: E402

SCRIPT = [{"name": "order-flow", "when": {"contains": "order 1182"}, "turns": [
    {"text": "Looking it up.", "tool_calls": [{"name": "lookup_order", "input": {"id": 1182}}]},
    {"text": "It is over the limit, so I have handed it to a manager."}]}]


def http_connection(fa, path="/chat"):
    return {"driver": "http", "url": fa.url + path, "reply": "output.text",
            "body": {"input": "{{message}}", "session": "{{conversation}}", "run": "{{run}}", "agent": "{{agent}}"}}


def refund(agent, sid="refund", **over):
    sc = {"id": sid, "description": "an over-limit refund is handed on", "tags": ["refunds"], "agent": agent, "model": {"rules": SCRIPT},
          "turns": [{"say": "I want a refund for order 1182", "expect": {"contains": "manager"}}],
          "checks": [{"type": "path", "mode": "strict", "expected": [{"tool": "lookup_order", "arguments": {"id": 1182}}]}]}
    sc.update(over)
    return sc


def put(sim, path, body, want=200):
    status, out = sim.api("PUT", path, body)
    assert status == want, (path, status, out)
    return out


def problems(sim, agent):
    return sim.api("POST", "/api/agents/validate", {"agent": agent})[1]["problems"]


def case_the_systems_say_how_each_is_reached(sim):
    """Adding an agent starts with where it lives, and each platform is reached its own way. What the kit
    offers for a platform has to be something it can actually do: a connection whose driver does not exist,
    or whose template the driver would refuse, is a form nobody can complete."""
    status, body = sim.api("GET", "/api/agents")
    assert status == 200 and [s["id"] for s in body["systems"]] == ["salesforce", "sap", "servicenow", "azure-foundry", "custom"], body
    known = {d["name"] for d in sim.api("GET", "/api/drivers")[1]["drivers"]}
    for system in body["systems"]:
        assert system["name"] and system["about"] and system["sees"], system["id"]
        assert system["connections"], system["id"]
        for conn in system["connections"]:
            assert conn["driver"] in known and conn["label"] and conn["note"], (system["id"], conn)
            # the template is complete for its driver; what is left is the blanks a person fills in
            left = problems(sim, {"id": "x", "system": system["id"], "connection": dict(conn["defaults"], driver=conn["driver"])})
            if conn["driver"] == "process":                      # a command, which this server does not run unless it was started to
                assert left == ["connection: the process driver is off: start LLMSim with LLMSIM_ALLOW_PROCESS=1 to allow it"], left
            else:
                assert left and all("still has <" in p for p in left), (system["id"], conn["driver"], left)
    # every platform named in the request is reachable over A2A, the one protocol they share
    for sid in ("salesforce", "sap", "servicenow", "azure-foundry", "custom"):
        assert "a2a" in [c["driver"] for c in next(s for s in body["systems"] if s["id"] == sid)["connections"]], sid


def case_an_agent_is_saved_only_when_what_it_says_can_be_used(sim):
    fa = sim.agent
    good = {"label": "Support (staging)", "system": "custom", "description": "The support agent", "tags": ["support"], "connection": http_connection(fa)}
    assert problems(sim, dict(good, id="support")) == []
    assert problems(sim, dict(good, id="support", system="oracle")) == ["system is one of salesforce, sap, servicenow, azure-foundry, custom"]
    assert problems(sim, dict(good, id="support", system="servicenow")) == ["ServiceNow is reached by a2a"]
    assert problems(sim, dict(good, id="support", connection={"driver": "http"})) == ["connection: http needs url"]
    assert problems(sim, dict(good, id="support", connection=dict(http_connection(fa), name="support"))) == ["connection.name belongs to a scenario, not to the agent"]
    assert problems(sim, dict(good, id="support", connection=dict(http_connection(fa), credential="nobody"))) == \
        ["connection.credential: there is no stored credential called nobody"]
    assert problems(sim, dict(good, id="support", owner="me")) == ["unknown field 'owner'"]
    assert sim.api("PUT", "/api/agents/support", dict(good, connection={"driver": "a2a", "url": "https://<agent-address>"}))[0] == 400
    put(sim, "/api/agents/support", good)
    row = next(a for a in sim.api("GET", "/api/agents")[1]["agents"] if a["id"] == "support")
    assert (row["system"], row["systemName"], row["driver"], row["status"]) == ("custom", "Custom", "http", "not tested"), row
    assert sim.api("GET", "/api/agents/support")[1]["agent"]["connection"]["url"] == fa.url + "/chat"
    assert sim.api("GET", "/api/agents/nobody")[0] == 404


def case_a_connection_test_says_which_step_is_wrong(sim):
    """"It does not work" is three different problems: the credential, the address, or the agent. A test
    walks them in order and stops at the first that is wrong, so a person fixes the right thing."""
    peer = FakeA2A()
    try:
        put(sim, "/api/credentials/peer-token", {"type": "bearer", "token": peer.token})
        put(sim, "/api/credentials/wrong-token", {"type": "bearer", "token": "not-the-token-at-all"})
        put(sim, "/api/agents/peer", {"system": "custom", "connection": {"driver": "a2a", "url": peer.url, "credential": "peer-token", "pollMs": 20}})
        status, body = sim.api("POST", "/api/agents/peer/test", {})
        test = body["test"]
        assert status == 200 and test["ok"] and test["status"] == "connected", test
        assert [(s["step"], s["status"]) for s in test["steps"]] == [("credential", "ok"), ("connection", "ok"), ("message", "ok")], test["steps"]
        assert "names it Fake" in test["steps"][1]["detail"] and test["reply"] == "Hello from the fake agent.", test
        assert peer.token not in json.dumps(body), "the credential is in what the test answered"
        row = next(a for a in sim.api("GET", "/api/agents")[1]["agents"] if a["id"] == "peer")
        assert row["status"] == "connected" and row["checkedAt"], row

        # without a message nothing is said to the agent, and for A2A the card alone shows it is there
        sent = len(peer.log)
        quiet = sim.api("POST", "/api/agents/peer/test", {"say": ""})[1]["test"]
        assert quiet["ok"] and [s["step"] for s in quiet["steps"]] == ["credential", "connection"] and quiet["reply"] is None, quiet
        assert len(peer.log) == sent, "a test that was told to say nothing sent the agent a message"

        # the wrong credential: the card is public on this agent, so it is the message that is refused
        put(sim, "/api/agents/peer", {"system": "custom", "connection": {"driver": "a2a", "url": peer.url, "credential": "wrong-token"}})
        assert next(a for a in sim.api("GET", "/api/agents")[1]["agents"] if a["id"] == "peer")["status"] == "not tested", "a changed connection kept its old result"
        bad = sim.api("POST", "/api/agents/peer/test", {})[1]["test"]
        assert not bad["ok"] and bad["status"] == "failing" and bad["steps"][-1]["step"] == "message" and "HTTP 401" in bad["steps"][-1]["detail"], bad
        # a credential that is not stored never gets as far as the agent
        gone = sim.api("POST", "/api/agents/test", {"agent": {"id": "x", "system": "custom", "connection": {"driver": "a2a", "url": peer.url}}, "say": ""})[1]["test"]
        assert gone["steps"][0] == {"step": "credential", "status": "warn", "detail": "No credential is named, so nothing is sent to say who is calling."}, gone
        # nothing at the address
        dead = sim.api("POST", "/api/agents/test", {"agent": {"id": "x", "system": "custom", "connection": {"driver": "a2a", "url": "http://127.0.0.1:9"}}})[1]["test"]
        assert not dead["ok"] and dead["steps"][-1]["step"] == "connection" and "could not reach" in dead["steps"][-1]["detail"], dead
        # an agent that is only an address cannot be known without a message, and says so
        fa = sim.agent
        only = sim.api("POST", "/api/agents/test", {"agent": {"id": "x", "system": "custom", "connection": http_connection(fa)}, "say": ""})[1]["test"]
        assert only["ok"] and only["status"] == "unverified" and only["steps"][-1]["status"] == "warn", only
    finally:
        peer.stop()


def case_an_azure_foundry_agent_is_reached_over_a2a_with_an_entra_token(sim):
    """Foundry keeps the agent card somewhere the specification does not name, behind the same Entra token
    as everything else, and issues that token only for its own scope. A driver that looked in the usual
    place would report an agent that is there as missing."""
    az = FakeFoundry()
    try:
        cred = {"type": "oauth_client_credentials", "tokenUrl": az.url + "/tenant-1/oauth2/v2.0/token", "clientId": az.client_id, "clientSecret": az.client_secret}
        put(sim, "/api/credentials/entra", dict(cred, scope="https://ai.azure.com/.default"))
        put(sim, "/api/credentials/entra-graph", dict(cred, scope="https://graph.microsoft.com/.default"))
        system = next(s for s in sim.api("GET", "/api/agents")[1]["systems"] if s["id"] == "azure-foundry")
        a2a = next(c for c in system["connections"] if c["driver"] == "a2a")
        assert a2a["defaults"]["card"] == "agentCard/v0.3" and a2a["credential"]["scope"] == "https://ai.azure.com/.default", a2a
        conn = dict(a2a["defaults"], driver="a2a", url=az.url + az.A2A, credential="entra")
        put(sim, "/api/agents/helper", {"label": "Helper", "system": "azure-foundry", "connection": conn})
        test = sim.api("POST", "/api/agents/helper/test", {"say": "Hello"})[1]["test"]
        assert test["status"] == "connected" and "names it helper" in test["steps"][1]["detail"] and "General Q&A" in test["steps"][1]["detail"], test
        assert test["reply"] == "Foundry helper, turn 1: you said Hello", test
        sent = [r for r in az.seen if r.get("path") == az.A2A]
        assert sent and sent[-1]["version"] == "0.3" and sent[-1]["auth"] == "Bearer " + az.TOKEN, sent
        assert not [r for r in az.seen if ".well-known" in r.get("path", "")], "it looked for the card where Foundry does not keep it"
        assert az.TOKEN not in json.dumps(test) and az.client_secret not in json.dumps(sim.api("GET", "/api/agents/helper")[1])

        # a conversation of two turns stays one conversation
        result = sim.api("POST", "/api/scenarios/run", {"scenario": {
            "id": "foundry-two-turns", "agent": {"use": "helper"},
            "turns": [{"say": "first", "expect": {"contains": "turn 1"}}, {"say": "second", "expect": {"contains": "turn 2: you said second"}}], "checks": []}})[1]["result"]
        assert result["verdict"] == "pass" and result["agentRefs"] == ["helper"], result

        # without the card's address the agent looks absent; a token for another service is not issued
        nocard = sim.api("POST", "/api/agents/test", {"agent": {"id": "x", "system": "azure-foundry",
                                                                "connection": {"driver": "a2a", "url": az.url + az.A2A, "credential": "entra"}}, "say": ""})[1]["test"]
        assert nocard["steps"][-1]["status"] == "warn" and "No agent card was found" in nocard["steps"][-1]["detail"], nocard
        wrong = sim.api("POST", "/api/agents/test", {"agent": {"id": "x", "system": "azure-foundry", "connection": dict(conn, credential="entra-graph")}})[1]["test"]
        assert not wrong["ok"] and wrong["steps"] == [wrong["steps"][0]] and wrong["steps"][0]["step"] == "credential" and "could not get a token" in wrong["steps"][0]["detail"], wrong
    finally:
        az.stop()


def case_the_responses_api_is_a_way_to_reach_an_agent(sim):
    az = FakeFoundry()
    try:
        put(sim, "/api/credentials/entra2", {"type": "oauth_client_credentials", "tokenUrl": az.url + "/tenant-1/oauth2/v2.0/token",
                                            "clientId": az.client_id, "clientSecret": az.client_secret, "scope": "https://ai.azure.com/.default"})
        conn = {"driver": "responses", "url": az.url + az.OPENAI, "credential": "entra2", "extra": {"agent_reference": {"type": "agent_reference", "name": "helper"}}}
        put(sim, "/api/agents/helper-responses", {"system": "azure-foundry", "connection": conn})
        assert sim.api("POST", "/api/agents/helper-responses/test", {})[1]["test"]["reply"] == "Turn 1: you said Hello"

        def run(connection, turns):
            return sim.api("POST", "/api/scenarios/run", {"scenario": {"id": "responses", "agent": dict(connection, name="helper"),
                                                                       "turns": [{"say": t} for t in turns], "checks": [{"type": "run", "cond": {"field": "turns", "op": "gte", "value": 1}}]}})[1]["result"]
        two = run(conn, ["first", "second"])
        assert [t["reply"].split(":")[0] for t in two["turns"]] == ["Turn 1", "Turn 2 after " + two["turns"][0]["state"]["response"]], two["turns"]
        kept = run(dict(conn, chain="conversation"), ["first", "second"])
        assert "Turn 2 in conv_" in kept["turns"][1]["reply"], kept["turns"]
        alone = run(dict(conn, chain="none"), ["first", "second"])
        assert [t["reply"].split(":")[0] for t in alone["turns"]] == ["Turn 1", "Turn 1"], alone["turns"]
        versioned = run(dict(conn, query={"api-version": "v1"}), ["first"])
        assert versioned["verdict"] == "pass" and [r for r in az.seen if r.get("query") == "api-version=v1"], az.seen[-2:]
        # what goes wrong is said in the platform's words, and is a failed run rather than a crash
        unnamed = run({"driver": "responses", "url": az.url + az.OPENAI, "credential": "entra2"}, ["first"])
        assert unnamed["verdict"] == "fail" and "agent_reference must name an agent" in unnamed["error"], unnamed
        broken = run(conn, ["please break"])
        assert broken["verdict"] == "fail" and "the agent's tool failed" in broken["error"], broken
        assert sim.api("POST", "/api/drivers/validate", {"driver": "responses", "url": "http://x", "chain": "sideways"})[1]["problems"] == ["chain is previous, conversation or none"]
    finally:
        az.stop()


def case_a_scenario_names_a_saved_agent_or_is_run_on_one(sim):
    """The point of saving an agent: a scenario says which agent, not how to reach it, and the same
    scenario is run on another agent without being edited."""
    fa = sim.agent
    put(sim, "/api/agents/support", {"label": "Support", "system": "custom", "connection": http_connection(fa)})
    put(sim, "/api/agents/support-v2", {"label": "Support, next version", "system": "custom", "connection": http_connection(fa, "/chat-v2")})
    put(sim, "/api/scenarios/refund", refund({"use": "support"}))
    put(sim, "/api/scenarios/refund-elsewhere", refund({"name": "desk", "driver": "http", "url": "http://127.0.0.1:9/nothing-here", "reply": "x"}, "refund-elsewhere"))
    listed = {s["id"]: s for s in sim.api("GET", "/api/scenarios")[1]["scenarios"]}
    assert listed["refund"]["uses"] == ["support"] and listed["refund-elsewhere"]["uses"] == [], listed

    named = sim.api("POST", "/api/scenarios/refund/run", {})[1]["result"]
    assert named["verdict"] == "pass" and named["agentRefs"] == ["support"] and named["agents"] == [{"name": "support", "driver": "http"}], named
    # the scenario's own agent cannot be reached; run on a saved one it passes, and keeps the name its checks know it by
    assert sim.api("POST", "/api/scenarios/refund-elsewhere/run", {})[1]["result"]["verdict"] == "fail"
    moved = sim.api("POST", "/api/scenarios/refund-elsewhere/run", {"agent": "support"})[1]["result"]
    assert moved["verdict"] == "pass" and moved["agents"] == [{"name": "desk", "driver": "http"}] and moved["agentRefs"] == ["support"], moved
    # the same scenario on the version that regressed
    regressed = sim.api("POST", "/api/scenarios/refund/run", {"agent": "support-v2"})[1]["result"]
    assert regressed["verdict"] == "fail" and regressed["agentRefs"] == ["support-v2"], regressed

    # an agent that is not saved is a failed run that says so, not a crash, and installs nothing
    lost = sim.api("POST", "/api/scenarios/refund/run", {"agent": "nobody"})[1]["result"]
    assert lost["verdict"] == "fail" and "there is no saved agent called nobody" in lost["error"] and lost["turns"] == [], lost
    assert sim.api("POST", "/api/scenarios/refund/run", {"agent": 7})[0] == 400
    several = {"id": "two", "agents": [dict(http_connection(fa), name="a", turns=[{"say": "hi"}]), dict(http_connection(fa), name="b", turns=[{"say": "hi"}])],
               "checks": [{"type": "run", "cond": {"field": "turns", "op": "gte", "value": 1}}]}
    one_for_two = sim.api("POST", "/api/scenarios/run", {"scenario": several, "agent": "support"})[1]["result"]
    assert "drives several agents" in one_for_two["error"], one_for_two
    assert sim.api("POST", "/api/scenarios/run", {"scenario": several, "agent": {"c": "support"}})[1]["result"]["error"].startswith("this scenario has no agent called c")

    # with `use`, how it is reached is the saved agent's to say
    said = sim.api("POST", "/api/scenarios/validate", {"scenario": refund({"use": "support", "url": "http://elsewhere"})})[1]["problems"]
    assert said == ["agent: with use, only name is given beside it; the rest is the saved agent's"], said
    assert sim.api("POST", "/api/scenarios/validate", {"scenario": refund({"use": "not an id"})})[1]["problems"] == ["agent.use is the id of a saved agent"]

    # results are found by the agent they were run on
    on = sim.api("GET", "/api/results?agent=support")[1]["results"]
    assert {r["scenario"] for r in on} == {"refund", "refund-elsewhere"} and all(r["agents"] == ["support"] for r in on), on
    assert [r["verdict"] for r in sim.api("GET", "/api/results?agent=support-v2")[1]["results"]] == ["fail"]

    # an agent scenarios name is not removed from under them
    status, body = sim.api("DELETE", "/api/agents/support")
    assert status == 409 and body["usedBy"] == ["refund"] and "refund" in body["message"], body
    assert sim.api("DELETE", "/api/agents/support-v2")[0] == 200 and sim.api("GET", "/api/agents/support-v2")[0] == 404


def case_a_test_run_is_scenarios_on_one_agent_kept_as_one_thing(sim):
    fa = sim.agent
    put(sim, "/api/agents/support", {"label": "Support", "system": "custom", "connection": http_connection(fa)})
    put(sim, "/api/scenarios/refund", refund({"use": "support"}))
    put(sim, "/api/scenarios/refund-polite", refund({"use": "support"}, "refund-polite", tags=["refunds", "tone"],
                                                    turns=[{"say": "I want a refund for order 1182", "expect": {"contains": "apolog"}}]))
    put(sim, "/api/scenarios/greeting", refund({"use": "support"}, "greeting", tags=["smoke"], model={"rules": [{"name": "hi", "when": {"contains": "hello"}, "turns": [{"text": "Hi there."}]}]},
                                               turns=[{"say": "hello", "expect": {"contains": "Hi"}}], checks=[]))
    status, body = sim.api("POST", "/api/testruns", {"agent": "support", "scenarios": ["refund", "refund-polite", "greeting"], "label": "before release"})
    tr = body["testrun"]
    assert status == 200 and tr["status"] == "done" and tr["verdict"] == "fail" and tr["agent"] == "support", tr
    assert [(i["scenario"], i["verdict"]) for i in tr["items"]] == [("refund", "pass"), ("refund-polite", "fail"), ("greeting", "pass")], tr["items"]
    assert tr["summary"] == {"pass": 2, "fail": 1, "no-data": 0, "error": 0, "waiting": 0, "total": 3}, tr["summary"]
    failed = tr["items"][1]
    assert failed["failed"][0]["name"] == "turn 1" and failed["run"], failed
    # each item is an ordinary result, which says which test run it was part of
    result = sim.api("GET", "/api/results/" + failed["run"])[1]["result"]
    assert result["testrun"] == tr["id"] and result["agentRefs"] == ["support"], result

    # chosen by tag, and all of them
    by_tag = sim.api("POST", "/api/testruns", {"agent": "support", "tags": ["smoke"]})[1]["testrun"]
    assert [i["scenario"] for i in by_tag["items"]] == ["greeting"] and by_tag["verdict"] == "pass", by_tag
    everything = sim.api("POST", "/api/testruns", {"agent": "support", "all": True, "wait": False})
    assert everything[0] == 202 and everything[1]["testrun"]["status"] == "running", everything
    rid = everything[1]["testrun"]["id"]
    for _ in range(200):
        doc = sim.api("GET", "/api/testruns/" + rid)[1]["testrun"]
        if doc["status"] == "done":
            break
        time.sleep(0.05)
    assert doc["status"] == "done" and doc["summary"]["total"] >= 3 and doc["summary"]["waiting"] == 0, doc

    # what is refused
    assert sim.api("POST", "/api/testruns", {"agent": "nobody", "all": True})[1]["problems"] == ["there is no saved agent called nobody"]
    assert "say which scenarios in one way" in sim.api("POST", "/api/testruns", {"agent": "support"})[1]["message"]
    assert "say which scenarios in one way" in sim.api("POST", "/api/testruns", {"agent": "support", "all": True, "tags": ["smoke"]})[1]["message"]
    assert sim.api("POST", "/api/testruns", {"agent": "support", "scenarios": ["refund", "no-such"]}) == (404, {"ok": False, "message": "There is no scenario called no-such."})
    assert sim.api("POST", "/api/testruns", {"agent": "support", "tags": ["nothing-has-this"]})[0] == 404

    # the list is newest first and by agent; coverage is each scenario's last run on this agent
    rows = sim.api("GET", "/api/testruns?agent=support")[1]["testruns"]
    assert rows[0]["id"] == rid and {r["id"] for r in rows} >= {tr["id"], by_tag["id"]} and rows[-1]["summary"]["total"] == 3, rows
    assert sim.api("GET", "/api/testruns?agent=someone-else")[1]["testruns"] == []
    cover = {c["scenario"]: c for c in sim.api("GET", "/api/agents/support/coverage")[1]["scenarios"]}
    assert cover["refund"]["last"]["verdict"] == "pass" and cover["refund-polite"]["last"]["verdict"] == "fail" and cover["refund"]["names"], cover
    put(sim, "/api/scenarios/never-run", refund({"name": "x", "driver": "http", "url": "http://127.0.0.1:9", "reply": "x"}, "never-run"))
    fresh = {c["scenario"]: c for c in sim.api("GET", "/api/agents/support/coverage")[1]["scenarios"]}
    assert fresh["never-run"]["last"] is None and fresh["never-run"]["names"] is False, fresh["never-run"]
    assert sim.api("DELETE", "/api/testruns/" + by_tag["id"])[0] == 200 and sim.api("GET", "/api/testruns/" + by_tag["id"])[0] == 404


def case_an_experiment_is_pointed_at_a_saved_agent(sim):
    fa = sim.agent
    put(sim, "/api/agents/support", {"label": "Support", "system": "custom", "connection": http_connection(fa)})
    unreachable = refund({"name": "support", "driver": "http", "url": "http://127.0.0.1:9", "reply": "x"})
    put(sim, "/api/datasets/refunds-on-agents", {"items": [{"id": "over-limit", "scenario": unreachable}]})
    as_written = sim.api("POST", "/api/experiments/run", {"dataset": "refunds-on-agents"})[1]["experiment"]
    assert as_written["items"][0]["status"] == "fail", as_written
    on_agent = sim.api("POST", "/api/experiments/run", {"dataset": "refunds-on-agents", "agent": "support"})[1]["experiment"]
    assert on_agent["items"][0]["status"] == "pass" and on_agent["agentOverride"] == "support", on_agent
    assert "agent is the id of a saved agent" in sim.api("POST", "/api/experiments/run", {"dataset": "refunds-on-agents", "agent": 5})[1]["message"]


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
