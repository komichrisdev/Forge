"""Direct tests for _normalize_tool_call_id and its usage across all
tool-call ID handling paths in the context-budget subsystem.

Covers the specific defect:
  assistant tool-call ID: "call-1"
  tool-result tool_call_id: " call-1 "
  The valid group was incorrectly removed because assistant IDs were
  stripped while result IDs were compared raw.

Tests:
  - clean assistant ID + whitespace-padded result ID
  - whitespace-padded assistant ID + clean result ID
  - whitespace-padded IDs on both sides
  - normalized output contains identical canonical IDs
  - whitespace-only tool-result ID
  - integer tool-result ID
  - None tool-result ID
  - one malformed group beside one valid group
  - valid parallel groups remain intact after normalization
"""
from __future__ import annotations

import json
import unittest

from swarm_router.developer import (
    DeveloperError,
    _compact_phase_messages,
    _normalize_messages,
    _normalize_tool_call_id,
)


# ======================================================================
# 1. _normalize_tool_call_id unit tests
# ======================================================================


class NormalizeToolCallIdUnitTest(unittest.TestCase):
    """Direct tests for _normalize_tool_call_id() itself."""

    def test_clean_string_returns_stripped(self) -> None:
        self.assertEqual(_normalize_tool_call_id("call-1"), "call-1")

    def test_leading_whitespace_is_stripped(self) -> None:
        self.assertEqual(_normalize_tool_call_id("  call-1"), "call-1")

    def test_trailing_whitespace_is_stripped(self) -> None:
        self.assertEqual(_normalize_tool_call_id("call-1  "), "call-1")

    def test_both_leading_and_trailing_whitespace_are_stripped(self) -> None:
        self.assertEqual(_normalize_tool_call_id("  call-1  "), "call-1")

    def test_tab_newline_whitespace_is_stripped(self) -> None:
        self.assertEqual(_normalize_tool_call_id("\t\ncall-1\n\r"), "call-1")

    def test_none_returns_None(self) -> None:
        self.assertIsNone(_normalize_tool_call_id(None))

    def test_integer_returns_None(self) -> None:
        self.assertIsNone(_normalize_tool_call_id(123))

    def test_list_returns_None(self) -> None:
        self.assertIsNone(_normalize_tool_call_id(["a", "b"]))

    def test_dict_returns_None(self) -> None:
        self.assertIsNone(_normalize_tool_call_id({"key": "value"}))

    def test_empty_string_returns_None(self) -> None:
        self.assertIsNone(_normalize_tool_call_id(""))

    def test_whitespace_only_returns_None(self) -> None:
        self.assertIsNone(_normalize_tool_call_id("   "))
        self.assertIsNone(_normalize_tool_call_id("\t\n\r"))

    def test_boolean_returns_None(self) -> None:
        self.assertIsNone(_normalize_tool_call_id(True))
        self.assertIsNone(_normalize_tool_call_id(False))


# ======================================================================
# 2. Helper functions for test construction
# ======================================================================


def _assistant_with_tool_calls(tool_calls: list[dict]) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": tool_calls,
    }


def _tool_result(tool_call_id: object, content: str = "ok") -> dict:
    """Create a tool message. tool_call_id may be str or other type."""
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


# ======================================================================
# 3. Whitespace normalization: clean + padded result
# ======================================================================


class WhitespaceNormalizationCleanAssistantPaddedResultTest(unittest.TestCase):
    """Defect reproduction: assistant ID "call-1", result ID " call-1 ".

    This was the exact scenario from the independent review:
    - assistant tool-call ID: "call-1"
    - tool-result tool_call_id: " call-1 "
    - the valid group was incorrectly removed because assistant IDs were
      stripped while result IDs were compared raw.
    """

    def test_clean_assistant_padded_result_survives(self) -> None:
        call_id = "call-1"
        assistant = _assistant_with_tool_calls([
            _make_terminal_call(call_id),
        ])
        worker = [
            assistant,
            _tool_result(" call-1 ", "/workspace"),  # whitespace-padded
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertFalse(meta["invalid_tool_groups_removed"])
        # The assistant call survives
        assistant_msgs = [m for m in compacted if m["role"] == "assistant"]
        self.assertEqual(len(assistant_msgs), 1)
        # The tool result survives
        tool_msgs = [m for m in compacted if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        # Their stored IDs are exactly equal after normalization
        self.assertEqual(
            assistant_msgs[0]["tool_calls"][0]["id"],
            tool_msgs[0]["tool_call_id"],
        )


# ======================================================================
# 4. Whitespace normalization: padded assistant + clean result
# ======================================================================


class WhitespaceNormalizationPaddedAssistantCleanResultTest(unittest.TestCase):
    """Whitespace-padded assistant ID + clean result ID."""

    def test_padded_assistant_clean_result_survives(self) -> None:
        call_id = "  call-1  "
        assistant = _assistant_with_tool_calls([
            _make_terminal_call(call_id),
        ])
        worker = [
            assistant,
            _tool_result("call-1", "/workspace"),  # clean
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertFalse(meta["invalid_tool_groups_removed"])
        assistant_msgs = [m for m in compacted if m["role"] == "assistant"]
        self.assertEqual(len(assistant_msgs), 1)
        tool_msgs = [m for m in compacted if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(
            assistant_msgs[0]["tool_calls"][0]["id"],
            tool_msgs[0]["tool_call_id"],
        )


# ======================================================================
# 5. Whitespace normalization: padded on both sides
# ======================================================================


class WhitespaceNormalizationBothSidesPaddedTest(unittest.TestCase):
    """Whitespace-padded IDs on both assistant and result sides."""

    def test_both_sides_padded_survives(self) -> None:
        call_id = "  call-1  "
        assistant = _assistant_with_tool_calls([
            _make_terminal_call(call_id),
        ])
        worker = [
            assistant,
            _tool_result("  call-1  ", "/workspace"),  # also padded
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertFalse(meta["invalid_tool_groups_removed"])
        assistant_msgs = [m for m in compacted if m["role"] == "assistant"]
        self.assertEqual(len(assistant_msgs), 1)
        tool_msgs = [m for m in compacted if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        # Canonical IDs must be identical
        self.assertEqual(
            assistant_msgs[0]["tool_calls"][0]["id"],
            tool_msgs[0]["tool_call_id"],
        )
        self.assertEqual(
            assistant_msgs[0]["tool_calls"][0]["id"],
            "call-1",
        )


# ======================================================================
# 6. Normalized output contains identical canonical IDs
# ======================================================================


class CanonicalIdConsistencyTest(unittest.TestCase):
    """The stored IDs in both assistant calls and tool results must be
    identical after normalization — no raw vs stripped mismatch."""

    def test_canonical_id_in_assistant_and_result_are_equal(self) -> None:
        """After compaction, the IDs stored in messages are identical."""
        assistant = _assistant_with_tool_calls([
            _make_terminal_call("  call-a  "),
        ])
        worker = [
            assistant,
            _tool_result("  call-a  ", "/workspace"),
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        assistant_msgs = [m for m in compacted if m["role"] == "assistant"]
        tool_msgs = [m for m in compacted if m["role"] == "tool"]
        self.assertEqual(len(assistant_msgs), 1)
        self.assertEqual(len(tool_msgs), 1)
        stored_call_id = assistant_msgs[0]["tool_calls"][0]["id"]
        stored_result_id = tool_msgs[0]["tool_call_id"]
        self.assertEqual(stored_call_id, stored_result_id)
        self.assertEqual(stored_call_id, "call-a")

    def test_canonical_id_same_for_all_variations(self) -> None:
        """Different whitespace variations must all yield the same canonical."""
        for raw_id in ["call-x", "  call-x  ", "\tcall-x\n", " call-x "]:
            self.assertEqual(
                _normalize_tool_call_id(raw_id),
                "call-x",
            )


# ======================================================================
# 7. Whitespace-only tool-result ID
# ======================================================================


class WhitespaceOnlyToolResultIdTest(unittest.TestCase):
    """Whitespace-only tool result IDs must be treated as invalid."""

    def test_whitespace_only_result_removed(self) -> None:
        assistant = _assistant_with_tool_calls([
            _make_terminal_call("call-1"),
        ])
        worker = [
            assistant,
            _tool_result("   ", "/workspace"),  # whitespace-only
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        # The tool result with whitespace-only ID must be removed
        tool_msgs = [m for m in compacted if m["role"] == "tool"]
        # If the result is removed, the assistant group is incomplete
        assistant_msgs = [m for m in compacted if m["role"] == "assistant"]
        self.assertTrue(meta["invalid_tool_groups_removed"])
        self.assertEqual(len(assistant_msgs), 0)


# ======================================================================
# 8. Integer tool-result ID
# ======================================================================


class IntegerToolResultIdTest(unittest.TestCase):
    """Integer tool result IDs must remain invalid and not be coerced."""

    def test_integer_result_id_removed(self) -> None:
        assistant = _assistant_with_tool_calls([
            _make_terminal_call("call-1"),
        ])
        worker = [
            assistant,
            _tool_result(42, "/workspace"),  # integer
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        # The tool result with integer ID must be removed
        tool_msgs = [m for m in compacted if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 0)
        # Assistant group is incomplete, so removed too
        assistant_msgs = [m for m in compacted if m["role"] == "assistant"]
        self.assertTrue(meta["invalid_tool_groups_removed"])
        self.assertEqual(len(assistant_msgs), 0)


# ======================================================================
# 9. None tool-result ID
# ======================================================================


class NoneToolResultIdTest(unittest.TestCase):
    """None tool result IDs must remain invalid and not be coerced."""

    def test_none_result_id_removed(self) -> None:
        assistant = _assistant_with_tool_calls([
            _make_terminal_call("call-1"),
        ])
        worker = [
            assistant,
            _tool_result(None, "/workspace"),  # None
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        tool_msgs = [m for m in compacted if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 0)
        assistant_msgs = [m for m in compacted if m["role"] == "assistant"]
        self.assertTrue(meta["invalid_tool_groups_removed"])
        self.assertEqual(len(assistant_msgs), 0)


# ======================================================================
# 10. One malformed group beside one valid group
# ======================================================================


class MalformedAndValidGroupSideBySideTest(unittest.TestCase):
    """A malformed group should not corrupt an unrelated valid group."""

    def test_malalous_group_removed_valid_group_intact(self) -> None:
        bad_assistant = _assistant_with_tool_calls([
            {"id": None, "type": "function", "function": {"name": "terminal", "arguments": '{"command":"pwd"}'}},
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
        self.assertTrue(meta["invalid_tool_groups_removed"])
        # Bad group removed, good group intact
        assistant_msgs = [m for m in compacted if m["role"] == "assistant"]
        self.assertEqual(len(assistant_msgs), 1)
        self.assertEqual(assistant_msgs[0]["tool_calls"][0]["id"], "call-good")
        tool_msgs = [m for m in compacted if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["tool_call_id"], "call-good")

    def test_mixed_valid_and_invalid_call_removes_matching_valid_result(self) -> None:
        mixed_assistant = _assistant_with_tool_calls([
            _make_terminal_call("call-valid"),
            {"id": None, "type": "function", "function": {"name": "terminal", "arguments": '{"command":"pwd"}'}},
        ])
        compacted, metadata = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=[
                mixed_assistant,
                _tool_result("call-valid", "/workspace"),
                _user("latest"),
            ],
            input_limit=9999999,
            model_id="test",
        )
        self.assertTrue(metadata["invalid_tool_groups_removed"])
        self.assertFalse(any(message["role"] == "assistant" for message in compacted))
        self.assertFalse(any(message["role"] == "tool" for message in compacted))

    def test_duplicate_id_across_assistant_messages_removes_every_group_atomically(self) -> None:
        compacted, metadata = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=[
                _assistant_with_tool_calls([_make_terminal_call("call-duplicate")]),
                _tool_result("call-duplicate", "first"),
                _assistant_with_tool_calls([_make_terminal_call(" call-duplicate ")]),
                _tool_result("call-duplicate", "second"),
                _user("latest"),
            ],
            input_limit=9999999,
            model_id="test",
        )
        self.assertTrue(metadata["invalid_tool_groups_removed"])
        self.assertFalse(any(message["role"] == "assistant" for message in compacted))
        self.assertFalse(any(message["role"] == "tool" for message in compacted))

    def test_malformed_group_reserves_id_against_later_reuse(self) -> None:
        compacted, metadata = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=[
                _assistant_with_tool_calls([
                    _make_terminal_call("call-reused"),
                    {"id": None},
                ]),
                _tool_result("call-reused", "first"),
                _assistant_with_tool_calls([_make_terminal_call("call-reused")]),
                _tool_result("call-reused", "second"),
                _user("latest"),
            ],
            input_limit=9999999,
            model_id="test",
        )
        self.assertTrue(metadata["invalid_tool_groups_removed"])
        self.assertFalse(any(message["role"] == "assistant" for message in compacted))
        self.assertFalse(any(message["role"] == "tool" for message in compacted))


# ======================================================================
# 11. Valid parallel groups remain intact after normalization
# ======================================================================


class ValidParallelGroupsAfterNormalizationTest(unittest.TestCase):
    """Valid parallel tool groups with whitespace-padded IDs must survive
    normalization intact."""

    def test_parallel_groups_intact_after_normalization(self) -> None:
        call_1 = "  call-1  "
        call_2 = "  call-2  "
        assistant = _assistant_with_tool_calls([
            _make_terminal_call(call_1, "pwd"),
            _make_terminal_call(call_2, "ls"),
        ])
        worker = [
            assistant,
            _tool_result("  call-1  ", "/workspace"),
            _tool_result("  call-2  ", "file1"),
            _user("latest"),
        ]
        compacted, meta = _compact_phase_messages(
            system_message={"role": "system", "content": "sys"},
            handoffs=[],
            worker_messages=worker,
            input_limit=9999999,
            model_id="test",
        )
        self.assertFalse(meta["invalid_tool_groups_removed"])
        assistant_msgs = [m for m in compacted if m["role"] == "assistant"]
        self.assertEqual(len(assistant_msgs), 1)
        self.assertEqual(len(assistant_msgs[0]["tool_calls"]), 2)
        tool_msgs = [m for m in compacted if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 2)
        # All IDs must be canonical "call-1" / "call-2"
        call_ids = {tc["id"] for tc in assistant_msgs[0]["tool_calls"]}
        self.assertEqual(call_ids, {"call-1", "call-2"})
        tool_ids = {m["tool_call_id"] for m in tool_msgs}
        self.assertEqual(tool_ids, {"call-1", "call-2"})


# ======================================================================
# 12. _normalize_messages normalizes tool_call_id
# ======================================================================


class NormalizeMessagesToolCallIdTest(unittest.TestCase):
    """_normalize_messages() must normalize tool result IDs."""

    def test_normalize_messages_strips_tool_call_id(self) -> None:
        msgs = _normalize_messages([
            {"role": "assistant", "content": "hi"},
            {"role": "tool", "tool_call_id": " call-1 ", "content": "ok"},
        ])
        self.assertEqual(msgs[1]["tool_call_id"], "call-1")

    def test_normalize_messages_rejects_non_string_call_id(self) -> None:
        with self.assertRaises(Exception):
            _normalize_messages([
                {"role": "tool", "tool_call_id": 123, "content": "ok"},
            ])

    def test_normalize_messages_rejects_empty_call_id(self) -> None:
        with self.assertRaises(Exception):
            _normalize_messages([
                {"role": "tool", "tool_call_id": "  ", "content": "ok"},
            ])

    def test_normalize_messages_rejects_malformed_assistant_call(self) -> None:
        with self.assertRaises(DeveloperError):
            _normalize_messages([
                {"role": "assistant", "content": None, "tool_calls": [{"id": None}]},
            ])

    def test_normalize_messages_rejects_duplicate_assistant_ids_globally(self) -> None:
        call = _make_terminal_call("call-duplicate")
        padded = _make_terminal_call(" call-duplicate ")
        with self.assertRaises(DeveloperError):
            _normalize_messages([
                _assistant_with_tool_calls([call]),
                _assistant_with_tool_calls([padded]),
            ])

    def test_normalize_messages_rejects_contentless_empty_tool_calls(self) -> None:
        with self.assertRaises(DeveloperError):
            _normalize_messages([
                {"role": "assistant", "content": None, "tool_calls": []},
            ])

    def test_normalize_messages_rejects_contentless_assistant_without_tool_calls(self) -> None:
        with self.assertRaises(DeveloperError):
            _normalize_messages([{"role": "assistant"}])

    def test_normalize_messages_rejects_empty_user_content(self) -> None:
        with self.assertRaises(DeveloperError):
            _normalize_messages([{"role": "user", "content": []}])


# ======================================================================
# Run
# ======================================================================


if __name__ == "__main__":
    unittest.main()
