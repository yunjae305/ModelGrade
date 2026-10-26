"""v16 코드 평가 결과 구조.

판정(verdict)은 ``PASS`` / ``FAIL`` / ``None`` 뿐이다. 실행 상태(status)는 판정과 분리한다.
심각·주의·HOLD·severity·가중치는 없다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = [
    "PASS", "FAIL", "Status", "SUBCATEGORIES", "SPECIALIZED", "CATEGORY_NAMES", "CODE_GROUP_OVERRIDE", "code_group", "ERROR_GROUPS", "is_error_check",
    "Evidence", "ConditionResult", "stable_hash",
]

PASS = "PASS"
FAIL = "FAIL"


class Status:
    """실행 상태. 판정이 아니다.

    공통 영역(area == "common")의 결과 행은 EXECUTED(verdict PASS/FAIL)만 허용한다 — 판정할 수 없는 조건은
    행을 만들지 않고(meta.skipped_conditions), 검사기 오류·한도 초과는 사례의 errors 로 남긴다.
    나머지 상태값은 보류 중인 AIONA 특화 검사(checks_aiona)와 v16.2.x 까지 저장된 옛 결과 JSON 을 읽기 위해 유지한다.
    """

    EXECUTED = "EXECUTED"                     # 검사 완료 → verdict 는 PASS/FAIL
    CONFIG_MISSING = "CONFIG_MISSING"         # 필요한 설정 없음·빈 목록·대응 불일치
    DISPLAY_MISSING = "DISPLAY_MISSING"       # 실제 표시본 없음
    INCOMPLETE = "INCOMPLETE"                 # 일부만 검사 (FAIL 이 확인되면 verdict=FAIL 보존)
    UNSUPPORTED = "UNSUPPORTED"               # 지원하지 않는 문법·모호한 요구
    DEPENDENCY_FAILED = "DEPENDENCY_FAILED"   # 선행 검사 실패로 측정 불가 (예: JSON 구문 실패 후 필드)
    CHECK_ERROR = "CHECK_ERROR"               # 검사기 자체 오류
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"         # 입력 크기·깊이·연산 한도 초과
    OUT_OF_SCOPE = "OUT_OF_SCOPE"             # 정책 적용 서비스가 아니거나 확인 불가
    DUPLICATE = "DUPLICATE"                   # 같은 대상·조건·측정 규칙의 결과를 참조 (재집계 안 함)
    APPLICABILITY_EXCEPTION = "APPLICABILITY_EXCEPTION"  # 명시 요청으로 해당 규칙 적용 제외


#: 화면 대분류·소분류. 임의로 늘리지 않는다.
CATEGORY_NAMES = {"0": "에러 케이스", "1": "출력 무결성", "2": "요구 조건 준수", "3": "수치·데이터 정합성"}   # 0 에러 케이스: 사용자 10-26 대분류로
SUBCATEGORIES = {
    "0-1": "에러 케이스",
    "1-1": "응답·문자 정상성",
    "1-2": "문서·코드 유효성",
    "1-3": "참조 연결 유효성",
    "2-1": "분량·표현 조건",
    "2-2": "형식·구성 조건",
    "3-1": "계산 결과 일치",
    "3-2": "선언 개수 일치",
    "3-3": "원본 보존·변환 일치",
}
#: 에러 케이스 소분류 — CODE · LLM 밖의 단독 기준 (사용자 10-26 저녁). 엔진은 같은 자리에서 재지만 개수 · 보고서는 따로 센다
ERROR_GROUPS = frozenset({"0-1"})
#: 기준 ID 앞부분과 소분류가 다른 검사 — ID 는 질문 세트 · 지난 결과의 키라 그대로 두고 소분류만 옮김 (10-26 에러 케이스 대분류)
CODE_GROUP_OVERRIDE = {"1-1.response_nonempty": "0-1", "2-1.explicit_empty": "0-1"}


def code_group(check_id: str) -> str:
    """CODE 검사 ID → 소분류(예: '2-2.table' → '2-2', '1-1.response_nonempty' → '0-1'). ':대상' 꼬리는 뗀다."""
    unit = str(check_id).split(":")[0]
    return CODE_GROUP_OVERRIDE.get(unit) or unit.split(".")[0]


def is_error_check(check_id: str) -> bool:
    """에러 케이스 기준(단독 기준)인가 — '1-1.response_nonempty' · '2-1.explicit_empty'."""
    return code_group(check_id) in ERROR_GROUPS


SPECIALIZED = {
    "AIONA-M": "내부 마커 노출",
    "AIONA-F": "금지 표현 출현",
}


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class Evidence:
    target: str               # question | answer | displayed_text | answer_projection | config
    start: Optional[int] = None
    end: Optional[int] = None
    excerpt: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"target": self.target, "start": self.start, "end": self.end,
                "excerpt": self.excerpt, "note": self.note}


@dataclass
class ConditionResult:
    check_id: str                  # 예: 2-1.bullet_count
    check_version: str
    subcategory: str               # 1-1 … 3-3 또는 AIONA-M / AIONA-F
    title: str                     # 화면 표시명
    source: str                    # question | answer_self | md_policy | format_spec | operating_policy | aiona_config
    status: str
    verdict: Optional[str] = None
    required: Any = None           # 질문·설정에서 읽은 요구값
    measured: Any = None           # 실제 측정값
    comparison: str = ""           # 비교식
    reason: str = ""               # 템플릿으로 만든 판정 이유 또는 미검사 사유
    evidence: List[Evidence] = field(default_factory=list)
    scope: str = "answer"          # 측정 범위
    applies_to: str = "all_services"
    policy_version: str = ""
    dedupe_key: Optional[str] = None
    duplicate_of: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
    condition_id: str = ""

    def __post_init__(self) -> None:
        if self.verdict not in (PASS, FAIL, None):
            raise ValueError(f"verdict 는 PASS/FAIL/None 만 허용: {self.verdict!r}")
        if self.status == Status.EXECUTED and self.verdict is None:
            raise ValueError(f"EXECUTED 결과에는 판정이 필요합니다: {self.check_id}")
        if self.status not in (Status.EXECUTED, Status.INCOMPLETE) and self.verdict is not None:
            raise ValueError(f"{self.status} 상태에는 판정을 남기지 않습니다: {self.check_id}")
        if self.status == Status.INCOMPLETE and self.verdict == PASS:
            raise ValueError("미완료 검사는 PASS 가 될 수 없습니다")
        if self.area == "common" and (self.status != Status.EXECUTED or self.verdict not in (PASS, FAIL)):
            raise ValueError(f"공통 결과 행은 PASS/FAIL 뿐입니다: {self.check_id} status={self.status}")
        if not self.condition_id:
            self.condition_id = stable_hash({
                "check": self.check_id, "key": self.dedupe_key,
                "required": self.required, "scope": self.scope})[:16]

    @property
    def area(self) -> str:
        return "aiona_specialized" if self.subcategory.startswith("AIONA") else "common"

    @property
    def category(self) -> Optional[str]:
        return None if self.area != "common" else self.subcategory.split("-")[0]

    @property
    def counted(self) -> bool:
        """PASS/FAIL 집계 대상인가 — 판정이 있고 중복 참조가 아닌 결과만."""
        return self.verdict in (PASS, FAIL) and self.status != Status.DUPLICATE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "check_id": self.check_id,
            "check_version": self.check_version,
            "area": self.area,
            "category": self.category,
            "category_name": CATEGORY_NAMES.get(self.category or "", None),
            "subcategory": self.subcategory,
            "subcategory_name": SUBCATEGORIES.get(self.subcategory) or SPECIALIZED.get(self.subcategory),
            "title": self.title,
            "source": self.source,
            "policy_version": self.policy_version,
            "applies_to": self.applies_to,
            "scope": self.scope,
            "status": self.status,
            "verdict": self.verdict,
            "counted": self.counted,
            "required": self.required,
            "measured": self.measured,
            "comparison": self.comparison,
            "reason": self.reason,
            "evidence": [e.to_dict() for e in self.evidence],
            "duplicate_of": self.duplicate_of,
            "details": self.details,
        }
