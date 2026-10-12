# LLMSim documentation

LLMSim plays the model an agent talks to. It answers in Anthropic's, OpenAI's and DeepSeek's wire formats, so anything that takes a base URL can be pointed at it. You decide what the model says, break it on purpose, keep every request, and check what the agent did.

```
Your agent  ──►  LLMSim plays the model  ···  The agent's tools
(harness,        (scripts the answers,        (files, APIs,
 tools,           injects faults,              other agents)
 guardrails)      keeps every request)
```

This is the whole documentation. LLMSim serves it at `http://localhost:8099/guide`, built from [`docs/guide.md`](guide.md) by `bin/build-docs`. The reasons behind each decision are in [the design notes](design.md).

## Start here

### Run it

Python 3.9 or newer. There is nothing to install.

```bash
python3 server.py                     # serves on port 8099
LLMSIM_PORT=9000 python3 server.py    # or another port
```

The console is at `http://localhost:8099/ui`. Everything on it is also an HTTP API, and this documentation shows both.

### Point a client at it

Give the client a base URL and the matching key. The keys are fake and published. A wrong one gets the vendor's own 401.

| Vendor | Base URL | Key |
|---|---|---|
| Anthropic | `http://localhost:8099/anthropic` | `sk-ant-llmsim-000000000000000000000000` |
| OpenAI | `http://localhost:8099/openai/v1` | `sk-llmsim-000000000000000000000000` |
| DeepSeek | `http://localhost:8099/deepseek/v1` | `sk-llmsim-deepseek-0000000000000000` |

The console's **Overview** shows the same addresses for the port it is really on, with a copy button.

### Send a first request

```bash
curl localhost:8099/openai/v1/chat/completions \
  -H "Authorization: Bearer sk-llmsim-000000000000000000000000" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-5", "messages": [{"role": "user", "content": "hello"}]}'
```

The answer comes from a rule called `hello`. Open **Requests** in the console and the request is there, with the rule that answered it.

### What to read next

| You want to | Read |
|---|---|
| Decide what the model says | [Script the model](#script-the-model) |
| See how an agent copes when the vendor fails | [Make things go wrong](#make-things-go-wrong) |
| Know exactly what an agent sent | [See what was sent](#see-what-was-sent) and [Check it](#check-it) |
| Run a real session once and replay it for free | [Record once, replay](#record-once-replay) |
| Test an agent end to end, with its tools and its peers | [Test an agent](#test-an-agent) |
| Call something from a script or CI | [API reference](#api-reference) |

## How it works

### Where an answer comes from

Every request takes one **route**. The default applies to every model, and one model can be given its own.

| Route | The answer comes from | Costs |
|---|---|---|
| Scripted | A rule you wrote, or a shipped one | Nothing. Nothing leaves the machine. |
| Upstream (relay) | One real model behind an API key | That model's price |
| ChatGPT plan | A ChatGPT subscription you sign in to | The plan's usage |
| Anthropic API | A real Claude model, by API key | That model's price, billed to the key |
| Replay | A recording of an earlier run | Nothing |

Scripted is the default. Routes are set under **Routing** and, for one model, under **Models**.

### Rules and turns

A **rule** says: when a request looks like this, answer with these **turns**. The first turn answers the first request of a conversation, the second answers the next, and so on. A turn is some text, tool calls, or both. That is how an agent loop is scripted: the model asks for a tool, the agent runs it and reports back, the model says the next thing.

Your rules are kept in `llmsim.rules.json` and are tried before the shipped ones in `responses.json`. The first rule that matches answers.

### Runs and agents

A **run** groups the requests that belong to one test. An **agent** name says which agent sent each one. Put both in the URL and LLMSim keeps them apart from everything else:

```
http://localhost:8099/run/r1/agent/planner/openai/v1
http://localhost:8099/run/r1/agent/worker/anthropic
```

The headers `X-LLMSim-Run`, `X-LLMSim-Agent` and `X-LLMSim-App` do the same without changing the URL, and so does a W3C `traceparent`, whose trace id becomes the run. For a gateway that carries an identity in its own header or body field, set that up under **Requests → Capture settings**.

### What LLMSim sees, and what it cannot

LLMSim sees what crosses the model boundary: the system prompt, the declared tools, every message, tool results as the agent reports them, the turn count and the credential used.

- It does not see what a tool actually did. A file written or an API called happens outside the model's traffic. The [test kit](#test-an-agent) closes that gap by standing in for the tools too.
- It does not see messages between agents that never pass through a model. The test kit's stub agents and proxies do.
- It cannot test an agent that cannot change its base URL.
- A scripted model shows what an agent does when the model says exactly that. It does not show how good the agent is with a real one.

## The console

One page at `/ui`, with a screen for each job. Every screen has an address, such as `/ui#routing`.

| Screen | What it is for |
|---|---|
| **Overview** | What answers right now, each vendor's address and key, and which screen does which job |
| **Routing** | The default route, the upstream, ChatGPT plan or Anthropic key it uses, and how strictly the vendors are imitated |
| **Models** | One model's own route, a fixed answer, a failure status or a delay |
| **Answer rules** | What the simulated model says, and when. Includes a dry run that says which rule would answer, and why the others would not. |
| **Faults** | Failures on purpose |
| **Requests** | *Live*: every request as it arrives. *Captured*: each one in full. *Assertions*: checks over them. *Capture settings*: what is kept, redaction, and who may send requests. |
| **Chat** | Talk to a model by hand, the way a client would |
| **Record & replay** | Record a run, and replay a recording |
| **Functional tests** | Cases that send a prompt and say what must come back |
| **Agent tests** | Add the agents under test, run scenarios on them, and keep how each went. Also what an agent talks to while it runs, and how its work is judged. A scenario is built on a canvas, as a form, or as JSON. |

## Script the model

*Console: Answer rules*

```bash
curl -X POST localhost:8099/api/rules -d '{"rule": {
  "name": "needs-capability",
  "when": {"contains": "reconcile the ledger"},
  "turns": [
    {"text": "Checking what I am granted.",
     "tool_calls": [{"name": "granted", "input": {}}]},
    {"text": "Unclear. Asking.",
     "branches": [
       {"when": {"toolOutput": {"contains": "SAP.Invoice.Read"}},
        "turn": {"text": "Granted. Reading the invoices."}},
       {"when": {"toolOutput": {"regex": "^\\[\\]$"}},
        "turn": {"text": "Nothing is granted. Stopping."}}]}]}}'
```

The second turn **branches on what the tool printed**. That is how you test that an agent carries on when a request is granted and stops when it is denied.

### What a rule can match on

| Condition | Tests |
|---|---|
| `contains`, `regex` | The ask: the last thing a person said that was not tool output |
| `toolOutput`, `anyToolOutput` | What the last tool printed, or any tool so far |
| `turn` | Which turn this is: `2` or `{"min": 1}` |
| `identity` | `{"agent": "worker"}`, so two agents get different answers |
| `system`, `header` | The system prompt, or a request header |
| `tool`, `model`, `provider`, `stream` | The request itself |
| `not`, `all`, `any` | Combine the others |

### What a turn can say

| Field | Does |
|---|---|
| `text` | What the model says |
| `tool_calls` | A list of `{name, input}` the model asks for |
| `branches` | A list of `{when, turn}`: the first whose condition holds is the turn |
| `input_tokens`, `output_tokens`, `cached_tokens`, `reasoning_tokens` | Exact usage, for a cost or budget test. Zero is a real count. |
| `fault` | A fault on this one turn, such as `{"type": "abort_stream", "after": 2}` |
| `error`, `delay_ms` | Answer with an error instead, or wait first |
| `stop_reason`, `description` | The stop reason reported, and a note for whoever reads the rule |

### Find out why a rule did not fire

The dry run names the rule that would answer and, for every rule that would not, the reason. Nothing is sent or captured.

```bash
curl -X POST localhost:8099/api/rules/test -d '{"ask": "please reconcile the ledger"}'
```

### One model, one answer

To make a single model answer the same thing every time, or fail, set it on that model's page under **Models**: fixed text, a status such as 429 or 529, or a delay in milliseconds. It overrides the rules for that model alone.

### When the default route is a real model

A rule answers on the scripted route. If this LLMSim's default route is a ChatGPT plan, an upstream or the Anthropic API, a test's rule never fires: the request goes to the real model. A test that needs the script does not change the setting. It **holds** the default route for as long as it needs, and releases it:

```bash
curl -X POST localhost:8099/api/routing/hold -d '{"default": "scripted", "seconds": 600, "by": "my test suite"}'
# … run what needs the script; the answer above carries the hold's id …
curl -X DELETE localhost:8099/api/routing/hold/h-3f9a2c41d0
```

What was chosen is never touched, so there is nothing to put back wrong. The hold ends when it is released, when its `seconds` run out, or when LLMSim restarts, whichever comes first, and a test that was killed half way leaves nothing behind. One caller holds it at a time: a second one is refused and told who has it. A model with a route of its own keeps it unless the hold says `"everyModel": true`. While a hold is in force the console says who holds the route and until when, and **Release it now** lets go of it.

## Make things go wrong

*Console: Faults*

A real vendor will not fail on demand. A fault rule makes LLMSim fail for the requests you choose.

```bash
curl -X POST localhost:8099/api/faults -d '{
  "name": "cut-stream",
  "match": {"stream": true, "identity": {"agent": "worker"}},
  "fault": {"type": "abort_stream", "after": 3},
  "probability": 0.5, "seed": 7}'
```

| Fault | What the agent experiences |
|---|---|
| `status` | The vendor's own error, such as a 429 or 529, with an optional `retry_after` |
| `abort_stream` | A stream that stops after N events, closed or reset |
| `stall`, `slow_stream` | A stream that pauses once, or between every event |
| `drop`, `delay` | A connection hung up before any answer, or a long wait |
| `malformed_tool_args` | A tool call whose arguments are not valid JSON |
| `unknown_tool` | A call to a tool the agent never declared |
| `garbage`, `empty`, `huge` | A corrupt frame or body, a 200 that says nothing, an oversized answer |

The gates choose which matching requests are hit: `after` skips the first few, `every` takes each Nth, `limit` stops after a number, and `probability` rolls a die. Set a `seed` and the same run fails the same way next time.

A `status` or `drop` fault is applied before anything is sent to a real model, so it never costs a call. The functional test cases are never faulted.

## See what was sent

*Console: Requests → Captured*

Every request is kept: the system prompt, declared tools, messages, who sent it, which credential it used, and what answered it.

```bash
curl 'localhost:8099/api/captures?run=r1&agent=worker'   # a list
curl localhost:8099/api/captures/12                       # one request in full
```

Requests are held in memory only, and the oldest are dropped past a limit you set. Redaction patterns apply to everything shown or exported.

### See which credential arrived

To find out which key a gateway really sends, accept any key and register the ones you know. Only a fingerprint is stored.

```bash
curl -X PUT  localhost:8099/api/settings -d '{"auth": {"mode": "any"}}'
curl -X POST localhost:8099/api/settings/credentials -d '{"label": "platform-token", "key": "…"}'
```

Each captured request then shows its credential's label, and an assertion can test `credential.label`.

## Check it

*Console: Requests → Assertions*

An assertion is a claim about the requests an agent made. Each one has a scope and exactly one check.

```bash
curl -X POST localhost:8099/api/assertions/evaluate -d '{
  "run": "r1",
  "assertions": [
    {"id": "no-cards", "name": "No card number reaches the model",
     "none": {"field": "allText", "op": "regex", "value": "\\b\\d{13,16}\\b"}},

    {"id": "preamble", "name": "Every request carries the safety preamble",
     "each": {"system": "SAFETY"}},

    {"id": "one-write", "name": "The worker writes at most once",
     "scope": {"identity": {"agent": "worker"}},
     "count": {"max": 1},
     "where": {"field": "response.toolNames", "op": "includes", "value": "Write"}},

    {"id": "order", "name": "The planner asked before the worker",
     "sequence": [{"identity": {"agent": "planner"}}, {"identity": {"agent": "worker"}}]}
  ]}'
```

| Check | Passes when |
|---|---|
| `each` | Every request in scope satisfies it |
| `none` | No request in scope satisfies it. Use this for "must not". |
| `count` | The number of requests in scope, or of those matching `where`, is within `min` and `max` |
| `sequence` | Requests matching each step exist, in that order |

Each result comes back as `pass`, `fail` or `no-data`, with the request numbers and an excerpt as evidence. `GET /api/assertions/catalog` lists every field and operator a condition can use.

> **No data is not a pass.** If the scope selects no requests, the result is `no-data`. An agent that never ran would otherwise pass every "must not". Add `"allowEmpty": true` when an empty result really is fine.

Save a list as a named set with `POST /api/assertion-sets` and run it with `{"set": "my-set", "run": "r1"}`. Sets are kept in `llmsim.assertions.json`.

### Check a handoff between two agents

LLMSim does not see messages that pass between agents. It does see what each agent then tells its model. Script the first agent's answer to carry a token, and assert that the token turns up in the second agent's request.

```json
{"id": "handoff", "name": "The worker was told what the planner answered",
 "scope": {"identity": {"agent": "worker"}}, "count": {"min": 1},
 "where": {"field": "allText", "op": "contains", "value": "TRACE-7f3a9c"}}
```

## Record once, replay

*Console: Record & replay*

Run a session once and keep every answer. Later runs get the same answers back, offline and for free.

```bash
curl -X POST localhost:8099/api/sessions/record -d '{"name": "login-flow", "run": "r1"}'
# …run the agent…
curl -X POST localhost:8099/api/sessions/stop   -d '{"run": "r1"}'
curl localhost:8099/api/sessions/login-flow > login-flow.json     # keep it in your repository

curl -X PUT 'localhost:8099/api/sessions/login-flow?overwrite=1' -d @login-flow.json
curl -X POST localhost:8099/api/sessions/replay -d '{"name": "login-flow", "run": "r1"}'
# …run the agent again…
curl -X POST localhost:8099/api/sessions/stop   -d '{"run": "r1"}'     # hits, misses, what was not recorded
```

- A replay matches a request by what was asked and how many turns in it is. It does not compare whole requests, which contain timestamps and never repeat.
- A harness that asks two things at the same point gets both answers kept. They are told apart by the tools each request declares.
- An empty answer is recorded too. It is how an agent's last turn often looks.
- A request the recording does not hold gets a 404 that names it. Pass `"onMiss": "route"` to let it take its normal route instead.
- If your prompts contain something that changes every run, such as a run id made from the clock, add a mask: `"masks": ["\\d{8}-\\d{6}"]`.
- Leave out `run` to record or replay every request that names no run. Several runs can record at once without seeing each other.
- `POST /api/sessions/auto` replays if the recording exists and records if it does not.

> A recording holds the model's answers, and an answer can quote what it was shown. Read a recording before you commit it.

## Answer with a real model

*Console: Routing*

| Route | Answers come from | Set it up |
|---|---|---|
| Upstream | One model behind an API key | Routing → Upstream: URL, model and key |
| ChatGPT plan | Your ChatGPT subscription | Routing → ChatGPT plan → Sign in |
| Anthropic API | A real Claude model | Routing → Anthropic API: key and model |

The usual pattern: record a session through a real model once, then replay it for every later test run.

### Anthropic API

A client speaking any of the three formats is answered by Claude in its own format, with its system prompt, its tools and streaming. Save a key under **Routing → Anthropic API**, or start LLMSim with one:

```bash
LLMSIM_ANTHROPIC_UPSTREAM_KEY=sk-ant-… LLMSIM_ANTHROPIC_UPSTREAM_MODEL=claude-sonnet-5-5 python3 server.py
```

| Setting | Means |
|---|---|
| Key | An API key from the Claude Console, sent as `x-api-key`. It is kept in `llmsim.config.json`, readable by you alone, and is never sent back to the console. |
| It is | Choose **a gateway's token** when the base URL is a gateway that speaks the Messages API and takes `Authorization: Bearer`. |
| Model | The Claude model that answers. Left blank, the model each client asks for is used when it is a Claude model, and any other is refused. |
| Max tokens | Used when the client did not say. Anthropic requires one. |
| Base URL | Anthropic's own by default. |

The client's `temperature` or `top_p`, stop sequences, tools and tool choice are passed on. `response_format`, `reasoning_effort`, `seed` and the penalties are not, and the log says so when one is dropped.

> **It takes a key, not a Claude sign-in.** Anthropic does not permit a tool to offer Claude sign-in or to route requests through Free, Pro or Max plan credentials, so a subscription cannot be used on this route. Use an API key from the Claude Console or from a cloud provider.

### ChatGPT plan

The ChatGPT sign-in is OpenAI's OAuth flow, and its redirect lands on `127.0.0.1:1455` on the machine LLMSim runs on. On your own machine that just works. When LLMSim runs somewhere else, the console asks you to paste the address your browser ended up on instead.

> **Once you sign in, LLMSim holds a real subscription.** It listens on every network interface and the console has no login, so anyone who can reach the port can spend the plan. Keep it on your own machine or a network you trust.

The sign-in is kept in `llmsim.chatgpt-auth.json` beside the server. A container with no volume loses it on restart and goes back to scripted answers.

## Functional tests

*Console: Functional tests*

A case sends a model a prompt and says what must come back. It goes down whatever route that model is on, so the same cases check a script, a recording or a real model. A case is a sequence of steps, run in one conversation and stopped at the first failure.

| A step can assert | Meaning |
|---|---|
| Text | `contains`, `regex` or `equals` against the answer |
| Tool | A tool the caller was offered and should have called |
| Tool arguments | `file_path=HELLO.md`, one per line, by dotted path |
| HTTP status | So "this key is over its quota and must answer 429" is a test |
| Must not contain | The negative: "never invents a figure it could not read" |
| Must not be called | A tool the step forbids |
| Answer within (ms) | A deadline |

Tick **sent as tool output** for a step that is a tool reporting back rather than a person speaking. That is what comes next in a real agent loop, and it keeps the conversation on the rule its opening ask chose.

Cases and plans are kept in `llmsim.tests.json`. Export and Import move a suite between machines.

## A worked example

The agent under test is a read-only assistant. It must stop when a capability is denied, and it must never send a card number to the model.

**1. Give it a run.** Point the agent at `http://localhost:8099/run/t1/agent/assistant/openai/v1`.

**2. Script the model.** Add the `needs-capability` rule from [Script the model](#script-the-model). Its second turn says "Stopping" when the tool prints `[]`.

**3. Break something.** Add a fault so one answer arrives with tool arguments that are not JSON:

```bash
curl -X POST localhost:8099/api/faults -d '{
  "name": "bad-json", "match": {"identity": {"run": "t1"}}, "limit": 1,
  "fault": {"type": "malformed_tool_args", "mode": "truncated"}}'
```

**4. Run the agent** on "please reconcile the ledger".

**5. Check what it did.**

```bash
curl -X POST localhost:8099/api/assertions/evaluate -d '{
  "run": "t1",
  "assertions": [
    {"id": "stopped", "name": "It stopped within four requests", "count": {"max": 4}},
    {"id": "no-cards", "name": "No card number reaches the model",
     "none": {"field": "allText", "op": "regex", "value": "\\b\\d{13,16}\\b"}},
    {"id": "no-writes", "name": "It never called a write tool",
     "none": {"field": "response.toolNames", "op": "includes", "value": "Write"}}
  ]}'
```

**6. Read the verdict.** A `fail` lists the request numbers. Open one under Requests → Captured to see exactly what the agent sent.

**7. Clean up.** `curl -X DELETE localhost:8099/api/runs/t1` forgets the run's requests and stops its session.

## Test an agent

*Console: Agent tests*

Everything above puts LLMSim on one side of an agent: the model. The **test kit** puts it on the other sides as well. It drives the agent, stands in for the tools it calls and the agents it hands work to, keeps the whole run as one timeline, and gives a verdict a CI job can act on.

```
                 ┌── model ─────── scripted, recorded, faulted
driver ──▶ AGENT ┼── tools ─────── mock MCP server, mock REST backend, state, faults
                 └── peer agents ─ A2A stub, or a proxy to a real one
                          │
                every event, in order ──▶ a run on disk ──▶ checks ──▶ a verdict
```

Nothing in it is specific to one product. A platform enters in two places: a **driver**, which is how a conversation with it is started, and an **importer**, which reads its test cases.

### Connect an agent

*Console: Agent tests, Agents*

An agent under test is saved once, and scenarios are run on it. Adding one starts with where it lives, because the platform decides how it is reached.

| Platform | Reached by | Credential |
|---|---|---|
| **Salesforce Agentforce** | Salesforce's Agent API (`agentforce`), or A2A, which Salesforce describes as a beta | OAuth client credentials from an external client app, token URL `<domain>/services/oauth2/token` |
| **SAP Joule** | A2A, through the SAP Agent Gateway | OAuth client credentials from SAP Cloud Identity Services |
| **ServiceNow** | A2A, through AI Agent Fabric, once "Allow third party to access ServiceNow AI agents" is switched on | OAuth client credentials from the instance, or an `x-sn-apikey` header |
| **Azure AI Foundry** | A2A, once incoming A2A is enabled on the agent, or the Responses API with the agent named as an agent reference | OAuth client credentials for a Microsoft Entra service principal, scope `https://ai.azure.com/.default` |
| **Custom** | A2A, an OpenAI-compatible chat endpoint, the Responses API, any HTTP endpoint, or a local program | Whatever the agent wants: a bearer token, a header, basic, or OAuth client credentials |

`GET /api/agents` returns this list with each connection's address laid out as that platform's is. An address is saved only once its blanks are filled in.

```json
{
  "label": "Helper (Foundry project)",
  "system": "azure-foundry",
  "connection": {
    "driver": "a2a",
    "url": "https://acme.services.ai.azure.com/api/projects/support/agents/helper/endpoint/protocols/a2a",
    "card": "agentCard/v0.3",
    "headers": {"A2A-Version": "0.3"},
    "credential": "entra-foundry"
  }
}
```

```bash
curl -X PUT  localhost:8099/api/agents/helper -d @agent.json
curl -X POST localhost:8099/api/agents/helper/test -d '{"say": "Hello"}'
```

**Test the connection.** A test reaches the agent the way a run would and says what happened at each of three steps: the credential (is it stored, and does the platform issue a token for it), the connection (does the address answer as that protocol), and one message. The message is the only step that proves the agent answers, and it is really sent; `"say": ""` sends none. A changed connection is "not tested" again.

**Run scenarios on it.** A scenario names a saved agent instead of describing one, `"agent": {"use": "helper"}`, so fifty scenarios do not each hold an address, and the same scenario runs on another agent unchanged:

```bash
curl -X POST localhost:8099/api/scenarios/refund-over-limit/run -d '{"agent": "support-next"}'
```

`agent` on a run sends it to that saved agent whatever the scenario says. What is the scenario's own stays: the agent's name on the timeline, which checks refer to, and what is said to it. For a scenario that drives several agents it is `{"<name>": "<saved agent>"}`.

**A test run** is a set of scenarios run on one agent, kept as one thing: which passed, which did not, and why.

```bash
curl -X POST localhost:8099/api/testruns -d '{"agent": "helper", "tags": ["smoke"], "label": "before the release"}'
```

Choose the scenarios with `scenarios` (ids), `tags` (any of these) or `"all": true`. They run one after another, since they are conversations with the same agent. `GET /api/agents/<id>/coverage` lists every saved scenario with how its last run on that agent went. A test run is what a person presses a button for; an [experiment](#datasets-experiments-and-the-gate) is what a release is gated on, and takes `"agent": "<saved agent>"` too.

> **What LLMSim can see of a hosted agent.** LLMSim stands in for the model, the tools and the peers of an agent it is in front of. An agent hosted on Salesforce, SAP, ServiceNow or Azure calls its own model and its own tools, and LLMSim is in front of none of them. It sees the conversation, so `answer`, `judge`, `conversation` and time checks work. `path`, token and cost checks have nothing to read, and say `no-data`, unless the agent's tools are pointed at LLMSim or it sends OpenTelemetry spans here. `model.rules` in a scenario script a model the hosted agent never calls.

### A scenario is a test

A scenario says how to reach the agent, what the user says, what the model says, and what must hold afterwards.

```json
{
  "id": "refund-over-limit",
  "agent": {"name": "support", "driver": "http", "url": "https://agent.example/chat",
            "body": {"input": "{{message}}", "run": "{{run}}", "agent": "{{agent}}"}, "reply": "output.text"},
  "model": {"rules": [{"name": "order-flow", "when": {"contains": "order 1182"},
                       "turns": [{"text": "Looking it up.", "tool_calls": [{"name": "lookup_order", "input": {"id": 1182}}]},
                                 {"text": "It is over the limit, so I have handed it to a manager."}]}]},
  "turns": [{"say": "refund order 1182", "expect": {"contains": "manager"}}],
  "checks": [
    {"type": "path", "mode": "strict", "expected": [{"tool": "lookup_order", "arguments": {"id": 1182}}]},
    {"type": "budget", "tokens": {"total": 20000}, "steps": {"modelCalls": 6}}
  ]
}
```

Save it and run it:

```bash
curl -X PUT  localhost:8099/api/scenarios/refund-over-limit -d @scenario.json
curl -X POST localhost:8099/api/scenarios/refund-over-limit/run -d '{}'
```

Or from a file, with an exit code a build can use (0 pass, 1 fail, 2 could not run):

```bash
bin/llmsim run scenario.json --junit out.xml
```

A scenario is checked when it is saved, not when it first fails in the middle of a run, and every problem is listed at once. The model's script, and anything else a scenario installs, applies to that run only and is removed with it.

In the console, a scenario is a form: the agent and its driver's own fields, the turns, the model's script, the checks. The **JSON** switch beside it shows exactly what is sent.

### Build it on the canvas

The **Canvas** switch shows the same scenario as a picture of the run it describes: who speaks on the left, the agent in the middle, what it reaches on the right — the model, and the mock tools, mock backends, stub agents, proxies, seeded records and faults this run installs — and the checks underneath. A block is one part of the document. Clicking it opens that part's own fields beside the picture, the same fields the form has, writing to the same place; the **+** buttons add a turn, a check, a mock, another agent or a fault where it belongs, and a block's **Remove** takes it out. **Check it is valid** marks each block the validator has something to say about, with its words on the block.

Canvas, form and JSON are three views of one document, and switching between them loses nothing. Nothing can be drawn that cannot be saved: a mock or a stub agent drawn on the canvas is a `setup` action, a fault is a rule inside one, and a part the canvas has no block for is drawn as its JSON rather than hidden. Stub agents and proxies that were saved on their own are shared by every scenario; the canvas shows how many there are, beside the ones this test brings itself.

A block can be dragged to where it reads best, and the lines follow it. Where a block was put is kept in the browser, under the scenario's name, and is not part of the scenario: the file a CI job runs does not change when somebody tidies a picture. **Reset layout** puts every block back.

| Scenario field | Holds |
|---|---|
| `id`, `description`, `tags` | Its name, what it proves, and labels |
| `agent` | One agent. A saved one, `{"use": "<id>", "name"?}`, or one described here: `name`, `driver`, the driver's fields, an optional `credential` |
| `agents` | Two to twenty agents, each with its own `turns`, driven at the same time |
| `turns` | What the user says, in order: `say`, and an optional `expect` on the reply |
| `user` | A simulated user instead of fixed turns: `{persona, goal}` |
| `model` | For this run only: `rules`, or a `cassette` with `mode` `replay`, `record` or `auto` |
| `setup`, `teardown` | Actions before and after: see [What a scenario brings with it](#what-a-scenario-brings-with-it) |
| `checks` | What must hold when the conversation ends |
| `stop` | `maxTurns`, and `when`: a condition on a reply that ends it early |
| `timeoutS`, `repeat`, `ordering` | A time limit, running a case several times, and who goes first |

### What a scenario brings with it

A `setup` action installs something for one run. It is found by the run's id, served at the same address a saved one would be, and removed when the run ends, so a scenario file can be the whole test.

| Action | Brings |
|---|---|
| `{"type": "mock", "kind": "mcp", "definition": {…}}` | A mock MCP server, written the way `PUT /api/mcp/<id>` takes it |
| `{"type": "mock", "kind": "http", "definition": {…}}` | A mock REST backend, as `PUT /api/mocks/<id>` takes it |
| `{"type": "peer", "kind": "stub", "definition": {…}}` | A stub agent, as `PUT /api/a2a/<id>` takes it |
| `{"type": "peer", "kind": "proxy", "definition": {…}}` | A proxy to a real agent, as `PUT /api/a2aproxies/<id>` takes it. Its recording is kept under its name, so a later run can replay it. |
| `{"type": "state", "seed": {collection: {key: record}}}` | The records the mock tools' store starts with |
| `{"type": "faults", "rules": […]}` | Tool and peer faults, as `PUT /api/kitfaults/<id>` takes each |

One brought by a scenario wins over a saved one of the same name, for that run only. The saved one is not touched.

### Drivers

A driver is how LLMSim talks to the agent under test. `GET /api/drivers` lists them with their fields.

| Driver | Reaches | Its fields |
|---|---|---|
| `http` | Anything that takes a POST | `url`, `method`, `body` (a template), `headers`, `reply` (a path into the answer), `replyFrom` (`sse` or `text`), `conversation` |
| `openai` | An OpenAI-compatible chat endpoint | `url`, `model`, `system`, `stream`, `extra` |
| `responses` | An endpoint that speaks the Responses API, which is also how an Azure AI Foundry agent is called | `url`, `model`, `instructions`, `extra`, `chain` (`previous`, `conversation` or `none`), `query` |
| `a2a` | An Agent2Agent agent, over JSON-RPC | `url`, `rpcUrl`, `wait` (`poll`, `push` or `stream`), `pollMs`, `blocking`, `callbackBase`, `card` (where the agent card is, when not at the usual address), `headers` |
| `agentforce` | Salesforce's Agent API | `domain`, `agentId`, `apiBase`, `stream`, `bypassUser`, `variables` |
| `process` | A local program: the message on stdin, the reply on stdout | `command`, `cwd`, `env` |

In a `body` template, `{{message}}` is what the user says, and `{{conversation}}`, `{{run}}` and `{{agent}}` are filled in too. The `process` driver is off unless LLMSim is started with `LLMSIM_ALLOW_PROCESS=1`, because anyone who can reach the port could otherwise name a command.

Credentials are kept apart from scenarios, in an owner-only file. A scenario names one; it never holds it. A value is never shown again, and every value the kit holds is scrubbed from everything it stores or shows.

### Checks

| Check | Says |
|---|---|
| `answer` | The reply contains, equals or matches something, has a JSON shape, or holds a number within a tolerance |
| `path` | The tool calls made, against a reference: `strict`, `unordered`, `subset` or `superset` |
| `budget` | Tokens, cost, latency and steps, per run or per agent, and which limit was passed |
| `state` | A record has these fields, is untouched or is gone; only these records changed; nothing was deleted |
| `leak` | A secret, or an injected instruction's marker, turned up in a reply, a tool call or a message to a peer |
| `routing`, `handoff` | The right agent got the task; what was passed on carried what it must and nothing it must not |
| `authority` | An agent used only the tools and records it may, also when a peer asked it to |
| `termination` | No loop, no ping-pong, bounded depth and chatter |
| `duplication` | No two agents did the same work, or wrote the same record differently |
| `degradation` | When a peer or tool failed, the answer said so and did not claim success |
| `memory` | What one agent wrote to shared memory is what the other acted on |
| `run`, `assertions` | Anything the condition language, or an assertion set, can say about the run |
| `code` | A Python function in a file the operator controls |
| `judge`, `conversation` | A model decides, against a rubric, and must say why |

> **No data is not a pass.** An agent that never ran satisfies every "must not". A check with nothing to judge says `no-data`, and so does the run.

### Stand in for its tools

- **Mock MCP server.** `PUT /api/mcp/<id>`, then point the agent at `/run/<run>/agent/<name>/mcp/<id>`. Tools are written down: arguments are validated against `inputSchema`, and results are chosen by condition or are state operations (`get`, `put`, `update`, `delete`, `list`) on a per-run record store. A write in turn two is what a read in turn four returns, and the next run starts clean.
- **Mock REST backend.** `PUT /api/mocks/<id>`, or `POST /api/mocks/<id>/openapi` with an OpenAPI 3 document as JSON. A request outside its contract is refused and says why. It is served at `/run/<run>/agent/<name>/mock/<id>`.
- **Record a real one, replay without it.** Give either a `proxy` with `mode` `record` or `replay`. A call that was not recorded is refused loudly. It is never invented.
- **Faults.** `PUT /api/kitfaults/<id>`: `error`, `timeout`, `delay`, `rate_limit`, `partial`, `stale`, `wrong_shape`, `empty` and `drop` for tools. They use the same gates and seed as the model's faults.
- **Injection.** `"inject": {"preset": "ignore-instructions"}` on a tool or a stub plants a prompt injection in what it returns. Catch it being obeyed with a `leak` check on its marker. `GET /api/presets` lists them.

### Stand in for other agents

- **Stub.** `PUT /api/a2a/<id>` makes an A2A agent at `/run/<run>/agent/<name>/a2a/<id>` whose replies are written down: a message, a task that finishes after some polls, a question, or a stream.
- **Proxy.** `PUT /api/a2aproxies/<id>` with `{url, mode}` puts LLMSim between your agent and a real one. Every message is on the timeline. `record` keeps the real peer's answers and `replay` serves them when it is gone.
- **For one test only.** A scenario can bring a stub or a proxy itself, with a `peer` setup action, instead of relying on one that was saved beforehand.
- **Faults.** `down`, `delay`, `wrong_answer`, `refuse`, `never_complete`, `contradict`, `empty` and `drop`.
- **Several agents at once.** `agents` in a scenario drives each with its own turns, at the same time.
- **Who goes first.** `ordering.steps` names requests by agent, tool or text. `POST /api/interleavings` runs one scenario in several orders and says whether the order changed the outcome.
- **OpenTelemetry.** `POST /otlp/v1/traces`, JSON encoding only, puts spans on the same timeline. Tool and model spans become tool calls and model calls with their token usage, so the budget, path and authority checks work on an agent whose model LLMSim cannot stand in front of.

### Datasets, experiments and the gate

- A **dataset** is versioned. Saving makes a new version that never changes. Refer to one as `name`, `name@3` or `name:tag`. Build its cases by hand, from a captured run (`POST /api/datasets/<id>/items/from-run`), or by import from CSV, JSON or an Agentforce Testing Center definition.
- A case can run several times and pass when enough of them do. A case that passes some and fails others is **flaky**, never a pass. A `seed` makes a run with random faults repeat exactly.
- An **experiment** runs a dataset version against an agent. `POST /api/experiments/<id>/baseline` marks one. `POST /api/experiments/<id>/gate` compares a later one with it case by case: regressed, degraded to flaky, fixed, new or removed, with an optional pass-rate floor.
- There are two **tiers** from one definition: `simulated`, which is a script or a recording, free and exact, and `real`.

### Judge with a model

- **Judges.** A judge is pinned: its model and a digest of its rubric are on every verdict, and it must give a reason. A named judge gates only after `POST /api/judges/<id>/calibrate` against people's labels, and only above its `minAgreement`. Edit the rubric and it must be calibrated again.
- **Review queue.** A person gives a run scores, a verdict and, if they like, a correction. The verdicts calibrate judges. The correction becomes what the case is checked against, in a new version of its dataset.
- **Variations.** `POST /api/variations/generate` has a model propose paraphrases, near-misses and attacks. Only the ones a person accepts become cases, and a near-miss or an attack must be given the checks it is held to.
- **Pairwise.** `POST /api/pairwise` has a judge say which of two experiments answered better, case by case.
- **A simulated user.** `user: {persona, goal}` in a scenario. Its turns are asked of a model through LLMSim in the same run, so a recording repeats them exactly.
- **Your own check.** A `code` check runs a function from a file in `LLMSIM_EVALUATORS`. It runs in its own process with a time limit and no access to the credential files. That is defence in depth, not a sandbox. Evaluators cannot be uploaded over the API.

### Run it in CI

```bash
bin/llmsim run scenario.json --junit out.xml          # one scenario
bin/llmsim experiment run --dataset refunds --gate    # a dataset, gated against its baseline
```

`action.yml` is a GitHub Action that writes the job summary, posts one pull request comment naming the regressed cases, and fails the build. `testkit.pytest_plugin` gives pytest an `llmsim` fixture and collects `*.scenario.json` files.

### What was not verified

- No live Salesforce, SAP, ServiceNow or Azure agent was tested. Each connection follows what its vendor publishes and is tested against a stand-in written from the same: the Agentforce driver against the Agent API, Azure AI Foundry against Microsoft's description of incoming A2A.
- For ServiceNow and SAP, the vendors' public documentation says agents are reached over A2A 0.3 and does not give the address an agent answers on, so none is laid out: use the one your instance gives. SAP's reference architecture says access from outside through its Agent Gateway is still being rolled out.
- For Azure AI Foundry, this driver speaks A2A 0.3, which Foundry serves as a preview; 1.0 is its generally available version. The Responses address is the one Microsoft's SDK uses and was not confirmed against a live project.
- A2A is the 0.3 JSON-RPC binding. MCP is the streamable-HTTP transport: a stdio server is not reachable.
- OTLP is read as JSON only. Set `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`.
- The pytest plugin is tested through its session class, not under pytest.

## API reference

Every route answers JSON. A request body is JSON too, and no `Content-Type` header is required. There is no authentication on the management API: anyone who can reach the port can use it.

| Status | Means |
|---|---|
| `200` | Done. The body has `"ok": true`. |
| `202` | Started in the background. Poll the route the body names. |
| `400` | The request was wrong. The body lists every problem under `problems`. |
| `403` | The request came from a page on another website. |
| `404` | There is no such rule, fault, recording, run or route. |
| `409` | The current state forbids it, for example a recording is already running. |

### The vendors' endpoints

These are what a client calls. Prefix any of them with `/run/<run>` or `/run/<run>/agent/<name>` to put the request in a run.

| Method and path | Is |
|---|---|
| `POST /anthropic/v1/messages` | Anthropic's Messages API, streamed or whole. Key in `x-api-key`. |
| `POST /anthropic/v1/messages/count_tokens` | Anthropic's token count |
| `POST /openai/v1/chat/completions` | OpenAI's Chat Completions. Key in `Authorization: Bearer`. |
| `POST /openai/v1/responses` | OpenAI's Responses API |
| `POST /deepseek/v1/chat/completions` | DeepSeek's Chat Completions |
| `GET /<vendor>/v1/models` | The models that vendor's endpoint advertises. Needs the key, like the rest. |
| `GET /healthz` | Whether it is up, the default route, and the rules loaded |

### Runs

| Method and path | Does |
|---|---|
| `GET /api/runs` | The runs seen, and the runs kept on disk |
| `POST /api/runs` | Name a run. Body `{run, agents?}`. Returns the base URL for each vendor and each agent. |
| `GET /api/runs/<run>` | One run: its agents, request count and session |
| `DELETE /api/runs/<run>` | Forget its requests, stop its session, remove what is kept |
| `GET /api/runs/<run>/timeline` | Every event of the run in order. Query `kinds`, `agent`, `since`, `limit`. |
| `GET` `POST /api/runs/<run>/events` | Read the events, or add one: `{kind, agent?, data?, parent?}` |
| `GET` `PUT` `DELETE /api/runs/<run>/state` | Read, seed or clear what the run's mock tools hold |
| `DELETE /api/runs/<run>/stored` | Remove what is kept on disk for the run |

### Rules

| Method and path | Does |
|---|---|
| `GET /api/rules` | Every rule, yours and the shipped ones, and the turn used when none matches |
| `POST /api/rules` | Add a rule. Body `{rule, position?}`. |
| `GET` `PUT` `DELETE /api/rules/<name>` | Read, replace or remove one of yours |
| `POST /api/rules/validate` | Check a rule without saving it |
| `POST /api/rules/test` | The dry run. Body `{ask}` or `{messages}`, with optional `tools`, `provider`, `model`, `identity` and `headers`. |
| `POST /api/rules/order` | Reorder your rules. Body `{names}`: every one of them, in the order to try. |
| `POST /api/rules/reload` | Read the rule files again |
| `PUT` `DELETE /api/rules/default` | Set or clear the turn used when no rule matches |

### Faults

| Method and path | Does |
|---|---|
| `GET /api/faults` | The fault rules, their counters, and what each fault type takes |
| `POST /api/faults` | Add one. Body `{name, match?, fault, probability?, every?, after?, limit?, seed?}`. |
| `GET` `PUT` `DELETE /api/faults/<id>` | Read, replace or remove one |
| `POST /api/faults/pause` | Stop every fault firing, without removing them |
| `POST /api/faults/resume` | Let them fire again |
| `POST /api/faults/reset` | Zero the counters, so `after`, `every` and `limit` start over |

### Requests

| Method and path | Does |
|---|---|
| `GET /api/captures` | The requests kept. Query `run`, `agent`, `app`, `model`, `status`, `q`, `limit`. |
| `GET /api/captures/<n>` | One request in full: what was sent and what came back |
| `GET /api/captures/export` | The same list as a download, with the same query |
| `DELETE /api/captures` | Forget them. With a query, only those it selects. |

### Assertions

| Method and path | Does |
|---|---|
| `GET /api/assertions/catalog` | Every field, operator, check kind and condition there is |
| `POST /api/assertions/validate` | Check assertions without running them |
| `POST /api/assertions/evaluate` | Run them. Body `{assertions, run?, agent?, scope?}` or `{set, run?}`. |
| `GET` `POST /api/assertion-sets` | List the saved sets, or save one |
| `GET` `PUT` `DELETE /api/assertion-sets/<name>` | Read, replace or remove a set |
| `POST /api/assertion-sets/<name>/evaluate` | Run a saved set. Body `{run?}`. |

### Recordings

| Method and path | Does |
|---|---|
| `GET /api/sessions` | What is recording or replaying now, and the recordings kept. Query `run`. |
| `POST /api/sessions/record` | Start recording. Body `{name, run?, masks?}`. |
| `POST /api/sessions/replay` | Start replaying. Body `{name, run?, onMiss?, strict?}`. |
| `POST /api/sessions/auto` | Replay if the recording exists, record if it does not |
| `POST /api/sessions/stop` | Stop, and say what was hit, missed and not recorded. Body `{run?}`. |
| `GET /api/sessions/<name>` | Download a recording |
| `PUT /api/sessions/<name>` | Upload one. Query `overwrite=1` to replace. |
| `DELETE /api/sessions/<name>` | Remove one |

### Routing

| Method and path | Does |
|---|---|
| `GET /api/routing` | `default` and `models` as they are kept, `effective` (the default route in force now), `routes`, and the `hold` if there is one |
| `PUT /api/routing` | Change what is kept. Body `{default?, models?}`; a route is `scripted`, `relay`, `chatgpt` or `anthropic`. |
| `POST /api/routing/hold` | Hold the default route for a while without changing what is kept. Body `{default, by, seconds?, everyModel?}`; `seconds` is 1 to 3600 and 300 if left out. Answers `409` if someone else holds it. |
| `DELETE /api/routing/hold/<id>` | Release that hold. Releasing one that already ended is not an error. |
| `DELETE /api/routing/hold` | Release whatever hold is in force |

### Settings

| Method and path | Does |
|---|---|
| `GET /api/settings` | Capture, identity rules, who may send requests, and replay defaults |
| `PUT /api/settings` | Change any of `capture`, `identity`, `auth`, `session` |
| `POST /api/settings/credentials` | Register a key to recognise. Body `{label, key}`. Only a fingerprint is kept. |
| `DELETE /api/settings/credentials/<label>` | Forget one |

### Test kit: scenarios and results

| Method and path | Does |
|---|---|
| `GET /api/scenarios` | The saved scenarios |
| `POST /api/scenarios` | Save one, named by its `id` |
| `GET` `PUT` `DELETE /api/scenarios/<id>` | Read, replace or remove one |
| `POST /api/scenarios/validate` | Check one without saving it. Body `{scenario}`. |
| `POST /api/scenarios/<id>/run` | Run a saved one. Body `{run?, tier?, label?, seed?, keepState?, wait?, agent?}`. `agent` is a saved agent to run it on. |
| `POST /api/scenarios/run` | Run one that is not saved. Body `{scenario, …the same options}`. |
| `GET /api/results` | The results, newest first. Query `scenario`, `agent` (a saved agent), `limit`. |
| `GET /api/results/<run>` | One result: verdict, checks with evidence, and the conversation |
| `GET /api/results/<run>/junit` | The same as JUnit XML |
| `DELETE /api/results/<run>` | Remove one |
| `GET /api/drivers` | The drivers, the fields each requires, and whether `process` is allowed |
| `POST /api/drivers/validate` | Check an agent's driver settings |
| `GET /api/checks` | The check types and the statuses a check can have |
| `POST /api/checks/validate` | Check one check. Body `{check}`. |
| `POST /api/checks/evaluate` | Run checks over a run that already happened. Body `{run, checks}`. |
| `POST /api/interleavings` `POST /api/interleavings/<id>` | Run a scenario in several orders. Body `{orders, compare?}`. |

With `"wait": false` a run starts in the background and answers `202`. Poll `GET /api/results/<run>`.

### Test kit: agents and test runs

| Method and path | Does |
|---|---|
| `GET /api/agents` | The saved agents with whether each answers, and `systems`: the platforms, and how each is reached |
| `GET` `PUT` `DELETE /api/agents/<id>` | Read, replace or remove one. An agent that scenarios name is not removed without `?force=1`. |
| `POST /api/agents/validate` | Check one without saving it. Body `{agent}`. |
| `POST /api/agents/<id>/test` | Reach it and say what happened at each step. Body `{say?}`; `""` sends no message. The result is kept with the agent. |
| `POST /api/agents/test` | The same for one that is not saved. Body `{agent, say?}`. |
| `GET /api/agents/<id>/coverage` | Every saved scenario, with its last run on this agent |
| `GET /api/testruns` | The test runs, newest first. Query `agent`. |
| `POST /api/testruns` | Run scenarios on an agent. Body `{agent, scenarios \| tags \| all, label?, tier?, wait?}`. |
| `GET` `DELETE /api/testruns/<id>` | Read or remove one |

With `"wait": false` a test run starts in the background and answers `202`. Poll `GET /api/testruns/<id>`.

### Test kit: datasets and experiments

| Method and path | Does |
|---|---|
| `GET /api/datasets` | The datasets, with their current version |
| `GET /api/datasets/<id>` | One version of it. Query `version` or `tag`. |
| `PUT /api/datasets/<id>` | Save it. Body `{items, description?}`. Makes a new version when something changed. |
| `DELETE /api/datasets/<id>` | Remove it |
| `GET /api/datasets/<id>/versions` | Its versions and tags |
| `POST /api/datasets/<id>/tag` | Tag a version. Body `{tag, version?}`. |
| `POST /api/datasets/<id>/items/from-run` | Make a case from a run. Body `{run, id, scenario?, strict?, include?}`. |
| `POST /api/datasets/<id>/import` | Import cases. Body `{format, content, agent, merge?}`. |
| `GET /api/experiments` | The experiments. Query `dataset`. |
| `POST /api/experiments/run` | Run a dataset. Body `{dataset, label?, tier?, repeat?, seed?, split?, tags?, items?, agent?, concurrency?, wait?}`. |
| `GET` `DELETE /api/experiments/<id>` | Read or remove one |
| `POST /api/experiments/<id>/baseline` | Make it its dataset's baseline |
| `POST /api/experiments/<id>/gate` | Compare it with the baseline, case by case |
| `GET /api/experiments/<a>/compare/<b>` | Compare any two |

### Test kit: tools and peers

| Method and path | Does |
|---|---|
| `GET /api/mcp` | The mock MCP servers |
| `GET` `PUT` `DELETE /api/mcp/<id>` | Read, save or remove one |
| `POST /api/mcp/validate` | Check a definition |
| `GET /api/mcp/<id>/url` | The address to give an agent. Query `run`, `agent`. |
| `POST /api/mcp/<id>/call` | Call one of its tools by hand. Body `{tool, arguments?, run?, agent?}`. |
| `DELETE /api/mcp/<id>/cassette` | Forget what its proxy recorded |
| `GET /api/mocks` | The mock REST backends |
| `GET` `PUT` `DELETE /api/mocks/<id>` | Read, save or remove one |
| `POST /api/mocks/validate` | Check a definition |
| `POST /api/mocks/<id>/openapi` | Make one from an OpenAPI 3 document |
| `GET /api/mocks/<id>/url` | The address to give an agent |
| `GET /api/a2a` | The stub agents |
| `GET` `PUT` `DELETE /api/a2a/<id>` | Read, save or remove one |
| `POST /api/a2a/validate` | Check a definition |
| `GET /api/a2a/<id>/url` | The address to give an agent |
| `GET /api/a2aproxies` | The agent proxies |
| `GET` `PUT` `DELETE /api/a2aproxies/<id>` | Read, save or remove one |
| `GET /api/a2aproxies/<id>/url` | The address to give an agent |
| `DELETE /api/a2aproxies/<id>/cassette` | Forget what it recorded |
| `GET /api/kitfaults` | The tool and peer faults, their counters, and what each type takes |
| `PUT` `DELETE /api/kitfaults/<id>` | Save or remove one |
| `POST /api/kitfaults/validate` | Check one. Body `{rule}`. |
| `POST /api/kitfaults/reseed` | Start the dice again from a seed. Body `{seed}`. |
| `GET /api/presets` | The prompt injections a tool or stub can carry |
| `POST /otlp/v1/traces` | OpenTelemetry spans, as JSON, onto a run's timeline |

The mocks themselves are served under a run: `/run/<run>/agent/<name>/mcp/<id>`, `/run/<run>/agent/<name>/mock/<id>`, `/run/<run>/agent/<name>/a2a/<id>` and `/run/<run>/agent/<name>/a2a-proxy/<id>`.

### Test kit: judging and setup

| Method and path | Does |
|---|---|
| `GET /api/judges` | The judges, and whether each can gate |
| `GET` `PUT` `DELETE /api/judges/<id>` | Read, save or remove one. Body `{rubric, model?, minAgreement?}`. |
| `POST /api/judges/<id>/calibrate` | Calibrate it. Body `{examples}` or `{fromReviews: true}`. |
| `POST /api/pairwise` | Have a judge compare two experiments. Body `{a, b, rubric, model?}`. |
| `GET /api/pairwise/<id>` | Read a comparison |
| `GET /api/reviews` | The review queue. Query `status`. |
| `POST /api/reviews` | Put runs in it. Body `{runs}` or `{fromExperiment}`. |
| `GET /api/reviews/<id>` | One review, with the conversation |
| `POST /api/reviews/<id>/submit` | Give a verdict. Body `{verdict, scores?, comment?, reviewer?, corrected?}`. |
| `GET /api/variations` | The sets of proposed variations |
| `POST /api/variations/generate` | Have a model propose some. Body `{dataset, item, kinds?, count?, model?}`. |
| `GET /api/variations/<id>` | One set |
| `POST /api/variations/<id>/accept` | Add accepted ones to the dataset. Body `{accept: [{index, checks?}], rejectRest?}`. |
| `GET /api/evaluators` | The code evaluators found on disk |
| `GET /api/credentials` | The stored credentials by name, kind and fingerprint, and the kinds there are |
| `PUT` `DELETE /api/credentials/<label>` | Store or remove one. The value is never returned. |
| `GET` `PUT /api/kit` | How many runs and events are kept, and whether every request is recorded |

### The console's own routes

The console uses a few routes of its own under `/api/ui/`: the state it polls, saving the routing, running the functional tests, the chat box, and the ChatGPT sign-in. They follow the console and can change with it. A script should use the routes above.

## Configuration

Set these in the environment before starting `server.py`.

| Variable | Default | Sets |
|---|---|---|
| `LLMSIM_PORT` | `8099` | The port |
| `LLMSIM_SCRIPT` | `responses.json` | The shipped rules |
| `LLMSIM_RULES` | `llmsim.rules.json` | Your rules |
| `LLMSIM_CONFIG` | `llmsim.config.json` | Routing, fixed answers and settings, as the console saves them |
| `LLMSIM_SUITE` | `llmsim.tests.json` | The functional test cases |
| `LLMSIM_ASSERTIONS` | `llmsim.assertions.json` | The saved assertion sets |
| `LLMSIM_CASSETTES` | `cassettes/` | Where recordings are kept |
| `LLMSIM_RECORD`, `LLMSIM_REPLAY` | none | Start already recording, or replaying, the named recording |
| `LLMSIM_ANTHROPIC_KEY`, `LLMSIM_OPENAI_KEY`, `LLMSIM_DEEPSEEK_KEY` | The published keys | The key each vendor endpoint accepts |
| `LLMSIM_UPSTREAM`, `LLMSIM_UPSTREAM_MODEL`, `LLMSIM_UPSTREAM_KEY`, `LLMSIM_UPSTREAM_FORMAT` | none | The upstream a relay goes to |
| `LLMSIM_UPSTREAM_FALLBACK` | off | Answer here when the upstream cannot be reached |
| `LLMSIM_ANTHROPIC_UPSTREAM_KEY`, `LLMSIM_ANTHROPIC_UPSTREAM_MODEL` | none | The key and model the Anthropic API route uses |
| `LLMSIM_ANTHROPIC_UPSTREAM`, `LLMSIM_ANTHROPIC_UPSTREAM_AUTH` | Anthropic's API, `api_key` | Its base URL, and `bearer` for a gateway's token |
| `LLMSIM_CHATGPT_AUTH` | `llmsim.chatgpt-auth.json` | Where the ChatGPT sign-in is kept |
| `LLMSIM_CHATGPT_MODEL` | The plan's own choice | The plan model to use |
| `LLMSIM_RUNS_DB` | `llmsim.runs.db` | Where the test kit keeps runs |
| `LLMSIM_CREDENTIALS` | `llmsim.credentials.json` | Where the test kit keeps credentials |
| `LLMSIM_EVALUATORS` | `evaluators/` | Where code evaluators are read from |
| `LLMSIM_ALLOW_PROCESS` | off | Allow the `process` driver |
| `LLMSIM_LOG_SIZE` | `300` | How many requests the live tail keeps |

### What to commit

Commit `llmsim.rules.json`, `llmsim.tests.json`, `llmsim.assertions.json` and the recordings you rely on. Never commit `llmsim.config.json`, `llmsim.chatgpt-auth.json`, `llmsim.credentials.json` or `llmsim.runs.db`: they hold an upstream or Anthropic key, a sign-in, credentials and what runs said.

### In a container

`Dockerfile` builds an image and `deploy/install.sh` installs it in a Kubernetes namespace as `http://llmsim.<namespace>.svc:8099`. The manifest mounts the rule script and nothing else, so routing, a ChatGPT sign-in and recordings made in the pod are lost when it restarts. Mount a volume and point the variables above at it to keep them.

## Limits

- There is no authentication. Anyone who can reach the port can use everything here, and can spend a signed-in ChatGPT plan or a saved Anthropic key. Keep it on your own machine or a network you trust.
- Captured requests are held in memory only.
- An assertion that passes over a request too large to keep whole says so in `warnings`.
- A streamed answer from a real model is passed through event by event, so only the stream faults can reach it.

### Tests

```bash
for t in tests/test_*.py; do python3 "$t" || break; done
```

Each file starts its own LLMSim on a free port with a throwaway directory. The instance you are running is never touched.
