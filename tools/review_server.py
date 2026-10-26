"""보류 바로 정하기 — 결과 폴더의 보고서를 이 PC 안(127.0.0.1)에서 열고, 보고서에서 누른 사람 판정을 바로 human_decisions.json 에 저장한다
(사용자 10-26 밤: 더블클릭으로 연 html 은 판단을 프로그램에 바로 못 보낸다 → 보고서 안에서 정하고 바로 저장).

    python -X utf8 tools/modelgrade.py review [--run <실행>]   # 브라우저가 열림 — 다 정했으면 터미널에서 Enter

보고서는 이 서버로 열리면 누를 때마다 POST /api/decisions 로 보내고(held-apply 와 같은 규칙으로 쌓음), 파일로 열리면 지금처럼 브라우저에
저장했다가 '판단 내보내기'. 끝낼 때(Enter) 판정 보고서 · 결과 보고서를 다시 만든다. 주소의 일회용 열쇠(t=…)로 받은 쿠키(SameSite=Strict)가
있어야 저장되므로 다른 사이트가 대신 보낼 수 없다. 이 PC 밖에서는 열리지 않는다(127.0.0.1 에만 묶음).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import secrets
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

JUDGE_OUT = "v4"
MAX_BODY = 4 * 1024 * 1024
COOKIE = "mg_review"


def find_dirs(run_id: Optional[str] = None) -> Tuple[str, Path, Path]:
    """(실행, 판정 폴더, 결과 폴더) — modelgrade report 가 쓰는 짝(실행의 result_dir · data/judge_runs/<실행>/v4)만. 실행을 안 주면
    결과 폴더를 최근 것부터 보며 그 짝이 있는 첫 실행. 다시 판정 폴더(run.json 에 rerun_of)처럼 따로 만든 폴더는 고르지 않는다."""
    from tools import modelgrade as mg
    from tools import result_dirs
    cands = [run_id] if run_id else []
    if not run_id and result_dirs.RESULTS.exists():
        for rd in sorted((d for d in result_dirs.RESULTS.iterdir() if (d / "run.json").exists()), reverse=True):
            try:
                rid = json.loads((rd / "run.json").read_text(encoding="utf-8")).get("run_id")
            except (OSError, ValueError):
                continue
            if rid and rid not in cands:
                cands.append(str(rid))
    for rid in cands:
        try:
            run = mg.load_run(rid)
        except SystemExit:
            continue
        jd = ROOT / "data" / "judge_runs" / rid / JUDGE_OUT
        if run.get("result_dir") and (jd / "results.jsonl").exists():
            return rid, jd, mg.RESULTS_JSON_DIR / run["result_dir"]
    if run_id:
        raise SystemExit(f"실행 {run_id} 의 결과 폴더 · 판정 기록(data/judge_runs/{run_id}/{JUDGE_OUT})을 찾지 못했습니다")
    raise SystemExit("판정한 실행을 찾지 못했습니다 — modelgrade start 로 평가한 뒤에 여세요")


def rebuild(judge_dir: Path, run_id: str) -> None:
    """판정 보고서 · 결과 보고서 · 결과 JSON 을 사람 판정까지 반영해 다시 만든다 (modelgrade report 와 같은 일, 출력은 숨김)."""
    from tools import modelgrade
    with contextlib.redirect_stdout(io.StringIO()):
        modelgrade.cmd_report(argparse.Namespace(dir=str(judge_dir), run=run_id, from_start=False))


def make_handler(run_id: str, judge_dir: Path, result_dir: Path, token: str, port_box: Dict[str, int],
                 on_saved: Callable[[Dict[str, Any]], None]) -> type:
    from tools import modelgrade
    lock = threading.Lock()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, directory=str(result_dir), **kw)

        def log_message(self, *_a: Any) -> None:   # 터미널을 어지럽히지 않는다
            pass

        def _host_ok(self) -> bool:   # 다른 이름으로 이 서버를 부르는 페이지(DNS rebinding)를 막는다
            host = (self.headers.get("Host") or "").lower()
            return host in (f"127.0.0.1:{port_box['port']}", f"localhost:{port_box['port']}")

        def _authed(self) -> bool:
            cookie = self.headers.get("Cookie") or ""
            return any(c.strip() == f"{COOKIE}={token}" for c in cookie.split(";"))

        def _json(self, code: int, body: Dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def end_headers(self) -> None:
            if getattr(self, "_set_cookie", False):
                self.send_header("Set-Cookie", f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict")
            super().end_headers()

        def do_GET(self) -> None:
            if not self._host_ok():
                return self._json(403, {"ok": False, "error": "host"})
            url = urlsplit(self.path)
            if url.path == "/api/ping":
                if not self._authed():
                    return self._json(403, {"ok": False, "error": "열쇠 없음 — 터미널에 나온 주소로 다시 여세요"})
                return self._json(200, {"ok": True, "run_id": run_id})
            if parse_qs(url.query).get("t") == [token]:
                self._set_cookie = True
            return super().do_GET()

        def do_POST(self) -> None:
            if not self._host_ok() or not self._authed():
                return self._json(403, {"ok": False, "error": "열쇠 없음 — 터미널에 나온 주소로 다시 여세요"})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._json(400, {"ok": False, "error": "본문 크기"})
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except ValueError:
                return self._json(400, {"ok": False, "error": "JSON 이 아님"})
            path = urlsplit(self.path).path
            try:
                with lock:
                    if path == "/api/decisions":
                        res = modelgrade.apply_human_decisions(judge_dir, body)
                        on_saved(res)
                        return self._json(200, {"ok": True, **{k: res[k] for k in ("added", "rejected", "total")}})
                    if path == "/api/rebuild":
                        rebuild(judge_dir, run_id)
                        return self._json(200, {"ok": True})
            except SystemExit as exc:   # 다른 실행의 판단 등 — 화면에 그대로
                return self._json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                return self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return self._json(404, {"ok": False, "error": "없는 주소"})

    return Handler


def serve(run_id: Optional[str] = None, open_browser: bool = True, page: str = "판정보고서.html",
          wait: Callable[[], Any] = lambda: input("  다 정했으면 Enter (보고서를 다시 만들고 끝냅니다) … "),
          say: Callable[[str], None] = lambda s: print(s, flush=True)) -> Dict[str, Any]:
    """서버를 띄우고 보고서를 연 뒤 wait() 가 끝나면 보고서를 다시 만들고 닫는다. 저장한 수를 돌려준다."""
    run_id, judge_dir, result_dir = find_dirs(run_id)
    if not (result_dir / page).exists():
        rebuild(judge_dir, run_id)
    token = secrets.token_urlsafe(18)
    port_box = {"port": 0}
    saved = {"calls": 0, "added": 0}

    def on_saved(res: Dict[str, Any]) -> None:
        saved["calls"] += 1
        saved["added"] += res["added"]
        saved["total"] = res["total"]

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(run_id, judge_dir, result_dir, token, port_box, on_saved))
    port_box["port"] = server.server_address[1]
    url = f"http://127.0.0.1:{port_box['port']}/{quote(page)}?t={token}#hold"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    say(f"  보류 정하기 — 브라우저에서 누르면 바로 저장됩니다: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        wait()
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        server.shutdown()
        server.server_close()
    if saved["calls"]:
        say("  보고서를 다시 만드는 중 …")
        rebuild(judge_dir, run_id)
        say(f"  사람 판정 {saved.get('total', 0)}건 반영 — {result_dir / 'report.html'}")
    else:
        say("  저장한 판단이 없습니다.")
    return saved
