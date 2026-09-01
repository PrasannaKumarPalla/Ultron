"""Agent template system â€” pre-configured agent manifests."""

from bujji.templates.agent_templates import (
    AgentTemplate,
    discover_templates,
    load_template,
)

__all__ = ["AgentTemplate", "discover_templates", "load_template"]
