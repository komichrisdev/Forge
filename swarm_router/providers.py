from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .client import OpenWebUIClient


@dataclass(frozen=True)
class ProviderModel:
    provider_id: str
    model_id: str
    display_name: str
    raw: dict[str, object]


class ProviderInventory(Protocol):
    provider_id: str

    def list_models(self) -> list[ProviderModel]:
        ...


class OpenAICompatibleProvider:
    def __init__(self, provider_id: str, client: OpenWebUIClient) -> None:
        self.provider_id = provider_id
        self.client = client

    def list_models(self) -> list[ProviderModel]:
        models: list[ProviderModel] = []
        for item in self.client.list_model_entries():
            model_id = str(item.get("id") or item.get("name") or "").strip()
            if not model_id:
                continue
            raw: dict[str, object] = dict(item)
            raw.setdefault("provider", model_id.split("/", 1)[0] if "/" in model_id else self.provider_id)
            models.append(
                ProviderModel(
                    provider_id=self.provider_id,
                    model_id=model_id,
                    display_name=str(item.get("name") or model_id),
                    raw=raw,
                )
            )
        return models


def provider_items(models: list[ProviderModel]) -> list[dict[str, object]]:
    return [
        {
            **model.raw,
            "id": model.model_id,
            "name": model.display_name,
            "provider_id": model.provider_id,
        }
        for model in models
    ]
