"""The credentials a driver uses to reach an agent platform.

Kept apart from scenarios on purpose. A scenario is a file people commit and share;
a credential must be in neither it nor any run's evidence. A scenario names a
credential by its label, and the secret is looked up at the moment a driver needs
it. The file is owner-only, the API lists labels and fingerprints and never a
value, and every value the kit holds is scrubbed from everything it stores or shows
(core.scrub) — so a driver that echoed its own header into an event would still
leave nothing behind.

Types:
  bearer                      {token}
  header                      {name, value}
  basic                       {username, password}
  oauth_client_credentials    {tokenUrl, clientId, clientSecret, scope?} — a token is fetched when first used
"""

import base64
import json
import os
import threading
import time
import urllib.parse
import urllib.request

from . import core

PATH_ENV = "LLMSIM_CREDENTIALS"
TYPES = {
    "bearer": ("token",),
    "header": ("name", "value"),
    "basic": ("username", "password"),
    "oauth_client_credentials": ("tokenUrl", "clientId", "clientSecret"),
}
SECRET_FIELDS = {"token", "value", "password", "clientSecret"}
_LOCK = threading.RLock()
_CACHE = {}                         # label -> (token, expires)


def path():
    return os.environ.get(PATH_ENV) or os.path.join(core.HERE, "llmsim.credentials.json")


def _load():
    try:
        with open(path()) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _save(data):
    tmp = path() + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path())


def _register(label, entry):
    for field in SECRET_FIELDS:
        core.know_secret("%s:%s" % (label, field), entry.get(field))


def load_secrets():
    """Tell the scrubber about every stored secret. Called at start-up."""
    for label, entry in _load().items():
        _register(label, entry)


def validate(label, entry):
    problems = []
    if not core.valid_id(label):
        problems.append("a credential's label is letters, digits, dot, dash, underscore or colon, up to 64")
    if not isinstance(entry, dict) or entry.get("type") not in TYPES:
        return problems + ["type is one of %s" % ", ".join(sorted(TYPES))]
    for field in TYPES[entry["type"]]:
        if not isinstance(entry.get(field), str) or not entry[field]:
            problems.append("%s needs %s" % (entry["type"], field))
    extra = set(entry) - set(TYPES[entry["type"]]) - {"type", "scope"}
    if extra:
        problems.append("unknown field(s): %s" % ", ".join(sorted(extra)))
    return problems


def put(label, entry):
    problems = validate(label, entry)
    if problems:
        return problems
    with _LOCK:
        data = _load()
        data[label] = entry
        _save(data)
        _CACHE.pop(label, None)
    _register(label, entry)
    return []


def drop(label):
    with _LOCK:
        data = _load()
        if label not in data:
            return False
        entry = data.pop(label)
        _save(data)
        _CACHE.pop(label, None)
    _register(label, {f: None for f in SECRET_FIELDS} if not entry else {f: None for f in SECRET_FIELDS})
    return True


def listing():
    out = []
    for label, entry in sorted(_load().items()):
        secret = next((entry[f] for f in sorted(SECRET_FIELDS) if f in entry), "")
        out.append({"label": label, "type": entry.get("type"), "fingerprint": core.digest(secret, 12) if secret else "",
                    "fields": sorted(k for k in entry if k != "type")})
    return out


def exists(label):
    return label in _load()


def headers(label, timeout=20):
    """The headers that present this credential. Raises KeyError when there is none."""
    entry = _load().get(label)
    if entry is None:
        raise KeyError(label)
    kind = entry["type"]
    if kind == "bearer":
        return {"Authorization": "Bearer " + entry["token"]}
    if kind == "header":
        return {entry["name"]: entry["value"]}
    if kind == "basic":
        return {"Authorization": "Basic " + base64.b64encode(("%s:%s" % (entry["username"], entry["password"])).encode()).decode()}
    token, _ = oauth_token(label, entry, timeout)
    return {"Authorization": "Bearer " + token}


def oauth_token(label, entry=None, timeout=20):
    """(access_token, response) for client credentials, cached until shortly before it expires."""
    entry = entry or _load()[label]
    with _LOCK:
        cached = _CACHE.get(label)
        if cached and cached[1] > time.time() + 30:
            return cached[0], cached[2]
    form = {"grant_type": "client_credentials", "client_id": entry["clientId"], "client_secret": entry["clientSecret"]}
    if entry.get("scope"):
        form["scope"] = entry["scope"]
    req = urllib.request.Request(entry["tokenUrl"], urllib.parse.urlencode(form).encode(), {"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read().decode())
    token = out.get("access_token")
    if not token:
        raise ValueError("the token endpoint answered without an access_token")
    core.know_secret(label + ":access", token)
    with _LOCK:
        _CACHE[label] = (token, time.time() + int(out.get("expires_in") or 600), out)
    return token, out
