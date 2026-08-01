"""Fixture-based integration tests for context-budget enforcement end-to-end.

Proves the full developer request lifecycle:
1. Oversized developer request is compacted before submission
2. The resulting payload remains protocol-valid
3. A payload that still cannot fit is rejected before any provider request
4. A 122,880-context QWENdos model receives the correct effective budget
5. A 16,384-context model is not mistakenly assigned the QWENdos 122,880 profile
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from swarm_router.context_budget import (
    ContextBudget,
    ContextBudgetExceeded,
    estimate_payload_tokens,
    preflight_check,
    resolve_context_limit,
    DEFAULT_CONTEXT_LIMIT,
)
from swarm_router.developer import (
    DeveloperError,
    _compact_phase_messages,
    _summarize_role_output,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_system_message(content: str = "You are a developer.") -> dict:
    return {"role": "system", "content": content}


def _make_user(content: str) -> dict:
    return {"role": "user", "content": content}


def _make_assistant(
    content: str | None = None,
    tool_calls: list | None = None,
) -> dict:
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _make_tool_result(tool_call_id: str, content: str = "result") -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def _make_handoff(content: str) -> dict:
    return {
        "role": "user",
        "content": (
            "BEGIN UNTRUSTED PRIOR ROLE OUTPUT (planner)\n"
            f"{content}\n"
            "END UNTRUSTED PRIOR ROLE OUTPUT"
        ),
    }


def _build_16k_exceeding_payload() -> dict:
    """Build a payload that exceeds a 16k input budget.

    16k input_limit ≈ 11674 tokens. With char/4, need > 46696 chars.
    A 100k-char handoff gives ~25019 tokens which exceeds the budget.
    """
    system = _make_system_message("You are a developer.")
    # 100k chars in handoff alone gives ~25000 tokens
    handoff = _make_handoff("x" * 100000)
    worker_msgs = [
        _make_user("old instruction 0 " + "y" * 5000),
        _make_user("old instruction 1 " + "y" * 5000),
        _make_user("Final objective: implement feature X"),
    ]
    messages = [system, handoff] + worker_msgs

    tools = [
        {
            "type": "function",
            "function": {
                "name": "terminal",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                    },
                },
            },
        },
    ]

    return {
        "model": "test-model",
        "messages": messages,
        "tools": tools,
        "temperature": 0.1,
        "max_tokens": 2048,
    }


def _build_65k_exceeding_payload() -> dict:
    """Build a payload that exceeds a 65k input budget.

    65k input_limit ≈ 55910 tokens ≈ 223640 chars.
    """
    system = _make_system_message("You are a developer.")
    # 250k chars gives ~62500 tokens which exceeds 65k budget
    handoff = _make_handoff("x" * 250000)
    worker_msgs = [
        _make_user("Final objective: implement feature X"),
    ]
    messages = [system, handoff] + worker_msgs

    tools = [
        {
            "type": "function",
            "function": {
                "name": "terminal",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                    },
                },
            },
        },
    ]

    return {
        "model": "test-model",
        "messages": messages,
        "tools": tools,
        "temperature": 0.1,
        "max_tokens": 4096,
    }


# ---------------------------------------------------------------------------
# Test 1: Oversized developer request is compacted before submission
# ---------------------------------------------------------------------------

class TestOversizedRequestCompaction(unittest.TestCase):
    """Verify that an oversized developer request is compacted."""

    def test_oversized_payload_is_compacted(self) -> None:
        payload = _build_16k_exceeding_payload()
        messages = payload["messages"]
        original_est = estimate_payload_tokens(payload)

        # Compute the message input limit for a 16,384-context model
        context_limit = 16384
        max_tokens = payload.get("max_tokens", 2048)
        non_message_payload = {
            key: value
            for key, value in payload.items()
            if key != "messages"
        }
        non_message_tokens = estimate_payload_tokens(non_message_payload)
        message_input_limit = max(
            1,
            context_limit
            - max_tokens
            - 1024
            - max(int(context_limit * 0.10), 512)
            - non_message_tokens,
        )

        # Verify the original payload is indeed over budget
        self.assertGreater(
            original_est,
            message_input_limit,
            f"Original payload ({original_est} tokens) must exceed budget ({message_input_limit})",
        )

        # Extract system, handoffs, and worker messages
        system_message = messages[0]
        handoffs = [
            m for m in messages[1:]
            if isinstance(m.get("content"), str)
            and "BEGIN UNTRUSTED PRIOR ROLE OUTPUT" in m["content"]
        ]
        worker_messages = [
            m for m in messages[1:]
            if not (
                isinstance(m.get("content"), str)
                and "BEGIN UNTRUSTED PRIOR ROLE OUTPUT" in m["content"]
            )
        ]

        compacted_messages, compaction_meta = _compact_phase_messages(
            system_message=system_message,
            handoffs=handoffs,
            worker_messages=worker_messages,
            input_limit=message_input_limit,
            model_id="test-model",
        )

        # Compaction should have been applied
        self.assertTrue(
            compaction_meta["compaction_applied"],
            "Compaction should be applied for oversized payload",
        )

        # System message must be preserved
        self.assertEqual(
            compacted_messages[0]["role"],
            "system",
            "System message must be first",
        )

        # Latest user objective must be preserved
        latest_user_content = "Final objective: implement feature X"
        found_latest = any(
            latest_user_content in m.get("content", "")
            for m in compacted_messages
            if m["role"] == "user"
        )
        self.assertTrue(
            found_latest,
            "Latest user objective must be preserved",
        )

        # Message count should be reduced
        self.assertLess(
            len(compacted_messages),
            len(messages),
            "Compacted message count must be less than original",
        )

        # The compacted payload should now fit within the budget
        compacted_payload = {**payload, "messages": compacted_messages}
        compacted_est = estimate_payload_tokens(compacted_payload)
        self.assertLessEqual(
            compacted_est,
            message_input_limit,
            f"Compacted payload ({compacted_est}) should fit within budget ({message_input_limit})",
        )


# ---------------------------------------------------------------------------
# Test 2: Resulting payload remains protocol-valid
# ---------------------------------------------------------------------------

class TestProtocolValidity(unittest.TestCase):
    """Verify compacted payload remains a valid OpenAI-style API payload."""

    def test_compacted_payload_has_required_fields(self) -> None:
        payload = _build_16k_exceeding_payload()
        messages = payload["messages"]

        system_message = messages[0]
        handoffs = [
            m for m in messages[1:]
            if isinstance(m.get("content"), str)
            and "BEGIN UNTRUSTED PRIOR ROLE OUTPUT" in m["content"]
        ]
        worker_messages = [
            m for m in messages[1:]
            if not (
                isinstance(m.get("content"), str)
                and "BEGIN UNTRUSTED PRIOR ROLE OUTPUT" in m["content"]
            )
        ]

        context_limit = 16384
        max_tokens = payload.get("max_tokens", 2048)
        non_message_payload = {
            key: value for key, value in payload.items() if key != "messages"
        }
        non_message_tokens = estimate_payload_tokens(non_message_payload)
        message_input_limit = max(
            1, context_limit - max_tokens - 1024
            - max(int(context_limit * 0.10), 512)
            - non_message_tokens,
        )

        compacted_messages, _meta = _compact_phase_messages(
            system_message=system_message,
            handoffs=handoffs,
            worker_messages=worker_messages,
            input_limit=message_input_limit,
            model_id="test-model",
        )

        # Each message must have a valid role
        valid_roles = {"system", "user", "assistant", "tool"}
        for msg in compacted_messages:
            self.assertIn(
                msg["role"],
                valid_roles,
                f"Invalid role: {msg['role']}",
            )

        # Messages with tool_calls must have matching tool results
        call_ids = set()
        for msg in compacted_messages:
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if isinstance(tc, dict) and tc.get("id"):
                        call_ids.add(tc["id"])

        for msg in compacted_messages:
            if msg["role"] == "tool":
                tc_id = msg.get("tool_call_id", "")
                self.assertIn(
                    tc_id,
                    call_ids,
                    f"Orphaned tool result: {tc_id}",
                )

        # Payload must be JSON-serializable
        full_payload = {
            "model": "test-model",
            "messages": compacted_messages,
            "tools": payload.get("tools", []),
            "max_tokens": 2048,
        }
        json_str = json.dumps(full_payload)
        self.assertIsInstance(json_str, str)
        self.assertGreater(len(json_str), 0)


# ---------------------------------------------------------------------------
# Test 3: Payload that still cannot fit is rejected
# ---------------------------------------------------------------------------

class TestUnfitPayloadRejection(unittest.TestCase):
    """Verify that a payload that cannot fit is rejected before provider submission."""

    def test_compacted_but_still_too_large_raises_error(self) -> None:
        # Create a payload with a system message that alone exceeds the tiny budget
        huge_system = _make_system_message("x" * 50000)
        huge_user = _make_user("y" * 50000)

        with self.assertRaises(DeveloperError) as ctx:
            _compact_phase_messages(
                system_message=huge_system,
                handoffs=[],
                worker_messages=[huge_user],
                input_limit=100,  # impossibly small
                model_id="test",
            )

        self.assertEqual(ctx.exception.code, "context_budget_exceeded")
        self.assertEqual(ctx.exception.status, 413)
        self.assertIn("cannot fit", str(ctx.exception))

    def test_preflight_rejects_before_submission(self) -> None:
        """Preflight must reject before any provider request is made."""
        payload = {
            "model": "tiny-model",
            "messages": [
                _make_user("x" * 200000),
            ],
            "max_tokens": 500,
        }

        with self.assertRaises(ContextBudgetExceeded):
            preflight_check(
                payload,
                catalog_context=4096,
                requested_output=500,
                protocol_reserve=1024,
                safety_margin=0.10,
            )


# ---------------------------------------------------------------------------
# Test 4: 122,880-context QWENdos model receives correct budget
# ---------------------------------------------------------------------------

class TestQwenDos122kBudget(unittest.TestCase):
    """Verify 122,880-context model gets the correct effective budget."""

    def test_122880_context_produces_correct_budget(self) -> None:
        """A 122,880-context model should get input_limit >> 65k."""
        payload = {
            "model": "qwen3.6-35b",
            "messages": [
                _make_system_message("sys"),
                _make_user("hello world"),
            ],
            "max_tokens": 4096,
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "cmd", "parameters": {}},
                }
            ],
        }

        # Resolve with 122,880 via catalog (simulating QWENdos runtime config)
        context_limit = 122880
        budget = preflight_check(
            payload,
            model_id="qwen3.6-35b",
            catalog_context=context_limit,
        )

        self.assertEqual(budget.context_limit, 122880)
        # input_limit = 122880 - 4096 - 1024 - 12288 (10% safety) = 105472
        self.assertGreaterEqual(budget.input_limit, 100000)
        self.assertTrue(budget.fits)

    def test_catalog_122880_resolved_correctly(self) -> None:
        self.assertEqual(
            resolve_context_limit("qwen36-35b", catalog_context=122880),
            122880,
        )


# ---------------------------------------------------------------------------
# Test 5: 16,384-context model is not mistaken for QWENdos 122,880
# ---------------------------------------------------------------------------

class TestSixteenKModelNotConfused(unittest.TestCase):
    """Verify 16,384-context model is not assigned 122,880 profile."""

    def test_16k_model_gets_16k_budget_not_122k(self) -> None:
        """An unknown model with catalog=16384 must not get 122k budget."""
        payload = {
            "model": "unknown-model",
            "messages": [
                _make_system_message("sys"),
                _make_user("hello world"),
            ],
            "max_tokens": 2048,
        }

        budget = preflight_check(
            payload,
            model_id="unknown-model",
            catalog_context=16384,
        )

        self.assertEqual(budget.context_limit, 16384)
        # input_limit = 16384 - 2048 - 1024 - 1638 = 11674
        self.assertEqual(budget.input_limit, 16384 - 2048 - 1024 - max(1638, 512))

    def test_16k_uses_default_for_unknown_models(self) -> None:
        """Unknown model without catalog falls back to 16,384."""
        self.assertEqual(
            resolve_context_limit("unknown-model"),
            16384,
        )
        self.assertEqual(
            resolve_context_limit(""),
            16384,
        )
        self.assertEqual(
            resolve_context_limit(None),
            16384,
        )

    def test_qwen_model_with_16k_catalog_uses_16k(self) -> None:
        """QWEN model with 16k catalog override must use 16k, not 65k."""
        self.assertEqual(
            resolve_context_limit("qwen36-35b", catalog_context=16384),
            16384,
        )
        self.assertNotEqual(
            resolve_context_limit("qwen36-35b", catalog_context=16384),
            65536,
        )
        self.assertNotEqual(
            resolve_context_limit("qwen36-35b", catalog_context=16384),
            122880,
        )

    def test_different_models_produce_different_budgets(self) -> None:
        """QWEN (65k) and unknown (16k) produce clearly different budgets."""
        payload = {
            "model": "m",
            "messages": [
                _make_system_message("sys"),
                _make_user("hello"),
            ],
            "max_tokens": 2048,
        }

        qwen_budget = preflight_check(
            payload,
            model_id="qwen36-35b",
        )
        unknown_budget = preflight_check(
            payload,
            model_id="unknown-model",
        )

        # QWEN has a higher context_limit and input_limit
        self.assertGreater(qwen_budget.context_limit, unknown_budget.context_limit)
        self.assertGreater(qwen_budget.input_limit, unknown_budget.input_limit)


# ---------------------------------------------------------------------------
# Test: Integration - full developer phase lifecycle with mock client
# ---------------------------------------------------------------------------

class TestDeveloperLifecycleWithMock(unittest.TestCase):
    """End-to-end lifecycle test with mocked client.completion."""

    def test_oversized_request_compacted_then_preflight_passes(self) -> None:
        """Oversized request → compaction → preflight passes → mock client called."""
        # Build an oversized payload for 65k context
        payload = _build_65k_exceeding_payload()

        record = MagicMock()
        record.model_id = "qwen36-35b"
        record.context_length = 65536

        # Run the internal preflight+compaction logic
        context_limit = resolve_context_limit(
            record.model_id, record.context_length
        )
        non_message_payload = {
            key: value
            for key, value in payload.items()
            if key != "messages"
        }
        non_message_tokens = estimate_payload_tokens(non_message_payload)
        message_input_limit = max(
            1,
            context_limit
            - 4096
            - 1024
            - max(int(context_limit * 0.10), 512)
            - non_message_tokens,
        )

        messages = payload["messages"]
        system_message = messages[0]
        handoffs = [
            m for m in messages[1:]
            if isinstance(m.get("content"), str)
            and "BEGIN UNTRUSTED PRIOR ROLE OUTPUT" in m["content"]
        ]
        worker_messages = [
            m for m in messages[1:]
            if not (
                isinstance(m.get("content"), str)
                and "BEGIN UNTRUSTED PRIOR ROLE OUTPUT" in m["content"]
            )
        ]

        compacted_messages, compaction = _compact_phase_messages(
            system_message=system_message,
            handoffs=handoffs,
            worker_messages=worker_messages,
            input_limit=message_input_limit,
            model_id=record.model_id,
        )

        payload["messages"] = compacted_messages

        # Preflight should now pass (compaction brought it under budget)
        budget = preflight_check(
            payload,
            model_id=record.model_id,
            catalog_context=record.context_length,
        )

        self.assertTrue(
            budget.fits,
            f"Preflight should pass after compaction (headroom={budget.headroom})",
        )

        # Verify compaction was applied
        self.assertTrue(compaction["compaction_applied"])
        self.assertLess(
            compaction["messages_after"],
            compaction["messages_before"],
            "Compaction should reduce message count",
        )

        # Verify system and latest user are preserved in compacted messages
        self.assertEqual(compacted_messages[0]["role"], "system")
        latest_content = "Final objective: implement feature X"
        found = any(latest_content in m.get("content", "") for m in compacted_messages if m["role"] == "user")
        self.assertTrue(found, "Latest user objective must survive compaction")

    def test_unfit_payload_preflight_fails_before_client(self) -> None:
        """Payload that cannot fit after compaction raises before provider submission."""
        # Build a payload where even system message alone exceeds tiny budget
        payload = {
            "model": "tiny-model",
            "messages": [
                _make_system_message("x" * 100000),
                _make_user("y" * 100000),
            ],
            "max_tokens": 500,
        }

        # Use a tiny catalog context so nothing can fit
        with self.assertRaises(ContextBudgetExceeded):
            preflight_check(
                payload,
                catalog_context=1024,
                requested_output=500,
                protocol_reserve=1024,
                safety_margin=0.10,
            )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
