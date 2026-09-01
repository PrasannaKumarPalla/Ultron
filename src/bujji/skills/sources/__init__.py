"""Skill source resolvers â€” Hermes, OpenClaw, generic GitHub."""

from bujji.skills.sources.base import ResolvedSkill, SourceResolver
from bujji.skills.sources.github import GitHubResolver
from bujji.skills.sources.hermes import HERMES_REPO_URL, HermesResolver
from bujji.skills.sources.openclaw import OPENCLAW_REPO_URL, OpenClawResolver

__all__ = [
    "GitHubResolver",
    "HERMES_REPO_URL",
    "HermesResolver",
    "OPENCLAW_REPO_URL",
    "OpenClawResolver",
    "ResolvedSkill",
    "SourceResolver",
]
