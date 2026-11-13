"""ml_training_pipeline - validation -> preprocessing -> training -> evaluation/MLflow -> quality gate -> registration.

Design
------
* Each stage runs ``python -m training.train <stage>`` in the project's own virtual environment,
  so Airflow's dependencies never conflict with the ML stack (the same commands back ``dvc repro``).
* Retry behaviour: every task retries transient failures (non-zero exit, e.g. MLflow briefly
  unreachable) up to 2 times with exponential back-off. Deterministic failures - invalid data or a
  failed quality gate (exit code 2) - raise ``AirflowFailException`` so they fail immediately
  instead of wasting retries.
* Promotion safety: ``register_model`` depends on ``quality_gate`` with the default
  ``all_success`` trigger rule, so a failed validation or quality gate leaves it
  ``upstream_failed`` and nothing is promoted in the model registry.
* All subprocess output is streamed into the Airflow task log.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from airflow.sdk import DAG, Param, get_current_context, task
from airflow.sdk.exceptions import AirflowFailException

PROJECT_ROOT = Path(os.environ.get("MLOPS_PROJECT_ROOT", Path(__file__).resolve().parents[1]))
PYTHON = os.environ.get("MLOPS_PYTHON", str(PROJECT_ROOT / ".venv" / "bin" / "python"))
DATASET = "data/raw/dataset.csv"
MODEL_TYPES = ["random_forest", "logistic_regression", "gradient_boosting", "svm", "decision_stump"]
EXIT_DETERMINISTIC_FAILURE = 2

log = logging.getLogger("ml_training_pipeline")

default_args = {
    "owner": "mlops",
    "retries": 2,
    "retry_delay": timedelta(seconds=15),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=2),
    "execution_timeout": timedelta(minutes=15),
}


def _workdir(run_id: str) -> Path:
    return PROJECT_ROOT / "artifacts" / "airflow" / re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)


def _run_stage(stage: str, *extra: str) -> None:
    ctx = get_current_context()
    run_id = ctx["run_id"]
    workdir = _workdir(run_id)
    cmd = [PYTHON, "-m", "training.train", stage, "--workdir", str(workdir), *extra]
    env = {**os.environ, "MLFLOW_DISABLE_AGENT_HINT": "1", "PYTHONUNBUFFERED": "1"}
    log.info("running stage '%s': %s", stage, " ".join(cmd))
    proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env=env)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        (log.error if " ERROR " in line else log.info)(line)
    rc = proc.wait()
    if rc == EXIT_DETERMINISTIC_FAILURE:
        raise AirflowFailException(f"stage '{stage}' failed deterministically (exit {rc}); not retrying")
    if rc != 0:
        raise RuntimeError(f"stage '{stage}' exited with {rc}; will be retried if attempts remain")


with DAG(
    dag_id="ml_training_pipeline",
    description="Validate DVC data, train, evaluate & log to MLflow, enforce a quality gate, register the model",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["ml", "training", "mlflow", "dvc"],
    params={
        "model_type": Param("random_forest", type="string", enum=MODEL_TYPES),
        "params": Param({}, type="object", description="Hyper-parameter overrides (model defaults otherwise)"),
        "experiment": Param("cancer-airflow-pipeline", type="string"),
        "run_name": Param("", type="string"),
    },
    doc_md=__doc__,
) as dag:

    @task(task_id="validate_data")
    def validate_data() -> None:
        _run_stage("validate", "--data", str(PROJECT_ROOT / DATASET))

    @task(task_id="preprocess_data")
    def preprocess_data() -> None:
        _run_stage("preprocess")

    @task(task_id="train_model")
    def train_model() -> None:
        p = get_current_context()["params"]
        _run_stage("train", "--model-type", p["model_type"], "--params", json.dumps(p["params"] or {}))

    @task(task_id="evaluate_and_log")
    def evaluate_and_log() -> None:
        ctx = get_current_context()
        p = ctx["params"]
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
        run_name = p.get("run_name") or f"airflow-{p['model_type']}-{stamp}"
        tags = {"airflow.dag_id": ctx["dag"].dag_id, "airflow.run_id": ctx["run_id"],
                "pipeline.trigger": "airflow"}
        _run_stage("evaluate", "--experiment", p["experiment"], "--run-name", run_name, "--tags", json.dumps(tags))

    @task(task_id="quality_gate")
    def quality_gate() -> None:
        _run_stage("quality-gate")

    @task(task_id="register_model")
    def register_model() -> None:
        _run_stage("register")

    validate_data() >> preprocess_data() >> train_model() >> evaluate_and_log() >> quality_gate() >> register_model()
