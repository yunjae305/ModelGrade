"""Real server process + OS interrupt + mock judges; no external model requests."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = r'''
import json, sys, threading, time
from pathlib import Path
from code_eval.cli_process import run_cli, CliCancelled
from code_eval.jev_client import JevCall, JevClientConfig
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService
import webapp.server as webserver

folder, mode = Path(sys.argv[1]), sys.argv[2]
class Judge:
    config = JevClientConfig()
    supports_cancellation = True
    def system_one(self, state, questions, *, cancel_event=None):
        index = int(state['question'].removeprefix('question '))
        if index == 0:
            return JevCall('OK', attempts=1, body={'answers': {
                cid: {'choice':'PASS', 'confidence':.9, 'probabilities':{'PASS':.9,'FAIL':.1}}
                for cid in questions}})
        if mode == 'http':
            (folder / ('waiting-' + str(index))).write_text('waiting')
            time.sleep(30)  # Simulate a synchronous external HTTP call ignoring cancellation.
            return JevCall('API_ERROR', attempts=1)
        program = ('import os,sys,time; from pathlib import Path; '
                   'Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)')
        try:
            run_cli([sys.executable, '-u', '-c', program, str(folder / ('waiting-' + str(index)))],
                    text=True, encoding='utf-8', timeout=25, cancel_event=cancel_event)
        except CliCancelled:
            return JevCall('SKIPPED', attempts=1, error='평가 중단 fixture')
        raise AssertionError('fixture should have been interrupted')

judge = Judge()
evaluator = CodeEvalService(save_dir=folder / 'eval', jev_resolver=lambda: (judge,judge.config,None))
comparison = ComparisonService(evaluator, save_dir=folder / 'runs', registry_path=folder / 'registry.json')
comparison.load_environment = lambda: None
server_class = webserver.Server
def factory(address):
    server = server_class(address, evaluator, comparison)
    run = comparison.start({'providers':[], 'auto_evaluate':True, 'cases':[
        {'id':str(i), 'question':f'question {i}', 'answer':'  answer\r\n'} for i in range(20)]})
    (folder / 'server.tmp').write_text(json.dumps({'port':server.server_port,'run_id':run['run_id']}))
    (folder / 'server.tmp').replace(folder / 'server.json')
    return server
webserver.Server = factory
if mode == 'http':
    original_stop = webserver.stop_services
    webserver.stop_services = lambda server: original_stop(server, timeout=.3)
sys.argv = ['run.py','--port','0']
webserver.main()
'''


def until(check, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.03)
    raise AssertionError('server fixture did not reach expected state')


@pytest.mark.parametrize('mode,exitcode', [('cli', 0), ('http', 130)])
def test_interrupt_exits_with_saved_results_and_closed_listener(tmp_path, mode, exitcode):
    script = tmp_path / 'server_fixture.py'
    script.write_text(FIXTURE, encoding='utf-8')
    env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHONIOENCODING': 'utf-8'}
    options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    process = subprocess.Popen([sys.executable, '-X', 'utf8', str(script), str(tmp_path), mode],
                               cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', **options)
    try:
        until(lambda: (tmp_path / 'server.json').exists())
        info = json.loads((tmp_path / 'server.json').read_text())
        until(lambda: list(tmp_path.glob('waiting-*')))
        # A successful GET also proves main() installed its OS signal handlers.
        with urlopen(f"http://127.0.0.1:{info['port']}/api/comparison/runs/{info['run_id']}", timeout=3) as response:
            assert response.status == 200
        started = time.monotonic()
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGINT)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == exitcode, (stdout, stderr)
        assert time.monotonic() - started < 12
        assert 'Traceback' not in stderr and '[LLM 비용]' in stdout
        saved = json.loads((tmp_path / 'runs' / (info['run_id'] + '.json')).read_text(encoding='utf-8'))
        assert saved['status'] == 'CANCELLED'
        assert saved['datasets']['aiona'][0]['answer'] == '  answer\r\n'
        records = saved['evaluations']['aiona']['jev_eval']['records']
        assert records[0]['status'] == 'SUCCESS'
        assert all(not r or r['status'] not in ('API_ERROR','TIMEOUT','RUNNING') for r in records)
        assert len(list(tmp_path.glob('waiting-*'))) < 20
        with socket.socket() as connection:
            connection.settimeout(.5)
            assert connection.connect_ex(('127.0.0.1', info['port'])) != 0
        if mode == 'http':
            assert '미확인' in stdout and '강제 종료' in stdout
        else:
            assert all(r['status'] == 'SKIPPED' for r in records[1:])
            assert '결과 저장 완료' in stdout
    finally:
        if process.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill.exe','/PID',str(process.pid),'/T','/F'], capture_output=True)
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
