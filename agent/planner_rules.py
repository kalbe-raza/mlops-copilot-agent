"""Deterministic rule-based planner (offline fallback and CI planner).

It classifies the request with keyword rules, executes a fixed multi-step tool plan through the
same safe executor used by the LLM planner, and renders an answer *only* from tool results.
When a tool fails, the answer states that the information is unavailable instead of guessing.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from agent.config import Settings
from agent.schemas import ToolResult
from agent.tools.base import ToolExecutor

NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                "ten": 10}
METRICS = {"f1": "f1", "f-1": "f1", "recall": "recall", "precision": "precision", "accuracy": "accuracy",
           "roc_auc": "roc_auc", "roc auc": "roc_auc", "auc": "roc_auc"}
TASK_IDS = ["validate_data", "preprocess_data", "train_model", "evaluate_and_log", "quality_gate",
            "register_model"]

TRIGGER = re.compile(r"\b(trigger|kick off|launch|start|execute|re-?run)\b[^.?!]*\b(pipeline|dag|training|retraining)\b"
                     r"|\brun the\b[^.?!]*\b(pipeline|dag|training)\b")


def _has(q: str, *words: str) -> bool:
    return any(re.search(rf"\b{re.escape(w)}", q) for w in words)


def _fail_line(r: ToolResult) -> str:
    return f"`{r.tool}` returned **{r.status.value}**: {r.error}"


def _section(title: str, lines: list[str]) -> str:
    body = "\n".join(f"- {line}" for line in lines) if lines else "- None"
    return f"**{title}**\n{body}"


def _answer(summary: str, evidence: list[str], uncertainties: list[str], actions: list[str] | None = None) -> str:
    parts = [f"**Answer**\n{summary}", _section("Evidence", evidence), _section("Uncertainties", uncertainties)]
    if actions:
        parts.append(_section("Recommended actions", actions))
    return "\n\n".join(parts)


class RuleBasedPlanner:
    name = "rules"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------ entry point
    def classify(self, question: str) -> str:
        q = question.lower()
        if TRIGGER.search(q):
            return "airflow_trigger"
        if _has(q, "restart") and _has(q, "deploy", "pod", "api", "service", "inference"):
            return "k8s_restart"
        if _has(q, "scale") and _has(q, "deploy", "replica", "api", "inference"):
            return "k8s_scale"
        if _has(q, "retrain") or (_has(q, "best") and _has(q, "deploy", "kubernetes", "k8s")):
            return "full_assessment"
        if _has(q, "dataset", "data version") and _has(q, "run", "experiment", "model", "trained", "training") \
                and not _has(q, "compare"):
            return "dataset_provenance"
        if _has(q, "reproduce", "checkout", "restore") and _has(q, "data", "version"):
            return "dvc_reproduce"
        if _has(q, "compare", "diff", "difference") and _has(q, "dataset", "data") and not _has(q, "runs"):
            return "dvc_compare"
        if _has(q, "changed", "modified", "drift", "changes") and _has(q, "data", "dvc"):
            return "dvc_changes"
        if _has(q, "history", "versions") and _has(q, "data", "dvc"):
            return "dvc_versions"
        if _has(q, "tracked", "which datasets", "list datasets"):
            return "dvc_tracked"
        if _has(q, "dataset", "dvc", "data version"):
            return "dvc_current"
        if _has(q, "fail", "error", "why", "broken") and _has(q, "pipeline", "dag", "task", "training", "airflow"):
            return "airflow_failures"
        if _has(q, "log") and any(t in q for t in TASK_IDS):
            return "airflow_logs"
        if _has(q, "dag", "pipeline", "airflow", "workflow") and _has(q, "status", "runs", "recent", "last",
                                                                       "latest", "succeed", "state"):
            return "airflow_status"
        if _has(q, "dags", "workflows", "airflow"):
            return "airflow_list"
        if _has(q, "unavailable", "down", "not working", "diagnose", "unhealthy", "why", "crash", "failing",
                "error", "health") and _has(q, "api", "pod", "deploy", "service", "kubernetes", "k8s",
                                            "prediction", "inference", "cluster"):
            return "k8s_diagnose"
        if _has(q, "log") and _has(q, "pod", "api", "inference", "kubernetes"):
            return "k8s_logs"
        if _has(q, "version") and _has(q, "deploy", "running", "serving", "kubernetes", "production"):
            return "k8s_version"
        if _has(q, "service") and _has(q, "kubernetes", "k8s", "cluster", "inspect", "endpoint"):
            return "k8s_services"
        if _has(q, "pod"):
            return "k8s_pods"
        if _has(q, "deployment", "kubernetes", "k8s"):
            return "k8s_deployments"
        if _has(q, "compare") and _has(q, "experiment") and not _has(q, "runs"):
            return "mlflow_compare_experiments"
        if _has(q, "compare") and _has(q, "run", "model", "candidate"):
            return "mlflow_compare_runs"
        if _has(q, "registry", "registered", "model version", "champion", "alias"):
            return "mlflow_registry"
        if _has(q, "best", "recommend", "top"):
            return "mlflow_best"
        if self._run_ref(question) and _has(q, "metric", "param", "detail", "artifact", "run"):
            return "mlflow_run_details"
        if _has(q, "experiments"):
            return "mlflow_experiments"
        if _has(q, "runs", "experiment", "mlflow", "metric"):
            return "mlflow_runs"
        return "help"

    def run(self, question: str, executor: ToolExecutor, request_id: str) -> tuple[str, list[ToolResult]]:
        results: list[ToolResult] = []

        def call(tool: str, **args: Any) -> ToolResult:
            res = executor.execute(tool, {k: v for k, v in args.items() if v is not None}, request_id)
            results.append(res)
            return res

        intent = self.classify(question)
        handler: Callable[[str, Callable[..., ToolResult]], str] = getattr(self, f"_h_{intent}")
        return handler(question, call), results

    # ------------------------------------------------------------------ extraction helpers
    @staticmethod
    def _number(q: str, default: int) -> int:
        m = re.search(r"\b(?:last|latest|recent|top)\s+(\d+|" + "|".join(NUMBER_WORDS) + r")\b", q.lower())
        if not m:
            return default
        token = m.group(1)
        return int(token) if token.isdigit() else NUMBER_WORDS[token]

    @staticmethod
    def _metric(q: str) -> str:
        ql = q.lower()
        for key, metric in METRICS.items():
            if re.search(rf"\b{re.escape(key)}\b", ql):
                return metric
        return "f1"

    @staticmethod
    def _run_ref(q: str) -> str | None:
        m = re.search(r"\b([0-9a-f]{32})\b", q)
        if m:
            return m.group(1)
        m = re.search(r"\b(?:experiment|run)\s*(?:#|number|no\.?)?\s*(\d{1,4})\b", q, re.IGNORECASE)
        if m:
            return m.group(1)
        m = re.search(r"\brun\s+(?:named\s+)?[`'\"]?([a-z0-9][\w.\-]*-[\w.\-]+)[`'\"]?", q, re.IGNORECASE)
        return m.group(1) if m else None

    @staticmethod
    def _revs(q: str) -> list[str]:
        return re.findall(r"\b(data-v\d+(?:\.\d+)*|[0-9a-f]{7,40}|HEAD)\b", q)

    # ------------------------------------------------------------------ DVC handlers
    def _h_dvc_current(self, q: str, call) -> str:
        r = call("dvc_get_dataset_version")
        if not r.ok:
            return _answer("The current dataset version could not be determined.", [], [_fail_line(r)])
        d = r.data
        shape = d.get("shape") or {}
        ev = [f"Dataset `{d['path']}` is at version **{d['version']}** (DVC md5 `{d['dvc_md5']}`, "
              f"{d['size_bytes']} bytes) [dvc_get_dataset_version]",
              f"Last data change committed in Git `{d['short_commit']}` on {d['committed_at']}: "
              f"\"{d['commit_message']}\" [dvc_get_dataset_version]"]
        if shape:
            ev.append(f"{shape['rows']} rows x {shape['columns']} columns in the workspace copy "
                      "[dvc_get_dataset_version]")
        unc = [] if d["workspace_matches_dvc"] else ["workspace file differs from the committed DVC hash"]
        return _answer(f"The current dataset version is **{d['version']}** at Git commit `{d['short_commit']}`.",
                       ev, unc)

    def _h_dvc_tracked(self, q: str, call) -> str:
        r = call("dvc_list_tracked_datasets")
        if not r.ok:
            return _answer("Tracked datasets could not be listed.", [], [_fail_line(r)])
        ev = [f"`{t['path']}` - {t['type']}, md5 `{t['md5']}`, {t['size']} bytes [dvc_list_tracked_datasets]"
              for t in r.data["tracked"]]
        ev += [f"DVC remote `{x.get('name')}` -> {x.get('url')} [dvc_list_tracked_datasets]" for x in r.data["remotes"]]
        return _answer(f"DVC tracks {r.data['count']} item(s).", ev, [])

    def _h_dvc_versions(self, q: str, call) -> str:
        r = call("dvc_list_dataset_versions")
        if not r.ok:
            return _answer("Dataset history could not be read.", [], [_fail_line(r)])
        ev = []
        for v in r.data["versions"]:
            rows = f", {v['shape']['rows']} rows" if v.get("shape") else ""
            ev.append(f"{', '.join(v['tags']) or 'untagged'}: commit `{v['short_commit']}` ({v['date']}), md5 "
                      f"`{v['md5']}`{rows} - \"{v['message']}\" [dvc_list_dataset_versions]")
        return _answer(f"`{r.data['path']}` has {r.data['count']} recorded version(s).", ev, [])

    def _h_dvc_compare(self, q: str, call) -> str:
        revs = self._revs(q)
        if len(revs) < 2:
            hist = call("dvc_list_dataset_versions")
            if not hist.ok:
                return _answer("Dataset versions could not be compared.", [], [_fail_line(hist)])
            tagged = [v for v in reversed(hist.data["versions"])]
            if len(tagged) < 2:
                return _answer("Only one dataset version exists; nothing to compare.", [], [])
            revs = [(tagged[0]["tags"] or [tagged[0]["commit"]])[0], (tagged[-1]["tags"] or [tagged[-1]["commit"]])[0]]
        r = call("dvc_compare_dataset_versions", rev_a=revs[0], rev_b=revs[1])
        if not r.ok:
            return _answer("Dataset versions could not be compared.", [], [_fail_line(r)])
        d = r.data
        ev = [f"{d['rev_a']['rev']}: md5 `{d['rev_a']['md5']}`, {d['rev_a']['size']} bytes, commit "
              f"`{d['rev_a']['commit'][:7]}` [dvc_compare_dataset_versions]",
              f"{d['rev_b']['rev']}: md5 `{d['rev_b']['md5']}`, {d['rev_b']['size']} bytes, commit "
              f"`{d['rev_b']['commit'][:7]}` [dvc_compare_dataset_versions]"]
        unc = []
        if isinstance(d.get("rows"), dict):
            ev.append(f"Rows {d['rows']['rev_a']} -> {d['rows']['rev_b']} (delta {d['rows']['delta']:+d}); "
                      f"added columns {d['schema']['added_columns']}, removed {d['schema']['removed_columns']} "
                      "[dvc_compare_dataset_versions]")
        else:
            unc.append(str(d.get("rows")))
        changed = "changed" if d["content_changed"] else "identical"
        return _answer(f"Content between {revs[0]} and {revs[1]} is **{changed}**.", ev, unc)

    def _h_dvc_changes(self, q: str, call) -> str:
        r = call("dvc_check_data_changes")
        if not r.ok:
            return _answer("Could not check DVC data status.", [], [_fail_line(r)])
        d = r.data
        ev = [f"{c['target']}: {c['kind']} {c.get('detail', '')} [dvc_check_data_changes]" for c in d["changes"]]
        ev += [f"Uncommitted DVC metadata: {x} [dvc_check_data_changes]" for x in d["uncommitted_dvc_metadata"]]
        summary = "Tracked data **has changed** in the workspace." if d["workspace_changed"] else \
            "No changes: all DVC-tracked data matches the committed versions."
        actions = ["Restore the committed data with `dvc checkout`, or version the change with `dvc add` + "
                   "`git commit` + a new data tag."] if d["workspace_changed"] else None
        return _answer(summary + f" {d['interpretation']}", ev, [], actions)

    def _h_dvc_reproduce(self, q: str, call) -> str:
        revs = self._revs(q)
        if not revs:
            return _answer("Please name the dataset version to reproduce (e.g. `data-v1.0`).", [], [])
        rev = revs[0]
        plan = call("dvc_explain_reproduction", rev=rev)
        if not plan.ok:
            return _answer(f"Reproduction of {rev} could not be planned.", [], [_fail_line(plan)])
        repro = call("dvc_reproduce_dataset_version", rev=rev)
        ev = [f"{rev} = commit `{plan.data['commit'][:7]}`, md5 `{plan.data['md5']}` [dvc_explain_reproduction]"]
        unc = []
        if repro.ok:
            ev.append(f"Materialised to `{repro.data['output_path']}`; hash verified = **{repro.data['verified']}** "
                      f"({repro.data['actual_md5']}) [dvc_reproduce_dataset_version]")
        else:
            unc.append(_fail_line(repro))
        steps = [f"`{c}`" for c in plan.data["non_destructive"] + plan.data["restore_in_workspace"]]
        return _answer(f"Dataset {rev} can be reproduced exactly from DVC.", ev, unc, steps)

    def _h_dataset_provenance(self, q: str, call) -> str:
        ref = self._run_ref(q)
        if ref is None:
            best = call("mlflow_find_best_run", metric=self._metric(q))
            if not best.ok:
                return _answer("The run could not be identified.", [], [_fail_line(best)])
            ref = best.data["best_run"]["run_id"]
        prov = call("mlflow_get_run_dataset_provenance", run_id=ref)
        if not prov.ok:
            return _answer(f"Dataset provenance for run `{ref}` is unavailable.", [], [_fail_line(prov)])
        p = prov.data
        ev = [f"MLflow run `{p['run_name']}` ({p['run_id']}) recorded dataset version **{p['dataset_version']}**, "
              f"md5 `{p['dataset_md5']}`, dataset commit `{(p['dataset_git_commit'] or '')[:7]}`, "
              f"{p['rows']} rows [mlflow_get_run_dataset_provenance]"]
        unc = []
        res = call("dvc_resolve_dataset_reference", md5=p["dataset_md5"]) if p["dataset_md5"] else None
        if res is not None and res.ok and res.data["found"]:
            ev.append(f"DVC history confirms md5 `{p['dataset_md5'][:8]}` = **{res.data['version']}** at commit "
                      f"`{res.data['git_commit'][:7]}`; current version: {res.data['is_current_version']}; "
                      f"in local cache: {res.data['in_local_cache']} [dvc_resolve_dataset_reference]")
        elif res is not None and res.ok:
            unc.append("the recorded md5 does not match any committed DVC version")
        elif res is not None:
            unc.append(_fail_line(res))
        if p.get("matches_dvc") != "true":
            unc.append("at training time the data file did not match its DVC hash")
        summary = (f"Run `{p['run_name']}` used dataset **{p['dataset_version']}** "
                   f"(Git commit `{(p['dataset_git_commit'] or '')[:7]}`).")
        return _answer(summary, ev, unc, [f"Reproduce it with `dvc get . data/raw/dataset.csv --rev "
                                          f"{p['dataset_version']}`"] if p["dataset_version"] else None)

    # ------------------------------------------------------------------ MLflow handlers
    @staticmethod
    def _run_line(s: dict[str, Any], tool: str) -> str:
        m = s["metrics"]
        return (f"`{s['run_name']}` ({s['model_type']}, data {s['dataset_version']}): f1={m.get('f1')}, "
                f"recall={m.get('recall')}, precision={m.get('precision')}, gap={m.get('f1_gap')}, "
                f"gate={s['quality_gate']} [{tool}]")

    def _h_mlflow_experiments(self, q: str, call) -> str:
        r = call("mlflow_list_experiments")
        if not r.ok:
            return _answer("MLflow experiments could not be listed.", [], [_fail_line(r)])
        ev = [f"`{e['name']}` (id {e['experiment_id']}): {e['run_count']} runs, last run {e['last_run']} "
              "[mlflow_list_experiments]" for e in r.data["experiments"]]
        return _answer(f"{r.data['count']} experiments are tracked.", ev, [])

    def _h_mlflow_runs(self, q: str, call) -> str:
        r = call("mlflow_list_runs", max_results=self._number(q, 10))
        if not r.ok:
            return _answer("MLflow runs could not be listed.", [], [_fail_line(r)])
        return _answer(f"{r.data['count']} most recent runs:", [self._run_line(s, "mlflow_list_runs")
                                                                for s in r.data["runs"]], [])

    def _h_mlflow_run_details(self, q: str, call) -> str:
        ref = self._run_ref(q) or ""
        r = call("mlflow_get_run_details", run_id=ref)
        if not r.ok:
            return _answer(f"Run `{ref}` could not be retrieved.", [], [_fail_line(r)])
        d = r.data
        ev = [self._run_line(d, "mlflow_get_run_details"), f"Params: {d['params']} [mlflow_get_run_details]",
              f"Artifacts: {d['artifacts']} [mlflow_get_run_details]",
              f"Registered as: {d['registered_versions'] or 'not registered'} [mlflow_get_run_details]"]
        return _answer(f"Details of run `{d['run_name']}` ({d['run_id']}).", ev, [])

    def _h_mlflow_best(self, q: str, call) -> str:
        metric = self._metric(q)
        r = call("mlflow_find_best_run", metric=metric)
        if not r.ok:
            return _answer(f"The best run by {metric} could not be determined.", [], [_fail_line(r)])
        d = r.data
        best = d["best_run"]
        ev = [self._run_line(s, "mlflow_find_best_run") for s in d["top_ranking"]]
        summary = f"Best of {d['runs_considered']} recent runs by **{metric}** is `{best['run_name']}` " \
                  f"({best['metrics'].get(metric)})."
        passing = d.get("best_quality_gate_passing_run")
        if passing and passing["run_id"] != best["run_id"]:
            summary += f" The best run that passed the quality gate is `{passing['run_name']}`."
        return _answer(summary, ev, d["caveats"], [d["note"]])

    def _h_mlflow_compare_runs(self, q: str, call) -> str:
        metric = self._metric(q)
        r = call("mlflow_compare_runs", last_n=self._number(q, 5), metric=metric)
        if not r.ok:
            return _answer("Runs could not be compared.", [], [_fail_line(r)])
        d = r.data
        ev = [self._run_line(s, "mlflow_compare_runs") for s in d["runs"]]
        unc = [f"Excluded (failed quality gate): {d['excluded_failed_quality_gate']}"] \
            if d["excluded_failed_quality_gate"] else []
        return _answer(f"Recommended candidate: `{d['recommended_run_name']}` because "
                       + "; ".join(d["reasons"]) + ".", ev, unc, [d["note"]])

    def _h_mlflow_compare_experiments(self, q: str, call) -> str:
        exps = call("mlflow_list_experiments")
        if not exps.ok:
            return _answer("Experiments could not be compared.", [], [_fail_line(exps)])
        names = [e["name"] for e in exps.data["experiments"] if e["run_count"] > 0]
        if len(names) < 2:
            return _answer("Fewer than two experiments contain runs; nothing to compare.", [], [])
        metric = self._metric(q)
        r = call("mlflow_compare_experiments", experiment_names=names, metric=metric)
        if not r.ok:
            return _answer("Experiments could not be compared.", [], [_fail_line(r)])
        ev = [f"`{e['experiment']}`: {e['runs']} runs, best {metric}={e[f'best_{metric}']}, mean="
              f"{e[f'mean_{metric}']}, std={e[f'std_{metric}']}, data {e['dataset_versions']} "
              "[mlflow_compare_experiments]" for e in r.data["experiments"]]
        return _answer(f"Highest best {metric}: `{r.data['highest_best']}`.", ev, [])

    def _h_mlflow_registry(self, q: str, call) -> str:
        r = call("mlflow_get_model_registry")
        if not r.ok:
            return _answer("The model registry could not be read.", [], [_fail_line(r)])
        ev = []
        for m in r.data["registered_models"]:
            ev.append(f"`{m['name']}` aliases {m['aliases']} latest v{m['latest_version']} [mlflow_get_model_registry]")
            for v in m["versions"][:6]:
                ev.append(f"v{v['version']} run `{v['run_id'][:8]}` aliases {v['aliases']} tags "
                          f"{ {k: v['tags'][k] for k in ('f1', 'dataset.version') if k in v['tags']} } "
                          "[mlflow_get_model_registry]")
        return _answer(f"{r.data['count']} registered model(s).", ev, [])

    # ------------------------------------------------------------------ Airflow handlers
    def _h_airflow_list(self, q: str, call) -> str:
        r = call("airflow_list_dags")
        if not r.ok:
            return _answer("Airflow DAGs could not be listed.", [], [_fail_line(r)])
        ev = [f"`{d['dag_id']}` paused={d['is_paused']} tags={d['tags']} approved_for_trigger="
              f"{d['approved_for_trigger']} [airflow_list_dags]" for d in r.data["dags"]]
        return _answer(f"{r.data['count']} DAG(s) available.", ev, [])

    def _h_airflow_status(self, q: str, call) -> str:
        runs = call("airflow_get_dag_runs", limit=self._number(q, 5))
        if not runs.ok:
            return _answer("Airflow run status is unavailable.", [], [_fail_line(runs)])
        ev = [f"`{r['dag_run_id']}`: **{r['state']}** ({r['start_date']} -> {r['end_date']}) [airflow_get_dag_runs]"
              for r in runs.data["runs"]]
        if not runs.data["runs"]:
            return _answer("The training DAG has no runs yet.", ev, [])
        tis = call("airflow_get_task_instances", dag_run_id=runs.data["runs"][0]["dag_run_id"])
        if tis.ok:
            ev += [f"latest run task `{t['task_id']}`: {t['state']} (try {t['try_number']}) "
                   "[airflow_get_task_instances]" for t in tis.data["task_instances"]]
        unc = [] if tis.ok else [_fail_line(tis)]
        return _answer(f"Latest training run is **{runs.data['latest_state']}**; states of the last "
                       f"{len(runs.data['runs'])}: {runs.data['state_counts']}.", ev, unc)

    def _render_failures(self, d: dict[str, Any]) -> list[str]:
        ev = []
        for t in d.get("failed_tasks", []):
            ev.append(f"Task `{t['task_id']}`: **{t['state']}** after {t.get('try_number')} attempt(s) "
                      "[airflow_get_failed_tasks]")
            for line in t.get("error_lines", [])[-4:]:
                ev.append(f"  log: `{line[:220]}` [airflow_get_failed_tasks]")
        if d.get("skipped_downstream"):
            ev.append(f"Downstream tasks not executed: {d['skipped_downstream']} [airflow_get_failed_tasks]")
        return ev

    def _h_airflow_failures(self, q: str, call) -> str:
        r = call("airflow_get_failed_tasks")
        if not r.ok:
            return _answer("Failed tasks could not be retrieved.", [], [_fail_line(r)])
        d = r.data
        if not d["failed_run_found"]:
            return _answer(d["message"], [], [])
        actions = []
        if d["root_cause_task"] == "validate_data":
            actions.append("Inspect the dataset with `dvc status`; restore the committed version with "
                           "`dvc checkout data/raw/dataset.csv` or fix and re-version the data.")
        elif d["root_cause_task"] == "quality_gate":
            actions.append("The model did not meet the promotion thresholds; it was not registered. Tune the "
                           "model or data before re-running.")
        return _answer(f"Run `{d['dag_run_id']}` failed at task **{d['root_cause_task']}**.",
                       self._render_failures(d), [], actions)

    def _h_airflow_logs(self, q: str, call) -> str:
        task = next(t for t in TASK_IDS if t in q)
        r = call("airflow_get_task_logs", task_id=task, tail_lines=25)
        if not r.ok:
            return _answer(f"Logs of `{task}` are unavailable.", [], [_fail_line(r)])
        ev = [f"`{line[:220]}` [airflow_get_task_logs]" for line in r.data["tail"]]
        return _answer(f"Last {len(r.data['tail'])} log lines of `{task}` (run {r.data['dag_run_id']}, try "
                       f"{r.data['try_number']}).", ev, [])

    def _h_airflow_trigger(self, q: str, call) -> str:
        conf: dict[str, Any] = {}
        for mt in ("logistic_regression", "random_forest", "gradient_boosting", "svm", "decision_stump"):
            if mt.replace("_", " ") in q.lower() or mt in q.lower():
                conf["model_type"] = mt
        r = call("airflow_trigger_dag", dag_id=self.settings.airflow_approved_dags[0], conf=conf,
                 wait_for_completion=True)
        if not r.ok:
            return _answer("The training pipeline was **not** triggered.", [], [_fail_line(r)])
        d = r.data
        ev = [f"Triggered `{d['dag_id']}` run `{d['dag_run_id']}` with conf {d['conf']} [airflow_trigger_dag]"]
        ev += [f"task `{t['task_id']}`: {t['state']} (try {t['try_number']}, {t['duration_s']}s) "
               "[airflow_trigger_dag]" for t in d.get("task_instances", [])]
        if d.get("failure_details"):
            ev += self._render_failures(d["failure_details"])
        state = d.get("final_state")
        verdict = {"success": "**succeeded**", "failed": "**failed**"}.get(state, f"is still **{state}**")
        return _answer(f"Training pipeline run `{d['dag_run_id']}` {verdict}.", ev,
                       [] if d.get("completed") else [d.get("message", "")])

    # ------------------------------------------------------------------ Kubernetes handlers
    def _h_k8s_deployments(self, q: str, call) -> str:
        r = call("k8s_list_deployments")
        if not r.ok:
            return _answer("Deployments could not be listed.", [], [_fail_line(r)])
        ev = [f"`{d['name']}`: {d['ready_replicas']}/{d['desired_replicas']} ready, image {d['images']}, version "
              f"{d['version_label']}, revision {d['revision']} [k8s_list_deployments]" for d in r.data["deployments"]]
        return _answer(f"{r.data['count']} deployment(s) in `{r.data['namespace']}`.", ev, [])

    def _h_k8s_pods(self, q: str, call) -> str:
        r = call("k8s_list_pods")
        if not r.ok:
            return _answer("Pods could not be listed.", [], [_fail_line(r)])
        ev = [f"`{p['name']}`: {p['phase']}, ready {p['ready']}, restarts {p['restarts']}, node {p['node']} "
              "[k8s_list_pods]" for p in r.data["pods"]]
        return _answer(f"{r.data['count']} application pod(s) in `{r.data['namespace']}`.", ev, [])

    def _h_k8s_services(self, q: str, call) -> str:
        r = call("k8s_list_services")
        if not r.ok:
            return _answer("Services could not be inspected.", [], [_fail_line(r)])
        ev = [f"`{s['name']}` {s['type']} {s['cluster_ip']} ports {[p['port'] for p in s['ports']]} -> ready "
              f"endpoints {s['ready_endpoints']}, not ready {s['not_ready_endpoints']} [k8s_list_services]"
              for s in r.data["services"]]
        return _answer(f"{r.data['count']} service(s) in `{r.data['namespace']}`.", ev, [])

    def _h_k8s_logs(self, q: str, call) -> str:
        pods = call("k8s_list_pods")
        if not pods.ok or not pods.data["pods"]:
            return _answer("No pod logs available.", [], [_fail_line(pods)] if not pods.ok else ["no pods found"])
        pod = pods.data["pods"][0]["name"]
        logs = call("k8s_get_pod_logs", pod_name=pod, tail_lines=20)
        if not logs.ok:
            return _answer(f"Logs of `{pod}` are unavailable.", [], [_fail_line(logs)])
        return _answer(f"Last {len(logs.data['lines'])} log lines of `{pod}`.",
                       [f"`{ln[:220]}` [k8s_get_pod_logs]" for ln in logs.data["lines"]], [])

    def _h_k8s_version(self, q: str, call) -> str:
        r = call("k8s_get_deployed_version")
        if not r.ok:
            return _answer("The deployed version could not be determined.", [], [_fail_line(r)])
        d = r.data
        live = d["live_version_endpoint"]
        ev = [f"Deployment `{d['deployment']}` image `{d['image']}` (tag {d['image_tag']}), version label "
              f"{d['app_version_label']}, revision {d['revision']} [k8s_get_deployed_version]",
              f"ConfigMap: {d['configmap']} [k8s_get_deployed_version]"]
        unc = []
        if isinstance(live, dict):
            ev.append(f"Live /version: app {live.get('app_version')}, model {live.get('model')} "
                      "[k8s_get_deployed_version]")
        else:
            unc.append(str(live))
        return _answer(f"Deployed application version is **{d['app_version_label']}** (image tag {d['image_tag']}).",
                       ev, unc)

    def _h_k8s_diagnose(self, q: str, call) -> str:
        unhealthy = call("k8s_find_unhealthy_pods")
        services = call("k8s_list_services")
        deployments = call("k8s_list_deployments")
        events = call("k8s_get_events", limit=10)
        ev, unc, actions = [], [], []
        for r in (unhealthy, services, deployments, events):
            if not r.ok:
                unc.append(_fail_line(r))
        if not unhealthy.ok and not services.ok and not deployments.ok:
            return _answer("The cluster could not be inspected, so no diagnosis is possible.", ev, unc)
        if deployments.ok:
            for d in deployments.data["deployments"]:
                ev.append(f"Deployment `{d['name']}`: {d['ready_replicas']}/{d['desired_replicas']} ready, "
                          f"{d['unavailable_replicas']} unavailable [k8s_list_deployments]")
        serving = None
        if services.ok:
            for s in services.data["services"]:
                serving = s["serving_traffic"] if serving is None else serving and s["serving_traffic"]
                ev.append(f"Service `{s['name']}` has {s['ready_endpoints']} ready / {s['not_ready_endpoints']} "
                          f"not-ready endpoints [k8s_list_services]")
        causes: list[str] = []
        if unhealthy.ok:
            for p in unhealthy.data["unhealthy_pods"]:
                ev.append(f"Pod `{p['name']}` problems: {p['problems']} [k8s_find_unhealthy_pods]")
                for e in p.get("recent_warning_events", [])[:2]:
                    ev.append(f"  event {e['reason']}: {e['message'][:200]} [k8s_find_unhealthy_pods]")
                    causes.append(e["reason"])
            if unhealthy.data["unhealthy_pods"]:
                pod = unhealthy.data["unhealthy_pods"][0]
                prev = any(c.get("last_termination") for c in pod["containers"])
                logs = call("k8s_get_pod_logs", pod_name=pod["name"], tail_lines=30, previous=prev)
                if not logs.ok and prev:
                    logs = call("k8s_get_pod_logs", pod_name=pod["name"], tail_lines=30)
                if logs.ok:
                    for line in (logs.data["error_lines"] or logs.data["lines"][-3:])[-4:]:
                        ev.append(f"  log `{pod['name']}`: `{line[:220]}` [k8s_get_pod_logs]")
                else:
                    unc.append(_fail_line(logs))
        if events.ok and not causes:
            for e in events.data["events"][:4]:
                ev.append(f"Earlier warning {e['reason']} on {e['object']} (last seen {e['last_seen']}): "
                          f"{e['message'][:200]} [k8s_get_events]")
        if unhealthy.ok and unhealthy.data["all_healthy"] and serving:
            return _answer("No fault found: all application pods are Running and Ready and the Service has ready "
                           "endpoints.", ev, unc)
        reasons = set(causes)
        if "Unhealthy" in reasons:
            actions.append("Readiness/liveness probes are failing - check the probe target and the model/config "
                           "the container loads (see log lines above), fix the ConfigMap or image, then restart "
                           "the deployment.")
        if reasons & {"BackOff", "CrashLoopBackOff"}:
            actions.append("Container is crash-looping - fix the startup error shown in the previous logs.")
        if reasons & {"Failed", "ErrImagePull", "ImagePullBackOff", "InspectFailed"}:
            actions.append("Image cannot be pulled - verify the image tag exists and is loaded into the cluster.")
        if not actions:
            actions.append("Review the evidence above; no single root cause was identified automatically.")
        summary = "The prediction API is **unavailable**: no ready endpoints back the Service." if serving is False \
            else "The prediction API is **degraded**: some pods are unhealthy."
        return _answer(summary, ev, unc, actions)

    def _h_k8s_restart(self, q: str, call) -> str:
        r = call("k8s_restart_deployment")
        if not r.ok:
            return _answer("The deployment was **not** restarted.", [], [_fail_line(r)])
        return _answer(f"Rollout restart of `{r.data['deployment']}` requested at {r.data['restarted_at']}.",
                       [f"{r.data} [k8s_restart_deployment]"], [])

    def _h_k8s_scale(self, q: str, call) -> str:
        m = re.search(r"\bto\s+(\d+)\b", q)
        if not m:
            return _answer("Please specify the replica count, e.g. 'scale the inference deployment to 3'.", [], [])
        r = call("k8s_scale_deployment", replicas=int(m.group(1)))
        if not r.ok:
            return _answer("The deployment was **not** scaled.", [], [_fail_line(r)])
        return _answer(f"`{r.data['deployment']}` scaled to {r.data['replicas']} replicas.",
                       [f"{r.data} [k8s_scale_deployment]"], [])

    # ------------------------------------------------------------------ multi-tool scenario
    def _h_full_assessment(self, q: str, call) -> str:
        metric = self._metric(q)
        ev: list[str] = []
        unc: list[str] = []
        best = call("mlflow_find_best_run", metric=metric, recent_n=25)
        best_run = None
        if best.ok:
            best_run = best.data["best_run"]
            ev.append(f"Best recent run by {metric}: " + self._run_line(best_run, "mlflow_find_best_run"))
            passing = best.data.get("best_quality_gate_passing_run")
            if passing and passing["run_id"] != best_run["run_id"]:
                ev.append("Best run that passed the quality gate: "
                          + self._run_line(passing, "mlflow_find_best_run"))
            unc += [f"MLflow caveat: {c}" for c in best.data["caveats"]]
            prov = call("mlflow_get_run_dataset_provenance", run_id=best_run["run_id"])
            if prov.ok and prov.data["dataset_md5"]:
                ev.append(f"Run recorded dataset {prov.data['dataset_version']} (md5 "
                          f"`{prov.data['dataset_md5'][:8]}`) [mlflow_get_run_dataset_provenance]")
                res = call("dvc_resolve_dataset_reference", md5=prov.data["dataset_md5"])
                if res.ok and res.data["found"]:
                    ev.append(f"DVC confirms this is **{res.data['version']}** (commit "
                              f"`{res.data['git_commit'][:7]}`), current version: {res.data['is_current_version']} "
                              "[dvc_resolve_dataset_reference]")
                elif res.ok:
                    unc.append("recorded dataset hash not found in DVC history")
                else:
                    unc.append(_fail_line(res))
            elif not prov.ok:
                unc.append(_fail_line(prov))
        else:
            unc.append(_fail_line(best))
        version = call("k8s_get_deployed_version")
        health = call("k8s_find_unhealthy_pods")
        if version.ok:
            live = version.data["live_version_endpoint"]
            served = live.get("model", {}) if isinstance(live, dict) else {}
            ev.append(f"Kubernetes `{version.data['deployment']}`: image `{version.data['image']}`, "
                      f"{version.data['replicas']['ready']}/{version.data['replicas']['desired']} ready, serving "
                      f"model v{served.get('version', '?')} (run `{str(served.get('run_id', '?'))[:8]}`) "
                      "[k8s_get_deployed_version]")
            if best_run and served.get("run_id") and served.get("run_id") != best_run["run_id"]:
                ev.append("The deployed model is **not** the best-scoring recent run [correlation]")
        else:
            unc.append(_fail_line(version))
        if health.ok:
            ev.append(f"Pod health: {len(health.data['healthy_pods'])} healthy, "
                      f"{len(health.data['unhealthy_pods'])} unhealthy [k8s_find_unhealthy_pods]")
        else:
            unc.append(_fail_line(health))
        runs = call("airflow_get_dag_runs", limit=5)
        if runs.ok:
            ev.append(f"Airflow `{runs.data['dag_id']}` last runs: {runs.data['state_counts']}, latest "
                      f"**{runs.data['latest_state']}** [airflow_get_dag_runs]")
        else:
            unc.append(_fail_line(runs))
        assess = call("assess_retraining_need")
        actions: list[str] = []
        if assess.ok:
            a = assess.data
            ev += [f"{f} [assess_retraining_need]" for f in a["facts"]]
            unc += a["uncertainties"]
            if a["retraining_recommended"]:
                summary = "**Retraining is recommended** because: " + "; ".join(a["triggers"]) + "."
                actions.append("Trigger `ml_training_pipeline` explicitly (the agent will ask for confirmation); "
                               "it was NOT started automatically.")
            else:
                summary = ("**Retraining is not necessary** based on the recorded evidence: no policy trigger fired "
                           f"(freshness {a['policy']['max_model_age_days']}d, F1 floor {a['policy']['min_f1']}, "
                           "data version, quality gate).")
            if health.ok and health.data["unhealthy_pods"]:
                actions.append("Fix the unhealthy pods first - this is an operational issue, not a model issue.")
            if not runs.ok or (runs.ok and runs.data["latest_state"] == "failed"):
                actions.append("Investigate the latest failed pipeline run before any retraining.")
        else:
            summary = "The retraining policy could not be evaluated."
            unc.append(_fail_line(assess))
        return _answer(summary, ev, unc, actions or None)

    # ------------------------------------------------------------------ help
    def _h_help(self, q: str, call) -> str:
        return ("I can answer questions about **DVC** datasets (versions, changes, comparisons, reproduction), "
                "**MLflow** experiments (runs, best model, comparisons, registry), **Airflow** pipelines (DAGs, "
                "run status, failed tasks, logs, triggering) and **Kubernetes** (pods, services, logs, health "
                "diagnosis, deployed version). Example: *Find the best recent model, identify the dataset version "
                "used to train it, check the deployment status, and explain whether retraining is necessary.*")
