"""항목별 의미 평가 Mock E2E 시연 — 실제 평가 경로에 Mock Judge 하나만 끼워 CASE A~F 를 돌리고 HTML 로 남긴다 (유료 호출 0회).

    python tools/items_e2e_demo.py            # results/items-e2e-demo/items-e2e-demo.html
    python tools/items_e2e_demo.py --json     # 시나리오별 근거·검사 JSON

실제 코드: ``CodeEvalService.evaluate`` (CODE 엔진 → 적용 항목 선택 → 의미 평가 요청 → 인용 대조·재확인 ≤3회 →
J5 반박 출처 원문 대조 ``server_check`` → 항목·세부 기준·기준 집계 → 질문 판정 → 저장 파일).
Mock: 의미 평가 모델의 YES/NO·인용·이유, J5 검색 결과와 출처 원문. Mock 판정은 Claude Opus 5.5 의 판정이 아니다.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval import multi_turn, semantic_sources  # noqa: E402
from code_eval.combined import combined_summary  # noqa: E402
from code_eval.criteria_registry import CODE_UNITS  # noqa: E402
from code_eval.jev_applicability import SUB_APPLICABILITY_VERSION  # noqa: E402
from code_eval.jev_client import JevCall, JevClientConfig, resolve_semantic_pipeline  # noqa: E402
from code_eval.report_html import render_report  # noqa: E402
from code_eval.semantic_facts import server_check  # noqa: E402
from code_eval.semantic_items import FACT_UNVERIFIABLE, ITEM_UNVERIFIABLE, ITEMS, SUBS, applicable_items  # noqa: E402
from tools.question_set_report import _CSS, _e  # noqa: E402
from webapp.code_eval_service import CodeEvalService  # noqa: E402
from webapp.run_summary import question_outcomes, question_units, unit_outcome  # noqa: E402

DEMO_VERSION = "items-e2e-demo-v1"
DEFAULT_OUT = ROOT / "results" / "items-e2e-demo" / "items-e2e-demo.html"
MOCK_MODEL = "mock-judge (대역, Claude 아님)"
STAMP = "2026-10-13T03:00:00+00:00"
URL_OK = "https://official.example/caffeine/half-life"
URL_BAD = "https://official.example/caffeine/overview"
REBUTTAL = "The average half-life of caffeine in healthy adults is about 5 hours."
#: 의미 평가 요청 state 에 들어가도 되는 키 — 평가 대상 시스템·모델 이름은 보내지 않는다 (블라인드)
STATE_KEYS = {"question", "answer", "history", "reference", "collected_at"}
JUDGE_IDS = ("J1", "J2", "J3", "J4", "J5", "J6", "J7", "J8")   # 설계안 v1.6 — J7 롱컨텍스트·J8 첨부파일 (시연 입력에는 긴 입력·첨부가 없어 적용되지 않음)

T1_Q = "앞으로 존댓말로만 답해 주세요. 부모님과 가는 부산 1박 2일 여행이고, 숙소 예산은 1박 20만 원 이하예요. 첫날 일정을 짜 주세요."
T2_Q = "숙소 예산을 1박 15만 원으로 줄여서 숙소만 다시 골라 주세요."
T3_Q = "좋아요. 그럼 첫날 일정과 숙소를 한 번에 정리해 주세요."
E_Q = "저녁에 커피 마시면 잠을 잘 못 자는 이유가 뭐야? 카페인이 몸에 얼마나 오래 남아?"   # J5-1 은 원리·이유·정의·정보 질의 표현이 있어야 적용
E2_Q = "커피를 마시면 왜 잠이 깨는 거야? 카페인은 몸에 얼마나 오래 남아 있어?"
CLAIM = "성인 기준 카페인의 반감기는 약 12시간"


def _claim(url: str, quote: str) -> List[Dict[str, Any]]:
    return [{"id": "J5-1.a#1", "decision": "NO", "question_quote": "", "answer_quote": CLAIM, "reason": "공식 자료의 반감기는 약 5시간",
             "turn": None, "fact": {"claim": CLAIM, "verdict": "FALSE", "correct": "약 5시간", "search_queries": ["caffeine half-life adults"],
                                    "sources": [{"url": url, "quote": quote}]}}]


#: (id, 제목, 확인할 것, 입력 행들, 질문별 Mock 대본) — 대본: items(항목 id → 덮어쓸 값·목록), apply_model_items, verification, status
SCENARIOS = [
    {"id": "A", "title": "정상 답변", "goal": "CODE 와 적용 기준이 모두 PASS 면 질문 PASS",
     "cases": [{"case_id": "A", "question": "대학생이 쓸 노트북을 불릿으로 정확히 3개 추천하고 각각 이유를 한 줄씩 붙여줘.",
                "answer": "- 레노버 아이디어패드 슬림 5: 가볍고 배터리가 오래가 강의실 이동에 좋습니다.\n"
                          "- 에이수스 비보북 14: 70만 원대 가격에 문서 작업 성능이 충분합니다.\n"
                          "- 애플 맥북 에어 M2: 학생 할인을 받으면 오래 쓰기 좋은 선택입니다."}],
     "scripts": [{}]},
    {"id": "B", "title": "CODE FAIL + 의미 평가 계속", "goal": "불릿 3개 요구에 2개 — CODE FAIL 이어도 의미 평가 요청이 나가고 판정이 따로 기록",
     "cases": [{"case_id": "B", "question": "공부 방법을 불릿으로 정확히 3개 작성해줘.", "answer": "- 개념을 정리하세요.\n- 문제를 풀어보세요."}],
     "scripts": [{}]},
    {"id": "C", "title": "의미 평가 FAIL과 근거", "goal": "추천 이유 요구 누락 — J4-2.a NO 가 질문 요구·답변 인용과 연결",
     "cases": [{"case_id": "C", "question": "주말에 가족과 볼 영화를 불릿으로 정확히 3개 추천하고 각각 추천 이유도 붙여줘.",
                "answer": "- 인사이드 아웃 2\n- 쿵푸팬더 4\n- 위키드"}],
     "scripts": [{"items": {"J4-2.a": {"decision": "NO", "question_quote": "각각 추천 이유도 붙여줘", "answer_quote": "- 인사이드 아웃 2",
                                       "reason": "추천 이유 요구가 답변에 없음"}}}]},
    {"id": "C2", "title": "없는 인용은 인정하지 않음", "goal": "Mock 이 답변에 없는 문장을 인용해 YES — 재확인 3회 뒤 확인 불가 FAIL",
     "cases": [{"case_id": "C2", "question": "초등학생이 이해할 수 있게 무지개가 생기는 원리를 설명해줘.",
                "answer": "햇빛이 공기 속 물방울에 들어가면 꺾이고 반사되면서 여러 색으로 나뉘어요. 그래서 비 온 뒤 해를 등지고 보면 무지개가 보여요."}],
     "scripts": [{"items": {"J2-1.a": {"decision": "YES", "answer_quote": "아이 눈높이에 맞춘 쉬운 말로 설명합니다", "reason": "쉬운 표현"}}}]},
    {"id": "D", "title": "멀티턴 J6", "goal": "1턴 존댓말 지시·2턴 예산 변경을 3턴이 어김 — 이전 턴 원문 전달과 J6 FAIL, 대화 1개 = 1문항",
     "cases": [{"case_id": "D-T1", "conversation_id": "D", "turn": 1, "question": T1_Q,
                "answer": "네, 첫날은 해운대 해변 산책 후 동백섬을 둘러보시고, 저녁에는 광안리 야경을 보시는 일정을 추천드립니다. "
                          "숙소는 1박 18만 원인 해운대 근처 호텔이 예산에 맞습니다."},
               {"case_id": "D-T2", "conversation_id": "D", "turn": 2, "question": T2_Q,
                "answer": "네, 1박 14만 원인 광안리 근처 비즈니스호텔을 추천드립니다. 바다 전망 객실도 선택하실 수 있습니다."},
               {"case_id": "D-T3", "conversation_id": "D", "turn": 3, "question": T3_Q,
                "answer": "첫날은 해운대 산책, 동백섬, 광안리 야경 순서로 다니면 돼. 숙소는 1박 18만 원인 해운대 근처 호텔로 잡으면 돼."}],
     "scripts": [{}, {"apply_model_items": True},
                 {"apply_model_items": True, "items": {
                     "J6-1.a": {"decision": "NO", "turn": 1, "question_quote": "앞으로 존댓말로만 답해 주세요",
                                "answer_quote": "광안리 야경 순서로 다니면 돼", "reason": "1턴의 존댓말 지시를 어기고 반말로 답함"},
                     "J6-3.a": {"decision": "NO", "turn": 2, "question_quote": "숙소 예산을 1박 15만 원으로 줄여서",
                                "answer_quote": "1박 18만 원인 해운대 근처 호텔", "reason": "2턴에서 줄인 숙소 예산 대신 옛 숙소를 다시 씀"}}}]},
    {"id": "E", "title": "J5 출처 원문 대조", "goal": "틀린 수치 주장 — 반박 출처의 인용문을 서버가 원문에서 확인해야 J5 FAIL 근거로 인정",
     "cases": [{"case_id": "E", "question": E_Q, "answer": f"카페인은 졸음을 부르는 아데노신이 수용체에 붙지 못하게 막습니다. {CLAIM}이라 오후에 마신 커피도 밤까지 영향을 줍니다."}],
     "scripts": [{"items": {"J5-1.a": _claim(URL_OK, REBUTTAL)},
                  "verification": {"search_performed": True, "source_urls": [URL_OK], "checked_at": STAMP}}]},
    {"id": "E2", "title": "J5 출처 인용 불일치", "goal": "반박 출처 원문에 인용문이 없음 — 사실 오류로 확정하지 않음. 현재 정책은 재확인 3회 뒤 '확인 불가' FAIL (FAIL·미완료 선택은 열린 결정)",
     "cases": [{"case_id": "E2", "question": E2_Q, "answer": f"카페인이 아데노신 수용체를 막아 졸음을 늦춥니다. {CLAIM}이라 저녁 커피는 피하는 게 좋습니다."}],
     "scripts": [{"items": {"J5-1.a": _claim(URL_BAD, REBUTTAL)},
                  "verification": {"search_performed": True, "source_urls": [URL_BAD], "checked_at": STAMP}}]},
    {"id": "F", "title": "평가기 오류는 답변 결함이 아님", "goal": "의미 평가 호출 시간 초과 — FAIL 이 아니라 판정 미완료",
     "cases": [{"case_id": "F", "question": "주말 아침에 30분 안에 만들 수 있는 메뉴를 하나 추천해줘.",
                "answer": "달걀과 채소를 넣은 오믈렛을 추천합니다. 재료 손질부터 굽기까지 20분이면 충분합니다."}],
     "scripts": [{"status": "TIMEOUT"}]},
]
PAGES = {URL_OK: f"Caffeine overview. {REBUTTAL} Effects vary by person.", URL_BAD: "Caffeine is a stimulant found in coffee and tea."}


def _default_item(item_id: str, spec: Dict[str, Any], state: Dict[str, Any], apply_model: bool) -> Dict[str, Any]:
    """대본에 없는 항목의 Mock 기본 응답 — tests/test_jev_eval_items.default_item 과 같은 계약 (원문 앞부분을 인용해 YES)."""
    question, answer = state.get("question") or "", state.get("answer") or ""
    if (spec.get("model_applies") and not apply_model) or spec.get("fact_block"):
        return {"id": item_id, "decision": "NOT_APPLICABLE", "question_quote": "", "answer_quote": "", "reason": "해당 없음",
                "turn": None, "fact": None}
    if spec.get("expand"):
        return {"id": f"{item_id}#1", "decision": "YES", "question_quote": "" if spec.get("origin") == "answer" else question[:6],
                "answer_quote": answer[:8], "reason": "충족", "turn": None, "fact": None}
    return {"id": item_id, "decision": "YES", "question_quote": question[:6], "answer_quote": answer[:8], "reason": "충족",
            "turn": None, "fact": None}


class MockJudge:
    """items 계약의 Mock Judge. 질문 원문별 대본대로 답하고, 실제 전송 client 처럼 본문에 J5 서버 검증(server_check)을 돌린다."""

    config = JevClientConfig()
    supports_cancellation = True

    def __init__(self, scripts: Dict[str, Dict[str, Any]], pages: Dict[str, str]):
        self.scripts, self.pages = scripts, pages
        self.calls: List[Dict[str, Any]] = []
        self.fetched: List[str] = []
        self._lock = threading.Lock()

    def _fetch(self, url: str) -> Dict[str, Any]:
        with self._lock:
            self.fetched.append(url)
        if url not in self.pages:
            return {"status": "http_error", "text": "", "final_url": None, "checked_at": STAMP, "content_type": None}
        return {"status": "ok", "text": self.pages[url], "final_url": url, "checked_at": STAMP, "content_type": "text/plain"}

    def script_for(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """이 요청에 쓸 대본 — 기본은 질문 원문으로 찾는다 (웹 시연은 AIONA 답변에만 대본을 쓰도록 바꾼다)."""
        return self.scripts.get(state.get("question"), {})

    def system_one(self, state, questions, *, cancel_event=None):
        script = self.script_for(state)
        round_no = max([(q.get("recheck") or {}).get("round", 0) for q in questions.values()] or [0])
        with self._lock:
            self.calls.append({"question": state.get("question"), "round": round_no,
                               "state": copy.deepcopy(state), "questions": copy.deepcopy(questions)})
        if script.get("status"):
            return JevCall(script["status"], error="Mock Judge 응답 시간 초과 (시나리오)", attempts=1, latency_ms=5)
        items = []
        for item_id, spec in questions.items():
            patch = (script.get("items") or {}).get(item_id)
            if isinstance(patch, list):
                items.extend(copy.deepcopy(patch))
                continue
            items.append({**_default_item(item_id, spec, state, script.get("apply_model_items", False)), **(patch or {})})
        verification = copy.deepcopy(script.get("verification") or {"search_performed": False, "source_urls": [], "source_validation": "provider"})
        body = server_check({"model": MOCK_MODEL, "items": items, "verification": verification}, state, questions, verification,
                            fetcher=self._fetch, cancel_event=cancel_event)
        return JevCall("OK", body=body, attempts=1, latency_ms=10, usage={"input_tokens": 0, "output_tokens": 0})


@contextmanager
def _no_network():
    """주입 fetcher 밖의 출처 원문 요청을 막는다 — 시연은 네트워크를 쓰지 않는다."""
    original = semantic_sources.fetch_public_source

    def blocked(*_a, **_k):
        raise RuntimeError("items_e2e_demo: 네트워크 출처 요청 금지")
    semantic_sources.fetch_public_source = blocked
    try:
        yield
    finally:
        semantic_sources.fetch_public_source = original


def _failed_items(decision: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for sid, sub in (decision.get("subs") or {}).items():
        for item in sub.get("items") or []:
            if item.get("decision") == "NO":
                qc = item.get("quote_check") or {}
                facts = [{"claim": (item.get("fact") or {}).get("claim"), "verdict": (item.get("fact") or {}).get("verdict"),
                          "sources": [{"url": s.get("url"), "quote": s.get("quote"), "quote_verified": s.get("quote_verified")}
                                      for s in (item.get("fact") or {}).get("sources") or []]}] if item.get("fact") else []
                out.append({"sub": sid, "id": item.get("id"),
                            "model_decision": (item.get("model_original") or {}).get("decision") or item.get("model_decision"),
                            "question_quote": item.get("question_quote"), "answer_quote": item.get("answer_quote"),
                            "turn": item.get("turn"), "reason": item.get("reason"), "unverifiable": bool(item.get("unverifiable")),
                            "rechecks": len(item.get("rechecks") or []), "fact": facts,
                            "question_quote_ok": (qc.get("question") or {}).get("ok"), "question_quote_source": (qc.get("question") or {}).get("source"),
                            "answer_quote_ok": (qc.get("answer") or {}).get("ok")})
    return out


def _consistent(record: Dict[str, Any]) -> bool:
    """항목 → 세부 기준 → 기준 판정을 독립적으로 다시 계산해 기록과 같은지 (NOT_APPLICABLE 항목은 판정에 넣지 않는다)."""
    for cid, decision in (record.get("decisions") or {}).items():
        subs = decision.get("subs") or {}
        values = []
        for sub in subs.values():
            applied = [it.get("decision") for it in sub.get("items") or [] if it.get("decision") != "NOT_APPLICABLE"]
            expected = "NOT_APPLICABLE" if not applied else "FAIL" if "NO" in applied else "PASS"
            if sub.get("items") is not None and sub.get("decision") != expected:
                return False
            values.append(sub.get("decision"))
        expected = None if not values or all(v == "NOT_APPLICABLE" for v in values) else "FAIL" if "FAIL" in values else "PASS"
        if subs and decision.get("decision") != expected:
            return False
        if decision.get("decision") in ("PASS", "FAIL") and cid not in record.get("criteria", []):
            return False
        if decision.get("status") == "NOT_APPLICABLE" and cid in record.get("criteria", []):
            return False
    stats = record.get("items") or {}
    return stats.get("applied", 0) == stats.get("yes", 0) + stats.get("no", 0)


@contextmanager
def _short_conversations_as_multi_turn():
    """시나리오 D 는 3턴짜리 짧은 대화로 J6 판정 경로(이전 턴 원문 전달·인용 확인·FAIL)를 보여 준다 — 시연하는 동안만
    멀티턴 길이 조건(10K 토큰 이상 턴 10개, multi-turn-v1)을 끈다. 실제 평가에서는 이 대화의 J6 는 해당 없음이다."""
    saved = multi_turn.MIN_LONG_TURNS
    multi_turn.MIN_LONG_TURNS = 0
    try:
        yield
    finally:
        multi_turn.MIN_LONG_TURNS = saved


def run_demo(save_dir: Optional[Path] = None) -> Dict[str, Any]:
    """CASE A~F 를 한 번의 실행으로 평가하고 시나리오별 근거·검사 결과를 돌려준다."""
    cases = [dict(case, system_id="aiona") for s in SCENARIOS for case in s["cases"]]
    scripts = {case["question"]: script for s in SCENARIOS for case, script in zip(s["cases"], s["scripts"])}
    judge = MockJudge(scripts, PAGES)
    with tempfile.TemporaryDirectory() as tmp, _no_network(), _short_conversations_as_multi_turn():
        service = CodeEvalService(save_dir=Path(save_dir or tmp), jev_resolver=lambda **_: (judge, judge.config, None))
        batch = service.evaluate({"cases": cases}, pipeline="items")
        saved_path = service.save_dir / f"code-eval-{batch['run']['run_id']}.json"
        restored = json.loads(saved_path.read_text(encoding="utf-8"))
    at_save = json.loads(json.dumps({**batch, "run": {k: v for k, v in batch["run"].items() if k != "saved_to"}}, ensure_ascii=False))
    jev, combined = batch["jev_eval"], batch["combined"]
    outcomes = question_outcomes(batch)
    index = {case["case_id"]: i for i, case in enumerate(batch["cases"])}
    problems = {p["case_id"]: p for p in combined["problems"]}
    calls_by_q: Dict[str, List[Dict[str, Any]]] = {}
    for call in judge.calls:
        calls_by_q.setdefault(call["question"], []).append(call)

    rows, scenario_out = {}, []
    for scenario in SCENARIOS:
        out_cases = []
        for case in scenario["cases"]:
            i = index[case["case_id"]]
            result, record = batch["cases"][i], jev["records"][i]
            calls = calls_by_q.get(case["question"], [])
            code_fails = [{"check_id": r.get("check_id"), "title": r.get("title"), "reason": (r.get("explain") or {}).get("headline") or r.get("reason")}
                          for r in result.get("results") or [] if r.get("area") == "common" and r.get("verdict") == "FAIL"]
            decisions = record.get("decisions") or {}
            row = {"case_id": case["case_id"], "question": case["question"], "answer": case["answer"],
                   "turn": case.get("turn"), "code_pass": sum(1 for r in result.get("results") or [] if r.get("area") == "common" and r.get("verdict") == "PASS"),
                   "code_fails": code_fails, "record_status": record.get("status"), "record_error": record.get("error"),
                   "criteria": record.get("criteria") or [],
                   "semantic": {cid: d.get("decision") or d.get("status") for cid, d in decisions.items()},
                   "failed_items": {cid: _failed_items(d) for cid, d in decisions.items() if d.get("decision") == "FAIL"},
                   "judge_calls": len(calls), "recheck_rounds": max([c["round"] for c in calls] or [0]),
                   "history_turns": [h.get("turn") for h in (calls[0]["state"].get("history") or [])] if calls else [],
                   "outcome": outcomes[i]["outcome"], "problem_sources": (problems.get(case["case_id"]) or {}).get("sources", [])}
            rows[case["case_id"]] = (row, record, calls)
            out_cases.append(row)
        scenario_out.append({k: scenario[k] for k in ("id", "title", "goal")} | {"cases": out_cases, "script": scenario["scripts"]})

    def r(cid):
        return rows[cid][0]

    def item(cid, criterion):
        found = r(cid)["failed_items"].get(criterion) or []
        return found[0] if found else {}
    units = question_units(cases, len(cases))   # 입력 행의 conversation_id 로 묶는다 (run_summary 와 같은 입력)
    d_units = [u for u in units if len(u) > 1]
    t3_calls = rows["D-T3"][2]
    checks = {
        "A": [("CODE 공통 FAIL 0건", not r("A")["code_fails"]), ("적용 기준 모두 PASS", all(v == "PASS" for c, v in r("A")["semantic"].items() if c in r("A")["criteria"])),
              ("질문 최종 PASS", r("A")["outcome"] == "PASS"), ("Mock 호출 1회 (재확인 없음)", r("A")["judge_calls"] == 1)],
        "B": [("CODE 2-1.count FAIL", any(f["check_id"].startswith("2-1.count") for f in r("B")["code_fails"])),
              ("CODE FAIL 뒤에도 의미 평가 요청이 나감", r("B")["judge_calls"] >= 1 and r("B")["record_status"] == "SUCCESS"),
              ("의미 기준은 따로 PASS 로 기록", all(v == "PASS" for c, v in r("B")["semantic"].items() if c in r("B")["criteria"])),
              ("질문 FAIL, 원인은 CODE 만", r("B")["outcome"] == "FAIL" and r("B")["problem_sources"] == ["CODE"])],
        "C": [("J4 FAIL · 세부 J4-2 FAIL", r("C")["semantic"].get("J4") == "FAIL" and item("C", "J4").get("sub") == "J4-2"),
              ("질문 요구 인용을 질문 원문에서 확인", item("C", "J4").get("question_quote_ok") is True),
              ("답변 인용을 답변 원문에서 확인", item("C", "J4").get("answer_quote_ok") is True),
              ("CODE 는 PASS, 원인은 의미 평가만", not r("C")["code_fails"] and r("C")["problem_sources"] == ["의미 평가"])],
        "C2": [("Mock 은 YES 라고 답함", item("C2", "J2").get("model_decision") == "YES"),
               ("없는 인용 — 재확인 3회", r("C2")["recheck_rounds"] == 3 and r("C2")["judge_calls"] == 4),
               ("확인 불가 NO 로 바뀌어 J2 FAIL", item("C2", "J2").get("unverifiable") is True and r("C2")["semantic"].get("J2") == "FAIL"),
               ("기준 사유 코드 ITEM_UNVERIFIABLE", (rows["C2"][1]["decisions"]["J2"].get("reason_code") == ITEM_UNVERIFIABLE))],
        "D": [("1턴에는 J6 적용 없음", "J6" not in r("D-T1")["criteria"]),
              ("2·3턴에 이전 턴 질문·답변 원문 전달", r("D-T2")["history_turns"] == [1] and r("D-T3")["history_turns"] == [1, 2]
               and t3_calls[0]["state"]["history"][0]["question"] == T1_Q),
              ("3턴 J6 FAIL — 1턴·2턴 인용을 이전 턴 원문에서 확인",
               r("D-T3")["semantic"].get("J6") == "FAIL" and all(f["question_quote_ok"] for f in r("D-T3")["failed_items"]["J6"])
               and sorted(f["turn"] for f in r("D-T3")["failed_items"]["J6"]) == [1, 2]),
              ("대화 1개 = 1문항, 한 턴 FAIL 이면 대화 FAIL", len(d_units) == 1 and len(d_units[0]) == 3
               and unit_outcome([outcomes[i]["outcome"] for i in d_units[0]]) == "FAIL")],
        "E": [("반박 출처를 주입 원문에서 열어 봄", URL_OK in judge.fetched),
              ("인용문이 원문에 있음 (quote_verified)", any(s["quote_verified"] for f in item("E", "J5").get("fact", []) for s in f["sources"])),
              ("J5 FAIL — 사실 오류 근거 확정", r("E")["semantic"].get("J5") == "FAIL" and not item("E", "J5").get("unverifiable"))],
        "E2": [("반박 출처 원문에 인용문 없음 — 재확인", r("E2")["recheck_rounds"] >= 1),
               ("출처 인용을 근거로 인정하지 않음 (확인 불가)", item("E2", "J5").get("unverifiable") is True
                and not any(s["quote_verified"] for f in item("E2", "J5").get("fact", []) for s in f["sources"])),
               ("J5 사유 코드 FACT_UNVERIFIABLE · 출처 quote_not_found — 사실 오류 확정이 아님",
                rows["E2"][1]["decisions"]["J5"].get("reason_code") == FACT_UNVERIFIABLE and any(
                    s.get("verification_status") == "quote_not_found" for c in rows["E2"][1]["decisions"]["J5"]["fact_check"]["claims"] for s in c["sources"]))],
        "F": [("평가기 오류로 기록 (TIMEOUT, 판정 없음)", r("F")["record_status"] == "TIMEOUT" and not r("F")["semantic"]),
              ("답변 결함 FAIL 로 세지 않음 — 미완료", r("F")["outcome"] == "PENDING" and "F" not in problems)],
    }
    singles = [c for s in SCENARIOS for c in s["cases"] if "conversation_id" not in c and not (s["scripts"][0].get("status"))]
    first_calls = {c["question"]: calls_by_q[c["question"]][0] for c in singles}
    request_text = json.dumps([{"state": c["state"], "questions": c["questions"]} for c in judge.calls], ensure_ascii=False).lower()
    records = jev["records"]
    common = [
        ("CODE 공통 검사 단위 28개 등록", sum(1 for unit in CODE_UNITS if not unit[0].startswith("AIONA")) == 28),
        ("세부 기준 30개 · 점검 항목 44개 등록 (설계안 v1.6, J7·J8 포함)", (len(SUBS), len(ITEMS)) == (30, 44)),
        ("의미 평가 방식 items 로 실행·기록", jev.get("pipeline") == "items" and all(rec.get("applicability", {}).get("version") == SUB_APPLICABILITY_VERSION for rec in records)),
        ("보낸 항목 = 코드 규칙(applicable_items)이 고른 항목", all(
            list(first_calls[c["question"]]["questions"]) == [it.id for it, _ in applicable_items({"question": c["question"], "answer": c["answer"]})]
            for c in singles)),
        ("요청에 평가 대상 시스템·모델 이름 없음 (블라인드)", all(set(c["state"]) <= STATE_KEYS for c in judge.calls) and "aiona" not in request_text),
        ("항목 → 세부 → 기준 판정 일치, NOT_APPLICABLE 은 판정·충족률 분모 제외", all(_consistent(rec) for rec in records if rec.get("status") == "SUCCESS")),
        ("기준별 PASS·FAIL 집계 = 레코드 판정 수", all(
            (jev["summary"][cid]["pass"], jev["summary"][cid]["fail"]) ==
            (sum(1 for rec in records if ((rec.get("decisions") or {}).get(cid) or {}).get("decision") == "PASS"),
             sum(1 for rec in records if ((rec.get("decisions") or {}).get(cid) or {}).get("decision") == "FAIL"))
            for cid in JUDGE_IDS if cid in jev["summary"])),
        ("CODE FAIL · 의미 FAIL 구분 집계 (결과 행·레코드에서 다시 센 값과 같음)", (combined["fail_code_only"], combined["fail_semantic_only"], combined["fail_both"]) == (
            sum(1 for row, _, _ in rows.values() if row["code_fails"] and "FAIL" not in row["semantic"].values()),
            sum(1 for row, _, _ in rows.values() if not row["code_fails"] and "FAIL" in row["semantic"].values()),
            sum(1 for row, _, _ in rows.values() if row["code_fails"] and "FAIL" in row["semantic"].values()))),
        ("저장 파일 = 실행 결과 (다시 읽어 일치)", restored == at_save),
        ("복원한 결과로 다시 집계해도 같음", combined_summary(restored) == restored["combined"] and question_outcomes(restored) == outcomes),
        ("복원한 결과로 기존 HTML 내보내기 동작", "<html" in render_report(restored)),
        ("네트워크·유료 API 호출 0회 (주입 원문만 사용)", set(judge.fetched) <= set(PAGES)),
    ]
    results = [{"id": s["id"], "checks": [{"name": n, "ok": bool(ok)} for n, ok in checks[s["id"]]]} for s in SCENARIOS]
    return {"version": DEMO_VERSION, "run_id": batch["run"]["run_id"], "pipeline": jev.get("pipeline"), "judge_model": MOCK_MODEL,
            "judge_calls": len(judge.calls), "fetched": sorted(set(judge.fetched)), "env_pipeline": resolve_semantic_pipeline(),
            "scenarios": scenario_out, "checks": results, "common_checks": [{"name": n, "ok": bool(ok)} for n, ok in common],
            "combined": {k: combined[k] for k in ("questions", "pass", "fail", "pending", "fail_code_only", "fail_semantic_only", "fail_both")},
            "units": {"questions": len(units), "turns": len(batch["cases"])}}


STATUS = [  # (점검 항목, 상태, 근거) — 상태: 구현됨 · 부분 구현 · 미구현 · Mock 검증됨 · 실호출 미검증
    ("CODE 28개 검사 적용", ["구현됨", "실제 실행"], "엔진 그대로 실행 — 이 보고서의 CODE 결과는 실제 엔진 값 (code_eval/engine.py)"),
    ("SEMANTIC_PIPELINE=items 항목별 평가", ["구현됨", "Mock 검증됨", "실호출 미검증"],
     "기본값은 legacy (jev_client.py:298) — 설계안 v1.3 '정할 것: 기본 실행 설정'(검증 뒤 items 기본 전환 여부). "
     "로컬 .env 에 SEMANTIC_PIPELINE 이 없어 웹 화면은 legacy 로 실행. 저장된 유료 실호출 기록에 items 실행 없음"),
    ("세부 기준 30개 · 점검 항목 44개 (v1.6, J7·J8 포함)", ["구현됨"], "semantic_items.py — import 때 기준표 데이터 검증"),
    ("적용 기준을 CODE 규칙으로 결정", ["구현됨", "Mock 검증됨"],
     "sub_applicability → applicable_items 가 보낼 항목을 정함. J1-3·J6 등 일부 항목은 보낸 뒤 평가 모델이 해당 여부를 정함"),
    ("평가 대상 모델 정보 블라인드", ["구현됨", "Mock 검증됨"],
     "요청 state 는 질문·답변·이력·reference·수집일만. 비교 실행은 blind_plan 순서·라벨 (webapp/blind_comparison.py). 실제 전송 본문은 기존 client 단위 테스트 범위"),
    ("항목 YES/NO · 질문·답변 인용 검증", ["구현됨", "Mock 검증됨", "실호출 미검증"], "semantic_quotes.verify_items · 재확인 ≤3회 뒤 확인 불가 FAIL"),
    ("CODE FAIL 이어도 의미 평가", ["구현됨", "Mock 검증됨"], "입력 오류(case_status≠EVALUATED)만 건너뜀 (jev_eval._prepare_batch)"),
    ("항목 → 세부 → J1~J8 → 질문 판정·집계", ["구현됨", "Mock 검증됨"], "semantic_items_eval · combined_summary · run_summary v2 (대화 1개 = 1문항, 308a717)"),
    ("평가 결과 저장·복원", ["구현됨", "Mock 검증됨"], "CodeEvalService._save → code-eval-<run_id>.json, 다시 읽어 재집계·HTML 내보내기"),
]
NOT_VERIFIED = ("Claude Opus 5.5 가 items 계약(항목별 YES/NO·인용)을 실제로 지키는지와 판정 품질 — 유료 실호출 필요",
                "items 방식의 J5 실제 웹검색·web_fetch 와 서버 원문 대조 (실제 출처 사이트)",
                "items 방식의 호출 비용·지연·프롬프트 캐시 효과",
                "실제 전송 client(anthropic_semantic_client)의 items 요청 본문 전체 — 이번 시연은 jev 요청 단계까지만 확인",
                "웹 화면에서 items 결과 표시 — 프론트엔드는 이번 범위 밖")


def render_html(demo: Dict[str, Any]) -> str:
    all_checks = [c for s in demo["checks"] for c in s["checks"]] + demo["common_checks"]
    passed = sum(c["ok"] for c in all_checks)
    tag = {"구현됨": "pass", "실제 실행": "pass", "Mock 검증됨": "mock", "부분 구현": "warn", "실호출 미검증": "warn", "미구현": "fail"}
    out = ['<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
           "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">",
           f"<title>항목별 의미 평가 Mock E2E</title><style>{_CSS}{_EXTRA_CSS}</style></head><body><main>",
           f'<header><h1>항목별 의미 평가 Mock E2E</h1><div class="meta">{_e(demo["version"])} · 실행 {_e(demo["run_id"])} · 의미 평가 방식 '
           f'<code>{_e(demo["pipeline"])}</code> · 평가 모델 <code>{_e(demo["judge_model"])}</code></div></header>',
           '<p class="free">Mock 검증 — CODE 결과와 서버 검증·집계·저장은 <b>실제 코드</b>가 낸 값입니다. 의미 평가의 YES/NO·인용·이유는 '
           '시나리오대로 답하는 <b>Mock Judge</b>의 값이며 Claude Opus 5.5 의 판정이 아닙니다. 유료 API 호출 0회.</p>',
           f'<section class="tiles"><div class="tile"><span>시연 검사</span><b>{passed}/{len(all_checks)}</b><span>실제 코드 결과로 확인</span></div>'
           f'<div class="tile"><span>질문 단위 · 턴</span><b>{demo["units"]["questions"]} · {demo["units"]["turns"]}</b><span>대화 1개 = 1문항</span></div>'
           f'<div class="tile"><span>질문 판정 PASS · FAIL · 미완료</span><b>{demo["combined"]["pass"]} · {demo["combined"]["fail"]} · {demo["combined"]["pending"]}</b><span>턴 기준</span></div>'
           f'<div class="tile"><span>Mock Judge 호출</span><b>{demo["judge_calls"]}</b><span>재확인 포함</span></div></section>']
    if demo["env_pipeline"] != "items":
        out.append(f'<div class="status partial">현재 설정의 의미 평가 방식은 <code>{_e(demo["env_pipeline"])}</code> 입니다 (v1.3: 검증 뒤 items 기본 전환 결정). '
                   '웹 화면에서 항목별 평가를 쓰려면 SEMANTIC_PIPELINE=items 설정이 필요합니다. 이 시연은 pipeline="items" 를 직접 넘깁니다.</div>')
    out.append('<section class="card"><h2>실제 2단계 평가 흐름</h2><ol class="flow">')
    for step, kind in (("질문·답변 입력 (단일·대화 턴)", "real"), ("1차 CODE 검사 28개 — PASS/FAIL, API 비용 없음", "real"),
                       ("적용 항목 선택 — 코드 규칙 (sub_applicability → applicable_items)", "real"),
                       ("의미 평가 요청 — 질문·답변·이전 턴 원문만 (모델 이름 없음)", "real"),
                       ("평가 모델의 항목별 YES/NO·인용·이유 — 이 시연에서는 Mock Judge", "mock"),
                       ("서버 검증 — 인용 원문 대조, J5 반박 출처 원문 대조, 실패 항목만 재확인 ≤3회", "real"),
                       ("집계 — 항목 → 세부 기준 → J1~J8 → 질문 판정 (CODE·의미 FAIL 구분)", "real"),
                       ("저장 — code-eval-<run_id>.json, 복원·재집계·HTML 내보내기", "real")):
        out.append(f'<li><span class="pill {"mock" if kind == "mock" else "pass"}">{"Mock" if kind == "mock" else "실제 코드"}</span> {_e(step)}</li>')
    out.append("</ol></section>")

    out.append('<section class="card"><h2>점검 항목별 구현 수준</h2><div class="wrap"><table><thead><tr><th>점검 항목</th><th>상태</th><th>근거</th></tr></thead><tbody>')
    for name, states, why in STATUS:
        pills = " ".join(f'<span class="pill {tag[s]}">{_e(s)}</span>' for s in states)
        out.append(f"<tr><td>{_e(name)}</td><td>{pills}</td><td>{_e(why)}</td></tr>")
    out.append("</tbody></table></div></section>")

    out.append('<section class="card"><h2>CODE와 J1~J8 결과 비교</h2><p class="note">CODE 는 실제 엔진, J1~J8 은 Mock Judge 응답을 서버가 검증·집계한 값. '
               '— 는 적용되지 않음.</p><div class="wrap"><table><thead><tr><th>CASE</th><th>CODE</th>'
               + "".join(f"<th>{c}</th>" for c in JUDGE_IDS) + "<th>질문 판정</th></tr></thead><tbody>")
    for scenario in demo["scenarios"]:
        for row in scenario["cases"]:
            code = ('<span class="pill fail">FAIL</span> ' + _e(", ".join(f["check_id"] for f in row["code_fails"]))) if row["code_fails"] \
                else '<span class="pill pass">PASS</span>'
            cells = []
            for cid in JUDGE_IDS:
                v = row["semantic"].get(cid) if cid in row["criteria"] or row["semantic"].get(cid) in ("PASS", "FAIL") else None
                cells.append(f'<td><span class="pill {"pass" if v == "PASS" else "fail" if v == "FAIL" else "na"}">{_e(v or "—")}</span></td>')
            outcome = row["outcome"]
            label = {"PASS": "PASS", "FAIL": "FAIL", "PENDING": "미완료"}.get(outcome, outcome)
            out.append(f'<tr><td><b>{_e(row["case_id"])}</b></td><td>{code}</td>{"".join(cells)}'
                       f'<td><span class="pill {"pass" if outcome == "PASS" else "fail" if outcome == "FAIL" else "warn"}">{_e(label)}</span></td></tr>')
    out.append("</tbody></table></div></section>")

    checks = {c["id"]: c["checks"] for c in demo["checks"]}
    for scenario in demo["scenarios"]:
        ok = all(c["ok"] for c in checks[scenario["id"]])
        out.append(f'<section class="card"><h2>CASE {_e(scenario["id"])} — {_e(scenario["title"])} '
                   f'<span class="pill {"pass" if ok else "fail"}">{"검사 통과" if ok else "검사 실패"}</span></h2><p class="note">{_e(scenario["goal"])}</p>')
        for row in scenario["cases"]:
            turn = f' ({_e(row["turn"])}턴)' if row["turn"] else ""
            out.append(f'<div class="qa"><div><b>질문{turn}</b><pre>{_e(row["question"])}</pre></div>'
                       f'<div><b>답변{turn}</b><pre>{_e(row["answer"])}</pre></div></div>')
            for f in row["code_fails"]:
                out.append(f'<p class="ev"><span class="pill pass">실제 CODE</span> <code>{_e(f["check_id"])}</code> FAIL — {_e(f["reason"])}</p>')
            for cid, items in row["failed_items"].items():
                for it in items:
                    quotes = []
                    if it["question_quote"]:
                        quotes.append(f'{"이전 " + str(it["turn"]) + "턴" if it["turn"] else "질문"} 인용 “{_e(it["question_quote"])}” '
                                      f'{"✓ 원문 확인" if it["question_quote_ok"] else "✗ 원문에 없음"}')
                    if it["answer_quote"]:
                        quotes.append(f'답변 인용 “{_e(it["answer_quote"])}” {"✓ 원문 확인" if it["answer_quote_ok"] else "✗ 원문에 없음"}')
                    for fact in it["fact"]:
                        for s in fact["sources"]:
                            quotes.append(f'반박 출처 {_e(s["url"])} 인용 {"✓ 원문 확인" if s["quote_verified"] else "✗ 원문에 없음"}')
                    extra = (f' · Mock 원래 답 {_e(it["model_decision"])} → 서버가 확인 불가 NO 로 바꿈 (재확인 {it["rechecks"]}회)' if it["unverifiable"] else "")
                    out.append(f'<p class="ev"><span class="pill mock">Mock 판정</span> {_e(cid)} › {_e(it["sub"])} › <code>{_e(it["id"])}</code> NO — '
                               f'{_e(it["reason"])}{extra}<br>{"<br>".join(quotes)}</p>')
            if row["record_status"] != "SUCCESS":
                out.append(f'<p class="ev"><span class="pill warn">평가기 오류</span> {_e(row["record_status"])}: {_e(row["record_error"])}</p>')
        out.append("<ul class=\"checks\">" + "".join(f'<li class="{"ok" if c["ok"] else "no"}">{"✓" if c["ok"] else "✗"} {_e(c["name"])}</li>'
                                                     for c in checks[scenario["id"]]) + "</ul></section>")

    out.append('<section class="cols"><div class="card"><h2>공통 검사</h2><ul class="checks">'
               + "".join(f'<li class="{"ok" if c["ok"] else "no"}">{"✓" if c["ok"] else "✗"} {_e(c["name"])}</li>' for c in demo["common_checks"])
               + '</ul></div><div class="card"><h2>아직 유료 실호출로 검증하지 못한 것</h2><ul>'
               + "".join(f"<li>{_e(x)}</li>" for x in NOT_VERIFIED) + "</ul></div></section>")
    out.append('<p class="meta">다시 만들기: <code>python tools/items_e2e_demo.py</code> · 테스트: <code>python -m pytest tests/test_items_e2e_demo.py</code></p>'
               "</main></body></html>")
    return "\n".join(out)


_EXTRA_CSS = """
.mock{color:var(--blue);background:var(--blue-soft)}.na{color:var(--muted);background:var(--head)}
.flow{margin:6px 0 0;padding-left:22px;display:grid;gap:4px}.qa{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px;margin-top:8px}
.qa>div{min-width:0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px 10px;
font:12.5px/1.6 var(--font);margin:4px 0 0;color:var(--ink)}.ev{margin:8px 0 0;font-size:13px}
.checks{list-style:none;padding-left:0}.checks .ok{color:var(--green)}.checks .no{color:var(--red);font-weight:700}
"""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"HTML 경로 (기본 {DEFAULT_OUT.relative_to(ROOT)})")
    parser.add_argument("--json", action="store_true", help="HTML 대신 결과 JSON 을 출력")
    args = parser.parse_args(argv)
    demo = run_demo()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    checks = [c for s in demo["checks"] for c in s["checks"]] + demo["common_checks"]
    failed = [c["name"] for c in checks if not c["ok"]]
    if args.json:
        print(json.dumps(demo, ensure_ascii=False, indent=1))
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(render_html(demo), encoding="utf-8")
        print(f"시연 검사 {len(checks) - len(failed)}/{len(checks)} 통과" + (f" — 실패: {', '.join(failed)}" if failed else "") + f"\n보고서: {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
