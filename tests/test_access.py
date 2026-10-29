#!/usr/bin/env python3
"""Who may use the console and the API: a sign-in with a cookie, an API key, and
what stays open because an agent under test cannot sign in.

    python3 tests/test_access.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kit import Sim, OPENAI_KEY, CHAT  # noqa: E402
from testkit.client import Client  # noqa: E402

PASSWORD, KEY = "correct horse battery", "llmsim-test-key-0123456789"
RULE = {"rule": {"name": "hi", "when": {"contains": "access-test"}, "turns": [{"text": "yes"}]}}


def sign_in(sim, user="admin", password=PASSWORD, headers=None):
    return sim.call("POST", "/login", {"user": user, "password": password}, headers or {})


def signing_in(text):
    return '<body class="signing-in">' in text


def cookie_of(headers):
    value = headers.get("Set-Cookie") or ""
    return value.split(";", 1)[0]


def check(cond, what):
    if not cond:
        raise AssertionError(what)


# -- open, as it always was ------------------------------------------------------

def case_open_by_default(sim):
    status, body = sim.api("GET", "/api/rules")
    check(status == 200, "an LLMSim with nothing configured answers /api: %s" % status)
    status, _, text = sim.call("GET", "/ui")
    check(status == 200 and not signing_in(text), "and serves the console, with no sign-in in front of it: %s" % status)
    status, body = sim.api("GET", "/healthz")
    check("rules" in body, "and its health check says everything: %s" % body)
    status, body = sim.api("GET", "/api/auth")
    check(body == {"on": False, "user": None, "via": None}, "nobody needs to be anybody: %s" % body)


# -- on: what is guarded and what is not -----------------------------------------

def case_api_refused_without_credentials(sim):
    status, headers, _ = sim.call("GET", "/api/rules")
    check(status == 401, "no credentials: %s" % status)
    check(headers.get("X-LLMSim-Sign-In") == "/ui", "the console is told to ask for a sign-in: %s" % headers)
    status, _, _ = sim.call("POST", "/api/rules", RULE)
    check(status == 401, "a change is refused too: %s" % status)
    status, _, _ = sim.call("GET", "/run/r1/agent/a/api/rules")
    check(status == 401, "a /run/…/agent/… prefix does not get round it: %s" % status)
    status, _, _ = sim.call("GET", "/openai/api/rules")
    check(status == 401, "nor does a provider prefix: %s" % status)
    status, _, _ = sim.call("POST", "/api/ui/chat", {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]})
    check(status == 401, "the console's own routes are guarded: %s" % status)
    status, body = sim.api("GET", "/api/auth")
    check(status == 200 and body == {"on": True, "user": None, "via": None}, "/api/auth says a sign-in is needed: %s %s" % (status, body))


def case_console_asks_for_a_sign_in_in_place(sim):
    status, headers, text = sim.call("GET", "/ui")
    check(status == 200 and not headers.get("Location"), "the console is served where it is, not redirected: %s" % status)
    check(signing_in(text) and 'id="si-pw"' in text, "with the sign-in drawn in front of it")


def case_health_says_only_alive(sim):
    status, body = sim.api("GET", "/healthz")
    check(status == 200 and body.get("ok") and "rules" not in body and "mode" not in body, "trimmed: %s" % body)


def case_agents_routes_stay_open(sim):
    status, body = sim.chat("access-test")
    check(status == 200, "a vendor route takes its own key, not a sign-in: %s %s" % (status, body))
    status, _, _ = sim.call("POST", "/otlp/v1/traces", {"resourceSpans": []})
    check(status != 401, "the test kit's stand-ins stay open: %s" % status)
    status, _, _ = sim.call("GET", "/guide")
    check(status == 200, "the guide is open: %s" % status)


def case_api_key(sim):
    status, _ = sim.api("GET", "/api/rules", headers={"X-LLMSim-Key": KEY})
    check(status == 200, "X-LLMSim-Key: %s" % status)
    status, _ = sim.api("POST", "/api/rules", RULE, headers={"Authorization": "Bearer " + KEY})
    check(status == 200, "a bearer token on /api: %s" % status)
    status, _ = sim.api("GET", "/api/rules", headers={"X-LLMSim-Key": KEY + "x"})
    check(status == 401, "a wrong key: %s" % status)
    status, body = sim.api("GET", "/api/auth", headers={"X-LLMSim-Key": KEY})
    check(body == {"on": True, "user": None, "via": "key"}, "who it is: %s" % body)
    status, body = sim.api("GET", "/healthz", headers={"X-LLMSim-Key": KEY})
    check("rules" in body, "the key sees the whole health check: %s" % body)


def case_client_sends_the_key(sim):
    status, _ = Client(sim.base, KEY).call("GET", "/api/rules")
    check(status == 200, "Client with the key: %s" % status)
    status, _ = Client(sim.base, "").call("GET", "/api/rules")
    check(status == 401, "Client without it: %s" % status)


def case_wrong_password(sim):
    status, headers, text = sign_in(sim, password="nope")
    check(status == 401 and "do not match" in json.loads(text)["message"], "refused: %s" % status)
    check(not headers.get("Set-Cookie"), "and no cookie")
    status, headers, _ = sign_in(sim, user="root")
    check(status == 401 and not headers.get("Set-Cookie"), "the wrong user is refused too: %s" % status)


def case_sign_in_and_use_the_cookie(sim):
    status, headers, text = sign_in(sim)
    check(status == 200 and json.loads(text) == {"ok": True, "on": True, "user": "admin"}, "signed in: %s %s" % (status, text))
    check(not headers.get("Location"), "and nothing is redirected")
    raw = headers.get("Set-Cookie") or ""
    for part in ("llmsim_session_%d=" % sim.port, "HttpOnly", "SameSite=Strict", "Path=/"):
        check(part in raw, "the cookie has %s: %s" % (part, raw))
    sim.cookie = cookie_of(headers)
    status, _, text = sim.call("GET", "/ui", headers={"Cookie": sim.cookie})
    check(status == 200 and not signing_in(text), "the console with the cookie, and no sign-in in front: %s" % status)
    status, body = sim.api("GET", "/api/auth", headers={"Cookie": sim.cookie})
    check(body == {"on": True, "user": "admin", "via": "cookie"}, "who: %s" % body)
    origin = "http://127.0.0.1:%d" % sim.port
    status, _ = sim.api("POST", "/api/rules", {"rule": dict(RULE["rule"], name="from-the-console")},
                        headers={"Cookie": sim.cookie, "Origin": origin})
    check(status == 200, "a change from the console's own page: %s" % status)


def case_cookie_refused_from_another_site(sim):
    status, _ = sim.api("POST", "/api/rules", RULE, headers={"Cookie": sim.cookie, "Origin": "http://localhost:3000"})
    check(status == 403, "another site (another port) cannot use the cookie: %s" % status)
    status, _ = sim.api("DELETE", "/api/rules/hi", headers={"Cookie": sim.cookie, "Sec-Fetch-Site": "same-site"})
    check(status == 403, "nor without an Origin, when the browser says it was another site: %s" % status)
    status, _ = sim.api("GET", "/api/rules", headers={"Cookie": sim.cookie, "Sec-Fetch-Site": "same-origin"})
    check(status == 200, "a read from the console's own page: %s" % status)


def case_forged_cookie(sim):
    name, value = sim.cookie.split("=", 1)
    user, expires, mac = value.split(".")
    for bad in ("%s.%d.%s" % (user, int(expires) + 999, mac), "%s.%s.%s" % (user, expires, "0" * 64), "nonsense"):
        status, _ = sim.api("GET", "/api/rules", headers={"Cookie": "%s=%s" % (name, bad)})
        check(status == 401, "a forged cookie is refused: %s" % bad[:40])


def case_sign_in_from_another_site(sim):
    status, headers, _ = sign_in(sim, headers={"Origin": "http://localhost:3000"})
    check(status == 403 and not headers.get("Set-Cookie"), "another site cannot sign this browser in: %s" % status)


def case_cookie_survives_a_restart(sim):
    sim.restart()
    status, _ = sim.api("GET", "/api/rules", headers={"Cookie": sim.cookie})
    check(status == 200, "signed in after a restart: %s" % status)
    check(os.path.exists(sim.env["LLMSIM_SECRET"]), "the signing secret is kept")
    check(oct(os.stat(sim.env["LLMSIM_SECRET"]).st_mode & 0o777) == "0o600", "owner-only")


def case_sign_out(sim):
    origin = "http://127.0.0.1:%d" % sim.port
    status, headers, _ = sim.call("POST", "/logout", raw="", headers={"Cookie": sim.cookie, "Origin": origin})
    check(status == 200 and "Max-Age=0" in (headers.get("Set-Cookie") or ""), "the cookie is cleared: %s %s" % (status, headers))


def case_a_new_password_signs_everyone_out(sim):
    sim.env["LLMSIM_ADMIN_PASSWORD"] = "a new one"
    sim.restart()
    status, _ = sim.api("GET", "/api/rules", headers={"Cookie": sim.cookie})
    check(status == 401, "the old cookie no longer works: %s" % status)
    status, _, _ = sign_in(sim, password="a new one")
    check(status == 200, "the new password does: %s" % status)
    sim.env["LLMSIM_ADMIN_PASSWORD"] = PASSWORD
    sim.restart()


def case_too_many_wrong_passwords(sim):
    for _ in range(5):
        sign_in(sim, password="wrong")
    status, headers, text = sign_in(sim)
    check(status == 429 and not headers.get("Set-Cookie"), "even the right one waits: %s" % status)


# -- only an API key: the password is what turns it on -----------------------------

def case_a_key_alone_changes_nothing(sim):
    status, _ = sim.api("GET", "/api/rules")
    check(status == 200, "with no password, /api is open as it always was: %s" % status)
    status, _, text = sim.call("GET", "/ui")
    check(status == 200 and not signing_in(text), "and so is the console")
    status, body = sim.api("GET", "/healthz")
    check("rules" in body, "and the whole health check: %s" % body)
    status, body = sim.api("GET", "/api/auth", headers={"X-LLMSim-Key": KEY})
    check(body["on"] is False, "the key is not used: %s" % body)
    status, headers, _ = sign_in(sim, password="")
    check(not headers.get("Set-Cookie"), "and nobody is signed in: %s" % status)


# -- set from Settings -------------------------------------------------------------

def act(sim, body, cookie=None, key=None, origin=True):
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    if key:
        headers["X-LLMSim-Key"] = key
    if origin:
        headers["Origin"] = "http://127.0.0.1:%d" % sim.port
    status, hdrs, text = sim.call("POST", "/api/access", body, headers)
    return status, hdrs, json.loads(text)


def case_settings_starts_open(sim):
    status, body = sim.api("GET", "/api/access")
    check(status == 200 and body["on"] is False and body["password"]["set"] is False, "open: %s" % body)
    status, _, body = act(sim, {"action": "new-key"})
    check(status == 409, "a key alone is refused while there is no password, or the console locks itself out: %s" % status)
    status, _, body = act(sim, {"action": "password", "password": "short"})
    check(status == 400, "a short password is refused: %s" % status)


def case_settings_turns_it_on(sim):
    status, headers, body = act(sim, {"action": "password", "user": "owner", "password": "first-password"})
    check(status == 200 and body["on"] and body["user"] == "owner", "on: %s %s" % (status, body))
    sim.cookie = cookie_of(headers)
    check(sim.cookie.startswith("llmsim_session_%d=" % sim.port), "whoever turned it on stays signed in: %s" % headers.get("Set-Cookie"))
    status, _ = sim.api("GET", "/api/rules")
    check(status == 401, "now /api needs a sign-in: %s" % status)
    status, _ = sim.api("GET", "/api/rules", headers={"Cookie": sim.cookie})
    check(status == 200, "and the cookie it gave works: %s" % status)
    status, _, _ = sign_in(sim, user="owner", password="first-password")
    check(status == 200, "/login takes the new password: %s" % status)
    kept = open(sim.env["LLMSIM_ACCESS"]).read()
    check("first-password" not in kept and "pbkdf2_sha256$" in kept, "only a hash is kept")
    check(oct(os.stat(sim.env["LLMSIM_ACCESS"]).st_mode & 0o777) == "0o600", "owner-only")


def case_settings_asks_for_the_current_password(sim):
    status, _, body = act(sim, {"action": "new-key"}, cookie=sim.cookie)
    check(status == 403, "a change made with a cookie needs the current password: %s" % status)
    status, _, body = act(sim, {"action": "new-key", "current": "wrong"}, cookie=sim.cookie)
    check(status == 403, "and the right one: %s" % status)
    status, _, _ = sim.call("POST", "/api/access", {"action": "off", "current": "first-password"},
                            {"Cookie": sim.cookie, "Origin": "http://localhost:3000"})
    check(status == 403, "another site cannot use the cookie here either: %s" % status)


def case_settings_makes_a_key(sim):
    status, _, body = act(sim, {"action": "new-key", "current": "first-password"}, cookie=sim.cookie)
    check(status == 200 and body["key"].startswith("llmsim-"), "made: %s" % status)
    sim.key = body["key"]
    status, st = sim.api("GET", "/api/access", headers={"X-LLMSim-Key": sim.key})
    check(status == 200 and st["key"] == {"set": True, "from": "settings", "env": False, "hint": "…" + sim.key[-4:]}, "the key works, and only its end is shown: %s" % st)
    check(sim.key not in open(sim.env["LLMSIM_ACCESS"]).read(), "only a hash of the key is kept")
    status, _, body = act(sim, {"action": "new-key", "current": "first-password"}, cookie=sim.cookie)
    status, _ = sim.api("GET", "/api/rules", headers={"X-LLMSim-Key": sim.key})
    check(status == 401, "a new key replaces the old one at once: %s" % status)
    sim.key = body["key"]


def case_settings_changes_the_password(sim):
    old = sim.cookie
    status, headers, body = act(sim, {"action": "password", "password": "second-password", "current": "first-password"}, cookie=old)
    check(status == 200, "changed: %s %s" % (status, body))
    sim.cookie = cookie_of(headers)
    status, _ = sim.api("GET", "/api/rules", headers={"Cookie": old})
    check(status == 401, "the cookie made before no longer works: %s" % status)
    status, _ = sim.api("GET", "/api/rules", headers={"Cookie": sim.cookie})
    check(status == 200, "the one handed back does: %s" % status)
    status, _, _ = act(sim, {"action": "password", "password": "third-password"}, key=sim.key)
    check(status == 200, "the API key may set the password without it: %s" % status)
    status, _, _ = act(sim, {"action": "password", "password": "second-password"}, key=sim.key)


def case_settings_survive_a_restart(sim):
    sim.restart()
    status, _ = sim.api("GET", "/api/rules", headers={"X-LLMSim-Key": sim.key})
    check(status == 200, "the key after a restart: %s" % status)
    status, headers, _ = sign_in(sim, user="owner", password="second-password")
    check(status == 200, "the password after a restart: %s" % status)
    sim.cookie = cookie_of(headers)


def case_settings_removes_the_key_and_turns_off(sim):
    status, _, _ = act(sim, {"action": "remove-key", "current": "second-password"}, cookie=sim.cookie)
    check(status == 200, "removed: %s" % status)
    status, _ = sim.api("GET", "/api/rules", headers={"X-LLMSim-Key": sim.key})
    check(status == 401, "the key is refused: %s" % status)
    status, headers, body = act(sim, {"action": "off", "current": "second-password"}, cookie=sim.cookie)
    check(status == 200 and body["on"] is False and "Max-Age=0" in (headers.get("Set-Cookie") or ""), "off: %s" % body)
    status, _ = sim.api("GET", "/api/rules")
    check(status == 200, "open again: %s" % status)


def case_settings_replaces_what_the_environment_gave(sim):
    status, headers, _ = sign_in(sim)
    check(status == 200, "the first sign-in is LLMSIM_ADMIN_PASSWORD: %s" % status)
    cookie = cookie_of(headers)
    status, body = sim.api("GET", "/api/access", headers={"Cookie": cookie})
    check(body["password"] == {"set": True, "from": "env", "env": True} and body["key"]["from"] == "env"
          and body["key"]["hint"] == "…" + KEY[-4:], "said where: %s" % body)
    status, _, body = act(sim, {"action": "off", "current": PASSWORD}, cookie=cookie)
    check(status == 409 and "LLMSIM_ADMIN_PASSWORD" in body["message"], "it cannot be turned off from here: %s" % status)
    status, _, body = act(sim, {"action": "remove-key", "current": PASSWORD}, cookie=cookie)
    check(status == 409, "nor the environment's key removed: %s" % status)
    status, headers, body = act(sim, {"action": "password", "password": "set-in-settings", "current": PASSWORD}, cookie=cookie)
    check(status == 200 and body["password"]["from"] == "settings", "Settings changes the password: %s %s" % (status, body.get("message")))
    cookie = cookie_of(headers)
    status, _, _ = sign_in(sim, password=PASSWORD)
    check(status == 401, "the environment's password no longer works: %s" % status)
    status, _, body = act(sim, {"action": "new-key", "current": "set-in-settings"}, cookie=cookie)
    check(status == 200 and body["key"].startswith("llmsim-"), "Settings makes a key: %s" % status)
    status, st = sim.api("GET", "/api/access", headers={"Cookie": cookie})
    check(st["key"]["from"] == "settings" and st["key"]["env"], "in use, in place of the environment's: %s" % st["key"])
    status, _ = sim.api("GET", "/api/rules", headers={"X-LLMSim-Key": KEY})
    check(status == 401, "which replaces LLMSIM_API_KEY: %s" % status)
    sim.restart()
    status, _, _ = sign_in(sim, password="set-in-settings")
    check(status == 200, "a restart keeps what Settings set over the environment: %s" % status)
    status, _, body = act(sim, {"action": "remove-key", "current": "set-in-settings"}, cookie=cookie)
    check(status == 200 and "LLMSIM_API_KEY works again" in body["message"], "removing the key made here: %s" % body.get("message"))
    status, _ = sim.api("GET", "/api/rules", headers={"X-LLMSim-Key": KEY})
    check(status == 200, "brings the environment's back: %s" % status)


def run(title, sim, cases):
    print(title)
    failed = 0
    sim.start()
    try:
        for case in cases:
            try:
                case(sim)
                print("  ok    %s" % case.__name__[5:].replace("_", " "))
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print("  FAIL  %s\n        %s: %s" % (case.__name__[5:].replace("_", " "), type(exc).__name__, str(exc)[:700]))
    finally:
        sim.stop()
    return len(cases), failed


def main():
    groups = [
        ("open", Sim(), [case_open_by_default]),
        ("signed in", Sim(env={"LLMSIM_ADMIN_PASSWORD": PASSWORD, "LLMSIM_API_KEY": KEY}), [
            case_api_refused_without_credentials, case_console_asks_for_a_sign_in_in_place, case_health_says_only_alive,
            case_agents_routes_stay_open, case_api_key, case_client_sends_the_key, case_wrong_password,
            case_sign_in_and_use_the_cookie, case_cookie_refused_from_another_site, case_forged_cookie,
            case_sign_in_from_another_site, case_cookie_survives_a_restart, case_sign_out, case_a_new_password_signs_everyone_out,
            case_too_many_wrong_passwords]),
        ("key only", Sim(env={"LLMSIM_API_KEY": KEY}), [case_a_key_alone_changes_nothing]),
        ("from Settings", Sim(), [
            case_settings_starts_open, case_settings_turns_it_on, case_settings_asks_for_the_current_password,
            case_settings_makes_a_key, case_settings_changes_the_password, case_settings_survive_a_restart,
            case_settings_removes_the_key_and_turns_off]),
        ("Settings beside the environment", Sim(env={"LLMSIM_ADMIN_PASSWORD": PASSWORD, "LLMSIM_API_KEY": KEY}),
         [case_settings_replaces_what_the_environment_gave]),
    ]
    total = failed = 0
    for title, sim, cases in groups:
        n, f = run(title, sim, cases)
        total, failed = total + n, failed + f
    print("\n%d passed, %d failed" % (total - failed, failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
