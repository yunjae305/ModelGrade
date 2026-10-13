"""가져오기 형식과 플랫폼별 수집 지원 현황 (2026-10-13 공식 문서 확인 기준).

'file_import' 는 사용자가 공식 내보내기·직접 저장한 파일을 가져오는 경로, 'api_import' 는 이미 받은 API 응답 파일,
'live_api' 는 이 서버의 기존 수집 경로로 직접 호출하는 경로다. 실제로 연결·검증하지 않은 플랫폼을 연동 완료로
표시하지 않는다 (verified 는 이 저장소의 테스트가 확인한 범위).
"""
from __future__ import annotations

from typing import Any, Dict, List

FORMATS: List[Dict[str, Any]] = [
    {"id": "collection", "label": "ModelGrade 공통 수집 형식", "schema": "modelgrade-collection/v1", "access_mode": "모두"},
    {"id": "transcript", "label": "대화 기록 JSON (역할별 메시지)", "schema": "modelgrade-transcript/v1", "access_mode": "web·platform"},
    {"id": "api_responses", "label": "API 응답 파일 (질문 + 원본 응답)", "schema": "modelgrade-api-responses/v1", "access_mode": "api"},
    {"id": "agent_run", "label": "에이전트 실행 기록", "schema": "modelgrade-agent-run/v1", "access_mode": "agent"},
    {"id": "chatgpt_export", "label": "ChatGPT 공식 내보내기 conversations.json · chat.html", "access_mode": "web"},
    {"id": "claude_export", "label": "Claude.ai 내보내기 conversations.json", "access_mode": "web"},
    {"id": "html", "label": "저장한 HTML (메시지 역할 표시가 있거나, 질문을 함께 입력한 단일 답변)", "access_mode": "web"},
    {"id": "agents_sdk", "label": "OpenAI Agents SDK trace·span", "access_mode": "agent"},
    {"id": "langsmith", "label": "LangSmith run", "access_mode": "agent"},
    {"id": "otel", "label": "OpenTelemetry GenAI span (OTLP JSON)", "access_mode": "agent"},
    {"id": "legacy", "label": "기존 Q/A 파일·비교 실행 JSON·모델별 원문 JSON", "access_mode": "모두"},
]

PLATFORMS: List[Dict[str, Any]] = [
    {"name": "OpenAI API (GPT)", "paths": ["live_api", "api_import"], "status": "기존 연결 재사용 · 응답 파일 가져오기",
     "sources": "Responses url_citation", "note": "실제 유료 호출은 승인 후 진행"},
    {"name": "Anthropic API (Claude)", "paths": ["live_api", "api_import"], "status": "기존 연결 재사용 · 응답 파일 가져오기",
     "sources": "text.citations (web_search_result_location)", "note": "실제 유료 호출은 승인 후 진행"},
    {"name": "Gemini API", "paths": ["live_api", "api_import"], "status": "기존 연결 재사용 · 응답 파일 가져오기",
     "sources": "groundingMetadata (중계 URL)", "note": "Grounding with Google Search 결과 분석은 약관 검토 전 보류"},
    {"name": "ChatGPT (웹·앱)", "paths": ["file_import"], "status": "공식 내보내기 파일",
     "sources": "metadata.content_references·citations (관찰 기반)", "note": "약관상 자동 추출 금지 — 화면 수집 안 함"},
    {"name": "Claude.ai (웹)", "paths": ["file_import"], "status": "공식 내보내기 파일", "sources": "text 블록 citations (관찰 기반)",
     "note": "약관상 자동 접근 금지"},
    {"name": "Perplexity", "paths": ["file_import", "api_import"], "status": "Markdown 내보내기·API 응답 파일",
     "sources": "Sonar citations·search_results / Agent API search_results", "note": "Sonar Chat Completions 지원 종료(2026-09-27)"},
    {"name": "xAI Grok API", "paths": ["api_import"], "status": "API 응답 파일 (Responses 형식)", "sources": "url_citation·citations",
     "note": "실제 연결 없음"},
    {"name": "Microsoft Copilot (개인)", "paths": ["file_import"], "status": "보류 — 내보내기 CSV 열 구성 미확인", "sources": "미확인",
     "note": "대화 기록 JSON 으로 옮기면 가져올 수 있음"},
    {"name": "Microsoft 365 Copilot", "paths": [], "status": "보류 — 테넌트·관리자 동의 필요", "sources": "links·attributions",
     "note": "Graph interactionHistory"},
    {"name": "DeepSeek · Upstage Solar · NAVER CLOVA Studio · Mistral", "paths": ["api_import"],
     "status": "API 응답 파일 (Chat Completions 형식)", "sources": "출처 필드 없음(DeepSeek·CLOVA) / tool_reference(Mistral, 미지원)",
     "note": "실제 연결 없음"},
    {"name": "뤼튼 · Genspark · Kakao Kanana", "paths": [], "status": "보류 — 공식 API·내보내기 미확인", "sources": "미확인",
     "note": "직접 복사 입력·대화 기록 JSON 으로만 가능"},
    {"name": "FactChat API Gateway (Mindlogic)", "paths": ["live_api", "api_import"],
     "status": "업로드 직후 자동 수집 · API 응답 파일 (Chat Completions 형식)", "sources": "출처 필드 미확인",
     "note": "FactChat 은 여러 제공자 모델을 중계하는 플랫폼 — 실제 모델 ID 를 따로 기록. 실제 키 연결 검증 전"},
    {"name": "우수AI (oosoo.kr)", "paths": [], "status": "보류 — 공개 API·공식 대화 내보내기 미확인", "sources": "미확인",
     "note": "화면 자동화·우회 로그인 안 함. 직접 복사 입력·대화 기록 JSON 으로만 가능"},
]


def formats() -> Dict[str, Any]:
    return {"schema": "modelgrade-collection/v1", "formats": FORMATS, "platforms": PLATFORMS, "checked_at": "2026-10-13"}
