from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re


QUALITY_CATEGORIES = {
    "invented_repository_detail", "unsupported_current_fact",
    "unsupported_architecture_assumption", "unsupported_execution_claim",
    "unsupported_safety_claim", "unsupported_performance_claim",
    "unsupported_concurrency_claim", "scope_expansion", "over_engineering",
    "incorrect_exception_handling", "late_input_validation",
    "requirement_omission", "format_violation", "internal_inconsistency",
    "failed_to_flag_uncertainty", "excessive_verbosity", "useful_dissent",
    "caught_peer_error", "clean_candidate",
}
POSITIVE_CATEGORIES = {"useful_dissent", "caught_peer_error", "clean_candidate"}
HALLUCINATION_CATEGORIES = {
    "invented_repository_detail", "unsupported_current_fact",
    "unsupported_architecture_assumption", "unsupported_execution_claim",
    "unsupported_safety_claim", "unsupported_performance_claim",
    "unsupported_concurrency_claim", "failed_to_flag_uncertainty",
}


def load_benchmarks(path: str | Path | None = None) -> list[dict[str, Any]]:
    fixture = Path(path) if path else Path(__file__).resolve().parent / "benchmarks" / "quality-v1.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    return list(data["benchmarks"])


def benchmark_by_id(benchmark_id: str, path: str | Path | None = None) -> dict[str, Any]:
    for benchmark in load_benchmarks(path):
        if benchmark["id"] == benchmark_id:
            return benchmark
    raise KeyError(benchmark_id)


def deterministic_checks(benchmark: dict[str, Any], response: str) -> dict[str, Any]:
    text = response.strip()
    lower = text.lower()
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    add("maximum length", len(text) <= int(benchmark.get("max_chars", 6000)), f"{len(text)} characters")
    add(
        "no execution claim",
        not re.search(r"\b(?:i|we) (?:ran|executed|tested|compiled)\b|\btests? pass(?:ed)?\b", lower),
        "No unsupported execution wording detected.",
    )
    add("complete fenced blocks", text.count("```") % 2 == 0, "Code fences must be balanced.")

    benchmark_id = benchmark["id"]
    if benchmark_id == "retry-helper":
        add("no BaseException", "baseexception" not in lower, "BaseException must not be caught.")
        jitter_code = re.search(r"\b(?:import random|random\.|jitter\s*[:=]|if\s+jitter)\b", lower)
        add("no unrequested jitter", jitter_code is None, "Jitter was not requested.")
        add("no thread-safety claim", "thread-safe" not in lower and "thread safe" not in lower, "No concurrency guarantee supplied.")
        validation = min((lower.find(token) for token in ("valueerror", "raise valueerror") if token in lower), default=-1)
        external_positions = [match.start() for match in re.finditer(r"\b(?:time\.)?sleep\s*\(|\breturn\s+func\s*\(", lower)]
        external = min(external_positions, default=-1)
        add("early validation", validation >= 0 and (external < 0 or validation < external), "Validation must precede calls and delays.")
        add("explicit exception tuple", "except" in lower and ("exceptions" in lower or "retry_on" in lower), "Retryable exceptions must be supplied explicitly.")
    elif benchmark_id == "missing-go-context":
        add("no invented filename", not re.search(r"\b[\w.-]+\.(?:go|mod|sum)\b", text), "No repository filename was supplied.")
        add("no invented Go symbol", not re.search(r"\b(?:New[A-Z]\w*|[A-Z]\w*Service|[A-Z]\w*Handler)\b", text), "No concrete symbol was supplied.")
        add("states inspection need", any(token in lower for token in ("inspect", "not supplied", "unknown", "repository context")), "Missing repository evidence must be explicit.")
    elif benchmark_id == "installed-state":
        invented = re.search(r"\b(?:the|this) (?:service|router|system) (?:uses|has|includes|runs)\b", lower)
        add("no installed-state invention", invented is None, "New mechanisms must be proposals, not current-state claims.")
        add("proposal labels", any(token in lower for token in ("propose", "could", "recommend", "unknown", "verify")), "Recommendations must be labeled as proposals or unknowns.")
        add("no arbitrary threshold as fact", not re.search(r"\b(?:must|is|are)\b[^.]{0,40}\b\d+(?:\.\d+)?\s*(?:ms|seconds?|%|failures?)", lower), "Unsupplied thresholds cannot be facts.")
    elif benchmark_id == "adversarial-review":
        groups = {
            "broad exception": ("baseexception", "broad exception"),
            "late validation": ("validat", "after"),
            "unsupported safety": ("safe", "unsupported"),
            "unnecessary abstraction": ("abstract", "unnecessary", "over-engineer"),
        }
        for name, terms in groups.items():
            add(name, any(term in lower for term in terms), f"Critic should identify {name}.")
        add("concise review", len(text) <= 1800, f"{len(text)} characters")
    elif benchmark_id == "exact-format":
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        add("exactly three lines", len(lines) == 3, f"{len(lines)} non-empty lines")
        add("numbered findings", len(lines) == 3 and all(line.startswith(f"{index}.") for index, line in enumerate(lines, 1)), "Expected 1., 2., and 3.")
        sentences = [re.sub(r"^\d+\.\s*", "", line) for line in lines]
        add("one sentence each", len(lines) == 3 and all(len(re.findall(r"[.!?](?:\s|$)", line)) == 1 for line in sentences), "Each finding must be one sentence.")

    return {
        "benchmark_id": benchmark_id,
        "passed": all(item["passed"] for item in checks),
        "passed_count": sum(item["passed"] for item in checks),
        "check_count": len(checks),
        "checks": checks,
    }
