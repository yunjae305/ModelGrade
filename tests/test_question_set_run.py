"""질문 세트 실행 — AIONA(웹)·API(+도구) 수집이 함께 돌고, 멀티턴은 각자 앞 답변을 잇고, 보낸 첨부만 남는다. 유료 호출 없음."""
from __future__ import annotations

import json
import time

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
    assert ("gpt", "이어지는 질문", [("첫 질문", "gpt: 첫 질문")]) in calls   # 멀티턴은 그 모델의 앞 답변을 잇는다
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
        return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]})

    agent_collection.ask_with_tools("gpt", {"key": "k", "model": "m"}, "질문", [], tmp_path, httpx.Client(transport=httpx.MockTransport(capture)),
                                    reference="긴 자료", history=[("앞 질문", "앞 답")])
    assert sent_body["tools"] == [{"type": "web_search"}] and sent_body["input"][0] == {"role": "user", "content": "앞 질문"}
    assert sent_body["input"][-1]["content"][-1]["text"].startswith("[참조 자료]\n긴 자료")
