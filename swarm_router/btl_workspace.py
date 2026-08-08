"""Manager-owned Git worktrees for BTL Developer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess


REMOTE = "origin"
TASK_ID_RE = re.compile(r"FT-[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?$")
BRANCH_RE = re.compile(r"btl/[A-Za-z0-9][A-Za-z0-9-]{1,98}$")
SHA_RE = re.compile(r"[0-9a-f]{40}$")


@dataclass(frozen=True)
class WorktreeInfo:
    repo_root: Path
    root: Path
    branch: str
    base_sha: str


@dataclass(frozen=True)
class ChangedFiles:
    paths: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.paths


def _git(*args: str, cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
        timeout=timeout,
    )


def _git_ok(*args: str, cwd: Path, timeout: int = 120) -> str:
    result = _git(*args, cwd=cwd, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git failed")
    return result.stdout.strip()


def validate_task_id(task_id: str) -> None:
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise ValueError("task_id must match FT-<letters, digits, or hyphens>")


def _slug(text: str, limit: int = 48) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit]
    return value or "task"


def generate_branch(task_id: str, instruction: str = "") -> str:
    validate_task_id(task_id)
    branch = f"btl/{task_id}-{_slug(instruction or task_id)}"
    if not validate_branch(branch):
        raise ValueError("generated invalid task branch")
    return branch


def validate_branch(branch: str) -> bool:
    return isinstance(branch, str) and bool(BRANCH_RE.fullmatch(branch))


def resolve_base_sha(repo_root: Path, base_branch: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", base_branch):
        raise ValueError("invalid base branch")
    if base_branch.startswith("btl/") or base_branch.startswith("refs/tags/"):
        raise ValueError("task branches and tags cannot be bases")
    sha = _git_ok("rev-parse", "--verify", f"{base_branch}^{{commit}}", cwd=repo_root)
    if not SHA_RE.fullmatch(sha):
        raise RuntimeError("base branch did not resolve to a commit")
    return sha


def resolve_worktree_root(configured_root: str | Path, task_id: str) -> Path:
    validate_task_id(task_id)
    root = Path(configured_root).expanduser().resolve()
    return root / task_id.lower()


def create_task_worktree(
    repo_root: Path,
    worktree_root: str | Path,
    task_id: str,
    branch: str,
    base_sha: str,
) -> WorktreeInfo:
    repo = repo_root.resolve(strict=True)
    validate_task_id(task_id)
    if not validate_branch(branch) or not branch.startswith(f"btl/{task_id}-"):
        raise ValueError("branch must be the generated branch for this task")
    if not SHA_RE.fullmatch(base_sha):
        raise ValueError("invalid base SHA")

    root = Path(worktree_root).expanduser().resolve()
    path = resolve_worktree_root(root, task_id)
    if path == repo or repo in path.parents:
        raise ValueError("BTL worktree must be outside the normal checkout")
    if path.exists() or _git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", cwd=repo).returncode == 0:
        raise FileExistsError("task worktree or branch already exists; inspect persisted state")
    remote = _git("ls-remote", "--exit-code", "--heads", REMOTE, f"refs/heads/{branch}", cwd=repo)
    if remote.returncode == 0:
        raise FileExistsError("remote task branch already exists")
    if remote.returncode != 2:
        raise RuntimeError(remote.stderr.strip() or "cannot check remote task branch")
    root.mkdir(parents=True, exist_ok=True)
    _git_ok("worktree", "add", "-b", branch, str(path), base_sha, cwd=repo)
    info = WorktreeInfo(repo, path.resolve(strict=True), branch, base_sha)
    issues = verify_workspace_integrity(info, require_base_head=True)
    if issues:
        raise RuntimeError("invalid created worktree: " + "; ".join(issues))
    return info


def inspect_changed_files(worktree: WorktreeInfo) -> ChangedFiles:
    result = _git("status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=worktree.root)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git status failed")
    raw = result.stdout
    paths: list[str] = []
    entries = raw.split("\0") if raw else []
    index = 0
    while index < len(entries):
        entry = entries[index]
        if not entry:
            index += 1
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if "R" in status or "C" in status:
            index += 1
            if index < len(entries) and entries[index]:
                paths.append(entries[index])
        index += 1
    return ChangedFiles(tuple(dict.fromkeys(paths)))


def verify_workspace_integrity(
    worktree: WorktreeInfo, *, require_base_head: bool = False
) -> list[str]:
    issues: list[str] = []
    try:
        repo = worktree.repo_root.resolve(strict=True)
        root = worktree.root.resolve(strict=True)
    except OSError as exc:
        return [f"missing worktree path: {exc}"]
    if root == repo or repo in root.parents:
        issues.append("worktree is not isolated from normal checkout")
    if not validate_branch(worktree.branch):
        issues.append("invalid task branch")
    if not SHA_RE.fullmatch(worktree.base_sha):
        issues.append("invalid base SHA")
    try:
        if Path(_git_ok("rev-parse", "--show-toplevel", cwd=root)).resolve() != root:
            issues.append("worktree top-level mismatch")
        if _git_ok("branch", "--show-current", cwd=root) != worktree.branch:
            issues.append("worktree branch mismatch")
        if require_base_head and _git_ok("rev-parse", "HEAD", cwd=root) != worktree.base_sha:
            issues.append("worktree HEAD does not match base SHA")
        common = Path(_git_ok("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=root)).resolve()
        expected = Path(_git_ok("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo)).resolve()
        if common != expected:
            issues.append("worktree belongs to another repository")
    except RuntimeError as exc:
        issues.append(str(exc))
    return issues


def manager_commit(worktree: WorktreeInfo, message: str) -> str:
    issues = verify_workspace_integrity(worktree, require_base_head=True)
    if issues:
        raise ValueError("commit blocked: " + "; ".join(issues))
    if inspect_changed_files(worktree).is_empty:
        raise ValueError("commit blocked: no changes")
    _git_ok("add", "--all", "--", ".", cwd=worktree.root)
    _git_ok("commit", "-m", message[:240], cwd=worktree.root)
    sha = _git_ok("rev-parse", "HEAD", cwd=worktree.root)
    if not SHA_RE.fullmatch(sha):
        raise RuntimeError("commit did not produce a valid SHA")
    return sha


def manager_push(worktree: WorktreeInfo, expected_sha: str) -> str:
    issues = verify_workspace_integrity(worktree)
    if issues:
        raise ValueError("push blocked: " + "; ".join(issues))
    if not SHA_RE.fullmatch(expected_sha) or _git_ok("rev-parse", "HEAD", cwd=worktree.root) != expected_sha:
        raise ValueError("push SHA does not match worktree HEAD")
    destination = f"refs/heads/{worktree.branch}"
    _git_ok("push", REMOTE, f"{expected_sha}:{destination}", cwd=worktree.root, timeout=300)
    remote = _git_ok("ls-remote", "--heads", REMOTE, destination, cwd=worktree.root, timeout=120)
    pushed = remote.split("\t", 1)[0] if remote else ""
    if pushed != expected_sha:
        raise RuntimeError("remote SHA does not match implementation commit")
    return pushed
