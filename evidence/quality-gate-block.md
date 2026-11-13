# Quality gate blocking promotion + an unexpected failure found during testing

## What happened

1. The request "Trigger the model training pipeline with a gradient boosting model" was sent to the Gemini planner.
   Gemini called `airflow_trigger_dag` (run `manual__2026-11-13T17:47:47`), which executed the DAG.
2. Gemini's *next* request failed with `429 RESOURCE_EXHAUSTED` (free tier: 20 requests/day for
   `gemini-2.5-flash`, see `logs/agent.jsonl` event `llm_fallback`).
3. The orchestrator fell back to the deterministic planner, which **triggered the DAG a second time**
   (`manual__2026-11-13T17:48:24`) - the transcript below is that fallback answer.
4. Both runs failed at `quality_gate`: at that time the DAG passed random-forest default hyper-parameters
   (`n_estimators=300, max_depth=8`) to every estimator; gradient boosting with depth 8 overfits
   (F1 0.9216 < 0.93, recall 0.8868 < 0.90, train/test gap 0.0784 > 0.06). `register_model` stayed
   `upstream_failed`, so **nothing was promoted** - the gate worked as designed.

## Root causes and fixes

| Problem | Root cause | Fix (commit in history) | Regression test |
|---|---|---|---|
| Duplicate DAG trigger | Fallback re-planned from scratch and ignored tool calls the LLM had already executed | `LLMError.partial_results`; if a state-changing call already succeeded the fallback only summarises it (`agent/orchestrator.py`) | `test_llm_failure_after_a_state_change_never_repeats_it` |
| Wrong hyper-parameters for non-RF models | DAG `params` default was RF-specific | Per-model `DEFAULT_PARAMS` in `training/train.py`, DAG default `params={}` | `test_rule_planner_*`, re-run `manual__2026-11-13T17:52:13` succeeded |
| LLM quota exhaustion | Small per-model free-tier quota | Model fallback chain (`GEMINI_FALLBACK_MODELS`) | `test_gemini_quota_exhaustion_switches_to_fallback_model` |

## Trigger the model training pipeline with a gradient boosting model and report whether execution succeeded.

*Request `34f1d74a970c` - planner `rules` - 2026-11-13T17:48:49+00:00*

> LLM planner unavailable (Gemini request failed: ClientError: 429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your curren); answered with the deterministic planner.

**Answer**
Training pipeline run `manual__2026-11-13T17:48:24.526204+00:00` **failed**.

**Evidence**
- Triggered `ml_training_pipeline` run `manual__2026-11-13T17:48:24.526204+00:00` with conf {'model_type': 'gradient_boosting'} [airflow_trigger_dag]
- task `validate_data`: success (try 1, 1.75s) [airflow_trigger_dag]
- task `preprocess_data`: success (try 1, 1.82s) [airflow_trigger_dag]
- task `train_model`: success (try 1, 3.01s) [airflow_trigger_dag]
- task `evaluate_and_log`: success (try 1, 6.12s) [airflow_trigger_dag]
- task `quality_gate`: failed (try 1, 3.49s) [airflow_trigger_dag]
- task `register_model`: upstream_failed (try 0, 0.0s) [airflow_trigger_dag]
- Task `quality_gate`: **failed** after 1 attempt(s) [airflow_get_failed_tasks]
-   log: `2026-11-13T17:48:42 ERROR 2026-11-13 22:48:42,968 ERROR [training] QUALITY GATE FAILED: recall 0.8868 < required 0.9` [airflow_get_failed_tasks]
-   log: `2026-11-13T17:48:42 ERROR 2026-11-13 22:48:42,968 ERROR [training] QUALITY GATE FAILED: train/test f1 gap 0.0784 > allowed 0.06 (overfitting)` [airflow_get_failed_tasks]
-   log: `2026-11-13T17:48:42 ERROR 2026-11-13 22:48:42,969 ERROR [training] stage 'quality-gate' failed: quality gate failed: f1 0.9216 < required 0.93; recall 0.8868 < required 0.9; train/test f1 gap 0.0784 > allowed 0.06 (overf` [airflow_get_failed_tasks]
-   log: `2026-11-13T17:48:43 ERROR Task failed with exception` [airflow_get_failed_tasks]
- Task `register_model`: **upstream_failed** after 0 attempt(s) [airflow_get_failed_tasks]
- Downstream tasks not executed: ['register_model'] [airflow_get_failed_tasks]

**Uncertainties**
- None

| # | tool | status | duration (ms) | error |
|---|---|---|---|---|
| 1 | `airflow_trigger_dag` | success | 25191 |  |

<details><summary>Raw tool outputs</summary>

```json
[
  {
    "call_id": "7cd72ace9b",
    "request_id": "34f1d74a970c",
    "tool": "airflow_trigger_dag",
    "status": "success",
    "duration_ms": 25191.34,
    "data": {
      "dag_id": "ml_training_pipeline",
      "dag_run_id": "manual__2026-11-13T17:48:24.526204+00:00",
      "unpaused_dag": false,
      "conf": {
        "model_type": "gradient_boosting"
      },
      "initial_state": "queued",
      "final_state": "failed",
      "completed": true,
      "task_instances": [
        {
          "task_id": "validate_data",
          "state": "success",
          "try_number": 1,
          "max_tries": 2,
          "start_date": "2026-11-13T17:48:25.624228Z",
          "end_date": "2026-11-13T17:48:27.376364Z",
          "duration_s": 1.75,
          "operator": "_PythonDecoratedOperator"
        },
        {
          "task_id": "preprocess_data",
          "state": "success",
          "try_number": 1,
          "max_tries": 2,
          "start_date": "2026-11-13T17:48:27.793611Z",
          "end_date": "2026-11-13T17:48:29.618136Z",
          "duration_s": 1.82,
          "operator": "_PythonDecoratedOperator"
        },
        {
          "task_id": "train_model",
          "state": "success",
          "try_number": 1,
          "max_tries": 2,
          "start_date": "2026-11-13T17:48:29.981377Z",
          "end_date": "2026-11-13T17:48:32.987355Z",
          "duration_s": 3.01,
          "operator": "_PythonDecoratedOperator"
        },
        {
          "task_id": "evaluate_and_log",
          "state": "success",
          "try_number": 1,
          "max_tries": 2,
          "start_date": "2026-11-13T17:48:33.380716Z",
          "end_date": "2026-11-13T17:48:39.498493Z",
          "duration_s": 6.12,
          "operator": "_PythonDecoratedOperator"
        },
        {
          "task_id": "quality_gate",
          "state": "failed",
          "try_number": 1,
          "max_tries": 2,
          "start_date": "2026-11-13T17:48:39.957423Z",
          "end_date": "2026-11-13T17:48:43.446205Z",
          "duration_s": 3.49,
          "operator": "_PythonDecoratedOperator"
        },
        {
          "task_id": "register_model",
          "state": "upstream_failed",
          "try_number": 0,
          "max_tries": 2,
          "start_date": "2026-11-13T17:48:44.135189Z",
          "end_date": "2026-11-13T17:48:44.135189Z",
          "duration_s": 0.0,
          "operator": "_PythonDecoratedOperator"
        }
      ],
      "failure_details": {
        "dag_id": "ml_training_pipeline",
        "dag_run_id": "manual__2026-11-13T17:48:24.526204+00:00",
        "failed_run_found": true,
        "failed_tasks": [
          {
            "task_id": "quality_gate",
            "state": "failed",
            "try_number": 1,
            "max_tries": 2,
            "start_date": "2026-11-13T17:48:39.957423Z",
            "end_date": "2026-11-13T17:48:43.446205Z",
            "duration_s": 3.49,
            "operator": "_PythonDecoratedOperator",
            "error_lines": [
              "2026-11-13T17:48:42 ERROR 2026-11-13 22:48:42,968 ERROR [training] QUALITY GATE FAILED: f1 0.9216 < required 0.93",
              "2026-11-13T17:48:42 ERROR 2026-11-13 22:48:42,968 ERROR [training] QUALITY GATE FAILED: recall 0.8868 < required 0.9",
              "2026-11-13T17:48:42 ERROR 2026-11-13 22:48:42,968 ERROR [training] QUALITY GATE FAILED: train/test f1 gap 0.0784 > allowed 0.06 (overfitting)",
              "2026-11-13T17:48:42 ERROR 2026-11-13 22:48:42,969 ERROR [training] stage 'quality-gate' failed: quality gate failed: f1 0.9216 < required 0.93; recall 0.8868 < required 0.9; train/test f1 gap 0.0784 > allowed 0.06 (overfitting)",
              "2026-11-13T17:48:43 ERROR Task failed with exception"
            ]
          },
          {
            "task_id": "register_model",
            "state": "upstream_failed",
            "try_number": 0,
            "max_tries": 2,
            "start_date": "2026-11-13T17:48:44.135189Z",
            "end_date": "2026-11-13T17:48:44.135189Z",
            "duration_s": 0.0,
            "operator": "_PythonDecoratedOperator",
            "note": "did not run because an upstream task failed"
          }
        ],
        "root_cause_task": "quality_gate",
        "skipped_downstream": [
          "register_model"
        ]
      }
    },
    "error": null
  }
]
```
</details>

---

