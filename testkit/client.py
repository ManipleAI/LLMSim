"""A client for a LLMSim server, for scripts, CI and tests. Standard library only.

    from testkit.client import Client
    result = Client("http://localhost:8099").run(scenario)
"""

import json
import urllib.error
import urllib.request


class LLMSimError(Exception):
    pass


class Client:
    def __init__(self, server=None, api_key=None):
        import os
        self.server = (server or os.environ.get("LLMSIM_URL") or "http://localhost:8099").rstrip("/")
        # Needed once the server asks for a sign-in (LLMSIM_API_KEY on it); sent only when there is one.
        self.api_key = api_key if api_key is not None else os.environ.get("LLMSIM_API_KEY", "")

    def call(self, method, path, body=None, timeout=3700):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-LLMSim-Key"] = self.api_key
        req = urllib.request.Request(self.server + path, json.dumps(body).encode() if body is not None else None, headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status, text = resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            status, text = exc.code, exc.read().decode()
        except (urllib.error.URLError, OSError) as exc:
            raise LLMSimError("cannot reach %s: %s" % (self.server, getattr(exc, "reason", exc)))
        try:
            return status, json.loads(text)
        except ValueError:
            return status, {"raw": text}

    def _ok(self, status, body):
        if status >= 400 or body.get("ok") is False:
            raise LLMSimError(body.get("message") or "HTTP %d" % status)
        return body

    def run(self, scenario, **options):
        """The result of running a scenario (a dict) or a saved one (its id)."""
        if isinstance(scenario, str):
            return self._ok(*self.call("POST", "/api/scenarios/%s/run" % scenario, options))["result"]
        return self._ok(*self.call("POST", "/api/scenarios/run", dict(options, scenario=scenario)))["result"]

    def junit(self, run):
        status, body = self.call("GET", "/api/results/%s/junit" % run)
        return body.get("raw") or body.get("junit", "")

    def experiment(self, **spec):
        return self._ok(*self.call("POST", "/api/experiments/run", spec))

    def gate(self, experiment, **options):
        return self._ok(*self.call("POST", "/api/experiments/%s/gate" % experiment, options))

    def compare(self, baseline, candidate, strict=False):
        return self._ok(*self.call("GET", "/api/experiments/%s/compare/%s%s" % (baseline, candidate, "?strict=1" if strict else "")))

    def set_baseline(self, experiment):
        return self._ok(*self.call("POST", "/api/experiments/%s/baseline" % experiment, {}))


def failure_text(result):
    """Why a run did not pass, in the words a person reads in a test failure."""
    lines = ["scenario %s: %s (run %s)" % (result["scenario"], result["verdict"], result["run"])]
    if result.get("error"):
        lines.append("  error: %s" % result["error"])
    for c in result.get("checks") or []:
        if c["status"] != "pass":
            lines.append("  %s %s: %s" % (c["status"], c["name"], c["detail"]))
    return "\n".join(lines)


def markdown(exp, gate=None):
    """A summary a pull request shows: the verdict, then every case that matters by name."""
    g = gate or {}
    head = "### LLMSim: %s" % (g.get("verdict") or exp["verdict"]).upper()
    lines = [head, "", "`%s` v%s, %s tier, %d of %d case(s) passed." % (exp["dataset"], exp["version"], exp["tier"], exp["summary"]["pass"] + exp["summary"]["flaky"], len(exp["items"])), ""]
    if g:
        for label, title in (("regressed", "Regressed"), ("degraded", "Got flaky"), ("fixed", "Fixed")):
            if g.get(label):
                lines += ["**%s (%d)**" % (title, len(g[label])), ""]
                for e in g[label]:
                    lines.append("- `%s` — %s → %s (%s)%s" % (e["id"], e["was"], e["now"], e["passes"], (": " + e["detail"]) if e.get("detail") and label != "fixed" else ""))
                lines.append("")
        if g.get("new"):
            lines.append("New: " + ", ".join("`%s`" % e["id"] for e in g["new"]) + "\n")
        if g.get("removed"):
            lines.append("Removed: " + ", ".join("`%s`" % e["id"] for e in g["removed"]) + "\n")
        if g.get("below"):
            lines.append("**%s**\n" % g["below"])
        lines.append("Tokens %+d, time %+.1fs against baseline `%s`." % (g.get("tokens", 0), g.get("durationS", 0), g.get("baseline")))
    else:
        for it in exp["items"]:
            if it["status"] != "pass":
                lines.append("- `%s` — %s (%d/%d)" % (it["id"], it["status"], it["passes"], it["n"]))
    return "\n".join(lines) + "\n"
