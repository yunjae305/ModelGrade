"""질문 세트 무료 점검 — 업로드 전에 기준별 적용 질문 수·중복·형식·유형 분포를 API 호출 없이 확인한다 (설계안 v1.2 '평가 전 무료 점검').

외부 LLM으로 만든 질문 세트를 올리기 전에 돌린다. 적용 여부는 평가 때와 같은 코드 규칙(CODE 엔진의 질문 조건 추출,
의미 기준 ``jev_applicability``·``fact_question``)으로 정하고, 모델은 부르지 않는다. 답변이 없어도 된다(빈 답변으로 점검).

    python tools/check_question_set.py 질문세트.json            # 사람이 읽는 보고
    python tools/check_question_set.py 질문세트.jsonl --json     # JSON 보고

기준마다 적용 질문 30개 이상이면 충족 — 단일 질문 1개 = 1, 멀티턴 대화 1개 = 1(같은 대화의 여러 턴에 적용돼도 1, 팀 확정 10-13).
적용 턴 수는 참고로 함께 낸다(``applicable``·``applicable_turns``). 평가 자체는 턴마다 한다 — 설계안 v1.2 '질문 세트 구성'.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval import multi_turn  # noqa: E402
from code_eval.engine import _conversation_of, evaluate_batch  # noqa: E402
from code_eval.jev_applicability import fact_question, jev_applicability, sub_applicability  # noqa: E402
from code_eval.jev_eval import build_history, conversation_contexts  # noqa: E402
from code_eval.jev_client import resolve_semantic_pipeline  # noqa: E402
from code_eval.criteria_registry import CODE_UNITS, SUBSUMED_KINDS  # noqa: E402
from code_eval.semantic_items import ITEMS, applicable_items  # noqa: E402

#: 두 작업 줄에서 v2 가 다른 뜻으로 쓰였다 — main v2(J7 롱컨텍스트 입력 8K 토큰 이상 적용 수·판별 불가 수)와 작업 브랜치 v2(충족 enough·
#: short_criteria 를 적용 독립 질문·대화 수로, applicable 은 그대로 적용 턴 수). 둘을 합친 것 → v3.
CHECK_VERSION = "question-set-check-v5"   # v5: 세부 조건(검사·질문 조건 종류) 단위 적용 수 · v4: 검사·항목 단위 적용 수(coverage), 첨부파일
MIN_APPLICABLE = 30
#: 팀원 구조도의 유형 비율 초안 (설계안 v1.2 질문 세트 구성) — 유형 필드가 있을 때만 비교한다
TARGET_RATIOS = {"일반 지식·설명": .20, "정보 검색·사실 확인": .20, "분석·추론": .15, "코딩·기술": .15,
                 "글쓰기·요약": .10, "멀티턴 대화": .10, "롱 컨텍스트": .05, "취약점·난도": .05}
_TYPE_FIELDS = ("type", "category", "question_type", "유형")
CRITERIA = ("CODE 1", "CODE 2", "CODE 3", "J1", "J2", "J3", "J4", "J5", "J6", "J7", "J8")
#: 답변이 만드는 조건 — 답변 없이 점검할 때 질문 조건으로 세지 않는다
_ANSWER_CHECKS = ("1-2.", "1-3.", "3-1.answer_equation", "3-2.declared_count")
#: CODE 검사 단위 (AIONA 특화 제외) — (id, 적용 계기, 질문 요구 종류)
_UNITS = [(uid, trigger, frozenset(kinds)) for uid, _, trigger, kinds in CODE_UNITS if trigger != "aiona"]
#: 세부 조건 — 검사 단위 안에서 질문이 만드는 조건 종류 ('2-1.count:sentence_count'). 같은 종류를 두 검사가 함께 보면(코드블록 요구 →
#: 1-2.code_block·code_fence_min, JSON 요구 → json_syntax·json_structure) 처음 검사에만 둔다. 판정하지 않는 종류(표 행 수가 모호함)는 뺀다.
_UNSCORED_KINDS = frozenset({"table_rows_ambiguous"})
KIND_UNIT: Dict[str, str] = {}
for _uid, _trigger, _kinds in _UNITS:
    for _kind in sorted(_kinds):
        if _kind not in _UNSCORED_KINDS:
            KIND_UNIT.setdefault(_kind, _uid)
KINDS = tuple(f"{unit}:{kind}" for kind, unit in KIND_UNIT.items())
#: 답변 구조로만 생기는 검사 — 질문만으로는 적용 여부를 셀 수 없다 (답변을 받은 뒤 확인)
ANSWER_DEPENDENT = tuple(uid for uid, trigger, _ in _UNITS if trigger == "answer")
#: 답변 문장으로 적용이 정해지는 의미 항목 — J5-3.a 는 답변이 확인·조회·출처를 언급할 때 켜진다 (j5_items_applicability)
ANSWER_DEPENDENT_ITEMS = ("J5-3.a",)


def load_items(path: Path) -> List[Any]:
    """JSON(배열·{cases}·id/turns 묶음) · MD/TXT 는 업로드 화면과 같은 변환, JSONL 은 줄마다 객체 → 항목 목록."""
    text = path.read_text(encoding="utf-8-sig")
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    # JSON 도 업로드 화면과 같은 변환을 쓴다 — 배열·{cases}·id/turns 묶음·회차별 JSON
    from webapp.dataset_import import import_text
    rows, _ = import_text(text, path.name)
    return rows


def _normalized(question: str) -> str:
    return re.sub(r"\s+", " ", question).strip().lower()


def unit_of(row: Dict[str, Any]) -> str:
    """질문 단위 — 단일 질문 1개 = 1, 대화 1개 = 1. 단일 질문은 항목 위치(index)로 구분한다 (같은 case_id 가 두 번 나와도 2)."""
    if row.get("conversation_id") is not None:
        return f"대화:{row['conversation_id']}"
    return f"질문#{row['index']}" if row.get("index") is not None else f"질문:{row['case_id']}"


def criteria_counts(rows: List[Dict[str, Any]], minimum: int = MIN_APPLICABLE) -> Dict[str, Dict[str, Any]]:
    """기준별 공식 집계 — ``rows`` 는 턴마다 한 행 [{case_id, index?, conversation_id?, criteria}] (check_items 의 cases).

    applicable·applicable_turns: 적용 턴 수(참고) · applicable_units: 적용 독립 질문·대화 수(공식) ·
    enough: applicable_units ≥ minimum · short: minimum 까지 모자란 질문·대화 수. CLI 와 HTML 보고서가 함께 쓴다.
    """
    turns: Counter = Counter()
    units: Dict[str, set] = {c: set() for c in CRITERIA}
    for row in rows:
        for c in row["criteria"]:
            turns[c] += 1
            units[c].add(unit_of(row))
    return {c: {"applicable": turns[c], "applicable_turns": turns[c], "applicable_units": len(units[c]),
                "units_singles": sum(1 for u in units[c] if not u.startswith("대화:")),
                "units_conversations": sum(1 for u in units[c] if u.startswith("대화:")),
                "enough": len(units[c]) >= minimum, "short": max(0, minimum - len(units[c]))} for c in CRITERIA}


#: 질문 요구로 켜지는 검사 단위 — 모든 답변 검사(all)와 함께 질문만으로 셀 수 있다
_QUESTION_UNITS = frozenset(uid for uid, trigger, _ in _UNITS if trigger in ("question", "question_or_answer"))


def _code_kinds(result: Dict[str, Any]) -> List[str]:
    """이 질문(턴)에서 엔진이 읽은 세부 조건 (status ok, 다른 조건에 합쳐지는 것 제외) — '검사:종류'."""
    meta = result.get("meta") or {}
    kinds = {cond.get("kind") for cond in meta.get("question_conditions") or [] if isinstance(cond, dict) and cond.get("status", "ok") == "ok"}
    effective = kinds - {cond for cond, with_cond, _, _ in SUBSUMED_KINDS if with_cond in kinds}
    return sorted(f"{KIND_UNIT[k]}:{k}" for k in effective if k in KIND_UNIT)


def _code_checks(result: Dict[str, Any]) -> set:
    """이 질문(턴)에 적용되는 CODE 검사 단위 — 모든 답변 검사 + 질문 요구로 켜지는 검사.

    질문 요구 종류(kind)로 세되, 엔진이 다른 검사에 합치는 조건(``SUBSUMED_KINDS``: '불릿 3개'는 2-1.count 만 실행, 2-2.list 는
    만들지 않음)은 뺀다. 엔진이 실제로 만든 결과 행·판정 제외 조건의 질문 검사도 센다 — 1-2.code_fence_min 은 행의 출처가
    md_policy 이고, 빈 답변의 언어 표지는 판정 제외에만 나온다 (Codex 리뷰 2회: 출처 문자열·접두사 필터로 세면 둘 다 빠져
    보충 호출을 되풀이했다). 답변 구조로만 생기는 검사(answer)와 '검사 대상 없음' 행은 세지 않는다."""
    meta = result.get("meta") or {}
    # status 가 ok 인 조건만 — unsupported('코드블록 약 2개') 조건은 엔진이 검사하지 않는다 (판정 제외는 skipped_conditions 로 센다)
    kinds = {cond.get("kind") for cond in meta.get("question_conditions") or [] if isinstance(cond, dict) and cond.get("status", "ok") == "ok"}
    effective = kinds - {cond for cond, with_cond, _, _ in SUBSUMED_KINDS if with_cond in kinds}
    found = {uid for uid, trigger, unit_kinds in _UNITS if trigger == "all" or (uid in _QUESTION_UNITS and unit_kinds & effective)}
    for row in result.get("results") or []:
        check_id = str(row.get("check_id") or "")
        if check_id and not check_id.endswith(":none"):
            found.add(check_id.split(":")[0])
    for skipped in meta.get("skipped_conditions") or []:
        found.add(str(skipped.get("check_id") or "").split(":")[0])
    return found & ({uid for uid, trigger, _ in _UNITS if trigger == "all"} | _QUESTION_UNITS)


def coverage(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """검사·항목 단위 적용 질문·대화 수 — 질문 생성기가 빠진 검사·항목을 찾는다 (단일 질문 1개·대화 1개 = 1).

    답변 구조로만 생기는 검사(``ANSWER_DEPENDENT``)와 답변 문장으로 켜지는 항목(``ANSWER_DEPENDENT_ITEMS``)은 질문만으로 셀 수 없어
    빠진 목록에 넣지 않는다.
    의미 항목 가운데 평가 모델이 해당 여부를 정하는 항목(model_applies)은 '보냄' 기준으로 센다."""
    checks: Dict[str, set] = {uid: set() for uid, _, _ in _UNITS}
    kinds: Dict[str, set] = {kid: set() for kid in KINDS}
    items: Dict[str, set] = {item.id: set() for item in ITEMS}
    for row in rows:
        unit = unit_of(row)
        for uid in row.get("checks") or []:
            checks.setdefault(uid, set()).add(unit)
        for kid in row.get("kinds") or []:
            kinds.setdefault(kid, set()).add(unit)
        for item_id in row.get("items") or []:
            items.setdefault(item_id, set()).add(unit)
    return {"checks": {uid: len(units) for uid, units in checks.items()}, "items": {iid: len(units) for iid, units in items.items()},
            "kinds": {kid: len(units) for kid, units in kinds.items()},
            "missing_kinds": [kid for kid, units in kinds.items() if not units],
            "answer_dependent": list(ANSWER_DEPENDENT), "answer_dependent_items": list(ANSWER_DEPENDENT_ITEMS),
            "missing_checks": [uid for uid, units in checks.items() if not units and uid not in ANSWER_DEPENDENT],
            "missing_items": [iid for iid, units in items.items() if not units and iid not in ANSWER_DEPENDENT_ITEMS]}


def check_items(items: List[Any], pipeline: Optional[str] = None) -> Dict[str, Any]:
    """``pipeline`` 은 의미 평가 방식(legacy·items) — 없으면 평가와 같게 SEMANTIC_PIPELINE(.env) 을 따른다."""
    pipeline = resolve_semantic_pipeline(pipeline=pipeline)
    errors: List[Dict[str, Any]] = []
    cases: List[Dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append({"index": index + 1, "message": "JSON 객체가 아님"})
            continue
        question = item.get("question")
        if not isinstance(question, str) or not question.strip():
            errors.append({"index": index + 1, "message": "question 이 비었거나 문자열이 아님"})
            continue
        answer = item.get("answer", "")
        if not isinstance(answer, str):
            errors.append({"index": index + 1, "message": "answer 가 문자열이 아님"})
            continue
        conversation, problem = _conversation_of(item)
        if problem:
            errors.append({"index": index + 1, "message": problem})
            continue
        case = {"index": index + 1, "case_id": str(item.get("case_id") or item.get("id") or f"Q{index + 1:03d}"),
                "question": question, "answer": answer, "reference": item.get("reference"), "attachments": item.get("attachments"),
                "type": next((str(item[f]) for f in _TYPE_FIELDS if item.get(f) not in (None, "")), None)}
        if conversation:
            case["conversation_id"], case["turn"] = conversation["id"], conversation["turn"]
        cases.append(case)

    turns: Dict[Any, List[int]] = {}
    for case in cases:
        if "conversation_id" in case:
            turns.setdefault(case["conversation_id"], []).append(case["turn"])
    for cid, values in turns.items():
        if sorted(values) != list(range(1, len(values) + 1)):
            errors.append({"index": None, "message": f"대화 {cid}: turn 이 1부터 연속되지 않거나 중복됨 ({sorted(values)})"})

    seen: Dict[str, str] = {}
    duplicates = []
    for case in cases:
        key = _normalized(case["question"])
        if key in seen:
            duplicates.append({"case_id": case["case_id"], "same_as": seen[key], "question": case["question"][:80]})
        else:
            seen[key] = case["case_id"]

    # CODE 조건 — 평가 때와 같은 엔진으로 질문(과 이전 턴 지시)에서 나온 조건만 센다. 답변은 비어 있어도 된다.
    payloads = [{k: case[k] for k in ("case_id", "question", "answer", "reference", "attachments", "conversation_id", "turn")
                 if case.get(k) is not None} for case in cases]
    evaluated = evaluate_batch(payloads)["cases"] if payloads else []
    undetermined: Counter = Counter()   # 적용 여부를 판별하지 못한 질문 수 (J7 — 토크나이저 없음)
    per_case = []
    contexts = conversation_contexts(evaluated)   # 평가 준비와 같은 대화 이력 — 오류·누락 턴은 이력에서 빠지고 issues 로 남는다
    # 답변이 아직 없는 대화(질문 생성 중·수집 전 점검)는 대화 전체 10K 토큰을 잴 수 없다 — 그 몫은 답변이 채우므로 턴 수만 차면
    # J6 적용으로 센다(answers_pending). 길이 판정은 답변을 받은 뒤 평가가 한다 (multi-turn-v2, Codex 리뷰: 생성기가 J6 를 빠진 항목으로
    # 보고 보충을 되풀이했다)
    answered = {r["conversation"]["id"] for r in evaluated if r.get("conversation") and str(r.get("answer") or "").strip()}
    for ctx in contexts.values():
        measure = ctx.get("multi_turn")
        if (isinstance(measure, dict) and measure.get("multi_turn") is False and ctx["id"] not in answered
                and measure.get("turns", 0) + measure.get("uncertain_turns", 0) >= multi_turn.MIN_TURNS):
            ctx["multi_turn"] = {**measure, "multi_turn": True, "answers_pending": True}
    usable = []
    for i, (case, result) in enumerate(zip(cases, evaluated)):
        if result.get("case_status") != "EVALUATED":   # 엔진의 입력 오류·한도 초과는 평가에서도 빠진다 — 적용 수에서 뺀다
            errors.append({"index": case["index"], "message": f"{result.get('case_status')}: {result.get('input_error') or '평가할 수 없는 입력'}"})
            continue
        usable.append((i, case, result))
    for i, case, result in usable:
        hit = {"CODE 1"}   # 출력 무결성은 질문과 상관없이 모든 답변에 적용된다
        for row in result.get("results") or []:
            if row.get("source") in ("question", "history_instruction") and row.get("category") in ("1", "2", "3"):
                hit.add("CODE " + row["category"])
        for skipped in (result.get("meta") or {}).get("skipped_conditions") or []:
            # 답변이 없어 판정만 못 한 질문 조건(예: '3 + 4는 얼마야?' 의 계산값)도 적용이다
            check_id = str(skipped.get("check_id") or "")
            if check_id[:1] in "123" and not check_id.startswith(_ANSWER_CHECKS):
                hit.add("CODE " + check_id[0])
        ctx, history, conversation = contexts.get(i), [], None
        if ctx:
            history, _ = build_history(ctx["prior"])
            conversation = {"id": ctx["id"], "turn": ctx["turn"], "issues": ctx["issues"],
                            "history_turns": len(history) if ctx["complete"] else 0, "multi_turn": ctx.get("multi_turn")}
        # 항목 단위 적용(coverage)은 의미 평가 방식과 상관없이 항목별 규칙으로 센다 — 생성기가 빠진 항목을 찾는 데 쓴다
        subs = sub_applicability(case["question"], case["answer"], reference=case.get("reference"), attachments=case.get("attachments"),
                                 conversation=conversation, answer_tools="unknown", history=history)
        item_ids = [item.id for item, _ in applicable_items(case, subs, conversation)]
        if pipeline == "items":   # 평가와 같은 항목별 적용 규칙
            hit.update(item_id.split("-", 1)[0] for item_id in item_ids)
            undetermined.update({sid.split("-", 1)[0] for sid, sub in subs.items() if sub.get("undetermined")})
        else:
            semantic = jev_applicability(case["question"], None, reference=case.get("reference"), attachments=case.get("attachments"),
                                         conversation=conversation)
            hit.update(semantic["criteria"])
            undetermined.update(list(semantic.get("undetermined") or {}))
            if fact_question(case["question"], history)[0]:
                hit.add("J5")
        row = {"case_id": case["case_id"], "index": case["index"], "criteria": [c for c in CRITERIA if c in hit],
               "checks": sorted(_code_checks(result)), "kinds": _code_kinds(result), "items": item_ids}
        if "conversation_id" in case:
            row.update(conversation_id=case["conversation_id"], turn=case["turn"])
        per_case.append(row)

    singles = sum(1 for case in cases if "conversation_id" not in case)
    conversations = len(turns)
    units = singles + conversations
    criteria = criteria_counts(per_case)
    types = Counter(case["type"] for case in cases if case["type"])
    type_report = None
    if types:
        total = sum(types.values())
        type_report = {name: {"count": n, "ratio": round(n / total, 3), "target": TARGET_RATIOS.get(name)}
                       for name, n in types.most_common()}
    return {"version": CHECK_VERSION, "pipeline": pipeline, "items": len(items), "questions": units, "single_questions": singles,
            "conversations": conversations, "conversation_turns": sum(len(v) for v in turns.values()),
            "errors": errors, "duplicates": duplicates, "min_applicable": MIN_APPLICABLE, "count_unit": "question_or_conversation", "criteria": criteria,
            "undetermined": dict(undetermined),
            "short_criteria": [c for c in CRITERIA if not criteria[c]["enough"]], "types": type_report, "cases": per_case,
            "coverage": coverage(per_case),
            # 대화별 멀티턴 판정(평가와 같은 규칙, 이 파일의 답변으로) — {대화 ID: multi_turn.measure_conversation 결과}
            "multi_turn": {ctx["id"]: ctx.get("multi_turn") for ctx in contexts.values()}}


def render(report: Dict[str, Any]) -> str:
    lines = [f"질문 세트 점검 ({report['version']}, 의미 평가 방식 {report['pipeline']}, API 호출 없음)",
             f"- 질문 {report['questions']}개 = 단일 질문 {report['single_questions']}개 + 대화 {report['conversations']}개"
             f"({report['conversation_turns']}턴) · 항목 {report['items']}개",
             f"- 형식 오류 {len(report['errors'])}건 · 중복 질문 {len(report['duplicates'])}건"]
    for error in report["errors"][:10]:
        lines.append(f"    · {error['index'] or '-'}번: {error['message']}")
    for dup in report["duplicates"][:10]:
        lines.append(f"    · {dup['case_id']} = {dup['same_as']}: {dup['question']}")
    lines.append(f"- 기준별 적용 질문·대화 수 — {report['min_applicable']}개 이상 충족 (단일 질문 1개·대화 1개 = 1, 같은 대화의 여러 턴은 1). "
                 "적용 턴 수는 참고 (CODE 1 은 모든 답변, CODE 2·3 은 질문이 준 형식·계산 요구만 — 답변의 계산식·선언 개수 조건은 답변에 따라 더해짐)")
    for cid, row in report["criteria"].items():
        verdict = "충족" if row["enough"] else f"부족 {row['short']}개"
        lines.append(f"    {cid:7s} {row['applicable_units']:4d}개  {verdict:8s} · 적용 턴 {row['applicable_turns']}")
    for cid, n in (report.get("undetermined") or {}).items():
        lines.append(f"    {cid}: {n}개 질문(턴)은 적용 여부를 판별하지 못함 (토크나이저를 불러오지 못했거나 대화에 빠지거나 겹친 턴)")
    if report["types"]:
        lines.append("- 유형 분포 (목표 비율은 초안)")
        for name, row in report["types"].items():
            target = f" · 목표 {round(row['target'] * 100)}%" if row["target"] is not None else ""
            lines.append(f"    {name}: {row['count']}개 ({round(row['ratio'] * 100)}%){target}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path)
    parser.add_argument("--json", action="store_true", help="JSON 으로 출력")
    parser.add_argument("--pipeline", choices=("legacy", "items"), help="의미 평가 방식 (기본: .env 의 SEMANTIC_PIPELINE)")
    args = parser.parse_args(argv)
    report = check_items(load_items(args.path), args.pipeline)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else render(report))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
