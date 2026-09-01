# Phase 2 Critic Pass — 2026-08-25

Scope: repo_intel, sandbox, speculative search + workflow integration,
bench_tasks. Findings ranked by impact; fixed items marked.

1. **FIXED** — Variant failure handler caught `Exception`, which swallows
   `RunCancelled`/`BudgetExhausted`; a kill switch during a speculative
   variant would be recorded as a bad candidate instead of stopping the run.
   Now re-raised.
2. **[#21](https://github.com/PrasannaKumarPalla/Ultron/issues/21)** — Variants
   execute sequentially over one shared worktree. Parallel fan-out needs
   per-variant worktrees or per-process index files.
3. **[#22](https://github.com/PrasannaKumarPalla/Ultron/issues/22)** — Verifier
   scores from output signals only; no test execution in the scoring loop and no
   LLM-backed scorer yet.
4. **[#23](https://github.com/PrasannaKumarPalla/Ultron/issues/23)** — Crash
   between variant commit and winner-forward strands the workspace on a variant
   branch; next `begin_candidate` self-heals.
5. **[#24](https://github.com/PrasannaKumarPalla/Ultron/issues/24)** — Job Object
   assignment happens post-spawn; a child can theoretically escape in the first
   ms. Needs ctypes `CreateProcess` with `CREATE_SUSPENDED`.
6. **ACCEPTED** — Network egress is NOT sandboxed (ADR-0005); accepted risk.
7. **FIXED** (2026-09-01) — `RepoIntel.churn` is now memoised on the current git
   HEAD, so repeated `/intel` hits don't re-shell `git log`.
8. **FIXED** (2026-09-01) — the `/intel` endpoint's `RepoIntel` cache is evicted
   when its workspace is deleted.
9. **OK** — Bench uses deterministic studios by design (isolates
   orchestration from model latency); Ollama-in-loop benchmarking lands in
   phase 3 warm-pool work.
10. **OK** — Search knobs default beam=1 (behavior-preserving), per-run
    configurable via settings; verified in api wiring.

## Bench evidence

`bench/metrics-phase2.json`: repair-rescued-by-speculation shows baseline
beam=1 FAILED while beam=2 COMPLETED under an identical repair budget;
fan-out overhead 0.9–1.6s per speculative round across fixtures.
