# ADR-0001: Content-addressed blob store beside SQLite, not a new database

Date: 2026-08-25 · Status: ACCEPTED · Phase: 1

## Context

Phase 1 requires every LLM/tool/file/shell/state mutation to be an immutable,
hash-addressed event with payloads that may exceed SQLite's comfortable row
size (workspace snapshots, model outputs can be hundreds of KiB). Candidates:

1. Keep everything in SQLite as BLOBs.
2. Add a real document store / vector DB now.
3. Flat content-addressed blob files under `data/blobs/<aa>/<hash>` + hash
   columns in the existing `events` table.

## Decision

Option 3. Events stay in SQLite (index, ordering, queries); payload bodies
over a 64 KiB threshold are stored once as flat files addressed by SHA-256.
Small payloads stay inline to avoid syscall overhead for the common case.

## Reasoning (decision policy order)

- More local: pure filesystem + existing SQLite. No service, no daemon.
- Faster at runtime: streaming large payloads from disk avoids fat rows in
  hot query paths (`run_events`, SSE replay); dedup is free via hash naming.
- Fewer deps: zero new dependencies vs sqlite-vss/LanceDB/etc.
- Easier to rip out: blobs are inert files; deleting `store.py` and the three
  new columns restores today's shape exactly.

## Consequences

- Blob GC is manual (per-run prune) until phase 3's consolidation job adopts it.
- Crash between DB commit and blob write is impossible by ordering: blob
  write happens first, then the row referencing it. Orphaned blobs are
  harmless and swept by GC.
- Windows file locking: blobs are opened read-only, never renamed in place.
