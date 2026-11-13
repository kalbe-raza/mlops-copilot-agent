# MLOps Copilot Agent

[![CI](https://github.com/kalbe-raza/mlops-copilot-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/kalbe-raza/mlops-copilot-agent/actions/workflows/ci.yml)

A conversational AI agent for ML engineers. It answers operational questions by **calling real tools** on
**DVC** (dataset versions), **MLflow** (experiments and model registry), **Apache Airflow 3** (training
pipeline) and **Kubernetes** (FastAPI inference service), then explains the evidence. It never answers
operational questions from model memory: every fact comes from a tool result. State-changing actions need
explicit confirmation and are limited to approved resources.

> MLOps & Production AI Engineering - Assignment 03 (Fall 2026)
> **Syed Kalbe Raza** - 22i-0794 - Section B - Instructor: Hammad Majeed
> Technical report: [`docs/REPORT.md`](docs/REPORT.md) ([PDF](docs/REPORT.pdf)) - Architecture: [`architecture.md`](architecture.md)

```text
you> Find the best recent model, identify the dataset version used to train it, check the deployment
     status, and explain whether retraining is necessary.

Answer: Retraining is not necessary. Best run run-11-svm (F1 0.9709, quality gate passed) was trained on
dataset data-v2.0 (DVC md5 44042793..., commit cefe3d7), which is the current version; Kubernetes serves the
same model (cancer-risk-classifier v2) on 2/2 ready pods; the latest Airflow training run succeeded.
Uncertainty: no production degradation results were supplied.
[7 tool calls: mlflow x2, dvc x1, k8s x2, airflow x1, policy x1 - all success]
```

## Contents

1. [What it does](#what-it-does)
2. [Repository layout](#repository-layout)
3. [Prerequisites](#prerequisites)
4. [Installation](#installation)
5. [Environment configuration](#environment-configuration)
6. [Launching each service](#launching-each-service)
7. [Talking to the agent](#talking-to-the-agent)
8. [Tests and CI](#tests-and-ci)
9. [Failure injection demos](#failure-injection-demos)
10. [Evidence](#evidence)
11. [Security notes](#security-notes)

## What it does

| Module | Tools | Examples |
|---|---|---|
| DVC (8) | list tracked data, current version + Git commit, version history, compare versions, detect changes, resolve a run's md5 to a version, explain and perform hash-verified reproduction | "Which dataset version was used for run 11?" |
| MLflow (8) | list experiments/runs, run details, best run by metric (with caveats), compare runs or experiments, registry versions/aliases, run provenance | "Compare the last five training runs and recommend the best candidate based on F1." |
| Airflow (6) | list DAGs, run status, task instances, failed tasks with log error lines, task logs, **trigger approved DAG** (confirmation) | "Trigger the training pipeline and report whether it succeeded." |
| Kubernetes (9) | deployments, pods, logs, services + endpoints, unhealthy pods + events, events, deployed version (live `/version`), **restart/scale** (confirmation) | "Why is my prediction API unavailable?" |
| Policy (2) | retraining policy, evidence-based retraining assessment (never starts training) | mandatory multi-tool scenario |

Planning uses **Gemini function calling** (with a fallback chain of models for free-tier quotas). If no key
is configured or the LLM fails, a **deterministic rule-based planner** answers using the same tools, so CI
and demos work offline. Every tool call passes through one executor that validates input, enforces
policy and confirmation, applies timeouts, classifies failures and writes a structured JSON log line.

## Repository layout

```text
mlops-copilot-agent/
├── agent/                      # the AI agent
│   ├── main.py                 # CLI chat / one-shot interface
│   ├── orchestrator.py         # planner selection, LLM fallback, grounding guard
│   ├── llm.py                  # Gemini function-calling loop
│   ├── planner_rules.py        # deterministic planner (offline fallback, CI)
│   ├── schemas.py              # Pydantic tool inputs + ToolResult / AgentResponse
│   ├── config.py, logging_utils.py
│   └── tools/
│       ├── base.py             # ToolSpec, registry, safe executor
│       ├── dvc_tools.py  mlflow_tools.py  airflow_tools.py  kubernetes_tools.py  policy_tools.py
├── api/main.py                 # FastAPI inference service (+ requirements.txt for the image)
├── dags/training_pipeline.py   # Airflow 3 DAG: validate -> preprocess -> train -> evaluate -> gate -> register
├── training/                   # pipeline stages (train.py), dataset schema, provenance capture
├── data/raw/dataset.csv.dvc    # DVC-tracked dataset (tags data-v1.0, data-v2.0)
├── kubernetes/                 # namespace, deployment, service, configmap, rbac, kind cluster config
├── tests/                      # 79 pytest tests (76 unit + 3 DAG integrity; services mocked)
├── scripts/                    # service start/stop, seeding, export, failure injection, evidence capture
├── evidence/                   # recorded conversations, failure investigations, exported CLI evidence
├── screenshots/                # MLflow / Airflow UI, DVC / Kubernetes / agent terminal evidence
├── docs/REPORT.md, docs/REPORT.pdf, docs/img/
├── .github/workflows/ci.yml    # lint + tests + DAG validation + image build on PRs and pushes
├── Dockerfile  dvc.yaml  dvc.lock  params.yaml  pyproject.toml  uv.lock  Makefile
├── README.md
└── architecture.md
```

## Prerequisites

| Tool | Version used | Notes |
|---|---|---|
| Linux / macOS, 4 CPU, ~4 GB free RAM | Ubuntu, 6.7 GB | MLflow + Airflow + kind together use about 2.5 GB |
| [uv](https://docs.astral.sh/uv/) | 0.13 | installs Python 3.12 and locked dependencies |
| Docker | 29 | for kind and the inference image |
| [kind](https://kind.sigs.k8s.io/) + kubectl | 0.33 / 1.37 | local Kubernetes |
| Git | 2.5x | DVC provenance relies on Git history and tags |
| Gemini API key (optional) | - | free key from Google AI Studio; without it the rule planner is used |

## Installation

```bash
git clone https://github.com/kalbe-raza/mlops-copilot-agent.git && cd mlops-copilot-agent
make setup            # uv sync --frozen --python 3.12  -> .venv (agent, DVC, MLflow, training, API)
make airflow-setup    # separate .venv-airflow with apache-airflow==3.4.0 + official constraints
cp .env.example .env  # then fill in GEMINI_API_KEY (AIRFLOW_PASSWORD is generated by `make airflow`)
```

Airflow lives in its own virtualenv because its pinned constraints conflict with MLflow's. The DAG
calls the training stages through the project interpreter (`MLOPS_PYTHON`), so the two never mix.

## Environment configuration

All settings are environment variables (loaded from `.env`; see [`.env.example`](.env.example)):

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` (or `gemini_api_key`) | - | LLM planner; unset -> deterministic planner |
| `GEMINI_MODEL`, `GEMINI_FALLBACK_MODELS` | `gemini-2.5-flash`, `gemini-3-flash-preview,...` | model and quota fallbacks |
| `MLFLOW_TRACKING_URI` | `http://127.0.0.1:5000` | tracking server |
| `AIRFLOW_BASE_URL`, `AIRFLOW_USERNAME`, `AIRFLOW_PASSWORD` | `http://127.0.0.1:8080`, `agent`, generated | REST API v2 credentials (JWT) |
| `AIRFLOW_APPROVED_DAGS` | `ml_training_pipeline` | DAGs the agent may trigger |
| `AGENT_KUBECONFIG` | `./agent.kubeconfig` | restricted ServiceAccount kubeconfig |
| `K8S_NAMESPACE`, `K8S_APPROVED_NAMESPACES` | `mlops-agent` | namespace scope for all Kubernetes tools |
| `TOOL_TIMEOUT_SECONDS` | `45` | per-tool timeout (trigger-and-wait has its own) |
| `RETRAIN_MAX_MODEL_AGE_DAYS`, `RETRAIN_MIN_F1` | `30`, `0.93` | explicit retraining policy |

## Launching each service

Run these from the repository root, in order. Each step is idempotent.

```bash
# 1. Data: the dataset is DVC-tracked. Pull it from your DVC remote, or regenerate it exactly -
#    the generator is deterministic and its bytes match the committed md5 (CI verifies this).
.venv/bin/dvc pull || { .venv/bin/python scripts/make_dataset.py --version v2 \
                        && .venv/bin/dvc add data/raw/dataset.csv; }   # fills the cache; .dvc file unchanged
git status --short data/                         # -> empty: the workspace matches data-v2.0

# 2. MLflow tracking server -> http://127.0.0.1:5000
make mlflow

# 3. Experiment history: six-model suite on the current data version (registers the best passing
#    run as `champion`), then the reproducible DVC pipeline
make seed
PATH=$PWD/.venv/bin:$PATH dvc repro              # (re)builds pipeline_outputs/ from the versioned data
#    (The cancer-baseline-v1 experiment in evidence/ was produced the same way with
#     `scripts/seed_experiments.py --suite v1` while data-v1.0 was the committed version.)

# 4. Airflow 3 (api-server, scheduler, dag-processor, triggerer) -> http://127.0.0.1:8080
make airflow          # UI login: admin / password in airflow_home/passwords.json

# 5. Kubernetes: kind cluster, image built from the MLflow champion model, manifests, agent kubeconfig
make cluster image deploy kubeconfig
kubectl -n mlops-agent get pods,svc

# Stop everything (state is kept)  /  start again later
make down
make up
```

The inference API can be reached through the API server proxy without port-forwarding:
`kubectl get --raw /api/v1/namespaces/mlops-agent/services/inference-api:http/proxy/version`.

## Talking to the agent

```bash
make chat                                          # interactive (Gemini; falls back to rules)
.venv/bin/python -m agent.main ask "Which dataset version was used for run 11?"
.venv/bin/python -m agent.main --planner rules ask "Why is my prediction API unavailable?"
.venv/bin/python -m agent.main tools               # 33 tools with their permissions
.venv/bin/python -m agent.main --yes ask "Trigger the training pipeline and report the result"
.venv/bin/python -m agent.main --json ask "..."    # full structured response incl. raw tool outputs
.venv/bin/python -m agent.main --transcript evidence/my-session.md ask "..."
```

State-changing tools (`airflow_trigger_dag`, `k8s_restart_deployment`, `k8s_scale_deployment`) always show
a confirmation prompt. Without a terminal they are rejected unless `--yes` is given, which counts as the
explicit confirmation. Each answer ends with a table of the tool calls made (status, duration, request id),
and `logs/agent.jsonl` holds the structured log.

## Tests and CI

```bash
make lint        # ruff
make test        # 76 tests, no live services needed (test_dag.py is skipped here and runs below)
make test-dag    # 3 DAG integrity tests inside the Airflow virtualenv
```

| Area | What is tested |
|---|---|
| Executor | success path, unknown tool, invalid arguments, timeouts, unavailable/error classification, confirmation, policy-before-prompt, secrets never logged |
| Kubernetes | unapproved namespace and unmanaged deployment rejected **without an API call**, missing confirmation rejected, RBAC 403 -> denied, unreachable cluster -> unavailable, unhealthy pod detection |
| Airflow | JWT bearer auth + refresh, unapproved DAG / disallowed conf denied, trigger-and-wait, failed-task diagnosis, unreachable API |
| MLflow | best run + caveats, comparison excludes failed gate, experiment comparison, provenance by run number, registry aliases, server down -> unavailable (temporary SQLite store) |
| DVC | versions/history, compare, change detection, md5 resolution, hash-verified `dvc get` reproduction, command allow-list (temporary Git+DVC repo) |
| Agent | tool selection for 12 intents, all-four-technology scenario without triggering retraining, **no invented results when tools are unavailable**, LLM hallucination replaced, unverified numbers flagged, LLM fallback, no repeated side effects, Gemini loop |
| Pipeline / API / manifests | corrupt data rejected, quality gate blocks promotion, end-to-end run with provenance, API health semantics, manifest requirements, least-privilege RBAC |

GitHub Actions ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs on every pull request and push
to `main`: **lint + unit tests** (with a check that the regenerated dataset matches the committed DVC md5),
**Airflow 3 DAG validation** in a separate job with the official constraints, and an **image build and smoke
test** that trains a model, exports it and checks `/health/ready`. External services are mocked.

## Failure injection demos

```bash
make inject-data-failure   # corrupt the DVC-tracked dataset (dropped column + 40 nulls)
#   ask: "Trigger the training pipeline and report whether it succeeded." -> validate_data fails, nothing promoted
#   ask: "Why did the latest training pipeline run fail?"                 -> root cause from task logs + dvc status
make resolve-data-failure  # dvc checkout --force

make inject-k8s-failure    # MODEL_PATH -> missing file in the ConfigMap, pods recreated
#   ask: "Why is my prediction API unavailable?" -> 0 ready endpoints, readiness 503, FileNotFoundError in logs
make resolve-k8s-failure   # re-apply the ConfigMap, then: "Restart the inference deployment" (confirmation)
```

Full transcripts: [`evidence/failure-1-data-validation.md`](evidence/failure-1-data-validation.md),
[`evidence/failure-2-k8s-config.md`](evidence/failure-2-k8s-config.md).

## Evidence

| Item | Location |
|---|---|
| Five agent conversations with live tool outputs | [`evidence/conversations.md`](evidence/conversations.md) |
| Two failure investigations (root cause and resolution) | [`evidence/failure-*.md`](evidence/) |
| Unexpected failure found while testing (duplicate trigger) | [`evidence/quality-gate-block.md`](evidence/quality-gate-block.md) |
| Exported DVC / MLflow / Airflow / Kubernetes CLI and API evidence | [`evidence/*.txt`](evidence/) (`scripts/export_evidence.sh`) |
| Structured agent log sample | [`evidence/agent-structured-logs.jsonl`](evidence/agent-structured-logs.jsonl) |
| Screenshots (MLflow UI, Airflow UI, DVC, Kubernetes, agent) | [`screenshots/`](screenshots/) (`scripts/capture_screenshots.py`) |

## Security notes

* No secrets in the repository: `.env`, `airflow_home/`, `agent.kubeconfig` and all local state are git-ignored.
* The agent talks to Kubernetes as a namespaced ServiceAccount (`kubernetes/rbac.yaml`): read-only plus
  `patch` on the single `inference-api` Deployment. It cannot delete anything or read other namespaces.
* Airflow is accessed as a dedicated `agent` user (role `user`), not as admin.
* Logs redact keys, passwords, tokens, bearer headers and any registered secret value.
* DVC/Git access is limited to an allow-list of read-only sub-commands run without a shell.
