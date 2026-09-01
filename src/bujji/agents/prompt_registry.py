"""Prompt registry for orchestrator structured mode.

Adapted from IPW's ``prompt_registry.py``.  Provides the canonical system
prompt template and tool descriptions used by the structured-mode
``OrchestratorAgent`` and by the SFT/GRPO training pipelines.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from bujji.tools._stubs import BaseTool

PROMPT_VERSION = "1.0"

SYSTEM_PROMPT_TEMPLATE = """\
You are B.U.J.J.I, an advanced personal AI assistant running locally on this Windows PC. \
You are voice-activated and respond to spoken commands. \
You can control this PC, search the web, open apps, adjust settings, and answer questions. \
Be concise — responses are spoken aloud via text-to-speech. \
Never use markdown formatting, bullet points, or emoji in your responses. \
Respond in plain conversational speech, 1-3 sentences max unless more detail is needed.

For PC control tasks (open apps, set volume, take screenshot, lock screen), \
use the windows_control or shell_exec tools immediately — do not ask for confirmation.

=== AVAILABLE TOOLS ===
{tools_description}

=== TOOL SELECTION GUIDE ===
{tool_selection_guide}

=== RESPONSE FORMAT ===
You MUST respond in this EXACT format:

THOUGHT: <briefly decide which tool to use>
TOOL: <exact tool name>
INPUT: <input for the tool>

After getting tool results:
THOUGHT: <analyze the result>
FINAL_ANSWER: <spoken response — plain text, no markdown, concise>

=== CRITICAL RULES ===
1. You MUST use at least one tool for every task
2. For launching apps/websites: use windows_control (launch_app) or shell_exec
3. For volume/brightness/screenshot/lock: use windows_control
4. For web searches: use web_search
5. Final answers must be plain speech — no asterisks, no bullet points, no emoji

NOW SOLVE THE TASK.
"""

# ---------------------------------------------------------------------------
# Tool descriptions for Bujji built-in tools
# ---------------------------------------------------------------------------

TOOL_DESCRIPTIONS: Dict[str, dict] = {
    # Utility tools (instant, free, deterministic)
    "calculator": {
        "category": "utility",
        "description": (
            "CALCULATOR - Instant math computation\n"
            "  - BEST FOR: Arithmetic, algebra, trigonometry, scientific calculations\n"
            "  - STRENGTHS: Instant (<1ms), perfect accuracy, zero cost\n"
            "  - USE WHEN: Any math expression needs evaluation\n"
            "  - COST: Free\n"
            "  - Input: math expression (e.g., '15 * 7 + 23', 'sqrt(144)')"
        ),
        "examples": [
            {
                "task": "What is 847 * 293?",
                "thought": "Simple arithmetic - calculator is instant and accurate.",
                "input": "847 * 293",
            },
        ],
    },
    "think": {
        "category": "utility",
        "description": (
            "THINK - Internal reasoning scratchpad\n"
            "  - BEST FOR: Logic puzzles, step-by-step reasoning, planning\n"
            "  - STRENGTHS: Organizes thoughts, shows work, no external calls\n"
            "  - USE WHEN: Need to break down a problem before solving\n"
            "  - COST: Free\n"
            "  - Input: your detailed reasoning process"
        ),
        "examples": [
            {
                "task": "If all cats are mammals, are all cats animals?",
                "thought": "Logical syllogism - use think to reason step by step.",
                "input": "Cats subset of mammals subset of animals => yes.",
            },
        ],
    },
    "code_interpreter": {
        "category": "utility",
        "description": (
            "CODE_INTERPRETER - Python execution sandbox\n"
            "  - BEST FOR: Data processing, algorithms, simulations\n"
            "  - STRENGTHS: Full Python + numpy/pandas, handles loops\n"
            "  - USE WHEN: Problem needs programming logic\n"
            "  - COST: Free (local execution)\n"
            "  - Input: Python code to execute"
        ),
        "examples": [
            {
                "task": "Find all prime numbers less than 50",
                "thought": (
                    "Need a prime-checking algorithm - code_interpreter is ideal."
                ),
                "input": (
                    "def is_prime(n):\n"
                    "    if n < 2: return False\n"
                    "    for i in range(2, int(n**0.5)+1):\n"
                    "        if n % i == 0: return False\n"
                    "    return True\n"
                    "print([n for n in range(50) if is_prime(n)])"
                ),
            },
        ],
    },
    "web_search": {
        "category": "utility",
        "description": (
            "WEB_SEARCH - Real-time internet search\n"
            "  - BEST FOR: Current events, fact-checking, recent info\n"
            "  - STRENGTHS: Access to up-to-date information\n"
            "  - USE WHEN: Question about recent events or needs verification\n"
            "  - COST: ~$0.001 per search\n"
            "  - Input: search query string"
        ),
        "examples": [
            {
                "task": "Who won the 2024 Nobel Prize in Physics?",
                "thought": "Recent events - need web_search for current info.",
                "input": "2024 Nobel Prize Physics winner",
            },
        ],
    },
    "file_read": {
        "category": "utility",
        "description": (
            "FILE_READ - Safe file reading\n"
            "  - BEST FOR: Reading file contents with path validation\n"
            "  - STRENGTHS: Sandboxed, prevents directory traversal\n"
            "  - USE WHEN: Need to read local file contents\n"
            "  - COST: Free\n"
            "  - Input: file path"
        ),
        "examples": [],
    },
    # Memory tools
    "memory_search": {
        "category": "memory",
        "description": (
            "MEMORY_SEARCH - Search indexed documents\n"
            "  - BEST FOR: Finding relevant stored knowledge\n"
            "  - STRENGTHS: Semantic search over indexed content\n"
            "  - USE WHEN: Answer may exist in indexed documents\n"
            "  - COST: Free (local)\n"
            "  - Input: search query"
        ),
        "examples": [],
    },
    "memory_store": {
        "category": "memory",
        "description": (
            "MEMORY_STORE - Store content in memory\n"
            "  - BEST FOR: Saving information for later retrieval\n"
            "  - STRENGTHS: Persistent storage with metadata\n"
            "  - USE WHEN: Need to remember something for future queries\n"
            "  - COST: Free (local)\n"
            "  - Input: content to store"
        ),
        "examples": [],
    },
    "memory_retrieve": {
        "category": "memory",
        "description": (
            "MEMORY_RETRIEVE - Retrieve stored content by key\n"
            "  - BEST FOR: Fetching previously stored information\n"
            "  - STRENGTHS: Fast key-based retrieval\n"
            "  - USE WHEN: Know the exact key of stored content\n"
            "  - COST: Free (local)\n"
            "  - Input: content key"
        ),
        "examples": [],
    },
    "windows_control": {
        "category": "system",
        "description": (
            "WINDOWS_CONTROL - Control Windows OS\n"
            "  - BEST FOR: Volume, brightness, launching apps, clipboard, screenshots, lock screen\n"
            "  - STRENGTHS: Direct OS integration, instant execution\n"
            "  - USE WHEN: User asks to open an app, change volume/brightness, take screenshot, lock PC\n"
            "  - COST: Free\n"
            "  - Input: action + optional value/name (e.g. action='launch_app', name='chrome')\n"
            "  - Actions: set_volume, get_volume, set_brightness, launch_app, lock_screen, clipboard_read, clipboard_write, screenshot"
        ),
        "examples": [
            {
                "task": "Open Chrome",
                "thought": "User wants to launch Chrome - use windows_control with launch_app.",
                "input": '{"action": "launch_app", "name": "chrome"}',
            },
            {
                "task": "Set volume to 50",
                "thought": "User wants volume at 50% - use windows_control.",
                "input": '{"action": "set_volume", "value": 50}',
            },
        ],
    },
    "shell_exec": {
        "category": "system",
        "description": (
            "SHELL_EXEC - Run Windows shell commands\n"
            "  - BEST FOR: Opening websites, running scripts, file operations, system tasks\n"
            "  - STRENGTHS: Full shell access for any OS operation\n"
            "  - USE WHEN: Need to run a command, open a URL, or perform system tasks not covered by windows_control\n"
            "  - COST: Free\n"
            "  - Input: shell command string"
        ),
        "examples": [
            {
                "task": "Open YouTube",
                "thought": "Need to open a URL in browser - use shell_exec with start command.",
                "input": "start https://youtube.com",
            },
        ],
    },
    # LLM tool
    "llm": {
        "category": "llm",
        "description": (
            "LLM - Sub-model calls via engine\n"
            "  - BEST FOR: Natural language understanding, generation, analysis\n"
            "  - STRENGTHS: General-purpose language capabilities\n"
            "  - USE WHEN: Task needs natural language reasoning\n"
            "  - COST: Varies by engine/model\n"
            "  - Input: prompt for the model"
        ),
        "examples": [
            {
                "task": "Explain photosynthesis simply",
                "thought": "General explanation - use LLM for natural language.",
                "input": "Explain photosynthesis in simple terms.",
            },
        ],
    },
}


# Category labels for tool selection guide auto-generation
_CAT_LABELS: Dict[str, str] = {
    "math": "MATH PROBLEMS",
    "utility": "UTILITY / CODING TASKS",
    "memory": "GENERAL Q&A / FACTUAL",
    "llm": "REASONING/LOGIC",
}


def build_system_prompt(
    tool_names: Optional[List[str]] = None,
    *,
    tools: Optional[List["BaseTool"]] = None,
) -> str:
    """Build the complete system prompt for the given tools.

    Args:
        tool_names: Tool names to include.  If ``None``, uses all
            tools from :data:`TOOL_DESCRIPTIONS`.  This path is kept for
            backward compatibility with training pipelines.
        tools: Optional list of ``BaseTool`` instances.  When provided,
            rich descriptions are auto-generated from ``ToolSpec``,
            replacing the hardcoded :data:`TOOL_DESCRIPTIONS` lookup.
            Unknown / MCP tools get full descriptions instead of
            ``"Tool: {name}"``.

    Returns:
        Complete system prompt string.
    """
    # When BaseTool instances are provided, generate descriptions from spec
    if tools is not None:
        from bujji.tools._stubs import build_tool_descriptions

        desc_text = build_tool_descriptions(tools, include_cost=True)

        # Auto-generate tool selection guide by grouping tools by category
        by_cat: Dict[str, List[str]] = {}
        for t in tools:
            cat = t.spec.category or "llm"
            by_cat.setdefault(cat, []).append(t.spec.name)

        guide: list[str] = ["Choose tools based on task type:\n"]
        for cat, names in by_cat.items():
            label = _CAT_LABELS.get(cat, cat.upper())
            guide.append(f"{label}:")
            for n in names:
                guide.append(f"- {n}")
            guide.append("")

        return SYSTEM_PROMPT_TEMPLATE.format(
            tools_description=desc_text,
            tool_selection_guide="\n".join(guide),
        )

    # Backward-compat: tool_names-only path (used by training pipelines)
    if tool_names is None:
        tool_names = list(TOOL_DESCRIPTIONS)

    # Tool descriptions
    desc_lines: list[str] = []
    for name in tool_names:
        if name in TOOL_DESCRIPTIONS:
            desc = TOOL_DESCRIPTIONS[name]["description"]
        else:
            desc = f"Tool: {name}"
        desc_lines.append(f"- {name}: {desc}")

    # Group tools by category
    by_cat_names: Dict[str, List[str]] = {}
    for name in tool_names:
        cat = (
            TOOL_DESCRIPTIONS[name]["category"] if name in TOOL_DESCRIPTIONS else "llm"
        )
        by_cat_names.setdefault(cat, []).append(name)

    guide = [
        "Choose tools based on task type:\n",
    ]

    # Math
    math_lines: list[str] = []
    if "calculator" in tool_names:
        math_lines.append(
            "- Simple arithmetic/algebra -> calculator (instant, accurate)"
        )
    if "code_interpreter" in tool_names:
        math_lines.append("- Numerical algorithms -> code_interpreter (programmable)")
    if math_lines:
        guide.append("MATH PROBLEMS:")
        guide.extend(math_lines)
        guide.append("")

    # Coding
    code_lines: list[str] = []
    if "code_interpreter" in tool_names:
        code_lines.append("- Algorithm implementation/execution -> code_interpreter")
    if code_lines:
        guide.append("CODING TASKS:")
        guide.extend(code_lines)
        guide.append("")

    # Reasoning
    reasoning_lines: list[str] = []
    if "think" in tool_names:
        reasoning_lines.append(
            "- Step-by-step analysis -> think (organize thoughts first)"
        )
    llm_tools = by_cat_names.get("llm", [])
    if llm_tools:
        reasoning_lines.append(f"- Complex reasoning -> {', '.join(llm_tools)}")
    if reasoning_lines:
        guide.append("REASONING/LOGIC:")
        guide.extend(reasoning_lines)
        guide.append("")

    # General Q&A
    general_lines: list[str] = []
    if "web_search" in tool_names:
        general_lines.append("- Current events/recent info -> web_search")
    memory_tools = by_cat_names.get("memory", [])
    if memory_tools:
        general_lines.append(f"- Stored knowledge -> {', '.join(memory_tools)}")
    if general_lines:
        guide.append("GENERAL Q&A / FACTUAL:")
        guide.extend(general_lines)
        guide.append("")

    return SYSTEM_PROMPT_TEMPLATE.format(
        tools_description="\n".join(desc_lines),
        tool_selection_guide="\n".join(guide),
    )


__all__ = [
    "TOOL_DESCRIPTIONS",
    "build_system_prompt",
]
