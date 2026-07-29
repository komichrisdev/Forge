from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable
import json
import re


AGENT_REGISTRY_VERSION = "0.1"
AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def _text(data: dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class AgentManifest:
    agent_id: str
    display_name: str
    description: str
    owner: str = "forge"
    version: str = "1.0"
    enabled: Any = True
    supported_task_types: Any = field(default_factory=tuple)
    preferred_capabilities: Any = field(default_factory=tuple)
    metadata: Any = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentManifest":
        task_types = data.get("supported_task_types", ())
        capabilities = data.get("preferred_capabilities", ())
        return cls(
            agent_id=_text(data, "agent_id"),
            display_name=_text(data, "display_name"),
            description=_text(data, "description"),
            owner=_text(data, "owner", "forge"),
            version=_text(data, "version", "1.0"),
            enabled=data.get("enabled", True),
            supported_task_types=tuple(task_types) if isinstance(task_types, list | tuple) else task_types,
            preferred_capabilities=tuple(capabilities) if isinstance(capabilities, list | tuple) else capabilities,
            metadata=data.get("metadata", {}),
        )

    def validate(self) -> list[str]:
        issues: list[str] = []
        if not isinstance(self.agent_id, str) or not AGENT_ID_RE.fullmatch(self.agent_id):
            issues.append("agent_id must be lowercase snake_case, 2-64 characters")
        for field_name in ("display_name", "description", "owner", "version"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                issues.append(f"{field_name} is required")
        if isinstance(self.agent_id, str) and ("/" in self.agent_id or ":" in self.agent_id):
            issues.append("agent_id must not reference a provider or model")
        if not isinstance(self.enabled, bool):
            issues.append("enabled must be a boolean")
        if not isinstance(self.supported_task_types, tuple):
            issues.append("supported_task_types must be a list")
        if not isinstance(self.preferred_capabilities, tuple):
            issues.append("preferred_capabilities must be a list")
        values: list[Any] = []
        if isinstance(self.supported_task_types, tuple):
            values.extend(self.supported_task_types)
        if isinstance(self.preferred_capabilities, tuple):
            values.extend(self.preferred_capabilities)
        for item in values:
            if not isinstance(item, str) or not item.strip():
                issues.append("task types and capabilities must be non-empty strings")
        if not isinstance(self.metadata, dict):
            issues.append("metadata must be an object")
        return issues

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if isinstance(self.supported_task_types, tuple):
            data["supported_task_types"] = list(self.supported_task_types)
        if isinstance(self.preferred_capabilities, tuple):
            data["preferred_capabilities"] = list(self.preferred_capabilities)
        return data


@dataclass(frozen=True)
class HandoffEnvelope:
    task_id: str
    source_agent: str
    destination_agent: str
    timestamp: str
    reason: str
    context_reference: str
    checkpoint_reference: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HandoffEnvelope":
        return cls(
            task_id=_text(data, "task_id"),
            source_agent=_text(data, "source_agent"),
            destination_agent=_text(data, "destination_agent"),
            timestamp=_text(data, "timestamp"),
            reason=_text(data, "reason"),
            context_reference=_text(data, "context_reference"),
            checkpoint_reference=_text(data, "checkpoint_reference"),
            metadata=data.get("metadata", {}),
        )

    def validate(self, registry: "AgentRegistry | None" = None) -> list[str]:
        issues: list[str] = []
        for field_name in (
            "task_id", "source_agent", "destination_agent", "timestamp",
            "reason", "context_reference", "checkpoint_reference",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                issues.append(f"{field_name} is required")
        for field_name in ("source_agent", "destination_agent"):
            agent_id = getattr(self, field_name)
            if agent_id and (not isinstance(agent_id, str) or not AGENT_ID_RE.fullmatch(agent_id)):
                issues.append(f"{field_name} must be a logical agent_id")
            if registry and agent_id and registry.get(agent_id) is None:
                issues.append(f"{field_name} is not registered")
        try:
            parsed = datetime.fromisoformat(self.timestamp) if isinstance(self.timestamp, str) else None
            if parsed is None:
                raise ValueError
            if parsed.tzinfo is None:
                issues.append("timestamp must include timezone")
        except ValueError:
            issues.append("timestamp must be ISO-8601")
        if self.source_agent == self.destination_agent and self.source_agent:
            issues.append("source_agent and destination_agent must differ")
        if not isinstance(self.metadata, dict):
            issues.append("metadata must be an object")
        return issues

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentRegistry:
    def __init__(self, manifests: Iterable[AgentManifest] = ()) -> None:
        self._agents: dict[str, AgentManifest] = {}
        for manifest in manifests:
            self.register(manifest)

    def register(self, manifest: AgentManifest) -> None:
        issues = manifest.validate()
        if issues:
            raise ValueError("; ".join(issues))
        if manifest.agent_id in self._agents:
            raise ValueError(f"duplicate agent_id: {manifest.agent_id}")
        self._agents[manifest.agent_id] = manifest

    def get(self, agent_id: str) -> AgentManifest | None:
        return self._agents.get(agent_id)

    def list(self) -> list[AgentManifest]:
        return sorted(self._agents.values(), key=lambda item: item.agent_id)

    def validate(self) -> list[str]:
        seen: set[str] = set()
        issues: list[str] = []
        for manifest in self.list():
            if manifest.agent_id in seen:
                issues.append(f"duplicate agent_id: {manifest.agent_id}")
            seen.add(manifest.agent_id)
            issues.extend(f"{manifest.agent_id}: {issue}" for issue in manifest.validate())
        return issues

    def status(self) -> dict[str, Any]:
        agents = self.list()
        return {
            "registry_version": AGENT_REGISTRY_VERSION,
            "agent_count": len(agents),
            "enabled_count": sum(agent.enabled for agent in agents),
            "disabled_count": sum(not agent.enabled for agent in agents),
            "agents": [agent.to_dict() for agent in agents],
        }


DEFAULT_AGENTS = (
    AgentManifest("critic", "Critic", "Reviews candidate plans for gaps, risks, and unsupported claims.", supported_task_types=("review",), preferred_capabilities=("reasoning.high",)),
    AgentManifest("crypto_keeper", "Crypto Keeper", "Monitors crypto context and produces bounded market/task summaries.", supported_task_types=("research", "monitoring")),
    AgentManifest("image_generator", "Image Generator", "Runs approved local image-generation presets through Forge.", supported_task_types=("image_generate",), preferred_capabilities=("local_image_generation",)),
    AgentManifest("implementer", "Implementer", "Produces concrete implementation proposals within supervisor constraints.", supported_task_types=("implementation",), preferred_capabilities=("coding",)),
    AgentManifest("judge", "Judge", "Reviews candidate outputs and produces bounded synthesis.", supported_task_types=("review", "synthesis"), preferred_capabilities=("reasoning.high",)),
    AgentManifest("manager", "Manager", "Coordinates multi-step work and delegates to specialized logical agents.", supported_task_types=("planning", "coordination"), preferred_capabilities=("reasoning.high",)),
    AgentManifest("media_manager", "Media Manager", "Handles media-library planning and conservative organization workflows.", supported_task_types=("media", "maintenance")),
    AgentManifest("night_owl", "Night Owl", "Processes asynchronous project work and prepares handoffs.", supported_task_types=("automation", "triage", "night_owl"), preferred_capabilities=("reasoning.fast", "tool_use")),
    AgentManifest("planner", "Planner", "Breaks work into safe steps and identifies constraints.", supported_task_types=("planning",), preferred_capabilities=("reasoning.high",)),
    AgentManifest("researcher", "Researcher", "Collects and summarizes evidence with source boundaries.", supported_task_types=("research",), preferred_capabilities=("long_context", "reasoning.fast")),
    AgentManifest("reviewer", "Reviewer", "Reviews repository changes for correctness, security, regressions, and scope.", supported_task_types=("review",), preferred_capabilities=("reasoning.high",)),
    AgentManifest("security_monitor", "Security Monitor", "Reviews system-security posture and reports risks.", supported_task_types=("security", "monitoring"), preferred_capabilities=("reasoning.high",)),
    AgentManifest("verifier", "Verifier", "Checks proposed outcomes against evidence and acceptance criteria.", supported_task_types=("verification",), preferred_capabilities=("reasoning.high",)),
)


def default_registry() -> AgentRegistry:
    return AgentRegistry(DEFAULT_AGENTS)


def load_json_object(text: str) -> dict[str, Any]:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("JSON document must be an object")
    return data
