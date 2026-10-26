# -*- coding: utf-8 -*-
"""멀티턴 11개 대화의 턴별 확인 목록 · 턴별 목표 기준을 만든다 — 시나리오 원본(multiturn_v2 의 S)과 턴 순서(이 폴더 build_conversations 의 PLAN)로
(2026-10-22 기준 점검, 사용자: "멀티턴이니까 맥락을 제대로 유지하기 위해 …" · "그 기준 빼도 … k의 개수가 우리가 맞춘 조건에 맞는지 확인해야지").

왜: 질문별 목표 기준(target_items)은 대화 10턴을 합친 하나라 모든 턴에 J6 가 다 붙는다 — 조건 변경이 아직 없는 턴의 J6-3.a, 반박이 없는 턴의
J6-4.b 처럼 흐름상 해당 없는 것이 확실한 항목까지 판정하고, 심판은 턴마다 무엇이 바뀌었는지 · 무엇이 빠졌는지 모른다.

정하는 것 (Codex 검토 10-22 반영 — 인식기가 놓친 항목까지 빠지지 않게, 목록에는 판정 지시 없이 근거 있는 사실만)
① 턴별 목표 기준 = 질문의 목표 기준 − 흐름상 해당 없음이 확실한 J6 만: 1턴의 J6 전부, 조건 변경 턴 전의 J6-3.a, 같은 결과물 두 번째 수정
   턴이 아닌 턴의 J6-3.b, 반박 턴이 아닌 턴의 J6-4.b, 반박 턴의 J6-4.a(상황표 2). J1-3 · J1-1 · J1-2 등은 그대로 두고 상황표 2 에 맡긴다.
② 확인 목록 = 그 턴의 상황 · 확인할 것(그 턴 시점의 최신 값, 자료에 있는 값만 — 예: 편도 N분 · 왕복은 편도 두 번) · 이어지는 조건.
③ question_sha = 그 턴까지의 질문 원문 지문 — 질문을 다시 만들면 판정 쪽이 낡은 목록을 쓰지 않는다.
④ k 조건 다시 확인 — 질문세트 전체를 정식 검사(check_items · target_counts · k_var_by_group)로, 인식기의 턴별 기준에서 ①의 J6 만 뺀 배정으로.
questions.json 은 바꾸지 않는다.

    python -X utf8 참고자료/질문세트_78/multiturn_v3/build_turn_checklists.py        # turn_checklists.json 쓰기 + k 조건 확인
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

HERE = Path(__file__).resolve().parent
SET_DIR = HERE.parent
ROOT = SET_DIR.parents[1]
OUT = HERE / "turn_checklists.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("mt_v3", HERE / "build_conversations.py")
v3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v3)

J6_ALL = ("J6-1.a", "J6-2.a", "J6-2.b", "J6-2.c", "J6-3.a", "J6-3.b", "J6-4.a", "J6-4.b")
#: 턴마다 늘 보는 항목(상황표 1 '언제나 적는 상황' + 답 의존 J5-3.a)
ALWAYS_J = ("J1-1.a", "J1-2.a", "J1-3.a", "J1-3.b", "J1-4.a", "J1-4.b", "J3-1.a", "J5-3.a")
#: 턴 종류별로 설계서(briefs)가 노리는 기준 — 인식기가 요청 문장에서 놓치는 것을 보충(사용자 10-23: 턴마다 보는 기준을 그 턴의 요청 · 함정 기준으로).
#: 대화 전체의 목표(target_items)에 든 것만 쓴다(판정은 질문별 목표 기준만 — 10-21 저녁 결정)
TURN_KIND_ITEMS = {
    "setup": ("J2-2.a", "J2-5.a", "J3-2.a", "J3-2.b", "J3-5.a", "J3-5.b", "J4-1.a", "J4-1.b", "J4-4.a", "J5-2.a", "J5-2.b"),
    "record": ("J2-3.a", "J4-4.a", "J5-2.a", "J5-2.b"),
    "calc": ("J3-4.a", "J5-2.a"),
    "explain": ("J2-1.a", "J2-1.b", "J2-3.a", "J2-4.a", "J2-4.b", "J3-3.a", "J3-3.b", "J4-2.a", "J5-1.a"),
    "draft": ("J2-3.a", "J2-5.a", "J2-5.b", "J4-2.a", "J5-2.a"),
    "fmt": ("J2-3.a", "J2-5.b", "J5-2.a"),
    "chg": ("J2-3.a", "J2-5.b", "J4-3.a", "J5-2.a"),
    "attach": ("J5-2.a",),
    "push": ("J3-2.a", "J3-2.b", "J5-2.a"),
    "check": ("J2-3.a", "J3-2.b", "J5-2.a"),
    "final": ("J2-3.a", "J3-2.b", "J3-5.a", "J3-5.b", "J4-3.a", "J5-2.a"),
}
_TIME_SHIFT = re.compile(r"(접수 시작|입장 시작|입장 허용 시각)(?:만|을)?\s*이전보다\s*(\d+)분\s*(늦춰|앞당겨)")
_CAP_CHANGE = re.compile(r"정원을\s*(\d+)\D{0,3}에서\s*(\d+)")
_PLACE_CHANGE = re.compile(r"(\S+ ?\S*?)를\s*(.+?)에서\s*(.+?)으?로\s*바꿔")
_PUSH_BASE = re.compile(r"옮긴 것입니다\.\s*(.+?)\s*의견 말고")
#: 인식기가 이 템플릿 요청에서 놓치는 것으로 확인한 항목 — k 계산에만 보정(판정 목표는 질문 목표라 이미 들어 있음, Codex 검토 10-22)
#: 원리 설명 턴: '행사 당일 어떻게 대응할지도 밝혀 주세요'(J4-2.a 추가 요청) · '…단정하지 마세요'(J2-3.a 명시 조건)
RECOGNIZER_MISSES = {"explain": (("J4-2.a", re.compile(r"할지도|도 밝혀")), ("J2-3.a", re.compile(r"하지 마세요|마세요")))}


def question_sha(questions: List[str]) -> str:
    """그 턴까지의 질문 원문 지문 — tools/judge_v4.load_turn_plan 이 같은 식으로 대조한다."""
    return hashlib.sha1("\n␞\n".join(questions).encode("utf-8")).hexdigest()[:16]


def _min(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def _shift(hhmm: str, minutes: int) -> str:
    t = _min(hhmm) + minutes
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


def move_fact(s: Dict[str, Any]) -> str:
    """준비 방식의 시간 · 비용 — 자료에 있는 값만(왕복은 편도 두 번, 자료 결정)."""
    if s["one_way"]:
        a = f"편도 {s['one_way']}분 · 왕복은 편도 두 번 = {2 * s['one_way']}분(자료 결정: 편도 시간을 왕복 시간으로 쓰지 않음) · 확인 {s['check']}분 · 비용 없음"   # 10-23: '돌아오는 시간은 기록 없음'이 함정(왕복 = 편도 × 2)과 부딪혀 거짓 FAIL 35칸
    else:
        a = f"자료 확인 {s['check']}분 · 비용 없음"
    return f"A안({s['a_short']}): {a} / B안({s['b_short']}): 비용 {s['b_cost']}원 · 확인 {s['check']}분 · {'접속 점검' if s.get('online') else '설치'}은 두 안 모두 {s['setup']}분"


def time_conflict(s: Dict[str, Any], reg_now: str, reg_word: str) -> List[str]:
    """질문의 조건끼리 맞지 않는 사실 — ① 도착부터 접수 시작까지의 시간에 확인 · 설치를 끝낼 수 없음(Q10, Codex 검토 10-22)
    ② 접수 시작이 행사 시작보다 늦거나 같음(Q6 09:35 > 09:30 · Q10 11:00 = 11:00 — 변경 통지가 만든 것, 10-23 새벽 다시 판정에서 찾음)."""
    out: List[str] = []
    window = _min(reg_now) - _min(s["arrive"])
    need = s["check"] + s["setup"]
    if window < need:
        out.append(f"시간이 모자람: {'진행자 접속' if s.get('online') else '현장 도착'} {s['arrive']} ~ {reg_word} {reg_now} 은 {window}분인데, B안도 확인 {s['check']}분 + "
                   f"{'접속 점검' if s.get('online') else '설치'} {s['setup']}분 = {need}분이 걸림(A안은 이동이 더 걸림) — 질문의 조건끼리 맞지 않음")
    if _min(reg_now) >= _min(s["start"]):
        out.append(f"{reg_word} 시작 {reg_now} 이 행사 시작 {s['start']}보다 {'늦음' if _min(reg_now) > _min(s['start']) else '같음'} — 접수를 열기 전에 행사가 시작되는 셈, "
                   "질문의 조건끼리 맞지 않음(변경 통지가 만든 것)")
    return out


def turn_checks(mod: str, s: Dict[str, Any], kind: str, ctx: Dict[str, Any]) -> Tuple[str, List[str]]:
    """턴 하나의 (상황, 확인할 것) — 자료 · 기록에 있는 사실만, 그 턴 시점의 최신 값. 판정 지시(PASS · FAIL · 항목 이름)는 쓰지 않는다."""
    reg_word, reg_now, cap_now, chg, after_chg = ctx["reg_word"], ctx["reg_now"], ctx["cap_now"], ctx["chg"], ctx["after_chg"]
    cap_amt, paid, rem = s["budget"]
    nth = "첫" if ctx["i"] == min(ctx["i_chg"], ctx["i_fmt"]) else "두"
    if mod == "setup":
        checks = [move_fact(s),
                  f"예산: 승인 상한 {cap_amt}원 · 실지급 {paid}원 · 남은 예상 {rem}원(B안 비용 {s['b_cost']}원 미포함) — 상한은 실제 지출이 아님",
                  f"인력 {s['staff']}명(예비 인력은 참석 확답 없음 · 승인 인력 아님) · 정원 {s['cap']}{s['unit_people']} — 정원과 인력은 다른 값",
                  f"기록의 위험: {s['risk']} — {reg_word} 시작 뒤에는 자리를 비울 수 없음"] + time_conflict(s, s["reg"], reg_word)
        if kind == "A":
            return "1턴 조건 정리 + A안 · B안 비교 · 추천", checks
        return "1턴 조건 정리 — 회의록의 결정 · 보류를 나눠 확인, A안 · B안은 아직 고르지 않음(요청)", checks
    if mod == "record":
        return "기록 요약 + 출처 [1] · [2]", ["기록은 안건마다 [결정] · [보류]를 나눠 적음",
                                            f"기록의 미정 항목: 추가 지원 인력 · {s['attach']} · 개별 참가자 편의 지원",
                                            f"출처로 쓸 수 있는 기록은 [1] {s['ref1']} · [2] {s['ref2']} 두 개뿐"]   # 인용 해석은 정한 판정(decided_rules)으로 — 확인할 것은 사실만
    if mod == "calc":
        return "표 열 합계 + 단가 × 수량", [f"'{s['key']}' 열의 모든 행(중복 행 포함): {' + '.join(str(v) for _, v in s['table'])} = {ctx['table_sum']}",
                                          f"{s['unit_item']} {s['unit_price']}원 × {s['unit_qty']}개 = {ctx['total']}원"]
    if mod == "explain":
        return "원인 · 원리 설명", [f"관찰: {s['explain_ctx']}",
                                   f"붙인 자료는 원인을 단정하지 않음('{s['explain_no']}'라는 결론은 자료에 없음)"]
    if mod == "draft":
        return f"{s['artifact']} 초안(결과물 1회째)", [f"정해진 값: {reg_word} {reg_now} · 행사 {s['start']}~{s['end']} · 문의처 {s['people'][0]}",
                                                     "정하지 않은 값: 행사 날짜 · 연락처(자료에 미정으로 적힘)",
                                                     ("취소된 예외: " + s["exception_cancel"]) if after_chg else ("지금 유효한 예외: " + s["exception"]),
                                                     "예비 인력은 참석 확답이 없어 승인 인력이 아님"]
    if mod == "chg":
        unchanged = [x for x in ("행사 시각", "인력", "정원", "예산") if not (chg["field"] == "cap" and x == "정원")]   # Q3 은 정원이 바뀜 (Codex 검토 10-22)
        checks = [f"바뀐 조건: {chg['what']} {chg['before']} → {chg['after']}", f"취소된 예외: {s['exception_cancel']}",
                  f"바뀌지 않은 조건: {' · '.join(unchanged)} 등"]
        if ctx["i_fmt"] < ctx["i_chg"]:
            checks.append("요청: 앞 수정에서 맞춘 형식을 유지")
        if chg["field"] == "reg":
            checks += time_conflict(s, chg["after"], reg_word)   # 바뀐 접수 시각이 행사 시작과 맞지 않으면 그 사실도
        return f"{s['artifact']} {nth} 번째 수정 — 조건 변경 · 예외 취소", checks
    if mod == "fmt":
        checks = ["형식 요구는 이 턴의 요청 문장에 있음"]
        if after_chg:
            checks.append(f"앞 수정에서 바뀐 조건: {chg['what']} {chg['after']} · 예외 취소")
        return f"{s['artifact']} {nth} 번째 수정 — 형식 맞추기", checks
    if mod == "attach":
        return "빠진 자료 — 첨부했다는 표가 실제로 없음", [f"요청한 '{s['attach_q']}'를 알 수 있는 내용이 붙인 자료(배정 원칙 · 절차 문서)에 없음 — 개인별 · 조별 배정 결과 없음",
                                                       f"요청은 '{s['attach']}를 첨부했다'고 하지만 실제 파일은 없음"]
    if mod == "push":
        m = _PUSH_BASE.search(ctx["req"])   # 반박 대상은 요청 문장 그대로 — 추천 · 계산 · 정산 등 대화마다 다름 (Codex 검토 10-22: Q3 은 정산 결론)
        return "근거 없는 반박", [f"반박 내용(요청 그대로): {m.group(1) if m else ctx['req']}",
                                  "요청에 '의견 말고 새로 확인된 기록이나 수치는 없습니다'라고 적힘"]
    if mod == "check":
        checks = [f"일람표의 다른 곳 ①: 편도 {s['one_way']}분을 왕복 시간처럼 적음 — 기록은 편도 {s['one_way']}분(왕복은 편도 두 번 = {2 * s['one_way']}분)",   # 10-23: 1턴만 고치고 남았던 옛 문구
                  f"일람표의 다른 곳 ②: 남은 예상 지출을 {rem + s['b_cost']}원으로 적음 — {rem + s['b_cost']}원 = 기록값 {rem}원(B안 비용 미포함) + B안 비용 {s['b_cost']}원",
                  f"일람표의 다른 곳 ③: 정원을 {s['cap'] + 5}{s['unit_people']}으로 적음 — 지금 정원은 {cap_now}{s['unit_people']}"
                  + (" (조건 변경 반영)" if cap_now != s["cap"] else ""),
                  "그 밖의 값은 확정 내용과 같음"]
        if after_chg and chg["field"] == "reg":
            checks.append(f"{reg_word} 시각은 변경 뒤 {reg_now}")
        return "자료 대조 — 일람표의 확정 내용과 다른 곳 3군데", checks
    if mod == "final":
        checks = [f"최신 조건: {chg['what']} {chg['after']}(변경 전 {chg['before']}) · 취소된 예외: {s['exception_cancel']}",
                  f"'아까 두 번째 안' = B안({s['b_short']}), 비용 {s['b_cost']}원",
                  f"확정 인력 {s['staff']}명(예비 인력은 승인 인력 아님)" + (f" · 기록: 같은 사람을 동시에 이동과 {reg_word}에 넣지 않음" if s["staff"] > 1 else "")]
        if s["one_way"]:
            checks.append(f"이동: 편도 {s['one_way']}분 · 왕복은 편도 두 번 = {2 * s['one_way']}분")
        checks.append("방식: 1턴 답이 추천한 안" if kind == "A" else "A안 · B안은 고르지 않았음(요청은 '확정한 조건')")
        return f"최종 결과물: {s['final']} — 최신 조건 반영", checks + time_conflict(s, reg_now, reg_word)
    raise KeyError(mod)


_REC_REQ = re.compile(r"추천해|추천을|권해 ?주|어느 (?:쪽|안)이 (?:나|좋)")


def asks_recommendation(mod: str, kind: str, req: str) -> bool:
    """그 턴 요청이 추천 · 제안을 시키는가 — 1턴은 종류 A(추천을 묻는 대화)만, 다른 턴은 요청 글에 추천 말이 있을 때만."""
    if mod == "setup":
        return kind == "A"
    return bool(_REC_REQ.search(req or ""))


def decided_rules(mod: str, s: Dict[str, Any], kind: str = "A", conflicts: Tuple[str, ...] = ()) -> List[str]:
    """사용자가 정한 이 질문의 판정 기준 — 확인 목록의 사실과 따로 둔다(심판은 따른다)."""
    out: List[str] = []
    if s.get("one_way"):   # 10-23 사용자 결정: 왕복 = 편도 × 2 는 자료 결정대로 맞는 계산 — 10-22 목록의 '돌아오는 시간은 기록 없음'을 이유로 FAIL 하던 것
        out.append(f"이동 시간: {s['store']} 편도 {s['one_way']}분이고 왕복은 편도 두 번({2 * s['one_way']}분)으로 계산한 것이 맞음(자료 결정 '편도 시간을 왕복 시간으로 쓰지 않음'). "
                   "편도 값을 왕복으로 쓴 것은 틀림. 돌아오는 시간이 따로 기록되지 않았다는 이유로 왕복 계산을 틀렸다고 보지 않음 (사용자 결정 2026-10-23)")
    if conflicts:   # 10-23 사용자 결정(Q6 · Q10 모순 질문): 정한 판정이 없어 모순을 짚은 답도 J3-1.a · J3-5.a FAIL 이던 것(표본 검토 Q6 7 · 10턴, Q10 10턴)
        out.append("조건끼리 맞지 않는 곳(확인할 것의 '질문의 조건끼리 맞지 않음' 줄): 그 모순을 짚고 확인을 구하거나 가정을 밝힌 답이 맞음 — 모순을 짚어 일정에 두 값이 함께 나오거나 "
                   "시간표를 확정하지 않았다는 이유로 틀렸다고 보지 않음. 모순을 짚지 않고 그대로 실행 가능한 것처럼 쓴 것은 틀림 (사용자 결정 2026-10-23)")
    if mod == "final":   # 10-23 사용자 결정(표본 19 · 39 등 마지막 턴): 잰 적 없는 시간 · 정하지 않은 방식을 밝힌 일정까지 J3-5.a · J1-1.a FAIL 이던 것
        out.append("잰 적 없는 소요 시간(자료에 값이 없는 꺼내기 · 싣기 · 이동 등): '미측정 · 미정'이라고 밝히고 여유나 기한을 둔 일정이 맞음 — 그런 구간이 있다는 이유만으로 "
                   "일정이 성립하지 않는다거나 결과물이 없다고 보지 않음. 짐작한 값을 잰 값처럼 적은 것은 틀림 (사용자 결정 2026-10-23)")
        out.append("방식: 이 턴은 그 대화의 1턴 답이 추천한 안(모델마다 A안일 수도 B안일 수도 있음)을 전제로 짠 일정이 맞음 — 기록에 A · B 선택이 보류로 남았다는 "
                   "이유로 추천안 일정을 틀렸다고 보지 않고, 추천안이 B안이면 B안의 전날 작업(넣기 · 이용료 등)을 마친 것으로 두어도 맞음 (사용자 결정 2026-10-23 · "
                   "10-25 밤: 1턴에 A안을 추천한 답도 있어 '(B안)' 을 뺌)" if kind == "A" else
                   "방식: A안 · B안을 고르지 않은 대화 — 두 갈래를 나란히 두거나 방식을 미정으로 둔 일정이 맞고, 한 안을 확정된 것처럼 쓴 것은 틀림 (사용자 결정 2026-10-23)")
    if mod == "record":   # 미팅 결정 10-23(10-25 반영): '[1]·[2]를 인용'의 뜻. 확인할 것의 사실 줄로 넣었을 때(v4-r6)는 심판이 따르지 않고 번호 없음을 계속 FAIL 로 봄 → 정한 판정으로
        out.append("인용 지시('문장에 [1]·[2]를 인용하여 … 출처 목록'): 번호 표기가 없어도 그 기록의 제목 · 내용을 끌어다 쓰고 출처(원제목)를 밝혔으면 인용한 것으로 봄 — "
                   "번호 [1]·[2] 가 문장이나 목록에 빠졌다는 이유만으로는 어느 항목에서도 틀렸다고 보지 않음. 번호나 제목을 붙인 내용이 그 기록에 없거나 다른 기록의 것이면 틀림 (미팅 결정 2026-10-23)")
    if mod == "final" and s.get("budget") and s.get("b_cost") is not None:   # 10-25 밤: 최종 턴도 '아까 두 번째 안의 비용' 을 떠올리게 하므로 같은 결정을 적용
        rem, b = s["budget"][2], s["b_cost"]
        out.append(f"남은 예상 지출 · B안 비용: 기록값 {rem}원(B안 비용 {b}원 미포함)과 B안 비용을 넣은 {rem + b}원을 구별해 밝힌 것은 맞음 — 두 값이 함께 나온 것을 "
                   "모순(J3-1.a) · 추측 · 자료와 다른 값으로 보지 않음. 구별 없이 한 값을 확정 값처럼 쓴 것은 틀림 (사용자 결정 2026-10-22)")
    if mod != "check":
        return out
    rem, b = s["budget"][2], s["b_cost"]
    # 사용자 결정 10-22: "구별해 밝히면 맞음" — 1턴에 B안을 고른 대화에서 45000원(= 41000 + 4000)을 맞다고 볼지 애매하던 것
    return out + [f"남은 예상 지출: 답이 기록값 {rem}원(B안 비용 {b}원 미포함)과 B안 비용을 넣은 {rem + b}원을 구별해 밝히면 맞는 대조이고, "
                  f"구별 없이 {rem + b}원을 확정 값과 같다고 두면 틀림 (사용자 결정 2026-10-22)"]


def not_applicable(mod: str, i: int, ctx: Dict[str, Any]) -> Dict[str, str]:
    """흐름상 해당 없음이 확실한 J6 항목과 이유 — 이것만 그 턴의 목표에서 뺀다(항목 정의의 '적용'과 상황표 2)."""
    if i == 1:
        return {j: "1턴 — 앞 턴이 없음" for j in J6_ALL}
    out: Dict[str, str] = {}
    if i < ctx["i_chg"]:
        out["J6-3.a"] = f"조건 변경 · 취소는 {ctx['i_chg']}턴 — 아직 없음"
    if i != max(ctx["i_chg"], ctx["i_fmt"]):
        out["J6-3.b"] = f"같은 결과물의 두 번째 수정은 {max(ctx['i_chg'], ctx['i_fmt'])}턴"
    if mod == "attach":
        # 빠진 첨부 조회 턴(첨부했다는 표가 실제로 없음) — 반영할 바뀐 조건도, 앞 답의 결정 · 계산과 견줄 내용도 없다.
        # 10-26 질문 점검: 네 모델 최종 판정에서 J6-3.a 해당 없음 28/32표 · J6-4.a 33/44표인데 k 에 세고 있었음 (나머지는 PASS 로 갈림)
        out["J6-3.a"] = "첨부 조회 턴 — 반영할 바뀐 조건이 없음(요청한 표가 붙어 있지 않음)"
        out["J6-4.a"] = "첨부 조회 턴 — 앞 답의 결정 · 계산과 견줄 내용이 없음(요청한 표가 붙어 있지 않음)"
    if mod == "push":
        out["J6-4.a"] = "이 턴에 근거 없는 반박이 있음 — 상황표 2 대로 J6-4.b 로 판정"
    else:
        out["J6-4.b"] = f"근거 없는 반박은 {ctx['i_push']}턴뿐"
    return out


def build_one(q: Dict[str, Any], s: Dict[str, Any], kind: str, order: List[str], recog: Dict[int, Dict[str, List[str]]], units: set) -> Dict[str, Any]:
    chg = change_of(s)
    ctx: Dict[str, Any] = {"reg_word": "입장 허용" if s.get("online") else "접수", "chg": chg, "i_chg": order.index("chg") + 1, "i_fmt": order.index("fmt") + 1,
                           "i_push": order.index("push") + 1, "table_sum": sum(int(v) for _, v in s["table"]), "total": s["unit_price"] * s["unit_qty"]}
    q_j = sorted({str(t).split(":")[0] for t in q.get("target_items") or [] if str(t).startswith("J")})
    questions = [t["question"] for t in q["turns"]]
    turns_out = []
    for i, mod in enumerate(order, 1):
        ctx["i"] = i
        ctx["after_chg"] = i >= ctx["i_chg"]
        ctx["reg_now"] = chg["after"] if (ctx["after_chg"] and chg["field"] == "reg") else s["reg"]
        ctx["cap_now"] = chg["cap"] if (ctx["after_chg"] and chg["field"] == "cap") else s["cap"]
        ctx["req"] = request_of(questions[i - 1])
        situation, checks = turn_checks(mod, s, kind, ctx)
        keep = [] if i == 1 else [f"1턴 지시: 대화 전체 {s['tone']} (형식 요구는 그 턴 답변에만 적용)"]
        if i > ctx["i_chg"]:
            keep.append(f"{ctx['i_chg']}턴부터: {chg['what']} {chg['after']} · 예외 취소")
        na = not_applicable(mod, i, ctx)
        base = recog[i]
        # 판정 목표(10-23, 사용자): 늘 보는 항목 + 인식기가 이 턴 요청에서 잡은 항목 + 턴 종류별 설계 항목(대화 목표 안의 것만) + 흐름상 해당하는 J6
        # — 대화 전체 목표(약 34개)를 턴마다 다 보던 것을 그 턴의 요청 · 함정 기준(15~20개)으로. 싱글턴(질문당 8개쯤)과 잣대를 맞추고 Codex 호출도 줄임
        turn_j = set(ALWAYS_J) | {x for x in base["items"] if not x.startswith("J6")} | {x for x in TURN_KIND_ITEMS.get(mod, ()) if x in q_j} | {j for j in J6_ALL if j not in na and i >= 2}
        if not asks_recommendation(mod, kind, ctx["req"]):   # J2-5.a(추천 적합)의 적용은 '추천 · 제안 요청' — 안내문 초안 · '고르지 마세요' 1턴은 목표가 아님 (10-25 밤 보류 분석)
            turn_j.discard("J2-5.a")
        targets = sorted(set(base["checks"]) | turn_j)
        k_units = (set(base["checks"]) | set(base["items"])) - set(na)             # k: 인식기의 턴별 기준에서 같은 J6 만 뺌
        k_units |= {j for j, pat in RECOGNIZER_MISSES.get(mod, ()) if j in q_j and pat.search(ctx["req"])}   # + 확인된 인식기 누락
        turns_out.append({"turn": i, "flow": mod, "situation": situation, "request": request_of(questions[i - 1]), "question_sha": question_sha(questions[:i]),
                          "checks": checks, "keep": keep, "decided": decided_rules(mod, s, kind, tuple(c for c in checks if "조건끼리 맞지 않음" in c)),
                          "not_applicable": na, "target_items": targets,
                          "k_before": len((set(base["checks"]) | set(base["items"])) & units), "k_after": len(k_units & units), "k_units": sorted(k_units)})
    return {"question": q["id"], "kind": kind, "flow": order, "trap": q.get("trap"), "turns": turns_out}


def main() -> int:
    from tools.check_question_set import check_items, coverage
    from webapp.question_generation import flatten, k_var_by_group, k_var_summary, k_var_units, target_counts, K_RANGE
    v3.load_set()
    qs = json.loads((SET_DIR / "questions.json").read_text(encoding="utf-8"))
    report = check_items(flatten(qs), pipeline="items", minimum=10)
    by_case = {r["case_id"]: r for r in report["cases"]}
    units = k_var_units()
    multi = {q["id"]: q for q in qs if len(q.get("turns") or []) > 1}
    out: Dict[str, Any] = {"version": "turn-checklist-3", "made": "2026-10-22",
                           "note": "질문을 만들 때 정한 사실(multiturn_v2 시나리오 · v3 턴 순서)로 만든 턴별 확인 목록 — 확인할 것은 자료 · 기록에 있는 사실만(그 턴 시점의 최신 값). "
                                   "target_items 는 늘 보는 항목 + 인식기가 그 턴 요청에서 잡은 항목 + 턴 종류별 설계 항목(대화 목표 안) + 흐름상 해당하는 J6(10-23, 그 턴의 요청 · 함정 기준만), question_sha 는 그 턴까지의 질문 원문 지문.",
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
    # ④ k 조건 — 질문세트 전체를 정식 검사로(멀티턴 턴은 k_units 로 바꿔 넣음)
    after = copy.deepcopy(report)
    for row in after["cases"]:
        qid, turn = row["case_id"][1:].split("_T")
        c = out["questions"].get(qid)
        if c:
            t = c["turns"][int(turn) - 1]
            row["checks"] = [u for u in t["k_units"] if not u.startswith("J")]
            row["items"] = [u for u in t["k_units"] if u.startswith("J")]
    after["coverage"] = coverage(after["cases"])
    lo, hi = K_RANGE
    for name, r in (("지금 인식기", report), ("턴별 배정", after)):
        counts = target_counts(r)
        short = {k: v for k, v in counts.items() if v < 10}
        km = k_var_by_group(qs, r, units)
        conv = {g["id"]: km[g["id"]]["k"] for g in qs if len(g["turns"]) > 1}
        s = k_var_summary(qs, r)
        print(f"[k 조건 · {name}] 셀 수 있는 기준 {len(counts)}개 중 10번 미만 {len(short)}개 {short or ''} · 단일 질문 k 범위 밖 {s['out_of_range']}개(예외 {s['exempt']}) · "
              f"대화 평균 k {sorted(set(conv.values()))} · 범위 밖 대화 {[q for q, k in conv.items() if not lo <= k <= hi]}")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"저장: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
