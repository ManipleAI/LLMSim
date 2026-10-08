"""Judges: a model that decides what a rule cannot.

Some answers cannot be checked by a pattern: was the reply helpful, was the refusal polite, did the agent
contradict itself in turn five. A judge is a model asked to decide, against a rubric, and to say why.

A judge is also a thing that can be wrong, and a wrong judge in a gate is worse than none. So:

  it must give a reason     a verdict with none is an error, not a pass
  it is pinned              the model and a digest of the rubric are recorded with every verdict
  it is calibrated          a NAMED judge is run against examples a person has labelled, and what it agrees
                            on is kept. Below its threshold, or not calibrated for its current rubric and
                            model, it cannot gate a run: its check says so and is not a pass

`judge` takes a rubric. `conversation` is the same with a rubric already written for the two questions that
whole conversations raise: was the goal reached, and was the agent consistent. Pairwise asks a judge which of
two versions answered better, case by case — with the order randomised, because judges favour what they read first.
"""

import json
import re

from . import checks, core, llm, rest_area
from .checks import check, result
from .core import fail, ok
from .store import store

BUILTIN = {
    "goal": "Decide whether the assistant helped the user reach this goal: {goal}\nThe goal is reached only if the conversation shows it was actually done, not merely promised.",
    "consistency": "Decide whether the assistant is consistent across the whole conversation. It FAILS if it contradicts something it said earlier "
                   "(a fact, a number, a decision, or what it did), or says one thing and then does another.",
}
INSTRUCTION = ('Answer with JSON only, in exactly this shape: {"verdict": "pass" or "fail", "reason": "one or two sentences saying why"}. '
               "A verdict without a reason is not accepted.")


def rubric_digest(rubric, model):
    return core.digest([rubric, model], 12)


def transcript_of(ctx, target="conversation", agent=None):
    lines = []
    for e in ctx.events:
        if agent and e["agent"] != agent:
            continue
        d = e["data"]
        if e["kind"] == "user_turn":
            lines.append("User: %s" % d.get("text", ""))
        elif e["kind"] == "agent_reply":
            lines.append("Assistant: %s" % d.get("text", ""))
        elif e["kind"] == "tool_call" and target == "conversation":
            lines.append("[tool %s %s -> %s]" % (d.get("tool"), core.dumps(d.get("arguments") or {})[:120], core.dumps(d.get("result"))[:160]))
        elif e["kind"] == "peer_message" and target == "conversation" and d.get("method") != "reply":
            lines.append("[%s told %s: %s]" % (d.get("from"), d.get("to"), (d.get("text") or "")[:160]))
    if target == "final":
        said = [l for l in lines if l.startswith(("User:", "Assistant:"))]
        lines = said[-2:]
    text = "\n".join(lines)
    return text[-12000:]


def ask(rubric, transcript, model, run, agent="judge"):
    """{verdict, reason} from one judgement. ValueError describes a judge that did not do as asked."""
    prompt = "%s\n\n%s\n\nThe conversation:\n%s" % (rubric, INSTRUCTION, transcript)
    text = llm.chat(model, [{"role": "system", "content": "You are a strict, fair evaluator of an AI assistant. JUDGE"}, {"role": "user", "content": prompt}], run, agent)
    try:
        data = checks.json_in(text)
    except ValueError:
        raise ValueError("the judge did not answer in JSON: %r" % text[:120])
    if not isinstance(data, dict) or data.get("verdict") not in ("pass", "fail"):
        raise ValueError("the judge's verdict is not pass or fail: %r" % text[:120])
    if not isinstance(data.get("reason"), str) or len(data["reason"].strip()) < 3:
        raise ValueError("the judge gave a verdict and no reason, so it is not accepted")
    return {"verdict": data["verdict"], "reason": data["reason"].strip()}


def named(judge_id):
    return store().get("judges", judge_id)


def gating_problem(j):
    """Why a named judge may not gate, or None."""
    cal = j.get("calibration")
    digest = rubric_digest(j["rubric"], j["model"])
    if not cal:
        return "judge %s has not been calibrated against labelled examples" % j["id"]
    if cal["rubricDigest"] != digest:
        return "judge %s was calibrated for an earlier rubric or model; calibrate it again" % j["id"]
    if cal["agreement"] < j.get("minAgreement", 0.8):
        return "judge %s agrees with the human labels on %.0f%%, below its threshold of %.0f%%, so it cannot gate a run" % (j["id"], cal["agreement"] * 100, j.get("minAgreement", 0.8) * 100)
    return None


def validate_judge_check(spec):
    problems = ["unknown field %r" % k for k in spec if k not in {"type", "name", "agent", "rubric", "judge", "model", "target"}]
    if not (spec.get("rubric") or spec.get("judge")):
        problems.append("give rubric (what to decide) or judge (a saved judge)")
    if spec.get("target", "conversation") not in ("conversation", "final"):
        problems.append("target is conversation or final")
    return problems


def _judge(spec, ctx, rubric, model, judge_doc=None):
    if judge_doc is not None:
        problem = gating_problem(judge_doc)
        if problem:
            return result(spec, "no-data", problem)
    text = transcript_of(ctx, spec.get("target", "conversation"), spec.get("agent"))
    if not text.strip():
        return result(spec, "no-data", "the run has no conversation to judge")
    pinned = {"model": model, "rubricDigest": rubric_digest(rubric, model), "judge": (judge_doc or {}).get("id")}
    try:
        got = ask(rubric, text, model, "judge-" + ctx.run)
    except (ValueError, llm.ModelError) as exc:
        return result(spec, "error", str(exc), judged=pinned)
    return result(spec, got["verdict"], got["reason"], [{"reason": got["reason"]}], judged=pinned)


@check("judge", validate_judge_check)
def check_judge(spec, ctx):
    doc = named(spec["judge"]) if spec.get("judge") else None
    if spec.get("judge") and doc is None:
        return result(spec, "error", "there is no judge called %s" % spec["judge"])
    return _judge(spec, ctx, doc["rubric"] if doc else spec["rubric"], spec.get("model") or (doc or {}).get("model") or llm.default_model(), doc)


def validate_conversation(spec):
    problems = ["unknown field %r" % k for k in spec if k not in {"type", "name", "agent", "evaluate", "goal", "judge", "model"}]
    if spec.get("evaluate") not in BUILTIN:
        problems.append("evaluate is one of %s" % ", ".join(sorted(BUILTIN)))
    if spec.get("evaluate") == "goal" and not spec.get("goal"):
        problems.append("goal is what the user was trying to do")
    return problems


@check("conversation", validate_conversation)
def check_conversation(spec, ctx):
    doc = named(spec["judge"]) if spec.get("judge") else None
    rubric = BUILTIN[spec["evaluate"]].format(goal=spec.get("goal", ""))
    model = spec.get("model") or (doc or {}).get("model") or llm.default_model()
    if doc is not None:
        problem = gating_problem(dict(doc, rubric=rubric))
        if problem:
            return result(spec, "no-data", problem)
    return _judge(dict(spec, target="conversation"), ctx, rubric, model)


# -- REST: judges, calibration, pairwise ----------------------------------------------------------------------------------

def validate_judge(j):
    problems = ["unknown field %r" % k for k in j if k not in {"id", "rubric", "model", "minAgreement", "description", "calibration", "version"}]
    if not core.valid_id(j.get("id")):
        problems.append("id is a name")
    if not isinstance(j.get("rubric"), str) or len(j["rubric"]) < 10:
        problems.append("rubric is what to decide, in a sentence or two")
    if "model" in j and not isinstance(j["model"], str):
        problems.append("model is a model name")
    if "minAgreement" in j and not (isinstance(j["minAgreement"], (int, float)) and 0 < j["minAgreement"] <= 1):
        problems.append("minAgreement is from 0 to 1")
    return problems


def calibrate(j, examples):
    """Run the judge on labelled examples and keep how often it agrees with the people who labelled them."""
    rows, agree = [], 0
    for i, ex in enumerate(examples):
        if ex.get("run"):
            ctx = checks.RunCtx(ex["run"])
            text = transcript_of(ctx)
        else:
            text = ex.get("transcript") or ""
        if not text.strip():
            rows.append({"i": i, "label": ex["label"], "verdict": None, "reason": "no conversation"})
            continue
        try:
            got = ask(j["rubric"], text, j["model"], "judge-calibration-%s" % j["id"], "judge")
        except (ValueError, llm.ModelError) as exc:
            rows.append({"i": i, "label": ex["label"], "verdict": None, "reason": str(exc)})
            continue
        rows.append({"i": i, "label": ex["label"], "verdict": got["verdict"], "reason": got["reason"]})
        agree += got["verdict"] == ex["label"]
    n = len(rows)
    conf = {"tp": sum(1 for r in rows if r["label"] == "pass" and r["verdict"] == "pass"), "fp": sum(1 for r in rows if r["label"] == "fail" and r["verdict"] == "pass"),
            "tn": sum(1 for r in rows if r["label"] == "fail" and r["verdict"] == "fail"), "fn": sum(1 for r in rows if r["label"] == "pass" and r["verdict"] == "fail")}
    return {"agreement": round(agree / n, 4) if n else 0, "n": n, "answered": sum(1 for r in rows if r["verdict"]), "confusion": conf,
            "rubricDigest": rubric_digest(j["rubric"], j["model"]), "model": j["model"], "at": core.now(), "rows": rows[:200]}


def pairwise(exp_a, exp_b, rubric, model):
    a, b = {i["id"]: i for i in exp_a["items"]}, {i["id"]: i for i in exp_b["items"]}
    rows, wins = [], {"a": 0, "b": 0, "tie": 0}
    for cid in a:
        if cid not in b or not a[cid]["runs"] or not b[cid]["runs"]:
            continue
        ra, rb = checks.RunCtx(a[cid]["runs"][0]), checks.RunCtx(b[cid]["runs"][0])
        if ra.final is None or rb.final is None:
            rows.append({"id": cid, "winner": "none", "reason": "one version never answered"})
            continue
        first_is_a = int(core.digest(cid, 4), 16) % 2 == 0           # which one is read first: fixed per case, not per version
        first, second = (ra.final, rb.final) if first_is_a else (rb.final, ra.final)
        ask_text = ("%s\n\nThe user said: %s\n\nResponse 1:\n%s\n\nResponse 2:\n%s\n\n"
                    'Answer with JSON only: {"winner": "1" or "2" or "tie", "reason": "why"}.' % (rubric, (ra.of("user_turn") or [{"data": {"text": ""}}])[0]["data"]["text"], first, second))
        try:
            text = llm.chat(model, [{"role": "system", "content": "You compare two answers from an AI assistant. JUDGE"}, {"role": "user", "content": ask_text}], "judge-pairwise", "judge")
            got = checks.json_in(text)
            pick = str(got.get("winner"))
            assert pick in ("1", "2", "tie") and len(str(got.get("reason", ""))) >= 3
        except (ValueError, AssertionError, llm.ModelError, AttributeError) as exc:
            rows.append({"id": cid, "winner": "none", "reason": "the judge did not give a usable answer: %s" % exc})
            continue
        winner = "tie" if pick == "tie" else ("a" if (pick == "1") == first_is_a else "b")
        wins[winner] += 1
        rows.append({"id": cid, "winner": winner, "reason": got["reason"], "firstShown": "a" if first_is_a else "b"})
    decided = sum(wins.values())
    return {"cases": rows, "wins": wins, "candidateWinRate": round(wins["b"] / decided, 4) if decided else None, "decided": decided}


@rest_area("judges")
def _rest(method, rest, body, query, host):
    st = store()
    if method == "GET" and not rest:
        out = []
        for i, j, _ in st.docs("judges"):
            problem = gating_problem(j)
            out.append({"id": i, "model": j["model"], "rubric": j["rubric"], "version": j.get("version", 1), "calibration": {k: (j.get("calibration") or {}).get(k) for k in ("agreement", "n", "at")},
                        "canGate": problem is None, "why": problem})
        return ok({"judges": out, "builtin": sorted(BUILTIN)})
    if len(rest) == 1 and core.valid_id(rest[0]) and method == "PUT":
        j = dict(body or {}, id=rest[0])
        j.setdefault("model", llm.default_model())
        problems = validate_judge(j)
        if problems:
            return fail(400, "That judge has %d problem(s): %s" % (len(problems), "; ".join(problems)), problems=problems)
        before = named(rest[0])
        j["version"] = (before or {}).get("version", 0) + (1 if not before or rubric_digest(before["rubric"], before["model"]) != rubric_digest(j["rubric"], j["model"]) else 0) or 1
        j["calibration"] = before.get("calibration") if before else None      # kept, and judged stale by its digest if the rubric moved
        st.put("judges", rest[0], j)
        return ok({"judge": j, "canGate": gating_problem(j) is None})
    if len(rest) >= 1 and core.valid_id(rest[0]):
        j = named(rest[0])
        if j is None:
            return fail(404, "There is no judge called %s." % rest[0])
        if method == "GET" and len(rest) == 1:
            return ok({"judge": j, "canGate": gating_problem(j) is None, "why": gating_problem(j)})
        if method == "DELETE" and len(rest) == 1:
            return ok(removed=st.drop("judges", rest[0]))
        if method == "POST" and rest[1:] == ["calibrate"]:
            examples = (body or {}).get("examples")
            if (body or {}).get("fromReviews"):
                from . import review
                examples = review.labelled_examples()
            if not (isinstance(examples, list) and 2 <= len(examples) <= 500 and all(isinstance(e, dict) and e.get("label") in ("pass", "fail") and (e.get("run") or e.get("transcript")) for e in examples)):
                return fail(400, "examples is a list of 2 to 500 of {run | transcript, label: pass|fail}.")
            cal = calibrate(j, examples)
            j["calibration"] = cal
            st.put("judges", rest[0], j)
            return ok({"calibration": {k: v for k, v in cal.items() if k != "rows"}, "rows": cal["rows"], "canGate": gating_problem(j) is None, "why": gating_problem(j)})
    return fail(404, "No such route: %s /api/judges/%s" % (method, "/".join(rest)))


@rest_area("pairwise")
def _pairwise(method, rest, body, query, host):
    st = store()
    if method == "POST" and not rest:
        b = body or {}
        a, c = st.get("experiments", b.get("a")), st.get("experiments", b.get("b"))
        if not a or not c or not isinstance(b.get("rubric"), str):
            return fail(400, "Give a and b (experiment ids) and rubric (what makes one answer better).")
        out = pairwise(a, c, b["rubric"], b.get("model") or llm.default_model())
        out.update(id=core.new_id("pw-", 6), a=b["a"], b=b["b"], rubric=b["rubric"], at=core.now())
        st.put("pairwise", out["id"], out)
        return ok({"pairwise": out})
    if method == "GET" and len(rest) == 1:
        got = st.get("pairwise", rest[0])
        return ok({"pairwise": got}) if got else fail(404, "No such comparison.")
    return fail(404, "No such route: %s /api/pairwise/%s" % (method, "/".join(rest)))
