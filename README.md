<div align="center">

# LLMSim

**A stand-in for the model your agent talks to, and a test kit for the agent itself.**

It speaks Anthropic's, OpenAI's and DeepSeek's own APIs, needs no vendor key,
and keeps every request. It runs as one Python process with no dependencies.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776AB.svg)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)
![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)

[Quick start](#quick-start) · [How to use it](#how-to-use-it) · [Test an agent](#test-an-agent) · [Ways to run it](#ways-to-run-it) · [Documentation](docs/guide.md)

<img src="docs/images/canvas.png" alt="A scenario on the canvas: a coordinator agent handing work to two stub agents, with routing, handoff, duplication and termination checks" width="900">

</div>

---

## What it is

An agent is a loop: a model decides, tools act, and the model reads what they returned.
Testing it against a real model is slow, costs money, and gives a different answer every
time. Testing it against a mock you wrote yourself only shows that the mock works.

LLMSim sits where the model would be. Anything that takes a base URL can point at it: an
SDK, a gateway such as LiteLLM, a coding agent, a LangGraph app or a flow editor. You decide
what the model says, including the tools it calls, and LLMSim answers in that vendor's exact
wire format, streaming included. Then you check what the agent actually did.

It comes as two products in one server:

| | **Simulate a model** | **Test agents** |
|---|---|---|
| **What it is** | A fake Anthropic, OpenAI or DeepSeek endpoint | A test runner for agents, with stand-ins for everything around them |
| **What you do** | Script answers and tool calls, inject faults, record a real session and replay it | Write scenarios, run them on your agents, and gate a build on the result |
| **Who it's for** | Anyone building on an LLM API who wants fast, free, repeatable runs | Teams shipping agents who need to know when one gets worse |

Nothing leaves your machine unless you route it to a real model on purpose.

## Highlights

- **Native wire formats.** Anthropic Messages, OpenAI Chat Completions and Responses, and
  DeepSeek, with streaming, tool use, usage and each vendor's own error shapes.
- **A model you can script.** Rules match on the prompt, the tools declared, the system
  prompt, or what the last tool printed, and answer turn by turn, tool calls included.
- **Faults on purpose.** Rate limits, 5xx errors, streams that stall or die half way,
  tool calls that are not JSON, unknown tools, huge bodies. Each one is seeded, so a failing
  run fails the same way again.
- **Record once, replay for free.** Send a run to a real model (Anthropic's API, any
  OpenAI-compatible upstream), record it, and replay it offline.
- **Every request kept.** See what was sent, which rule answered it, and which credential
  arrived, then make assertions about it, such as "no card number reaches the model".
- **Stand-ins for the agent's world.** Mock MCP tool servers, mock REST backends (including
  ones imported from OpenAPI), stub A2A agents, and record/replay proxies in front of real
  ones.
- **Checks that understand agents.** Tool paths, state changes, handoffs between agents,
  routing, duplicated work, termination, leaks, authority, budgets, memory, LLM judges and
  your own code evaluators.
- **Built for CI.** A CLI with exit codes and JUnit output, datasets and experiments gated
  against a baseline, a pytest plugin, and a GitHub Action.
- **Open on your machine, signed in when shared.** Set a password, and the console asks
  for a sign-in (then a cookie); add an API key for scripts and CI.
- **One console for all of it.** It uses the same HTTP API a script does, and adds a visual
  canvas for building tests.

## Quick start

You need Python 3.9 or newer. There is nothing to install.

```bash
git clone https://github.com/ManipleAI/LLMSim.git
cd LLMSim
python3 server.py              # serves on http://localhost:8099
```

Point your client at the vendor you use, with the key beside it. The keys are fake and
published, so they're safe to commit to a test config.

| Vendor | Base URL | Key |
|---|---|---|
| Anthropic | `http://localhost:8099/anthropic` | `sk-ant-llmsim-000000000000000000000000` |
| OpenAI | `http://localhost:8099/openai/v1` | `sk-llmsim-000000000000000000000000` |
| DeepSeek | `http://localhost:8099/deepseek/v1` | `sk-llmsim-deepseek-0000000000000000` |

**Which models.** The three routes are three request formats, not three vendors. Every model
goes through the one whose format its clients speak:

- **`/anthropic`:** Claude.
- **`/openai`:** GPT, and every model reached through an OpenAI-compatible endpoint: Gemini,
  Grok, Qwen, Kimi, Llama, Mistral, GLM, Nemotron, MiniMax and the open-weight models agent
  harnesses run.
- **`/deepseek`:** DeepSeek.

Each route's `/v1/models` lists the model ids coding agents and gateways commonly ask for,
about seventy in all. Any id missing from it still gets an answer, so a client never has to
wait for this list to know its model.

Send it something:

```bash
curl localhost:8099/openai/v1/chat/completions \
  -H "Authorization: Bearer sk-llmsim-000000000000000000000000" \
  -d '{"model": "gpt-5", "messages": [{"role": "user", "content": "hello"}]}'
```

Or use the vendor's own SDK unchanged:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8099/openai/v1", api_key="sk-llmsim-000000000000000000000000")
reply = client.chat.completions.create(model="gpt-5", messages=[{"role": "user", "content": "hello"}])
print(reply.choices[0].message.content)
```

```python
import anthropic

client = anthropic.Anthropic(base_url="http://localhost:8099/anthropic", api_key="sk-ant-llmsim-000000000000000000000000")
reply = client.messages.create(model="claude-sonnet-5", max_tokens=200, messages=[{"role": "user", "content": "hello"}])
print(reply.content[0].text)
```

Then open the console at **http://localhost:8099/ui**. Your request is under **Traffic**,
with the rule that answered it. The full documentation is served at
**http://localhost:8099/guide**.

<img src="docs/images/home.png" alt="The LLMSim console: Simulate a model, Test agents, Settings" width="900">

## How to use it

### 1. Script the model

A rule says what the model answers, and when. A rule with several turns answers each step
of an agent loop in order, so a tool-using agent can be tested end to end.

```bash
curl -X POST localhost:8099/api/rules -d '{"rule": {
  "name": "write-a-file", "when": {"contains": "make a readme"},
  "turns": [{"text": "Writing it.", "tool_calls": [{"name": "Write", "input": {"file_path": "README.md", "content": "# Hi"}}]},
            {"text": "README.md is written."}]}}'
```

The first request that says "make a readme" gets a `Write` tool call. Once the agent sends
back the tool's result, the next request gets the closing sentence. The **Try a request**
panel shows which rule would answer a given request, and why each of the others would not.

<img src="docs/images/rules.png" alt="Answer rules, with a panel that explains which rule would answer" width="900">

### 2. Break it on purpose

```bash
curl -X POST localhost:8099/api/faults -d '{
  "name": "rate-limited", "fault": {"type": "status", "status": 429, "retry_after": 2}, "limit": 1}'
```

A fault can return a status code, add a delay, drop the connection, cut a stream short,
stall it, send garbage, return a tool call whose arguments are not JSON, or call a tool that
doesn't exist. Gates such as `after`, `every`, `limit` and `probability` (with a `seed`)
decide when it fires.

<img src="docs/images/faults.png" alt="Faults: every kind of failure and its parameters" width="900">

### 3. See what was sent, and check it

Every request is kept in full. An assertion is a claim about those requests. An agent that
never ran does not pass.

```bash
curl -X POST localhost:8099/api/assertions/evaluate -d '{"assertions": [
  {"id": "no-cards", "name": "No card number reaches the model",
   "none": {"field": "allText", "op": "regex", "value": "\\b\\d{13,16}\\b"}}]}'
```

### 4. Record a real run once, replay it for free

Route to a real model, record the session, and every later run gets the same answers, with
no key and no cost.

```bash
curl -X POST localhost:8099/api/sessions/record -d '{"name": "login-flow"}'
# …run the agent…
curl -X POST localhost:8099/api/sessions/stop
curl -X POST localhost:8099/api/sessions/replay -d '{"name": "login-flow"}'
```

You choose where answers come from under **Settings**, for all models or one model at a
time: your rules, an OpenAI- or Anthropic-compatible upstream, or Anthropic's API.

## Test an agent

The test kit drives your agent and plays its model. It also stands in for the agent's tools
and for the agents it hands work to, and gives a verdict a build can act on.

### Connect the agents you test

Add an agent by how it is reached: a plain HTTP endpoint, an OpenAI-compatible chat
endpoint, the Responses API, A2A, a local program, or a platform such as Salesforce
Agentforce, SAP Joule, ServiceNow or Azure AI Foundry. LLMSim checks that the agent answers
before you run anything on it.

<img src="docs/images/agents.png" alt="Agents under test, each with how it is reached and whether it answered" width="900">

### A scenario is a test

A scenario names the agent, what the user says, what the model says, and what must hold
afterwards.

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

```bash
curl -X PUT  localhost:8099/api/scenarios/refund-over-limit -d @scenario.json
curl -X POST localhost:8099/api/scenarios/refund-over-limit/run -d '{}'
```

LLMSim validates a scenario when you save it and lists every problem at once. Anything the
scenario installs (rules, mock servers, stub agents, faults) applies to that run only.

### Build it on a canvas

You can also build the same scenario visually: who speaks, the agent under test, what it
reaches (the model, stub agents, proxies, mock tool servers, mock backends, seeded records,
faults), and what must hold at the end. The **Form** and **JSON** views show the same
scenario. The picture at the top of this page is one.

### See what passes, what fails, and why

The dashboard shows every scenario's last result and the first check that did not hold. A
test run groups the scenarios run on one agent. Open a result to see each step it took in
order: model calls, tool calls, and messages between agents.

<img src="docs/images/dashboard.png" alt="The dashboard: scenarios, passing, failing, and why" width="900">

<img src="docs/images/testrun.png" alt="A test run: 18 of 19 scenarios passed on one agent" width="900">

### Run it in CI

```bash
bin/llmsim run scenario.json --junit out.xml          # exit 0 pass, 1 fail, 2 could not run
bin/llmsim experiment run --dataset refunds --gate    # a dataset, gated against its baseline
```

As a GitHub Action, it writes the job summary, comments on the pull request with the cases
that regressed, and fails the build:

```yaml
- uses: ManipleAI/LLMSim@main
  with:
    server-url: http://localhost:8099
    api-key: ${{ secrets.LLMSIM_API_KEY }}   # when the server requires a sign-in
    dataset: refunds
    tier: simulated          # a scripted or recorded model: free and exact
```

There is also a pytest plugin (`testkit.pytest_plugin`) that gives tests an `llmsim`
fixture and collects `*.scenario.json` files.

### Real agents to try it on

[`test-agents/langgraph`](test-agents/langgraph/README.md) is a team of real agents built on
LangChain and LangGraph:
- an assistant;
- an operations agent with MCP and REST tools;
- a coordinator that hands work to other agents over A2A;
- a researcher served over A2A.

Two suites run every capability of the test kit on them, using their own LLMSim in Docker.

## Ways to run it

**Python**, with nothing to install:

```bash
python3 server.py                     # port 8099
LLMSIM_PORT=9000 python3 server.py    # another port
```

**Docker:**

```bash
docker build -t llmsim .
docker run -p 8099:8099 -v llmsim-data:/data llmsim
```

**Docker Compose**, with the data on a named volume:

```bash
docker compose up -d
```

**Kubernetes** (k3s or k3d): `deploy/install.sh` builds the image and applies
`deploy/k8s.yaml` with a persistent volume.

```bash
NAMESPACE=llmsim ./deploy/install.sh
```

### Where it keeps things

Your rules, runs, saved keys and routing are kept in one directory: beside
`server.py`, or wherever `LLMSIM_DATA` points. The Docker image, `compose.yml` and
`deploy/install.sh` put it on a volume, so a restart or a new image keeps it.

### Configuration

You can set everything in the console, and most of it with environment variables, for
example:

| Variable | What it sets |
|---|---|
| `LLMSIM_PORT` | The port (default `8099`) |
| `LLMSIM_ADMIN_USER`, `LLMSIM_ADMIN_PASSWORD` | The console's sign-in |
| `LLMSIM_API_KEY` | The key scripts and CI send as `X-LLMSim-Key` |
| `LLMSIM_DATA` | Where LLMSim keeps what it writes |
| `LLMSIM_SCRIPT` | The rules file to answer from (default `responses.json`) |
| `LLMSIM_UPSTREAM`, `LLMSIM_UPSTREAM_KEY`, `LLMSIM_UPSTREAM_FORMAT` | A real model to send requests to |
| `LLMSIM_RECORD`, `LLMSIM_REPLAY` | Record a session, or replay one |

The [documentation](docs/guide.md) lists all of them.

## Security

**LLMSim starts open**, as it always has, which suits your own machine: anyone who can
reach the port can use the console and the whole API. Anywhere else, set a password, in
either of two ways:

- **In the console**, under **Settings › Sign-in and API key**. Set a password, then make
  an API key (it is shown once). Only hashes of them are kept.
- **Where LLMSim is started**, for the first sign-in. A password or key set in the console
  later replaces it:

```bash
LLMSIM_ADMIN_PASSWORD='choose one' LLMSIM_API_KEY='llmsim-…' python3 server.py
```

- **The console** shows a sign-in in front of the page you opened. Sign in once and a
  cookie keeps you signed in (`HttpOnly`, `SameSite=Strict`, 7 days). **Sign out** is at
  the bottom of the menu.
- **The API key** is a second way in, for what cannot sign in. It is used only once a
  password is set: a key alone changes nothing.
- **Scripts and CI** send the key as the `X-LLMSim-Key` header, or as a bearer token on
  `/api/…`. `bin/llmsim`, the pytest plugin and the GitHub Action (`api-key`) read it from
  `LLMSIM_API_KEY`.
- **What an agent calls stays open**, because an agent under test cannot sign in: the
  vendors' routes, which take their own keys, and the test kit's stand-ins. If LLMSim
  answers with a real model (a saved Anthropic key or an upstream), also set
  `LLMSIM_OPENAI_KEY`, `LLMSIM_ANTHROPIC_KEY` and `LLMSIM_DEEPSEEK_KEY` to keys of your own.

Even with it on, put LLMSim behind HTTPS before you expose it beyond a network you trust.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Documentation

| | |
|---|---|
| [Guide](docs/guide.md) | How to use every part, and the whole API. Also served at `/guide`. |
| [Testing an agent](docs/agent-testing.md) | How the test kit works, and what was not verified |
| [Design notes](docs/design.md) | Why it is built this way |
| [Examples](examples/) | `try-it.sh` exercises every endpoint; `litellm-config.yaml` points LiteLLM at LLMSim |

## Development

The server is `server.py`, the test kit is `testkit/`, and the console is a single file,
`ui.html`. The server reads `ui.html` on every request, so a console change needs no
restart.

```bash
for t in tests/test_*.py; do python3 "$t" || break; done
```

Each test file starts its own LLMSim on a free port, with a throwaway data directory. The
documentation page is built from `docs/guide.md` with `bin/build-docs`, and a test fails if
the two are out of step.

`tests/test_access_page.py` also clicks through **Settings › Sign-in and API key** in a real
browser. It needs Node and Playwright for Node, and skips without them:

```bash
NODE_PATH=/path/to/node_modules python3 tests/test_access_page.py
```

Contributions are welcome. Open an issue to discuss a change, or send a pull request with a
test that shows it working.

## License

[MIT](LICENSE)
