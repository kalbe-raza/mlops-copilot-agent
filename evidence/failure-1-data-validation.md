# Failure investigation 1 - corrupted training data (data/workflow layer)

## Injection

```console
$ make inject-data-failure
wrote 569 rows x 31 cols (corrupt) -> /home/kalbe-raza/kalbe_raza/mlops_ass2/mlops-copilot-agent/data/raw/dataset.csv
data/raw/dataset.csv.dvc:
	changed outs:
		modified:           data/raw/dataset.csv
```

## Agent transcript

## Trigger the training pipeline and report whether it succeeded.

*Request `836ef5f2f03d` - planner `gemini:gemini-3-flash-preview` - 2026-11-13T17:53:14+00:00*

**Answer**
The training pipeline `ml_training_pipeline` was triggered but failed during the data validation stage. The failure is caused by schema inconsistencies and missing values in the local dataset.

**Evidence**
*   **DAG Run Status:** The run `manual__2026-11-13T17:52:56.308674+00:00` finished with state `failed` [airflow_trigger_dag].
*   **Root Cause:** The `validate_data` task failed, causing all downstream tasks (preprocess, train, evaluate, quality gate, register) to be skipped [airflow_trigger_dag].
*   **Validation Errors:** Logs show the dataset is missing the `worst_concave_points` column and contains 40 null values in the `mean_radius` column [airflow_trigger_dag].
*   **Data Inconsistency:** DVC detects that `data/raw/dataset.csv` has been modified in the workspace and no longer matches the version recorded in `dvc.lock` [dvc_check_data_changes].

**Uncertainties**
*   None.

**Recommended actions**
*   Revert the local changes to `data/raw/dataset.csv` using `dvc checkout` to restore the last known good version.
*   Investigate the source of the corrupted data if the file was recently updated or pulled from an external source.
*   Once the data is restored and validated, re-trigger the pipeline.

| # | tool | status | duration (ms) | error |
|---|---|---|---|---|
| 1 | `airflow_trigger_dag` | success | 10188 |  |
| 2 | `dvc_check_data_changes` | success | 894 |  |

<details><summary>Raw tool outputs</summary>

```json
[
  {
    "call_id": "94d9cadaa5",
    "request_id": "836ef5f2f03d",
    "tool": "airflow_trigger_dag",
    "status": "success",
    "duration_ms": 10188.22,
    "data": {
      "dag_id": "ml_training_pipeline",
      "dag_run_id": "manual__2026-11-13T17:52:56.308674+00:00",
      "unpaused_dag": false,
      "conf": {},
      "initial_state": "queued",
      "final_state": "failed",
      "completed": true,
      "task_instances": [
        {
          "task_id": "validate_data",
          "state": "failed",
          "try_number": 1,
          "max_tries": 2,
          "start_date": "2026-11-13T17:52:57.274176Z",
          "end_date": "2026-11-13T17:52:59.080549Z",
          "duration_s": 1.81,
          "operator": "_PythonDecoratedOperator"
        },
        {
          "task_id": "preprocess_data",
          "state": "upstream_failed",
          "try_number": 0,
          "max_tries": 2,
          "start_date": "2026-11-13T17:52:59.195081Z",
          "end_date": "2026-11-13T17:52:59.195081Z",
          "duration_s": 0.0,
          "operator": "_PythonDecoratedOperator"
        },
        {
          "task_id": "train_model",
          "state": "upstream_failed",
          "try_number": 0,
          "max_tries": 2,
          "start_date": "2026-11-13T17:53:00.239063Z",
          "end_date": "2026-11-13T17:53:00.239063Z",
          "duration_s": 0.0,
          "operator": "_PythonDecoratedOperator"
        },
        {
          "task_id": "evaluate_and_log",
          "state": "upstream_failed",
          "try_number": 0,
          "max_tries": 2,
          "start_date": "2026-11-13T17:53:01.284443Z",
          "end_date": "2026-11-13T17:53:01.284443Z",
          "duration_s": 0.0,
          "operator": "_PythonDecoratedOperator"
        },
        {
          "task_id": "quality_gate",
          "state": "upstream_failed",
          "try_number": 0,
          "max_tries": 2,
          "start_date": "2026-11-13T17:53:01.498908Z",
          "end_date": "2026-11-13T17:53:01.498908Z",
          "duration_s": 0.0,
          "operator": "_PythonDecoratedOperator"
        },
        {
          "task_id": "register_model",
          "state": "upstream_failed",
          "try_number": 0,
          "max_tries": 2,
          "start_date": "2026-11-13T17:53:01.858051Z",
          "end_date": "2026-11-13T17:53:01.858051Z",
          "duration_s": 0.0,
          "operator": "_PythonDecoratedOperator"
        }
      ],
      "failure_details": {
        "dag_id": "ml_training_pipeline",
        "dag_run_id": "manual__2026-11-13T17:52:56.308674+00:00",
        "failed_run_found": true,
        "failed_tasks": [
          {
            "task_id": "validate_data",
            "state": "failed",
            "try_number": 1,
            "max_tries": 2,
            "start_date": "2026-11-13T17:52:57.274176Z",
            "end_date": "2026-11-13T17:52:59.080549Z",
            "duration_s": 1.81,
            "operator": "_PythonDecoratedOperator",
            "error_lines": [
              "2026-11-13T17:52:58 ERROR 2026-11-13 22:52:58,826 ERROR [training] DATA VALIDATION FAILED: missing columns: ['worst_concave_points']",
              "2026-11-13T17:52:58 ERROR 2026-11-13 22:52:58,826 ERROR [training] DATA VALIDATION FAILED: null values found: {'mean_radius': 40}",
              "2026-11-13T17:52:58 ERROR 2026-11-13 22:52:58,826 ERROR [training] stage 'validate' failed: data validation failed: missing columns: ['worst_concave_points']; null values found: {'mean_radius': 40}",
              "2026-11-13T17:52:59 ERROR Task failed with exception"
            ]
          },
          {
            "task_id": "preprocess_data",
            "state": "upstream_failed",
            "try_number": 0,
            "max_tries": 2,
            "start_date": "2026-11-13T17:52:59.195081Z",
            "end_date": "2026-11-13T17:52:59.195081Z",
            "duration_s": 0.0,
            "operator": "_PythonDecoratedOperator",
            "note": "did not run because an upstream task failed"
          },
          {
            "task_id": "train_model",
            "state": "upstream_failed",
            "try_number": 0,
            "max_tries": 2,
            "start_date": "2026-11-13T17:53:00.239063Z",
            "end_date": "2026-11-13T17:53:00.239063Z",
            "duration_s": 0.0,
            "operator": "_PythonDecoratedOperator",
            "note": "did not run because an upstream task failed"
          },
          {
            "task_id": "evaluate_and_log",
            "state": "upstream_failed",
            "try_number": 0,
            "max_tries": 2,
            "start_date": "2026-11-13T17:53:01.284443Z",
            "end_date": "2026-11-13T17:53:01.284443Z",
            "duration_s": 0.0,
            "operator": "_PythonDecoratedOperator",
            "note": "did not run because an upstream task failed"
          },
          {
            "task_id": "quality_gate",
            "state": "upstream_failed",
            "try_number": 0,
            "max_tries": 2,
            "start_date": "2026-11-13T17:53:01.498908Z",
            "end_date": "2026-11-13T17:53:01.498908Z",
            "duration_s": 0.0,
            "operator": "_PythonDecoratedOperator",
            "note": "did not run because an upstream task failed"
          },
          {
            "task_id": "register_model",
            "state": "upstream_failed",
            "try_number": 0,
            "max_tries": 2,
            "start_date": "2026-11-13T17:53:01.858051Z",
            "end_date": "2026-11-13T17:53:01.858051Z",
            "duration_s": 0.0,
            "operator": "_PythonDecoratedOperator",
            "note": "did not run because an upstream task failed"
          }
        ],
        "root_cause_task": "validate_data",
        "skipped_downstream": [
          "preprocess_data",
          "train_model",
          "evaluate_and_log",
          "quality_gate",
          "register_model"
        ]
      }
    },
    "error": null
  },
  {
    "call_id": "1bbbef04f1",
    "request_id": "836ef5f2f03d",
    "tool": "dvc_check_data_changes",
    "status": "success",
    "duration_ms": 894.45,
    "data": {
      "workspace_changed": true,
      "dvc_status": {
        "validate": [
          {
            "changed deps": {
              "data/raw/dataset.csv": "modified"
            }
          }
        ],
        "train": [
          {
            "changed deps": {
              "training/train.py": "modified"
            }
          }
        ],
        "data/raw/dataset.csv.dvc": [
          {
            "changed outs": {
              "data/raw/dataset.csv": "modified"
            }
          }
        ]
      },
      "changes": [
        {
          "target": "validate",
          "kind": "changed deps",
          "detail": {
            "data/raw/dataset.csv": "modified"
          }
        },
        {
          "target": "train",
          "kind": "changed deps",
          "detail": {
            "training/train.py": "modified"
          }
        },
        {
          "target": "data/raw/dataset.csv.dvc",
          "kind": "changed outs",
          "detail": {
            "data/raw/dataset.csv": "modified"
          }
        }
      ],
      "uncommitted_dvc_metadata": [],
      "interpretation": "Tracked data in the workspace differs from the versions recorded in .dvc/dvc.lock files."
    },
    "error": null
  }
]
```
</details>

---

## Why did the latest training pipeline run fail? Find the root cause.

*Request `95281dc1f86a` - planner `gemini:gemini-3-flash-preview` - 2026-11-13T17:53:32+00:00*

**Answer**
The latest training pipeline run failed during the `validate_data` task because the input dataset in the workspace contains schema violations and missing values.

**Evidence**
*   **Failed Run:** DAG run `manual__2026-11-13T17:52:56.308674+00:00` failed on 2026-11-13 [airflow_get_dag_runs].
*   **Root Cause:** The `validate_data` task failed with errors: missing column `['worst_concave_points']` and 40 null values found in `mean_radius` [airflow_get_failed_tasks].
*   **Data State:** DVC detects that `data/raw/dataset.csv` has been modified in the workspace and differs from the committed version [dvc_check_data_changes].
*   **Downstream Impact:** Tasks `preprocess_data`, `train_model`, and `evaluate_and_log` were skipped due to the upstream failure [airflow_get_failed_tasks].

**Uncertainties**
None.

**Recommended actions**
*   Investigate the source of the data update to `data/raw/dataset.csv` to resolve the missing column and null values.
*   Revert the local data changes or fix the dataset and commit the new version to DVC before re-running the pipeline.

| # | tool | status | duration (ms) | error |
|---|---|---|---|---|
| 1 | `airflow_get_dag_runs` | success | 70 |  |
| 2 | `airflow_get_failed_tasks` | success | 34 |  |
| 3 | `dvc_check_data_changes` | success | 978 |  |

<details><summary>Raw tool outputs</summary>

```json
[
  {
    "call_id": "93063fbf54",
    "request_id": "95281dc1f86a",
    "tool": "airflow_get_dag_runs",
    "status": "success",
    "duration_ms": 69.79,
    "data": {
      "dag_id": "ml_training_pipeline",
      "runs": [
        {
          "dag_run_id": "manual__2026-11-13T17:52:56.308674+00:00",
          "state": "failed",
          "run_type": "manual",
          "run_after": "2026-11-13T17:52:56.308674Z",
          "start_date": "2026-11-13T17:52:57.211079Z",
          "end_date": "2026-11-13T17:53:02.915052Z",
          "duration_s": 5.703973,
          "conf": {},
          "note": "Triggered by MLOps Copilot agent"
        }
      ],
      "state_counts": {
        "failed": 1
      },
      "latest_state": "failed"
    },
    "error": null
  },
  {
    "call_id": "e5d545c7c5",
    "request_id": "95281dc1f86a",
    "tool": "airflow_get_failed_tasks",
    "status": "success",
    "duration_ms": 34.32,
    "data": {
      "dag_id": "ml_training_pipeline",
      "dag_run_id": "manual__2026-11-13T17:52:56.308674+00:00",
      "failed_run_found": true,
      "failed_tasks": [
        {
          "task_id": "validate_data",
          "state": "failed",
          "try_number": 1,
          "max_tries": 2,
          "start_date": "2026-11-13T17:52:57.274176Z",
          "end_date": "2026-11-13T17:52:59.080549Z",
          "duration_s": 1.81,
          "operator": "_PythonDecoratedOperator",
          "error_lines": [
            "2026-11-13T17:52:58 ERROR 2026-11-13 22:52:58,826 ERROR [training] DATA VALIDATION FAILED: missing columns: ['worst_concave_points']",
            "2026-11-13T17:52:58 ERROR 2026-11-13 22:52:58,826 ERROR [training] DATA VALIDATION FAILED: null values found: {'mean_radius': 40}",
            "2026-11-13T17:52:58 ERROR 2026-11-13 22:52:58,826 ERROR [training] stage 'validate' failed: data validation failed: missing columns: ['worst_concave_points']; null values found: {'mean_radius': 40}",
            "2026-11-13T17:52:59 ERROR Task failed with exception"
          ]
        },
        {
          "task_id": "preprocess_data",
          "state": "upstream_failed",
          "try_number": 0,
          "max_tries": 2,
          "start_date": "2026-11-13T17:52:59.195081Z",
          "end_date": "2026-11-13T17:52:59.195081Z",
          "duration_s": 0.0,
          "operator": "_PythonDecoratedOperator",
          "note": "did not run because an upstream task failed"
        },
        {
          "task_id": "train_model",
          "state": "upstream_failed",
          "try_number": 0,
          "max_tries": 2,
          "start_date": "2026-11-13T17:53:00.239063Z",
          "end_date": "2026-11-13T17:53:00.239063Z",
          "duration_s": 0.0,
          "operator": "_PythonDecoratedOperator",
          "note": "did not run because an upstream task failed"
        },
        {
          "task_id": "evaluate_and_log",
          "state": "upstream_failed",
          "try_number": 0,
          "max_tries": 2,
          "start_date": "2026-11-13T17:53:01.284443Z",
          "end_date": "2026-11-13T17:53:01.284443Z",
          "duration_s": 0.0,
          "operator": "_PythonDecoratedOperator",
          "note": "did not run because an upstream task failed"
        },
        {
          "task_id": "quality_gate",
          "state": "upstream_failed",
          "try_number": 0,
          "max_tries": 2,
          "start_date": "2026-11-13T17:53:01.498908Z",
          "end_date": "2026-11-13T17:53:01.498908Z",
          "duration_s": 0.0,
          "operator": "_PythonDecoratedOperator",
          "note": "did not run because an upstream task failed"
        },
        {
          "task_id": "register_model",
          "state": "upstream_failed",
          "try_number": 0,
          "max_tries": 2,
          "start_date": "2026-11-13T17:53:01.858051Z",
          "end_date": "2026-11-13T17:53:01.858051Z",
          "duration_s": 0.0,
          "operator": "_PythonDecoratedOperator",
          "note": "did not run because an upstream task failed"
        }
      ],
      "root_cause_task": "validate_data",
      "skipped_downstream": [
        "preprocess_data",
        "train_model",
        "evaluate_and_log",
        "quality_gate",
        "register_model"
      ]
    },
    "error": null
  },
  {
    "call_id": "d291510414",
    "request_id": "95281dc1f86a",
    "tool": "dvc_check_data_changes",
    "status": "success",
    "duration_ms": 978.45,
    "data": {
      "workspace_changed": true,
      "dvc_status": {
        "validate": [
          {
            "changed deps": {
              "data/raw/dataset.csv": "modified"
            }
          }
        ],
        "train": [
          {
            "changed deps": {
              "training/train.py": "modified"
            }
          }
        ],
        "data/raw/dataset.csv.dvc": [
          {
            "changed outs": {
              "data/raw/dataset.csv": "modified"
            }
          }
        ]
      },
      "changes": [
        {
          "target": "validate",
          "kind": "changed deps",
          "detail": {
            "data/raw/dataset.csv": "modified"
          }
        },
        {
          "target": "train",
          "kind": "changed deps",
          "detail": {
            "training/train.py": "modified"
          }
        },
        {
          "target": "data/raw/dataset.csv.dvc",
          "kind": "changed outs",
          "detail": {
            "data/raw/dataset.csv": "modified"
          }
        }
      ],
      "uncommitted_dvc_metadata": [],
      "interpretation": "Tracked data in the workspace differs from the versions recorded in .dvc/dvc.lock files."
    },
    "error": null
  }
]
```
</details>

---

## Resolution

```console
$ make resolve-data-failure
M       data/raw/dataset.csv
Data and pipelines are up to date.
```

## Verification (agent)

## Has the tracked data changed? If it is clean, trigger the training pipeline again and report the result.

*Request `55abd8f9625f` - planner `gemini:gemini-3-flash-preview` - 2026-11-13T17:55:31+00:00*

**Answer**
The DVC-tracked data is clean with no uncommitted changes, and the training pipeline `ml_training_pipeline` was successfully triggered and completed.

**Evidence**
*   DVC workspace check confirmed that all tracked data matches the committed versions (`workspace_changed: false`) [dvc_check_data_changes].
*   Airflow DAG `ml_training_pipeline` (run `manual__2026-11-13T17:54:57.948146+00:00`) finished with state `success` [airflow_trigger_dag].
*   All pipeline tasks, including `validate_data`, `train_model`, `quality_gate`, and `register_model`, completed successfully [airflow_trigger_dag].

**Uncertainties**
*   None.

**Recommended actions**
*   Review the newly registered model version in the MLflow Model Registry to evaluate its performance metrics compared to the current production model.

| # | tool | status | duration (ms) | error |
|---|---|---|---|---|
| 1 | `dvc_check_data_changes` | success | 1122 |  |
| 2 | `airflow_trigger_dag` | success | 30285 |  |

<details><summary>Raw tool outputs</summary>

```json
[
  {
    "call_id": "8c0ef85a43",
    "request_id": "55abd8f9625f",
    "tool": "dvc_check_data_changes",
    "status": "success",
    "duration_ms": 1121.5,
    "data": {
      "workspace_changed": false,
      "dvc_status": {},
      "changes": [],
      "uncommitted_dvc_metadata": [],
      "interpretation": "All DVC-tracked data matches the committed versions."
    },
    "error": null
  },
  {
    "call_id": "2f9e69bbee",
    "request_id": "55abd8f9625f",
    "tool": "airflow_trigger_dag",
    "status": "success",
    "duration_ms": 30285.43,
    "data": {
      "dag_id": "ml_training_pipeline",
      "dag_run_id": "manual__2026-11-13T17:54:57.948146+00:00",
      "unpaused_dag": false,
      "conf": {},
      "initial_state": "queued",
      "final_state": "success",
      "completed": true,
      "task_instances": [
        {
          "task_id": "validate_data",
          "state": "success",
          "try_number": 1,
          "max_tries": 2,
          "start_date": "2026-11-13T17:54:58.822942Z",
          "end_date": "2026-11-13T17:55:00.918896Z",
          "duration_s": 2.1,
          "operator": "_PythonDecoratedOperator"
        },
        {
          "task_id": "preprocess_data",
          "state": "success",
          "try_number": 1,
          "max_tries": 2,
          "start_date": "2026-11-13T17:55:01.784785Z",
          "end_date": "2026-11-13T17:55:03.979530Z",
          "duration_s": 2.19,
          "operator": "_PythonDecoratedOperator"
        },
        {
          "task_id": "train_model",
          "state": "success",
          "try_number": 1,
          "max_tries": 2,
          "start_date": "2026-11-13T17:55:04.379770Z",
          "end_date": "2026-11-13T17:55:07.063840Z",
          "duration_s": 2.68,
          "operator": "_PythonDecoratedOperator"
        },
        {
          "task_id": "evaluate_and_log",
          "state": "success",
          "try_number": 1,
          "max_tries": 2,
          "start_date": "2026-11-13T17:55:07.851868Z",
          "end_date": "2026-11-13T17:55:15.253543Z",
          "duration_s": 7.4,
          "operator": "_PythonDecoratedOperator"
        },
        {
          "task_id": "quality_gate",
          "state": "success",
          "try_number": 1,
          "max_tries": 2,
          "start_date": "2026-11-13T17:55:15.903000Z",
          "end_date": "2026-11-13T17:55:19.311731Z",
          "duration_s": 3.41,
          "operator": "_PythonDecoratedOperator"
        },
        {
          "task_id": "register_model",
          "state": "success",
          "try_number": 1,
          "max_tries": 2,
          "start_date": "2026-11-13T17:55:19.417060Z",
          "end_date": "2026-11-13T17:55:23.145669Z",
          "duration_s": 3.73,
          "operator": "_PythonDecoratedOperator"
        }
      ]
    },
    "error": null
  }
]
```
</details>

---

