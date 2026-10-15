"""질문 세트 실행 — AIONA(웹)·API(+도구) 수집이 함께 돌고, 멀티턴은 각자 앞 답변을 잇고, 보낸 첨부만 남는다. 유료 호출 없음."""
from __future__ import annotations

import json
import time

import pytest

import httpx

from webapp import agent_collection, aiona_web, comparison_service
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService

ROWS = [{"case_id": "Q1", "question": "첨부 PDF 합계는?", "attachments": [{"name": "견적.pdf", "type": "pdf", "content": "합계 10", "sha256": "a1",
                                                                         "path": "a1.pdf"}]},
        {"case_id": "Q2_T1", "conversation_id": "C2", "turn": 1, "question": "첫 질문"},
        {"case_id": "Q2_T2", "conversation_id": "C2", "turn": 2, "question": "이어지는 질문"}]


class FakeChat:
    asked = []

    def __init__(self, **_):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def new_chat(self):
        FakeChat.asked.append("새 채팅")

    def ask(self, question, files=None, reference=None):
        FakeChat.asked.append(question)
        return {"answer": "AIONA: " + question, "status": "SUCCESS", "sent": [{"name": f.name, "how": "파일 그대로"} for f in files or []]}


def test_question_set_run_collects_web_and_api_with_history_and_keeps_only_sent_files(tmp_path, monkeypatch):
    calls = []

    def fake_ask(provider, config, question, attachments, files_dir, client, max_tokens, *, reference=None, history=()):
        calls.append((provider, question, list(history)))
        sent = [{"name": a["name"], "type": a["type"], "sha256": a["sha256"]} for a in attachments or []]
        return {"answer": f"{provider}: {question}", "status": "SUCCESS", "sent": sent}

    monkeypatch.setattr(aiona_web, "AionaChat", FakeChat)
    monkeypatch.setattr(comparison_service, "ask_with_tools", fake_ask)
    # 우수AI 는 일시 중지 — 로그인 정보가 있어도 수집 대상에 넣지 않는다 (사용자 결정 2026-10-14, 아래 providers 확인)
    monkeypatch.setattr(comparison_service, "oosoo_login_status", lambda *_, **__: {"credentials": True})
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "a1.pdf").write_bytes(b"%PDF")
    svc = ComparisonService(CodeEvalService(), save_dir=tmp_path / "runs")
    svc.configure("gpt", {"key": "k", "model": "gpt-x"})
    run = svc.start_question_set_run({"set_id": "qs-1", "rows": ROWS, "files_dir": str(tmp_path / "files"), "auto_evaluate": False})
    for _ in range(500):
        if svc.status(run["run_id"])["status"] != "COLLECTING":
            break
        time.sleep(0.01)
    result = svc.result(run["run_id"])
    assert result["status"] == "READY" and result["providers"] == ["aiona", "gpt"] and result["run_kind"] == "question_set"
    gpt, aiona = result["datasets"]["gpt"], result["datasets"]["aiona"]
    assert [r["answer"] for r in gpt] == ["gpt: 첨부 PDF 합계는?", "gpt: 첫 질문", "gpt: 이어지는 질문"]
    assert ("gpt", "이어지는 질문", [(ROWS[1], "gpt: 첫 질문")]) in calls   # 멀티턴은 앞 턴 질문 행과 그 모델의 앞 답변을 잇는다
    assert gpt[0]["attachments"][0]["sha256"] == "a1" and aiona[0]["attachments"][0]["name"] == "견적.pdf"   # 실제로 보낸 첨부
    assert FakeChat.asked.count("새 채팅") == 2 and aiona[2]["answer"] == "AIONA: 이어지는 질문"   # 대화는 같은 채팅에서 이어 묻는다
    plain = ComparisonService._answer_case(ROWS[0], "gpt", "m", 0, {"status": "SUCCESS", "answer": "a"})
    assert "attachments" not in plain   # 글자만 보내는 기존 수집은 첨부를 남기지 않는다


def test_api_tools_collector_keeps_refusals_and_turns_bad_responses_into_errors(tmp_path):
    replies = {"api.openai.com": {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "도울 수 없어요"}]}]},
               "api.anthropic.com": {"stop_reason": "refusal", "content": [{"type": "text", "text": "거절합니다"}]}}

    def handler(request):
        if request.url.host == "generativelanguage.googleapis.com":
            return httpx.Response(200, text="not json")
        return httpx.Response(200, json=replies[request.url.host])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gpt = agent_collection.ask_with_tools("gpt", {"key": "k", "model": "m"}, "질문", [], tmp_path, client)
    claude = agent_collection.ask_with_tools("claude", {"key": "k", "model": "m"}, "질문", [], tmp_path, client)
    gemini = agent_collection.ask_with_tools("gemini", {"key": "k", "model": "m"}, "질문", [], tmp_path, client)
    assert (gpt["status"], gpt["answer"]) == ("SUCCESS", "도울 수 없어요") and (claude["status"], claude["answer"]) == ("SUCCESS", "거절합니다")
    assert gemini["status"] == "ERROR" and "JSON" in gemini["error"]
    missing = agent_collection.ask_with_tools("gpt", {"key": "k", "model": "m"}, "질문", [{"name": "x.pdf", "type": "pdf", "path": "../x.pdf"}],
                                              tmp_path, client)
    assert missing["status"] == "ERROR" and "첨부파일이 없습니다" in missing["error"]   # 폴더 밖 경로는 읽지 않는다
    sent_body = {}

    def capture(request):
        sent_body.update(json.loads(request.content))
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "web_search_call", "action": {"sources": [{"url": "https://example.com/a"}]}},
            {"type": "message", "content": [{"type": "output_text", "text": "ok", "annotations": [
                {"type": "url_citation", "url": "https://example.com/a", "title": "A", "start_index": 0, "end_index": 2}]}]}]})

    (tmp_path / "a1.pdf").write_bytes(b"%PDF")
    out = agent_collection.ask_with_tools("gpt", {"key": "k", "model": "m"}, "질문", [], tmp_path, httpx.Client(transport=httpx.MockTransport(capture)),
                                          reference="긴 자료", history=[(ROWS[0], "앞 답"), ("앞 질문", "앞 답 2")])
    assert sent_body["tools"] == [{"type": "web_search"}] and sent_body["input"][2] == {"role": "user", "content": [{"type": "input_text", "text": "앞 질문"}]}
    assert [b["type"] for b in sent_body["input"][0]["content"]] == ["input_file", "input_text"]   # 앞 턴의 첨부도 다시 보낸다
    assert sent_body["input"][-1]["content"][-1]["text"].startswith("[참조 자료]\n긴 자료")
    assert out["status"] == "SUCCESS" and out["tools_used"] == ["web_search"] and out["answer_sources"][0]["url"] == "https://example.com/a"
    empty = agent_collection.ask_with_tools("gpt", {"key": "k", "model": "m"}, "질문", [], tmp_path, httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": "completed", "output": None}))))
    assert empty["status"] == "ERROR" and "message" in empty["error"]   # 답변 자리가 없는 응답은 빈 답변(성공)과 구분한다
    broken = agent_collection.ask_with_tools("claude", {"key": "k", "model": "m"}, "질문", [], tmp_path, httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": 42}]}))))
    assert broken["status"] == "ERROR" and "글자가 아닙니다" in broken["error"]   # 글자가 아닌 본문은 빈 정상 답변으로 보지 않는다
    shape = agent_collection.ask_with_tools("gemini", {"key": "k", "model": "m"}, "질문", [], tmp_path, httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": 42}}]}))))
    assert shape["status"] == "ERROR" and "모양" in shape["error"]   # 본문 그릇이 목록이 아니면 예외 대신 수집 오류


def test_weak_units_lists_checks_and_criteria_every_model_passed(tmp_path):
    """변별력 되먹임 — 모든 모델이 모든 평가된 적용 질문에 PASS 한 CODE 검사·의미 기준만. FAIL·미확정(확인 필요), 검사기 소분류 오류
    (<소분류>.check_error → 그 소분류 단위 전부), 답은 있는데 의미 평가가 실패한 질문은 제외. 수집 실패(답 없음)는 unevaluated 로만 센다."""
    svc = ComparisonService(CodeEvalService(), save_dir=tmp_path)
    run = {"providers": ["gpt", "claude"],
           "datasets": {"gpt": [{"answer": "a"}, {"answer": "b"}, {"answer": "c"}],
                        "claude": [{"answer": "a"}, {"answer": "", "__parse_error__": "수집 실패"}, {"answer": "c"}]},
           "evaluations": {
        "gpt": {"cases": [{"case_status": "EVALUATED", "results": [{"check_id": "2-1.count", "verdict": "PASS"}, {"check_id": "2-2.table", "verdict": "FAIL"},
                                                                   {"check_id": "1-2.json_syntax:none", "verdict": "PASS"}, {"check_id": "3-3.aggregate", "verdict": "PASS"}]},
                          {"case_status": "EVALUATED", "results": [{"check_id": "2-1.char_count", "verdict": "PASS"}]},
                          {"case_status": "EVALUATED", "results": [{"check_id": "2-1.literal", "verdict": "PASS"}]}],
                "jev_eval": {"records": [{"status": "SUCCESS", "decisions": {"J1": {"decision": "PASS"}, "J5": {"decision": "FAIL"},
                                                                             "J7": {"decision": None, "status": "UNVERIFIED"}, "J8": {"decision": None, "status": "NOT_APPLICABLE"}}},
                                         {"status": "SUCCESS", "decisions": {"J1": {"decision": "PASS"}, "J2": {"decision": "PASS"}}},
                                         {"status": "SUCCESS", "decisions": {"J3": {"decision": "PASS"}}}]}},
        "claude": {"cases": [{"case_status": "EVALUATED", "results": [{"check_id": "2-1.count", "verdict": "PASS"}],
                              "errors": [{"check_id": "3-3.check_error", "subcategory": "3-3", "message": "검사기 오류"}]},
                             {"case_status": "INPUT_ERROR", "results": []},
                             {"case_status": "EVALUATED", "results": [{"check_id": "2-1.literal", "verdict": "PASS"}]}],
                   "jev_eval": {"records": [{"status": "SUCCESS", "decisions": {"J1": {"decision": "PASS"}}}, {"status": "SKIPPED", "decisions": {}},
                                            {"status": "INVALID_RESPONSE", "decisions": None}]}}}}
    svc.result = lambda run_id: run
    assert svc.weak_units("x") == [{"id": "2-1.char_count", "name": "글자 수", "unevaluated": 1},
                                   {"id": "2-1.count", "name": "불릿·항목·문장·문단·줄·어절 수", "unevaluated": 0},
                                   {"id": "2-1.literal", "name": "필수·금지·시작·끝 문자열 (정확 일치 요구 외 공백 접기)", "unevaluated": 0},
                                   {"id": "J1", "name": "의도 충족성", "unevaluated": 1}, {"id": "J2", "name": "내용 적절성", "unevaluated": 1}]
    assert svc.weak_units("x") and ComparisonService.weak_units(svc, "x")   # 3-3.aggregate(검사기 오류)·J3(의미 평가 실패)는 빠진다
    run["providers"].append("gemini")   # 평가가 없는 모델이 있으면 '모든 모델 PASS' 를 말할 수 없다
    assert svc.weak_units("x") == []

def test_api_collector_sends_every_attachment_type_as_the_original_file(tmp_path):
    """TXT·DOCX·PDF·PNG 를 세 API 모두 원본 파일로 보낸다 — GPT input_file/input_image, Gemini inline_data, Claude 는 TXT 문서 블록·
    DOCX Files API + container_upload + 코드 실행(요청 뒤 삭제) (2026-10-14 사용자 요청, 작은 파일로 실제 확인)."""
    files = {"m.txt": b"hello", "d.docx": b"PK-docx", "r.pdf": b"%PDF", "c.png": b"PNG"}
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    attachments = [{"name": n, "type": {"txt": "txt", "docx": "docx", "pdf": "pdf", "png": "image"}[n.split(".")[1]], "path": n} for n in files]
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.content))
        if request.url.path.endswith("/files") and request.method == "POST":
            return httpx.Response(200, json={"id": "file_1"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"id": "file_1", "type": "file_deleted"})
        if request.url.host == "api.openai.com":
            return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]})
        if request.url.host == "api.anthropic.com":
            if len([c for c in calls if c[1].endswith("/messages")]) == 1:   # 첫 응답은 도구 실행 중 멈춤 — 이어 받기
                return httpx.Response(200, json={"stop_reason": "pause_turn", "content": [{"type": "text", "text": "읽는 중 "}]})
            return httpx.Response(200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]})
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "ok"}]}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    for provider in ("gpt", "gemini", "claude"):
        out = agent_collection.ask_with_tools(provider, {"key": "k", "model": "m"}, "질문", attachments, tmp_path, client)
        assert out["status"] == "SUCCESS" and all(s["how"].startswith("파일 그대로") for s in out["sent"]), (provider, out)
    bodies = {path: json.loads(content) for method, path, content in calls if method == "POST" and not path.endswith("/files")}
    gpt = [b["type"] for b in bodies["/v1/responses"]["input"][0]["content"]]
    assert gpt == ["input_file", "input_file", "input_file", "input_image", "input_text"]
    mimes = [p["inline_data"]["mime_type"] for p in bodies[next(k for k in bodies if k.endswith(":generateContent"))]["contents"][0]["parts"] if "inline_data" in p]
    assert mimes == ["text/plain", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/pdf", "image/png"]
    claude = bodies["/v1/messages"]
    assert [b["type"] for b in claude["messages"][0]["content"]] == ["document", "container_upload", "document", "image", "text"]
    assert claude["messages"][0]["content"][0]["source"] == {"type": "text", "media_type": "text/plain", "data": "hello"}
    assert {"type": "code_execution_20260120", "name": "code_execution"} in claude["tools"] and claude["messages"][-1]["role"] == "assistant"
    assert ("DELETE", "/v1/files/file_1", b"") in calls   # 올린 DOCX 는 요청 뒤 지운다

def test_claude_docx_upload_cleanup_errors_and_continuation_are_safe(tmp_path):
    """Claude DOCX 업로드 도중 실패해도 올린 파일을 지우고, content 가 목록이 아닌 응답·삭제 실패·제공자 오류 문구를 안전하게 다룬다.
    pause_turn 이어 받기는 같은 컨테이너를 쓰고 사용량을 더한다 (Codex 리뷰)."""
    for name in ("a.docx", "b.docx"):
        (tmp_path / name).write_bytes(b"PK")
    docs = [{"name": n, "type": "docx", "path": n} for n in ("a.docx", "b.docx")]
    config = {"key": "k", "model": "m"}

    def run(handler, attachments=docs):
        calls = []

        def wrapped(request):
            calls.append((request.method, request.url.path, json.loads(request.content) if request.method == "POST" and request.url.path.endswith("/messages") else None))
            return handler(request, calls)
        out = agent_collection.ask_with_tools("claude", config, "질문", attachments, tmp_path, httpx.Client(transport=httpx.MockTransport(wrapped)))
        return out, calls

    uploads = iter([httpx.Response(200, json={"id": "file_a"}), httpx.Response(200, json=[])])
    out, calls = run(lambda r, c: next(uploads) if r.url.path.endswith("/files") else httpx.Response(200, json={}))
    assert out["status"] == "ERROR" and ("DELETE", "/v1/files/file_a", None) in calls   # 두 번째 업로드가 깨져도 첫 파일은 지운다

    def bad_content(request, calls):
        if "/files" in request.url.path:
            return httpx.Response(200, json={"id": "file_x"}) if request.method == "POST" else httpx.Response(503)
        return httpx.Response(200, json={"stop_reason": "end_turn", "content": None})
    out, calls = run(bad_content, docs[:1])
    assert out["status"] == "ERROR" and "content" in out["error"]   # 빈 답변 성공으로 바꾸지 않는다
    assert out["cleanup_failed"]["provider_files"] == ["file_x"]   # 삭제가 끝내 실패하면 file_id 를 남긴다

    def paused(request, calls):
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json={"id": "file_y"})
        first = sum(1 for c in calls if c[1].endswith("/messages")) == 1
        return httpx.Response(200, json={"stop_reason": "pause_turn" if first else "end_turn", "container": {"id": "cont_1"},
                                         "content": [{"type": "text", "text": "a" if first else "b"}],
                                         "usage": {"input_tokens": 100 if first else 200, "output_tokens": 10 if first else 20}})
    out, calls = run(paused, docs[:1])
    second = [c[2] for c in calls if c[2] is not None][1]
    assert out["status"] == "SUCCESS" and out["answer"] == "ab" and second["container"] == "cont_1"
    assert out["usage"]["input_tokens"] == 300 and out["usage"]["output_tokens"] == 30

    leaked = agent_collection.ask_with_tools("gpt", config, "질문", [], tmp_path, httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(429, json={"error": {"type": "insufficient_quota", "message": "secret-input echo; exceeded your current quota"}}))))
    assert leaked["error"] == "HTTP 429 (insufficient_quota · 크레딧·사용 한도 부족 — 제공자 콘솔에서 충전 필요)"   # 제공자 문구는 남기지 않는다

def test_claude_usage_shape_changes_and_partial_failures_keep_usage(tmp_path):
    """이어 받기 사용량은 타입이 바뀐 필드를 건너뛰고(예외 없음), 도중에 실패해도 이미 쓴 토큰을 오류 결과에 남긴다. 404 삭제는 이미 없음."""
    (tmp_path / "a.docx").write_bytes(b"PK")
    replies = iter([{"stop_reason": "pause_turn", "content": [], "usage": {"input_tokens": 100, "server_tool_use": 3}},
                    {"stop_reason": "end_turn", "content": None, "usage": {"input_tokens": 50, "server_tool_use": {"web_search_requests": 1}}}])

    def handler(request):
        if "/files" in request.url.path:
            return httpx.Response(200, json={"id": "file_z"}) if request.method == "POST" else httpx.Response(404)
        return httpx.Response(200, json=next(replies))
    out = agent_collection.ask_with_tools("claude", {"key": "k", "model": "m"}, "질문", [{"name": "a.docx", "type": "docx", "path": "a.docx"}],
                                          tmp_path, httpx.Client(transport=httpx.MockTransport(handler)))
    assert out["status"] == "ERROR" and out["usage"]["input_tokens"] == 150 and "cleanup_failed" not in out


def test_aiona_login_status_reports_presence_only(tmp_path):
    """연결된 모델 화면 — .env 에 AIONA 로그인 정보가 있는지와 로그인 프로필이 있는지만 알리고 값은 돌려주지 않는다."""
    from webapp.aiona_web import login_status
    env = tmp_path / ".env"
    env.write_text("AIONA_LOGIN_ID=tester@example.test\nAIONA_LOGIN_PASSWORD=not-a-real-password\n", encoding="utf-8")
    (tmp_path / "profile" / "Default").mkdir(parents=True)
    status = login_status(env, tmp_path / "profile")
    assert status["credentials"] is True and status["profile_saved"] is True
    assert "tester@example.test" not in str(status) and "not-a-real-password" not in str(status)
    assert login_status(tmp_path / "missing.env", tmp_path / "none")["credentials"] is False


def test_cdp_web_models_replace_their_api_models_when_the_collection_chrome_is_up(tmp_path, monkeypatch):
    """수집용 Chrome(tools/chrome_cdp.py)이 떠 있고 사이트 탭이 열려 있으면 ChatGPT·Gemini·Claude 는 웹 탭으로 받고 같은 회사 API 는 빼며,
    각 웹 모델은 자기 어댑터로 새 채팅 → 질문 순서대로 묻는다 (사용자 결정 2026-10-15: 수집은 전부 웹브라우저)."""
    from webapp import cdp_chat_web

    class WebFake(FakeChat):
        NAME = "웹"

        def ask(self, question, files=None, reference=None):
            FakeChat.asked.append(question)
            return {"answer": f"{self.NAME}: {question}", "status": "SUCCESS", "sent": []}

    fakes = {p: type(f"Fake{p}", (WebFake,), {"NAME": p}) for p in cdp_chat_web.CDP_PROVIDERS}
    monkeypatch.setattr(aiona_web, "AionaChat", FakeChat)
    monkeypatch.setattr(cdp_chat_web, "CDP_CHATS", fakes)
    monkeypatch.setattr(comparison_service, "cdp_login_status",
                        lambda *_, **__: {"credentials": True, "tabs": {"chatgpt": True, "gemini": True, "claude": False}})
    monkeypatch.setattr(comparison_service, "ask_with_tools",
                        lambda provider, *a, **k: {"answer": f"{provider}: api", "status": "SUCCESS", "sent": []})
    svc = ComparisonService(CodeEvalService(), save_dir=tmp_path / "runs")
    svc.configure("gpt", {"key": "k", "model": "gpt-x"})
    svc.configure("claude", {"key": "k", "model": "claude-x"})
    run = svc.start_question_set_run({"set_id": "qs-2", "rows": ROWS[1:], "auto_evaluate": False})
    for _ in range(500):
        if svc.status(run["run_id"])["status"] != "COLLECTING":
            break
        time.sleep(0.01)
    result = svc.result(run["run_id"])
    # Claude 탭이 없으니 Claude 는 API 그대로, ChatGPT 는 웹 탭이 API(gpt) 를 대신한다
    assert result["providers"] == ["aiona", "chatgpt_web", "gemini_web", "claude"]
    assert result["models"]["chatgpt_web"].startswith("ChatGPT 웹브라우저") and result["question_set"]["chatgpt_web"] == cdp_chat_web.CDP_WEB_VERSION
    assert [r["answer"] for r in result["datasets"]["gemini_web"]] == ["gemini_web: 첫 질문", "gemini_web: 이어지는 질문"]
    assert [r["answer"] for r in result["datasets"]["claude"]] == ["claude: api", "claude: api"]


def test_resume_keeps_successful_turns_and_recollects_broken_conversations_whole(tmp_path, monkeypatch):
    """이어서 수집(resume_from): 앞선 실행에서 성공한 단일 질문은 그대로 옮기고 다시 묻지 않는다. 한 턴이라도 못 받은 대화는
    채팅 하나로 이어져야 하므로 통째로 다시 받는다 (사용자 요청 2026-10-15: 50분까지 하고 멈췄다가 나중에 이어서)."""
    monkeypatch.setattr(aiona_web, "AionaChat", FakeChat)
    svc = ComparisonService(CodeEvalService(), save_dir=tmp_path / "runs")
    rows = [{"case_id": "Q1", "question": "단일 질문"}, *ROWS[1:]]
    first = svc.start_question_set_run({"set_id": "qs-3", "rows": rows, "auto_evaluate": False})
    for _ in range(500):
        if svc.status(first["run_id"])["status"] != "COLLECTING":
            break
        time.sleep(0.01)
    prev = svc._runs[first["run_id"]]   # 저장된 실행 기록을 직접 — result() 는 복사본
    prev["datasets"]["aiona"][2]["collection"]["status"] = "ERROR"   # 대화 C2 의 2번째 턴을 못 받은 것으로
    FakeChat.asked.clear()
    second = svc.start_question_set_run({"set_id": "qs-3", "rows": rows, "auto_evaluate": False, "resume_from": first["run_id"]})
    for _ in range(500):
        if svc.status(second["run_id"])["status"] != "COLLECTING":
            break
        time.sleep(0.01)
    result = svc.result(second["run_id"])
    assert result["resumed_from"] == first["run_id"] and result["prefilled"] == {"aiona": [0]}
    assert "단일 질문" not in FakeChat.asked and FakeChat.asked == ["새 채팅", "첫 질문", "이어지는 질문"]   # 대화만 통째로 다시
    assert [r["answer"] for r in result["datasets"]["aiona"]] == ["AIONA: 단일 질문", "AIONA: 첫 질문", "AIONA: 이어지는 질문"]
    assert result["done"] == 3 and result["progress"]["aiona"]["success"] == 3
    with pytest.raises(ValueError):   # 다른 질문 세트의 실행으로는 이어받지 않는다
        svc.start_question_set_run({"set_id": "qs-4", "rows": ROWS[:1], "auto_evaluate": False, "resume_from": first["run_id"]})


def test_resume_recollects_web_answers_taken_without_the_chosen_model_settings():
    """웹 수집에서 모델 설정을 맞추지 못한 채 받은 답(한도 등)은 성공이어도 이어받기에서 옮기지 않는다 (2026-10-15 Gemini 한도)."""
    ok = {"collection": {"status": "SUCCESS", "model": "Gemini 자동 모드 · 사고 수준 보통 (웹)"}}
    degraded = {"collection": {"status": "SUCCESS", "model": "Gemini 자동 모드 · 사고 수준 보통 (웹) (설정 확인 못 함: TimeoutError)"}}
    failed = {"collection": {"status": "ERROR", "model": "Gemini 자동 모드 · 사고 수준 보통 (웹)"}}
    assert ComparisonService._reusable(ok) and not ComparisonService._reusable(degraded)
    assert not ComparisonService._reusable(failed) and not ComparisonService._reusable(None)
