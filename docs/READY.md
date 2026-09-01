# Ultron — Ready-to-Ship Checklist

Acceptance criteria for calling Ultron "ready to install." Every item is a
must-pass; each has a verification step so pass/fail is measurable, not asserted.

## 1. Build integrity

- [ ] `pytest -v` — full suite green (0 failures, only intentional skips).
      Verify: `.\.venv\Scripts\python -m pytest -q`.
- [ ] PyInstaller build produces `dist\Ultron.exe` under 60 MB with no
      import errors during the build log.
      Verify: `.\.venv\Scripts\python -m PyInstaller --clean --noconfirm Ultron.spec`.
- [ ] `bujji` legacy full test suite still runs under `pytest-bujji.ini`
      OR the file is retired and this line deleted.
      Verify: `.\.venv\Scripts\python -m pytest -c pytest-bujji.ini -q`.
- [ ] Semgrep + TruffleHog CI green on `main`.
- [x] `.github/workflows/ci.yml` runs `pytest` on push/PR to `main`.
- [x] `bujji.sdk.__version__` resolves to the real package version
      (not `0.0.0+unknown`) inside the built exe.

## 2. Cold-start install (fresh Windows 11 user)

- [x] User has Ollama installed OR the first-run bootstrap offers to
      auto-download and silently install it (`OllamaSetup.exe
      /VERYSILENT`). If missing after decline, the app opens the download
      page as a fallback.
- [x] First-run bootstrap detects zero installed chat models and offers
      to pull `qwen3.6:27b` in a separate console window while Ultron
      starts.
- [ ] First launch creates `%LOCALAPPDATA%\Ultron\` with `ultron.db`,
      `checkpoints.db`, `ultron.log`; no writes elsewhere.
- [ ] First launch creates `%USERPROFILE%\Documents\UltronProjects\` if
      missing; existing folder is not overwritten.
- [ ] First launch opens the native window inside 5 seconds after Ollama
      is confirmed ready.
- [ ] Onboarding overlay appears once, dismisses, does not re-appear on
      relaunch (localStorage flag persists).

## 3. Golden-path mission

- [ ] Create project → start mission "generate a hello module with a
      passing test" → mission reaches `COMPLETED`.
- [ ] Mission timeline shows role hand-offs (architect → developer → UI →
      tester), no role stuck > 60s without event.
- [ ] Shadow-git gate opens a candidate, forwards on green tests, rolls
      back on red — both paths reachable via demo mission.
- [ ] Deliverables land in the project workspace; no writes leak outside.

## 4. Assistant desk

- [ ] Wake word "assistant" triggers within 2 seconds of speech.
- [ ] Direct-answer path (short question) returns text under 10 seconds
      on a 12 GB VRAM machine.
- [ ] "Assistant, start a mission called X" creates a mission and returns
      the mission id in the same session.
- [ ] Legacy wake word "bujji" still fires (documented back-compat).

## 5. Surfaces & safety

- [ ] `/health` returns `status:healthy` with `database:healthy`,
      `ollama:healthy`.
- [ ] Every API route in `src/ultron/api.py` returns non-5xx on a
      fresh DB (empty lists are OK).
- [ ] `WorkspaceGuard` blocks a write outside the selected project
      workspace (unit test present + green).
- [ ] Shell/test commands run without shell interpolation (unit test
      present + green).
- [ ] Pre-mission security scan gate fires on every mission start
      (verified in demo mission event log).

## 6. Clean uninstall

- [ ] Deleting `dist\Ultron.exe` leaves no background service running
      (`tasklist` shows no `Ultron.exe`, no orphaned uvicorn worker).
- [ ] Documented uninstall step for `%LOCALAPPDATA%\Ultron\` and
      `%USERPROFILE%\Documents\UltronProjects\`.
- [ ] No autostart entries, no registry writes outside the app's own
      LOCALAPPDATA path.

## 7. Repo hygiene (blocks polish, not function)

- [x] No tracked `.coverage`, no tracked `*.log`, no tracked
      `__pycache__`.
- [x] `relocate-tmp.ps1` removed (one-time migration, done).
- [x] Top-level `bujji/` companion tree deleted (Ultron ships the
      embedded assistant; companion apps not part of scope).
- [ ] `LICENSE` present at repo root.
- [x] `README.md` lists the exact prerequisites (Ollama, VRAM floor,
      Python version for source builds).

## 8. Distribution (only if shipping externally)

- [x] `LICENSE` (AGPL-3.0) at repo root.
- [x] NSIS installer (`installer/ultron.nsi`) producing
      `Ultron-Setup-<version>.exe` — per-user install, Start Menu +
      Desktop shortcuts, Add/Remove entry, uninstaller preserves user data.
- [x] Release workflow (`.github/workflows/release.yml`) builds exe +
      installer on `v*.*.*` tag push (or manual dispatch) and uploads to
      GitHub Release.
- [x] Auto-update path: **GitHub Releases** (users check the Releases
      page; no in-app updater — accepted trade-off).
- [x] Crash reports: **none** — no telemetry, no crash uploads, local
      log only at `%LOCALAPPDATA%\Ultron\ultron.log`.
- [ ] Code signing — **skipped** (paid cert). SmartScreen warning on
      first run is documented in the README.

---

Verdict format: for each item, `PASS` / `FAIL` / `SKIP(reason)`.
Ship = all sections 1–7 pass. Section 8 gates external distribution only.
