"""Training pipeline stages: validate -> preprocess -> train -> evaluate (MLflow) -> quality gate -> register.

Every stage is an independent CLI command so it can be orchestrated by Apache Airflow
(one task per stage) or reproduced with ``dvc repro``. Stages exchange state through a
work directory containing ``state.json`` plus intermediate files.

Examples
--------
    python -m training.train validate   --workdir artifacts/runs/demo --data data/raw/dataset.csv
    python -m training.train preprocess --workdir artifacts/runs/demo
    python -m training.train train      --workdir artifacts/runs/demo --model-type random_forest
    python -m training.train evaluate   --workdir artifacts/runs/demo --experiment cancer-airflow-pipeline
    python -m training.train quality-gate --workdir artifacts/runs/demo
    python -m training.train register   --workdir artifacts/runs/demo
    python -m training.train run-all    --workdir artifacts/runs/demo --data data/raw/dataset.csv
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC
from sklearn.tree import DecisionTreeClassifier

from training import schema
from training.provenance import PROJECT_ROOT, dataset_provenance

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

LOG = logging.getLogger("training")
DEFAULT_MODEL_NAME = "cancer-risk-classifier"
DEFAULT_EXPERIMENT = "cancer-airflow-pipeline"
RANDOM_STATE = 42
EXIT_DETERMINISTIC_FAILURE = 2  # any other non-zero exit is treated as transient and retried by Airflow

# Quality-gate thresholds (overridable through environment variables).
GATE_MIN_F1 = float(os.getenv("GATE_MIN_F1", "0.93"))
GATE_MIN_RECALL = float(os.getenv("GATE_MIN_RECALL", "0.90"))
GATE_MAX_F1_GAP = float(os.getenv("GATE_MAX_F1_GAP", "0.06"))


class StageError(RuntimeError):
    """Raised when a stage must fail (and stop downstream promotion)."""


# --------------------------------------------------------------------------- state helpers
def _state_path(workdir: Path) -> Path:
    return workdir / "state.json"


def load_state(workdir: Path) -> dict[str, Any]:
    path = _state_path(workdir)
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(workdir: Path, **updates: Any) -> dict[str, Any]:
    workdir.mkdir(parents=True, exist_ok=True)
    state = load_state(workdir)
    state.update(updates)
    _state_path(workdir).write_text(json.dumps(state, indent=2, sort_keys=True, default=str))
    return state


def _require(state: dict[str, Any], key: str, stage: str) -> Any:
    if key not in state:
        raise StageError(f"stage '{stage}' requires '{key}' from an earlier stage; run the pipeline in order")
    return state[key]


# --------------------------------------------------------------------------- stage: validate
def validate(data_path: Path, workdir: Path) -> dict[str, Any]:
    """Validate schema, completeness, label sanity and class balance. Raises StageError on failure."""
    LOG.info("validating dataset %s", data_path)
    if not data_path.exists():
        raise StageError(f"dataset not found: {data_path}")
    df = pd.read_csv(data_path)
    errors: list[str] = []
    missing = [c for c in schema.ALL_COLUMNS if c not in df.columns]
    unexpected = [c for c in df.columns if c not in schema.ALL_COLUMNS]
    if missing:
        errors.append(f"missing columns: {missing}")
    if unexpected:
        errors.append(f"unexpected columns: {unexpected}")
    if len(df) < schema.MIN_ROWS:
        errors.append(f"too few rows: {len(df)} < {schema.MIN_ROWS}")
    present = [c for c in schema.ALL_COLUMNS if c in df.columns]
    null_counts = {c: int(n) for c, n in df[present].isna().sum().items() if n > 0}
    if null_counts:
        errors.append(f"null values found: {null_counts}")
    if schema.ID_COLUMN in df.columns and df[schema.ID_COLUMN].duplicated().any():
        errors.append(f"duplicate {schema.ID_COLUMN} values: {int(df[schema.ID_COLUMN].duplicated().sum())}")
    positive_fraction = None
    if schema.TARGET_COLUMN in df.columns:
        labels = set(df[schema.TARGET_COLUMN].dropna().unique().tolist())
        if not labels <= {0, 1}:
            errors.append(f"invalid label values: {sorted(labels)}")
        positive_fraction = float(df[schema.TARGET_COLUMN].mean())
        if not schema.MIN_POSITIVE_FRACTION <= positive_fraction <= schema.MAX_POSITIVE_FRACTION:
            errors.append(f"class imbalance out of range: positive fraction {positive_fraction:.3f}")
    numeric = [c for c in schema.FEATURE_COLUMNS if c in df.columns]
    negatives = {c: int((df[c] < 0).sum()) for c in numeric if (df[c] < 0).any()}
    if negatives:
        errors.append(f"negative feature values: {negatives}")

    report = {
        "dataset_path": str(data_path),
        "rows": int(len(df)),
        "columns": int(df.shape[1]),
        "positive_fraction": positive_fraction,
        "errors": errors,
        "status": "failed" if errors else "passed",
    }
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "validation.json").write_text(json.dumps(report, indent=2))
    save_state(workdir, data_path=str(data_path.resolve()), validation=report)
    if errors:
        for err in errors:
            LOG.error("DATA VALIDATION FAILED: %s", err)
        raise StageError("data validation failed: " + "; ".join(errors))
    LOG.info("validation passed: %d rows, %d columns, positive fraction %.3f",
             report["rows"], report["columns"], positive_fraction or 0.0)
    return report


# --------------------------------------------------------------------------- stage: preprocess
def preprocess(workdir: Path, test_size: float = 0.25) -> dict[str, Any]:
    state = load_state(workdir)
    if state.get("validation", {}).get("status") != "passed":
        raise StageError("preprocess refused: validation has not passed for this run")
    data_path = Path(_require(state, "data_path", "preprocess"))
    df = pd.read_csv(data_path).drop_duplicates()
    train_df, test_df = train_test_split(
        df, test_size=test_size, stratify=df[schema.TARGET_COLUMN], random_state=RANDOM_STATE
    )
    train_df.to_csv(workdir / "train.csv", index=False)
    test_df.to_csv(workdir / "test.csv", index=False)
    info = {"train_rows": int(len(train_df)), "test_rows": int(len(test_df)), "test_size": test_size,
            "random_state": RANDOM_STATE}
    save_state(workdir, preprocess=info)
    LOG.info("preprocess complete: %s", info)
    return info


# --------------------------------------------------------------------------- stage: train
# Sensible per-model defaults; explicit params override them (so a DAG run that only sets model_type
# never receives hyper-parameters meant for a different estimator).
DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "logistic_regression": {"C": 1.0},
    "random_forest": {"n_estimators": 300, "max_depth": 8},
    "gradient_boosting": {"n_estimators": 200, "learning_rate": 0.05, "max_depth": 2},
    "svm": {"C": 3.0},
    "decision_stump": {},
}


def build_model(model_type: str, params: dict[str, Any]) -> Pipeline:
    params = {**DEFAULT_PARAMS.get(model_type, {}), **params}
    if model_type == "logistic_regression":
        est = LogisticRegression(max_iter=5000, random_state=RANDOM_STATE, **params)
    elif model_type == "random_forest":
        est = RandomForestClassifier(random_state=RANDOM_STATE, n_jobs=1, **params)
    elif model_type == "gradient_boosting":
        est = GradientBoostingClassifier(random_state=RANDOM_STATE, **params)
    elif model_type == "svm":
        est = SVC(probability=True, random_state=RANDOM_STATE, **params)
    elif model_type == "decision_stump":
        est = DecisionTreeClassifier(max_depth=1, random_state=RANDOM_STATE, **params)
    else:
        raise StageError(f"unsupported model_type {model_type!r}")
    return Pipeline([("scaler", StandardScaler()), ("model", est)])


def train(workdir: Path, model_type: str, params: dict[str, Any]) -> dict[str, Any]:
    state = load_state(workdir)
    _require(state, "preprocess", "train")
    train_df = pd.read_csv(workdir / "train.csv")
    x, y = train_df[schema.FEATURE_COLUMNS], train_df[schema.TARGET_COLUMN]
    params = {**DEFAULT_PARAMS.get(model_type, {}), **params}
    model = build_model(model_type, params)
    started = time.perf_counter()
    model.fit(x, y)
    duration = time.perf_counter() - started
    joblib.dump(model, workdir / "model.joblib")
    info = {"model_type": model_type, "params": params, "fit_seconds": round(duration, 4)}
    save_state(workdir, train=info)
    LOG.info("trained %s with %s in %.3fs", model_type, params, duration)
    return info


# --------------------------------------------------------------------------- stage: evaluate + MLflow
def compute_metrics(model: Pipeline, x: pd.DataFrame, y: pd.Series) -> dict[str, float]:
    pred = model.predict(x)
    proba = model.predict_proba(x)[:, 1]
    return {
        "accuracy": float(accuracy_score(y, pred)),
        "precision": float(precision_score(y, pred, zero_division=0)),
        "recall": float(recall_score(y, pred, zero_division=0)),
        "f1": float(f1_score(y, pred, zero_division=0)),
        "roc_auc": float(roc_auc_score(y, proba)),
    }


def evaluate(workdir: Path, experiment: str, run_name: str | None, extra_tags: dict[str, str]) -> dict[str, Any]:
    import mlflow
    from mlflow.models import infer_signature

    state = load_state(workdir)
    train_info = _require(state, "train", "evaluate")
    model = joblib.load(workdir / "model.joblib")
    train_df = pd.read_csv(workdir / "train.csv")
    test_df = pd.read_csv(workdir / "test.csv")
    x_test, y_test = test_df[schema.FEATURE_COLUMNS], test_df[schema.TARGET_COLUMN]
    test_metrics = compute_metrics(model, x_test, y_test)
    train_metrics = compute_metrics(model, train_df[schema.FEATURE_COLUMNS], train_df[schema.TARGET_COLUMN])
    metrics = dict(test_metrics)
    metrics["train_f1"] = train_metrics["f1"]
    metrics["f1_gap"] = train_metrics["f1"] - test_metrics["f1"]
    cm = confusion_matrix(y_test, model.predict(x_test)).tolist()

    data_path = Path(_require(state, "data_path", "evaluate"))
    provenance = dataset_provenance(data_path)
    tags = {
        **provenance,
        "model.type": train_info["model_type"],
        "pipeline.workdir": str(workdir),
        "dataset.rows": str(state["validation"]["rows"]),
        **{k: str(v) for k, v in extra_tags.items()},
    }

    mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", f"sqlite:///{PROJECT_ROOT / 'mlflow.db'}"))
    mlflow.set_experiment(experiment)
    with mlflow.start_run(run_name=run_name) as run:
        mlflow.log_params({"model_type": train_info["model_type"], **train_info["params"],
                           "test_size": state["preprocess"]["test_size"],
                           "random_state": state["preprocess"]["random_state"],
                           "train_rows": state["preprocess"]["train_rows"],
                           "test_rows": state["preprocess"]["test_rows"]})
        mlflow.log_metrics(metrics)
        mlflow.set_tags(tags)
        mlflow.log_dict({"confusion_matrix": cm, "labels": ["benign", "malignant"]}, "evaluation/confusion_matrix.json")
        mlflow.log_dict(metrics, "evaluation/metrics.json")
        mlflow.log_artifact(str(workdir / "validation.json"), artifact_path="data_validation")
        signature = infer_signature(x_test, model.predict(x_test))
        # skops serialisation: tree node storage must be explicitly trusted (we produced this model ourselves)
        info = mlflow.sklearn.log_model(model, name="model", signature=signature, input_example=x_test.head(3),
                                        skops_trusted_types=["sklearn.tree._tree.Tree"])
        mlflow.log_artifact(str(workdir / "model.joblib"), artifact_path="serving")
        run_id = run.info.run_id
        experiment_id = run.info.experiment_id

    (workdir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    save_state(workdir, mlflow={"run_id": run_id, "experiment_id": experiment_id, "experiment": experiment,
                                "model_uri": info.model_uri},
               metrics=metrics, provenance=provenance)
    LOG.info("logged MLflow run %s in experiment '%s': %s", run_id, experiment,
             {k: round(v, 4) for k, v in metrics.items()})
    return {"run_id": run_id, "metrics": metrics}


# --------------------------------------------------------------------------- stage: quality gate
def quality_gate(workdir: Path) -> dict[str, Any]:
    import mlflow

    state = load_state(workdir)
    metrics = _require(state, "metrics", "quality-gate")
    provenance = state.get("provenance", {})
    reasons: list[str] = []
    if metrics["f1"] < GATE_MIN_F1:
        reasons.append(f"f1 {metrics['f1']:.4f} < required {GATE_MIN_F1}")
    if metrics["recall"] < GATE_MIN_RECALL:
        reasons.append(f"recall {metrics['recall']:.4f} < required {GATE_MIN_RECALL}")
    if metrics["f1_gap"] > GATE_MAX_F1_GAP:
        reasons.append(f"train/test f1 gap {metrics['f1_gap']:.4f} > allowed {GATE_MAX_F1_GAP} (overfitting)")
    if provenance.get("dataset.matches_dvc") != "true":
        reasons.append("training data does not match a DVC-tracked dataset version")
    status = "failed" if reasons else "passed"
    result = {"status": status, "reasons": reasons,
              "thresholds": {"min_f1": GATE_MIN_F1, "min_recall": GATE_MIN_RECALL, "max_f1_gap": GATE_MAX_F1_GAP}}

    run_id = state.get("mlflow", {}).get("run_id")
    if run_id:
        mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", f"sqlite:///{PROJECT_ROOT / 'mlflow.db'}"))
        client = mlflow.MlflowClient()
        client.set_tag(run_id, "quality_gate.status", status)
        client.set_tag(run_id, "quality_gate.reasons", "; ".join(reasons) if reasons else "none")
    save_state(workdir, quality_gate=result)
    if reasons:
        for reason in reasons:
            LOG.error("QUALITY GATE FAILED: %s", reason)
        raise StageError("quality gate failed: " + "; ".join(reasons))
    LOG.info("quality gate passed: %s", result["thresholds"])
    return result


# --------------------------------------------------------------------------- stage: register / promote
def register(workdir: Path, model_name: str) -> dict[str, Any]:
    import mlflow

    state = load_state(workdir)
    if state.get("quality_gate", {}).get("status") != "passed":
        raise StageError("registration refused: quality gate has not passed for this run")
    run = _require(state, "mlflow", "register")
    metrics = state["metrics"]
    mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", f"sqlite:///{PROJECT_ROOT / 'mlflow.db'}"))
    client = mlflow.MlflowClient()
    version = mlflow.register_model(run["model_uri"], model_name)
    vnum = str(version.version)  # int on SQLite stores, str over REST: normalise
    prov = state.get("provenance", {})
    for key in ("dataset.version", "dataset.dvc_md5", "dataset.git_commit"):
        if key in prov:
            client.set_model_version_tag(model_name, vnum, key, prov[key])
    client.set_model_version_tag(model_name, vnum, "f1", f"{metrics['f1']:.4f}")
    client.set_model_version_tag(model_name, vnum, "quality_gate", "passed")
    client.set_model_version_tag(model_name, vnum, "source_run_id", run["run_id"])

    promoted = False
    champion_f1 = None
    try:
        champion = client.get_model_version_by_alias(model_name, "champion")
        champion_f1 = client.get_run(champion.run_id).data.metrics.get("f1")
    except Exception:  # noqa: BLE001 - no champion yet
        champion = None
    if champion is None or champion_f1 is None or metrics["f1"] >= champion_f1:
        client.set_registered_model_alias(model_name, "champion", vnum)
        promoted = True
    client.set_registered_model_alias(model_name, "latest-passed", vnum)
    result = {"model_name": model_name, "version": vnum, "promoted_to_champion": promoted,
              "previous_champion_f1": champion_f1}
    save_state(workdir, registry=result)
    LOG.info("registered %s v%s (champion=%s)", model_name, vnum, promoted)
    return result


# --------------------------------------------------------------------------- CLI
def _setup_logging() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s", stream=sys.stdout)
    logging.getLogger("mlflow").setLevel(logging.WARNING)
    logging.getLogger("alembic").setLevel(logging.WARNING)


def _parse_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("expected a JSON object")
    return parsed


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="training.train", description="ML training pipeline stages")
    parser.add_argument("stage", choices=["validate", "preprocess", "train", "evaluate", "quality-gate",
                                          "register", "run-all"])
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--data", type=Path, default=PROJECT_ROOT / "data" / "raw" / "dataset.csv")
    parser.add_argument("--model-type", default="random_forest")
    parser.add_argument("--params", default="{}", help="JSON object of estimator hyper-parameters")
    parser.add_argument("--params-file", type=Path, default=None,
                        help="YAML file with train.model_type / train.params (used by dvc.yaml)")
    parser.add_argument("--experiment", default=DEFAULT_EXPERIMENT)
    parser.add_argument("--run-name", default=None)
    parser.add_argument("--tags", default="{}", help="JSON object of extra MLflow tags")
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--no-register", action="store_true", help="run-all: stop after the quality gate")
    args = parser.parse_args(argv)
    np.random.seed(RANDOM_STATE)
    if args.params_file is not None:
        import yaml

        cfg = (yaml.safe_load(args.params_file.read_text()) or {}).get("train", {})
        args.model_type = cfg.get("model_type", args.model_type)
        args.params = json.dumps(cfg.get("params") or {})
    workdir: Path = args.workdir
    try:
        if args.stage == "validate":
            validate(args.data, workdir)
        elif args.stage == "preprocess":
            preprocess(workdir)
        elif args.stage == "train":
            train(workdir, args.model_type, _parse_json(args.params))
        elif args.stage == "evaluate":
            evaluate(workdir, args.experiment, args.run_name, _parse_json(args.tags))
        elif args.stage == "quality-gate":
            quality_gate(workdir)
        elif args.stage == "register":
            register(workdir, args.model_name)
        else:
            validate(args.data, workdir)
            preprocess(workdir)
            train(workdir, args.model_type, _parse_json(args.params))
            evaluate(workdir, args.experiment, args.run_name, _parse_json(args.tags))
            quality_gate(workdir)
            if not args.no_register:
                register(workdir, args.model_name)
    except StageError as exc:
        # Exit code 2 = deterministic failure (bad data, failed gate): orchestrators must not retry.
        LOG.error("stage '%s' failed: %s", args.stage, exc)
        return EXIT_DETERMINISTIC_FAILURE
    return 0


if __name__ == "__main__":
    sys.exit(main())
