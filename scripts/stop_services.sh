#!/usr/bin/env bash
# Stop MLflow and Airflow started by the scripts in this folder (the kind cluster is stopped separately).
cd "$(dirname "$0")/.."
for name in airflow mlflow; do
  if [ -f "logs/$name.pid" ]; then
    pid=$(cat "logs/$name.pid")
    pkill -TERM -P "$pid" 2>/dev/null || true
    kill -TERM "$pid" 2>/dev/null || true
    rm -f "logs/$name.pid"
    echo "stopped $name"
  fi
done
sleep 3
pgrep -f "airflow (scheduler|api-server|api_server|dag-processor|triggerer|serve-logs|standalone)" | xargs -r kill -TERM 2>/dev/null || true
pgrep -f "[.]venv/bin/mlflow server" | xargs -r kill -TERM 2>/dev/null || true
pgrep -f "uvicorn.*mlflow" | xargs -r kill -TERM 2>/dev/null || true
exit 0
