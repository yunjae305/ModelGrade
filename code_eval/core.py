"""검사 공용 도구: 실행 문맥, 버전, 결과 생성, 비교식."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .analysis import ANALYSIS_VERSION, TextAnalysis
from .extract import EXTRACT_VERSION, QuestionConditions, extract_conditions
from .model import ConditionResult, Evidence, FAIL, PASS
from .scripts import SCRIPT_DATA_VERSION

#: v16.3.0 은 두 작업 줄에서 서로 다른 규칙에 쓰였다 — 이전 턴 지시 승계(fix/evaluation-reliability 21322b1)와
#: 공통 행 PASS/FAIL 전용(main 9bf9a37). 두 줄을 합친 이 버전부터 새 번호를 쓴다 (MEASUREMENT_v16.md §20 버전 이력).
ENGINE_VERSION = "aiona-code-eval-v16.4.0"
MD_SOURCE = "참고자료/채팅품질 평가기준.md"
MD_SHA256 = "fb7126e7a31073745517273a1dfdfea6508cb3af2454cca978cc88055eb182dd"
MD_POLICY_VERSION = f"채팅품질 평가기준(8).md sha256:{MD_SHA256[:12]}"

#: 검사별 규칙 버전. 결과마다 남긴다.
CHECK_VERSIONS: Dict[str, str] = {
    "1-1.response_nonempty": "v16-1.1 (비내용 문자 제거 후 빈 응답 확인, 빈 출력 명시 요청 시 행 없음)",
    "1-1.character_rules": "v16-1.2 (깨짐 서명 + C0 제어 문자, 질문 주제·주변 설명·예시 맥락 구분)",
    "1-1.foreign_script": "v16-1.1 (질문 주제·주변 설명·괄호 병기가 없는 외국 문자, 허용 문자 조건 시 행 없음)",
    "1-2.code_block": "v16-1.2 (질문 요구, CommonMark 펜스 파서, 목록 항목 안 펜스, 근사 개수·지원 밖 요구는 행 없음)",
    "1-2.code_language_label": "v16-1.1 (info string 첫 낱말, 코드블록 요구 FAIL 뒤 행 없음, 요구 없이 블록 0개면 FAIL)",
    "1-2.code_fence_min": "v16-1.1 (MD 165행, 코드블록 존재·개수 FAIL 뒤 행 없음)",
    "1-2.fence_imbalance": "v16-1.0 (MD 170행, 코드 홀수 OR 수식 홀수)",
    "1-2.json_syntax": "v16-1.1 (RFC 8259 엄격 파싱, NaN/Infinity 거부, 오류 예시·jsonc·부분 JSON 은 행 없음, 한도 초과는 실행 오류)",
    "1-2.python_syntax": "v16-1.2 (ast.parse+compile, 실행·import 없음, 펜스 들여쓰기를 뺀 내용, 오류 예시·발췌 코드는 행 없음, 한도 초과는 실행 오류)",
    "1-2.orphan_closing_tag": "v16-1.0 (MD 169행, 여는 태그 대응 스택)",
    "1-3.footnote_link": "v16-1.2 (코드 예시 제외, 각주 파서·빈 정의, 지원 밖 정의는 행 없음)",
    "1-3.citation_link": "v16-1.2 (코드 예시 제외, 인용 번호 파서, 출처 목록 문법 없으면 행 없음)",
    "1-3.internal_link": "v16-1.2 (코드 예시 제외, 실제 본문 제목·앵커 연결, 대상 목록 없음·ID 규칙 차이는 행 없음)",
    "2-1.explicit_empty": "v16-1.0",
    "2-1.count": "v16-1.6 (질문 출력 범위·번호 제목 연결, 펜스 길이에 따른 코드 제외, 모호한 구조·결과물 내부는 행 없음, CommonMark 형제 항목 들여쓰기, 인용문 안 목록, 'N개 열'은 항목 수 아님)",
    "2-1.char_count": "v16-1.2 (코드 포인트, 펜스 길이에 따른 코드 제외·줄바꿈 제외, 근사 분량은 행 없음)",
    "2-1.literal": "v16-1.3 (코드 제외·강조 제거 투영, NFC, 정확 일치 요구가 아니면 공백·줄바꿈 접기, 정확 일치 수식어 여러 개·문구와 동사 사이 수식어 인식, 항목별 요구는 행 없음)",
    "2-1.charset": "v16-1.0 (문자 범주 L*)",
    "2-2.table": "v17-1.4 (명시 표 개수 추가, 고정 최소 행 수 없음, 빈 헤더 줄은 헤더 아님, 코드·데이터 결과물 안의 표는 판정 제외, 양끝 파이프 생략·인용문 안 GFM 표, 표 존재 FAIL 뒤 열·헤더·행 수 행 없음)",
    "2-2.list": "v16-1.4 (코드·데이터 결과물 안의 목록·자료형 목록·결과물 안인지 불확정·마크다운 금지 충돌은 조건 미생성 행 없음, 인용문 안 목록 인식)",
    "2-2.heading": "v16-1.2 (ATX 제목, 코드·데이터 결과물 안의 제목은 판정 제외, 굵은 글씨 줄만·단계 여러 개는 행 없음)",
    "2-2.json_structure": "v16-1.1 (JSON 구문 FAIL·출력 없음 FAIL 뒤 필드 행 없음, 대상 여러 개·필수 목록 밖 키 없음은 행 없음)",
    "3-1.question_calculation": "v16-1.2 (Fraction 정확 계산, 질문 피연산자·답변 등식 결과 부호 보존, eval 없음, 값 연결 실패는 행 없음)",
    "3-1.answer_equation": "v16-1.4 (숫자 제곱·괄호 곱셈, 실수 단어 경계, 백분율 접미사 구분, 식은 줄을 넘지 않고 '=' 줄머리 유도식만 이어 읽음, 괄호 짝·TeX 구분자·연산자, 등록 단위 확장(장·권·자루 등)·1인당·연결 어미, 명사 부정은 같은 문장 앞부분만 보류, 보류 등식은 행 없음)",
    "3-2.declared_count": "v16-1.4 (연속 번호 제목·목록 구조 경계, 도입 설명·항목 설명 줄 보존, CommonMark 형제 항목 들여쓰기, 부분집합·구조 없음은 행 없음)",
    "3-3.transform": "v17-1.4 (펜스·본문 원본 후보 통합, 코드·함수 작성 요구 비활성, 원본 없음·여러 개는 조건 미생성, 형식 변환 요구 조건 없음, 결과 구조 없음·연산 적용 불가는 행 없음)",
    "3-3.unique_values": "v16-1.2 (코드·함수 작성 요구 비활성, 원본 있을 때만 조건 생성, 결과 구조 없음은 행 없음)",
    "3-3.aggregate": "v16-1.2 (코드·함수 작성 요구 비활성, 원본 없음·여러 개는 조건 미생성, 값 연결 불가는 행 없음)",
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
    inherited: List[Any] = field(default_factory=list)   # 이전 턴에서 이어받은 형식·분량 조건 (Req)
    #: 검사기 실행 오류·한도 초과 (결과 행이 아니라 사례의 errors 로 나감)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    #: 판정하지 않은 조건의 사유 (결과 행이 아니라 meta.skipped_conditions 로 나감, 집계·해시 제외)
    skipped: List[Dict[str, Any]] = field(default_factory=list)

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
        conds = extract_conditions(self.question)
        conds.reqs.extend(self.inherited)
        return conds

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
    return Evidence(**req.evidence())   # 이어받은 지시는 target=history (이전 턴 위치는 쓰지 않음)


#: 사례 errors 의 kind 값. Status 상수와 같은 문자열이지만 결과 행의 status 가 아니다.
ERROR_LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
ERROR_CHECK_ERROR = "CHECK_ERROR"


def case_error(ctx: Ctx, check_id: str, sub: str, kind: str, message: str, **details: Any) -> None:
    """검사기 자체 오류·입력 한도 초과를 결과 행이 아니라 사례 단위 실행 오류로 남긴다 (품질 FAIL 아님)."""
    ctx.errors.append({"check_id": check_id, "subcategory": sub, "kind": kind, "message": message, "details": details})


def skip_condition(ctx: Ctx, check_id: str, sub: str, reason: str) -> None:
    """판정할 수 없는 조건의 사유만 남긴다. 결과 행을 만들지 않으며 집계·결과 해시에 들어가지 않는다."""
    ctx.skipped.append({"check_id": check_id, "subcategory": sub, "reason": reason})


def skip_req(ctx: Ctx, req, check_id: str, sub: str) -> None:
    skip_condition(ctx, check_id, sub, req.reason or "지원하지 않는 요구 표현")
