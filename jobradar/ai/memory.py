from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Iterable

from .clients import create_embedding_client
from .models import AiMemory, EmbeddingConfig


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def embedding_to_json(values: list[float]) -> str:
    return json.dumps(values, separators=(",", ":"))


def embedding_from_json(value: str) -> list[float]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [float(x) for x in parsed]


class AiMemoryService:
    """Stores and retrieves semantic user memories.

    The database owns persistence. This service owns embeddings and similarity
    search. It does not touch Tkinter widgets.
    """

    def __init__(self, db, embedding_config: EmbeddingConfig) -> None:
        self.db = db
        self.embedding_config = embedding_config

    def embed_text(self, text: str) -> list[float]:
        client = create_embedding_client(self.embedding_config)
        return client.embed_text(text)

    def add_memory(
        self,
        text: str,
        category: str = "preference",
        source: str = "auto",
        confidence: float = 1.0,
        importance: float = 1.0,
    ) -> int:
        text = text.strip()
        if not text:
            raise ValueError("Memory text is empty.")
        embedding = self.embed_text(text)
        return self.db.add_ai_memory(
            text=text,
            category=category,
            source=source,
            confidence=confidence,
            importance=importance,
            embedding_json=embedding_to_json(embedding),
        )

    def retrieve_relevant_memories(self, query_text: str, limit: int = 10) -> list[tuple[AiMemory, float]]:
        query_embedding = self.embed_text(query_text)
        memories = self.db.list_ai_memories(active_only=True)
        scored: list[tuple[AiMemory, float]] = []
        for memory in memories:
            memory_embedding = embedding_from_json(memory.embedding_json)
            score = cosine_similarity(query_embedding, memory_embedding)
            if score > 0.0:
                scored.append((memory, score))
        scored.sort(key=lambda item: (item[1] * max(item[0].importance, 0.1)), reverse=True)
        return scored[:limit]

    def rebuild_missing_embeddings(self) -> int:
        count = 0
        for memory in self.db.list_ai_memories(active_only=False):
            if memory.embedding_json:
                continue
            embedding = self.embed_text(memory.text)
            self.db.update_ai_memory_embedding(memory.id, embedding_to_json(embedding))
            count += 1
        return count


def default_profile_path() -> Path:
    return Path(__file__).resolve().parents[1] / "profile.md"


def load_profile_text(profile_path: str | Path | None = None) -> str:
    if profile_path:
        path = Path(profile_path).expanduser()
        if not path.is_absolute():
            package_root = Path(__file__).resolve().parents[1]
            candidates = [
                Path.cwd() / path,
                package_root / path,
                package_root.parent / path,
            ]
            for candidate in candidates:
                if candidate.exists():
                    return candidate.read_text(encoding="utf-8")
            return ""
    else:
        path = default_profile_path()
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")
