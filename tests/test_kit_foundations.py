#!/usr/bin/env python3
"""Phase 0: a run is kept on disk and seen as one timeline.

    python3 tests/test_kit_foundations.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kit import Sim, run_cases, CHAT  # noqa: E402


def timeline(sim, run, **query):
    qs = "&".join("%s=%s" % kv for kv in query.items())
    return sim.api("GET", "/api/runs/%s/timeline%s" % (run, "?" + qs if qs else ""))


def case_a_model_call_in_a_run_is_an_event_with_its_agent(sim):
    for agent in ("planner", "worker"):
        status, _ = sim.chat("write the report", path="/run/f1/agent/%s%s" % (agent, CHAT))
        assert status == 200
    status, body = timeline(sim, "f1")
    assert status == 200 and body["counts"] == {"model_call": 2}, body
    assert [e["agent"] for e in body["events"]] == ["planner", "worker"] and body["agents"] == ["planner", "worker"], body
    first = body["events"][0]
    assert first["seq"] == 1 and first["t"] == 0 and first["data"]["model"] == "gpt-5", first
    assert first["data"]["response"]["text"] and first["label"], first


def case_a_request_with_no_run_is_not_kept(sim):
    before = len(sim.api("GET", "/api/runs")[1]["stored"])
    sim.chat("nobody's run")
    assert len(sim.api("GET", "/api/runs")[1]["stored"]) == before, "an unscoped request created a run"


def case_events_can_be_added_from_outside_with_a_cause(sim):
    sim.chat("go", path="/run/f2/agent/a" + CHAT)
    call = timeline(sim, "f2")[1]["events"][0]["id"]
    status, added = sim.api("POST", "/api/runs/f2/events", {"kind": "tool_call", "agent": "a", "parent": call,
                                                               "data": {"tool": "lookup", "arguments": {"id": 1}, "result": {"ok": True}}})
    assert status == 200 and added["id"] > call, added
    events = timeline(sim, "f2")[1]["events"]
    assert [e["kind"] for e in events] == ["model_call", "tool_call"] and events[1]["parent"] == call, events
    assert "lookup(" in events[1]["label"]
    only = timeline(sim, "f2", kinds="tool_call")[1]["events"]
    assert [e["kind"] for e in only] == ["tool_call"]
    for bad in ({"kind": "nope"}, {"kind": "note", "agent": "a b"}, {"kind": "note", "data": "x"}, {"kind": "note", "parent": "x"}):
        assert sim.api("POST", "/api/runs/f2/events", bad)[0] == 400, bad


def case_a_trace_context_header_puts_requests_in_one_run(sim):
    trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    header = {"traceparent": "00-%s-00f067aa0ba902b7-01" % trace}
    sim.chat("one", path="/agent/a" + CHAT, headers=header)
    sim.chat("two", path="/agent/b" + CHAT, headers=header)
    body = timeline(sim, trace)[1]
    assert body["counts"] == {"model_call": 2} and body["agents"] == ["a", "b"], body
    # an explicit run in the URL still wins over the trace
    sim.chat("three", path="/run/chosen/agent/c" + CHAT, headers=header)
    assert timeline(sim, "chosen")[0] == 200
    # a malformed or all-zero trace names no run
    sim.chat("four", path="/agent/d" + CHAT, headers={"traceparent": "00-%s-00f067aa0ba902b7-01" % ("0" * 32)})
    sim.chat("five", path="/agent/e" + CHAT, headers={"traceparent": "garbage"})
    assert timeline(sim, "0" * 32)[0] == 404


def case_the_run_survives_a_restart(sim):
    sim.chat("before the restart", path="/run/f3/agent/a" + CHAT)
    sim.api("POST", "/api/runs/f3/events", {"kind": "note", "agent": "a", "data": {"text": "kept"}})
    before = timeline(sim, "f3")[1]
    sim.restart()
    after = timeline(sim, "f3")
    assert after[0] == 200, after
    assert [(e["id"], e["kind"], e["agent"]) for e in after[1]["events"]] == [(e["id"], e["kind"], e["agent"]) for e in before["events"]]
    assert after[1]["events"][0]["data"]["ask"] == "before the restart"
    assert any(r["run"] == "f3" for r in sim.api("GET", "/api/runs")[1]["stored"])
    # the file is the owner's alone
    assert oct(os.stat(sim.env["LLMSIM_RUNS_DB"]).st_mode & 0o777) == "0o600"
    # and a run can be forgotten
    assert sim.api("DELETE", "/api/runs/f3")[1]["storedRemoved"] >= 2
    assert timeline(sim, "f3")[0] == 404


def case_what_is_shown_is_redacted_and_what_is_kept_is_not(sim):
    sim.api("PUT", "/api/settings", {"capture": {"redact": {"headers": [], "patterns": [{"pattern": "TOPSECRET\\d+", "replace": "[redacted]"}]}}})
    sim.chat("my code is TOPSECRET42", path="/run/f4" + CHAT)
    shown = timeline(sim, "f4")[1]["events"][0]["data"]
    assert "TOPSECRET42" not in str(shown) and "[redacted]" in str(shown), shown
    sim.api("PUT", "/api/settings", {"capture": {"redact": {"headers": [], "patterns": []}}})


def case_retention_is_configurable_and_validated(sim):
    assert sim.api("PUT", "/api/kit", {"retention": {"maxEventsPerRun": 3}})[0] == 200
    for _ in range(5):
        sim.chat("again", path="/run/f5" + CHAT)
    assert timeline(sim, "f5")[1]["counts"] == {"model_call": 3}, "a run was allowed past its limit"
    for bad in ({"retention": {"maxRuns": 0}}, {"retention": {"nonsense": 1}}, {"timeline": {"recordAll": "yes"}}):
        assert sim.api("PUT", "/api/kit", bad)[0] == 400, bad
    sim.api("PUT", "/api/kit", {"retention": {"maxEventsPerRun": 20000}})
    status, body = sim.api("PUT", "/api/kit", {"timeline": {"recordAll": True}})
    assert body["timeline"]["recordAll"] is True
    sim.chat("unscoped but kept")
    assert timeline(sim, "unscoped")[1]["counts"].get("model_call") == 1
    sim.api("PUT", "/api/kit", {"timeline": {"recordAll": False}})


def case_the_kit_does_not_change_what_a_model_call_returns(sim):
    plain = sim.chat("compare me")[1]["choices"][0]["message"]["content"]
    scoped = sim.chat("compare me", path="/run/f6/agent/a" + CHAT)[1]["choices"][0]["message"]["content"]
    assert plain == scoped


CASES = [v for k, v in list(globals().items()) if k.startswith("case_")]

if __name__ == "__main__":
    run_cases(CASES)
