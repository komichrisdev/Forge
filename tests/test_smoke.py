from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from dataclasses import replace
from urllib import error
from io import BytesIO
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from swarm_router.catalog import ModelCatalog
from swarm_router.client import ChatResult, OpenWebUIClient, RequestFailure
from swarm_router.config import load_config
from swarm_router.dashboard import DashboardApp
from swarm_router.orchestrator import SwarmOrchestrator, _bounded_context


class FakeOpenWebUI(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/health":
            data = {"status": "ok"}
        elif self.path == "/api/models":
            data = {
                "data": [
                    {"id": "vendor/reasoner"},
                    {"id": "vendor/coder-instruct"},
                    {"id": "other/critic-instruct"},
                    {"id": "third/verifier-instruct"},
                    {"id": "vendor/bad-instruct"},
                    {"id": "vendor/embed"},
                ]
            }
        else:
            self.send_response(404)
            self.end_headers()
            return
        self._send(data)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        if "integration clerk" in system and body["model"] == "vendor/invalid-judge":
            content = json.dumps({"answer": "invalid partial result", "confidence": 99})
        elif "integration clerk" in system:
            content = json.dumps(
                {
                    "answer": "Integrated proposal",
                    "confidence": 0.8,
                    "agreements": ["A"],
                    "disagreements": [],
                    "verification": ["Run tests"],
                    "selected_candidates": ["planner", "critic"],
                    "stale_or_uncertain_claims": ["Current data not supplied"],
                    "confidence_reasons": ["Candidates require supervisor verification"],
                }
            )
        elif "HEALTHY" in user:
            content = "HEALTHY"
        else:
            content = f"Candidate from {body['model']}"
        self._send({"choices": [{"message": {"content": content}}]})

    def _send(self, data: object) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_config(root: Path):
    os.environ["OPEN_WEBUI_API_KEY"] = "test-only"
    path = root / "config.toml"
    path.write_text(
        f'''[openwebui]\nbase_url="http://127.0.0.1:9"\napi_key_env="OPEN_WEBUI_API_KEY"\ntimeout_seconds=9\n[swarm]\nrun_directory="{root / 'runs'}"\ncatalog_path="{root / 'catalog.db'}"\nmax_workers=4\nmax_parallel_workers=3\nworker_timeout_seconds=1\njudge_timeout_seconds=2\n[probe]\ntimeout_seconds=3\nmax_parallel=2\n[reliability]\nrecent_attempt_window=8\ncooldown_after_consecutive_failures=3\ncooldown_minutes=60\n[dashboard]\nmetadata_directory="{root / 'dashboard'}"\n[authority]\nsupervisor_name="Codex"\n[judge]\nname="integrator"\nmodel="judge/model"\nsystem="integration clerk"\n[[workers]]\nname="planner"\nmodel="worker/planner"\nmodes=["auto","code"]\nsystem="planner"\n[[workers]]\nname="implementer"\nmodel="worker/implementer"\nmodes=["auto","code"]\nsystem="implementer"\n[[workers]]\nname="critic"\nmodel="worker/critic"\nmodes=["auto","code"]\nsystem="critic"\n[[workers]]\nname="verifier"\nmodel="worker/verifier"\nmodes=["auto","code"]\nsystem="verifier"\n''',
        encoding="utf-8",
    )
    return load_config(path)


class ScriptedClient:
    def __init__(self, worker_failures: dict[str, str] | None = None, judge_failure: str = "") -> None:
        self.worker_failures = worker_failures or {}
        self.judge_failure = judge_failure
        self.calls: list[dict[str, object]] = []

    def chat(self, model: str, system: str, user: str, max_tokens: int,
             temperature: float, timeout_seconds: int | None = None) -> ChatResult:
        is_judge = "integration clerk" in system
        self.calls.append({"model": model, "judge": is_judge, "timeout": timeout_seconds, "user": user})
        category = self.judge_failure if is_judge else self.worker_failures.get(model, "")
        if category:
            raise RequestFailure(f"controlled {category}", category)
        content = json.dumps({
            "answer": "Integrated proposal", "confidence": 0.9,
            "agreements": ["supported"], "disagreements": [],
            "verification": ["Verify independently"],
            "selected_candidates": ["planner"],
            "stale_or_uncertain_claims": [],
            "confidence_reasons": ["Scripted candidate evidence"],
        }) if is_judge else f"Candidate from {model}"
        return ChatResult(model, content, {})


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return b"not-json test-only"


class SwarmSmokeTest(unittest.TestCase):
    def test_model_override_dashboard_and_authority(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenWebUI)
        Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                config_path = root / "config.toml"
                config_path.write_text(
                    f'''[openwebui]\nbase_url="http://127.0.0.1:{server.server_port}"\napi_key_env="OPEN_WEBUI_API_KEY"\n[swarm]\nrun_directory="{root / 'runs'}"\ncatalog_path="{root / 'catalog.db'}"\nmax_context_chars=1000\nmax_workers=4\n[dashboard]\nmetadata_directory="{root / 'dashboard'}"\n[authority]\nsupervisor_name="Codex"\n[judge]\nname="integrator"\nmodel="vendor/reasoner"\nsystem="Act as subordinate integration clerk"\n[[workers]]\nname="planner"\nmodel="vendor/reasoner"\nmodes=["auto","code"]\nsystem="planner"\n[[workers]]\nname="implementer"\nmodel="vendor/reasoner"\nmodes=["auto","code"]\nsystem="implementer"\n[[workers]]\nname="critic"\nmodel="vendor/reasoner"\nmodes=["auto","code"]\nsystem="critic"\n[[workers]]\nname="verifier"\nmodel="vendor/reasoner"\nmodes=["auto","code"]\nsystem="verifier"\n''',
                    encoding="utf-8",
                )
                os.environ["OPEN_WEBUI_API_KEY"] = "test"
                config = load_config(config_path)
                client = OpenWebUIClient(
                    config.openwebui.base_url,
                    config.openwebui.endpoint,
                    config.openwebui.api_key_env,
                    10,
                )
                catalog = ModelCatalog(config.swarm.catalog_path)
                catalog.sync(client.list_model_entries())
                _, run_dir, parsed = SwarmOrchestrator(config).run(
                    "Do hard task",
                    "code",
                    "Must pass",
                    [("large.txt", "x" * 5000), ("later.txt", "y" * 20)],
                    role_model_overrides={
                        "planner": "vendor/coder-instruct",
                        "implementer": "vendor/reasoner",
                        "critic": "other/critic-instruct",
                        "verifier": "third/verifier-instruct",
                    },
                    judge_model_override="vendor/reasoner",
                )
                self.assertEqual(parsed["answer"], "Integrated proposal")
                manifest = json.loads((run_dir / "context/manifest.json").read_text())
                self.assertTrue(manifest[0]["truncated"])
                self.assertEqual(len(manifest), 2)
                self.assertEqual(manifest[1]["status"], "omitted")
                self.assertEqual(manifest[1]["omitted_chars"], 20)
                prompt = (run_dir / "prompts/worker-planner.txt").read_text()
                self.assertIn("Codex is the controlling supervisor", prompt)
                detail = DashboardApp(config).run_detail(run_dir.name)
                self.assertTrue(detail["prompts"])
                self.assertTrue(detail["workers"])
                self.assertTrue(detail["final"])
                self.assertTrue(detail["judge"])
                self.assertEqual(run_dir.stat().st_mode & 0o777, 0o700)
                self.assertEqual((run_dir / "final.md").stat().st_mode & 0o777, 0o600)

                invalid_config = replace(
                    config, judge=replace(config.judge, model="vendor/invalid-judge")
                )
                _, invalid_dir, invalid = SwarmOrchestrator(invalid_config).run(
                    "Judge schema check", "code", "", []
                )
                self.assertEqual(invalid["confidence"], 0.2)
                self.assertIn("failed the required JSON schema", invalid["disagreements"][0])
                self.assertTrue((invalid_dir / "judge/response.md").exists())
        finally:
            server.shutdown()
            server.server_close()

    def test_catalog_requires_exact_healthy_and_prefers_family_diversity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            catalog = ModelCatalog(Path(temp) / "catalog.db")
            catalog.sync(
                [
                    {"id": "vendor/a-code-instruct", "provider": "vendor"},
                    {"id": "vendor/b-code-instruct", "provider": "vendor"},
                    {"id": "other/a-code-instruct", "provider": "other"},
                    {"id": "vendor/embed-model", "provider": "vendor"},
                ]
            )
            catalog.record_probe("vendor/a-code-instruct", "healthy", 100)
            catalog.record_probe("vendor/b-code-instruct", "failed", 10, "NOT HEALTHY")
            catalog.record_probe("other/a-code-instruct", "healthy", 200)
            selected = catalog.recommend("code", 3)
            self.assertEqual(
                {record.model_id for record in selected},
                {"vendor/a-code-instruct", "other/a-code-instruct"},
            )
            self.assertEqual(len({record.family for record in selected}), 2)
            self.assertEqual(len(catalog.probe_history()), 3)

    def test_context_manifest_keeps_files_after_limit(self) -> None:
        _text, manifest = _bounded_context(
            [("first", "x" * 2000), ("second", "y" * 10)], 1000
        )
        self.assertEqual([item["label"] for item in manifest], ["first", "second"])
        self.assertEqual(manifest[1]["sent_chars"], 0)
        self.assertEqual(manifest[1]["omitted_chars"], 10)

    def test_partial_success_passes_failure_metadata_and_caps_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = test_config(Path(temp))
            orchestrator = SwarmOrchestrator(config)
            scripted = ScriptedClient({"worker/critic": "timeout"})
            orchestrator.client = scripted  # type: ignore[assignment]
            _text, run_dir, result = orchestrator.run(
                "Task", "code", "", [], requested_workers=["planner", "critic"]
            )
            self.assertEqual(result["confidence"], 0.5)
            self.assertEqual(result["partial_success"]["worker_failures"], 1)
            judge_call = next(call for call in scripted.calls if call["judge"])
            self.assertIn("role=critic", str(judge_call["user"]))
            self.assertIn("category=timeout", str(judge_call["user"]))
            self.assertNotIn("controlled timeout", str(judge_call["user"]))
            self.assertEqual(
                [call["timeout"] for call in scripted.calls], [1, 1, 2]
            )
            events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
            failed = next(event for event in events if event["event"] == "worker_failed")
            self.assertEqual(failed["failure_category"], "timeout")
            self.assertEqual(failed["retry_count"], 0)
            self.assertEqual(sum(call["model"] == "worker/critic" for call in scripted.calls), 1)

    def test_one_success_among_several_and_all_worker_failure_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = test_config(Path(temp))
            orchestrator = SwarmOrchestrator(config)
            scripted = ScriptedClient({
                "worker/implementer": "timeout", "worker/critic": "transport"
            })
            orchestrator.client = scripted  # type: ignore[assignment]
            _text, _run_dir, result = orchestrator.run(
                "Task", "code", "", [],
                requested_workers=["planner", "implementer", "critic"],
            )
            self.assertEqual(result["confidence"], 0.3)
            self.assertIn("independently verify", result["verification"][-1])

        with tempfile.TemporaryDirectory() as temp:
            config = test_config(Path(temp))
            orchestrator = SwarmOrchestrator(config)
            scripted = ScriptedClient({
                "worker/planner": "timeout", "worker/critic": "http"
            })
            orchestrator.client = scripted  # type: ignore[assignment]
            with self.assertRaisesRegex(RuntimeError, "Every worker failed within bounded"):
                orchestrator.run(
                    "Task", "code", "", [], requested_workers=["planner", "critic"]
                )
            self.assertFalse(any(call["judge"] for call in scripted.calls))
            run_dir = next((Path(temp) / "runs").iterdir())
            self.assertTrue((run_dir / "failure.txt").exists())
            self.assertTrue((run_dir / "workers/_failures.json").exists())

    def test_judge_timeout_preserves_workers_and_records_category(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = test_config(Path(temp))
            orchestrator = SwarmOrchestrator(config)
            scripted = ScriptedClient(judge_failure="timeout")
            orchestrator.client = scripted  # type: ignore[assignment]
            with self.assertRaisesRegex(RequestFailure, "controlled timeout"):
                orchestrator.run("Task", "code", "", [], requested_workers=["planner"])
            run_dir = next((Path(temp) / "runs").iterdir())
            self.assertTrue((run_dir / "workers/planner.md").exists())
            failure = json.loads((run_dir / "judge/failure.json").read_text())
            self.assertEqual(failure["category"], "timeout")
            events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
            judge_failed = next(event for event in events if event["event"] == "judge_failed")
            self.assertEqual(judge_failed["timeout_seconds"], 2)

    def test_reliability_penalty_cooldown_recovery_and_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = test_config(root)
            catalog = ModelCatalog(root / "catalog.db")
            catalog.sync([
                {"id": "vendor/a-reasoning"}, {"id": "other/b-reasoning"},
                {"id": "vendor/disabled-reasoning"}, {"id": "vendor/embed"},
            ])
            for model in ("vendor/a-reasoning", "other/b-reasoning", "vendor/disabled-reasoning"):
                catalog.record_probe(model, "healthy", 100)
                catalog.update(model, quality=8, capabilities=["chat", "reasoning"])
            catalog.update("vendor/disabled-reasoning", enabled=False)
            catalog.record_task_attempt("r1", "vendor/a-reasoning", "planner", "code", "timeout", 1000)
            selected = catalog.recommend("code", 2, config.reliability, "planner")
            self.assertEqual(selected[0].model_id, "other/b-reasoning")
            self.assertIn("vendor/a-reasoning", {item.model_id for item in selected})
            self.assertNotIn("vendor/disabled-reasoning", {item.model_id for item in selected})
            self.assertNotIn("vendor/embed", {item.model_id for item in selected})
            reason = catalog.explicit_override_reason(catalog.get("vendor/a-reasoning"), config.reliability)
            self.assertIn("bypassed automatic reliability", reason)

            catalog.record_task_attempt("r2", "vendor/a-reasoning", "planner", "code", "protocol", 10)
            catalog.record_task_attempt("r3", "vendor/a-reasoning", "planner", "code", "timeout", 1000)
            self.assertTrue(catalog.reliability_summary("vendor/a-reasoning", config.reliability)["cooldown"])
            self.assertNotIn(
                "vendor/a-reasoning",
                {item.model_id for item in catalog.recommend("code", 4, config.reliability, "planner")},
            )
            catalog.record_task_attempt("r4", "vendor/a-reasoning", "planner", "code", "success", 200)
            recovered = catalog.reliability_summary("vendor/a-reasoning", config.reliability)
            self.assertFalse(recovered["cooldown"])
            self.assertEqual(recovered["consecutive_failures"], 0)

            catalog.record_task_attempt("r5", "other/b-reasoning", "planner", "code", "capacity", 10)
            self.assertTrue(catalog.reliability_summary("other/b-reasoning", config.reliability)["cooldown"])
            self.assertNotIn(
                "other/b-reasoning",
                {item.model_id for item in catalog.recommend("code", 4, config.reliability, "planner")},
            )

    def test_recent_success_latency_is_only_a_tie_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = test_config(root)
            catalog = ModelCatalog(root / "catalog.db")
            catalog.sync([{"id": "a/fast"}, {"id": "b/slow"}, {"id": "c/high-quality"}])
            for model in ("a/fast", "b/slow", "c/high-quality"):
                catalog.record_probe(model, "healthy", 100)
                catalog.update(model, quality=5, capabilities=["chat", "reasoning"])
            catalog.update("c/high-quality", quality=9)
            catalog.record_task_attempt("r", "a/fast", "planner", "code", "success", 100)
            catalog.record_task_attempt("r", "b/slow", "planner", "code", "success", 1000)
            catalog.record_task_attempt("r", "c/high-quality", "planner", "code", "success", 5000)
            ordered = catalog.recommend("code", 3, config.reliability, "planner")
            self.assertEqual(ordered[0].model_id, "c/high-quality")
            self.assertLess(
                [item.model_id for item in ordered].index("a/fast"),
                [item.model_id for item in ordered].index("b/slow"),
            )

    def test_client_protocol_error_redacts_secret(self) -> None:
        os.environ["TEST_SWARM_KEY"] = "test-only"
        client = OpenWebUIClient("http://127.0.0.1:9", "/chat", "TEST_SWARM_KEY", 1)
        with patch("swarm_router.client.request.urlopen", return_value=FakeResponse()):
            with self.assertRaises(RequestFailure) as raised:
                client.chat("model", "system", "user", 10, 0.0)
        self.assertEqual(raised.exception.category, "protocol")
        self.assertNotIn("test-only", str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    def test_client_resource_exhausted_is_capacity(self) -> None:
        os.environ["TEST_SWARM_KEY"] = "test-only"
        client = OpenWebUIClient("http://127.0.0.1:9", "/chat", "TEST_SWARM_KEY", 1)
        http_error = error.HTTPError(
            "http://127.0.0.1:9/chat",
            400,
            "Bad Request",
            {},
            BytesIO(b'{"detail":"ResourceExhausted: Worker local total request limit reached (48/48)"}'),
        )
        with patch("swarm_router.client.request.urlopen", side_effect=http_error):
            with self.assertRaises(RequestFailure) as raised:
                client.chat("model", "system", "user", 10, 0.0)
        self.assertEqual(raised.exception.category, "capacity")


if __name__ == "__main__":
    unittest.main()
