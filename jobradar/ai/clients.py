from __future__ import annotations

import os
import time
from email.utils import parsedate_to_datetime
from typing import Any
from dataclasses import dataclass


@dataclass
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, int | None]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
        }



import requests

from .models import AiModelConfig, EmbeddingConfig


def _read_api_key(env_name: str) -> str:
    env_name = (env_name or "").strip()
    if not env_name:
        return ""
    return os.getenv(env_name, "").strip()


class AiClientBase:
    """Common provider interface for chat/evaluation calls."""

    def __init__(self, config: AiModelConfig) -> None:
        self.config = config
        self.last_usage = TokenUsage()

    def _set_usage(self, data: dict[str, Any], provider: str) -> None:
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            print(f"[AI usage debug] provider={provider}: usage unavailable")
            self.last_usage = TokenUsage()
            return
        def number(*names):
            for name in names:
                value = usage.get(name)
                if isinstance(value, (int, float)):
                    return int(value)
            return None
        inp = number("input_tokens", "prompt_tokens")
        out = number("output_tokens", "completion_tokens")
        cached = number("cached_tokens")
        details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details")
        if cached is None and isinstance(details, dict):
            value = details.get("cached_tokens")
            if isinstance(value, (int, float)):
                cached = int(value)
        self.last_usage = TokenUsage(inp, out, cached)
        if inp is None or out is None:
            print(f"[AI usage debug] provider={provider}: keys={sorted(usage.keys())}")

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        raise NotImplementedError

    def _api_key(self) -> str:
        key = _read_api_key(self.config.api_key_env)
        if not key and self.config.provider.lower().strip() != "ollama":
            raise RuntimeError(f"API key not found in environment variable: {self.config.api_key_env}")
        return key


class OpenAIClient(AiClientBase):
    @staticmethod
    def _retry_after_seconds(response: requests.Response, fallback: float) -> float:
        value = str(response.headers.get("Retry-After") or "").strip()
        if value:
            try:
                return max(1.0, float(value))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(value)
                    return max(1.0, retry_at.timestamp() - time.time())
                except Exception:
                    pass
        return max(1.0, fallback)

    @staticmethod
    def _openai_error_details(response: requests.Response) -> tuple[str, str, str]:
        try:
            payload = response.json()
        except Exception:
            return "", "", response.text[:1000].strip()
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        if not isinstance(error, dict):
            return "", "", str(error)
        return (
            str(error.get("type") or "").strip(),
            str(error.get("code") or "").strip(),
            str(error.get("message") or "").strip(),
        )

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        base = (self.config.base_url.strip() or "https://api.openai.com/v1/responses").rstrip("/")
        url = base if base.endswith("/responses") else base + "/responses"
        payload: dict[str, Any] = {
            "model": self.config.model,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self.config.supports_web_search:
            payload["tools"] = [{"type": "web_search"}]

        # A paid API account still has request/token rate limits. 429 responses
        # caused by a temporary limit are retried with progressively longer
        # pauses. Quota/billing errors are not retried because waiting cannot
        # resolve them.
        fallback_delays = (5.0, 15.0, 30.0, 60.0)
        response: requests.Response | None = None
        for attempt in range(len(fallback_delays) + 1):
            response = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._api_key()}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=180,
            )
            if response.status_code != 429:
                break

            error_type, error_code, error_message = self._openai_error_details(response)
            quota_error = error_code == "insufficient_quota" or error_type == "insufficient_quota"
            if quota_error:
                raise RuntimeError(
                    "OpenAI API quota/billing limit reached (HTTP 429, insufficient_quota). "
                    "Waiting will not fix this. Check API billing, project budget and usage limits. "
                    + (f"OpenAI message: {error_message}" if error_message else "")
                )
            if attempt >= len(fallback_delays):
                raise RuntimeError(
                    "OpenAI temporary rate limit still active after automatic waits "
                    "of up to about 110 seconds. Try the failed jobs again later or reduce "
                    "the number of analyses per agent run. "
                    + (f"OpenAI message: {error_message}" if error_message else "")
                )
            delay = self._retry_after_seconds(response, fallback_delays[attempt])
            # Avoid pathological server values while still honoring useful
            # Retry-After headers.
            time.sleep(min(max(delay, fallback_delays[attempt]), 120.0))

        assert response is not None
        if response.status_code >= 400:
            error_type, error_code, error_message = self._openai_error_details(response)
            detail = ", ".join(part for part in (error_type, error_code) if part)
            suffix = f" ({detail})" if detail else ""
            if error_message:
                raise RuntimeError(f"OpenAI API error HTTP {response.status_code}{suffix}: {error_message}")
            response.raise_for_status()

        data: dict[str, Any] = response.json()
        self._set_usage(data, "openai")
        if isinstance(data.get("output_text"), str):
            return data["output_text"]
        # Robust fallback for Responses API output items.
        parts: list[str] = []
        for item in data.get("output", []) or []:
            for content in item.get("content", []) or []:
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts).strip()


class MistralClient(AiClientBase):
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        base = (self.config.base_url.strip() or "https://api.mistral.ai/v1/chat/completions").rstrip("/")
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self._api_key()}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
            },
            timeout=180,
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        self._set_usage(data, "mistral")
        return str(data["choices"][0]["message"]["content"])


class OllamaClient(AiClientBase):
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        base = (self.config.base_url.strip() or "http://127.0.0.1:11434/v1/chat/completions").rstrip("/")
        url = base if base.endswith("/chat/completions") else base + "/v1/chat/completions"
        response = requests.post(
            url,
            json={
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
            },
            timeout=240,
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        self._set_usage(data, "ollama")
        return str(data["choices"][0]["message"]["content"])


def create_ai_client(config: AiModelConfig) -> AiClientBase:
    provider = config.provider.lower().strip()
    if provider == "openai":
        return OpenAIClient(config)
    if provider == "mistral":
        return MistralClient(config)
    if provider == "ollama":
        return OllamaClient(config)
    raise ValueError(f"Unknown AI provider: {config.provider}")


class EmbeddingClientBase:
    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config

    def embed_text(self, text: str) -> list[float]:
        raise NotImplementedError

    def _api_key(self) -> str:
        key = os.getenv(self.config.api_key_env.strip()) if self.config.api_key_env else ""
        if not key:
            raise RuntimeError(f"API key not found in environment variable: {self.config.api_key_env}")
        return key


class OpenAIEmbeddingClient(EmbeddingClientBase):
    def embed_text(self, text: str) -> list[float]:
        url = (self.config.base_url.strip() or "https://api.openai.com/v1").rstrip("/") + "/embeddings"
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self._api_key()}",
                "Content-Type": "application/json",
            },
            json={"model": self.config.model, "input": text},
            timeout=60,
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return [float(x) for x in data["data"][0]["embedding"]]


class MistralEmbeddingClient(EmbeddingClientBase):
    def embed_text(self, text: str) -> list[float]:
        url = (self.config.base_url.strip() or "https://api.mistral.ai/v1").rstrip("/") + "/embeddings"
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self._api_key()}",
                "Content-Type": "application/json",
            },
            json={"model": self.config.model, "input": [text]},
            timeout=60,
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return [float(x) for x in data["data"][0]["embedding"]]


class OllamaEmbeddingClient(EmbeddingClientBase):
    def embed_text(self, text: str) -> list[float]:
        base_url = (self.config.base_url.strip() or "http://localhost:11434").rstrip("/")
        response = requests.post(
            base_url + "/api/embed",
            json={"model": self.config.model, "input": text},
            timeout=120,
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        if "embeddings" in data:
            return [float(x) for x in data["embeddings"][0]]
        if "embedding" in data:
            return [float(x) for x in data["embedding"]]
        raise RuntimeError("Ollama embedding response did not contain embeddings.")


def create_embedding_client(config: EmbeddingConfig) -> EmbeddingClientBase:
    provider = config.provider.lower().strip()
    if provider == "openai":
        return OpenAIEmbeddingClient(config)
    if provider == "mistral":
        return MistralEmbeddingClient(config)
    if provider == "ollama":
        return OllamaEmbeddingClient(config)
    raise ValueError(f"Unknown embedding provider: {config.provider}")
