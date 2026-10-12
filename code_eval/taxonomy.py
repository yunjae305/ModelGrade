"""화면의 기준 열람 영역(전체 분류 / 판정 방법 / AIONA 특화 평가 / 근거·원문)에 쓰는 정의.

v16 분류·명칭·검사 위치를 그대로 옮긴다. 판정 결과를 담지 않는다 — 결과는 항상 엔진 실행에서 나온다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .config import ROOT
from .core import MD_SHA256, MD_SOURCE
from .jev_applicability import APPLIES_WHEN
from .jev_criteria import describe as jev_describe

__all__ = ["taxonomy", "md_excerpt", "EXAMPLES"]

CATEGORIES: List[Dict[str, Any]] = [
    {"id": "1", "name": "출력 무결성", "tag": "출력 자체의 결함", "question": "출력 자체에 확인 가능한 결함이 있는가?",
     "subs": [
         {"id": "1-1", "name": "응답·문자 정상성", "target": "일반 응답의 비공백 내용, 등록된 깨진 문자·제어 문자, 한자·가나·키릴 등 외국 문자 구간 (코드 안은 제외).",
          "method": "공백·제어문자 제거 후 실질 문자 확인, 등록 문자 규칙 대조. 깨짐 표기·외국 문자는 한 줄이 아니라 질문 주제, 같은 문단·목록 항목·표 행·코드블록 앞뒤 설명, 따옴표·괄호 병기를 보고 설명·예시면 제외.",
          "pass": "응답이 있고, 깨짐 표기·외국 문자가 없거나 모두 질문 주제·주변 설명·예시·병기와 함께 나옴.", "fail": "일반 응답이 비었거나, 설명·예시 없이(또는 실제 출력 결과로) 깨진 문자가 있거나, 외국 문자가 맥락 없이 갑자기 나옴.",
          "basis": "내부 운영 정책 (응답 존재·문자 규칙). 명시 빈 출력 요청은 적용 예외로 기록.", "trace": "code_eval/checks_integrity.py · code_eval/textparse.py"},
         {"id": "1-2", "name": "문서·코드 유효성", "target": "요구한 코드블록의 존재·언어 표지·개수·구획 표시 수, 정상 JSON·완성 Python 소스, 짝이 없는 닫는 태그, 코드·수식 구획.",
          "method": "코드 구조를 한 번 읽어 질문 요구·구획 정책·구문을 각각 비교. JSON 은 RFC 8259 엄격 파싱, Python 은 ast.parse+compile 만 (실행·import 없음).",
          "pass": "실제로 검사한 코드 요구·구문·표시 조건 충족.", "fail": "요구 코드블록 부재·개수·언어 표지 불일치, 구문 오류, 구획·태그 조건 위반.",
          "basis": "질문 원문 · RFC 8259 · Python compile · MD 165·169·170행 표시 정책. 코드 구획 표시 < 2 와 (코드 홀수 OR 수식 홀수)는 서로 다른 비교식.",
          "trace": "code_eval/checks_integrity.py · code_eval/textparse.py (_fenced_code_blocks)"},
         {"id": "1-3", "name": "참조 연결 유효성", "target": "실제 사용한 각주·인용 번호·문서 내부 링크와 정의·대상.",
          "method": "사용 ID·정의 ID·링크 목적지 추출 → 정의 누락·중복·빈 정의·연결 불일치 대조. 외부 URL 접속·출처 사실성 검사 없음.",
          "pass": "적용된 연결 조건 충족.", "fail": "정의 누락·중복·빈 정의 또는 잘못된 연결.",
          "basis": "질문의 참조 요구와 각주·인용 번호·내부 링크의 연결 규칙.", "trace": "code_eval/checks_integrity.py · code_eval/textparse.py"},
     ]},
    {"id": "2", "name": "요구 조건 준수", "tag": "분량·형식·구성", "question": "질문의 명시 조건과 적용이 확정된 형식 규칙을 지켰는가?",
     "subs": [
         {"id": "2-1", "name": "분량·표현 조건", "target": "불릿·목록·문장·문단·줄·글자·어절 수, 필수·금지 문자열, 시작·끝 표현, 허용 문자, 명시 빈 출력.",
          "method": "질문에서 조건값·비교 연산·대상을 코드로 추출 → 고정 단위로 측정 → 대조. ‘정도·약’ 같은 근사 요구는 엄격 비교하지 않음.",
          "pass": "실제로 검사한 명시 조건 충족.", "fail": "확정된 비교식·문자열 조건 위반.",
          "basis": "IFEval 의 검증 가능 지시 접근, 텍스트 측정 규칙. MD 163·166행(불릿·문장 수)을 이 소분류에 연결.", "trace": "code_eval/checks_requirements.py · code_eval/textparse.py"},
         {"id": "2-2", "name": "형식·구성 조건", "target": "요구한 표·목록·제목, 표의 열 이름·순서·헤더·명시 행 수, JSON 최상위 자료형·필수 키·필드 자료형.",
          "method": "질문의 명시 요구만 추출 → 구조를 한 번 읽어 비교. 요구 없는 고정 최소 행 수는 적용하지 않음 (header_rows·data_rows·total_rows 는 관측). "
                    "YAML·JSON·CSV·HTML·SQL·Python 등 코드·데이터 결과물을 요청한 질문에서 결과물 안을 가리키는 목록·표·제목 요구는 Markdown 구조로 판정하지 않음(미판정). "
                    "불릿·번호 목록·마크다운 표처럼 Markdown 전용 표현이나 설명 부분을 정리하라는 요구는 판정.",
          "pass": "실제로 검사한 표·목록·제목·필드·명시 개수 조건 충족.", "fail": "요구 구조 부재 또는 명시된 열·헤더·행 수·필드·자료형 위반.",
          "basis": "질문 원문 · JSON Schema 개념 · GFM 표 문법. 코드블록 조건은 1-2 에서만 검사.", "trace": "code_eval/checks_requirements.py"},
     ]},
    {"id": "3", "name": "수치·데이터 정합성", "tag": "계산·데이터 불일치", "question": "직접 계산하거나 대조할 수 있는 값이 맞는가?",
     "subs": [
         {"id": "3-1", "name": "계산 결과 일치", "target": "질문이 요구한 계산값(독립 계산), 답변이 주장한 직접 산술 관계(재계산).",
          "method": "Fraction 정확 계산 · 재귀 하강 파서 (eval/exec 없음). 답변 값은 라벨·구조로 연결하고 기대값과 닮았다는 이유로 고르지 않음.",
          "pass": "재계산값과 연결된 출력값 일치.", "fail": "확정된 계산 관계의 불일치.",
          "basis": "수학적 연산과 질문의 명시 수치 관계. 등식 자체의 정합과 질문 요구 계산은 별도 조건.", "trace": "code_eval/checks_consistency.py · code_eval/arith.py"},
         {"id": "3-2", "name": "선언 개수 일치", "target": "‘다음 세 가지’처럼 답변이 선언한 개수와 바로 이어지는 목록·표(데이터 행)·소제목.",
          "method": "수사 정규화 → 구조 경계를 선언과 독립으로 확정 → 항목 수 대조. 예시·부분집합 소개는 제외.",
          "pass": "선언 수와 실제 수 일치.", "fail": "같은 범위의 확정된 개수 불일치.",
          "basis": "선언값과 관측값의 정확 비교, 목록 경계 규칙.", "trace": "code_eval/checks_consistency.py · code_eval/structure_counts.py"},
         {"id": "3-3", "name": "원본 보존·변환 일치", "target": "질문의 원본 표·목록·JSON 과 명시 정렬·필터·중복 제거·이름 변경·집계, 명시 고유성.",
          "method": "원본에 명시 연산을 적용한 비교값 생성 → 출력과 자료형 구분 정확 비교 (0·false·빈 문자열·null·누락 구분).",
          "pass": "적용 보존·고유성·변환 조건 일치.", "fail": "값·자료형·순서·중복·건수 오류.",
          "basis": "질문 원본과 연산 정의, 정확 비교.", "trace": "code_eval/checks_consistency.py"},
     ]},
]

SPECIALIZED_ITEMS = [
    {"id": "AIONA-M", "name": "내부 마커 노출", "md_line": 155,
     "condition": "시각화 자리표시자 · 일반 자리표시자 · 내부 구간 태그 · 도구/아티팩트 태그 · 후속질문 태그 · 인용 번호 2개 이상 연속 잔재 중 하나라도 본문에 남아 있음",
     "method": "등록 문자열·패턴을 실제 표시본(코드 제외·강조 제거)과 대조. 여섯 범주가 모두 등록돼야 PASS 가능.",
     "requires": "실제 마커 목록 (code_eval/settings/aiona_service_rules_v1.json u8_1) · 실제 표시본 displayed_text"},
    {"id": "AIONA-F", "name": "금지 표현 출현", "md_line": 156,
     "condition": "그 질문에 대해 미리 정의한 \"나오면 안 되는 표현\"이 본문에 존재 (환각 카나리아)",
     "method": "case_id + 정확한 질문 대응으로 등록 목록을 연결하고 표시본에서 문자열 출현 검사. 부정문도 단순 출현으로 셈.",
     "requires": "질문별 금지 표현 목록 (data/aiona_specialized/forbidden_expressions.json) · 실제 표시본"},
]

REFERENCES = [
    {"name": "IFEval · Zhou et al. (2023)", "url": "https://arxiv.org/abs/2311.07911", "scope": "명시 분량·키워드·형식 조건의 코드 검증 접근"},
    {"name": "RFC 8259", "url": "https://www.rfc-editor.org/rfc/rfc8259.html", "scope": "JSON 구문"},
    {"name": "Python · compile", "url": "https://docs.python.org/3/library/functions.html#compile", "scope": "실행 없는 구문·컴파일 확인"},
    {"name": "JSON Schema Validation 2020-12", "url": "https://json-schema.org/draft/2020-12/json-schema-validation", "scope": "자료형·필수 속성 개념"},
    {"name": "CommonMark 0.31.2", "url": "https://spec.commonmark.org/0.31.2/", "scope": "Markdown 구조 해석·코드 펜스"},
    {"name": "Unicode UAX #24 Script Property", "url": "https://www.unicode.org/reports/tr24/tr24-41.html", "scope": "문자 종류 측정 (언어·맥락 판정 아님)"},
]

#: 화면의 ‘예시로 실행’ 버튼용 합성 입력. 결과는 항상 엔진이 새로 계산한다.
EXAMPLES = [
    {"label": "표 · 데이터 행 수 요구 (2-2)", "question": "할 일 목록을 표로 작성해줘. 열 이름은 순서대로 ‘구분’, ‘설명’이고, 헤더를 제외한 데이터 행은 정확히 3개로 해줘.",
     "answer": "| 구분 | 설명 |\n|---|---|\n| 준비 | 자료 수집 |\n| 진행 | 내용 정리 |"},
    {"label": "행 수 요구 없음 · 데이터 1행 표 (2-2)", "question": "할 일과 시간을 표로 정리해줘. 할 일은 자료 정리이고, 시간은 오전 10시야.",
     "answer": "| 할 일 | 시간 |\n|---|---|\n| 자료 정리 | 오전 10시 |"},
    {"label": "코드블록 + Python 구문 (1-2)", "question": "1을 출력하는 완성된 Python 코드를 코드블록 정확히 1개로 작성해줘.",
     "answer": "```python\nprint(1\n```"},
    {"label": "펜스 불균형 (1-2)", "question": "덧셈 예제를 코드 구획과 수식 구획으로 각각 보여줘.",
     "answer": "```python\nprint(1 + 1)\n```\n\n$$\n1 + 1 = 2\n"},
    {"label": "불릿 개수 (2-1)", "question": "공부 방법을 불릿으로 정확히 3개 작성해줘.", "answer": "- 개념을 정리하세요.\n- 문제를 풀어보세요."},
    {"label": "문장 수 (2-1)", "question": "이용 안내를 정확히 2문장으로 작성해줘.",
     "answer": "시작 버튼을 누르세요. 안내에 따라 진행하세요. 완료 버튼을 누르세요."},
    {"label": "질문 계산 vs 답변 등식 (3-1)", "question": "단가 2,000원·수량 3개의 총액을 알려줘.", "answer": "2,000 + 3 = 2,003"},
    {"label": "선언 개수 (3-2)", "question": "공부 방법을 알려줘.", "answer": "다음 세 가지를 추천합니다.\n\n1. 개념 정리\n2. 문제 풀이\n3. 오답 복습\n4. 휴식"},
    {"label": "설명 없는 외국 문자 (1-1)", "question": "파일 이름을 바꾸는 방법을 알려줘.",
     "answer": "파일을 선택하세요. こんにちは. 이름을 변경하세요."},
    {"label": "짝이 없는 닫는 태그 (1-2)", "question": "회의 시작 시간을 한 문장으로 알려줘.", "answer": "회의는 오후 2시에 시작합니다. </result>"},
]

_MD_LINES = (134, 136, 137, 141, 143, 145, 146, 147, 148, 149, 155, 156, 163, 164, 165, 166, 169, 170, 187, 189, 190, 191, 192, 193)


def md_excerpt() -> Dict[str, Any]:
    path = ROOT / MD_SOURCE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {"source": MD_SOURCE, "error": str(exc), "lines": []}
    return {"source": MD_SOURCE, "sha256": MD_SHA256,
            "lines": [{"line": n, "text": lines[n - 1], "retired": n == 164} for n in _MD_LINES if n <= len(lines)],
            "retired_note": "164행 ‘형식 위반 — 표: 표 행이 2개 미만’은 사용자 결정으로 폐기됨. 이력으로만 표시하며 판정·집계·설정에 쓰지 않음.",
            "display_name_changes": [{"line": 169, "source_name": "고아 닫는 태그", "display_name": "짝이 없는 닫는 태그"}]}


def _jev_with_applicability() -> Dict[str, Any]:
    jev = jev_describe()
    for c in jev["criteria"]:
        c["applies_when"] = APPLIES_WHEN[c["id"]]
        if c["id"] == "J5":
            c["applies_when"] = ("CLI/API 평가는 모든 답변에서 사실 주장을 선별합니다. 사실이 없으면 해당 없음, "
                                 "검증 근거가 끝까지 부족하면 검증 기준 미충족 FAIL이며 사실 오류 확정과 구분합니다. "
                                 "Native Jev는 답변의 수치·날짜·통계 또는 참조 자료 규칙을 사용합니다.")
    return jev


def taxonomy() -> Dict[str, Any]:
    return {"categories": CATEGORIES, "specialized": SPECIALIZED_ITEMS, "references": REFERENCES,
            "examples": EXAMPLES, "md": md_excerpt(), "jev": _jev_with_applicability(),
            "principles": [
                "객관적 검증은 코드 검사, 의미 품질은 설정된 평가자가 여섯 기준으로 판단합니다. "
                "J1은 모든 질문, J2~J4는 해당 요구가 있는 질문, J6는 앞 대화가 있는 2번째 턴부터 적용합니다. "
                "CLI/API의 J5는 모든 답변에서 사실 주장을 선별하고 참조 자료나 웹 출처를 확인합니다.",
                "코드·의미 기준별 결과와 질문별 종합 결과를 함께 표시합니다. 코드에서 확정한 판정은 유지하고, 공통 미판정 조건은 의미 평가로 보완할 수 있습니다.",
                "확인한 항목은 PASS/FAIL입니다. 검증 근거가 끝까지 부족한 항목은 검증 기준 미충족 FAIL로 확정하고 사실 오류 확정과 구분합니다. "
                "미적용·설정 없음·호출 오류는 품질 FAIL로 바꾸지 않습니다.",
                "기준 통과율 = PASS / (PASS + FAIL). 확인 필요는 분모에서 제외하므로 완료 건수와 함께 읽습니다. 질문 통과점수는 모든 적용 기준을 통과한 질문 수 / 전체 질문 수입니다.",
                "표 고정 최소 행 수 규칙 없음 — 질문이 명시한 행 수만 비교",
                "깨진 문자·외국 문자는 질문 주제나 주변 설명·예시·괄호 병기가 있으면 PASS, 없이 갑자기 나오면 FAIL",
                "AIONA 특화 결과는 공통 통과율에 더하지 않음",
            ]}
