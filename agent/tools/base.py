"""Tool abstraction, registry and the safe executor every tool call goes through.

The executor is the single choke point that enforces:
  * input validation against the tool's Pydantic schema
  * authorization policies (approved namespaces / DAGs) *before* anything runs
  * explicit user confirmation for state-changing tools
  * a hard timeout per call
  * error classification (unavailable / timeout / error / denied / invalid_request)
  * one structured log line per call (tool, status, duration, request id; secrets redacted)
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from agent.logging_utils import log_event
from agent.schemas import ToolResult, ToolStatus


class ToolError(Exception):
    """Operation failed although the service responded (not found, bad state ...)."""


class ToolUnavailableError(ToolError):
    """The backing service could not be reached (connection refused, DNS, no kubeconfig ...)."""


class ToolPermissionError(ToolError):
    """Blocked by a safety policy."""


ConfirmCallback = Callable[["ToolSpec", dict[str, Any]], bool]
AuthorizeHook = Callable[[BaseModel], str | None]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    func: Callable[[BaseModel], Any]
    module: str
    mutating: bool = False
    authorize: AuthorizeHook | None = None
    timeout: float | Callable[[BaseModel], float] | None = None  # overrides the executor default
    tags: list[str] = field(default_factory=list)

    def timeout_for(self, parsed: BaseModel, default: float) -> float:
        if callable(self.timeout):
            return float(self.timeout(parsed))
        return float(self.timeout or default)

    def json_schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        return _clean_schema(schema)


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline $defs and drop keys that function-calling APIs reject."""
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            out = {}
            for k, v in node.items():
                if k in {"title", "additionalProperties"} or (k == "default" and v is None):
                    continue
                if k == "anyOf":
                    non_null = [s for s in v if s.get("type") != "null"]
                    if len(non_null) == 1:  # Optional[X] -> X (optional fields are simply not required)
                        out.update(walk(non_null[0]))
                        continue
                out[k] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    cleaned = walk(schema)
    cleaned.setdefault("type", "object")
    cleaned.setdefault("properties", {})
    return cleaned


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec] | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for spec in specs or []:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool name {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def by_module(self) -> dict[str, list[ToolSpec]]:
        grouped: dict[str, list[ToolSpec]] = {}
        for spec in self._tools.values():
            grouped.setdefault(spec.module, []).append(spec)
        return grouped


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, logger: logging.Logger, timeout_seconds: float = 45.0,
                 confirm: ConfirmCallback | None = None) -> None:
        self.registry = registry
        self.logger = logger
        self.timeout_seconds = timeout_seconds
        self.confirm = confirm

    def execute(self, name: str, arguments: dict[str, Any] | None, request_id: str) -> ToolResult:
        call_id = uuid.uuid4().hex[:10]
        started = time.perf_counter()
        arguments = dict(arguments or {})
        spec = self.registry.get(name)

        def finish(status: ToolStatus, data: Any = None, error: str | None = None) -> ToolResult:
            duration = round((time.perf_counter() - started) * 1000, 2)
            result = ToolResult(call_id=call_id, request_id=request_id, tool=name, status=status,
                                duration_ms=duration, data=data, error=error)
            log_event(self.logger, "tool_call",
                      level=logging.INFO if status == ToolStatus.SUCCESS else logging.WARNING,
                      request_id=request_id, call_id=call_id, tool=name,
                      module=spec.module if spec else None, status=status.value, duration_ms=duration,
                      mutating=bool(spec and spec.mutating), arguments=arguments, error=error)
            return result

        if spec is None:
            return finish(ToolStatus.INVALID_REQUEST, error=f"unknown tool '{name}'")
        try:
            parsed = spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors())
            return finish(ToolStatus.INVALID_REQUEST, error=f"invalid arguments: {problems}")

        if spec.authorize is not None:
            reason = spec.authorize(parsed)
            if reason:
                return finish(ToolStatus.DENIED, error=reason)
        if spec.mutating:
            approved = False
            if self.confirm is not None:
                try:
                    approved = bool(self.confirm(spec, parsed.model_dump()))
                except Exception:  # noqa: BLE001 - a broken prompt must never count as approval
                    approved = False
            if not approved:
                return finish(ToolStatus.DENIED,
                              error=f"'{name}' changes system state and requires explicit user confirmation; "
                                    "the operation was not executed")

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{name}")
        future = pool.submit(spec.func, parsed)
        timeout = spec.timeout_for(parsed, self.timeout_seconds)
        try:
            data = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return finish(ToolStatus.TIMEOUT, error=f"'{name}' did not finish within {timeout:g}s")
        except ToolPermissionError as exc:
            return finish(ToolStatus.DENIED, error=str(exc))
        except ToolUnavailableError as exc:
            return finish(ToolStatus.UNAVAILABLE, error=str(exc))
        except ToolError as exc:
            return finish(ToolStatus.ERROR, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - classify anything unexpected as a tool error
            return finish(ToolStatus.ERROR, error=f"{type(exc).__name__}: {exc}")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return finish(ToolStatus.SUCCESS, data=data)
