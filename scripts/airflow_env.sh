#!/usr/bin/env bash
# Shared environment for every Airflow command (source this file).
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AIRFLOW_HOME="$PROJECT_ROOT/airflow_home"
export AIRFLOW__CORE__DAGS_FOLDER="$PROJECT_ROOT/dags"
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS="admin:admin,agent:user"
export AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE="$AIRFLOW_HOME/passwords.json"
export AIRFLOW__API__HOST=127.0.0.1
export AIRFLOW__API__PORT=8080
export AIRFLOW__CORE__EXECUTION_API_SERVER_URL="http://127.0.0.1:8080/execution/"
export AIRFLOW__DAG_PROCESSOR__REFRESH_INTERVAL=30
export AIRFLOW__CORE__PARALLELISM=4          # small LocalExecutor pool for a laptop
export AIRFLOW__API__WORKERS=1
export AIRFLOW__API_AUTH__JWT_SECRET="$(cat "$AIRFLOW_HOME/.jwt_secret" 2>/dev/null || true)"
export MLOPS_PROJECT_ROOT="$PROJECT_ROOT"
export MLOPS_PYTHON="$PROJECT_ROOT/.venv/bin/python"
export MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-http://127.0.0.1:5000}"
export PATH="$PROJECT_ROOT/.venv-airflow/bin:$PATH"
