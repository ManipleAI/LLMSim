"""The REST surface of the kit, and the doors the server opens for it.

Every area here is a client-visible part of the same API the console uses, so
whatever a person can do on a page a harness can do over HTTP.
"""

from . import core, rest_area, run_route
from . import store, timeline
from .core import ok, fail


def _list_arg(query, name):
    raw = (query.get(name) or [""])[0]
    return [x for x in raw.split(",") if x] or None


def _int(query, name, default):
    try:
        return int((query.get(name) or [default])[0])
    except (TypeError, ValueError):
        return default


@rest_area("kit")
def _kit(method, rest, body, query, host):
    st = store.store()
    if method == "GET" and not rest:
        return ok({"retention": st.retention(), "timeline": timeline.settings(), "store": st.path, "kinds": list(timeline.KINDS)})
    if method == "PUT" and not rest:
        body = body or {}
        if "retention" in body:
            r = body["retention"]
            if not isinstance(r, dict) or any(k not in store.DEFAULT_RETENTION or not isinstance(v, int) or v < 1 for k, v in r.items()):
                return fail(400, "retention is {maxRuns, maxEventsPerRun, days}, each a positive whole number.")
            st.put("settings", "retention", dict(st.retention(), **r))
        if "timeline" in body:
            t = body["timeline"]
            if not isinstance(t, dict) or set(t) - {"recordAll"} or not isinstance(t.get("recordAll", False), bool):
                return fail(400, "timeline is {recordAll: true|false}.")
            st.put("settings", "timeline", dict(timeline.settings(), **t))
        return _kit("GET", [], None, query, host)
    return fail(404, "No such route: %s /api/kit/%s" % (method, "/".join(rest)))


@run_route("timeline")
def _timeline(method, run, rest, body, query):
    if method != "GET":
        return fail(405, "A timeline is read, not written. Add to it with POST /api/runs/%s/events." % run)
    if not core.valid_id(run):
        return fail(400, "That is not a run id.")
    out = timeline.timeline(run, kinds=_list_arg(query, "kinds"), agent=(query.get("agent") or [None])[0],
                            since=_int(query, "since", 0), limit=_int(query, "limit", 0) or None)
    if not out["events"] and not store.store().run(run):
        return fail(404, "Nothing is kept for run %s." % run)
    return ok(out)


@run_route("events")
def _events(method, run, rest, body, query):
    if not core.valid_id(run):
        return fail(400, "That is not a run id.")
    if method == "GET":
        return _timeline("GET", run, rest, body, query)
    if method == "POST":
        body = body or {}
        kind = body.get("kind")
        if kind not in timeline.KINDS:
            return fail(400, "kind is one of %s." % ", ".join(timeline.KINDS))
        agent = body.get("agent", "")
        if agent and not core.valid_id(agent):
            return fail(400, "agent is letters, digits, dot, dash, underscore or colon.")
        if not isinstance(body.get("data", {}), dict):
            return fail(400, "data is an object.")
        parent = body.get("parent")
        if parent is not None and not isinstance(parent, int):
            return fail(400, "parent is the id of an earlier event.")
        event_id = timeline.emit(run, kind, agent, parent, body.get("data") or {}, body.get("at"))
        if event_id is None:
            return fail(409, "The event was not kept: the run is full (see /api/kit retention) or the id is not one.")
        return ok({"id": event_id})
    return fail(405, "GET lists a run's events; POST adds one.")


@run_route("stored")
def _stored(method, run, rest, body, query):
    if method == "DELETE":
        return ok({"removed": store.store().delete_run(run)})
    return fail(405, "DELETE removes what is kept for a run.")
