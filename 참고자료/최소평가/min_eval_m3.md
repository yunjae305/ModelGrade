# 최소 평가 리스트 (min-eval-list-v1)

정밀 평가 = 질문 세트 `qs-b468b011e6c3` 전체 326개. 최소 평가 = 그중 **37개** — 질문당 평가 기준이 많은 질문부터 보며, 평가 기준(64개, 질문으로 셀 수 있는 것)이 모두 **3번 이상** 적용될 때까지 고름(새 기준을 더하지 못하는 질문은 건너뜀).
구성: {'대화': 11, '단일': 23, '긴 문서': 3}. 기준마다 목표 횟수별 필요 질문 수: 1번↑ 17개, 3번↑ 37개, 5번↑ 56개, 10번↑ 110개.
모든 기준이 목표 횟수를 채움.

## 고른 질문 (고른 순서)

| 순서 | 질문 | 종류 | 기준 수 | 새로 덮은 기준 |
|---|---|---|---|---|
| 1 | Q12 | 대화 | 36 | 1-1.character_rules, 1-1.foreign_script, 1-1.response_nonempty, 2-2.table, J1-1.a, J1-2.a, J1-3.a, J1-3.b, J1-4.a, J1-4.b, J2-1.a, J2-1.b, J2-2.a, J2-3.a, J2-5.a, J2-5.b, J3-1.a, J3-2.a, J3-2.b, J3-4.a, J3-5.a, J3-5.b, J4-1.a, J4-2.a, J4-3.a, J5-1.a, J5-2.a, J5-2.b, J6-1.a, J6-2.a, J6-2.b, J6-2.c, J6-3.a, J6-3.b, J6-4.a, J6-4.b |
| 2 | Q15 | 대화 | 36 | 1-1.character_rules, 1-1.foreign_script, 1-1.response_nonempty, 2-2.table, J1-1.a, J1-2.a, J1-3.a, J1-3.b, J1-4.a, J1-4.b, J2-2.a, J2-3.a, J2-4.a, J2-4.b, J2-5.a, J2-5.b, J3-1.a, J3-2.a, J3-2.b, J3-3.a, J3-3.b, J3-5.a, J3-5.b, J4-1.a, J4-2.a, J4-3.a, J5-1.a, J5-2.a, J6-1.a, J6-2.a, J6-2.b, J6-2.c, J6-3.a, J6-3.b, J6-4.a, J6-4.b |
| 3 | Q21 | 대화 | 36 | 1-1.character_rules, 1-1.foreign_script, 1-1.response_nonempty, J1-1.a, J1-2.a, J1-3.a, J1-3.b, J1-4.a, J1-4.b, J2-1.a, J2-1.b, J2-2.a, J2-3.a, J2-5.a, J3-1.a, J3-2.a, J3-2.b, J3-3.a, J3-3.b, J3-5.a, J3-5.b, J4-1.a, J4-1.b, J4-2.a, J4-3.a, J5-1.a, J5-2.a, J5-2.b, J6-1.a, J6-2.a, J6-2.b, J6-2.c, J6-3.a, J6-3.b, J6-4.a, J6-4.b |
| 4 | Q22 | 대화 | 35 | J2-1.a, J2-1.b, J2-5.b, J3-3.a, J3-3.b |
| 5 | Q26 | 대화 | 35 | 2-1.count, 2-2.table, J4-1.b, J5-2.b |
| 6 | Q17 | 대화 | 34 | J2-4.a, J2-4.b, J3-4.a |
| 7 | Q23 | 대화 | 33 | 2-1.literal, J4-1.b |
| 8 | Q25 | 대화 | 33 | 2-1.literal |
| 9 | Q19 | 대화 | 32 | J3-4.a |
| 10 | Q24 | 대화 | 32 | 2-1.count, 2-2.list |
| 11 | Q36 | 단일 | 20 | 2-2.heading, J2-4.a, J2-4.b |
| 12 | Q41 | 단일 | 20 | 2-1.count |
| 13 | Q54 | 단일 | 20 | 3-1.question_calculation, 3-3.aggregate, 3-3.transform, 3-3.unique_values |
| 14 | Q56 | 대화 | 20 | 2-1.char_count, 2-1.literal |
| 15 | Q63 | 단일 | 20 | 2-2.list |
| 16 | Q65 | 단일 | 20 | 3-1.question_calculation, 3-3.aggregate, 3-3.unique_values |
| 17 | Q70 | 단일 | 20 | 1-2.code_block, 1-2.code_fence_min, 1-2.code_language_label, 3-1.question_calculation |
| 18 | Q72 | 단일 | 20 | 2-1.char_count, 2-1.charset, 2-2.heading |
| 19 | Q82 | 단일 | 20 | 1-2.code_block, 1-2.code_fence_min, 1-2.code_language_label, 1-2.json_syntax, 2-2.json_structure, 3-3.aggregate, 3-3.transform, 3-3.unique_values |
| 20 | Q86 | 단일 | 20 | 1-2.json_syntax, 2-2.json_structure |
| 21 | Q89 | 단일 | 20 | 1-2.code_block, 1-2.code_fence_min, 1-2.code_language_label, 1-2.json_syntax, 2-2.json_structure, 3-3.transform |
| 22 | Q100 | 단일 | 20 | 2-1.char_count, 2-2.heading |
| 23 | Q130 | 단일 | 20 | 2-1.charset |
| 24 | Q144 | 단일 | 20 | 2-1.charset, 2-2.list |
| 25 | Q272 | 단일 | 20 | 1-2.python_syntax |
| 26 | Q288 | 단일 | 20 | J4-4.a |
| 27 | Q315 | 단일 | 20 | 1-2.python_syntax |
| 28 | Q5 | 긴 문서 | 19 | J7-1.a, J7-2.a |
| 29 | Q30 | 단일 | 19 | J4-4.a, J8-1.a, J8-2.a, J8-3.a |
| 30 | Q48 | 단일 | 19 | J4-4.a, J8-1.a, J8-2.a, J8-3.a |
| 31 | Q61 | 단일 | 19 | 1-2.python_syntax |
| 32 | Q1 | 긴 문서 | 18 | J7-1.a, J7-2.a |
| 33 | Q2 | 긴 문서 | 18 | J7-1.a, J7-2.a |
| 34 | Q49 | 단일 | 18 | J8-1.a, J8-2.a, J8-3.a |
| 35 | Q31 | 단일 | 15 | 2-1.explicit_empty |
| 36 | Q71 | 단일 | 15 | 2-1.explicit_empty |
| 37 | Q120 | 단일 | 15 | 2-1.explicit_empty |
