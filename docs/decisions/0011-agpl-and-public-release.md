# 0011 - AGPL-3.0 and public release

- **Date:** 2026-09-01
- **Status:** accepted
- **Context:** Ultron moves from a private repo under an employer-linked org to a
  public repo on the author's personal account (`PrasannaKumarPalla/Ultron`). It
  needs a licence and a contribution model.
- **Decision:**
  - Licence: **AGPL-3.0** (was MIT). A distributed or network-served modification
    must publish its source under the same terms. The author holds 100% of the
    code and can grant commercial exceptions separately if ever needed.
  - Contribution model: fork + PR into `main`; issues and discussions on; only
    maintainers merge; squash-merge, linear history, signed commits required by
    the `main` ruleset. **DCO** (`Signed-off-by`) on every commit, enforced by
    `.github/workflows/dco.yml`. No CLA.
  - `LICENSE`, `.github/`, `installer/`, `pyproject.toml`, and the release
    workflow are maintainer-owned (`.github/CODEOWNERS`).
  - The pre-absorption standalone assistant project's source archive
    (~300 MB) is **not** carried into the public repo. Its decision map survives
    as `docs/architecture/bujji-absorption.md`; its own history remains in the
    original private repository.
  - History: the public repo starts from a single squashed root commit
    (clean slate). Pre-public development history stays in the private archive repo.
- **Rejected:**
  - MIT / Apache-2.0 - allow a closed competitor fork.
  - `gh repo transfer` keeping full history - carries internal references and the
    300 MB archive into public view.
- **Consequences:**
  - Contributors, issues, PRs, and stars start from zero.
  - `git log --follow` across the absorption boundary no longer works; the map
    doc is the substitute.
