from __future__ import annotations

import re


AUTO_MODEL = "auto"

CODING_SIGNALS = {
    "api", "app", "backend", "bug", "build", "code", "coding", "database", "debug",
    "developer", "frontend", "implement", "integration", "refactor", "repository", "test", "ui", "web",
}

REASONING_SIGNALS = {
    "agent", "ai", "architecture", "architect", "cloud", "design", "infrastructure", "llm",
    "plan", "planning", "product", "rag", "research", "security", "strategy", "system",
}

CODING_MODELS = (
    "qwen3-coder:30b", "devstral-small-2:24b", "devstral-small-2:latest",
    "devstral:24b", "qwen2.5-coder:32b",
)

REASONING_MODELS = (
    "qwen3.6:27b", "qwen3.6:latest", "qwen3.6:35b", "gpt-oss:20b",
    "qwen3:30b", "phi4:latest",
)

GENERAL_CHAT_MODELS = (
    "phi4:latest", "qwen3.5:27b", "qwen3.6:27b", "qwen3:30b",
)


def route_general_chat_model(installed: set[str], fallback: str) -> tuple[str, str]:
    selected = next((model for model in GENERAL_CHAT_MODELS if model in installed), fallback)
    return selected, "Auto selected for responsive general conversation"


def route_model(project_name: str, description: str, objective: str, installed: set[str], fallback: str) -> tuple[str, str]:
    words = set(re.findall(r"[a-z0-9.+#-]+", f"{project_name} {description} {objective}".lower()))
    coding_score = len(words & CODING_SIGNALS)
    reasoning_score = len(words & REASONING_SIGNALS)
    preferred = CODING_MODELS if coding_score > reasoning_score else REASONING_MODELS
    secondary = REASONING_MODELS if preferred is CODING_MODELS else CODING_MODELS
    selected = next((model for model in (*preferred, *secondary) if model in installed), fallback)
    category = "coding and implementation" if preferred is CODING_MODELS else "architecture and reasoning"
    return selected, f"Auto selected for {category} signals ({coding_score} coding, {reasoning_score} reasoning)"
