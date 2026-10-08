"""Somewhere for an agent to call back.

An agent that takes longer than a request allows answers later, by posting to a URL
the caller gave it. LLMSim is the caller in a test, so it has to be the one that
listens: a driver registers a token, hands the agent /callbacks/<token>, and waits.
"""

import json
import threading

from . import core, route

_WAITING = {}
_LOCK = threading.Lock()


class Waiter:
    def __init__(self):
        self.event = threading.Event()
        self.payloads = []

    def wait(self, timeout):
        self.event.wait(timeout)
        return list(self.payloads)


def register(token):
    with _LOCK:
        _WAITING[token] = Waiter()
        return _WAITING[token]


def forget(token):
    with _LOCK:
        _WAITING.pop(token, None)


@route("/callbacks/")
def _callback(handler, method, path, rest, raw):
    token = rest.split("/")[0]
    with _LOCK:
        waiter = _WAITING.get(token)
    if waiter is None:
        return handler.send_json(404, {"ok": False, "message": "Nobody is waiting on that callback."})
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        payload = {"raw": raw.decode("utf-8", "replace")[:4000]}
    waiter.payloads.append({"headers": {k.lower(): v for k, v in handler.headers.items()
                                        if k.lower() in ("x-a2a-notification-token", "content-type")}, "body": payload})
    waiter.event.set()
    handler.send_json(200, {"ok": True})
