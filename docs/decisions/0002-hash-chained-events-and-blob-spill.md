# ADR-0002: Hash-chained events with blob spill beside SQLite

Status: ACCEPTED · Phase 1

## Context

Target G1 requires an append-only, tamper-evident event store. Every LLM call,
tool call, file op, and state mutation is already an event in the `events`
table, but payloads live inline as TEXT with no integrity guarantee and no
size discipline. Node outputs (workspace snapshots, test evidence) can reach
hundreds of KiB.

## Decision

1. Each row in `events` gains three additive columns: `hash`, `parent_hash`,
   `blob_ref` (nullable TEXT). Old databases migrate via `ALTER TABLE`; the
   API stays compatible.
2. The per-run event chain is a hash chain:
   `hash = sha256(parent_hash || canonical_json(run_id, agent, kind,
   payload_bytes, ts))`, genesis `parent_hash = ""`. Verification walks the
   chain (`Repository.verify_event_chain`) and reports the first break.
   Writes use `BEGIN IMMEDIATE` so the parent lookup and insert are one
   transaction.
3. Payloads larger than 64 KiB spill to the content-addressed blob store
   (`store.BlobStore`, flat files under `<db dir>/blobs/aa/<sha256>`). The
   row stores `{"blob": <sha>, "size": n, "preview": <512 chars>}` and the
   sha in `blob_ref`. Readers get transparent inlining from
   `Repository.run_events` via `BlobStore.resolve`.
4. The blob store is plain files, not a database. Append-only means no
   refcounting or GC; disk cost equals content written.

## Alternatives rejected

- **sqlite-vss / LMDB / new storage engine**: extra deps for capability we
  don't need yet; blobs-as-files is rip-out-friendly (ADR policy: fewer deps).
- **Hashing over the whole table (Merkle per run)**: no cross-run tamper
  evidence needed for a single-operator local tool; per-run chains give
  replay verification at zero coupling between runs.
- **Spilling to the same SQLite file**: keeps one store but couples blob
  lifetime to row deletes and grows WAL pressure on token-heavy runs.

## Consequences

- Crash-resume and time-travel can prove they rehydrate exactly the recorded
  past (chain verify over prefix).
- Event consumers must treat payloads as resolved-on-read; previews bound UI
  damage if a blob file is deleted manually.
- Concurrent appends to one run serialize on the write lock — acceptable at
  single-operator throughput (bench/metrics-phase1.json records the number).
