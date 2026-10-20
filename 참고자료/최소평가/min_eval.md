# 최소 평가 리스트 (min-eval-list-v2-setcover)

정밀 평가 = 질문 세트 `qs-b468b011e6c3` 전체 326개. 최소 평가 = 그중 **7개** — 아직 덜 채운 기준을 가장 많이 채우는 질문부터(집합 덮기 표준 탐욕법) 평가 기준(64개, 질문으로 셀 수 있는 것)이 모두 **1번 이상** 적용될 때까지 고름.
구성: {'대화': 1, '단일': 5, '긴 문서': 1}. 기준마다 목표 횟수별 필요 질문 수: 1번↑ 7개, 3번↑ 21개, 5번↑ 37개, 10번↑ 78개.
모든 기준이 목표 횟수를 채움.

## 정밀 vs 최소 결과 (같은 실행의 판정, 질문 단위 · 보류는 분모에 둠)

| 모델 | 정밀 PASS·FAIL·보류 / n (통과율) | 최소 PASS·FAIL·보류 / n (통과율) |
|---|---|---|
| aiona | 166·84·76 / 326 (51%) | 4·2·1 / 7 (57%) |
| gpt | 190·77·59 / 326 (58%) | 2·3·2 / 7 (29%) |
| claude | 131·112·83 / 326 (40%) | 2·2·3 / 7 (29%) |
| gemini | 120·124·82 / 326 (37%) | 2·3·2 / 7 (29%) |

순위 — 정밀: gpt > aiona > claude > gemini · 최소: aiona > claude > gemini > gpt · 다름

**FAIL 포착** (FAIL 을 보는 것이 목적): 전체에서 FAIL 이 난 기준 28종류 중 이 목록에서 한 번이라도 보이는 것 **10종류 (36%)** (참고: (모델, 기준) 조합 87개 중 21개 (24%)) · FAIL 사건 586건 중 23건 (4%).

## 고른 질문 (고른 순서)

| 순서 | 질문 | 종류 | 기준 수 | 새로 덮은 기준 |
|---|---|---|---|---|
| 1 | Q12 | 대화 | 36 | 1-1.character_rules, 1-1.foreign_script, 1-1.response_nonempty, 2-2.table, J1-1.a, J1-2.a, J1-3.a, J1-3.b, J1-4.a, J1-4.b, J2-1.a, J2-1.b, J2-2.a, J2-3.a, J2-5.a, J2-5.b, J3-1.a, J3-2.a, J3-2.b, J3-4.a, J3-5.a, J3-5.b, J4-1.a, J4-2.a, J4-3.a, J5-1.a, J5-2.a, J5-2.b, J6-1.a, J6-2.a, J6-2.b, J6-2.c, J6-3.a, J6-3.b, J6-4.a, J6-4.b |
| 2 | Q82 | 단일 | 20 | 1-2.code_block, 1-2.code_fence_min, 1-2.code_language_label, 1-2.json_syntax, 2-2.json_structure, 3-3.aggregate, 3-3.transform, 3-3.unique_values |
| 3 | Q288 | 단일 | 20 | 2-1.char_count, 2-1.charset, 2-2.heading, J2-4.a, J2-4.b, J4-4.a |
| 4 | Q323 | 단일 | 18 | 2-1.count, 2-2.list, J8-1.a, J8-2.a, J8-3.a |
| 5 | Q119 | 단일 | 19 | 1-2.python_syntax, 3-1.question_calculation, J3-3.a, J3-3.b |
| 6 | Q5 | 긴 문서 | 19 | J4-1.b, J7-1.a, J7-2.a |
| 7 | Q187 | 단일 | 15 | 2-1.explicit_empty, 2-1.literal |
