# -*- coding: utf-8 -*-
"""판정 사유 점검(tools/judge_reason_audit.py) — 정의 밖 사유 · 같은 오류 두 번 · 가정 사유 · 1차↔최종 · 모델 간 갈림을 합성 결과로. 모델 호출 없음."""
from tools import judge_reason_audit as ra


def _line(p, q, items_by_turn):
    turns = {str(tn): {"items": {iid: dict(it) for iid, it in items.items()}} for tn, items in items_by_turn.items()}
    return {"provider": p, "question": q, "status": "judged", "turn_results": turns}


def test_misroute_double_assumption_flip_and_split():
    it = lambda d, reason="", quote="", first=None: {"decision": d, "reason": reason, "answer_quote": quote, "jev": first or d}
    lines = {
        ("aiona", 2): _line("aiona", 2, {2: {
            "J5-2.b": it("FAIL", "본문에 인용 번호 [1]·[2]를 붙이지 않았습니다."),                       # 인용 지시 → 정의 밖
            "J4-4.a": it("FAIL", "요약에 원문에 없는 문장을 덧붙였습니다.", "덧붙인 문장"),               # J5-2.b 몫 → 정의 밖
            "J1-4.b": it("FAIL", "표의 내용이 설명에서 반복됩니다.", "강변 새벽 라이딩 사전 모임 안내 첫 단락"),
            "J2-3.a": it("FAIL", "조건을 어겼습니다.", "강변 새벽 라이딩 사전 모임 안내 첫 단락"),                 # 같은 답 구절 → 두 번
            "J3-1.a": it("FAIL", "아마 다른 값일 것으로 보입니다."),                                  # 가정, 인용 없음
            "J6-2.a": it("PASS", first="FAIL"),                                                    # 1차 FAIL → 최종 PASS
        }}),
        ("gpt", 2): _line("gpt", 2, {2: {"J5-2.b": it("PASS"), "J6-2.a": it("PASS")}}),              # 모델 간 갈림(J5-2.b)
    }
    res = ra.audit(lines, top=5)
    owners = {m["cell"].split()[-1]: m["owner"] for m in res["misroute"]}
    assert owners["J5-2.b"].startswith("인용 지시") and owners["J4-4.a"] == "J5-2.b"
    assert any(d["items"] == "J1-4.b + J2-3.a" and d["why"] == "같은 답 구절" for d in res["double"])
    assert [a["cell"] for a in res["assumed"]] == ["Q2 aiona T2 J3-1.a"]
    assert res["flip"]["J6-2.a"] == {"1차 FAIL→최종 통과": 1}
    assert res["split"]["J5-2.b"] == 1 and res["fail_cells"] == 5
    md = ra.to_md(res, "시험")
    assert "정의 밖 사유" in md and "같은 오류를 두 항목에서" in md and "J6-2.a" in md


def test_own_cue_keeps_cell_out_of_misroute():
    it = {"decision": "FAIL", "reason": "원문에 없는 문장을 덧붙였고, 기록의 일부도 빠뜨렸습니다.", "answer_quote": "", "jev": "FAIL"}
    res = ra.audit({("gpt", 1): _line("gpt", 1, {1: {"J5-2.b": it}})})
    assert res["misroute"] == []   # 덧붙임이 주 사유 → J5-2.b 몫이 맞음


def test_na_rule_audit_flags_wrong_rule_use():
    """⑦ 해당 없음의 사유가 규칙 밖 — 대상 아닌 규칙 · 조건이 거짓인 규칙 · 제 판정을 PASS/FAIL 로 말함 (10-25 밤 AIONA Q6 7턴)."""
    from tools import judge_reason_audit as ra
    x = {"status": "judged", "turn_results": {"7": {"items": {
        "J1-1.a": {"decision": "NOT_APPLICABLE", "reason": ""},
        "J1-3.a": {"decision": "NOT_APPLICABLE", "reason": "답변이 이를 지적해 J1-3.a는 PASS입니다."},
        "J4-3.a": {"decision": "NOT_APPLICABLE", "reason": "J1-1.a가 FAIL인 경우 J4 항목은 해당 없음입니다."},
        "J6-3.b": {"decision": "NOT_APPLICABLE", "reason": "J1-3 상황이므로 수정 유지 항목은 해당하지 않습니다."},
        "J2-3.a": {"decision": "NOT_APPLICABLE", "reason": "J1-2.a가 FAIL이므로 해당 없음", "override": "거절"},
        "J6-1.a": {"decision": "PASS", "reason": "존댓말을 지켰습니다."}},
        "5": {"items": {"J1-1.a": {"decision": "FAIL", "how": "… → 에러 케이스 턴(CODE 1-1): 결과물 없음"},
                        "J2-3.a": {"decision": "NOT_APPLICABLE", "reason": "J1-2.a가 FAIL인 거절·회피 답변이므로 결과물 품질 항목은 판정하지 않습니다."},
                        "J1-2.a": {"decision": "NOT_APPLICABLE", "reason": "거절", "override": "에러 케이스 턴"}}}}}}
    res = ra.audit({("aiona", 6): x})   # 5턴(에러 케이스 턴)의 NA 사유는 점검하지 않음
    whys = {m["cell"].split()[-1]: m["why"] for m in res["na_rule"]}
    assert whys == {"J4-3.a": "규칙의 조건이 이 턴에서 거짓", "J6-3.b": "규칙의 대상 아님", "J1-3.a": "사유는 제 판정을 PASS/FAIL 로 말함"}
    md = ra.to_md(res, "t")
    assert "⑦ 규칙 밖 해당 없음(NA 사유) 3" in md and "J4-3.a" in md


def test_citation_number_cue_is_not_misroute_when_question_asks_for_numbers():
    """질문이 '출처 번호와 목록을 맞춰' 달라고 직접 말한 턴의 J2-3.a FAIL 은 정의 밖(인용 해석) 의심이 아니다 (v4-r8-fix AIONA Q8 9턴)."""
    from tools import judge_reason_audit as ra
    cell = {"decision": "FAIL", "reason": "본문에서 입장·접수 시각에 [1]을 붙이지 않았고 출처 목록에 [2]도 없습니다.", "answer_quote": "## 출처 - 공연장 점검 메모"}
    asks = {**cell, "question_quote": "자료의 [1]·[2] 출처 번호와 목록도 맞춰 주세요."}
    cites = {**cell, "question_quote": "기록 [1]·[2]를 인용해 정리해 주세요."}
    flagged = lambda c: [m["cell"] for m in ra.audit({("aiona", 8): {"status": "judged", "turn_results": {"9": {"items": {"J2-3.a": c}}}}})["misroute"]]
    assert flagged(asks) == [] and flagged(cites) == ["Q8 aiona T9 J2-3.a"]

