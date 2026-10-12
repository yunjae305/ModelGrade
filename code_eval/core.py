"""검사 공용 도구: 실행 문맥, 버전, 결과 생성, 비교식."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .analysis import ANALYSIS_VERSION, TextAnalysis
from .extract import EXTRACT_VERSION, QuestionConditions, extract_conditions
from .model import ConditionResult, Evidence, FAIL, PASS, Status
from .scripts import SCRIPT_DATA_VERSION

ENGINE_VERSION = "aiona-code-eval-v16.2.6"
MD_SOURCE = "참고자료/채팅품질 평가기준.md"
MD_SHA256 = "fb7126e7a31073745517273a1dfdfea6508cb3af2454cca978cc88055eb182dd"
MD_POLICY_VERSION = f"채팅품질 평가기준(8).md sha256:{MD_SHA256[:12]}"

#: 검사별 규칙 버전. 결과마다 남긴다.
CHECK_VERSIONS: Dict[str, str] = {
    "1-1.response_nonempty": "v16-1.0 (비내용 문자 제거 후 빈 응답 확인)",
    "1-1.character_rules": "v16-1.2 (깨짐 서명 + C0 제어 문자, 질문 주제·주변 설명·예시 맥락 구분)",
    "1-1.foreign_script": "v16-1.0 (질문 주제·주변 설명·괄호 병기가 없는 외국 문자)",
    "1-2.code_block": "v16-1.1 (질문 요구, CommonMark 펜스 파서, 목록 항목 안 펜스)",
    "1-2.code_language_label": "v16-1.0 (info string 첫 낱말)",
    "1-2.code_fence_min": "v16-1.0 (MD 165행)",
    "1-2.fence_imbalance": "v16-1.0 (MD 170행, 코드 홀수 OR 수식 홀수)",
    "1-2.json_syntax": "v16-1.0 (RFC 8259 엄격 파싱, NaN/Infinity 거부)",
    "1-2.python_syntax": "v16-1.1 (ast.parse+compile, 실행·import 없음, 펜스 들여쓰기를 뺀 내용)",
    "1-2.orphan_closing_tag": "v16-1.0 (MD 169행, 여는 태그 대응 스택)",
    "1-3.footnote_link": "v16-1.1 (코드 예시 제외, 각주 파서·빈 정의, 지원 밖 정의 격리)",
    "1-3.citation_link": "v16-1.1 (코드 예시 제외, 인용 번호 파서)",
    "1-3.internal_link": "v16-1.1 (코드 예시 제외, 실제 본문 제목·앵커 연결)",
    "2-1.explicit_empty": "v16-1.0",
    "2-1.count": "v16-1.4 (질문 출력 범위·번호 제목 연결, 펜스 길이에 따른 코드 제외, 모호한 구조·결과물 내부 미판정, CommonMark 형제 항목 들여쓰기)",
    "2-1.char_count": "v16-1.1 (코드 포인트, 펜스 길이에 따른 코드 제외·줄바꿈 제외)",
    "2-1.literal": "v16-1.1 (코드 제외·강조 제거 투영, NFC, 정확 일치 요구가 아니면 공백·줄바꿈 접기)",
    "2-1.charset": "v16-1.0 (문자 범주 L*)",
    "2-2.table": "v17-1.3 (명시 표 개수 추가, 고정 최소 행 수 없음, 빈 헤더 줄은 헤더 아님, 코드·데이터 결과물 안의 표는 판정 제외, 양끝 파이프 생략·인용문 안 GFM 표)",
    "2-2.list": "v16-1.1 (코드·데이터 결과물 안의 목록은 판정 제외)",
    "2-2.heading": "v16-1.1 (ATX 제목, 코드·데이터 결과물 안의 제목은 판정 제외)",
    "2-2.json_structure": "v16-1.0",
    "3-1.question_calculation": "v16-1.1 (Fraction 정확 계산, 질문 피연산자·답변 등식 결과 부호 보존, eval 없음)",
    "3-1.answer_equation": "v16-1.2 (숫자 제곱·괄호 곱셈, 실수 단어 경계, 백분율 접미사 구분)",
    "3-2.declared_count": "v16-1.3 (연속 번호 제목·목록 구조 경계, 도입 설명·항목 설명 줄 보존, CommonMark 형제 항목 들여쓰기)",
    "3-3.transform": "v17-1.2 (펜스·본문 원본 후보 통합, 여러 원본 미판정, 원본 없는 함수 요구 비활성)",
    "3-3.unique_values": "v16-1.0",
    "3-3.aggregate": "v16-1.0",
    "AIONA-M.internal_marker": "v16-1.0 (등록 패턴 대조, 표시본 필요)",
    "AIONA-F.forbidden_expression": "v16-1.0 (질문별 등록 목록, 표시본 필요)",
}

#: 입력 안전 한도 (초과는 품질 FAIL 이 아니라 실행 사유)
LIMITS = {
    "question_chars": 100_000,
    "answer_chars": 400_000,
    "displayed_chars": 400_000,
    "json_chars": 1_000_000,
    "python_chars": 200_000,
    "reference_chars": 100_000,
    "batch_cases": 20_000,
}


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def versions() -> Dict[str, Any]:
    return {
        "engine": ENGINE_VERSION,
        "analysis": ANALYSIS_VERSION,
        "extract": EXTRACT_VERSION,
        "script_data": SCRIPT_DATA_VERSION,
        "runtime_unicodedata": unicodedata.unidata_version,
        "md_policy": MD_POLICY_VERSION,
        "explain": "v16-explain-1.0 (고정 템플릿 설명, 판정 불변)",
        "checks": dict(CHECK_VERSIONS),
    }


OPS = {
    "==": lambda a, b: a == b,
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
}


def compare(measured: Any, op: str, required: Any) -> bool:
    return OPS[op](measured, required)


def cmp_text(measured: Any, op: str, required: Any) -> str:
    return f"{measured} {op} {required}"


@dataclass
class Ctx:
    case_id: str
    question: str
    answer: str
    displayed_text: Optional[str] = None
    system_id: Optional[str] = None
    aiona_config: Any = None
    shared: Dict[str, Any] = field(default_factory=dict)

    @cached_property
    def qa(self) -> TextAnalysis:
        return TextAnalysis(self.question)

    @cached_property
    def aa(self) -> TextAnalysis:
        return TextAnalysis(self.answer)

    @cached_property
    def da(self) -> Optional[TextAnalysis]:
        return TextAnalysis(self.displayed_text) if isinstance(self.displayed_text, str) else None

    @cached_property
    def conds(self) -> QuestionConditions:
        return extract_conditions(self.question)

    @cached_property
    def projection(self) -> Tuple[str, List[int]]:
        text, offsets = self.aa.projection
        return nfc(text), offsets


def result(check_id: str, subcategory: str, title: str, source: str, status: str,
           verdict: Optional[str] = None, **kw: Any) -> ConditionResult:
    version = CHECK_VERSIONS.get(check_id.split(":")[0], "v16-1.0")
    evidence = kw.pop("evidence", [])
    ev = [e if isinstance(e, Evidence) else Evidence(**e) for e in evidence]
    return ConditionResult(check_id=check_id, check_version=version, subcategory=subcategory,
                           title=title, source=source, status=status, verdict=verdict,
                           evidence=ev, **kw)


def verdict_of(ok: bool) -> str:
    return PASS if ok else FAIL


def excerpt(text: str, start: int, end: int, margin: int = 25) -> str:
    return text[max(0, start - margin):min(len(text), end + margin)]


def q_evidence(ctx: Ctx, req) -> Evidence:
    return Evidence(target="question", start=req.start, end=req.end, excerpt=req.text)


def unsupported_from_req(ctx: Ctx, req, check_id: str, sub: str, title: str) -> ConditionResult:
    return result(check_id, sub, title, "question", Status.UNSUPPORTED,
                  required=req.value, reason=req.reason or "지원하지 않는 요구 표현",
                  evidence=[q_evidence(ctx, req)])
