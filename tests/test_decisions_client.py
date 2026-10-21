# -*- coding: utf-8 -*-
"""OpenAI Decisions API 대체 심판 (decisions-v1) — 요청 모양·응답 변환·오류·둘째 심판 고르기."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from code_eval.decisions_client import (DecisionsClientConfig, HttpDecisionsClient, from_decisions_answers, resolve_decisions_client,
                                        to_decisions_questions)
from tools import judge_v4

QUESTIONS = {"T1:J1-1.a": {"type": "choice", "instructions": "[J1-1.a] 요청 작업 수행 — …",
                           "criteria": {"PASS": "예", "FAIL": "아니오", "NOT_APPLICABLE": "해당 없음"}},
             "T1:J2-2.a": {"type": "choice", "instructions": "[J2-2.a] 말투", "criteria": {"PASS": "예", "FAIL": "아니오"}}}
STATE = {"guide": "규칙", "질문번호": 7, "판정할_턴": [1], "턴": [{"턴": 1, "질문": "존댓말로 답해 주세요.", "답변": "네, 그렇게 하겠습니다."}]}


def test_questions_become_choice_questions_with_safe_names():
    qs, names = to_decisions_questions(QUESTIONS)
    assert [q["name"] for q in qs] == ["T1__J1-1.a", "T1__J2-2.a"]
    assert names == {"T1__J1-1.a": "T1:J1-1.a", "T1__J2-2.a": "T1:J2-2.a"}
    assert qs[0]["type"] == "choice" and qs[0]["instructions"].startswith("[J1-1.a]")
    assert [c["value"] for c in qs[0]["choices"]] == ["PASS", "FAIL", "NOT_APPLICABLE"]


def test_answers_map_back_to_jev_shape():
    body = {"model": "gpt-6-luna", "answers": [
        {"type": "choice", "name": "T1__J1-1.a", "choice": "PASS", "probabilities": [{"value": "PASS", "probability": 0.9}, {"value": "FAIL", "probability": 0.1}], "confidence": 0.9},
        {"type": "refusal", "name": "T1__J2-2.a", "reason": "policy"}], "usage": {"input_tokens": 12}}
    out = from_decisions_answers(body, {"T1__J1-1.a": "T1:J1-1.a", "T1__J2-2.a": "T1:J2-2.a"})
    assert out["answers"]["T1:J1-1.a"] == {"choice": "PASS", "probabilities": {"PASS": 0.9, "FAIL": 0.1}, "confidence": 0.9, "reason": None}
    assert out["answers"]["T1:J2-2.a"]["choice"] is None and "거절" in out["answers"]["T1:J2-2.a"]["reason"]


def test_http_client_posts_decisions_and_parses(tmp_path: Path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "gpt-6-luna", "answers": [
            {"type": "choice", "name": "T1__J1-1.a", "choice": "FAIL", "probabilities": [{"value": "FAIL", "probability": 0.8}], "confidence": 0.8},
            {"type": "choice", "name": "T1__J2-2.a", "choice": "PASS", "probabilities": [{"value": "PASS", "probability": 0.95}], "confidence": 0.95}],
            "usage": {"input_tokens": 100}})

    client = HttpDecisionsClient(DecisionsClientConfig(), "sk-test", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    call = client.decide(STATE, QUESTIONS)
    assert call.status == "OK"
    assert seen["url"].endswith("/v1/decisions") and seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "gpt-6-luna" and set(seen["body"]) == {"model", "input", "questions"}
    assert "존댓말로 답해 주세요" in seen["body"]["input"] and len(seen["body"]["questions"]) == 2   # 원문 그대로, 자르지 않음
    assert call.body["answers"]["T1:J1-1.a"]["choice"] == "FAIL" and call.body["answers"]["T1:J2-2.a"]["choice"] == "PASS"
    assert call.usage == {"input_tokens": 100}


def _set_with_files(tmp_path: Path) -> Path:
    """실제 파일 네 가지 — PNG(Pillow)·PDF(fpdf2 없으면 건너뜀)·DOCX(python-docx)·TXT."""
    from PIL import Image
    files = tmp_path / "files"
    files.mkdir()
    Image.new("RGB", (40, 30), "white").save(files / "사진.png")
    (files / "메모.txt").write_text("첫 줄\n둘째 줄 금액 1,200원", encoding="utf-8")
    import docx
    d = docx.Document()
    d.add_paragraph("규정 개정안")
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "항목", "값"
    d.save(files / "규정.docx")
    return files


def test_attachment_parts_send_original_files(tmp_path: Path):
    from code_eval.decisions_client import attachment_parts, build_input
    files = _set_with_files(tmp_path)
    parts, note = attachment_parts({"이름": "사진.png", "종류": "image", "경로": "사진.png"}, files)
    assert len(parts) == 1 and parts[0]["type"] == "input_image" and parts[0]["image_url"].startswith("data:image/png;base64,") and "원본 1장" in note
    parts, note = attachment_parts({"이름": "메모.txt", "종류": "txt", "경로": "메모.txt"}, files)
    assert parts[0]["type"] == "input_text" and "둘째 줄 금액 1,200원" in parts[0]["text"] and "글 파일 원본" in note
    parts, note = attachment_parts({"이름": "규정.docx", "종류": "docx", "경로": "규정.docx"}, files)
    if parts and parts[0]["type"] == "input_image":   # Word 가 있으면 docx 도 쪽 이미지로 (2026-10-21 사용자 요청)
        assert all(p["type"] == "input_image" for p in parts) and "DOCX" in note
    else:                                              # Word 가 없으면 예전처럼 문단·표 글
        assert parts[0]["type"] == "input_text" and "규정 개정안" in parts[0]["text"] and "| 항목 | 값 |" in parts[0]["text"] and "DOCX 원본" in note
    parts, note = attachment_parts({"이름": "없음.pdf", "종류": "pdf", "경로": "없음.pdf"}, files)
    assert parts == [] and "파일 없음" in note
    # build_input: 경로가 있는 첨부는 원본 조각을 붙여 메시지 모양으로, 없으면 글 그대로
    state = {"guide": "g", "질문번호": 1, "판정할_턴": [1], "턴": [{"턴": 1, "질문": "q", "답변": "a",
             "첨부파일": [{"이름": "사진.png", "종류": "image", "내용": "글로 옮긴 내용", "경로": "사진.png"}, {"이름": "메모.txt", "종류": "txt", "내용": "x", "경로": "메모.txt"}]}]}
    inp, info = build_input(state, files)
    assert isinstance(inp, list) and inp[0]["role"] == "user" and info["images"] == 1 and len(info["files"]) == 2
    kinds = [c["type"] for c in inp[0]["content"]]
    assert kinds == ["input_text", "input_image", "input_text"] and "글로 옮긴 내용" in inp[0]["content"][0]["text"]
    text_only, info2 = build_input(state, None)
    assert isinstance(text_only, str) and info2 == {"images": 0, "files": []}


def test_pdf_pages_become_images(tmp_path: Path):
    pytest.importorskip("pypdfium2")
    fpdf = pytest.importorskip("fpdf")
    from code_eval.decisions_client import attachment_parts
    files = tmp_path / "files"
    files.mkdir()
    pdf = fpdf.FPDF()
    for _ in range(3):
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.cell(0, 10, "page")
    pdf.output(str(files / "안내.pdf"))
    parts, note = attachment_parts({"이름": "안내.pdf", "종류": "pdf", "경로": "안내.pdf"}, files)
    assert len(parts) == 3 and all(p["type"] == "input_image" for p in parts) and "PDF 3쪽" in note


def test_http_errors_are_results_not_exceptions():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "input too long"}})

    client = HttpDecisionsClient(DecisionsClientConfig(), "sk-test", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    call = client.decide(STATE, QUESTIONS)
    assert call.status == "API_ERROR" and "HTTP 400" in call.error and "input too long" in call.error


def test_retries_on_429_then_ok():
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json={"answers": [{"type": "choice", "name": "T1__J1-1.a", "choice": "PASS", "probabilities": [], "confidence": 1}]})

    client = HttpDecisionsClient(DecisionsClientConfig(), "sk-test", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    call = client.decide(STATE, {"T1:J1-1.a": QUESTIONS["T1:J1-1.a"]})
    assert call.status == "OK" and call.attempts == 2


def test_resolve_uses_openai_key_and_can_be_turned_off(tmp_path: Path):
    none_env = tmp_path / ".no-env"
    assert resolve_decisions_client(environment={}, env_path=none_env)[0] is None
    client, config, reason = resolve_decisions_client(environment={"OPENAI_API_KEY": "sk-x"}, env_path=none_env)
    assert client is not None and reason is None and config.model == "gpt-6-luna"
    assert resolve_decisions_client(environment={"OPENAI_API_KEY": "sk-x", "DECISIONS_FALLBACK": "0"}, env_path=none_env)[0] is None
    client, config, _ = resolve_decisions_client(environment={"DECISIONS_API_KEY": "sk-d", "DECISIONS_MODEL": "gpt-6-luna-2"}, env_path=none_env)
    assert client is not None and config.model == "gpt-6-luna-2"


class _Judge:
    def __init__(self, name, status="OK", error=""):
        self.name, self.status, self.error, self.calls = name, status, error, []

    def judge(self, state, questions):
        self.calls.append(judge_v4.js.state_chars(state))
        return (self.status, {"answers": {k: {"choice": "PASS", "probabilities": {"PASS": 1.0}} for k in questions}} if self.status == "OK" else None, self.error)


def _part(chars: int):
    return {"guide": "g", "질문번호": 1, "판정할_턴": [1], "턴": [{"턴": 1, "질문": "q", "답변": "a", "첨부파일": [{"이름": "f.txt", "내용": "x" * chars}]}]}


def test_second_judge_prefers_jev_within_budget():
    jev, dec = _Judge("jev"), _Judge("decisions")
    status, body, err, used, cut = judge_v4.second_judge(_part(100), {"T1:J1-1.a": {}}, jev, dec, budget=1000)
    assert (status, used, cut) == ("OK", "jev", 0) and dec.calls == []


def test_second_judge_uses_decisions_when_over_budget_or_jev_fails():
    jev, dec = _Judge("jev"), _Judge("decisions")
    status, body, err, used, cut = judge_v4.second_judge(_part(5000), {"T1:J1-1.a": {}}, jev, dec, budget=1000)
    assert (status, used, cut) == ("OK", "decisions", 0) and jev.calls == [] and dec.calls and dec.calls[0] > 5000   # 원문 그대로
    jev_bad = _Judge("jev", "ERROR", "HTTP 400")
    status, _, _, used, _ = judge_v4.second_judge(_part(100), {"T1:J1-1.a": {}}, jev_bad, dec, budget=1000)
    assert (status, used) == ("OK", "decisions")


def test_second_judge_falls_back_to_trimmed_jev_then_none():
    jev, dec_bad = _Judge("jev"), _Judge("decisions", "ERROR", "HTTP 404")
    status, _, _, used, cut = judge_v4.second_judge(_part(5000), {"T1:J1-1.a": {}}, jev, dec_bad, budget=1000)
    assert (status, used) == ("OK", "jev-trimmed") and cut > 0 and jev.calls and jev.calls[0] <= 1000 + 400
    status, _, err, used, _ = judge_v4.second_judge(_part(5000), {"T1:J1-1.a": {}}, None, dec_bad, budget=1000)
    assert status == "ERROR" and used is None and "Jev 없음" in err and "Decisions" in err


def test_combine_label_names_decisions():
    dec, how = judge_v4.combine({"decision": "PASS"}, {"decision": "FAIL"}, False, "Decisions(OpenAI)")
    assert dec == "UNVERIFIED" and how.startswith("심판 갈림 — Decisions(OpenAI) FAIL")
