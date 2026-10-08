"""Datasets: scenarios kept together, with their expected outcomes, as versions.

A scenario tests one thing. A dataset is the set of things that must keep working. The point of
keeping them is to run the same set against a changed agent next month and know what changed,
which only works if the set does not change underneath: so a dataset is VERSIONED. Saving it
makes a new immutable version (unless nothing changed), a run names the version it ran, and a
version can carry a tag ("baseline", "release-4") that a later run can name instead of a number.

  an item     one scenario, with its tags and an optional split (e.g. "smoke") to run a part of the set
  from a run  a captured run turned into an item whose expectation is what the run did
  importing   a CSV, or an Agentforce Testing Center test definition, turned into items

A dataset is referred to as  name  (the current version),  name@3  or  name:tag.
"""

import csv
import io
import json
import re
import xml.etree.ElementTree as ET

from . import checks, core, fakes, rest_area, scenario as scn
from .core import fail, ok
from .store import store

ITEM_KEYS = {"id", "scenario", "scenarioId", "tags", "split", "metadata", "repeat"}


def parse_ref(ref):
    """(name, version, tag) from 'name', 'name@3' or 'name:tag'."""
    m = re.fullmatch(r"([A-Za-z0-9._-]{1,64})(?:@(\d+)|:([A-Za-z0-9._-]{1,40}))?", ref or "")
    if not m:
        return None, None, None
    return m.group(1), int(m.group(2)) if m.group(2) else None, m.group(3)


def validate_repeat(r, where="repeat"):
    if not isinstance(r, dict) or set(r) - {"n", "required"}:
        return ["%s is {n, required}" % where]
    n, req = r.get("n", 1), r.get("required", r.get("n", 1))
    if not (isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 100) or not (isinstance(req, int) and not isinstance(req, bool) and 1 <= req <= n):
        return ["%s.n is 1 to 100 and required is 1 to n" % where]
    return []


def validate_item(item, where="items[0]"):
    if not isinstance(item, dict):
        return ["%s is an object" % where]
    problems = ["%s: unknown field %r" % (where, k) for k in item if k not in ITEM_KEYS]
    if not core.valid_id(item.get("id")):
        problems.append("%s.id is a name" % where)
    if ("scenario" in item) == ("scenarioId" in item):
        problems.append("%s: give scenario (the scenario itself) or scenarioId (a saved one)" % where)
    if "scenario" in item:
        sc = dict(item["scenario"], id=item["scenario"].get("id") or item.get("id")) if isinstance(item["scenario"], dict) else item["scenario"]
        problems += ["%s.scenario: %s" % (where, p) for p in scn.validate(sc)]
    if "repeat" in item:
        problems += validate_repeat(item["repeat"], where + ".repeat")
    if not all(isinstance(t, str) for t in item.get("tags") or []):
        problems.append("%s.tags is a list of words" % where)
    return problems


def _resolve(items):
    """Items with every saved scenario copied in, so a version does not move when the scenario does."""
    out = []
    for item in items:
        item = dict(item)
        if "scenarioId" in item:
            found = store().get("scenarios", item.pop("scenarioId"))
            item["scenario"] = found
        elif isinstance(item.get("scenario"), dict):
            item["scenario"] = dict(item["scenario"], id=item["scenario"].get("id") or item["id"])
        out.append(item)
    return out


def head(name):
    return store().get("datasets", name)


def version(name, number=None, tag=None):
    meta = head(name)
    if not meta:
        return None, None
    if tag:
        number = meta.get("tags", {}).get(tag)
        if number is None:
            return None, None
    number = number or meta["current"]
    body = store().get("datasetversions", "%s@%d" % (name, number))
    return (number, body) if body else (None, None)


def save(name, items, description=None):
    """(version, created) — a new version, unless this is what the current one already is."""
    items = _resolve(items)
    meta = head(name) or {"current": 0, "tags": {}, "description": ""}
    if description is not None:
        meta["description"] = description
    digest = core.digest(items, 24)
    if meta["current"]:
        current = store().get("datasetversions", "%s@%d" % (name, meta["current"]))
        if current and current["digest"] == digest:
            store().put("datasets", name, meta)
            return meta["current"], False
    meta["current"] += 1
    store().put("datasetversions", "%s@%d" % (name, meta["current"]), {"items": items, "digest": digest, "createdAt": core.now(), "version": meta["current"]})
    store().put("datasets", name, meta)
    return meta["current"], True


def select(items, split=None, tags=None, ids=None):
    out = []
    for it in items:
        if split and it.get("split") != split:
            continue
        if tags and not set(tags) & set(it.get("tags") or []):
            continue
        if ids and it["id"] not in ids:
            continue
        out.append(it)
    return out


# -- an item from a run -------------------------------------------------------------------------------

def item_from_run(run, item_id, base_scenario=None, strict=False, include=("answer", "path", "state")):
    """A scenario whose checks are what this run did. `base_scenario` supplies the agent, model and setup."""
    result = store().get("results", run) or {}
    base = dict(base_scenario or store().get("scenarios", result.get("scenario")) or {})
    if not base.get("agent") and not base.get("agents"):
        raise ValueError("a run does not say how to reach its agent: give agent, or run a saved scenario")
    events = store().events(run)
    if not events:
        raise ValueError("nothing is kept for run %s" % run)
    ctx = checks.RunCtx(run)
    users = {}
    for e in events:
        if e["kind"] == "user_turn":
            users.setdefault(e["agent"], []).append({"say": e["data"].get("text", "")})
    new = {k: v for k, v in base.items() if k in ("agent", "agents", "model", "setup", "teardown", "ordering", "timeoutS", "stop")}
    new["id"] = item_id
    new["description"] = "Made from run %s" % run
    if "agents" in new:
        for a in new["agents"]:
            a["turns"] = users.get(a["name"]) or a.get("turns")
    else:
        new["turns"] = users.get((new["agent"].get("name") or "agent")) or next(iter(users.values()), [{"say": "hello"}])
    specs = []
    if "answer" in include and ctx.final is not None:
        specs.append({"type": "answer", "name": "final answer", "equals": ctx.final} if strict else
                     {"type": "answer", "name": "final answer", "contains": re.split(r"(?<=[.!?])\s", ctx.final.strip())[0][:80]})
    if "path" in include:
        tools = ctx.tool_calls()
        calls = [{"tool": c["tool"], "arguments": c["arguments"]} for c in tools] if tools else \
            [{"tool": c["tool"], "arguments": c["arguments"]} for c in ctx.requested_calls()]
        if calls:
            specs.append({"type": "path", "name": "the path taken", "source": "tools" if tools else "model", "mode": "strict", "expected": calls})
    if "state" in include:
        snap = result.get("state") or fakes.snapshot(run)
        seeded = {(e["data"]["collection"], e["data"]["key"]) for e in ctx.of("state_change") if e["data"].get("op") == "seed"}
        for coll, records in snap.items():
            for key, record in records.items():
                specs.append({"type": "state", "name": "%s/%s" % (coll, key), "collection": coll, "key": key, "expect": record} if isinstance(record, dict) else
                             {"type": "state", "collection": coll, "key": key, "exists": True})
    if not specs:
        raise ValueError("the run did nothing that can be expected (no answer, no tool calls, no state)")
    new["checks"] = specs
    return {"id": item_id, "scenario": new, "metadata": {"fromRun": run}, "tags": ["from-run"]}


# -- importing --------------------------------------------------------------------------------------------------

def _list(value):
    if isinstance(value, list):
        return [str(v) for v in value]
    value = (value or "").strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            return [str(v) for v in json.loads(value)]
        except ValueError:
            pass
    return [v.strip() for v in re.split(r"[;|]", value) if v.strip()]


def _case_to_item(i, case, agent, source):
    """One test case (utterance and expectations) as a dataset item."""
    utterance = case.get("utterance") or case.get("input") or case.get("say")
    if not utterance:
        raise ValueError("test case %d has no utterance" % (i + 1))
    expect, specs, warnings = {}, [], []
    if case.get("expected_response") or case.get("expectedResponse"):
        expect["contains"] = case.get("expected_response") or case.get("expectedResponse")
    if case.get("expected_contains"):
        expect["contains"] = case["expected_contains"]
    actions = _list(case.get("expected_actions") or case.get("expectedActions"))
    if actions:
        specs.append({"type": "path", "name": "actions", "source": source, "mode": "strict", "args": "ignore", "expected": [{"tool": a} for a in actions]})
    if case.get("expected_topic") or case.get("expectedTopic"):
        warnings.append("case %d: the expected topic is kept as metadata; LLMSim can only see which topic an agent chose if the platform reports it" % (i + 1))
    outcome = case.get("expected_outcome") or case.get("expectedOutcome")
    if outcome:
        if "judge" in checks.CHECKS:
            specs.append({"type": "judge", "name": "expected outcome", "rubric": outcome})
        else:
            warnings.append("case %d: an expected outcome needs a judge, and none is installed; kept as metadata" % (i + 1))
    scenario = {"id": "case-%d" % (i + 1), "agent": agent, "turns": [dict({"say": utterance}, **({"expect": expect} if expect else {}))], "checks": specs}
    if not expect and not specs:
        scenario["checks"] = [{"type": "run", "name": "the agent replied", "cond": {"field": "turns", "op": "gte", "value": 1}}]
    meta = {k: v for k, v in case.items() if k not in ("utterance",)}
    return {"id": case.get("id") or "case-%d" % (i + 1), "scenario": scenario, "metadata": meta, "tags": ["imported"]}, warnings


def parse_cases(fmt, content):
    if fmt == "csv":
        return [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(io.StringIO(content))]
    if fmt == "json":
        data = json.loads(content)
        cases = data.get("testCases") if isinstance(data, dict) else data
        if not isinstance(cases, list):
            raise ValueError("expected a list of test cases, or {testCases: [...]}")
        return cases
    if fmt == "xml":
        root = ET.fromstring(content)
        ns = {"m": root.tag.split("}")[0][1:]} if root.tag.startswith("{") else None
        find = (lambda el, tag: el.findall("m:" + tag, ns)) if ns else (lambda el, tag: el.findall(tag))
        cases = []
        for tc in find(root, "testCase"):
            case = {}
            for inp in find(tc, "inputs"):
                for u in find(inp, "utterance"):
                    case["utterance"] = (u.text or "").strip()
            for ex in find(tc, "expectation"):
                name = (find(ex, "name") or [None])[0]
                value = (find(ex, "expectedValue") or [None])[0]
                if name is None or value is None:
                    continue
                key = {"topic_sequence_match": "expectedTopic", "action_sequence_match": "expectedActions", "bot_response_rating": "expectedOutcome",
                       "string_comparison": "expected_contains"}.get(name.text.strip())
                if key:
                    case[key] = (value.text or "").strip()
            cases.append(case)
        return cases
    raise ValueError("format is csv, json or xml (convert YAML to JSON first: LLMSim reads no YAML)")


def import_items(fmt, content, agent, source="tools"):
    cases = parse_cases(fmt, content)
    if not cases or len(cases) > 1000:
        raise ValueError("expected 1 to 1000 test cases, found %d" % len(cases))
    items, warnings = [], []
    for i, case in enumerate(cases):
        item, w = _case_to_item(i, case, agent, source)
        items.append(item)
        warnings += w
    return items, warnings


# -- REST ------------------------------------------------------------------------------------------------------------

@rest_area("datasets")
def _rest(method, rest, body, query, host):
    st = store()
    if method == "GET" and not rest:
        out = []
        for name, meta, updated in st.docs("datasets"):
            _, current = version(name)
            out.append({"id": name, "version": meta["current"], "items": len((current or {}).get("items", [])), "tags": meta.get("tags", {}),
                        "description": meta.get("description", ""), "updated": updated})
        return ok({"datasets": out})
    if len(rest) < 1 or not core.valid_id(rest[0]):
        return fail(404, "No such route: %s /api/datasets/%s" % (method, "/".join(rest)))
    name = rest[0]
    if method == "PUT" and len(rest) == 1:
        items = (body or {}).get("items")
        if not isinstance(items, list) or not items or len(items) > 1000:
            return fail(400, "items is a list of 1 to 1000 items.")
        problems = [p for i, it in enumerate(items) for p in validate_item(it, "items[%d]" % i)]
        ids = [it.get("id") for it in items if isinstance(it, dict)]
        if len(set(ids)) != len(ids):
            problems.append("item ids must be different")
        if problems:
            return fail(400, "That dataset has %d problem(s): %s" % (len(problems), "; ".join(problems[:5])), problems=problems)
        v, created = save(name, items, (body or {}).get("description"))
        return ok({"id": name, "version": v, "created": created})
    meta = head(name)
    if method == "GET" and len(rest) == 1:
        if not meta:
            return fail(404, "There is no dataset called %s." % name)
        want = (query.get("version") or [None])[0]
        number, body_ = version(name, int(want) if want and want.isdigit() else None, (query.get("tag") or [None])[0])
        if not body_:
            return fail(404, "That version of %s does not exist." % name)
        return ok({"id": name, "version": number, "tags": meta.get("tags", {}), "description": meta.get("description", ""), "items": body_["items"], "createdAt": body_["createdAt"]})
    if not meta and rest[1:] not in (["import"], ["items", "from-run"]):
        return fail(404, "There is no dataset called %s." % name)
    if method == "DELETE" and len(rest) == 1:
        for v in range(1, meta["current"] + 1):
            st.drop("datasetversions", "%s@%d" % (name, v))
        return ok(removed=st.drop("datasets", name))
    if method == "GET" and rest[1:] == ["versions"]:
        return ok({"versions": [{"version": v, "createdAt": (st.get("datasetversions", "%s@%d" % (name, v)) or {}).get("createdAt"),
                                 "items": len((st.get("datasetversions", "%s@%d" % (name, v)) or {}).get("items", []))} for v in range(1, meta["current"] + 1)],
                   "tags": meta.get("tags", {})})
    if method == "POST" and rest[1:] == ["tag"]:
        tag, number = (body or {}).get("tag"), (body or {}).get("version") or meta["current"]
        if not (isinstance(tag, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,40}", tag)) or not st.get("datasetversions", "%s@%s" % (name, number)):
            return fail(400, "Give tag (a short word) and a version that exists.")
        meta.setdefault("tags", {})[tag] = number
        st.put("datasets", name, meta)
        return ok({"tags": meta["tags"]})
    if method == "POST" and rest[1:] == ["items", "from-run"]:
        b = body or {}
        if not core.valid_id(b.get("run")) or not core.valid_id(b.get("id")):
            return fail(400, "Give run (a run id) and id (the new item's name).")
        try:
            item = item_from_run(b["run"], b["id"], b.get("scenario"), bool(b.get("strict")), tuple(b.get("include") or ("answer", "path", "state")))
        except ValueError as exc:
            return fail(400, str(exc))
        problems = validate_item(item)
        if problems:
            return fail(400, "The item made from that run is not valid: %s" % "; ".join(problems[:4]), problems=problems)
        _, current = version(name)
        items = [i for i in (current or {}).get("items", []) if i["id"] != b["id"]] + [item]
        v, _ = save(name, items)
        return ok({"version": v, "item": item})
    if method == "POST" and rest[1:] == ["import"]:
        b = body or {}
        agent = b.get("agent")
        problems = __import__("testkit.drivers", fromlist=["x"]).validate(agent)
        if problems:
            return fail(400, "agent: " + "; ".join(problems))
        try:
            items, warnings = import_items(b.get("format"), b.get("content") or "", agent, b.get("source", "tools"))
        except (ValueError, ET.ParseError, KeyError) as exc:
            return fail(400, "Could not read that: %s" % exc)
        bad = [p for i, it in enumerate(items) for p in validate_item(it, "items[%d]" % i)]
        if bad:
            return fail(400, "The imported cases are not valid scenarios: %s" % "; ".join(bad[:4]), problems=bad)
        _, current = version(name)
        kept = [i for i in (current or {}).get("items", []) if i["id"] not in {n["id"] for n in items}] if b.get("merge") else []
        v, _ = save(name, kept + items)
        return ok({"version": v, "imported": len(items), "warnings": warnings})
    return fail(404, "No such route: %s /api/datasets/%s" % (method, "/".join(rest)))
