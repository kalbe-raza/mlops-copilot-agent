"""Export the registry model behind an alias (default: champion) for the inference image.

Writes models/model.joblib and models/model_metadata.json; the metadata (registry version, run id,
dataset version) is served by the API on /version so the deployment can be traced back to MLflow
and DVC.

Usage:  python scripts/export_model.py [--alias champion] [--model-name cancer-risk-classifier]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
from mlflow.tracking import MlflowClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-name", default="cancer-risk-classifier")
    parser.add_argument("--alias", default="champion")
    parser.add_argument("--out", default=str(ROOT / "models"))
    args = parser.parse_args()

    client = MlflowClient(os.getenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:5000"))
    mv = client.get_model_version_by_alias(args.model_name, args.alias)
    run = client.get_run(mv.run_id)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        local = client.download_artifacts(mv.run_id, "serving/model.joblib", tmp)
        shutil.copy(local, out / "model.joblib")
    tags = run.data.tags
    metadata = {
        "name": args.model_name,
        "version": mv.version,
        "alias": args.alias,
        "run_id": mv.run_id,
        "run_name": run.info.run_name,
        "model_type": tags.get("model.type"),
        "f1": round(run.data.metrics.get("f1", float("nan")), 4),
        "dataset_version": tags.get("dataset.version"),
        "dataset_md5": tags.get("dataset.dvc_md5"),
        "code_commit": tags.get("git.commit"),
        "exported_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
    }
    (out / "model_metadata.json").write_text(json.dumps(metadata, indent=2))
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
