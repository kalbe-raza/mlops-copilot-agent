#!/usr/bin/env bash
# Create a short-lived, namespace-scoped kubeconfig for the agent's ServiceAccount (kubernetes/rbac.yaml).
# Output: ./agent.kubeconfig (git-ignored). Set AGENT_KUBECONFIG=./agent.kubeconfig in .env.
set -euo pipefail
cd "$(dirname "$0")/.."
NS=mlops-agent
SA=mlops-agent
DURATION="${1:-24h}"
TOKEN=$(kubectl -n "$NS" create token "$SA" --duration="$DURATION")
SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
CA=$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
umask 077
cat > agent.kubeconfig <<KCFG
apiVersion: v1
kind: Config
clusters:
- name: kind-mlops
  cluster:
    server: ${SERVER}
    certificate-authority-data: ${CA}
users:
- name: ${SA}
  user:
    token: ${TOKEN}
contexts:
- name: mlops-agent
  context:
    cluster: kind-mlops
    namespace: ${NS}
    user: ${SA}
current-context: mlops-agent
KCFG
echo "wrote agent.kubeconfig (ServiceAccount ${NS}/${SA}, token valid ${DURATION})"
