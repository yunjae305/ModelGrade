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
