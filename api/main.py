"""FastAPI inference service for the cancer-risk classifier.

Configuration comes from environment variables (populated by a Kubernetes ConfigMap):

    APP_VERSION            application release (also exposed on /version)
    MODEL_PATH             path to the serialized sklearn pipeline (joblib)
    MODEL_METADATA_PATH    JSON file describing the model (registry version, run id, dataset version)
    PREDICTION_THRESHOLD   probability threshold for the malignant class
    LOG_LEVEL              logging level

Health model: liveness only checks that the process responds; readiness requires a loaded model,
so a Pod with a missing/corrupt model is removed from Service endpoints instead of serving errors.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

FEATURE_COLUMNS: list[str] = [
    "mean_radius", "mean_texture", "mean_perimeter", "mean_area", "mean_smoothness", "mean_compactness",
    "mean_concavity", "mean_concave_points", "mean_symmetry", "mean_fractal_dimension", "radius_error",
    "texture_error", "perimeter_error", "area_error", "smoothness_error", "compactness_error",
    "concavity_error", "concave_points_error", "symmetry_error", "fractal_dimension_error", "worst_radius",
    "worst_texture", "worst_perimeter", "worst_area", "worst_smoothness", "worst_compactness",
    "worst_concavity", "worst_concave_points", "worst_symmetry", "worst_fractal_dimension",
]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
                   "logger": record.name, "message": record.getMessage()}
        payload.update(getattr(record, "extra_fields", {}))
        return json.dumps(payload)


def _configure_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("inference-api")
    logger.handlers[:] = [handler]
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    return logger


LOG = _configure_logging()


class Settings:
    def __init__(self) -> None:
        self.app_version = os.getenv("APP_VERSION", "dev")
        self.model_path = Path(os.getenv("MODEL_PATH", "models/model.joblib"))
        self.metadata_path = Path(os.getenv("MODEL_METADATA_PATH", "models/model_metadata.json"))
        self.threshold = float(os.getenv("PREDICTION_THRESHOLD", "0.5"))
        self.environment = os.getenv("APP_ENV", "local")


class ModelState:
    def __init__(self) -> None:
        self.model: Any = None
        self.metadata: dict[str, Any] = {}
        self.error: str | None = None
        self.loaded_at: float | None = None

    def load(self, settings: Settings) -> None:
        self.model, self.error = None, None
        try:
            if not settings.model_path.exists():
                raise FileNotFoundError(f"Model file not found: {settings.model_path}")
            self.model = joblib.load(settings.model_path)
            if settings.metadata_path.exists():
                self.metadata = json.loads(settings.metadata_path.read_text())
            self.loaded_at = time.time()
            LOG.info("model loaded", extra={"extra_fields": {"model_path": str(settings.model_path),
                                                             "model_version": self.metadata.get("version")}})
        except Exception as exc:  # noqa: BLE001 - surface any load failure through readiness
            self.error = f"{type(exc).__name__}: {exc}"
            LOG.error("model load failed", extra={"extra_fields": {"error": self.error,
                                                                   "model_path": str(settings.model_path)}})


settings = Settings()
state = ModelState()


@asynccontextmanager
async def lifespan(_: FastAPI):
    state.load(settings)
    LOG.info("service started", extra={"extra_fields": {"app_version": settings.app_version,
                                                        "ready": state.model is not None}})
    yield


app = FastAPI(title="Cancer Risk Inference API", version=settings.app_version, lifespan=lifespan)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    request_id = request.headers.get("x-request-id", uuid.uuid4().hex[:12])
    started = time.perf_counter()
    response: Response = await call_next(request)
    if not request.url.path.startswith("/health"):
        LOG.info("request", extra={"extra_fields": {
            "request_id": request_id, "method": request.method, "path": request.url.path,
            "status": response.status_code, "duration_ms": round((time.perf_counter() - started) * 1000, 2)}})
    response.headers["x-request-id"] = request_id
    return response


class PredictRequest(BaseModel):
    instances: list[dict[str, float]] = Field(..., min_length=1, max_length=1000)

    @field_validator("instances")
    @classmethod
    def check_features(cls, rows: list[dict[str, float]]) -> list[dict[str, float]]:
        for i, row in enumerate(rows):
            missing = [c for c in FEATURE_COLUMNS if c not in row]
            if missing:
                raise ValueError(f"instance {i} missing features: {missing}")
        return rows


class Prediction(BaseModel):
    malignant_probability: float
    label: str


class PredictResponse(BaseModel):
    predictions: list[Prediction]
    model_version: str | None
    app_version: str


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready")
def ready(response: Response) -> dict[str, Any]:
    if state.model is None:
        response.status_code = 503
        return {"status": "not_ready", "reason": state.error or "model not loaded"}
    return {"status": "ready", "model_version": state.metadata.get("version")}


@app.get("/version")
def version() -> dict[str, Any]:
    return {"app_version": settings.app_version, "environment": settings.environment,
            "model_loaded": state.model is not None, "model": state.metadata, "model_error": state.error}


@app.get("/metadata")
def metadata() -> dict[str, Any]:
    return {"features": FEATURE_COLUMNS, "positive_class": "malignant", "threshold": settings.threshold}


@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest) -> PredictResponse:
    if state.model is None:
        raise HTTPException(status_code=503, detail=f"model unavailable: {state.error}")
    frame = pd.DataFrame(req.instances)[FEATURE_COLUMNS]
    proba = state.model.predict_proba(frame)[:, 1]
    preds = [Prediction(malignant_probability=round(float(p), 6),
                        label="malignant" if p >= settings.threshold else "benign") for p in proba]
    return PredictResponse(predictions=preds, model_version=str(state.metadata.get("version")),
                           app_version=settings.app_version)
