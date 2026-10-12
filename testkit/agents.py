"""The agents under test: who they are, and how each is reached.

A scenario used to carry its agent inside it: a driver and that driver's settings, typed
into every test. That is right for one test of one agent and wrong for a team, where the
same agent is the subject of fifty scenarios and the same scenario is run against the
build in staging and the one in production. An agent is now something saved once:

    {"id": "support-prod", "label": "Support agent (production)", "system": "servicenow",
     "connection": {"driver": "a2a", "url": "https://…", "credential": "sn-oauth"}}

and a scenario names it — `"agent": {"use": "support-prod"}` — or is simply run on it:
`POST /api/scenarios/<id>/run {"agent": "support-prod"}`.

THE SYSTEMS

An agent lives on a platform, and the platform decides how it is reached. `SYSTEMS` is
what this kit knows about each: which protocols it answers on, what the address looks
like, what kind of credential it wants, and — said plainly, because it decides what a
test can prove — what LLMSim can see of an agent that runs there.

That last part is the honest limit of all of this. LLMSim stands in for the model, the
tools and the peers of an agent it is in front of. An agent hosted on Salesforce or
ServiceNow calls its own model and its own tools, and LLMSim is in front of none of
them: it sees the conversation, and anything the agent was pointed at here. Checks on
what the agent said, how long it took and whether a judge is satisfied work everywhere.
Checks on which tools it called work only where those calls come here, or are sent as
OpenTelemetry spans.

Nothing here was run against a live Salesforce, SAP, ServiceNow or Azure tenant. Each
connection is written from the vendor's published documentation, and tested against a
stand-in written from the same.
"""

import copy
import re
import time

from . import core, credentials, drivers, rest_area
from .store import store

AGENT_KEYS = {"id", "label", "system", "description", "tags", "connection"}
PLACEHOLDER = re.compile(r"<[a-z][a-z0-9 -]*>")

A2A_SEES = ("The conversation, turn by turn, and the task states behind it. Its model and its tools are the platform's own, "
            "so tool-call and token checks have nothing to read unless the agent's tools are pointed at LLMSim or it sends OpenTelemetry spans here.")

SYSTEMS = [
    {"id": "salesforce", "name": "Salesforce Agentforce", "about": "An Agentforce agent in a Salesforce org.",
     "connections": [
         {"driver": "agentforce", "label": "Agent API", "recommended": True,
          "note": "A session is opened, each turn is a message in it, and it is ended afterwards. It needs an external client app that "
                  "allows the client-credentials flow. Salesforce does not offer this API for agents built with Agent Script or for the "
                  "Agentforce (Default) agent.",
          "defaults": {"domain": "https://<my-domain>.my.salesforce.com", "agentId": "<agent-id>"},
          "credential": {"type": "oauth_client_credentials", "tokenUrl": "https://<my-domain>.my.salesforce.com/services/oauth2/token",
                         "note": "The consumer key and secret of the external client app."}},
         {"driver": "a2a", "label": "Agent2Agent (A2A)",
          "note": "Salesforce describes A2A for Agentforce as a beta feature. Use the address your org publishes for the agent.",
          "defaults": {"url": "https://<agent-a2a-address>"},
          "credential": {"type": "oauth_client_credentials", "tokenUrl": "https://<my-domain>.my.salesforce.com/services/oauth2/token"}}],
     "sees": "The conversation. Agentforce runs its own model and actions, so tool-call and token checks have nothing to read.",
     "docs": ["https://developer.salesforce.com/docs/einstein/genai/guide/agent-api.html"]},

    {"id": "sap", "name": "SAP Joule", "about": "A Joule agent on SAP Business Technology Platform.",
     "connections": [
         {"driver": "a2a", "label": "Agent2Agent (A2A)", "recommended": True,
          "note": "SAP's agents speak A2A 0.3, and agents outside SAP reach them through the SAP Agent Gateway. SAP's own reference "
                  "architecture says access from outside through the gateway is still being rolled out, so check that your landscape has it.",
          "defaults": {"url": "https://<agent-gateway-address>"},
          "credential": {"type": "oauth_client_credentials", "tokenUrl": "https://<tenant>.accounts.ondemand.com/oauth2/token",
                         "note": "A client registered in SAP Cloud Identity Services that is trusted to call the agent."}}],
     "sees": A2A_SEES,
     "docs": ["https://architecture.learning.sap.com/docs/ref-arch/ae6821"]},

    {"id": "servicenow", "name": "ServiceNow", "about": "An AI agent on a ServiceNow instance, reached through AI Agent Fabric.",
     "connections": [
         {"driver": "a2a", "label": "Agent2Agent (A2A)", "recommended": True,
          "note": "ServiceNow answers A2A 0.3. In AI Agent Studio, under Settings, External AI agents, switch on "
                  "\"Allow third party to access ServiceNow AI agents\", and use the address the instance gives for the agent.",
          "defaults": {"url": "https://<instance>.service-now.com/<agent-a2a-path>"},
          "credential": {"type": "oauth_client_credentials", "tokenUrl": "https://<instance>.service-now.com/oauth_token.do",
                         "note": "An OAuth application registry entry on the instance. An API key is a header credential named x-sn-apikey instead."}}],
     "sees": A2A_SEES,
     "docs": ["https://www.servicenow.com/docs/r/intelligent-experiences/external-agent-protocols.html"]},

    {"id": "azure-foundry", "name": "Azure AI Foundry", "about": "An agent in Microsoft Foundry Agent Service.",
     "connections": [
         {"driver": "a2a", "label": "Agent2Agent (A2A)", "recommended": True,
          "note": "Incoming A2A has to be enabled on the agent, and the identity that calls it needs the Foundry Agent Consumer role. "
                  "The agent card is not at the usual address and is not public, which is why it is named here. Text only, and no streaming. "
                  "This driver speaks A2A 0.3, which Foundry serves as a preview; 1.0 is its generally available version.",
          "defaults": {"url": "https://<account>.services.ai.azure.com/api/projects/<project>/agents/<agent>/endpoint/protocols/a2a",
                       "card": "agentCard/v0.3", "headers": {"A2A-Version": "0.3"}, "wait": "poll"},
          "credential": {"type": "oauth_client_credentials", "tokenUrl": "https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token",
                         "scope": "https://ai.azure.com/.default", "note": "A service principal in Microsoft Entra ID. Keys are not accepted."}},
         {"driver": "responses", "label": "Responses API",
          "note": "The agent is named in the request as an agent reference. Microsoft documents this through its SDK; the address below is "
                  "the one the SDK uses and was not confirmed against a live project.",
          "defaults": {"url": "https://<account>.services.ai.azure.com/api/projects/<project>/openai/v1",
                       "extra": {"agent_reference": {"type": "agent_reference", "name": "<agent>"}}},
          "credential": {"type": "oauth_client_credentials", "tokenUrl": "https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token",
                         "scope": "https://ai.azure.com/.default"}}],
     "sees": "The conversation. A Foundry agent's model and tools are Azure's, so tool-call and token checks have nothing to read "
             "unless its tools are pointed at LLMSim.",
     "docs": ["https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint"]},

    {"id": "custom", "name": "Custom", "about": "An agent you run yourself, on any framework.",
     "connections": [
         {"driver": "a2a", "label": "Agent2Agent (A2A)", "note": "Any agent that publishes an agent card and answers message/send.",
          "defaults": {"url": "https://<agent-address>"}},
         {"driver": "openai", "label": "OpenAI-compatible chat", "note": "An endpoint that answers /chat/completions.",
          "defaults": {"url": "https://<agent-address>/v1"}},
         {"driver": "responses", "label": "Responses API", "note": "An endpoint that answers /responses, as the OpenAI Agents SDK serves.",
          "defaults": {"url": "https://<agent-address>/v1"}},
         {"driver": "http", "label": "HTTP: post a message, read a reply", "recommended": True,
          "note": "Anything that takes a POST. Say what is sent and where in the answer the reply is.",
          "defaults": {"url": "https://<agent-address>/chat", "body": {"input": "{{message}}"}, "reply": "reply"}},
         {"driver": "process", "label": "A local program", "note": "The message arrives on stdin and the reply is read from stdout.",
          "defaults": {"command": ["python3", "agent.py"]}}],
     "sees": "Everything it is pointed at. Give the agent LLMSim's address as its model, its tools and its peers, and every call it makes "
             "is on the run's timeline.",
     "docs": []},
]
BY_ID = {s["id"]: s for s in SYSTEMS}


def _placeholders(value, where):
    """A connection made from a system's template still has the template's blanks in it until someone fills them in."""
    if isinstance(value, str):
        return ["%s still has %s in it: put the real value there" % (where, m) for m in PLACEHOLDER.findall(value)][:1]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _placeholders(v, "%s.%s" % (where, k))]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _placeholders(v, "%s[%d]" % (where, i))]
    return []


def validate(defn):
    if not isinstance(defn, dict):
        return ["an agent is an object"]
    problems = ["unknown field %r" % k for k in defn if k not in AGENT_KEYS]
    if not core.valid_id(defn.get("id")):
        problems.append("id is required: letters, digits, dot, dash, underscore or colon, up to 64")
    for key in ("label", "description"):
        if key in defn and not isinstance(defn[key], str):
            problems.append("%s is text" % key)
    tags = defn.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) and 0 < len(t) <= 40 for t in tags):
        problems.append("tags is a list of short words")
    system = BY_ID.get(defn.get("system"))
    if system is None:
        problems.append("system is one of %s" % ", ".join(s["id"] for s in SYSTEMS))
    conn = defn.get("connection")
    if not isinstance(conn, dict):
        return problems + ["connection is {driver, …}: how the agent is reached"]
    for key in ("name", "turns", "user", "use"):
        if key in conn:
            problems.append("connection.%s belongs to a scenario, not to the agent" % key)
    if system is not None and conn.get("driver") not in [c["driver"] for c in system["connections"]]:
        problems.append("%s is reached by %s" % (system["name"], " or ".join(c["driver"] for c in system["connections"])))
    else:
        problems += ["connection: " + p for p in drivers.validate(conn)]
    problems += _placeholders({k: v for k, v in conn.items() if k != "credential"}, "connection")
    if conn.get("credential") and core.valid_id(conn["credential"]) and not credentials.exists(conn["credential"]):
        problems.append("connection.credential: there is no stored credential called %s" % conn["credential"])
    return problems


# -- naming a saved agent from a scenario --------------------------------------------------

def uses(scenario):
    """The saved agents a scenario names."""
    parts = scenario.get("agents") if isinstance(scenario.get("agents"), list) else [scenario.get("agent")]
    return [a["use"] for a in parts if isinstance(a, dict) and a.get("use")]


def resolve(scenario, override=None):
    """(scenario, problems, refs): the scenario with every named agent replaced by how that agent is reached.

    `override` runs the scenario on a saved agent whatever it says itself: an id for a scenario with one
    agent, or {name: id} for one with several. What is the scenario's own stays — the agent's name on the
    timeline, which checks refer to, and what is said to it."""
    problems, refs = [], []
    multi = isinstance(scenario.get("agents"), list)
    if override is not None:
        if multi and not isinstance(override, dict):
            return scenario, ["this scenario drives several agents: say which with agent: {<name>: <saved agent>}"], []
        if not multi and not isinstance(override, str):
            return scenario, ["agent is the id of a saved agent"], []
    if not override and not uses(scenario):
        return scenario, [], []
    sc = copy.deepcopy(scenario)

    def one(part, forced):
        ref = forced or part.get("use")
        if not ref:
            return part
        saved = store().get("agents", ref)
        if saved is None:
            problems.append("there is no saved agent called %s (see /api/agents)" % ref)
            return part
        refs.append(ref)
        own = {k: part[k] for k in ("name", "turns", "user") if k in part}
        return dict(saved["connection"], **dict({"name": ref}, **own))

    if multi:
        unknown = sorted(set(override or {}) - {a.get("name") for a in sc["agents"] if isinstance(a, dict)})
        if unknown:
            problems.append("this scenario has no agent called %s" % ", ".join(unknown))
        sc["agents"] = [one(a, (override or {}).get(a.get("name"))) for a in sc["agents"]]
    else:
        sc["agent"] = one(sc["agent"], override)
    return sc, problems, refs


def validate_reference(part, where):
    """A scenario's agent that names a saved one holds the name, and what is the scenario's own."""
    problems = ["%s: with use, only name is given beside it; the rest is the saved agent's" % where
                for k in part if k not in {"use", "name", "turns", "user"}][:1]
    if not core.valid_id(part.get("use")):
        problems.append("%s.use is the id of a saved agent" % where)
    return problems


# -- is it there ---------------------------------------------------------------------------

def test(defn, say="Hello", base_host="localhost:8099"):
    """Reach the agent the way a run would, and say what happened at each step.

    Three steps, each of which can be the one that is wrong: the credential (is there one, and
    does the platform give a token for it), the connection (does the address answer as that
    protocol), and one message. The message is the only step that proves the agent answers, and
    it is a real message to a real agent, so it is the caller's to send or not."""
    conn, steps = defn["connection"], []
    ctx = drivers.Ctx(core.new_id("probe-", 8), defn["id"], {"self": "http://" + base_host}, timeout=float(conn.get("timeoutS") or 30))

    def step(name, status, detail, **extra):
        steps.append(dict({"step": name, "status": status, "detail": detail}, **extra))
        return status != "fail"

    def finish(reply=None):
        failed = [s for s in steps if s["status"] == "fail"]
        unsure = [s for s in steps if s["status"] == "warn"]
        out = {"ok": not failed, "status": "failing" if failed else ("connected" if reply is not None or not unsure else "unverified"),
               "steps": steps, "at": time.time(), "said": say or "", "reply": reply}
        return core.redact(out)

    label = conn.get("credential")
    if label:
        try:
            drivers._auth_headers(conn, ctx)
            kind = next((c["type"] for c in credentials.listing() if c["label"] == label), "")
            step("credential", "ok", "%s is stored%s." % (label, " and the platform issued a token for it" if kind == "oauth_client_credentials" else ""))
        except drivers.DriverError as exc:
            step("credential", "fail", str(exc))
            return finish()
    else:
        step("credential", "warn", "No credential is named, so nothing is sent to say who is calling.")
    driver = drivers.make(dict(conn, name=defn["id"]), ctx)
    reply = None
    try:
        driver.start()
        status, detail = driver.describe()
        if step("connection", status, detail) and say:
            started = time.time()
            answer = driver.send(say)
            reply = answer.text
            step("message", "ok" if reply.strip() else "warn", "It answered in %d ms." % answer.ms if reply.strip() else
                 "It answered, and the answer had no text in it.", ms=int((time.time() - started) * 1000))
    except drivers.DriverError as exc:
        step("message" if any(s["step"] == "connection" for s in steps) else "connection", "fail", str(exc))
    except Exception as exc:  # noqa: BLE001 — a probe that crashed is a result, and the person asked to be told
        step("connection", "fail", "%s: %s" % (type(exc).__name__, exc))
    finally:
        try:
            driver.end()
        except Exception:  # noqa: BLE001
            pass
    return finish(reply)


def listing():
    st = store()
    out = []
    for agent_id, defn, updated in st.docs("agents"):
        last = st.get("agentchecks", agent_id) or {}
        system = BY_ID.get(defn.get("system"), {})
        out.append({"id": agent_id, "label": defn.get("label", ""), "system": defn.get("system"), "systemName": system.get("name", defn.get("system")),
                    "driver": (defn.get("connection") or {}).get("driver"), "credential": (defn.get("connection") or {}).get("credential", ""),
                    "description": defn.get("description", ""), "tags": defn.get("tags", []), "status": last.get("status", "not tested"),
                    "checkedAt": last.get("at"), "updated": updated})
    return sorted(out, key=lambda a: a["id"])


def used_by(agent_id):
    return sorted(sid for sid, sc, _ in store().docs("scenarios") if agent_id in uses(sc))


def coverage(agent_id):
    """Every saved scenario, and how its last run on this agent went."""
    last = {}
    for run, result, _ in store().docs("results"):                 # oldest first, so the newest is what is left
        if agent_id in (result.get("agentRefs") or []) and result.get("status") == "done":
            last[result.get("scenario")] = {"run": run, "verdict": result.get("verdict"), "finishedAt": result.get("finishedAt"),
                                            "summary": result.get("summary"), "error": result.get("error")}
    rows = []
    for sid, sc, _ in store().docs("scenarios"):
        rows.append({"scenario": sid, "description": sc.get("description", ""), "tags": sc.get("tags", []), "checks": len(sc.get("checks") or []),
                     "several": isinstance(sc.get("agents"), list), "names": agent_id in uses(sc), "last": last.get(sid)})
    return sorted(rows, key=lambda r: r["scenario"])


@rest_area("agents")
def _rest(method, rest, body, query, host):
    st = store()
    body = body or {}
    if method == "GET" and not rest:
        return core.ok({"agents": listing(), "systems": SYSTEMS})
    if method == "POST" and rest == ["validate"]:
        problems = validate(body.get("agent"))
        return core.ok(valid=not problems, problems=problems)
    if method == "POST" and rest == ["test"]:
        problems = validate(body.get("agent"))
        if problems:
            return core.fail(400, "That agent has %d problem(s): %s" % (len(problems), "; ".join(problems[:5])), problems=problems)
        return core.ok({"test": test(body["agent"], body.get("say", "Hello"), host)})
    if len(rest) >= 1 and core.valid_id(rest[0]):
        agent_id, found = rest[0], st.get("agents", rest[0])
        if method == "PUT" and len(rest) == 1:
            defn = dict(body, id=agent_id)
            problems = validate(defn)
            if problems:
                return core.fail(400, "That agent has %d problem(s): %s" % (len(problems), "; ".join(problems[:5])), problems=problems)
            if found and found.get("connection") != defn["connection"]:
                st.drop("agentchecks", agent_id)                  # a changed connection has not been tried
            st.put("agents", agent_id, defn)
            return core.ok({"agent": defn})
        if not found:
            return core.fail(404, "There is no agent called %s." % agent_id)
        if method == "GET" and len(rest) == 1:
            return core.ok({"agent": found, "check": st.get("agentchecks", agent_id), "usedBy": used_by(agent_id)})
        if method == "DELETE" and len(rest) == 1:
            users = used_by(agent_id)
            if users and (query.get("force") or [""])[0] not in ("1", "true"):
                return core.fail(409, "%d scenario(s) name this agent and would have nothing to run on: %s. Point them elsewhere first, "
                                 "or add ?force=1." % (len(users), ", ".join(users[:8])), usedBy=users)
            st.drop("agentchecks", agent_id)
            return core.ok(removed=st.drop("agents", agent_id))
        if method == "POST" and rest[1:] == ["test"]:
            out = test(found, body.get("say", "Hello"), host)
            st.put("agentchecks", agent_id, {k: out[k] for k in ("ok", "status", "steps", "at", "said")})
            return core.ok({"test": out})
        if method == "GET" and rest[1:] == ["coverage"]:
            return core.ok({"agent": agent_id, "scenarios": coverage(agent_id)})
    return core.fail(404, "No such route: %s /api/agents/%s" % (method, "/".join(rest)))
