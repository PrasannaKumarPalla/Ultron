"""Repo intelligence: symbol, import, and call graphs built with stdlib ast.

Decision (ADR-0004): stdlib `ast` instead of tree-sitter — zero deps, no
C extension build for PyInstaller, sufficient fidelity for Python-first
workspaces. Churn/ownership come from local git history.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ultron-shadow", ".pytest_cache"}
MAX_FILE_BYTES = 300_000


class RepoIntel:
    """Incremental per-file parse cache keyed by (path, mtime, size)."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._cache: dict[str, tuple[float, int, dict | None]] = {}

    def python_files(self) -> list[Path]:
        if not self.root.exists():
            return []
        return [path for path in sorted(self.root.rglob("*.py"))
                if not any(part in IGNORED_DIRS for part in path.parts)
                and path.stat().st_size <= MAX_FILE_BYTES]

    def analyze_file(self, relative: str) -> dict | None:
        """Symbols, imports, and intra-file calls for one file; cached on mtime."""
        path = self.root / relative
        try:
            stat = path.stat()
        except OSError:
            self._cache.pop(relative, None)
            return None
        key = (stat.st_mtime, stat.st_size)
        cached = self._cache.get(relative)
        if cached and (cached[0], cached[1]) == key:
            return cached[2]
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            self._cache[relative] = (*key, None)
            return None
        result = self._extract(relative, tree)
        self._cache[relative] = (*key, result)
        return result

    @staticmethod
    def _extract(relative: str, tree: ast.Module) -> dict:
        symbols: list[dict] = []
        imports: list[dict] = []
        calls: list[dict] = []

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.scope: list[str] = []

            def _qualname(self, name: str) -> str:
                return ".".join([*self.scope, name])

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                symbols.append({"kind": "class", "name": self._qualname(node.name),
                                "file": relative, "line": node.lineno,
                                "end_line": node.end_lineno})
                self.scope.append(node.name)
                self.generic_visit(node)
                self.scope.pop()

            def visit_FunctionDef(self, node) -> None:
                caller = self._qualname(node.name)
                symbols.append({"kind": "function", "name": caller,
                                "file": relative, "line": node.lineno,
                                "end_line": node.end_lineno})
                self.scope.append(node.name)
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        name = getattr(child.func, "id", None) or getattr(child.func, "attr", None)
                        if name:
                            calls.append({"caller": caller, "callee": name})
                self.scope.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Import(self, node: ast.Import) -> None:
                for alias in node.names:
                    imports.append({"module": alias.name, "names": [],
                                    "file": relative, "line": node.lineno})

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                imports.append({"module": node.module or "",
                                "names": [alias.name for alias in node.names],
                                "file": relative, "line": node.lineno})

        visitor = Visitor()
        visitor.visit(tree)
        return {"file": relative, "symbols": symbols, "imports": imports, "calls": calls}

    def graph(self) -> dict:
        files: list[dict] = []
        for path in self.python_files():
            relative = path.relative_to(self.root).as_posix()
            analyzed = self.analyze_file(relative)
            if analyzed is not None:
                files.append(analyzed)

        modules = {Path(item["file"]).with_suffix("").as_posix().replace("/", ".")
                   for item in files}
        top_level = {name.split(".")[0] for name in modules}
        internal_edges: list[dict] = []
        for item in files:
            source = Path(item["file"]).with_suffix("").as_posix().replace("/", ".")
            for imported in item["imports"]:
                if imported["module"].split(".")[0] in top_level and not imported["module"].startswith("relative:"):
                    internal_edges.append({"from": source, "to": imported["module"]})

        return {
            "root": str(self.root),
            "files": [item["file"] for item in files],
            "symbols": [symbol for item in files for symbol in item["symbols"]],
            "imports": [{"from": item["file"], **imp}
                        for item in files for imp in item["imports"]],
            "internal_imports": internal_edges,
            "calls": [call for item in files for call in item["calls"]],
        }

    def churn(self, max_commits: int = 200) -> dict[str, int]:
        """Commit counts per file from local git log ({} when not a repo)."""
        completed = subprocess.run(
            ["git", "-C", str(self.root), "log", "--name-only", "--format=",
             f"-{max_commits}"],
            capture_output=True, text=True, timeout=30)
        if completed.returncode != 0:
            return {}
        counts: dict[str, int] = {}
        for line in completed.stdout.splitlines():
            path = line.strip()
            if path:
                counts[path] = counts.get(path, 0) + 1
        return counts

    def hotspots(self, limit: int = 10) -> list[dict]:
        churn = self.churn()
        tracked = {path.relative_to(self.root).as_posix() for path in self.python_files()}
        ranked = sorted(churn.items(), key=lambda kv: kv[1], reverse=True)
        return [{"file": path, "commits": count, "tracked_python": path in tracked}
                for path, count in ranked[:limit]]

    def invalidate(self, relative: str | None = None) -> None:
        if relative is None:
            self._cache.clear()
        else:
            self._cache.pop(relative, None)


