"""Shared fixtures. No test requires a live MLflow/Airflow/Kubernetes service: external systems are
mocked (httpx MockTransport, fake Kubernetes clients) or replaced by local temporary instances
(SQLite-backed MLflow, throw-away Git+DVC repositories)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

from agent.config import Settings  # noqa: E402
from agent.schemas import NoInput  # noqa: E402
from agent.tools.base import ToolExecutor, ToolRegistry, ToolSpec, ToolUnavailableError  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=ROOT,
        mlflow_tracking_uri=f"sqlite:///{tmp_path / 'mlflow.db'}",
        dvc_repo=ROOT,
        airflow_base_url="http://airflow.test",
        airflow_username="agent",
        airflow_password="s3cr3t-airflow-pass",
        gemini_api_key=None,
        tool_timeout_seconds=5.0,
        log_file=tmp_path / "agent.jsonl",
    )


@pytest.fixture
def logger() -> logging.Logger:
    log = logging.getLogger("mlops_agent")
    log.setLevel(logging.INFO)
    return log


def make_executor(specs: list[ToolSpec], logger: logging.Logger, confirm=None, timeout: float = 5.0) -> ToolExecutor:
    return ToolExecutor(ToolRegistry(specs), logger, timeout, confirm)


class UnavailableModule:
    """A tool module whose every method fails as if the backing service were down."""

    def __init__(self, module: Any) -> None:
        self._module = module

    def specs(self) -> list[ToolSpec]:
        specs = []
        for spec in self._module.specs():
            def boom(_args, _name=spec.name):
                raise ToolUnavailableError(f"{_name}: connection refused")

            specs.append(ToolSpec(spec.name, spec.description, spec.input_model, boom, spec.module, spec.mutating,
                                  spec.authorize))
        return specs

    def __getattr__(self, item: str) -> Any:
        if item.startswith("_"):
            raise AttributeError(item)

        def boom(*_a, **_k):
            raise ToolUnavailableError(f"{item}: connection refused")

        return boom


__all__ = ["NoInput", "UnavailableModule", "make_executor"]
