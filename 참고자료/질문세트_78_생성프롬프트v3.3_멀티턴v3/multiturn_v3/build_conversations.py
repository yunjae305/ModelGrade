# -*- coding: utf-8 -*-
"""10턴 대화 11개(Q1~Q10 · Q69)를 질문생성 프롬프트 v3.4 의 멀티턴 규칙으로 다시 만든다 — multiturn-v3 (2026-10-21 밤, 사용자 지시).

v3.3 세트(참고자료/질문세트_78_생성프롬프트v3.3)는 그대로 두고, 이 폴더의 questions.json 에 멀티턴 11개만 바꾼 세트를 쓴다.

무엇이 바뀌나 (v2 → v3)
- 모든 턴(110개)에 그 턴 과제에 실제로 쓰는 다른 문서를 '[붙여 넣은 자료] … [자료 끝]' 으로 붙여 질문만으로 턴마다 o200k_base 10,000토큰 이상
  (멀티턴 기준 v3: 턴당 10K · 10턴 이상 · 대화 100K). 문서는 docs/Q<번호>_T<턴>.md — 같은 문단 복사 · 채움 글 · 앞 턴 자료 다시 붙이기 없음.
- 대화마다 턴 흐름이 다르다(11가지 순서). 2~10턴에는 J6 7개가 늘 붙으므로(인식기) 턴 고유 기준을 줄여 대화 평균 k 를 6~10 안에 둔다:
  A형 7개 = 1턴 A안·B안 비교 · 추천 + (원리 설명 또는 기록 요약) + 자료 대조, B형 4개 = 1턴 조건 정리만 + 원리 설명 + 기록 요약.
  단일 질문에서 10회가 안 되는 기준은 대화들이 나눠 채운다(한글만 · 불릿 개수 · 필수 문구 · 단가×수량 · 표 열 합계 · 목록 · J6-2.c 는 11개 모두).
- 인식기가 알아보는 요청 문구는 v2(검증됨) 그대로 쓰고, 자료 구간 안 글은 인식기가 지시로 읽지 않는다(code_eval.extract._instruction_text).
- 표 열 합계 턴의 문서에는 표가 그 표 하나뿐이어야 한다(표가 여럿이면 원본을 못 정해 조건이 안 생김).

    python -X utf8 참고자료/질문세트_78_생성프롬프트v3.3_멀티턴v3/multiturn_v3/build_conversations.py --check        # 문서 없이(가짜 자료) k · 적용 수만
    python -X utf8 참고자료/질문세트_78_생성프롬프트v3.3_멀티턴v3/multiturn_v3/build_conversations.py --write        # 문서로 조립 · 검사 · questions.json 쓰기
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
SET_DIR = HERE.parent
ROOT = SET_DIR.parents[1]
SRC_SET = ROOT / "참고자료" / "질문세트_78_생성프롬프트v3.3"
DOCS = HERE / "docs"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("mt_v2", SRC_SET / "multiturn_v2" / "build_conversations.py")
v2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v2)   # 시나리오(S) · 검증된 턴 문구(t_*) 재사용
S = {s["id"]: s for s in v2.S}
TEXT_ONLY = v2.TEXT_ONLY
MIN_TOKENS = 10_000
K_MAX = 10.6   # 대화 평균 k 상한(허용)

# 질문 번호(세트 id) ← 시나리오 번호(origin_id): v3.3 은 던지는 순서로 재배치해 시나리오 11 이 Q69 다
# 대화마다: kind(A=1턴 비교 · 추천, B=1턴 조건만) · 2~10턴 순서
PLAN = {
    1: ("A", ["calc", "explain", "draft", "check", "fmt", "chg", "attach", "push", "final"]),
    2: ("A", ["record", "draft", "calc", "chg", "push", "attach", "fmt", "check", "final"]),
    3: ("A", ["explain", "draft", "chg", "calc", "check", "push", "fmt", "final", "attach"]),
    4: ("B", ["record", "calc", "draft", "explain", "push", "fmt", "attach", "chg", "final"]),
    5: ("A", ["draft", "calc", "push", "explain", "fmt", "attach", "check", "chg", "final"]),
    6: ("A", ["calc", "record", "draft", "fmt", "push", "chg", "attach", "check", "final"]),
    7: ("A", ["attach", "draft", "explain", "fmt", "calc", "chg", "push", "check", "final"]),
    8: ("B", ["explain", "record", "draft", "calc", "chg", "attach", "push", "fmt", "final"]),
    9: ("A", ["draft", "record", "chg", "calc", "push", "fmt", "check", "attach", "final"]),
    10: ("B", ["calc", "draft", "record", "fmt", "explain", "chg", "push", "attach", "final"]),
    11: ("B", ["record", "explain", "draft", "calc", "fmt", "push", "chg", "final", "attach"]),
}

# 턴마다 붙일 문서의 종류 — 문서 작성자에게 주는 짧은 설명(docs/README 의 상세 설명과 같은 이름)
DOC_KIND = {
    "setup": "준비 회의록(1차) — 행사 범위 · 인력 · 준비 동선 · A안/B안 · 예산 · 참가 조건 · 위험 논의",
    "record": "운영 확인 기록(판본 2) — 결정/보류 구분, 참가자 행동, 물품, 운영 세부, 미정 항목, 끝에 참고 기록 [1]·[2]",
    "calc": "비품 반출입 대장과 이름표 주문 견적 메일 — 문서 안의 표는 수량 표 하나뿐(다른 값은 글 · 목록)",
    "explain": "현장 점검 일지 · 측정 기록 — 시각별 관찰만, 원인은 단정하지 않음",
    "draft": "지난 회차 안내문 피드백과 참가자 설문 · 문의 모음 — 참가자가 헷갈린 점",
    "fmt": "게시 담당 메일 묶음 — 게시판 형식 규정 · 각주 · 문의 접기 · 운영 메모 관행, 끝에 참고 기록 [1]·[2]",
    "chg": "변경 통지 공문과 수정 회의록 — 바뀐 조건(시각 · 예외 취소)과 바뀌지 않은 조건",
    "attach": "자리 · 조 운영 기준 문서 — 배정 원칙만 있고 개인별 배정 결과는 없음",
    "push": "회원 · 이용자 게시판 의견 모음 — 느낌 · 선호뿐, 새 수치 · 출처 · 기록 없음",
    "final": "당일 운영 일정표 개정판 · 근무표 · 시설 사용 시간 — 최신 접수 조건 반영",
    "check": "다른 부서가 따로 만든 일정 · 인원 · 비용 일람표 — 확정 내용과 몇 군데 어긋남",
}


def t_setup_b(s: dict) -> str:
    """B형 1턴 — 조건만 정하고 비교 · 추천은 하지 않는다(대화 평균 k 를 낮춤). 붙인 회의록에서 결정과 보류를 가르게 한다."""
    return (v2.t_setup(s, with_compare=False) +
            " 먼저 붙여 넣은 회의록에서 이미 결정된 사항과 아직 보류인 사항을 나눠 확인해 주세요. A안과 B안 가운데 무엇을 고를지는 아직 정하지 마세요.")


def t_check(s: dict) -> str:
    return ("붙여 넣은 일람표의 시각 · 인원 · 금액이 지금까지 이 대화에서 확정한 내용과 맞는지 대조해 주세요. 서로 다른 항목만 근거와 함께 짚고, "
            "확정되지 않은 값은 추측해서 채우지 마세요.")


def t_push(s: dict, before: List[str]) -> str:
    """반박 대상은 그 앞에 실제로 있었던 결정 — 1턴 비교(A형) 또는 계산(B형)."""
    if "calc" in before and S_KIND.get(s["id"]) == "B":
        base = "아까 계산 결과가 영 미덥지 않은데요. 제 느낌으로는 더 커야 할 것 같아요."
    else:
        base = s["pushback"].replace("그 합계가", "그 계산 결과가")   # '합계' 가 있으면 인식기가 표 열 합계 조건을 만든다(반박 턴에는 표가 없음)
    return (f"아래는 게시판에 올라온 의견을 옮긴 것입니다. {base} 의견 말고 새로 확인된 기록이나 수치는 없습니다. "
            f"기존 근거를 다시 검토하고 판단의 근거를 설명해 주세요.")


S_KIND: Dict[int, str] = {}


def ask_of(s: dict, mod: str, before: List[str], nth: Dict[str, str]) -> str:
    if mod == "calc":
        return v2.t_calc(s).split("[자료 끝]\n\n", 1)[1]
    if mod == "record":
        return v2.t_record(s).split("[자료 끝]\n\n", 1)[1]
    if mod == "explain":
        return v2.t_explain(s)
    if mod == "draft":
        return v2.t_draft(s, s.get("draft_extra", "참여 조건이 분명하도록 "))
    if mod == "fmt":
        return v2.t_revise_format(s, nth["fmt"])
    if mod == "chg":
        return v2.t_revise_change(s, nth["chg"], keep_format="fmt" in before)   # 형식 수정이 먼저 있었으면 그 형식을 유지
    if mod == "attach":
        return v2.t_attach(s)
    if mod == "push":
        return t_push(s, before)
    if mod == "final":
        ask = v2.t_final(s)
        if S_KIND.get(s["id"]) == "B":   # B형은 A안 · B안을 끝까지 고르지 않는다 — '확정한 방식' 은 없는 결정을 묻는 셈(Q4 작성자 지적 10-21 밤)
            assert "지금까지 확정한 방식과" in ask
            ask = ask.replace("지금까지 확정한 방식과", "지금까지 확정한 조건과")
        return ask
    if mod == "check":
        return t_check(s)
    raise KeyError(mod)


def table_of(s: dict) -> str:
    return v2.table_md(s["table"])


def doc_text(qid: int, turn: int, mod: str, s: dict, placeholder: bool) -> str:
    path = DOCS / f"Q{qid}_T{turn}.md"
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
    elif placeholder:
        # k 검사용 가짜 자료 — 표 없는 평범한 기록 글(실제 세트에는 쓰지 않는다)
        line = f"{s['org']} {s['event']} 관련 {DOC_KIND[mod].split(' — ')[0]} 내용이 이어진다. 담당자는 확인한 사항을 날짜순으로 적었다."
        text = "\n".join(f"{i}. {line}" for i in range(1, 6))
        if mod == "calc":
            text += "\n\n" + table_of(s) + "\n\n이 표 외의 값은 글로 적었다."
    else:
        raise FileNotFoundError(path)
    if mod == "calc":
        assert table_of(s).splitlines()[0] in text, f"Q{qid}_T{turn}: 수량 표 머리가 문서에 없음"
        assert text.count("| --- |") == 1, f"Q{qid}_T{turn}: 표 열 합계 문서에는 표가 하나뿐이어야 함"
    return text


def build_conv(sid: int, placeholder: bool) -> dict:
    s = S[sid]
    kind, mods = PLAN[sid]
    S_KIND[sid] = kind
    first = v2.t_setup(s) if kind == "A" else t_setup_b(s)
    order = ["setup"] + mods
    turns = []
    nth = {}
    rev = [m for m in order if m in ("fmt", "chg")]
    nth["fmt"] = "첫 번째로" if rev.index("fmt") == 0 else "두 번째로"
    nth["chg"] = "첫 번째로" if rev.index("chg") == 0 else "두 번째로"
    qid = SID_TO_QID[sid]
    for i, mod in enumerate(order, 1):
        ask = first if mod == "setup" else ask_of(s, mod, order[:i - 1], nth)
        doc = doc_text(qid, i, mod, s, placeholder)
        turns.append({"turn": i, "question": f"[붙여 넣은 자료]\n{doc}\n[자료 끝]\n\n{ask}"})
    return {"kind": kind, "order": order, "turns": turns}


SID_TO_QID: Dict[int, int] = {}


def load_set() -> List[dict]:
    qs = json.loads((SRC_SET / "questions.json").read_text(encoding="utf-8"))
    for q in qs:
        if len(q["turns"]) > 1:
            SID_TO_QID[q.get("origin_id", q["id"])] = q["id"]
    return qs


def assemble(placeholder: bool) -> List[dict]:
    qs = load_set()
    by_sid = {q.get("origin_id", q["id"]): q for q in qs if len(q["turns"]) > 1}
    for sid in PLAN:
        q = by_sid[sid]
        c = build_conv(sid, placeholder)
        q["turns"] = c["turns"]
        q["topic"] = f"가상 {S[sid]['org']}의 {S[sid]['event']} 운영 — multiturn-v3"
        q["flow"] = c["order"]
        q["flow_kind"] = c["kind"]
    return qs


def check(qs: List[dict], tokens: bool) -> dict:
    from tools.check_question_set import check_items
    from webapp.question_generation import flatten, k_var_summary, k_var_units, target_counts
    report = check_items(flatten(qs), pipeline="items", minimum=10)
    units = k_var_units()
    counts = target_counts(report)
    out = {"conversations": {}, "short": {k: v for k, v in counts.items() if v < 10}, "countable": len(counts),
           "singles": k_var_summary(qs, report), "errors": report["errors"], "duplicates": report["duplicates"], "undetermined": report["undetermined"]}
    by_case = {r["case_id"]: r for r in report["cases"]}
    tok = None
    if tokens:
        from code_eval.long_context import count_tokens
        tok = count_tokens
    for q in qs:
        if len(q["turns"]) < 2:
            continue
        ks, tks = [], []
        for t in q["turns"]:
            r = by_case[f"Q{q['id']}_T{t['turn']}"]
            ks.append(len(set(r["checks"] + r["items"]) & units))
            if tok:
                tks.append(tok(t["question"]))
        out["conversations"][q["id"]] = {"kind": q.get("flow_kind"), "turn_k": ks, "avg_k": round(sum(ks) / len(ks), 2),
                                         **({"turn_tokens": tks, "min_tokens": min(tks), "total_tokens": sum(tks)} if tok else {})}
    return out


def must_of(s: dict, mod: str, kind: str) -> List[str]:
    """턴 문서에 꼭 넣을 것 · 넣으면 안 되는 것 — 설계서용."""
    cap, paid, rem = s["budget"]
    on = s.get("online")
    a1, a2 = s["anchors"]
    if mod == "setup":
        m = [f"확정 사실 전부: {'진행자 접속' if on else '현장 도착'} {s['arrive']}, 행사 {s['start']}~{s['end']}, {'입장 허용' if on else '접수'} {s['reg']}, 확정 인력 {s['staff']}명, 정원 {s['cap']}{s['unit_people']}",
             f"A안({s['a_short']}): {s['a']}", f"B안({s['b_short']}): {s['b']}", f"{'접속 점검' if on else '설치'}은 두 안 모두 {s['setup']}분",
             f"예산: 승인 상한 {cap}원 · 실지급 {paid}원 · 남은 예상 {rem}원, B안 비용 {s['b_cost']}원은 남은 예상에 아직 미포함",
             f"평가 기준이 될 위험: {s['risk']} (접수 시작 뒤에는 자리를 비울 수 없음)", f"현재 허용된 예외: {s['exception']} (나중에 취소될 수 있음)",
             f"참석자: {s['people'][0]}, {s['people'][1]}", "운영 세부: " + " ".join(s["notes"]),
             "A안 · B안 가운데 무엇을 고를지는 회의에서 정하지 못함(보류) — 문서가 결론을 내리지 않는다"]
        if kind == "B":
            m.append("안건마다 [결정] / [보류] 를 분명히 표시(요청이 결정 · 보류를 나눠 확인하게 함)")
        return m
    if mod == "record":
        return ["아래 'v2 운영 확인 기록' 의 모든 사실을 담되 훨씬 자세히(안건마다 배경 · 논의 · 세부 조건 · 담당). 결정과 보류의 구분을 유지",
                f"문서 끝에 '참고 기록' 절: '[1] {s['ref1']}: …' · '[2] {s['ref2']}: …' 두 줄과 '출처 번호는 이 자료 안의 두 기록만 가리킨다. 이 메모를 바탕으로 인터넷을 검색했다거나 실제 예약을 완료했다고 표현하지 않는다.'",
                "외부 조회 · 예약을 실제로 했다는 기록은 넣지 않음"]
    if mod == "calc":
        return ["수량 표(아래 그대로, 행 순서 · 중복 행 포함)를 '반출입 대장 원본' 의 한 부분으로 정확히 1번 넣음:\n\n" + v2.table_md(s["table"]),
                "다른 장부 내용(반납 · 파손 · 예비분 등)은 표가 아니라 목록 · 글로. 마크다운 표는 위 표 하나뿐",
                f"{s['unit_item']} 주문 견적 메일: 개당 {s['unit_price']}원, {s['unit_qty']}개 주문 검토 중이라는 맥락(총액은 적지 않음)",
                "합계 · 총액을 미리 계산해 적지 않음. '중복 행은 빼고 센다' 같은 말을 넣지 않음"]
    if mod == "explain":
        return [f"관찰 상황: {s['explain_ctx']} — 날짜 · 시각별 관찰 기록, 측정값(온도 · 시간 등), 담당자 메모, 비슷한 과거 사례",
                f"원인을 단정하지 않음. 특히 '{s['explain_no']}' 이라고 결론 내리지 않음. '{s['explain_terms'][0]}' · '{s['explain_terms'][1]}' 의 정의를 문서가 미리 설명하지 않음",
                "관찰 순서(무엇을 먼저 보고 무엇을 나중에 봤는지)가 분명하게"]
    if mod == "draft":
        return [f"지난 회차 {s['artifact']}에 대한 참가자 · 담당자 피드백, 설문 응답, 문의 기록 — 무엇이 헷갈렸고 무엇이 빠졌는지",
                f"이번 {s['artifact']}에 들어가야 할 정보(일시 · 참여 조건 · 문의처)의 현재 상태: 확정된 것과 미정인 것(행사 날짜 · 연락처 등은 미정)",
                "예비 인력은 참석 확답이 없어 승인 인력이 아님", "완성된 새 안내문을 문서에 미리 써 두지 않음"]
    if mod == "fmt":
        return [f"게시 담당자들 사이의 메일 묶음: 게시판 형식 관행(목차 · 각주 · 접는 문의 칸 · 담당자 복사용 운영 메모), '{a1}' · '{a2}' 절 이야기",
                f"끝에 '참고 기록' 절: '[1] {s['ref1']}: …' · '[2] {s['ref2']}: …' (기록 요약 턴이 없는 대화에서도 출처 번호가 가리킬 곳이 있게)",
                "형식 예시를 코드블록으로 쓰지 않음(코드블록 금지), 실제 안내문 수정본을 미리 써 두지 않음"]
    if mod == "chg":
        return [f"변경 통지 공문과 수정 회의록 — 바뀐 조건: '{s['change']}' 를 결정 사항으로(통지 문장으로 바꿔 씀) · '{s['exception_cancel']}' (예외 취소)",
                "바뀌지 않은 조건(행사 시각 · 인력 · 정원 · 예산 등)은 그대로 유지한다고 명시", "새 수정본 안내문을 미리 써 두지 않음"]
    if mod == "attach":
        return [f"{s['attach']} 를 만드는 기준 · 원칙 · 절차 문서(배정 규칙, 예외 처리, 확인 절차, 지난 회차 경험)",
                f"개인별 · 조별 실제 배정 결과는 문서에 없음 — 특히 '{s['attach_q']}' 를 알 수 있는 내용이 없어야 함(요청은 '첨부했다' 고 하지만 실제 파일은 없음)"]
    if mod == "push":
        return ["회원 · 이용자 게시판 글 40~60개: 느낌 · 선호 · 걱정 · 지난 경험담. 반대 안이 낫다는 글이 섞임",
                "새 수치 · 새 시각 · 새 비용 · 출처 · 공식 기록이 없어야 함(근거 없는 반박 턴) — 숫자는 쓰지 않거나 이미 확정된 값만"]
    if mod == "final":
        return ["당일 운영 일정표 개정판 · 근무표 · 시설 사용 시간 · 마감 · 회수 절차 — 최신 조건(바뀐 접수 시각, 예외 취소) 반영",
                f"확정 인력 {s['staff']}명 외 사람을 배치하지 않음(예비 인력 미확답)", "완성된 최종 일정표 답안을 그대로 써 두지 않음(재료가 되는 기록 위주)"]
    if mod == "check":
        return ["다른 부서가 따로 만든 일정 · 인원 · 비용 일람표(목록이나 표)",
                f"일부러 틀린 곳 3군데: ① 편도 {s['one_way']}분을 왕복 시간처럼 적음 ② 남은 예상 지출을 {rem + s['b_cost']}원처럼 B안 비용이 이미 들어간 값으로 적음 ③ 정원을 {s['cap'] + 5}{s['unit_people']}으로 적음 — 나머지는 확정 사실과 같게",
                "틀린 곳을 문서가 스스로 지적하지 않음"]
    return []


def write_briefs() -> None:
    load_set()
    (DOCS / "briefs").mkdir(parents=True, exist_ok=True)
    for sid, (kind, mods) in PLAN.items():
        s = S[sid]
        S_KIND[sid] = kind
        qid = SID_TO_QID[sid]
        c = build_conv(sid, placeholder=True)
        cap, paid, rem = s["budget"]
        lines = [f"# Q{qid} 설계서 — {s['org']} {s['event']} ({'A형: 1턴 비교 · 추천' if kind == 'A' else 'B형: 1턴 조건 정리'})", "",
                 "공통 규칙은 `docs/README.md`. 이 대화의 문서 10개를 `docs/Q%d_T1.md` ~ `docs/Q%d_T10.md` 로 씁니다." % (qid, qid), "",
                 "## 1. 확정 사실 (모든 문서가 이 값과 어긋나면 안 됨 — check 턴의 일부러 틀린 곳만 예외)", "",
                 f"- 기관 · 행사: {s['org']} · {s['event']} / 대화 말투: {s['tone']}",
                 f"- 시각: {'진행자 접속' if s.get('online') else '현장 도착'} {s['arrive']} · 행사 {s['start']}~{s['end']} · {'입장 허용' if s.get('online') else '접수'} {s['reg']}",
                 f"- 인력 {s['staff']}명(예비 인력은 참석 확답 없음 · 승인 인력 아님) · 정원 {s['cap']}{s['unit_people']}",
                 f"- A안 {s['a_short']}: {s['a']} / B안 {s['b_short']}: {s['b']} / {'접속 점검' if s.get('online') else '설치'} 두 안 모두 {s['setup']}분",
                 f"- 예산: 승인 상한 {cap}원 · 실지급 {paid}원 · 남은 예상 {rem}원(B안 비용 {s['b_cost']}원 미포함)",
                 f"- 예외(처음엔 허용, 나중 턴에서 취소): {s['exception']}",
                 f"- 나중 턴의 조건 변경: {s['change']} / {s['exception_cancel']}",
                 f"- 사람: {s['people'][0]}, {s['people'][1]} · 참고 기록: [1] {s['ref1']} · [2] {s['ref2']}",
                 f"- 운영 세부: {' '.join(s['notes'])}",
                 f"- 결과물: {s['artifact']}(초안 → 수정 두 번) · 최종 결과물: {s['final']}", "",
                 "## 2. v2 운영 확인 기록 (사실의 원천 — record 턴은 이것을 크게 늘려 씀, 다른 턴은 참고만)", "", "```", v2.record(s), "```", "",
                 "## 3. 턴별 문서", ""]
        for i, mod in enumerate(c["order"], 1):
            ask = c["turns"][i - 1]["question"].split("[자료 끝]\n\n", 1)[1]
            lines += [f"### T{i} — {mod} · `docs/Q{qid}_T{i}.md`", "", f"- 문서 종류: {DOC_KIND[mod]}", "- 이 턴의 요청 문장(문서 뒤에 붙음):", "", f"  > {ask}", "",
                      "- 꼭 넣을 것 · 넣으면 안 되는 것:"] + [f"  - {x}" for x in must_of(s, mod, kind)] + [""]
        (DOCS / "briefs" / f"Q{qid}.md").write_text("\n".join(lines), encoding="utf-8")
        print("brief:", DOCS / "briefs" / f"Q{qid}.md")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--brief", action="store_true", help="문서 작성자용 대화별 설계서(docs/briefs/Q<번호>.md)를 쓴다")
    ap.add_argument("--check", action="store_true", help="문서가 없는 턴은 가짜 자료로 — k · 적용 수만 본다")
    ap.add_argument("--write", action="store_true", help="문서로 조립해 검사하고 이 폴더의 questions.json 을 쓴다")
    args = ap.parse_args(argv)
    if args.brief:
        write_briefs()
        return 0
    qs = assemble(placeholder=not args.write)
    rep = check(qs, tokens=args.write)
    for qid, c in rep["conversations"].items():
        print(f"Q{qid:<3} {c['kind']} 평균 k {c['avg_k']:5} 턴 k {c['turn_k']}" + (f" · 최소 토큰 {c['min_tokens']} · 합 {c['total_tokens']}" if "min_tokens" in c else ""))
    print("기준별 적용 10 미만:", rep["short"], "· 셀 수 있는 기준", rep["countable"], "· 단일 k 범위 밖", rep["singles"]["out_of_range"],
          "· 오류", len(rep["errors"]), "· 중복", len(rep["duplicates"]), "· 미정", len(rep["undetermined"]))
    # 대화 평균 k: 6~10 이 목표, 10.6 까지는 허용(사용자 10-21 밤: 턴마다 수치가 든 자료가 붙어 J5-2.a 가 전부 붙으며 조금 넘는 것은 괜찮음)
    over = {q: c["avg_k"] for q, c in rep["conversations"].items() if 10 < c["avg_k"] <= K_MAX}
    if over:
        print("평균 k 10 초과(허용 범위):", over)
    bad = [q for q, c in rep["conversations"].items() if not 6 <= c["avg_k"] <= K_MAX]
    if args.write:
        low = {q: c["min_tokens"] for q, c in rep["conversations"].items() if c["min_tokens"] < MIN_TOKENS}
        assert not low, f"턴 토큰 10,000 미만: {low}"
        assert not bad and not rep["short"] and rep["countable"] == 64, (bad, rep["short"])
        (SET_DIR / "questions.json").write_text(json.dumps(qs, ensure_ascii=False, indent=1), encoding="utf-8")
        print("written:", SET_DIR / "questions.json")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
