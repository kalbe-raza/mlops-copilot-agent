"""Runtime configuration loaded from environment variables (optionally via a local .env file).

Secrets (Gemini API key, Airflow password) are only ever read from the environment and are
never logged; see ``agent.logging_utils`` for the redaction layer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _csv(value: str | None, default: str) -> list[str]:
    raw = value if value not in (None, "") else default
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    # MLflow
    mlflow_tracking_uri: str = "http://127.0.0.1:5000"
    registered_model_name: str = "cancer-risk-classifier"
    # DVC
    dvc_repo: Path = PROJECT_ROOT
    dvc_command_timeout: float = 60.0
    # Airflow
    airflow_base_url: str = "http://127.0.0.1:8080"
    airflow_username: str = "admin"
    airflow_password: str | None = field(default=None, repr=False)
    airflow_approved_dags: list[str] = field(default_factory=lambda: ["ml_training_pipeline"])
    # Kubernetes
    k8s_namespace: str = "mlops-agent"
    k8s_approved_namespaces: list[str] = field(default_factory=lambda: ["mlops-agent"])
    k8s_app_label: str = "app.kubernetes.io/name=inference-api"
    k8s_deployment: str = "inference-api"
    k8s_service: str = "inference-api"
    kubeconfig: str | None = None
    k8s_context: str | None = None
    # LLM
    gemini_api_key: str | None = field(default=None, repr=False)
    gemini_model: str = "gemini-2.5-flash"
    gemini_fallback_models: list[str] = field(
        default_factory=lambda: ["gemini-3-flash-preview", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])
    # Agent behaviour
    tool_timeout_seconds: float = 45.0
    max_agent_steps: int = 12
    log_file: Path = PROJECT_ROOT / "logs" / "agent.jsonl"
    # Retraining policy
    retrain_max_model_age_days: int = 30
    retrain_min_f1: float = 0.93

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Settings:
        load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
        root = Path(_env("MLOPS_PROJECT_ROOT", default=str(PROJECT_ROOT)))
        return cls(
            project_root=root,
            mlflow_tracking_uri=_env("MLFLOW_TRACKING_URI", default="http://127.0.0.1:5000"),
            registered_model_name=_env("MLFLOW_MODEL_NAME", default="cancer-risk-classifier"),
            dvc_repo=Path(_env("DVC_REPO_PATH", default=str(root))),
            dvc_command_timeout=float(_env("DVC_COMMAND_TIMEOUT", default="60")),
            airflow_base_url=_env("AIRFLOW_BASE_URL", default="http://127.0.0.1:8080").rstrip("/"),
            airflow_username=_env("AIRFLOW_USERNAME", default="admin"),
            airflow_password=_env("AIRFLOW_PASSWORD"),
            airflow_approved_dags=_csv(os.getenv("AIRFLOW_APPROVED_DAGS"), "ml_training_pipeline"),
            k8s_namespace=_env("K8S_NAMESPACE", default="mlops-agent"),
            k8s_approved_namespaces=_csv(os.getenv("K8S_APPROVED_NAMESPACES"), "mlops-agent"),
            k8s_app_label=_env("K8S_APP_LABEL", default="app.kubernetes.io/name=inference-api"),
            k8s_deployment=_env("K8S_DEPLOYMENT", default="inference-api"),
            k8s_service=_env("K8S_SERVICE", default="inference-api"),
            kubeconfig=_env("AGENT_KUBECONFIG", "KUBECONFIG"),
            k8s_context=_env("K8S_CONTEXT"),
            gemini_api_key=_env("GEMINI_API_KEY", "gemini_api_key", "GOOGLE_API_KEY"),
            gemini_model=_env("GEMINI_MODEL", default="gemini-2.5-flash"),
            gemini_fallback_models=_csv(os.getenv("GEMINI_FALLBACK_MODELS"),
                                        "gemini-3-flash-preview,gemini-3.5-flash-lite,gemini-3.1-flash-lite"),
            tool_timeout_seconds=float(_env("TOOL_TIMEOUT_SECONDS", default="45")),
            max_agent_steps=int(_env("MAX_AGENT_STEPS", default="12")),
            log_file=Path(_env("AGENT_LOG_FILE", default=str(root / "logs" / "agent.jsonl"))),
            retrain_max_model_age_days=int(_env("RETRAIN_MAX_MODEL_AGE_DAYS", default="30")),
            retrain_min_f1=float(_env("RETRAIN_MIN_F1", default="0.93")),
        )

    def secret_values(self) -> list[str]:
        return [s for s in (self.gemini_api_key, self.airflow_password) if s]
