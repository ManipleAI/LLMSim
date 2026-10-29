# Testing an agent, and a system of agents

LLMSim began as the model an agent talks to. The **test kit** puts it on the other sides of the agent as well
— the tools it calls, the agents it hands work to, the person who drives it — and adds what a test needs that
a simulator does not: a run kept as one timeline, scenarios, checks, datasets, repetition, and a verdict a CI job
can act on. It is in the `testkit/` package beside `server.py`, uses only the standard library, and is the
same API and the same console page (**Agent tests**) whichever platform the agent runs on.

Nothing in it is specific to a product. A platform enters in exactly two places: a **driver** (how to start a
conversation with it) and an **importer** (how to read its test cases).

## The shape of it

```
                 ┌── model ─────── scripted, recorded, faulted         (always was)
driver ──▶ AGENT ┼── tools ─────── mock MCP server, mock REST backend, state, faults
                 └── peer agents ─ A2A stub, or a proxy to a real one
                          │
                every event, in order ──▶ a run on disk ──▶ checks ──▶ a verdict
```

A **run** is everything between the first message and the last: user turns, model calls, tool calls, messages
between agents, state that changed. It is kept in SQLite (`llmsim.runs.db`, owner-only, never committed) so it
survives a restart, and read back as `GET /api/runs/<run>/timeline`.

How a request is put in a run: the base URL (`/run/<run>/agent/<name>/…`), a header (`X-LLMSim-Run`), or a W3C
`traceparent`, whose trace id is the run. A request that names no run is not kept, so the shared instance's
ordinary traffic is not recorded unless you turn `recordAll` on (`PUT /api/kit`).

## A scenario is a test

```json
{
  "id": "refund-over-limit",
  "agent": {"name": "support", "driver": "http", "url": "https://agent.example/chat",
            "body": {"input": "{{message}}", "run": "{{run}}", "agent": "{{agent}}"}, "reply": "output.text"},
  "model": {"rules": [ …what the model says, for this run only… ]},
  "setup": [{"type": "state", "seed": {"orders": {"1182": {"status": "shipped", "total": 240}}}}],
  "turns": [{"say": "refund order 1182", "expect": {"contains": "manager"}}],
  "checks": [
    {"type": "path",   "source": "tools", "mode": "strict", "expected": [{"tool": "lookup_order", "arguments": {"id": 1182}}]},
    {"type": "state",  "collection": "orders", "key": "1182", "unchanged": true},
    {"type": "budget", "tokens": {"total": 20000}, "steps": {"modelCalls": 6}}
  ]
}
```

Run it: `PUT /api/scenarios/<id>` then `POST /api/scenarios/<id>/run`, or `bin/llmsim run scenario.json --junit out.xml`
(exit 0 pass, 1 fail, 2 could not run). A scenario is validated when saved, not when it first fails mid-run.
The model script and everything else a scenario installs applies to **that run only** and is removed with it.

**Drivers:** `http` (a template and a reply path), `openai`, `a2a` (message/send, polling, push, streaming,
input-required), `agentforce` (Salesforce Agent API session), `process` (off unless LLMSim is started with
`LLMSIM_ALLOW_PROCESS=1`: anyone who can reach the port could otherwise name a command).
Credentials live in `/api/credentials`, apart from scenarios, in an owner-only file. The API never shows a value
again, and every value the kit holds is scrubbed from everything it stores or shows.

## Checks

| Check | Says |
|---|---|
| `answer` | the reply contains / equals / matches / has this JSON shape / this number within a tolerance |
| `path` | the tool calls made, against a reference: `strict`, `unordered`, `subset` (nothing beyond it), `superset` (at least it) |
| `budget` | tokens, cost, latency, steps — per run or per agent, and which limit was passed |
| `state` | a record has these fields, is untouched, is gone; **only** these records changed; nothing was deleted |
| `leak` | a secret, or an injected instruction's marker, turned up in a reply, a tool call's arguments, or a message to a peer |
| `routing` `handoff` | the right agent got the task; what was passed on carried what it must and nothing it must not |
| `authority` | an agent used only the tools and records it may — also when a *peer* asked it to |
| `termination` | no loop, no ping-pong, bounded depth and chatter |
| `duplication` | no two agents did the same work, or wrote the same record differently |
| `degradation` | when a peer or tool failed, the answer said so and did not claim success |
| `memory` | what one agent wrote to shared memory is what the other acted on |
| `run` `assertions` | anything the server's condition language, or an assertion set, can say about the run |
| `code` | a Python function in a file the operator controls (see below) |
| `judge` `conversation` | a model decides, against a rubric, and must say why |

**No data is not a pass.** An agent that never ran satisfies every "must not"; a check with nothing to judge
says `no-data`, and so does the run.

## Tools

* **Mock MCP server** — `PUT /api/mcp/<id>`; the agent is pointed at `/run/<run>/agent/<name>/mcp/<id>`. Tools
  are written down: arguments validated against `inputSchema`, results chosen by condition, or **state** ops
  (`get`, `put`, `update`, `delete`, `list`) on a per-run record store. A write in turn two is what a read in turn
  four returns; the next run starts clean. Each call is an event linked to the model call that asked for it.
* **Mock REST backend** — `PUT /api/mocks/<id>`, or `POST /api/mocks/<id>/openapi` with an OpenAPI 3 document (JSON).
  A request outside its contract is refused and says why.
* **Record once, replay without it** — give either a `proxy` and `mode: record|replay`. A call that was not recorded
  is refused loudly; it is never invented.
* **Faults** — `PUT /api/kitfaults/<id>`: `error`, `timeout`, `delay`, `rate_limit`, `partial`, `stale`, `wrong_shape`,
  `empty`, `drop` for tools; `down`, `delay`, `wrong_answer`, `refuse`, `never_complete`, `contradict`, `empty`, `drop`
  for peers. The same gates (`after`, `every`, `limit`, `probability`) and seed as the model faults. A scenario can
  bring its own with a `faults` setup action.
* **Injection** — `"inject": {"preset": "ignore-instructions"}` on a tool or a stub; catch it being obeyed with a
  `leak` check on its marker. `GET /api/presets` lists them.

## Other agents

* **Stub** — `PUT /api/a2a/<id>`; an A2A agent at `/run/<run>/agent/<name>/a2a/<id>` whose replies are written down
  (a message, a task that finishes after N polls or M ms, a question, a stream).
* **Proxy** — `PUT /api/a2aproxies/<id>` with `{url, mode}`; LLMSim between your agent and a real one. Every message
  is a `peer_message` event; `record` keeps the real peer's answers and `replay` serves them when it is gone.
* **Several agents at once** — `agents: [...]` in a scenario drives each with its own turns, concurrently.
* **Who goes first** — `ordering.steps` name requests (by agent, tool, text) and an order says which is released first.
  `POST /api/interleavings` runs one scenario in several orders and says whether the order changed the outcome.
* **OpenTelemetry** — `POST /otlp/v1/traces` (JSON encoding only) puts spans on the same timeline; `gen_ai.tool.name`
  spans become tool calls and `gen_ai.request.model` spans become model calls with their token usage, so the budget,
  path and authority checks work on an agent whose model LLMSim cannot stand in front of.

## Datasets, repetition, experiments, CI

* A **dataset** is versioned: saving makes a new immutable version (nothing new, no new version). Refer to
  `name`, `name@3` or `name:tag`. Build items by hand, **from a captured run** (`POST /api/datasets/<n>/items/from-run`),
  or by **import** (CSV, JSON, or an Agentforce Testing Center definition as XML).
* A case can run **N times** and pass when `required` do. A case that passes some and fails others is **flaky**,
  never a pass. A `seed` makes a run with random faults repeat exactly.
* An **experiment** runs a dataset version against an agent version (`agent: {"url": …}` replaces each scenario's agent).
  `POST /api/experiments/<id>/baseline` marks one; `…/gate` compares a later one case by case — regressed, degraded
  to flaky, fixed, new, removed — with an optional pass-rate floor.
* **Two tiers** from one definition: `simulated` (a script or a recording, free and exact) and `real`.
* **CI**: `bin/llmsim experiment run --dataset d --gate`; the GitHub Action in `action.yml` writes the job summary and
  one pull request comment naming the regressed cases and fails the build; `testkit.pytest_plugin` gives pytest a
  `llmsim` fixture and collects `*.scenario.json` files.

## Judgement

* **Code evaluators** — a function in a file in `LLMSIM_EVALUATORS` (default `./evaluators`). It runs in its own
  process with no environment, a time limit, and an audit hook that refuses it the credential files, sockets and new
  processes. That is defence in depth, **not a sandbox**: an evaluator is code someone wrote and committed, and should be
  read like any other. They cannot be uploaded over the API.
* **Judges** — pinned (model and a digest of the rubric on every verdict), must give a reason, and a *named* judge
  gates only after `POST /api/judges/<id>/calibrate` against people's labels, and only above its `minAgreement`.
  Edit the rubric and it must be calibrated again.
* **A simulated user** — `user: {persona, goal}`; its turns are asked of a model through LLMSim in the same run, so a
  recording repeats them exactly.
* **Review queue** — `/api/reviews`: scores, a verdict and a correction. The verdicts calibrate judges. The correction
  becomes what the case is checked against, in a new version of its dataset.
* **Variations** — `/api/variations/generate` has a model propose paraphrases, near-misses and attacks; only the ones
  a person accepts become cases, and a near-miss or attack must be given the checks it is held to.
* **Pairwise** — `/api/pairwise` has a judge say which of two experiments answered better, case by case, with the order
  it reads in randomised.

## What this does not do, and what was not verified

* **No live Salesforce or SAP agent was tested.** The Agentforce driver follows the published Agent API and is tested
  against a stand-in written from it; the A2A driver, stub and proxy are tested against each other and against a
  second stand-in written by the same hands. Whether an Agentforce agent can use LLMSim as its bring-your-own model,
  and whether a Joule agent can be pointed at a model endpoint at all, needs an account and was not tried. Where a
  platform keeps its model closed, LLMSim can drive, mock the tools, stub the peers and read its OpenTelemetry — and
  otherwise only observe.
* A2A is the 0.3 JSON-RPC binding. gRPC and the HTTP+JSON binding are not served. MCP is the streamable-HTTP transport;
  a stdio server is not reachable.
* OTLP is read as JSON only (set `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`): protobuf needs a library this does not have.
* The pytest plugin was written without pytest installed here and is tested through its session class, not under pytest.
* `server.py` is not split into modules. The kit is, and the existing file is untouched except for a handful of hooks.
* Unless a sign-in is required (`LLMSIM_ADMIN_PASSWORD`, with `LLMSIM_API_KEY` for scripts), anyone who can reach the port can use all of
  this, including the shared run store. With one, the management API needs the key; the stand-ins an agent calls
  (`/mcp`, `/mock`, `/a2a`, `/a2a-proxy`, `/otlp`) stay open, because an agent cannot sign in.
