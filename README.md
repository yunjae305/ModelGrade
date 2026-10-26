# ModelGrade · AIONA 채팅 품질 평가

> **English summary** — ModelGrade is a reproducible quality evaluation for Korean chat assistants. It sends the same 78 real-use questions (single-turn, 10-turn conversations, attachments, long context, explicit-empty-output) to four chat services through their web UIs, judges every answer against 72 criteria (28 deterministic CODE checks + 44 LLM-judged items, only the criteria each question targets), and reports how many questions each model fully passes (/78). Every LLM verdict must quote the answer it relies on; cells that cannot be verified are held for a human instead of being guessed. Results and human marks feed a refine loop that improves the criteria and the judge prompt.
> **Quick start**: `powershell -ExecutionPolicy Bypass -File .\install.ps1` (Windows) or `bash install.sh` (Linux · macOS), fill `.env`, then `modelgrade doctor` → `modelgrade login` → `modelgrade start`.

ModelGrade 는 **한국어 채팅 AI 의 답변 품질을 같은 조건으로 비교하는 평가 프로그램**입니다. AIONA · ChatGPT · Claude · Gemini 에 같은 질문 78개를 보내 답을 모으고, 답마다 72개 기준(CODE 28 + LLM 44 — 기준 8 · 세부 기준 30·점검 항목 44)으로 판정해 **모델별로 모든 기준을 통과한 질문 수(/78)** 를 냅니다. 벤치마크 점수가 아니라 실제로 쓰는 질문에 대한 답을 봅니다.

## 무엇을 하나

1. **질문 세트** — 실제 사용을 본뜬 질문 78개(단일 질문 67 · 10턴 대화 11). 첨부 파일을 읽는 질문, 약 30만 토큰의 긴 자료를 주는 질문, 아무것도 출력하지 말라는 질문도 들어 있습니다. 질문마다 노리는 기준이 정해져 있습니다(질문당 6~10개).
2. **수집** — 네 서비스의 웹 화면에 질문을 보내고, 답변 아래 복사 버튼으로 원문을 받습니다. 대화 질문은 한 채팅에서 1턴부터 10턴까지 이어 묻습니다. 처음 한 번만 사람이 로그인합니다.
3. **판정** — 답마다 그 질문이 노리는 기준만 봅니다.
   - **CODE 28개**: 글자 수 · 표 · 코드블록 · JSON 구문 · 요구한 계산값 · 금지 표현처럼 프로그램이 정확히 잴 수 있는 것.
   - **LLM 44개**: 요청 수행 · 조건 준수 · 사실과 자료 충실성 · 내부 모순 · 멀티턴 맥락 유지처럼 뜻을 봐야 하는 것. 단일 질문은 1차 심판 둘(Jev · Decisions API)이 함께 보고 둘이 같은 답으로 확실할 때만 끝내며, 멀티턴 · 첨부 · 긴 자료는 Decisions 가 봅니다. 어긋나거나 애매하거나 FAIL 인 칸은 2차 심판(Codex CLI)이 답변 문장을 인용해 확정합니다.
   - **안전장치**: 인용이 답변에 실제로 있는지, 사유와 판정이 맞는지 등을 프로그램이 다시 확인합니다.
   - **보류**: 근거를 확인할 수 없으면 추측하지 않고 보류로 남겨 사람이 정합니다.
4. **결과** — 실행마다 `참고자료/결과/<시작 시각>/` 폴더 하나를 만듭니다.

## 무엇을 얻나

- **결과 보고서** `report.html`: 모델별 통과 질문 수(/78), 기준별 · 질문 유형별(단일 · 대화 · 첨부 · 긴 자료) 통과, 수집 · 판정 시간.
- **판정 보고서** `판정보고서.html`: 칸마다 질문 · 답변 원문, 판정과 사유, 근거로 든 답변 문장. 보류 칸은 여기서 사람이 판단해 반영합니다.
- **원자료**: 판정 결과 JSON(`results.jsonl`)과 수집 기록(`수집기록/`). 같은 질문 세트로 다시 돌려 전후를 비교할 수 있습니다.

## 다른 점

- **기준별로 판정합니다.** 답 하나에 점수 하나를 주지 않고, 그 질문이 노리는 기준마다 지켰는지를 따로 봅니다. 어느 기준에서 떨어졌는지가 남습니다.
- **근거 없이는 FAIL 이 없습니다.** 2차 심판은 답변 문장을 인용해야 하고, 인용이 원문에 없으면 다시 묻거나 보류합니다.
- **돌릴수록 기준이 다듬어집니다.** 판정 결과 · 보류 · 사람 표시를 재료로 기준 문서와 판정 프롬프트의 잘못을 찾아 고치는 루프가 들어 있습니다.

## 빠른 시작 — 다른 PC 에서

미리 설치할 것: **Python 3.10+** · **Google Chrome** · **Node.js**(Codex CLI 설치용). 계정: ChatGPT(Codex CLI 로그인 · 답 수집), 수집할 사이트 계정(Claude · Gemini · AIONA), 1차 심판 키(OpenAI 또는 Jev). Windows · macOS · Linux 에서 돕니다.

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
| `start` | 메뉴 1(간단 78): 네 모델 동시 수집 → 판정 → `참고자료/결과/<시작 시각>/` 결과 폴더 |

다른 명령은 `.\modelgrade --help`(Windows) · `./modelgrade.sh --help`(Linux · macOS).

## 더 보기

- **기준의 원본**: [참고자료/기준/채팅품질 평가기준_v2.md](참고자료/기준/채팅품질%20평가기준_v2.md) — 72개 기준 · 판정 체계 · 결론 규칙 · 안전장치.
- **변경 이력**: `참고자료/변경내역/`.
- **개발 · 운영 명령**(다시 판정 · 비교 · 기준 다듬기 루프 · 시험 등)과 폴더 구조: [CONTRIBUTING.md](CONTRIBUTING.md).
- **라이선스**: [Apache License 2.0](LICENSE) · 바깥 자료와 의존성은 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
