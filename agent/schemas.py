"""Structured input/output contracts for every agent tool.

Inputs are Pydantic models: they are validated before a tool runs and are converted into the
JSON-schema function declarations given to the LLM. Every tool returns a ``ToolResult``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DATASET_PATH = "data/raw/dataset.csv"


class ToolStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"  # the service answered but the operation failed (e.g. not found)
    UNAVAILABLE = "unavailable"  # the service could not be reached
    TIMEOUT = "timeout"
    INVALID_REQUEST = "invalid_request"  # unknown tool or arguments failed validation
    DENIED = "denied"  # blocked by a safety policy or missing user confirmation


class ToolResult(BaseModel):
    call_id: str
    request_id: str
    tool: str
    status: ToolStatus
    duration_ms: float
    data: Any = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == ToolStatus.SUCCESS

    def for_llm(self) -> dict[str, Any]:
        """Compact representation returned to the model as the function response."""
        payload: dict[str, Any] = {"status": self.status.value}
        if self.ok:
            payload["data"] = self.data
        else:
            payload["error"] = self.error
            payload["note"] = "No data was retrieved. Do not invent values for this tool."
        return payload


class AgentResponse(BaseModel):
    request_id: str
    question: str
    answer: str
    planner: str
    tool_results: list[ToolResult] = Field(default_factory=list)
    grounding_warnings: list[str] = Field(default_factory=list)

    @property
    def tools_used(self) -> list[str]:
        return [r.tool for r in self.tool_results]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoInput(_Input):
    pass


# ----------------------------------------------------------------------------- DVC
class DatasetPathInput(_Input):
    path: str = Field(DATASET_PATH, description="Repository-relative path of the DVC-tracked dataset.")


class CompareDatasetVersionsInput(_Input):
    rev_a: str = Field(..., description="Older Git revision or data tag, e.g. 'data-v1.0'.")
    rev_b: str = Field("HEAD", description="Newer Git revision or data tag, e.g. 'data-v2.0' or 'HEAD'.")
    path: str = Field(DATASET_PATH, description="Repository-relative path of the DVC-tracked dataset.")


class ResolveDatasetInput(_Input):
    md5: str | None = Field(None, description="DVC md5 hash recorded for a training run.")
    git_commit: str | None = Field(None, description="Git commit recorded for the dataset of a training run.")
    path: str = Field(DATASET_PATH, description="Repository-relative path of the DVC-tracked dataset.")


class DatasetRevisionInput(_Input):
    rev: str = Field(..., description="Git revision or data tag to reproduce, e.g. 'data-v1.0'.")
    path: str = Field(DATASET_PATH, description="Repository-relative path of the DVC-tracked dataset.")


# ----------------------------------------------------------------------------- MLflow
class ListRunsInput(_Input):
    experiment_names: list[str] | None = Field(None, description="Experiments to search; all if omitted.")
    max_results: int = Field(10, ge=1, le=100, description="Maximum number of runs, newest first.")
    filter_string: str | None = Field(None, description="Optional MLflow search filter, e.g. \"metrics.f1 > 0.9\".")


class RunIdInput(_Input):
    run_id: str = Field(..., min_length=1, description="MLflow run id (or exact run name).")


class BestRunInput(_Input):
    metric: str = Field("f1", description="Evaluation metric to rank by, e.g. f1, recall, roc_auc.")
    experiment_names: list[str] | None = Field(None, description="Experiments to search; all if omitted.")
    recent_n: int = Field(25, ge=1, le=200, description="Only consider the N most recent runs.")
    mode: Literal["max", "min"] = Field("max", description="Whether higher (max) or lower (min) is better.")


class CompareRunsInput(_Input):
    run_ids: list[str] | None = Field(None, description="Explicit run ids to compare.")
    last_n: int | None = Field(None, ge=2, le=50, description="Compare the N most recent runs instead.")
    experiment_names: list[str] | None = Field(None, description="Restrict last_n to these experiments.")
    metric: str = Field("f1", description="Primary metric used to recommend a candidate.")

    @field_validator("run_ids")
    @classmethod
    def _at_least_two(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(v) < 2:
            raise ValueError("provide at least two run ids to compare")
        return v


class CompareExperimentsInput(_Input):
    experiment_names: list[str] = Field(..., min_length=2, description="Two or more experiment names.")
    metric: str = Field("f1", description="Metric to aggregate.")


class ModelRegistryInput(_Input):
    model_name: str | None = Field(None, description="Registered model name; lists all models if omitted.")


# ----------------------------------------------------------------------------- Airflow
class DagIdInput(_Input):
    dag_id: str = Field("ml_training_pipeline", description="Airflow DAG id.")


class DagRunsInput(_Input):
    dag_id: str = Field("ml_training_pipeline", description="Airflow DAG id.")
    limit: int = Field(5, ge=1, le=50, description="Number of most recent runs.")


class TriggerDagInput(_Input):
    dag_id: str = Field("ml_training_pipeline", description="Approved training DAG id.")
    conf: dict[str, Any] = Field(default_factory=dict,
                                 description="Run configuration, e.g. {\"model_type\": \"random_forest\"}.")
    wait_for_completion: bool = Field(True, description="Poll until the run finishes and report task states.")
    timeout_seconds: int = Field(600, ge=10, le=1800, description="Maximum time to wait for completion.")


class DagRunInput(_Input):
    dag_id: str = Field("ml_training_pipeline", description="Airflow DAG id.")
    dag_run_id: str | None = Field(None, description="DAG run id; the most recent run if omitted.")


class TaskLogsInput(_Input):
    dag_id: str = Field("ml_training_pipeline", description="Airflow DAG id.")
    dag_run_id: str | None = Field(None, description="DAG run id; the most recent run if omitted.")
    task_id: str = Field(..., description="Task id, e.g. validate_data.")
    try_number: int | None = Field(None, ge=1, description="Attempt number; the last attempt if omitted.")
    tail_lines: int = Field(40, ge=5, le=400, description="Number of log lines to return from the end.")


# ----------------------------------------------------------------------------- Kubernetes
class NamespaceInput(_Input):
    namespace: str | None = Field(None, description="Kubernetes namespace; the approved default if omitted.")


class PodsInput(_Input):
    namespace: str | None = Field(None, description="Kubernetes namespace; the approved default if omitted.")
    label_selector: str | None = Field(None, description="Label selector; the inference app if omitted.")


class PodLogsInput(_Input):
    pod_name: str = Field(..., description="Pod name.")
    namespace: str | None = Field(None, description="Kubernetes namespace; the approved default if omitted.")
    tail_lines: int = Field(40, ge=1, le=500, description="Number of log lines.")
    previous: bool = Field(False, description="Logs of the previous (crashed) container instance.")


class EventsInput(_Input):
    namespace: str | None = Field(None, description="Kubernetes namespace; the approved default if omitted.")
    warnings_only: bool = Field(True, description="Only return Warning events.")
    limit: int = Field(25, ge=1, le=200, description="Maximum number of events, newest first.")


class DeploymentInput(_Input):
    namespace: str | None = Field(None, description="Kubernetes namespace; the approved default if omitted.")
    deployment: str | None = Field(None, description="Deployment name; the inference API if omitted.")


class ScaleInput(_Input):
    namespace: str | None = Field(None, description="Kubernetes namespace; the approved default if omitted.")
    deployment: str | None = Field(None, description="Deployment name; the inference API if omitted.")
    replicas: int = Field(..., ge=0, le=5, description="Desired replica count (0-5).")


# ----------------------------------------------------------------------------- Policy
class RetrainingAssessmentInput(_Input):
    model_name: str | None = Field(None, description="Registered model name; the configured model if omitted.")
    observed_f1: float | None = Field(None, ge=0, le=1,
                                      description="Optional F1 measured on recent production data (degradation).")
