"""Startup voice briefing — spoken when BUJJI comes online.

Composes a short spoken checklist: time-appropriate greeting, date, weather
(Open-Meteo via IP geolocation — free, no API key), pending self-dev branches,
and top items from an optional vault checklist note. Every section is
best-effort: whatever fails is silently skipped so startup never breaks.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_WEATHER_CODES = {
    0: "clear skies", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    81: "rain showers", 82: "heavy showers", 95: "thunderstorms",
    96: "thunderstorms", 99: "severe thunderstorms",
}


def _greeting(name: str) -> str:
    hour = datetime.now().hour
    if hour < 12:
        wish = "Good morning"
    elif hour < 17:
        wish = "Good afternoon"
    else:
        wish = "Good evening"
    return f"{wish}, {name}." if name else f"{wish}."


def _date_line() -> str:
    now = datetime.now()
    return f"It's {now:%A}, {now.day} {now:%B}, {now:%I:%M %p}."


# Common short names people actually say, mapped to geocodable names
_CITY_ALIASES = {"vizag": "Visakhapatnam", "hyd": "Hyderabad", "bombay": "Mumbai"}


def _locate(configured_city: str = "") -> Optional[tuple]:
    """Return (lat, lon, city) from the configured city, else IP geolocation."""
    import httpx

    city_query = configured_city.strip()
    if city_query:
        city_query = _CITY_ALIASES.get(city_query.lower(), city_query)
        try:
            geo = httpx.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city_query, "count": 1},
                timeout=5,
            ).json()
            hit = (geo.get("results") or [None])[0]
            if hit:
                return hit["latitude"], hit["longitude"], hit.get("name", city_query)
        except Exception:
            logger.debug("Briefing: geocoding '%s' failed", city_query, exc_info=True)
    try:
        geo = httpx.get("http://ip-api.com/json/?fields=lat,lon,city", timeout=5).json()
        if geo.get("lat") is not None:
            return geo["lat"], geo["lon"], geo.get("city", "")
    except Exception:
        pass
    return None


def _weather_line(configured_city: str = "") -> Optional[str]:
    try:
        import httpx

        loc = _locate(configured_city)
        if loc is None:
            return None
        lat, lon, city = loc
        wx = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min",
                "forecast_days": 1, "timezone": "auto",
            },
            timeout=6,
        ).json()
        cur = wx.get("current", {})
        temp = cur.get("temperature_2m")
        cond = _WEATHER_CODES.get(cur.get("weather_code", -1), "")
        daily = wx.get("daily", {})
        hi = (daily.get("temperature_2m_max") or [None])[0]
        if temp is None:
            return None
        line = f"It's {round(temp)} degrees"
        if city:
            line += f" in {city}"
        if cond:
            line += f" with {cond}"
        if hi is not None:
            line += f", going up to {round(hi)}"
        return line + "."
    except Exception:
        logger.debug("Briefing: weather unavailable", exc_info=True)
        return None


def _selfdev_line() -> Optional[str]:
    """Report self-dev branches awaiting review, if any."""
    try:
        repo = Path(__file__).resolve()
        root = None
        for parent in repo.parents:
            if (parent / ".git").is_dir():
                root = parent
                break
        if root is None:
            return None
        r = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/self-dev/"],
            cwd=str(root), capture_output=True, text=True, timeout=10,
        )
        branches = [b for b in (r.stdout or "").splitlines() if b.strip()]
        if not branches:
            return None
        n = len(branches)
        return (
            f"{n} self-development branch{'es' if n > 1 else ''} "
            "awaiting your review."
        )
    except Exception:
        return None


def _checklist_line() -> Optional[str]:
    """Read top unchecked items from <vault>/<folder>/Checklist.md, if present."""
    try:
        from bujji.core.config import load_config

        cfg = load_config()
        ob = getattr(getattr(cfg, "connectors", None), "obsidian", None)
        if ob is None or not getattr(ob, "enabled", False):
            return None
        note = Path(str(ob.vault_path)) / str(ob.notes_folder or "Bujji") / "Checklist.md"
        if not note.is_file():
            return None
        items = [
            line.split("]", 1)[1].strip()
            for line in note.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip().startswith(("- [ ]", "* [ ]"))
        ][:3]
        if not items:
            return None
        return "On your checklist: " + "; ".join(items) + "."
    except Exception:
        return None


def build_briefing(name: str = "Prasanna", location: str = "") -> str:
    """Compose the spoken startup briefing text."""
    parts = [_greeting(name), _date_line()]
    for maybe in (_weather_line(location), _selfdev_line(), _checklist_line()):
        if maybe:
            parts.append(maybe)
    parts.append(
        f"All systems online. So {name}, how are you doing today?"
        if name else "All systems online. How are you doing today?"
    )
    return " ".join(parts)


__all__ = ["build_briefing"]
