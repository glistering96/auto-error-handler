from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from aeh.config import Limits
from aeh.errors import AehError

log = logging.getLogger("aeh.gitops")


def git(repo: Path, *args: str, timeout: int = 30) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            timeout=timeout,
            check=False,
            env={"PATH": os.environ.get("PATH", "")},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AehError(503, "AEH-GIT-503-001", "Git repository is unavailable") from exc
    if result.returncode:
        raise AehError(422, "AEH-GIT-422-001", "Commit or Git operation is invalid")
    return result.stdout


def resolve_commit(repo: Path, sha: str | None, default_branch: str) -> str:
    try:
        top = Path(git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    except AehError as exc:
        raise AehError(503, "AEH-GIT-503-001", "Git repository is unavailable") from exc
    if top != repo.resolve():
        raise AehError(503, "AEH-GIT-503-001", "Repository root is invalid")
    if sha is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
            raise AehError(422, "AEH-GIT-422-001", "Invalid commit SHA")
        ref = sha + "^{commit}"
    else:
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", default_branch) or ".." in default_branch:
            raise AehError(503, "AEH-GIT-503-001", "Default branch is invalid")
        ref = f"refs/heads/{default_branch}^{{commit}}"
    full = git(repo, "rev-parse", "--verify", "--end-of-options", ref).decode().strip()
    if len(full) not in (40, 64) or not re.fullmatch("[0-9a-f]+", full):
        raise AehError(422, "AEH-GIT-422-001", "Commit SHA did not resolve uniquely")
    return full


@contextmanager
def repository_lock(root: Path, repo: Path) -> Iterator[None]:
    lock_dir = root / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / (hashlib.sha256(str(repo).encode()).hexdigest() + ".lock")
    with lock_path.open("a+b") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


@contextmanager
def worktree(repo: Path, root: Path, kind: str, run_id: str, sha: str) -> Iterator[Path]:
    path = root / kind / run_id
    path.parent.mkdir(parents=True, exist_ok=True)
    with repository_lock(root, repo):
        git(repo, "worktree", "add", "--detach", "--", str(path), sha)
    try:
        yield path
    finally:
        with repository_lock(root, repo):
            try:
                git(repo, "worktree", "remove", "--force", "--", str(path))
            except AehError:
                log.exception("worktree_cleanup_failed path=%s", path)


def assert_clean(repo: Path) -> None:
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise AehError(422, "AEH-POLICY-422-001", "Analysis changed the repository")


def check_diff(workspace: Path, policy: dict) -> tuple[list[str], str]:
    ignored = git(workspace, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    if ignored:
        raise AehError(422, "AEH-POLICY-422-001", "Ignored files were created")
    # Intent-to-add makes new regular files visible to diff without committing them.
    raw = git(workspace, "ls-files", "--others", "--exclude-standard", "-z")
    untracked = [p.decode() for p in raw.split(b"\x00") if p]
    for path in untracked:
        candidate = workspace / path
        if candidate.is_symlink() or not candidate.is_file():
            raise AehError(422, "AEH-POLICY-422-001", "Non-regular changed file")
        git(workspace, "add", "-N", "--", path)
    names = [
        p.decode()
        for p in git(workspace, "diff", "--name-only", "-z", "HEAD", "--").split(b"\x00")
        if p
    ]
    if not names or len(names) > min(policy["maxChangedFiles"], Limits.max_changed_files):
        raise AehError(422, "AEH-POLICY-422-001", "Changed file count is invalid")
    for name in names:
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or not name or "\n" in name:
            raise AehError(422, "AEH-POLICY-422-001", "Invalid changed path")
        if not any(fnmatchcase(name, pattern) for pattern in policy["allowedPaths"]):
            raise AehError(422, "AEH-POLICY-422-001", "Changed path is not allowed")
        if any(fnmatchcase(name, pattern) for pattern in policy["deniedPaths"]):
            raise AehError(422, "AEH-POLICY-422-001", "Changed path is denied")
        if (
            name.startswith((".git/", ".github/", "infra/", ".env"))
            or "/.env" in name
            or name.endswith((".pem", ".key"))
        ):
            raise AehError(422, "AEH-POLICY-422-001", "Protected path was changed")
        if name.startswith("tests/") and name not in untracked:
            raise AehError(
                422, "AEH-POLICY-422-001", "Existing tests cannot be modified or deleted"
            )
        candidate = workspace / name
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
            raise AehError(422, "AEH-POLICY-422-001", "Changed file is a symlink or special file")
        modes = git(workspace, "ls-files", "--stage", "--", name).decode()
        if any(line.startswith(("120000", "160000")) for line in modes.splitlines()):
            raise AehError(422, "AEH-POLICY-422-001", "Symlink or submodule change is denied")
    numstat = git(workspace, "diff", "--numstat", "-z", "HEAD", "--")
    lines = 0
    for entry in numstat.split(b"\x00"):
        if not entry:
            continue
        parts = entry.split(b"\t", 2)
        if len(parts) < 3 or parts[0] == b"-" or parts[1] == b"-":
            raise AehError(422, "AEH-POLICY-422-001", "Binary change is denied")
        lines += int(parts[0]) + int(parts[1])
    if lines > min(policy["maxDiffLines"], Limits.max_diff_lines):
        raise AehError(422, "AEH-POLICY-422-001", "Diff line limit exceeded")
    git(workspace, "diff", "--check", "HEAD", "--")
    diff = git(workspace, "diff", "--no-ext-diff", "HEAD", "--").decode("utf-8")
    if len(diff.encode()) > Limits.diff_bytes:
        raise AehError(422, "AEH-POLICY-422-001", "Diff size limit exceeded")
    return names, diff
