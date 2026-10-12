"""Run only our CLI children in isolated process groups with bounded cancellation."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from concurrent.futures import CancelledError


class _WindowsJob:
    """Own a suspended process and all its descendants before any code can run."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_process_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        self._ctypes, self._lock = ctypes, threading.Lock()
        self._api = api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        self._handle = api.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not api.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, pid):
        # The process is still suspended, so it cannot spawn outside this job.
        handle = self._api.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not handle:
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        try:
            with self._lock:
                if not self._handle or not self._api.AssignProcessToJobObject(self._handle, handle):
                    raise self._ctypes.WinError(self._ctypes.get_last_error())
        finally:
            self._api.CloseHandle(handle)

    def resume(self, pid):
        # Popen closes CreateProcess's primary-thread handle. Before resuming,
        # find only threads owned by this still-suspended, already-job-bound PID.
        ctypes, api = self._ctypes, self._api
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                        ("thread_id", wintypes.DWORD), ("owner_pid", wintypes.DWORD),
                        ("base_priority", wintypes.LONG), ("delta_priority", wintypes.LONG),
                        ("flags", wintypes.DWORD)]

        api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        api.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        api.Thread32First.restype = wintypes.BOOL
        api.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        api.Thread32Next.restype = wintypes.BOOL
        api.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenThread.restype = wintypes.HANDLE
        api.GetProcessIdOfThread.argtypes = [wintypes.HANDLE]
        api.GetProcessIdOfThread.restype = wintypes.DWORD
        api.ResumeThread.argtypes = [wintypes.HANDLE]
        api.ResumeThread.restype = wintypes.DWORD
        snapshot = api.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        thread_ids = []
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            more = api.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.owner_pid == pid:
                    thread_ids.append(entry.thread_id)
                entry.size = ctypes.sizeof(entry)
                more = api.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            api.CloseHandle(snapshot)
        if not thread_ids:
            raise OSError("Cannot find the suspended CLI process thread")
        for thread_id in thread_ids:
            handle = api.OpenThread(0x0002 | 0x0800, False, thread_id)  # SUSPEND_RESUME | QUERY_LIMITED_INFORMATION
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                # Recheck after opening: a concurrent stop could have freed a
                # snapshot thread ID before OpenThread acquired its handle.
                if api.GetProcessIdOfThread(handle) != pid:
                    raise OSError("Suspended CLI thread owner changed")
                if api.ResumeThread(handle) == 0xFFFFFFFF:
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                api.CloseHandle(handle)

    def close(self):
        with self._lock:
            if self._handle:
                if not self._api.CloseHandle(self._handle):
                    raise self._ctypes.WinError(self._ctypes.get_last_error())
                self._handle = None


class CliCancelled(CancelledError):
    def __init__(self, stdout="", stderr="", *, started=False):
        super().__init__("평가 중단 요청으로 CLI 실행을 종료했습니다.")
        self.stdout, self.stderr, self.started = stdout, stderr, started


_processes = set()
_lock = threading.Lock()


def _terminate_tree(process):
    # Never search for or stop unrelated Codex/Claude processes by executable name.
    job = getattr(process, "_aiona_job", None)
    if job is not None:
        job.close()  # Also kills descendants when the original parent has exited.
    elif os.name == "nt" and process.poll() is None:
        try:
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=3, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def terminate_active_cli():
    with _lock:
        processes = list(_processes)
    # Each runner also observes its cancellation event and kills its own tree.
    for process in processes:
        _terminate_tree(process)


def active_cli_count():
    with _lock:
        return len(_processes)


def run_cli(args, *, input=None, timeout=None, cancel_event=None, capture_output=True, **kwargs):
    """subprocess.run-compatible subset; retain partial output for cost accounting."""
    if cancel_event is not None and cancel_event.is_set():
        raise CliCancelled()
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    if os.name == "nt":
        # Assign the child to its private job before it can launch descendants.
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004
    else:
        kwargs["start_new_session"] = True
    started = time.monotonic()
    job = _WindowsJob() if os.name == "nt" else None
    process, reader = None, None
    reader_started = False
    output, errors = [], []
    finished = threading.Event()
    def communicate():
        try:
            output.append(process.communicate(input=input))
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()
    # On Windows communicate() can block while writing a large stdin payload;
    # monitor it from here so cancellation also covers that initial write.
    try:
        process = subprocess.Popen(args, **kwargs)
        if job is not None:
            process._aiona_job = job
            job.assign(process.pid)
        with _lock:
            _processes.add(process)
        if cancel_event is not None and cancel_event.is_set():
            raise CliCancelled(started=os.name != "nt")
        if job is not None:
            job.resume(process.pid)
        reader = threading.Thread(target=communicate, name="cli-output", daemon=True)
        reader.start()
        reader_started = True
        while True:
            cancelled = cancel_event is not None and cancel_event.is_set()
            expired = timeout is not None and time.monotonic() - started >= timeout
            if (cancelled or expired) and not finished.is_set():
                _terminate_tree(process)
                finished.wait(2)
                stdout, stderr = output[0] if output else ("", "")
                if cancelled:
                    raise CliCancelled(stdout, stderr, started=True)
                raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr)
            if process.poll() is not None and not finished.is_set():
                # Descendants may retain stdout after the parent exits. Closing
                # our job/group releases those pipes and preserves collected text.
                _terminate_tree(process)
            if finished.wait(0.1):
                if errors:
                    raise errors[0]
                stdout, stderr = output[0]
                # Completed output wins a race with cancellation. The caller validates it.
                return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    except BaseException:
        if process is not None:
            _terminate_tree(process)
            if reader_started and reader.is_alive():
                finished.wait(2)
            elif not reader_started:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        raise
    finally:
        try:
            if job is not None:
                job.close()
            elif process is not None:
                _terminate_tree(process)
        finally:
            with _lock:
                _processes.discard(process)
            if process is not None and (not reader_started or finished.is_set()):
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
