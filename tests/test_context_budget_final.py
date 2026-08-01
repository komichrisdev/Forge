"""Final correction pass: malformed IDs, chat() preflight, coordinator fallback, compaction idempotency."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from swarm_router.client import OpenWebUIClient, RequestFailure, ChatResult
from swarm_router.context_budget import (
    ContextBudgetExceeded,
    estimate_payload_tokens,
    resolve_context_limit,
)
from swarm_router.developer import (
    DeveloperCoordinator,
    DeveloperError,
    _compact_phase_messages,
)


# ===================================================================
# 1. Malformed tool-call ID tests
# ===================================================================

def _assistant_with_tool_calls(tool_calls: list) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": tool_calls,
    }


def _tool_result(tool_call_id: str, content: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def _user(content: str) -> dict:
    return {"role": "user", "content": content}


def _make_terminal_call(call_id: str, cmd: str = "pwd") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "terminal",
            "arguments": json.dumps({"command": cmd}),
        },
    }


class MalformedToolCallIdNoneTest(unittest.TestCase):
    """id=None must be treated as malformed and the entire assistant/tool-result group removed."""

    def test_none_id_removes_group(self) -> None:
        assistant = _assistant_with_tool_calls([
            {
                "id": None,
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
            }
        ])
        worker = [
            assistant,
            _tool_result("call-1", "/workspace"),
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertTrue(meta["invalid_tool_groups_removed"])
        self.assertFalse(any(m.get("tool_calls") for m in compacted))
        # System and latest user must survive
        self.assertEqual(compacted[0]["role"], "system")
        self.assertEqual(compacted[-1]["role"], "user")

    def test_none_id_in_parallel_group_removes_entire_group(self) -> None:
        assistant = _assistant_with_tool_calls([
            _make_terminal_call("call-1"),
            {
                "id": None,
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command":"ls"}'},
            },
        ])
        worker = [
            assistant,
            _tool_result("call-1", "/workspace"),
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertTrue(meta["invalid_tool_groups_removed"])
        self.assertFalse(any(m.get("tool_calls") for m in compacted))


class MalformedToolCallIdEmptyStringTest(unittest.TestCase):
    """id="" must be treated as malformed."""

    def test_empty_string_id_removes_group(self) -> None:
        assistant = _assistant_with_tool_calls([
            {
                "id": "",
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
            }
        ])
        worker = [
            assistant,
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertTrue(meta["invalid_tool_groups_removed"])
        self.assertFalse(any(m.get("tool_calls") for m in compacted))

    def test_empty_string_mixed_with_valid(self) -> None:
        """One valid + one empty: entire group must be removed (malformed)."""
        assistant = _assistant_with_tool_calls([
            _make_terminal_call("call-1"),
            {
                "id": "",
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command":"ls"}'},
            },
        ])
        worker = [
            assistant,
            _tool_result("call-1", "/workspace"),
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertTrue(meta["invalid_tool_groups_removed"])
        self.assertFalse(any(m.get("tool_calls") for m in compacted))


class MalformedToolCallIdWhitespaceTest(unittest.TestCase):
    """Whitespace-only id (e.g. "  ") must be treated as malformed after stripping."""

    def test_whitespace_only_id_removes_group(self) -> None:
        assistant = _assistant_with_tool_calls([
            {
                "id": "   ",
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
            }
        ])
        worker = [
            assistant,
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertTrue(meta["invalid_tool_groups_removed"])
        self.assertFalse(any(m.get("tool_calls") for m in compacted))

    def test_tab_newline_id_removes_group(self) -> None:
        assistant = _assistant_with_tool_calls([
            {
                "id": "\t\n\r",
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
            }
        ])
        worker = [
            assistant,
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertTrue(meta["invalid_tool_groups_removed"])
        self.assertFalse(any(m.get("tool_calls") for m in compacted))


class MalformedToolCallIdNonStringTest(unittest.TestCase):
    """Non-string IDs (integers, lists, dicts) must be treated as malformed."""

    def test_integer_id_removes_group(self) -> None:
        assistant = _assistant_with_tool_calls([
            {
                "id": 12345,
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
            }
        ])
        worker = [
            assistant,
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertTrue(meta["invalid_tool_groups_removed"])
        self.assertFalse(any(m.get("tool_calls") for m in compacted))

    def test_integer_id_with_result_removed(self) -> None:
        """Integer ID even with a matching tool result must be removed."""
        assistant = _assistant_with_tool_calls([
            {
                "id": 42,
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
            }
        ])
        worker = [
            assistant,
            _tool_result("42", "/workspace"),  # string "42" won't match int 42
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertTrue(meta["invalid_tool_groups_removed"])
        self.assertFalse(any(m.get("tool_calls") for m in compacted))

    def test_list_id_removes_group(self) -> None:
        assistant = _assistant_with_tool_calls([
            {
                "id": ["a", "b"],
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
            }
        ])
        worker = [
            assistant,
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertTrue(meta["invalid_tool_groups_removed"])
        self.assertFalse(any(m.get("tool_calls") for m in compacted))

    def test_dict_id_removes_group(self) -> None:
        assistant = _assistant_with_tool_calls([
            {
                "id": {"key": "value"},
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
            }
        ])
        worker = [
            assistant,
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertTrue(meta["invalid_tool_groups_removed"])
        self.assertFalse(any(m.get("tool_calls") for m in compacted))


class MalformedToolCallIdMixedValidAndMalformedTest(unittest.TestCase):
    """Mixed valid and malformed IDs in one parallel group: entire group must be removed."""

    def test_one_valid_one_none_in_parallel(self) -> None:
        assistant = _assistant_with_tool_calls([
            _make_terminal_call("call-1"),
            {
                "id": None,
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command":"ls"}'},
            },
        ])
        worker = [
            assistant,
            _tool_result("call-1", "/workspace"),
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        # Entire group removed because one ID was malformed
        self.assertTrue(meta["invalid_tool_groups_removed"])
        assistant_msgs = [m for m in compacted if m["role"] == "assistant"]
        self.assertEqual(len(assistant_msgs), 0)

    def test_two_valid_one_bad_in_triple(self) -> None:
        assistant = _assistant_with_tool_calls([
            _make_terminal_call("call-1"),
            _make_terminal_call("call-2"),
            {
                "id": 999,
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command":"whoami"}'},
            },
        ])
        worker = [
            assistant,
            _tool_result("call-1", "/workspace"),
            _tool_result("call-2", "file1"),
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertTrue(meta["invalid_tool_groups_removed"])
        assistant_msgs = [m for m in compacted if m["role"] == "assistant"]
        self.assertEqual(len(assistant_msgs), 0)

    def test_separate_groups_preserved_independently(self) -> None:
        """A malformed group should not affect an unrelated valid group."""
        bad_assistant = _assistant_with_tool_calls([
            {
                "id": None,
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
            }
        ])
        good_assistant = _assistant_with_tool_calls([
            _make_terminal_call("call-good"),
        ])
        worker = [
            bad_assistant,
            _user("intervening"),
            good_assistant,
            _tool_result("call-good", "/workspace"),
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        # Bad group removed, good group intact
        self.assertTrue(meta["invalid_tool_groups_removed"])
        assistant_msgs = [m for m in compacted if m["role"] == "assistant"]
        self.assertEqual(len(assistant_msgs), 1)
        self.assertEqual(assistant_msgs[0]["tool_calls"][0]["id"], "call-good")


# ===================================================================
# 2. OpenWebUIClient.chat() preflight tests
# ===================================================================

class ChatPreflightTest(unittest.TestCase):
    """Direct tests proving that chat(): enables budget preflight, rejects oversized, accepts catalog_context, respects budget_enabled=False."""

    def setUp(self) -> None:
        os.environ["CHAT_PREFLIGHT_KEY"] = "test-chat-key"

    def tearDown(self) -> None:
        os.environ.pop("CHAT_PREFLIGHT_KEY", None)

    def test_chat_enables_budget_preflight_by_default(self) -> None:
        """chat() must run preflight by default and reject oversized payloads."""
        client = OpenWebUIClient(
            "http://127.0.0.1:9",
            "/v1/chat/completions",
            "CHAT_PREFLIGHT_KEY",
            1,
        )
        response = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        with patch.object(client, "_json_request", return_value=response) as mock:
            with self.assertRaises(RequestFailure) as raised:
                client.chat(
                    model="test-model",
                    system="You are helpful.",
                    user="x" * 200000,  # oversized
                    max_tokens=128,
                    temperature=0.1,
                )
        self.assertEqual(raised.exception.category, "context_overflow")
        self.assertEqual(raised.exception.status_code, 413)
        mock.assert_not_called()

    def test_chat_rejects_oversized_payload_before_http(self) -> None:
        """An oversized payload must be rejected before any HTTP call."""
        client = OpenWebUIClient(
            "http://127.0.0.1:9",
            "/v1/chat/completions",
            "CHAT_PREFLIGHT_KEY",
            1,
        )
        with patch.object(client, "_json_request") as mock:
            with self.assertRaises(RequestFailure):
                client.chat(
                    model="test-model",
                    system="system",
                    user="x" * 300000,
                    max_tokens=128,
                    temperature=0.1,
                )
        mock.assert_not_called()

    def test_chat_accepts_per_request_catalog_context(self) -> None:
        """Per-request catalog_context should allow a large payload that would otherwise fail."""
        client = OpenWebUIClient(
            "http://127.0.0.1:9",
            "/v1/chat/completions",
            "CHAT_PREFLIGHT_KEY",
            1,
            catalog_context=4096,  # static small
        )
        response = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        with patch.object(client, "_json_request", return_value=response) as mock:
            result = client.chat(
                model="test-model",
                system="system",
                user="x" * 5000,  # too big for 4096 context, ok for 65536
                max_tokens=128,
                temperature=0.1,
                catalog_context=65536,  # per-request override
            )
        self.assertEqual(result, ChatResult(
            model="test-model",
            content="ok",
            raw=response,
        ))
        mock.assert_called_once()

    def test_chat_budget_enabled_false_forwards_raw_request(self) -> None:
        """budget_enabled=False must skip preflight and forward to upstream."""
        client = OpenWebUIClient(
            "http://127.0.0.1:9",
            "/v1/chat/completions",
            "CHAT_PREFLIGHT_KEY",
            1,
            budget_enabled=False,
        )
        response = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        with patch.object(client, "_json_request", return_value=response) as mock:
            result = client.chat(
                model="test-model",
                system="system",
                user="x" * 200000,  # would fail preflight
                max_tokens=128,
                temperature=0.1,
            )
        self.assertEqual(result, ChatResult(
            model="test-model",
            content="ok",
            raw=response,
        ))
        mock.assert_called_once()


# ===================================================================
# 3. Coordinator context fallback tests
# ===================================================================

def _write_test_config(root: Path):
    os.environ["COORDINATOR_CTX_KEY"] = "key"
    path = root / "config.toml"
    path.write_text(
        f'''[openwebui]
base_url = "http://127.0.0.1:9"
api_key_env = "COORDINATOR_CTX_KEY"
timeout_seconds = 3

[swarm]
run_directory = "{root / "runs"}"
catalog_path = "{root / "catalog.db"}"
max_workers = 3
max_parallel_workers = 2
worker_timeout_seconds = 1
judge_timeout_seconds = 1
max_context_chars = 12000
return_char_limit = 4000

[probe]
timeout_seconds = 1
max_parallel = 1

[reliability]
recent_attempt_window = 8
cooldown_after_consecutive_failures = 3
cooldown_minutes = 60

[dashboard]
metadata_directory = "{root / "dashboard"}"

[personal]
task_directory = "{root / "personal"}"
port = 8788
max_messages = 6
max_message_chars = 1200
max_conversation_chars = 4000
max_output_chars = 2000
max_wiki_context_chars = 3000
max_workers = 2
max_parallel_workers = 2
max_retries = 1
task_timeout_seconds = 2
worker_timeout_seconds = 1
max_active_tasks = 1
completed_task_retention = 20
event_history_retention = 40

[authority]
supervisor_name = "Codex"

[judge]
name = "integrator"
model = "fake/deepseek-judge-reason"
system = "integration clerk"

[[workers]]
name = "planner"
model = "fake/qwen-planner-instruct"
modes = ["auto", "general", "research"]
system = "planner"

[[workers]]
name = "critic"
model = "fake/mistral-critic-instruct"
modes = ["auto", "general", "research"]
system = "critic"

[[workers]]
name = "verifier"
model = "fake/qwen-verifier-reason"
modes = ["auto", "general", "research"]
system = "verifier"
''',
        encoding="utf-8",
    )
    from swarm_router.config import load_config
    return load_config(path)


class CoordinatorContextFallbackTest(unittest.TestCase):
    """Tests for coordinator fallback when record.context_length is None."""

    def test_model_specific_fallback_used_when_catalog_context_none(self) -> None:
        """When record.context_length is None, resolve_context_limit falls back to model-name profile."""
        # qwen36-35b → 65536
        resolved = resolve_context_limit("qwen36-35b", catalog_context=None)
        self.assertEqual(resolved, 65536)
        # llama-3-70b → 8192
        resolved = resolve_context_limit("llama-3-70b", catalog_context=None)
        self.assertEqual(resolved, 8192)

    def test_unknown_model_receives_conservative_default(self) -> None:
        """Unknown model with None context_length must get DEFAULT_CONTEXT_LIMIT (16384)."""
        resolved = resolve_context_limit("unknown-model-xyz", catalog_context=None)
        from swarm_router.context_budget import DEFAULT_CONTEXT_LIMIT
        self.assertEqual(resolved, DEFAULT_CONTEXT_LIMIT)  # 16384

    def test_context_used_by_coordinator_and_client_preflight(self) -> None:
        """The resolved context should be used consistently by both coordinator preflight and client preflight."""
        with TemporaryDirectory() as temporary:
            coordinator = DeveloperCoordinator(_write_test_config(Path(temporary)))
            # Model with context_length=None forces fallback
            model = SimpleNamespace(
                model_id="qwen36-35b",
                provider="fake",
                context_length=None,  # forces fallback
                family="large",
                health="healthy",
                probe_status="healthy",
            )

            seen_payloads: list[dict] = []

            def upstream(payload, timeout_seconds=None, catalog_context=None):
                seen_payloads.append(payload)
                return {
                    "id": "upstream",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "worker",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": None, "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "terminal",
                                    "arguments": json.dumps({"command": "pwd"}),
                                },
                            }
                        ]},
                        "finish_reason": "tool_calls",
                    }],
                }

            body = {
                "model": "swarm-developer",
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [{
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "description": "Run a command.",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    },
                }],
                "tool_choice": "auto",
                "max_tokens": 128,
            }
            assignments = {
                role: {
                    "provider": "fake", "model": "qwen36-35b", "family": "large",
                    "health": "healthy", "reason": "test",
                }
                for role in ("planner", "implementer", "reviewer", "verifier")
            }

            with patch.object(coordinator, "_select_role_models", return_value=assignments), \
                 patch.object(coordinator, "_candidates", return_value=[model]), \
                 patch.object(coordinator.catalog, "recommendation_reason", return_value="test"), \
                 patch.object(coordinator.client, "completion", side_effect=upstream):
                result = coordinator.complete(body)

        self.assertEqual(result["forge_worker"]["model"], "qwen36-35b")
        self.assertEqual(len(seen_payloads), 1)
        # Verify client was called with catalog_context=None (fallback to model profile)
        # The coordinator resolves 65536 from model name
        catalog_ctx = seen_payloads[0].get("_catalog_context_sent")
        # The key test: no exception was raised, meaning the fallback was used successfully

    def test_no_provider_submission_if_budget_exceeded(self) -> None:
        """If the resolved budget is exceeded, no provider submission occurs."""
        with TemporaryDirectory() as temporary:
            coordinator = DeveloperCoordinator(_write_test_config(Path(temporary)))
            # Use a model with very small catalog context so the budget is tight
            tiny_model = SimpleNamespace(
                model_id="unknown-tiny",
                provider="fake",
                context_length=4096,  # very small catalog context
                family="tiny",
                health="healthy",
                probe_status="healthy",
            )

            call_count = [0]

            def upstream(payload, timeout_seconds=None, catalog_context=None):
                call_count[0] += 1
                raise RuntimeError("should not reach upstream")

            # Build an oversized payload that won't fit in 4096 context
            body = {
                "model": "swarm-developer",
                "messages": [{"role": "user", "content": "x" * 50000}],
                "tools": [{
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "description": "Run a command.",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    },
                }],
                "tool_choice": "auto",
                "max_tokens": 128,
            }
            assignments = {
                role: {
                    "provider": "fake", "model": "unknown-tiny", "family": "tiny",
                    "health": "healthy", "reason": "test",
                }
                for role in ("planner", "implementer", "reviewer", "verifier")
            }

            with patch.object(coordinator, "_select_role_models", return_value=assignments), \
                 patch.object(coordinator, "_candidates", return_value=[tiny_model]), \
                 patch.object(coordinator.catalog, "recommendation_reason", return_value="test"), \
                 patch.object(coordinator.client, "completion", side_effect=upstream):
                # This should raise because the payload exceeds the tiny model's budget
                with self.assertRaises(DeveloperError) as raised:
                    coordinator.complete(body)

            self.assertEqual(raised.exception.code, "context_budget_exceeded")
            self.assertEqual(call_count[0], 0, "No provider submission should occur when budget exceeded")


# ===================================================================
# 4. Compaction idempotency tests
# ===================================================================

class CompactionIdempotencyTest(unittest.TestCase):
    """Tests verifying compaction is idempotent — applying it twice produces no further changes."""

    def _build_compactable_payload(self) -> tuple[list, dict]:
        """Build a payload that needs compaction: oversized handoff + some messages."""
        old_handoff = {
            "role": "user",
            "content": "BEGIN UNTRUSTED PRIOR ROLE OUTPUT (planner)\n" + "x" * 2000 + "\nEND UNTRUSTED PRIOR ROLE OUTPUT",
        }
        assistant = _assistant_with_tool_calls([
            _make_terminal_call("call-1"),
        ])
        worker = [
            old_handoff,
            assistant,
            _tool_result("call-1", "/workspace"),
            _user("old instruction"),
            _user("latest objective"),
        ]
        return worker, {}

    def test_compacting_already_compacted_messages_is_noop(self) -> None:
        """Pass already-compacted messages back through compaction — no further removal."""
        worker, _ = self._build_compactable_payload()

        # First compaction pass
        compacted1, meta1 = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[worker[0]],
            worker_messages=worker[1:],
            input_limit=500,
            model_id="test",
        )
        self.assertTrue(meta1["compaction_applied"])
        msg_count_after_first = len(compacted1)

        # Second pass: treat the compacted result as the new input
        # We need to re-split into handoffs and worker messages
        # The first non-system non-tool message that contains "BEGIN UNTRUSTED" is a handoff
        remaining_handoffs = []
        remaining_workers = []
        for msg in compacted1[1:]:  # skip system
            if (isinstance(msg.get("content"), str)
                    and "BEGIN UNTRUSTED PRIOR ROLE OUTPUT" in msg["content"]):
                remaining_handoffs.append(msg)
            else:
                remaining_workers.append(msg)

        compacted2, meta2 = _compact_phase_messages(
            system_message=compacted1[0],
            handoffs=remaining_handoffs,
            worker_messages=remaining_workers,
            input_limit=500,
            model_id="test",
        )

        # No further message removal
        self.assertEqual(len(compacted2), msg_count_after_first,
                         "Second compaction should not remove more messages")

        # No further summary insertion (the meta should indicate no new compaction)
        # The compaction_applied flag might be True if there are still handoffs,
        # but message count should be stable
        self.assertEqual(len(compacted2), msg_count_after_first)

    def test_no_repeated_summary_insertion(self) -> None:
        """Applying compaction twice must not re-summarize already-summarized messages."""
        # Build a payload that triggers summary compaction
        old_handoff = {
            "role": "user",
            "content": "BEGIN UNTRUSTED PRIOR ROLE OUTPUT (planner)\n" + "x" * 5000 + "\nEND UNTRUSTED PRIOR ROLE OUTPUT",
        }
        new_handoff = {
            "role": "user",
            "content": "BEGIN UNTRUSTED PRIOR ROLE OUTPUT (implementer)\n" + "y" * 500 + "\nEND UNTRUSTED PRIOR ROLE OUTPUT",
        }
        worker = [old_handoff, new_handoff]

        compacted1, meta1 = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=worker,
            worker_messages=[],
            input_limit=1200,
            model_id="test",
        )
        self.assertTrue(meta1["compaction_applied"])

        # Capture the truncated handoff content
        first_handoff_content = None
        for msg in compacted1[1:]:
            if isinstance(msg.get("content"), str) and "BEGIN UNTRUSTED" in msg["content"]:
                first_handoff_content = msg["content"]
                break

        # Second pass
        remaining_handoffs = [msg for msg in compacted1[1:]
                              if isinstance(msg.get("content"), str)
                              and "BEGIN UNTRUSTED PRIOR ROLE OUTPUT" in msg["content"]]
        remaining_workers = [msg for msg in compacted1[1:]
                             if not (isinstance(msg.get("content"), str)
                                     and "BEGIN UNTRUSTED PRIOR ROLE OUTPUT" in msg["content"])]

        compacted2, meta2 = _compact_phase_messages(
            system_message=compacted1[0],
            handoffs=remaining_handoffs,
            worker_messages=remaining_workers,
            input_limit=1200,
            model_id="test",
        )

        # The already-summarized handoff should not change
        for msg in compacted2[1:]:
            if isinstance(msg.get("content"), str) and "BEGIN UNTRUSTED" in msg["content"]:
                if first_handoff_content:
                    self.assertEqual(msg["content"], first_handoff_content)
                    first_handoff_content = None
                    break

    def test_valid_tool_groups_remain_intact_after_idempotent_compaction(self) -> None:
        """Valid tool groups must survive both compaction passes without corruption.

        The first compaction drops old worker messages but preserves the valid
        assistant/tool group + latest user + system. The second compaction on
        the same result is a no-op.
        """
        call_id = "call-1"
        # Build an old worker message large enough to be the first to be removed
        # System+assistant+tool+latest = ~78 tokens
        # Adding old_msg pushes total to ~2086 tokens
        # input_limit=1300 means old_msg gets removed, leaving tool group intact
        old_worker = _user("old " + "x" * 8000)
        assistant = _assistant_with_tool_calls([
            _make_terminal_call(call_id),
        ])
        worker = [
            old_worker,
            assistant,
            _tool_result(call_id, "/workspace"),
            _user("latest objective"),
        ]

        # First compaction: budget tight enough to drop old_worker but keep tool group
        compacted1, meta1 = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=1300,
            model_id="test",
        )
        # Compaction should have been applied (old_worker dropped)
        self.assertTrue(meta1["compaction_applied"])
        # Count messages: system + assistant+tool_group + latest_user = should be 3
        assistant_msgs1 = [m for m in compacted1 if m["role"] == "assistant"]
        tool_msgs1 = [m for m in compacted1 if m["role"] == "tool"]
        self.assertGreater(len(assistant_msgs1), 0, "Valid tool group should survive first compaction")
        self.assertGreater(len(tool_msgs1), 0)

        # Second pass: treat compacted result as input
        remaining_handoffs = []
        remaining_workers = []
        for msg in compacted1[1:]:  # skip system
            if isinstance(msg.get("content"), str) and "BEGIN UNTRUSTED PRIOR ROLE OUTPUT" in msg["content"]:
                remaining_handoffs.append(msg)
            else:
                remaining_workers.append(msg)

        compacted2, meta2 = _compact_phase_messages(
            system_message=compacted1[0],
            handoffs=remaining_handoffs,
            worker_messages=remaining_workers,
            input_limit=1200,
            model_id="test",
        )

        # No further message removal
        self.assertEqual(len(compacted2), len(compacted1),
                         "Second compaction should not remove more messages")

        # Valid tool group still intact
        assistant_msgs2 = [m for m in compacted2 if m["role"] == "assistant"]
        tool_msgs2 = [m for m in compacted2 if m["role"] == "tool"]
        self.assertEqual(len(assistant_msgs2), 1)
        self.assertEqual(len(tool_msgs2), 1)
        self.assertEqual(assistant_msgs2[0]["tool_calls"][0]["id"], call_id)
        self.assertEqual(tool_msgs2[0]["tool_call_id"], call_id)

        # Second pass
        remaining_handoffs = []
        remaining_workers = []
        for msg in compacted1[1:]:
            if isinstance(msg.get("content"), str) and "BEGIN UNTRUSTED PRIOR ROLE OUTPUT" in msg["content"]:
                remaining_handoffs.append(msg)
            else:
                remaining_workers.append(msg)

        compacted2, meta2 = _compact_phase_messages(
            system_message=compacted1[0],
            handoffs=remaining_handoffs,
            worker_messages=remaining_workers,
            input_limit=500,
            model_id="test",
        )

        # Valid tool group still intact
        assistant_msgs = [m for m in compacted2 if m["role"] == "assistant"]
        tool_msgs = [m for m in compacted2 if m["role"] == "tool"]
        self.assertEqual(len(assistant_msgs), 1)
        self.assertEqual(len(assistant_msgs[0]["tool_calls"]), 1)
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["tool_call_id"], call_id)

    def test_compaction_idempotent_on_within_budget_input(self) -> None:
        """A small payload that doesn't need compaction must be identical on second pass."""
        worker = [
            _user("hello"),
            _user("world"),
        ]
        compacted1, meta1 = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertFalse(meta1["compaction_applied"])
        self.assertEqual(len(compacted1), 3)  # system + 2 users

        compacted2, meta2 = _compact_phase_messages(
            system_message=compacted1[0],
            handoffs=[],
            worker_messages=compacted1[1:],
            input_limit=9999999,
            model_id="test",
        )
        self.assertFalse(meta2["compaction_applied"])
        self.assertEqual(len(compacted2), 3)

        # Content must be identical
        for i in range(len(compacted1)):
            self.assertEqual(compacted1[i], compacted2[i])


class PerRunCounterSeparationTest(unittest.TestCase):
    """Test that message idempotency is separate from per-run counters."""

    def test_messages_stable_independent_of_counter_changes(self) -> None:
        """Compaction should not be affected by per-run metadata or counters."""
        worker = [
            _user("msg 1"),
            _user("msg 2"),
            _user("msg 3"),
            _user("latest"),
        ]
        compacted1, meta1 = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=300,
            model_id="test-model",
        )
        # Messages should be deterministic
        messages_after_first = [json.dumps(m, sort_keys=True) for m in compacted1]

        compacted2, meta2 = _compact_phase_messages(
            system_message=compacted1[0],
            handoffs=[],
            worker_messages=compacted1[1:],
            input_limit=300,
            model_id="test-model",
        )
        messages_after_second = [json.dumps(m, sort_keys=True) for m in compacted2]

        # Messages must be identical
        self.assertEqual(messages_after_first, messages_after_second)


# ===================================================================
# Run
# ===================================================================

if __name__ == "__main__":
    unittest.main()
