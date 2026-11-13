"""Inference API: health semantics, prediction contract and model-missing behaviour."""

from __future__ import annotations

import importlib
import json

import joblib
import pytest
from fastapi.testclient import TestClient
from sklearn.datasets import load_breast_cancer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


def _client(monkeypatch, model_path, meta_path):
    monkeypatch.setenv("MODEL_PATH", str(model_path))
    monkeypatch.setenv("MODEL_METADATA_PATH", str(meta_path))
    monkeypatch.setenv("APP_VERSION", "9.9.9")
    import api.main as module

    module = importlib.reload(module)
    return TestClient(module.app), module


@pytest.fixture
def model_files(tmp_path):
    data = load_breast_cancer()
    model = Pipeline([("s", StandardScaler()), ("m", LogisticRegression(max_iter=2000))])
    model.fit(data.data, 1 - data.target)
    joblib.dump(model, tmp_path / "model.joblib")
    (tmp_path / "meta.json").write_text(json.dumps({"name": "m", "version": "7", "dataset_version": "data-v2.0"}))
    return tmp_path / "model.joblib", tmp_path / "meta.json", data


def test_ready_and_predict_with_model(monkeypatch, model_files):
    model_path, meta_path, data = model_files
    client, module = _client(monkeypatch, model_path, meta_path)
    with client:
        assert client.get("/health/live").json() == {"status": "alive"}
        ready = client.get("/health/ready")
        assert ready.status_code == 200 and ready.json()["model_version"] == "7"
        row = dict(zip(module.FEATURE_COLUMNS, data.data[0].tolist(), strict=True))
        resp = client.post("/predict", json={"instances": [row]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["model_version"] == "7" and body["app_version"] == "9.9.9"
        assert body["predictions"][0]["label"] in {"malignant", "benign"}
        assert client.get("/version").json()["model"]["dataset_version"] == "data-v2.0"


def test_missing_model_fails_readiness_but_not_liveness(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path / "missing.joblib", tmp_path / "missing.json")
    with client:
        assert client.get("/health/live").status_code == 200
        ready = client.get("/health/ready")
        assert ready.status_code == 503 and "Model file not found" in ready.json()["reason"]
        assert client.post("/predict", json={"instances": [{"x": 1.0}]}).status_code == 422
        assert client.get("/version").json()["model_loaded"] is False


def test_predict_rejects_incomplete_features(monkeypatch, model_files):
    model_path, meta_path, _ = model_files
    client, _ = _client(monkeypatch, model_path, meta_path)
    with client:
        resp = client.post("/predict", json={"instances": [{"mean_radius": 10.0}]})
        assert resp.status_code == 422
