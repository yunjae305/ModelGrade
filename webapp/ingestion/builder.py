"""어댑터 공통 — 대화 턴 목록을 공통 묶음의 질문·답변으로 바꾼다.

질문 원문과 답변 원문은 받은 그대로 둔다. 한 턴뿐인 대화는 단일 질문으로 둔다 (다른 출처의 단일 질문과
같은 질문으로 맞춰지도록). 앞부분이 빠진 대화는 빠진 턴 번호를 비워 두어 평가기가 '앞 대화 누락'으로 다룬다.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional

from .schema import SCHEMA, quality

SYSTEM_FIELDS = ("system_key", "label", "provider", "platform", "model", "model_version", "access_mode", "settings",
                 "environment")


def system_from(options: Optional[Dict[str, Any]], defaults: Dict[str, Any]) -> Dict[str, Any]:
    """어댑터가 아는 값(defaults) 위에 사용자가 밝힌 값(options.system)을 덮는다. 모르는 값은 지어내지 않는다."""
    given = (options or {}).get("system")
    system = copy.deepcopy(defaults)
    if isinstance(given, dict):
        for key in SYSTEM_FIELDS:
            if key == "environment" and isinstance(given.get(key), dict):
                system.setdefault("environment", {}).update(copy.deepcopy(given[key]))
            elif given.get(key) is not None:
                system[key] = copy.deepcopy(given[key])
    return system


def safe_id(value: Any, limit: int = 40) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-")
    return text[:limit] or "x"


class BundleBuilder:
    def __init__(self, system: Dict[str, Any], *, adapter: str, format_label: str, source_name: str,
                 collection_method: str):
        self.system = system
        self.method = collection_method
        self.questions: List[Dict[str, Any]] = []
        self.responses: List[Dict[str, Any]] = []
        self.notes: List[str] = []
        self.info = {"adapter": adapter, "format": format_label, "source_name": source_name}

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    def _response(self, case_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        response = {"case_id": case_id, "system_key": self.system.get("system_key"), "collection_method": self.method}
        response.update(copy.deepcopy(data))
        response.setdefault("collection_quality", [])
        return response

    def add_conversation(self, key: str, turns: List[Dict[str, Any]], *, start_turn: int = 1,
                         reference: Any = None) -> None:
        """turns: [{"question": str, "response": {...} | None, "turn"?: int}] — response 가 None 이면 답변이 없는 턴이다.

        turn 번호가 없으면 start_turn 부터 차례로 붙인다. 번호 사이가 비면 빈 턴을 만들지 않는다.
        """
        if not turns:
            return
        prefix = safe_id(key)
        numbers = [t.get("turn") or start_turn + offset for offset, t in enumerate(turns)]
        single = len(turns) == 1 and numbers[0] == 1
        for number, turn in zip(numbers, turns):
            case_id = prefix if single else f"{prefix}-T{number:02d}"
            question = {"case_id": case_id, "question": turn["question"]}
            if not single:
                question.update(conversation_id=key, turn=number)
            if reference is not None:
                question["reference"] = reference
            self.questions.append(question)
            if turn.get("response") is None:
                self.responses.append(self._response(case_id, {
                    "answer": "", "status": "NOT_COLLECTED", "error": "원본에 이 질문의 AI 답변이 없습니다.",
                    "collection_quality": [quality("answer_missing", "원본 대화에 이 질문의 답변이 없습니다.", "warning")]}))
            else:
                self.responses.append(self._response(case_id, turn["response"]))

    def bundle(self) -> Dict[str, Any]:
        return {"schema": SCHEMA, "questions": self.questions, "systems": [self.system], "responses": self.responses,
                "import": {**self.info, "notes": list(self.notes), "converted": True}}
