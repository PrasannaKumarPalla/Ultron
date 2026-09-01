"""Windows full machine control — volume, brightness, apps, clipboard, screenshot,
media keys, mouse, keyboard, window management, process control.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import tempfile
import time
from typing import Any

from bujji.core.registry import ToolRegistry
from bujji.core.types import ToolResult
from bujji.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------

# ctypes.windll exists only on Windows. Guard so this module still imports on
# other platforms (the package eagerly imports it to register the tool); the
# Win32 handles stay None and the tool's methods fail clearly if invoked there.
if os.name == "nt":
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
else:  # pragma: no cover - non-Windows import guard
    user32 = None
    kernel32 = None

# Virtual key codes
_VK = {
    "play_pause": 0xB3,
    "next_track": 0xB0,
    "prev_track": 0xB1,
    "stop":       0xB2,
    "volume_up":  0xAF,
    "volume_down":0xAE,
    "mute":       0xAD,
}

KEYEVENTF_KEYUP = 0x0002


def _press_vk(vk: int) -> None:
    """Send a single virtual-key press + release."""
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


_ACTIONS = {
    # existing
    "set_volume", "get_volume", "set_brightness",
    "launch_app", "lock_screen",
    "clipboard_read", "clipboard_write", "screenshot",
    # new — media
    "media_play_pause", "media_next", "media_prev", "media_stop",
    # new — system
    "volume_up", "volume_down", "mute",
    "sleep", "shutdown", "restart",
    # new — mouse
    "mouse_move", "mouse_click", "mouse_scroll",
    # new — keyboard
    "key_press", "type_text", "hotkey",
    # new — windows
    "focus_window", "list_windows", "close_window",
    "minimize_window", "maximize_window",
    # new — process
    "list_processes", "kill_process",
    # new — misc
    "get_cursor_pos", "get_screen_size",
}


@ToolRegistry.register("windows_control")
class WindowsControlTool(BaseTool):
    """Full Windows machine control: media, mouse, keyboard, windows, processes."""

    tool_id = "windows_control"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="windows_control",
            description=(
                "Full Windows machine control. Actions: "
                "media_play_pause, media_next, media_prev, media_stop, "
                "volume_up, volume_down, mute, set_volume, get_volume, set_brightness, "
                "launch_app, lock_screen, sleep, shutdown, restart, "
                "mouse_move, mouse_click, mouse_scroll, "
                "key_press, type_text, hotkey, "
                "focus_window, list_windows, close_window, minimize_window, maximize_window, "
                "list_processes, kill_process, "
                "clipboard_read, clipboard_write, screenshot, "
                "get_cursor_pos, get_screen_size."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Action to perform.",
                        "enum": sorted(_ACTIONS),
                    },
                    "value": {
                        "type": "number",
                        "description": "Numeric value: volume 0-100, brightness 0-100, scroll lines.",
                    },
                    "x": {"type": "number", "description": "Screen X coordinate for mouse actions."},
                    "y": {"type": "number", "description": "Screen Y coordinate for mouse actions."},
                    "button": {
                        "type": "string",
                        "description": "Mouse button: left (default), right, middle.",
                        "enum": ["left", "right", "middle"],
                    },
                    "clicks": {"type": "number", "description": "Number of clicks (default 1)."},
                    "name": {"type": "string", "description": "App name / window title / process name."},
                    "text": {"type": "string", "description": "Text to type or write to clipboard."},
                    "key": {
                        "type": "string",
                        "description": "Key name for key_press (e.g. 'enter', 'escape', 'space', 'f5', 'ctrl+c').",
                    },
                    "keys": {
                        "type": "string",
                        "description": "Hotkey combo for hotkey action e.g. 'ctrl+c', 'win+d', 'alt+f4'.",
                    },
                    "path": {"type": "string", "description": "File path for screenshot."},
                    "pid": {"type": "number", "description": "Process ID for kill_process."},
                },
                "required": ["action"],
            },
            category="system",
            timeout_seconds=15.0,
        )

    def execute(self, action: str = "", **kwargs: Any) -> ToolResult:  # type: ignore[override]
        if action not in _ACTIONS:
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Unknown action '{action}'. Available: {', '.join(sorted(_ACTIONS))}",
                success=False,
            )
        handler = getattr(self, f"_do_{action}")
        return handler(**kwargs)

    # ------------------------------------------------------------------
    # Media keys
    # ------------------------------------------------------------------

    def _do_media_play_pause(self, **_: Any) -> ToolResult:
        _press_vk(_VK["play_pause"])
        return ToolResult(tool_name=self.tool_id, content="Play/Pause toggled", success=True)

    def _do_media_next(self, **_: Any) -> ToolResult:
        _press_vk(_VK["next_track"])
        return ToolResult(tool_name=self.tool_id, content="Next track", success=True)

    def _do_media_prev(self, **_: Any) -> ToolResult:
        _press_vk(_VK["prev_track"])
        return ToolResult(tool_name=self.tool_id, content="Previous track", success=True)

    def _do_media_stop(self, **_: Any) -> ToolResult:
        _press_vk(_VK["stop"])
        return ToolResult(tool_name=self.tool_id, content="Media stopped", success=True)

    def _do_volume_up(self, **_: Any) -> ToolResult:
        _press_vk(_VK["volume_up"])
        return ToolResult(tool_name=self.tool_id, content="Volume up", success=True)

    def _do_volume_down(self, **_: Any) -> ToolResult:
        _press_vk(_VK["volume_down"])
        return ToolResult(tool_name=self.tool_id, content="Volume down", success=True)

    def _do_mute(self, **_: Any) -> ToolResult:
        _press_vk(_VK["mute"])
        return ToolResult(tool_name=self.tool_id, content="Mute toggled", success=True)

    # ------------------------------------------------------------------
    # Volume / Brightness
    # ------------------------------------------------------------------

    @staticmethod
    def _endpoint_volume():
        """Return the speakers' IAudioEndpointVolume across pycaw API versions."""
        from pycaw.pycaw import AudioUtilities

        speakers = AudioUtilities.GetSpeakers()
        # pycaw >= 2024: GetSpeakers() returns an AudioDevice wrapper
        ev = getattr(speakers, "EndpointVolume", None)
        if ev is not None:
            return ev
        # older pycaw: raw IMMDevice needing manual activation
        from pycaw.pycaw import IAudioEndpointVolume
        from comtypes import CLSCTX_ALL

        interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return interface.QueryInterface(IAudioEndpointVolume)

    def _do_set_volume(self, value: float = 50, **_: Any) -> ToolResult:
        vol = max(0, min(100, int(value)))
        try:
            volume = self._endpoint_volume()
            volume.SetMasterVolumeLevelScalar(vol / 100.0, None)
            return ToolResult(tool_name=self.tool_id, content=f"Volume set to {vol}%", success=True)
        except Exception:
            pass
        try:
            subprocess.run(["nircmd", "setsysvolume", str(int(vol / 100.0 * 65535))], check=True, timeout=5)
            return ToolResult(tool_name=self.tool_id, content=f"Volume set to {vol}%", success=True)
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=f"set_volume failed: {exc}", success=False)

    def _do_get_volume(self, **_: Any) -> ToolResult:
        try:
            volume = self._endpoint_volume()
            level = int(volume.GetMasterVolumeLevelScalar() * 100)
            return ToolResult(tool_name=self.tool_id, content=str(level), success=True)
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=f"get_volume failed: {exc}", success=False)

    def _do_set_brightness(self, value: float = 50, **_: Any) -> ToolResult:
        brt = max(0, min(100, int(value)))
        try:
            import screen_brightness_control as sbc
            sbc.set_brightness(brt)
            return ToolResult(tool_name=self.tool_id, content=f"Brightness set to {brt}%", success=True)
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=f"set_brightness failed: {exc}", success=False)

    # ------------------------------------------------------------------
    # App launch / system
    # ------------------------------------------------------------------

    def _do_launch_app(self, name: str = "", **_: Any) -> ToolResult:
        if not name:
            return ToolResult(tool_name=self.tool_id, content="'name' required", success=False)
        try:
            # os.startfile resolves app names / paths / documents via the shell
            # file-association layer without spawning a command interpreter, so
            # a name like "notepad & del x" cannot inject a second command.
            try:
                os.startfile(name)  # noqa: S606  (Windows-only, no shell)
            except OSError:
                subprocess.Popen([name])
            return ToolResult(tool_name=self.tool_id, content=f"Launched: {name}", success=True)
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=f"launch_app failed: {exc}", success=False)

    def _do_lock_screen(self, **_: Any) -> ToolResult:
        user32.LockWorkStation()
        return ToolResult(tool_name=self.tool_id, content="Screen locked", success=True)

    def _do_sleep(self, **_: Any) -> ToolResult:
        subprocess.Popen("rundll32.exe powrprof.dll,SetSuspendState 0,1,0", shell=True)
        return ToolResult(tool_name=self.tool_id, content="Sleeping", success=True)

    def _do_shutdown(self, **_: Any) -> ToolResult:
        subprocess.Popen("shutdown /s /t 10", shell=True)
        return ToolResult(tool_name=self.tool_id, content="Shutting down in 10 seconds", success=True)

    def _do_restart(self, **_: Any) -> ToolResult:
        subprocess.Popen("shutdown /r /t 10", shell=True)
        return ToolResult(tool_name=self.tool_id, content="Restarting in 10 seconds", success=True)

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------

    def _do_mouse_move(self, x: float = 0, y: float = 0, **_: Any) -> ToolResult:
        user32.SetCursorPos(int(x), int(y))
        return ToolResult(tool_name=self.tool_id, content=f"Mouse moved to ({int(x)}, {int(y)})", success=True)

    def _do_mouse_click(self, x: float = -1, y: float = -1,
                        button: str = "left", clicks: float = 1, **_: Any) -> ToolResult:
        if x >= 0 and y >= 0:
            user32.SetCursorPos(int(x), int(y))
            time.sleep(0.05)
        # Win32 mouse_event flags
        down_flags = {"left": 0x0002, "right": 0x0008, "middle": 0x0020}
        up_flags   = {"left": 0x0004, "right": 0x0010, "middle": 0x0040}
        df = down_flags.get(button, 0x0002)
        uf = up_flags.get(button, 0x0004)
        for _ in range(int(clicks)):
            user32.mouse_event(df, 0, 0, 0, 0)
            time.sleep(0.05)
            user32.mouse_event(uf, 0, 0, 0, 0)
            time.sleep(0.05)
        return ToolResult(
            tool_name=self.tool_id,
            content=f"{button} click x{int(clicks)} at ({int(x)},{int(y)})",
            success=True,
        )

    def _do_mouse_scroll(self, value: float = 3, **_: Any) -> ToolResult:
        # positive = scroll up, negative = scroll down
        lines = int(value)
        user32.mouse_event(0x0800, 0, 0, lines * 120, 0)
        return ToolResult(tool_name=self.tool_id, content=f"Scrolled {lines} lines", success=True)

    def _do_get_cursor_pos(self, **_: Any) -> ToolResult:
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return ToolResult(tool_name=self.tool_id, content=f"({pt.x}, {pt.y})", success=True)

    def _do_get_screen_size(self, **_: Any) -> ToolResult:
        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)
        return ToolResult(tool_name=self.tool_id, content=f"{w}x{h}", success=True)

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

    # Map friendly key names to virtual key codes
    _KEY_MAP: dict[str, int] = {
        "enter": 0x0D, "return": 0x0D,
        "escape": 0x1B, "esc": 0x1B,
        "space": 0x20,
        "tab": 0x09,
        "backspace": 0x08,
        "delete": 0x2E,
        "home": 0x24, "end": 0x23,
        "pageup": 0x21, "pagedown": 0x22,
        "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
        "f1":0x70,"f2":0x71,"f3":0x72,"f4":0x73,"f5":0x74,"f6":0x75,
        "f7":0x76,"f8":0x77,"f9":0x78,"f10":0x79,"f11":0x7A,"f12":0x7B,
        "ctrl": 0x11, "control": 0x11,
        "alt": 0x12,
        "shift": 0x10,
        "win": 0x5B, "windows": 0x5B,
        "printscreen": 0x2C,
        "insert": 0x2D,
        "capslock": 0x14,
        "numlock": 0x90,
        "scrolllock": 0x91,
    }

    def _vk_from_name(self, name: str) -> int:
        name = name.lower().strip()
        if name in self._KEY_MAP:
            return self._KEY_MAP[name]
        # Single character
        if len(name) == 1:
            return user32.VkKeyScanW(ord(name)) & 0xFF
        # Fallback: try MapVirtualKey
        return 0

    def _do_key_press(self, key: str = "", **_: Any) -> ToolResult:
        if not key:
            return ToolResult(tool_name=self.tool_id, content="'key' required", success=False)
        # Support combos like "ctrl+c" via key_press too
        if "+" in key:
            return self._do_hotkey(keys=key)
        vk = self._vk_from_name(key)
        if not vk:
            return ToolResult(tool_name=self.tool_id, content=f"Unknown key: {key}", success=False)
        _press_vk(vk)
        return ToolResult(tool_name=self.tool_id, content=f"Key pressed: {key}", success=True)

    def _do_hotkey(self, keys: str = "", key: str = "", **_: Any) -> ToolResult:
        combo = keys or key
        if not combo:
            return ToolResult(tool_name=self.tool_id, content="'keys' required", success=False)
        parts = [p.strip() for p in combo.lower().split("+")]
        vks = [self._vk_from_name(p) for p in parts]
        if not all(vks):
            return ToolResult(tool_name=self.tool_id, content=f"Unknown key in combo: {combo}", success=False)
        # Press all modifiers down, then main key, then release all
        for vk in vks:
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.03)
        for vk in reversed(vks):
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.03)
        return ToolResult(tool_name=self.tool_id, content=f"Hotkey: {combo}", success=True)

    def _do_type_text(self, text: str = "", **_: Any) -> ToolResult:
        if not text:
            return ToolResult(tool_name=self.tool_id, content="'text' required", success=False)
        # Use clipboard paste for reliable Unicode typing
        try:
            import pyperclip
            prev = pyperclip.paste()
            pyperclip.copy(text)
            time.sleep(0.1)
            # Ctrl+V
            user32.keybd_event(0x11, 0, 0, 0)
            user32.keybd_event(0x56, 0, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(0x56, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(0x11, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.1)
            pyperclip.copy(prev)  # restore clipboard
            return ToolResult(tool_name=self.tool_id, content=f"Typed: {text[:50]}", success=True)
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=f"type_text failed: {exc}", success=False)

    # ------------------------------------------------------------------
    # Window management
    # ------------------------------------------------------------------

    def _do_list_windows(self, **_: Any) -> ToolResult:
        titles: list[str] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
        def enum_cb(hwnd, _lparam):  # type: ignore[misc]
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    titles.append(buf.value)
            return True

        user32.EnumWindows(enum_cb, 0)
        return ToolResult(tool_name=self.tool_id, content="\n".join(titles[:40]), success=True)

    def _find_hwnd(self, name: str) -> int:
        """Find first window whose title contains name (case-insensitive)."""
        found: list[int] = []
        nl = name.lower()

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
        def enum_cb(hwnd, _lparam):  # type: ignore[misc]
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    if nl in buf.value.lower():
                        found.append(hwnd)
            return True

        user32.EnumWindows(enum_cb, 0)
        return found[0] if found else 0

    def _do_focus_window(self, name: str = "", **_: Any) -> ToolResult:
        if not name:
            return ToolResult(tool_name=self.tool_id, content="'name' required", success=False)
        hwnd = self._find_hwnd(name)
        if not hwnd:
            return ToolResult(tool_name=self.tool_id, content=f"Window not found: {name}", success=False)
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return ToolResult(tool_name=self.tool_id, content=f"Focused: {name}", success=True)

    def _do_close_window(self, name: str = "", **_: Any) -> ToolResult:
        if not name:
            return ToolResult(tool_name=self.tool_id, content="'name' required", success=False)
        hwnd = self._find_hwnd(name)
        if not hwnd:
            return ToolResult(tool_name=self.tool_id, content=f"Window not found: {name}", success=False)
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        return ToolResult(tool_name=self.tool_id, content=f"Closed: {name}", success=True)

    def _do_minimize_window(self, name: str = "", **_: Any) -> ToolResult:
        hwnd = self._find_hwnd(name) if name else user32.GetForegroundWindow()
        if not hwnd:
            return ToolResult(tool_name=self.tool_id, content="Window not found", success=False)
        user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        return ToolResult(tool_name=self.tool_id, content="Minimized", success=True)

    def _do_maximize_window(self, name: str = "", **_: Any) -> ToolResult:
        hwnd = self._find_hwnd(name) if name else user32.GetForegroundWindow()
        if not hwnd:
            return ToolResult(tool_name=self.tool_id, content="Window not found", success=False)
        user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
        return ToolResult(tool_name=self.tool_id, content="Maximized", success=True)

    # ------------------------------------------------------------------
    # Processes
    # ------------------------------------------------------------------

    def _do_list_processes(self, **_: Any) -> ToolResult:
        try:
            result = subprocess.run(
                ["tasklist", "/fo", "csv", "/nh"],
                capture_output=True, text=True, timeout=10,
            )
            lines = result.stdout.strip().splitlines()
            procs = []
            for line in lines[:50]:
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 2:
                    procs.append(f"{parts[0]} (PID {parts[1]})")
            return ToolResult(tool_name=self.tool_id, content="\n".join(procs), success=True)
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=f"list_processes failed: {exc}", success=False)

    def _do_kill_process(self, name: str = "", pid: float = 0, **_: Any) -> ToolResult:
        if pid:
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/F"], capture_output=True)
            return ToolResult(tool_name=self.tool_id, content=f"Killed PID {int(pid)}", success=True)
        if name:
            subprocess.run(["taskkill", "/IM", name, "/F"], capture_output=True)
            return ToolResult(tool_name=self.tool_id, content=f"Killed {name}", success=True)
        return ToolResult(tool_name=self.tool_id, content="'name' or 'pid' required", success=False)

    # ------------------------------------------------------------------
    # Clipboard
    # ------------------------------------------------------------------

    def _do_clipboard_read(self, **_: Any) -> ToolResult:
        try:
            import pyperclip
            return ToolResult(tool_name=self.tool_id, content=pyperclip.paste() or "", success=True)
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=f"clipboard_read failed: {exc}", success=False)

    def _do_clipboard_write(self, text: str = "", **_: Any) -> ToolResult:
        try:
            import pyperclip
            pyperclip.copy(text)
            return ToolResult(tool_name=self.tool_id, content="Clipboard updated", success=True)
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=f"clipboard_write failed: {exc}", success=False)

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    def _do_screenshot(self, path: str = "", **_: Any) -> ToolResult:
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            if not path:
                fd, path = tempfile.mkstemp(suffix=".png", prefix="bujji_screenshot_")
                os.close(fd)
            img.save(path)
            return ToolResult(tool_name=self.tool_id, content=path, success=True)
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=f"screenshot failed: {exc}", success=False)


__all__ = ["WindowsControlTool"]
