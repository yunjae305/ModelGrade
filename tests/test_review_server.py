"""보류 바로 정하기 서버(tools/review_server) — 열쇠 쿠키 · Host 확인 · 저장 전달. 실제 보고서 다시 만들기는 부르지 않는다."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer


def _serve(tmp_path, monkeypatch):
    from tools import modelgrade, review_server
    calls = []
    monkeypatch.setattr(modelgrade, "apply_human_decisions",
                        lambda d, doc: calls.append((d, doc)) or {"added": len(doc["decisions"]), "rejected": 0, "total": 7, "run_id": "r1"})
    (tmp_path / "판정보고서.html").write_text("<html>보류</html>", encoding="utf-8")
    port = {"port": 0}
    saved = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), review_server.make_handler("r1", tmp_path / "jd", tmp_path, "KEY", port, saved.append))
    port["port"] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port['port']}", calls, saved


def _req(url, data=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode("utf-8")


def test_only_the_page_opened_with_the_key_can_save(tmp_path, monkeypatch):
    srv, base, calls, saved = _serve(tmp_path, monkeypatch)
    try:
        doc = {"run_id": "r1", "decisions": [{"provider": "gpt", "question": 3, "turn": 1, "item": "J2-1.a", "new": "PASS"}]}
        assert _req(base + "/api/ping")[0] == 403 and _req(base + "/api/decisions", doc)[0] == 403   # 열쇠 없음
        code, headers, body = _req(base + "/%ED%8C%90%EC%A0%95%EB%B3%B4%EA%B3%A0%EC%84%9C.html?t=KEY")
        cookie = headers.get("Set-Cookie", "").split(";")[0]
        assert code == 200 and "보류" in body and cookie == "mg_review=KEY" and "SameSite=Strict" in headers.get("Set-Cookie")
        assert "Set-Cookie" not in _req(base + "/%ED%8C%90%EC%A0%95%EB%B3%B4%EA%B3%A0%EC%84%9C.html?t=WRONG")[1]
        assert json.loads(_req(base + "/api/ping", headers={"Cookie": cookie})[2]) == {"ok": True, "run_id": "r1"}
        code, _h, body = _req(base + "/api/decisions", doc, {"Cookie": cookie, "Content-Type": "application/json"})
        assert code == 200 and json.loads(body) == {"ok": True, "added": 1, "rejected": 0, "total": 7}
        assert calls[0][0] == tmp_path / "jd" and calls[0][1] == doc and saved[0]["total"] == 7
        assert _req(base + "/api/decisions", doc, {"Cookie": cookie, "Host": "evil.test:" + base.rsplit(":", 1)[1]})[0] == 403   # 다른 이름으로 부름
        assert _req(base + "/api/nothing", doc, {"Cookie": cookie})[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_offer_review_only_hints_when_not_a_terminal(monkeypatch, capsys):
    """평가 끝 보류 묻기 — 터미널이 아니면(자동 실행 · CI) 묻지 않고 명령만 알린다."""
    from tools import modelgrade
    monkeypatch.setattr(modelgrade.sys.stdin, "isatty", lambda: False, raising=False)
    modelgrade.offer_review("r1")
    assert "보류 정하기: modelgrade review" in capsys.readouterr().out
