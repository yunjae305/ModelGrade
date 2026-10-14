# IMPLEMENTATION_MAP — AIONA 코드 평가기 v16

기준 → 파싱·계산 도구 → 구현 함수 → 테스트. 테스트는 주로 `tests/test_code_eval_v16.py`, 회귀 사례는 `tests/test_code_eval_refinement.py`·`tests/test_code_eval_engine.py`.
측정 규칙과 지원 범위 상세: [`참고자료/MEASUREMENT_v16.md`](MEASUREMENT_v16.md)

적용 우선순위: v16 구현 프롬프트(표 최소 행 수 폐기 · 코드블록 1-2 통합 · 외국 문자 관측만) → v16 분류 HTML → 기존 프로젝트 코드 → 원본 MD(`참고자료/채팅품질 평가기준.md`, sha256 `fb7126e7…`).
옛 검사기 코드는 쓰지 않고, 필요한 파싱·계산 함수만 `code_eval/textparse.py`·`code_eval/arith.py` 로 옮겨 쓴다 (2026-10-02).

## 1. 공통 코드 평가

| 소분류 · 조건 | 기준 출처 | 파싱·계산 도구 (`code_eval/`) | 구현 함수 (`code_eval/`) | 테스트 |
|---|---|---|---|---|
| 1-1 응답 존재 | 운영 정책 | `textparse.strip_non_content` | `checks_integrity.check_1_1` | `test_empty_answer_vs_explicit_empty_vs_missing_field` |
| 1-1 문자 규칙 | 운영 정책 | `textparse.find_corruption`, `CORRUPTION_SIGNATURES` + C0 제어(신규) | `check_1_1`, `_allowed_codepoints`, `_corruption_reading`, `_connected_context` | `test_broken_character_fails_without_context_but_question_topic_makes_it_an_example`, `test_corruption_counts_as_example_only_with_structural_context` |
| 1-1 외국 문자 (설명 없는 출현) | 운영 정책 · UAX #24 | `scripts.observe_scripts`, `script_name` (한자·가나 구간 표 + 문자 이름) | `checks_integrity._foreign_script_check`, `_script_reading`, `_connected_context` | `test_hanja_gloss…`, `test_inserted_japanese…`, `test_explicit_forbidden_japanese…`, `test_foreign_script_needs_question_topic_explanation_or_gloss`, `test_script_table_matches_unicode_names` |
| 1-2 코드블록 존재·개수·금지 | 질문 원문 | `textparse._fenced_code_blocks` (CommonMark) | `checks_integrity._code_checks` | `test_code_block_count_vs_delimiter…`, `test_code_block_exact_count_fail_lives_only_in_1_2` |
| 1-2 언어 표지 | 질문 원문 | 펜스 info string | `_code_checks` | (추출 경로는 `extract._LANG_LABEL`) |
| 1-2 형식 위반 — 코드블록 (구획 표시 < 2) | MD 165 | `_fenced_code_blocks` | `_code_checks` (`1-2.code_fence_min`) | `test_code_block_count_vs_delimiter_count…` |
| 1-2 펜스 불균형 (코드 홀수 OR 수식 홀수) | MD 170 | `_fenced_code_blocks` + `$$` 계수(신규) | `_code_checks` (`1-2.fence_imbalance`) | `test_fence_imbalance_code_one_and_math_one…` |
| 1-2 JSON 구문 | RFC 8259 | `textparse._reject_json_constant`, `_json_type_name` | `checks_integrity.json_targets`, `_json_syntax` | `test_json_valid_error_and_intended_error_example`, `test_json_nan_rejected…` |
| 1-2 Python 구문 | Python compile | 신규 (ast.parse+compile, 실행 없음) | `_python_syntax` | `test_python_syntax_checked_without_execution`, `test_code_block_format_pass_but_python_syntax_fail`, `test_heading_form_footnote_and_code_fragment` |
| 1-2 짝이 없는 닫는 태그 | MD 169 (고아 닫는 태그) | `textparse.scannable_text` (코드 제외·강조 제거) | `_orphan_tags` | `test_orphan_closing_tag_body_vs_code_example` |
| 1-3 각주·인용·내부 링크 | 질문 참조 요구·연결 규칙 | `textparse._parse_footnotes`, `_parse_references`, `internal_links`, `link_targets` | `checks_integrity.check_1_3` | `test_footnote_missing_definition…`, `test_heading_form_footnote…` |
| 2-1 불릿·문장·문단·줄·어절 수 | 질문 원문 · MD 163·166 | `textparse.find_list_items`, `split_sentences`, `find_paragraphs`, `count_words`; 수사 `textparse._NUM_WORD`, `_count_governed_by_output_verb` | `extract._count_reqs`, `_scope`; `checks_requirements.check_2_1` | `test_bullet_count`, `test_sentence_count`, `test_sentence_split_keeps…`, `test_count_scope_is_not_replaced…`, `test_upper_limit_phrase…` |
| 2-1 항목 수 (N개 추천) | 질문 원문 | `textparse.top_level_list_items` | `checks_requirements._list_items_measure` | `test_ambiguous_numbered_structures…` |
| 2-1 글자 수 | 질문 원문 | 본문 글자 정의 (`textparse.content_lines`) | `check_2_1` | `test_approximate_length_is_not_strictly_failed` |
| 2-1 필수·금지·시작·끝 문자열, 허용 문자, 빈 출력 | 질문 원문 | 질문 문형 규칙(`extract`), `textparse.scannable_text` | `extract._literal_reqs`; `check_2_1` | `test_explicit_forbidden_japanese…`, `test_per_item_boundary…`, `test_empty_answer…` |
| 2-2 표 존재·열·헤더·명시 행 수 (고정 최소 행 수 없음) | 질문 원문 · MD 164 폐기 | `textparse.find_markdown_tables` + HTML 표(신규) | `analysis.TextAnalysis.tables`, `checks_requirements._table_checks`, `table_observation` | `test_no_row_requirement…`, `test_header_only_table…`, `test_headerless_one_row_html_table…`, `test_explicit_headerless…`, `test_data_rows_required_three…`, `test_table_requested_but_prose_given`, `test_prose_without_table…` |
| 2-2 목록·제목 | 질문 원문 | `textparse.find_headings`, `find_list_items` | `_list_heading_checks` | `test_markdown_forbidden_conflict_and_list_negation` |
| 2-2 JSON 최상위·필수 키·자료형 | 질문 원문 · JSON Schema 개념 | 1-2 파싱 결과 공유 | `_json_structure` | `test_json_syntax_failure_does_not_create_missing_field_fail` |
| 3-1 질문 계산값 | 질문 원문 | `arith._evaluate`, `_tolerance_for` | `extract._calc_reqs`; `checks_consistency._question_calculation`, `_claimed_by_label` | `test_equation_correct_but_question_total_wrong` |
| 3-1 답변 등식 | 수학적 연산 | `arith._evaluate`, `_tolerance_for`, `NEGATION_MARKERS`, 접미사 목록 | `_find_chains`, `_answer_equations` | `test_equation_correct…`, `test_chained_equation_and_presented_mistakes` |
| 3-2 선언 개수 | 선언값·관측값 비교 | `structure_counts.heading_series_from` | `check_3_2`, `_next_structure` | `test_declared_three_but_four…`, `test_declared_count_uses_table_data_rows…`, `test_ambiguous_numbered_structures_and_container_heading` |
| 3-3 원본 보존·변환·고유성·집계 | 질문 원본·연산 정의 | 신규 (자료형 구분 비교) | `extract._transform_reqs`, `_source_data`; `check_3_3`, `same`, `_apply_ops` | `test_transform_filter_sort…`, `test_dedupe_distinguishes_zero_false_null…` |

## 2. AIONA 특화 평가

| 항목 | 기준 | 설정 연결 | 구현 | 테스트 |
|---|---|---|---|---|
| 내부 마커 노출 | MD 155 | `code_eval/settings/aiona_specialized.json` → `code_eval/settings/aiona_service_rules_v1.json` (`u8_1`, `rule_pattern.RulePattern` 검증) — **운영 패턴 0건** | `config.load_aiona_config`, `checks_aiona._markers` | `test_registered_marker_fail_pass_and_missing_config_or_display` |
| 금지 표현 출현 | MD 156 | `data/aiona_specialized/forbidden_expressions.json` — **파일 없음** | `checks_aiona._forbidden` | `test_forbidden_expression_states`, `test_same_forbidden_condition_counted_once` |

## 3. 엔진·입출력·화면

| 요구 | 구현 | 테스트·검증 |
|---|---|---|
| 같은 검사 경로, 결과 구조, 집계, 중복 제거, 재현 해시 | `engine.evaluate_case/evaluate_batch/summarize_batch/result_digest`, `model.ConditionResult` | `test_rerun_and_cli_and_web_give_same_digest`, `test_system_id_does_not_change_common_results` |
| 입력 JSON/JSONL, 깨진 줄 보존 | `io.parse_input_text` | `test_jsonl_broken_line_becomes_input_error_not_dropped` |
| 안전 한도 | `core.LIMITS`, `engine.evaluate_case` | `test_limit_exceeded_is_not_quality_fail` |
| 조작 문구·스크립트 | 데이터로만 처리, 화면 `textContent`, 보고서 이스케이프 | `test_manipulative_answer_text_changes_nothing`, `test_report_escapes_answer_markup`, 브라우저 점검(삽입 요소 0개) |
| 1,500건 일괄 | `evaluate_batch` | `test_batch_1500_cases_without_loss` |
| CLI | `code_eval/__main__.py` (`run`, `single`, `info`) | `test_rerun_and_cli_and_web…` |
| 기본 의미 평가 J1~J6 | `jev_applicability.jev_applicability` → `jev_eval.evaluate_jev_batch` (모든 유효 질문, CODE 결과와 독립) | `tests/test_jev_eval.py`, `tests/test_review_semantic_regressions.py` |
| 별도 FAIL 질문 LLM 재검토 | `llm_review.review_after_code` (별도 실행, FAIL 질문만), `llm_client` | `test_llm_review_routes_only_failed_questions_and_checks` |
| 웹 | `webapp/code_eval_service.py`, `webapp/server.py` `/api/code-eval/*`, `frontend/code-eval.html`, `frontend/src/code-eval.{js,css}` | 같은 테스트(서비스 경로) + 헤드리스 Chromium 점검 |
| HTML 보고서 | `report_html.render_report` | `test_report_escapes_answer_markup` |
| 비전공자용 쉬운 설명 (왜 FAIL·기대/실제·위치·고치는 법) | `explain.explain_result` (engine 이 모든 결과에 부착), 화면 `code-eval.js` `renderFailOverview/resultDetail/renderLoc`, 보고서 `_result_html`, CLI `single` | `test_every_result_has_plain_explanation…`, `test_python_explanation_points_to_error_line…`, `test_character_explanation…`, `test_declared_count_explanation…`, `test_explanation_uses_natural_korean_particles` |

## 4. 옮겨 온 도구 · 수정 · 신규 · 삭제

- **옮겨 온 도구 (2026-10-02)**: 옛 검사기 모듈에서 쓰던 함수만 원본 그대로 `code_eval/textparse.py`(줄·제목·목록·표·문단·문장·단어, 깨진 문자, 코드 펜스·JSON, 각주·참조·내부 링크, 강조 제거 투영, 수사), `code_eval/arith.py`(등식 계산·허용 오차·부정·접미사 목록), `code_eval/rule_pattern.py`(마커 패턴 형식), `code_eval/llm_client.py`(LLM 호출)로 옮겼다. 마커 목록은 `code_eval/settings/aiona_service_rules_v1.json`, 원본 기준은 `참고자료/채팅품질 평가기준.md`.
- **수정**: `webapp/server.py` (코드 평가 전용, `/` → `/code-eval.html`), `.gitignore` (`data/code_eval_runs/`).
- **신규**: `code_eval/*` (v16.2.0 에 `explain.py` 추가), `webapp/code_eval_service.py`, `frontend/code-eval.html`, `frontend/src/code-eval.js`, `frontend/src/code-eval.css`, `tests/test_code_eval_v16.py`, `tests/test_code_eval_refinement.py`, `tests/test_code_eval_engine.py`, `참고자료/MEASUREMENT_v16.md`, `참고자료/aiona_specialized_settings.example.json`, `data/code_eval/*`, `results/set2_q100/`, 이 문서.
- **폐기 규칙**: MD 164 "표 행이 2개 미만" — 코드·설정·집계 어디에도 없음. 원문 열람 화면에 취소선 이력으로만 표시.
- **삭제 (2026-10-02)**: 옛 `evaluator/` 패키지(옛 검사기·라우팅·V2 기준), CLI `--mode code-only`·`--baseline-v17`·`convert-aiona`(`baseline_v17.py`·`adapters.py`).

## 5. 실제 연결 데이터가 없는 항목

| 필요한 자료 | 확인한 경로 | 영향 |
|---|---|---|
| AIONA 내부 마커 실제 목록 | `code_eval/settings/aiona_service_rules_v1.json` (`u8_1` 빈 배열, `pending_registration` 에 범주만) | 내부 마커 노출: 전 케이스 `CONFIG_MISSING` |
| 질문별 금지 표현 목록 | 연결 경로 `data/aiona_specialized/forbidden_expressions.json` 미존재 | 금지 표현 출현: 전 케이스 `CONFIG_MISSING` |
| 실제 AIONA 표시본 | 입력 파일에 `displayed_text` 없음 (`system_id` 는 `aiona`) | 설정을 넣어도 `DISPLAY_MISSING` (원문으로 대신 판정하지 않음) |

공통 8개 소분류 검사는 위 자료와 무관하게 질문·답변만으로 실행된다.

## 6. v16.2.4 회귀 보완 (2026-10-07)

| 보완 대상 | 구현 | 회귀 검증 |
|---|---|---|
| 음수 질문·답변, 퍼센트 결과, 설명이 붙은 목록, 중첩 펜스 | `extract`, `checks_consistency`, `textparse` | `tests/test_review_rules_regressions.py` |
| 요청 종결 구두점, 긴 history 원문 전체 전송(v2.3) | `jev_applicability`, `jev_eval` | `tests/test_review_semantic_regressions.py`, `tests/test_full_history_original_text.py` |
| 중복 ID 질문의 선택·집계·보고서 링크 | `combined`, `jev_eval`, `report_html`, `frontend/src/code-eval.js` | `tests/test_review_report_regressions.py` |
| 실행 중인 웹 작업 보존, 접수 한도 HTTP 429 | `webapp/code_eval_service.py`, `webapp/server.py` | `tests/test_review_service_regressions.py` |

## 7. v16.2.5 추가 검증 보완 (2026-10-07)

| 보완 대상 | 구현 | 회귀 검증 |
|---|---|---|
| 비유한수 입력 격리·유효 JSON 출력, CLI 원문 보존·출력 충돌 사전 검사 | `io`, `engine`, `__main__`, `webapp/code_eval_service.py`, `webapp/server.py` | `tests/test_review_io_cli_regressions.py` |
| 각주 예외·코드 속 참조 제외, 여러 원본 후보 탐색 | `checks_integrity`, `textparse`, `extract` | `tests/test_review_reference_regressions.py` |
| 잘못된 turn 격리·누락 구간 표현, 소수점 요청 조건, 적용 조건 스냅샷 | `engine`, `jev_eval`, `jev_applicability` | `tests/test_review_turn_regressions.py` |
| 저장 당시 보고서 기준 유지·저장 실패 안내 | `report_html`, `frontend/src/code-eval.js` | `tests/test_review_saved_report_regressions.py` |

## 8. Q165 통합과 의미 평가 v2.3 원문 전송 (2026-10-07)

| 대상 | 구현 | 검증 |
|---|---|---|
| Q100 + 멀티턴 65개, 15·20·30턴 대화 분리 | `data/code_eval/AIONA_winz_Q165_Q100_multiturn65_v1.json`, `webapp/comparison_service.py` | `tests/test_combined_aiona_dataset.py` |
| 기본 Q165 불러오기, 회차·턴 표시 | `frontend/workspace.html`, `frontend/src/workspace.js` | `tests/workspace_ui_smoke.py` |
| 이전 질문·답변 전문 전송, 길이에 따른 축약·생략 제거 | `code_eval/jev_eval.py` | `tests/test_review_semantic_regressions.py`, `tests/test_full_history_original_text.py` |

## 9. GPT CLI 의미 평가 (2026-10-07)

| 대상 | 구현 | 검증 |
|---|---|---|
| Codex native 실행, 모델 `gpt-6.1-sol`·추론 `high` 고정, CLI 로그인 검사 | `code_eval/codex_cli_client.py`, `code_eval/jev_client.py`, `.env`, `.env.example` | `tests/test_codex_cli_client.py` |
| CLI 공통 J 기준·원문 프롬프트·JSON Schema·확신도 변환 | `code_eval/semantic_cli_common.py`, `code_eval/claude_cli_client.py` | `tests/test_codex_cli_client.py`, `tests/test_jev_eval.py`, `tests/test_full_history_original_text.py` |
| `codex-cli`·`gpt-cli` 선택, 엔진·모델·추론 수준 표시 및 자기 확신도 안내 | `code_eval/__main__.py`, `code_eval/jev_eval.py`, `code_eval/report_html.py`, `frontend/src/code-eval.js`, `frontend/src/workspace.js` | `tests/test_codex_cli_client.py`, `tests/workspace_ui_smoke.py`, `tests/workspace_live_ui_smoke.py` |

## 10. v16.3.0 PASS·FAIL 고정·3-1·3-3 정리 (2026-10-13, 설계 2단계)

| 대상 | 구현 | 검증 |
|---|---|---|
| 공통 행 PASS/FAIL 뿐, 보류 조건 `meta.skipped_conditions`, 검사기 오류 `case.errors`, 딸린 검사 행 미생성, C 보완 제거 | `code_eval/model.py`(`ConditionResult`), `engine.py`, `checks_*.py`, `common_adjudication.py`(옛 `code_completion` 읽기 전용), `combined.py`, `report_html.py`, `webapp/evaluation_events.py` | `tests/test_code_eval_engine.py`, `tests/test_common_adjudication.py`, `tests/test_semantic_no_completion.py` |
| 3-1 답변 등식 v16-1.4 | `arith.py`(`MEASUREMENT_SUFFIXES`), `checks_consistency._find_chains`·`_answer_equations` | `tests/test_code_eval_refinement.py` |
| 3-3·2-2 조건 추출 정리 | `extract._transform_reqs`·`_source_data`, `checks_consistency` 3-3 | `tests/test_code_eval_refinement.py` |
| 2-1.literal 공백 접기 | `extract._literal_reqs`, `checks_requirements` | `tests/test_code_eval_v16.py` |

## 11. 의미 평가 항목별 파이프라인 (2026-10-13, 설계 3단계)

| 대상 | 구현 (`code_eval/`) | 검증 (`tests/`) |
|---|---|---|
| 설정 `SEMANTIC_PIPELINE` legacy\|items | `jev_client.resolve_semantic_pipeline`, `semantic_setup` | `test_semantic_setup.py`, `conftest.py`(테스트는 legacy 고정) |
| 기준표(세부 25·항목 39·펼치기·누락형·'판정하는 곳'·reason_code) | `semantic_items.py` | `test_semantic_items.py` |
| 적용 규칙(세부 기준별, J5 는 질문 유형) | `jev_applicability.sub_applicability`·`j5_items_applicability` | `test_semantic_items.py`, `test_j5_items_applicability.py` |
| 프롬프트·평평한 strict 스키마·transport 공통 헬퍼 | `semantic_items_prompt.py`, `semantic_cli_common.py`(`is_items`·`strip_search_items`·`validate_items_shape`) | `test_semantic_items_prompt.py` |
| transport 4종의 items 분기(검색 꺼짐 J5-1.a 미전송, J5 서버 검증) | `anthropic_semantic_client.py`, `semantic_api_client.py`, `claude_cli_client.py`, `codex_cli_client.py` | `test_semantic_items_transports.py` |
| 인용 검증·재확인(≤3)·확인 불가·모델 간 요소 대조 | `semantic_quotes.py` | `test_semantic_quotes.py` |
| J5 주장별 서버 검증(반박 출처 대조·값 대조·링크 존재)·legacy 투영 | `semantic_facts.py` | `test_semantic_facts.py` |
| 집계(파싱 → 규칙 → 세부 → 기준, 충족률) | `semantic_items_eval.py` | `test_semantic_items_eval.py` |
| jev_eval 통합(요청 조립·재확인 콜백·레코드·요약) | `jev_eval.py`(`jev_request`·`_items_record`·`summarize_records`) | `test_jev_eval_items.py` |
| webapp 실시간 반영(문맥·SSE·프로필·registry·결과 화면 subs)·요소 합집합 | `webapp/comparison_service.py`, `code_eval_service.py`, `evaluation_events.py`, `version_registry.py`, `semantic_union.py`, `criteria_registry.py`, `frontend/src/workspace-results.js`·`evaluation-stream.js`·`version-comparison.js` | `test_items_webapp.py`, `workspace_results_test.js`, `evaluation_stream_test.js`, `version_comparison_test.js` |

## 12. Message Batches 호출·버전 비교·이력 (2026-10-13, 설계 3c·3d)

| 대상 | 구현 | 검증 |
|---|---|---|
| Claude 클라이언트 분리(`build_request`·`_consume`·`finish_batch_result`), 설정 `SEMANTIC_DISPATCH` — 10-13 에 뺐다가 10-14 다시 넣음 | `code_eval/anthropic_semantic_client.py`, `jev_client.py`, `semantic_setup.py` | `tests/test_anthropic_semantic_client.py`, `test_jev_batch.py` |
| Batches HTTP 클라이언트·디스패처(pilot → 묶음 → 폴링 → 재전송 → 취소 → 이어받기) | `code_eval/anthropic_batch_client.py`, `jev_batch.py`, `jev_eval._prepare_batch`·`_finish_batch` | `tests/test_jev_batch.py` |
| webapp 묶음 ID 저장·진행·이어받기(API `evaluate(run_id, {"resume": true})`, 화면 버튼은 다시 넣지 않음) | `webapp/comparison_service.py` | `tests/test_batch_webapp.py` |
| 버전 비교(조건 차이·질문→기준→세부→항목·코드 조건·대화 단위 부호검정·꼬리)·실행 요약·기준 CHANGELOG·API·화면 | `webapp/version_comparison.py`, `run_summary.py`, `comparison_service.version_comparison`, `server.py`, `code_eval/criteria_registry.CHANGELOG`, `frontend/src/version-workspace.js` | `tests/test_version_comparison_server.py` |
| 이력 관리(AIONA 결과 이력: 완료 실행마다 한 줄, 같은 질문 세트 직전 대비 요인·통과율 변화, 재평가는 원본과 비교, CHANGELOG 연결)·모델 버전 저장소(작업 이력 제거) | `webapp/aiona_history.py`, `comparison_service.aiona_history`, `server.py`, `webapp/registry_store.py`, `version_registry.py`, `frontend/src/history-workspace.js` | `tests/test_version_comparison_server.py`, `tests/test_version_registry_store.py`, `tests/test_evaluation_summary.py` |
