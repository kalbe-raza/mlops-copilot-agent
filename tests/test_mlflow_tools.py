"""MLflow tools against a real, temporary SQLite-backed tracking store (MlflowClient API)."""

from __future__ import annotations

import dataclasses

import mlflow
import pytest

from agent.schemas import BestRunInput, CompareRunsInput, ModelRegistryInput, RunIdInput, ToolStatus
from agent.tools.base import ToolError
from agent.tools.mlflow_tools import MLflowTools
from tests.conftest import make_executor

RUNS = [
    # name, f1, recall, gap, gate, dataset
    ("run-01-logistic_regression", 0.944, 0.94, 0.01, "passed", "data-v1.0"),
    ("run-02-random_forest", 0.990, 0.99, 0.09, "failed", "data-v2.0"),  # best F1 but overfits -> failed gate
    ("run-03-svm", 0.971, 0.94, 0.016, "passed", "data-v2.0"),
]


@pytest.fixture
def tracked(settings):
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_experiment("unit-exp")
    ids = {}
    for name, f1, recall, gap, gate, data in RUNS:
        with mlflow.start_run(run_name=name) as run:
            mlflow.log_params({"model_type": name.split("-", 2)[2]})
            mlflow.log_metrics({"f1": f1, "recall": recall, "precision": 0.95, "f1_gap": gap})
            mlflow.set_tags({"quality_gate.status": gate, "dataset.version": data, "dataset.dvc_md5": "a" * 32,
                             "dataset.git_commit": "c" * 40, "git.commit": "d" * 40, "dataset.matches_dvc": "true",
                             "dataset.rows": "569", "model.type": name.split("-", 2)[2]})
            mlflow.log_dict({"f1": f1}, "evaluation/metrics.json")
            ids[name] = run.info.run_id
    client = mlflow.MlflowClient(settings.mlflow_tracking_uri)
    client.create_registered_model("unit-model")
    mv = client.create_model_version("unit-model", f"runs:/{ids['run-03-svm']}/model", ids["run-03-svm"])
    client.set_registered_model_alias("unit-model", "champion", mv.version)
    return ids


def test_find_best_run_ranks_by_metric_and_flags_quality_gate(settings, tracked):
    out = MLflowTools(settings).find_best_run(BestRunInput(metric="f1"))
    assert out["best_run"]["run_name"] == "run-02-random_forest"
    assert out["best_quality_gate_passing_run"]["run_name"] == "run-03-svm"
    assert any("quality gate" in c for c in out["caveats"])
    assert any("overfitting" in c for c in out["caveats"])
    assert "not make a model production-ready" in out["note"]


def test_compare_runs_recommends_best_gate_passing_candidate(settings, tracked):
    out = MLflowTools(settings).compare_runs(CompareRunsInput(last_n=3, metric="f1"))
    assert out["recommended_run_name"] == "run-03-svm"
    assert out["excluded_failed_quality_gate"] == ["run-02-random_forest"]
    assert len(out["runs"]) == 3


def test_run_dataset_provenance_and_resolution_by_number(settings, tracked):
    tools = MLflowTools(settings)
    out = tools.get_run_dataset_provenance(RunIdInput(run_id="3"))  # "run 3" -> run-03-*
    assert out["run_name"] == "run-03-svm"
    assert out["dataset_version"] == "data-v2.0" and out["dataset_md5"] == "a" * 32
    with pytest.raises(ToolError):
        tools.get_run_dataset_provenance(RunIdInput(run_id="25"))


def test_run_details_include_artifacts_and_registry(settings, tracked):
    out = MLflowTools(settings).get_run_details(RunIdInput(run_id=tracked["run-03-svm"]))
    assert "evaluation/metrics.json" in out["artifacts"]
    assert out["registered_versions"][0]["aliases"] == ["champion"]


def test_model_registry_lists_versions_and_aliases(settings, tracked):
    out = MLflowTools(settings).get_model_registry(ModelRegistryInput(model_name="unit-model"))
    model = out["registered_models"][0]
    assert model["aliases"] == {"champion": "1"}
    assert model["versions"][0]["run_id"] == tracked["run-03-svm"]


def test_unreachable_tracking_server_is_unavailable(settings, logger):
    down = dataclasses.replace(settings, mlflow_tracking_uri="http://127.0.0.1:9")  # nothing listens on port 9
    tools = MLflowTools(down)
    result = make_executor(tools.specs(), logger).execute("mlflow_list_experiments", {}, "r1")
    assert result.status == ToolStatus.UNAVAILABLE
    assert result.data is None
