"""Capture evidence screenshots of the MLflow and Airflow UIs and render CLI evidence as images.

Requires Playwright (not a project dependency):
    uv run --with playwright python scripts/capture_screenshots.py [--chrome /path/to/chrome]

Output: screenshots/*.png
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
from playwright.async_api import Page, async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "screenshots"
MLFLOW = "http://127.0.0.1:5000"
AIRFLOW = "http://127.0.0.1:8080"
DAG = "ml_training_pipeline"

TERMINAL_CSS = """
body{margin:0;background:#0d1117;color:#c9d1d9;font:13px/1.45 'DejaVu Sans Mono',Menlo,Consolas,monospace}
.bar{background:#161b22;color:#8b949e;padding:8px 14px;font:12px sans-serif;border-bottom:1px solid #30363d}
pre{margin:0;padding:14px 18px;white-space:pre-wrap;word-break:break-word}
.cmd{color:#7ee787}.hdr{color:#79c0ff;font-weight:bold}
"""


async def shot(page: Page, name: str, wait_ms: int = 2500, full: bool = False) -> None:
    await page.wait_for_timeout(wait_ms)
    await page.screenshot(path=str(OUT / f"{name}.png"), full_page=full)
    print("saved", name)


def mlflow_ids() -> tuple[dict[str, str], dict[str, str]]:
    exps = httpx.get(f"{MLFLOW}/api/2.0/mlflow/experiments/search", params={"max_results": 100}).json()["experiments"]
    exp_ids = {e["name"]: e["experiment_id"] for e in exps}
    runs = httpx.post(f"{MLFLOW}/api/2.0/mlflow/runs/search",
                      json={"experiment_ids": list(exp_ids.values()), "max_results": 500}).json()["runs"]
    run_ids = {r["info"]["run_name"]: r["info"]["run_id"] for r in runs}
    return exp_ids, run_ids


async def tidy_mlflow(page: Page) -> None:
    """MLflow 3.x opens in GenAI mode with an assistant panel: switch to classic ML and close the panel."""
    await page.wait_for_timeout(2500)
    toggle = page.get_by_text("Model training", exact=True)
    if await toggle.count():
        await toggle.first.click()
        await page.wait_for_timeout(1500)
    close = page.locator("button[aria-label='Close']")
    if await close.count():
        await close.first.click()
        await page.wait_for_timeout(800)


async def capture_mlflow(page: Page) -> None:
    exp_ids, run_ids = mlflow_ids()
    await page.goto(f"{MLFLOW}/#/experiments", wait_until="load")
    await tidy_mlflow(page)
    await shot(page, "mlflow-01-experiments")
    for name, exp in (("mlflow-02-runs-cancer-tuning-v2", "cancer-tuning-v2"),
                      ("mlflow-03-runs-airflow-pipeline", "cancer-airflow-pipeline")):
        await page.goto(f"{MLFLOW}/#/experiments/{exp_ids[exp]}/runs?workflowType=machine_learning", wait_until="load")
        await tidy_mlflow(page)
        await shot(page, name, 3000)
        await page.mouse.click(296, 119)  # "chart view" toggle next to the table view (fixed viewport)
        await shot(page, name + "-charts", 4000)
    ids = [run_ids[n] for n in ("run-07-logistic_regression", "run-09-random_forest", "run-10-gradient_boosting",
                                "run-11-svm", "run-12-decision_stump")]
    url = (f"{MLFLOW}/#/compare-runs?runs={quote(json.dumps(ids))}"
           f"&experiments={quote(json.dumps([exp_ids['cancer-tuning-v2']]))}")
    await page.goto(url, wait_until="load")
    await tidy_mlflow(page)
    await shot(page, "mlflow-04-compare-runs", 3000)
    metrics_row = page.get_by_text("f1_gap", exact=True)
    if await metrics_row.count():
        await metrics_row.first.scroll_into_view_if_needed()
        await page.mouse.wheel(0, 250)
        await shot(page, "mlflow-04b-compare-runs-metrics", 1500)
    await page.goto(f"{MLFLOW}/#/experiments/{exp_ids['cancer-tuning-v2']}/runs/{run_ids['run-11-svm']}",
                    wait_until="load")
    await tidy_mlflow(page)
    await shot(page, "mlflow-05-run-11-provenance-tags", 3000)
    await page.goto(f"{MLFLOW}/#/models/cancer-risk-classifier", wait_until="load")
    await tidy_mlflow(page)
    await shot(page, "mlflow-06-model-registry", 2000)


async def capture_airflow(page: Page) -> None:
    pw = json.loads((ROOT / "airflow_home" / "passwords.json").read_text())
    await page.goto(f"{AIRFLOW}/auth/login", wait_until="load")
    await page.fill("input[name=username]", "admin")
    await page.fill("input[name=password]", pw["admin"])
    await page.keyboard.press("Enter")
    await page.wait_for_url(lambda u: "/auth/login" not in u, timeout=20000)
    await page.goto(f"{AIRFLOW}/dags", wait_until="load")
    await shot(page, "airflow-01-dags", 3000)
    await page.goto(f"{AIRFLOW}/dags/{DAG}", wait_until="load")
    await shot(page, "airflow-02-dag-runs-grid", 4000)
    await page.goto(f"{AIRFLOW}/dags/{DAG}", wait_until="load")
    await page.wait_for_timeout(3000)
    await page.mouse.click(139, 101)  # graph-view toggle beside the grid toggle (fixed viewport)
    await shot(page, "airflow-03-dag-graph", 4000)
    await page.mouse.click(104, 101)  # back to grid view (the UI remembers the choice)
    await page.wait_for_timeout(1500)

    token = httpx.post(f"{AIRFLOW}/auth/token", json={"username": "admin", "password": pw["admin"]}).json()
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    runs = httpx.get(f"{AIRFLOW}/api/v2/dags/{DAG}/dagRuns", params={"order_by": "-run_after", "limit": 50},
                     headers=headers).json()["dag_runs"]
    for run in runs:
        rid = quote(run["dag_run_id"], safe="")
        tis = httpx.get(f"{AIRFLOW}/api/v2/dags/{DAG}/dagRuns/{rid}/taskInstances", headers=headers).json()
        failed = [t["task_id"] for t in tis["task_instances"] if t["state"] == "failed"]
        if "validate_data" in failed and not (OUT / "airflow-04-failed-validate-data-log.png").exists():
            await page.goto(f"{AIRFLOW}/dags/{DAG}/runs/{rid}/tasks/validate_data", wait_until="load")
            await shot(page, "airflow-04-failed-validate-data-log", 5000)
        if "quality_gate" in failed and not (OUT / "airflow-05-failed-quality-gate-log.png").exists():
            await page.goto(f"{AIRFLOW}/dags/{DAG}/runs/{rid}/tasks/quality_gate", wait_until="load")
            await shot(page, "airflow-05-failed-quality-gate-log", 5000)
    latest_ok = next(r for r in runs if r["state"] == "success")
    await page.goto(f"{AIRFLOW}/dags/{DAG}/runs/{quote(latest_ok['dag_run_id'], safe='')}", wait_until="load")
    await shot(page, "airflow-06-successful-run", 4000)


def terminal_html(title: str, body: str) -> str:
    lines = []
    for line in body.splitlines():
        esc = html.escape(line)
        if line.startswith("$ "):
            esc = f'<span class="cmd">{esc}</span>'
        elif line.startswith("#"):
            esc = f'<span class="hdr">{esc}</span>'
        lines.append(esc)
    return (f"<html><head><style>{TERMINAL_CSS}</style></head><body><div class='bar'>{html.escape(title)}</div>"
            f"<pre>{chr(10).join(lines)}</pre></body></html>")


def agent_html(question: str, planner: str) -> str:
    """Run the CLI agent with colour forced and convert its ANSI output to HTML via rich."""
    from rich.console import Console
    from rich.text import Text

    env = {**os.environ, "COLUMNS": "118", "FORCE_COLOR": "1", "TERM": "xterm-256color"}
    out = subprocess.run([sys.executable, "-W", "ignore", "-m", "agent.main", "--planner", planner, "ask", question],
                         cwd=ROOT, capture_output=True, text=True, env=env, timeout=600).stdout
    out = "\n".join(line for line in out.splitlines() if "Working..." not in line)  # drop spinner frames
    console = Console(record=True, width=118, force_terminal=True, color_system="truecolor")
    console.print(Text.from_ansi(f"$ python -m agent.main --planner {planner} ask \"{question}\"\n\n" + out))
    return console.export_html(inline_styles=True, code_format=(
        "<html><head><style>body{{margin:0;background:#0d1117}}pre{{margin:0;padding:14px 18px;"
        "font:13px/1.4 'DejaVu Sans Mono',monospace;color:#c9d1d9;background:#0d1117}}</style></head>"
        "<body><pre><code>{code}</code></pre></body></html>"))


async def render(page: Page, name: str, content: str) -> None:
    tmp = OUT / f".{name}.html"
    tmp.write_text(content)
    await page.set_viewport_size({"width": 1040, "height": 700})
    await page.goto(tmp.as_uri())
    await shot(page, name, 300, full=True)
    tmp.unlink()


def section(text: str, start: str, end: str | None = None) -> str:
    i = text.index(start)
    j = text.index(end, i + 1) if end else len(text)
    return text[i:j].rstrip()


async def capture_cli(page: Page) -> None:
    ev = ROOT / "evidence"
    dvc = (ev / "dvc.txt").read_text()
    await render(page, "dvc-01-versions-status-diff", terminal_html("DVC - dataset versions, status, diff",
                 section(dvc, "$ git tag", "$ dvc dag")))
    await render(page, "dvc-02-pipeline-metrics-reproduce", terminal_html("DVC - pipeline DAG, metrics, reproduction",
                 section(dvc, "$ dvc dag")))
    k8s = (ev / "kubernetes.txt").read_text()
    await render(page, "k8s-01-resources", terminal_html("Kubernetes - workloads in namespace mlops-agent",
                 section(k8s, "$ kubectl get nodes", "$ kubectl -n mlops-agent describe")))
    await render(page, "k8s-02-version-health-rbac", terminal_html("Kubernetes - live version, readiness, agent RBAC",
                 section(k8s, "$ kubectl get --raw")))
    await render(page, "airflow-07-rest-api-evidence", terminal_html("Airflow 3 REST API (authenticated)",
                 (ev / "airflow.txt").read_text()))
    await render(page, "mlflow-07-client-evidence", terminal_html("MLflow (MlflowClient)",
                 (ev / "mlflow.txt").read_text()))
    await render(page, "agent-01-dataset-provenance",
                 agent_html("Which dataset version was used for run 11?", "rules"))
    await render(page, "agent-02-deployment-diagnosis", agent_html("Why is my prediction API unavailable?", "rules"))
    await render(page, "agent-03-multi-tool-retraining", agent_html(
        "Find the best recent model, identify the dataset version used to train it, check the deployment status, "
        "and explain whether retraining is necessary.", "rules"))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", default="/opt/google/chrome/chrome")
    parser.add_argument("--only", choices=["mlflow", "airflow", "cli"], default=None)
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=args.chrome, headless=True)
        page = await browser.new_page(viewport={"width": 1800, "height": 1000}, device_scale_factor=1)
        if args.only in (None, "mlflow"):
            await capture_mlflow(page)
        if args.only in (None, "airflow"):
            await capture_airflow(page)
        if args.only in (None, "cli"):
            await capture_cli(page)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
