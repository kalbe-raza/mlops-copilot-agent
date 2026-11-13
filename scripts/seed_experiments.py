"""Run a batch of training experiments on the *currently checked-out* DVC dataset version.

Each configuration goes through the full pipeline (validate -> preprocess -> train -> evaluate ->
quality gate) and is logged to MLflow as ``run-NN-<model>``. The best run that passes the quality
gate is then registered in the model registry (and becomes champion if it beats the current one).

Usage:  python scripts/seed_experiments.py --experiment cancer-baseline-v1 --suite v1
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MLFLOW_TRACKING_URI", "http://127.0.0.1:5000")
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

SUITES = {
    "v1": [
        ("logistic_regression", {"C": 1.0}),
        ("logistic_regression", {"C": 0.05}),
        ("random_forest", {"n_estimators": 100}),
        ("gradient_boosting", {"n_estimators": 100, "learning_rate": 0.1}),
        ("svm", {"C": 1.0}),
        ("decision_stump", {}),
    ],
    "v2": [
        ("logistic_regression", {"C": 1.0}),
        ("logistic_regression", {"C": 10.0}),
        ("random_forest", {"n_estimators": 300, "max_depth": 8}),
        ("gradient_boosting", {"n_estimators": 200, "learning_rate": 0.05, "max_depth": 2}),
        ("svm", {"C": 3.0}),
        ("decision_stump", {}),
    ],
}


def next_run_number() -> int:
    from mlflow.tracking import MlflowClient

    client = MlflowClient()
    ids = [e.experiment_id for e in client.search_experiments()]
    runs = client.search_runs(ids, filter_string="tags.mlflow.runName LIKE 'run-%'", max_results=1000)
    nums = []
    for r in runs:
        part = r.info.run_name.split("-")[1]
        if part.isdigit():
            nums.append(int(part))
    return max(nums, default=0) + 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--suite", choices=sorted(SUITES), required=True)
    args = parser.parse_args()

    number = next_run_number()
    passing: list[tuple[float, Path]] = []
    for model_type, params in SUITES[args.suite]:
        name = f"run-{number:02d}-{model_type}"
        workdir = ROOT / "artifacts" / "runs" / name
        cmd = [sys.executable, "-m", "training.train", "run-all", "--workdir", str(workdir),
               "--model-type", model_type, "--params", json.dumps(params), "--experiment", args.experiment,
               "--run-name", name, "--tags", json.dumps({"pipeline.trigger": "seed_experiments"}), "--no-register"]
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
        state = json.loads((workdir / "state.json").read_text())
        gate = state.get("quality_gate", {}).get("status", "failed")
        f1 = state.get("metrics", {}).get("f1")
        print(f"{name:34s} f1={f1:.4f} gate={gate} exit={proc.returncode}")
        if gate == "passed":
            passing.append((f1, workdir))
        number += 1
    if passing:
        best_f1, best_dir = max(passing, key=lambda x: x[0])
        subprocess.run([sys.executable, "-m", "training.train", "register", "--workdir", str(best_dir)],
                       cwd=ROOT, check=True)
        print(f"registered best passing run {best_dir.name} (f1={best_f1:.4f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
