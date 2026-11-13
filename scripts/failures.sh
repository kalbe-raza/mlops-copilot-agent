#!/usr/bin/env bash
# Deliberate failure injection for the demonstration.
#   inject-data  : overwrite data/raw/dataset.csv with a corrupted version (dropped column + nulls)
#   resolve-data : restore the committed DVC version (dvc checkout)
#   inject-k8s   : point MODEL_PATH in the ConfigMap to a missing file and recreate the pods
#                  (simulates a config change that only surfaces when pods restart, e.g. node drain)
#   resolve-k8s  : re-apply the correct ConfigMap (the restart is then performed by the agent)
set -euo pipefail
cd "$(dirname "$0")/.."
case "${1:-}" in
  inject-data)
    .venv/bin/python scripts/make_dataset.py --version corrupt
    .venv/bin/dvc status data/raw/dataset.csv.dvc || true ;;
  resolve-data)
    .venv/bin/dvc checkout data/raw/dataset.csv
    .venv/bin/dvc status ;;
  inject-k8s)
    kubectl -n mlops-agent patch configmap inference-api-config --type merge \
      -p '{"data":{"MODEL_PATH":"/app/models/model_v2.joblib"}}'
    kubectl -n mlops-agent delete pod -l app.kubernetes.io/name=inference-api --wait=false
    echo "pods recreated with broken MODEL_PATH" ;;
  resolve-k8s)
    kubectl apply -f kubernetes/configmap.yaml ;;
  *) echo "usage: $0 {inject-data|resolve-data|inject-k8s|resolve-k8s}" >&2; exit 2 ;;
esac
