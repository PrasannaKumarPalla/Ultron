"""Analytics stays off unless a fork supplies its own PostHog host + key."""

from __future__ import annotations

from bujji.analytics.client import AnalyticsClient
from bujji.core.config import AnalyticsConfig


def _cfg(tmp_path, **over):
    base = dict(enabled=False, host="", key="",
                anon_id_path=str(tmp_path / "anon"))
    base.update(over)
    return AnalyticsConfig(**base)


def test_disabled_by_default(tmp_path):
    client = AnalyticsClient(_cfg(tmp_path))
    assert client.enabled is False


def test_enabled_but_no_host_or_key_stays_disabled(tmp_path):
    client = AnalyticsClient(_cfg(tmp_path, enabled=True))
    assert client.enabled is False


def test_enabled_with_only_a_host_still_disabled(tmp_path):
    client = AnalyticsClient(_cfg(tmp_path, enabled=True, host="https://ph.example"))
    assert client.enabled is False


def test_shipped_defaults_carry_no_endpoint():
    fresh = AnalyticsConfig()
    assert fresh.host == "" and fresh.key == ""
