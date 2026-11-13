#!/usr/bin/env bash
# Export plain-text evidence from DVC, MLflow, Airflow and Kubernetes into evidence/*.txt
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/.venv/bin:$PATH" MLFLOW_DISABLE_AGENT_HINT=1
set -a; . ./.env; set +a
run() { echo; echo "\$ $*"; eval "$@" 2>&1; }

{
  echo "# DVC evidence - $(date -u +%FT%TZ)"
  run git tag -n --list "data-v*"
  run git log --oneline -- data/raw/dataset.csv.dvc
  run cat data/raw/dataset.csv.dvc
  run dvc remote list
  run dvc status
  run dvc data status --granular
  run dvc diff data-v1.0 data-v2.0
  run dvc dag
  run dvc metrics show
  run dvc params diff HEAD~3
  run "dvc get . data/raw/dataset.csv --rev data-v1.0 -o /tmp/dataset-v1.csv --force && md5sum /tmp/dataset-v1.csv && wc -l /tmp/dataset-v1.csv"
  run "git show data-v1.0:data/raw/dataset.csv.dvc | grep md5"
} > evidence/dvc.txt

python - > evidence/mlflow.txt <<'PY'
from datetime import UTC, datetime
from mlflow.tracking import MlflowClient
c = MlflowClient("http://127.0.0.1:5000")
print(f"# MLflow evidence - {datetime.now(tz=UTC).isoformat(timespec='seconds')} (MlflowClient, http://127.0.0.1:5000)\n")
exps = c.search_experiments()
for e in exps:
    runs = c.search_runs([e.experiment_id], order_by=["attributes.start_time ASC"], max_results=200)
    print(f"## Experiment '{e.name}' (id {e.experiment_id}) - {len(runs)} runs")
    print(f"{'run name':38} {'status':9} {'model':20} {'data':10} {'f1':>7} {'recall':>7} {'gap':>7}  gate")
    for r in runs:
        m, t = r.data.metrics, r.data.tags
        f = lambda k: f"{m[k]:.4f}" if k in m else "-"
        print(f"{r.info.run_name:38} {r.info.status:9} {t.get('model.type','-'):20} {t.get('dataset.version','-'):10} "
              f"{f('f1'):>7} {f('recall'):>7} {f('f1_gap'):>7}  {t.get('quality_gate.status','-')}")
    print()
for rm in c.search_registered_models():
    print(f"## Registered model '{rm.name}' aliases={dict(rm.aliases)}")
    for mv in sorted(c.search_model_versions(f"name='{rm.name}'"), key=lambda v: int(v.version)):
        run = c.get_run(mv.run_id)
        print(f"  v{mv.version:<3} run={run.info.run_name:38} f1={mv.tags.get('f1')} data={mv.tags.get('dataset.version')} "
              f"md5={mv.tags.get('dataset.dvc_md5','')[:8]}")
PY

python - > evidence/airflow.txt <<'PY'
import os
from datetime import UTC, datetime
from urllib.parse import quote
import httpx
base = "http://127.0.0.1:8080"
token = httpx.post(f"{base}/auth/token", json={"username": os.environ["AIRFLOW_USERNAME"],
                                               "password": os.environ["AIRFLOW_PASSWORD"]}).json()["access_token"]
h = {"Authorization": f"Bearer {token}"}
get = lambda path, **p: httpx.get(f"{base}/api/v2/{path}", headers=h, params=p).json()
print(f"# Airflow 3 REST API evidence - {datetime.now(tz=UTC).isoformat(timespec='seconds')} (JWT from POST /auth/token)")
print("\n$ GET /api/v2/version\n", get("version"))
print("\n$ GET /api/v2/dags")
for d in get("dags")["dags"]:
    print(f"  {d['dag_id']} paused={d['is_paused']} tags={[t['name'] for t in d['tags']]}")
print("\n$ GET /api/v2/dags/ml_training_pipeline/dagRuns?order_by=run_after")
runs = get("dags/ml_training_pipeline/dagRuns", order_by="run_after", limit=50)["dag_runs"]
for r in runs:
    print(f"  {r['dag_run_id']:45} {r['state']:8} conf={r.get('conf')}")
for r in runs:
    rid = r["dag_run_id"]
    print(f"\n$ GET /api/v2/dags/ml_training_pipeline/dagRuns/{rid}/taskInstances   [{r['state']}]")
    tis = get(f"dags/ml_training_pipeline/dagRuns/{quote(rid, safe='')}/taskInstances")["task_instances"]
    for t in sorted(tis, key=lambda t: t["start_date"] or "9"):
        dur = f"{t['duration']:.1f}s" if t.get("duration") is not None else "-"
        print(f"  {t['task_id']:18} {str(t['state']):16} try={t['try_number']} duration={dur}")
PY

{
  echo "# Kubernetes evidence - $(date -u +%FT%TZ) (kind cluster 'mlops')"
  run kubectl get nodes -o wide
  run kubectl -n mlops-agent get deploy,rs,pods,svc,endpointslices,cm -o wide
  run kubectl -n mlops-agent describe deploy inference-api
  run kubectl -n mlops-agent get configmap inference-api-config -o yaml
  run kubectl -n mlops-agent get events --sort-by=.lastTimestamp
  POD=$(kubectl -n mlops-agent get pods -l app.kubernetes.io/name=inference-api -o jsonpath='{.items[0].metadata.name}')
  run kubectl -n mlops-agent logs "$POD" --tail=8
  run kubectl get --raw "/api/v1/namespaces/mlops-agent/services/inference-api:http/proxy/version"
  run kubectl get --raw "/api/v1/namespaces/mlops-agent/services/inference-api:http/proxy/health/ready"
  run "KUBECONFIG=agent.kubeconfig kubectl auth can-i --list -n mlops-agent"
  run "KUBECONFIG=agent.kubeconfig kubectl auth can-i delete pods -n mlops-agent"
  run "KUBECONFIG=agent.kubeconfig kubectl auth can-i get pods -n kube-system"
} > evidence/kubernetes.txt

tail -n 40 logs/agent.jsonl > evidence/agent-structured-logs.jsonl
echo "evidence exported"; wc -l evidence/*.txt evidence/*.jsonl
