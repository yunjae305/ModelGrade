"""기존 ModelGrade 형식 → 공통 묶음. 기존 파일·결과는 읽기만 하고 바꾸지 않는다.

- Q/A 업로드 형식(JSON·JSONL·MD·TXT): 기존 업로드 변환기(dataset_import)를 그대로 쓴다. 이 형식에는 어느 모델의
  답변인지가 없으므로 제공자·플랫폼·모델 정보를 함께 받아야 한다 (없으면 검증 오류).
- 저장된 비교 실행 JSON·모델별 원문 JSON: 수집 결과와 원본 응답을 그대로 옮긴다. 질문별 실제 수집 시각은
  기존 기록에 없으므로 비워 둔다 (실행 시각을 수집 시각처럼 쓰지 않는다).
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

from webapp.dataset_import import import_text

from .api_adapter import LIVE_PROVIDERS, parse_api_response, response_fields
from .builder import BundleBuilder, system_from
from .schema import SCHEMA, quality

_QUESTION_FIELDS = ("case_id", "question", "reference", "conversation_id", "turn")


def qa_text(text: str, options: Dict[str, Any], name: str) -> Dict[str, Any]:
    rows, info = import_text(text, name)
    system = system_from(options, {})
    questions, responses = [], []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if row.get("__parse_error__"):
            raise ValueError(row["__parse_error__"])
        case_id = str(row.get("case_id") or row.get("id") or row.get("question_id") or f"Q{index + 1:03d}")
        question = {"case_id": case_id, "question": row.get("question")}
        for key in ("reference", "conversation_id", "turn"):
            if row.get(key) is not None:
                question[key] = row[key]
        questions.append(question)
        answer = row.get("answer")
        response = {"case_id": case_id, "system_key": system.get("system_key"), "collection_method": "legacy_import",
                    "answer": answer if isinstance(answer, str) else "", "status": "SUCCESS"}
        if not isinstance(answer, str) or not answer.strip():
            response.update(status="NOT_COLLECTED", error="파일에 이 질문의 답변이 없습니다.")
        for key in ("sources", "collected_at"):
            if key in row:
                response[key] = row[key]
        responses.append(response)
    notes = list(info.get("notes") or [])
    return {"schema": SCHEMA, "questions": questions, "systems": [system], "responses": responses,
            "import": {"adapter": "legacy_qa", "format": f"기존 Q/A {info.get('format')}", "source_name": name,
                       "notes": notes, "converted": True}}


def looks_like_run(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("datasets"), dict) and isinstance(value.get("providers"), list)


def looks_like_provider_export(value: Any) -> bool:
    return (isinstance(value, list) and bool(value) and all(isinstance(r, dict) and "system_id" in r and "collection" in r
                                                            for r in value[:5]))


def _system_for(provider: str, run: Dict[str, Any]) -> Dict[str, Any]:
    if provider == "aiona":
        release = run.get("aiona_release") or {}
        return {"system_key": "aiona-service", "provider": "AIONA", "platform": "AIONA", "label": "AIONA (업로드 원문)",
                "model": "unknown", "model_version": release.get("version") if isinstance(release.get("version"), str) else None,
                "access_mode": "platform",
                "environment": {"web_search_available": None, "tools_available": None, "system_instruction": {"known": False},
                                "notes": ["AIONA 서비스에서 받은 답변을 업로드한 원문입니다."]}}
    kind, vendor, platform = LIVE_PROVIDERS[provider]
    return {"system_key": f"{provider}-api", "provider": vendor, "platform": platform,
            "model": str((run.get("models") or {}).get(provider) or "unknown"), "access_mode": "api",
            "settings": {"max_output_tokens": run["max_tokens"]} if isinstance(run.get("max_tokens"), int) else {},
            "environment": {"web_search_available": False, "tools_available": [],
                            "system_instruction": {"known": True, "note": "시스템 프롬프트를 보내지 않는 기존 수집 경로"},
                            "notes": ["기존 비교의 API 수집 경로는 도구·웹검색 없이 질문(과 앞 대화)만 보냅니다."]}}


def _collected_response(row: Dict[str, Any], provider: str, system_key: str) -> Dict[str, Any]:
    outcome = row.get("collection") if isinstance(row.get("collection"), dict) else {}
    status = outcome.get("status") if outcome.get("status") in ("SUCCESS", "INCOMPLETE", "ERROR", "NOT_COLLECTED") else "ERROR"
    response = {"case_id": row.get("case_id"), "system_key": system_key, "collection_method": "legacy_import",
                "answer": row.get("answer") if isinstance(row.get("answer"), str) else "", "status": status,
                "error": outcome.get("error"), "collected_at": outcome.get("collected_at")}
    if isinstance(outcome.get("raw"), dict) and provider in LIVE_PROVIDERS:
        try:
            fields, notes = response_fields(parse_api_response(outcome["raw"], LIVE_PROVIDERS[provider][0]), answer=response["answer"])
            response.update(sources=fields["sources"], environment={**fields["environment"], "web_search_used": False},
                            usage={**fields["usage"], **({"latency_ms": outcome["latency_ms"]} if isinstance(outcome.get("latency_ms"), (int, float)) else {})},
                            finish_reason=fields["finish_reason"], collection_quality=notes, raw=copy.deepcopy(outcome["raw"]))
        except ValueError:
            response["collection_quality"] = [quality("raw_unreadable", "보존된 원본 응답을 해석하지 못했습니다.", "info")]
    return response


def comparison_run(value: Dict[str, Any], options: Dict[str, Any], name: str) -> Dict[str, Any]:
    base = value["datasets"].get("aiona")
    if not isinstance(base, list) or not base:
        raise ValueError("저장된 비교 실행에 기준 질문 목록(datasets.aiona)이 없습니다.")
    questions = [{k: copy.deepcopy(row[k]) for k in _QUESTION_FIELDS if row.get(k) is not None} for row in base]
    systems, responses = [], []
    for provider in value["providers"]:
        if provider != "aiona" and provider not in LIVE_PROVIDERS:
            continue
        system = _system_for(provider, value)
        systems.append(system)
        for index, row in enumerate(value["datasets"].get(provider) or []):
            if row is None:
                responses.append({"case_id": base[index]["case_id"], "system_key": system["system_key"],
                                  "collection_method": "legacy_import", "answer": "", "status": "NOT_COLLECTED"})
            elif provider == "aiona":
                responses.append({"case_id": row["case_id"], "system_key": system["system_key"], "collection_method": "legacy_import",
                                  "answer": row.get("answer", ""), "status": "SUCCESS" if (row.get("answer") or "").strip() else "NOT_COLLECTED"})
            else:
                responses.append(_collected_response(row, provider, system["system_key"]))
    return {"schema": SCHEMA, "questions": questions, "systems": systems, "responses": responses,
            "import": {"adapter": "legacy_run", "format": "저장된 비교 실행 JSON", "source_name": name,
                       "notes": [f"원래 실행 {value.get('run_id')} 의 원문을 그대로 옮겼습니다. 원래 실행 기록은 바꾸지 않습니다.",
                                 "기존 기록에는 질문별 수집 시각이 없어 수집 시각을 비워 두었습니다."],
                       "source_run_id": value.get("run_id"), "converted": True}}


def provider_export(rows: List[Dict[str, Any]], options: Dict[str, Any], name: str) -> Dict[str, Any]:
    provider = rows[0].get("system_id")
    if any(r.get("system_id") != provider for r in rows):
        raise ValueError("모델별 원문 JSON 에 여러 모델이 섞여 있습니다.")
    if provider not in LIVE_PROVIDERS:
        raise ValueError(f"알 수 없는 모델 원문입니다: {provider}")
    system = system_from(options, _system_for(provider, {"models": {provider: rows[0].get("source")}}))
    questions = [{k: copy.deepcopy(row[k]) for k in _QUESTION_FIELDS if row.get(k) is not None} for row in rows]
    responses = [_collected_response(row, provider, system["system_key"]) for row in rows]
    return {"schema": SCHEMA, "questions": questions, "systems": [system], "responses": responses,
            "import": {"adapter": "legacy_provider_export", "format": "모델별 원문 JSON", "source_name": name,
                       "notes": ["기존 기록에는 질문별 수집 시각이 없어 수집 시각을 비워 두었습니다."], "converted": True}}
