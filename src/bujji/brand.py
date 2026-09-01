"""User-facing branding helpers for distribution builds.

This keeps visible product naming out of the engine internals so we can
rebrand the shipped app without immediately renaming the whole Python package.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class Branding:
    product_name: str
    command_name: str
    tagline: str
    install_dir_name: str
    home_dir_name: str
    xdg_subdir_name: str
    home_env_var: str
    display_name: str
    wake_word: str
    legacy_home_env_var: str


_DEFAULT_BRANDING = Branding(
    product_name="Your Assistant",
    command_name="assistant",
    tagline="Local AI, On Your Personal Devices.",
    install_dir_name="YourAssistant",
    home_dir_name=".yourassistant",
    xdg_subdir_name="yourassistant",
    home_env_var="YOUR_ASSISTANT_HOME",
    display_name="Y.A.S.",
    wake_word="assistant",
    legacy_home_env_var="BUJJI_HOME",
)


def _repo_root() -> Path | None:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "branding" / "brand.json").exists():
            return candidate
    return None


def _brand_file() -> Path | None:
    explicit = os.environ.get("BUJJI_BRAND_FILE")
    if explicit:
        path = Path(explicit).expanduser()
        if path.exists():
            return path

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            bundled = Path(meipass) / "branding" / "brand.json"
            if bundled.exists():
                return bundled
        exe_brand = Path(sys.executable).resolve().parent / "branding" / "brand.json"
        if exe_brand.exists():
            return exe_brand

    root = _repo_root()
    if root is not None:
        return root / "branding" / "brand.json"
    return None


def _load_repo_brand_overrides() -> dict[str, Any]:
    path = _brand_file()
    if path is None:
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _resolve_user_brand_dir(base: Mapping[str, Any]) -> Path:
    home_env_var = str(base.get("home_env_var") or _DEFAULT_BRANDING.home_env_var)
    legacy_home_env_var = str(
        base.get("legacy_home_env_var") or _DEFAULT_BRANDING.legacy_home_env_var
    )
    xdg_subdir_name = str(
        base.get("xdg_subdir_name") or _DEFAULT_BRANDING.xdg_subdir_name
    )
    home_dir_name = str(base.get("home_dir_name") or _DEFAULT_BRANDING.home_dir_name)

    env_home = os.environ.get(home_env_var) or os.environ.get(legacy_home_env_var)
    if env_home:
        return Path(env_home).expanduser()

    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data).expanduser() / xdg_subdir_name

    return Path.home() / home_dir_name


def _user_brand_file(base: Mapping[str, Any]) -> Path:
    explicit = os.environ.get("BUJJI_USER_BRAND_FILE")
    if explicit:
        return Path(explicit).expanduser()
    return _resolve_user_brand_dir(base) / "branding.json"


def _load_user_brand_overrides(base: Mapping[str, Any]) -> dict[str, Any]:
    path = _user_brand_file(base)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@lru_cache(maxsize=1)
def get_branding() -> Branding:
    raw = _DEFAULT_BRANDING.__dict__ | _load_repo_brand_overrides()
    raw |= _load_user_brand_overrides(raw)

    # Env vars win so installers and CI can override without patching files.
    raw["product_name"] = os.environ.get("BUJJI_PRODUCT_NAME", raw["product_name"])
    raw["command_name"] = os.environ.get("BUJJI_COMMAND_NAME", raw["command_name"])
    raw["tagline"] = os.environ.get("BUJJI_TAGLINE", raw["tagline"])
    raw["install_dir_name"] = os.environ.get(
        "BUJJI_INSTALL_DIR",
        raw["install_dir_name"],
    )
    raw["home_env_var"] = os.environ.get("BUJJI_HOME_ENV_VAR", raw["home_env_var"])
    raw["display_name"] = os.environ.get("BUJJI_DISPLAY_NAME", raw["display_name"])
    raw["wake_word"] = os.environ.get("BUJJI_WAKE_WORD", raw["wake_word"])

    return Branding(
        product_name=str(raw["product_name"]).strip() or _DEFAULT_BRANDING.product_name,
        command_name=str(raw["command_name"]).strip() or _DEFAULT_BRANDING.command_name,
        tagline=str(raw["tagline"]).strip() or _DEFAULT_BRANDING.tagline,
        install_dir_name=str(raw["install_dir_name"]).strip()
        or _DEFAULT_BRANDING.install_dir_name,
        home_dir_name=str(raw["home_dir_name"]).strip() or _DEFAULT_BRANDING.home_dir_name,
        xdg_subdir_name=str(raw["xdg_subdir_name"]).strip()
        or _DEFAULT_BRANDING.xdg_subdir_name,
        home_env_var=str(raw["home_env_var"]).strip() or _DEFAULT_BRANDING.home_env_var,
        display_name=str(raw["display_name"]).strip() or _DEFAULT_BRANDING.display_name,
        wake_word=str(raw["wake_word"]).strip().lower() or _DEFAULT_BRANDING.wake_word,
        legacy_home_env_var=str(raw["legacy_home_env_var"]).strip()
        or _DEFAULT_BRANDING.legacy_home_env_var,
    )


def get_user_brand_file() -> Path:
    """Return the user-local branding override path."""
    base = _DEFAULT_BRANDING.__dict__ | _load_repo_brand_overrides()
    return _user_brand_file(base)


def save_user_branding(overrides: Mapping[str, Any]) -> Path:
    """Persist user-specific branding overrides."""
    path = get_user_brand_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = {
        key: value
        for key, value in overrides.items()
        if value is not None and str(value).strip()
    }
    path.write_text(json.dumps(cleaned, indent=2) + "\n", encoding="utf-8")
    get_branding.cache_clear()
    return path


def reset_user_branding() -> Path:
    """Delete the user-local branding override file if present."""
    path = get_user_brand_file()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    get_branding.cache_clear()
    return path
