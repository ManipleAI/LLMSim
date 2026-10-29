#!/usr/bin/env python3
"""Who may use the console and the API: a sign-in with a cookie, an API key, and
what stays open because an agent under test cannot sign in.

    python3 tests/test_access.py
"""

import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kit import Sim, OPENAI_KEY, CHAT  # noqa: E402
from testkit.client import Client  # noqa: E402

PASSWORD, KEY = "correct horse battery", "llmsim-test-key-0123456789"
RULE = {"rule": {"name": "hi", "when": {"contains": "access-test"}, "turns": [{"text": "yes"}]}}


def form(**fields):
    return urllib.parse.urlencode(fields)


def sign_in(sim, user="admin", password=PASSWORD, next_path="/ui", headers=None):
    return sim.call("POST", "/login", raw=form(user=user, password=password, next=next_path),
                    headers=dict({"Content-Type": "application/x-www-form-urlencoded"}, **(headers or {})))


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
    status, _, _ = sim.call("GET", "/ui")
    check(status == 200, "and serves the console: %s" % status)
    status, body = sim.api("GET", "/healthz")
    check("rules" in body, "and its health check says everything: %s" % body)


# -- on: what is guarded and what is not -----------------------------------------

def case_api_refused_without_credentials(sim):
    status, headers, _ = sim.call("GET", "/api/rules")
    check(status == 401, "no credentials: %s" % status)
    check(headers.get("X-LLMSim-Sign-In") == "/login", "the console is told where to sign in: %s" % headers)
    status, _, _ = sim.call("POST", "/api/rules", RULE)
    check(status == 401, "a change is refused too: %s" % status)
    status, _, _ = sim.call("GET", "/run/r1/agent/a/api/rules")
    check(status == 401, "a /run/…/agent/… prefix does not get round it: %s" % status)
    status, _, _ = sim.call("GET", "/openai/api/rules")
    check(status == 401, "nor does a provider prefix: %s" % status)
    status, _, _ = sim.call("POST", "/api/ui/chat", {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]})
    check(status == 401, "the console's own routes are guarded: %s" % status)


def case_console_sends_you_to_sign_in(sim):
    status, headers, _ = sim.call("GET", "/ui")
    check(status == 303 and headers.get("Location") == "/login?next=/ui", "the console redirects: %s %s" % (status, headers.get("Location")))
    status, _, text = sim.call("GET", "/login")
    check(status == 200 and 'name="password"' in text, "the sign-in page is served: %s" % status)


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
    check(status == 401 and "do not match" in text, "refused: %s" % status)
    check(not headers.get("Set-Cookie"), "and no cookie")
    status, headers, _ = sign_in(sim, user="root")
    check(status == 401 and not headers.get("Set-Cookie"), "the wrong user is refused too: %s" % status)


def case_sign_in_and_use_the_cookie(sim):
    status, headers, _ = sign_in(sim, next_path="/ui#lab/scenarios")
    check(status == 303 and headers.get("Location") == "/ui#lab/scenarios", "back to the screen: %s %s" % (status, headers.get("Location")))
    raw = headers.get("Set-Cookie") or ""
    for part in ("llmsim_session_%d=" % sim.port, "HttpOnly", "SameSite=Strict", "Path=/"):
        check(part in raw, "the cookie has %s: %s" % (part, raw))
    sim.cookie = cookie_of(headers)
    status, _, _ = sim.call("GET", "/ui", headers={"Cookie": sim.cookie})
    check(status == 200, "the console with the cookie: %s" % status)
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


def case_next_never_leaves(sim):
    for target in ("//evil.example/", "https://evil.example/", "\\\\evil", "/ui\r\nSet-Cookie: x=1"):
        status, headers, _ = sign_in(sim, next_path=target)
        check(headers.get("Location") == "/ui", "next=%r went to %s" % (target, headers.get("Location")))


def case_cookie_survives_a_restart(sim):
    sim.restart()
    status, _ = sim.api("GET", "/api/rules", headers={"Cookie": sim.cookie})
    check(status == 200, "signed in after a restart: %s" % status)
    check(os.path.exists(sim.env["LLMSIM_SECRET"]), "the signing secret is kept")
    check(oct(os.stat(sim.env["LLMSIM_SECRET"]).st_mode & 0o777) == "0o600", "owner-only")


def case_sign_out(sim):
    origin = "http://127.0.0.1:%d" % sim.port
    status, headers, _ = sim.call("POST", "/logout", raw="", headers={"Cookie": sim.cookie, "Origin": origin})
    check(status == 303 and "Max-Age=0" in (headers.get("Set-Cookie") or ""), "the cookie is cleared: %s %s" % (status, headers))


def case_a_new_password_signs_everyone_out(sim):
    sim.env["LLMSIM_ADMIN_PASSWORD"] = "a new one"
    sim.restart()
    status, _ = sim.api("GET", "/api/rules", headers={"Cookie": sim.cookie})
    check(status == 401, "the old cookie no longer works: %s" % status)
    status, _, _ = sign_in(sim, password="a new one")
    check(status == 303, "the new password does: %s" % status)
    sim.env["LLMSIM_ADMIN_PASSWORD"] = PASSWORD
    sim.restart()


def case_too_many_wrong_passwords(sim):
    for _ in range(5):
        sign_in(sim, password="wrong")
    status, headers, text = sign_in(sim)
    check(status == 429 and not headers.get("Set-Cookie"), "even the right one waits: %s" % status)


# -- only an API key -------------------------------------------------------------

def case_key_only_has_no_console_password(sim):
    status, _, text = sim.call("GET", "/login")
    check("No console password is set" in text, "the page says so")
    status, headers, _ = sign_in(sim, password="")
    check(status == 401 and not headers.get("Set-Cookie"), "an empty password never signs in: %s" % status)
    status, _ = sim.api("GET", "/api/rules", headers={"X-LLMSim-Key": KEY})
    check(status == 200, "the key works: %s" % status)


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
            case_api_refused_without_credentials, case_console_sends_you_to_sign_in, case_health_says_only_alive,
            case_agents_routes_stay_open, case_api_key, case_client_sends_the_key, case_wrong_password,
            case_sign_in_and_use_the_cookie, case_cookie_refused_from_another_site, case_forged_cookie,
            case_next_never_leaves, case_cookie_survives_a_restart, case_sign_out, case_a_new_password_signs_everyone_out,
            case_too_many_wrong_passwords]),
        ("key only", Sim(env={"LLMSIM_API_KEY": KEY}), [case_key_only_has_no_console_password]),
    ]
    total = failed = 0
    for title, sim, cases in groups:
        n, f = run(title, sim, cases)
        total, failed = total + n, failed + f
    print("\n%d passed, %d failed" % (total - failed, failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
