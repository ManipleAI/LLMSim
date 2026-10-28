#!/usr/bin/env bash
# Start the agent and its chat page.
#   ./run.sh                 the model on Azure AI Foundry (.env)
#   ./run.sh .env.llmsim     the model played by LLMSim, to test the agent
set -euo pipefail
cd "$(dirname "$0")"
export AGENT_ENV_FILE="${1:-.env}"
[ -f "$AGENT_ENV_FILE" ] || { echo "no $AGENT_ENV_FILE: copy .env.example and fill it in" >&2; exit 1; }
PORT="${AGENT_PORT:-$(sed -n 's/^AGENT_PORT=//p' "$AGENT_ENV_FILE" | tail -1)}"
PORT="${PORT:-8770}"
echo "chat: http://localhost:$PORT   (model settings from $AGENT_ENV_FILE)"
exec .venv/bin/uvicorn agent.server:app --host 127.0.0.1 --port "$PORT"
