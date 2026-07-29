from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import os
import tomllib


@dataclass(frozen=True)
class OpenWebUIConfig:
    base_url: str
    endpoint: str
    health_endpoint: str
    models_endpoint: str
    api_key_env: str
    timeout_seconds: int


@dataclass(frozen=True)
class SwarmConfig:
    max_workers: int
    max_parallel_workers: int
    probe_concurrency: int
    worker_timeout_seconds: int
    judge_timeout_seconds: int
    max_context_chars: int
    max_worker_output_tokens: int
    max_judge_output_tokens: int
    temperature: float
    return_char_limit: int
    run_directory: str
    catalog_path: str


@dataclass(frozen=True)
class ProbeConfig:
    timeout_seconds: int
    max_parallel: int


@dataclass(frozen=True)
class ReliabilityConfig:
    enabled: bool
    recent_attempt_window: int
    timeout_penalty: float
    failure_penalty: float
    consecutive_failure_penalty: float
    latency_weight: float
    cooldown_after_consecutive_failures: int
    cooldown_minutes: int
    allow_explicit_override: bool


@dataclass(frozen=True)
class DashboardConfig:
    host: str
    port: int
    auth_token_env: str
    metadata_directory: str


@dataclass(frozen=True)
class PersonalConfig:
    model_id: str
    loopback_host: str
    port: int
    auth_token_env: str
    task_directory: str
    max_messages: int
    max_message_chars: int
    max_conversation_chars: int
    max_output_chars: int
    max_wiki_context_chars: int
    max_workers: int
    max_parallel_workers: int
    max_retries: int
    task_timeout_seconds: int
    worker_timeout_seconds: int
    max_active_tasks: int
    completed_task_retention: int
    event_history_retention: int


@dataclass(frozen=True)
class SchedulerConfig:
    poll_interval_seconds: int
    lease_seconds: int
    timezone: str


@dataclass(frozen=True)
class ImageGenerationConfig:
    comfyui_base_url: str
    artifact_directory: str
    connect_timeout_seconds: int
    request_timeout_seconds: int
    generation_timeout_seconds: int
    max_image_bytes: int
    poll_interval_seconds: float


@dataclass(frozen=True)
class AuthorityConfig:
    supervisor_name: str
    worker_trust: str
    current_data_policy: str
    execution_policy: str


@dataclass(frozen=True)
class AgentConfig:
    name: str
    model: str
    system: str
    modes: tuple[str, ...]


@dataclass(frozen=True)
class AppConfig:
    openwebui: OpenWebUIConfig
    swarm: SwarmConfig
    probe: ProbeConfig
    reliability: ReliabilityConfig
    dashboard: DashboardConfig
    personal: PersonalConfig
    scheduler: SchedulerConfig
    image_generation: ImageGenerationConfig
    authority: AuthorityConfig
    judge: AgentConfig
    workers: tuple[AgentConfig, ...]


def _require(table: dict[str, Any], key: str, section: str) -> Any:
    if key not in table:
        raise ValueError(f"Missing required config value [{section}].{key}")
    return table[key]


def load_config(path: str | Path, require_api_key: bool = True) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    ow = raw.get("openwebui", {})
    sw = raw.get("swarm", {})
    dash = raw.get("dashboard", {})
    personal_raw = raw.get("personal", {})
    scheduler_raw = raw.get("scheduler", {})
    image_raw = raw.get("image_generation", {})
    probe_raw = raw.get("probe", {})
    reliability_raw = raw.get("reliability", {})
    authority_raw = raw.get("authority", {})
    judge_raw = raw.get("judge", {})
    workers_raw = raw.get("workers", [])

    openwebui = OpenWebUIConfig(
        base_url=str(_require(ow, "base_url", "openwebui")).rstrip("/"),
        endpoint=str(ow.get("endpoint", "/api/chat/completions")),
        health_endpoint=str(ow.get("health_endpoint", "/health")),
        models_endpoint=str(ow.get("models_endpoint", "/api/models")),
        api_key_env=str(ow.get("api_key_env", "OPEN_WEBUI_API_KEY")),
        timeout_seconds=int(ow.get("timeout_seconds", 240)),
    )
    swarm = SwarmConfig(
        max_workers=max(1, int(sw.get("max_workers", 4))),
        max_parallel_workers=max(1, int(sw.get("max_parallel_workers", 4))),
        probe_concurrency=max(1, int(sw.get("probe_concurrency", 2))),
        worker_timeout_seconds=max(1, int(sw.get("worker_timeout_seconds", ow.get("timeout_seconds", 240)))),
        judge_timeout_seconds=max(1, int(sw.get("judge_timeout_seconds", ow.get("timeout_seconds", 240)))),
        max_context_chars=max(1000, int(sw.get("max_context_chars", 50000))),
        max_worker_output_tokens=max(128, int(sw.get("max_worker_output_tokens", 2200))),
        max_judge_output_tokens=max(128, int(sw.get("max_judge_output_tokens", 2600))),
        temperature=float(sw.get("temperature", 0.15)),
        return_char_limit=max(1000, int(sw.get("return_char_limit", 12000))),
        run_directory=str(sw.get("run_directory", "~/.local/share/owui-swarm/runs")),
        catalog_path=str(sw.get("catalog_path", "~/.local/share/owui-swarm/catalog.sqlite3")),
    )
    probe = ProbeConfig(
        timeout_seconds=max(1, int(probe_raw.get("timeout_seconds", ow.get("timeout_seconds", 45)))),
        max_parallel=max(1, int(probe_raw.get("max_parallel", sw.get("probe_concurrency", 2)))),
    )
    reliability = ReliabilityConfig(
        enabled=bool(reliability_raw.get("enabled", True)),
        recent_attempt_window=max(1, int(reliability_raw.get("recent_attempt_window", 8))),
        timeout_penalty=max(0.0, float(reliability_raw.get("timeout_penalty", 0.20))),
        failure_penalty=max(0.0, float(reliability_raw.get("failure_penalty", 0.15))),
        consecutive_failure_penalty=max(0.0, float(reliability_raw.get("consecutive_failure_penalty", 0.10))),
        latency_weight=max(0.0, float(reliability_raw.get("latency_weight", 0.10))),
        cooldown_after_consecutive_failures=max(1, int(reliability_raw.get("cooldown_after_consecutive_failures", 3))),
        cooldown_minutes=max(1, int(reliability_raw.get("cooldown_minutes", 60))),
        allow_explicit_override=bool(reliability_raw.get("allow_explicit_override", True)),
    )
    dashboard = DashboardConfig(
        host=str(dash.get("host", "127.0.0.1")),
        port=int(dash.get("port", 8787)),
        auth_token_env=str(dash.get("auth_token_env", "SWARM_DASHBOARD_TOKEN")),
        metadata_directory=str(
            dash.get("metadata_directory", "~/.local/share/owui-swarm/dashboard")
        ),
    )
    personal = PersonalConfig(
        model_id=str(personal_raw.get("model_id", "swarm-personal")),
        loopback_host=str(personal_raw.get("loopback_host", "127.0.0.1")),
        port=int(personal_raw.get("port", 8788)),
        auth_token_env=str(personal_raw.get("auth_token_env", "SWARM_PERSONAL_API_KEY")),
        task_directory=str(
            personal_raw.get("task_directory", "~/.local/share/owui-swarm/personal-tasks")
        ),
        max_messages=max(1, int(personal_raw.get("max_messages", 24))),
        max_message_chars=max(256, int(personal_raw.get("max_message_chars", 12000))),
        max_conversation_chars=max(512, int(personal_raw.get("max_conversation_chars", 48000))),
        max_output_chars=max(256, int(personal_raw.get("max_output_chars", 10000))),
        max_wiki_context_chars=max(256, int(personal_raw.get("max_wiki_context_chars", 6000))),
        max_workers=max(1, int(personal_raw.get("max_workers", 2))),
        max_parallel_workers=max(1, int(personal_raw.get("max_parallel_workers", 2))),
        max_retries=max(0, min(1, int(personal_raw.get("max_retries", 1)))),
        task_timeout_seconds=max(5, int(personal_raw.get("task_timeout_seconds", 240))),
        worker_timeout_seconds=max(5, int(personal_raw.get("worker_timeout_seconds", 180))),
        max_active_tasks=max(1, int(personal_raw.get("max_active_tasks", 2))),
        completed_task_retention=max(1, int(personal_raw.get("completed_task_retention", 200))),
        event_history_retention=max(10, int(personal_raw.get("event_history_retention", 100))),
    )
    scheduler = SchedulerConfig(
        poll_interval_seconds=max(1, int(scheduler_raw.get("poll_interval_seconds", 30))),
        lease_seconds=max(5, int(scheduler_raw.get("lease_seconds", 60))),
        timezone=str(scheduler_raw.get("timezone", "UTC")),
    )
    image_generation = ImageGenerationConfig(
        comfyui_base_url=str(image_raw.get("comfyui_base_url", "http://127.0.0.1:18188")).rstrip("/"),
        artifact_directory=str(
            image_raw.get("artifact_directory", "~/.local/share/owui-swarm/artifacts/images")
        ),
        connect_timeout_seconds=max(1, int(image_raw.get("connect_timeout_seconds", 3))),
        request_timeout_seconds=max(1, int(image_raw.get("request_timeout_seconds", 15))),
        generation_timeout_seconds=max(10, int(image_raw.get("generation_timeout_seconds", 900))),
        max_image_bytes=max(1024, int(image_raw.get("max_image_bytes", 25 * 1024 * 1024))),
        poll_interval_seconds=max(0.1, float(image_raw.get("poll_interval_seconds", 2.0))),
    )
    authority = AuthorityConfig(
        supervisor_name=str(authority_raw.get("supervisor_name", "Codex")),
        worker_trust=str(authority_raw.get("worker_trust", "low")),
        current_data_policy=str(
            authority_raw.get(
                "current_data_policy",
                "Workers must treat current facts as unverified unless supplied by the supervisor.",
            )
        ),
        execution_policy=str(
            authority_raw.get(
                "execution_policy",
                "Workers propose; the supervisor inspects, applies, tests, and decides.",
            )
        ),
    )

    judge = AgentConfig(
        name=str(judge_raw.get("name", "integrator")),
        model=str(_require(judge_raw, "model", "judge")),
        system=str(judge_raw.get("system", "Integrate candidate answers.")),
        modes=("auto", "code", "spec", "research", "general"),
    )

    workers: list[AgentConfig] = []
    for index, item in enumerate(workers_raw):
        workers.append(
            AgentConfig(
                name=str(item.get("name", f"worker-{index + 1}")),
                model=str(_require(item, "model", f"workers[{index}]")),
                system=str(item.get("system", "Solve the task independently.")),
                modes=tuple(str(mode) for mode in item.get("modes", ["auto"])),
            )
        )
    if not workers:
        raise ValueError("At least one [[workers]] entry is required.")

    if require_api_key and not os.environ.get(openwebui.api_key_env):
        raise RuntimeError(
            f"Environment variable {openwebui.api_key_env} is not set. "
            "Generate a restricted Open WebUI API key and export it before running."
        )

    return AppConfig(
        openwebui=openwebui,
        swarm=swarm,
        probe=probe,
        reliability=reliability,
        dashboard=dashboard,
        personal=personal,
        scheduler=scheduler,
        image_generation=image_generation,
        authority=authority,
        judge=judge,
        workers=tuple(workers),
    )
