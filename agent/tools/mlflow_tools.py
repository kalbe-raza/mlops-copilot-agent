"""MLflow experiment-intelligence tools (MlflowClient API only, never the web UI)."""

from __future__ import annotations

import os
import re
import statistics
from datetime import UTC, datetime
from typing import Any

import httpx

from agent.config import Settings
from agent.schemas import (
    BestRunInput,
    CompareExperimentsInput,
    CompareRunsInput,
    ListRunsInput,
    ModelRegistryInput,
    NoInput,
    RunIdInput,
)
from agent.tools.base import ToolError, ToolSpec, ToolUnavailableError

# Fail fast instead of MLflow's default long exponential back-off when the server is down.
os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "0")
os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "15")
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

KEY_METRICS = ["f1", "recall", "precision", "accuracy", "roc_auc", "f1_gap"]
RUN_ID = re.compile(r"^[0-9a-f]{32}$")
UNAVAILABLE_HINTS = ("connection", "max retries", "failed to establish", "timed out", "name or service")


def _ts(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat(timespec="seconds") if ms else None


def _age_days(ms: int | None) -> float | None:
    if not ms:
        return None
    return round((datetime.now(tz=UTC).timestamp() * 1000 - ms) / 86_400_000, 2)


class MLflowTools:
    module = "mlflow"

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.settings = settings
        self._client = client

    # ------------------------------------------------------------------ plumbing
    @property
    def client(self) -> Any:
        if self._client is None:
            uri = self.settings.mlflow_tracking_uri
            if uri.startswith("http"):
                try:
                    httpx.get(f"{uri.rstrip('/')}/health", timeout=5.0).raise_for_status()
                except httpx.HTTPError as exc:
                    raise ToolUnavailableError(f"MLflow tracking server unreachable at {uri}: {exc}") from exc
            from mlflow.tracking import MlflowClient

            self._client = MlflowClient(tracking_uri=uri)
        return self._client

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - classify MLflow/requests errors
            text = str(exc).lower()
            if any(h in text for h in UNAVAILABLE_HINTS):
                raise ToolUnavailableError(f"MLflow tracking server unreachable: {str(exc)[:300]}") from exc
            raise ToolError(f"MLflow error: {str(exc)[:300]}") from exc

    def _experiments(self, names: list[str] | None) -> dict[str, str]:
        exps = self._call(self.client.search_experiments)
        mapping = {e.experiment_id: e.name for e in exps}
        if names:
            wanted = {n.lower() for n in names}
            mapping = {i: n for i, n in mapping.items() if n.lower() in wanted}
            missing = wanted - {n.lower() for n in mapping.values()}
            if missing:
                available = sorted(e.name for e in exps)
                raise ToolError(f"experiment(s) not found: {sorted(missing)}; available: {available}")
        return mapping

    def _search(self, names: list[str] | None, max_results: int, filter_string: str | None = None,
                finished_only: bool = False) -> tuple[list[Any], dict[str, str]]:
        exps = self._experiments(names)
        if not exps:
            return [], exps
        filters = [f for f in (filter_string, "attributes.status = 'FINISHED'" if finished_only else None) if f]
        runs = self._call(self.client.search_runs, experiment_ids=list(exps), filter_string=" and ".join(filters),
                          order_by=["attributes.start_time DESC"], max_results=max_results)
        return list(runs), exps

    def _summary(self, run: Any, exps: dict[str, str]) -> dict[str, Any]:
        tags = run.data.tags
        return {
            "run_id": run.info.run_id,
            "run_name": run.info.run_name,
            "experiment": exps.get(run.info.experiment_id, run.info.experiment_id),
            "status": run.info.status,
            "start_time": _ts(run.info.start_time),
            "age_days": _age_days(run.info.start_time),
            "model_type": tags.get("model.type") or run.data.params.get("model_type"),
            "metrics": {k: round(v, 4) for k, v in run.data.metrics.items() if k in KEY_METRICS},
            "dataset_version": tags.get("dataset.version"),
            "quality_gate": tags.get("quality_gate.status", "not_evaluated"),
        }

    def _resolve_run(self, ref: str) -> Any:
        if RUN_ID.match(ref):
            return self._call(self.client.get_run, ref)
        exps = self._experiments(None)
        safe = ref.replace("'", "").strip()
        if safe.isdigit():  # "run 25" / "experiment 25" -> run named run-25-*
            filters = [f"tags.mlflow.runName LIKE 'run-{int(safe):02d}-%'"]
        else:
            filters = [f"tags.mlflow.runName = '{safe}'", f"tags.mlflow.runName LIKE '{safe}%'"]
        for flt in filters:
            runs = self._call(self.client.search_runs, experiment_ids=list(exps), filter_string=flt,
                              order_by=["attributes.start_time DESC"], max_results=5)
            if runs:
                return runs[0]
        raise ToolError(f"no MLflow run with id or name '{ref}'")

    def _registry_index(self) -> dict[str, list[dict[str, Any]]]:
        """run_id -> registered versions (with aliases)."""
        index: dict[str, list[dict[str, Any]]] = {}
        try:
            models = self._call(self.client.search_registered_models)
        except ToolError:
            return index
        for model in models:
            aliases: dict[str, list[str]] = {}
            for alias, version in (getattr(model, "aliases", None) or {}).items():
                aliases.setdefault(str(version), []).append(alias)
            for mv in self._call(self.client.search_model_versions, f"name='{model.name}'"):
                index.setdefault(mv.run_id, []).append(
                    {"model": model.name, "version": mv.version, "aliases": aliases.get(str(mv.version), [])})
        return index

    # ------------------------------------------------------------------ tools
    def list_experiments(self, _: NoInput) -> dict[str, Any]:
        exps = self._call(self.client.search_experiments)
        out = []
        for e in exps:
            runs = self._call(self.client.search_runs, experiment_ids=[e.experiment_id], max_results=500)
            out.append({"experiment_id": e.experiment_id, "name": e.name, "lifecycle_stage": e.lifecycle_stage,
                        "created": _ts(e.creation_time), "run_count": len(runs),
                        "last_run": _ts(max((r.info.start_time for r in runs), default=None))})
        return {"experiments": out, "count": len(out), "tracking_uri": self.settings.mlflow_tracking_uri}

    def list_runs(self, args: ListRunsInput) -> dict[str, Any]:
        runs, exps = self._search(args.experiment_names, args.max_results, args.filter_string)
        return {"runs": [self._summary(r, exps) for r in runs], "count": len(runs)}

    def get_run_details(self, args: RunIdInput) -> dict[str, Any]:
        run = self._resolve_run(args.run_id)
        exps = self._experiments(None)
        artifacts = []
        for item in self._call(self.client.list_artifacts, run.info.run_id):
            artifacts.append(item.path + ("/" if item.is_dir else ""))
            if item.is_dir:
                artifacts.extend(c.path for c in self._call(self.client.list_artifacts, run.info.run_id, item.path))
        tags = {k: v for k, v in run.data.tags.items() if not k.startswith("mlflow.") or k == "mlflow.runName"}
        return {
            **self._summary(run, exps),
            "end_time": _ts(run.info.end_time),
            "params": dict(run.data.params),
            "all_metrics": {k: round(v, 6) for k, v in run.data.metrics.items()},
            "tags": tags,
            "artifacts": artifacts,
            "registered_versions": self._registry_index().get(run.info.run_id, []),
        }

    def find_best_run(self, args: BestRunInput) -> dict[str, Any]:
        runs, exps = self._search(args.experiment_names, args.recent_n, finished_only=True)
        scored = [r for r in runs if args.metric in r.data.metrics]
        if not scored:
            raise ToolError(f"no finished runs with metric '{args.metric}' among the {len(runs)} most recent runs")
        scored.sort(key=lambda r: r.data.metrics[args.metric], reverse=args.mode == "max")
        registry = self._registry_index()
        ranking = []
        for r in scored[:5]:
            s = self._summary(r, exps)
            s["registered_versions"] = registry.get(r.info.run_id, [])
            ranking.append(s)
        best = ranking[0]
        caveats = []
        if best["quality_gate"] != "passed":
            caveats.append(f"best run quality gate status is '{best['quality_gate']}'")
        gap = best["metrics"].get("f1_gap")
        if gap is not None and gap > 0.05:
            caveats.append(f"train/test F1 gap of {gap} suggests overfitting")
        if not best["registered_versions"]:
            caveats.append("best run is not registered in the model registry")
        best_passing = next((s for s in ranking if s["quality_gate"] == "passed"), None)
        return {
            "metric": args.metric, "mode": args.mode, "runs_considered": len(scored),
            "best_run": best, "best_quality_gate_passing_run": best_passing, "top_ranking": ranking,
            "caveats": caveats,
            "note": "Highest metric alone does not make a model production-ready; check quality gate, "
                    "overfitting gap, data provenance and deployment validation.",
        }

    def compare_runs(self, args: CompareRunsInput) -> dict[str, Any]:
        if args.run_ids:
            runs = [self._resolve_run(r) for r in args.run_ids]
            exps = self._experiments(None)
        else:
            runs, exps = self._search(args.experiment_names, args.last_n or 5, finished_only=True)
        if len(runs) < 2:
            raise ToolError("need at least two runs to compare")
        rows = [self._summary(r, exps) for r in runs]
        eligible = [r for r in rows if r["quality_gate"] == "passed" and args.metric in r["metrics"]]
        pool = eligible or [r for r in rows if args.metric in r["metrics"]]
        if not pool:
            raise ToolError(f"none of the runs logged metric '{args.metric}'")
        ranked = sorted(pool, key=lambda r: (r["metrics"][args.metric], r["metrics"].get("recall", 0),
                                             -r["metrics"].get("f1_gap", 0)), reverse=True)
        rec = ranked[0]
        reasons = [f"highest {args.metric} ({rec['metrics'][args.metric]})"
                   + (" among runs that passed the quality gate" if eligible else "")]
        if "recall" in rec["metrics"]:
            reasons.append(f"recall {rec['metrics']['recall']} (missed malignant cases are costly)")
        if "f1_gap" in rec["metrics"]:
            reasons.append(f"train/test F1 gap {rec['metrics']['f1_gap']}")
        return {"metric": args.metric, "runs": rows, "recommended_run": rec["run_id"],
                "recommended_run_name": rec["run_name"], "reasons": reasons,
                "excluded_failed_quality_gate": [r["run_name"] for r in rows if r["quality_gate"] == "failed"],
                "note": "Recommendation is a candidate for review, not an automatic production promotion."}

    def compare_experiments(self, args: CompareExperimentsInput) -> dict[str, Any]:
        exps = self._experiments(args.experiment_names)
        out = []
        for exp_id, name in exps.items():
            runs = self._call(self.client.search_runs, experiment_ids=[exp_id],
                              filter_string="attributes.status = 'FINISHED'", max_results=500)
            values = [r.data.metrics[args.metric] for r in runs if args.metric in r.data.metrics]
            best = max(runs, key=lambda r: r.data.metrics.get(args.metric, float("-inf")), default=None)
            out.append({"experiment": name, "runs": len(runs), "runs_with_metric": len(values),
                        f"best_{args.metric}": round(max(values), 4) if values else None,
                        f"mean_{args.metric}": round(statistics.mean(values), 4) if values else None,
                        f"std_{args.metric}": round(statistics.pstdev(values), 4) if len(values) > 1 else None,
                        "best_run": best.info.run_name if best and values else None,
                        "dataset_versions": sorted({r.data.tags.get("dataset.version", "unknown") for r in runs})})
        winner = max((e for e in out if e[f"best_{args.metric}"] is not None),
                     key=lambda e: e[f"best_{args.metric}"], default=None)
        return {"metric": args.metric, "experiments": out,
                "highest_best": winner["experiment"] if winner else None}

    def get_model_registry(self, args: ModelRegistryInput) -> dict[str, Any]:
        models = self._call(self.client.search_registered_models)
        if args.model_name:
            models = [m for m in models if m.name == args.model_name]
            if not models:
                raise ToolError(f"registered model '{args.model_name}' not found")
        out = []
        for model in models:
            aliases = dict(getattr(model, "aliases", None) or {})
            versions = []
            for mv in sorted(self._call(self.client.search_model_versions, f"name='{model.name}'"),
                             key=lambda v: int(v.version), reverse=True):
                versions.append({"version": mv.version, "run_id": mv.run_id, "status": mv.status,
                                 "created": _ts(mv.creation_timestamp),
                                 "aliases": [a for a, v in aliases.items() if str(v) == str(mv.version)],
                                 "tags": dict(mv.tags or {})})
            out.append({"name": model.name, "aliases": {k: str(v) for k, v in aliases.items()},
                        "latest_version": versions[0]["version"] if versions else None, "versions": versions})
        return {"registered_models": out, "count": len(out)}

    def get_run_dataset_provenance(self, args: RunIdInput) -> dict[str, Any]:
        run = self._resolve_run(args.run_id)
        tags = run.data.tags
        prov = {k: v for k, v in tags.items() if k.startswith(("dataset.", "git.", "airflow."))}
        if not any(k.startswith("dataset.") for k in prov):
            raise ToolError(f"run '{args.run_id}' has no recorded dataset provenance tags")
        return {"run_id": run.info.run_id, "run_name": run.info.run_name, "start_time": _ts(run.info.start_time),
                "dataset_version": tags.get("dataset.version"), "dataset_md5": tags.get("dataset.dvc_md5"),
                "dataset_git_commit": tags.get("dataset.git_commit"), "code_git_commit": tags.get("git.commit"),
                "dataset_path": tags.get("dataset.path"), "matches_dvc": tags.get("dataset.matches_dvc"),
                "rows": tags.get("dataset.rows"), "all_provenance_tags": prov}

    # ------------------------------------------------------------------ registry
    def specs(self) -> list[ToolSpec]:
        m = self.module
        return [
            ToolSpec("mlflow_list_experiments", "List MLflow experiments with run counts and last activity.",
                     NoInput, self.list_experiments, m),
            ToolSpec("mlflow_list_runs", "List recent training runs (newest first) with key metrics, model type, "
                     "dataset version and quality-gate status.", ListRunsInput, self.list_runs, m),
            ToolSpec("mlflow_get_run_details", "Full details of one run: params, metrics, tags, artifacts and "
                     "registered model versions.", RunIdInput, self.get_run_details, m),
            ToolSpec("mlflow_find_best_run", "Rank recent finished runs by an evaluation metric and return the "
                     "best run with caveats (quality gate, overfitting, registry status).",
                     BestRunInput, self.find_best_run, m),
            ToolSpec("mlflow_compare_runs", "Compare explicit runs or the last N runs side by side and "
                     "recommend a candidate with reasons.", CompareRunsInput, self.compare_runs, m),
            ToolSpec("mlflow_compare_experiments", "Compare two or more experiments by best/mean/std of a metric.",
                     CompareExperimentsInput, self.compare_experiments, m),
            ToolSpec("mlflow_get_model_registry", "Registered models, versions, aliases (e.g. champion) and "
                     "the source run of each version.", ModelRegistryInput, self.get_model_registry, m),
            ToolSpec("mlflow_get_run_dataset_provenance", "Recorded dataset provenance of a run: DVC md5, "
                     "dataset version tag, dataset Git commit and code commit.",
                     RunIdInput, self.get_run_dataset_provenance, m),
        ]
