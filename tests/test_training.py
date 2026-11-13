"""Training pipeline: data validation, quality gate and promotion safety."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from training import train
from training.provenance import file_md5

ROOT = Path(__file__).resolve().parents[1]


def _make(tmp_path: Path, version: str) -> Path:
    out = tmp_path / f"{version}.csv"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_dataset.py"), "--version", version, "--out", str(out)],
                   check=True, capture_output=True)
    return out


def test_validation_accepts_clean_dataset(tmp_path):
    report = train.validate(_make(tmp_path, "v1"), tmp_path / "w")
    assert report["status"] == "passed" and report["rows"] == 400


def test_validation_rejects_corrupted_dataset(tmp_path):
    with pytest.raises(train.StageError) as exc:
        train.validate(_make(tmp_path, "corrupt"), tmp_path / "w")
    msg = str(exc.value)
    assert "missing columns: ['worst_concave_points']" in msg
    assert "null values found" in msg
    saved = json.loads((tmp_path / "w" / "validation.json").read_text())
    assert saved["status"] == "failed"
    with pytest.raises(train.StageError):  # downstream stages refuse to run on unvalidated data
        train.preprocess(tmp_path / "w")


def test_quality_gate_blocks_weak_model_and_promotion(tmp_path):
    work = tmp_path / "w"
    train.save_state(work, metrics={"f1": 0.83, "recall": 0.74, "f1_gap": 0.07},
                     provenance={"dataset.matches_dvc": "true"})
    with pytest.raises(train.StageError) as exc:
        train.quality_gate(work)
    assert "f1 0.8300 < required" in str(exc.value)
    assert train.load_state(work)["quality_gate"]["status"] == "failed"
    with pytest.raises(train.StageError, match="registration refused"):
        train.register(work, "unit-model")


def test_cli_exit_code_marks_deterministic_failures(tmp_path):
    code = train.main(["validate", "--workdir", str(tmp_path / "w"), "--data", str(_make(tmp_path, "corrupt"))])
    assert code == train.EXIT_DETERMINISTIC_FAILURE == 2


def test_end_to_end_run_records_provenance_and_registers(tmp_path, monkeypatch):
    data = _make(tmp_path, "v2")
    (tmp_path / "v2.csv.dvc").write_text(f"outs:\n- md5: {file_md5(data)}\n  size: {data.stat().st_size}\n"
                                         "  hash: md5\n  path: v2.csv\n")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{tmp_path / 'mlflow.db'}")
    work = tmp_path / "run"
    code = train.main(["run-all", "--workdir", str(work), "--data", str(data), "--model-type", "logistic_regression",
                       "--params", '{"C": 1.0}', "--experiment", "unit", "--model-name", "unit-model"])
    assert code == 0
    state = train.load_state(work)
    assert state["quality_gate"]["status"] == "passed"
    assert state["provenance"]["dataset.matches_dvc"] == "true"
    assert state["provenance"]["dataset.dvc_md5"] == file_md5(data)
    assert state["registry"]["version"] == "1" and state["registry"]["promoted_to_champion"] is True
