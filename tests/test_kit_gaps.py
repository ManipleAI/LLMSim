#!/usr/bin/env python3
"""The parts of the kit the other test files do not reach: cost and per-agent budgets, the assertions check,
streamed replies, the process driver once it is allowed, background and parallel experiments, keeping state,
and the command line's reading commands.

    python3 tests/test_kit_gaps.py
"""

import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kit import Sim  # noqa: E402
from fake_agent import FakeAgent  # noqa: E402
from test_kit_tools import MCP, REFUND_SCRIPT  # noqa: E402
from test_kit_regression import refund_scenario, items as dataset_items  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(sim, sc, **options):
    status, body = sim.api("POST", "/api/scenarios/run", dict({"scenario": sc}, **options))
    assert status == 200, body
    return body["result"]


def evaluate(sim, run_id, *specs):
    status, body = sim.api("POST", "/api/checks/evaluate", {"run": run_id, "checks": list(specs)})
    assert status == 200, body
    return [r["status"] for r in body["results"]], body["results"]


def model_call(agent, i, o, ms=10):
    return {"kind": "model_call", "agent": agent, "data": {"model": "gpt-5", "ms": ms, "response": {"text": "", "toolCalls": [], "usage": {"input": i, "output": o, "cacheRead": 0, "reasoning": 0}}}}


def case_cost_is_a_budget_and_a_missing_price_is_an_error_not_a_pass(sim):
    sim.api("PUT", "/api/mcp/orders", MCP)
    base = refund_scenario(sim.agent)
    cheap = {"type": "budget", "cost": {"max": 100, "prices": {"*": {"input": 1, "output": 1}}}}
    dear = {"type": "budget", "cost": {"max": 0.0001, "prices": {"gpt-5": {"input": 10, "output": 10}}}}
    unpriced = {"type": "budget", "cost": {"max": 1, "prices": {"some-other-model": {"input": 1, "output": 1}}}}
    out = run(sim, dict(base, checks=[cheap, dear, unpriced]))
    assert [c["status"] for c in out["checks"]] == ["pass", "fail", "error"], out["checks"]
    assert "over the limit" in out["checks"][1]["detail"] and "no price for model" in out["checks"][2]["detail"]
    assert sim.api("POST", "/api/checks/validate", {"check": {"type": "budget", "cost": {"max": 1}}})[1]["valid"] is False, "a cost with no prices was accepted"


def case_a_budget_can_be_per_agent_and_names_the_agent_that_broke_it(sim):
    for e in (model_call("a", 100, 50), model_call("b", 900, 400), model_call("b", 10, 10)):
        assert sim.api("POST", "/api/runs/bud1/events", e)[0] == 200
    got, results = evaluate(sim, "bud1", {"type": "budget", "tokens": {"total": 1000}, "scope": "agent"}, {"type": "budget", "tokens": {"total": 1000}},
                            {"type": "budget", "tokens": {"total": 2000}, "scope": "agent"}, {"type": "budget", "latencyMs": {"modelCall": 5}})
    assert got == ["fail", "fail", "pass", "fail"], results
    assert "agent b" in results[0]["detail"] and "agent a" not in results[0]["detail"], results[0]
    assert "total tokens 1470 over" in results[1]["detail"] and "slower than 5ms" in results[3]["detail"]


def case_assertions_over_the_models_requests_are_a_check_of_a_scenario(sim):
    sim.api("PUT", "/api/mcp/orders", MCP)
    base = refund_scenario(sim.agent)
    ok = {"type": "assertions", "assertions": [{"id": "asked", "name": "the model was asked about the order", "each": {"field": "allText", "op": "contains", "value": "order 1182"}}]}
    bad = {"type": "assertions", "assertions": [{"id": "never", "name": "no request mentions the order", "none": {"field": "allText", "op": "contains", "value": "order 1182"}}]}
    out = run(sim, dict(base, checks=[ok, bad]))
    assert [c["status"] for c in out["checks"]] == ["pass", "fail"], out["checks"]
    assert sim.api("POST", "/api/checks/validate", {"check": {"type": "assertions"}})[1]["valid"] is False
    missing = run(sim, dict(base, checks=[{"type": "assertions", "set": "no-such-set"}]))
    assert missing["checks"][0]["status"] == "error" and "no assertion set" in missing["checks"][0]["detail"]


def case_replies_that_arrive_in_pieces_are_read_whole(sim):
    cfg = {"name": "streamer", "driver": "http", "url": sim.agent.url + "/sse", "replyFrom": "sse", "reply": "delta", "body": {"x": "{{message}}"}}
    out = run(sim, {"id": "sse", "agent": cfg, "turns": [{"say": "go", "expect": {"equals": "Streamed in pieces."}}]})
    assert out["verdict"] == "pass", out
    rules = [{"name": "s", "when": {"field": "userText", "op": "contains", "value": "hello"}, "turns": [{"text": "A reply that is long enough to be sent in several pieces."}]}]
    sim.api("PUT", "/api/credentials/k", {"type": "bearer", "token": "sk-llmsim-000000000000000000000000"})
    oa = {"id": "oa-stream", "agent": {"name": "bot", "driver": "openai", "url": sim.url("/run/oas/agent/bot/openai/v1"), "stream": True, "credential": "k"},
          "model": {"rules": rules}, "turns": [{"say": "hello", "expect": {"equals": "A reply that is long enough to be sent in several pieces."}}]}
    assert run(sim, oa, run="oas")["verdict"] == "pass"
    sim.api("DELETE", "/api/credentials/k")


def case_state_can_be_kept_after_a_run_to_be_looked_at(sim):
    sim.api("PUT", "/api/mcp/orders", MCP)
    sc = refund_scenario(sim.agent)
    gone = run(sim, sc, run="ks-1")
    assert gone["state"]["orders"]["1182"]["status"] == "refunded"
    assert sim.api("GET", "/api/runs/ks-1/state")[1]["from"] == "the result of the run", "state was not purged and then read back from the result"
    kept = run(sim, sc, run="ks-2", keepState=True)
    live = sim.api("GET", "/api/runs/ks-2/state")[1]
    assert live["from"] == "live" and live["state"]["orders"]["1182"]["status"] == "refunded", live
    assert sim.api("DELETE", "/api/runs/ks-2/state")[1]["removed"] >= 1
    assert sim.api("POST", "/api/scenarios/run", {"scenario": sc, "keepState": "yes"})[0] == 400


def case_an_experiment_can_run_in_the_background_and_in_parallel(sim):
    sim.api("PUT", "/api/mcp/orders", MCP)
    sim.api("PUT", "/api/datasets/par", {"items": dataset_items(sim.agent)})
    sequential = sim.api("POST", "/api/experiments/run", {"dataset": "par"})[1]["experiment"]
    parallel = sim.api("POST", "/api/experiments/run", {"dataset": "par", "concurrency": 2})[1]["experiment"]
    assert {i["id"]: i["status"] for i in parallel["items"]} == {i["id"]: i["status"] for i in sequential["items"]} and parallel["verdict"] == "pass"
    status, body = sim.api("POST", "/api/experiments/run", {"dataset": "par", "wait": False})
    assert status == 202 and body["status"] == "running", body
    for _ in range(100):
        got = sim.api("GET", "/api/experiments/%s" % body["experiment"])[1]["experiment"]
        if got["status"] == "done":
            break
        time.sleep(0.1)
    assert got["status"] == "done" and got["verdict"] == "pass" and len(got["items"]) == 2, got
    assert sim.api("POST", "/api/experiments/run", {"dataset": "par", "concurrency": 99})[0] == 400
    assert sim.api("POST", "/api/experiments/run", {"dataset": "par", "surprise": 1})[0] == 400
    sim.api("DELETE", "/api/datasets/par")


def case_the_process_driver_works_once_the_operator_allows_it(sim):
    allowed = Sim(env={"LLMSIM_ALLOW_PROCESS": "1"}).start()
    try:
        assert allowed.api("GET", "/api/drivers")[1]["processAllowed"] is True
        code = "import os,sys; print('%s|%s|%s' % (sys.stdin.read().strip(), os.environ['LLMSIM_RUN'], os.environ['LLMSIM_URL_OPENAI']))"
        sc = {"id": "proc", "agent": {"name": "local", "driver": "process", "command": [sys.executable, "-c", code]},
              "turns": [{"say": "hello", "expect": {"regex": "^hello\\|proc-run\\|http://127.0.0.1:%d/run/proc-run/agent/local/openai/v1$" % allowed.port}}]}
        out = run(allowed, sc, run="proc-run")
        assert out["verdict"] == "pass", out
        failing = dict(sc, id="proc2", agent={"name": "local", "driver": "process", "command": [sys.executable, "-c", "import sys; sys.exit(3)"]})
        out = run(allowed, failing)
        assert out["verdict"] == "fail" and "exited 3" in out["error"], out
        slow = dict(sc, id="proc3", timeoutS=1, agent={"name": "local", "driver": "process", "command": [sys.executable, "-c", "import time; time.sleep(20)"]})
        assert "did not finish" in run(allowed, slow)["error"]
        assert allowed.api("POST", "/api/drivers/validate", {"driver": "process", "command": "not a list"})[1]["valid"] is False
    finally:
        allowed.stop()


def case_the_command_line_reads_results_and_timelines(sim):
    sim.api("PUT", "/api/mcp/orders", MCP)
    out = run(sim, refund_scenario(sim.agent), run="cli-read")
    cli = os.path.join(ROOT, "bin", "llmsim")
    sh = lambda *a: subprocess.run([sys.executable, cli, "--server", sim.base, *a], capture_output=True, text=True)
    res = sh("results", "--scenario", "refund")
    assert res.returncode == 0 and "cli-read" in res.stdout and "pass" in res.stdout, res.stdout
    tl = sh("timeline", "cli-read")
    assert tl.returncode == 0 and "user_turn" in tl.stdout and "tool_call" in tl.stdout and "state_change" in tl.stdout, tl.stdout
    assert sh("timeline", "nothing-here").returncode == 2
    saved = sim.api("PUT", "/api/scenarios/refund", refund_scenario(sim.agent))[0] == 200
    run_saved = sh("run", "--saved", "refund", "--json")
    assert run_saved.returncode == 0 and json.loads(run_saved.stdout)["verdict"] == "pass", run_saved.stdout[:200]
    assert sh("run", "--saved", "nope").returncode == 2


def case_the_console_page_is_valid_javascript_and_names_every_area(sim):
    import re
    import shutil
    page = open(os.path.join(ROOT, "ui.html")).read()
    assert 'data-view="lab"' in page and 'id="v-lab"' in page and "PAGES.lab" in page
    served = sim.call("GET", "/ui")[2]
    assert 'id="v-lab"' in served, "the server does not serve the page"
    for area in ("scenarios", "results", "datasets", "experiments", "mcp", "mocks", "a2a", "a2aproxies", "kitfaults", "judges", "reviews", "variations", "credentials"):
        assert re.search(r"\n  %s: \{ title:" % area, page), "the console has no tab for %s" % area
        assert sim.api("GET", "/api/%s" % area)[0] == 200 or area == "results", area
    node = shutil.which("node")
    if node:
        script = max(re.findall(r"<script>(.*?)</script>", page, re.S), key=len)
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(script)
        check = subprocess.run([node, "--check", fh.name], capture_output=True, text=True)
        os.unlink(fh.name)
        assert check.returncode == 0, check.stderr[:400]


def case_every_console_screen_can_be_reached_and_none_is_a_dead_end(sim):
    """The console is one page of many screens, and the ways it has gone wrong are structural:
    a screen nothing links to, a link to a screen that is not there, a job with no screen, an
    area on the agent-tests page that belongs to no group and so is never drawn."""
    import re
    page = open(os.path.join(ROOT, "ui.html")).read()
    nav = re.findall(r'<button data-view="([a-z]+)" onclick="show\(\'([a-z]+)\'\)"', page)
    assert nav and all(a == b for a, b in nav), "a navigation entry opens a screen other than its own: %s" % nav
    linked = [a for a, _b in nav]
    assert len(linked) == len(set(linked)), "a screen is in the navigation twice: %s" % linked
    views = re.findall(r'<section class="view" id="v-([a-z]+)"', page)
    assert sorted(views) == sorted(linked), "screens and navigation disagree: only as screens %s, only in navigation %s" % (
        sorted(set(views) - set(linked)), sorted(set(linked) - set(views)))
    assert linked[0] == "overview", "the console does not open on the overview"
    assert len(re.findall(r'<section class="view" id="v-[a-z]+">', page)) == 1, "more than one screen is showing before the script runs"

    # Every job the overview offers goes to a screen that exists, and every screen is offered.
    tasks = re.findall(r'\n  \["([a-z]+)", "[^"]+", "[^"]+"\],', page.split("const TASKS = [")[1].split("];")[0])
    assert sorted(tasks) == sorted(v for v in views if v != "overview"), "the overview's jobs and the screens disagree: %s" % tasks

    # A screen that was renamed or folded away is still an address.
    aliases = dict(re.findall(r'(\w+): "(\w+)"', page.split("const VIEW_ALIAS = {")[1].split("}")[0]))
    assert aliases.get("logs") == "evidence" and all(target in views for target in aliases.values()), aliases
    for tab in ("live", "requests", "assertions", "settings"):
        assert 'id="ev-tab-%s"' % tab in page and 'id="ev-%s"' % tab in page, "the requests screen has no %s tab" % tab

    # Every area of the agent-tests page is in a group that is drawn.
    groups = re.findall(r'\n  \["(\w+)", "[^"]+", "[^"]+"\],', page.split("const LAB_GROUPS = [")[1].split("];")[0])
    areas = re.findall(r'\n  (\w+): \{ title: "[^"]+", group: "(\w+)",', page)
    assert len(areas) == 13, "an agent-tests area has no group: %d of 13 have one" % len(areas)
    assert all(g in groups for _a, g in areas) and all(any(g == h for _a, h in areas) for g in groups), (groups, areas)

    # The addresses on the overview are the ones the server answers on.
    for provider in ("anthropic", "openai", "deepseek"):
        assert re.search(r"\n  %s: \{ name:" % provider, page), "the overview does not say how to reach %s" % provider
    assert sim.call("GET", "/ui")[0] == 200


def case_every_agent_test_area_has_a_form_or_a_view_and_they_keep_up_with_the_api(sim):
    """The agent-tests page edits each area with a form made of that area's own fields. A form is
    only as good as its list of what exists, so the lists are held to the API's: a driver, a check
    type or a credential kind the server offers and the console does not know is a field nobody
    can fill in."""
    import re
    page = open(os.path.join(ROOT, "ui.html")).read()
    lab = page.split("const LAB = {")[1].split("\n};")[0]
    areas = re.split(r"\n  (?=\w+: \{ title:)", "\n" + lab)[1:]
    assert len(areas) == 13, len(areas)
    for area in areas:
        name = area.split(":")[0]
        readonly = "readonly: true" in area
        assert ("view: " in area) if readonly else ("form: " in area), "%s is %s and has no %s" % (name, "read" if readonly else "edited", "view" if readonly else "form")
        assert readonly or "put: " in area, "%s has a form and nowhere to save it" % name

    def block(const):
        return page.split("const %s = {" % const)[1].split("\n};")[0]

    drivers = {d["name"] for d in sim.api("GET", "/api/drivers")[1]["drivers"]}
    fields = set(re.findall(r"\n  (\w+): \(p\) =>", block("DRIVER_FIELDS")))
    assert drivers <= fields, "no fields for the driver(s) %s" % sorted(drivers - fields)
    declared = set(re.findall(r"(\w+): \[", page.split("const DRIVER_KEYS = {")[1].split("};")[0]))
    assert drivers <= declared, "DRIVER_KEYS does not list %s, so changing driver would leave its fields behind" % sorted(drivers - declared)
    checks = set(sim.api("GET", "/api/checks")[1]["types"])
    described = set(re.findall(r"(\w+): \"", block("CHECK_HELP")))
    assert checks <= described, "the console cannot say what the check(s) %s do" % sorted(checks - described)

    # The one piece of the form engine that can lose data: writing a value into the document by path.
    node = shutil.which("node")
    if node:
        engine = page[page.index("const getAt = "):page.index("const pj = ")]
        script = engine + """
const eq = (a, b, what) => { if (JSON.stringify(a) !== JSON.stringify(b)) { console.error(what + ': ' + JSON.stringify(a) + ' is not ' + JSON.stringify(b)); process.exit(1); } };
let d = {};
setAt(d, ['agent', 'url'], 'http://x'); eq(d, {agent: {url: 'http://x'}}, 'a value makes the objects it needs');
setAt(d, ['turns', 0, 'say'], 'hi'); eq(d.turns, [{say: 'hi'}], 'a number in the path makes a list');
setAt(d, ['turns', 0, 'expect', 'contains'], 'x'); setAt(d, ['turns', 0, 'expect', 'contains'], undefined);
eq(d.turns, [{say: 'hi'}], 'clearing the only expectation removes expect, and leaves the turn in its list');
setAt(d, ['stop', 'maxTurns'], 3); setAt(d, ['stop', 'maxTurns'], undefined); eq('stop' in d, false, 'an emptied object goes');
setAt(d, ['agent', 'url'], undefined); eq('agent' in d, false, 'up to the document');
setAt(d, ['model', 'rules'], []); eq(d.model, {rules: []}, 'an empty list that was typed is kept');
setAt(d, ['a', 'b'], 0); setAt(d, ['a', 'c'], false); eq(d.a, {b: 0, c: false}, 'zero and false are values');
eq(getAt(d, ['nope', 'deeper']), undefined, 'a missing path reads as nothing');
"""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(script)
        ran = subprocess.run([node, fh.name], capture_output=True, text=True)
        os.unlink(fh.name)
        assert ran.returncode == 0, ran.stderr[:600]


def case_a_scenario_is_drawn_as_a_canvas_that_reaches_every_part_of_it(sim):
    """The canvas is a third view of a scenario beside the form and the JSON: the run as a picture,
    each block one part of the document. A part the canvas has no block for would be a part nobody
    can see from it, so what it draws is held to the validator's own list of a scenario's fields, and
    the setup actions it draws to the kinds the server accepts. The form's parts were made to work at a
    path so the canvas could reuse them; a mock backend's proxy is one of them, and names the real one
    as the server wants it."""
    import re
    page = open(os.path.join(ROOT, "ui.html")).read()
    assert 'id="lab-mode-canvas"' in page and "function cvDraw" in page
    assert "canvas: true" in page.split("const LAB = {")[1].split("\n};")[0].split("\n  results:")[0], "the scenario area has no canvas"
    covers = set(re.findall(r'"(\w+)"', page.split("const CV_COVERS = [")[1].split("]")[0]))
    sys.path.insert(0, ROOT)
    from testkit.scenario import TOP
    assert set(TOP) <= covers, "the canvas has no block for %s" % sorted(set(TOP) - covers)
    _s, body = sim.api("POST", "/api/scenarios/validate", {"scenario": {"id": "x", "agent": {"driver": "http", "url": "http://x"}, "turns": [{"say": "hi"}], "checks": [{"type": "answer", "contains": "x"}], "setup": [{"type": "zzz"}]}})
    said = next(p for p in body["problems"] if "setup[0].type is one of " in p)
    kinds = set(said.split("one of ")[1].split(", "))
    drawn = set(re.findall(r"\n  (\w+): \(", page.split("const CV_SETUP = {")[1].split("\n};")[0]))
    assert kinds <= drawn, "the canvas cannot draw the setup action(s) %s" % sorted(kinds - drawn)
    assert 'proxyFields(p.concat("proxy"), "baseUrl")' in page, "a mock backend's proxy must be saved as baseUrl, which is what the server reads"


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
