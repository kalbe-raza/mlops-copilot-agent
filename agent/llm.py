"""Gemini function-calling planner.

The model receives JSON-schema declarations of every registered tool, decides which tools to
call (possibly several in parallel), and the orchestrator executes them through the safe
``ToolExecutor``. Tool results are returned as function responses; the loop ends when the model
answers in text or the step budget is exhausted. Automatic function calling in the SDK is
disabled so every call passes through our validation, confirmation, timeout and logging layer.
"""

from __future__ import annotations

import json
from typing import Any

from agent.schemas import ToolResult
from agent.tools.base import ToolExecutor, ToolRegistry

SYSTEM_PROMPT = """You are MLOps Copilot, an operations agent for a machine-learning platform built on
DVC (dataset versioning), MLflow (experiment tracking + model registry), Apache Airflow 3 (training
pipeline `ml_training_pipeline`) and Kubernetes (FastAPI inference service `inference-api` in
namespace `mlops-agent`).

Hard rules
1. Every operational statement (metric values, versions, run ids, statuses, counts, log lines) MUST
   come from a tool result in this conversation. Never rely on prior knowledge and never guess.
   If a tool returns status unavailable/error/timeout/denied, say so explicitly and do not fill in
   the missing values.
2. Use the minimum set of tools that fully answers every part of the request. Call independent
   tools in parallel. Use results of earlier calls (e.g. a run id) as inputs to later calls.
3. State-changing tools (airflow_trigger_dag, k8s_restart_deployment, k8s_scale_deployment) may only
   be called when the user explicitly asks for that action in the current message. Never start
   retraining on your own initiative - recommend it instead. If such a call is denied, report it.
4. The highest metric does not make a model production-ready: also consider the quality gate,
   train/test F1 gap (overfitting), dataset provenance and registry/deployment status.
5. Recommend retraining only with evidence (freshness policy, failed quality gate, newer dataset
   version, or supplied degradation results). Use assess_retraining_need to apply the policy.
6. Dataset provenance: read the run's recorded tags (mlflow_get_run_dataset_provenance) and confirm
   them against DVC history (dvc_resolve_dataset_reference with the recorded md5).
7. Deployment diagnosis: check unhealthy pods, warning events, service endpoints, deployment
   status and the logs of failing pods before concluding.
8. Pipeline diagnosis: use airflow_get_failed_tasks (and airflow_get_task_logs when needed).

Answer in concise Markdown with these sections:
**Answer** - direct answer in 1-3 sentences.
**Evidence** - bullet points of facts, each ending with the source tool in brackets.
**Uncertainties** - what could not be verified (write "None" if nothing).
**Recommended actions** - only when useful.
"""


class LLMError(RuntimeError):
    """The LLM backend failed (missing key, quota, network); the orchestrator may fall back."""


class GeminiPlanner:
    name = "gemini"

    def __init__(self, api_key: str | None, model: str, registry: ToolRegistry, max_steps: int = 12,
                 client: Any = None) -> None:
        if not api_key and client is None:
            raise LLMError("no Gemini API key configured (GEMINI_API_KEY / gemini_api_key)")
        self.model = model
        self.registry = registry
        self.max_steps = max_steps
        self._client = client
        self._api_key = api_key
        self.history: list[Any] = []

    @property
    def client(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def _config(self) -> Any:
        from google.genai import types

        declarations = [types.FunctionDeclaration(name=s.name, description=s.description
                                                  + (" [STATE-CHANGING]" if s.mutating else ""),
                                                  parameters_json_schema=s.json_schema())
                        for s in self.registry]
        return types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[types.Tool(function_declarations=declarations)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.0,
        )

    def reset(self) -> None:
        self.history = []

    def run(self, question: str, executor: ToolExecutor, request_id: str) -> tuple[str, list[ToolResult]]:
        from google.genai import types

        config = self._config()
        user_turn = types.Content(role="user", parts=[types.Part.from_text(text=question)])
        contents = [*self.history, user_turn]
        results: list[ToolResult] = []
        for _ in range(self.max_steps):
            try:
                response = self.client.models.generate_content(model=self.model, contents=contents, config=config)
            except Exception as exc:  # noqa: BLE001 - surface as LLMError for graceful fallback
                raise LLMError(f"Gemini request failed: {type(exc).__name__}: {str(exc)[:300]}") from exc
            candidate = (response.candidates or [None])[0]
            if candidate is None or candidate.content is None:
                raise LLMError("Gemini returned no candidate (possibly blocked or empty response)")
            calls = response.function_calls or []
            contents.append(candidate.content)
            if not calls:
                text = (response.text or "").strip()
                if not text:
                    raise LLMError("Gemini returned an empty answer")
                # keep only question/answer pairs as conversational memory (never dangling tool calls)
                answer_turn = types.Content(role="model", parts=[types.Part.from_text(text=text)])
                self.history = [*self.history, user_turn, answer_turn][-8:]
                return text, results
            parts = []
            for call in calls:
                result = executor.execute(call.name, dict(call.args or {}), request_id)
                results.append(result)
                payload = json.loads(json.dumps(result.for_llm(), default=str))
                parts.append(types.Part.from_function_response(name=call.name, response=payload))
            contents.append(types.Content(role="user", parts=parts))
        raise LLMError(f"step budget of {self.max_steps} exhausted before a final answer")
