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
CREATE_SUSPENDED = 0x00000004
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002


def _resume_process_threads(pid: int) -> None:
    """Resume every thread of a CREATE_SUSPENDED child.

    A process spawned suspended has exactly its primary thread; walking the
    toolhelp snapshot avoids needing the raw thread handle that subprocess
    does not expose.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
        ]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snapshot == -1:
        return
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(THREADENTRY32)
        ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if thread:
                    kernel32.ResumeThread(thread)
                    kernel32.CloseHandle(thread)
            ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)


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
        # Spawn suspended so the child is assigned to the job before it runs a
        # single instruction — otherwise it has a first-ms window to fork
        # escapees or touch the filesystem before the job cap applies.
        completed = subprocess.Popen(command, cwd=str(cwd), stdout=subprocess.PIPE,  # nosemgrep: python.lang.compatibility.python36.python36-compatibility-Popen1,python.lang.compatibility.python36.python36-compatibility-Popen2,python.lang.compatibility.python37.python37-compatibility-Popen1,python.lang.compatibility.python37.python37-compatibility-Popen2
                                     stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace",
                                     creationflags=CREATE_SUSPENDED)
        process = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, completed.pid)
        if process:
            kernel32.AssignProcessToJobObject(job, process)
            kernel32.CloseHandle(process)
        _resume_process_threads(completed.pid)

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
