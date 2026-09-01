# Contributing to Ultron

Thanks for helping. Ultron is a local-first Windows app; most contributions are
bug fixes and small, focused improvements.

## Ground rules

- **One change per PR.** Every changed line should trace to the PR's stated goal.
  No drive-by refactors, formatting sweeps, or unrelated fixes.
- **Match the surrounding code.** Style, naming, comment density. No comments
  unless the *why* is non-obvious.
- **Tests.** New behaviour needs a test in `tests/test_<module>.py` mirroring
  `src/ultron/<module>.py`. Bug fixes need a test that fails before and passes after.
- **Keep it local-first.** No feature may add a hosted-provider call outside the
  OmniRoute sidecar. Failover between providers must stay visible
  (`provider.switched` events), never silent.
- No backward-compatibility shims. Delete obsolete paths rather than branching.

## Workflow

1. Fork, branch from `main` (short-lived, one topic).
2. `python -m pytest -v` green locally. `dist/Ultron.exe` still builds if you
   touched packaging.
3. Open a PR into `main`. CI (pytest + Semgrep + TruffleHog) must pass.
4. A maintainer reviews and merges. Only maintainers merge; squash-merge, linear
   history.

## Developer Certificate of Origin (DCO)

Every commit must be signed off, certifying you wrote the change or have the right
to submit it under the project licence:

```
git commit -s -m "fix: ..."
```

This appends `Signed-off-by: Your Name <you@example.com>`. PRs without a
sign-off on every commit are blocked by the DCO check.

## Licence

Ultron is **AGPL-3.0**. Contributions are accepted under the same licence and the
DCO above. There is no CLA. Do not change `LICENSE`, `.github/`, `installer/`, or
the release workflows in a PR - those are maintainer-owned (see `.github/CODEOWNERS`).

## Security

Do not open a public issue for vulnerabilities. See [SECURITY.md](.github/SECURITY.md).
