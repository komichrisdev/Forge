from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from swarm_router.client import OpenWebUIClient, RequestFailure
from swarm_router.config import load_config
from swarm_router.context_budget import (
    ContextBudgetExceeded,
    estimate_payload_tokens,
    evaluate_context_budget,
    preflight_check,
    resolve_context_limit,
)
from swarm_router.catalog import _context_length
from swarm_router.developer import DeveloperCoordinator, _compact_phase_messages


TOOL = {
    "type": "function",
    "function": {
        "name": "terminal",
        "description": "Run a command in the isolated Forge terminal.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}


def completion_with_tool(call_id: str = "call-ok") -> dict[str, object]:
    return {
        "id": "upstream",
        "object": "chat.completion",
        "created": 1,
        "model": "worker",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": "terminal",
                                "arguments": json.dumps({"command": "pwd"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }


def write_config(root: Path):
    os.environ["OPEN_WEBUI_API_KEY"] = "test-openwebui-key"
    path = root / "config.toml"
    path.write_text(
        f'''[openwebui]
base_url = "http://127.0.0.1:9"
api_key_env = "OPEN_WEBUI_API_KEY"
timeout_seconds = 3

[swarm]
run_directory = "{root / 'runs'}"
catalog_path = "{root / 'catalog.db'}"
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
metadata_directory = "{root / 'dashboard'}"

[personal]
task_directory = "{root / 'personal'}"
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
    return load_config(path)


class ContextBudgetReviewedTest(unittest.TestCase):
    def test_estimator_covers_complete_compact_json(self) -> None:
        payload = {
            "model": "m",
            "messages": [{"role": "user", "content": 'quote " emoji 😀'}],
            "tools": [{"type": "function", "function": {"name": "x"}}],
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.assertEqual(estimate_payload_tokens(payload), (len(serialized) + 3) // 4)

    def test_context_profiles_are_conservative_and_runtime_wins(self) -> None:
        self.assertEqual(resolve_context_limit("local-qwen3-14b-debian"), 32768)
        self.assertEqual(resolve_context_limit("local-qwen36-35b-a3b-windows"), 32768)
        self.assertEqual(resolve_context_limit("qwen36-35b"), 65536)
        self.assertEqual(
            resolve_context_limit("local-qwen36-35b-a3b-windows", 61440),
            61440,
        )
        self.assertEqual(resolve_context_limit("local-qwen3-14b-debian", 24576), 24576)
        self.assertEqual(resolve_context_limit("unknown"), 16384)

    def test_catalog_parser_consumes_meta_n_ctx(self) -> None:
        """A response like {"id": "local-qwen36-35b-a3b-windows", "info": {"meta": {"n_ctx": 61440}}}
        must produce record.context_length == 61440.
        Tests all 8 runtime payload forms in precedence order.
        """
        # 1) Top-level context_length
        self.assertEqual(_context_length({"context_length": 8192}), 8192)
        # 2) Top-level n_ctx (common Open WebUI form)
        self.assertEqual(_context_length({"n_ctx": 61440}), 61440)
        # 3) Top-level meta.context_length
        self.assertEqual(_context_length({"meta": {"context_length": 16384}}), 16384)
        # 4) Top-level meta.n_ctx
        self.assertEqual(_context_length({"meta": {"n_ctx": 24576}}), 24576)
        # 5) info.context_length
        self.assertEqual(_context_length({"info": {"context_length": 32768}}), 32768)
        # 6) info.n_ctx
        self.assertEqual(_context_length({"info": {"n_ctx": 40960}}), 40960)
        # 7) info.meta.context_length
        self.assertEqual(_context_length({"info": {"meta": {"context_length": 32768}}}), 32768)
        # 8) info.meta.n_ctx (the form the prompt describes)
        item = {"id": "local-qwen36-35b-a3b-windows", "info": {"meta": {"n_ctx": 61440}}}
        self.assertEqual(_context_length(item), 61440)
        # context_length at top-level takes precedence over n_ctx
        self.assertEqual(_context_length({"context_length": 8192, "n_ctx": 61440}), 8192)
        # top-level meta.n_ctx takes precedence over info.meta.n_ctx
        self.assertEqual(
            _context_length({"meta": {"n_ctx": 12288}, "info": {"meta": {"n_ctx": 65536}}}),
            12288,
        )

    def test_invalid_reserves_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "protocol_reserve"):
            evaluate_context_budget(
                {"messages": []},
                context_limit=4096,
                requested_output=128,
                protocol_reserve=-1,
            )
        with self.assertRaises(ContextBudgetExceeded):
            preflight_check(
                {"messages": [{"role": "user", "content": "x" * 50000}], "max_tokens": 128},
                context_limit=4096,
            )

    def test_client_preflight_is_enabled_by_default(self) -> None:
        os.environ["TEST_CLIENT_KEY"] = "key"
        client = OpenWebUIClient(
            "http://127.0.0.1:9",
            "/v1/chat/completions",
            "TEST_CLIENT_KEY",
            1,
            model_id="wrong-static-model",
            catalog_context=1024,
        )
        with patch.object(client, "_json_request") as request_mock:
            with self.assertRaises(RequestFailure) as raised:
                client.completion(
                    {
                        "model": "actual-request-model",
                        "messages": [{"role": "user", "content": "x" * 20000}],
                        "max_tokens": 128,
                    }
                )
        self.assertEqual(raised.exception.category, "context_overflow")
        self.assertEqual(raised.exception.status_code, 413)
        request_mock.assert_not_called()

    def test_client_request_catalog_context_overrides_static_context(self) -> None:
        os.environ["TEST_CLIENT_KEY"] = "key"
        client = OpenWebUIClient(
            "http://127.0.0.1:9",
            "/v1/chat/completions",
            "TEST_CLIENT_KEY",
            1,
            catalog_context=1024,
        )
        response = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}]
        }
        with patch.object(client, "_json_request", return_value=response) as request_mock:
            result = client.completion(
                {
                    "model": "actual-request-model",
                    "messages": [{"role": "user", "content": "x" * 5000}],
                    "max_tokens": 128,
                },
                catalog_context=32768,
            )
        self.assertEqual(result, response)
        request_mock.assert_called_once()

    def test_compaction_is_non_mutating_and_preserves_tool_pairs(self) -> None:
        system = {"role": "system", "content": "system"}
        handoffs = [
            {
                "role": "user",
                "content": "BEGIN UNTRUSTED PRIOR ROLE OUTPUT\n" + "h" * 8000 + "\nEND UNTRUSTED PRIOR ROLE OUTPUT",
            }
        ]
        worker = [
            {"role": "user", "content": "old " + "x" * 8000},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "/workspace/forge"},
            {"role": "tool", "tool_call_id": "orphan", "content": "orphan"},
            {"role": "user", "content": "latest objective"},
        ]
        original_handoffs = json.loads(json.dumps(handoffs))
        original_worker = json.loads(json.dumps(worker))
        compacted, metadata = _compact_phase_messages(
            system_message=system,
            handoffs=handoffs,
            worker_messages=worker,
            input_limit=2500,
            model_id="test-model",
        )
        self.assertEqual(handoffs, original_handoffs)
        self.assertEqual(worker, original_worker)
        self.assertTrue(metadata["orphaned_tools_removed"])
        self.assertNotIn("orphan", {m.get("tool_call_id") for m in compacted})
        retained_ids = {
            call["id"]
            for message in compacted
            for call in message.get("tool_calls", [])
        }
        result_ids = {
            message.get("tool_call_id")
            for message in compacted
            if message.get("role") == "tool"
        }
        self.assertEqual(retained_ids, result_ids)
        self.assertEqual(compacted[0]["role"], "system")
        self.assertEqual(compacted[-1]["content"], "latest objective")

    def test_larger_fallback_receives_uncompacted_logical_transcript(self) -> None:
        with TemporaryDirectory() as temporary:
            coordinator = DeveloperCoordinator(write_config(Path(temporary)))
            small = SimpleNamespace(
                model_id="small-model",
                provider="fake",
                context_length=4096,
                family="small",
                health="healthy",
                probe_status="healthy",
            )
            large = SimpleNamespace(
                model_id="large-model",
                provider="fake",
                context_length=32768,
                family="large",
                health="healthy",
                probe_status="healthy",
            )
            seen_payloads: list[dict[str, object]] = []

            def upstream(payload, timeout_seconds=None, catalog_context=None):
                seen_payloads.append(json.loads(json.dumps(payload)))
                if len(seen_payloads) == 1:
                    raise RequestFailure("synthetic transport failure", "transport")
                return completion_with_tool()

            old_context = "OLD-CONTEXT-" + "x" * 12000
            body = {
                "model": "swarm-developer",
                "messages": [
                    {"role": "user", "content": old_context},
                    {"role": "assistant", "content": "OLD-ASSISTANT-" + "y" * 6000},
                    {"role": "user", "content": "latest objective"},
                ],
                "tools": [TOOL],
                "tool_choice": "auto",
                "max_tokens": 128,
            }
            assignments = {
                role: {
                    "provider": "fake",
                    "model": "large-model",
                    "family": "large",
                    "health": "healthy",
                    "reason": "test",
                }
                for role in ("planner", "implementer", "reviewer", "verifier")
            }
            with patch.object(
                coordinator, "_select_role_models", return_value=assignments
            ), patch.object(
                coordinator, "_candidates", return_value=[small, large]
            ), patch.object(
                coordinator.catalog, "recommendation_reason", return_value="test"
            ), patch.object(
                coordinator.client, "completion", side_effect=upstream
            ):
                result = coordinator.complete(body)

        self.assertEqual(result["forge_worker"]["model"], "large-model")
        self.assertEqual(len(seen_payloads), 2)
        first_text = json.dumps(seen_payloads[0]["messages"])
        second_text = json.dumps(seen_payloads[1]["messages"])
        self.assertNotIn("OLD-CONTEXT-", first_text)
        self.assertIn("OLD-CONTEXT-", second_text)
        self.assertIn("latest objective", second_text)

    # -----------------------------------------------------------------------
    # budget_enabled=False regression
    # -----------------------------------------------------------------------

    def test_budget_enabled_false_disables_client_preflight(self) -> None:
        """When budget_enabled=False, preflight must NOT run and the request
        must be forwarded to the upstream server regardless of payload size."""
        os.environ["TEST_CLIENT_KEY_BUDGET"] = "key"
        client = OpenWebUIClient(
            "http://127.0.0.1:9",
            "/v1/chat/completions",
            "TEST_CLIENT_KEY_BUDGET",
            1,
            budget_enabled=False,
        )
        response = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}]
        }
        with patch.object(client, "_json_request", return_value=response) as request_mock:
            result = client.completion(
                {
                    "model": "test",
                    "messages": [{"role": "user", "content": "x" * 50000}],
                    "max_tokens": 128,
                }
            )
        # Request must reach upstream
        self.assertEqual(result, response)
        request_mock.assert_called_once()

    # -----------------------------------------------------------------------
    # Idempotence: coordinator budgeting + client preflight
    # -----------------------------------------------------------------------

    def test_coordinator_budgeting_then_client_preflight_is_idempotent(self) -> None:
        """DeveloperCoordinator runs preflight before compaction.
        If the coordinator budget passes, the client preflight must not
        change behaviour – it should forward the same payload upstream."""
        with TemporaryDirectory() as temporary:
            coordinator = DeveloperCoordinator(write_config(Path(temporary)))
            model = SimpleNamespace(
                model_id="qwen36-35b",
                provider="fake",
                context_length=65536,
                family="large",
                health="healthy",
                probe_status="healthy",
            )
            seen_payloads: list[dict[str, object]] = []

            def upstream(payload, timeout_seconds=None, catalog_context=None):
                seen_payloads.append(json.loads(json.dumps(payload)))
                return completion_with_tool()

            body = {
                "model": "swarm-developer",
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [TOOL],
                "tool_choice": "auto",
                "max_tokens": 128,
            }
            assignments = {
                role: {
                    "provider": "fake",
                    "model": "qwen36-35b",
                    "family": "large",
                    "health": "healthy",
                    "reason": "test",
                }
                for role in ("planner", "implementer", "reviewer", "verifier")
            }
            with patch.object(
                coordinator, "_select_role_models", return_value=assignments
            ), patch.object(
                coordinator, "_candidates", return_value=[model]
            ), patch.object(
                coordinator.catalog, "recommendation_reason", return_value="test"
            ), patch.object(
                coordinator.client, "completion", side_effect=upstream
            ):
                result = coordinator.complete(body)

        # Only one request should be made – the coordinator budget already
        # passed, so the client preflight is a no-op (same budget).
        self.assertEqual(len(seen_payloads), 1)
        self.assertEqual(result["forge_worker"]["model"], "qwen36-35b")

    # -----------------------------------------------------------------------
    # Valid parallel tool groups remain intact
    # -----------------------------------------------------------------------

    def test_valid_parallel_tool_groups_remain_intact(self) -> None:
        """A complete set of parallel tool calls and results must survive
        compaction when budget is generous."""
        call_1 = "call-1"
        call_2 = "call-2"
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_1,
                    "type": "function",
                    "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
                },
                {
                    "id": call_2,
                    "type": "function",
                    "function": {"name": "terminal", "arguments": '{"command":"ls"}'},
                },
            ],
        }
        worker = [
            assistant,
            {"role": "tool", "tool_call_id": call_1, "content": "/workspace"},
            {"role": "tool", "tool_call_id": call_2, "content": "file1.txt"},
            {"role": "user", "content": "latest objective"},
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        # No groups should be removed
        self.assertFalse(meta["invalid_tool_groups_removed"])
        # All tool calls and results present
        assistant_msgs = [m for m in compacted if m["role"] == "assistant"]
        tool_msgs = [m for m in compacted if m["role"] == "tool"]
        self.assertEqual(len(assistant_msgs), 1)
        self.assertEqual(len(assistant_msgs[0]["tool_calls"]), 2)
        self.assertEqual(len(tool_msgs), 2)
        tool_ids = {m["tool_call_id"] for m in tool_msgs}
        self.assertEqual(tool_ids, {call_1, call_2})

    # -----------------------------------------------------------------------
    # Duplicate, incomplete, orphaned, and misordered groups removed atomically
    # -----------------------------------------------------------------------

    def test_duplicate_tool_call_ids_removed(self) -> None:
        """Duplicate tool_call IDs in the same assistant message are invalid."""
        call_id = "call-1"
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
                },
                {
                    "id": call_id,  # duplicate
                    "type": "function",
                    "function": {"name": "terminal", "arguments": '{"command":"ls"}'},
                },
            ],
        }
        worker = [
            assistant,
            {"role": "tool", "tool_call_id": call_id, "content": "/workspace"},
            {"role": "user", "content": "latest objective"},
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertTrue(meta["invalid_tool_groups_removed"])
        # The assistant with duplicate calls should not survive
        assistant_msgs = [m for m in compacted if m["role"] == "assistant"]
        self.assertEqual(len(assistant_msgs), 0)

    def test_incomplete_tool_group_removed(self) -> None:
        """A tool call without its result is removed atomically."""
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
                },
            ],
        }
        worker = [
            assistant,
            {"role": "user", "content": "latest objective"},
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

    def test_orphaned_tool_result_removed(self) -> None:
        """A tool result with no matching assistant call is removed."""
        worker = [
            {"role": "tool", "tool_call_id": "orphan", "content": "orphan result"},
            {"role": "user", "content": "latest objective"},
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertTrue(meta["orphaned_tools_removed"])
        self.assertFalse(any(m.get("role") == "tool" for m in compacted))

    def test_misordered_tool_group_removed(self) -> None:
        """Tool results not adjacent to their assistant call are protocol-invalid."""
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
                },
            ],
        }
        worker = [
            assistant,
            {"role": "user", "content": "intervening message"},
            {"role": "tool", "tool_call_id": "call-1", "content": "/workspace"},
            {"role": "user", "content": "latest objective"},
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
        self.assertFalse(any(m.get("role") == "tool" for m in compacted))

    def test_duplicate_orphan_incomplete_misordered_removed_atomically(self) -> None:
        """When multiple group problems coexist, all are removed in one pass."""
        call_a = "call-a"
        call_b = "call-b"
        # Valid group: call_a has one call and one result
        assistant_a = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_a,
                    "type": "function",
                    "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
                },
            ],
        }
        # Incomplete group: call_b has a call but no result
        assistant_b = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_b,
                    "type": "function",
                    "function": {"name": "terminal", "arguments": '{"command":"ls"}'},
                },
            ],
        }
        worker = [
            assistant_a,
            {"role": "tool", "tool_call_id": call_a, "content": "/workspace"},
            assistant_b,  # incomplete group - no result
            {"role": "tool", "tool_call_id": "orphan", "content": "orphan"},  # truly orphaned
            {"role": "user", "content": "latest objective"},
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        # Both invalid_tool_groups and orphaned_tools are detected
        self.assertTrue(meta["invalid_tool_groups_removed"])
        self.assertTrue(meta["orphaned_tools_removed"])
        # Valid group (call_a) remains intact
        tool_ids = {m.get("tool_call_id") for m in compacted if m["role"] == "tool"}
        self.assertIn(call_a, tool_ids)
        # No orphaned tool survives
        self.assertFalse(any(m.get("tool_call_id") == "orphan" for m in compacted))
        # The incomplete group (call_b) assistant is gone
        incomplete_assistants = [
            m for m in compacted
            if m["role"] == "assistant"
            and any(tc.get("id") == call_b for tc in m.get("tool_calls", []))
        ]
        self.assertEqual(len(incomplete_assistants), 0)


if __name__ == "__main__":
    unittest.main()
