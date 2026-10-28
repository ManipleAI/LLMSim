# LLMSim

LLMSim plays the model an agent talks to. It answers in Anthropic's, OpenAI's and
DeepSeek's own wire formats, from one process with no dependencies, so anything
that takes a base URL can be pointed at it: a gateway, a coding agent, a flow
editor.

You decide what the model says, break it on purpose, keep every request, and
check what the agent did. Nothing leaves your machine and nothing is spent.

```
Your agent  ──►  LLMSim plays the model  ···  The agent's tools
(harness,        (scripts the answers,        (files, APIs,
 tools,           injects faults,              other agents)
 guardrails)      keeps every request)
```

## Quick start

Python 3.9 or newer. There is nothing to install.

```bash
python3 server.py            # serves on port 8099
```

Point your client at one of these, with the key beside it. The keys are fake and
published.

| Vendor | Base URL | Key |
|---|---|---|
| Anthropic | `http://localhost:8099/anthropic` | `sk-ant-llmsim-000000000000000000000000` |
| OpenAI | `http://localhost:8099/openai/v1` | `sk-llmsim-000000000000000000000000` |
| DeepSeek | `http://localhost:8099/deepseek/v1` | `sk-llmsim-deepseek-0000000000000000` |

Send it something:

```bash
curl localhost:8099/openai/v1/chat/completions \
  -H "Authorization: Bearer sk-llmsim-000000000000000000000000" \
  -d '{"model": "gpt-5", "messages": [{"role": "user", "content": "hello"}]}'
```

Then open the console at `http://localhost:8099/ui`. The request is under
**Traffic**, with the rule that answered it.

## What you can do with it

**Script the model.** A rule says what the model answers, turn by turn, including
the tools it calls. This is what makes an agent loop testable.

```bash
curl -X POST localhost:8099/api/rules -d '{"rule": {
  "name": "write-a-file", "when": {"contains": "make a readme"},
  "turns": [{"text": "Writing it.", "tool_calls": [{"name": "Write", "input": {"file_path": "README.md", "content": "# Hi"}}]},
            {"text": "README.md is written."}]}}'
```

**Break it on purpose.** A rate limit, a stream that dies half way, a tool call
that is not JSON. Seeded, so a failing run fails the same way again.

```bash
curl -X POST localhost:8099/api/faults -d '{
  "name": "rate-limited", "fault": {"type": "status", "status": 429, "retry_after": 2}, "limit": 1}'
```

**Connect the agents you test, and run tests on them.** Add an agent by where it
lives — Salesforce Agentforce, SAP Joule, ServiceNow, Azure AI Foundry, or your
own — check that it answers, and run scenarios on it. A test run keeps which
passed and which did not.

**See what the agent sent, and check it.** Every request is kept in full. An
assertion is a claim about them, and an agent that never ran does not pass.

```bash
curl -X POST localhost:8099/api/assertions/evaluate -d '{"assertions": [
  {"id": "no-cards", "name": "No card number reaches the model",
   "none": {"field": "allText", "op": "regex", "value": "\\b\\d{13,16}\\b"}}]}'
```

**Record a real run once, replay it for free.** Route to a real model (Anthropic's
API, a ChatGPT plan or any upstream), record the session, and every later run gets
the same answers.

```bash
curl -X POST localhost:8099/api/sessions/record -d '{"name": "login-flow"}'
# …run the agent…
curl -X POST localhost:8099/api/sessions/stop
curl -X POST localhost:8099/api/sessions/replay -d '{"name": "login-flow"}'
```

**Test an agent end to end.** The test kit drives the agent, stands in for its
tools (MCP, REST) and the agents it hands work to (A2A), and gives a verdict a
build can act on.

```bash
bin/llmsim run scenario.json --junit out.xml       # exit 0 pass, 1 fail, 2 could not run
```

## The console

One page at `/ui`, in three spaces: **Simulate a model** (playground, answer rules,
models, faults, record and replay, traffic, model checks), **Test agents** (a dashboard
of what passes and fails, agents, scenarios, runs, stand-ins and judging) and
**Settings** (where answers come from, model connections, clients, capture and storage). Everything on it is the same HTTP API a
script uses.

## Documentation

| | |
|---|---|
| [Documentation](docs/guide.md) | How to use every part, and the whole API. LLMSim serves the same pages at `http://localhost:8099/guide`. |
| [Testing an agent](docs/agent-testing.md) | How the test kit works, and what was not verified |
| [Design notes](docs/design.md) | Why it is built this way, and the detail behind each part |

## Know before you share it

LLMSim has no login. Anyone who can reach the port can use all of it, and once a
ChatGPT plan is signed in or an Anthropic key is saved, can spend it. Keep it on your own machine or a
network you trust.

## Where it keeps things

A ChatGPT sign-in, saved keys, routing, your rules and runs are kept in one
directory: beside `server.py`, or wherever `LLMSIM_DATA` says. The Docker image,
`compose.yml` and `deploy/install.sh` put it on a volume, so a restart or a new
image does not sign anybody out.

## Tests

```bash
for t in tests/test_*.py; do python3 "$t" || break; done
```

Each file starts its own LLMSim on a free port with a throwaway directory. The
documentation page is built from `docs/guide.md` with `bin/build-docs`, and a test
fails if the two are out of step.
