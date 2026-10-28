#!/usr/bin/env bash
# This agent's own LLMSim, in a container, apart from the one Maniple's tests use on :8099.
#
#   llmsim/start-llmsim.sh                      build the LLMSim this folder is part of, and (re)start it
#   LLMSIM_DIR=/path/to/LLMSim llmsim/start-llmsim.sh   build another checkout instead
#
# Console http://127.0.0.1:18110/ui. Its state (scenarios, saved agents, results) is on the
# volume llmsim-agents-data, so a restart or a rebuild keeps it. Its upstream is the agent's own
# Azure model from .env, so the flows that want a real model get gpt-5-nano; the key goes to the
# container through a temporary owner-only file and is never printed.
#
# llmsim/kit is mounted read-only at /kit: the code checks (evaluators/) and the program the process
# driver runs (bin/ask-agent.py). The process driver is on because this LLMSim listens on 127.0.0.1 only.
set -euo pipefail
cd "$(dirname "$0")/.."
LLMSIM_DIR="${LLMSIM_DIR:-$(cd ../.. && pwd)}"          # test-agents/langgraph sits in the LLMSim repository
PORT="${LLMSIM_AGENTS_PORT:-18110}"
docker build -q -t llmsim:test-agents "$LLMSIM_DIR" >/dev/null
envfile=$(mktemp); chmod 600 "$envfile"; trap 'rm -f "$envfile"' EXIT
if [ -f .env ]; then                                            # a real model for it to relay to, if there is one
  set -a; . ./.env; set +a
  upstream="${MODEL_BASE_URL%/}"; upstream="${upstream%/v1}"     # LLMSim's relay adds /v1/chat/completions itself
  printf 'LLMSIM_UPSTREAM=%s\nLLMSIM_UPSTREAM_KEY=%s\nLLMSIM_UPSTREAM_MODEL=%s\nLLMSIM_UPSTREAM_FORMAT=openai\n' \
    "$upstream" "$MODEL_API_KEY" "${MODEL_NAME:-gpt-5-nano}" > "$envfile"
fi
docker rm -f llmsim-agents >/dev/null 2>&1 || true
# The ChatGPT sign-in sends the browser back to 127.0.0.1:<port>/auth/callback on this Mac. 1455 is
# often taken (Codex, or an ssh tunnel to another LLMSim), so this one listens on 1457, the other
# port the sign-in allows, published from the container.
SIGNIN_PORT="${LLMSIM_AGENTS_SIGNIN_PORT:-1457}"
docker run -d --name llmsim-agents --restart unless-stopped -p "127.0.0.1:$PORT:8099" -p "127.0.0.1:$SIGNIN_PORT:$SIGNIN_PORT" \
  -e LLMSIM_CHATGPT_PORTS="$SIGNIN_PORT" -e LLMSIM_CHATGPT_BIND=0.0.0.0 -v llmsim-agents-data:/data \
  -v "$PWD/llmsim/kit:/kit:ro" -e LLMSIM_EVALUATORS=/kit/evaluators -e LLMSIM_ALLOW_PROCESS=1 \
  --env-file "$envfile" --add-host host.docker.internal:host-gateway llmsim:test-agents >/dev/null
for _ in $(seq 1 30); do curl -fsS -o /dev/null -m 2 "http://127.0.0.1:$PORT/healthz" && break; sleep 1; done
echo "LLMSim for this agent: http://127.0.0.1:$PORT/ui"
