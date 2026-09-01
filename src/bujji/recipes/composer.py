"""Composer bridge — convert a Recipe into an OperatorManifest.

A pure-function transformation that lets the unified Recipe format drive
the operator system without it needing to know about recipes directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bujji.operators.types import OperatorManifest
    from bujji.recipes.loader import Recipe


def recipe_to_operator(recipe: Recipe) -> OperatorManifest:
    """Build an ``OperatorManifest`` from a recipe.

    Maps the recipe's agent, schedule, and channel fields into the
    operator manifest format used by ``OperatorManager``.

    Raises:
        ValueError: If schedule information is missing.
    """
    from bujji.operators.types import OperatorManifest

    if not recipe.schedule_type:
        raise ValueError(
            f"Recipe '{recipe.name}' has no [schedule] section.  "
            "Operator recipes must define schedule_type and schedule_value."
        )

    prompt = recipe.system_prompt or ""
    prompt_path = recipe.system_prompt_path or ""

    return OperatorManifest(
        id=recipe.name,
        name=recipe.name,
        version=recipe.version,
        description=recipe.description,
        tools=list(recipe.tools),
        system_prompt=prompt,
        system_prompt_path=prompt_path,
        max_turns=recipe.max_turns or 20,
        temperature=recipe.temperature or 0.3,
        schedule_type=recipe.schedule_type,
        schedule_value=recipe.schedule_value or "300",
        required_capabilities=list(recipe.required_capabilities),
    )


__all__ = ["recipe_to_operator"]
