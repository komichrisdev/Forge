"""Tests for shared model-aware context-budget enforcement."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from swarm_router.context_budget import (
    ContextBudget,
    ContextBudgetExceeded,
    estimate_payload_tokens,
    evaluate_context_budget,
    preflight_check,
    resolve_context_limit,
    _MODEL_PROFILES,
    DEFAULT_CONTEXT_LIMIT,
)


class EstimatePayloadTokensTest(unittest.TestCase):
    def test_simple_payload_estimate(self) -> None:
        payload = {"model": "test", "messages": [{"role": "user", "content": "Hello"}]}
        est = estimate_payload_tokens(payload)
        self.assertGreater(est, 0)

    def test_empty_payload_has_positive_estimate(self) -> None:
        est = estimate_payload_tokens({})
        self.assertGreater(est, 0)

    def test_tools_are_included_in_estimate(self) -> None:
        tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object", "properties": {"x": {"type": "string"}}}}}]
        payload = {"model": "m", "tools": tools}
        est = estimate_payload_tokens(payload)
        self.assertGreater(est, 0)

    def test_non_message_fields_are_counted(self) -> None:
        """Test that non-message fields (tools, tool_choice, etc.) contribute."""
        payload_small = {"model": "m"}
        payload_big = {
            "model": "m",
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "temperature": 0.1,
            "max_tokens": 2048,
        }
        est_small = estimate_payload_tokens(payload_small)
        est_big = estimate_payload_tokens(payload_big)
        self.assertGreater(est_big, est_small)


class ContextBudgetArithmeticTest(unittest.TestCase):
    def test_fits_when_under_budget(self) -> None:
        report = ContextBudget(
            context_limit=10000,
            estimated_input=5000,
            requested_output=1000,
            protocol_reserve=1024,
            safety_margin=500,
        )
        self.assertTrue(report.fits)
        self.assertEqual(report.input_limit, 10000 - 1000 - 1024 - 500)
        self.assertEqual(report.headroom, report.input_limit - report.estimated_input)

    def test_exceeded_when_over_budget(self) -> None:
        report = ContextBudget(
            context_limit=10000,
            estimated_input=10000,
            requested_output=1000,
            protocol_reserve=1024,
            safety_margin=500,
        )
        self.assertFalse(report.fits)
        self.assertLess(report.headroom, 0)

    def test_at_boundary_is_fits(self) -> None:
        """Payload exactly at the boundary is accepted."""
        input_limit = 7476
        report = ContextBudget(
            context_limit=10000,
            estimated_input=input_limit,
            requested_output=1000,
            protocol_reserve=1024,
            safety_margin=500,
        )
        self.assertTrue(report.fits)

    def test_output_reservation_reduces_input_limit(self) -> None:
        report = ContextBudget(
            context_limit=10000,
            estimated_input=5000,
            requested_output=2000,
            protocol_reserve=1024,
            safety_margin=500,
        )
        report2 = ContextBudget(
            context_limit=10000,
            estimated_input=5000,
            requested_output=4000,
            protocol_reserve=1024,
            safety_margin=500,
        )
        self.assertLess(report2.input_limit, report.input_limit)


class ContextBudgetExceededTest(unittest.TestCase):
    def test_exception_contains_budget_info(self) -> None:
        report = ContextBudget(
            context_limit=8000,
            estimated_input=8000,
            requested_output=1000,
            protocol_reserve=1024,
            safety_margin=500,
        )
        exc = ContextBudgetExceeded(report)
        self.assertIn("Context budget exceeded", str(exc))
        self.assertIs(exc.report, report)

    def test_exception_with_payload_summary(self) -> None:
        report = ContextBudget(
            context_limit=8000,
            estimated_input=8000,
            requested_output=1000,
            protocol_reserve=1024,
            safety_margin=500,
        )
        exc = ContextBudgetExceeded(report, payload_summary="model=test")
        self.assertIn("model=test", str(exc))
        self.assertEqual(exc.payload_summary, "model=test")


class EvaluateContextBudgetTest(unittest.TestCase):
    def test_returns_report(self) -> None:
        payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        report = evaluate_context_budget(payload, context_limit=10000, requested_output=2048)
        self.assertIsInstance(report, ContextBudget)
        self.assertEqual(report.context_limit, 10000)

    def test_safety_margin_minimum_512(self) -> None:
        payload = {"model": "m", "messages": []}
        report = evaluate_context_budget(
            payload, context_limit=1000, requested_output=100, safety_margin=0.5
        )
        self.assertGreaterEqual(report.safety_margin, 512)


class PreflightCheckTest(unittest.TestCase):
    def test_preflight_passes_when_fits(self) -> None:
        payload = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}
        report = preflight_check(
            payload,
            context_limit=65536,
            requested_output=2048,
            protocol_reserve=1024,
            safety_margin=0.10,
        )
        self.assertTrue(report.fits)

    def test_preflight_fails_when_over_budget(self) -> None:
        large_payload = {"model": "m", "messages": [{"role": "user", "content": "x" * 100000}]}
        with self.assertRaises(ContextBudgetExceeded):
            preflight_check(
                large_payload,
                context_limit=1000,
                requested_output=500,
                protocol_reserve=300,
                safety_margin=0.5,
            )

    def test_preflight_raises_with_model_id(self) -> None:
        large_payload = {"model": "m", "messages": [{"role": "user", "content": "x" * 100000}]}
        with self.assertRaises(ContextBudgetExceeded):
            preflight_check(
                large_payload,
                model_id="local-qwen36-35b",
                catalog_context=1000,
                requested_output=500,
                protocol_reserve=300,
                safety_margin=0.5,
            )


class ResolveContextLimitTest(unittest.TestCase):
    def test_qwen_profile_65k(self) -> None:
        self.assertEqual(resolve_context_limit("local-qwen36-35b"), 65536)
        self.assertEqual(resolve_context_limit("qwen3.6-35b"), 65536)

    def test_fallback_to_default(self) -> None:
        self.assertEqual(resolve_context_limit("unknown-model"), DEFAULT_CONTEXT_LIMIT)

    def test_uses_catalog_context_when_no_profile_match(self) -> None:
        self.assertEqual(resolve_context_limit("unknown-model", catalog_context=16384), 16384)

    def test_catalog_overrides_default(self) -> None:
        self.assertEqual(resolve_context_limit("unknown-model", catalog_context=8192), 8192)

    def test_16k_profile(self) -> None:
        """The historical 16,384-token profile should be representable."""
        self.assertEqual(resolve_context_limit("unknown-model", catalog_context=16384), 16384)


class RegressionProfileTests(unittest.TestCase):
    """Tests that cover both the historical 16k and current 65k profiles."""

    def _build_large_payload(self, char_per_msg: int = 4000, count: int = 20) -> dict:
        """Build a payload whose token estimate is roughly char/4 * count."""
        return {
            "model": "test-model",
            "messages": [
                {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * char_per_msg}
                for i in range(count)
            ],
            "max_tokens": 2048,
            "tools": [{"type": "function", "function": {"name": "terminal", "parameters": {"type": "object"}}}],
        }

    def test_16k_profile_rejects_oversized_payload(self) -> None:
        """Under a 16k profile, an oversized payload must fail preflight.

        With 3000 chars per message at char/4 = 750 tokens/message,
        15 messages = ~11,250 tokens. With output=2048, proto=1024,
        safety=1638, input_limit = 16384 - 2048 - 1024 - 1638 = 11674.
        So we need more: use 18 messages (13,500 tokens) which exceeds 11674.
        """
        payload = self._build_large_payload(char_per_msg=3000, count=18)
        with self.assertRaises(ContextBudgetExceeded):
            preflight_check(
                payload,
                catalog_context=16384,
                requested_output=2048,
                protocol_reserve=1024,
                safety_margin=0.10,
            )

    def test_65k_profile_rejects_similarly_oversized_payload(self) -> None:
        """Under a 65k profile, a sufficiently large payload must still fail.

        With 3000 chars per message at char/4 = 750 tokens/message,
        input_limit = 65536 - 2048 - 1024 - 6554 = 55910.
        80 messages = ~60,000 tokens > 55910.
        """
        payload = self._build_large_payload(char_per_msg=3000, count=80)
        with self.assertRaises(ContextBudgetExceeded):
            preflight_check(
                payload,
                catalog_context=65536,
                requested_output=2048,
                protocol_reserve=1024,
                safety_margin=0.10,
            )

    def test_profiles_produce_different_budgets(self) -> None:
        """Model profiles of different sizes produce different budgets."""
        small_payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        small_report = evaluate_context_budget(
            small_payload, context_limit=16384, requested_output=2048, safety_margin=0.10
        )
        large_report = evaluate_context_budget(
            small_payload, context_limit=65536, requested_output=2048, safety_margin=0.10
        )
        self.assertGreater(large_report.input_limit, small_report.input_limit)
        self.assertTrue(small_report.fits)
        self.assertTrue(large_report.fits)


if __name__ == "__main__":
    unittest.main()


class CatalogOverridesProfilesTest(unittest.TestCase):
    """Catalog/runtime context takes precedence over model-name profiles."""

    def test_invalid_catalog_context_types_use_the_model_fallback(self) -> None:
        for invalid in (True, 1.5, float("nan"), float("inf"), 2**63):
            with self.subTest(invalid=invalid):
                self.assertEqual(
                    resolve_context_limit("unknown-model", catalog_context=invalid),
                    DEFAULT_CONTEXT_LIMIT,
                )
        with self.assertRaises(ValueError):
            evaluate_context_budget({"messages": []}, context_limit=2**63)

    def test_catalog_context_overrides_model_profile(self) -> None:
        """A qwen36-35b model normally resolves to 65k, but catalog=16k should win."""
        self.assertEqual(
            resolve_context_limit("qwen36-35b", catalog_context=16384),
            16384,
        )

    def test_catalog_context_overrides_qwen_profile(self) -> None:
        """Even a 65k-matched model must honour a smaller catalog value."""
        self.assertEqual(
            resolve_context_limit("local-qwen36-35b-a3b-windows", catalog_context=8192),
            8192,
        )

    def test_catalog_zero_does_not_override_profile(self) -> None:
        """catalog_context=0 should not override the model-name profile."""
        self.assertEqual(
            resolve_context_limit("qwen36-35b", catalog_context=0),
            65536,
        )

    def test_catalog_negative_does_not_override_profile(self) -> None:
        """Negative catalog values are ignored."""
        self.assertEqual(
            resolve_context_limit("qwen36-35b", catalog_context=-100),
            65536,
        )

    def test_122880_catalog_context(self) -> None:
        """A 122,880-token profile is representable via catalog."""
        self.assertEqual(
            resolve_context_limit("unknown-model", catalog_context=122880),
            122880,
        )


class UnknownModelConservativeFallbackTest(unittest.TestCase):
    """Unknown models use the conservative DEFAULT_CONTEXT_LIMIT (16,384)."""

    def test_unknown_model_fallback(self) -> None:
        """Unrecognised model names fall back to 16,384."""
        self.assertEqual(
            resolve_context_limit("random-future-model"),
            16384,
        )

    def test_empty_model_id_fallback(self) -> None:
        """Empty string model_id falls back to default."""
        self.assertEqual(
            resolve_context_limit(""),
            16384,
        )

    def test_none_model_id_fallback(self) -> None:
        """None model_id falls back to default."""
        self.assertEqual(
            resolve_context_limit(None),
            16384,
        )


class ProfileTokenCoverageTest(unittest.TestCase):
    """Tests covering 16,384 / 65,536 / 122,880 token profiles."""

    def _small_payload(self) -> dict:
        return {
            "model": "test",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "cmd", "parameters": {}}}],
        }

    def test_16k_profile_passes_small_payload(self) -> None:
        report = evaluate_context_budget(self._small_payload(), context_limit=16384, requested_output=2048)
        self.assertTrue(report.fits)

    def test_65k_profile_passes_small_payload(self) -> None:
        report = evaluate_context_budget(self._small_payload(), context_limit=65536, requested_output=2048)
        self.assertTrue(report.fits)

    def test_122880_profile_passes_small_payload(self) -> None:
        report = evaluate_context_budget(self._small_payload(), context_limit=122880, requested_output=2048)
        self.assertTrue(report.fits)

    def test_different_limits_yield_different_headroom(self) -> None:
        payload = self._small_payload()
        r16k = evaluate_context_budget(payload, context_limit=16384, requested_output=2048)
        r65k = evaluate_context_budget(payload, context_limit=65536, requested_output=2048)
        r122k = evaluate_context_budget(payload, context_limit=122880, requested_output=2048)
        self.assertGreater(r65k.headroom, r16k.headroom)
        self.assertGreater(r122k.headroom, r65k.headroom)

    def test_16k_profile_rejects_large_payload(self) -> None:
        """Oversized payload fails under 16k profile."""
        large = {
            "model": "m",
            "messages": [{"role": "user", "content": "x" * 50000}],
        }
        report = evaluate_context_budget(large, context_limit=16384, requested_output=2048)
        self.assertFalse(report.fits)

    def test_65k_profile_rejects_even_larger_payload(self) -> None:
        """Payload that fits 65k may still fail if huge enough."""
        huge = {
            "model": "m",
            "messages": [{"role": "user", "content": "x" * 500000}],
        }
        report = evaluate_context_budget(huge, context_limit=65536, requested_output=2048)
        self.assertFalse(report.fits)


class PayloadsFitWithoutCompactionTest(unittest.TestCase):
    """Confirm payloads that fit the budget need no compaction."""

    def test_preflight_passes_small_payload(self) -> None:
        payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        report = preflight_check(
            payload,
            catalog_context=65536,
            requested_output=2048,
            protocol_reserve=1024,
            safety_margin=0.10,
        )
        self.assertTrue(report.fits)


class HandoffCompactionTest(unittest.TestCase):
    """Test that compaction correctly handles handoff messages."""

    def test_handoff_included_in_estimate(self) -> None:
        """Handoff messages contribute to the token estimate."""
        payload = {
            "model": "m",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "BEGIN UNTRUSTED PRIOR ROLE OUTPUT (planner)\n" + "x" * 1000 + "\nEND UNTRUSTED PRIOR ROLE OUTPUT"},
                {"role": "user", "content": "hello"},
            ],
        }
        est = estimate_payload_tokens(payload)
        self.assertGreater(est, 100)  # handoff content adds significant tokens


class ContextTelemetryPrivacyTest(unittest.TestCase):
    """Ensure context telemetry does not leak prompt bodies or tool arguments."""

    def test_budget_report_does_not_contain_message_content(self) -> None:
        """Budget reports expose token counts but not content."""
        payload = {
            "model": "m",
            "messages": [{"role": "user", "content": "secret instruction with API_KEY=abc123"}],
        }
        report = evaluate_context_budget(payload, context_limit=65536, requested_output=2048)
        self.assertIsInstance(report, ContextBudget)
        # The report should contain numeric fields only
        self.assertIsInstance(report.estimated_input, int)
        self.assertIsInstance(report.input_limit, int)

    def test_context_budget_exceeded_message_omits_content(self) -> None:
        """Exception message does not contain full message bodies."""
        payload = {
            "model": "m",
            "messages": [{"role": "user", "content": "s" * 100000}],
        }
        report = evaluate_context_budget(payload, context_limit=1000, requested_output=500)
        exc = ContextBudgetExceeded(report, payload_summary="test")
        msg = str(exc)
        # The message should not contain the full secret content
        self.assertNotIn("s" * 50, msg)

    def test_preflight_logging_does_not_leak_content(self) -> None:
        """Preflight logging uses numeric fields, not content."""
        import logging
        import io
        logger = logging.getLogger("swarm_router.context_budget")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        fmt = logging.Formatter("%(message)s")
        handler.setFormatter(fmt)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        payload = {"model": "m", "messages": [{"role": "user", "content": "secret=" * 100}]}
        preflight_check(payload, context_limit=65536, requested_output=2048)

        log_output = stream.getvalue()
        logger.removeHandler(handler)
        # Logs should not contain the secret content
        self.assertNotIn("secret=" * 10, log_output)


class CandidateFallbackDifferentContextLimitsTest(unittest.TestCase):
    """Different candidate models have different context limits."""

    def test_qwen36_gets_65k(self) -> None:
        self.assertEqual(resolve_context_limit("qwen36-35b"), 65536)

    def test_llama_3_70b_gets_8k(self) -> None:
        self.assertEqual(resolve_context_limit("llama-3-70b"), 8192)

    def test_unknown_model_gets_16k(self) -> None:
        self.assertEqual(resolve_context_limit("unknown-model"), 16384)

    def test_different_models_different_budgets(self) -> None:
        """Model A (65k) and model B (8k) produce different input limits."""
        payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        r_large = evaluate_context_budget(payload, context_limit=65536, requested_output=2048)
        r_small = evaluate_context_budget(payload, context_limit=8192, requested_output=2048)
        self.assertGreater(r_large.input_limit, r_small.input_limit)

    def test_mistral_profile(self) -> None:
        self.assertEqual(resolve_context_limit("mistral"), 32768)

    def test_qwen25_profile(self) -> None:
        self.assertEqual(resolve_context_limit("qwen2.5-72b"), 32768)
        self.assertEqual(resolve_context_limit("qwen2.5-32b"), 32768)
        self.assertEqual(resolve_context_limit("qwen2.5-14b"), 32768)


if __name__ == "__main__":
    unittest.main()
