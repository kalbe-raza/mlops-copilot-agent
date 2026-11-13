"""Airflow 3 REST tools against a mocked API (httpx.MockTransport): auth, policy and diagnostics."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx

from agent.schemas import ToolStatus
from agent.tools import build_registry
from agent.tools.airflow_tools import AirflowClient, AirflowTools
from agent.tools.base import ToolExecutor


class FakeAirflow:
    """Minimal stateful fake of the Airflow 3 API used by the tools."""

    def __init__(self, final_state="success", fail_task=None):
        self.requests: list[httpx.Request] = []
        self.tokens_issued = 0
        self.final_state = final_state
        self.fail_task = fail_task
        self.polls = 0
        self.expire_first_token = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/auth/token":
            body = json.loads(request.content)
            if body != {"username": "agent", "password": "s3cr3t-airflow-pass"}:
                return httpx.Response(401, json={"detail": "bad credentials"})
            self.tokens_issued += 1
            return httpx.Response(201, json={"access_token": f"jwt-{self.tokens_issued}"})
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer jwt-"):
            return httpx.Response(401)
        if self.expire_first_token and auth == "Bearer jwt-1":
            return httpx.Response(401)
        if path == "/api/v2/dags":
            return httpx.Response(200, json={"dags": [{"dag_id": "ml_training_pipeline", "is_paused": True,
                                                       "tags": [{"name": "ml"}]}], "total_entries": 1})
        if path == "/api/v2/dags/ml_training_pipeline" and request.method == "GET":
            return httpx.Response(200, json={"dag_id": "ml_training_pipeline", "is_paused": True})
        if path == "/api/v2/dags/ml_training_pipeline" and request.method == "PATCH":
            return httpx.Response(200, json={"dag_id": "ml_training_pipeline", "is_paused": False})
        if path == "/api/v2/dags/ml_training_pipeline/dagRuns" and request.method == "POST":
            return httpx.Response(200, json={"dag_run_id": "manual__1", "state": "queued"})
        if path == "/api/v2/dags/ml_training_pipeline/dagRuns" and request.method == "GET":
            return httpx.Response(200, json={"dag_runs": [{"dag_run_id": "manual__1", "state": self.final_state}]})
        if path == "/api/v2/dags/ml_training_pipeline/dagRuns/manual__1":
            self.polls += 1
            return httpx.Response(200, json={"dag_run_id": "manual__1",
                                             "state": "running" if self.polls < 2 else self.final_state})
        if path.endswith("/dagRuns/manual__1/taskInstances"):
            tis = []
            for t in ["validate_data", "preprocess_data", "train_model", "evaluate_and_log", "quality_gate",
                      "register_model"]:
                state = "success"
                if self.fail_task:
                    order = ["validate_data", "preprocess_data", "train_model", "evaluate_and_log", "quality_gate",
                             "register_model"]
                    idx, fail_idx = order.index(t), order.index(self.fail_task)
                    state = "success" if idx < fail_idx else "failed" if idx == fail_idx else "upstream_failed"
                tis.append({"task_id": t, "state": state, "try_number": 1, "duration": 1.5,
                            "start_date": f"2026-11-13T10:00:0{len(tis)}Z"})
            return httpx.Response(200, json={"task_instances": tis})
        if "/logs/" in path:
            return httpx.Response(200, json={"content": [
                {"timestamp": "2026-11-13T10:00:00Z", "level": "info", "event": "running stage 'validate'"},
                {"timestamp": "2026-11-13T10:00:01Z", "level": "error",
                 "event": "DATA VALIDATION FAILED: missing columns: ['worst_concave_points']"},
            ], "continuation_token": None})
        return httpx.Response(404)


def _tools(settings, fake: FakeAirflow) -> AirflowTools:
    client = AirflowClient(settings.airflow_base_url, "agent", settings.airflow_password,
                           transport=httpx.MockTransport(fake.handler))
    return AirflowTools(settings, client=client, poll_interval=0.01)


def _executor(settings, logger, airflow, confirm=None):
    registry = build_registry(settings, dvc=MagicMock(), mlflow=MagicMock(), airflow=airflow, k8s=MagicMock())
    return ToolExecutor(registry, logger, 5.0, confirm)


def test_requests_are_authenticated_with_a_bearer_token(settings):
    fake = FakeAirflow()
    out = _tools(settings, fake).list_dags(None)
    assert out["dags"][0]["dag_id"] == "ml_training_pipeline"
    assert out["dags"][0]["approved_for_trigger"] is True
    api_calls = [r for r in fake.requests if r.url.path.startswith("/api/")]
    assert all(r.headers["authorization"] == "Bearer jwt-1" for r in api_calls)


def test_expired_token_is_refreshed_once(settings):
    fake = FakeAirflow()
    fake.expire_first_token = True
    _tools(settings, fake).list_dags(None)
    assert fake.tokens_issued == 2


def test_trigger_of_unapproved_dag_is_denied_without_http_calls(settings, logger):
    fake = FakeAirflow()
    result = _executor(settings, logger, _tools(settings, fake), confirm=lambda s, a: True).execute(
        "airflow_trigger_dag", {"dag_id": "delete_everything"}, "r1")
    assert result.status == ToolStatus.DENIED
    assert fake.requests == []


def test_trigger_with_disallowed_conf_is_denied(settings, logger):
    fake = FakeAirflow()
    ex = _executor(settings, logger, _tools(settings, fake), confirm=lambda s, a: True)
    assert ex.execute("airflow_trigger_dag", {"conf": {"bash_command": "rm -rf /"}}, "r").status == ToolStatus.DENIED
    assert ex.execute("airflow_trigger_dag", {"conf": {"model_type": "xgboost"}}, "r").status == ToolStatus.DENIED
    assert fake.requests == []


def test_trigger_requires_confirmation(settings, logger):
    fake = FakeAirflow()
    result = _executor(settings, logger, _tools(settings, fake), confirm=None).execute("airflow_trigger_dag", {}, "r")
    assert result.status == ToolStatus.DENIED
    assert fake.requests == []


def test_confirmed_trigger_waits_and_reports_success(settings, logger):
    fake = FakeAirflow(final_state="success")
    result = _executor(settings, logger, _tools(settings, fake), confirm=lambda s, a: True).execute(
        "airflow_trigger_dag", {"conf": {"model_type": "svm"}}, "r1")
    assert result.status == ToolStatus.SUCCESS
    data = result.data
    assert data["final_state"] == "success" and data["unpaused_dag"] is True
    assert [t["state"] for t in data["task_instances"]] == ["success"] * 6
    post = next(r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/dagRuns"))
    body = json.loads(post.content)
    assert body["logical_date"] is None and body["conf"] == {"model_type": "svm"}


def test_failed_run_is_diagnosed_with_root_cause_and_log_lines(settings):
    fake = FakeAirflow(final_state="failed", fail_task="validate_data")
    out = _tools(settings, fake).get_failed_tasks(MagicMock(dag_id="ml_training_pipeline", dag_run_id=None))
    assert out["root_cause_task"] == "validate_data"
    failed = out["failed_tasks"][0]
    assert any("missing columns" in line for line in failed["error_lines"])
    assert "register_model" in out["skipped_downstream"]


def test_unreachable_airflow_is_unavailable(settings, logger):
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = AirflowClient(settings.airflow_base_url, "agent", "pw", transport=httpx.MockTransport(refuse))
    result = _executor(settings, logger, AirflowTools(settings, client=client)).execute("airflow_list_dags", {}, "r")
    assert result.status == ToolStatus.UNAVAILABLE
