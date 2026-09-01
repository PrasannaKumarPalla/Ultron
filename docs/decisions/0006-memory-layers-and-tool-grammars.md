# ADR-0006: hashing embedder instead of sqlite-vss for episodic memory

Status: ACCEPTED · Phase 3

## Context

Target G9 specifies episodic memory over "sqlite-vss + local embed model".
sqlite-vss is an unmaintained extension with platform-specific loading pain
inside PyInstaller; embedding models add GPU/RAM cost and a cold-start hit.

## Decision

`memory_layers.HashEmbedder`: deterministic 256-dim bag-of-token hashing
embedding (md5 bucket + sign, L2-normalized), cosine ranking in Python over
SQLite-stored vectors. Swap-in point is the `Embedder` protocol; an Ollama
`nomic-embed-text` client can replace it without storage changes.

Layers:
- working: `WorkingMemory`, summarize-on-evict (extractive, stdlib).
- episodic: `episodic_memories` table, `LayeredMemory.recall`.
- semantic: nightly consolidation distills recurring themes (>=3 mentions)
  into deduplicated `semantic_lessons`; episodes flagged consolidated.
- project: existing `memories` table stays the source of truth.

## Consequences

Hash embeddings are lexical, not semantic — synonyms do not match, and
sparse false positives from bucket collisions exist. Ranking tests assert
ordering, not absolute exclusion. Recall quality is sufficient for prompt
context injection; revisit vss/model embeddings when missions demonstrably
suffer.
