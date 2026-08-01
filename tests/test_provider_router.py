from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from swarm_router.catalog import ModelCatalog
from swarm_router.providers import ProviderModel, provider_items


class ProviderRouterTest(unittest.TestCase):
    def test_context_metadata_defaults_safely_and_only_auto_tightens(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            catalog = ModelCatalog(Path(temp) / "catalog.db")
            model_id = "provider/runtime-model"
            catalog.sync([{"id": model_id, "context_length": 65536}])
            self.assertEqual(catalog.get(model_id).context_length, 65536)  # type: ignore[union-attr]

            catalog.sync([{"id": model_id, "n_ctx": 8192}])
            self.assertEqual(catalog.get(model_id).context_length, 8192)  # type: ignore[union-attr]
            catalog.sync([{"id": model_id, "context_length": 131072}])
            self.assertEqual(catalog.get(model_id).context_length, 8192)  # type: ignore[union-attr]
            catalog.sync([{"id": model_id}])
            self.assertEqual(catalog.get(model_id).context_length, 8192)  # type: ignore[union-attr]

            unknown_id = "provider/no-runtime-metadata"
            catalog.sync([{"id": model_id}, {"id": unknown_id}])
            self.assertIsNone(catalog.get(unknown_id).context_length)  # type: ignore[union-attr]
            catalog.sync([{"id": model_id}, {"id": unknown_id, "context_length": 65536}])
            self.assertEqual(catalog.get(unknown_id).context_length, 65536)  # type: ignore[union-attr]
            self.assertEqual(catalog.update(model_id, context_length=32768).context_length, 32768)
            catalog.sync([{"id": model_id}, {"id": unknown_id}])
            self.assertEqual(catalog.get(model_id).context_length, 32768)  # type: ignore[union-attr]
            for invalid in (True, 0, -1, 2**63, "invalid"):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    catalog.update(model_id, context_length=invalid)

    def test_inventory_reconcile_quarantine_misses_and_last_known_good(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            catalog = ModelCatalog(Path(temp) / "catalog.db")
            entry = {
                "id": "nvidia/deepseek-code-reason-pro",
                "name": "DeepSeek Code Reason Pro",
                "provider": "nvidia",
                "supports_tools": True,
                "supports_streaming": False,
                "supports_images": "true",
                "context_length": 65536,
                "cost_hint": "low",
                "latency_hint": "fast",
            }
            first = catalog.reconcile_inventory("nvidia", [entry], mode="shadow")
            self.assertEqual(first["added"], [entry["id"]])

            record = catalog.get(entry["id"])
            assert record is not None
            self.assertFalse(record.available)
            self.assertTrue(record.quarantined)
            self.assertEqual(record.provider_id, "nvidia")
            self.assertEqual(record.display_name, "DeepSeek Code Reason Pro")
            self.assertIn("coding", record.capabilities)
            self.assertIn("reasoning.high", record.capabilities)
            self.assertIn("tool_use", record.capabilities)
            self.assertIn("vision", record.capabilities)
            self.assertIn("long_context", record.capabilities)
            self.assertTrue(record.supports_tools)
            self.assertFalse(record.supports_streaming)
            self.assertTrue(record.supports_images)
            self.assertTrue(record.supports_reasoning)

            catalog.record_probe(entry["id"], "healthy", 5)
            self.assertTrue(catalog.get(entry["id"]).available)  # type: ignore[union-attr]
            failure = catalog.record_inventory_failure("nvidia", "temporary outage")
            self.assertTrue(failure["preserved_last_known_good"])
            self.assertTrue(catalog.get(entry["id"]).available)  # type: ignore[union-attr]
            self.assertEqual(catalog.provider_status()["providers"][0]["health"], "failed")

            missing = catalog.reconcile_inventory("nvidia", [], mode="live")
            self.assertEqual(missing["missing_once"], [entry["id"]])
            self.assertTrue(catalog.get(entry["id"]).available)  # type: ignore[union-attr]

            gone = catalog.reconcile_inventory("nvidia", [], mode="live")
            self.assertEqual(gone["unavailable"], [entry["id"]])
            self.assertFalse(catalog.get(entry["id"]).available)  # type: ignore[union-attr]

            recovered = catalog.reconcile_inventory("nvidia", [entry], mode="live")
            self.assertEqual(recovered["recovered"], [entry["id"]])
            record = catalog.get(entry["id"])
            assert record is not None
            self.assertTrue(record.quarantined)
            self.assertFalse(record.available)
            self.assertEqual([item.model_id for item in catalog.recommend("code", 1)], [])

            catalog.record_probe(entry["id"], "healthy", 5)
            self.assertEqual([item.model_id for item in catalog.recommend("code", 1)], [entry["id"]])

    def test_provider_and_model_cooldowns_are_tracked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            catalog = ModelCatalog(Path(temp) / "catalog.db")
            catalog.reconcile_inventory("nvidia", ["nvidia/qwen-reasoning"], mode="shadow")
            catalog.record_probe("nvidia/qwen-reasoning", "healthy", 5)

            state = catalog.set_provider_cooldown("nvidia", minutes=10)
            self.assertEqual(state["health"], "cooldown")
            self.assertTrue(state["cooldown_until"])
            self.assertEqual(catalog.set_provider_cooldown("nvidia", clear=True)["cooldown_until"], "")

            catalog.record_task_attempt("run-1", "nvidia/qwen-reasoning", "planner", "code", "capacity", 10)
            self.assertTrue(catalog.get("nvidia/qwen-reasoning").cooldown_until)  # type: ignore[union-attr]
            self.assertEqual(catalog.recommend("code", 1), [])
            catalog.record_task_attempt("run-2", "nvidia/qwen-reasoning", "planner", "code", "success", 10)
            self.assertEqual(catalog.get("nvidia/qwen-reasoning").cooldown_until, "")  # type: ignore[union-attr]

    def test_provider_items_keep_provider_metadata_generic(self) -> None:
        models = provider_items([
            ProviderModel("nvidia", "nvidia/model", "Model", {"supports_tools": True})
        ])
        self.assertEqual(
            models,
            [{
                "supports_tools": True,
                "id": "nvidia/model",
                "name": "Model",
                "provider_id": "nvidia",
            }],
        )


if __name__ == "__main__":
    unittest.main()
