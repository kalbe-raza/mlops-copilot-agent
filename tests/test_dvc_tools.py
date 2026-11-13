"""DVC tools against a throw-away Git + DVC repository (real dvc/git binaries, no network)."""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path

import pytest

from agent.schemas import CompareDatasetVersionsInput, DatasetPathInput, DatasetRevisionInput, ResolveDatasetInput
from agent.tools.base import ToolPermissionError
from agent.tools.dvc_tools import CommandRunner, DVCTools, _dvc_binary

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

V1 = "patient_id,mean_radius,malignant\nP0,1.0,0\nP1,2.0,1\n"
V2 = V1 + "P2,3.0,1\nP3,4.0,0\n"


def _run(repo: Path, *cmd: str) -> None:
    subprocess.run(list(cmd), cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    repo = tmp_path_factory.mktemp("dvcrepo")
    dvc = _dvc_binary()
    _run(repo, "git", "init", "-q", "-b", "main")
    _run(repo, "git", "config", "user.email", "test@example.com")
    _run(repo, "git", "config", "user.name", "Test")
    _run(repo, dvc, "init", "-q")
    data = repo / "data" / "raw"
    data.mkdir(parents=True)
    (data / "dataset.csv").write_text(V1)
    _run(repo, dvc, "add", "-q", "data/raw/dataset.csv")
    _run(repo, "git", "add", "-A")
    _run(repo, "git", "commit", "-q", "-m", "data v1")
    _run(repo, "git", "tag", "data-v1.0")
    (data / "dataset.csv").write_text(V2)
    _run(repo, dvc, "add", "-q", "data/raw/dataset.csv")
    _run(repo, "git", "add", "-A")
    _run(repo, "git", "commit", "-q", "-m", "data v2")
    _run(repo, "git", "tag", "data-v2.0")
    return repo


@pytest.fixture
def tools(settings, repo) -> DVCTools:
    return DVCTools(dataclasses.replace(settings, dvc_repo=repo))


def test_current_version_and_history(tools):
    cur = tools.get_dataset_version(DatasetPathInput())
    assert cur["version"] == "data-v2.0"
    assert cur["workspace_matches_dvc"] is True
    assert cur["shape"]["rows"] == 4
    hist = tools.list_dataset_versions(DatasetPathInput())
    assert [v["tags"] for v in hist["versions"]] == [["data-v2.0"], ["data-v1.0"]]
    assert hist["versions"][1]["shape"]["rows"] == 2  # read from the DVC cache


def test_compare_versions_reports_row_delta(tools):
    out = tools.compare_dataset_versions(CompareDatasetVersionsInput(rev_a="data-v1.0", rev_b="data-v2.0"))
    assert out["content_changed"] is True
    assert out["rows"] == {"rev_a": 2, "rev_b": 4, "delta": 2}
    assert out["schema"] == {"added_columns": [], "removed_columns": []}


def test_resolve_md5_recorded_by_a_run(tools):
    v1_md5 = tools.list_dataset_versions(DatasetPathInput())["versions"][1]["md5"]
    out = tools.resolve_dataset_reference(ResolveDatasetInput(md5=v1_md5))
    assert out["found"] and out["version"] == "data-v1.0" and out["is_current_version"] is False
    assert tools.resolve_dataset_reference(ResolveDatasetInput(md5="0" * 32))["found"] is False


def test_detects_modified_tracked_data(tools, repo):
    target = repo / "data" / "raw" / "dataset.csv"
    original = target.read_text()
    try:
        assert tools.check_data_changes(None)["workspace_changed"] is False
        target.write_text(original + "P9,9.9,1\n")
        changed = tools.check_data_changes(None)
        assert changed["workspace_changed"] is True
        assert any("dataset.csv" in c["target"] for c in changed["changes"])
    finally:
        target.write_text(original)


def test_reproduce_previous_version_is_hash_verified(tools, repo):
    plan = tools.explain_reproduction(DatasetRevisionInput(rev="data-v1.0"))
    assert any("dvc get" in c for c in plan["non_destructive"])
    out = tools.reproduce_dataset_version(DatasetRevisionInput(rev="data-v1.0"))
    assert out["verified"] is True
    assert (repo / out["output_path"]).read_text() == V1
    assert (repo / "data" / "raw" / "dataset.csv").read_text() == V2  # workspace untouched


def test_command_allowlist_and_argument_validation(tools, repo):
    runner = CommandRunner(repo)
    for argv in (("dvc", "push"), ("dvc", "gc", "-w"), ("git", "push"), ("git", "reset", "--hard"),
                 ("rm", "-rf", "/")):
        with pytest.raises(ToolPermissionError):
            runner.run(*argv)
    with pytest.raises(ToolPermissionError):
        tools.compare_dataset_versions(CompareDatasetVersionsInput(rev_a="--output=/etc/passwd"))
    with pytest.raises(ToolPermissionError):
        tools.get_dataset_version(DatasetPathInput(path="../../etc/passwd"))

