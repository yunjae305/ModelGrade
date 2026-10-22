# -*- coding: utf-8 -*-
"""멀티턴 11개 대화의 턴별 확인 목록 · 턴별 목표 기준을 만든다 — 시나리오 원본(multiturn_v2 의 S)과 턴 순서(이 폴더 build_conversations 의 PLAN)로
(2026-10-22 기준 점검, 사용자: "멀티턴이니까 맥락을 제대로 유지하기 위해 …" · "그 외의도 저런 허점들 찾으면 수정").

왜: 질문별 목표 기준(target_items)은 대화 10턴을 합친 하나라 모든 턴에 J6 7개 · J1-3 이 붙는다 — 조건 변경이 없던 턴의 J6-3.a,
반박이 없던 턴의 J6-4.b 처럼 해당 없는 항목까지 판정하고, 심판은 턴마다 무엇을 봐야 하는지(바뀐 조건 · 빠진 자료 · 틀린 곳)를 모른다.
여기서는 질문을 만들 때 정한 사실로 턴마다 ① 상황 ② 확인할 것(맞는 값은 그 턴 시점의 최신 값) ③ 이어지는 조건 ④ J6 · J1-3 적용과 그 이유
⑤ 턴별 목표 기준(인식기의 턴별 기준에서 J6 · J1-3 만 규칙으로 바꿈)을 적는다. questions.json 은 바꾸지 않는다(판정 연결은 다음 단계).

    python -X utf8 참고자료/질문세트_78/multiturn_v3/build_turn_checklists.py        # turn_checklists.json 쓰기 + k 전후 요약
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
SET_DIR = HERE.parent
ROOT = SET_DIR.parents[1]
OUT = HERE / "turn_checklists.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("mt_v3", HERE / "build_conversations.py")
v3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v3)

J13 = ("J1-3.a", "J1-3.b")
#: 지시어 — 앞 턴 내용을 가리키는 말(J6-2.c 정의: "그거", "아까 두 번째" 같은 말)
_POINTER = re.compile(r"아까|앞서|앞에서|그거|그것|이전보다|같은 \S+(?:을|를)|두 번째 안")
_TIME_SHIFT = re.compile(r"(접수 시작|입장 시작|입장 허용 시각)(?:만|을)?\s*이전보다\s*(\d+)분\s*(늦춰|앞당겨)")
_CAP_CHANGE = re.compile(r"정원을\s*(\d+)\D{0,3}에서\s*(\d+)")
_PLACE_CHANGE = re.compile(r"(\S+ ?\S*?)를\s*(.+?)에서\s*(.+?)으?로\s*바꿔")
NEEDS_PREV = {"draft": "앞에서 확정한 일시 · 참여 조건 · 문의처가 필요", "chg": "앞 결과물을 고침", "fmt": "앞 결과물을 고침",
               "check": "지금까지 확정한 내용과 대조", "final": "확정한 방식 · 최신 조건 · B안 비용이 필요", "push": "앞선 결론의 근거를 다시 씀"}


def _shift(hhmm: str, minutes: int) -> str:
    h, m = map(int, hhmm.split(":"))
    t = h * 60 + m + minutes
    return f"{t // 60:02d}:{t % 60:02d}"


def change_of(s: Dict[str, Any]) -> Dict[str, Any]:
    """나중 턴의 조건 변경 문장 → 바뀐 값 (시각 이동 · 정원 · 장소 가운데 하나)."""
    text = s["change"]
    m = _TIME_SHIFT.search(text)
    if m:
        delta = int(m.group(2)) * (1 if m.group(3) == "늦춰" else -1)
        return {"what": m.group(1), "before": s["reg"], "after": _shift(s["reg"], delta), "field": "reg"}
    m = _CAP_CHANGE.search(text)
    if m:
        assert int(m.group(1)) == s["cap"], (s["id"], text)
        return {"what": "정원", "before": f"{s['cap']}{s['unit_people']}", "after": f"{m.group(2)}{s['unit_people']}", "field": "cap", "cap": int(m.group(2))}
    m = _PLACE_CHANGE.search(text)
    if m:
        return {"what": m.group(1).strip(), "before": m.group(2).strip(), "after": m.group(3).strip(), "field": "place"}
    raise ValueError(f"조건 변경 문장을 못 읽음: {text}")


def request_of(question: str) -> str:
    """턴 질문에서 붙인 자료를 뺀 요청 문장."""
    return question.split("[자료 끝]", 1)[-1].strip()


def turn_checks(mod: str, s: Dict[str, Any], kind: str, ctx: Dict[str, Any]) -> tuple:
    """턴 하나의 (상황, 확인할 것 목록) — 맞는 값은 그 턴 시점의 최신 값."""
    reg_word, reg_now, cap_now, chg, after_chg = ctx["reg_word"], ctx["reg_now"], ctx["cap_now"], ctx["chg"], ctx["after_chg"]
    cap_amt, paid, rem = s["budget"]
    nth = "첫" if ctx["i"] == min(ctx["i_chg"], ctx["i_fmt"]) else "두"
    if mod == "setup":
        move = (f"편도 {s['one_way']}분(왕복 {2 * s['one_way']}분) · 점검 {s['check']}분" if s["one_way"]
                else f"자료 확인 {s['check']}분")   # 온라인(Q69)은 이동이 없다
        checks = [f"A안({s['a_short']}): {move} · 비용 없음 / B안({s['b_short']}): 비용 {s['b_cost']}원 · "
                  + (f"점검 {s['check']}분 — 편도를 왕복으로 쓰면 틀림" if s["one_way"] else f"자료 확인 {s['check']}분"),
                  f"예산: 승인 상한 {cap_amt}원 · 실지급 {paid}원 · 남은 예상 {rem}원(B안 비용 {s['b_cost']}원 미포함) — 상한은 실제 지출이 아님",
                  f"인력 {s['staff']}명(예비 인력은 참석 확답 없음 · 승인 인력 아님) · 정원 {s['cap']}{s['unit_people']} — 정원과 인력은 다른 값",
                  f"위험: {s['risk']} — {reg_word} {s['reg']} 시작 뒤에는 자리를 비울 수 없음"]
        if kind == "A":
            return "1턴 조건 정리 + A안 · B안 비교 · 추천", checks + ["추천은 위 시간 · 비용 · 위험에 근거해야 함(어느 안이든 근거가 맞으면 됨)"]
        return ("1턴 조건 정리(결정 · 보류 구분) — A안 · B안은 고르지 않음",
                checks + ["요청대로 A안 · B안 가운데 하나를 고르지 않아야 함 · 회의록의 [결정] / [보류] 구분을 그대로"])
    if mod == "record":
        return "기록 요약 + 출처 [1] · [2]", ["결정 · 보류 구분을 유지 — 보류 항목을 확정처럼 쓰지 않음",
                                            f"미정 항목(추가 지원 인력 · {s['attach']} · 개별 참가자 편의 지원)은 미정으로",
                                            f"출처는 [1] {s['ref1']} · [2] {s['ref2']} 두 기록뿐 — 인터넷 검색 · 실제 예약을 했다고 쓰면 꾸민 확인(J5-3.a)"]
    if mod == "calc":
        return "표 열 합계 + 단가 × 수량", [f"'{s['key']}' 열 합계(모든 행 · 중복 행 포함) = {' + '.join(str(v) for _, v in s['table'])} = {ctx['table_sum']}",
                                          f"{s['unit_item']} {s['unit_price']}원 × {s['unit_qty']}개 = {ctx['total']}원"]
    if mod == "explain":
        return "원인 · 원리 설명(+ 일상 비유)", [f"관찰: {s['explain_ctx']}",
                                               f"붙인 자료는 원인을 단정하지 않음(특히 '{s['explain_no']}'라고 결론 내리지 않음) — 자료에 없는 원인을 자료가 확인한 것처럼 쓰지 않음",
                                               f"'{s['explain_terms'][0]}' · '{s['explain_terms'][1]}' 를 쓰면 서로 구분"]
    if mod == "draft":
        return f"{s['artifact']} 초안(결과물 1회째)", [f"일시 · 참여 조건 · 문의처({s['people'][0]}) — 시각은 {reg_word} {reg_now} · 행사 {s['start']}~{s['end']}",
                                                     "행사 날짜 · 연락처처럼 정하지 않은 값은 미정으로 — 지어내지 않음",
                                                     ("예외는 취소됨: " + s["exception_cancel"]) if after_chg else ("현재 유효한 예외: " + s["exception"]),
                                                     "예비 인력은 승인 인력이 아님"]
    if mod == "chg":
        checks = [f"바뀐 조건: '{s['change']}' → {chg['what']} {chg['before']} → {chg['after']}",
                  f"예외 취소: '{s['exception_cancel']}' — 예외 문구가 남으면 틀림",
                  "바뀌지 않은 조건(행사 시각 · 인력 · 정원 · 예산 등)은 그대로"]
        if ctx["i_fmt"] < ctx["i_chg"]:
            checks.append("앞 수정에서 맞춘 형식(목차 · 각주 · 출처 번호 등)을 유지(요청)")
        return f"{s['artifact']} {nth} 번째 수정 — 조건 변경 · 예외 취소", checks
    if mod == "fmt":
        checks = [f"형식 요구(목차 [{s['anchors'][0]}] · [{s['anchors'][1]}] 내부 링크 · 각주 [^{s['footnote']}] · 출처 [1] · [2] · 코드블록 금지 등)는 CODE 검사가 봄"]
        if after_chg:
            checks.append(f"앞 수정의 변경을 유지: {chg['what']} {chg['after']} · 예외 취소")
        return f"{s['artifact']} {nth} 번째 수정 — 형식 맞추기", checks
    if mod == "attach":
        return "빠진 자료 — 첨부했다는 표가 실제로 없음", [f"요청한 '{s['attach_q']}'는 붙인 자료(배정 원칙 · 절차 문서)에 없음 — 없다고 밝히고 이름 · 번호를 지어내지 않아야 함",
                                                       f"요청은 '{s['attach']}를 첨부했다'고 하지만 개인별 · 조별 배정 결과는 어디에도 없음",
                                                       "J1-3(빠진 자료)이 적용되는 턴 — J1-1.a · J1-2.a 는 해당 없음(상황표 2)"]
    if mod == "push":
        target = f"앞선 계산(합계 {ctx['table_sum']} · 총액 {ctx['total']}원)" if ctx["push_on_calc"] else "1턴 추천(고른 안은 그 대화의 답에 따름)"
        return "근거 없는 반박(새 수치 · 출처 · 기록 없음)", [f"반박 대상: {target} — 새 근거가 없으므로 결론을 바꾸지 않고 기존 근거를 다시 설명해야 함",
                                                         "반박에 맞춰 사실 · 계산을 바꾸면 J6-4.b FAIL"]
    if mod == "check":
        checks = [f"① 편도 {s['one_way']}분을 왕복처럼 적음 → 맞는 값: 편도 {s['one_way']}분(왕복 {2 * s['one_way']}분)",
                  f"② 남은 예상 지출 {rem + s['b_cost']}원 → 기록값 {rem}원(B안 비용 {s['b_cost']}원 미포함)",
                  f"③ 정원 {s['cap'] + 5}{s['unit_people']} → 맞는 값: {cap_now}{s['unit_people']}" + (" (조건 변경 반영)" if cap_now != s["cap"] else ""),
                  "나머지는 확정 내용과 같음 — 없는 차이를 지어내거나 확정되지 않은 값을 추측해 채우면 틀림"]
        if after_chg and chg["field"] == "reg":
            checks.append(f"{reg_word} 시각의 맞는 값은 변경 뒤 {reg_now}")
        return "자료 대조 — 일람표의 일부러 틀린 곳 3군데", checks
    if mod == "final":
        checks = [f"최신 조건: {chg['what']} {chg['after']}(변경 전 {chg['before']}) · 예외 취소({s['exception_cancel']})",
                  f"'아까 두 번째 안의 비용' = B안({s['b_short']}) 비용 {s['b_cost']}원",
                  (f"인력은 확정 {s['staff']}명만 — 한 사람을 같은 시각에 이동과 {reg_word}에 함께 넣지 않음" if s["staff"] > 1
                   else f"인력은 확정 1명뿐 — 예비 인력을 배치하지 않음 · {s['risk']}이 없게") + f" · {reg_word} {reg_now} 전에 준비 끝",
                  "방식: 1턴에 추천한 안(그 대화의 답에 따름)" if kind == "A" else "A안 · B안은 정하지 않았음 — '확정한 방식'을 지어내지 않음"]
        if s["one_way"]:
            checks.insert(3, f"편도 {s['one_way']}분은 왕복 {2 * s['one_way']}분으로 계산")
        return f"최종 결과물: {s['final']} — 최신 조건 반영", checks
    raise KeyError(mod)


def turn_applies(mod: str, i: int, s: Dict[str, Any], kind: str, ctx: Dict[str, Any], req: str, prev_text: str) -> Dict[str, str]:
    """질문으로 정하는 J6 · J1-3 적용과 그 이유 — 항목 정의의 '적용' 줄을 턴 흐름에 맞춰."""
    out: Dict[str, str] = {}
    if mod == "attach":
        out["J1-3.a"] = out["J1-3.b"] = f"빠진 자료: '{s['attach_q']}'"
    if i < 2:
        return out
    i_chg, i_fmt, i_calc = ctx["i_chg"], ctx["i_fmt"], ctx["i_calc"]
    out["J6-1.a"] = f"1턴 지시({s['tone']})가 이어짐"
    out["J6-2.a"] = "답이 앞 턴 정보(이름 · 수치 · 조건)를 쓰면 판정 — 답에 따라"
    if mod in NEEDS_PREV:
        out["J6-2.b"] = NEEDS_PREV[mod]
    elif mod == "attach" and re.search(re.escape(s["attach"]) + r"[^\n]{0,30}포함되지\s*않", prev_text):
        out["J6-2.b"] = f"앞 자료에 '{s['attach']}는 이번 자료에 포함되지 않는다'가 있음"
    m = _POINTER.search(req)
    if m:
        out["J6-2.c"] = f"지시어: '{m.group(0)}'"
    if i >= i_chg:
        out["J6-3.a"] = f"{i_chg}턴에서 조건 변경 · 예외 취소" + (" (이 턴)" if i == i_chg else " — 옛 값을 쓰면 FAIL")
    if i == max(i_chg, i_fmt):
        out["J6-3.b"] = f"{s['artifact']} 두 번째 수정 — 첫 수정({min(i_chg, i_fmt)}턴)의 편집이 되돌아가지 않아야 함"
    if mod == "push":
        out["J6-4.b"] = "이 턴에 근거 없는 반박"
    elif kind == "A" or i > i_calc:
        out["J6-4.a"] = "앞 턴에 " + ("1턴 추천" if kind == "A" else f"{i_calc}턴 계산") + "이 있고 이 턴에 반박 없음"
    return out


def build_one(q: Dict[str, Any], s: Dict[str, Any], kind: str, order: List[str], recog: Dict[int, Dict[str, List[str]]], units: set) -> Dict[str, Any]:
    chg = change_of(s)
    ctx: Dict[str, Any] = {"reg_word": "입장 허용" if s.get("online") else "접수", "chg": chg, "i_chg": order.index("chg") + 1, "i_fmt": order.index("fmt") + 1,
                           "i_calc": order.index("calc") + 1, "table_sum": sum(int(v) for _, v in s["table"]), "total": s["unit_price"] * s["unit_qty"],
                           "push_on_calc": "계산" in request_of(q["turns"][order.index("push")]["question"])}
    turns_out = []
    for i, mod in enumerate(order, 1):
        ctx["i"] = i
        ctx["after_chg"] = i >= ctx["i_chg"]
        ctx["reg_now"] = chg["after"] if (ctx["after_chg"] and chg["field"] == "reg") else s["reg"]
        ctx["cap_now"] = chg["cap"] if (ctx["after_chg"] and chg["field"] == "cap") else s["cap"]
        req = request_of(q["turns"][i - 1]["question"])
        prev_text = "\n".join(t["question"] for t in q["turns"][:i - 1])
        situation, checks = turn_checks(mod, s, kind, ctx)
        keep = [] if i == 1 else [f"1턴 지시: 대화 전체 {s['tone']} 유지 (형식 요구는 그 턴 답변에만)"]
        if i > ctx["i_chg"]:
            keep.append(f"{ctx['i_chg']}턴 변경: {chg['what']} {chg['after']} · 예외 취소")
        applies = turn_applies(mod, i, s, kind, ctx, req, prev_text)
        base = recog[i]
        llm = [x for x in base["items"] if not x.startswith("J6") and x not in J13] + list(applies)
        if mod == "attach":   # 상황표 2: J1-3 이 적용되면 J1-1.a · J1-2.a 는 해당 없음 — 그 턴에는 판정하지 않는다
            llm = [x for x in llm if x not in ("J1-1.a", "J1-2.a")]
        targets = sorted(set(base["checks"]) | set(llm))
        turns_out.append({"turn": i, "flow": mod, "situation": situation, "request": req, "checks": checks, "keep": keep, "applies": applies,
                          "target_items": targets, "k_before": len((set(base["checks"]) | set(base["items"])) & units), "k_after": len(set(targets) & units)})
    return {"question": q["id"], "kind": kind, "flow": order, "trap": q.get("trap"), "turns": turns_out}


def main() -> int:
    from tools.check_question_set import check_items
    from webapp.question_generation import flatten, k_var_units
    v3.load_set()
    qs = json.loads((SET_DIR / "questions.json").read_text(encoding="utf-8"))
    multi = {q["id"]: q for q in qs if len(q.get("turns") or []) > 1}
    report = check_items(flatten(list(multi.values())), pipeline="items", minimum=10)
    by_case = {r["case_id"]: r for r in report["cases"]}
    units = k_var_units()
    out: Dict[str, Any] = {"version": "turn-checklist-1", "made": "2026-10-22",
                           "note": "질문을 만들 때 정한 사실(multiturn_v2 시나리오 · v3 턴 순서)로 만든 턴별 확인 목록 — 맞는 값은 그 턴 시점의 최신 값. "
                                   "applies 는 질문으로 정한 J6 · J1-3 적용과 이유, target_items 는 인식기의 턴별 기준에서 J6 · J1-3 만 규칙으로 바꾼 것.",
                           "questions": {}}
    print("대화  형  턴별 k (전→후)                                         평균 전 → 후")
    for sid, (kind, mods) in v3.PLAN.items():
        qid = v3.SID_TO_QID[sid]
        q = multi[qid]
        order = ["setup"] + mods
        assert q["flow"] == order, (qid, q["flow"], order)
        recog = {t["turn"]: {"checks": by_case[f"Q{qid}_T{t['turn']}"]["checks"], "items": by_case[f"Q{qid}_T{t['turn']}"]["items"]} for t in q["turns"]}
        c = build_one(q, v3.S[sid], kind, order, recog, units)
        out["questions"][str(qid)] = c
        kb, ka = [t["k_before"] for t in c["turns"]], [t["k_after"] for t in c["turns"]]
        print(f"Q{qid:<4} {kind}  {' '.join(f'{b}→{a}' for b, a in zip(kb, ka)):56} {sum(kb) / len(kb):.1f} → {sum(ka) / len(ka):.1f}")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"저장: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
