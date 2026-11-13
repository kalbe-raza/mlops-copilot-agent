# Architecture - MLOps Copilot Agent

This document describes how the agent, its tools and the four MLOps systems fit together, why each
technology was chosen, and which safety controls apply to every request. Diagrams are Mermaid
(rendered by GitHub); PNG renders are in `docs/img/`.

## 1. Component view

```mermaid
flowchart TB
    U([ML engineer]) -->|natural language| CLI["CLI chat<br/>agent/main.py"]
    CLI --> ORCH["Orchestrator<br/>agent/orchestrator.py"]
    ORCH --> PLAN{Planner}
    PLAN -->|primary| GEM["Gemini function calling<br/>agent/llm.py<br/>(model fallback chain)"]
    PLAN -->|offline / LLM failure| RULES["Deterministic planner<br/>agent/planner_rules.py"]
    GEM --> EXEC
    RULES --> EXEC
    EXEC["ToolExecutor<br/>schema validation - policy - confirmation<br/>timeout - error classes - JSON logs"]
    EXEC --> DVCT["DVC tools (8)"]
    EXEC --> MLT["MLflow tools (8)"]
    EXEC --> AFT["Airflow tools (6)"]
    EXEC --> K8T["Kubernetes tools (9)"]
    EXEC --> POL["Policy tools (2)<br/>retraining assessment"]
    POL -.reads.-> DVCT & MLT & AFT & K8T
    DVCT -->|allow-listed git/dvc argv| REPO[("Git + DVC repo<br/>data/raw/dataset.csv.dvc<br/>local DVC remote")]
    MLT -->|MlflowClient| MLF[("MLflow server :5000<br/>SQLite + artifact store<br/>model registry")]
    AFT -->|REST /api/v2 + JWT| AF["Airflow 3.4 :8080<br/>scheduler, dag-processor,<br/>LocalExecutor"]
    K8T -->|kubernetes client<br/>namespaced ServiceAccount| K8S["kind cluster<br/>ns mlops-agent"]
    AF -->|ml_training_pipeline| TRAIN["training/train.py stages"]
    TRAIN -->|params, metrics, tags, model| MLF
    TRAIN -->|reads versioned data| REPO
    MLF -->|champion model export| IMG["inference image<br/>mlops-inference-api:1.0.0"]
    IMG --> K8S
    ORCH --> GUARD["Grounding guard<br/>+ tool notes"]
    EXEC --> LOG[("logs/agent.jsonl")]
```

| Layer | Responsibility | Key files |
|---|---|---|
| Interface | Chat / one-shot CLI, confirmation prompt, transcripts | `agent/main.py` |
| Orchestration | Request ids, planner selection, LLM fallback without repeating state changes, grounding guard | `agent/orchestrator.py` |
| Planning | Gemini function calling over JSON-schema tool declarations; deterministic rule planner | `agent/llm.py`, `agent/planner_rules.py` |
| Execution | Validation, authorization, confirmation, timeout, error classification, structured logging | `agent/tools/base.py`, `agent/logging_utils.py` |
| Tools | 33 tools across 5 modules with Pydantic inputs | `agent/tools/*.py`, `agent/schemas.py` |
| Data | DVC-versioned dataset (tags `data-v1.0`, `data-v2.0`), `dvc.yaml` pipeline, local remote | `data/`, `dvc.yaml`, `dvc.lock` |
| Experiments | MLflow tracking server, experiments, registry with `champion` alias | `training/train.py` |
| Orchestration of training | Airflow 3 DAG with retries, fail-fast and quality gate | `dags/training_pipeline.py` |
| Serving | FastAPI image on Kubernetes (2 replicas, probes, limits, ConfigMap, RBAC) | `api/`, `Dockerfile`, `kubernetes/` |

## 2. Request lifecycle (mandatory integration scenario)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant O as Orchestrator
    participant P as Planner (Gemini)
    participant X as ToolExecutor
    participant M as MLflow
    participant D as DVC
    participant K as Kubernetes
    participant A as Airflow
    User->>O: best model + dataset + deployment + retrain?
    O->>P: question + 33 tool declarations
    P->>X: mlflow_find_best_run(metric=f1)
    X->>M: search_runs / registry
    P->>X: mlflow_get_run_dataset_provenance(run_id)
    X->>M: run tags (dataset.dvc_md5, dataset.version)
    P->>X: dvc_resolve_dataset_reference(md5)
    X->>D: git log / git show *.dvc (allow-listed)
    P->>X: k8s_get_deployed_version + k8s_find_unhealthy_pods
    X->>K: Deployment, ConfigMap, Pods, /version via service proxy
    P->>X: airflow_get_dag_runs
    X->>A: GET /api/v2/dags/ml_training_pipeline/dagRuns (Bearer JWT)
    P->>X: assess_retraining_need
    X-->>P: facts, uncertainties, triggers (never starts training)
    P-->>O: Answer / Evidence / Uncertainties / Recommended actions
    O->>O: grounding guard (all failed -> failure report, unverified numbers flagged)
    O-->>User: answer + tool evidence table (request id)
```

## 3. Training pipeline (Airflow DAG `ml_training_pipeline`)

```mermaid
flowchart LR
    V[validate_data] --> P[preprocess_data] --> T[train_model] --> E[evaluate_and_log<br/>MLflow run + provenance tags] --> G{quality_gate}
    G -->|passed| R[register_model<br/>registry version, champion alias if better]
    G -->|failed| X[register_model = upstream_failed<br/>nothing promoted]
    V -->|invalid data, exit 2| F[fail fast - no retry<br/>downstream upstream_failed]
```

* **Retries**: every task `retries=2`, `retry_delay=15s`, exponential back-off, `execution_timeout=15m`.
* **Fail-fast**: stages exit with code `2` for deterministic failures (bad data, failed gate); the DAG maps
  that to `AirflowFailException`, so retries are only spent on transient failures.
* **Quality gate**: F1 >= 0.93, recall >= 0.90, train/test F1 gap <= 0.06, and the training data must match
  a DVC-tracked version. `register_model` uses `all_success`, so a failed validation or gate prevents promotion.
* The same stage commands back `dvc repro` (`dvc.yaml`), so the pipeline is reproducible without Airflow.

## 4. Provenance chain

```mermaid
flowchart LR
    TAG["Git tag data-v2.0<br/>commit cefe3d7"] --> DVCF["data/raw/dataset.csv.dvc<br/>md5 44042793..."]
    DVCF --> RUN["MLflow run tags<br/>dataset.version, dataset.dvc_md5,<br/>dataset.git_commit, git.commit"]
    RUN --> REG["Registry version tags<br/>cancer-risk-classifier v2 @champion"]
    REG --> META["models/model_metadata.json<br/>baked into the image"]
    META --> LIVE["GET /version on the running Pods<br/>(read by k8s_get_deployed_version)"]
```

Every link is recorded data, so the agent can walk it in both directions and verify each hop
(`dvc_resolve_dataset_reference` confirms an MLflow-recorded md5 against Git history).

## 5. Safety model

| Control | Where | Effect |
|---|---|---|
| Strict input schemas (`extra="forbid"`, ranges) | `agent/schemas.py` | Invalid or unexpected arguments -> `invalid_request`, tool never runs |
| Approved namespaces (reads and writes) | `KubernetesTools._ns`, `_authorize_mutation` | Other namespaces -> `denied` before any API call |
| Managed deployment only | `_authorize_mutation` | Only `inference-api` can be restarted/scaled (0-5 replicas) |
| Approved DAGs + conf allow-list | `AirflowTools.authorize_trigger` | Unknown DAG, conf keys or model types -> `denied` |
| Explicit confirmation | `ToolExecutor` + CLI prompt / `--yes` | Every state-changing tool; a broken prompt counts as "no" |
| Command allow-list | `CommandRunner` | Only read-only `git`/`dvc` sub-commands, `shell=False`, validated revisions/paths |
| Cluster RBAC | `kubernetes/rbac.yaml` | Namespaced Role, no delete/create, `patch` only on `inference-api` |
| Timeouts | executor + HTTP/K8s/subprocess clients | Hung services -> `timeout`, never block the agent |
| Grounding guard | `MLOpsAgent._ground` | All tools failed -> factual failure report; unverified numbers flagged |
| No repeated side effects | LLM fallback path | A state change already executed is summarised, never re-run |
| Secret hygiene | `.env` (git-ignored), redaction filter | Keys/passwords/tokens replaced by `***REDACTED***` in logs |

## 6. Technology choices

| Need | Choice | Why |
|---|---|---|
| Dataset versioning | DVC 3 + Git tags + local remote | Content hashes give exact provenance; `dvc get --rev` reproduces any version without touching the workspace |
| Experiment tracking | MLflow 3 server, SQLite backend | `MlflowClient` gives structured search, registry and aliases; SQLite is enough for one machine |
| Orchestration | Apache Airflow 3.4 (standalone, LocalExecutor) | Required Airflow 3 REST API v2 with JWT auth; separate virtualenv avoids dependency conflicts |
| Serving | FastAPI + uvicorn | Typed request validation, simple health endpoints for probes |
| Cluster | kind | Real Kubernetes API locally (RBAC, probes, EndpointSlices, service proxy) at low cost |
| LLM | Gemini (google-genai) with fallback models | Native function calling with JSON schemas; free tier; model fallback on quota |
| Agent framework | Own loop (no LangChain) | Every call passes through one auditable executor; fewer dependencies; easy to test |
