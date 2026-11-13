#!/usr/bin/env bash
# Start the MLflow tracking server (SQLite backend + proxied local artifact store) in the background.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs mlartifacts
if curl -sf http://127.0.0.1:5000/health >/dev/null 2>&1; then echo "MLflow already running"; exit 0; fi
# Background GenAI job workers are not needed for classic ML tracking and cost ~2 GB RAM.
MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false MLFLOW_DISABLE_AGENT_HINT=1 nohup .venv/bin/mlflow server \
  --backend-store-uri "sqlite:///$(pwd)/mlflow.db" \
  --artifacts-destination "$(pwd)/mlartifacts" \
  --host 127.0.0.1 --port 5000 --workers 1 > logs/mlflow.log 2>&1 &
echo $! > logs/mlflow.pid
for _ in $(seq 1 60); do
  if curl -sf http://127.0.0.1:5000/health >/dev/null 2>&1; then echo "MLflow up at http://127.0.0.1:5000"; exit 0; fi
  sleep 1
done
echo "MLflow failed to start; see logs/mlflow.log" >&2; exit 1
