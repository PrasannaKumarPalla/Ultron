"""External anonymous usage analytics.

Sends anonymized events to PostHog so the Bujji team can measure
setup success, retention, feature usage, and churn â€” without ever
collecting chat content, prompts, file paths, emails, IPs, or hardware
identifiers.

Distinct from :mod:`bujji.telemetry`, which stores local FLOPs and
energy metrics in a SQLite DB and never leaves the machine.

Disable: set ``[analytics] enabled = false`` in ``~/.bujji/config.toml``.
"""

from bujji.analytics.aggregator import SessionAggregator
from bujji.analytics.bridge import EventBridge
from bujji.analytics.client import AnalyticsClient
from bujji.analytics.identity import (
    get_or_create_anon_id,
    is_analytics_enabled,
    reset_anon_id,
)
from bujji.analytics.redaction import hash_id, redact

__all__ = [
    "AnalyticsClient",
    "EventBridge",
    "SessionAggregator",
    "get_or_create_anon_id",
    "is_analytics_enabled",
    "reset_anon_id",
    "redact",
    "hash_id",
]
