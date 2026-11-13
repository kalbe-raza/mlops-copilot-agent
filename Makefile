# MLOps Copilot - common workflows. Run `make help` for the list.
SHELL := /bin/bash
PY := .venv/bin/python
IMAGE ?= mlops-inference-api
TAG ?= 1.0.0
CLUSTER ?= mlops
export MLFLOW_TRACKING_URI ?= http://127.0.0.1:5000
export MLFLOW_DISABLE_AGENT_HINT := 1

.PHONY: help setup airflow-setup mlflow airflow cluster image deploy kubeconfig up down cluster-down \
        seed test test-dag lint chat demo inject-data-failure resolve-data-failure \
        inject-k8s-failure resolve-k8s-failure

help:  ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-22s %s\n", $$1, $$2}'

setup:  ## Create the project virtualenv (uv, Python 3.12) from uv.lock
	uv sync --frozen --python 3.12

airflow-setup:  ## Create the isolated Airflow 3.4 virtualenv (.venv-airflow)
	uv venv --python 3.12 .venv-airflow
	VIRTUAL_ENV=.venv-airflow uv pip install "apache-airflow==3.4.0" pytest \
	  --constraint https://raw.githubusercontent.com/apache/airflow/constraints-3.4.0/constraints-3.12.txt

mlflow:  ## Start the MLflow tracking server on :5000
	./scripts/start_mlflow.sh

airflow:  ## Start Airflow 3 (standalone) on :8080
	./scripts/start_airflow.sh

cluster:  ## Create the local kind cluster
	kind get clusters | grep -qx $(CLUSTER) || kind create cluster --config kubernetes/kind-cluster.yaml

image:  ## Export the champion model from MLflow, build the image and load it into kind
	$(PY) scripts/export_model.py
	docker build -t $(IMAGE):$(TAG) --build-arg APP_VERSION=$(TAG) .
	kind load docker-image $(IMAGE):$(TAG) --name $(CLUSTER)

deploy:  ## Apply namespace, RBAC, ConfigMap, Deployment and Service
	kubectl apply -f kubernetes/namespace.yaml
	kubectl apply -f kubernetes/rbac.yaml -f kubernetes/configmap.yaml -f kubernetes/deployment.yaml -f kubernetes/service.yaml
	kubectl -n mlops-agent rollout status deploy/inference-api --timeout=180s

kubeconfig:  ## Generate the restricted, namespace-scoped kubeconfig used by the agent
	./scripts/make_agent_kubeconfig.sh 720h

up: mlflow airflow  ## Start MLflow + Airflow and ensure the cluster is running
	@docker start $(CLUSTER)-control-plane >/dev/null 2>&1 || $(MAKE) cluster
	@kubectl wait --for=condition=Ready node --all --timeout=120s >/dev/null && echo "kind cluster ready"

down:  ## Stop MLflow, Airflow and the kind node container (state is preserved)
	./scripts/stop_services.sh
	-docker stop $(CLUSTER)-control-plane

cluster-down:  ## Delete the kind cluster entirely
	kind delete cluster --name $(CLUSTER)

seed:  ## Run the six-model experiment suite on the checked-out dataset (registers best passing run)
	$(PY) scripts/seed_experiments.py --experiment cancer-tuning-v2 --suite v2

lint:  ## Ruff lint
	.venv/bin/ruff check .

test:  ## Unit tests (all external services mocked)
	.venv/bin/pytest

test-dag:  ## DAG integrity tests inside the Airflow virtualenv
	source scripts/airflow_env.sh && .venv-airflow/bin/python -m pytest --noconftest -p no:cacheprovider tests/test_dag.py -q

chat:  ## Interactive agent (Gemini planner, falls back to rules)
	$(PY) -m agent.main

inject-data-failure:  ## Failure 1: overwrite the tracked dataset with a corrupted file
	./scripts/failures.sh inject-data

resolve-data-failure:  ## Restore the committed dataset version with dvc checkout
	./scripts/failures.sh resolve-data

inject-k8s-failure:  ## Failure 2: break MODEL_PATH in the ConfigMap and restart pods
	./scripts/failures.sh inject-k8s

resolve-k8s-failure:  ## Restore the ConfigMap (then restart via the agent)
	./scripts/failures.sh resolve-k8s
