# 기여 안내 (Contributing)

이 저장소는 AIONA 채팅 품질 평가 도구입니다. 같은 질문 78개를 네 모델에 보내 답을 모으고, 72개 기준(CODE 28 + LLM 44)으로 판정해 모델별 통과 질문 수(/78)를 냅니다.

## 준비

설치는 README 의 '빠른 시작'과 같습니다 — Windows `powershell -ExecutionPolicy Bypass -File .\install.ps1`, Linux · macOS `bash install.sh`(둘 다 `.venv` · 패키지 · `.env` · 점검). 그다음 시험:

```bash
python -X utf8 -m pytest -q
```

모델 호출 없이 돕니다(약 2~3분, 가짜 심판). 환경 점검은 `modelgrade doctor`, 새 설치 스크립트 · 실행 래퍼(`install.*` · `modelgrade.cmd` · `modelgrade.sh`)를 고치면 두 벌을 같이 고칩니다.

- Python 3.10 이상. Windows 에서 만들었고 Mac · Linux 에서도 돌도록 경로는 `pathlib` 로만 씁니다(절대 경로 · 사용자 이름을 코드에 적지 않습니다).
- 판정은 Codex CLI(ChatGPT 구독) · Claude CLI · Jev · Decisions API 중 있는 것으로 돕니다. 시험은 모두 가짜 심판을 써서 키 없이 돕니다.

## 어디에 무엇이 있나

| 폴더 | 내용 |
|---|---|
| `tools/` | 명령 도구 — 수집(`modelgrade.py` · `chrome_cdp.py`), 판정(`judge_v4.py`), 비교(`judge_compare_runs.py` · `judge_claude_compare.py`), 기준 다듬기 루프(`refine_loop.py`), 기준 재확인(`criteria_consistency.py`), 보고서(`report_html.py` · `build_result_summary.py`) |
| `code_eval/` | CODE 기준 28개 엔진과 LLM 기준 정의(`semantic_items.py`), 심판 클라이언트 |
| `webapp/` | 웹 수집기(ChatGPT · Claude · Gemini · AIONA 탭을 한 Chrome 에서) |
| `참고자료/기준/` | **기준의 원본** `채팅품질 평가기준_v2.md`(72개 · 판정 체계 · 안전장치 · 바뀐 규칙 · 교훈) · CODE 세부 `MEASUREMENT_v16.md` |
| `참고자료/프롬프트/` | 판정 프롬프트 `judge_prompt_v4.txt`(직접 고치는 파일) · 1차 항목 정의 `jev_questions_v4.json` · 질문생성 프롬프트 |
| `참고자료/질문세트_78/` | 질문 세트(첨부 원본 `files/`, 멀티턴 턴별 확인 목록) |
| `참고자료/결과/` | 실행마다 폴더 하나(결과 보고서 · 판정 보고서 · 결과 JSON · 수집 기록) |
| `참고자료/변경내역/` | 날짜별 변경 기록 — 모든 변경은 여기에 한 줄 이상 |
| `tests/` | pytest — 기준 · 문서 · 코드의 버전과 사실이 어긋나지 않는지도 시험합니다 |

## 기준 · 프롬프트를 바꾸는 순서

1. 재료를 모읍니다 — 판정 결과의 FAIL · 보류 칸과 같은 수의 통과 칸, 사람 표시. `tools/refine_loop.py` 가 이 순서(모으기 → 제안 → 같은 칸 전후 재판정 → 채택 판단 → 교훈)를 한 명령으로 돕니다.
2. **기준 문서(v2) → 판정 프롬프트 → 코드(CODE 엔진 · 안전장치) → 시험 → 변경내역** 순으로 고칩니다. 기준의 뜻이 바뀌면 v2 11절에 한 줄(전 → 후 · 완화/엄격) 남깁니다.
3. `python -X utf8 tools/criteria_consistency.py` 로 문서 · 프롬프트 · 코드가 같은 말을 하는지 확인합니다(어긋남이 있으면 종료 코드 1).
4. 질문 세트의 질문 수 · 내용은 바꾸지 않습니다(결과 비교의 기준). 질문을 더하는 것은 새 세트 폴더로.

## 커밋 · 비밀값

- 커밋 메시지는 한 줄 요약 + 무엇을 왜 바꿨는지. 변경내역 파일과 함께 올립니다.
- `.env` · API 키 · 비밀번호 · 로그인 쿠키는 어떤 파일 · 출력에도 적지 않습니다. `data/`(수집 답변 · 판정 기록 · 브라우저 프로필)는 git 에서 제외됩니다.
- 수집한 모델 답변을 공개 저장소에 올리기 전에는 `참고자료/문서/공개_전_점검_2026-10-25.md` 를 확인합니다.
