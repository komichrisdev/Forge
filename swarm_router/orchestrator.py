from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any
import hashlib
import json
import os
import re

from .catalog import ModelCatalog
from .client import OpenWebUIClient, RequestFailure
from .config import AgentConfig, AppConfig
from .eventlog import RunEventLog
from .prompts import authority_block, judge_prompt, worker_prompt


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "agent"


def _extract_json(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(stripped[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _bounded_context(
    parts: list[tuple[str, str]], limit: int
) -> tuple[str, list[dict[str, Any]]]:
    remaining = limit
    rendered: list[str] = []
    manifest: list[dict[str, Any]] = []
    for label, content in parts:
        header = f"\n===== CONTEXT: {label} =====\n"
        available = max(0, remaining - len(header))
        sent = content[:available]
        truncated = len(sent) < len(content)
        if remaining >= len(header):
            rendered.append(header + sent)
            remaining -= len(header) + len(sent)
        manifest.append(
            {
                "label": label,
                "original_chars": len(content),
                "sent_chars": len(sent),
                "omitted_chars": len(content) - len(sent),
                "truncated": truncated,
                "status": "omitted" if not sent and content else ("truncated" if truncated else "included"),
            }
        )
        if truncated and rendered and not rendered[-1].endswith("[Context truncated by swarm router]\n"):
            rendered.append("\n[Context truncated by swarm router]\n")
    return "".join(rendered).strip(), manifest


def _private_write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def _invalid_judge_result(raw: str, candidates: list[tuple[str, str, str]]) -> dict[str, Any]:
    return {
        "answer": raw.strip(),
        "confidence": 0.2,
        "agreements": [],
        "disagreements": ["Judge output failed the required JSON schema."],
        "verification": ["Supervisor must inspect the raw judge response and worker proposals."],
        "selected_candidates": [name for name, _, _ in candidates],
        "stale_or_uncertain_claims": [],
        "confidence_reasons": ["Judge output failed the required JSON schema."],
    }


def _normalize_judge_result(
    parsed: dict[str, Any] | None, raw: str, candidates: list[tuple[str, str, str]]
) -> dict[str, Any]:
    required = {
        "answer", "confidence", "agreements", "disagreements", "verification",
        "selected_candidates", "stale_or_uncertain_claims",
        "confidence_reasons",
    }
    if not parsed or set(parsed) != required:
        return _invalid_judge_result(raw, candidates)
    confidence = parsed["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        return _invalid_judge_result(raw, candidates)
    if not isinstance(parsed["answer"], str) or not parsed["answer"].strip():
        return _invalid_judge_result(raw, candidates)
    if any(
        not isinstance(parsed[key], list) or not all(isinstance(item, str) for item in parsed[key])
        for key in required - {"answer", "confidence"}
    ):
        return _invalid_judge_result(raw, candidates)
    return parsed


def _format_final(result: dict[str, Any]) -> str:
    answer = str(result.get("answer", "")).strip()
    confidence = result.get("confidence", "unknown")
    selected = result.get("selected_candidates", [])
    sections = [
        ("Agreements", result.get("agreements", [])),
        ("Disagreements", result.get("disagreements", [])),
        ("Stale or uncertain claims", result.get("stale_or_uncertain_claims", [])),
        ("Supervisor verification", result.get("verification", [])),
        ("Confidence reasons", result.get("confidence_reasons", [])),
    ]
    lines = [answer, "", f"Worker-confidence estimate: {confidence}"]
    if selected:
        lines.append("Selected candidates: " + ", ".join(map(str, selected)))
    for title, items in sections:
        if items:
            lines.extend(["", f"{title}:", *[f"- {item}" for item in items]])
    return "\n".join(lines).strip() + "\n"


def _calibrate_judge_result(
    result: dict[str, Any], candidates: list[tuple[str, str, str]], mode: str,
    context_present: bool, worker_total: int,
) -> dict[str, Any]:
    reasons = list(result.get("confidence_reasons", []))
    caps: list[float] = [1.0]

    def reduce(cap: float, reason: str) -> None:
        caps.append(cap)
        if reason not in reasons:
            reasons.append(reason)

    if not context_present:
        reduce(0.7, "No repository or installation context was supplied.")
    if mode == "code":
        reduce(0.65, "Proposed code was not executed or applied by workers.")
    if len(candidates) == 1:
        reduce(0.5, "Only one independent candidate was available.")
    if result.get("disagreements"):
        reduce(0.7, "Material candidate disagreement requires Codex resolution.")
    if len(candidates) < worker_total:
        reduce(max(0.3, len(candidates) / worker_total), "One or more worker candidates were missing.")

    contents = [content.lower() for _, _, content in candidates]
    shared_markers = [
        marker for marker in (
            "baseexception", "thread-safe", "learned router", "observability pipeline",
            "safety registry", "circuit breaker", "jitter",
        ) if sum(marker in content for content in contents) > 1
    ]
    if shared_markers:
        reduce(0.5, "Candidates repeated a shared unsupported assumption: " + ", ".join(shared_markers) + ".")
    if not context_present and any(
        re.search(r"\b[\w.-]+\.(?:py|go|js|ts|rs|java|toml|yaml|yml)\b", content)
        for _, _, content in candidates
    ):
        reduce(0.5, "A candidate named repository files without supplied repository evidence.")
    if any(
        marker in content for content in contents
        for marker in ("production-ready", "safe for concurrent", "is thread-safe", "is thread safe")
    ):
        reduce(0.55, "A candidate made an unsupported safety or concurrency claim.")
    if not context_present and mode != "code" and any(
        re.search(r"\b\d+(?:\.\d+)?\s*(?:ms|milliseconds|seconds?|%|failures?)", content)
        for content in contents
    ):
        reduce(0.6, "A candidate introduced an unsupplied numeric threshold.")

    cap = min(caps)
    result["confidence"] = round(min(float(result.get("confidence", 0.0)), cap), 1)
    result["confidence_cap"] = cap
    result["confidence_reasons"] = reasons
    return result


class SwarmOrchestrator:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.client = OpenWebUIClient(
            base_url=config.openwebui.base_url,
            endpoint=config.openwebui.endpoint,
            api_key_env=config.openwebui.api_key_env,
            timeout_seconds=config.openwebui.timeout_seconds,
        )
        self.catalog = ModelCatalog(config.swarm.catalog_path)

    def _resolve_workers(
        self,
        mode: str,
        requested: list[str] | None,
        role_model_overrides: dict[str, str] | None,
    ) -> list[AgentConfig]:
        workers = [
            worker
            for worker in self.config.workers
            if mode in worker.modes or "auto" in worker.modes
        ]
        known = {worker.name for worker in self.config.workers}
        unknown = (set(requested or []) | set(role_model_overrides or {})) - known
        if unknown:
            raise ValueError("Unknown worker role(s): " + ", ".join(sorted(unknown)))
        if requested:
            wanted = set(requested)
            workers = [worker for worker in workers if worker.name in wanted]
        overrides = role_model_overrides or {}
        workers = [replace(worker, model=overrides.get(worker.name, worker.model)) for worker in workers]
        if not workers:
            raise RuntimeError(f"No configured workers support mode '{mode}'.")
        if len(workers) > self.config.swarm.max_workers:
            raise RuntimeError(
                f"Selected {len(workers)} workers; configured maximum is {self.config.swarm.max_workers}."
            )
        return workers

    def run(
        self,
        objective: str,
        mode: str,
        acceptance: str,
        context_parts: list[tuple[str, str]],
        image_parts: list[dict[str, Any]] | None = None,
        requested_workers: list[str] | None = None,
        role_model_overrides: dict[str, str] | None = None,
        judge_model_override: str | None = None,
        selection_reasons: dict[str, str] | None = None,
        run_id: str | None = None,
    ) -> tuple[str, Path, dict[str, Any]]:
        config = self.config
        workers = self._resolve_workers(mode, requested_workers, role_model_overrides)
        judge = replace(
            config.judge,
            model=judge_model_override or config.judge.model,
        )
        context, manifest = _bounded_context(context_parts, config.swarm.max_context_chars)
        base_prompt = worker_prompt(objective, mode, acceptance, context, config.authority)
        user_content: str | list[dict[str, Any]] = base_prompt
        if image_parts:
            user_content = [{"type": "text", "text": base_prompt}, *image_parts]

        digest = hashlib.sha256((objective + mode + context[:2000]).encode("utf-8")).hexdigest()[:10]
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run_name = run_id or f"{timestamp}-{digest}"
        run_dir = Path(config.swarm.run_directory).expanduser().resolve() / _safe_name(run_name)
        run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        run_dir.parent.chmod(0o700)
        log = RunEventLog(run_dir)
        log.emit("run_created", run_id=run_name, mode=mode, objective_chars=len(objective))

        prompts_dir = run_dir / "prompts"
        context_dir = run_dir / "context"
        prompts_dir.mkdir(mode=0o700)
        context_dir.mkdir(mode=0o700)
        _private_write(context_dir / "sent.txt", context)
        _private_write(
            context_dir / "manifest.json",
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        )
        for item in manifest:
            log.emit("context_chunked", **item)

        task_data = {
            "run_id": run_name,
            "objective": objective,
            "mode": mode,
            "acceptance": acceptance,
            "context_labels": [label for label, _ in context_parts],
            "context_manifest": manifest,
            "context_original_chars": sum(item["original_chars"] for item in manifest),
            "context_sent_chars": sum(item["sent_chars"] for item in manifest),
            "context_omitted_chars": sum(item["omitted_chars"] for item in manifest),
            "authority": asdict(config.authority),
            "timeouts": {
                "worker_seconds": config.swarm.worker_timeout_seconds,
                "judge_seconds": config.swarm.judge_timeout_seconds,
                "probe_seconds": config.probe.timeout_seconds,
            },
            "retry_policy": "No automatic retries; retry_count is always zero.",
            "workers": [
                {**asdict(worker), "selection_reason": (selection_reasons or {}).get(worker.name, "Configured role default.")}
                for worker in workers
            ],
            "judge": {
                **asdict(judge),
                "selection_reason": (selection_reasons or {}).get("__judge__", "Configured judge default."),
            },
        }
        _private_write(run_dir / "task.json", json.dumps(task_data, indent=2, ensure_ascii=False) + "\n")

        candidates: list[tuple[str, str, str]] = []
        errors: list[str] = []
        failures: list[dict[str, Any]] = []

        def call_worker(worker: AgentConfig) -> tuple[str, str, str, int]:
            system_prompt = f"{worker.system.strip()}\n\n{authority_block(config.authority)}"
            worker_prompt_path = prompts_dir / f"worker-{_safe_name(worker.name)}.txt"
            _private_write(worker_prompt_path, f"SYSTEM\n{system_prompt}\n\nUSER\n{base_prompt}\n")
            log.emit(
                "worker_sent", agent=worker.name, model=worker.model,
                timeout_seconds=config.swarm.worker_timeout_seconds, retry_count=0,
            )
            started = monotonic()
            try:
                result = self.client.chat(
                    model=worker.model,
                    system=system_prompt,
                    user=user_content,
                    max_tokens=config.swarm.max_worker_output_tokens,
                    temperature=config.swarm.temperature,
                    timeout_seconds=config.swarm.worker_timeout_seconds,
                )
            except Exception as exc:
                elapsed = int((monotonic() - started) * 1000)
                category = exc.category if isinstance(exc, RequestFailure) else "internal"
                self.catalog.record_task_attempt(
                    run_name, worker.model, worker.name, mode, category, elapsed
                )
                log.emit(
                    "worker_failed",
                    agent=worker.name,
                    model=worker.model,
                    duration_ms=elapsed,
                    timeout_seconds=config.swarm.worker_timeout_seconds,
                    failure_category=category,
                    retry_count=0,
                    error=str(exc)[:2000],
                )
                raise
            elapsed = int((monotonic() - started) * 1000)
            self.catalog.record_task_attempt(
                run_name, worker.model, worker.name, mode, "success", elapsed
            )
            log.emit(
                "worker_returned",
                agent=worker.name,
                model=worker.model,
                duration_ms=elapsed,
                timeout_seconds=config.swarm.worker_timeout_seconds,
                retry_count=0,
                output_chars=len(result.content),
            )
            return worker.name, worker.model, result.content, elapsed

        for worker in workers:
            log.emit("worker_enlisted", agent=worker.name, model=worker.model)
        log.emit("judge_enlisted", agent=judge.name, model=judge.model)

        try:
            with ThreadPoolExecutor(
                max_workers=min(config.swarm.max_parallel_workers, len(workers))
            ) as executor:
                future_map = {executor.submit(call_worker, worker): worker for worker in workers}
                for future in as_completed(future_map):
                    worker = future_map[future]
                    try:
                        name, model, content, _elapsed = future.result()
                        candidates.append((name, model, content))
                    except Exception as exc:
                        errors.append(f"{worker.name} ({worker.model}): {exc}")
                        failures.append(
                            {
                                "role": worker.name,
                                "model": worker.model,
                                "category": exc.category if isinstance(exc, RequestFailure) else "internal",
                                "missing": True,
                                "retry_count": 0,
                            }
                        )

            candidates.sort(key=lambda item: item[0])
            workers_dir = run_dir / "workers"
            workers_dir.mkdir(mode=0o700)
            for name, model, content in candidates:
                _private_write(
                    workers_dir / f"{_safe_name(name)}.md",
                    f"# {name}\n\nModel: `{model}`\n\n{content}\n",
                )
            if errors:
                _private_write(workers_dir / "_errors.txt", "\n".join(errors) + "\n")
                _private_write(
                    workers_dir / "_failures.json",
                    json.dumps(failures, indent=2, ensure_ascii=False) + "\n",
                )

            if not candidates:
                categories = ", ".join(sorted({str(item["category"]) for item in failures}))
                raise RuntimeError(
                    f"Every worker failed within bounded request limits ({categories or 'unknown'}). "
                    "Use an explicit known-working model or retry later; no second swarm was launched."
                )

            integration_prompt = judge_prompt(
                objective=objective,
                mode=mode,
                acceptance=acceptance,
                candidates=candidates,
                failures=failures,
                authority=config.authority,
            )
            judge_system_prompt = f"{judge.system.strip()}\n\n{authority_block(config.authority)}"
            _private_write(
                prompts_dir / "judge.txt",
                f"SYSTEM\n{judge_system_prompt}\n\nUSER\n{integration_prompt}\n",
            )
            judge_dir = run_dir / "judge"
            judge_dir.mkdir(mode=0o700)
            log.emit(
                "judge_sent", agent=judge.name, model=judge.model,
                candidate_count=len(candidates), missing_candidate_count=len(failures),
                timeout_seconds=config.swarm.judge_timeout_seconds, retry_count=0,
            )
            judge_started = monotonic()
            try:
                judge_result = self.client.chat(
                    model=judge.model,
                    system=judge_system_prompt,
                    user=([{"type": "text", "text": integration_prompt}, *image_parts] if image_parts else integration_prompt),
                    max_tokens=config.swarm.max_judge_output_tokens,
                    temperature=0.0,
                    timeout_seconds=config.swarm.judge_timeout_seconds,
                )
            except Exception as exc:
                elapsed = int((monotonic() - judge_started) * 1000)
                category = exc.category if isinstance(exc, RequestFailure) else "internal"
                self.catalog.record_task_attempt(
                    run_name, judge.model, "__judge__", mode, category, elapsed
                )
                log.emit(
                    "judge_failed", agent=judge.name, model=judge.model,
                    duration_ms=elapsed, timeout_seconds=config.swarm.judge_timeout_seconds,
                    failure_category=category, retry_count=0, error=str(exc)[:2000],
                )
                _private_write(
                    judge_dir / "failure.json",
                    json.dumps({"category": category, "missing": True, "retry_count": 0}, indent=2) + "\n",
                )
                raise
            judge_elapsed = int((monotonic() - judge_started) * 1000)
            self.catalog.record_task_attempt(
                run_name, judge.model, "__judge__", mode, "success", judge_elapsed
            )
            log.emit(
                "judge_returned",
                agent=judge.name,
                model=judge.model,
                duration_ms=judge_elapsed,
                timeout_seconds=config.swarm.judge_timeout_seconds,
                retry_count=0,
                output_chars=len(judge_result.content),
            )

            _private_write(
                judge_dir / "response.md",
                f"# {judge.name}\n\nModel: `{judge.model}`\n\n{judge_result.content}\n",
            )
            parsed = _normalize_judge_result(
                _extract_json(judge_result.content), judge_result.content, candidates
            )
            if errors:
                parsed.setdefault("verification", [])
                parsed["verification"].append(
                    f"{len(errors)} worker(s) failed or timed out; independently verify conclusions with reduced support."
                )
                parsed.setdefault("disagreements", [])
                parsed["disagreements"].append(
                    "Missing workers are not evidence of agreement."
                )
                confidence_cap = max(0.30, len(candidates) / len(workers))
                parsed["confidence"] = min(float(parsed.get("confidence", 0.0)), confidence_cap)
                parsed["partial_success"] = {
                    "worker_successes": len(candidates),
                    "worker_failures": len(failures),
                    "confidence_cap": confidence_cap,
                }

            parsed = _calibrate_judge_result(
                parsed, candidates, mode, bool(context.strip()), len(workers)
            )

            final_text = _format_final(parsed)
            _private_write(run_dir / "final.md", final_text)
            _private_write(run_dir / "final.json", json.dumps(parsed, indent=2, ensure_ascii=False) + "\n")
            log.emit("run_complete", run_id=run_name, worker_successes=len(candidates), worker_failures=len(errors))
        except Exception as exc:
            log.emit("run_failed", run_id=run_name, error=str(exc)[:2000])
            _private_write(run_dir / "failure.txt", str(exc) + "\n")
            raise

        bounded = final_text[: config.swarm.return_char_limit]
        if len(final_text) > len(bounded):
            bounded += (
                "\n[Output truncated on stdout. Read final.md only; avoid loading "
                "raw worker transcripts unless verification fails.]\n"
            )
        return bounded, run_dir, parsed
