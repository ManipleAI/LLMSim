#!/bin/sh
# Everything LLMSim claims, exercised against a running instance.
#
#   python3 server.py &
#   sh examples/try-it.sh
#
# Each block prints what a client would actually receive.
set -u
BASE=${LLMSIM_BASE:-http://localhost:8099}
AK=${LLMSIM_ANTHROPIC_KEY:-sk-ant-llmsim-000000000000000000000000}
OK=${LLMSIM_OPENAI_KEY:-sk-llmsim-000000000000000000000000}
DK=${LLMSIM_DEEPSEEK_KEY:-sk-llmsim-deepseek-0000000000000000}
T="--max-time 30"

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }
anth() { curl -s $T -X POST "$BASE/anthropic/v1/messages" -H "x-api-key: $AK" \
         -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' -d "$1"; }
oai()  { curl -s $T -X POST "$BASE/openai/v1/chat/completions" -H "authorization: Bearer $OK" \
         -H 'content-type: application/json' -d "$1"; }
deep() { curl -s $T -X POST "$BASE/deepseek/v1/chat/completions" -H "authorization: Bearer $DK" \
         -H 'content-type: application/json' -d "$1"; }

say "0. health, and the rules currently loaded"
curl -s $T "$BASE/healthz"; echo

say "1. a wrong key is refused, in each vendor's own error envelope"
curl -s $T -X POST "$BASE/anthropic/v1/messages" -H 'x-api-key: wrong' \
  -H 'content-type: application/json' -d '{"model":"claude-sonnet-5","max_tokens":8,"messages":[]}'; echo
curl -s $T -X POST "$BASE/openai/v1/chat/completions" -H 'authorization: Bearer wrong' \
  -H 'content-type: application/json' -d '{"model":"gpt-5","messages":[]}'; echo

say "2. model discovery — Claude Code queries this on startup"
curl -s $T "$BASE/anthropic/v1/models" -H "x-api-key: $AK"; echo

say "3. Anthropic, plain text"
anth '{"model":"claude-sonnet-5","max_tokens":256,"messages":[{"role":"user","content":"hello there"}]}'; echo

say "4. Anthropic, a real tool_use block (stop_reason: tool_use)"
anth '{"model":"claude-sonnet-5","max_tokens":256,"messages":[{"role":"user","content":"please create a file"}]}'; echo

say "5. the same conversation, one turn later — the script advances"
anth '{"model":"claude-sonnet-5","max_tokens":256,"messages":[
  {"role":"user","content":"please create a file"},
  {"role":"assistant","content":[{"type":"tool_use","id":"toolu_1","name":"Write","input":{}}]},
  {"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1","content":"File created at HELLO.md"}]}]}'; echo

say "6. Anthropic streaming — the event order clients depend on"
curl -s -N $T -X POST "$BASE/anthropic/v1/messages" -H "x-api-key: $AK" \
  -H 'content-type: application/json' \
  -d '{"model":"claude-sonnet-5","max_tokens":256,"stream":true,"messages":[{"role":"user","content":"make one file"}]}' \
  | grep '^event:' | uniq -c

say "7. OpenAI, tool_calls + finish_reason"
oai '{"model":"gpt-5","messages":[{"role":"user","content":"please write a file"}]}'; echo

say "8. DeepSeek, same shape, its own key"
deep '{"model":"deepseek-chat","messages":[{"role":"user","content":"hello"}]}'; echo

say "9. OpenAI streaming, ending in [DONE]"
curl -s -N $T -X POST "$BASE/openai/v1/chat/completions" -H "authorization: Bearer $OK" \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-5","messages":[{"role":"user","content":"hello"}],"stream":true}' | tail -3

say "10. a scripted 429 — the failure path no live key will give you on demand"
anth '{"model":"claude-sonnet-5","max_tokens":64,"messages":[{"role":"user","content":"llmsim:429 please"}]}'; echo

say "11. the multi-turn capability scenario, turn by turn"
for n in 0 1 2; do
  case $n in
    0) MSGS='[{"role":"user","content":"report the SAP invoices that are not three-way matched"}]' ;;
    1) MSGS='[{"role":"user","content":"report the SAP invoices that are not three-way matched"},
             {"role":"assistant","content":[{"type":"tool_use","id":"t1","name":"Bash","input":{}}]},
             {"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"[]"}]}]' ;;
    2) MSGS='[{"role":"user","content":"report the SAP invoices that are not three-way matched"},
             {"role":"assistant","content":[{"type":"tool_use","id":"t1","name":"Bash","input":{}}]},
             {"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"[]"}]},
             {"role":"assistant","content":[{"type":"tool_use","id":"t2","name":"Bash","input":{}}]},
             {"role":"user","content":[{"type":"tool_result","tool_use_id":"t2","content":"filed"}]}]' ;;
  esac
  printf '  turn %s: ' "$n"
  anth "{\"model\":\"claude-sonnet-5\",\"max_tokens\":512,\"messages\":$MSGS}" \
    | python3 -c 'import json,sys
d=json.load(sys.stdin)
kinds=[b["type"] for b in d["content"]]
print(d["stop_reason"], kinds)'
done
echo
