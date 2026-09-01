"""Layered memory: working (summarize-on-evict), episodic (embedded recall),
semantic (distilled lessons), project (Repository.memories).

ADR-0006: episodic vectors come from a local hashing embedder, not
sqlite-vss + an embedding model. Zero deps, deterministic, offline; swap the
Embedder for an Ollama `nomic-embed-text` client later without touching
storage.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

from .db import Repository

EMBED_DIM = 256
_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_-]{2,}")
STOP_WORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "was",
    "are", "not", "but", "has", "had", "were", "will", "would", "there",
}


def _tokens(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text)
            if token.lower() not in STOP_WORDS]


class HashEmbedder:
    """Deterministic bag-of-token hashing embedding, L2-normalized."""

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * EMBED_DIM
        for token in _tokens(text):
            digest = hashlib.md5(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % EMBED_DIM
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return vector


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def summarize(text: str, max_chars: int = 280) -> str:
    """Extractive summary: lead sentence plus the most keyword-dense one."""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?\n])\s+", text.strip()) if part.strip()]
    if not sentences:
        return ""
    if len(sentences) == 1:
        return sentences[0][:max_chars]
    keywords = {token for token in _tokens(text)}
    best = max(sentences[1:], key=lambda sentence: len(set(_tokens(sentence)) & keywords))
    joined = f"{sentences[0]} {best}"
    return joined[:max_chars]


class WorkingMemory:
    """Bounded recent-turns buffer; evicted halves collapse into a summary."""

    def __init__(self, cap_items: int = 12, summarizer=summarize):
        if cap_items < 2:
            raise ValueError("cap_items must be >= 2")
        self.cap_items = cap_items
        self.summarizer = summarizer
        self._items: list[dict] = []
        self._rolling_summary = ""

    def add(self, role: str, text: str) -> None:
        self._items.append({"role": role, "text": text})
        if len(self._items) > self.cap_items:
            evicted = self._items[:len(self._items) - self.cap_items // 2]
            self._items = self._items[len(self._items) - self.cap_items // 2:]
            digest = self.summarizer(" ".join(item["text"] for item in evicted))
            self._rolling_summary = summarize(f"{self._rolling_summary} {digest}".strip())

    def context(self) -> str:
        parts = ([f"earlier: {self._rolling_summary}"] if self._rolling_summary else [])
        parts.extend(f"{item['role']}: {item['text']}" for item in self._items)
        return "\n".join(parts)

    def __len__(self) -> int:
        return len(self._items)


class LayeredMemory:
    def __init__(self, repo: Repository, embedder: HashEmbedder | None = None):
        self.repo = repo
        self.embedder = embedder or HashEmbedder()

    def observe(self, project_id: str, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        return self.repo.add_episodic(project_id, text, self.embedder.embed(text))

    def recall(self, project_id: str, query: str, limit: int = 5) -> list[dict]:
        query_vector = self.embedder.embed(query)
        scored = []
        for row in self.repo.episodic_rows(project_id, consolidated=False):
            score = cosine(query_vector, row["embedding"])
            scored.append((score, row))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [{"text": row["text"], "score": round(score, 4),
                 "id": row["id"], "created_at": row["created_at"]}
                for score, row in scored[:limit] if score > 0]

    def lessons(self, project_id: str) -> list[dict]:
        return self.repo.lessons(project_id)

    def consolidate(self, project_id: str | None = None, min_mentions: int = 3,
                    max_lessons: int = 20) -> list[dict]:
        """Nightly job: distill recurring themes from unconsolidated episodes
        into deduplicated semantic lessons, then mark episodes consolidated."""
        if project_id is None:
            with self.repo.connect() as db:
                rows = db.execute("SELECT DISTINCT project_id FROM episodic_memories").fetchall()
            projects = [row["project_id"] for row in rows]
        else:
            projects = [project_id]

        created: list[dict] = []
        for pid in projects:
            pending = self.repo.episodic_rows(pid, consolidated=False)
            if len(pending) < min_mentions:
                continue
            counter: Counter[str] = Counter()
            sources: dict[str, list[str]] = {}
            for row in pending:
                for token in set(_tokens(row["text"])):
                    counter[token] += 1
                    sources.setdefault(token, []).append(row["text"])
            for token, count in counter.most_common(max_lessons):
                if count < min_mentions:
                    break
                example = sources[token][0][:160]
                lesson = f"Recurring theme '{token}' across {count} episodes; e.g. {example}"
                if self.repo.add_lesson(pid, lesson, source_count=count):
                    created.append({"project_id": pid, "lesson": lesson, "count": count})
            self.repo.mark_episodic_consolidated([row["id"] for row in pending])
        return created

