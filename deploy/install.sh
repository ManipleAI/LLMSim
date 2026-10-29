#!/usr/bin/env bash
# Build LLMSim, put it in k3s, and apply it — script, volume and all.
#
#   NAMESPACE=maniple ./deploy/install.sh
#
# On k3d rather than k3s: K3D_CLUSTER=<name> NAMESPACE=... ./deploy/install.sh
#
# Who may use the console and the API is the Secret llmsim-access. A new install
# makes one and prints the password and the API key once; to choose them, pass
#
#   LLMSIM_ADMIN_USER=admin LLMSIM_ADMIN_PASSWORD=... LLMSIM_API_KEY=... ./deploy/install.sh
#
# which also changes them on an install that has them. LLMSIM_OPEN=1 installs one
# with no sign-in. An LLMSim installed before there was a sign-in is left as open
# as it was unless they are passed, so what already talks to it keeps working.
#
# What LLMSim writes for itself (the ChatGPT sign-in, keys, routing, your rules,
# runs) lives on the volume llmsim-data, so installing again, a new image or a
# restarted pod keeps it. An LLMSim installed before that volume existed kept
# all of it inside its container; this carries it over once, so nobody has to
# sign in again.
set -euo pipefail
NS=${NAMESPACE:-default}
HERE=$(cd "$(dirname "$0")/.." && pwd)
STATE='llmsim.config.json llmsim.chatgpt-auth.json llmsim.access.json llmsim.secret llmsim.credentials.json llmsim.rules.json llmsim.assertions.json llmsim.tests.json llmsim.runs.db llmsim.runs.db-wal cassettes evaluators'

docker build -q -t llmsim:dev "$HERE"
if command -v k3s >/dev/null 2>&1; then
  docker save llmsim:dev | k3s ctr images import - >/dev/null
elif [ -n "${K3D_CLUSTER:-}" ]; then
  k3d image import -c "$K3D_CLUSTER" llmsim:dev >/dev/null
else
  echo "no k3s here and no K3D_CLUSTER: load llmsim:dev into the cluster yourself" >&2
fi

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

secret_value() {
  kubectl -n "$NS" get secret llmsim-access -o jsonpath="{.data.$1}" 2>/dev/null | base64 -d 2>/dev/null || true
}
make_secret() { head -c 24 /dev/urandom | base64 | tr -d '/+=\n'; }
HAD_SECRET=; kubectl -n "$NS" get secret llmsim-access >/dev/null 2>&1 && HAD_SECRET=1
NEW_INSTALL=1; kubectl -n "$NS" get deploy llmsim >/dev/null 2>&1 && NEW_INSTALL=
MADE=
if [ -n "${LLMSIM_ADMIN_PASSWORD:-}${LLMSIM_API_KEY:-}" ] || { [ -z "$HAD_SECRET" ] && [ -n "$NEW_INSTALL" ] && [ "${LLMSIM_OPEN:-}" != 1 ]; }; then
  # What was not passed is kept from the Secret, and made only when there is none.
  ADMIN_USER=${LLMSIM_ADMIN_USER:-$(secret_value LLMSIM_ADMIN_USER)}
  ADMIN_PASSWORD=${LLMSIM_ADMIN_PASSWORD:-$(secret_value LLMSIM_ADMIN_PASSWORD)}
  API_KEY=${LLMSIM_API_KEY:-$(secret_value LLMSIM_API_KEY)}
  [ -n "$ADMIN_USER" ] || ADMIN_USER=admin
  [ -n "$ADMIN_PASSWORD" ] || { ADMIN_PASSWORD=$(make_secret); MADE=1; }
  [ -n "$API_KEY" ] || { API_KEY="llmsim-$(make_secret)"; MADE=1; }
  # From a file, owner-only and gone straight after, so the values never sit in a process list.
  ENVF=$(mktemp); chmod 600 "$ENVF"
  printf 'LLMSIM_ADMIN_USER=%s\nLLMSIM_ADMIN_PASSWORD=%s\nLLMSIM_API_KEY=%s\n' "$ADMIN_USER" "$ADMIN_PASSWORD" "$API_KEY" > "$ENVF"
  kubectl -n "$NS" create secret generic llmsim-access --from-env-file="$ENVF" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  rm -f "$ENVF"
elif [ -z "$HAD_SECRET" ]; then
  echo "LLMSim stays OPEN: anyone who can reach it can use the console and the whole API."
  echo "  To require a sign-in: LLMSIM_ADMIN_PASSWORD=... LLMSIM_API_KEY=... $0"
fi

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
  if kubectl -n "$NS" exec llmsim-seed -- sh -c 'cd /data && ls llmsim.config.json llmsim.chatgpt-auth.json llmsim.credentials.json llmsim.rules.json llmsim.assertions.json >/dev/null 2>&1'; then
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
if kubectl -n "$NS" get secret llmsim-access >/dev/null 2>&1; then
  echo "  console    http://llmsim.$NS.svc:8099/login  as ${ADMIN_USER:-$(secret_value LLMSIM_ADMIN_USER)}"
  if [ -n "$MADE" ]; then
    echo
    echo "  Made for this install, and shown only now (they are kept in the Secret llmsim-access):"
    echo "    password  $ADMIN_PASSWORD"
    echo "    API key   $API_KEY"
  fi
  echo "  read them again:  kubectl -n $NS get secret llmsim-access -o jsonpath='{.data.LLMSIM_ADMIN_PASSWORD}' | base64 -d"
fi
echo "  anthropic  http://llmsim.$NS.svc:8099/anthropic"
echo "  openai     http://llmsim.$NS.svc:8099/openai/v1"
echo "  deepseek   http://llmsim.$NS.svc:8099/deepseek/v1"
