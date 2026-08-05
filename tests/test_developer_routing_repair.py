from __future__ import annotations

import io
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from swarm_router.catalog import ModelCatalog
from swarm_router.client import (
    OpenWebUIClient,
    RequestFailure,
)
from swarm_router.developer import (
    DeveloperCoordinator,
    DeveloperError,
)


class OpenWebUIFailureClassificationTest(
    unittest.TestCase
):
    @staticmethod
    def _client() -> OpenWebUIClient:
        client = OpenWebUIClient.__new__(
            OpenWebUIClient
        )
        client.base_url = "http://open-webui.invalid"
        client.endpoint = "/api/chat/completions"
        client.api_key = "test-key"
        client.timeout_seconds = 1
        client.health_endpoint = "/health"
        client.models_endpoint = "/api/models"
        client._budget_enabled = False
        client._model_id = None
        client._catalog_context = None
        return client

    @staticmethod
    def _http_error(
        detail: str,
        status: int = 400,
    ) -> HTTPError:
        return HTTPError(
            "http://open-webui.invalid/api/chat/completions",
            status,
            "provider error",
            None,
            io.BytesIO(detail.encode("utf-8")),
        )

    def _category(
        self,
        detail: str,
        status: int = 400,
    ) -> str:
        client = self._client()

        with patch(
            "swarm_router.client.request.urlopen",
            side_effect=self._http_error(
                detail,
                status,
            ),
        ):
            with self.assertRaises(
                RequestFailure
            ) as captured:
                client._json_request(
                    "POST",
                    "/api/chat/completions",
                    payload={},
                )

        return captured.exception.category

    def test_wrapped_overload_is_capacity(
        self,
    ) -> None:
        self.assertEqual(
            self._category(
                '{"detail":"Service temporarily overloaded"}'
            ),
            "capacity",
        )

    def test_wrapped_internal_error_is_capacity(
        self,
    ) -> None:
        self.assertEqual(
            self._category(
                '{"detail":"Internal server error"}'
            ),
            "capacity",
        )

    def test_generic_bad_request_remains_http(
        self,
    ) -> None:
        self.assertEqual(
            self._category(
                '{"detail":"messages field is invalid"}'
            ),
            "http",
        )


class DeveloperRoutingFeedbackTest(
    unittest.TestCase
):
    def test_catalog_health_must_be_healthy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = (
                Path(temporary)
                / "catalog.sqlite3"
            )

            with sqlite3.connect(db_path) as db:
                db.execute(
                    """
                    CREATE TABLE
                    forge_developer_tool_models(
                        model_id TEXT PRIMARY KEY,
                        provider TEXT NOT NULL
                            DEFAULT '',
                        last_success_at TEXT NOT NULL
                            DEFAULT '',
                        success_count INTEGER NOT NULL
                            DEFAULT 0,
                        last_failure_at TEXT NOT NULL
                            DEFAULT '',
                        failure_count INTEGER NOT NULL
                            DEFAULT 0,
                        last_failure TEXT NOT NULL
                            DEFAULT ''
                    )
                    """
                )

            record = SimpleNamespace(
                model_id="thinkingmachines/inkling",
                enabled=True,
                available=True,
                kind="chat",
                health="timeout",
                probe_status="healthy",
                quarantined=False,
                supports_tools=True,
            )

            coordinator = (
                DeveloperCoordinator.__new__(
                    DeveloperCoordinator
                )
            )
            coordinator.path = db_path
            coordinator.catalog = SimpleNamespace(
                list=lambda: [record]
            )
            coordinator.config = SimpleNamespace(
                personal=SimpleNamespace(
                    model_id="swarm-personal"
                )
            )

            self.assertEqual(
                coordinator._eligible_models(),
                [],
            )

            record.health = "healthy"

            self.assertEqual(
                coordinator._eligible_models(),
                [record],
            )

    def test_attempt_is_written_to_main_ledger(
        self,
    ) -> None:
        recorder = Mock()

        coordinator = (
            DeveloperCoordinator.__new__(
                DeveloperCoordinator
            )
        )
        coordinator.catalog = SimpleNamespace(
            record_task_attempt=recorder
        )

        run = {"task_id": "FT-test-routing"}
        record = SimpleNamespace(
            model_id="vendor/model"
        )

        coordinator._record_developer_attempt(
            run,
            "implementer",
            record,
            RequestFailure(
                "Internal server error",
                "capacity",
            ),
            1250,
            2,
        )

        recorder.assert_called_once()

        arguments = recorder.call_args.args

        self.assertTrue(
            arguments[0].startswith(
                "FT-test-routing:developer:implementer:2:"
            )
        )
        self.assertEqual(
            arguments[1:],
            (
                "vendor/model",
                "implementer",
                "code",
                "capacity",
                1250,
                1,
            ),
        )

    def test_policy_is_not_capacity(
        self,
    ) -> None:
        self.assertEqual(
            (
                DeveloperCoordinator
                ._developer_attempt_status(
                    DeveloperError(
                        "Command is not allowed.",
                        code="policy_rejected",
                    )
                )
            ),
            "policy",
        )

    def test_success_classification(
        self,
    ) -> None:
        self.assertEqual(
            DeveloperCoordinator
            ._developer_attempt_status(None),
            "success",
        )


class CatalogPolicyHealthTest(
    unittest.TestCase
):
    def test_policy_event_preserves_provider_health(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = (
                Path(temporary)
                / "catalog.sqlite3"
            )
            catalog = ModelCatalog(path)

            with sqlite3.connect(path) as db:
                db.execute(
                    """
                    INSERT INTO models(
                        model_id,
                        kind,
                        capabilities,
                        health
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        "vendor/model",
                        "chat",
                        '["chat"]',
                        "healthy",
                    ),
                )

            catalog.record_task_attempt(
                "FT-policy",
                "vendor/model",
                "implementer",
                "code",
                "policy",
                25,
            )

            record = catalog.get(
                "vendor/model"
            )

            self.assertIsNotNone(record)
            self.assertEqual(
                record.health,
                "healthy",
            )


if __name__ == "__main__":
    unittest.main()
