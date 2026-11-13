"""DVC data-management tools.

All commands run through ``CommandRunner`` which only executes an allow-list of read-only
``dvc``/``git`` sub-commands with ``shell=False``, validated arguments and a timeout. The only
tool that writes anything is ``dvc_reproduce_dataset_version`` and it can only write inside
``artifacts/reproduced/`` (never the tracked workspace).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from agent.config import Settings
from agent.schemas import (
    CompareDatasetVersionsInput,
    DatasetPathInput,
    DatasetRevisionInput,
    NoInput,
    ResolveDatasetInput,
)
from agent.tools.base import ToolError, ToolPermissionError, ToolSpec, ToolUnavailableError

SAFE_REV = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-~^]{0,99}$")
SAFE_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/\-]{0,199}$")

ALLOWED_COMMANDS: dict[str, set[tuple[str, ...]]] = {
    "dvc": {("status",), ("diff",), ("data", "status"), ("get",), ("remote", "list"), ("version",), ("list",)},
    "git": {("log",), ("rev-parse",), ("show",), ("tag",), ("describe",), ("rev-list",), ("status",),
            ("ls-files",), ("cat-file",)},
}


def _dvc_binary() -> str:
    local = Path(sys.executable).with_name("dvc")
    return str(local) if local.exists() else (shutil.which("dvc") or "dvc")


class CommandRunner:
    """Executes allow-listed dvc/git commands inside the repository."""

    def __init__(self, repo: Path, timeout: float = 60.0) -> None:
        self.repo = repo
        self.timeout = timeout

    def run(self, binary: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        allowed = ALLOWED_COMMANDS.get(binary)
        if allowed is None:
            raise ToolPermissionError(f"binary '{binary}' is not allowed")
        if not any(tuple(args[: len(prefix)]) == prefix for prefix in allowed):
            raise ToolPermissionError(f"command '{binary} {' '.join(args[:2])}' is not on the approved list")
        exe = _dvc_binary() if binary == "dvc" else (shutil.which("git") or "git")
        if not self.repo.exists():
            raise ToolUnavailableError(f"repository path does not exist: {self.repo}")
        try:
            proc = subprocess.run([exe, *args], cwd=self.repo, capture_output=True, text=True,
                                  timeout=self.timeout, check=False)
        except FileNotFoundError as exc:
            raise ToolUnavailableError(f"'{binary}' executable not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"'{binary} {args[0]}' timed out after {self.timeout:g}s") from exc
        if check and proc.returncode != 0:
            msg = (proc.stderr or proc.stdout).strip().splitlines()
            raise ToolError(f"'{binary} {' '.join(args[:3])}' failed: {msg[-1] if msg else proc.returncode}")
        return proc


def _check_rev(rev: str) -> str:
    if not SAFE_REV.match(rev) or ".." in rev:
        raise ToolPermissionError(f"invalid revision '{rev}'")
    return rev


def _check_path(path: str) -> str:
    if not SAFE_PATH.match(path) or ".." in path.split("/") or path.startswith("/"):
        raise ToolPermissionError(f"invalid dataset path '{path}'")
    return path.removesuffix(".dvc")


def _md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - DVC content hash, not used for security
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DVCTools:
    module = "dvc"

    def __init__(self, settings: Settings, runner: CommandRunner | None = None) -> None:
        self.settings = settings
        self.repo = settings.dvc_repo
        self.runner = runner or CommandRunner(self.repo, settings.dvc_command_timeout)

    # ------------------------------------------------------------------ helpers
    def _git(self, *args: str, check: bool = True) -> str:
        return self.runner.run("git", *args, check=check).stdout.strip()

    def _dvc_meta_at(self, path: str, rev: str | None) -> dict[str, Any]:
        dvc_file = f"{path}.dvc"
        if rev is None:
            file = self.repo / dvc_file
            if not file.exists():
                raise ToolError(f"'{path}' is not tracked by DVC (no {dvc_file})")
            text = file.read_text()
        else:
            proc = self.runner.run("git", "show", f"{_check_rev(rev)}:{dvc_file}", check=False)
            if proc.returncode != 0:
                raise ToolError(f"'{dvc_file}' does not exist at revision '{rev}'")
            text = proc.stdout
        outs = (yaml.safe_load(text) or {}).get("outs") or []
        if not outs:
            raise ToolError(f"{dvc_file} has no outputs")
        return {"md5": outs[0].get("md5"), "size": outs[0].get("size"), "hash": outs[0].get("hash", "md5")}

    def _cache_object(self, md5: str | None) -> Path | None:
        if not md5:
            return None
        obj = self.repo / ".dvc" / "cache" / "files" / "md5" / md5[:2] / md5[2:]
        return obj if obj.exists() else None

    def _csv_shape(self, file: Path | None) -> dict[str, Any] | None:
        if file is None or not file.exists():
            return None
        with file.open("r", encoding="utf-8", errors="replace") as fh:
            header = fh.readline().strip().split(",")
            rows = sum(1 for _ in fh)
        return {"rows": rows, "columns": len(header), "column_names": header}

    def _data_tags(self, commit: str) -> list[str]:
        out = self._git("tag", "--points-at", commit, "--list", "data-v*", check=False)
        return [t for t in out.splitlines() if t]

    def _versions(self, path: str) -> list[dict[str, Any]]:
        log = self._git("log", "--format=%H|%h|%cI|%s", "--", f"{path}.dvc")
        versions = []
        for line in log.splitlines():
            full, short, date, subject = line.split("|", 3)
            meta = self._dvc_meta_at(path, full)
            versions.append({"commit": full, "short_commit": short, "date": date, "message": subject,
                             "tags": self._data_tags(full), **meta})
        return versions

    # ------------------------------------------------------------------ tools
    def list_tracked_datasets(self, _: NoInput) -> dict[str, Any]:
        files = [f for f in self._git("ls-files", "--", "*.dvc").splitlines() if f]
        datasets = []
        for f in files:
            path = f.removesuffix(".dvc")
            meta = self._dvc_meta_at(path, None)
            datasets.append({"path": path, "dvc_file": f, **meta, "type": "dvc add",
                             "in_local_cache": self._cache_object(meta["md5"]) is not None})
        lock = self.repo / "dvc.lock"
        if lock.exists():
            stages = (yaml.safe_load(lock.read_text()) or {}).get("stages", {})
            for stage, body in stages.items():
                for out in body.get("outs", []):
                    datasets.append({"path": out.get("path"), "md5": out.get("md5"), "size": out.get("size"),
                                     "type": f"pipeline output (stage '{stage}')"})
        remotes = self.runner.run("dvc", "remote", "list", check=False).stdout.strip().splitlines()
        return {"tracked": datasets, "count": len(datasets),
                "remotes": [dict(zip(("name", "url"), r.split(maxsplit=1), strict=False)) for r in remotes if r]}

    def get_dataset_version(self, args: DatasetPathInput) -> dict[str, Any]:
        path = _check_path(args.path)
        meta = self._dvc_meta_at(path, None)
        commit_line = self._git("log", "-1", "--format=%H|%h|%cI|%s", "--", f"{path}.dvc")
        if not commit_line:
            raise ToolError(f"{path}.dvc has never been committed to Git")
        full, short, date, subject = commit_line.split("|", 3)
        tags = self._data_tags(full)
        uncommitted = bool(self._git("status", "--porcelain", "--", f"{path}.dvc"))
        workspace = self.repo / path
        actual = _md5(workspace) if workspace.exists() else None
        return {
            "path": path,
            "version": tags[0] if tags else "untagged",
            "dvc_md5": meta["md5"],
            "size_bytes": meta["size"],
            "git_commit": full,
            "short_commit": short,
            "committed_at": date,
            "commit_message": subject,
            "head_commit": self._git("rev-parse", "HEAD"),
            "dvc_file_uncommitted_changes": uncommitted,
            "workspace_md5": actual,
            "workspace_matches_dvc": actual == meta["md5"],
            "shape": self._csv_shape(workspace),
        }

    def list_dataset_versions(self, args: DatasetPathInput) -> dict[str, Any]:
        path = _check_path(args.path)
        versions = self._versions(path)
        for v in versions:
            v["shape"] = self._csv_shape(self._cache_object(v["md5"]))
            if v["shape"]:
                v["shape"].pop("column_names", None)
        return {"path": path, "versions": versions, "count": len(versions)}

    def compare_dataset_versions(self, args: CompareDatasetVersionsInput) -> dict[str, Any]:
        path = _check_path(args.path)
        rev_a, rev_b = _check_rev(args.rev_a), _check_rev(args.rev_b)
        meta_a, meta_b = self._dvc_meta_at(path, rev_a), self._dvc_meta_at(path, rev_b)
        shape_a = self._csv_shape(self._cache_object(meta_a["md5"]))
        shape_b = self._csv_shape(self._cache_object(meta_b["md5"]))
        diff_proc = self.runner.run("dvc", "diff", "--json", rev_a, rev_b, check=False)
        try:
            dvc_diff = json.loads(diff_proc.stdout) if diff_proc.stdout.strip() else {}
        except json.JSONDecodeError:
            dvc_diff = {"raw": diff_proc.stdout.strip()[:500]}
        result: dict[str, Any] = {
            "path": path,
            "rev_a": {"rev": rev_a, "commit": self._git("rev-list", "-n", "1", rev_a), **meta_a},
            "rev_b": {"rev": rev_b, "commit": self._git("rev-list", "-n", "1", rev_b), **meta_b},
            "content_changed": meta_a["md5"] != meta_b["md5"],
            "size_delta_bytes": (meta_b["size"] or 0) - (meta_a["size"] or 0),
            "dvc_diff": dvc_diff,
        }
        if shape_a and shape_b:
            result["rows"] = {"rev_a": shape_a["rows"], "rev_b": shape_b["rows"],
                              "delta": shape_b["rows"] - shape_a["rows"]}
            cols_a, cols_b = set(shape_a["column_names"]), set(shape_b["column_names"])
            result["schema"] = {"added_columns": sorted(cols_b - cols_a), "removed_columns": sorted(cols_a - cols_b)}
        else:
            result["rows"] = "unavailable: one of the versions is not in the local DVC cache (run dvc pull)"
        return result

    def check_data_changes(self, _: NoInput) -> dict[str, Any]:
        status_proc = self.runner.run("dvc", "status", "--json", check=False)
        try:
            status = json.loads(status_proc.stdout) if status_proc.stdout.strip() else {}
        except json.JSONDecodeError:
            status = {"raw": status_proc.stdout.strip()[:500]}
        changed_paths: list[dict[str, Any]] = []
        for target, entries in (status or {}).items():
            for entry in entries if isinstance(entries, list) else [entries]:
                if isinstance(entry, dict):
                    for kind, detail in entry.items():
                        changed_paths.append({"target": target, "kind": kind, "detail": detail})
                else:
                    changed_paths.append({"target": target, "kind": str(entry)})
        git_changes = [line for line in self._git("status", "--porcelain", "--", "*.dvc", "dvc.lock",
                                                   "dvc.yaml", check=False).splitlines() if line]
        return {
            "workspace_changed": bool(status),
            "dvc_status": status,
            "changes": changed_paths,
            "uncommitted_dvc_metadata": git_changes,
            "interpretation": (
                "Tracked data in the workspace differs from the versions recorded in .dvc/dvc.lock files."
                if status else "All DVC-tracked data matches the committed versions."
            ),
        }

    def resolve_dataset_reference(self, args: ResolveDatasetInput) -> dict[str, Any]:
        if not args.md5 and not args.git_commit:
            raise ToolError("provide an md5 or a git_commit to resolve")
        path = _check_path(args.path)
        if args.git_commit:
            _check_rev(args.git_commit)
        current = self._dvc_meta_at(path, None)
        matches = []
        for v in self._versions(path):
            if (args.md5 and v["md5"] == args.md5) or (args.git_commit and v["commit"].startswith(args.git_commit)):
                matches.append(v)
        if not matches:
            return {"found": False, "md5": args.md5, "git_commit": args.git_commit,
                    "message": "no committed DVC version of this dataset matches the reference"}
        best = matches[-1]  # oldest commit introducing that content
        return {"found": True, "path": path, "version": best["tags"][0] if best["tags"] else "untagged",
                "git_commit": best["commit"], "committed_at": best["date"], "md5": best["md5"],
                "size_bytes": best["size"], "is_current_version": best["md5"] == current["md5"],
                "in_local_cache": self._cache_object(best["md5"]) is not None, "all_matching_commits":
                [m["short_commit"] for m in matches]}

    def explain_reproduction(self, args: DatasetRevisionInput) -> dict[str, Any]:
        path, rev = _check_path(args.path), _check_rev(args.rev)
        commit = self._git("rev-list", "-n", "1", rev)
        meta = self._dvc_meta_at(path, rev)
        tag = self._data_tags(commit)
        out_dir = f"artifacts/reproduced/{rev.replace('/', '_')}"
        return {
            "rev": rev, "commit": commit, "tags": tag, "md5": meta["md5"], "size_bytes": meta["size"],
            "in_local_cache": self._cache_object(meta["md5"]) is not None,
            "non_destructive": [f"dvc get . {path} --rev {rev} -o {out_dir}/{Path(path).name}",
                                f"md5sum {out_dir}/{Path(path).name}  # expect {meta['md5']}"],
            "restore_in_workspace": [f"git checkout {commit} -- {path}.dvc",
                                     f"dvc checkout {path}   # or 'dvc pull {path}' if not cached",
                                     "python -m training.train run-all --workdir artifacts/runs/repro "
                                     "--no-register  # retrain on that exact data"],
            "return_to_latest": [f"git checkout HEAD -- {path}.dvc", f"dvc checkout {path}"],
        }

    def reproduce_dataset_version(self, args: DatasetRevisionInput) -> dict[str, Any]:
        path, rev = _check_path(args.path), _check_rev(args.rev)
        meta = self._dvc_meta_at(path, rev)
        out_dir = self.repo / "artifacts" / "reproduced" / rev.replace("/", "_")
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / Path(path).name
        self.runner.run("dvc", "get", str(self.repo), path, "--rev", rev, "-o", str(target), "--force")
        actual = _md5(target)
        return {"rev": rev, "commit": self._git("rev-list", "-n", "1", rev), "output_path":
                str(target.relative_to(self.repo)), "expected_md5": meta["md5"], "actual_md5": actual,
                "verified": actual == meta["md5"], "shape": self._csv_shape(target)}

    # ------------------------------------------------------------------ registry
    def specs(self) -> list[ToolSpec]:
        m = self.module
        return [
            ToolSpec("dvc_list_tracked_datasets", "List every dataset/artifact tracked by DVC with its hash, size, "
                     "cache status and the configured DVC remotes.", NoInput, self.list_tracked_datasets, m),
            ToolSpec("dvc_get_dataset_version", "Current version of a DVC-tracked dataset: data tag, DVC md5, "
                     "Git commit, row/column counts and whether the workspace file matches the recorded hash.",
                     DatasetPathInput, self.get_dataset_version, m),
            ToolSpec("dvc_list_dataset_versions", "History of a dataset: every Git commit that changed its .dvc "
                     "file with md5, size, rows and data tags (e.g. data-v1.0).",
                     DatasetPathInput, self.list_dataset_versions, m),
            ToolSpec("dvc_compare_dataset_versions", "Compare two dataset versions (tags/commits): hash change, "
                     "row delta, schema changes and the dvc diff output.",
                     CompareDatasetVersionsInput, self.compare_dataset_versions, m),
            ToolSpec("dvc_check_data_changes", "Detect whether DVC-tracked data in the workspace changed compared "
                     "with the committed version (dvc status).", NoInput, self.check_data_changes, m),
            ToolSpec("dvc_resolve_dataset_reference", "Map a dataset md5 or Git commit recorded by a training run "
                     "(e.g. MLflow tags) to the DVC dataset version/tag.",
                     ResolveDatasetInput, self.resolve_dataset_reference, m),
            ToolSpec("dvc_explain_reproduction", "Explain the exact commands to reproduce a previous dataset "
                     "version (non-destructive and in-workspace).", DatasetRevisionInput,
                     self.explain_reproduction, m),
            ToolSpec("dvc_reproduce_dataset_version", "Materialise a previous dataset version into "
                     "artifacts/reproduced/<rev>/ with 'dvc get' and verify its hash (does not modify the "
                     "tracked workspace).", DatasetRevisionInput, self.reproduce_dataset_version, m),
        ]
