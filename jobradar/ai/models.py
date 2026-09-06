from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AiModelConfig:
    """User-configured AI backend."""

    name: str
    provider: str
    model: str
    api_key_env: str = ""
    base_url: str = ""
    enabled: bool = True
    supports_web_search: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AiModelConfig":
        return cls(
            name=str(data.get("name", "")).strip(),
            provider=str(data.get("provider", "")).strip().lower(),
            model=str(data.get("model", "")).strip(),
            api_key_env=str(data.get("api_key_env", "")).strip(),
            base_url=str(data.get("base_url", "")).strip(),
            enabled=bool(data.get("enabled", True)),
            supports_web_search=bool(data.get("supports_web_search", str(data.get("provider", "")).strip().lower() == "openai")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "base_url": self.base_url,
            "enabled": self.enabled,
            "supports_web_search": self.supports_web_search,
        }


@dataclass
class EmbeddingConfig:
    """Configuration for semantic memory embeddings."""

    provider: str = "openai"
    model: str = "text-embedding-3-small"
    api_key_env: str = "OPENAI_API_KEY"
    base_url: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EmbeddingConfig":
        data = data or {}
        return cls(
            provider=str(data.get("provider", "openai")).strip().lower(),
            model=str(data.get("model", "text-embedding-3-small")).strip(),
            api_key_env=str(data.get("api_key_env", "OPENAI_API_KEY")).strip(),
            base_url=str(data.get("base_url", "")).strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "base_url": self.base_url,
        }


@dataclass
class AiMemory:
    """Long-term user preference or correction used for job evaluation."""

    id: int | None
    text: str
    category: str = "preference"
    source: str = "auto"
    confidence: float = 1.0
    importance: float = 1.0
    active: bool = True
    created_at: str = ""
    updated_at: str = ""
    embedding_json: str = ""


@dataclass
class AiEvaluationResult:
    """Structured AI output stored in the database."""

    job_id: int
    ai_name: str
    provider: str
    model: str
    summary: str = ""
    rating: str = ""
    score: int | None = None
    decision: str = ""
    raw_response: str = ""
