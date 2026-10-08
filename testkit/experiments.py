"""Experiments: one version of an agent against one version of a dataset, kept, and compared.

A single run says whether an agent passed once. What a team needs to know is whether the agent got
worse since last week, and where. That needs three things this module adds to the runner:

  repetition   a real model does not answer the same way twice, so a case is run N times and passes
               when at least `required` of them do. One that passes some and fails others is FLAKY: it
               is reported as that, never as a pass
  an experiment every case of a dataset version, run, with how many times it passed, what it cost, how long
               it took, and the run ids to look at. Kept
  a comparison two experiments, case by case: what regressed, what was fixed, what got flaky, what is new
               — and a gate, which is the comparison with a verdict a CI job can act on

The simulated tier makes this free and exact: the model is a script or a recording, so a regression is
the agent's, not the model's mood. The real tier runs the same dataset against a real model, when asked.
"""

import copy
import threading
import time

from . import checks, core, datasets, fakes, rest_area, runner
from .core import fail, ok
from .store import store

JOBS = {}
_LOCK = threading.Lock()
STATUS_RANK = {"pass": 0, "flaky": 1, "no-data": 2, "fail": 3, "error": 3}


def merge_agent(scenario, override):
    """The scenario with its agent configuration overridden: how a NEW VERSION of the agent is tried
    against the same cases. {"url": …} for a single agent, or {"<name>": {…}} for several."""
    if not override:
        return scenario
    sc = copy.deepcopy(scenario)
    if "agents" in sc:
        sc["agents"] = [dict(a, **(override.get(a["name"]) or {})) for a in sc["agents"]]
    else:
        sc["agent"] = dict(sc["agent"], **override)
    return sc


def run_case(scenario, n, required, options, base_host, seed=None, item_id=""):
    """The result of running one case N times: status, passes, and what it cost."""
    runs, verdicts, failures, tokens, seconds = [], [], [], [], []
    for i in range(n):
        opts = dict(options)
        if seed is not None:
            opts["seed"] = "%s:%s:%d" % (seed, item_id, i)
        result = runner.run_scenario(scenario, opts, base_host)
        runs.append(result["run"])
        verdicts.append(result["verdict"])
        seconds.append(result.get("durationS") or 0)
        tokens.append(checks.RunCtx(result["run"]).usage()["total"])
        if result["verdict"] != "pass" and len(failures) < 3:
            bad = [c for c in result["checks"] if c["status"] != "pass"]
            failures.append({"run": result["run"], "error": result.get("error"), "checks": [{"name": c["name"], "status": c["status"], "detail": c["detail"]} for c in bad[:3]]})
    passes = verdicts.count("pass")
    if passes >= required:
        status = "pass" if passes == n else "flaky"
    elif all(v == "no-data" for v in verdicts):
        status = "no-data"
    else:
        status = "fail"
    return {"status": status, "n": n, "required": required, "passes": passes, "verdicts": verdicts, "runs": runs, "failures": failures,
            "tokens": round(sum(tokens) / len(tokens)) if tokens else 0, "durationS": round(sum(seconds) / len(seconds), 3) if seconds else 0}


def summarize(items):
    counts = {k: sum(1 for i in items if i["status"] == k) for k in ("pass", "flaky", "fail", "no-data", "error")}
    return counts, ("fail" if counts["fail"] or counts["error"] or counts["no-data"] else "pass"), \
        round((counts["pass"] + counts["flaky"]) / len(items), 4) if items else 0


def validate_spec(spec):
    problems = []
    if datasets.parse_ref(spec.get("dataset"))[0] is None:
        problems.append("dataset is a name, name@version or name:tag")
    if spec.get("tier", "simulated") not in ("simulated", "real"):
        problems.append("tier is simulated or real")
    if "repeat" in spec:
        problems += datasets.validate_repeat(spec["repeat"] if isinstance(spec["repeat"], dict) else {"n": spec["repeat"]})
    if spec.get("concurrency", 1) not in range(1, 9):
        problems.append("concurrency is 1 to 8")
    if spec.get("agent") is not None and not isinstance(spec["agent"], dict):
        problems.append("agent is the fields to override in each scenario's agent")
    for k in spec:
        if k not in {"dataset", "label", "tier", "repeat", "seed", "split", "tags", "items", "agent", "concurrency", "wait", "keepForBaseline"}:
            problems.append("unknown field %r" % k)
    return problems


def run_experiment(spec, base_host, exp_id=None):
    name, number, tag = datasets.parse_ref(spec["dataset"])
    number, ds = datasets.version(name, number, tag)
    exp_id = exp_id or core.new_id("x-", 8)
    exp = {"id": exp_id, "label": spec.get("label", ""), "dataset": name, "version": number, "tier": spec.get("tier", "simulated"), "seed": spec.get("seed"),
           "agentOverride": spec.get("agent") or {}, "startedAt": core.now(), "status": "running", "items": []}
    store().put("experiments", exp_id, exp)
    items = datasets.select(ds["items"], spec.get("split"), spec.get("tags"), spec.get("items"))
    rep = spec.get("repeat")
    rep = {"n": rep} if isinstance(rep, int) else rep

    def one(item):
        sc = merge_agent(item["scenario"], spec.get("agent"))
        r = rep or item.get("repeat") or sc.get("repeat") or {"n": 1}       # an experiment's own repeat wins over the case's
        n = r.get("n", 1)
        row = run_case(sc, n, min(r.get("required", n), n), {"tier": exp["tier"], "label": exp_id}, base_host, spec.get("seed"), item["id"])
        row.update(id=item["id"], tags=item.get("tags", []), split=item.get("split"))
        return row

    workers = spec.get("concurrency", 1) if spec.get("seed") is None else 1
    if workers == 1:
        for it in items:
            exp["items"].append(one(it))
            store().put("experiments", exp_id, exp)
    else:
        rows, lock = {}, threading.Lock()
        pending = list(items)

        def worker():
            while True:
                with lock:
                    if not pending:
                        return
                    it = pending.pop(0)
                row = one(it)
                with lock:
                    rows[it["id"]] = row
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        exp["items"] = [rows[i["id"]] for i in items if i["id"] in rows]
    exp["summary"], exp["verdict"], exp["passRate"] = summarize(exp["items"])
    exp["finishedAt"] = core.now()
    exp["durationS"] = round(exp["finishedAt"] - exp["startedAt"], 2)
    exp["status"] = "done"
    exp["report"] = report(exp)
    store().put("experiments", exp_id, exp)
    return exp


def report(exp):
    lines = ["%s  experiment %s: %s v%s on the %s tier — %d of %d case(s) passed (%s)" % (
        exp["verdict"].upper(), exp["id"], exp["dataset"], exp["version"], exp["tier"], exp["summary"]["pass"] + exp["summary"]["flaky"], len(exp["items"]),
        ", ".join("%d %s" % (v, k) for k, v in exp["summary"].items() if v))]
    for it in exp["items"]:
        if it["status"] != "pass":
            bad = (it["failures"][0]["checks"][0]["detail"] if it["failures"] and it["failures"][0]["checks"] else (it["failures"][0].get("error") if it["failures"] else "")) or ""
            lines.append("  %-8s %s (%d/%d passed) %s" % (it["status"], it["id"], it["passes"], it["n"], bad[:140]))
    return "\n".join(lines)


# -- comparing ------------------------------------------------------------------------------------------------------------

def compare(base, cand, strict=False):
    b = {i["id"]: i for i in base["items"]}
    c = {i["id"]: i for i in cand["items"]}
    out = {"regressed": [], "degraded": [], "fixed": [], "unchanged": [], "new": [], "removed": []}
    for cid, now in c.items():
        was = b.get(cid)
        if was is None:
            out["new"].append({"id": cid, "status": now["status"]})
            continue
        rank_was, rank_now = STATUS_RANK[was["status"]], STATUS_RANK[now["status"]]
        entry = {"id": cid, "was": was["status"], "now": now["status"], "passes": "%d/%d → %d/%d" % (was["passes"], was["n"], now["passes"], now["n"]),
                 "detail": ((now["failures"][0]["checks"][0]["detail"] if now["failures"] and now["failures"][0]["checks"] else (now["failures"][0].get("error") if now["failures"] else "")) or "")[:200],
                 "tokens": now["tokens"] - was["tokens"], "durationS": round(now["durationS"] - was["durationS"], 3)}
        if was["status"] in ("pass", "flaky") and now["status"] in ("fail", "error", "no-data"):
            out["regressed"].append(entry)
        elif was["status"] == "pass" and now["status"] == "flaky":
            out["degraded"].append(entry)
        elif rank_now < rank_was:
            out["fixed"].append(entry)
        else:
            out["unchanged"].append(entry)
    out["removed"] = [{"id": i, "was": b[i]["status"]} for i in b if i not in c]
    verdict = "fail" if out["regressed"] or (strict and out["degraded"]) else "pass"
    return dict(out, verdict=verdict, baseline=base["id"], candidate=cand["id"], tokens=cand_total(cand) - cand_total(base),
                durationS=round(sum(i["durationS"] for i in cand["items"]) - sum(i["durationS"] for i in base["items"]), 2))


def cand_total(exp):
    return sum(i["tokens"] for i in exp["items"])


def compare_report(cmp):
    lines = ["%s  %s against baseline %s: %d regressed, %d degraded, %d fixed, %d unchanged, %d new, %d removed" % (
        cmp["verdict"].upper(), cmp["candidate"], cmp["baseline"], len(cmp["regressed"]), len(cmp["degraded"]), len(cmp["fixed"]),
        len(cmp["unchanged"]), len(cmp["new"]), len(cmp["removed"]))]
    for label in ("regressed", "degraded", "fixed"):
        for e in cmp[label]:
            lines.append("  %-9s %s: %s → %s (%s) %s" % (label, e["id"], e["was"], e["now"], e["passes"], e.get("detail", "") if label != "fixed" else ""))
    for e in cmp["new"]:
        lines.append("  new       %s: %s" % (e["id"], e["status"]))
    for e in cmp["removed"]:
        lines.append("  removed   %s (was %s)" % (e["id"], e["was"]))
    lines.append("  tokens %+d, time %+.1fs" % (cmp["tokens"], cmp["durationS"]))
    return "\n".join(lines)


def junit(exp, cmp=None):
    from xml.sax.saxutils import escape, quoteattr
    cases, failures = [], 0
    regressed = {e["id"]: e for e in (cmp or {}).get("regressed", [])}
    for it in exp["items"]:
        body = ""
        if cmp is not None and it["id"] in regressed:
            failures += 1
            body = "<failure message=%s>regressed: %s</failure>" % (quoteattr("passed on the baseline, now %s" % it["status"]), escape(regressed[it["id"]].get("detail", "")))
        elif cmp is None and it["status"] in ("fail", "error", "no-data"):
            failures += 1
            body = "<failure message=%s/>" % quoteattr("%s (%d/%d)" % (it["status"], it["passes"], it["n"]))
        elif it["status"] == "flaky":
            body = "<system-out>flaky: %d of %d runs passed</system-out>" % (it["passes"], it["n"])
        cases.append("<testcase classname=%s name=%s time=\"%s\">%s</testcase>" % (quoteattr(exp["dataset"]), quoteattr(it["id"]), it["durationS"], body))
    return '<?xml version="1.0" encoding="UTF-8"?>\n<testsuite name=%s tests="%d" failures="%d" time="%s">\n%s\n</testsuite>\n' % (
        quoteattr("%s@%s" % (exp["dataset"], exp["version"])), len(exp["items"]), failures, exp.get("durationS", 0), "\n".join(cases))


def baseline_of(dataset):
    exp_id = store().get("baselines", dataset)
    return store().get("experiments", exp_id["experiment"]) if exp_id else None


# -- REST ---------------------------------------------------------------------------------------------------------------------

@rest_area("experiments")
def _rest(method, rest, body, query, host):
    st = store()
    if method == "GET" and not rest:
        rows = [{"id": i, "label": d.get("label"), "dataset": d["dataset"], "version": d["version"], "tier": d["tier"], "status": d["status"], "verdict": d.get("verdict"),
                 "summary": d.get("summary"), "passRate": d.get("passRate"), "finishedAt": d.get("finishedAt")} for i, d, _ in reversed(st.docs("experiments"))]
        want = (query.get("dataset") or [None])[0]
        return ok({"experiments": [r for r in rows if not want or r["dataset"] == want][:100]})
    if method == "POST" and rest == ["run"]:
        spec = body or {}
        problems = validate_spec(spec)
        if not problems and not datasets.version(*datasets.parse_ref(spec["dataset"]))[1]:
            problems.append("there is no dataset %s" % spec["dataset"])
        if problems:
            return fail(400, "That experiment has %d problem(s): %s" % (len(problems), "; ".join(problems[:4])), problems=problems)
        if spec.get("wait") is False:
            exp_id = core.new_id("x-", 8)
            threading.Thread(target=run_experiment, args=(spec, host, exp_id), daemon=True).start()
            return 202, {"ok": True, "experiment": exp_id, "status": "running", "message": "Poll GET /api/experiments/%s." % exp_id}
        exp = run_experiment(spec, host)
        out = {"experiment": exp, "report": exp["report"], "junit": junit(exp)}
        if spec.get("baseline") or (spec.get("gate")):
            pass
        return ok(out)
    if method == "GET" and len(rest) == 3 and rest[1] == "compare":
        a, b = st.get("experiments", rest[0]), st.get("experiments", rest[2])
        if not a or not b:
            return fail(404, "Both experiments must exist.")
        cmp = compare(a, b, (query.get("strict") or ["0"])[0] == "1")
        return ok({"comparison": cmp, "report": compare_report(cmp)})
    if len(rest) >= 1 and core.valid_id(rest[0]):
        exp = st.get("experiments", rest[0])
        if not exp:
            return fail(404, "There is no experiment called %s." % rest[0])
        if method == "GET" and len(rest) == 1:
            return ok({"experiment": exp})
        if method == "DELETE" and len(rest) == 1:
            return ok(removed=st.drop("experiments", rest[0]))
        if method == "POST" and rest[1:] == ["baseline"]:
            st.put("baselines", exp["dataset"], {"experiment": exp["id"], "since": core.now()})
            return ok({"dataset": exp["dataset"], "baseline": exp["id"]})
        if method == "POST" and rest[1:] == ["gate"]:
            b = body or {}
            base = st.get("experiments", b["baseline"]) if b.get("baseline") else baseline_of(exp["dataset"])
            if not base:
                return fail(400, "There is no baseline for %s: mark an experiment as one (POST /api/experiments/<id>/baseline) or give baseline." % exp["dataset"])
            cmp = compare(base, exp, bool(b.get("strict")))
            floor = b.get("minPassRate")
            verdict = cmp["verdict"]
            if floor is not None and exp.get("passRate", 0) < floor:
                verdict = "fail"
                cmp["below"] = "the pass rate is %.2f, below the floor of %.2f" % (exp.get("passRate", 0), floor)
            gate = dict(cmp, verdict=verdict)
            return ok({"gate": gate, "report": compare_report(gate) + ("\n  " + cmp["below"] if cmp.get("below") else ""), "junit": junit(exp, gate)})
    return fail(404, "No such route: %s /api/experiments/%s" % (method, "/".join(rest)))
