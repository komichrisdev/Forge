from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest.mock import patch
import json
import os
import unittest

from swarm_router.cli import main
from swarm_router.config import load_config
from swarm_router.image_generation import (
    PRESET_ID,
    ComfyUIStatus,
    ImageGenerationError,
    build_workflow,
    gallery,
    validate_image_bytes,
    validate_comfyui_requirements,
    validate_image_payload,
    validate_workflow,
)
from swarm_router.journal import SideEffectState, TaskJournal
from swarm_router.personal import PersonalTaskManager


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?"
    b"\x00\x05\xfe\x02\xfeA\xe2!\xbc\x00\x00\x00\x00IEND\xaeB`\x82"
)


def write_config(root: Path) -> Path:
    path = root / "config.toml"
    path.write_text(
        f'''[openwebui]
base_url = "http://127.0.0.1:9"
api_key_env = "OPEN_WEBUI_API_KEY"

[swarm]
run_directory = "{root / 'runs'}"
catalog_path = "{root / 'catalog.db'}"

[dashboard]
metadata_directory = "{root / 'dashboard'}"

[personal]
task_directory = "{root / 'personal'}"
task_timeout_seconds = 5
worker_timeout_seconds = 1
max_active_tasks = 1

[image_generation]
comfyui_base_url = "http://127.0.0.1:9"
artifact_directory = "{root / 'artifacts' / 'images'}"
generation_timeout_seconds = 5
poll_interval_seconds = 0.01

[scheduler]
timezone = "UTC"

[authority]
supervisor_name = "Codex"

[judge]
model = "fake/judge"

[[workers]]
name = "planner"
model = "fake/planner"
modes = ["auto"]
''',
        encoding="utf-8",
    )
    return path


class FakeDashboard:
    def __init__(self, config):
        self.config = config


class FakeComfyUI:
    def __init__(self, *_args, fail_submit: bool = False, **_kwargs) -> None:
        self.fail_submit = fail_submit
        self.polls = 0

    def status(self) -> ComfyUIStatus:
        return ComfyUIStatus("ready", queue_depth=0, system={"devices": []})

    def submit(self, _workflow):
        if self.fail_submit:
            raise ImageGenerationError("lost confirmation", category="queue_rejected")
        return "prompt-123"

    def history(self, prompt_id: str):
        self.polls += 1
        if self.polls < 2:
            return {}
        return {prompt_id: {"outputs": {"9": {"images": [{"filename": "out.png", "subfolder": "", "type": "output"}]}}}}

    def retrieve_output(self, _image, _max_bytes: int) -> bytes:
        return PNG_1X1

    def object_info(self):
        return {
            "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["flux1-schnell-fp8.safetensors"], {}]}}},
            "EmptySD3LatentImage": {},
            "KSampler": {},
            "CLIPTextEncode": {},
            "VAEDecode": {},
            "SaveImage": {},
        }


class ImageGenerationTest(unittest.TestCase):
    def test_payload_workflow_and_image_validation(self) -> None:
        payload = validate_image_payload({"preset_id": PRESET_ID, "prompt": "daily validation image", "seed": "42"})
        self.assertEqual(payload.seed, 42)
        workflow = build_workflow(payload)
        self.assertFalse(validate_workflow(workflow))
        self.assertIn("flux1-schnell-fp8.safetensors", json.dumps(workflow))
        self.assertEqual(validate_image_bytes(PNG_1X1, 1024)[1:], (1, 1))
        self.assertFalse(validate_comfyui_requirements(FakeComfyUI().object_info()))
        self.assertIn("required checkpoint", "; ".join(validate_comfyui_requirements({"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [[], {}]}}}})))
        for bad in (
            {"preset_id": "other", "prompt": "x"},
            {"preset_id": PRESET_ID, "prompt": "http://example.com"},
            {"preset_id": PRESET_ID, "prompt": "../secret"},
            {"preset_id": PRESET_ID, "prompt": "x", "workflow_json": {}},
            {"preset_id": PRESET_ID, "prompt": "x", "seed": -1},
        ):
            with self.assertRaises(ImageGenerationError):
                validate_image_payload(bad)
        with self.assertRaises(ImageGenerationError):
            validate_image_bytes(b"not-image", 1024)

    def test_cli_presets_and_offline_status(self) -> None:
        with TemporaryDirectory() as temp:
            config = str(write_config(Path(temp)))
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--config", config, "image", "presets", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue())[0]["preset_id"], PRESET_ID)
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--config", config, "image", "status", "--json"]), 1)
            self.assertEqual(json.loads(output.getvalue())["connection"]["state"], "offline")

    def test_personal_image_task_stores_artifact_and_journal_side_effects(self) -> None:
        with TemporaryDirectory() as temp, patch.dict(os.environ, {"OPEN_WEBUI_API_KEY": "x", "SWARM_PERSONAL_API_KEY": "personal"}):
            root = Path(temp)
            config = load_config(write_config(root), require_api_key=False)
            with patch("swarm_router.personal.DashboardApp", FakeDashboard), patch("swarm_router.personal.ComfyUIClient", FakeComfyUI):
                manager = PersonalTaskManager(config)
                task = manager.create_task({
                    "model": "swarm-personal",
                    "messages": [{"role": "user", "content": "image"}],
                    "task_type": "image_generate",
                    "agent_id": "image_generator",
                    "task_payload": {"preset_id": PRESET_ID, "prompt": "daily validation image", "seed": 7},
                })
                done = self._wait(manager, task["task_id"], "completed")
            self.assertEqual(done["progress"], 100)
            self.assertEqual(done["comfyui_prompt_id"], "prompt-123")
            self.assertTrue(Path(done["image_path"]).exists())
            self.assertTrue(Path(done["metadata_path"]).exists())
            self.assertEqual(gallery(config)[0]["forge_task_id"], done["forge_task_id"])
            events = TaskJournal(config.swarm.catalog_path).events(done["forge_task_id"])
            self.assertIn(SideEffectState.CONFIRMED.value, [event.side_effect_state for event in events])

    def test_ambiguous_submission_fails_without_retry(self) -> None:
        class FailingComfy(FakeComfyUI):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, fail_submit=True, **kwargs)

        with TemporaryDirectory() as temp, patch.dict(os.environ, {"OPEN_WEBUI_API_KEY": "x", "SWARM_PERSONAL_API_KEY": "personal"}):
            root = Path(temp)
            config = load_config(write_config(root), require_api_key=False)
            with patch("swarm_router.personal.DashboardApp", FakeDashboard), patch("swarm_router.personal.ComfyUIClient", FailingComfy):
                manager = PersonalTaskManager(config)
                task = manager.create_task({
                    "model": "swarm-personal",
                    "messages": [{"role": "user", "content": "image"}],
                    "task_type": "image_generate",
                    "agent_id": "image_generator",
                    "task_payload": {"preset_id": PRESET_ID, "prompt": "daily validation image"},
                })
                failed = self._wait(manager, task["task_id"], "failed")
            self.assertEqual(failed["failure_category"], "unknown_submission")
            self.assertEqual(failed["retry_count"], 0)
            states = [event.side_effect_state for event in TaskJournal(config.swarm.catalog_path).events(failed["forge_task_id"])]
            self.assertIn(SideEffectState.UNKNOWN.value, states)

    def _wait(self, manager: PersonalTaskManager, task_id: str, status: str) -> dict[str, object]:
        deadline = monotonic() + 5
        while monotonic() < deadline:
            task = manager.task_view(task_id)
            if task["status"] == status:
                return task
            sleep(0.02)
        self.fail(f"timed out waiting for {status}")


if __name__ == "__main__":
    unittest.main()
