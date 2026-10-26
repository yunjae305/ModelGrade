"""검사 공용 도구: 실행 문맥, 버전, 결과 생성, 비교식."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Dict, List, Optional, Tuple

from .analysis import ANALYSIS_VERSION, DisplayedTextAnalysis, TextAnalysis
from .extract import EXTRACT_VERSION, QuestionConditions, extract_conditions
from .model import ConditionResult, Evidence, FAIL, PASS
from .scripts import SCRIPT_DATA_VERSION

#: v16.3.0 은 두 작업 줄에서 서로 다른 규칙에 쓰였다 — 이전 턴 지시 승계(fix/evaluation-reliability 21322b1)와
#: 공통 행 PASS/FAIL 전용(main 9bf9a37). 두 줄을 합친 이 버전부터 새 번호를 쓴다 (MEASUREMENT_v16.md §23 버전 이력).
#: v16.5.0 — 답변 구조에서 생기는 검사(ANSWER_UNITS)는 답변마다 조건 1개로 집계하고, 대상이 없으면 '검사 대상 없음' PASS 행을 둔다 (§26).
#: v16.6.0 — 질문 속 붙여 넣은 자료([붙여 넣은 자료]…[자료 끝] 줄)는 지시가 아니다: 요구 조건·지속 지시·작업명은 요청 부분에서만 (§27).
#: v16.7.0 — 1-1 응답 존재 → 에러 케이스 없음: 빈 답 · 답 없음(수집 오류) · 능력 부정 정형 문구만을 FAIL, 복사 실패는 행 없이 보류 (10-25, 미팅 10-23 2 · 3번)
ENGINE_VERSION = "aiona-code-eval-v16.8.0"   # v16.8.0: 에러 케이스 대분류(0-1) — 1-1.response_nonempty · 2-1.explicit_empty 소분류 이동(10-26)
MD_SHA256 = "fb7126e7a31073745517273a1dfdfea6508cb3af2454cca978cc88055eb182dd"
MD_POLICY_VERSION = f"채팅품질 평가기준(8).md sha256:{MD_SHA256[:12]}"

#: 검사별 규칙 버전. 결과마다 남긴다.
CHECK_VERSIONS: Dict[str, str] = {
    "1-1.response_nonempty": "v16-1.6 (에러 케이스: 빈 답 · 답 없음(수집 오류) · 능력 부정 정형 문구만 · 서비스 오류 정형 문구만 — 10-25; 번역 · 인용 요청 건너뛰기는 붙여 넣은 자료 밖 지시문만 — 10-25 밤; ③ 문구에 '프로그래밍 범위를 넘어 · 텍스트를 생성할 뿐' — 10-25 밤; 빈 출력 명시 요청 시 행 없음, 복사 실패는 행 없음)",
    "1-1.character_rules": "v16-1.2 (깨짐 서명 + C0 제어 문자, 질문 주제·주변 설명·예시 맥락 구분)",
    "1-1.foreign_script": "v16-1.1 (질문 주제·주변 설명·괄호 병기가 없는 외국 문자, 허용 문자 조건 시 행 없음)",
    "1-2.code_block": "v16-1.2 (질문 요구, CommonMark 펜스 파서, 목록 항목 안 펜스, 근사 개수·지원 밖 요구는 행 없음)",
    "1-2.code_language_label": "v16-1.2 (info string 첫 낱말, 빈 표지는 통과·다른 언어만 FAIL(10-21), 코드블록 요구 FAIL 뒤 행 없음, 요구 없이 블록 0개면 FAIL)",
    "1-2.code_fence_min": "v16-1.1 (MD 165행, 코드블록 존재·개수 FAIL 뒤 행 없음)",
    "1-2.fence_imbalance": "v16-1.0 (MD 170행, 코드 홀수 OR 수식 홀수)",
    "1-2.json_syntax": "v16-1.1 (RFC 8259 엄격 파싱, NaN/Infinity 거부, 오류 예시·jsonc·부분 JSON 은 행 없음, 한도 초과는 실행 오류)",
    "1-2.python_syntax": "v16-1.2 (ast.parse+compile, 실행·import 없음, 펜스 들여쓰기를 뺀 내용, 오류 예시·발췌 코드는 행 없음, 한도 초과는 실행 오류)",
    "1-2.orphan_closing_tag": "v16-1.0 (MD 169행, 여는 태그 대응 스택)",
    "1-3.footnote_link": "v16-1.2 (코드 예시 제외, 각주 파서·빈 정의, 지원 밖 정의는 행 없음)",
    "1-3.citation_link": "v16-1.2 (코드 예시 제외, 인용 번호 파서, 출처 목록 문법 없으면 행 없음)",
    "1-3.internal_link": "v16-1.2 (코드 예시 제외, 실제 본문 제목·앵커 연결, 대상 목록 없음·ID 규칙 차이는 행 없음)",
    "2-1.explicit_empty": "v16-1.0",
    "2-1.count": "v16-1.7 (질문 출력 범위·번호 제목 연결, 펜스 길이에 따른 코드 제외, 모호한 구조·결과물 내부는 행 없음, CommonMark 형제 항목 들여쓰기, 인용문 안 목록, 'N개 열'은 항목 수 아님, 문장 끝 뒤 강조 닫기(**·__) 허용)",
    "2-1.char_count": "v16-1.3 (코드 포인트, 펜스 길이에 따른 코드 제외·줄바꿈 제외, 근사 분량은 행 없음; v16-1.3 ‘공백 포함 N자’·‘글자 수는 …N자여야’ 를 글자 수 조건으로 인식, 세는 방식이 명시된 ‘N자로’ 는 정확히 N자)",
    "2-1.literal": "v16-1.3 (코드 제외·강조 제거 투영, NFC, 정확 일치 요구가 아니면 공백·줄바꿈 접기, 정확 일치 수식어 여러 개·문구와 동사 사이 수식어 인식, 항목별 요구는 행 없음)",
    "2-1.charset": "v16-1.0 (문자 범주 L*)",
    "2-2.table": "v17-1.4 (명시 표 개수 추가, 고정 최소 행 수 없음, 빈 헤더 줄은 헤더 아님, 코드·데이터 결과물 안의 표는 판정 제외, 양끝 파이프 생략·인용문 안 GFM 표, 표 존재 FAIL 뒤 열·헤더·행 수 행 없음)",
    "2-2.list": "v16-1.4 (코드·데이터 결과물 안의 목록·자료형 목록·결과물 안인지 불확정·마크다운 금지 충돌은 조건 미생성 행 없음, 인용문 안 목록 인식)",
    "2-2.heading": "v16-1.2 (ATX 제목, 코드·데이터 결과물 안의 제목은 판정 제외, 굵은 글씨 줄만·단계 여러 개는 행 없음)",
    "2-2.json_structure": "v16-1.1 (JSON 구문 FAIL·출력 없음 FAIL 뒤 필드 행 없음, 대상 여러 개·필수 목록 밖 키 없음은 행 없음)",
    "3-1.question_calculation": "v16-1.3 (Fraction 정확 계산, 질문 피연산자·답변 등식 결과 부호 보존, eval 없음, 값 연결 실패는 행 없음, 등식 연결 먼저 · 질문이 따로 합계를 요구하면 '합계'·'총' 라벨 제외 — 10-21)",
    "3-1.answer_equation": "v16-1.6 (숫자 제곱·괄호 곱셈, 실수 단어 경계, 백분율 접미사 구분, 식은 줄을 넘지 않고 '=' 줄머리 유도식만 이어 읽음, 괄호 짝·TeX 구분자·연산자, 등록 단위 확장(장·권·자루 등)·1인당·연결 어미, 명사 부정은 같은 문장 앞부분만 보류, 보류 등식은 행 없음, 좌변 앞 화살표·연산자와 '/ 한도(이하·이상)' 표기는 보류; v16-1.6 좌변 앞 말로 쓴 식('7개 × 상자마다 구슬 6개 = 7 × 6 = 42')은 첫 구간만 떼고 나머지 재계산)",
    "3-2.declared_count": "v16-1.5 (연속 번호 제목·목록 구조 경계, 도입 설명·항목 설명 줄 보존, CommonMark 형제 항목 들여쓰기, 부분집합·구조 없음·묻는 문장(…까요?)은 행 없음)",
    "3-3.transform": "v17-1.4 (펜스·본문 원본 후보 통합, 코드·함수 작성 요구 비활성, 원본 없음·여러 개는 조건 미생성, 형식 변환 요구 조건 없음, 결과 구조 없음·연산 적용 불가는 행 없음)",
    "3-3.unique_values": "v16-1.2 (코드·함수 작성 요구 비활성, 원본 있을 때만 조건 생성, 결과 구조 없음은 행 없음)",
    "3-3.aggregate": "v16-1.2 (코드·함수 작성 요구 비활성, 원본 없음·여러 개는 조건 미생성, 값 연결 불가는 행 없음)",
    "AIONA-M.internal_marker": "v16-1.0 (등록 패턴 대조, 표시본 필요)",
    "AIONA-F.forbidden_expression": "v16-1.0 (질문별 등록 목록, 표시본 필요)",
}

#: 답변 구조에서 생기는 검사 단위 (criteria_registry.CODE_UNITS 의 적용 계기 answer·question_or_answer).
#: 질문이 같으면 모델마다 조건 수가 같아야 하므로(사용자 결정, v16.5.0) 이 단위는 답변마다 조건 1개로 센다 —
#: 대상(코드블록·등식·각주…)이 몇 개든 하나라도 FAIL 이면 FAIL, 모두 통과하면 PASS, 대상이 없으면 '검사 대상 없음' PASS 행.
#: 값: (소분류, 결과 행 제목, source, 대상 이름)
ANSWER_UNITS: Dict[str, Tuple[str, str, str, str]] = {
    "1-2.fence_imbalance": ("1-2", "펜스 불균형", "md_policy", "코드·수식 구획 표시"),
    "1-2.json_syntax": ("1-2", "JSON 구문", "format_spec", "JSON 코드블록·본문 JSON"),
    "1-2.python_syntax": ("1-2", "Python 구문", "format_spec", "Python 코드블록"),
    "1-2.orphan_closing_tag": ("1-2", "짝이 없는 닫는 태그", "md_policy", "닫는 HTML 태그"),
    "1-3.footnote_link": ("1-3", "각주 ↔ 정의 연결", "answer_self", "각주 표시"),
    "1-3.citation_link": ("1-3", "인용 번호 ↔ 출처 목록", "answer_self", "인용 번호"),
    "1-3.internal_link": ("1-3", "내부 링크 ↔ 대상", "answer_self", "문서 내부 링크"),
    "3-1.answer_equation": ("3-1", "답변 등식 재계산", "answer_self", "등식(계산식)"),
    "3-2.declared_count": ("3-2", "선언 개수 ↔ 구조", "answer_self", "개수 선언 문장"),
}
#: 검사 대상이 없을 때 두는 행의 check_id 접미사
ABSENT_SUFFIX = ":none"

#: 입력 안전 한도 (초과는 품질 FAIL 이 아니라 실행 사유)
LIMITS = {
    # 질문·참조 자료 300만 자 — J7 롱컨텍스트(입력 272K 토큰 이상, 경계 시험 250K~500K, 영어 약 5자/토큰)가 CODE 입력 검증에서 막히지 않도록
    # (2026-10-13, 예전 10만 자). 200만 자 질문의 CODE 검사는 약 5초.
    "question_chars": 3_000_000,
    "answer_chars": 400_000,
    "displayed_chars": 400_000,
    "json_chars": 1_000_000,
    "python_chars": 200_000,
    "reference_chars": 3_000_000,
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
    #: 수집 상태(SUCCESS · ERROR · COPY_FAILED …)와 수집 오류 글 — 1-1 에러 케이스(답 없음) 판정용 (10-25). 없으면 None
    collection_status: Optional[str] = None
    collection_error: Optional[str] = None
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
        return DisplayedTextAnalysis(self.displayed_text) if isinstance(self.displayed_text, str) else None

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
