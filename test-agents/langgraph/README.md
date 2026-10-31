# LangGraph test agents

Real agents, built on LangChain and LangGraph, that LLMSim is tested against. LLMSim's own
tests (`tests/`) drive a fake agent written for them; these are agents written the way people
write agents, so what they find is what a user of LLMSim would find.

Nothing here is part of LLMSim's image or its stdlib-only server. It has dependencies of its
own (`requirements.txt`), and runs beside an LLMSim.

## The agents

One server (`agent/server.py`) serves a team. Each member is the same LangGraph graph,
`START → model → (tools → model)* → END`, with other tools and other instructions (`agent/team.py`):

| Agent | Reached at | Its tools |
|---|---|---|
| assistant | `POST /api/chat`, and the chat page at `/` | files in its workspace, Python (in a macOS sandbox), web pages, the time |
| ops | `POST /agents/ops/api/chat` | whatever the MCP servers it is given list, loaded at run time, and a `call_<api>` tool per REST API |
| coordinator | `POST /agents/coordinator/api/chat` | `ask_agent(agent, task)`: other agents over A2A |
| researcher | `POST /agents/researcher/api/chat`, and as an A2A agent at `/a2a/researcher` (bearer token) | Python and the time |

The assistant is also served as an OpenAI-compatible chat endpoint (`/v1/chat/completions`) and
over the Responses API (`/v1/responses`), so every one of LLMSim's drivers has something real to
talk to. `/services/mcp` and `/services/rest/…` are two small real services, an MCP server and a
REST API, for LLMSim's proxies to record. With `AGENT_OTLP_URL` set, each turn is also reported as
OpenTelemetry spans.

What an agent reaches comes with each request (`mcp`, `api`, `peers` in the body), because under
test those are LLMSim's mocks and stubs, at addresses that carry the test run.

## Run it

```bash
cd test-agents/langgraph
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # once
llmsim/start-llmsim.sh            # an LLMSim of its own, in Docker, built from this checkout: http://127.0.0.1:18110/ui
cp .env.llmsim.example .env.llmsim && ./run.sh .env.llmsim          # the team, its model played by that LLMSim, on :8771
```

That LLMSim is apart from any other on purpose: these tests save agents, stubs, mocks and faults,
and hold the route while they run. Its state is on the Docker volume `llmsim-agents-data`, so a
rebuild keeps its scenarios and results. `llmsim/kit/` is mounted into it:
the code checks (`evaluators/`) and the program the process driver runs (`bin/ask-agent.py`).

For the tests that want a real model, give that LLMSim an upstream or an Anthropic key. The
launcher sets its upstream from `.env` when there is one (copy `.env.example`: an Azure AI
Foundry endpoint and key). `./run.sh` alone runs the assistant on that real model, on :8770, to
chat with.

## The tests

```bash
.venv/bin/python llmsim/capabilities.py      # every capability of the test kit, 58 scenarios and more
.venv/bin/python llmsim/flows.py             # 22 flows of the assistant on its own
```

`capabilities.py` saves what the scenarios use and leaves it in place, so every part has its page
in the console and every scenario its canvas:

- **Stand-ins.** Mock MCP servers (stateful orders, a help centre whose page carries an injection,
  a warehouse, a proxy to the real MCP server, recorded then replayed); mock REST backends (one
  from an OpenAPI document, a stateful CRM, a proxy to the real API); stub agents (an answer, a
  task that takes a few polls, a question back, one carrying an injection, one behind a fault);
  a proxy in front of the real researcher, recorded then replayed.
- **Faults.** Every tool fault type and every peer fault type, saved faults, model faults saved and
  per run.
- **Drivers.** http, a2a (with a bearer credential), openai, responses and process, and an agent
  seen only through OpenTelemetry.
- **Checks.** answer, path (model, agent and tools), state, budget, leak, handoff, routing,
  authority, termination, duplication, degradation, memory, run, code, judge and conversation.
- **More than one scenario.** Two agents at once with a kept order, interleavings, recordings,
  shared answer rules under a routing hold, a simulated user, an assertion set, a dataset with
  versions, imports and a case from a run, experiments with a baseline, a gate, a comparison and a
  pairwise judgement, reviews, variations, a calibrated judge, and a test run on each agent.

Two scenarios are controls, tagged `expected-fail`: an agent that obeys an injection, and one that
refunds because another agent said so. Their checks must fail. The run ends with how many
scenarios came out as expected, and writes `llmsim/capabilities-report.json`.

`flows.py` is the assistant alone: scripted answers, tools run for real, guardrails, failing
tools, model faults, a runaway loop, memory, an agent not told the run, a strict script, a script
with a real model writing the answer, a real model end to end, an injection and a simulated user.

## Ports

| Port | What |
|---|---|
| 18110 | This folder's LLMSim (`llmsim/start-llmsim.sh`) |
| 8770 | The assistant on its real model (`./run.sh`) |
| 8771 | The team, on LLMSim (`./run.sh .env.llmsim`) |
| 8772 | The assistant on its real model, reporting spans to LLMSim (`./run.sh .env.otel`, from `.env.otel.example`) |
