"""Agent-level behaviour: tool selection, multi-tool correlation, grounding and LLM fallback."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace as NS
from typing import Any

import pytest

from agent.llm import GeminiPlanner, LLMError
from agent.orchestrator import MLOpsAgent, ungrounded_numbers
from agent.planner_rules import RuleBasedPlanner
from agent.schemas import ToolResult, ToolStatus
from agent.tools import build_registry
from agent.tools.airflow_tools import AirflowTools
from agent.tools.dvc_tools import DVCTools
from agent.tools.kubernetes_tools import KubernetesTools
from agent.tools.mlflow_tools import MLflowTools
from tests.conftest import UnavailableModule

V2_MD5 = "44042793903d981829eb545e67d08fee"
V1_MD5 = "c0437af750ce5e7418d94a4689cebac6"


# ----------------------------------------------------------------------------- canned tool modules
class FakeMLflow(MLflowTools):
    def __init__(self, settings, champion_md5=V2_MD5, champion_data="data-v2.0"):
        super().__init__(settings, client=object())
        self.md5, self.data = champion_md5, champion_data

    def _run(self):
        return {"run_id": "r" * 32, "run_name": "run-11-svm", "experiment": "cancer-tuning-v2", "model_type": "svm",
                "metrics": {"f1": 0.9709, "recall": 0.9434, "precision": 1.0, "f1_gap": 0.0164},
                "dataset_version": self.data, "quality_gate": "passed", "start_time": "2026-11-13T17:00:00+00:00",
                "age_days": 0.2, "status": "FINISHED"}

    def find_best_run(self, args):
        return {"metric": "f1", "mode": "max", "runs_considered": 12, "best_run": self._run(),
                "best_quality_gate_passing_run": self._run(), "top_ranking": [self._run()], "caveats": [],
                "note": "Highest metric alone does not make a model production-ready."}

    def get_run_dataset_provenance(self, args):
        return {"run_id": "r" * 32, "run_name": "run-11-svm", "dataset_version": self.data, "dataset_md5": self.md5,
                "dataset_git_commit": "cefe3d7" + "0" * 33, "code_git_commit": "a" * 40, "matches_dvc": "true",
                "rows": "569", "dataset_path": "data/raw/dataset.csv", "start_time": None, "all_provenance_tags": {}}

    def get_model_registry(self, args):
        return {"registered_models": [{"name": "cancer-risk-classifier", "aliases": {"champion": "2"},
                                       "latest_version": "3", "versions": [{"version": "2", "run_id": "r" * 32,
                                                                            "aliases": ["champion"], "tags": {}}]}]}

    def get_run_details(self, args):
        return {**self._run(), "tags": {"dataset.dvc_md5": self.md5}, "params": {}, "artifacts": [],
                "registered_versions": []}


class FakeDVC(DVCTools):
    def resolve_dataset_reference(self, args):
        version = "data-v2.0" if args.md5 == V2_MD5 else "data-v1.0"
        return {"found": True, "version": version, "git_commit": "cefe3d7" + "0" * 33, "md5": args.md5,
                "is_current_version": args.md5 == V2_MD5, "in_local_cache": True}

    def get_dataset_version(self, args):
        return {"version": "data-v2.0", "dvc_md5": V2_MD5, "git_commit": "cefe3d7" + "0" * 33,
                "short_commit": "cefe3d7", "workspace_matches_dvc": True}


class FakeAirflow(AirflowTools):
    triggered = 0

    def get_dag_runs(self, args):
        return {"dag_id": "ml_training_pipeline", "runs": [{"dag_run_id": "manual__1", "state": "success"}],
                "state_counts": {"success": 1}, "latest_state": "success"}

    def trigger_dag(self, args):
        FakeAirflow.triggered += 1
        return {"dag_run_id": "should-not-happen"}


class FakeK8s(KubernetesTools):
    def __init__(self, settings, healthy=True):
        super().__init__(settings, core=object(), apps=object(), discovery=object())
        self.healthy = healthy

    def get_deployed_version(self, args):
        return {"namespace": "mlops-agent", "deployment": "inference-api", "image": "mlops-inference-api:1.0.0",
                "image_tag": "1.0.0", "app_version_label": "1.0.0", "replicas": {"desired": 2, "ready": 2},
                "live_version_endpoint": {"app_version": "1.0.0", "model": {"version": "2", "run_id": "r" * 32}},
                "configmap": {}, "annotations": {}, "revision": "1"}

    def find_unhealthy_pods(self, args):
        bad = [] if self.healthy else [{"name": "api-x", "problems": ["not ready (0/1)"], "containers": [],
                                        "recent_warning_events": []}]
        return {"namespace": "mlops-agent", "total_pods": 2, "healthy_pods": ["api-a", "api-b"][: 2 - len(bad)],
                "unhealthy_pods": bad, "all_healthy": not bad}


def _agent(settings, planner="rules", llm=None, **modules) -> MLOpsAgent:
    registry = build_registry(settings, dvc=modules.get("dvc") or FakeDVC(settings),
                              mlflow=modules.get("mlflow") or FakeMLflow(settings),
                              airflow=modules.get("airflow") or FakeAirflow(settings),
                              k8s=modules.get("k8s") or FakeK8s(settings))
    return MLOpsAgent(settings, registry=registry, planner=planner, confirm=lambda s, a: True, llm=llm)


SCENARIO = ("Find the best recent model, identify the dataset version used to train it, check the deployment "
            "status, and explain whether retraining is necessary.")


# ----------------------------------------------------------------------------- tool selection
@pytest.mark.parametrize(("question", "intent"), [
    (SCENARIO, "full_assessment"),
    ("Which dataset version was used for experiment 25?", "dataset_provenance"),
    ("Compare the last five training runs and recommend the best candidate based on F1 score.",
     "mlflow_compare_runs"),
    ("Trigger the model training pipeline and report whether execution succeeded.", "airflow_trigger"),
    ("Why is my prediction API unavailable?", "k8s_diagnose"),
    ("Why did the training pipeline fail?", "airflow_failures"),
    ("Has the tracked data changed?", "dvc_changes"),
    ("Compare dataset versions data-v1.0 and data-v2.0", "dvc_compare"),
    ("How do I reproduce dataset data-v1.0?", "dvc_reproduce"),
    ("Show the model registry", "mlflow_registry"),
    ("Restart the inference deployment", "k8s_restart"),
    ("What version of the application is deployed?", "k8s_version"),
])
def test_rule_planner_selects_the_right_tools(settings, question, intent):
    assert RuleBasedPlanner(settings).classify(question) == intent


def test_rule_planner_extracts_parameters():
    p = RuleBasedPlanner
    assert p._number("compare the last five runs", 3) == 5
    assert p._metric("best by recall") == "recall"
    assert p._run_ref("dataset used for experiment 25?") == "25"
    assert p._revs("compare data-v1.0 with data-v2.0") == ["data-v1.0", "data-v2.0"]


# ----------------------------------------------------------------------------- multi-tool reasoning
def test_mandatory_scenario_correlates_all_four_technologies_without_retraining(settings):
    FakeAirflow.triggered = 0
    resp = _agent(settings).ask(SCENARIO)
    modules = {r.tool.split("_")[0] for r in resp.tool_results}
    assert {"mlflow", "dvc", "k8s", "airflow", "assess"} <= modules
    assert all(r.ok for r in resp.tool_results)
    assert "airflow_trigger_dag" not in resp.tools_used and FakeAirflow.triggered == 0
    assert "Retraining is not necessary" in resp.answer
    for section in ("**Answer**", "**Evidence**", "**Uncertainties**"):
        assert section in resp.answer
    assert "data-v2.0" in resp.answer and "0.9709" in resp.answer
    assert resp.grounding_warnings == []


def test_retraining_recommended_only_with_evidence(settings):
    stale = FakeMLflow(settings, champion_md5=V1_MD5, champion_data="data-v1.0")
    resp = _agent(settings, mlflow=stale).ask(SCENARIO)
    assert "Retraining is recommended" in resp.answer
    assert "newer dataset data-v2.0 is available" in resp.answer
    assert "was NOT started automatically" in resp.answer
    assert "airflow_trigger_dag" not in resp.tools_used


# ----------------------------------------------------------------------------- grounding / no invention
def test_agent_does_not_invent_results_when_tools_are_unavailable(settings):
    agent = _agent(settings, dvc=UnavailableModule(FakeDVC(settings)), mlflow=UnavailableModule(FakeMLflow(settings)),
                   airflow=UnavailableModule(FakeAirflow(settings)), k8s=UnavailableModule(FakeK8s(settings)))
    resp = agent.ask(SCENARIO)
    assert resp.tool_results and all(r.status == ToolStatus.UNAVAILABLE for r in resp.tool_results
                                     if r.tool != "assess_retraining_need")
    assert "could not retrieve any operational data" in resp.answer
    assert "0.97" not in resp.answer and "data-v2.0" not in resp.answer
    assert "Retraining is" not in resp.answer
    assert resp.grounding_warnings


class ScriptedLLM:
    """Stands in for Gemini: performs given tool calls through the real executor, then answers."""

    model = "scripted"

    def __init__(self, calls: list[tuple[str, dict[str, Any]]], answer: str | Exception) -> None:
        self.calls, self.answer = calls, answer

    def run(self, question, executor, request_id):
        if isinstance(self.answer, Exception):
            raise self.answer
        results = [executor.execute(name, args, request_id) for name, args in self.calls]
        return self.answer, results

    def reset(self):
        pass


def test_llm_answer_is_replaced_when_every_tool_failed(settings):
    llm = ScriptedLLM([("mlflow_find_best_run", {})], "The best model has F1 0.99 and is deployed and healthy.")
    agent = _agent(settings, planner="gemini", llm=llm, mlflow=UnavailableModule(FakeMLflow(settings)))
    resp = agent.ask("What is the best model?")
    assert "0.99" not in resp.answer
    assert "could not retrieve any operational data" in resp.answer
    assert "mlflow_find_best_run" in resp.answer and "unavailable" in resp.answer


def test_unverified_numbers_in_llm_answer_are_flagged(settings):
    llm = ScriptedLLM([("mlflow_find_best_run", {})], "Best run has F1 0.9709 (97.09%) and recall 0.88.")
    resp = _agent(settings, planner="gemini", llm=llm).ask("best model?")
    assert resp.grounding_warnings == ["unverified values: ['0.88']"]
    assert "Grounding check" in resp.answer


def test_grounding_accepts_values_supplied_by_the_user():
    ok = ToolResult(call_id="c", request_id="r", tool="t", status=ToolStatus.SUCCESS, duration_ms=1, data={"f1": 0.95})
    assert ungrounded_numbers("observed 0.81 vs recorded 0.950", [ok], extra_text="production f1 is 0.81") == []


def test_llm_failure_falls_back_to_the_deterministic_planner(settings):
    llm = ScriptedLLM([], LLMError("429 RESOURCE_EXHAUSTED"))
    resp = _agent(settings, planner="gemini", llm=llm).ask("Which dataset version was used for run 11?")
    assert resp.planner == "rules"
    assert "LLM planner unavailable" in resp.answer
    assert "data-v2.0" in resp.answer


# ----------------------------------------------------------------------------- Gemini function-calling loop
class FakeGenAI:
    """Mimics google-genai: first turn requests a tool, second turn answers from the tool output."""

    def __init__(self):
        from google.genai import types

        self.types = types
        self.turn = 0
        self.seen_tools = 0
        self.models = self

    def generate_content(self, model, contents, config):
        types = self.types
        self.seen_tools = len(config.tools[0].function_declarations)
        self.turn += 1
        if self.turn == 1:
            content = types.Content(role="model", parts=[
                types.Part.from_function_call(name="dvc_get_dataset_version", args={})])
            return NS(candidates=[NS(content=content)],
                      function_calls=[NS(name="dvc_get_dataset_version", args={})], text=None)
        last = contents[-1].parts[0].function_response.response
        text = f"**Answer**\nCurrent dataset is {last['data']['version']}."
        return NS(candidates=[NS(content=types.Content(role="model", parts=[types.Part.from_text(text=text)]))],
                  function_calls=None, text=text)


def test_gemini_planner_executes_tool_calls_through_the_executor(settings):
    registry = build_registry(settings, dvc=FakeDVC(settings), mlflow=FakeMLflow(settings),
                              airflow=FakeAirflow(settings), k8s=FakeK8s(settings))
    fake = FakeGenAI()
    llm = GeminiPlanner("test-key", "gemini-test", registry, client=fake)
    agent = MLOpsAgent(dataclasses.replace(settings), registry=registry, planner="gemini", llm=llm)
    resp = agent.ask("What is the current dataset version?")
    assert resp.planner == "gemini:gemini-test"
    assert resp.tools_used == ["dvc_get_dataset_version"]
    assert "data-v2.0" in resp.answer
    assert fake.seen_tools == len(registry)


def test_partial_outage_never_concludes_no_retraining(settings):
    """Regression: with MLflow down the policy must report insufficient evidence, not 'no retraining needed'."""
    resp = _agent(settings, mlflow=UnavailableModule(FakeMLflow(settings))).ask(SCENARIO)
    assessment = next(r for r in resp.tool_results if r.tool == "assess_retraining_need")
    assert assessment.ok and assessment.data["decision"] == "insufficient_evidence"
    assert assessment.data["retraining_recommended"] is None
    assert "Cannot determine whether retraining is necessary" in resp.answer
    assert "Retraining is not necessary" not in resp.answer


def test_grounding_does_not_misread_identifiers_as_negative_numbers():
    ok = ToolResult(call_id="c", request_id="r", tool="t", status=ToolStatus.SUCCESS, duration_ms=1,
                    data={"run_name": "airflow-random_forest-9.631695+00:00"})
    assert ungrounded_numbers("run airflow-random_forest-9.631695 was compared", [ok]) == []


def test_gemini_quota_exhaustion_switches_to_fallback_model(settings):
    registry = build_registry(settings, dvc=FakeDVC(settings), mlflow=FakeMLflow(settings),
                              airflow=FakeAirflow(settings), k8s=FakeK8s(settings))

    class QuotaError(Exception):
        code = 429

    class Client:
        def __init__(self):
            self.models, self.calls = self, []

        def generate_content(self, model, contents, config):
            from google.genai import types

            self.calls.append(model)
            if model == "primary":
                raise QuotaError("RESOURCE_EXHAUSTED per-day quota")
            text = "**Answer**\nok"
            return NS(candidates=[NS(content=types.Content(role="model", parts=[types.Part.from_text(text=text)]))],
                      function_calls=None, text=text)

    client = Client()
    llm = GeminiPlanner("k", "primary", registry, client=client, fallback_models=["secondary"], retry_backoff=0)
    answer, _ = llm.run("hi", None, "r1")
    assert answer.endswith("ok") and client.calls == ["primary", "secondary"] and llm.model == "secondary"


def test_llm_failure_after_a_state_change_never_repeats_it(settings):
    """Regression: a quota error after the LLM triggered the DAG must not make the fallback trigger it again."""

    class TriggerThenFail:
        model = "scripted"

        def run(self, question, executor, request_id):
            done = [executor.execute("airflow_trigger_dag", {"conf": {"model_type": "svm"}}, request_id)]
            raise LLMError("429 RESOURCE_EXHAUSTED", partial_results=done)

        def reset(self):
            pass

    FakeAirflow.triggered = 0
    resp = _agent(settings, planner="gemini", llm=TriggerThenFail()).ask("Trigger the training pipeline")
    assert FakeAirflow.triggered == 1
    assert resp.tools_used == ["airflow_trigger_dag"]
    assert "without re-executing" in resp.answer and "airflow_trigger_dag" in resp.answer
