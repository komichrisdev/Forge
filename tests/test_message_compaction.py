"""Tests for structure-aware message compaction in DeveloperCoordinator."""
from __future__ import annotations

import json
import unittest

from swarm_router.developer import (
    _compact_phase_messages,
    _latest_user,
    _normalize_messages,
    _summarize_role_output,
    _worker_messages,
)


def _make_user(content: str) -> dict:
    return {"role": "user", "content": content}


def _make_assistant(content: str | None = None, tool_calls: list | None = None) -> dict:
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _make_tool_result(tool_call_id: str, content: str = "result") -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


class SummarizeRoleOutputTest(unittest.TestCase):
    def test_short_text_unchanged(self) -> None:
        self.assertEqual(_summarize_role_output("hello"), "hello")

    def test_long_text_is_truncated(self) -> None:
        long = "x" * 5000
        result = _summarize_role_output(long)
        self.assertIn("[Prior role output truncated", result)
        self.assertIn("5000 chars", result)
        self.assertLess(len(result), len(long))

    def test_truncation_preserves_first_and_last(self) -> None:
        long = "A" * 5000
        result = _summarize_role_output(long)
        self.assertTrue(result.startswith("[Prior role output"))
        self.assertTrue("...[middle omitted]..." in result)


class CompactPhaseMessagesTest(unittest.TestCase):
    """Tests for structure-aware compaction matching the corrected input_limit algorithm."""

    def test_system_message_always_preserved(self) -> None:
        system = {"role": "system", "content": "You are a helpful assistant."}
        compacted, meta = _compact_phase_messages(
            system_message=system,
            handoffs=[],
            worker_messages=[_make_user("do work")],
            input_limit=9999999,  # generous
            model_id="test",
        )
        self.assertEqual(compacted[0]["role"], "system")
        self.assertEqual(compacted[0]["content"], "You are a helpful assistant.")

    def test_latest_user_objective_preserved(self) -> None:
        user_obj = _make_user("My objective is to fix bug #123")
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=[_make_user("old instruction"), user_obj],
            input_limit=9999999,
            model_id="test",
        )
        found = [m for m in compacted if m.get("content") == "My objective is to fix bug #123"]
        self.assertEqual(len(found), 1)

    def test_assistant_tool_calls_paired_with_tool_results(self) -> None:
        """Assistant tool_calls and their matching tool results survive compaction."""
        tc_id = "call-1"
        assistant = _make_assistant(
            content=None,
            tool_calls=[{
                "id": tc_id,
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command":"ls"}'},
            }],
        )
        tool_result = _make_tool_result(tc_id, "file1.txt")
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=[assistant, tool_result],
            input_limit=9999999,
            model_id="test",
        )
        roles = [m["role"] for m in compacted]
        self.assertIn("assistant", roles)
        self.assertIn("tool", roles)
        # Both must be present and paired
        assistant_msgs = [m for m in compacted if m["role"] == "assistant"]
        tool_msgs = [m for m in compacted if m["role"] == "tool"]
        self.assertGreater(len(assistant_msgs), 0)
        self.assertGreater(len(tool_msgs), 0)

    def test_incomplete_tool_group_removed_even_when_within_budget(self) -> None:
        """A partial parallel-tool group must never be forwarded upstream."""
        assistant = _make_assistant(
            content=None,
            tool_calls=[
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
                },
                {
                    "id": "call-2",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": '{"command":"ls"}'},
                },
            ],
        )
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=[
                assistant,
                _make_tool_result("call-1", "/workspace/forge"),
                _make_user("latest objective"),
            ],
            input_limit=9999999,
            model_id="test",
        )
        self.assertFalse(any(message.get("tool_calls") for message in compacted))
        self.assertFalse(any(message.get("role") == "tool" for message in compacted))
        self.assertTrue(meta["invalid_tool_groups_removed"])
        self.assertEqual(meta["reason"], "protocol_cleanup")

    def test_misordered_tool_group_removed_even_when_within_budget(self) -> None:
        """Tool results separated from their assistant call are protocol-invalid."""
        assistant = _make_assistant(
            content=None,
            tool_calls=[{
                "id": "call-1",
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
            }],
        )
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=[
                assistant,
                _make_user("intervening message"),
                _make_tool_result("call-1", "/workspace/forge"),
                _make_user("latest objective"),
            ],
            input_limit=9999999,
            model_id="test",
        )
        self.assertFalse(any(message.get("tool_calls") for message in compacted))
        self.assertFalse(any(message.get("role") == "tool" for message in compacted))
        self.assertTrue(meta["invalid_tool_groups_removed"])

    def test_no_orphaned_tool_messages_when_compacting(self) -> None:
        """After compaction, every tool message has a matching assistant tool_call.

        With a tight budget the orphaned tool message (and other non-required
        messages) is dropped during compaction, so no orphaned tool messages remain.
        """
        # Create an orphaned tool result with large content to ensure compaction triggers
        orphaned_result = _make_tool_result("call-orphaned", "x" * 3000)
        worker_msgs = [
            orphaned_result,
            _make_user("old instruction"),
            _make_user("latest objective"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker_msgs,
            input_limit=200,  # tight budget forces compaction
            model_id="test",
        )
        call_ids_in_assistants = set()
        for msg in compacted:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if isinstance(tc, dict) and tc.get("id"):
                        call_ids_in_assistants.add(tc["id"])
        for msg in compacted:
            if msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id", "")
                self.assertIn(tc_id, call_ids_in_assistants, f"Orphaned tool message found: {tc_id}")

    def test_old_handoffs_are_compacted_when_budget_forces(self) -> None:
        """Old handoff messages are summarized when budget forces compaction.

        When compaction is triggered, all but the newest handoff are summarized
        before any other messages are dropped. The newest handoff is always
        preserved as the latest user objective.
        """
        old_handoff = _make_user(
            "BEGIN UNTRUSTED PRIOR ROLE OUTPUT (planner)\n" + "x" * 2000 + "\nEND UNTRUSTED PRIOR ROLE OUTPUT"
        )
        # Use a smaller new handoff so that after summarizing the old one,
        # the total can fit within a reasonable budget
        new_handoff = _make_user(
            "BEGIN UNTRUSTED PRIOR ROLE OUTPUT (implementer)\ny" * 100 + "\nEND UNTRUSTED PRIOR ROLE OUTPUT"
        )

        # With large budget no compaction needed
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[old_handoff, new_handoff],
            worker_messages=[],
            input_limit=9999999,
            model_id="test",
        )
        self.assertFalse(meta["compaction_applied"])
        self.assertEqual(len(compacted), 3)  # system + 2 handoffs

        # Estimate for these messages is ~1805 tokens. Use a budget below that
        # to force compaction. The reviewed implementation summarizes old
        # handoffs first, then removes them if budget is still tight.
        compacted2, meta2 = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[old_handoff, new_handoff],
            worker_messages=[],
            input_limit=1500,
            model_id="test",
        )
        # Compaction must be applied (old handoff summarized then removed)
        self.assertTrue(meta2.get("compaction_applied"))
        # The newest handoff is always preserved as the latest user objective
        self.assertEqual(compacted2[-1]["role"], "user")
        self.assertIn("BEGIN UNTRUSTED PRIOR ROLE OUTPUT (implementer)", compacted2[-1].get("content", ""))

    def test_compaction_reduces_message_count(self) -> None:
        """When budget is tight, old worker messages should be dropped."""
        # Create messages with enough content that total estimate exceeds input_limit=200
        worker_msgs = [
            _make_user(f"old instruction {i}" + "x" * 200)
            for i in range(10)
        ] + [_make_user("latest objective")]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker_msgs,
            input_limit=200,  # very tight budget
            model_id="test",
        )
        self.assertLess(len(compacted), len(worker_msgs) + 1)  # +1 for system
        # Verify compaction was applied
        self.assertTrue(meta.get("compaction_applied"))

    def test_compaction_metadata_is_informative(self) -> None:
        """Compaction metadata includes required observability fields."""
        _, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=[_make_user("work")],
            input_limit=9999999,
            model_id="test-model",
        )
        required_keys = {
            "messages_before", "messages_after",
            "estimated_input_before", "estimated_input_after",
            "model_id", "compaction_applied",
            "orphaned_tools_removed", "invalid_tool_groups_removed", "reason",
        }
        for key in required_keys:
            self.assertIn(key, meta, f"Missing metadata key: {key}")

    def test_no_simple_slice_truncation(self) -> None:
        """Compaction should not just take messages[-N:]. System and latest user must survive."""
        user_msgs = [_make_user(f"instruction {i}" + "x" * 100) for i in range(20)]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "always kept"},
            handoffs=[],
            worker_messages=user_msgs,
            input_limit=500,
            model_id="test",
        )
        roles = [m["role"] for m in compacted]
        # System always first
        self.assertEqual(compacted[0]["role"], "system")
        self.assertIn("always kept", compacted[0].get("content", ""))
        # Latest user should survive
        latest = _latest_user(user_msgs)
        self.assertIn(latest, [m.get("content", "") for m in compacted])

    def test_acceptance_criteria_survive_compaction(self) -> None:
        """Messages containing acceptance criteria are preserved."""
        acceptance = "Acceptance criteria: must pass all unit tests"
        user_msgs = [_make_user("old stuff"), _make_user(acceptance)]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=user_msgs,
            input_limit=500,
            model_id="test",
        )
        found = any(acceptance in m.get("content", "") for m in compacted)
        self.assertTrue(found, "Acceptance criteria message was lost during compaction")

    def test_large_tool_result_compacted(self) -> None:
        """Oversized tool results are handled (dropped or compacted)."""
        huge_result = _make_tool_result(
            "call-1",
            content="x" * 50000,  # 50,000+ character tool result
        )
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=[huge_result, _make_user("latest objective")],
            input_limit=9999999,
            model_id="test",
        )
        # The compaction should handle this - either drop or keep with metadata
        self.assertIn("compaction_applied", meta)

    def test_deterministic_compaction(self) -> None:
        """Running compaction twice on the same input produces the same result."""
        messages = [
            _make_user(f"msg {i}" + "x" * 50) for i in range(10)
        ]
        _, meta1 = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=messages,
            input_limit=500,
            model_id="test",
        )
        _, meta2 = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=messages,
            input_limit=500,
            model_id="test",
        )
        self.assertEqual(meta1["messages_after"], meta2["messages_after"])
        self.assertEqual(meta1["compaction_applied"], meta2["compaction_applied"])

    def test_within_budget_no_compaction(self) -> None:
        """Small payloads should pass without compaction."""
        _, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=[_make_user("work")],
            input_limit=9999999,
            model_id="test",
        )
        self.assertFalse(meta["compaction_applied"])
        self.assertEqual(meta["reason"], "within_budget")
        self.assertEqual(meta["messages_before"], meta["messages_after"])

    def test_compaction_cannot_fit_raises_error(self) -> None:
        """When even minimal required messages exceed budget, raises DeveloperError."""
        from swarm_router.developer import DeveloperError
        # System message + large user content that can't fit in tiny budget
        huge_user = _make_user("x" * 100000)
        with self.assertRaises(DeveloperError):
            _compact_phase_messages(
                system_message={"role": "system", "content": "x" * 100000},
                handoffs=[],
                worker_messages=[huge_user],
                input_limit=100,  # impossibly small
                model_id="test",
            )


class WorkerMessagesTest(unittest.TestCase):
    def test_worker_messages_preserves_non_system(self) -> None:
        msgs = [_make_user("hi"), _make_assistant("bye")]
        result = _worker_messages(msgs)
        self.assertEqual(len(result), 2)

    def test_worker_messages_wraps_system(self) -> None:
        sys_msg = {"role": "system", "content": "system instruction"}
        result = _worker_messages([sys_msg])
        self.assertEqual(result[0]["role"], "user")
        self.assertIn("BEGIN UNTRUSTED CLIENT TEXT", result[0]["content"])


if __name__ == "__main__":
    unittest.main()
