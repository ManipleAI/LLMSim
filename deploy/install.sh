#!/usr/bin/env bash
# Build LLMSim, put it in k3s, and apply it — script, volume and all.
#
#   NAMESPACE=llmsim ./deploy/install.sh
#
# On k3d rather than k3s: K3D_CLUSTER=<name> NAMESPACE=... ./deploy/install.sh
#
# What LLMSim writes for itself (keys, routing, your rules,
# runs) lives on the volume llmsim-data, so installing again, a new image or a
# restarted pod keeps it. An LLMSim installed before that volume existed kept
# all of it inside its container; this carries it over once, so nobody has to
# set it up again.
set -euo pipefail
NS=${NAMESPACE:-default}
HERE=$(cd "$(dirname "$0")/.." && pwd)
STATE='llmsim.config.json llmsim.credentials.json llmsim.rules.json llmsim.assertions.json llmsim.tests.json llmsim.runs.db llmsim.runs.db-wal cassettes evaluators'

docker build -q -t llmsim:dev "$HERE"
if command -v k3s >/dev/null 2>&1; then
  docker save llmsim:dev | k3s ctr images import - >/dev/null
elif [ -n "${K3D_CLUSTER:-}" ]; then
  k3d image import -c "$K3D_CLUSTER" llmsim:dev >/dev/null
else
  echo "no k3s here and no K3D_CLUSTER: load llmsim:dev into the cluster yourself" >&2
fi

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# Created once, never re-applied: a claim cannot be changed in place, and one
# made by hand earlier (say with a storage class named) must not be fought.
if ! kubectl -n "$NS" get pvc llmsim-data >/dev/null 2>&1; then
  kubectl -n "$NS" apply -f "$HERE/deploy/k8s-data.yaml"
fi

# An LLMSim running without the volume holds everything in /app. Take a copy
# before its pod is replaced. Owner-only: it carries the sign-in and the keys.
CARRY=
if kubectl -n "$NS" get deploy llmsim >/dev/null 2>&1 \
   && ! kubectl -n "$NS" get deploy llmsim -o jsonpath='{.spec.template.spec.volumes[*].persistentVolumeClaim.claimName}' | grep -qw llmsim-data; then
  TMPD=$(mktemp -d)
  chmod 700 "$TMPD"
  trap 'rm -rf "${TMPD:?}"' EXIT
  kubectl -n "$NS" exec deploy/llmsim -- sh -c "cd /app && ls -d $STATE 2>/dev/null | xargs -r tar cf -" > "$TMPD/state.tar" || true
  if [ -s "$TMPD/state.tar" ]; then
    CARRY="$TMPD/state.tar"
    echo "carrying over from the running LLMSim: $(tar tf "$CARRY" | grep -v '/.' | tr '\n' ' ')"
  fi
fi

# Onto the volume before the new LLMSim starts, so it reads all of it when it
# does, and never under a running one: SQLite does not take kindly to its
# files being replaced while they are open. And never over a volume that
# already holds what a person made (settings, a sign-in, credentials, rules):
# that is newer than the container's copy. A run database and the sample suite
# alone are what any first start writes, and do not count.
if [ -n "$CARRY" ]; then
  kubectl -n "$NS" delete pod llmsim-seed --ignore-not-found --wait=true >/dev/null
  kubectl -n "$NS" apply -f - >/dev/null <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: llmsim-seed
  labels: { app: llmsim-seed }
spec:
  restartPolicy: Never
  containers:
    - name: seed
      image: llmsim:dev
      imagePullPolicy: IfNotPresent
      command: ["sleep", "600"]
      volumeMounts: [{ name: data, mountPath: /data }]
  volumes:
    - name: data
      persistentVolumeClaim: { claimName: llmsim-data }
YAML
  kubectl -n "$NS" wait --for=condition=Ready pod/llmsim-seed --timeout=3m >/dev/null
  if kubectl -n "$NS" exec llmsim-seed -- sh -c 'cd /data && ls llmsim.config.json llmsim.credentials.json llmsim.rules.json llmsim.assertions.json >/dev/null 2>&1'; then
    echo "the volume already holds LLMSim's state; the container's copy is not used"
  else
    kubectl -n "$NS" exec -i llmsim-seed -- sh -c 'cd /data && rm -f llmsim.runs.db llmsim.runs.db-wal llmsim.runs.db-shm && umask 077 && tar xf -' < "$CARRY"
    echo "carried over onto the volume: the new LLMSim starts with it"
  fi
  kubectl -n "$NS" delete pod llmsim-seed --wait=true >/dev/null
fi

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
echo "LLMSim is at http://llmsim.$NS.svc:8099  (its state: the volume llmsim-data)"
echo "  anthropic  http://llmsim.$NS.svc:8099/anthropic"
echo "  openai     http://llmsim.$NS.svc:8099/openai/v1"
echo "  deepseek   http://llmsim.$NS.svc:8099/deepseek/v1"
