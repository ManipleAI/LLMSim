# LLMSim: design notes and reference

Why LLMSim is built the way it is, and the detail behind each part. This was the
README until the documentation was written; nothing in it has been dropped.

- To get going, read the [README](../README.md).
- To use it, read [the documentation](guide.md), which LLMSim also serves at
  `http://localhost:8099/guide`.
- For the test kit's internals, read [Testing an agent](agent-testing.md).

## Why simulate the vendors and not the gateway

A gateway already translates between formats. Simulating the gateway would
only ever prove the simulator agrees with itself, and would leave the
gateway's own translation — the part most likely to be wrong — untested.
Sitting where the vendor sits keeps the entire real path in the picture:

```
Claude Code ─┐
HarnessRouter ├→ LiteLLM ──→ LLMSim
Langflow      │              (instead of api.anthropic.com)
flows        ─┘
```

One simulator, every caller, whatever format each of them speaks.

## What it does that a gateway's built-in mock cannot

LiteLLM has `mock_response`, and it is genuinely useful: it streams, it
serves both formats, it needs no provider key. It was measured against a
live LiteLLM 1.103.0 before this was written, and it does all of that.

What it cannot do is **emit a tool call**. `mock_tool_calls` is ignored, and
per-request overrides are ignored; a model entry returns one fixed string
forever. A fixed string proves routing, budgets, translation and streaming.
It can never make an agent write a file, run a command, or take a second
turn — so it cannot test an agent loop at all, only the pipe the loop runs
through.

LLMSim emits real `tool_use` / `tool_calls` blocks, with the right
`stop_reason`, and advances through a scripted conversation turn by turn.

## Endpoints

| Provider | Base URL | Route | Auth header |
|---|---|---|---|
| Anthropic | `http://localhost:8099/anthropic` | `POST /v1/messages` | `x-api-key` |
| OpenAI | `http://localhost:8099/openai` | `POST /v1/chat/completions` | `Authorization: Bearer` |
| DeepSeek | `http://localhost:8099/deepseek` | `POST /v1/chat/completions` | `Authorization: Bearer` |

Also served: `GET /v1/models` (Claude Code queries this on startup for
gateway discovery, and shows an empty model list if it 404s),
`POST /v1/messages/count_tokens`, and `GET /healthz`, which lists the rules
currently loaded.

The provider prefix is how one process serves three vendors whose paths
collide — OpenAI and DeepSeek both want `/v1/chat/completions`. Drop the
prefix and the provider is inferred from the key presented instead.

## Keys

Hardcoded, fake by construction, and overridable by environment variable:

| Provider | Key |
|---|---|
| Anthropic | `sk-ant-llmsim-000000000000000000000000` |
| OpenAI | `sk-llmsim-000000000000000000000000` |
| DeepSeek | `sk-llmsim-deepseek-0000000000000000` |

A client presenting the wrong key gets a **401 in that vendor's own error
envelope**, so a misconfiguration fails the way it would in production
rather than quietly succeeding.

Nothing here is a real credential and nothing here should become one. If you
find yourself wanting to put a live key in this file, you want a different
tool.

## Scripting responses

`responses.json` holds ordered rules. Each has a `when` and a list of
`turns`:

```json
{
  "name": "write-a-file",
  "when": { "regex": "create|write|make (a|one) file" },
  "turns": [
    { "text": "I'll create that file now.",
      "tool_calls": [{ "name": "Write", "input": { "file_path": "HELLO.md" } }] },
    { "text": "HELLO.md is written, so I'll stop here." }
  ]
}
```

`when` takes any of `contains`, `regex`, `provider`, `model` — all optional,
all must hold. A turn takes `text`, `tool_calls`, `stop_reason`,
`input_tokens`, `delay_ms`, and `error { status, type, message }`.

Two placeholders are filled in a turn's `text` and in the string arguments of
its tool calls: `{{ask}}` is the opening ask the rule was matched on, and
`{{tool_output}}` is what the most recent tool call printed. They are what lets
one rule drive a retrieval loop — search *for the question*, then answer *with
what came back* — so a test of a grounded agent can assert that a passage
reached the reader, not merely that a tool was called. The `knowledge-search`
rule, keyed on the tool `search_context`, is the example.

**Two decisions, kept apart.** *Which rule* comes from the conversation's
opening ask. *Which turn within it* comes from how many assistant turns have
already happened.

Both halves of that are load-bearing, and both were arrived at the hard way:

- Matching on the *latest* user message looks right and is wrong. By turn two
  the latest user message is a tool result, so the conversation gets
  re-matched against whatever the tool printed. A file named `HELLO.md`
  dragged an agent-loop conversation into the `hello` rule, mid-loop.
- Selecting a turn by *hashing the request* fails even faster. Turn three's
  request contains turns one and two, tool output included — timestamps, ids,
  directory listings — so a hash misses on the second turn, every time. A
  count of assistant messages is stable under all of it.

The last turn repeats if a conversation runs longer than the script.

## The console

```
python3 server.py            # then open http://localhost:8099/ui
```

One page, served by the same process, no build step and no dependency, laid
out as a control plane:

| | |
|---|---|
| **Overview** | What it is doing right now, the address and key for each vendor, and which screen does which job. |
| **Routing** | The default route, and the upstream or ChatGPT plan everything on it goes to. |
| **Models** | Every model the simulator advertises. Pick one to set what it answers — fixed text, a failure status, a delay — and where its traffic goes. |
| **Answer rules** | What the simulated model says, and when. |
| **Faults** | Things that go wrong on purpose. |
| **Requests** | Every request as it arrives (*Live*), each one in full (*Captured*), checks over them (*Assertions*), and what is kept (*Capture settings*). |
| **Chat** | Talk to a model by hand, the way a client would. |
| **Record & replay** | Record a real run once and replay it exactly. |
| **Functional tests** | Cases that send a prompt and say what must come back. |
| **Agent tests** | Drive an agent, simulate what it talks to, judge the result. |

Every screen has an address (`/ui#routing`, `/ui#lab`), so a page can be linked to.

### Functional tests

The simulator answers; these check that whatever is calling it behaves. A
case is an ordered list of steps, each one a prompt in and an expectation
about what comes back — so an agent loop is written as the sequence it
actually is:

```
Code generation: writes the file it was asked for     claude-sonnet-5
  1  Create a file called HELLO.md...  → calls Write, says "create"
  2  HELLO.md written, 1 line.         → contains "HELLO.md is written"
     (sent as tool output)
```

**Tick "sent as tool output" for a step that is a tool reporting back rather
than a person speaking.** It matters more than it looks: `subject()`
deliberately ignores tool output when deciding what a conversation is about,
so an agent loop keeps the rule its opening ask chose. A step sent as plain
user text reads as a new question and pulls the conversation elsewhere —
which is exactly how the two-step case above failed the first time it was
written.

Six cases ship, and they pass on a fresh install:

| | |
|---|---|
| Code generation | Writes the file it was asked for, then reports it done |
| Code generation, OpenAI-shaped | The same ask from an agent declaring `exec_command` rather than `Write` — the harness follows the caller, not one vendor's spelling |
| Planning | Asks for `SAP.Invoice.Read` and files a dependency **rather than inventing invoice data** — and asserts no currency figure appears anywhere, in the answer or in a tool call |
| Back-pressure, 429 | The caller is handed a rate limit |
| Back-pressure, 529 | The vendor is overloaded |
| Patience | An eight-second answer must not be abandoned |

#### What a step can assert

All of these are optional and they compose.

| | |
|---|---|
| **Text** | `contains`, `regex` or `equals` against the answer. |
| **Tool** | A tool the caller was offered and should have called. |
| **Tool arguments** | `file_path=HELLO.md`, one per line, by dotted path (`edits.0.new_string`). This is usually the real question: *"Write was called"* passes just as happily when the agent writes to `/tmp`. |
| **HTTP status** | So *"this key is over its quota and must answer 429"* is a test rather than a note. |
| **Must NOT contain** | The one that can only be a negative. *"Never invents a figure it could not read"* has no positive form, and an agent that answers with a total it could not have fetched looks like success. Checked against the answer **and every tool argument** — a made-up number inside a `Bash` command is just as fabricated. |
| **Must NOT be called** | A tool the step forbids. Blocked on a capability it does not hold, an agent should stop rather than reach for the shell again. |
| **Answer within (ms)** | A deadline. The `llmsim:slow` case proves a late answer still arrives; this is the other half — a path that is meant to be quick staying quick, which is what regresses silently. |

Steps run **in order, in one conversation**, and stop at the first failure,
because a later step normally assumes the one before it worked.

#### Plans

A plan is an **ordered** list of cases, run and reported together. Order is
information: put the quick cases first, so a run that is going to fail fails
early. Add, remove and reorder on the page; a new plan starts empty.

#### Where a suite lives

Cases and plans are written to **`llmsim.tests.json`**, beside the code and
**not** gitignored — commit it, review it in a diff, put it on a branch. It
is the one file here meant to be shared; `llmsim.config.json` stays
machine-local because it holds the upstream key.

**Export** downloads that file; **Import** takes one somebody else wrote,
merging into yours by default (an id that already exists is updated, so
re-importing an edited suite does not double it) or replacing it outright if
you say so.

Cases execute against this server over HTTP on the real vendor routes, with
the real keys — not through a shortcut into the internals. The point is to
exercise the whole path the caller uses, so that a passing test means the
caller's own request would have worked.

### A model's own answer

`responses.json` matches on the **prompt**, which is right for a scenario and
wrong for "this model is always slow" or "this one is over its quota". Those
are properties of the model, so they are set per model and win over the
script:

```json
"responses": {
  "claude-opus-5":    {"text": "...", "delay_ms": 250},
  "claude-haiku-4.5": {"status": 429, "text": "Over its quota."}
}
```

A status becomes that vendor's own error envelope, so a client's retry and
backoff handling is exercised rather than bypassed. Leave a model unset and
it falls through to the script exactly as before.

There is no sign-in. This runs on a developer's machine or a test box, holds
nothing worth taking, and a password in front of it was friction on the one
path everybody uses — the same posture the vendor endpoints already have,
since their keys are published in this file. Bind the port somewhere
reachable and the console is reachable too. The upstream key is still never
sent to the browser: the page is told whether one is stored, and no more.

## Two modes: scripted, or one real model behind every name

Scripted is the default and everything above describes it: `responses.json`
decides, deterministically, offline, free.

**Relay** answers with one real model instead. Whatever model the client
asked for — `claude-opus-5`, `gpt-5.4`, `gemini-3.8-flash` — the answer comes
from the upstream model, and is returned in the format the client asked in.

**The choice is per model, not per process.** A default covers everything,
and any model can be pinned to the other route. So a demo can send the one
model somebody will actually look at to a real cheap one, and keep the other
sixty-nine offline and free. The table lives in `llmsim.config.json` beside
the script, written by the console, and survives a restart; the environment
variables below seed it the first time and the file wins after that.

```
LLMSIM_UPSTREAM=https://api.openai.com/v1 \
LLMSIM_UPSTREAM_KEY=sk-... \
LLMSIM_UPSTREAM_MODEL=gpt-4o-mini \
python3 server.py
```

| Variable | Default | |
|---|---|---|
| `LLMSIM_UPSTREAM` | *(unset)* | Base URL. Unset means scripted — nothing leaves the machine. |
| `LLMSIM_UPSTREAM_KEY` | *(unset)* | The upstream's key. |
| `LLMSIM_UPSTREAM_MODEL` | `gpt-4o-mini` | The one model that answers everything. |
| `LLMSIM_UPSTREAM_FORMAT` | `openai` | `openai` or `anthropic` — what the upstream speaks, which need not be what the client speaks. |
| `LLMSIM_UPSTREAM_FALLBACK` | off | On an upstream failure, serve the script instead of an error. |

### Why this exists

Scripted answers are identical every run and visibly canned, which is right
for a test and wrong for a demo. Wiring seventy model names to real vendors
is seventy configurations and a real bill. Relay gives genuinely generated,
varied answers for any model name, from one cheap model, with nothing
downstream reconfigured — the gateway, the router and the agent still ask
for the model they were always going to ask for.

### What it does and does not change

The client's own tool declarations are forwarded, so an agent loop is real:
the model can call the tools that client actually has, and the call returns
through the same path a scripted one does, name realignment included.

Streaming still streams. The upstream is called once, unstreamed, and the
answer is re-emitted as SSE in the client's format — so the shape is right
even though the timing is not the upstream's.

`/healthz` reports which mode is running, and the startup log says so in a
sentence. A mode you cannot see is a mode you will misconfigure.

On an upstream failure the default is a vendor-shaped **502**, not a quiet
fall back to the script: a demo that silently serves canned answers looks
like it is working and proves nothing. `LLMSIM_UPSTREAM_FALLBACK=1` opts
into the other behaviour.

## A third mode: a ChatGPT subscription

Relay answers with a real model through an API key. This answers with one
through a **ChatGPT plan** — the person signs in with OpenAI, and every model
routed to it is answered by their subscription, in whichever format the
client asked in.

```
client (Chat Completions | Anthropic Messages | Responses)
        │  canon_request()
        ▼
CanonicalRequest ── chatgpt_payload() ──▶ POST {backend}/responses
        ▲                                   store:false, stream:true
        │                                          │
 encoders ◀── CanonicalEvents ◀── chatgpt_events() ┘
 (the client's own wire format, whole or streamed)
```

The middle is the one format the plan backend speaks. Every client format
goes in and out through the same two seams, so a fourth client format is one
translator rather than another copy of the whole path.

**To use it:** Routing → *ChatGPT plan* → **Sign in with ChatGPT**. A tab opens
at OpenAI; confirm access, and the console flips to *signed in*. Then pick
the route — as the default, or per model on the Models page — and choose the
plan model (the picker lists what the plan actually offers).

**The plan model is the plan's own choice unless you make one.** Which models an
account may use differs by plan and changes over time, so no name is built in —
the first one, `gpt-5.4`, was a guess that an account without it was refused on.
Left blank, LLMSim asks the plan for its list and uses its first *visible* model by
the plan's own priority (hidden models are never picked for you, though you may
name one). The list is cached for ten minutes and dropped on sign-in and sign-out.
If nothing is chosen and the list cannot be read, the request fails and says so
rather than guessing. A model you choose always wins. A config saved while
`gpt-5.4` was the built-in default is read as "not chosen".

```
curl localhost:8099/openai/v1/chat/completions \
  -H "Authorization: Bearer sk-llmsim-000000000000000000000000" \
  -d '{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hi"}]}'
```

**What crosses the seam.** Text, system prompts, tool declarations, tool calls
and their results, images, JSON-schema output and reasoning effort all
translate, and tool-call ids round-trip so an agent loop works. Sampling
controls — `max_tokens`, `temperature`, `top_p`, `stop` — are *not sent*,
because the plan backend rejects them; each request that carried one logs
which were dropped. Usage is the plan's own, cache hits included. A tool call
is passed on whole when the backend finishes it, not argument-fragment by
fragment, so streamed text is live but a streamed call arrives in one piece.
Anthropic's `message_start` carries zero input tokens, since they are not
known until the end; the real figures are on `message_delta`.

**How the sign-in works.** It is the OAuth + PKCE flow OpenAI's open-source
Codex client uses, with that client's registered id — read from its source,
not guessed. That is why the redirect must land on `127.0.0.1:1455` (or
`1457`): LLMSim listens there, on loopback only, for the length of the
sign-in. If it is running on another machine, or Codex holds the port, the
console asks you to paste the address the browser ended up on instead. The
`state` is checked, a code is single-use, and a request from another site's
origin cannot start, finish or cancel a sign-in.

Tokens live in `llmsim.chatgpt-auth.json` — created owner-only, replaced
atomically, gitignored — and are never sent to the console, which is told only
whether somebody is signed in and as whom. The access token is refreshed
before it lapses; if the refresh token is refused for good, LLMSim stops
asking and says *sign in again* rather than retrying on every request.
*Sign out* forgets the tokens and asks OpenAI to revoke the grant; if that
call fails it says so, and the grant can be removed in ChatGPT's settings.

**Failures are the client's own.** A reached plan limit is a **429** in the
vendor's envelope with the reset time in the message and a `retry-after`; a
bad request is a 400; an unreachable or refusing backend is a 502. A 401 from
OpenAI is *not* passed on as one — it would tell a gateway its own key was
wrong and send somebody to fix the wrong thing. After the first byte the
status is already sent, so a failure is reported inside the stream in that
format's error form. As with relay, nothing silently falls back to the
script; *Answer here if the plan cannot be reached* is opt-in.

| Variable | Default | |
|---|---|---|
| `LLMSIM_CHATGPT_MODEL` | *(empty)* | The plan model that seeds the setting; empty means the plan's own default, and the console wins after that. |
| `LLMSIM_CHATGPT_AUTH` | `llmsim.chatgpt-auth.json` | Where the tokens are kept. |
| `LLMSIM_CHATGPT_PORTS` | `1455,1457` | The redirect ports to try. |
| `LLMSIM_CHATGPT_ISSUER` / `_BACKEND` | OpenAI's | Overridden by the tests to point at a stand-in. |
| `LLMSIM_CHATGPT_CLIENT_ID` / `_ORIGINATOR` | Codex's | What the sign-in presents itself as. |

**Read this before turning it on.** The rest of LLMSim holds nothing worth
taking, and that is why the console has no sign-in. Signed in, this holds a
subscription: anyone who can reach the port can spend it, through the vendor
routes with their published keys. Keep it on your own machine or a network you
trust. The sign-in presents as the Codex client, which is what makes it work;
whether a given use of a plan that way is permitted is OpenAI's to say, not
this file's. Usage counts against the plan's own limits.

**What is and is not verified.** `tests/test_chatgpt_plan.py` runs the whole
route against a stand-in for OpenAI — no network, no account — and checks the
contract strictly: the PKCE verifier against its challenge, `store:false`,
`stream:true`, the account header, rotating refresh tokens, a revoked token
refreshed once and retried, a dead one not retried, tool-call round trips in
all three formats, and every failure path. That proves LLMSim speaks the
contract it read. It cannot prove OpenAI still honours it today; only a real
sign-in does, and the first one is the real test.

```
python3 tests/test_chatgpt_plan.py
```

## Recording a session, and replaying it exactly

Scripted answers are written by hand, and relayed ones differ every time. A
**session recording** is the third thing: run a real session once — a Maniple
agent going through the gateway to a real model, say — keep every answer, and
from then on answer that same run with exactly those words. Offline, free, and
identical every time, which is what a regression test of an agent needs and a
live model can never give.

```
Record & replay → name it → Start recording → run the session → Stop and save
Record & replay → Replay
```

### The API a test harness drives

Plain HTTP, so a harness needs nothing but a client. Maniple's suite drives it
this way (`test/system/llmsim.py`, with `--llmsim-record` / `--llmsim-replay`).

| | |
|---|---|
| `GET /api/sessions` | What is running, its counts, and every cassette LLMSim holds. |
| `POST /api/sessions/record` | `{name, masks?, overwrite?}` — start recording. An existing name is a 409 unless `overwrite`. |
| `POST /api/sessions/replay` | `{name, masks?, onMiss?, strict?}` — start replaying. |
| `POST /api/sessions/auto` | `{name, …}` — replay if the cassette exists, otherwise record it: the first run of a test makes the recording and every run after it uses it. |
| `POST /api/sessions/stop` | Stop, and return the **summary**: `entries`, `hits`, `misses`, `diverged`, and `unmatched` — each ask with no recording, and why. |
| `GET /api/sessions/<name>` | The cassette, as a file. |
| `PUT /api/sessions/<name>` | Upload one (the same JSON); `?overwrite=1` replaces. |
| `DELETE /api/sessions/<name>` | Delete one. |

Status codes say what went wrong so a caller need not read a sentence: **400**
it asked wrongly (a bad name, a mask that is not a pattern), **404** there is no
such cassette, **409** the state forbids it (something is already running, the
name exists, nothing is running to stop), **403** the request came from another
website's page. One session runs at a time.

```
curl -X POST localhost:8099/api/sessions/record -d '{"name":"login-flow","masks":["\\d{8}-\\d{6}"]}'
…run the session…
curl -X POST localhost:8099/api/sessions/stop        # → summary
curl localhost:8099/api/sessions/login-flow > login-flow.json

curl -X PUT 'localhost:8099/api/sessions/login-flow?overwrite=1' -d @login-flow.json
curl -X POST localhost:8099/api/sessions/replay -d '{"name":"login-flow"}'
…run it again…
curl -X POST localhost:8099/api/sessions/stop        # → hits, misses, unmatched

LLMSIM_REPLAY=login-flow python3 server.py           # or start already replaying
```

**The cassette is a file the caller keeps.** LLMSim's own directory is
machine-local, and a test should not depend on what an earlier run left there. So
a harness downloads the cassette after recording, keeps it in its own repository,
and uploads it again before a replay.

**Masks.** A request is matched on its ask, and an ask can carry text that is
different every run — a run id made from the clock, say, which Maniple puts in
every prompt it sends. Left alone, a replay asks for something the recording
never held. A *mask* is a regular expression whose matches are removed from the
ask, in the recording **and again in the replay**:

```
"masks": ["\\d{8}-\\d{6}"]        # "system test 20261006-153012 verified …"  →  "system test <*> verified …"
```

Masks are stored in the cassette, so a replay applies the ones the recording
used and any the caller adds. They are bounded (20, each up to 200 characters)
and checked when the call is made.

**A replay's settings are its own.** `onMiss` and `strict` on a replay apply to
that replay and no other. They default to the console's settings but are not
those settings: a test that wants strictness for one run must not change what the
next run, or the console, does.

**The functional cases are exempt.** The cases on *Functional tests* check a
*caller* against the script's rules. LLMSim answers them from the script whatever
the default route is — a relay or the ChatGPT plan would spend something and say
something else, and the cases would fail — and leaves them out of any recording,
so they neither pollute a cassette nor fail a replay. The marker is a header the
runner sets on its own requests, honoured only from this machine.

**What is recorded is the answer, not the request.** Its text, its tool calls
with their arguments and their ids, and the token counts the route reported.
Not the request: a harness request is megabytes of system prompt and file
contents, none of it needed to answer the next identical one. An answer can
still quote one, so a cassette is written owner-only to `cassettes/` and is not
committed. Whatever route produced the answer — the script, a relay, the ChatGPT
plan — is kept, so the usual way to make one is to record a session through the
plan and replay it for free from then on. A refusal, a rate limit or a failure
is never recorded as an answer.

**How a request is matched.** Not by the whole request. That cannot work: turn
three carries turns one and two inside it, tool output included — timestamps,
ids, directory listings — so it never repeats. This is the same reason the
script advances by counting assistant turns. A request is its **ask** — the last
thing a person said that was not tool output, the same text a script rule is
matched on — plus **how far into the conversation it is**. Harnesses prepend
`<system-reminder>` blocks carrying the date and the like; they are not the ask
and change daily, so they are removed first.

That is lenient on purpose: the same ask at the same point gets the same answer
even if a tool printed something different this time. The difference is counted
and shown (*last message differed*), not swallowed. **Strict** turns it into a
miss, for the cases where the tool output is what is being tested.

- **A request the recording cannot answer** fails with a 404 naming the cassette,
  the ask and the turn, and appears in the console under *Not in the recording*.
  Failing is the default because a run that has wandered off its recording should
  be loud, not quietly talking to something else. Set it to *take its normal
  route* when the harness makes side calls that are not worth recording.
- **A replay comes before routing.** A recorded question is answered from the
  cassette whatever the model is routed to, so the run no longer depends on
  anything that route reaches.
- **Any wire format.** A recording made through one vendor's format replays
  through another's — the answer is stored once and built per caller. The one
  place that is not exact is a parallel tool call, which Chat Completions sends as
  one assistant message and Responses as several: they count turns differently.
- **One answer per ask, turn and caller.** A retry, or the same step of a second
  run, replaces the earlier one. A harness that asks two things at the same
  point is the exception that is kept apart: OpenCode asks for a title for the
  conversation alongside the agent's first request — the same ask, the same
  turn, one wanting eight words and the other a tool call. They are told apart
  by the tools each request declares, and each is given its own answer back; a
  caller whose tools have changed a little is given the nearest.
- **An answer that says nothing is an answer.** An agent's last turn is often
  empty — the work is done and the model stops. It is recorded and replayed as
  that, so a run is replayed through to its end.

A cassette is a JSON file. **Download** and **Import** move one between machines;
an imported file is checked, and the keys in it are worked out again rather than
believed, since a key decides which request is given which answer.

**What is not verified.** The matching has been tested against the script, the
plan stand-in and all three wire formats. It has not been tried against a real
Maniple session, whose requests carry more than any test here does. The first
recording will say: if requests that should match show as *no recording*, the
console lists the ask each one arrived with.

## Testing an agent: behaviors, faults and evidence

Everything above makes LLMSim a convincing *model*. This is what makes it a way to
test the *agent* around the model — any agent that can be pointed at a base URL.
LLMSim is the one place that sees every request the agent makes to its model, and
that is a better place to check an agent from than inside it.

Four things, sharing one description of a request (a *view*) and one language for
saying something about it (a *condition*). All of it is configurable on the console
(**Answer rules**, **Faults**, **Requests**) and over HTTP: the console is a client of
the same API a test harness uses.

### Who sent it

Point each agent at its own base URL and LLMSim knows who is who:

```
http://localhost:8099/run/<run>/agent/<agent>/openai/v1        also …/anthropic, …/deepseek/v1
POST /api/runs {"run": "r1", "agents": ["planner", "worker"]}  → the URLs, ready to paste
```

That is the one thing every agent platform has to support, so it needs nothing of
the platform — not a header, not a plugin. Identity can also be read from a header
(`X-LLMSim-Run`, `-Agent`, `-App` by default), a body field or the credential, with
rules you configure under *Requests → Capture settings*: which header a given gateway
forwards is a fact about that gateway, so it is configured and not assumed.

Which **credential** arrived is recorded too, as a fingerprint — never the key.
Register what a fingerprint is (`POST /api/settings/credentials {"label","key"}`:
the key is fingerprinted and not stored) and an assertion can say "the session used
the platform token". `auth.mode` is `published` (only this file's keys, the default),
`registered`, or `any` — *witness mode*, which accepts anything and says which came.

### Conditions

One language, used by a rule's `when`, a fault's `match`, an assertion's scope and its
checks. Every key must hold; `not`, `all` and `any` combine them.

| | |
|---|---|
| `contains`, `regex` | the ask — the last thing a person said that was not tool output |
| `provider`, `model`, `route`, `shape`, `path`, `stream` | the request itself |
| `tool` / `tools` | tools the client declared |
| `turn` | `2`, or `{"min": 1, "max": 3}` — assistant turns already taken |
| `toolOutput`, `anyToolOutput`, `lastMessage`, `system` | text conditions: a string (contains) or `{contains, not_contains, regex, not_regex, equals, empty, case}` |
| `identity` | `{"run": …, "agent": …, "app": …}` |
| `header`, `credential` | `{"x-tenant": "acme"}`, `{"label": "platform-token", "known": true}` |
| `status`, `fault` | the answer, for assertions |
| `{field, op, value}` | any field of the view: `response.toolNames includes Write`, `response.usage.input gt 1000`, … |

Text matches ignore case unless `{"case": true}`.

### Rules that branch

Your rules live in `llmsim.rules.json` — also source, also meant to be committed —
and are tried **before** the shipped `responses.json`, which is never written. A rule
may now condition on what the last tool said, so an agent's conditional flows are
testable: *granted, so carry on; denied, so stop.*

```json
{"name": "needs-capability", "when": {"contains": "reconcile the ledger"},
 "turns": [
   {"text": "Checking what I am granted.", "tool_calls": [{"name": "granted", "input": {}}]},
   {"text": "Unclear. Asking.", "branches": [
     {"when": {"toolOutput": {"contains": "SAP.Invoice.Read"}}, "turn": {"text": "Granted. Reading."}},
     {"when": {"toolOutput": {"regex": "^\\[\\]$"}},            "turn": {"text": "Nothing granted. Stopping."}}]}]}
```

A turn can also set **exact usage** — `input_tokens`, `output_tokens`, `cached_tokens`,
`reasoning_tokens` (zero is a count, not "unset") — which is what a cost or budget test
needs. A rule with `when.turn: {"min": 2}` answers from its first turn when the
conversation reaches turn 2. `{{ask}}`, `{{tool_output}}`, `{{run}}`, `{{agent}}`,
`{{app}}`, `{{turn}}` and `{{model}}` are filled in.

`POST /api/rules/test` is a dry run that says which rule would answer **and, for every
one that would not, why not** — the question that follows writing any rule.

### Faults

The paths that only run when something goes wrong are the ones no live vendor will
show you on demand. A fault is a thing to do, with two ways to say when: on a scripted
turn (`"fault": {…}`), or as a *fault rule* with a `match`, `probability`, `every`,
`after`, `limit` and a `seed` — so a chaotic run can be run again.

| `type` | |
|---|---|
| `status` | the vendor's own error envelope, with an optional `retry_after` |
| `delay`, `drop` | wait first; hang up before answering (`close` or `reset`) |
| `abort_stream` | stop after N events, as a clean close or a reset |
| `stall`, `slow_stream`, `garbage` | pause once; pause between events; one frame (or a whole body) that is not JSON |
| `malformed_tool_args` | arguments that are not valid JSON (`truncated`, `not_json`, `array`, `empty`) |
| `unknown_tool`, `empty`, `huge` | call a tool nobody declared; a 200 that says nothing; an answer of N bytes |

Faults apply on **every route** — scripted, relay, the ChatGPT plan, a replay — since a
fault is about how the vendor misbehaves, not who answered. `status` and `drop` happen
*before* anything is spent upstream, so injecting one never costs a call. A recording
keeps the real answer, not the fault. LLMSim's own functional cases are never faulted.
Limit: a streamed relay or plan answer is passed through event by event, so only the
stream faults (`abort_stream`, `stall`, `slow_stream`, `garbage`) reach it.

### Evidence and assertions

Every request is **captured**: what was sent, who sent it, what answered it, with what
usage. Held in memory, bounded (`capture.max`, `capture.maxBytes`), never written to
disk. `GET /api/captures` filters by run, agent, app, model, route, status or text;
`…/<n>` is one in full; `…/export` is a download.

An assertion is a claim about the captured requests:

```json
{"id": "no-card-numbers", "name": "No card number reaches the model",
 "scope": {"identity": {"run": "r1"}},
 "none": {"field": "allText", "op": "regex", "value": "\\b\\d{13,16}\\b"}}
```

with exactly one of `each` (every request satisfies it), `none` (no request does),
`count` (`{min, max}`, with an optional `where`) or `sequence` (conditions found, in
order). `POST /api/assertions/evaluate` returns a verdict, and for each assertion a
status, a detail and **evidence**: which requests, and an excerpt. Sets of assertions
are saved in `llmsim.assertions.json` (commit it) and run by name.

Four decisions, each from something that fails otherwise:

- **No data is not a pass.** "No request carried a card number" is true of an agent
  that never ran. A check that selects nothing reports `no-data` unless it says
  `allowEmpty`.
- **Assertions run on what was sent; what is shown is redacted.** A leak check run on
  redacted text could never find a leak. Redactions (`capture.redact`) apply to the
  console, the API and exports — including the evidence an assertion returns.
- **It says when it saw less than everything.** A request too big to hold whole is
  reduced; an assertion that passed over one says so.
- **A handoff between agents never crosses LLMSim, but its contents do.** Have agent A's
  scripted answer carry a trace token, and assert that it appears in agent B's next
  request: that proves the handoff happened, what B was told, and in what order.

### Runs that do not see each other

Sessions (recording and replay) are scoped by run: `POST /api/sessions/record
{"name", "run": "r1"}` records only `/run/r1/…` traffic, and several runs can record or
replay at once, each with its own `onMiss`, `strict` and masks. A request is handled
under its run's session if the run has one, and under the default session otherwise, so
everything that names no run behaves as it always did. A cassette being recorded is
taken: nobody else may record, replay or delete it; one being replayed may be replayed
by others. `DELETE /api/runs/<run>` stops its session and forgets its requests.

### The API

| | |
|---|---|
| `GET/PUT /api/settings` · `POST /api/settings/credentials` | capture, identity and credential mode |
| `GET/POST /api/runs` · `GET/DELETE /api/runs/<run>` | runs, and the base URLs for one |
| `GET/DELETE /api/captures` · `…/<n>` · `…/export` | what was sent and what answered |
| `GET/POST /api/rules` · `PUT/DELETE …/<name>` · `POST …/test`, `…/validate`, `…/order`, `…/reload` | rules |
| `GET/POST /api/faults` · `PUT/DELETE …/<id>` · `POST …/pause`, `…/resume`, `…/reset` | faults |
| `POST /api/assertions/evaluate` · `GET …/catalog` · `/api/assertion-sets` | assertions |
| `/api/sessions` | recording and replay, now per run |

Status codes say what went wrong — 400 it asked wrongly (with every `problems` item at
once, not the first), 404 there is no such thing, 409 the state forbids it, 403 the
request came from another website's page.

### What this is not

LLMSim sees what crosses the **model boundary** of each agent. It does not see tool-side
effects (files written, calls made), channels between agents that never pass through a
model, or an agent that cannot be pointed at a base URL. A claim about those needs
something that does see them. And a conformance result from a scripted model says what
an agent does when the model does *that*; it does not say how good the agent is with a
real one.

```
python3 tests/test_behaviors.py        # 44 cases: identity, captures, rules, faults, assertions, runs, the guide
python3 tests/test_chatgpt_plan.py     # 38 cases: the ChatGPT route, recording, replay
```

## Testing a system of agents

Everything above puts LLMSim on one side of an agent: the model. The **test kit** puts it on the rest — the tools
the agent calls (a mock MCP server, a mock REST backend with state), the agents it hands work to (an A2A stub or
a proxy), and the person driving it — and judges the whole run: the answer, the path, the records left behind,
what passed between agents, what it cost. Scenarios, datasets with versions, repetition, experiments compared
with a baseline, a CI gate, judges calibrated against people. Standard library only; one **Agent tests** page in the
console and the same API for a script. How it works, and what was not verified, is in
[`agent-testing.md`](agent-testing.md).

## Testing the unhappy paths

The rules shipped in `responses.json` include ones no live API key will give
you on demand:

| Say this | And you get |
|---|---|
| `llmsim:429` | a rate-limit error in the vendor's envelope |
| `llmsim:529` | an overloaded error |
| `llmsim:slow` | an eight-second first token |

These are usually the least-tested paths in any integration, because
provoking them for real means waiting for a bad day.

## Container

```
docker compose up --build
```

Serves on the same port, with `responses.json` bind-mounted so edits take
effect on restart without a rebuild.

## Fidelity

How closely the vendors are imitated, set in the console or in
`llmsim.config.json`. Each is separate because each carries different risk to
a setup that currently works.

| | Default | |
|---|---|---|
| `validate` | on | Refuses a malformed request the way the vendor does — missing `max_tokens`, empty `messages`, an invalid role. A simulator that answers 200 to a bad request is worse than none: the client passes here and fails in production. |
| `limits` | on | Enforces each model's context window and maximum output. |
| `headers` | on | `request-id`, the rate-limit trio, and **`retry-after` on a 429** — without which a 429 is a dead end and every backoff is a guess. |
| `unknownModel` | **off** | 404s a model that is not in the catalogue. Off deliberately: a gateway may route an upstream name this list has never heard of — Maniple's own sends `claude-sonnet-4-5` — so turning it on without checking breaks a working deployment. |

Responses carry what a real one carries: Anthropic's `cache_creation_input_tokens`
and `cache_read_input_tokens`, OpenAI's `system_fingerprint`, `service_tier`,
`logprobs`, `refusal` and the `*_tokens_details` breakdowns, and the Responses
envelope's `reasoning`, `previous_response_id`, `tool_choice` and the rest —
absent as `null` rather than missing, so a client that tests for the key can
tell the difference.

## Limits, stated plainly

- **Token counts are synthetic** when the answer is scripted — roughly four
  characters per token. Cost arithmetic can be exercised, but the numbers are
  not the vendors'. In **relay** mode the upstream's own counts are carried
  through instead and shown in the log, because those really were billed.
- **Streaming in relay mode is real.** The upstream is asked for a stream and
  its deltas are forwarded as they land, re-framed for whatever format the
  client asked in — so the pauses are the model's. Scripted answers still
  stream from a finished turn, which is the right shape and a made-up rhythm.
- **It has no idea what it is saying.** Answers are scripted. It proves a
  pipeline carries a response of a given shape; it proves nothing about
  whether a model would produce that response.
- **Scripts go stale on purpose.** Change a prompt or a harness and a script
  written for the old one no longer fits. That is information, but it means
  scripts need maintaining alongside the thing they test.
- **A recording is only as general as its matching.** Scripts are written by
  hand; real sessions can be captured and replayed (see *Recording a session*
  above), which gives real tool sequences instead of imagined ones. A replay
  answers a request by its ask and how far into the conversation it is, so it
  follows a run that makes the same asks in the same order. It does not
  generalise: change the prompt and there is, correctly, nothing recorded for it.

## Pointing LiteLLM at it

Verified end to end against LiteLLM 1.103.0 — `examples/litellm-config.yaml`
is the config that was actually run.

Only `api_base` changes per model, and **each provider appends a different
suffix to it**, which is the one detail worth getting right:

| LiteLLM provider | Calls | So `api_base` is |
|---|---|---|
| `anthropic/*` | `{api_base}/v1/messages` | `http://llmsim:8099/anthropic` |
| `openai/*` | `{api_base}/chat/completions` | `http://llmsim:8099/openai/v1` |
| `deepseek/*` | `{api_base}/chat/completions` | `http://llmsim:8099/deepseek/v1` |

Anthropic wants the bare prefix; the OpenAI-shaped providers want `/v1` on
the end. Get it wrong and you get a 404 that reads like a broken key.

Locally, a LiteLLM container reaches a host-run LLMSim at
`host.docker.internal:8099` (with `--add-host host.docker.internal:host-gateway`
on Linux).

What that chain was confirmed to do:

- `/v1/chat/completions` against an `anthropic/` model — LiteLLM translated
  Anthropic to OpenAI and **the tool call survived**: `finish_reason:
  "tool_calls"`, arguments intact.
- `/v1/messages` — LiteLLM's Anthropic-native endpoint returned the
  `tool_use` block untouched, `stop_reason: "tool_use"`. This is the path a
  coding agent pointed at the gateway actually takes.
- Streaming through the gateway, terminating in `[DONE]`.
- All three providers, each on its own key.

### On a deployment where models live in the database

A LiteLLM started with `STORE_MODEL_IN_DB` ignores `model_list` in a config
file — models are registered through its API instead:

```
POST {litellm}/model/new
Authorization: Bearer {master key}

{
  "model_name": "sim-claude",
  "litellm_params": {
    "model": "anthropic/claude-sonnet-5",
    "api_base": "http://llmsim.default.svc:8099/anthropic",
    "api_key": "sk-ant-llmsim-000000000000000000000000"
  }
}
```

Note the service address: in a cluster, `localhost` is the gateway's own pod.
LLMSim has to be reachable as a service.

## Driving a real agent: what it took

Proven end to end on a k3s deployment — OpenCode in a workspace pod, a
virtual key minted by LiteLLM scoped to one model, LLMSim answering as
DeepSeek. The agent wrote a real file. No vendor key anywhere in the chain.

Three things had to be right, and each failed silently first:

1. **Rule specificity.** `contains: "hello"` sat first in the script and
   swallowed the prompt *"Create a file called HELLO.md"* — the filename
   matched. The agent got prose where it needed a tool call, did nothing, and
   the session finished `completed` with no file and no error. Specific rules
   first, and prefer an anchored regex to a bare substring.
2. **Tool names differ by agent.** Claude Code declares `Write`; OpenCode
   declares `write`. A script naming the wrong one hands the agent a tool it
   has never heard of. Names are now resolved case-insensitively against what
   the client declared, and a genuine mismatch logs a warning rather than
   failing quietly.
3. **Tool arguments differ too, and cannot be resolved.** Claude Code's
   `Write` takes `file_path`; OpenCode's `write` takes `filePath`. So a rule
   that calls a tool is client-specific in its arguments — which is what
   `when.provider` is for. The server logs the real parameter names of the
   tool it is about to call, so writing the rule means reading one log line
   instead of guessing.

The common thread: **every one of these failures produced a clean, successful,
empty session.** A simulator that can do that is worse than no simulator, so
the logging above is not a convenience — it is the feature that makes the
thing trustworthy.

It is also the strongest argument for recording real sessions rather than
hand-writing them. A recording carries the right tool names, the right
argument names and the right sequence by construction.
