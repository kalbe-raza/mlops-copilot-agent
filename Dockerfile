# Inference image for the cancer-risk classifier (FastAPI + scikit-learn).
# Build:  python scripts/export_model.py && docker build -t mlops-inference-api:1.0.0 .
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY api/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY api/__init__.py api/main.py /app/api/
COPY models/model.joblib models/model_metadata.json /app/models/

RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER 10001

ARG APP_VERSION=1.0.0
ENV APP_VERSION=${APP_VERSION} \
    MODEL_PATH=/app/models/model.joblib \
    MODEL_METADATA_PATH=/app/models/model_metadata.json

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
