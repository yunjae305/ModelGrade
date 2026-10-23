"""Exercise owned process cancellation with harmless local Python children."""
import os
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_eval.cli_process import CliCancelled, active_cli_count, run_cli
from code_eval.codex_cli_client import CodexCliClient, CodexCliConfig
from code_eval.claude_cli_client import ClaudeCliClient, ClaudeCliConfig
from code_eval.jev_eval import jev_request


def wait_for(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError('fixture did not reach expected state')


def alive(pid):
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_local_tree_cancel_is_bounded_and_preserves_partial_stdout(tmp_path):
    pidfile = tmp_path / 'child.pid'
    token, outcomes = threading.Event(), []
    program = ('import subprocess,sys,time; from pathlib import Path; '
               'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]); '
               'Path(sys.argv[1]).write_text(str(child.pid)); '
               'print("partial usage",flush=True); time.sleep(30)')
    def work():
        try:
            run_cli([sys.executable, '-u', '-c', program, str(pidfile)],
                    text=True, encoding='utf-8', timeout=25, cancel_event=token)
        except CliCancelled as exc:
            outcomes.append(exc)
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    wait_for(lambda: pidfile.exists() and pidfile.read_text())
    child = int(pidfile.read_text())
    assert alive(child)
    started = time.monotonic()
    token.set()
    worker.join(6)
    assert not worker.is_alive() and time.monotonic() - started < 6
    assert len(outcomes) == 1 and outcomes[0].started
    assert 'partial usage' in outcomes[0].stdout
    wait_for(lambda: not alive(child))
    assert active_cli_count() == 0


def test_cancel_interrupts_blocked_large_stdin():
    token = threading.Event()
    timer = threading.Timer(.3, token.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(CliCancelled):
            run_cli([sys.executable, '-c', 'import time;time.sleep(30)'], input='x' * 2_000_000,
                    text=True, encoding='utf-8', timeout=25, cancel_event=token)
        assert time.monotonic() - started < 6
        assert active_cli_count() == 0
    finally:
        timer.cancel()


def test_timeout_preserves_output_and_reaps_process():
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run_cli([sys.executable, '-u', '-c', 'import time;print("usage",flush=True);time.sleep(30)'],
                text=True, encoding='utf-8', timeout=.3)
    assert 'usage' in caught.value.stdout and active_cli_count() == 0


def test_uncancelled_unicode_io_is_exact():
    text = '  질문😀\r\n답변  '
    # Binary mode avoids subprocess universal-newline translation of the fixture.
    result = run_cli([sys.executable, '-c', 'import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())'],
                     input=text.encode('utf-8'), timeout=3)
    assert result.returncode == 0 and result.stdout == text.encode('utf-8')


@pytest.mark.parametrize('module,client', [
    ('code_eval.codex_cli_client', CodexCliClient(CodexCliConfig())),
    ('code_eval.claude_cli_client', ClaudeCliClient(ClaudeCliConfig()))])
def test_live_transport_cancel_returns_skipped_without_retry(monkeypatch, module, client):
    calls, token = [], threading.Event()
    def fake_process(*args, **kwargs):
        calls.append(kwargs)
        token.set()
        raise CliCancelled('{"type":"turn.started"}', started=True)
    monkeypatch.setattr(module + '.run_cli', fake_process)
    result = client.system_one({'question': 'q', 'answer': 'a'}, jev_request('q', 'a', ('J1',))['questions'], cancel_event=token)
    assert result.status == 'SKIPPED' and result.attempts == 1
    assert len(calls) == 1 and calls[0]['cancel_event'] is token


def test_claude_returned_usage_survives_cancellation():
    token = threading.Event()
    def runner(*args, **kwargs):
        token.set()
        return SimpleNamespace(returncode=1, stderr='', stdout=json.dumps({
            'usage': {'input_tokens': 123, 'output_tokens': 7}, 'total_cost_usd': .25}))
    call = ClaudeCliClient(ClaudeCliConfig(), runner).system_one(
        {'question': 'q', 'answer': 'a'}, jev_request('q', 'a', ('J1',))['questions'], cancel_event=token)
    assert call.status == 'SKIPPED' and call.attempts == 1
    assert call.accounting['usage']['input_tokens'] == 123
    assert call.accounting['reported_cost_usd'] == .25


def test_codex_completed_verdict_wins_cancellation_race_without_pending_web_checks(tmp_path):
    token = threading.Event()
    def runner(cmd, **kwargs):
        Path(cmd[cmd.index('--output-last-message') + 1]).write_text(json.dumps({
            'J1': {'choice':'PASS', 'confidence':.9, 'reason':'completed'}}), encoding='utf-8')
        token.set()
        return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps({
            'type':'turn.completed', 'usage':{'input_tokens':10, 'output_tokens':2}}))
    call = CodexCliClient(CodexCliConfig(), runner, tmp_path).system_one(
        {'question':'q','answer':'a'}, jev_request('q','a',('J1',))['questions'], cancel_event=token)
    assert call.status == 'OK' and call.body['answers']['J1']['choice'] == 'PASS'
