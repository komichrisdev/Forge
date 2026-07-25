from __future__ import annotations

from pathlib import Path
import json
import os
import sqlite3
import tempfile
import unittest

from swarm_router.catalog import ModelCatalog
from swarm_router.config import load_config
from swarm_router.orchestrator import _calibrate_judge_result
from swarm_router.prompts import judge_prompt, worker_prompt
from swarm_router.quality import benchmark_by_id, deterministic_checks, load_benchmarks


def config_for(root: Path):
    os.environ["OPEN_WEBUI_API_KEY"] = "test-only"
    path = root / "config.toml"
    path.write_text(
        f'''[openwebui]\nbase_url="http://127.0.0.1:9"\napi_key_env="OPEN_WEBUI_API_KEY"\n[swarm]\nrun_directory="{root / 'runs'}"\ncatalog_path="{root / 'catalog.db'}"\n[dashboard]\nmetadata_directory="{root / 'dashboard'}"\n[authority]\nsupervisor_name="Codex"\n[judge]\nmodel="judge/model"\nsystem="Reject invented details and preserve dissent."\n[[workers]]\nname="planner"\nmodel="a/model"\nmodes=["auto","code"]\nsystem="Separate installed state from proposals."\n[[workers]]\nname="implementer"\nmodel="a/model"\nmodes=["auto","code"]\nsystem="Use narrow exceptions and validate early."\n[[workers]]\nname="critic"\nmodel="b/model"\nmodes=["auto","code"]\nsystem="Detect unsupported architecture and broad catches."\n[[workers]]\nname="verifier"\nmodel="b/model"\nmodes=["auto","code"]\nsystem="Separate fact, inference, proposal, and unknown."\n''',
        encoding="utf-8",
    )
    return load_config(path)


class QualityTest(unittest.TestCase):
    def test_prompts_enforce_context_code_and_role_discipline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = config_for(Path(temp))
            prompt = worker_prompt("Change code", "code", "", "", config.authority)
            for phrase in (
                "Never invent filenames", "unknown and name what Codex must inspect",
                "Do not claim code or commands were run", "Never catch BaseException",
                "Validate inputs before sleeping", "thread/process safety",
                "existing supplied fact", "proposed change",
            ):
                self.assertIn(phrase, prompt)
            systems = {worker.name: worker.system for worker in config.workers}
            self.assertIn("installed state", systems["planner"])
            self.assertIn("narrow exceptions", systems["implementer"])
            self.assertIn("unsupported architecture", systems["critic"])
            self.assertIn("fact, inference, proposal", systems["verifier"])

    def test_judge_prompt_and_calibration_reject_shared_assumptions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = config_for(Path(temp))
            candidates = [
                ("planner", "a", "The observability pipeline proves the service is thread-safe."),
                ("implementer", "b", "Use the observability pipeline; the service is thread-safe."),
            ]
            prompt = judge_prompt("Task", "general", "", candidates, [], config.authority)
            for phrase in (
                "Reject invented repository details", "Preserve a strong, actionable dissent",
                "Arbitrary numbers are", "confidence_reasons",
            ):
                self.assertIn(phrase, prompt)
            result = {
                "answer": "Proposal", "confidence": 0.9, "agreements": [],
                "disagreements": ["Material dissent"], "verification": [],
                "selected_candidates": ["planner"], "stale_or_uncertain_claims": [],
                "confidence_reasons": [],
            }
            calibrated = _calibrate_judge_result(result, candidates, "general", False, 2)
            self.assertEqual(calibrated["confidence"], 0.5)
            self.assertIn("Material dissent", calibrated["disagreements"])
            self.assertTrue(any("shared unsupported assumption" in reason for reason in calibrated["confidence_reasons"]))
            self.assertTrue(any("context" in reason for reason in calibrated["confidence_reasons"]))

    def test_quality_evidence_is_role_specific_sparse_and_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = config_for(root)
            catalog = ModelCatalog(root / "catalog.db")
            catalog.sync([{"id": "a/model"}, {"id": "b/model"}, {"id": "c/disabled"}])
            for model in ("a/model", "b/model", "c/disabled"):
                catalog.record_probe(model, "healthy", 100)
                catalog.update(model, quality=7, capabilities=["chat", "code", "reasoning"])
            catalog.update("c/disabled", enabled=False)
            catalog.record_quality_event("r1", "a/model", "implementer", "code", "over_engineering", 1, note="extra abstraction")
            sparse = catalog.quality_summary("a/model", "implementer")
            self.assertTrue(sparse["quality_provisional"])
            self.assertGreater(sparse["quality_contribution"], -0.5)
            catalog.record_quality_event("r2", "a/model", "implementer", "code", "over_engineering", 2, note="second extra abstraction")
            repeated = catalog.quality_summary("a/model", "implementer")
            self.assertLess(repeated["quality_contribution"], sparse["quality_contribution"])
            catalog.record_quality_event("r3", "a/model", "implementer", "code", "clean_candidate", 0, codex_verified=True, note="minimal clean answer")
            recovered = catalog.quality_summary("a/model", "implementer")
            self.assertGreater(recovered["quality_contribution"], repeated["quality_contribution"])

            good_dims = {"requirement_adherence": 2, "role_usefulness": 2, "minimality": 2}
            bad_dims = {"requirement_adherence": 0, "role_usefulness": 0, "minimality": 0}
            catalog.record_benchmark_result("x", "br1", "b/model", "implementer", "code", "", {}, good_dims, "codex_review")
            catalog.record_benchmark_result("x", "br2", "a/model", "planner", "code", "", {}, bad_dims, "codex_review")
            selected = catalog.recommend("code", 3, config.reliability, "implementer")
            self.assertEqual(selected[0].model_id, "b/model")
            self.assertNotIn("c/disabled", {record.model_id for record in selected})
            reason = catalog.recommendation_reason(selected[0], "code", config.reliability, "implementer")
            self.assertIn("role quality", reason)
            self.assertIn("quality evidence", reason)
            self.assertIn("provisional", reason)

    def test_benchmark_checks_cover_all_five_tasks(self) -> None:
        self.assertEqual(len(load_benchmarks()), 5)
        samples = {
            "retry-helper": "def retry(func, exceptions):\n    if not exceptions: raise ValueError('exceptions')\n    try: return func()\n    except exceptions: raise",
            "missing-go-context": "Repository context is not supplied. Codex must inspect the service entry points and existing middleware; then propose the smallest neutral tracing hook.",
            "installed-state": "Supplied fact: failures are isolated. Proposed change: Codex could record outcome quality; the threshold is unknown and must be verified.",
            "adversarial-review": "BaseException is too broad. Validation occurs after work and must move before it. The safety claim is unsupported. AbstractRetryManager and its plugin registry are unnecessary abstraction.",
            "exact-format": "1. Invented files can misdirect edits.\n2. Invented APIs can break compatibility.\n3. Unchecked claims can conceal missing evidence.",
        }
        for benchmark_id, response in samples.items():
            result = deterministic_checks(benchmark_by_id(benchmark_id), response)
            self.assertTrue(result["passed"], (benchmark_id, result))
        bad = deterministic_checks(
            benchmark_by_id("missing-go-context"),
            "Edit tracing.go and call NewTracingService().",
        )
        self.assertFalse(bad["passed"])

    def test_migration_storage_and_dashboard_payload_exclude_chain_of_thought(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = config_for(root)
            catalog = ModelCatalog(root / "catalog.db")
            catalog.sync([{"id": "a/model"}])
            catalog.record_probe("a/model", "healthy", 100)
            catalog.record_quality_event(
                "r", "a/model", "critic", "code", "caught_peer_error", 0,
                judge_caught=True, codex_verified=True, note="Caught broad catch.",
            )
            catalog.record_benchmark_result(
                "adversarial-review", "r", "a/model", "critic", "code", "response.md",
                {"passed": True}, {"role_usefulness": 2}, "codex_review", "Concise review.",
            )
            payload = catalog.as_dict(catalog.get("a/model"), config.reliability)  # type: ignore[arg-type]
            self.assertIn("quality_by_role", payload)
            self.assertIn("quality_evidence_count", payload)
            self.assertEqual(payload["quality_by_role"]["critic"]["quality_evidence_count"], 1)
            self.assertTrue(payload["quality_by_role"]["critic"]["quality_provisional"])
            self.assertNotIn("chain", json.dumps(payload).lower())
            db = sqlite3.connect(root / "catalog.db")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            db.close()
            self.assertTrue({"quality_events", "benchmark_results"}.issubset(tables))


if __name__ == "__main__":
    unittest.main()
