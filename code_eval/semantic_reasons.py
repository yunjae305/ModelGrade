"""J1~J4·J7·J8 FAIL 근거의 형식 검증 (근거 없는 FAIL 정책, D-1).

J5·J6은 서버가 출처·이전 턴 인용을 원문과 대조하지만 J1~J4·J7은 이유 한 문장뿐이다.
평가 지시문은 FAIL 이유에 답변의 문제 부분을 따옴표로 인용하도록 요구하므로, 여기서는
그 요구를 형식으로만 확인한다: 이유가 있는가, 평가 데이터(질문·답변·참조 자료)에 실제로 있는 구절을
인용했는가. 이유가 판정을 의미적으로 뒷받침하는지는 검증하지 않으며 그렇게 표시하지 않는다.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, Optional

from .attachments import attachment_text

__all__ = ["REASON_EVIDENCE_POLICY_VERSION", "CHECKED_CRITERIA", "MISSING_EVIDENCE_CODE", "PROBLEMS",
           "fail_evidence", "incomplete_decision"]

#: v3 — J8 첨부파일 활용도 같은 규칙. 인용은 첨부 내용(attachments[].content)에서 찾아도 되고, 답변 인용이 하나 이상 있어야 한다.
#: v2 — J7 롱컨텍스트도 같은 규칙(이유 + 원문 인용), 인용은 참조 자료(reference)에서 찾아도 된다.
#: J7 은 입력(현재 질문·참조 자료) 인용과 답변 인용이 각각 하나 이상 원문과 맞아야 한다 (이전 턴 인용은 입력으로 보지 않음)
REASON_EVIDENCE_POLICY_VERSION = "j1-j4-j7-j8-fail-evidence-v3"
CHECKED_CRITERIA = ("J1", "J2", "J3", "J4", "J7", "J8")
MISSING_EVIDENCE_CODE = "JUDGE_FAIL_EVIDENCE_MISSING"
PROBLEMS = {
    "reason_missing": "FAIL 이유가 비어 있음",
    "quote_missing": "질문·답변 원문을 인용하지 않음",
    "quote_not_in_evaluated_text": "인용한 구절이 질문·답변 원문에 없음",
    "input_quote_missing": "입력(질문·참조 자료) 원문에서 찾을 수 있는 인용이 없음 (J7)",
    "answer_quote_missing": "답변 원문에서 찾을 수 있는 인용이 없음 (J7·J8)",
}
SCOPE = "형식 검증 — 이유 존재와 원문 인용 일치만 확인하며, 이유의 의미적 타당성은 검증하지 않음"

#: 따옴표로 감싼 인용 (‘…’ '…' “…” "…" 「…」 『…』)
_QUOTED = re.compile(r"[‘'“\"「『]([^‘’'“”\"「」『』\n]{2,200}?)[’'”\"」』]")


def _norm(text: Any) -> str:
    """대조용 정규화: NFKC, Markdown 강조·백틱 제거, 공백 묶기. 원문은 바꾸지 않는다."""
    if not isinstance(text, str):
        return ""
    text = re.sub(r"\*\*|__|`", "", unicodedata.normalize("NFKC", text))
    return " ".join(text.split())


def fail_evidence(decision: Optional[Dict[str, Any]], state: Dict[str, Any], criterion: Optional[str] = None) -> Dict[str, Any]:
    """FAIL 이유의 형식 검증 결과. valid 이면 이유가 있고 질문·답변 원문 인용이 하나 이상 일치한다.

    ``criterion == "J7"`` 이면 입력(질문·참조 자료)과 답변에서 각각 맞는 인용이 있어야 한다.
    ``criterion == "J8"`` 이면 답변에서 맞는 인용이 있어야 한다 (첨부 내용 인용은 있으면 근거로 센다)."""
    reason = decision.get("reason") if isinstance(decision, dict) else None
    reason = reason.strip() if isinstance(reason, str) else ""
    reference = state.get("reference")
    sources = {"answer": _norm(state.get("answer")), "question": _norm(state.get("question")),
               "reference": _norm(reference if isinstance(reference, str) else json.dumps(reference, ensure_ascii=False)
                                  if reference not in (None, "", {}, []) else ""),
               "attachment": _norm(attachment_text(state.get("attachments")))}
    history = " ".join(_norm(item.get(key)) for item in state.get("history") or []
                       if isinstance(item, dict) for key in ("question", "answer"))
    matched, unmatched, found = [], [], set()
    for raw in _QUOTED.findall(reason):
        quote = _norm(raw)
        if not quote:
            continue
        places = [name for name, text in sources.items() if text and quote in text]
        if history and quote in history:
            places.append("history")   # 다른 원문에 있어도 이전 턴에 있으면 함께 적는다 (places_complete)
        if places:
            # 답변이 입력 문구를 그대로 옮긴 경우처럼 여러 원문에 있으면 모두 근거로 센다 (found_in 은 첫 원문)
            matched.append({"quote": raw.strip(), "found_in": places[0], **({"also_in": places[1:]} if len(places) > 1 else {})})
            found.update(places)
        else:
            unmatched.append(raw.strip())
    problems = []
    if not reason:
        problems.append("reason_missing")
    elif not matched and not unmatched:
        problems.append("quote_missing")
    elif not any(item["found_in"] in sources for item in matched):
        problems.append("quote_not_in_evaluated_text")
    if criterion == "J7" and reason and (matched or unmatched):
        if not found & {"question", "reference"}:
            problems.append("input_quote_missing")
        if "answer" not in found:
            problems.append("answer_quote_missing")
    if criterion == "J8" and reason and (matched or unmatched) and "answer" not in found:
        problems.append("answer_quote_missing")
    # places_complete: 각 인용이 있는 곳(found_in + also_in)을 답변·질문·참조 자료·이전 턴 모두 확인해 적었다는 표시 — 화면이
    # '답변에만 있는 인용'을 판단할 때 이 표시가 있는 기록만 믿는다 (옛 기록은 첫 원문만 적음)
    return {"policy": REASON_EVIDENCE_POLICY_VERSION, "valid": not problems, "problems": problems, "places_complete": True,
            "matched_quotes": matched[:5], "unmatched_quotes": unmatched[:5], "scope": SCOPE}


def incomplete_decision(cid: str, name: str, cause: str) -> Dict[str, Any]:
    """재평가 뒤에도 근거 있는 판정을 얻지 못한 기준. PASS·FAIL 이 아니며 최종 정책도 FAIL 로 바꾸지 않는다."""
    return {"criterion_id": cid, "name": name, "decision": None, "status": "INCOMPLETE",
            "confidence": None, "probabilities": {}, "reason_code": MISSING_EVIDENCE_CODE,
            "reason": "판정 미완료 — 근거 없는 FAIL을 1회 재평가했지만 근거 있는 판정을 얻지 못했습니다: " + cause}
