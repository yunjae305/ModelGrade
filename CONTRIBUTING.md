# 기여 안내 (Contributing)

이 저장소는 AIONA 채팅 품질 평가 도구입니다. 같은 질문 78개를 네 모델에 보내 답을 모으고, 72개 기준(CODE 28 + LLM 44)으로 판정해 모델별 통과 질문 수(/78)를 냅니다.

## 준비

설치는 README 의 '빠른 시작'과 같습니다 — 받아 둔 폴더에서 Windows `powershell -ExecutionPolicy Bypass -File .\install.ps1`, macOS · Linux `bash install.sh`(둘 다 파이썬 · Chrome · Node.js · `.venv` · 패키지 · `.env` · Codex · Claude CLI · 전역 `modelgrade` 명령 · 처음 설정). 묻지 않게 하려면 `MODELGRADE_NONINTERACTIVE=1`, CLI 설치를 건너뛰려면 `MODELGRADE_SKIP_CODEX=1` · `MODELGRADE_SKIP_CLAUDE=1`. 그다음 시험:

```bash
python -X utf8 -m pytest -q
```

모델 호출 없이 돕니다(약 1~2분, 가짜 심판). 환경 점검은 `modelgrade doctor`, 새 설치 스크립트 · 실행 래퍼(`install.*` · `modelgrade.cmd` · `modelgrade.sh`)를 고치면 두 벌을 같이 고칩니다.

- Python 3.10 이상. Windows 에서 만들었고 Mac · Linux 에서도 돌도록 경로는 `pathlib` 로만 씁니다(절대 경로 · 사용자 이름을 코드에 적지 않습니다).
- 판정은 Codex CLI(ChatGPT 구독) · Claude CLI · Jev · Decisions API 중 있는 것으로 돕니다. 시험은 모두 가짜 심판을 써서 키 없이 돕니다.

## 어디에 무엇이 있나

| 폴더 | 내용 |
|---|---|
| `tools/` | 명령 도구 — 아래 '도구 한눈에' |
| `code_eval/` | CODE 기준 28개 엔진(`engine.py` · `checks_*.py`)과 LLM 기준 정의(`semantic_items.py`), 기준 목록(`criteria_registry.py`), 심판 클라이언트(Decisions · Jev · Codex CLI · Claude CLI · Claude API) |
| `webapp/` | 웹 수집기 — ChatGPT · Claude · Gemini(`cdp_chat_web.py`) · AIONA(`aiona_web.py`, 화면 HTML → 마크다운은 `ingestion/html_markdown.py`)를 한 Chrome 의 탭에서. 질문 결론 규칙(`run_summary.py` — 빈 출력 요구 보류 · 질문 단위 판정). 질문 자동 생성기(`question_generation.py` · `api_request.py` · `attachment_files.py`, 지금 흐름에서는 쓰지 않지만 질문 생성을 손볼 때 쓰려고 남김) |
| `참고자료/기준/` | **기준의 원본** `채팅품질 평가기준_v2.md`(72개 · 판정 체계 · 안전장치 · 바뀐 규칙 · 교훈) · CODE 세부 `MEASUREMENT_v16.md` |
| `참고자료/프롬프트/` | 판정 프롬프트 `judge_prompt_v4.txt`(직접 고치는 파일) · 1차 항목 정의 `jev_questions_v4.json` · 질문생성 프롬프트 |
| `참고자료/질문세트_78/` | 질문 세트(첨부 원본 `files/`, 멀티턴 턴별 확인 목록) |
| `참고자료/결과/` | 실행마다 폴더 하나(결과 보고서 · 판정 보고서 · 결과 JSON · 수집 기록) |
| `참고자료/변경내역/` | 날짜별 변경 기록 — 모든 변경은 여기에 한 줄 이상 |
| `tests/` | pytest — 기준 · 문서 · 코드의 버전과 사실이 어긋나지 않는지도 시험합니다 |

## 동작 세부

- **수집**: 네 모델 모두 웹(chatgpt.com · claude.ai · gemini.google.com · AIONA). 평소 Chrome 을 전용 프로필 + 디버깅 포트로 띄우고(`tools/chrome_cdp.py`) 프로그램은 포트로만 붙습니다. 답은 복사 아이콘 원문만(페이지 안에서 가로채기 — 시스템 클립보드 · 포커스를 안 써서 수집 중에도 컴퓨터를 쓸 수 있음). 수집기마다 자기 탭을 차지해 같은 사이트의 두 모델도 동시에 받습니다(예: 터미널 두 개에서 `modelgrade start --model claude:Opus 5.5` · `--model claude:Sonnet 5.5`).
- **판정**: CODE 28(프로그램) → 1차 심판(상황별 — 단일 질문은 Jev 2번(갈리면 3번째) + Decisions 1번 교차: 같은 답이고 둘 다 0.8 이상이면 끝 · 멀티턴 · 첨부 · 롱컨텍스트는 Decisions 1번 + 선별 규칙, `--first-judge auto` 는 옛 크기 나눔) → Codex CLI 확정(넘긴 칸만, 인용 근거 필수) → 안전장치(인용 대조 · 사유와 판정 · 연결 규칙 · 사실 확인 출처) → 결론(보류 우선). `tools/judge_v4.py`.
- **결과 폴더**: `report.html` · `판정보고서.html`(보류 칸은 '판단 내보내기' → `modelgrade held-apply`) · `results.jsonl` · `수집기록/`.
- 실행 래퍼(`modelgrade.cmd` · `modelgrade.sh`)는 `.venv` 의 파이썬을 쓰고, 직접 부를 때는 `python -X utf8 tools/modelgrade.py …` 와 같습니다.

## 자주 쓰는 명령

아래 `modelgrade` 는 OS 에 맞게(`.\modelgrade` · `./modelgrade.sh`) 읽습니다. Windows 에서 `python` 대신 `py -3` 이나 `.venv\Scripts\python` 을 써도 됩니다.

| 할 일 | 명령 |
|---|---|
| 실행 환경 점검 | `modelgrade doctor` |
| 처음 설정 — .env 키 · Codex · Claude CLI 로그인 · 웹 로그인 · 점검 | `modelgrade setup` (`--keys` · `--cli` · `--web` 로 하나만) |
| 수집용 Chrome 띄우기 · 웹 로그인 · 로그인 확인 | `modelgrade login` |
| AIONA 두 모델을 한 번에 받아 비교(한 실행 · 한 보고서) | `modelgrade start --aiona "자동 추천" --aiona2 "Claude Sonnet 5.5"` |
| 업데이트 전 vs 후 — 두 실행의 AIONA 답을 묶어 같은 기준으로 판정 | `modelgrade pair --before <전 실행> --after <후 실행> --judge y` |
| 질문 세트 점검(기준별 10문항 이상, 모델 호출 없음) | `python -X utf8 tools/check_question_set.py 참고자료/질문세트_78/questions.json --min-applicable 10` |
| 질문당 기준 수 k 6~10 확인(멀티턴 포함) | `python -X utf8 참고자료/질문세트_78/multiturn_v3/build_conversations.py --check` |
| 멀티턴 턴별 확인 목록 다시 만들기 | `python -X utf8 참고자료/질문세트_78/multiturn_v3/build_turn_checklists.py` |
| 다시 판정(저장 출력 복구 + 바뀐 항목만, 20개씩) | `python -X utf8 tools/judge_v4.py --run <실행> --set 참고자료/질문세트_78 --questions <20개> --out <폴더> --repair` |
| CODE 엔진을 올린 뒤 CODE 평가 다시 계산 → 판정에 반영(심판 호출 없음) | `modelgrade code-eval --run <실행>` → `python -X utf8 tools/judge_v4.py --run <실행> --out <판정 폴더> --set 참고자료/질문세트_78 --recode` |
| 전후 비교 · 새 결과 폴더 발행 | `python -X utf8 tools/judge_compare_runs.py publish --run <실행> --before <전> --after <후> --name "…"` |
| 사람 표시용 표(칸마다 질문 · 답변 원본과 AI 판정 → 사람이 PASS · FAIL · 에러 케이스를 고름, 더블클릭으로 열기) | `python -X utf8 tools/build_calibration_sheet.py --run <실행> --pick-file <고를칸.json> --dest <폴더>` (`--out <판정 폴더>` 만 주면 FAIL · PASS 칸을 고루 뽑음) |
| 판정 사유 점검(정의 밖 사유 · 같은 오류 두 번 · 규칙 밖 해당 없음 · 보류 종류) | `python -X utf8 tools/judge_reason_audit.py --run <실행> --judge <판정 폴더> --out <보고.md>` |
| 판정 토큰(질문 × 모델 × 심판) | `python -X utf8 tools/judge_token_report.py --results <results.jsonl> --set 참고자료/질문세트_78 --out 토큰기록.md --html-dir <결과 폴더>` |
| 1차 심판 비교(Jev · Decisions 를 각각 고정해 3번씩, Codex 안 부름, 20개씩) | `python -X utf8 tools/judge_v4.py --run <실행> --set 참고자료/질문세트_78 --questions <20개> --out first-jev --first-judge jev --first-only --votes 3` (decisions 도 같게) → `python -X utf8 tools/judge_first_compare.py` |
| 첫 Codex 호출을 한 번으로(시험 — 묶음 대신 첨부 없는 단일 질문 전부, 저장된 1차 그대로) | `python -X utf8 tools/judge_v4.py --run <실행> --set 참고자료/질문세트_78 --pick <질문:모델,…> --out <폴더> --reuse-first <1차 폴더> --codex-one-call stdin` (`file` 은 입력을 파일 하나로 두고 Codex 가 셸로 읽음) |
| 2차 심판 비교(Codex ↔ Claude CLI) | `python -X utf8 tools/judge_claude_compare.py judge --run <실행> --questions <10~20개> --base <Codex 판정 폴더> --out <폴더>` → `compare` |
| 기준 다듬기 루프(harness2 흐름: 모으기 → 제안(Claude CLI) → 전후 다시 판정 → 채택 → 교훈) | `python -X utf8 tools/refine_loop.py run --run <실행> --judge <판정 폴더> --item J1-4.b [--marks 표시결과.json --apply]` |
| 기준 · 프롬프트 · 코드가 같은 말을 하는지 | `python -X utf8 tools/criteria_consistency.py` |
| 이력 없는 깨끗한 사본 만들기(회의록만 빼고 복사본을 다시 점검) | `python -X utf8 tools/export_public.py --out <빈 폴더>` (`--no-results` 로 결과 폴더 빼기) |

표준 시험은 모델 API 를 호출하지 않습니다(가짜 심판). GitHub Actions 가 Windows · Linux 에서 시험을, Windows · Linux · macOS 에서 설치 스크립트를 돌립니다.

## 도구 한눈에 (`tools/`)

쓰는 법은 `python -X utf8 tools/<파일> --help`.

| 묶음 | 도구 |
|---|---|
| 실행 | `modelgrade.py`(start · collect · judge · run · code-eval · report · held · held-apply · doctor · login · setup — 실행 래퍼 `modelgrade` 가 부름) · `setup_wizard.py`(처음 설정) · `chrome_cdp.py`(수집용 Chrome — 전용 프로필 + 디버깅 포트) · `web_chat_login.py`(AIONA · FactChat · 우수AI 로그인 창) · `doctor.py`(실행 환경 점검) |
| 판정 | `judge_v4.py`(지금 판정 — `start` 가 씀) · `judge_folder.py`(한 모델의 입력을 폴더에 모아 Codex 세션 하나가 판정) · `judge_item_rerun.py`(기준 하나만 다시 판정 · 다수결) · `build_judge_prompt_v4.py`(판정 프롬프트 · 1차 항목 파일의 경로를 `judge_v4.py` 에 주고, v4 를 처음 만든 생성기 — 프롬프트는 `judge_prompt_v4.txt` 를 직접 고침) |
| 옛 판정(v3, `modelgrade judge`) | `judge_run.py` · `judge_split.py` · `judge_inputs.py` · `build_judge_situations.py` — `judge_v4.py` 가 심판 호출 · 연결 규칙을 이 파일들에서 가져다 씀 |
| 비교 · 점검 · 다듬기 | `judge_compare_runs.py`(전후 비교 · 새 결과 폴더) · `judge_first_compare.py`(1차 심판 Jev ↔ Decisions 3번씩) · `judge_claude_compare.py`(2차 심판 Codex ↔ Claude CLI) · `judge_reason_audit.py`(판정 사유 점검) · `criteria_consistency.py`(문서 · 프롬프트 · 코드 대조) · `refine_loop.py`(기준 다듬기 루프) · `build_calibration_sheet.py`(사람 표시용 표) · `check_question_set.py`(질문 세트 점검) |
| 결과 · 보고 | `build_result_summary.py`(발표 페이지 · 결과 폴더의 `report.html`) · `report_html.py` · `result_dirs.py` · `result_flow.py`(앞 셋은 다른 도구가 쓰는 부품) · `judge_token_report.py`(판정 토큰) · `export_results_json.py` · `export_held.py`(보류 · 오류 CSV) · `export_review.py`(옛 비교 실행 형식의 검수 CSV) · `build_prompt_diff_page.py`(기준 · 프롬프트 변경 원문 페이지) · `export_public.py`(공개본 사본) |
| 질문 고르기 | `min_eval_list.py`(기준을 모두 덮는 최소 질문 목록) · `question_gradient.py`(질문을 늘릴 때 커버리지 · 순위 기울기) — 옛 326 세트 실행 기록을 읽음. 지금 세트 조립은 `참고자료/질문세트_78/multiturn_v3/` |

## 기준 · 프롬프트를 바꾸는 순서

1. 재료를 모읍니다 — 판정 결과의 FAIL · 보류 칸과 같은 수의 통과 칸, 사람 표시. `tools/refine_loop.py` 가 이 순서(모으기 → 제안 → 같은 칸 전후 재판정 → 채택 판단 → 교훈)를 한 명령으로 돕니다.
2. **기준 문서(v2) → 판정 프롬프트 → 코드(CODE 엔진 · 안전장치) → 시험 → 변경내역** 순으로 고칩니다. 기준의 뜻이 바뀌면 v2 11절에 한 줄(전 → 후 · 완화/엄격) 남깁니다.
3. `python -X utf8 tools/criteria_consistency.py` 로 문서 · 프롬프트 · 코드가 같은 말을 하는지 확인합니다(어긋남이 있으면 종료 코드 1).
4. 질문 세트의 질문 수 · 내용은 바꾸지 않습니다(결과 비교의 기준). 질문을 더하는 것은 새 세트 폴더로.

## 커밋 · 비밀값

- 커밋 메시지는 한 줄 요약 + 무엇을 왜 바꿨는지. 변경내역 파일과 함께 올립니다.
- `.env` · API 키 · 비밀번호 · 로그인 쿠키는 어떤 파일 · 출력에도 적지 않습니다. `data/`(수집 답변 · 판정 기록 · 브라우저 프로필)는 git 에서 제외됩니다.
- 수집한 모델 답변을 공개 저장소에 올리기 전에는 `참고자료/문서/공개_전_점검_2026-10-25.md` 를 확인합니다.
