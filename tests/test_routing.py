#!/usr/bin/env python3
"""Routing over the API, and a route held for a while and given back.

    python3 tests/test_routing.py

The default route is somebody's standing choice. A test that scripts the model needs the
script to answer while it runs, and has no business changing that choice. So it holds the
route and releases it, and everything here is about the ways that could leave a person's
LLMSim on the wrong route afterwards: a caller that never releases, one that crashes, two
that want different things, a restart in the middle.

The route that is "somewhere else" in these cases is Anthropic with no key saved. It needs
no network, and a request sent there fails, which is how a case can tell which route answered.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kit import Sim, run_cases  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def routing(sim):
    status, body = sim.api("GET", "/api/routing")
    assert status == 200, body
    return body


def answers(sim, model="gpt-5"):
    """Whether the script answered: a request on the Anthropic route fails, since no key is saved."""
    return sim.chat("hello there", model=model)[0] == 200


def hold(sim, default="anthropic", by="a test", **more):
    return sim.api("POST", "/api/routing/hold", dict({"default": default, "by": by}, **more))


def clear(sim):
    sim.api("DELETE", "/api/routing/hold")
    sim.api("PUT", "/api/routing", {"default": "scripted", "models": {}})


def case_routing_is_read_and_changed_through_the_api(sim):
    clear(sim)
    r = routing(sim)
    assert (r["default"], r["effective"], r["models"], r["hold"]) == ("scripted", "scripted", {}, None), r
    assert r["routes"] == ["scripted", "relay", "anthropic"], r
    status, body = sim.api("PUT", "/api/routing", {"default": "anthropic", "models": {"gpt-5": "scripted", "gpt-5-mini": "anthropic"}})
    # a model set to what the default already is is not an override, and is not kept as one
    assert status == 200 and (body["default"], body["models"]) == ("anthropic", {"gpt-5": "scripted"}), body
    assert answers(sim, "gpt-5") and not answers(sim, "gpt-5-mini")
    assert sim.api("GET", "/healthz")[1]["mode"] == "anthropic"
    # it is the same setting the console saves, and it is kept
    assert sim.api("GET", "/api/ui/state")[1]["config"]["routing"] == {"default": "anthropic", "models": {"gpt-5": "scripted"}}
    assert json.load(open(sim.env["LLMSIM_CONFIG"]))["routing"]["default"] == "anthropic"
    for bad, said in (({"default": "claude"}, "routing.default must be one of"), ({"models": {"gpt-5": "nowhere"}}, "gpt-5: must be one of"),
                      ({"models": ["gpt-5"]}, "routing.models is {model: route}"), ({"upstream": {}}, "Not understood: upstream")):
        status, body = sim.api("PUT", "/api/routing", bad)
        assert status == 400 and said in body["message"], (bad, body)
    assert routing(sim)["default"] == "anthropic", "a refused change changed something"
    clear(sim)


def case_a_hold_changes_what_answers_and_not_what_was_chosen(sim):
    clear(sim)
    assert answers(sim)
    status, body = hold(sim, seconds=120)
    assert status == 200 and "goes back to scripted when released" in body["message"], body
    h = body["hold"]
    assert (h["default"], h["by"], h["everyModel"]) == ("anthropic", "a test", False) and 100 < h["until"] - time.time() <= 120, h
    assert (body["default"], body["effective"]) == ("scripted", "anthropic"), body
    assert not answers(sim), "the hold did not change which route answers"
    health = sim.api("GET", "/healthz")[1]
    assert health["mode"] == "anthropic" and health["hold"]["by"] == "a test", health
    assert sim.api("GET", "/api/ui/state")[1]["hold"]["id"] == h["id"]
    # what was chosen is untouched, in memory and on disk: there is nothing to put back
    assert sim.api("GET", "/api/ui/state")[1]["config"]["routing"]["default"] == "scripted"
    assert json.load(open(sim.env["LLMSIM_CONFIG"]))["routing"]["default"] == "scripted"
    status, body = sim.api("DELETE", "/api/routing/hold/" + h["id"])
    assert status == 200 and body["released"] is True and body["hold"] is None and body["effective"] == "scripted", body
    assert answers(sim)
    # releasing what is already released is not an error: the caller wanted it gone, and it is
    assert sim.api("DELETE", "/api/routing/hold/" + h["id"])[1]["released"] is False


def case_a_hold_nobody_releases_runs_out_by_itself(sim):
    """The caller that crashed. Its hold is not left in force for a person to find a day later."""
    clear(sim)
    assert hold(sim, seconds=1)[0] == 200 and not answers(sim)
    time.sleep(1.3)
    assert answers(sim), "a hold outlived the time it asked for"
    r = routing(sim)
    assert r["hold"] is None and r["effective"] == "scripted", r
    for seconds in (0, 3601, "60", True, None):
        status, body = hold(sim, seconds=seconds)
        assert status == 400 and "seconds is how long the hold may last at most: 1 to 3600" in body["message"], (seconds, body)
    assert routing(sim)["hold"] is None


def case_a_restart_gives_the_route_back(sim):
    clear(sim)
    assert hold(sim, seconds=3600)[0] == 200 and routing(sim)["effective"] == "anthropic"
    sim.restart()
    r = routing(sim)
    assert r["hold"] is None and (r["default"], r["effective"]) == ("scripted", "scripted"), r
    assert answers(sim)


def case_two_callers_cannot_both_hold_it(sim):
    clear(sim)
    first = hold(sim, "anthropic", "suite A", seconds=60)[1]["hold"]
    status, body = hold(sim, "anthropic", "suite B", seconds=60)
    assert status == 409 and "already held on anthropic by suite A" in body["message"] and body["hold"]["id"] == first["id"], body
    assert routing(sim)["hold"]["id"] == first["id"], "the second caller took it anyway"
    # the one who holds it may hold it again, for longer or on something else
    again = hold(sim, "anthropic", "suite A", seconds=600)[1]["hold"]
    assert again["id"] != first["id"] and again["default"] == "anthropic" and again["until"] - time.time() > 500, again
    # a release names the hold, so the caller whose hold was replaced does not end somebody else's
    status, body = sim.api("DELETE", "/api/routing/hold/" + first["id"])
    assert status == 409 and "suite A holds the default route now" in body["message"] and routing(sim)["hold"]["id"] == again["id"], body
    # a person at the console can always let go of it
    assert sim.api("DELETE", "/api/routing/hold")[1]["released"] is True and routing(sim)["hold"] is None


def case_a_hold_leaves_a_models_own_route_alone_unless_asked(sim):
    clear(sim)
    assert sim.api("PUT", "/api/routing", {"default": "anthropic", "models": {"gpt-5-mini": "anthropic", "gpt-5": "scripted"}})[0] == 200
    assert sim.api("PUT", "/api/routing", {"default": "scripted", "models": {"gpt-5-mini": "anthropic"}})[0] == 200
    assert answers(sim, "gpt-5") and not answers(sim, "gpt-5-mini")
    assert hold(sim, "scripted", "a test")[0] == 200
    assert not answers(sim, "gpt-5-mini"), "a hold on the default route moved a model that has a route of its own"
    held = hold(sim, "scripted", "a test", everyModel=True)[1]["hold"]
    assert held["everyModel"] is True and answers(sim, "gpt-5-mini") and answers(sim, "gpt-5")
    sim.api("DELETE", "/api/routing/hold/" + held["id"])
    assert not answers(sim, "gpt-5-mini") and routing(sim)["models"] == {"gpt-5-mini": "anthropic"}
    clear(sim)


def case_what_a_hold_refuses(sim):
    clear(sim)
    for body, said in (({"default": "nowhere", "by": "x"}, "default must be one of scripted, relay, anthropic"),
                       ({"by": "x"}, "default must be one of"), ({"default": "scripted"}, "by says who is holding it"),
                       ({"default": "scripted", "by": "  "}, "by says who is holding it"), ({"default": "scripted", "by": "x" * 81}, "by says who is holding it"),
                       ({"default": "scripted", "by": "x", "everyModel": "yes"}, "everyModel is true or false"),
                       ({"default": "scripted", "by": "x", "forever": True}, "Not understood: forever")):
        status, out = sim.api("POST", "/api/routing/hold", body)
        assert status == 400 and said in out["message"], (body, out)
    assert routing(sim)["hold"] is None
    assert sim.api("POST", "/api/routing", {"default": "scripted"})[0] == 404
    assert sim.api("DELETE", "/api/routing")[0] == 404
    assert sim.api("DELETE", "/api/routing/hold/x/y")[0] == 404


def case_the_console_says_when_the_route_is_held_and_lets_go_of_it(sim):
    """Somebody looking at the console during a hold sees answers come from a route they did not choose.
    The page has to say whose doing that is and that it will pass, or it reads as a broken setting."""
    page = open(os.path.join(ROOT, "ui.html")).read()
    assert 'id="held"' in page and "function releaseHold" in page and 'REST("DELETE", "/api/routing/hold")' in page
    assert "HOLD = s.hold || null" in page, "the console does not read the hold from the state it polls"
    assert "HOLD ? HOLD.default : CFG.routing.default" in page, "the overview says the kept route answers while another is held"


if __name__ == "__main__":
    run_cases([v for k, v in list(globals().items()) if k.startswith("case_")])
