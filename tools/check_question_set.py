"""질문 세트 무료 점검 — 업로드 전에 기준별 적용 질문 수·중복·형식·유형 분포를 API 호출 없이 확인한다 (설계안 v1.2 '평가 전 무료 점검').

외부 LLM으로 만든 질문 세트를 올리기 전에 돌린다. 적용 여부는 평가 때와 같은 코드 규칙(CODE 엔진의 질문 조건 추출,
의미 기준 ``jev_applicability``·``fact_question``)으로 정하고, 모델은 부르지 않는다. 답변이 없어도 된다(빈 답변으로 점검).

    python tools/check_question_set.py 질문세트.json            # 사람이 읽는 보고
    python tools/check_question_set.py 질문세트.jsonl --json     # JSON 보고

기준마다 적용 질문(턴) 30개 이상을 권장한다 — 설계안 v1.1 '질문 수 실험', v1.2 '질문 세트 구성'.
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

from code_eval.engine import _conversation_of, evaluate_batch  # noqa: E402
from code_eval.jev_applicability import fact_question, jev_applicability, sub_applicability  # noqa: E402
from code_eval.jev_eval import build_history, conversation_contexts  # noqa: E402
from code_eval.jev_client import resolve_semantic_pipeline  # noqa: E402
from code_eval.semantic_items import applicable_items  # noqa: E402

CHECK_VERSION = "question-set-check-v2"   # v2: J7 롱컨텍스트(입력 8K 토큰 이상) 적용 수
MIN_APPLICABLE = 30
#: 팀원 구조도의 유형 비율 초안 (설계안 v1.2 질문 세트 구성) — 유형 필드가 있을 때만 비교한다
TARGET_RATIOS = {"일반 지식·설명": .20, "정보 검색·사실 확인": .20, "분석·추론": .15, "코딩·기술": .15,
                 "글쓰기·요약": .10, "멀티턴 대화": .10, "롱 컨텍스트": .05, "취약점·난도": .05}
_TYPE_FIELDS = ("type", "category", "question_type", "유형")
CRITERIA = ("CODE 1", "CODE 2", "CODE 3", "J1", "J2", "J3", "J4", "J5", "J6", "J7")
#: 답변이 만드는 조건 — 답변 없이 점검할 때 질문 조건으로 세지 않는다
_ANSWER_CHECKS = ("1-2.", "1-3.", "3-1.answer_equation", "3-2.declared_count")


def load_items(path: Path) -> List[Any]:
    """JSON(배열·{cases}) · JSONL · MD/TXT(업로드 화면과 같은 변환) → 항목 목록."""
    text = path.read_text(encoding="utf-8-sig")
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if suffix == ".json":
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("cases"), list):
            return data["cases"]
        return data if isinstance(data, list) else [data]
    from webapp.dataset_import import import_text
    rows, _ = import_text(text, path.name)
    return rows


def _normalized(question: str) -> str:
    return re.sub(r"\s+", " ", question).strip().lower()


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
                "question": question, "answer": answer, "reference": item.get("reference"),
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
    payloads = [{k: case[k] for k in ("case_id", "question", "answer", "reference", "conversation_id", "turn")
                 if case.get(k) is not None} for case in cases]
    evaluated = evaluate_batch(payloads)["cases"] if payloads else []
    applicable: Counter = Counter()
    undetermined: Counter = Counter()   # 적용 여부를 판별하지 못한 질문 수 (J7 — 토크나이저 없음)
    per_case = []
    contexts = conversation_contexts(evaluated)   # 평가 준비와 같은 대화 이력 — 오류·누락 턴은 이력에서 빠지고 issues 로 남는다
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
                            "history_turns": len(history) if ctx["complete"] else 0}
        if pipeline == "items":   # 평가와 같은 항목별 적용 규칙
            subs = sub_applicability(case["question"], case["answer"], reference=case.get("reference"),
                                     conversation=conversation, answer_tools="unknown", history=history)
            hit.update(item.id.split("-", 1)[0] for item, _ in applicable_items(case, subs, conversation))
            undetermined.update({sid.split("-", 1)[0] for sid, sub in subs.items() if sub.get("undetermined")})
        else:
            semantic = jev_applicability(case["question"], None, reference=case.get("reference"), conversation=conversation)
            hit.update(semantic["criteria"])
            undetermined.update(list(semantic.get("undetermined") or {}))
            if fact_question(case["question"], history)[0]:
                hit.add("J5")
        applicable.update(hit)
        per_case.append({"case_id": case["case_id"], "criteria": [c for c in CRITERIA if c in hit]})

    singles = sum(1 for case in cases if "conversation_id" not in case)
    conversations = len(turns)
    units = singles + conversations
    criteria = {c: {"applicable": applicable.get(c, 0), "enough": applicable.get(c, 0) >= MIN_APPLICABLE} for c in CRITERIA}
    types = Counter(case["type"] for case in cases if case["type"])
    type_report = None
    if types:
        total = sum(types.values())
        type_report = {name: {"count": n, "ratio": round(n / total, 3), "target": TARGET_RATIOS.get(name)}
                       for name, n in types.most_common()}
    return {"version": CHECK_VERSION, "pipeline": pipeline, "items": len(items), "questions": units, "single_questions": singles,
            "conversations": conversations, "conversation_turns": sum(len(v) for v in turns.values()),
            "errors": errors, "duplicates": duplicates, "min_applicable": MIN_APPLICABLE, "criteria": criteria,
            "undetermined": dict(undetermined),
            "short_criteria": [c for c in CRITERIA if not criteria[c]["enough"]], "types": type_report, "cases": per_case}


def render(report: Dict[str, Any]) -> str:
    lines = [f"질문 세트 점검 ({report['version']}, 의미 평가 방식 {report['pipeline']}, API 호출 없음)",
             f"- 질문 {report['questions']}개 = 단일 질문 {report['single_questions']}개 + 대화 {report['conversations']}개"
             f"({report['conversation_turns']}턴) · 항목 {report['items']}개",
             f"- 형식 오류 {len(report['errors'])}건 · 중복 질문 {len(report['duplicates'])}건"]
    for error in report["errors"][:10]:
        lines.append(f"    · {error['index'] or '-'}번: {error['message']}")
    for dup in report["duplicates"][:10]:
        lines.append(f"    · {dup['case_id']} = {dup['same_as']}: {dup['question']}")
    lines.append(f"- 기준별 적용 질문(턴) 수 — 권장 {report['min_applicable']}개 이상 "
                 "(CODE 1 은 모든 답변, CODE 2·3 은 질문이 준 형식·계산 요구만 — 답변의 계산식·선언 개수 조건은 답변에 따라 더해짐)")
    for cid, row in report["criteria"].items():
        lines.append(f"    {cid:7s} {row['applicable']:5d}  {'충분' if row['enough'] else '부족'}")
    for cid, n in (report.get("undetermined") or {}).items():
        lines.append(f"    {cid}: {n}개 질문은 적용 여부를 판별하지 못함 (토크나이저를 불러오지 못함 — tiktoken 설치·사전 내려받기 확인)")
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
