"""AIONA 특화 마커 목록 항목의 형식 ``RulePattern`` 과 형식 오류 ``RegistryError``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

__all__ = ["PATTERN_KINDS", "RegistryError", "RulePattern"]

#: 패턴 표기 방식
PATTERN_KINDS = ("regex", "literal")

#: 마커 패턴이 쓸 수 있는 값 검증 함수. 지금은 등록된 것이 없다.
#: 카드번호 Luhn, 주민등록번호 검증식 등은 사내 규격을 받은 뒤 여기에 등록한다.
PROTECTED_VALIDATORS: Dict[str, Any] = {}


class RegistryError(ValueError):
    """Registry 설정이 잘못됐을 때. 답변 결함이 아니라 설정 결함이다."""


@dataclass(frozen=True)
class RulePattern:
    """등록된 규칙 1건."""

    id: str
    pattern: str
    kind: str = "regex"
    description: str = ""
    #: 매치된 값을 한 번 더 검증할 함수 이름
    validation: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise RegistryError("규칙 id 가 비어 있습니다")
        if not isinstance(self.pattern, str) or not self.pattern:
            raise RegistryError(f"{self.id}: pattern 이 비어 있습니다")
        if self.kind not in PATTERN_KINDS:
            raise RegistryError(f"{self.id}: 알 수 없는 kind 입니다 ({self.kind})")
        if self.kind == "regex":
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise RegistryError(f"{self.id}: 정규식이 잘못됐습니다 ({exc})") from exc
        if self.validation is not None and self.validation not in PROTECTED_VALIDATORS:
            raise RegistryError(
                f"{self.id}: 등록되지 않은 검증 함수입니다 ({self.validation})"
            )

    def compiled(self) -> "re.Pattern[str]":
        if self.kind == "literal":
            return re.compile(re.escape(self.pattern))
        return re.compile(self.pattern)

    def to_dict(self) -> Dict[str, Any]:
        data = {"id": self.id, "kind": self.kind, "pattern": self.pattern}
        if self.description:
            data["description"] = self.description
        if self.validation:
            data["validation"] = self.validation
        return data

    @classmethod
    def from_dict(cls, payload: Any, where: str) -> "RulePattern":
        if isinstance(payload, str):
            # 문자열만 적으면 리터럴로 본다
            return cls(id=payload, pattern=payload, kind="literal")
        if not isinstance(payload, dict):
            raise RegistryError(f"{where}: 규칙은 객체나 문자열이어야 합니다")
        try:
            return cls(
                id=payload["id"],
                pattern=payload["pattern"],
                kind=payload.get("kind", "regex"),
                description=payload.get("description", ""),
                validation=payload.get("validation"),
            )
        except KeyError as exc:
            raise RegistryError(f"{where}: 필수 항목이 없습니다 ({exc})") from exc
