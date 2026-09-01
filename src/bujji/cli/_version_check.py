"""Best-effort update check for packaged upstream builds."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

from bujji.core.paths import get_config_dir

logger = logging.getLogger(__name__)

_CACHE_PATH = get_config_dir() / "version-check.json"
_CACHE_TTL = 86400
_PYPI_API = "https://pypi.org/pypi/bujji/json"
_CHECK_COMMANDS = {
    "ask",
    "chat",
    "serve",
    "doctor",
    "init",
    "quickstart",
    "model",
    "agents",
    "skill",
    "memory",
    "bench",
    "telemetry",
    "config",
    "eval",
    "optimize",
}
_OPT_OUT_ENV_VARS = ("BUJJI_NO_UPDATE_CHECK",)


def _config_path() -> Path:
    override = os.environ.get("BUJJI_CONFIG")
    if override:
        return Path(override).expanduser()
    return get_config_dir() / "config.toml"


def _check_disabled() -> bool:
    for name in _OPT_OUT_ENV_VARS:
        raw = os.environ.get(name, "")
        if raw and raw.strip().lower() not in ("", "0", "false", "no", "off"):
            return True
    if os.environ.get("CI", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return _config_disabled()


def _config_disabled() -> bool:
    path = _config_path()
    if not path.exists():
        return False
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            logger.debug("tomli not available, skipping config opt-out check")
            return False
    try:
        with open(path, "rb") as f:
            config = tomllib.load(f)
    except OSError as exc:
        logger.debug("config read failed: %s", exc)
        return False
    except tomllib.TOMLDecodeError as exc:
        logger.debug("config malformed at %s: %s - treating as opt-out", path, exc)
        return True
    return not config.get("updates", {}).get("auto_update", True)


def check_for_updates(command_name: str) -> None:
    if command_name not in _CHECK_COMMANDS:
        return
    if _check_disabled():
        return
    try:
        _do_check()
    except Exception:
        pass


def _do_check() -> None:
    import bujji

    current = bujji.__version__
    if current.endswith("+unknown") or current.startswith("0.0.0"):
        return
    latest = _get_latest_version(current)
    if latest is None:
        return

    from packaging.version import InvalidVersion, Version

    try:
        if Version(latest) > Version(current):
            from bujji.cli._install_detect import detect_install

            cmd = detect_install().upgrade_command
            sys.stderr.write(
                f"\033[33mA newer packaged build is available "
                f"(v{current} -> v{latest})\n"
                f"Update: {cmd}\n"
                f"Or run: assistant self-update\033[0m\n\n"
            )
    except InvalidVersion:
        pass


def _get_latest_version(current: str) -> str | None:
    try:
        if _CACHE_PATH.exists():
            data = json.loads(_CACHE_PATH.read_text())
            last_check = data.get("last_check", 0)
            if time.time() - last_check < _CACHE_TTL:
                cached = data.get("latest_version")
                return cached or None
    except Exception:
        pass

    latest = _fetch_latest_stable()
    if not latest:
        return None

    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(
            json.dumps(
                {
                    "last_check": time.time(),
                    "latest_version": latest,
                    "current_version": current,
                }
            )
        )
    except Exception:
        pass

    return latest


def _fetch_latest_stable() -> str | None:
    try:
        import urllib.request

        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected  -- fixed host / operator-configured model endpoint; URL is not request-derived
        with urllib.request.urlopen(_PYPI_API, timeout=3) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        logger.debug("PyPI poll failed: %s", exc)
        return None

    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:
        return data.get("info", {}).get("version") or None

    releases = data.get("releases", {})
    stable: list[Version] = []
    for raw in releases.keys():
        try:
            v = Version(raw)
        except InvalidVersion:
            continue
        if v.is_prerelease or v.is_devrelease:
            continue
        stable.append(v)

    if stable:
        return str(max(stable))
    return data.get("info", {}).get("version") or None
