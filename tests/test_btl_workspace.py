from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from swarm_router.btl_workspace import (
    create_task_worktree, generate_branch, inspect_changed_files, manager_commit,
    manager_push, resolve_base_sha, validate_branch, validate_task_id,
    verify_workspace_integrity,
)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


class RepositoryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.repo, self.remote, self.worktrees = base / "repo", base / "remote.git", base / "worktrees"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "feature/btl-developer")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "base")
        git(base, "init", "--bare", str(self.remote))
        git(self.repo, "remote", "add", "origin", str(self.remote))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def worktree(self, task_id: str = "FT-123"):
        sha = resolve_base_sha(self.repo, "feature/btl-developer")
        branch = generate_branch(task_id, "Fix The Thing")
        return create_task_worktree(self.repo, self.worktrees, task_id, branch, sha)


class TestValidation(unittest.TestCase):
    def test_task_id_rejects_path_traversal(self) -> None:
        for value in ("../FT-1", "FT/1", "main", "FT-", ""):
            with self.assertRaises(ValueError):
                validate_task_id(value)

    def test_branch_is_btl_and_slug_is_lowercase(self) -> None:
        branch = generate_branch("FT-123", "Mixed CASE task")
        self.assertEqual(branch, "btl/FT-123-mixed-case-task")
        self.assertTrue(validate_branch(branch))
        for value in ("main", "feature/btl-developer", "refs/tags/x", "btl/x/y", "btl/x..y"):
            self.assertFalse(validate_branch(value))


class TestWorkspace(RepositoryCase):
    def test_worktree_is_external_attached_and_exact_base(self) -> None:
        worktree = self.worktree()
        self.assertNotIn(self.repo, worktree.root.parents)
        self.assertEqual(git(worktree.root, "branch", "--show-current"), worktree.branch)
        self.assertEqual(git(worktree.root, "rev-parse", "HEAD"), worktree.base_sha)
        self.assertEqual(verify_workspace_integrity(worktree, require_base_head=True), [])

    def test_existing_task_is_not_reused_or_reset(self) -> None:
        worktree = self.worktree()
        with self.assertRaises(FileExistsError):
            create_task_worktree(
                self.repo, self.worktrees, "FT-123", worktree.branch, worktree.base_sha,
            )

    def test_existing_remote_task_branch_is_rejected(self) -> None:
        sha = resolve_base_sha(self.repo, "feature/btl-developer")
        branch = generate_branch("FT-remote", "collision")
        git(self.repo, "push", "origin", f"{sha}:refs/heads/{branch}")
        with self.assertRaises(FileExistsError):
            create_task_worktree(self.repo, self.worktrees, "FT-remote", branch, sha)

    def test_commit_requires_changes_and_push_confirms_remote_sha(self) -> None:
        worktree = self.worktree()
        with self.assertRaises(ValueError):
            manager_commit(worktree, "empty")
        (worktree.root / "new.py").write_text("value = 1\n", encoding="utf-8")
        self.assertEqual(inspect_changed_files(worktree).paths, ("new.py",))
        commit = manager_commit(worktree, "BTL FT-123: test")
        self.assertEqual(manager_push(worktree, commit), commit)
        self.assertEqual(
            git(self.repo, "ls-remote", "--heads", "origin", f"refs/heads/{worktree.branch}").split()[0],
            commit,
        )

    def test_push_rejects_wrong_sha(self) -> None:
        worktree = self.worktree()
        with self.assertRaises(ValueError):
            manager_push(worktree, "0" * 40)


if __name__ == "__main__":
    unittest.main()
