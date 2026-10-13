"""Serve only frontend assets and an explicit local JSON API."""
from __future__ import annotations
import argparse
import errno
import json
import mimetypes
import os
import signal
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
from .code_eval_service import CodeEvalService, JobCapacityError, ROOT
from .comparison_service import ComparisonService, import_dataset
from .ingestion import BundleError
from .ingestion.platforms import formats as external_formats
from code_eval.report_html import render_report
from code_eval.cli_process import active_cli_count, terminate_active_cli


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR permits two live listeners to share a port, sending
    # requests to stale code even after a second server reports successful start.
    allow_reuse_address = not hasattr(socket, 'SO_EXCLUSIVEADDRUSE')

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address, code_eval_service=None, comparison_service=None):
        # v16 코드 평가기 (/code-eval.html). CLI 와 같은 code_eval.engine 을 쓴다.
        self.code_eval_service = code_eval_service or CodeEvalService()
        self.comparison_service = comparison_service or ComparisonService(self.code_eval_service)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, value, content_type='application/json; charset=utf-8', extra_headers=None):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() if not isinstance(value, bytes) else value
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        for name, header_value in (extra_headers or {}).items():
            self.send_header(name, header_value)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def allowed(self):
        port = self.server.server_port
        hosts = {f'localhost:{port}', f'127.0.0.1:{port}'}
        if self.headers.get('Host') not in hosts:
            self.respond(403, {'error': 'localhost에서 접속해 주세요.'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin not in {f'http://{host}' for host in hosts}:
            self.respond(403, {'error': '동일 출처 요청만 허용합니다.'})
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        url = urlparse(self.path)
        path = unquote(url.path)
        try:
            if path == '/api/health':
                return self.respond(200, {'status': 'ok'})
            if path.startswith('/api/code-eval/'):
                return self.code_eval_get(path)
            if path.startswith('/api/comparison/'):
                return self.comparison_get(path)
            if path.startswith('/api/'):
                return self.respond(404, {'error': 'API를 찾을 수 없습니다.'})
            # 루트는 비교 워크스페이스로 보낸다. 영구 캐시를 피하려고 302를 쓴다.
            if path == '/':
                self.send_response(302)
                self.send_header('Location', '/workspace.html' + (f'?{url.query}' if url.query else ''))
                self.send_header('Content-Length', '0')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                return
            frontend = (ROOT / 'frontend').resolve()
            asset = (frontend / path.lstrip('/')).resolve()
            if not asset.is_relative_to(frontend) or not asset.is_file() or any(p.startswith('.') for p in asset.relative_to(frontend).parts):
                return self.respond(404, {'error': '파일을 찾을 수 없습니다.'})
            content_type = 'text/javascript' if asset.suffix in ('.js', '.mjs') else mimetypes.guess_type(asset.name)[0] or 'application/octet-stream'
            return self.respond(200, asset.read_bytes(), content_type + '; charset=utf-8')
        except (ValueError, TypeError) as exc:
            self.respond(400, {'error': str(exc)})
        except FileNotFoundError:
            self.respond(404, {'error': '데이터를 찾을 수 없습니다.'})
        except Exception:
            self.respond(500, {'error': '서버 데이터 처리 오류입니다. 실행 콘솔을 확인해 주세요.'})
            import traceback
            traceback.print_exc()

    def code_eval_get(self, path):
        ce = self.server.code_eval_service
        parts = path.strip('/').split('/')
        if path == '/api/code-eval/info':
            return self.respond(200, ce.info())
        if len(parts) == 4 and parts[2] == 'jobs':
            return self.respond(200, ce.job_status(parts[3]))
        if len(parts) == 5 and parts[2] == 'jobs' and parts[4] == 'result':
            return self.respond(200, ce.job_result(parts[3]))
        if len(parts) == 6 and parts[2] == 'jobs' and parts[4] == 'export':
            body, ctype, name = ce.export(parts[3], parts[5])
            return self.respond(200, body, ctype + '; charset=utf-8', {
                'Content-Disposition': f'attachment; filename="{name}"',
                'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'"})
        return self.respond(404, {'error': 'API를 찾을 수 없습니다.'})

    def comparison_get(self, path):
        service = self.server.comparison_service
        parts = path.strip('/').split('/')
        if path == '/api/comparison/providers':
            return self.respond(200, service.providers())
        if path == '/api/comparison/info':
            return self.respond(200, service.info())
        if path == '/api/comparison/sample':
            return self.respond(200, service.sample())
        if path == '/api/comparison/history':
            return self.respond(200, service.history())
        if path == '/api/comparison/external/formats':
            return self.respond(200, external_formats())
        if path == '/api/comparison/version-history':
            return self.respond(200, service.version_history())
        if path == '/api/comparison/version-comparison':
            query = {key: values[-1] for key, values in parse_qs(urlparse(self.path).query).items()}
            return self.respond(200, service.version_comparison(query.get('baseline'), query.get('target')))
        if path == '/api/comparison/aiona-history':
            return self.respond(200, service.aiona_history())
        if path == '/api/comparison/auto-collection':
            return self.respond(200, service.auto_collection_status())
        if len(parts) >= 4 and parts[2] == 'runs':
            if len(parts) == 5 and parts[4] == 'events':
                stream = service.event_stream(parts[3])
                last_id = self.headers.get('Last-Event-ID')
                if last_id is not None and (not last_id.isdecimal() or len(last_id) > 20):
                    raise ValueError('잘못된 이벤트 위치입니다.')
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache, no-store')
                self.send_header('X-Accel-Buffering', 'no')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                self.connection.settimeout(15)
                try:
                    for frame in stream.frames(int(last_id) if last_id is not None else None):
                        self.wfile.write(frame)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                    pass  # disconnecting viewers never cancel evaluation workers
                return
            if len(parts) == 4:
                return self.respond(200, service.status(parts[3]))
            if len(parts) == 5 and parts[4] == 'result':
                return self.respond(200, service.result(parts[3]))
            if len(parts) == 5 and parts[4] == 'code-recheck':
                return self.respond(200, service.code_recheck(parts[3]))
            if len(parts) == 5 and parts[4] == 'version-template':
                return self.respond(200, service.version_template(parts[3]))
            if len(parts) == 6 and parts[4] == 'evaluation':
                batch = service.result(parts[3])['evaluations'].get(parts[5])
                if batch is None or batch.get('live'):
                    raise FileNotFoundError(path)
                return self.respond(200, batch)
            if len(parts) == 7 and parts[4] == 'evaluation' and parts[6] in ('json', 'jsonl', 'html'):
                batch = service.result(parts[3])['evaluations'].get(parts[5])
                if batch is None or batch.get('live'):
                    raise FileNotFoundError(path)
                fmt = parts[6]
                if fmt == 'html':
                    body, ctype = render_report(batch).encode('utf-8'), 'text/html; charset=utf-8'
                elif fmt == 'jsonl':
                    body = ('\n'.join(json.dumps(c, ensure_ascii=False, allow_nan=False) for c in batch['cases']) + '\n').encode('utf-8')
                    ctype = 'application/x-ndjson; charset=utf-8'
                else:
                    body = json.dumps(batch, ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8')
                    ctype = 'application/json; charset=utf-8'
                return self.respond(200, body, ctype, {
                    'Content-Disposition': f'attachment; filename="evaluation-{parts[3]}-{parts[5]}.{fmt}"',
                    'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'"})
            if len(parts) in (5, 6) and parts[4] == 'export':
                provider = parts[5] if len(parts) == 6 else None
                body = service.export(parts[3], provider)
                name = f"comparison-{parts[3]}-{provider or 'all'}.json"
                return self.respond(200, body, extra_headers={'Content-Disposition': f'attachment; filename="{name}"'})
        return self.respond(404, {'error': 'API를 찾을 수 없습니다.'})

    def comparison_post(self, path):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.respond(415, {'error': 'application/json 요청이 필요합니다.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 64 * 1024 * 1024:
                return self.respond(413, {'error': '요청은 64MB 이하여야 합니다.'})
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('요청 본문은 JSON 객체여야 합니다.')
            service = self.server.comparison_service
            parts = path.strip('/').split('/')
            if path == '/api/comparison/dataset':
                return self.respond(200, import_dataset(payload))
            if path == '/api/comparison/runs':
                return self.respond(202, service.start_comparison(payload))
            if path == '/api/comparison/version-runs':
                return self.respond(202, service.start_version_run(payload))
            if path == '/api/comparison/model-versions':
                return self.respond(200, service.manage_release(payload))
            if path == '/api/comparison/version-check':
                return self.respond(200, service.prepare_version_comparison(payload))
            if path == '/api/comparison/external/preview':
                return self.respond(200, service.preview_external(payload))
            if path == '/api/comparison/external/runs':
                return self.respond(202, service.start_external(payload))
            if path == '/api/comparison/auto-collection/jobs':
                return self.respond(202, service.start_auto_collection(payload))
            if path == '/api/comparison/auto-collection/policy':
                return self.respond(200, service.update_auto_policy(payload))
            if len(parts) == 4 and parts[2] == 'providers':
                return self.respond(200, service.configure(parts[3], payload))
            if len(parts) == 5 and parts[2] == 'runs':
                if parts[4] == 'evaluate':
                    return self.respond(202, service.evaluate(parts[3], payload))
                if parts[4] == 'collect':
                    return self.respond(202, service.collect_more(parts[3], payload))
                if parts[4] == 'cancel':
                    return self.respond(200, service.cancel(parts[3]))
            return self.respond(404, {'error': 'API를 찾을 수 없습니다.'})
        except JobCapacityError as exc:
            self.respond(429, {'error': str(exc)})
        except BundleError as exc:
            self.respond(400, {'error': str(exc), 'errors': exc.errors})
        except (ValueError, TypeError, KeyError) as exc:
            self.respond(400, {'error': str(exc)})
        except FileNotFoundError:
            self.respond(404, {'error': '실행 기록을 찾을 수 없습니다.'})
        except Exception:
            # Provider requests are handled inside the worker. Never dump a
            # credential request or a provider exception into the console.
            self.respond(500, {'error': '비교 작업을 처리하지 못했습니다.'})

    def code_eval_post(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.respond(415, {'error': 'application/json 요청이 필요합니다.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 64 * 1024 * 1024:
                return self.respond(413, {'error': '요청은 64MB 이하여야 합니다.'})
            payload = json.loads(self.rfile.read(length))
            ce = self.server.code_eval_service
            if self.path == '/api/code-eval/jobs':
                return self.respond(202, ce.start_job(payload))
            return self.respond(200, ce.evaluate(payload))
        except JobCapacityError as exc:
            self.respond(429, {'error': str(exc)})
        except (ValueError, TypeError, KeyError) as exc:
            self.respond(400, {'error': str(exc)})
        except Exception:
            self.respond(500, {'error': '평가 중 서버 오류가 발생했습니다. 실행 콘솔을 확인해 주세요.'})
            import traceback
            traceback.print_exc()

    def do_POST(self):
        if not self.allowed():
            return
        if self.path in ('/api/code-eval/evaluate', '/api/code-eval/jobs'):
            return self.code_eval_post()
        path = unquote(urlparse(self.path).path)
        if path.startswith('/api/comparison/'):
            return self.comparison_post(path)
        return self.respond(404, {'error': 'API를 찾을 수 없습니다.'})


def stop_services(server, *, timeout=10):
    """Persist cancellation before waiting; never let executor atexit hang forever."""
    errors = set()
    def notice(message):
        try:
            print(message, flush=True)
        except (OSError, ValueError):
            pass
    def step(label, action):
        try:
            return action()
        except Exception:
            if label not in errors:
                notice(f'종료 처리 중 {label} 오류. 나머지 작업 정리를 계속합니다.')
            errors.add(label)
            return False
    notice('\n중단 요청: 새 평가 호출을 멈추고 실행 중인 작업과 결과를 정리합니다.')
    comparison, evaluator = server.comparison_service, server.code_eval_service
    step('비교 작업 중단', lambda: comparison.shutdown(timeout=0))
    step('평가 작업 중단', evaluator.shutdown)
    step('서버 닫기', server.server_close)
    deadline = time.monotonic() + timeout
    while True:
        stopped = step('비교 작업 중단', lambda: comparison.shutdown(timeout=0))
        idle = step('평가 종료 확인', evaluator.is_idle)
        drained = stopped and idle and not active_cli_count() and not errors
        if drained or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    if not drained:
        step('CLI 종료', terminate_active_cli)
        step('실행 기록 정리', comparison.finish_shutdown_costs)
        notice('저장된 결과는 유지됩니다. 응답이 끝나지 않은 작업을 강제 종료합니다.')
    else:
        notice('작업 중단 및 결과 저장 완료. 서버를 종료합니다.')
    return drained


def main():
    parser = argparse.ArgumentParser(description='AIONA 모델 비교 워크스페이스')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    try:
        server = Server(('127.0.0.1', args.port))
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE and getattr(exc, 'winerror', None) != 10048:
            raise
        alternate = 8090 if args.port == 8080 else args.port + 1 if args.port < 65535 else 65534
        print(f'포트 {args.port}가 이미 사용 중입니다.\n'
              '기존 서버를 실행한 터미널에서 Ctrl+C로 종료한 뒤 다시 실행하세요.\n'
              f'다른 포트로 실행: py -3 -X utf8 run.py --port {alternate}\n'
              f'위 명령으로 실행한 뒤 접속: http://localhost:{alternate}/workspace.html', file=sys.stderr)
        raise SystemExit(1) from None
    previous_break = signal.signal(signal.SIGBREAK, signal.default_int_handler) if hasattr(signal, 'SIGBREAK') else None
    try:
        server.comparison_service.load_environment()
        print(f'AIONA 모델 비교: http://localhost:{server.server_port}/workspace.html', flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Repeated Ctrl+C must not cut off the save/child-process cleanup halfway.
        previous_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
        if hasattr(signal, 'SIGBREAK'):
            signal.signal(signal.SIGBREAK, signal.SIG_IGN)
        try:
            drained = stop_services(server)
            if not drained:
                # ThreadPoolExecutor joins its workers at interpreter exit even
                # when the owning web thread is a daemon. An in-flight HTTP call
                # can outlive our deadline, so bypass that join AFTER saving.
                try:
                    sys.stdout.flush()
                    sys.stderr.flush()
                finally:
                    os._exit(130)
        finally:
            signal.signal(signal.SIGINT, previous_int)
            if hasattr(signal, 'SIGBREAK'):
                signal.signal(signal.SIGBREAK, previous_break)


if __name__ == '__main__':
    main()
