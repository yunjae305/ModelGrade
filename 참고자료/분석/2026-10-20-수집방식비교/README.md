# 수집 방식 비교 — 지금 방식(데스크톱 앱 + Chrome CDP 웹) vs ego lite (2026-10-20)

결론(사용자 결정 10-20 밤): **ego lite 는 쓰지 않는다.** 바꿔도 "브라우저 창 하나 떠 있고, 사이트마다 사람이 한 번 로그인" 조건이 그대로이고, 수집 시간의 대부분은 모델이 답을 쓰는 시간이라 자동화 층을 바꿔도 빨라지지 않는다.

## ego lite 가 무엇인가 (저장소 citrolabs/ego-lite 확인, 최근 커밋 2026-10-19)
- Citro Labs 의 독자 Chromium 데스크톱 브라우저(비공개 바이너리). 공개된 것은 Node 헬퍼 SDK(`ego-browser`)와 에이전트용 안내서뿐.
- 쓰는 법은 `ego-browser nodejs` 에 JS 를 넘기는 것 하나. Node 스크립트가 `globalThis.ego` 로 떠 있는 브라우저(ego.exe)에 CDP 명령을 보낸다. headless·숨김 모드 없음. Playwright 가 아니라고 명시(`locator()`·`getByRole()` 없음).
- 설계 목적은 Claude Code·Codex 같은 에이전트가 즉석에서 JS 를 써서 사이트를 조작하는 것. 정해진 절차를 수백 번 반복하는 Python 배치와는 방향이 다르다.
- 이 PC 시험: Chrome 데이터 가져오기로는 chatgpt.com·claude.ai·AIONA 로그인이 넘어오지 않았다(수집용 로그인은 별도 프로필). ego lite 안의 chatgpt.com 이 로그아웃 상태라 받은 답 3개(`ego_chatgpt.json`)는 무료 비로그인 ChatGPT 의 답 — GPT-6·Medium 과 비교 불가, 모델 메뉴를 못 찾아 질문마다 17초 허비. 작업 공간이 사람에게 넘어가면(창을 만지면) 에이전트가 되찾아야 하는 절차도 있다.
- 자동 로그인(.env 아이디·비밀번호): AIONA 만 가능(이미 그렇게 함). Google 은 자동화 브라우저의 로그인을 거부("로그인할 수 없음", 오늘 확인), OpenAI·Anthropic 은 봇 확인·2단계 인증·약관 문제 — 어느 브라우저든 같다.

## 지금 방식 시간 (질문 세트 qs-2026-10-21 의 Q22 코드 작성 · Q58 사과문 · Q69 빈 출력 요구, 실행 b2552a20a1a3, 이 PC)

| 모델 | Q22 | Q58 | Q69 빈 출력 | 3개 벽시계 | 비고 |
|---|---:|---:|---:|---:|---|
| GPT 데스크톱 앱 (CDP) | 18.6초 | 16.4초 | 109초 (빈 답) | 2분 44초 | Q69 는 15초 규칙 전 — 규칙 뒤면 약 18초 |
| Claude 데스크톱 앱 (UIA) | 10.4초 | 9.6초 | 8.1초 (25자 답) | 48초 | 이 PC 의 Claude 앱 창 = Claude Code 세션 창 |
| Gemini 웹 (Chrome CDP) | 18초 | 22초 | 18초 (→ 보류) | 1분 36초 | 15초 규칙 적용 첫 사례, 스크린샷 확인(답 말풍선 없음) |
| AIONA 웹 (Chrome CDP, 자동 로그인) | 12.6초 | 8.0초 | 6.3초 (빈 답) | 35초 | |

시간은 전송부터 복사까지(`collection.latency_ms`). 벽시계는 새 채팅·모델 설정·CODE 평가 포함. 처음 쓰는 PC 라 playwright·pywinauto 설치, 수집용 Chrome 프로필 로그인(`python tools/chrome_cdp.py --login` 뒤 **창을 닫아야** 포트 Chrome 이 뜬다), ChatGPT 앱 `--restart` 가 먼저 필요했다.

## 함께 넣은 규칙 — 빈 출력 요구 질문은 15초 (사용자 규칙 10-20 밤)
빈 출력을 요구한 질문은 전송 확인 뒤 **15초 안에 답 말풍선과 복사 버튼이 안 뜨면**('출력 없음'·응답 중지·말풍선 없음) 더 기다리지 않고 `COPY_FAILED`(보류)로 남긴다. 판정은 이미 빈 출력 요구를 모두 보류로 두므로(empty-output-hold-v5) 결과는 같고 대기만 짧아진다. 적용: `webapp/cdp_chat_web.py`(ChatGPT 웹·앱, Gemini, Claude 웹) · `webapp/uia_chat_app.py`(Claude 앱) · `webapp/aiona_web.py`.

## 파일
- `ego_lite_collect.py` — ego-browser CLI 로 ChatGPT·Claude 웹을 받던 시험 스크립트(기록용, tools/ 에서 뺌). 조작 순서는 cdp_chat_web 과 같게 맞춤.
- `ego_chatgpt.json` — 로그아웃 상태에서 받은 결과(비교 불가, 기록용).
