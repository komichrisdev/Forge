from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import json
import tempfile
import unittest

from swarm_router.agents import AgentManifest, AgentRegistry, HandoffEnvelope, default_registry
from swarm_router.cli import main


class AgentRegistryTest(unittest.TestCase):
    def test_default_registry_lookup_status_and_serialization(self) -> None:
        registry = default_registry()
        self.assertIsNotNone(registry.get("night_owl"))
        self.assertIsNone(registry.get("missing"))
        self.assertEqual(registry.validate(), [])
        status = registry.status()
        self.assertEqual(status["agent_count"], 13)
        self.assertEqual(status["disabled_count"], 0)
        payload = registry.get("planner").to_dict()  # type: ignore[union-attr]
        self.assertEqual(AgentManifest.from_dict(payload).to_dict(), payload)
        self.assertNotIn("model", json.dumps(payload).lower())

    def test_duplicate_agents_are_rejected(self) -> None:
        manifest = AgentManifest("planner", "Planner", "Plans work.")
        with self.assertRaisesRegex(ValueError, "duplicate agent_id"):
            AgentRegistry([manifest, manifest])

    def test_invalid_manifest_reports_all_basic_shape_errors(self) -> None:
        manifest = AgentManifest.from_dict({
            "agent_id": "OpenAI/gpt-4",
            "display_name": "",
            "description": "",
            "enabled": "yes",
            "supported_task_types": "code",
            "preferred_capabilities": [""],
            "metadata": [],
        })
        issues = manifest.validate()
        self.assertIn("agent_id must be lowercase snake_case, 2-64 characters", issues)
        self.assertIn("display_name is required", issues)
        self.assertIn("description is required", issues)
        self.assertIn("enabled must be a boolean", issues)
        self.assertIn("supported_task_types must be a list", issues)
        self.assertIn("task types and capabilities must be non-empty strings", issues)
        self.assertIn("metadata must be an object", issues)

    def test_handoff_envelope_validation_and_serialization(self) -> None:
        registry = default_registry()
        envelope = HandoffEnvelope.from_dict({
            "task_id": "task-1",
            "source_agent": "planner",
            "destination_agent": "judge",
            "timestamp": "2026-07-28T18:00:00+00:00",
            "reason": "review required",
            "context_reference": "runs/task-1/context/sent.txt",
            "checkpoint_reference": "runs/task-1/final.json",
            "metadata": {"priority": "normal"},
        })
        self.assertEqual(envelope.validate(registry), [])
        self.assertEqual(HandoffEnvelope.from_dict(envelope.to_dict()).to_dict(), envelope.to_dict())

    def test_invalid_handoff_envelope_is_rejected(self) -> None:
        issues = HandoffEnvelope.from_dict({
            "task_id": "",
            "source_agent": "planner",
            "destination_agent": "planner",
            "timestamp": "2026-07-28T18:00:00",
            "reason": "",
            "context_reference": "",
            "checkpoint_reference": "",
            "metadata": [],
        }).validate(default_registry())
        self.assertIn("task_id is required", issues)
        self.assertIn("reason is required", issues)
        self.assertIn("timestamp must include timezone", issues)
        self.assertIn("source_agent and destination_agent must differ", issues)
        self.assertIn("metadata must be an object", issues)

    def test_cli_agent_and_handoff_commands_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            handoff = root / "handoff.json"
            handoff.write_text(json.dumps({
                "task_id": "task-1",
                "source_agent": "planner",
                "destination_agent": "judge",
                "timestamp": "2026-07-28T18:00:00+00:00",
                "reason": "review",
                "context_reference": "ctx",
                "checkpoint_reference": "checkpoint",
                "metadata": {},
            }), encoding="utf-8")
            manifest = root / "agent.json"
            manifest.write_text(json.dumps({
                "agent_id": "custom_agent",
                "display_name": "Custom Agent",
                "description": "Fixture logical agent.",
                "owner": "tests",
                "version": "1.0",
                "enabled": True,
                "supported_task_types": ["test"],
                "preferred_capabilities": [],
                "metadata": {},
            }), encoding="utf-8")

            for args in (
                ["status", "--json"],
                ["agent", "list", "--json"],
                ["agent", "show", "planner", "--json"],
                ["agent", "validate", "--json"],
                ["agent", "validate", str(manifest), "--json"],
                ["handoff", "validate", str(handoff), "--json"],
            ):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(args), 0)
                self.assertTrue(json.loads(output.getvalue()))


if __name__ == "__main__":
    unittest.main()
