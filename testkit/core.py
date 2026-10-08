"""The ground every part of the test kit stands on.

The kit is written against the server's own pieces — its condition language, its
redaction, its identity — rather than beside them, so a thing learned in one place
is true in all of them. It reaches them through `host`, the server module, which
`attach` binds once at start-up. Nothing here imports the server: the server
imports the kit, and the kit would otherwise import the server back.

Standard library only, as the server is.
"""

import hashlib
import json
import os
import random
import re
import string
import threading
import time

host = None                                     # the server module, bound by attach()
ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def attach(module):
    global host
    host = module


def log(*parts):
    (host.log if host else print)(*parts)


def now():
    return time.time()


def new_id(prefix="", n=8):
    return prefix + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def valid_id(value):
    return isinstance(value, str) and bool(ID_RE.fullmatch(value))


def dumps(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def digest(value, n=16):
    raw = value if isinstance(value, str) else dumps(value)
    return hashlib.sha256(raw.encode()).hexdigest()[:n]


def ok(payload=None, **extra):
    return 200, dict({"ok": True}, **(payload or {}), **extra)


def fail(code, message, **extra):
    return code, dict({"ok": False, "message": message}, **extra)


def cond(condition, view):
    """(ok, why) — the server's one condition language, over any dict."""
    return host.cond_result(condition, view)


def check_cond(condition, where="when"):
    """A list of problems with a condition; empty when it is well-formed."""
    try:
        return list(host.validate_cond(condition, where))
    except Exception as exc:  # noqa: BLE001
        return ["%s: %s" % (where, exc)]


def dig(value, path):
    return host._dig_path(value, path)


def redact(value):
    """A value as it may leave this machine: the server's own redaction, then the
    kit's credentials, which no pattern would know to look for."""
    patterns = host.CONFIG["capture"]["redact"]["patterns"] if host else []
    out = host._redact_value(value, patterns) if patterns else value
    return scrub(out)


_SECRETS = {}                                    # label -> secret, filled by the credentials store
_SECRETS_LOCK = threading.Lock()


def know_secret(label, secret):
    with _SECRETS_LOCK:
        if secret:
            _SECRETS[label] = secret
        else:
            _SECRETS.pop(label, None)


def scrub(value):
    """Replace every secret the kit holds, wherever it turns up in a value. A
    credential used to drive an agent must be in no evidence of the run, and the
    surest way is to look for it in everything that is written or shown."""
    with _SECRETS_LOCK:
        secrets = [s for s in _SECRETS.values() if len(s) >= 6]
    if not secrets:
        return value
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[credential]")
        return value
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    return value
