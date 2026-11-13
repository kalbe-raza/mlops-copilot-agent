"""Agent orchestrator: planner selection, tool execution and the grounding guard.

Flow for each request
1. assign a request id and log the request
2. run the planner (Gemini function calling, or the deterministic rule planner); every tool call
   goes through ToolExecutor (validation, policy, confirmation, timeout, structured logging)
3. if the LLM backend fails (quota, network, no key) fall back to the rule planner
4. grounding guard: if every tool call failed, replace the answer with a factual failure report;
   otherwise flag numeric values in the answer that do not appear in any tool output
5. return an AgentResponse containing the answer and the full tool evidence
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

from agent.config import Settings
from agent.llm import GeminiPlanner, LLMError
from agent.logging_utils import get_logger, log_event, register_secrets
from agent.planner_rules import RuleBasedPlanner
from agent.schemas import AgentResponse, ToolResult, ToolStatus
from agent.tools import build_registry
from agent.tools.base import ConfirmCallback, ToolExecutor, ToolRegistry

NUMBER = re.compile(r"(?<![\w.])(\d+\.\d+)(%?)")
REMEDIATION = {
    "mlflow": "start the tracking server (`make mlflow`) and check MLFLOW_TRACKING_URI",
    "airflow": "start Airflow (`make airflow`) and check AIRFLOW_BASE_URL / credentials",
    "kubernetes": "check that the kind cluster is running (`kubectl get nodes`) and the kubeconfig",
    "dvc": "check that the repository path is a Git+DVC repository and dvc is installed",
    "policy": "restore the underlying services listed above",
}


def _numbers_in(text: str) -> list[tuple[float, int, bool]]:
    out = []
    for value, pct in NUMBER.findall(text):
        decimals = len(value.split(".")[1])
        out.append((float(value), decimals, bool(pct)))
    return out


def ungrounded_numbers(answer: str, results: list[ToolResult], extra_text: str = "") -> list[str]:
    """Decimal numbers in the answer that cannot be matched (after rounding) to any tool output.

    ``extra_text`` (the user's question) also counts as a source, so user-supplied values are grounded.
    """
    evidence_text = json.dumps([r.data for r in results if r.ok], default=str) + " " + extra_text
    evidence = [float(v) for v in re.findall(r"-?\d+\.\d+|-?\d+", evidence_text)]
    unmatched = []
    for value, decimals, pct in _numbers_in(answer):
        tol = 0.5 * 10 ** (-decimals) + 1e-9
        checks = [(value, tol)] + ([(value / 100, tol / 100)] if pct else [])
        if not any(abs(c - e) <= t for c, t in checks for e in evidence):
            unmatched.append(f"{value}{'%' if pct else ''}")
    return unmatched


class MLOpsAgent:
    def __init__(self, settings: Settings | None = None, registry: ToolRegistry | None = None,
                 planner: str = "auto", confirm: ConfirmCallback | None = None,
                 llm: GeminiPlanner | None = None, logger: logging.Logger | None = None) -> None:
        self.settings = settings or Settings.from_env()
        register_secrets(self.settings.secret_values())
        self.logger = logger or get_logger(self.settings.log_file)
        self.registry = registry or build_registry(self.settings)
        self.executor = ToolExecutor(self.registry, self.logger, self.settings.tool_timeout_seconds, confirm)
        self.rules = RuleBasedPlanner(self.settings)
        self.llm: GeminiPlanner | None = llm
        if planner not in {"auto", "gemini", "rules"}:
            raise ValueError("planner must be 'auto', 'gemini' or 'rules'")
        if self.llm is None and planner in {"auto", "gemini"}:
            try:
                self.llm = GeminiPlanner(self.settings.gemini_api_key, self.settings.gemini_model, self.registry,
                                         self.settings.max_agent_steps)
            except LLMError:
                if planner == "gemini":
                    raise
        self.planner_mode = planner

    def ask(self, question: str) -> AgentResponse:
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        log_event(self.logger, "request_received", request_id=request_id, question=question[:500],
                  planner=self.planner_mode)
        notes: list[str] = []
        results: list[ToolResult] = []
        planner_name = "rules"
        if self.llm is not None and self.planner_mode != "rules":
            try:
                answer, results = self.llm.run(question, self.executor, request_id)
                planner_name = f"gemini:{self.llm.model}"
            except LLMError as exc:
                log_event(self.logger, "llm_fallback", level=logging.WARNING, request_id=request_id, error=str(exc))
                notes.append(f"LLM planner unavailable ({str(exc)[:120]}); answered with the deterministic planner.")
                answer, more = self.rules.run(question, self.executor, request_id)
                results = results + more
        else:
            answer, results = self.rules.run(question, self.executor, request_id)

        answer, warnings = self._ground(answer, results, question)
        if notes:
            answer = "\n".join(f"> {n}" for n in notes) + "\n\n" + answer
        response = AgentResponse(request_id=request_id, question=question, answer=answer, planner=planner_name,
                                 tool_results=results, grounding_warnings=warnings)
        log_event(self.logger, "request_completed", request_id=request_id, planner=planner_name,
                  tools=[r.tool for r in results], statuses=[r.status.value for r in results],
                  grounding_warnings=warnings, duration_ms=round((time.perf_counter() - started) * 1000, 2))
        return response

    def _ground(self, answer: str, results: list[ToolResult], question: str = "") -> tuple[str, list[str]]:
        if results and not any(r.ok for r in results):
            lines = ["**Answer**", "I could not retrieve any operational data for this request, so I cannot "
                     "report results. No values have been assumed or estimated.", "", "**Tool failures**"]
            for r in results:
                module = r.tool.split("_", 1)[0]
                module = {"k8s": "kubernetes", "assess": "policy", "get": "policy"}.get(module, module)
                hint = REMEDIATION.get(module, "check the service configuration")
                lines.append(f"- `{r.tool}`: **{r.status.value}** - {r.error} (fix: {hint})")
            denied = [r for r in results if r.status == ToolStatus.DENIED]
            if denied and len(denied) == len(results):
                lines[1] = "The requested operation was not performed because it was blocked by a safety policy."
            return "\n".join(lines), ["all tool calls failed; answer replaced with a failure report"]
        unmatched = ungrounded_numbers(answer, results, question)
        if unmatched:
            answer += ("\n\n> Grounding check: these values were not found in any tool output and should be "
                       f"treated as unverified: {', '.join(unmatched)}")
            return answer, [f"unverified values: {unmatched}"]
        return answer, []

    def reset(self) -> None:
        if self.llm is not None:
            self.llm.reset()

    def tool_catalog(self) -> list[dict[str, Any]]:
        return [{"name": s.name, "module": s.module, "mutating": s.mutating, "description": s.description}
                for s in self.registry]
