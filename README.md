# LLMSim

The model vendors' own APIs, simulated. Anthropic, OpenAI and DeepSeek, in
their native wire formats, from one process with no dependencies.

Point anything that takes a base URL at it — a gateway, a coding agent, a
flow editor — and it behaves as though it reached the vendor.

```
python3 server.py
sh examples/try-it.sh
```

That's the whole setup. Python 3.9+, standard library only, nothing to
install.

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

## Two modes: scripted, or one real model behind every name

Scripted is the default and everything above describes it: `responses.json`
decides, deterministically, offline, free.

**Relay** answers every request with one real model instead. Whatever model
the client asked for — `claude-opus-5`, `gpt-5.4`, `gemini-3.8-flash` — the
answer comes from `LLMSIM_UPSTREAM_MODEL`, and is returned in the format the
client asked in.

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

## Limits, stated plainly

- **Token counts are synthetic.** Roughly four characters per token. Cost
  and budget arithmetic can be exercised, but the numbers are not the
  vendors' numbers. Real accounting is the one thing a simulator cannot
  honestly provide, and rounding that off would make cost tests lie rather
  than fail.
- **It has no idea what it is saying.** Answers are scripted. It proves a
  pipeline carries a response of a given shape; it proves nothing about
  whether a model would produce that response.
- **Scripts go stale on purpose.** Change a prompt or a harness and a script
  written for the old one no longer fits. That is information, but it means
  scripts need maintaining alongside the thing they test.
- **No recording yet.** Scripts are written by hand. Capturing real sessions
  and replaying them — the cassette approach, as in
  [cs-vcr](https://github.com/codesweep-ai/vcr) — would give real tool
  sequences instead of imagined ones. The `turns` format was designed to
  accept them: a recorder can emit this shape directly.

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
