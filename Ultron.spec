# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata


datas = [("src/ultron/ui", "ultron/ui"), ("src/ultron/roles.yaml", "ultron"), ("branding", "branding"), ("assets/ultron.ico", "assets")]
datas += copy_metadata("ultron-control-plane")
hiddenimports = collect_submodules("langgraph")
for package in ("langgraph", "pydantic_settings", "webview"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    hiddenimports += package_hiddenimports
# Absorbed assistant subsystem: bake code and package data into the one exe.
hiddenimports += collect_submodules("bujji")
datas += collect_data_files("bujji")
# Explicitly surface every ultron module (incl. lazy imports inside functions)
# so the one-file bundle stays complete after the phase 1-4 additions.
hiddenimports += [
    "ultron.store",
    "ultron.shadow_git",
    "ultron.repo_intel",
    "ultron.sandbox",
    "ultron.search",
    "ultron.memory_layers",
    "ultron.model_pool",
    "ultron.tools_registry",
    "ultron.builtin_tools",
    "ultron.trace",
    "ultron.bootstrap",
]

a = Analysis(
    ["desktop_app.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["bench", "tests", "pytest", "coverage"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Ultron",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["assets/ultron.ico"],
)
