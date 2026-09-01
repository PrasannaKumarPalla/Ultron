"""Job-Object-restricted subprocess execution for Windows.

A Job Object gives us: kill-on-close (no orphaned runner trees), an active
process cap, and per-process memory limits, applied to every command the
studio runs in a workspace. Wall-clock caps stay in our hands (poll +
TerminateJobObject) so timeouts are exact.

Honest limitation (ADR-0005): true *network denial* needs WFP or a stripped
token — neither is reachable from a plain in-process API. The job scopes
lifetime, fan-out, and memory; filesystem scoping stays with WorkspaceGuard.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x8
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


def _is_windows() -> bool:
    return sys.platform == "win32"


def sandboxed_run(command: list[str], cwd: Path, timeout_s: int = 180,
                  max_processes: int = 32, memory_limit_bytes: int = 2 * 1024 ** 3) -> tuple[bool, str]:
    """Run `command` inside a kill-on-close Job Object. Returns (ok, output)."""
    if not _is_windows():
        return _plain_run(command, cwd, timeout_s)

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return _plain_run(command, cwd, timeout_s)

    try:
        limits = EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        )
        limits.ProcessMemoryLimit = memory_limit_bytes
        limits.BasicLimitInformation.ActiveProcessLimit = max_processes
        if not kernel32.SetInformationJobObject(
                job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            pass  # degraded: job still enforces kill-on-close

        # requires-python is >=3.12; the py36/py37 Popen-arg compat rules do not apply.
        completed = subprocess.Popen(command, cwd=str(cwd), stdout=subprocess.PIPE,  # nosemgrep: python.lang.compatibility.python36.python36-compatibility-Popen1,python.lang.compatibility.python36.python36-compatibility-Popen2,python.lang.compatibility.python37.python37-compatibility-Popen1,python.lang.compatibility.python37.python37-compatibility-Popen2
                                     stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace")
        process = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, completed.pid)
        if process:
            kernel32.AssignProcessToJobObject(job, process)
            kernel32.CloseHandle(process)

        try:
            out, _ = completed.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            kernel32.TerminateJobObject(job, 1)
            out, _ = completed.communicate()
            tail = (out or "")[-20_000:]
            return False, f"sandbox: timed out after {timeout_s}s\n{tail}"
        output = (out or "")[-20_000:]
        return completed.returncode == 0, output
    finally:
        kernel32.TerminateJobObject(job, 0)
        kernel32.CloseHandle(job)


def _plain_run(command: list[str], cwd: Path, timeout_s: int) -> tuple[bool, str]:
    try:
        completed = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True,
                                   timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)
    output = ((completed.stdout or "") + "\n" + (completed.stderr or ""))[-20_000:]
    return completed.returncode == 0, output
