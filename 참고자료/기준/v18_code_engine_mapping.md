# v18 설계 ↔ 실제 CODE 엔진 연결 현황

기준 문서는 같은 폴더의 `AIONA_코드평가_대분류_v18_문자맥락판정.html`이다. 이 연결표는 **현재 실행 코드의 지원 범위**를 기록한다. 옛 검사기와 V2 코드는 현재 엔진이 아니며 저장소에서 삭제했다 (2026-10-02). 기계 판독용 상세 목록은 [`v18_code_engine_mapping.csv`](v18_code_engine_mapping.csv)이다.

## 실제 실행 경로

| 단계 | 현재 코드 | 확인한 동작 |
|---|---|---|
| CLI·입력 | `code_eval/__main__.py`, `code_eval/io.py` | `run`이 JSON/JSONL의 `question`·`answer`를 읽고 `evaluate_batch` 호출 |
| 공통 문맥·조건 | `code_eval/core.py`, `code_eval/extract.py`, `code_eval/analysis.py` | 질문 조건 추출과 답변 구조 분석을 공유 |
| 검사 선택 | `code_eval/engine.py:_COMMON` | 모든 질문을 1-1~3-3 그룹에 보냄. 각 그룹이 적용 조건이 있을 때 결과 행을 생성 |
| 공통 checker | `checks_integrity.py`, `checks_requirements.py`, `checks_consistency.py` | 결과별 `check_id`·`status`·`verdict`·이유·근거 생성 |
| AIONA 특화 | `checks_aiona.py`, `config.py` | `system_id=aiona`에 적용. 표시본·운영 패턴·질문별 금지 목록이 없으면 미판정 |
| 판정·오류 격리 | `engine.py:_run_group`, `model.py:ConditionResult` | checker 그룹 예외는 `CHECK_ERROR`로 기록. verdict는 PASS/FAIL/null만 허용 |
| 배치·저장 | `engine.py:evaluate_batch`, `__main__.py`, `io.py` | `results.json`·`results.jsonl`·`report.html` 저장 |
| LLM 재검토 | `llm_review.py`, `llm_client.py` | CODE 평가 직후 FAIL 질문만 LLM 재검토. 코드 판정은 바꾸지 않음 |
| 평가 입력 | `data/code_eval/AIONA_winz_set2_Q100_id_question_answer_v1.json` | Q001~Q100 전체 100건 (`system_id: aiona`). 입력 순서대로 평가하며 샘플링하지 않음 |

답변 수집 도구(`e2e/`)와 V2 canonical U/T/S 경로는 v18의 3대분류·8소분류 CODE 엔진이 아니어서 2026-10-02에 삭제했다.

## 구현 상태

CSV의 30개 검사 단위 기준: **구현됨 7, 부분 구현 21, 설정 필요 2, 미구현 0**. `부분 구현`은 지원 문형·구조가 들어오면 실제 판정하지만 임의 질문·모든 형식을 처리하지는 못한다는 뜻이다. 함수 존재만으로 구현됨이라 쓰지 않았다.

| 구분 | 조사 결과 |
|---|---|
| A. 문서·구현 모두 있음 | 1-1~3-3의 명시 CODE 검사와 AIONA 특화 checker가 실제 엔진에 연결됨. 지원 조건에서는 PASS/FAIL 생성 |
| B. 문서에는 있으나 구현 없음 | v18 운영 탭의 **항목별 사전 CODE/LLM 배분**은 현재 엔진에 없다. 대신 모든 질문을 CODE로 평가한 뒤 FAIL 질문만 LLM 재검토한다. 확정된 8개 CODE 소분류 자체의 그룹 함수가 빠진 곳은 확인되지 않음 |
| C. 구현은 있으나 v18 분류 연결 불명확 | 이 `code_eval` 엔진의 공통 결과에는 `subcategory`가 있어 8개 소분류에 연결됨. `table_rows`는 관측값이며 품질 criterion이 아님. 외국 문자 구간 기록(`foreign_script_spans`)은 `1-1.foreign_script` 판정의 근거로 쓴다. Legacy·V2 구현은 삭제됨 |
| D. 동작 차이 | 현재 엔진은 모든 질문에 같은 CODE 그룹을 호출한 뒤 각 그룹에서 적용 여부를 결정한다. v18의 사전 평가 방법 지정/선택 체계는 아직 없음. 실제 입력에서 조건 추출·중복 FAIL·오탐은 결과 검토 대상으로 남김 |
| E. 별도 입력·설정 필요 | 내부 마커는 실제 `displayed_text`와 운영 패턴, 금지 표현은 표시본과 질문별 등록 목록이 필요. Q100 JSON의 `system_id`는 `aiona`이고 `displayed_text`는 없음. 없는 필드를 임의 생성하지 않음 |

## 정책 대조

- **표:** `checks_requirements._table_checks`는 질문에서 표 요구를 추출한 경우 존재·개수·구성을 판정한다. `표 하나로만`·`표만 출력` 표현과 명시 열 5개도 처리한다. 행 수 비교는 `table_data_rows`/`table_total_rows` 요구가 있을 때만 생성한다. 고정 2행 최소 규칙은 없다. 칸이 모두 빈 마크다운 헤더 줄은 헤더 0행으로 센다(`analysis.TextAnalysis.tables`). 질문이 헤더 없이를 요구하면 한 줄뿐인 마크다운 표의 그 줄을 내용 행으로 본다.
- **코드블록:** `checks_integrity._code_checks`에서 1-2로 기록한다. 2-1/2-2에서 같은 코드블록 조건을 재생성하지 않는다.
- **외국 문자:** `1-1.foreign_script`가 한자·가나·키릴·태국·아랍 문자 등을 판정한다. 질문이 그 문자·언어를 다루거나, 따옴표·괄호 안·바로 뒤 한글 병기이거나, 같은 문단·목록 항목·표 행에 뜻·번역·예시 설명이 있으면 PASS, 없으면 FAIL. 질문에 허용 문자 조건이 있으면 2-1 결과를 따른다. 영문·그리스 문자는 대상이 아니다.
- **깨진 문자 예시:** 한 줄이 아니라 질문 주제와 같은 문단·목록 항목(목록을 여는 줄 포함)·표 행·코드블록 앞뒤 설명을 보고, 인코딩·예시 설명이 있으면 예시로 제외한다. '결과는·실제 출력'처럼 실제 결과로 제시한 문장 속 깨짐과 설명 없이 섞인 깨짐은 FAIL이다.
- **개수 연결:** 2-1의 요구 개수와 3-2의 답변 선언은 연속 번호 제목·바로 뒤 목록의 구조 범위를 기록한다. 번호 항목 뒤 설명 제목은 항목으로 합산하지 않고, 중단된 번호열은 미판정한다.
- **산술 표기:** 3-1은 단일 숫자의 제곱과 숫자 괄호 곱셈을 위치 보존 방식으로 정규화해 검산한다. 변수·함수 호출로 일반화하지 않는다.
- **미검사:** `model.Status`가 `CONFIG_MISSING`, `DISPLAY_MISSING`, `INCOMPLETE`, `UNSUPPORTED`, `DEPENDENCY_FAILED`, `CHECK_ERROR`, `LIMIT_EXCEEDED` 등을 verdict와 분리한다. `INCOMPLETE`에서는 명확한 위반을 찾은 FAIL만 허용하고 PASS는 금지한다.
- **원본 변환·보존:** 지원하지 않는 텍스트 원본의 명시 변환·보존 요구는 `3-3.transform`의 `UNSUPPORTED`로 남긴다. 실제 변환의 PASS/FAIL 구현 범위는 기존 지원 JSON 배열·표·목록과 연산에 한정한다.
- **판정 범위:** 질문의 지원 문형과 검사 대상을 추출하지 못한 조건은 임의로 생성하지 않는다. 다만 추출되지 않은 잠재 요구 자체는 현재 결과 행으로 나타나지 않아, 이번 CODE 처리율의 분모에 들어가지 않는 한계가 있다.

## 이번 실험의 집계 범위

엔진이 생성한 결과 중 `OUT_OF_SCOPE`, `APPLICABILITY_EXCEPTION`, `DUPLICATE`를 제외한 행을 **활성 평가**로 정의한다. 이 엔진에서 활성 행은 모두 CODE checker 대상이므로 CODE 처리율과 전체 활성 평가 대비 CODE 판정 비율의 분모가 같다. `CONFIG_MISSING`·`DISPLAY_MISSING`은 AIONA 특화 정책이 적용되는 한 활성 미판정으로 남긴다. 외국 문자·표 행 관측은 판정 행이 아니므로 분모에 넣지 않는다. 이 비율은 문서의 모든 가능한 평가항목을 구현했다는 비율이 아니다.

## 테스트 기준

`python -m pytest` 결과 (2026-10-02): **114 passed**. 표 추출 누락·원본 변환 미판정·CODE 단계 네트워크 차단 회귀 테스트는 `tests/test_code_eval_engine.py`, 1-1 문자 맥락 회귀 테스트는 `tests/test_code_eval_refinement.py`에 있다.
