"""Shadow-git gate: candidate diffs never touch the workspace until tests pass.

The shadow repo lives at <workspace>/.ultron-shadow with GIT_DIR there and
GIT_WORK_TREE pointed at the workspace itself. Baseline commits land on
`main`. Candidate work happens on `ultron-candidate`; a green test run
fast-forwards main, a red one rolls the workspace back to baseline.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

MAIN_BRANCH = "main"
CANDIDATE_BRANCH = "ultron-candidate"
SHADOW_DIR = ".ultron-shadow"


class ShadowGitError(RuntimeError):
    pass


class ShadowGit:
    def __init__(self, workspace: Path, enabled: bool = True):
        self.workspace = Path(workspace).resolve()
        self.git_dir = self.workspace / SHADOW_DIR
        self.enabled = enabled
        self.available = False

    def _git(self, *args: str) -> tuple[int, str]:
        env = dict(os.environ)
        env["GIT_DIR"] = str(self.git_dir)
        env["GIT_WORK_TREE"] = str(self.workspace)
        for var in ("GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR"):
            env.pop(var, None)
        try:
            completed = subprocess.run(["git", *args], capture_output=True, text=True,
                                       timeout=60, env=env)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return 1, str(exc)
        return completed.returncode, completed.stdout + completed.stderr

    def _must(self, *args: str) -> str:
        code, out = self._git(*args)
        if code != 0:
            raise ShadowGitError(f"git {' '.join(args)} failed: {out.strip()}")
        return out

    def ensure(self) -> bool:
        """Initialize the shadow repo and baseline commit. False => gate disabled."""
        if not self.enabled:
            return False
        if not shutil.which("git"):
            return False
        if not self.git_dir.exists():
            self.git_dir.mkdir(parents=True)
            code, out = self._git("init")
            if code != 0:
                raise ShadowGitError(f"git init failed: {out.strip()}")
            exclude = self.git_dir / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(
                "\n".join([f"{SHADOW_DIR}/", ".worktrees/", ".pytest_cache/",
                           "__pycache__/", "*.pyc", ".venv/", "node_modules/"]) + "\n",
                encoding="utf-8")
            self._must("symbolic-ref", "HEAD", f"refs/heads/{MAIN_BRANCH}")
        code, _ = self._git("rev-parse", "--verify", "HEAD")
        if code != 0:
            self.commit_all("ultron baseline", allow_empty=True)
        self._recover_stranded_branch()
        self.available = True
        return True

    def _recover_stranded_branch(self) -> None:
        """A crash between a variant checkout and forward_variant/rollback leaves
        the workspace on a candidate branch. Every mission starts from main, so
        detect that state here and reset to baseline before handing back."""
        current = self.branch()
        if not current or current == MAIN_BRANCH:
            return
        if current == CANDIDATE_BRANCH or current.startswith(f"{CANDIDATE_BRANCH}-"):
            logger.warning("shadow-git: workspace stranded on %s; resetting to %s",
                           current, MAIN_BRANCH)
            self._git("checkout", "-f", MAIN_BRANCH)
            self._git("reset", "--hard")

    def head(self) -> str | None:
        code, out = self._git("rev-parse", "--verify", "HEAD")
        return out.strip() if code == 0 else None

    def branch(self) -> str | None:
        code, out = self._git("symbolic-ref", "--short", "HEAD")
        return out.strip() if code == 0 else None

    def commit_all(self, message: str, allow_empty: bool = False) -> str | None:
        self._must("add", "-A", "--", ".")
        args = ["-c", "user.name=Ultron", "-c", "user.email=ultron@local",
                "commit", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        self._must(*args)
        return self.head()

    def begin_candidate(self) -> None:
        """Point ultron-candidate at main and hard-switch the workspace to it."""
        self.ensure()
        main_head = self._must("rev-parse", "--verify", MAIN_BRANCH).strip()
        self._must("update-ref", f"refs/heads/{CANDIDATE_BRANCH}", main_head)
        self._must("checkout", "-f", CANDIDATE_BRANCH)

    def candidate_commit(self, label: str) -> str | None:
        """Snapshot the candidate diff; safe to call when nothing changed."""
        status = self._git("status", "--porcelain")[1].strip()
        if not status and self._git("diff", "--quiet", "HEAD")[0] == 0:
            return self.head()
        return self.commit_all(label)

    def fast_forward(self) -> str:
        """Green gate: move main to the candidate and leave the workspace on main."""
        self._must("checkout", "-f", MAIN_BRANCH)
        self._must("merge", "--ff-only", CANDIDATE_BRANCH)
        return self.head()

    def rollback(self) -> None:
        """Red gate: restore the workspace to baseline and drop candidate content."""
        self._must("checkout", "-f", MAIN_BRANCH)
        self._must("reset", "--hard")

    def changed_files(self) -> list[str]:
        base = f"{MAIN_BRANCH}...{CANDIDATE_BRANCH}"
        code, out = self._git("diff", "--name-only", base)
        if code != 0:
            logger.warning("shadow-git diff --name-only %s failed (%d): %s", base, code, out.strip())
            return []
        return [line for line in out.splitlines() if line.strip()]

    def diff_stat(self) -> str:
        base = f"{MAIN_BRANCH}...{CANDIDATE_BRANCH}"
        code, out = self._git("diff", "--stat", base)
        if code != 0:
            logger.warning("shadow-git diff --stat %s failed (%d): %s", base, code, out.strip())
            return ""
        return out.strip()

    def begin_variant(self, tag: str) -> str:
        """Isolate one speculative candidate on its own branch at baseline."""
        self.ensure()
        main_head = self._must("rev-parse", "--verify", MAIN_BRANCH).strip()
        ref = f"refs/heads/{CANDIDATE_BRANCH}-{tag}"
        self._must("update-ref", ref, main_head)
        self._must("checkout", "-f", f"{CANDIDATE_BRANCH}-{tag}")
        return main_head

    def forward_variant(self, tag: str) -> str:
        """Fast-forward main to the winning variant's branch."""
        self._must("checkout", "-f", MAIN_BRANCH)
        self._must("merge", "--ff-only", f"{CANDIDATE_BRANCH}-{tag}")
        return self.head()

    def _git_at(self, wt_path: Path, *args: str) -> tuple[int, str]:
        """git inside a linked worktree: its `.git` file resolves the repo, so
        the GIT_DIR / GIT_WORK_TREE overrides must be cleared."""
        env = dict(os.environ)
        for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                    "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR"):
            env.pop(var, None)
        try:
            completed = subprocess.run(["git", "-C", str(wt_path), *args],
                                       capture_output=True, text=True, timeout=120, env=env)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return 1, str(exc)
        return completed.returncode, completed.stdout + completed.stderr

    def add_variant_worktree(self, tag: str) -> Path:
        """Check the tag's branch out in its own linked worktree so variants can
        run in parallel without sharing one checkout."""
        self.ensure()
        main_head = self._must("rev-parse", "--verify", MAIN_BRANCH).strip()
        branch = f"{CANDIDATE_BRANCH}-{tag}"
        self._must("update-ref", f"refs/heads/{branch}", main_head)
        wt = self.workspace / ".worktrees" / f"variant-{tag}"
        if wt.exists():
            self._git("worktree", "remove", "--force", str(wt))
        wt.parent.mkdir(parents=True, exist_ok=True)
        self._must("worktree", "add", "--force", str(wt), branch)
        return wt

    def commit_variant_worktree(self, wt_path: Path, label: str) -> str | None:
        code, out = self._git_at(wt_path, "status", "--porcelain")
        if code == 0 and not out.strip():
            code, head = self._git_at(wt_path, "rev-parse", "HEAD")
            return head.strip() if code == 0 else None
        self._git_at(wt_path, "add", "-A", "--", ".")
        code, out = self._git_at(wt_path, "-c", "user.name=Ultron", "-c",
                                 "user.email=ultron@local", "commit", "-m", label)
        if code != 0:
            raise ShadowGitError(f"variant commit failed: {out.strip()}")
        code, head = self._git_at(wt_path, "rev-parse", "HEAD")
        return head.strip() if code == 0 else None

    def remove_variant_worktrees(self) -> None:
        root = self.workspace / ".worktrees"
        if not root.exists():
            return
        for wt in sorted(root.glob("variant-*")):
            self._git("worktree", "remove", "--force", str(wt))
        self._git("worktree", "prune")
