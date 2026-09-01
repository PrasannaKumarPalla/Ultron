# 0010 — OmniRoute hosted-model bridge

**Status:** ACCEPTED — 2026-08-29, operator ratified after reviewing the
upstream repo (`diegosouzapw/OmniRoute`, MIT, local-first, keyless free tiers).
Default router mode set to `auto` (prefer OmniRoute when the sidecar is
reachable, fall back to local Ollama, every switch emits `provider.switched`).
Bujji Control Core routes through the same sidecar in `auto`/`hosted` mode.

`constraints.md` still says "Ollama-only" — that line is now superseded by this
record for Ultron. B.U.J.J.I as a standalone product is moot (absorbed).

## Context

`constraints.md` states: "Neither B.U.J.J.I nor Ultron makes a cloud model
call. Ultron is Ollama-only. Adding a hosted-model code path to either is a
product decision requiring a decision record, never a convenience edit."

The OmniRoute integration (sidecar, `provider_router`, `catalog`, `redaction`,
consent gates, dashboard, ~1000 LOC + tests) adds exactly such a path: an
OpenAI-compatible sidecar on `localhost:20128` that routes prompts to
third-party free-tier providers. It shipped on the feature branch with no
decision record and, until 2026-08-29, never started successfully on this
machine (Docker daemon down, no npm fallback, wrong health paths, wrong serve
command — all now fixed).

## Decision

OPEN QUESTION — operator to resolve:

1. **Is a hosted bridge acceptable at all?** It contradicts the current
   written constraint and the "local-first is a product constraint" line.
2. **If yes:** default router mode. Currently persisted as `hosted` in the DB;
   `auto` (prefer hosted when healthy, fall back to Ollama) or `local` (opt-in
   only) are safer defaults.
3. **Free-tier privacy.** Free upstreams may train on inputs. The consent
   banner exists per-repo; is that sufficient, or does hosted stay off until
   explicitly enabled per workspace?
4. **Does Bujji get the same bridge?** (Requested 2026-08-29.) Currently
   `bujji_bridge.py` is hardcoded to Ollama and the embedded Bujji Core has its
   own model UI. Routing it through OmniRoute is net-new hosted surface in the
   absorbed subsystem — blocked on this record.

## Consequences

Until ratified: OmniRoute code stays in place (now functional) but the default
should be flipped to `local` or `auto`, not `hosted`, and Bujji stays
Ollama-only.
