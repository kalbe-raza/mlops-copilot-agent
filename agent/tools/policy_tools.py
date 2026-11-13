"""Retraining policy: correlates MLflow, DVC, Airflow and Kubernetes evidence deterministically.

Retraining is only *recommended* (never triggered) and only when at least one explicit trigger
fires: the model is older than the freshness policy, its quality gate failed, its F1 is below the
policy floor, a newer DVC dataset version exists than the one it was trained on, or the user
supplied degradation results below the floor. Anything that could not be verified is reported as
an uncertainty rather than assumed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent.config import Settings
from agent.schemas import DagRunsInput, DatasetPathInput, DeploymentInput, NoInput, RetrainingAssessmentInput
from agent.tools.base import ToolError, ToolSpec


class PolicyTools:
    module = "policy"

    def __init__(self, settings: Settings, mlflow: Any, dvc: Any, airflow: Any, k8s: Any) -> None:
        self.settings = settings
        self.mlflow, self.dvc, self.airflow, self.k8s = mlflow, dvc, airflow, k8s

    def get_policy(self, _: NoInput) -> dict[str, Any]:
        return {
            "max_model_age_days": self.settings.retrain_max_model_age_days,
            "min_f1": self.settings.retrain_min_f1,
            "triggers": [
                "model older than max_model_age_days (freshness policy)",
                "champion model quality gate failed or F1 below min_f1",
                "a newer DVC dataset version exists than the one used for training",
                "user-supplied production F1 (degradation result) below min_f1",
            ],
            "non_triggers": [
                "a failed Airflow run alone (fix the pipeline first)",
                "unhealthy Kubernetes pods (operational issue, not a model issue)",
            ],
            "automatic_retraining": False,
        }

    def assess(self, args: RetrainingAssessmentInput) -> dict[str, Any]:
        model_name = args.model_name or self.settings.registered_model_name
        facts: list[str] = []
        uncertainties: list[str] = []
        triggers: list[str] = []
        evidence: dict[str, Any] = {}

        # 1. Champion model from the MLflow registry ---------------------------------------
        champion = None
        try:
            from agent.schemas import ModelRegistryInput, RunIdInput

            reg = self.mlflow.get_model_registry(ModelRegistryInput(model_name=model_name))
            model = reg["registered_models"][0]
            champ_version = model["aliases"].get("champion")
            if champ_version is None:
                uncertainties.append(f"registered model '{model_name}' has no 'champion' alias")
            else:
                champion = next(v for v in model["versions"] if v["version"] == champ_version)
                run = self.mlflow.get_run_details(RunIdInput(run_id=champion["run_id"]))
                evidence["champion"] = {"version": champ_version, "run_id": champion["run_id"],
                                        "run_name": run["run_name"], "metrics": run["metrics"],
                                        "start_time": run["start_time"], "age_days": run["age_days"],
                                        "dataset_version": run["dataset_version"],
                                        "dataset_md5": run["tags"].get("dataset.dvc_md5"),
                                        "quality_gate": run["quality_gate"]}
                facts.append(f"Champion model {model_name} v{champ_version} (run {run['run_name']}) has "
                             f"F1={run['metrics'].get('f1')} and was trained {run['age_days']} days ago on "
                             f"dataset {run['dataset_version']}.")
                if run["age_days"] is not None and run["age_days"] > self.settings.retrain_max_model_age_days:
                    triggers.append(f"model age {run['age_days']}d exceeds freshness policy "
                                    f"({self.settings.retrain_max_model_age_days}d)")
                if run["quality_gate"] == "failed":
                    triggers.append("champion run's quality gate failed")
                f1 = run["metrics"].get("f1")
                if f1 is not None and f1 < self.settings.retrain_min_f1:
                    triggers.append(f"champion F1 {f1} below policy floor {self.settings.retrain_min_f1}")
        except (ToolError, IndexError, StopIteration) as exc:
            uncertainties.append(f"MLflow registry could not be evaluated: {exc}")

        # 2. Data freshness: newer DVC dataset than the champion's training data ----------
        try:
            current = self.dvc.get_dataset_version(DatasetPathInput())
            evidence["current_dataset"] = {k: current[k] for k in ("version", "dvc_md5", "git_commit",
                                                                    "workspace_matches_dvc")}
            facts.append(f"Current DVC dataset is {current['version']} (md5 {current['dvc_md5'][:8]}, "
                         f"commit {current['short_commit']}).")
            champ_md5 = evidence.get("champion", {}).get("dataset_md5")
            if champ_md5 and champ_md5 != current["dvc_md5"]:
                triggers.append(f"newer dataset {current['version']} is available; champion was trained on "
                                f"{evidence['champion']['dataset_version']}")
            elif champ_md5:
                facts.append("Champion was trained on the current dataset version.")
            if not current["workspace_matches_dvc"]:
                uncertainties.append("workspace dataset differs from its committed DVC version (uncommitted change)")
        except ToolError as exc:
            uncertainties.append(f"DVC dataset version could not be read: {exc}")

        # 3. Supplied degradation results ---------------------------------------------------
        if args.observed_f1 is not None:
            facts.append(f"User-supplied production F1 = {args.observed_f1}.")
            if args.observed_f1 < self.settings.retrain_min_f1:
                triggers.append(f"supplied production F1 {args.observed_f1} below floor {self.settings.retrain_min_f1}")
        else:
            uncertainties.append("no production performance (degradation) results were supplied")

        # 4. Airflow: recent training workflow status --------------------------------------
        try:
            dag_id = self.settings.airflow_approved_dags[0]
            runs = self.airflow.get_dag_runs(DagRunsInput(dag_id=dag_id, limit=5))
            evidence["airflow_recent_runs"] = runs["runs"]
            if runs["runs"]:
                latest = runs["runs"][0]
                facts.append(f"Latest '{dag_id}' run {latest['dag_run_id']} is '{latest['state']}'; "
                             f"last 5 runs: {runs['state_counts']}.")
                if latest["state"] == "failed":
                    uncertainties.append("the most recent training run failed, so no newer candidate is available "
                                         "until the pipeline is fixed")
            else:
                uncertainties.append(f"DAG '{dag_id}' has no recorded runs")
        except ToolError as exc:
            uncertainties.append(f"Airflow status could not be retrieved: {exc}")

        # 5. Kubernetes: what is actually serving -------------------------------------------
        try:
            dep = self.k8s.get_deployed_version(DeploymentInput())
            live = dep["live_version_endpoint"]
            served = live.get("model", {}).get("version") if isinstance(live, dict) else None
            evidence["deployment"] = {"image": dep["image"], "replicas": dep["replicas"], "served_model_version":
                                      served, "app_version": dep["app_version_label"]}
            facts.append(f"Kubernetes deployment runs image {dep['image']} with "
                         f"{dep['replicas']['ready']}/{dep['replicas']['desired']} ready replicas"
                         + (f", serving model version {served}." if served else "."))
            if served is None:
                uncertainties.append("served model version could not be read from the live /version endpoint")
            elif champion is not None and str(served) != str(champion["version"]):
                facts.append(f"Deployed model version {served} differs from registry champion v{champion['version']}"
                             " (a redeploy, not retraining, would close this gap).")
        except ToolError as exc:
            uncertainties.append(f"Kubernetes deployment could not be inspected: {exc}")

        return {
            "model_name": model_name,
            "retraining_recommended": bool(triggers),
            "triggers": triggers,
            "facts": facts,
            "uncertainties": uncertainties,
            "policy": self.get_policy(NoInput()),
            "evidence": evidence,
            "assessed_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
            "note": "Assessment only: retraining is never started automatically by this tool.",
        }

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec("get_retraining_policy", "The explicit retraining policy (freshness window, F1 floor, what "
                     "does and does not justify retraining).", NoInput, self.get_policy, self.module),
            ToolSpec("assess_retraining_need", "Correlate MLflow registry, DVC data version, Airflow runs and the "
                     "Kubernetes deployment against the retraining policy. Returns facts, uncertainties and "
                     "whether retraining is recommended. Never starts training.",
                     RetrainingAssessmentInput, self.assess, self.module, timeout=90),
        ]
