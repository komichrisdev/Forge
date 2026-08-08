from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from swarm_router.btl_developer import (
    BTLConfig, BTLDeveloperError, BTLTaskStatus, authoritative_verification,
    load_task_record, run_btl_manager, run_model_phase,
)
from swarm_router.btl_tools import BTLTools
from swarm_router.btl_workspace import PushConfirmationError
from swarm_router.cli import _parser, _run_btl
from swarm_router.config import load_config


def completion(content: str = "", call: tuple[str, dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if call:
        name, arguments = call
        message["tool_calls"] = [{
            "id": "call-1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }]
    return {"choices": [{"message": message}]}


class FakeClient:
    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.payloads: list[dict] = []

    def completion(self, payload: dict) -> dict:
        self.payloads.append(payload)
        if not self.responses:
            raise AssertionError("unexpected model call")
        return self.responses.pop(0)


class TestModelLoop(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init"], cwd=self.root, capture_output=True, check=True)
        self.tools = BTLTools(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_real_completion_shape_tools_and_same_model(self) -> None:
        client = FakeClient(
            completion(call=("write_file", {"path": "done.py", "content": "ok = True\n"})),
            completion("implemented"),
        )
        output = run_model_phase(
            client, "one-model", [{"role": "system", "content": "x"}], self.tools,
            writable=True, max_turns=2, max_tokens=512,
        )
        self.assertEqual(output, "implemented")
        self.assertTrue((self.root / "done.py").exists())
        self.assertEqual({payload["model"] for payload in client.payloads}, {"one-model"})
        self.assertTrue(client.payloads[0]["tools"])
        self.assertFalse(client.payloads[0]["parallel_tool_calls"])
        self.assertEqual(client.payloads[1]["messages"][-1]["role"], "tool")

    def test_planner_has_no_write_tools(self) -> None:
        client = FakeClient(completion(call=("write_file", {"path": "x", "content": "x"})))
        with self.assertRaises(BTLDeveloperError):
            run_model_phase(
                client, "model", [], self.tools, writable=False, max_turns=1, max_tokens=128,
            )
        self.assertFalse((self.root / "x").exists())

    def test_malformed_and_exhaustion_fail_boundedly(self) -> None:
        with self.assertRaises(BTLDeveloperError):
            run_model_phase(
                FakeClient({"bad": True}), "model", [], self.tools,
                writable=False, max_turns=1, max_tokens=128,
            )
        client = FakeClient(completion(call=("list_files", {})))
        with self.assertRaisesRegex(BTLDeveloperError, "turn limit"):
            run_model_phase(
                client, "model", [], self.tools, writable=False, max_turns=1, max_tokens=128,
            )


class ManagerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.repo, self.remote, self.worktrees = base / "repo", base / "remote.git", base / "worktrees"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "feature/btl-developer"], cwd=self.repo, check=True, capture_output=True)
        for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
            subprocess.run(["git", "config", key, value], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "init", "--bare", str(self.remote)], cwd=base, check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(self.remote)], cwd=self.repo, check=True)
        subprocess.run(["git", "push", "origin", "feature/btl-developer"], cwd=self.repo, check=True, capture_output=True)
        self.config = BTLConfig(model_id="configured-model", worktree_root=str(self.worktrees), max_phase_turns=2)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def client(self) -> FakeClient:
        return FakeClient(
            completion("inspect and change one file"),
            completion(call=("write_file", {"path": "implemented.py", "content": "value = 1\n"})),
            completion("implementation complete"),
        )


class TestManager(ManagerCase):
    @patch("swarm_router.btl_developer.authoritative_verification", return_value=(True, "all checks passed"))
    def test_success_verifies_commits_pushes_and_persists(self, verify) -> None:
        client = self.client()
        record = run_btl_manager(client, self.repo, "FT-100", "Add value", self.config)
        self.assertEqual(record.status, BTLTaskStatus.READY_FOR_EXTERNAL_REVIEW.value)
        self.assertEqual(record.implementation_commit, record.implementation_push_sha)
        self.assertEqual(record.verification_summary, "all checks passed")
        self.assertEqual({payload["model"] for payload in client.payloads}, {"configured-model"})
        persisted = load_task_record(self.worktrees, "FT-100")
        self.assertEqual(persisted, record)
        remote = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{record.task_branch}"],
            cwd=self.repo, capture_output=True, text=True, check=True,
        ).stdout
        self.assertTrue(remote.startswith(record.implementation_commit))
        verify.assert_called_once()

    @patch("swarm_router.btl_developer.authoritative_verification", return_value=(False, "tests failed"))
    def test_failed_verification_prevents_commit_and_push(self, _verify) -> None:
        with self.assertRaises(BTLDeveloperError):
            run_btl_manager(self.client(), self.repo, "FT-101", "Add value", self.config)
        record = load_task_record(self.worktrees, "FT-101")
        self.assertEqual(record.status, BTLTaskStatus.FAILED.value)
        self.assertFalse(record.implementation_commit)
        self.assertFalse(record.implementation_push_sha)
        self.assertIn("tests failed", record.failure_summary)

    @patch("swarm_router.btl_developer.manager_push", side_effect=PushConfirmationError("confirm unavailable"))
    @patch("swarm_router.btl_developer.authoritative_verification", return_value=(True, "passed"))
    def test_accepted_push_with_unconfirmed_sha_is_blocked(self, _verify, _push) -> None:
        with self.assertRaises(PushConfirmationError):
            run_btl_manager(self.client(), self.repo, "FT-104", "Add value", self.config)
        record = load_task_record(self.worktrees, "FT-104")
        self.assertEqual(record.status, BTLTaskStatus.BLOCKED.value)
        self.assertTrue(record.implementation_commit)
        self.assertFalse(record.implementation_push_sha)

    def test_invalid_task_id_cannot_escape_persistence_root(self) -> None:
        with self.assertRaises(ValueError):
            run_btl_manager(FakeClient(), self.repo, "../FT-1", "x", self.config)
        self.assertFalse((self.worktrees.parent / "FT-1.json").exists())

    def test_worktree_root_inside_checkout_is_rejected_before_state_write(self) -> None:
        config = BTLConfig(model_id="model", worktree_root=str(self.repo / "worktrees"))
        with self.assertRaisesRegex(ValueError, "outside the normal checkout"):
            run_btl_manager(FakeClient(), self.repo, "FT-103", "x", config)
        self.assertFalse((self.repo / "worktrees").exists())

    def test_existing_state_is_not_blindly_resumed(self) -> None:
        with patch("swarm_router.btl_developer.authoritative_verification", return_value=(False, "stop")):
            with self.assertRaises(BTLDeveloperError):
                run_btl_manager(self.client(), self.repo, "FT-102", "x", self.config)
        with self.assertRaises(FileExistsError):
            run_btl_manager(self.client(), self.repo, "FT-102", "x", self.config)

    def test_no_catalog_or_deprecated_coordinator_dependency(self) -> None:
        source = Path(__file__).parents[1].joinpath("swarm_router/btl_developer.py").read_text()
        self.assertNotIn("ModelCatalog", source)
        self.assertNotIn("DeveloperCoordinator", source)
        self.assertNotIn("merge", {status.value for status in BTLTaskStatus})
        self.assertNotIn("deploy", {status.value for status in BTLTaskStatus})


class TestVerification(unittest.TestCase):
    def test_nonzero_diff_check_fails_even_with_stderr_only(self) -> None:
        worktree = unittest.mock.MagicMock()
        worktree.root = Path("/tmp")
        failed = subprocess.CompletedProcess([], 1, "", "fatal error")
        with patch("swarm_router.btl_developer.workspace_fingerprint", return_value="tree"), patch(
            "swarm_router.btl_developer.subprocess.run", return_value=failed,
        ):
            passed, summary = authoritative_verification(worktree)
        self.assertFalse(passed)
        self.assertIn("fatal error", summary)

    def test_verification_does_not_forward_credentials(self) -> None:
        worktree = unittest.mock.MagicMock()
        worktree.root = Path("/tmp")
        failed = subprocess.CompletedProcess([], 1, "", "stop")
        with patch.dict("os.environ", {"OPEN_WEBUI_API_KEY": "secret"}), patch(
            "swarm_router.btl_developer.workspace_fingerprint", return_value="tree",
        ), patch(
            "swarm_router.btl_developer.subprocess.run", return_value=failed,
        ) as run:
            authoritative_verification(worktree)
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("OPEN_WEBUI_API_KEY", environment)
        self.assertEqual(environment["OPEN_TERMINAL_API_KEY"], "btl-verification-placeholder")

    def test_model_written_test_is_filesystem_and_network_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo, root, outside = base / "repo", base / "worktree", base / "outside"
            repo.mkdir()
            root.mkdir()
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
            (root / "swarm_router").mkdir()
            (root / "swarm_router" / "__init__.py").write_text("", encoding="utf-8")
            (root / "tests").mkdir()
            test_source = (
                "import pathlib, subprocess, unittest\n"
                "class Escape(unittest.TestCase):\n"
                "    def test_escape(self):\n"
                f"        target = pathlib.Path({str(outside)!r})\n"
                "        subprocess.run(['/bin/sh', '-c', 'echo escaped > ' + str(target)], check=True)\n"
                "        self.assertTrue(target.exists())\n"
            )
            (root / "tests" / "test_escape.py").write_text(test_source, encoding="utf-8")
            for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
                subprocess.run(["git", "config", key, value], cwd=root, check=True)
            subprocess.run(["git", "add", ".gitignore", "swarm_router/__init__.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True,
            ).stdout.strip()
            worktree = __import__("swarm_router.btl_workspace", fromlist=["WorktreeInfo"]).WorktreeInfo(
                repo, root, "btl/FT-1-test", sha,
            )
            passed, summary = authoritative_verification(worktree)
            self.assertTrue(passed, summary)
            self.assertFalse(outside.exists())


class TestIntegration(unittest.TestCase):
    def test_example_config_is_disabled_and_uses_one_btl_model(self) -> None:
        root = Path(__file__).parents[1]
        config = load_config(root / "config.example.toml", require_api_key=False)
        self.assertFalse(config.btl_developer.enabled)
        self.assertEqual(config.btl_developer.model, "local-qwen36-35b-a3b-windows")
        self.assertEqual(config.btl_developer.base_branch, "feature/btl-developer")

    def test_cli_accepts_minimal_operator_command(self) -> None:
        args = _parser().parse_args(["btl-dev", "run", "--prompt-file", "task.txt"])
        self.assertEqual((args.command, args.btl_command, args.prompt_file), ("btl-dev", "run", "task.txt"))

    def test_btl_prompt_reuses_secret_file_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prompt = Path(temporary) / ".env"
            prompt.write_text("TOKEN=value\n", encoding="utf-8")
            args = SimpleNamespace(btl_command="run", prompt_file=str(prompt), task_id="FT-1")
            config = SimpleNamespace(btl_developer=SimpleNamespace(enabled=True))
            with self.assertRaisesRegex(RuntimeError, "secret file"):
                _run_btl(args, config, FakeClient())

    def test_cli_routes_btl_before_catalog(self) -> None:
        source = Path(__file__).parents[1].joinpath("swarm_router/cli.py").read_text()
        self.assertLess(source.index('args.command == "btl-dev"'), source.index("catalog = ModelCatalog"))


if __name__ == "__main__":
    unittest.main()
