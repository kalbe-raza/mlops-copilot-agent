"""Capture dataset provenance (DVC hash, data version tag, Git commit) for a training run.

These values are written to MLflow as run tags so the agent can later answer
"which dataset version trained this model?" from recorded evidence instead of guessing.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_TAG_PATTERN = "data-v*"


def _git(*args: str, cwd: Path = PROJECT_ROOT) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=15, check=True)
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    return out.stdout.strip() or None


def file_md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - matches the content hash DVC uses, not a security control
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_dvc_file(dvc_file: Path) -> dict[str, Any] | None:
    if not dvc_file.exists():
        return None
    data = yaml.safe_load(dvc_file.read_text()) or {}
    outs = data.get("outs") or []
    return outs[0] if outs else None


def dataset_provenance(dataset_path: Path, project_root: Path = PROJECT_ROOT) -> dict[str, str]:
    """Return a flat dict of provenance tags for ``dataset_path``."""
    dataset_path = dataset_path.resolve()
    try:
        rel = dataset_path.relative_to(project_root)
    except ValueError:
        rel = dataset_path
    dvc_file = dataset_path.with_name(dataset_path.name + ".dvc")
    out = read_dvc_file(dvc_file)
    actual_md5 = file_md5(dataset_path) if dataset_path.exists() else "missing"

    tags: dict[str, str] = {
        "dataset.path": str(rel),
        "dataset.actual_md5": actual_md5,
        "dataset.dvc_tracked": str(out is not None).lower(),
    }
    if out is not None:
        recorded_md5 = str(out.get("md5", ""))
        tags["dataset.dvc_md5"] = recorded_md5
        tags["dataset.dvc_size"] = str(out.get("size", ""))
        tags["dataset.matches_dvc"] = str(recorded_md5 == actual_md5).lower()
        dvc_rel = str(dvc_file.relative_to(project_root)) if dvc_file.is_relative_to(project_root) else str(dvc_file)
        data_commit = _git("log", "-1", "--format=%H", "--", dvc_rel, cwd=project_root)
        if data_commit:
            tags["dataset.git_commit"] = data_commit
            version = _git("describe", "--tags", "--exact-match", "--match", DATA_TAG_PATTERN, data_commit,
                           cwd=project_root)
            if version is None:
                version = _git("describe", "--tags", "--abbrev=0", "--match", DATA_TAG_PATTERN, data_commit,
                               cwd=project_root)
                if version:
                    version = f"{version}+untagged"
            if _git("status", "--porcelain", "--", dvc_rel, cwd=project_root):
                version = f"{version or 'untagged'}+uncommitted"
            tags["dataset.version"] = version or "untagged"
    head = _git("rev-parse", "HEAD", cwd=project_root)
    if head:
        tags["git.commit"] = head
    remote = _git("config", "--get", "remote.origin.url", cwd=project_root)
    if remote:
        tags["git.remote"] = re.sub(r"//[^@/]+@", "//", remote)  # never record embedded credentials
    return tags
