# Hybrid local+cloud paradigm agents

Six paradigms ported from the original ``hybrid-local-cloud-compute``
harness â€” each is registered as a standard Bujji agent so the rest
of the platform (SDK, CLI, distillation, evals) can use them like any
other agent. Results live under ``$BUJJI_HYBRID_EXPERIMENTS_DIR``
(defaults to ``~/.bujji/experiments/hybrid/``).

| Agent             | Plan shape      | Trains what?         | Workers                   |
|-------------------|-----------------|----------------------|---------------------------|
| `minions`         | reactive loop   | nothing              | 1 local + 1 cloud         |
| `conductor`       | static DAG      | (paper: 7B planner)  | up to 5 frontier+open     |
| `archon`          | static recipe   | nothing (search)     | K local + cloud rank/fuse |
| `advisors`        | reactive loop   | (paper: local model) | 1 local + 1 cloud         |
| `skillorchestra`  | per-query pick  | (paper: profiler)    | 1 local + 1 cloud         |
| `toolorchestra`   | reactive loop   | (paper: 8B RL)       | local + tools + LLM pool  |

Items in parentheses are what the *paper* trains. These Bujji ports
are **inference-only** â€” none modify weights. The trained variants (advisor
RL, Orchestrator-8B, SkillOrchestra learn-phase) stay TODOs; the prompted
lower-bounds get you 80-90% of the headline accuracy at zero training cost.

## What's where

```
src/bujji/agents/hybrid/
â”œâ”€â”€ _base.py          LocalCloudAgent ABC + SDK helpers
â”œâ”€â”€ _prices.py        cloud-model pricing + temp-strip quirks
â”œâ”€â”€ _prompts.py       GAIA / SWE-bench answer-format instructions
â”œâ”€â”€ advisors.py       executor â†” advisor â†” executor (3-step)
â”œâ”€â”€ conductor.py      static DAG planner
â”œâ”€â”€ minions.py        HazyResearch Minions wrapper
â”œâ”€â”€ archon.py         Archon (generator â†’ ranker â†’ fuser)
â”œâ”€â”€ skillorchestra.py skill-aware router
â”œâ”€â”€ toolorchestra.py  prompted multi-turn tool dispatcher
â”œâ”€â”€ runner.py         CLI: python -m ...hybrid.runner --cell NAME
â”œâ”€â”€ registry/         <method>.toml â€” one cell per (bench, local, cloud, N)
â””â”€â”€ scripts/
    â””â”€â”€ new_experiment.sh   scaffold a new cell, run instructions
```

The Modal-backed SWE-bench-Verified scorer is in
`src/bujji/evals/scorers/swebench_harness.py` (next to the existing
structural scorer).

## Quickstart

```bash
cd Bujji
source .env                                           # API keys

# 1. Start vLLM in another shell (see your local launch recipe)
#    CUDA_VISIBLE_DEVICES=0 .venv/bin/python -m vllm.entrypoints.openai.api_server \
#       --model Qwen/Qwen3.5-27B-FP8 --port 8001 ...

# 2. (Optional) for Minions: install the upstream library
.venv/bin/uv pip install -e path/to/minions

# 3. Run a smoke cell
.venv/bin/python -m bujji.agents.hybrid.runner \
    --cell minions-gaia-qwen27b-opus-3
```

Outputs land in
`$BUJJI_HYBRID_EXPERIMENTS_DIR/runs/<cell>/{results.jsonl,summary.json,config.json,logs/}`
(defaults to `~/.bujji-hybrid/experiments/`). The schema matches the
hybrid harness so the existing rescore / dashboard scripts work
unmodified.

## Adding a cell

```bash
src/bujji/agents/hybrid/scripts/new_experiment.sh \
    --method conductor --bench gaia \
    --local qwen3.5-27b --cloud claude-opus-4-7 --n 30
```

That appends a `[cells.<name>]` block to
`registry/conductor.toml` and prints the runner invocation.

## How good is each paradigm?

Numbers from the upstream hybrid harness
(`~/.bujji/experiments/hybrid/docs/results.md`) at full N â€”
GAIA val n=165, SWE-bench-Verified n=500. Local = Qwen-3.5-27B-FP8, cloud
= Opus 4.7. Cloud-only baseline: GAIA 0.570 / $1.09, SWE 0.238 / $0.95.

| paradigm           | shape                | GAIA acc / $    | SWE acc / $     | verdict                                          |
|--------------------|----------------------|-----------------|-----------------|--------------------------------------------------|
| **minions**        | reactive 1L+1C loop  | 0.576 / $0.67   | 0.276 / $0.09   | **keep** â€” matches cloud, ~10Ã— cheaper on SWE    |
| **skillorchestra** | per-query router     | 0.570 / $0.02   | 0.298 / $0.05   | **keep** â€” cloud-tier acc at 1/50Ã— cost          |
| conductor          | static DAG planner   | 0.503 / $0.03   | 0.296 / $0.07   | mixed â€” wins SWE, âˆ’7pp on GAIA                   |
| advisors           | exec â†” advisor loop  | 0.497 / $0.02   | 0.302 / $0.07   | mixed â€” wins SWE, âˆ’7pp on GAIA                   |
| archon             | gen â†’ rank â†’ fuse    | 0.376 / $0.14   | 0.288 / $0.17   | dominated on GAIA (âˆ’19pp); only mid on SWE       |
| toolorchestra      | RL'd 8B + tool pool  | â€”               | â€”               | port lands but untested at full N (heavy infra)  |

Cell configs in `registry/` are copies of the hybrid harness's
`experiments/registry/` â€” same models, same N, same `method_cfg` â€” so
these Bujji cells should reproduce the harness numbers within
noise. Until that's validated, the harness stays the authoritative
reference.
