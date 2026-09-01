# ADR-0004: stdlib ast for repo intelligence (tree-sitter deferred)

Status: ACCEPTED · Phase 2

## Context

Target G5 calls for tree-sitter parsing with symbol/call/import graphs.
tree-sitter means a C extension plus per-language grammar packages
(`tree-sitter>=0.21`, `tree-sitter-python`, ...) compiled into the
PyInstaller bundle.

## Decision

Build the graphs on Python's stdlib `ast` module:

- Symbols: qualified class/function defs with line ranges.
- Imports: absolute + from-imports; internal-vs-external edge classification.
- Calls: caller→callee name pairs collected inside function bodies.
- Churn/hotspots: local `git log --name-only` counts.

Incremental: per-file cache keyed by `(mtime, size)`; `invalidate()` on save.

## Alternatives rejected

- **tree-sitter now**: multi-language fidelity we don't need yet — every
  mission workspace so far is Python-first; costs a C toolchain in the exe,
  new pinned deps, and grammar version churn. Revisit when a non-Python
  workspace actually needs precise parsing; module boundary (`analyze_file`)
  makes that swap local.

## Consequences

- Call graph resolves names only, not types (no cross-file binding). Good
  enough for edit planning context; not a compiler.
- Zero new deps; PyInstaller stays single-file friendly.
