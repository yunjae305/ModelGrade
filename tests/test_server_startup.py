"""Startup failures are readable; existing listeners remain untouched."""
import errno
from types import SimpleNamespace

import pytest

import webapp.server as webserver


@pytest.mark.parametrize('port,windows_error,alternate', [(8080, False, 8090), (8090, True, 8091), (65535, False, 65534)])
def test_used_port_exits_once_with_matching_command_and_url(monkeypatch, capsys, port, windows_error, alternate):
    error = OSError(errno.EADDRINUSE, 'fixture address in use')
    if windows_error:
        error = OSError('fixture Windows address in use')
        error.winerror = 10048
    binds = []

    def fail(address):
        binds.append(address)
        raise error

    monkeypatch.setattr(webserver, 'Server', fail)
    monkeypatch.setattr('sys.argv', ['run.py', '--port', str(port)])
    with pytest.raises(SystemExit) as caught:
        webserver.main()
    assert caught.value.code == 1
    assert binds == [('127.0.0.1', port)]  # Never silently retry another port.
    output = capsys.readouterr()
    assert not output.out
    assert f'포트 {port}가 이미 사용 중입니다.' in output.err
    assert '기존 서버를 실행한 터미널에서 Ctrl+C' in output.err
    assert f'py -3 -X utf8 run.py --port {alternate}' in output.err
    assert f'http://localhost:{alternate}/workspace.html' in output.err
    assert 'Traceback' not in output.err


def test_other_bind_oserrors_are_not_disguised(monkeypatch, capsys):
    failure = OSError(errno.EACCES, 'fixture permission denied')

    def fail(_):
        raise failure

    monkeypatch.setattr(webserver, 'Server', fail)
    monkeypatch.setattr('sys.argv', ['run.py'])
    with pytest.raises(OSError) as caught:
        webserver.main()
    assert caught.value is failure
    assert not capsys.readouterr().err


def test_real_port_collision_does_not_close_existing_listener(monkeypatch, capsys):
    listener = webserver.Server(('127.0.0.1', 0))
    try:
        port = listener.server_port
        descriptor = listener.socket.fileno()
        monkeypatch.setattr('sys.argv', ['run.py', '--port', str(port)])
        with pytest.raises(SystemExit) as caught:
            webserver.main()
        assert caught.value.code == 1
        assert f'포트 {port}가 이미 사용 중입니다.' in capsys.readouterr().err
        assert listener.socket.fileno() == descriptor
        assert listener.socket.getsockname()[1] == port
    finally:
        listener.server_close()


def test_ctrl_c_still_closes_a_successful_server(monkeypatch, capsys):
    actions = []

    def serve():
        actions.append('serve')
        raise KeyboardInterrupt

    server = SimpleNamespace(server_port=8090, comparison_service=SimpleNamespace(
        load_environment=lambda: actions.append('environment')), serve_forever=serve,
        server_close=lambda: actions.append('close'))
    monkeypatch.setattr(webserver, 'stop_services', lambda s: (s.server_close(), True)[1])
    monkeypatch.setattr(webserver, 'Server', lambda address: server)
    monkeypatch.setattr('sys.argv', ['run.py', '--port', '8090'])
    webserver.main()
    assert actions == ['environment', 'serve', 'close']
    assert 'http://localhost:8090/workspace.html' in capsys.readouterr().out


def test_shutdown_signals_both_services_before_waiting_and_closes_listener(capsys):
    actions = []
    comparison = SimpleNamespace(shutdown=lambda timeout: actions.append('comparison') or True)
    evaluator = SimpleNamespace(shutdown=lambda: actions.append('evaluator'), is_idle=lambda: True)
    server = SimpleNamespace(comparison_service=comparison, code_eval_service=evaluator,
                             server_close=lambda: actions.append('close'))
    assert webserver.stop_services(server, timeout=0)
    assert actions == ['comparison', 'evaluator', 'close', 'comparison']
    assert '결과 저장 완료' in capsys.readouterr().out


def test_unresponsive_http_shutdown_finishes_partial_costs_without_waiting_forever(monkeypatch, capsys):
    actions = []
    comparison = SimpleNamespace(shutdown=lambda timeout: False,
                                  finish_shutdown_costs=lambda: actions.append('costs'))
    evaluator = SimpleNamespace(shutdown=lambda: None, is_idle=lambda: False)
    server = SimpleNamespace(comparison_service=comparison, code_eval_service=evaluator,
                             server_close=lambda: actions.append('close'))
    monkeypatch.setattr(webserver, 'terminate_active_cli', lambda: actions.append('children'))
    assert webserver.stop_services(server, timeout=0) is False
    assert actions == ['close', 'children', 'costs']
    assert '강제 종료' in capsys.readouterr().out


def test_shutdown_failure_does_not_skip_other_services_or_children(monkeypatch):
    actions = []
    def broken(timeout):
        raise RuntimeError('fixture save failure')
    comparison = SimpleNamespace(shutdown=broken, finish_shutdown_costs=lambda: actions.append('costs'))
    evaluator = SimpleNamespace(shutdown=lambda: actions.append('evaluator'), is_idle=lambda: False)
    server = SimpleNamespace(comparison_service=comparison, code_eval_service=evaluator,
                             server_close=lambda: actions.append('close'))
    monkeypatch.setattr(webserver, 'terminate_active_cli', lambda: actions.append('children'))
    assert webserver.stop_services(server, timeout=0) is False
    assert actions == ['evaluator', 'close', 'children', 'costs']
