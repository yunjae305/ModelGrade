"""Harmless Python process trees reproduce parent-exit and retained-pipe races."""
import os
import signal
import subprocess
import sys
import time

import pytest

from code_eval import cli_process


def alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(api.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Orphans can briefly remain as already-dead zombies until init reaps them.
    from pathlib import Path
    status = Path(f"/proc/{pid}/stat")
    return not (status.exists() and status.read_text().rsplit(") ", 1)[1].startswith("Z"))


def cleanup(pid):
    """Failure cleanup targets only PIDs written by this test's owned children."""
    if not alive(pid):
        return
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(1, False, pid)
        if handle:
            try:
                api.TerminateProcess(handle, 1)
            finally:
                api.CloseHandle(handle)
    else:
        os.kill(pid, signal.SIGKILL)


def wait_dead(pid):
    deadline = time.monotonic() + 3
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(.01)
    assert not alive(pid)


@pytest.mark.parametrize("retain_pipes", [True, False])
def test_parent_exit_cleans_descendants_and_retains_completed_stdout(tmp_path, retain_pipes):
    pidfile = tmp_path / "child.pid"
    options = "" if retain_pipes else ",stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL"
    program = ("import subprocess,sys; from pathlib import Path; "
               "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']" + options + "); "
               "Path(sys.argv[1]).write_text(str(child.pid)); print('completed-output',flush=True)")
    started = time.monotonic()
    try:
        result = cli_process.run_cli([sys.executable, "-u", "-c", program, str(pidfile)],
                                     text=True, encoding="utf-8", timeout=10)
        assert result.returncode == 0 and result.stdout == "completed-output\n"
        assert time.monotonic() - started < 5
        assert cli_process.active_cli_count() == 0
        wait_dead(int(pidfile.read_text()))
    finally:
        if pidfile.exists():
            cleanup(int(pidfile.read_text()))


def test_timeout_preserves_usage_kills_child_and_grandchild_but_not_other_process(tmp_path):
    child_pid, grandchild_pid = tmp_path / "child.pid", tmp_path / "grandchild.pid"
    child_program = ("import subprocess,sys,time; from pathlib import Path; "
                     "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
                     "Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)")
    program = ("import subprocess,sys,time; from pathlib import Path; "
               "child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[3]]); "
               "Path(sys.argv[2]).write_text(str(child.pid)); "
               "print('{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":123}}',flush=True); "
               "time.sleep(30)")
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            cli_process.run_cli([sys.executable, "-u", "-c", program, child_program,
                                 str(child_pid), str(grandchild_pid)], timeout=1,
                                text=True, encoding="utf-8")
        assert '"input_tokens":123' in caught.value.stdout
        assert child_pid.exists() and grandchild_pid.exists()
        wait_dead(int(child_pid.read_text()))
        wait_dead(int(grandchild_pid.read_text()))
        assert unrelated.poll() is None
        assert cli_process.active_cli_count() == 0
    finally:
        unrelated.kill()
        unrelated.wait(timeout=3)
        for path in (grandchild_pid, child_pid):
            if path.exists():
                cleanup(int(path.read_text()))


@pytest.mark.skipif(os.name != "nt", reason="Windows must assign a suspended process before any child code")
def test_job_assignment_failure_never_executes_child_code_and_cleans_registry(tmp_path, monkeypatch):
    marker = tmp_path / "must-not-exist"
    pids, jobs = [], []
    def refuse(self, pid):
        pids.append(pid)
        jobs.append(self)
        raise OSError("fixture job assignment failure")
    monkeypatch.setattr(cli_process._WindowsJob, "assign", refuse)
    try:
        with pytest.raises(OSError, match="fixture job assignment failure"):
            cli_process.run_cli([sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                                 str(marker)], timeout=3)
        assert not marker.exists()
        assert len(pids) == 1 and jobs[0]._handle is None
        wait_dead(pids[0])
        assert cli_process.active_cli_count() == 0
    finally:
        for pid in pids:
            cleanup(pid)


def test_process_creation_failure_closes_its_windows_job(monkeypatch):
    jobs = []
    if os.name == "nt":
        real_job = cli_process._WindowsJob
        def make_job():
            job = real_job()
            jobs.append(job)
            return job
        monkeypatch.setattr(cli_process, "_WindowsJob", make_job)
    def fail(*args, **kwargs):
        raise OSError("fixture spawn failure")
    monkeypatch.setattr(cli_process.subprocess, "Popen", fail)
    with pytest.raises(OSError, match="fixture spawn failure"):
        cli_process.run_cli(["never-called"], timeout=3)
    assert cli_process.active_cli_count() == 0
    assert all(job._handle is None for job in jobs)
