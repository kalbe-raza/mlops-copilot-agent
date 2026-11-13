#!/usr/bin/env bash
# Start Apache Airflow 3 (api-server, scheduler, dag-processor, triggerer) via `airflow standalone`.
# Users (SimpleAuthManager): admin (UI) and agent (role "user", used by the agent's REST calls).
# Random passwords are generated once into airflow_home/passwords.json and the agent password is
# written to .env (both git-ignored).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p airflow_home logs
if [ ! -f airflow_home/passwords.json ]; then
  .venv/bin/python - <<'PY'
import json, secrets, pathlib
pw = {"admin": secrets.token_urlsafe(12), "agent": secrets.token_urlsafe(16)}
pathlib.Path("airflow_home/passwords.json").write_text(json.dumps(pw))
pathlib.Path("airflow_home/.jwt_secret").write_text(secrets.token_urlsafe(32))
env = pathlib.Path(".env")
lines = [l for l in (env.read_text().splitlines() if env.exists() else []) if not l.startswith(("AIRFLOW_PASSWORD=", "AIRFLOW_USERNAME="))]
lines += ["AIRFLOW_USERNAME=agent", f"AIRFLOW_PASSWORD={pw['agent']}"]
env.write_text("\n".join(lines) + "\n")
print("generated Airflow credentials (airflow_home/passwords.json, .env)")
PY
fi
source scripts/airflow_env.sh
if curl -sf http://127.0.0.1:8080/api/v2/monitor/health >/dev/null 2>&1; then echo "Airflow already running"; exit 0; fi
airflow db migrate > logs/airflow-migrate.log 2>&1
nohup airflow standalone > logs/airflow.log 2>&1 &
echo $! > logs/airflow.pid
for _ in $(seq 1 120); do
  if curl -sf http://127.0.0.1:8080/api/v2/monitor/health >/dev/null 2>&1; then
    echo "Airflow up at http://127.0.0.1:8080 (UI login: admin / see airflow_home/passwords.json)"; exit 0
  fi
  sleep 2
done
echo "Airflow failed to start; see logs/airflow.log" >&2; exit 1
