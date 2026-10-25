# 제3자 자료 · 라이선스 고지 (Third-party notices)

이 저장소가 쓰거나 참고한 바깥 자료입니다. 코드 의존성의 라이선스는 각 패키지의 배포본을 따릅니다.

## 방식만 가져온 것

- **Harness²** — Google Research, 2026, Apache-2.0 (https://github.com/google-research/harness2). 기준 다듬기 루프(`tools/refine_loop.py`)의 **흐름**(제안 → 시험 → 채택 → 교훈)과 원칙 문구("증거가 뒷받침하는 것만 고친다", "KEEP_BASE 도 답이다", 교훈 = cue · lesson · evidence · confidence)를 따랐습니다. 코드 · 실행기 · 벤치마크는 가져오지 않았습니다.

## 질문 세트 검토에만 쓴 공개 데이터 (저장소에 포함하지 않음)

- **IFEval-Ko** — allganize, Apache-2.0. **KoMT-Bench** — LGAI-EXAONE, LGPL-3.0. 2026-10-25 공개 세트 검토(`참고자료/문서/공개세트_검토_2026-10-25.md`)에서 우리 질문 기준(k 6~10 · 자료 포함)에 맞는 질문이 있는지 보는 데만 썼고, 채택하지 않았습니다. 원본은 git 제외 폴더(`data/`)에만 둡니다.

## 파이썬 의존성 (`requirements.txt`)

| 패키지 | 라이선스 | 쓰임 |
|---|---|---|
| httpx | BSD-3-Clause | 심판 API 호출 |
| pytest | MIT | 시험 |
| tiktoken | MIT | 롱컨텍스트 판별 토큰 수(o200k_base) |
| python-docx | MIT | 첨부 DOCX 생성 · 읽기 |
| fpdf2 | LGPL-3.0 | 첨부 PDF 생성(라이브러리로만 호출) |
| Pillow | HPND(PIL) | 첨부 이미지 |
| pypdfium2 | Apache-2.0 / BSD-3-Clause (PDFium) | PDF 쪽 이미지 |
| playwright | Apache-2.0 | 웹 수집(설치된 Chrome 에 붙음) |

## 보고서 화면

- 글꼴은 Google Fonts 의 IBM Plex Sans KR · IBM Plex Mono(SIL Open Font License 1.1)를 링크로만 씁니다.

## 외부 서비스

- 답변 수집은 ChatGPT · Claude · Gemini 웹과 AIONA 웹의 **사람이 로그인한 화면**에서 복사 아이콘으로 받습니다. 각 서비스의 이용약관은 사용자가 확인합니다. 판정에는 Codex CLI(ChatGPT 구독) · Claude CLI · Jev · Decisions API 를 씁니다.
