"""An agent test kit, built into LLMSim.

LLMSim began as a simulator of the model an agent talks to. The kit puts it on the
other sides of the agent as well — the tools it calls, the peer agents it hands work
to, and the person (or script) that drives it — and adds what a test needs that a
simulator does not: a run kept on disk as one timeline, scenarios, checks on the
answer and the path and the state a run left behind, datasets, repetition, and a
verdict a CI job can act on.

Nothing here is specific to any product. A platform enters only through a driver
(how to start a conversation with it) or an importer (how to read its test cases).

The server imports this package and calls `attach` once. Everything else the kit
adds is reached through three doors the server opens for it:

    REST areas        /api/kit, /api/scenarios, … registered into the server's dispatcher
    protocol routes   /mcp/…, /mock/…, /a2a/…, /otlp/… served directly, ahead of the vendors
    run sub-routes    /api/runs/<run>/timeline, … answered before the server's own
"""

from . import core

ROUTE_TABLE = []              # (prefix, function(handler, method, path, rest, raw)) — protocol routes
REST = {}                     # area -> function(method, rest, body, query, host)
RUN_ROUTES = {}               # sub-route name -> function(method, run, rest, body, query)


def route(prefix):
    def wrap(fn):
        ROUTE_TABLE.append((prefix, fn))
        return fn
    return wrap


def rest_area(name):
    def wrap(fn):
        REST[name] = fn
        return fn
    return wrap


def run_route(name):
    def wrap(fn):
        RUN_ROUTES[name] = fn
        return fn
    return wrap


def attach(module):
    """Bind the server and open its doors. Safe to call once."""
    core.attach(module)
    from . import api            # noqa: F401 — importing registers every area and route
    from . import credentials
    credentials.load_secrets()      # so what a driver is trusted with is scrubbed from everything kept
    module.REST_AREAS.update(REST)
    module.REST_PREFIXES = tuple(module.REST_PREFIXES) + tuple("/api/" + a for a in REST if "/api/" + a not in module.REST_PREFIXES)
    core.log("testkit: %d REST area(s), %d protocol route(s); runs kept in %s" % (len(REST), len(ROUTE_TABLE), api.store.store().path))


def owns(path):
    return any(path == p.rstrip("/") or path.startswith(p) for p, _ in ROUTE_TABLE)


def serve(handler, method, path, raw):
    """Serve a protocol route. True when the path was the kit's."""
    for prefix, fn in ROUTE_TABLE:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            fn(handler, method, path, path[len(prefix):].lstrip("/"), raw)
            return True
    return False


def run_sub(method, rest, body, query):
    """A /api/runs/<run>/<sub> route, or None when it is not one of the kit's."""
    if len(rest) >= 2 and rest[1] in RUN_ROUTES:
        return RUN_ROUTES[rest[1]](method, rest[0], rest[2:], body, query)
    return None


def on_capture(cap):
    from . import timeline
    timeline.on_capture(cap)


def store_runs():
    from . import store
    return store.store().runs()


def forget_run(run):
    from . import store
    return store.store().delete_run(run)


def run_rules():
    """Rules a running scenario brought with it, tried before every other."""
    from . import runtime
    return runtime.run_rules()
