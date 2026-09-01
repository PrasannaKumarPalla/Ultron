import json
import subprocess
from pathlib import Path

import pytest

from ultron.agent_runtime import WorkspaceGuard
from ultron.db import Repository
from ultron.models import Classification, MemoryCreate, ProjectCreate
from ultron.security_scan import scan_dependencies, scan_secrets


# --- 1. Path escape attempts ---------------------------------------------

@pytest.mark.parametrize("relative", [
    "../outside.txt",
    "../../etc/passwd",
    "a/../../outside.txt",
    ".git/config",
    ".venv/pyvenv.cfg",
    "node_modules/pkg/index.js",
    "sub/__pycache__/module.pyc",
    "sub/node_modules/x.js",
])
def test_resolve_rejects_relative_escapes_and_protected_paths(tmp_path: Path, relative):
    guard = WorkspaceGuard(tmp_path / "workspace")
    with pytest.raises(ValueError):
        guard.resolve(relative)


def test_resolve_rejects_absolute_path_outside_root(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")
    outside = tmp_path / "outside.txt"
    with pytest.raises(ValueError):
        guard.resolve(str(outside))


def test_resolve_rejects_root_itself(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")
    with pytest.raises(ValueError):
        guard.resolve(".")


def test_resolve_rejects_symlink_escape_if_supported(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")
    outside_target = tmp_path / "outside_dir"
    outside_target.mkdir()
    link = guard.root / "escape_link"
    try:
        link.symlink_to(outside_target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not supported/permitted on this platform")
    with pytest.raises(ValueError):
        guard.resolve("escape_link/secret.txt")


def test_resolve_allows_legitimate_nested_path(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")
    resolved = guard.resolve("src/app/main.py")
    assert resolved == (guard.root / "src" / "app" / "main.py").resolve()


# --- 2. Role write-boundary enforcement -----------------------------------

def test_architect_role_only_writes_architecture_doc(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")
    files = [
        {"path": "docs/ARCHITECTURE.md", "content": "# arch"},
        {"path": "src/app.py", "content": "print('nope')"},
        {"path": "docs/OTHER.md", "content": "nope"},
    ]
    written = guard.write_files(files, role="architect")
    assert written == ["docs/ARCHITECTURE.md"]
    assert (guard.root / "docs" / "ARCHITECTURE.md").exists()
    assert not (guard.root / "src" / "app.py").exists()
    assert not (guard.root / "docs" / "OTHER.md").exists()


@pytest.mark.parametrize("role", ["product-manager", "security-engineer", "devops-engineer"])
def test_pm_security_devops_roles_only_write_doc_config_files(tmp_path: Path, role):
    guard = WorkspaceGuard(tmp_path / "workspace")
    files = [
        {"path": "docs/plan.md", "content": "plan"},
        {"path": "config.yaml", "content": "key: value"},
        {"path": "notes.txt", "content": "notes"},
        {"path": "src/app.py", "content": "print('nope')"},
        {"path": "app.js", "content": "console.log('nope')"},
    ]
    written = guard.write_files(files, role=role)
    assert set(written) == {"docs/plan.md", "config.yaml", "notes.txt"}
    assert not (guard.root / "src" / "app.py").exists()
    assert not (guard.root / "app.js").exists()


@pytest.mark.parametrize("role", ["ui-expert", "frontend-developer"])
def test_ui_roles_only_write_frontend_asset_files(tmp_path: Path, role):
    guard = WorkspaceGuard(tmp_path / "workspace")
    files = [
        {"path": "index.html", "content": "<html></html>"},
        {"path": "style.css", "content": "body{}"},
        {"path": "app.jsx", "content": "export default () => null;"},
        {"path": "src/server.py", "content": "print('nope')"},
        {"path": "config.yaml", "content": "nope: true"},
    ]
    written = guard.write_files(files, role=role)
    assert set(written) == {"index.html", "style.css", "app.jsx"}
    assert not (guard.root / "src" / "server.py").exists()
    assert not (guard.root / "config.yaml").exists()


@pytest.mark.parametrize("role", ["tester", "qa-engineer"])
def test_tester_and_qa_roles_write_nothing(tmp_path: Path, role):
    guard = WorkspaceGuard(tmp_path / "workspace")
    files = [
        {"path": "docs/ARCHITECTURE.md", "content": "nope"},
        {"path": "src/app.py", "content": "nope"},
        {"path": "notes.txt", "content": "nope"},
    ]
    written = guard.write_files(files, role=role)
    assert written == []
    assert not any(guard.root.rglob("*.py"))
    assert not any(guard.root.rglob("*.md"))
    assert not any(guard.root.rglob("*.txt"))


def test_developer_role_is_unrestricted_by_file_type(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")
    files = [
        {"path": "src/app.py", "content": "print('ok')"},
        {"path": "app.js", "content": "console.log('ok')"},
        {"path": "docs/ARCHITECTURE.md", "content": "# arch"},
    ]
    written = guard.write_files(files, role="developer")
    assert set(written) == {"src/app.py", "app.js", "docs/ARCHITECTURE.md"}


def test_write_files_rejects_path_escape_even_for_developer(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")
    with pytest.raises(ValueError):
        guard.write_files([{"path": "../escape.py", "content": "x"}], role="developer")


# --- 3. Cross-project memory isolation ------------------------------------

def _make_project(repo: Repository, tmp_path: Path, name: str) -> str:
    project = repo.create_project(ProjectCreate(
        name=name, workspace_path=tmp_path / name, classification=Classification.PERSONAL,
    ))
    return project.id


def test_project_memory_not_visible_to_other_project(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project_a = _make_project(repo, tmp_path, "project-a")
    project_b = _make_project(repo, tmp_path, "project-b")

    repo.add_memory(project_a, MemoryCreate(
        scope="project", role="supervisor", content="Project A secret plan",
        provenance="test", confidence=1.0, sensitivity=Classification.CLIENT_CONFIDENTIAL,
    ))

    project_b_memories = repo.memories(project_b, include_global=True)
    assert all("Project A secret plan" not in m.content for m in project_b_memories)

    project_a_memories = repo.memories(project_a, include_global=True)
    assert any("Project A secret plan" == m.content for m in project_a_memories)


def test_global_scoped_memory_visible_to_all_projects(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project_a = _make_project(repo, tmp_path, "project-a")
    project_b = _make_project(repo, tmp_path, "project-b")

    repo.add_memory(None, MemoryCreate(
        scope="global", role="supervisor", content="Global shared fact",
        provenance="test", confidence=1.0, sensitivity=Classification.PUBLIC_OPEN_SOURCE,
    ))

    assert any(m.content == "Global shared fact" for m in repo.memories(project_a, include_global=True))
    assert any(m.content == "Global shared fact" for m in repo.memories(project_b, include_global=True))


def test_project_memory_absent_without_include_global_for_other_project(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project_a = _make_project(repo, tmp_path, "project-a")
    project_b = _make_project(repo, tmp_path, "project-b")

    repo.add_memory(project_a, MemoryCreate(
        scope="project", role="supervisor", content="Project A only",
        provenance="test", confidence=1.0, sensitivity=Classification.PERSONAL,
    ))

    assert repo.memories(project_b, include_global=False) == []


# --- 5. Known gaps: do not fabricate false confidence ----------------------

# --- 6. Secrets and dependency scanning ------------------------------------

def test_scan_secrets_detects_and_redacts_aws_key(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config.py").write_text("AWS_KEY = \"AKIAABCDEFGHIJKLMNOP\"\n", encoding="utf-8")
    findings = scan_secrets(workspace)
    assert len(findings) == 1
    finding = findings[0]
    assert finding["rule"] == "aws_access_key"
    assert finding["file"] == "config.py"
    assert finding["line"] == 1
    assert "AKIAABCDEFGHIJKLMNOP" not in finding["match_preview"]
    assert finding["match_preview"].startswith("AKI")
    assert finding["match_preview"].endswith("NOP")


def test_scan_secrets_detects_generic_api_key_assignment(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("api_key = 'sk_live_abcdef0123456789'\n", encoding="utf-8")
    findings = scan_secrets(workspace)
    assert any(f["rule"] == "generic_api_key_or_token" for f in findings)
    assert all("sk_live_abcdef0123456789" not in f["match_preview"] for f in findings)


def test_scan_secrets_detects_private_key_header(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "id_rsa").write_text("-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK...\n", encoding="utf-8")
    findings = scan_secrets(workspace)
    assert any(f["rule"] == "private_key_header" for f in findings)


def test_scan_secrets_ignores_excluded_directories(tmp_path: Path):
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    (workspace / ".git" / "config").write_text("AKIAABCDEFGHIJKLMNOP\n", encoding="utf-8")
    (workspace / "node_modules" / "pkg").mkdir(parents=True)
    (workspace / "node_modules" / "pkg" / "index.js").write_text("AKIAABCDEFGHIJKLMNOP\n", encoding="utf-8")
    findings = scan_secrets(workspace)
    assert findings == []


def test_scan_secrets_clean_workspace_returns_no_findings(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("print('hello world')\n", encoding="utf-8")
    (workspace / "README.md").write_text("# My Project\nNothing sensitive here.\n", encoding="utf-8")
    assert scan_secrets(workspace) == []


def test_scan_dependencies_reports_unavailable_when_tooling_missing(tmp_path: Path, monkeypatch):
    from ultron import security_scan

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "requirements.txt").write_text("requests==2.0.0\n", encoding="utf-8")
    monkeypatch.setattr(security_scan.shutil, "which", lambda name: None)

    result = scan_dependencies(workspace)
    assert result.available is False
    assert result.tool == "pip-audit"
    assert result.findings == []


def test_scan_dependencies_no_manifest_reports_unavailable_with_no_tool(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = scan_dependencies(workspace)
    assert result.available is False
    assert result.tool is None
    assert result.findings == []


def test_scan_dependencies_runs_pip_audit_and_parses_findings(tmp_path: Path, monkeypatch):
    from ultron import security_scan

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "requirements.txt").write_text("requests==2.0.0\n", encoding="utf-8")

    sample_output = json.dumps({
        "dependencies": [
            {
                "name": "requests",
                "version": "2.0.0",
                "vulns": [
                    {"id": "PYSEC-2023-1234", "severity": "high"},
                ],
            },
            {"name": "certifi", "version": "2024.1.1", "vulns": []},
        ]
    })

    def fake_run(command, capture_output, text, timeout):
        assert command[0] == "pip-audit"
        return subprocess.CompletedProcess(command, 0, stdout=sample_output, stderr="")

    monkeypatch.setattr(security_scan.shutil, "which", lambda name: "/usr/bin/pip-audit" if name == "pip-audit" else None)
    monkeypatch.setattr(security_scan.subprocess, "run", fake_run)

    result = scan_dependencies(workspace)
    assert result.tool == "pip-audit"
    assert result.available is True
    assert result.findings == [{
        "package": "requests",
        "installed_version": "2.0.0",
        "vulnerability_id": "PYSEC-2023-1234",
        "severity": "high",
    }]
