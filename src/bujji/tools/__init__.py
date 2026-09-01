"""Tools primitive â€” tool system with ABC interface and built-in tools."""

from __future__ import annotations

from bujji.tools._stubs import BaseTool, ToolExecutor, ToolSpec

# Import built-in tools to trigger @ToolRegistry.register() decorators.
# Each is wrapped in try/except so the package loads even before the
# individual tool modules are created.
try:
    import bujji.tools.calculator  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.think  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.retrieval  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.llm_tool  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.file_read  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.web_search  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.code_interpreter  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.code_interpreter_docker  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.repl  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.storage_tools  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.mcp_adapter  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.channel_tools  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.http_request  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.docker_shell_exec  # noqa: F401
    import bujji.tools.shell_exec  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.memory_manage  # noqa: F401
except ImportError:
    pass
try:
    import bujji.tools.user_profile_manage  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.skill_manage  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.file_write  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.windows_control  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.apply_patch  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.git_tool  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.db_query  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.pdf_tool  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.image_tool  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.audio_tool  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.knowledge_tools  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.text_to_speech  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.digest_collect  # noqa: F401
except ImportError:
    pass

# Personal-assistant capability tools (BUJJI goals). Each module imports
# cleanly; optional deps (Google API, Playwright, HA creds) only fail at
# call-time with a helpful setup message, so enabling them is safe.
try:
    import bujji.tools.system_info  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.home_assistant  # noqa: F401  # home automation
except ImportError:
    pass

try:
    import bujji.tools.calendar_tool  # noqa: F401  # calendar (Google)
except ImportError:
    pass

try:
    import bujji.tools.browser  # noqa: F401  # shopping/booking automation
except ImportError:
    pass

try:
    import bujji.tools.switch_voice  # noqa: F401  # runtime TTS voice switching
except ImportError:
    pass

try:
    import bujji.tools.self_dev  # noqa: F401  # BUJJI self-improvement
except ImportError:
    pass

try:
    import bujji.tools.dev_loop  # noqa: F401  # code+test loop for any project
except ImportError:
    pass

__all__ = ["BaseTool", "ToolExecutor", "ToolSpec"]
