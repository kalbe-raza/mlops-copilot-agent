"""Executor: validation, timeouts, failure classification, confirmation and structured logging."""

from __future__ import annotations

import json
import logging
import time

from pydantic import BaseModel, ConfigDict, Field

from agent.logging_utils import JsonFormatter, register_secrets
from agent.schemas import NoInput, ToolStatus
from agent.tools.base import ToolError, ToolSpec, ToolUnavailableError
from tests.conftest import make_executor


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")  # same policy as every real tool input schema
    text: str = Field(..., min_length=1)
    count: int = Field(1, ge=1, le=3)


def test_successful_call_returns_data_and_logs_structured_record(logger, caplog):
    spec = ToolSpec("echo", "echo text", EchoInput, lambda a: {"echo": a.text * a.count}, "test")
    executor = make_executor([spec], logger)
    with caplog.at_level(logging.INFO, logger="mlops_agent"):
        result = executor.execute("echo", {"text": "hi", "count": 2}, request_id="req-123")
    assert result.status == ToolStatus.SUCCESS
    assert result.data == {"echo": "hihi"}
    record = next(r for r in caplog.records if r.getMessage() == "tool_call")
    fields = record.fields
    assert fields["tool"] == "echo"
    assert fields["status"] == "success"
    assert fields["request_id"] == "req-123"
    assert isinstance(fields["duration_ms"], float)


def test_unknown_tool_is_rejected_as_invalid_request(logger):
    result = make_executor([], logger).execute("drop_database", {}, "r1")
    assert result.status == ToolStatus.INVALID_REQUEST
    assert "unknown tool" in result.error


def test_invalid_arguments_are_rejected_before_the_tool_runs(logger):
    calls = []
    spec = ToolSpec("echo", "echo", EchoInput, lambda a: calls.append(a), "test")
    executor = make_executor([spec], logger)
    for bad in ({}, {"text": ""}, {"text": "x", "count": 99}, {"text": "x", "unexpected": 1}):
        result = executor.execute("echo", bad, "r1")
        assert result.status == ToolStatus.INVALID_REQUEST, bad
    assert calls == []


def test_tool_timeout_is_reported_not_raised(logger):
    def slow(_):
        time.sleep(2)
        return {"late": True}

    executor = make_executor([ToolSpec("slow", "slow", NoInput, slow, "test")], logger, timeout=0.2)
    started = time.perf_counter()
    result = executor.execute("slow", {}, "r1")
    assert result.status == ToolStatus.TIMEOUT
    assert result.data is None
    assert time.perf_counter() - started < 1.5


def test_per_tool_timeout_override(logger):
    def slow(_):
        time.sleep(0.4)
        return "ok"

    spec = ToolSpec("slow", "slow", NoInput, slow, "test", timeout=1.0)
    result = make_executor([spec], logger, timeout=0.1).execute("slow", {}, "r1")
    assert result.status == ToolStatus.SUCCESS


def test_service_failures_are_classified(logger):
    def down(_):
        raise ToolUnavailableError("connection refused")

    def not_found(_):
        raise ToolError("run not found")

    def crash(_):
        raise KeyError("boom")

    executor = make_executor([ToolSpec("down", "", NoInput, down, "t"), ToolSpec("nf", "", NoInput, not_found, "t"),
                              ToolSpec("crash", "", NoInput, crash, "t")], logger)
    assert executor.execute("down", {}, "r").status == ToolStatus.UNAVAILABLE
    assert executor.execute("nf", {}, "r").status == ToolStatus.ERROR
    crashed = executor.execute("crash", {}, "r")
    assert crashed.status == ToolStatus.ERROR and "KeyError" in crashed.error


def test_mutating_tool_requires_confirmation(logger):
    calls = []
    spec = ToolSpec("restart", "restart", NoInput, lambda a: calls.append(1) or "done", "k8s", mutating=True)
    denied = make_executor([spec], logger, confirm=None).execute("restart", {}, "r")
    assert denied.status == ToolStatus.DENIED and calls == []
    rejected = make_executor([spec], logger, confirm=lambda s, a: False).execute("restart", {}, "r")
    assert rejected.status == ToolStatus.DENIED and calls == []

    def broken_prompt(spec, args):
        raise RuntimeError("tty closed")

    broken = make_executor([spec], logger, confirm=broken_prompt).execute("restart", {}, "r")
    assert broken.status == ToolStatus.DENIED and calls == []
    approved = make_executor([spec], logger, confirm=lambda s, a: True).execute("restart", {}, "r")
    assert approved.status == ToolStatus.SUCCESS and calls == [1]


def test_authorization_hook_runs_before_confirmation(logger):
    asked = []
    spec = ToolSpec("restart", "restart", NoInput, lambda a: "done", "k8s", mutating=True,
                    authorize=lambda a: "namespace not approved")
    result = make_executor([spec], logger, confirm=lambda s, a: asked.append(1) or True).execute("restart", {}, "r")
    assert result.status == ToolStatus.DENIED
    assert asked == []  # the user is never even asked for a forbidden operation


def test_structured_logs_never_contain_secrets(logger, caplog):
    secret = "AIzaSyD-very-secret-gemini-key-1234567890"
    register_secrets([secret])

    class LoginInput(BaseModel):
        username: str
        password: str
        note: str = ""

    spec = ToolSpec("login", "", LoginInput, lambda a: {"ok": True, "echo": f"used {secret}"}, "t")
    executor = make_executor([spec], logger)
    with caplog.at_level(logging.INFO, logger="mlops_agent"):
        executor.execute("login", {"username": "agent", "password": "hunter2-pw", "note": f"Bearer {secret}"}, "r9")
    formatter = JsonFormatter()
    lines = [formatter.format(r) for r in caplog.records]
    text = "\n".join(lines)
    assert "hunter2-pw" not in text
    assert secret not in text
    payload = json.loads(lines[-1])
    assert payload["arguments"]["password"] == "***REDACTED***"
    assert payload["tool"] == "login" and payload["request_id"] == "r9" and "duration_ms" in payload
