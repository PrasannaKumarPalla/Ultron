"""System info tool — CPU, RAM, disk, GPU stats.

Called from tool executor/orchestrator. Registered as "system_info".
No existing file. Returns JSON with cpu_percent, ram_*, disk_percent, gpu_* fields.
User instruction: do all remaining ones.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from bujji.core.registry import ToolRegistry
from bujji.core.types import ToolResult
from bujji.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)


@ToolRegistry.register("system_info")
class SystemInfoTool(BaseTool):
    """Return a JSON snapshot of system resource usage."""

    tool_id = "system_info"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="system_info",
            description=(
                "Return current system resource usage as JSON: CPU%, RAM, disk, "
                "and GPU stats (NVIDIA via pynvml, fallback to WMI/null)."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            category="system",
            timeout_seconds=10.0,
        )

    def execute(self, **_: Any) -> ToolResult:  # type: ignore[override]
        info: dict = {}

        # CPU / RAM / Disk via psutil
        try:
            import psutil

            info["cpu_percent"] = psutil.cpu_percent(interval=0.5)
            vm = psutil.virtual_memory()
            info["ram_percent"] = vm.percent
            info["ram_used_gb"] = round(vm.used / 1024**3, 2)
            info["ram_total_gb"] = round(vm.total / 1024**3, 2)
            # "/" resolves to the wrong volume on Windows when the cwd is on a
            # non-system drive; use the root of the current drive instead.
            import os as _os

            disk = psutil.disk_usage(_os.path.abspath(_os.sep))
            info["disk_percent"] = disk.percent
        except ImportError:
            logger.warning("psutil not installed — CPU/RAM/disk unavailable")
            info["cpu_percent"] = None
            info["ram_percent"] = None
            info["ram_used_gb"] = None
            info["ram_total_gb"] = None
            info["disk_percent"] = None
        except Exception as exc:
            logger.warning("psutil error: %s", exc)
            info.setdefault("cpu_percent", None)

        # GPU — try pynvml (NVIDIA) first
        gpu_name: Optional[str] = None
        gpu_vram_used_gb: Optional[float] = None
        gpu_vram_total_gb: Optional[float] = None
        gpu_util_percent: Optional[float] = None

        try:
            import pynvml

            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            gpu_name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(gpu_name, bytes):
                gpu_name = gpu_name.decode()
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            gpu_vram_used_gb = round(mem.used / 1024**3, 2)
            gpu_vram_total_gb = round(mem.total / 1024**3, 2)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            gpu_util_percent = float(util.gpu)
            pynvml.nvmlShutdown()
        except ImportError:
            # Fall back to WMI on Windows for basic GPU name
            try:
                import wmi  # type: ignore

                w = wmi.WMI()
                gpus = w.Win32_VideoController()
                if gpus:
                    gpu_name = gpus[0].Name
            except Exception:
                pass
        except Exception as exc:
            logger.debug("pynvml GPU query failed: %s", exc)

        info["gpu_name"] = gpu_name
        info["gpu_vram_used_gb"] = gpu_vram_used_gb
        info["gpu_vram_total_gb"] = gpu_vram_total_gb
        info["gpu_util_percent"] = gpu_util_percent

        return ToolResult(
            tool_name=self.tool_id,
            content=json.dumps(info),
            success=True,
        )


__all__ = ["SystemInfoTool"]
