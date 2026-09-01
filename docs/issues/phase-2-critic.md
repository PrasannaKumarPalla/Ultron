# Phase 2 Critic Pass — 2026-08-25

Scope: repo_intel, sandbox, speculative search + workflow integration,
bench_tasks. Findings ranked by impact; fixed items marked.

1. **FIXED** — Variant failure handler caught `Exception`, which swallows
   `RunCancelled`/`BudgetExhausted`; a kill switch during a speculative
   variant would be recorded as a bad candidate instead of stopping the run.
   Now re-raised.
2. **FILED** — Variants execute sequentially over one shared worktree. True
   parallel fan-out needs per-variant worktrees (`git worktree add`) or
   per-process index files; deferred until model latency makes the win real.
3. **FILED** — Verifier scores from output signals only; no test execution
   inside the scoring loop and no LLM-backed scorer yet (planned phase 3).
4. **FILED** — Crash between variant commit and winner forward strands the
   workspace on a variant branch; next `begin_candidate` self-heals.
5. **FILED** — Job Object assignment happens post-spawn (subprocess gives no
   suspended handle); a child can theoretically escape in the first ms.
   Needs ctypes CreateProcess with CREATE_SUSPENDED.
6. **FILED** — Network egress is NOT sandboxed (ADR-0005); accepted risk.
7. **FILED** — `RepoIntel.churn` shells out to git per call; hotspots should
   cache per mtime like file parses.
8. **FILED** — Intel endpoint cache never evicts closed projects.
9. **OK** — Bench uses deterministic studios by design (isolates
   orchestration from model latency); Ollama-in-loop benchmarking lands in
   phase 3 warm-pool work.
10. **OK** — Search knobs default beam=1 (behavior-preserving), per-run
    configurable via settings; verified in api wiring.

## Bench evidence

`bench/metrics-phase2.json`: repair-rescued-by-speculation shows baseline
beam=1 FAILED while beam=2 COMPLETED under an identical repair budget;
fan-out overhead 0.9–1.6s per speculative round across fixtures.
