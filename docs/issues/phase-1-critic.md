# Phase 1 Critic Pass — 2026-08-25

Adversarial self-review over the phase 1 diff (`store`, event chain, blob
spill, timeline/verify/fork, shadow-git gate, bench). 20 flaws ranked by
impact. Top 10 fixed before tagging; the rest filed below.

## Fixed (top 10)

1. **Per-event SQLite connection churn** — every append opened/closed a
   connection (~17ms p50), capping the bus at ~58 events/s; token-heavy runs
   emit thousands of token events. Fixed with thread-local connection reuse.
   Regenerated bench: 57.7 → **329.7 events/s** (5.7×).
2. **Candidate commits captured scratch artifacts** — `.pytest_cache/`,
   `__pycache__/`, `.venv/` would land in candidate diffs and forwarded
   history. Fixed via shadow repo `info/exclude`.
3. **`/timeline` decoded spilled blobs needlessly** — a run with large
   snapshots would transfer megabytes to list hashes. Added
   `Repository.event_timeline()` (metadata only, no blob decode).
4. **Dead conditional in `begin_candidate`** — both branches identical;
   collapsed.
5. **No degraded-mode test** for missing git executable — added
   (gate returns pass-through and emits nothing further).
6. **Demo harness didn't touch phase-1 surfaces** — now creates a run,
   publishes through the bus, checks `/timeline` + `/verify`, gates health
   on chain integrity.
7. **Bench health gate wrong** — required non-empty folded state although
   `log` events are not foldable by design; now gates on replay wall time.
8. **Coverage tooling unpinned** — `coverage`/`pytest-cov` added to dev
   extras so the ≥80% floor is reproducible in CI.
9. **Phase-0 tag never created** by the prior session; CHANGELOG lacked a
   phase-1 entry. Both created at this exit.
10. **Rollback semantics undocumented for manual-check missions** — decided:
    keep candidate on manual-checks (operator reviews), roll back only on
    explicit test failure. Documented here and in ADR-0003.

## Filed — now tracked as GitHub issues

The open items from this pass are tracked issues; this list is the historical
record of where they came from.

| # | Item | Issue |
|---|---|---|
| 11 | Cancelled mid-candidate missions strand the workspace on the candidate branch | [#13](https://github.com/PrasannaKumarPalla/Ultron/issues/13) |
| 12 | `mission_events` mirror re-inlines spilled payloads (>64 KiB double-stored) | [#14](https://github.com/PrasannaKumarPalla/Ultron/issues/14) |
| 13 | `verify_event_chain` loads all rows into memory | [#15](https://github.com/PrasannaKumarPalla/Ultron/issues/15) |
| 14 | Fork failures write no back-reference to the source run | [#16](https://github.com/PrasannaKumarPalla/Ultron/issues/16) |
| 16 | `changed_files()` / `diff_stat()` swallow git errors | [#17](https://github.com/PrasannaKumarPalla/Ultron/issues/17) |
| 17 | `shadow.candidate_opened` emitted for read-only missions | [#18](https://github.com/PrasannaKumarPalla/Ultron/issues/18) |
| 19 | Blob store has no GC / retention | [#19](https://github.com/PrasannaKumarPalla/Ultron/issues/19) |
| 20 | `event_timeline` returns raw ts strings | [#20](https://github.com/PrasannaKumarPalla/Ultron/issues/20) |

Not filed as issues (informational / accepted):

- **15.** SSE payload gained nullable `hash`/`parent_hash`/`blob_ref` fields. The
  UI ignores unknown fields; recorded as an API-surface note for any future
  external consumer.
- **18.** Benign TOCTOU between `BlobStore.has` and `get` — local-only tool,
  single writer.

## Bench evidence (regenerated after fixes)

See `bench/metrics-phase1.json`: inline append p50 2.9ms / 329.7 ev/s,
spilled-100KiB append ~23 ev/s, replay of 510 events 161ms, full chain
verify of 510 events 3.9ms.
