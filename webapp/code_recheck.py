"""저장된 실행의 CODE 판정을 현재 엔진으로 다시 검사한 차이 — 읽기 전용(저장 결과는 바꾸지 않음), 모델 호출 없음.

옛 엔진이 답변을 잘못 읽어 만든 FAIL(예: v16.2.x 의 계산식 오독)이 결과 화면에 남아 '왜 FAIL 인지' 알 수 없던 문제를 위해,
열람할 때 같은 입력(질문·답변·대화 턴·참조 자료)을 현재 엔진으로 검사해 저장 당시와 달라진 FAIL 조건만 알려 준다.
판정·점수는 저장된 값 그대로이고, 새 판정은 원문 재평가(새 실행)로만 남는다.
"""
from __future__ import annotations

import re
from typing import Any, Dict

from code_eval.common_adjudication import resolved_case
from code_eval.core import ENGINE_VERSION
from code_eval.engine import evaluate_batch

__all__ = ["RECHECK_VERSION", "code_recheck"]

RECHECK_VERSION = "code-recheck-v1"
#: 판정에 쓰인 입력 필드 — 표시본(displayed_text)이 빠지면 표시본 기준 검사(닫는 태그 등)가 해제된 것처럼 보인다
_FIELDS = ("case_id", "question", "answer", "displayed_text", "reference", "conversation_id", "turn", "system_id")


def _fails(case: Dict[str, Any]) -> Dict[str, str]:
    """FAIL 조건 → {대응 키: check_id}. 순번이 붙은 조건은 순번을 뺀 검사 종류 + 공백을 접은 제목(등식이면 식 자체)으로 —
    앞 식이 보류되면 순번이 당겨지므로 순번으로 대응하면 다른 식에 '해제' 안내가 붙는다."""
    out: Dict[str, str] = {}
    for row in (case or {}).get("results") or []:
        if isinstance(row, dict) and row.get("area") == "common" and row.get("counted") and row.get("verdict") == "FAIL":
            check_id = str(row.get("check_id"))
            ordinal = re.match(r"(.*):\d+$", check_id)
            # 순번이 붙은 조건(답변 등식 ':1'·':2')만 제목(식)으로 짝짓는다 — 나머지 검사 ID 는 순번이 없어 그대로 안정적이고,
            # 제목 문구는 엔진 버전마다 바뀔 수 있다
            key = ordinal.group(1) + "|" + " ".join(str(row.get("title") or "").split()) if ordinal else check_id
            out[key] = check_id
    return out


def code_recheck(run: Dict[str, Any]) -> Dict[str, Any]:
    """run(result() 모양) → {version, engine, providers: {provider: {stored_engine, same_engine, cases: {index: {cleared, added}}}}}.

    ``cleared`` 는 저장 당시 FAIL 이었지만 현재 엔진에서는 FAIL 이 아닌 조건, ``added`` 는 그 반대다. 엔진이 같으면 다시 검사하지 않는다.
    """
    providers: Dict[str, Any] = {}
    for provider, batch in (run.get("evaluations") or {}).items():
        if not isinstance(batch, dict) or batch.get("live"):
            continue
        stored_engine = (batch.get("versions") or {}).get("engine")
        entry: Dict[str, Any] = {"stored_engine": stored_engine, "same_engine": stored_engine == ENGINE_VERSION, "cases": {}}
        providers[provider] = entry
        if entry["same_engine"]:
            continue
        rows = (run.get("datasets") or {}).get(provider) or []
        stored = batch.get("cases") or []
        records = (batch.get("jev_eval") or {}).get("records") or []
        payloads = [{key: row[key] for key in _FIELDS if key in row} if isinstance(row, dict) else {} for row in rows]
        fresh = evaluate_batch(payloads)["cases"] if payloads else []
        for index, (old, new) in enumerate(zip(stored, fresh)):
            if not isinstance(old, dict) or old.get("case_status") != "EVALUATED" or new.get("case_status") != "EVALUATED":
                continue
            record = records[index] if index < len(records) and isinstance(records[index], dict) else {}
            try:
                old = resolved_case(old, record)   # 화면과 같게 옛 보완 판정(code_completion)을 반영한 저장 판정과 비교
            except (KeyError, TypeError, IndexError):
                pass
            before, after = _fails(old), _fails(new)
            if set(before) != set(after):
                entry["cases"][str(index)] = {"cleared": sorted(before[k] for k in set(before) - set(after)),
                                              "added": sorted(after[k] for k in set(after) - set(before))}
    return {"version": RECHECK_VERSION, "engine": ENGINE_VERSION, "providers": providers}
