"""DAG integrity (runs where Apache Airflow is installed: the CI 'dag-validation' job or .venv-airflow)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("airflow")

DAGS = Path(__file__).resolve().parents[1] / "dags"
STAGES = ["validate_data", "preprocess_data", "train_model", "evaluate_and_log", "quality_gate", "register_model"]


@pytest.fixture(scope="module")
def dag():
    from airflow.dag_processing.dagbag import DagBag

    bag = DagBag(dag_folder=str(DAGS))  # examples are disabled via AIRFLOW__CORE__LOAD_EXAMPLES=False
    assert bag.import_errors == {}
    return bag.get_dag("ml_training_pipeline")


def test_stages_are_chained_in_order(dag):
    assert dag is not None
    assert sorted(dag.task_ids) == sorted(STAGES)
    for upstream, downstream in zip(STAGES, STAGES[1:], strict=False):
        assert dag.get_task(downstream).upstream_task_ids == {upstream}


def test_retry_policy_and_promotion_guard(dag):
    for task in dag.tasks:
        assert task.retries == 2
        assert task.retry_delay == timedelta(seconds=15)
        assert task.retry_exponential_backoff
    assert getattr(dag.get_task("register_model").trigger_rule, "value", None) == "all_success"


def test_manual_schedule_and_params(dag):
    assert dag.schedule is None or str(dag.timetable.summary) in {"None", "never"}
    assert dag.max_active_runs == 1
    assert "model_type" in dag.params
