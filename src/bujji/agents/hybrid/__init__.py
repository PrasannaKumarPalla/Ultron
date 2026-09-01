"""Hybrid local+cloud paradigms â€” ported from `hybrid-local-cloud-compute`.

Each module here registers one agent under ``@AgentRegistry.register("<name>")``:

    advisors          â€” executor (cloud) â†” advisor (local) â†” executor (cloud)
    conductor         â€” zero-shot planner emits a DAG of up to 5 worker calls
    minions           â€” supervisor (cloud) â†” worker (local) reactive loop
    archon            â€” layered (generator â†’ ranker â†’ fuser) inference-time search
    skillorchestra    â€” eval orchestrator: skill-routed searchâ†’reasoningâ†’answer loop
    toolorchestra     â€” prompted multi-turn dispatcher over a mixed tool/model pool

All agents share :class:`LocalCloudAgent` as the base. They are bench-agnostic:
the caller formats the prompt (using ``hybrid_prompts.format_prompt(task, bench)``
or the bench's native formatter) and hands it in via ``run(input=...)``. Task
metadata that the paradigm needs (a problem statement vs. a question, hints,
etc.) goes through ``context.metadata``.

The original ``hybrid-local-cloud-compute`` harness is the reference
implementation and stays untouched â€” these ports are the Bujji-native
versions of the same paradigms.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Import each paradigm to trigger its @AgentRegistry.register() decorator.
for _modname in (
    "advisors",
    "conductor",
    "minions",
    "archon",
    "skillorchestra",
    "toolorchestra",
    "mini_swe_agent",
    "baseline_cloud",
    "baseline_local",
):
    try:
        __import__(f"bujji.agents.hybrid.{_modname}")
    except Exception as exc:  # pragma: no cover â€” optional deps may be missing
        logger.debug("hybrid agent %s skipped: %s", _modname, exc)
