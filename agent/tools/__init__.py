"""Tool modules exposed to the agent and the registry factory."""

from __future__ import annotations

from typing import Any

from agent.config import Settings
from agent.tools.airflow_tools import AirflowTools
from agent.tools.base import ToolExecutor, ToolRegistry, ToolSpec
from agent.tools.dvc_tools import DVCTools
from agent.tools.kubernetes_tools import KubernetesTools
from agent.tools.mlflow_tools import MLflowTools
from agent.tools.policy_tools import PolicyTools


def build_registry(settings: Settings, *, dvc: Any = None, mlflow: Any = None, airflow: Any = None,
                   k8s: Any = None) -> ToolRegistry:
    """Create all tool modules (dependencies can be injected for tests) and register their tools."""
    dvc = dvc or DVCTools(settings)
    mlflow = mlflow or MLflowTools(settings)
    airflow = airflow or AirflowTools(settings)
    k8s = k8s or KubernetesTools(settings)
    policy = PolicyTools(settings, mlflow=mlflow, dvc=dvc, airflow=airflow, k8s=k8s)
    registry = ToolRegistry()
    for module in (dvc, mlflow, airflow, k8s, policy):
        for spec in module.specs():
            registry.register(spec)
    return registry


__all__ = ["ToolExecutor", "ToolRegistry", "ToolSpec", "build_registry"]
