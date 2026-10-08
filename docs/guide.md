# Test an agent by playing its model

LLMSim answers in Anthropic, OpenAI and DeepSeek wire formats, so any agent that
takes a base URL can be pointed at it. You decide what the model says, break it
on purpose, and check what the agent sent.

```
Your agent  ──►  LLMSim plays the model  ···  The agent's tools
(harness,        (scripts the answers,        (files, APIs,
 tools,           injects faults,              other agents)
 guardrails)      keeps every request)
```

LLMSim sees the system prompt, declared tools, every message, tool results, the
turn count and the credential used. It does not see what the agent's tools did.

The same guide is served by LLMSim at `http://localhost:8099/guide`
([`guide.html`](guide.html)), with a copy button on every command. The
[README](../README.md) is the reference and the reasons behind each decision.

- [Start it](#start-it)
- [Point an agent at it](#point-an-agent-at-it)
- [Decide what the model says](#decide-what-the-model-says)
- [Make things go wrong](#make-things-go-wrong)
- [See what was sent](#see-what-was-sent)
- [Check it](#check-it)
- [Record once, replay](#record-once-replay)
- [Answer with a real model](#answer-with-a-real-model)
- [A worked example](#a-worked-example)
- [The API on one page](#the-api-on-one-page)
- [What it cannot see](#what-it-cannot-see)

## Start it

Python 3.9 or newer. Nothing to install.

```bash
python3 server.py            # serves on port 8099
LLMSIM_PORT=9000 python3 server.py
```

The console is at `http://localhost:8099/ui`. Everything on it can also be done
over HTTP, and this guide shows both.

## Point an agent at it

Give the agent a base URL and the matching key. The keys are fake and published;
a wrong one gets the vendor's own 401.

| Format | Base URL | Key |
|---|---|---|
| Anthropic | `http://localhost:8099/anthropic` | `sk-ant-llmsim-000000000000000000000000` |
| OpenAI | `http://localhost:8099/openai/v1` | `sk-llmsim-000000000000000000000000` |
| DeepSeek | `http://localhost:8099/deepseek/v1` | `sk-llmsim-deepseek-0000000000000000` |

### Say who is asking

Put a run and an agent name in the URL. LLMSim then knows which requests belong
together, and which agent sent each one.

```
http://localhost:8099/run/r1/agent/planner/openai/v1
http://localhost:8099/run/r1/agent/worker/anthropic
```

Or ask for the URLs:

```bash
curl -X POST localhost:8099/api/runs -d '{"run": "r1", "agents": ["planner", "worker"]}'
```

If your traffic comes through a gateway that carries an identity in a header or
a body field, set that up under **Evidence → Settings**. The headers
`X-LLMSim-Run`, `X-LLMSim-Agent` and `X-LLMSim-App` work without any setup.

## Decide what the model says

*Console: Behaviors*

A rule says: when a request looks like this, answer with these turns. The first
turn answers the first request of a conversation, the second answers the next,
and so on.

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

The second turn **branches on what the tool printed**. That is how you test that
an agent carries on when a request is granted and stops when it is denied.

### What a rule can test

| Condition | Tests |
|---|---|
| `contains`, `regex` | The ask: the last thing a person said that was not tool output |
| `toolOutput`, `anyToolOutput` | What the last tool printed, or any tool so far |
| `turn` | Which turn this is: `2` or `{"min": 1}` |
| `identity` | `{"agent": "worker"}`, so two agents get different answers |
| `system`, `header` | The system prompt, or a request header |
| `tool`, `model`, `provider`, `stream` | The request itself |
| `not`, `all`, `any` | Combine the others |

### Exact token counts

For a cost or budget test, a turn can state its usage. Zero is a real count.

```json
{"text": "counted", "input_tokens": 1000, "output_tokens": 250, "cached_tokens": 400}
```

### Find out why a rule did not fire

The dry run names the rule that would answer and, for every rule that would not,
the reason. Nothing is sent or captured.

```bash
curl -X POST localhost:8099/api/rules/test -d '{"ask": "please reconcile the ledger"}'
```

Your rules are saved in `llmsim.rules.json` and are tried before the shipped
ones in `responses.json`. Commit the file.

## Make things go wrong

*Console: Faults*

A real vendor will not fail on demand. A fault rule makes LLMSim fail for the
requests you choose.

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

The gates choose which matching requests are hit: `after` skips the first few,
`every` takes each Nth, `limit` stops after a number, and `probability` rolls a
die. Set a `seed` and the same run fails the same way next time.

A fault can also sit on one scripted turn:
`{"text": "…", "fault": {"type": "abort_stream", "after": 2}}`.

A `status` or `drop` fault is applied before anything is sent to a real model,
so it never costs a call.

## See what was sent

*Console: Evidence → Requests*

Every request is kept: the system prompt, declared tools, messages, who sent it,
which credential it used, and what answered it.

```bash
curl 'localhost:8099/api/captures?run=r1&agent=worker'   # a list
curl localhost:8099/api/captures/12                       # one request in full
```

Requests are held in memory only, and the oldest are dropped past a limit you
set. Redaction patterns apply to everything shown or exported.

## Check it

*Console: Evidence → Assertions*

An assertion is a claim about the requests an agent made. Each one has a `scope`
and exactly one check.

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

Each result comes back as `pass`, `fail` or `no-data`, with the request numbers
and an excerpt as evidence.

> **No data is not a pass.** If the scope selects no requests, the result is
> `no-data`. An agent that never ran would otherwise pass every "must not". Add
> `"allowEmpty": true` when an empty result really is fine.

Save a list as a named set with `POST /api/assertion-sets` and run it with
`{"set": "my-set", "run": "r1"}`. Sets are saved in `llmsim.assertions.json`.

### Check a handoff between two agents

LLMSim does not see messages that pass between agents. It does see what each
agent then tells its model. Script the first agent's answer to carry a token,
and assert that the token turns up in the second agent's request.

```json
{"id": "handoff", "name": "The worker was told what the planner answered",
 "scope": {"identity": {"agent": "worker"}}, "count": {"min": 1},
 "where": {"field": "allText", "op": "contains", "value": "TRACE-7f3a9c"}}
```

## Record once, replay

*Console: Sessions*

Run a session once and keep every answer. Later runs get the same answers back,
offline and for free.

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

- A replay matches a request by what was asked and how many turns in it is. It
  does not compare whole requests, which contain timestamps and never repeat.
- A harness that asks two things at the same point — OpenCode asks for a
  conversation title alongside the agent's first request — gets both answers
  kept. They are told apart by the tools each request declares.
- An empty answer is recorded too. It is how an agent's last turn often looks.
- A request the recording does not hold gets a 404 that names it. Pass
  `"onMiss": "route"` to let it take its normal route instead.
- If your prompts contain something that changes every run, such as a run id
  made from the clock, add a mask: `"masks": ["\\d{8}-\\d{6}"]`.
- Leave out `run` to record or replay every request that names no run. Several
  runs can record at once without seeing each other.
- `POST /api/sessions/auto` replays if the recording exists and records if it
  does not.

> A recording holds the model's answers, and an answer can quote what it was
> shown. Read a cassette before you commit it.

## Answer with a real model

*Console: Routing*

By default every answer is scripted and nothing leaves your machine. Two other
routes use a real model, for one model or for all of them.

| Route | Answers come from | Set it up |
|---|---|---|
| Relay | One model behind an API key | Routing → Upstream: URL, model and key |
| ChatGPT plan | Your ChatGPT subscription | Routing → ChatGPT plan → Sign in |

The usual pattern: record a session through a real model once, then replay it
for every later test run.

> **Once you sign in, LLMSim holds a real subscription.** It listens on every
> network interface and the console has no login, so anyone who can reach the
> port can spend the plan. Keep it on your own machine or a network you trust.

## A worked example

The agent under test is a read-only assistant. It must stop when a capability is
denied, and it must never send a card number to the model.

1. **Give it a run.** Point the agent at
   `http://localhost:8099/run/t1/agent/assistant/openai/v1`.
2. **Script the model.** Add the `needs-capability` rule from
   [above](#decide-what-the-model-says). Its second turn says "Stopping" when
   the tool prints `[]`.
3. **Break something.** Add a fault so one answer arrives with tool arguments
   that are not JSON:

   ```bash
   curl -X POST localhost:8099/api/faults -d '{
     "name": "bad-json", "match": {"identity": {"run": "t1"}}, "limit": 1,
     "fault": {"type": "malformed_tool_args", "mode": "truncated"}}'
   ```
4. **Run the agent** on "please reconcile the ledger".
5. **Check what it did.**

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
6. **Read the verdict.** A `fail` lists the request numbers. Open one under
   Evidence to see exactly what the agent sent.
7. **Clean up.** `curl -X DELETE localhost:8099/api/runs/t1` forgets the run's
   requests and stops its session.

## The API on one page

| Area | Routes |
|---|---|
| Runs | `GET POST /api/runs` · `GET DELETE /api/runs/<run>` |
| Rules | `GET POST /api/rules` · `PUT DELETE /api/rules/<name>` · `POST /api/rules/test` · `/validate` · `/order` · `/reload` |
| Faults | `GET POST /api/faults` · `PUT DELETE /api/faults/<id>` · `POST /api/faults/pause` · `/resume` · `/reset` |
| Requests | `GET DELETE /api/captures` · `GET /api/captures/<n>` · `GET /api/captures/export` |
| Assertions | `POST /api/assertions/evaluate` · `GET /api/assertions/catalog` · `/api/assertion-sets` |
| Sessions | `GET /api/sessions` · `POST /api/sessions/record` · `/replay` · `/auto` · `/stop` · `GET PUT DELETE /api/sessions/<name>` |
| Settings | `GET PUT /api/settings` · `POST /api/settings/credentials` |

| Status | Means |
|---|---|
| `400` | The request was wrong. The body lists every problem under `problems`. |
| `404` | There is no such rule, fault, cassette or run. |
| `409` | The current state forbids it, for example a recording is already running. |
| `403` | The request came from a page on another website. |

### See which credential arrived

To find out which key a gateway really sends, accept any key and register the
ones you know. Only a fingerprint is stored.

```bash
curl -X PUT  localhost:8099/api/settings -d '{"auth": {"mode": "any"}}'
curl -X POST localhost:8099/api/settings/credentials -d '{"label": "platform-token", "key": "…"}'
```

Each captured request then shows its credential's label, and an assertion can
test `credential.label`.

## What it cannot see

- **What tools actually did.** Files written and calls made happen outside the
  model's traffic.
- **Messages between agents** that never pass through a model.
- **An agent that cannot change its base URL,** or one that runs in a vendor's
  cloud.
- **How good the agent is with a real model.** A scripted model shows what the
  agent does when the model says exactly that.

Two more things to know. An assertion that passes over a request too large to
keep whole says so in `warnings`. And a streamed answer from a real model is
passed through event by event, so only the stream faults can reach it.

---

Tests: `python3 tests/test_behaviors.py` and `python3 tests/test_chatgpt_plan.py`.
