from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Specialist:
    role_id: str
    name: str
    purpose: str
    skills: tuple[str, ...]
    permissions: tuple[str, ...]
    signals: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"role_id": self.role_id, "name": self.name, "purpose": self.purpose,
                "skills": list(self.skills), "permissions": list(self.permissions)}


CATALOG = (
    Specialist("cloud-architect", "Senior Cloud Architect", "Design architecture, boundaries, reliability, and deployment.", ("architecture", "cloud", "reliability", "security-design"), ("read", "write:docs")),
    Specialist("product-manager", "AI Product Manager", "Translate the objective into user outcomes and acceptance criteria.", ("requirements", "prioritization", "acceptance-criteria"), ("read", "write:docs")),
    Specialist("backend-developer", "Senior Backend Developer", "Implement services, APIs, persistence, and business logic.", ("python", "api-design", "databases", "integration"), ("read", "write:code")),
    Specialist("frontend-developer", "Senior Frontend Developer", "Implement responsive interactive product interfaces.", ("html", "css", "javascript", "accessibility"), ("read", "write:ui"), ("ui", "dashboard", "web", "frontend", "portal", "app")),
    Specialist("ui-expert", "UI/UX Expert", "Review accessibility, usability, hierarchy, responsiveness, and copy.", ("ux-review", "accessibility", "responsive-design"), ("read", "write:ui"), ("ui", "dashboard", "web", "frontend", "mobile")),
    Specialist("security-engineer", "Security Engineer", "Threat-model authentication, secrets, input, and dependencies.", ("threat-modeling", "owasp", "secrets", "secure-coding"), ("read", "write:docs"), ("auth", "authentication", "login", "payment", "secret", "security", "client", "health", "financial")),
    Specialist("data-engineer", "Data Engineer", "Design data models, pipelines, quality, and lifecycle.", ("data-modeling", "sql", "pipelines", "data-quality"), ("read", "write:code"), ("data", "analytics", "etl", "database", "report", "pipeline")),
    Specialist("ml-engineer", "ML/AI Engineer", "Design model, retrieval, prompt, evaluation, and inference components.", ("llm", "rag", "embeddings", "evaluation", "inference"), ("read", "write:code"), ("ai", "llm", "agent", "rag", "model", "embedding", "inference")),
    Specialist("devops-engineer", "DevOps / SRE", "Define build, deployment, observability, and operational recovery.", ("ci-cd", "containers", "observability", "sre"), ("read", "write:ops"), ("deploy", "deployment", "cloud", "docker", "kubernetes", "production", "monitor", "monitoring", "infra")),
    Specialist("qa-engineer", "Senior QA Engineer", "Verify behavior, regressions, performance, and acceptance evidence.", ("test-design", "automation", "regression", "quality-gates"), ("read", "run:tests")),
)


class TeamPlanner:
    """Deterministic, auditable team formation based on mission signals."""

    @staticmethod
    def plan(objective: str) -> list[dict]:
        text = objective.lower()
        required = {"cloud-architect", "product-manager", "backend-developer", "qa-engineer"}
        for specialist in CATALOG:
            if specialist.signals and any(re.search(rf"\b{re.escape(signal)}\b", text) for signal in specialist.signals):
                required.add(specialist.role_id)
        order = [specialist for specialist in CATALOG if specialist.role_id in required]
        return [specialist.as_dict() for specialist in order]
