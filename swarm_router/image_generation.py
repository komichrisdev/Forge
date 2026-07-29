from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Callable
from urllib import error, parse, request
import copy
import hashlib
import json
import os
import random
import re
import uuid

from .config import AppConfig
from .discord_notifications import NotificationStore, deliver, notification_from_store


PRESET_ID = "flux-schnell-768-daily"
IMAGE_AGENT_ID = "image_generator"
PROMPT_MAX = 1200
NEGATIVE_MAX = 1200
SEED_MAX = 2**63 - 1
PROGRESS_MARKS = (0, 25, 50, 75, 100)
SAFE_FIELD_RE = re.compile(r"(://|^[A-Za-z]:\\|(?:^|[\s'\"])/(?:home|tmp|srv|etc|var|opt)\b|\.\.)")
JSONISH_RE = re.compile(r"^\s*[\[{]")


class ImageGenerationError(RuntimeError):
    def __init__(self, message: str, *, category: str = "image_generation") -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class ImagePayload:
    preset_id: str
    prompt: str
    negative_prompt: str
    seed: int
    notification_requested: bool


@dataclass(frozen=True)
class ComfyUIStatus:
    state: str
    queue_depth: int = 0
    detail: str = ""
    system: dict[str, Any] | None = None


@dataclass(frozen=True)
class GenerationResult:
    prompt_id: str
    artifact_dir: str
    image_path: str
    thumbnail_path: str
    metadata_path: str
    checksum_sha256: str
    seed: int
    width: int
    height: int
    duration_ms: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _reject_text(name: str, value: str, limit: int) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if name == "prompt" and not text:
        raise ImageGenerationError("prompt is required", category="payload_invalid")
    if len(text) > limit:
        raise ImageGenerationError(f"{name} exceeds {limit} characters", category="payload_invalid")
    if SAFE_FIELD_RE.search(text) or JSONISH_RE.search(text):
        raise ImageGenerationError(f"{name} must not contain URLs, paths, or workflow JSON", category="payload_invalid")
    return text


def validate_image_payload(raw: dict[str, Any]) -> ImagePayload:
    allowed = {"preset_id", "prompt", "negative_prompt", "seed", "notification_requested"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ImageGenerationError("unknown image payload field(s): " + ", ".join(unknown), category="payload_invalid")
    preset_id = str(raw.get("preset_id", PRESET_ID)).strip()
    if preset_id != PRESET_ID:
        raise ImageGenerationError("preset is not approved", category="preset_not_allowed")
    if any(key in raw for key in ("workflow", "workflow_json", "model", "model_path", "output_path", "script")):
        raise ImageGenerationError("arbitrary workflow, model, output, and script fields are not allowed", category="payload_invalid")
    seed_value = raw.get("seed")
    if seed_value in (None, ""):
        seed = random.SystemRandom().randint(0, SEED_MAX)
    else:
        if isinstance(seed_value, bool):
            raise ImageGenerationError("seed must be an integer", category="payload_invalid")
        try:
            seed = int(seed_value)
        except (TypeError, ValueError) as exc:
            raise ImageGenerationError("seed must be an integer", category="payload_invalid") from exc
        if seed < 0 or seed > SEED_MAX:
            raise ImageGenerationError("seed is outside the supported range", category="payload_invalid")
    return ImagePayload(
        preset_id=preset_id,
        prompt=_reject_text("prompt", str(raw.get("prompt", "")), PROMPT_MAX),
        negative_prompt=_reject_text("negative_prompt", str(raw.get("negative_prompt", "")), NEGATIVE_MAX),
        seed=seed,
        notification_requested=bool(raw.get("notification_requested", False)),
    )


def preset_summary() -> dict[str, Any]:
    return {
        "preset_id": PRESET_ID,
        "name": "FLUX Schnell 768 Daily",
        "model": "FLUX.1 Schnell FP8",
        "width": 768,
        "height": 768,
        "steps": 4,
        "cfg": 1.0,
        "sampler": "euler",
        "scheduler": "simple",
        "images": 1,
    }


def workflow_template() -> dict[str, Any]:
    return {
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}", "clip": ["30", 1]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["31", 0], "vae": ["30", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "forge_flux_schnell_768", "images": ["8", 0]}},
        "27": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 768, "height": 768, "batch_size": 1}},
        "30": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "flux1-schnell-fp8.safetensors"}},
        "31": {
            "class_type": "KSampler",
            "inputs": {
                "seed": "{{seed}}",
                "steps": 4,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
                "model": ["30", 0],
                "positive": ["6", 0],
                "negative": ["33", 0],
                "latent_image": ["27", 0],
            },
        },
        "33": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{negative_prompt}}", "clip": ["30", 1]}},
    }


def build_workflow(payload: ImagePayload) -> dict[str, Any]:
    graph = copy.deepcopy(workflow_template())
    graph["6"]["inputs"]["text"] = payload.prompt
    graph["31"]["inputs"]["seed"] = payload.seed
    graph["33"]["inputs"]["text"] = payload.negative_prompt
    return graph


def validate_workflow(graph: dict[str, Any]) -> list[str]:
    required_classes = {"CheckpointLoaderSimple", "EmptySD3LatentImage", "KSampler", "SaveImage"}
    classes = {str(node.get("class_type", "")) for node in graph.values() if isinstance(node, dict)}
    issues = [f"missing node class: {name}" for name in sorted(required_classes - classes)]
    text = json.dumps(graph, sort_keys=True)
    if any(token in text for token in ("{{prompt}}", "{{seed}}", "{{negative_prompt}}")):
        issues.append("workflow placeholders were not fully substituted")
    if "flux1-schnell-fp8.safetensors" not in text:
        issues.append("approved FLUX Schnell FP8 checkpoint is not configured")
    return issues


def validate_comfyui_requirements(object_info: dict[str, Any]) -> list[str]:
    required_nodes = {"CheckpointLoaderSimple", "EmptySD3LatentImage", "KSampler", "CLIPTextEncode", "VAEDecode", "SaveImage"}
    issues = [f"missing ComfyUI node: {name}" for name in sorted(required_nodes - set(object_info))]
    ckpt = object_info.get("CheckpointLoaderSimple", {}).get("input", {}).get("required", {}).get("ckpt_name")
    choices = ckpt[0] if isinstance(ckpt, list) and ckpt else []
    if "flux1-schnell-fp8.safetensors" not in choices:
        issues.append("required checkpoint is missing: flux1-schnell-fp8.safetensors")
    return issues


class ComfyUIClient:
    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout: float = 3,
        request_timeout: float = 15,
        open_url: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.open_url = open_url or request.urlopen
        self.client_id = f"forge-{uuid.uuid4().hex}"

    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        url = self.base_url + path
        return url + ("?" + parse.urlencode(query)) if query else url

    def _json(self, method: str, path: str, payload: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = request.Request(self._url(path), data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with self.open_url(req, timeout=timeout or self.request_timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except (TimeoutError, OSError, error.URLError) as exc:
            raise ImageGenerationError("ComfyUI is offline or unreachable", category="comfyui_unavailable") from exc
        except json.JSONDecodeError as exc:
            raise ImageGenerationError("ComfyUI returned malformed JSON", category="comfyui_unavailable") from exc

    def status(self) -> ComfyUIStatus:
        try:
            system = self._json("GET", "/system_stats", timeout=self.connect_timeout)
            queue = self.queue()
        except ImageGenerationError as exc:
            return ComfyUIStatus("offline", detail=str(exc))
        depth = queue_depth(queue)
        return ComfyUIStatus("busy" if depth else "ready", queue_depth=depth, system=system if isinstance(system, dict) else {})

    def queue(self) -> dict[str, Any]:
        data = self._json("GET", "/queue")
        return data if isinstance(data, dict) else {}

    def object_info(self) -> dict[str, Any]:
        data = self._json("GET", "/object_info")
        return data if isinstance(data, dict) else {}

    def submit(self, graph: dict[str, Any]) -> str:
        data = self._json("POST", "/prompt", {"prompt": graph, "client_id": self.client_id})
        prompt_id = str(data.get("prompt_id") or "")
        if not prompt_id:
            raise ImageGenerationError("ComfyUI did not return a prompt ID", category="queue_rejected")
        return prompt_id

    def history(self, prompt_id: str) -> dict[str, Any]:
        data = self._json("GET", f"/history/{parse.quote(prompt_id)}")
        return data if isinstance(data, dict) else {}

    def retrieve_output(self, image: dict[str, Any], max_bytes: int) -> bytes:
        filename = str(image.get("filename") or "")
        subfolder = str(image.get("subfolder") or "")
        kind = str(image.get("type") or "output")
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            raise ImageGenerationError("ComfyUI returned an unsafe output filename", category="malformed_output")
        url = self._url("/view", {"filename": filename, "subfolder": subfolder, "type": kind})
        try:
            with self.open_url(request.Request(url), timeout=self.request_timeout) as response:
                data = response.read(max_bytes + 1)
        except (TimeoutError, OSError, error.URLError) as exc:
            raise ImageGenerationError("failed to retrieve ComfyUI output", category="artifact_copy_failure") from exc
        if len(data) > max_bytes:
            raise ImageGenerationError("generated image exceeds maximum size", category="oversized_output")
        return data

    def cancel(self) -> bool:
        try:
            self._json("POST", "/interrupt", {})
            return True
        except ImageGenerationError:
            return False


def queue_depth(data: dict[str, Any]) -> int:
    running = data.get("queue_running") if isinstance(data, dict) else []
    pending = data.get("queue_pending") if isinstance(data, dict) else []
    return len(running or []) + len(pending or [])


def progress_from_history(history: dict[str, Any], prompt_id: str) -> tuple[int, dict[str, Any] | None]:
    item = history.get(prompt_id) if isinstance(history, dict) else None
    if not isinstance(item, dict):
        return 75, None
    outputs = item.get("outputs") if isinstance(item.get("outputs"), dict) else {}
    for value in outputs.values():
        if isinstance(value, dict) and value.get("images"):
            return 100, value["images"][0]
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    if status.get("completed"):
        return 100, None
    return 75, None


def wait_for_output(
    client: ComfyUIClient,
    prompt_id: str,
    *,
    timeout_seconds: int,
    poll_interval_seconds: float,
    progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    started = monotonic()
    emitted: set[int] = set()
    if progress:
        progress(0)
        emitted.add(0)
    while monotonic() - started < timeout_seconds:
        history = client.history(prompt_id)
        pct, image = progress_from_history(history, prompt_id)
        for mark in PROGRESS_MARKS:
            if pct >= mark and mark not in emitted and progress:
                progress(mark)
                emitted.add(mark)
        if image:
            return image
        sleep(poll_interval_seconds)
    raise ImageGenerationError("generation timed out", category="generation_timeout")


def validate_image_bytes(data: bytes, max_bytes: int) -> tuple[str, int, int]:
    if len(data) > max_bytes:
        raise ImageGenerationError("generated image exceeds maximum size", category="oversized_output")
    kind = ""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        kind = "jpeg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        kind = "webp"
    if kind not in {"png", "jpeg", "webp"}:
        raise ImageGenerationError("generated output is not a supported image", category="malformed_output")
    width = height = 0
    if kind == "png" and data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
    return kind, width, height


def store_artifact(config: AppConfig, forge_task_id: str, prompt_id: str, payload: ImagePayload, data: bytes) -> GenerationResult:
    kind, width, height = validate_image_bytes(data, config.image_generation.max_image_bytes)
    root = Path(config.image_generation.artifact_directory).expanduser().resolve()
    task_dir = (root / forge_task_id).resolve()
    if task_dir.parent != root:
        raise ImageGenerationError("unsafe artifact path", category="artifact_copy_failure")
    task_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    image_path = task_dir / ("output.png" if kind == "png" else f"output.{kind}")
    image_path.write_bytes(data)
    image_path.chmod(0o600)
    checksum = hashlib.sha256(data).hexdigest()
    thumbnail_path = task_dir / "thumbnail.webp"
    try:
        from PIL import Image

        with Image.open(image_path) as image:
            image.thumbnail((256, 256))
            image.save(thumbnail_path, "WEBP", quality=82)
    except Exception as exc:
        raise ImageGenerationError("thumbnail generation failed", category="artifact_copy_failure") from exc
    thumbnail_path.chmod(0o600)
    metadata = {
        "forge_task_id": forge_task_id,
        "comfyui_prompt_id": prompt_id,
        "preset_id": payload.preset_id,
        "seed": payload.seed,
        "width": width,
        "height": height,
        "created_at": _utc_now(),
        "sha256": checksum,
        "image_file": image_path.name,
        "thumbnail_file": thumbnail_path.name,
        "prompt_summary": payload.prompt[:160],
    }
    metadata_path = task_dir / "metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metadata_path.chmod(0o600)
    return GenerationResult(prompt_id, str(task_dir), str(image_path), str(thumbnail_path), str(metadata_path), checksum, payload.seed, width, height, 0)


def gallery(config: AppConfig, limit: int = 20) -> list[dict[str, Any]]:
    root = Path(config.image_generation.artifact_directory).expanduser().resolve()
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for path in sorted(root.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        meta = path / "metadata.json"
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        data["artifact_id"] = path.name
        data["thumbnail_url"] = f"/api/images/artifacts/{parse.quote(path.name)}/thumbnail"
        data["image_url"] = f"/api/images/artifacts/{parse.quote(path.name)}/original"
        rows.append(data)
    return rows[:limit]


def artifact_file(config: AppConfig, forge_task_id: str, kind: str) -> tuple[Path, str]:
    if not re.fullmatch(r"FT-\d{8}-\d{6}", forge_task_id):
        raise FileNotFoundError(forge_task_id)
    root = Path(config.image_generation.artifact_directory).expanduser().resolve()
    task_dir = (root / forge_task_id).resolve()
    if task_dir.parent != root:
        raise FileNotFoundError(forge_task_id)
    metadata = json.loads((task_dir / "metadata.json").read_text(encoding="utf-8"))
    name = metadata["thumbnail_file"] if kind == "thumbnail" else metadata["image_file"]
    path = (task_dir / str(name)).resolve()
    if path.parent != task_dir or not path.exists():
        raise FileNotFoundError(forge_task_id)
    content_type = "image/webp" if path.suffix == ".webp" else ("image/png" if path.suffix == ".png" else "image/jpeg")
    return path, content_type


def notify_image_completion(
    config: AppConfig,
    *,
    task_id: str,
    forge_task_id: str,
    result: GenerationResult | None,
    failure_category: str = "",
    dashboard_url: str = "",
) -> dict[str, Any] | None:
    store = NotificationStore(config.swarm.catalog_path)
    if result:
        message = (
            f"Task {forge_task_id} completed. Preset {PRESET_ID}. "
            f"Seed {result.seed}. Duration {result.duration_ms} ms."
        )
        if dashboard_url:
            message += f" Dashboard: {dashboard_url}"
        return deliver(
            store,
            notification_from_store(
                store,
                event_type="image_generation.completed",
                severity="success",
                title="Forge image generation complete",
                message=message,
                task_id=task_id,
                forge_task_id=forge_task_id,
                agent_id=IMAGE_AGENT_ID,
                deduplication_key=f"image:{forge_task_id}:completed",
                metadata={"preset_id": PRESET_ID, "seed": result.seed},
            ),
        )
    if failure_category:
        return deliver(
            store,
            notification_from_store(
                store,
                event_type="image_generation.failed",
                severity="error",
                title="Forge image generation failed",
                message=f"Task {forge_task_id} failed. Category: {failure_category}.",
                task_id=task_id,
                forge_task_id=forge_task_id,
                agent_id=IMAGE_AGENT_ID,
                deduplication_key=f"image:{forge_task_id}:failed",
                metadata={"category": failure_category},
            ),
        )
    return None
