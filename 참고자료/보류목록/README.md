# 보류 목록 — 실행 8c61f117e661 (사람이 PASS/FAIL 을 정할 판정 1,034행)

`8c61f117e661-held.csv` 를 엑셀로 열어 **gold 열**에 `PASS` 또는 `FAIL` 을 적는다(확인할 수 없으면 `UNVERIFIABLE`, 그 행은 보류로 남음). `reviewer_note` 에 이유를 적어도 된다. 다른 열은 고치지 않는다.

| 열 | 뜻 |
|---|---|
| system | 모델 (aiona · gpt · claude · gemini) |
| case_id · turn | 질문 번호와 턴 (대화는 턴마다 한 행일 수 있음) |
| criterion | 보류된 기준 (J5 714 · J2 107 · J6 99 · 빈 출력 40 · J3 31 · J4 28 · J1 15) |
| cause_label | 왜 보류인가 — 확인 못 함 522 · 출처 원문 확인 실패 192 · 심판 갈림 162 · 확인 필요 99 · 빈 출력 요구 40 · 미완료 19 |
| judge_reason · claims · failed_sources | 심판이 남긴 사유·주장·못 연 출처 |
| question · answer | 원문 발췌 (전문은 평가 화면에서) |

기입이 끝나면 (평가를 돌린 PC 에서):
```bash
python -X utf8 tools/k_split_from_run.py 8c61f117e661 --gold 참고자료/보류목록/8c61f117e661-held.csv
```
→ 보류를 반영한 k 별 갈림 표와 모델별 PASS·FAIL·보류 / 326 이 `참고자료/최소평가/k별_갈림_8c61f117e661_보류반영.md` 로 나온다.
규칙: 그 (모델, 질문)의 보류 행 중 하나라도 FAIL 이면 그 질문은 FAIL, 모두 PASS 면 PASS, 비었거나 UNVERIFIABLE 이 남으면 보류 유지.
