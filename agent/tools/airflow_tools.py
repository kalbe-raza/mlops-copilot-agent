"""Apache Airflow 3 workflow tools using the authenticated stable REST API (``/api/v2``).

Authentication: a JWT is obtained from ``POST /auth/token`` with the configured credentials and
sent as a Bearer token; it is refreshed once on HTTP 401. Credentials come only from environment
variables and are never logged.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import httpx

from agent.config import Settings
from agent.schemas import DagIdInput, DagRunInput, DagRunsInput, NoInput, TaskLogsInput, TriggerDagInput
from agent.tools.base import ToolError, ToolSpec, ToolUnavailableError

TERMINAL_STATES = {"success", "failed"}
FAILED_TASK_STATES = {"failed", "upstream_failed", "up_for_retry"}
ALLOWED_CONF_KEYS = {"model_type", "params", "experiment", "run_name"}
ALLOWED_MODEL_TYPES = {"logistic_regression", "random_forest", "gradient_boosting", "svm", "decision_stump"}
ERROR_MARKERS = ("error", "failed", "traceback", "exception", "raise ")


class AirflowClient:
    def __init__(self, base_url: str, username: str, password: str | None, timeout: float = 15.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self._password = password
        self._token: str | None = None
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)

    def _login(self) -> str:
        if not self._password:
            raise ToolError("AIRFLOW_PASSWORD is not configured; cannot authenticate to the Airflow API")
        try:
            resp = self._http.post("/auth/token", json={"username": self.username, "password": self._password})
        except httpx.TransportError as exc:
            raise ToolUnavailableError(f"Airflow API unreachable at {self.base_url}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ToolError("Airflow authentication failed (check AIRFLOW_USERNAME / AIRFLOW_PASSWORD)")
        if resp.status_code >= 400:
            raise ToolError(f"Airflow token endpoint returned HTTP {resp.status_code}")
        token = resp.json().get("access_token")
        if not token:
            raise ToolError("Airflow token endpoint returned no access_token")
        self._token = token
        return token

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(2):
            token = self._token or self._login()
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            try:
                resp = self._http.request(method, path, headers=headers, **kwargs)
            except httpx.TimeoutException as exc:
                raise ToolUnavailableError(f"Airflow API timed out ({method} {path})") from exc
            except httpx.TransportError as exc:
                raise ToolUnavailableError(f"Airflow API unreachable at {self.base_url}: {exc}") from exc
            if resp.status_code == 401 and attempt == 0:
                self._token = None
                continue
            if resp.status_code == 404:
                raise ToolError(f"not found: {method} {path}")
            if resp.status_code >= 500:
                raise ToolUnavailableError(f"Airflow API server error HTTP {resp.status_code} on {path}")
            if resp.status_code >= 400:
                detail = resp.text[:300]
                raise ToolError(f"Airflow API HTTP {resp.status_code} on {method} {path}: {detail}")
            return resp.json() if resp.content else {}
        raise ToolError("Airflow authentication failed after token refresh")


def _q(value: str) -> str:
    return quote(value, safe="")


class AirflowTools:
    module = "airflow"

    def __init__(self, settings: Settings, client: AirflowClient | None = None, poll_interval: float = 5.0) -> None:
        self.settings = settings
        self._client = client
        self.poll_interval = poll_interval

    @property
    def client(self) -> AirflowClient:
        if self._client is None:
            self._client = AirflowClient(self.settings.airflow_base_url, self.settings.airflow_username,
                                         self.settings.airflow_password)
        return self._client

    # ------------------------------------------------------------------ helpers
    def _runs(self, dag_id: str, limit: int) -> list[dict[str, Any]]:
        data = self.client.request("GET", f"/api/v2/dags/{_q(dag_id)}/dagRuns",
                                   params={"order_by": "-run_after", "limit": limit})
        return [self._run_summary(r) for r in data.get("dag_runs", [])]

    @staticmethod
    def _run_summary(r: dict[str, Any]) -> dict[str, Any]:
        return {"dag_run_id": r.get("dag_run_id"), "state": r.get("state"), "run_type": r.get("run_type"),
                "run_after": r.get("run_after"), "start_date": r.get("start_date"), "end_date": r.get("end_date"),
                "duration_s": r.get("duration"), "conf": r.get("conf"), "note": r.get("note")}

    def _resolve_run_id(self, dag_id: str, dag_run_id: str | None) -> str:
        if dag_run_id:
            return dag_run_id
        runs = self._runs(dag_id, 1)
        if not runs:
            raise ToolError(f"DAG '{dag_id}' has no runs yet")
        return runs[0]["dag_run_id"]

    def _task_instances(self, dag_id: str, run_id: str) -> list[dict[str, Any]]:
        data = self.client.request("GET", f"/api/v2/dags/{_q(dag_id)}/dagRuns/{_q(run_id)}/taskInstances")
        tis = [{"task_id": t.get("task_id"), "state": t.get("state"), "try_number": t.get("try_number"),
                "max_tries": t.get("max_tries"), "start_date": t.get("start_date"), "end_date": t.get("end_date"),
                "duration_s": round(t["duration"], 2) if t.get("duration") is not None else None,
                "operator": t.get("operator")} for t in data.get("task_instances", [])]
        return sorted(tis, key=lambda t: (t["start_date"] or "9999", t["task_id"]))

    def _log_lines(self, dag_id: str, run_id: str, task_id: str, try_number: int) -> list[str]:
        data = self.client.request(
            "GET", f"/api/v2/dags/{_q(dag_id)}/dagRuns/{_q(run_id)}/taskInstances/{_q(task_id)}/logs/{try_number}",
            params={"full_content": "true"})
        lines: list[str] = []
        content = data.get("content", [])
        if isinstance(content, str):
            content = content.splitlines()
        for entry in content:
            if isinstance(entry, dict):
                ts = (entry.get("timestamp") or "")[:19]
                level = (entry.get("level") or "").upper()
                event = entry.get("event", "")
                lines.append(" ".join(x for x in (ts, level, str(event)) if x))
            else:
                lines.extend(str(entry).splitlines())
        return lines

    # ------------------------------------------------------------------ tools
    def list_dags(self, _: NoInput) -> dict[str, Any]:
        data = self.client.request("GET", "/api/v2/dags", params={"limit": 100})
        dags = [{"dag_id": d.get("dag_id"), "description": d.get("description"), "is_paused": d.get("is_paused"),
                 "tags": [t.get("name") for t in d.get("tags", [])], "schedule": d.get("timetable_summary"),
                 "approved_for_trigger": d.get("dag_id") in self.settings.airflow_approved_dags}
                for d in data.get("dags", [])]
        return {"dags": dags, "count": len(dags)}

    def get_dag_runs(self, args: DagRunsInput) -> dict[str, Any]:
        runs = self._runs(args.dag_id, args.limit)
        states: dict[str, int] = {}
        for r in runs:
            states[r["state"]] = states.get(r["state"], 0) + 1
        return {"dag_id": args.dag_id, "runs": runs, "state_counts": states,
                "latest_state": runs[0]["state"] if runs else None}

    def get_task_instances(self, args: DagRunInput) -> dict[str, Any]:
        run_id = self._resolve_run_id(args.dag_id, args.dag_run_id)
        run = self.client.request("GET", f"/api/v2/dags/{_q(args.dag_id)}/dagRuns/{_q(run_id)}")
        return {"dag_id": args.dag_id, "dag_run": self._run_summary(run),
                "task_instances": self._task_instances(args.dag_id, run_id)}

    def get_failed_tasks(self, args: DagRunInput) -> dict[str, Any]:
        if args.dag_run_id:
            run_id = args.dag_run_id
        else:
            failed = [r for r in self._runs(args.dag_id, 25) if r["state"] == "failed"]
            if not failed:
                return {"dag_id": args.dag_id, "failed_run_found": False,
                        "message": "none of the 25 most recent runs failed"}
            run_id = failed[0]["dag_run_id"]
        tis = self._task_instances(args.dag_id, run_id)
        failed_tasks = []
        for t in tis:
            if t["state"] not in FAILED_TASK_STATES:
                continue
            entry = dict(t)
            if t["state"] != "upstream_failed" and t.get("try_number"):
                try:
                    lines = self._log_lines(args.dag_id, run_id, t["task_id"], int(t["try_number"]))
                    entry["error_lines"] = [ln for ln in lines if any(m in ln.lower() for m in ERROR_MARKERS)][-8:]
                except ToolError as exc:
                    entry["error_lines"] = [f"log unavailable: {exc}"]
            else:
                entry["note"] = "did not run because an upstream task failed"
            failed_tasks.append(entry)
        root = next((t for t in failed_tasks if t["state"] == "failed"), None)
        return {"dag_id": args.dag_id, "dag_run_id": run_id, "failed_run_found": True,
                "failed_tasks": failed_tasks, "root_cause_task": root["task_id"] if root else None,
                "skipped_downstream": [t["task_id"] for t in tis if t["state"] in ("upstream_failed", "skipped")]}

    def get_task_logs(self, args: TaskLogsInput) -> dict[str, Any]:
        run_id = self._resolve_run_id(args.dag_id, args.dag_run_id)
        try_number = args.try_number
        if try_number is None:
            ti = self.client.request(
                "GET", f"/api/v2/dags/{_q(args.dag_id)}/dagRuns/{_q(run_id)}/taskInstances/{_q(args.task_id)}")
            try_number = max(int(ti.get("try_number") or 1), 1)
        lines = self._log_lines(args.dag_id, run_id, args.task_id, try_number)
        return {"dag_id": args.dag_id, "dag_run_id": run_id, "task_id": args.task_id, "try_number": try_number,
                "total_lines": len(lines), "tail": lines[-args.tail_lines:]}

    def trigger_dag(self, args: TriggerDagInput) -> dict[str, Any]:
        dag = self.client.request("GET", f"/api/v2/dags/{_q(args.dag_id)}")
        unpaused = False
        if dag.get("is_paused"):
            self.client.request("PATCH", f"/api/v2/dags/{_q(args.dag_id)}", json={"is_paused": False})
            unpaused = True
        body = {"logical_date": None, "conf": args.conf, "note": "Triggered by MLOps Copilot agent"}
        run = self.client.request("POST", f"/api/v2/dags/{_q(args.dag_id)}/dagRuns", json=body)
        run_id = run["dag_run_id"]
        result: dict[str, Any] = {"dag_id": args.dag_id, "dag_run_id": run_id, "unpaused_dag": unpaused,
                                  "conf": args.conf, "initial_state": run.get("state")}
        if not args.wait_for_completion:
            result["final_state"] = None
            result["message"] = "run queued; use airflow_get_task_instances to follow progress"
            return result
        deadline = time.monotonic() + args.timeout_seconds
        state = run.get("state")
        while state not in TERMINAL_STATES and time.monotonic() < deadline:
            time.sleep(self.poll_interval)
            state = self.client.request("GET", f"/api/v2/dags/{_q(args.dag_id)}/dagRuns/{_q(run_id)}").get("state")
        result["final_state"] = state
        result["completed"] = state in TERMINAL_STATES
        result["task_instances"] = self._task_instances(args.dag_id, run_id)
        if state == "failed":
            result["failure_details"] = self.get_failed_tasks(DagRunInput(dag_id=args.dag_id, dag_run_id=run_id))
        elif state not in TERMINAL_STATES:
            result["message"] = f"run still '{state}' after {args.timeout_seconds}s; it continues in Airflow"
        return result

    # ------------------------------------------------------------------ policy
    def authorize_trigger(self, args: TriggerDagInput) -> str | None:
        if args.dag_id not in self.settings.airflow_approved_dags:
            return (f"DAG '{args.dag_id}' is not on the approved list {self.settings.airflow_approved_dags}; "
                    "trigger rejected")
        unknown = set(args.conf) - ALLOWED_CONF_KEYS
        if unknown:
            return f"conf keys {sorted(unknown)} are not allowed; permitted keys: {sorted(ALLOWED_CONF_KEYS)}"
        model_type = args.conf.get("model_type")
        if model_type is not None and model_type not in ALLOWED_MODEL_TYPES:
            return f"model_type '{model_type}' is not allowed; permitted: {sorted(ALLOWED_MODEL_TYPES)}"
        if "params" in args.conf and not isinstance(args.conf["params"], dict):
            return "conf.params must be a JSON object"
        return None

    def specs(self) -> list[ToolSpec]:
        m = self.module
        return [
            ToolSpec("airflow_list_dags", "List Airflow DAGs with paused state, tags and whether each is approved "
                     "for triggering by the agent.", NoInput, self.list_dags, m),
            ToolSpec("airflow_get_dag_runs", "Recent runs of a DAG (newest first) with state, timing and conf.",
                     DagRunsInput, self.get_dag_runs, m),
            ToolSpec("airflow_get_task_instances", "Task-level execution info (state, tries, duration) for a DAG "
                     "run; latest run if no run id is given.", DagRunInput, self.get_task_instances, m),
            ToolSpec("airflow_get_failed_tasks", "Identify failed tasks of a DAG run (latest failed run if no id) "
                     "with error lines extracted from their logs and the skipped downstream tasks.",
                     DagRunInput, self.get_failed_tasks, m),
            ToolSpec("airflow_get_task_logs", "Tail of the log of one task attempt for failure diagnostics.",
                     TaskLogsInput, self.get_task_logs, m),
            ToolSpec("airflow_trigger_dag", "Trigger an APPROVED training DAG (state-changing: requires explicit "
                     "user confirmation) and optionally wait for the result and task states.",
                     TriggerDagInput, self.trigger_dag, m, mutating=True, authorize=self.authorize_trigger,
                     timeout=lambda a: a.timeout_seconds + 30),
        ]


__all__ = ["AirflowClient", "AirflowTools", "DagIdInput"]
