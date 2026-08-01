"""Shared model-aware context-budget enforcement utilities.

The effective input budget is derived from the model context limit, requested
output tokens, protocol/tool overhead, and a safety margin. Runtime/catalog
metadata is authoritative; model-name profiles are conservative fallbacks.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_LIMIT = 16384
DEFAULT_PROTOCOL_RESERVE = 1024
DEFAULT_SAFETY_MARGIN = 0.10
MIN_SAFETY_MARGIN_TOKENS = 512

# Ordered from most specific to broadest. Runtime/catalog metadata takes
# precedence, so these values are only safe fallbacks when metadata is absent.
_MODEL_PROFILES: tuple[tuple[str, int], ...] = (
    ("local-qwen3-14b-debian", 32768),
    # The currently deployed Windows alias has previously reported n_ctx=32768.
    # Larger configured/runtime values must arrive through catalog metadata.
    ("local-qwen36-35b-a3b-windows", 32768),
    ("qwen3-14b", 32768),
    ("qwen3.6-35b", 65536),
    ("qwen36-35b", 65536),
    ("qwen2.5-72b", 32768),
    ("qwen2.5-32b", 32768),
    ("qwen2.5-14b", 32768),
    ("llama-3-70b", 8192),
    ("llama-3-8b", 8192),
    ("mistral", 32768),
    # Conservative generic Qwen3 fallback. Individual deployments may expose
    # larger training contexts but can still be launched with smaller runtimes.
    ("qwen3", 32768),
)


@dataclass(frozen=True)
class ContextBudget:
    """Immutable result of one context-budget evaluation."""

    context_limit: int
    estimated_input: int
    requested_output: int
    protocol_reserve: int
    safety_margin: int

    @property
    def input_limit(self) -> int:
        return (
            self.context_limit
            - self.requested_output
            - self.protocol_reserve
            - self.safety_margin
        )

    @property
    def fits(self) -> bool:
        return self.input_limit >= 0 and self.estimated_input <= self.input_limit

    @property
    def headroom(self) -> int:
        return self.input_limit - self.estimated_input


class ContextBudgetExceeded(ValueError):
    """Raised when a request cannot fit its effective model context budget."""

    def __init__(self, report: ContextBudget, payload_summary: str | None = None) -> None:
        self.report = report
        self.payload_summary = payload_summary
        message = (
            f"Context budget exceeded: estimated_input={report.estimated_input} "
            f"> input_limit={report.input_limit} (headroom={report.headroom})"
        )
        if payload_summary:
            message += f" | payload: {payload_summary}"
        super().__init__(message)


def _coerce_positive_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if not 0 < result <= 2**63 - 1:
        raise ValueError(f"{name} must be a positive integer")
    return result


def _coerce_nonnegative_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if result < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return result


def estimate_payload_tokens(payload: Mapping[str, Any]) -> int:
    """Estimate tokens for the complete serialized request payload.

    The compact UTF-8 JSON representation includes field names, message roles,
    tool schemas, escaping, punctuation, and non-message request fields. The
    byte/4 heuristic is intentionally paired with protocol and safety reserves;
    it is deterministic and does not log or expose payload contents.
    """

    serialized = json.dumps(
        dict(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return max(1, (len(serialized) + 3) // 4)


def resolve_context_limit(model_id: str | None, catalog_context: int | None = None) -> int:
    """Resolve the effective context limit.

    Positive runtime/catalog metadata is authoritative because a model can be
    launched below its training maximum. Invalid or absent metadata falls back
    to a conservative ordered model profile, then ``DEFAULT_CONTEXT_LIMIT``.
    """

    if catalog_context is not None:
        try:
            return _coerce_positive_int(catalog_context, name="catalog_context")
        except ValueError:
            # Treat unusable optional metadata as absent rather than preventing
            # the conservative model-profile fallback.
            pass

    normalized = str(model_id or "").strip().lower()
    for pattern, limit in _MODEL_PROFILES:
        if pattern in normalized:
            return limit
    return DEFAULT_CONTEXT_LIMIT


def evaluate_context_budget(
    payload: Mapping[str, Any],
    *,
    context_limit: int | None = None,
    requested_output: int | None = None,
    protocol_reserve: int = DEFAULT_PROTOCOL_RESERVE,
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
) -> ContextBudget:
    """Evaluate a payload and return an immutable budget report."""

    resolved_context = _coerce_positive_int(
        DEFAULT_CONTEXT_LIMIT if context_limit is None else context_limit,
        name="context_limit",
    )
    if requested_output is None:
        requested_output = payload.get(
            "max_tokens",
            payload.get("max_completion_tokens", 2048),
        )
    resolved_output = _coerce_nonnegative_int(
        requested_output,
        name="requested_output",
    )
    resolved_protocol = _coerce_nonnegative_int(
        protocol_reserve,
        name="protocol_reserve",
    )
    try:
        margin_ratio = float(safety_margin)
    except (TypeError, ValueError) as exc:
        raise ValueError("safety_margin must be between 0 and 1") from exc
    if not 0 <= margin_ratio < 1:
        raise ValueError("safety_margin must be between 0 and 1")

    margin_tokens = max(
        int(resolved_context * margin_ratio),
        MIN_SAFETY_MARGIN_TOKENS,
    )
    report = ContextBudget(
        context_limit=resolved_context,
        estimated_input=estimate_payload_tokens(payload),
        requested_output=resolved_output,
        protocol_reserve=resolved_protocol,
        safety_margin=margin_tokens,
    )
    logger.debug(
        "context budget evaluated: ctx=%d est_in=%d out=%d proto=%d margin=%d "
        "input_limit=%d fits=%s",
        report.context_limit,
        report.estimated_input,
        report.requested_output,
        report.protocol_reserve,
        report.safety_margin,
        report.input_limit,
        report.fits,
    )
    return report


def preflight_check(
    payload: Mapping[str, Any],
    model_id: str | None = None,
    catalog_context: int | None = None,
    context_limit: int | None = None,
    requested_output: int | None = None,
    protocol_reserve: int = DEFAULT_PROTOCOL_RESERVE,
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
) -> ContextBudget:
    """Return a passing budget report or fail closed before submission."""

    resolved_context = (
        resolve_context_limit(model_id, catalog_context)
        if context_limit is None
        else context_limit
    )
    report = evaluate_context_budget(
        payload,
        context_limit=resolved_context,
        requested_output=requested_output,
        protocol_reserve=protocol_reserve,
        safety_margin=safety_margin,
    )
    if report.fits:
        logger.debug(
            "context preflight passed: model=%s ctx=%d est_in=%d input_limit=%d",
            model_id or "default",
            report.context_limit,
            report.estimated_input,
            report.input_limit,
        )
        return report

    logger.warning(
        "context preflight failed: model=%s ctx=%d est_in=%d input_limit=%d "
        "headroom=%d",
        model_id or "default",
        report.context_limit,
        report.estimated_input,
        report.input_limit,
        report.headroom,
    )
    raise ContextBudgetExceeded(report, payload_summary=f"model={model_id or 'default'}")
