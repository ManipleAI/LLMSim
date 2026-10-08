"""People, in the loop.

Two things a person does that nothing else can: say whether an answer was good, and say what it should
have been. This is where they do it.

  the queue      runs waiting for a person, each with its conversation and a rubric to score against. A reviewer
                 gives scores, a verdict and a comment — and, if the answer was wrong, the CORRECT one. Those
                 verdicts are what a judge is calibrated against.
  a correction   when the review is of a case in a dataset, the corrected answer becomes what that case is checked
                 against: a new version of the dataset, so the next run of it is held to what the person said.
  variations     a model proposes inputs near an existing case — paraphrases, near-misses, attempts to trick the
                 agent. They are generated once, shown to a person, and only the ones a person accepts become
                 cases. Nothing a model made goes into a dataset unread.
"""

import re

from . import checks, core, datasets, judges, llm, rest_area
from .core import fail, ok
from .store import store


def labelled_examples():
    return [{"run": r["run"], "label": r["submission"]["verdict"]} for _, r, _ in store().docs("reviews")
            if r.get("status") == "done" and r.get("run") and (r.get("submission") or {}).get("verdict") in ("pass", "fail")]


def _review_for(run, rubric, dataset=None, item=None):
    ctx = checks.RunCtx(run)
    if not ctx.events:
        raise ValueError("nothing is kept for run %s" % run)
    result = store().get("results", run) or {}
    return {"id": core.new_id("rv-", 6), "kind": "run", "run": run, "scenario": result.get("scenario"), "rubric": rubric or ["correct", "helpful"],
            "dataset": dataset, "item": item, "status": "open", "createdAt": core.now(), "transcript": core.redact(judges.transcript_of(ctx))}


def apply_correction(review, corrected):
    """Hold the case to what the person said it should have been, in a new version of its dataset."""
    ds, item_id = review.get("dataset"), review.get("item")
    number, version = datasets.version(*datasets.parse_ref(ds))
    if not version:
        raise ValueError("there is no dataset %s" % ds)
    items = [dict(i) for i in version["items"]]
    target = next((i for i in items if i["id"] == item_id), None)
    if target is None:
        raise ValueError("there is no item %s in %s" % (item_id, ds))
    sc = dict(target["scenario"])
    kept = [c for c in sc.get("checks") or [] if c.get("name") != "final answer"]
    sc["checks"] = kept + [{"type": "answer", "name": "final answer", "equals": corrected}]
    if sc.get("turns"):
        sc["turns"] = [{k: v for k, v in t.items() if k != "expect"} for t in sc["turns"]]      # an old expectation must not outvote the correction
    target["scenario"] = sc
    v, _ = datasets.save(datasets.parse_ref(ds)[0], items)
    return v


@rest_area("reviews")
def _rest(method, rest, body, query, host):
    st = store()
    if method == "GET" and not rest:
        want = (query.get("status") or [None])[0]
        rows = [{k: r.get(k) for k in ("id", "kind", "run", "scenario", "status", "dataset", "item", "createdAt")} for _, r, _ in st.docs("reviews")]
        return ok({"reviews": [r for r in rows if not want or r["status"] == want]})
    if method == "POST" and not rest:
        b = body or {}
        runs = list(b.get("runs") or [])
        if b.get("fromExperiment"):
            exp = st.get("experiments", b["fromExperiment"])
            if not exp:
                return fail(404, "There is no experiment %s." % b["fromExperiment"])
            want = set(b.get("status") or ["fail", "flaky"])
            for it in exp["items"]:
                if it["status"] in want and it["runs"]:
                    runs.append(it["runs"][0])
        if not runs or len(runs) > 200 or not all(core.valid_id(r) for r in runs):
            return fail(400, "Give runs (run ids) or fromExperiment.")
        created = []
        for run in runs:
            try:
                review = _review_for(run, b.get("rubric"), b.get("dataset"), b.get("item") if len(runs) == 1 else None)
            except ValueError as exc:
                return fail(400, str(exc))
            st.put("reviews", review["id"], review)
            created.append(review["id"])
        return ok({"created": created})
    if len(rest) >= 1 and core.valid_id(rest[0]):
        review = st.get("reviews", rest[0])
        if not review:
            return fail(404, "There is no review %s." % rest[0])
        if method == "GET" and len(rest) == 1:
            return ok({"review": review})
        if method == "POST" and rest[1:] == ["submit"]:
            b = body or {}
            if review["status"] == "done":
                return fail(409, "That review was already submitted.")
            scores = b.get("scores") or {}
            if not isinstance(scores, dict) or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= 5 for v in scores.values()):
                return fail(400, "scores is {criterion: 1 to 5}.")
            if b.get("verdict") not in ("pass", "fail"):
                return fail(400, "verdict is pass or fail.")
            sub = {"scores": scores, "verdict": b["verdict"], "comment": str(b.get("comment", ""))[:2000], "reviewer": str(b.get("reviewer", ""))[:80], "at": core.now()}
            corrected = b.get("corrected")
            if corrected is not None:
                if not isinstance(corrected, str) or not corrected.strip():
                    return fail(400, "corrected is the answer the agent should have given.")
                sub["corrected"] = corrected
                if review.get("dataset") and review.get("item"):
                    try:
                        sub["datasetVersion"] = apply_correction(review, corrected)
                    except ValueError as exc:
                        return fail(400, str(exc))
            review.update(status="done", submission=sub)
            st.put("reviews", review["id"], review)
            return ok({"review": review})
    return fail(404, "No such route: %s /api/reviews/%s" % (method, "/".join(rest)))


# -- variations -------------------------------------------------------------------------------------------------------------------

KINDS = {"paraphrase": "the same request in different words", "near-miss": "a request that looks similar but should be handled differently",
         "adversarial": "a request that tries to trick the assistant into misbehaving"}


@rest_area("variations")
def _variations(method, rest, body, query, host):
    st = store()
    if method == "GET" and not rest:
        return ok({"variations": [{"id": i, "dataset": v["dataset"], "item": v["item"], "proposals": len(v["proposals"]), "accepted": sum(1 for p in v["proposals"] if p["status"] == "accepted")}
                                  for i, v, _ in st.docs("variations")]})
    if method == "POST" and rest == ["generate"]:
        b = body or {}
        number, ds = datasets.version(*datasets.parse_ref(b.get("dataset")))
        item = next((i for i in (ds or {}).get("items", []) if i["id"] == b.get("item")), None)
        kinds = b.get("kinds") or list(KINDS)
        count = b.get("count", 2)
        if not item or not set(kinds) <= set(KINDS) or not (isinstance(count, int) and 1 <= count <= 10):
            return fail(400, "Give dataset, item (one that exists), kinds (any of %s) and count (1 to 10)." % ", ".join(KINDS))
        first = ((item["scenario"].get("turns") or (item["scenario"].get("agents") or [{}])[0].get("turns") or [{}])[0]).get("say", "")
        prompt = ("Original user message: %s\n\nWrite %d variations of each kind:\n%s\n\nAnswer with JSON only: {%s}" % (
            first, count, "\n".join("- %s: %s" % (k, KINDS[k]) for k in kinds), ", ".join('"%s": [..]' % k for k in kinds)))
        try:
            text = llm.chat(b.get("model") or llm.default_model(), [{"role": "system", "content": "You write test inputs for an AI assistant. VARIATIONS"}, {"role": "user", "content": prompt}], "variations", "generator")
            data = checks.json_in(text)
        except (ValueError, llm.ModelError) as exc:
            return fail(502, "the model did not give usable variations: %s" % exc)
        proposals = [{"kind": k, "text": t, "status": "proposed"} for k in kinds for t in (data.get(k) or [])[:count] if isinstance(t, str) and t.strip()]
        if not proposals:
            return fail(502, "the model gave no variations.")
        vid = core.new_id("vr-", 6)
        st.put("variations", vid, {"id": vid, "dataset": datasets.parse_ref(b["dataset"])[0], "item": item["id"], "proposals": proposals, "createdAt": core.now()})
        return ok({"id": vid, "proposals": proposals})
    if len(rest) >= 1 and core.valid_id(rest[0]):
        v = st.get("variations", rest[0])
        if not v:
            return fail(404, "There is no variation set %s." % rest[0])
        if method == "GET" and len(rest) == 1:
            return ok({"variations": v})
        if method == "POST" and rest[1:] == ["accept"]:
            accept = (body or {}).get("accept")
            if not isinstance(accept, list) or not accept:
                return fail(400, "accept is a list of {index, checks?} — what a person decided to keep.")
            number, ds = datasets.version(v["dataset"])
            items = [dict(i) for i in ds["items"]]
            base = next(i for i in items if i["id"] == v["item"])
            added = []
            for a in accept:
                idx = a.get("index")
                if not isinstance(idx, int) or not 0 <= idx < len(v["proposals"]) or v["proposals"][idx]["status"] != "proposed":
                    return fail(400, "index %r is not a proposal still waiting for a decision." % idx)
                prop = v["proposals"][idx]
                sc = dict(base["scenario"])
                if "agents" in sc:
                    sc["agents"] = [dict(sc["agents"][0], turns=[{"say": prop["text"]}])] + sc["agents"][1:]
                else:
                    sc["turns"] = [{"say": prop["text"]}]
                specs = a.get("checks")
                if specs is None and prop["kind"] == "paraphrase":
                    specs = [c for c in sc.get("checks") or []]
                if not specs:
                    return fail(400, "a %s needs the checks a person says it should meet: give checks for index %d." % (prop["kind"], idx))
                sc["checks"] = specs
                new_id = "%s-%s-%d" % (v["item"], prop["kind"].replace("-", ""), idx + 1)
                item = {"id": new_id, "scenario": dict(sc, id=new_id), "tags": ["variation", prop["kind"]], "metadata": {"from": v["item"], "variation": v["id"]}}
                bad = datasets.validate_item(item)
                if bad:
                    return fail(400, "That case is not valid: %s" % "; ".join(bad[:3]))
                items = [i for i in items if i["id"] != new_id] + [item]
                prop["status"] = "accepted"
                added.append(new_id)
            for p in v["proposals"]:
                if p["status"] == "proposed" and (body or {}).get("rejectRest"):
                    p["status"] = "rejected"
            st.put("variations", v["id"], v)
            version, _ = datasets.save(v["dataset"], items)
            return ok({"added": added, "datasetVersion": version})
    return fail(404, "No such route: %s /api/variations/%s" % (method, "/".join(rest)))
