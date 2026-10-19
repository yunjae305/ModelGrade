# 최소 평가 리스트 (min-eval-list-v2-setcover)

정밀 평가 = 질문 세트 `qs-b468b011e6c3` 전체 326개. 최소 평가 = 그중 **78개** — 아직 덜 채운 기준을 가장 많이 채우는 질문부터(집합 덮기 표준 탐욕법) 평가 기준(64개, 질문으로 셀 수 있는 것)이 모두 **10번 이상** 적용될 때까지 고름.
구성: {'대화': 11, '단일': 57, '긴 문서': 10}. 기준마다 목표 횟수별 필요 질문 수: 1번↑ 7개, 3번↑ 21개, 5번↑ 37개, 10번↑ 78개.
모든 기준이 목표 횟수를 채움.
최소 평가 질문 세트: `qs-c5d8a1624770` (화면 '질문 세트'에서 수집·평가 가능)

## 정밀 vs 최소 결과 (같은 실행의 판정, 질문 단위 · 보류는 분모에 둠)

| 모델 | 정밀 PASS·FAIL·보류 / n (통과율) | 최소 PASS·FAIL·보류 / n (통과율) |
|---|---|---|
| aiona | 166·84·76 / 326 (51%) | 31·25·22 / 78 (40%) |
| gpt | 190·77·59 / 326 (58%) | 34·23·21 / 78 (44%) |
| claude | 131·112·83 / 326 (40%) | 29·28·21 / 78 (37%) |
| gemini | 120·124·82 / 326 (37%) | 26·29·23 / 78 (33%) |

순위 — 정밀: gpt > aiona > claude > gemini · 최소: gpt > aiona > claude > gemini · 같음

**FAIL 포착** (FAIL 을 보는 것이 목적): 전체에서 나온 (모델, 기준) FAIL 조합 87개 중 이 목록에서 한 번이라도 보이는 것 **70개 (80%)** · FAIL 사건 586건 중 209건 (36%).

## 고른 질문 (고른 순서)

| 순서 | 질문 | 종류 | 기준 수 | 새로 덮은 기준 |
|---|---|---|---|---|
| 1 | Q12 | 대화 | 36 | 1-1.character_rules, 1-1.foreign_script, 1-1.response_nonempty, 2-2.table, J1-1.a, J1-2.a, J1-3.a, J1-3.b, J1-4.a, J1-4.b, J2-1.a, J2-1.b, J2-2.a, J2-3.a, J2-5.a, J2-5.b, J3-1.a, J3-2.a, J3-2.b, J3-4.a, J3-5.a, J3-5.b, J4-1.a, J4-2.a, J4-3.a, J5-1.a, J5-2.a, J5-2.b, J6-1.a, J6-2.a, J6-2.b, J6-2.c, J6-3.a, J6-3.b, J6-4.a, J6-4.b |
| 2 | Q15 | 대화 | 36 | 1-1.character_rules, 1-1.foreign_script, 1-1.response_nonempty, 2-2.table, J1-1.a, J1-2.a, J1-3.a, J1-3.b, J1-4.a, J1-4.b, J2-2.a, J2-3.a, J2-4.a, J2-4.b, J2-5.a, J2-5.b, J3-1.a, J3-2.a, J3-2.b, J3-3.a, J3-3.b, J3-5.a, J3-5.b, J4-1.a, J4-2.a, J4-3.a, J5-1.a, J5-2.a, J6-1.a, J6-2.a, J6-2.b, J6-2.c, J6-3.a, J6-3.b, J6-4.a, J6-4.b |
| 3 | Q21 | 대화 | 36 | 1-1.character_rules, 1-1.foreign_script, 1-1.response_nonempty, J1-1.a, J1-2.a, J1-3.a, J1-3.b, J1-4.a, J1-4.b, J2-1.a, J2-1.b, J2-2.a, J2-3.a, J2-5.a, J3-1.a, J3-2.a, J3-2.b, J3-3.a, J3-3.b, J3-5.a, J3-5.b, J4-1.a, J4-1.b, J4-2.a, J4-3.a, J5-1.a, J5-2.a, J5-2.b, J6-1.a, J6-2.a, J6-2.b, J6-2.c, J6-3.a, J6-3.b, J6-4.a, J6-4.b |
| 4 | Q22 | 대화 | 35 | 1-1.character_rules, 1-1.foreign_script, 1-1.response_nonempty, J1-1.a, J1-2.a, J1-3.a, J1-3.b, J1-4.a, J1-4.b, J2-1.a, J2-1.b, J2-2.a, J2-3.a, J2-5.a, J2-5.b, J3-1.a, J3-2.a, J3-2.b, J3-3.a, J3-3.b, J3-5.a, J3-5.b, J4-1.a, J4-2.a, J4-3.a, J5-1.a, J5-2.a, J6-1.a, J6-2.a, J6-2.b, J6-2.c, J6-3.a, J6-3.b, J6-4.a, J6-4.b |
| 5 | Q26 | 대화 | 35 | 1-1.character_rules, 1-1.foreign_script, 1-1.response_nonempty, 2-1.count, 2-2.table, J1-1.a, J1-2.a, J1-3.a, J1-3.b, J1-4.a, J1-4.b, J2-1.a, J2-1.b, J2-3.a, J2-5.a, J2-5.b, J3-1.a, J3-2.a, J3-2.b, J3-5.b, J4-1.a, J4-1.b, J4-2.a, J4-3.a, J5-1.a, J5-2.a, J5-2.b, J6-1.a, J6-2.a, J6-2.b, J6-2.c, J6-3.a, J6-3.b, J6-4.a, J6-4.b |
| 6 | Q11 | 대화 | 34 | 1-1.character_rules, 1-1.foreign_script, 1-1.response_nonempty, 2-2.table, J1-1.a, J1-2.a, J1-3.a, J1-3.b, J1-4.a, J1-4.b, J2-2.a, J2-3.a, J2-5.a, J3-1.a, J3-2.a, J3-2.b, J3-3.a, J3-3.b, J3-5.a, J3-5.b, J4-1.a, J4-2.a, J4-3.a, J5-1.a, J5-2.a, J5-2.b, J6-1.a, J6-2.a, J6-2.b, J6-2.c, J6-3.a, J6-3.b, J6-4.a, J6-4.b |
| 7 | Q17 | 대화 | 34 | 1-1.character_rules, 1-1.foreign_script, 1-1.response_nonempty, J1-1.a, J1-2.a, J1-3.a, J1-3.b, J1-4.a, J1-4.b, J2-1.a, J2-1.b, J2-2.a, J2-3.a, J2-4.a, J2-4.b, J2-5.a, J2-5.b, J3-1.a, J3-2.a, J3-2.b, J3-4.a, J3-5.b, J4-1.a, J4-2.a, J4-3.a, J5-2.a, J6-1.a, J6-2.a, J6-2.b, J6-2.c, J6-3.a, J6-3.b, J6-4.a, J6-4.b |
| 8 | Q18 | 대화 | 33 | 1-1.character_rules, 1-1.foreign_script, 1-1.response_nonempty, J1-1.a, J1-2.a, J1-3.a, J1-3.b, J1-4.a, J1-4.b, J2-1.a, J2-1.b, J2-2.a, J2-3.a, J2-5.a, J2-5.b, J3-1.a, J3-2.a, J3-2.b, J3-5.a, J3-5.b, J4-1.a, J4-2.a, J4-3.a, J5-1.a, J5-2.a, J6-1.a, J6-2.a, J6-2.b, J6-2.c, J6-3.a, J6-3.b, J6-4.a, J6-4.b |
| 9 | Q23 | 대화 | 33 | 1-1.character_rules, 1-1.foreign_script, 1-1.response_nonempty, 2-1.literal, 2-2.table, J1-1.a, J1-2.a, J1-3.a, J1-3.b, J1-4.a, J1-4.b, J2-1.a, J2-1.b, J2-2.a, J2-3.a, J2-5.a, J3-1.a, J3-2.a, J3-2.b, J3-5.b, J4-1.a, J4-1.b, J4-2.a, J4-3.a, J5-2.a, J6-1.a, J6-2.a, J6-2.b, J6-2.c, J6-3.a, J6-3.b, J6-4.a, J6-4.b |
| 10 | Q25 | 대화 | 33 | 1-1.character_rules, 1-1.foreign_script, 1-1.response_nonempty, 2-1.literal, 2-2.table, J1-1.a, J1-2.a, J1-3.a, J1-3.b, J1-4.a, J1-4.b, J2-1.a, J2-1.b, J2-2.a, J2-3.a, J2-5.a, J3-1.a, J3-2.a, J3-2.b, J4-1.a, J4-2.a, J4-3.a, J5-1.a, J5-2.a, J5-2.b, J6-1.a, J6-2.a, J6-2.b, J6-2.c, J6-3.a, J6-3.b, J6-4.a, J6-4.b |
| 11 | Q65 | 단일 | 20 | 2-1.count, 3-1.question_calculation, 3-3.aggregate, 3-3.unique_values, J2-1.a, J2-1.b, J2-2.a, J2-5.b, J3-4.a |
| 12 | Q272 | 단일 | 20 | 1-2.code_block, 1-2.code_fence_min, 1-2.code_language_label, 1-2.python_syntax, 2-1.char_count, 3-1.question_calculation, J2-1.a, J2-1.b, J3-4.a |
| 13 | Q119 | 단일 | 19 | 1-2.code_block, 1-2.code_fence_min, 1-2.python_syntax, 3-1.question_calculation, J2-4.a, J2-4.b, J3-3.a, J3-3.b, J5-1.a |
| 14 | Q41 | 단일 | 20 | 2-1.count, 2-2.table, J2-4.a, J2-4.b, J3-3.a, J3-3.b, J3-5.a, J5-1.a |
| 15 | Q82 | 단일 | 20 | 1-2.code_block, 1-2.code_fence_min, 1-2.code_language_label, 1-2.json_syntax, 2-2.json_structure, 3-3.aggregate, 3-3.transform, 3-3.unique_values |
| 16 | Q288 | 단일 | 20 | 2-1.char_count, 2-1.charset, 2-2.heading, J2-4.a, J2-4.b, J2-5.b, J4-4.a, J5-2.b |
| 17 | Q102 | 단일 | 19 | 1-2.code_block, 1-2.code_fence_min, 1-2.code_language_label, 1-2.json_syntax, 2-1.count, 2-2.json_structure, 3-3.transform, 3-3.unique_values |
| 18 | Q304 | 단일 | 19 | 2-1.char_count, 2-2.heading, 2-2.table, 3-1.question_calculation, 3-3.aggregate, 3-3.transform, 3-3.unique_values, J3-4.a |
| 19 | Q89 | 단일 | 20 | 1-2.code_block, 1-2.code_fence_min, 1-2.code_language_label, 1-2.json_syntax, 2-2.json_structure, 3-3.transform, 3-3.unique_values |
| 20 | Q283 | 단일 | 20 | 2-1.char_count, 2-2.heading, 2-2.list, 3-1.question_calculation, 3-3.aggregate, 3-3.unique_values, J3-4.a |
| 21 | Q315 | 단일 | 20 | 1-2.python_syntax, J2-4.a, J2-4.b, J2-5.b, J3-4.a, J3-5.b, J4-1.b |
| 22 | Q48 | 단일 | 19 | 2-1.count, 2-2.heading, J4-4.a, J5-2.b, J8-1.a, J8-2.a, J8-3.a |
| 23 | Q121 | 단일 | 19 | 1-2.code_block, 1-2.code_fence_min, 1-2.python_syntax, 3-1.question_calculation, J3-3.a, J3-3.b, J3-4.a |
| 24 | Q171 | 단일 | 19 | 1-2.code_block, 1-2.code_fence_min, 1-2.code_language_label, 1-2.python_syntax, 2-1.literal, 3-1.question_calculation, J3-4.a |
| 25 | Q27 | 단일 | 18 | 1-2.code_block, 1-2.code_fence_min, 1-2.code_language_label, 1-2.python_syntax, 2-1.count, 3-1.question_calculation, J3-4.a |
| 26 | Q75 | 단일 | 18 | 1-2.code_block, 1-2.code_fence_min, 1-2.code_language_label, 1-2.json_syntax, 2-2.json_structure, 3-3.transform, 3-3.unique_values |
| 27 | Q90 | 단일 | 18 | 1-2.code_block, 1-2.code_fence_min, 1-2.code_language_label, 1-2.json_syntax, 2-2.json_structure, 3-3.transform, 3-3.unique_values |
| 28 | Q278 | 단일 | 18 | 2-1.char_count, 2-2.heading, 2-2.list, J5-2.b, J8-1.a, J8-2.a, J8-3.a |
| 29 | Q290 | 단일 | 18 | 2-1.char_count, 2-1.charset, 2-2.heading, 2-2.table, J2-4.a, J2-4.b, J2-5.b |
| 30 | Q323 | 단일 | 18 | 2-1.count, 2-2.list, J4-4.a, J5-2.b, J8-1.a, J8-2.a, J8-3.a |
| 31 | Q30 | 단일 | 19 | 2-1.count, J4-4.a, J5-2.b, J8-1.a, J8-2.a, J8-3.a |
| 32 | Q258 | 단일 | 18 | 2-1.char_count, 2-1.charset, 2-1.count, 2-2.table, J2-4.a, J2-4.b |
| 33 | Q107 | 단일 | 17 | 2-1.count, 2-2.list, J4-4.a, J8-1.a, J8-2.a, J8-3.a |
| 34 | Q270 | 단일 | 17 | 2-1.char_count, 2-2.heading, J4-4.a, J8-1.a, J8-2.a, J8-3.a |
| 35 | Q289 | 단일 | 17 | 2-1.char_count, 2-2.heading, 2-2.list, J8-1.a, J8-2.a, J8-3.a |
| 36 | Q298 | 단일 | 17 | 2-1.char_count, 2-2.heading, 2-2.list, J8-1.a, J8-2.a, J8-3.a |
| 37 | Q86 | 단일 | 20 | 1-2.json_syntax, 2-2.json_structure, 3-3.aggregate, 3-3.unique_values, J4-1.b |
| 38 | Q144 | 단일 | 20 | 2-1.charset, 2-2.list, J2-4.a, J2-4.b, J4-1.b |
| 39 | Q62 | 단일 | 19 | 1-2.json_syntax, 2-1.charset, 2-2.json_structure, J2-4.a, J2-4.b |
| 40 | Q92 | 단일 | 18 | 1-2.code_language_label, 1-2.json_syntax, 2-2.json_structure, 3-3.transform, 3-3.unique_values |
| 41 | Q227 | 단일 | 18 | 1-2.code_language_label, 1-2.json_syntax, 1-2.python_syntax, 2-2.json_structure, 3-3.transform |
| 42 | Q162 | 단일 | 17 | 1-2.json_syntax, 2-1.literal, 2-2.json_structure, 3-3.aggregate, 3-3.transform |
| 43 | Q72 | 단일 | 20 | 2-1.charset, 2-2.heading, J3-3.a, J3-3.b |
| 44 | Q314 | 단일 | 20 | J3-3.a, J3-3.b, J3-5.a, J4-1.b |
| 45 | Q143 | 단일 | 19 | 2-1.charset, 2-1.literal, 3-1.question_calculation, J4-1.b |
| 46 | Q4 | 긴 문서 | 18 | J3-3.a, J3-3.b, J7-1.a, J7-2.a |
| 47 | Q115 | 단일 | 18 | J4-1.b, J8-1.a, J8-2.a, J8-3.a |
| 48 | Q113 | 단일 | 17 | J4-4.a, J8-1.a, J8-2.a, J8-3.a |
| 49 | Q136 | 단일 | 17 | 1-2.python_syntax, 2-1.charset, 2-1.literal, 2-2.list |
| 50 | Q54 | 단일 | 20 | 3-1.question_calculation, 3-3.aggregate, 3-3.transform |
| 51 | Q130 | 단일 | 20 | 2-1.charset, 2-1.literal, J4-1.b |
| 52 | Q148 | 단일 | 17 | 2-1.charset, 2-1.literal, J4-4.a |
| 53 | Q187 | 단일 | 15 | 2-1.explicit_empty, 2-1.literal, 2-2.list |
| 54 | Q265 | 단일 | 15 | 2-1.explicit_empty, 2-1.literal, 2-2.list |
| 55 | Q5 | 긴 문서 | 19 | J7-1.a, J7-2.a |
| 56 | Q1 | 긴 문서 | 18 | J7-1.a, J7-2.a |
| 57 | Q2 | 긴 문서 | 18 | J7-1.a, J7-2.a |
| 58 | Q7 | 긴 문서 | 18 | J7-1.a, J7-2.a |
| 59 | Q8 | 긴 문서 | 18 | J7-1.a, J7-2.a |
| 60 | Q10 | 긴 문서 | 18 | J7-1.a, J7-2.a |
| 61 | Q317 | 단일 | 18 | 3-3.aggregate, J4-4.a |
| 62 | Q3 | 긴 문서 | 17 | J7-1.a, J7-2.a |
| 63 | Q6 | 긴 문서 | 17 | J7-1.a, J7-2.a |
| 64 | Q9 | 긴 문서 | 17 | J7-1.a, J7-2.a |
| 65 | Q120 | 단일 | 15 | 1-2.python_syntax, 2-1.explicit_empty |
| 66 | Q57 | 단일 | 19 | J3-5.a |
| 67 | Q61 | 단일 | 19 | 1-2.python_syntax |
| 68 | Q79 | 단일 | 19 | J4-4.a |
| 69 | Q169 | 단일 | 19 | 3-3.aggregate |
| 70 | Q218 | 단일 | 19 | J3-5.a |
| 71 | Q84 | 단일 | 18 | 3-3.aggregate |
| 72 | Q31 | 단일 | 15 | 2-1.explicit_empty |
| 73 | Q71 | 단일 | 15 | 2-1.explicit_empty |
| 74 | Q145 | 단일 | 15 | 2-1.explicit_empty |
| 75 | Q146 | 단일 | 15 | 2-1.explicit_empty |
| 76 | Q271 | 단일 | 15 | 2-1.explicit_empty |
| 77 | Q310 | 단일 | 15 | 2-1.explicit_empty |
| 78 | Q266 | 대화 | 11 | 2-1.explicit_empty |
