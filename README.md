# ModelGrade · AIONA 채팅 품질 평가

> **English summary** — A reproducible quality benchmark for Korean chat assistants: the same 78 questions (single-turn, 10-turn conversations, attachments, long context, explicit-empty-output) are sent to four models, every answer is judged against 72 criteria (28 deterministic CODE checks + 44 LLM-judged items with per-question target criteria), and the result is the number of fully passing questions per model (/78). Judges: a first-pass API judge with 3-vote majority, then a CLI judge (Codex or Claude) that confirms flagged items with quoted evidence; unverifiable cells are held for a human instead of being guessed. A refine loop (`tools/refine_loop.py`, Harness²-style propose → probe → adopt → lesson) improves the judge prompt and criteria from results and human marks.
> **Quick start**: `powershell -ExecutionPolicy Bypass -File .\install.ps1` (Windows) or `bash install.sh` (Linux · macOS), fill `.env`, then `modelgrade doctor` → `modelgrade login` → `modelgrade start`.

같은 질문 78개를 AIONA · ChatGPT · Claude · Gemini 에 보내고, 72개 기준(CODE 28 + LLM 44 — 기준 8 · 세부 기준 30·점검 항목 44)으로 답을 판정해 모델별 통과 질문 수(/78)를 냅니다. Windows · macOS · Linux, Python 3.10 이상.

## 빠른 시작 — 다른 PC 에서

미리 설치할 것: **Python 3.10+** · **Google Chrome** · **Node.js**(Codex CLI 설치용). 계정: ChatGPT(Codex CLI 로그인 · 답 수집), 수집할 사이트 계정(Claude · Gemini · AIONA), 1차 심판 키(OpenAI 또는 Jev).

**Windows (PowerShell)**

```powershell
git clone https://github.com/yunjae305/ModelGrade.git
cd ModelGrade
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

```powershell
notepad .env
```

```powershell
codex login
```

```powershell
.\modelgrade doctor
```

```powershell
.\modelgrade login
```

```powershell
.\modelgrade start
```

**Linux · macOS (bash)**

```bash
git clone https://github.com/yunjae305/ModelGrade.git && cd ModelGrade && bash install.sh
```

```bash
nano .env
```

```bash
codex login
```

```bash
./modelgrade.sh doctor
```

```bash
./modelgrade.sh login
```

```bash
./modelgrade.sh start
```

| 단계 | 하는 일 |
|---|---|
| 설치 스크립트 | 파이썬 확인 → `.venv` 만들고 패키지 설치 → `.env` 만들기 → Codex CLI 설치(없고 npm 이 있으면) → 점검 |
| `.env` | 1차 심판 키 하나 이상: `OPENAI_API_KEY`(Decisions) 또는 `TYPESAFE_API_KEY`(Jev). AIONA 를 받으면 `AIONA_LOGIN_ID` · `AIONA_LOGIN_PASSWORD`. 저장소에 올라가지 않음 |
| `codex login` | 2차 심판(Codex CLI)이 ChatGPT 계정으로 판정 — 로그인 한 번 |
| `doctor` | 빠진 것을 ✅ / ❌ 로 보여 주고 고치는 명령을 알려 줌(키 값은 출력 안 함) |
| `login` | 수집용 Chrome(전용 프로필)을 띄워 네 사이트 창을 엶 — 처음 한 번 사람이 직접 로그인, 상태는 프로필에 남음 |
| `start` | 메뉴 1(간단 78): 네 모델 동시 수집(웹) → 판정 → `참고자료/결과/<시작 시각>/` 결과 폴더 |

다른 명령은 `.\modelgrade --help`(Windows) · `./modelgrade.sh --help`(Linux · macOS). 아래 표의 `modelgrade` 는 OS 에 맞게 읽으면 됩니다. 실행 래퍼는 `.venv` 의 파이썬을 쓰고, 직접 부를 때는 `python -X utf8 tools/modelgrade.py …` 와 같습니다.

## 흐름

- **수집**: 네 모델 모두 웹(chatgpt.com · claude.ai · gemini.google.com · AIONA). 평소 Chrome 을 전용 프로필 + 디버깅 포트로 띄우고(`tools/chrome_cdp.py`) 프로그램은 포트로만 붙습니다. 답은 복사 아이콘 원문만(페이지 안에서 가로채기 — 시스템 클립보드 · 포커스를 안 써서 수집 중에도 컴퓨터를 쓸 수 있음). 수집기마다 자기 탭을 차지해 같은 사이트의 두 모델도 동시에 받습니다(예: 터미널 두 개에서 `modelgrade start --model claude:Opus 5.5` · `--model claude:Sonnet 5.5`).
- **판정**: CODE 28(프로그램) → 1차 심판(Jev · Decisions, 같은 턴을 3번 판정해 다수결) → Codex CLI 확정(걸린 항목만, 인용 근거 필수) → 안전장치(인용 대조 · 사유와 판정 · 연결 규칙 · 사실 확인 출처) → 결론(보류 우선). `tools/judge_v4.py`.
- **결과 폴더**: `report.html`(결과 보고서) · `판정보고서.html`(보류 칸은 '판단 내보내기' → `modelgrade held-apply`) · `results.jsonl` · `수집기록/`.

## 자주 쓰는 명령

| 할 일 | 명령 |
|---|---|
| 실행 환경 점검 | `modelgrade doctor` |
| 수집용 Chrome 띄우기 · 로그인 | `modelgrade login` |
| 질문 세트 점검(기준별 10문항 이상, 모델 호출 없음) | `python -X utf8 tools/check_question_set.py 참고자료/질문세트_78/questions.json --min-applicable 10` |
| 질문당 기준 수 k 6~10 확인(멀티턴 포함) | `python -X utf8 참고자료/질문세트_78/multiturn_v3/build_conversations.py --check` |
| 멀티턴 턴별 확인 목록 다시 만들기 | `python -X utf8 참고자료/질문세트_78/multiturn_v3/build_turn_checklists.py` |
| 다시 판정(저장 출력 복구 + 바뀐 항목만, 20개씩) | `python -X utf8 tools/judge_v4.py --run <실행> --set 참고자료/질문세트_78 --questions <20개> --out <폴더> --repair` |
| CODE 엔진을 올린 뒤 CODE 평가 다시 계산 → 판정에 반영(심판 호출 없음) | `modelgrade code-eval --run <실행>` → `python -X utf8 tools/judge_v4.py --run <실행> --out <판정 폴더> --set 참고자료/질문세트_78 --recode` |
| 전후 비교 · 새 결과 폴더 발행 | `python -X utf8 tools/judge_compare_runs.py publish --run <실행> --before <전> --after <후> --name "…"` |
| 판정 사유 점검(정의 밖 사유 · 같은 오류 두 번 · 규칙 밖 해당 없음 · 보류 종류) | `python -X utf8 tools/judge_reason_audit.py --run <실행> --judge <판정 폴더> --out <보고.md>` |
| 판정 토큰(질문 × 모델 × 심판) | `python -X utf8 tools/judge_token_report.py --results <results.jsonl> --set 참고자료/질문세트_78 --out 토큰기록.md --html-dir <결과 폴더>` |
| 2차 심판 비교(Codex ↔ Claude CLI) | `python -X utf8 tools/judge_claude_compare.py judge --run <실행> --questions <10~20개> --base <Codex 판정 폴더> --out <폴더>` → `compare` |
| 기준 다듬기 루프(harness2 흐름: 모으기 → 제안(Claude CLI) → 전후 다시 판정 → 채택 → 교훈) | `python -X utf8 tools/refine_loop.py run --run <실행> --judge <판정 폴더> --item J1-4.b [--marks 표시결과.json --apply]` |
| 기준 · 프롬프트 · 코드가 같은 말을 하는지 | `python -X utf8 tools/criteria_consistency.py` |
| 공개본 내보내기(내부 메모 · 수집 답변 원문을 빼고 복사본을 다시 점검) | `python -X utf8 tools/export_public.py --out <빈 폴더>` |

Windows 에서 `python` 대신 `py -3` 이나 `.venv\Scripts\python` 을 써도 됩니다.

## 기준 · 문서

- 기준의 원본: [참고자료/기준/채팅품질 평가기준_v2.md](참고자료/기준/채팅품질%20평가기준_v2.md) — 72개 기준 · 판정 체계 · 결론 규칙 · 안전장치. 기준을 바꿀 때는 이 문서를 먼저 고치고 판정 프롬프트(`참고자료/프롬프트/judge_prompt_v4.txt`) · 코드가 따라갑니다.
- CODE 검사 세부: `참고자료/기준/MEASUREMENT_v16.md` · 기준 목록 `python -X utf8 -m code_eval criteria`.
- 변경 이력: `참고자료/변경내역/`.

## 테스트

```bash
python -X utf8 -m pytest -q
```

표준 테스트는 모델 API 를 호출하지 않습니다(가짜 심판). GitHub Actions 가 Windows · Linux 에서 테스트를, Windows · Linux · macOS 에서 설치 스크립트를 돌립니다. 비밀번호 · API 키는 파일 · 출력에 쓰지 않습니다(`.env` 는 git 제외).

## 기여 · 라이선스

- 구조와 바꾸는 순서는 [CONTRIBUTING.md](CONTRIBUTING.md), 바깥 자료 · 의존성 라이선스는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- 라이선스: [Apache License 2.0](LICENSE).
