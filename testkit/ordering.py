"""Control over who goes first.

Two agents working at once are racing, and whether the system is right can depend on who wins:
A reads a record before B writes it, or after. A real run takes whichever order the network
gives, and passes or fails by luck. A test cannot work like that.

An ordering is a list of steps, each a condition on a request:

    "steps": {"a-reads":  {"when": {"field": "agent", "op": "eq", "value": "a"}},
              "b-writes": {"when": {"all": [{"field": "agent", "op": "eq", "value": "b"}, {"field": "kind", "op": "eq", "value": "tool"}]}}}

and an order is a sequence of those: ["b-writes", "a-reads"]. While it is in force, a request
matching a step is HELD until every earlier step has completed. Requests that match no step go
straight through. A step nobody asks for does not hold the others for ever: the wait ends after
a timeout, and the run says that it did.

What a request is: the scope has `kind` (model, tool, peer), `agent`, `run`, and the tool, text or
arguments of the call, so a step can name any request an agent makes.
"""

import contextlib
import threading
import time

from . import core

_LOCK = threading.Lock()
_ORDERS = {}                          # run -> Order


class Order:
    def __init__(self, steps, sequence, timeout):
        self.steps = [(name, steps[name]["when"]) for name in sequence]
        self.timeout, self.cond = timeout, threading.Condition()
        self.next, self.claimed, self.timed_out = 0, set(), []

    def _step_for(self, scope):
        for i, (name, when) in enumerate(self.steps):
            if i not in self.claimed and core.cond(when, scope)[0]:
                return i
        return None


def validate(ordering):
    if not isinstance(ordering, dict) or not isinstance(ordering.get("steps"), dict) or not ordering["steps"]:
        return ["ordering is {steps: {name: {when: <condition>}}, timeoutS?}"]
    problems = []
    for name, step in ordering["steps"].items():
        if not core.valid_id(name) or not isinstance(step, dict) or "when" not in step:
            problems.append("ordering.steps.%s is {when: <condition>}" % name)
        else:
            problems += core.check_cond(step["when"], "ordering.steps.%s.when" % name)
    if ordering.get("timeoutS") is not None and not (isinstance(ordering["timeoutS"], (int, float)) and 0 < ordering["timeoutS"] <= 600):
        problems.append("ordering.timeoutS is up to 600")
    if set(ordering) - {"steps", "timeoutS"}:
        problems.append("ordering has steps and timeoutS only")
    return problems


def install(run, ordering, sequence):
    unknown = [s for s in sequence if s not in ordering["steps"]]
    if unknown:
        return ["the order names steps that do not exist: %s" % ", ".join(unknown)]
    with _LOCK:
        _ORDERS[run] = Order(ordering["steps"], sequence, float(ordering.get("timeoutS") or 5))
    return []


def remove(run):
    with _LOCK:
        order = _ORDERS.pop(run, None)
    return order.timed_out if order else []


@contextlib.contextmanager
def guard(run, scope):
    """Hold the request until its turn, and let the next step go when it is done."""
    order = _ORDERS.get(run) if run else None
    if order is None:
        yield
        return
    with order.cond:
        i = order._step_for(scope)
        if i is None:
            held = False
        else:
            order.claimed.add(i)
            held = True
            deadline = time.time() + order.timeout
            while order.next < i:
                left = deadline - time.time()
                if left <= 0:
                    order.timed_out.append(order.steps[i][0])
                    break
                order.cond.wait(left)
    try:
        yield
    finally:
        if held:
            with order.cond:
                order.next = max(order.next, i + 1)
                order.cond.notify_all()


def model_guard(handler, body, shape):
    """The guard for a model request. Cheap when no order is in force, which is almost always."""
    ident = getattr(handler, "identity", None) or {}
    run = ident.get("run")
    if not run or run not in _ORDERS:
        return contextlib.nullcontext()
    try:
        ask = core.host.subject(body.get("messages") or []) if shape != "responses" else ""
    except Exception:  # noqa: BLE001
        ask = ""
    return guard(run, {"kind": "model", "agent": ident.get("agent", ""), "run": run, "text": ask, "model": body.get("model", "")})
