#!/usr/bin/env bash
# Build LLMSim, put it in k3s, and apply it — script and all.
#
#   NAMESPACE=maniple ./deploy/install.sh
set -euo pipefail
NS=${NAMESPACE:-default}
HERE=$(cd "$(dirname "$0")/.." && pwd)

docker build -q -t llmsim:dev "$HERE"
docker save llmsim:dev | k3s ctr images import - >/dev/null

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# The script is the one thing that changes often, so it comes from the file on
# disk rather than the copy pasted into the manifest.
kubectl -n "$NS" create configmap llmsim-script \
  --from-file=responses.json="$HERE/responses.json" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NS" apply -f "$HERE/deploy/k8s.yaml" \
  --prune=false 2>/dev/null || kubectl -n "$NS" apply -f "$HERE/deploy/k8s.yaml"
# Re-apply the real script over the manifest's placeholder, and restart so the
# pod picks it up (a ConfigMap change alone does not restart a Deployment).
kubectl -n "$NS" create configmap llmsim-script \
  --from-file=responses.json="$HERE/responses.json" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" rollout restart deploy/llmsim
kubectl -n "$NS" rollout status deploy/llmsim --timeout=3m

echo
echo "LLMSim is at http://llmsim.$NS.svc:8099"
echo "  anthropic  http://llmsim.$NS.svc:8099/anthropic"
echo "  openai     http://llmsim.$NS.svc:8099/openai/v1"
echo "  deepseek   http://llmsim.$NS.svc:8099/deepseek/v1"
