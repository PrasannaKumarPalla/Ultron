# OmniRoute Integration

Ultron hires agents via OmniRoute when online, from its free-tier pool. Ollama
remains the default when offline or when local mode is pinned. Failover is
automatic and always observable (`provider.switched` events).

## Architecture

- **Sidecar** (`src/ultron/sidecar.py`): Ultron's lifecycle owns the OmniRoute
  process — Docker image `diegosouzapw/omniroute` preferred, npm package
  `omniroute` fallback. Health-checked with backoff against `/health`
  then `/v1/models`; crashes auto-restart with a cap of 3.
- **Config**: `%APPDATA%/Ultron/omniroute.yaml` maps providers, combos,
  quota-share policy and compression.
- **Providers** (`src/ultron/llm_providers.py`): `chat(messages, model, tools,
  format, stream) -> AsyncIterator[ChatEvent]`. `OmniRouteProvider` speaks
  OpenAI-compatible SSE to the sidecar with `model: "auto"` by default;
  tool calls, JSON-schema mode and streaming pass through.
- **Router** (`src/ultron/provider_router.py`): modes `{local, hosted, auto}`
  (default auto). Auto prefers hosted when the sidecar is healthy and the user
  has not pinned local; falls back on sidecar-down, 5xx, quota (429) or
  timeout — every switch emits a switch event.
- **Catalog** (`src/ultron/catalog.py`): merges `/v1/models` with `ollama list`,
  cached in SQLite (`model_catalog`), refreshed every 6h (and at boot). Roles
  hire by capability profile; nameplate badges look like
  `[OmniRoute · Groq · llama-3.3-70b · FREE]`.
- **Privacy** (`src/ultron/redaction.py`): secrets are redacted before every
  hosted send (JWTs, PEM blocks, key assignments, connection strings…);
  deny-list extendable at `data/omniroute/extra_patterns.txt`.
  Provider keys live under `data/omniroute/`, are never
  logged and only ever sent to localhost:20128. Privacy Mode stops the sidecar,
  disables the catalog and forces local-only.

## HTTP surface (`/api/omniroute/*`)

| Route | Purpose |
| --- | --- |
| `GET /status` | sidecar LED, mode, restarts, cooldowns, free-tier signals |
| `POST /install` · `/start` · `/stop` | first-run wizard + lifecycle |
| `GET/PUT /config` | omniroute.yaml surface |
| `GET /models` | merged catalog + badges + availability lights |
| `POST /hire` | resolve capability profile → agent badge |
| `PUT /router/mode` | pin `local` / `hosted` / `auto` |
| `POST /privacy` | privacy-mode toggle |
| `POST/GET /hosted/consent` | per-repo first-hosted-call banner |
| `POST /redaction/dry-run` | preview exactly what would leave |
| `GET /dashboard` | hosted vs local split, upstream mix, savings, cost |
| `GET /switches` | recent provider handoffs |
| `POST /costs/acknowledge` | clear the cost-pause after review |

## Bench

`bench/bench_omniroute.py` runs the same task across the top free upstreams
plus local Ollama and commits `bench/omniroute-vs-local.json`; the router uses
its scores to rank picks.
