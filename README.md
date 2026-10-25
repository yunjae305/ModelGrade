# ModelGrade · AIONA 채팅 품질 평가

> **English summary** — A reproducible quality benchmark for Korean chat assistants: the same 78 questions (single-turn, 10-turn conversations, attachments, long context, explicit-empty-output) are sent to four models, every answer is judged against 72 criteria (28 deterministic CODE checks + 44 LLM-judged items with per-question target criteria), and the result is the number of fully passing questions per model (/78). Judges: a first-pass API judge with 3-vote majority, then a CLI judge (Codex or Claude) that confirms flagged items with quoted evidence; unverifiable cells are held for a human instead of being guessed. A refine loop (`tools/refine_loop.py`, Harness²-style propose → probe → adopt → lesson) improves the judge prompt and criteria from results and human marks.

같은 질문 78개를 AIONA · GPT · Claude · Gemini 에 보내고, 72개 기준(CODE 28 + LLM 44 — 기준 8 · 세부 기준 30·점검 항목 44)으로 답을 판정해 모델별 통과 질문 수(/78)를 냅니다. Windows · Python 3.10 이상. 터미널(PowerShell)에서 실행합니다.

## 설치

```powershell
git clone https://github.com/yunjae305/ModelGrade.git
Set-Location ModelGrade
py -3 -m pip install -r requirements.txt
Copy-Item .env.example .env   # 심판 API 키 · 수집 로그인 정보 — 저장소에 넣지 않음
```

`data/`(수집 답변 · 판정 기록)는 실행 때 생깁니다. 결과는 `참고자료/결과/<시작 시각>/` 에 폴더째 남깁니다.

## 흐름

```powershell
py -3 -X utf8 tools/modelgrade.py start          # 1 간단 78 — 네 모델 동시 수집(웹) → 판정 → 결과 폴더
```

- 수집: 네 모델 모두 웹(chatgpt.com · claude.ai · gemini.google.com · dev.aiona.kr) — 평소 Chrome 을 전용 프로필 + 디버깅 포트로 띄우고(`tools/chrome_cdp.py`) 프로그램은 포트로만 붙는다. 답은 복사 아이콘 원문만(페이지 안에서 가로채기, 클립보드 · 포커스 안 씀 → 수집 중에도 컴퓨터 사용 가능, Windows · macOS). 로그인은 사람이 그 Chrome 창에서 한 번(`py -3 -X utf8 tools/chrome_cdp.py --login`), AIONA 는 `.env`.
- 판정: CODE 28(프로그램) → 1차 심판(Jev · Decisions, 같은 턴을 3번 판정해 다수결 — `.env FIRST_JUDGE_VOTES` · `--votes`) → Codex CLI 확정(첨부 없는 단일 질문은 N개를 한 번에 — `.env CODEX_BATCH` · `--codex-batch`, 묶음 글자 합은 `CODEX_BATCH_CHARS` 까지) → 안전장치 → 결론(보류 우선). `tools/judge_v4.py`.
- 결과 폴더: `report.html`(결과 보고서) · `판정보고서.html`(보류 칸은 '판단 내보내기' → `modelgrade held-apply`) · `results.jsonl` · `수집기록/`. 판정 토큰 표(`판정토큰.html` · `토큰기록.md`)는 아래 명령으로 따로 만듭니다.

## 자주 쓰는 명령

| 할 일 | 명령 |
|---|---|
| 질문 세트 점검(기준별 10문항 이상, 모델 호출 없음) | `py -3 -X utf8 tools/check_question_set.py 참고자료/질문세트_78/questions.json --min-applicable 10` |
| 질문당 기준 수 k 6~10 확인(멀티턴 포함) | `py -3 -X utf8 참고자료/질문세트_78/multiturn_v3/build_conversations.py --check` |
| 멀티턴 턴별 확인 목록 다시 만들기 | `py -3 -X utf8 참고자료/질문세트_78/multiturn_v3/build_turn_checklists.py` |
| 다시 판정(저장 출력 복구 + 바뀐 항목만, 20개씩 나눠) | `py -3 -X utf8 tools/judge_v4.py --run <실행> --set 참고자료/질문세트_78 --questions <20개> --providers aiona,gpt,claude,gemini --out <폴더> --repair` |
| 전후 비교 · 새 결과 폴더 발행 | `py -3 -X utf8 tools/judge_compare_runs.py publish --run <실행> --before <전> --after <후> --name "…"` |
| 판정 토큰(질문 × 모델 × 심판 · 질문 합계 · 전체) | `py -3 -X utf8 tools/judge_token_report.py --results <results.jsonl> --set 참고자료/질문세트_78 --out 토큰기록.md --html-dir <결과 폴더>` |
| 기준 하나 다시 판정 · 다수결(Codex · Claude CLI) | `tools/judge_item_rerun.py` · `tools/judge_claude_compare.py` |
| 2차 심판 비교(Codex ↔ Claude CLI, 같은 1차 결과) | `py -3 -X utf8 tools/judge_claude_compare.py judge --run <실행> --questions <10~20개> --base <Codex 판정 폴더> --out <폴더> [--no-publish]` → `compare` |
| 기준 다듬기 루프(harness2 흐름: 모으기 → 제안(Claude CLI) → 전후 다시 판정 → 채택 → 교훈) | `py -3 -X utf8 tools/refine_loop.py run --run <실행> --judge <판정 폴더> --item J1-4.b [--types 싱글 --marks 표시결과.json --apply]` |
| 심판 표시용 표(FAIL 60 · 통과 40 사람 표시) | `py -3 -X utf8 tools/build_calibration_sheet.py --run <실행> --out <판정 폴더>` |
| 판정 프롬프트 변경 원문 페이지 | `py -3 -X utf8 tools/build_prompt_diff_page.py` |

## 기준 · 문서

- 기준의 원본: [참고자료/기준/채팅품질 평가기준_v2.md](참고자료/기준/채팅품질%20평가기준_v2.md) — 72개 기준 · 판정 체계 · 결론 규칙 · 안전장치. 기준을 바꿀 때는 이 문서를 먼저 고치고 프롬프트(`참고자료/프롬프트/judge_prompt_v4.txt`, 생성기 `tools/build_judge_prompt_v4.py`) · 코드가 따라갑니다.
- CODE 검사 세부: `참고자료/기준/MEASUREMENT_v16.md` · 기준 목록 `py -3 -X utf8 -m code_eval criteria`.
- 작업 이어받기: `참고자료/이어서진행_YYYY-MM-DD.md` · 변경 이력: `참고자료/변경내역/`.
- 발표: `참고자료/발표/`(`미팅 개선 체크리스트.html` · 결과 보고서 빌더 `tools/build_result_summary.py`).

## 테스트

```powershell
py -3 -X utf8 -m pytest -q
```

표준 테스트는 모델 API 를 호출하지 않습니다. 비밀번호 · API 키는 파일 · 출력에 쓰지 않습니다(`.env` 는 git 제외).

## 저장소 구조 · 기여 · 라이선스

- 구조와 바꾸는 순서는 [CONTRIBUTING.md](CONTRIBUTING.md), 바깥 자료 · 의존성 라이선스는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- 기준 · 프롬프트 · 코드가 같은 말을 하는지: `py -3 -X utf8 tools/criteria_consistency.py` (CI 에서도 돌림, 어긋남이 있으면 실패).
- 라이선스는 공개 전에 정합니다 — `참고자료/문서/공개_전_점검_2026-10-25.md` (라이선스 · 저장소 이름 · 수집 답변 공개 여부 · 내부 정보).
