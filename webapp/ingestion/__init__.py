"""외부 AI 답변 수집 통합 (4단계).

여러 경로(API·웹·에이전트·외부 플랫폼)의 답변을 공통 묶음 ``modelgrade-collection/v1`` 로 바꾸고 검증한다.
평가는 기존 CODE·J1~J6 평가기를 그대로 쓴다 — 이 패키지는 판정 기준을 바꾸지 않는다.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable, Dict, List, Optional

from . import agent_adapter, legacy_adapter, web_adapter
from .api_adapter import API_RESPONSES_SCHEMA, api_responses_file, detect_kind
from .schema import SCHEMA, BundleError, merge_bundles, quality, validate_bundle
from .sources import resolve_relays

__all__ = ["SCHEMA", "BundleError", "convert_file", "import_files", "validate_bundle", "merge_bundles"]
MAX_FILES = 20


def _json_value(text: str, name: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if name.lower().endswith(".jsonl") or text.lstrip().startswith("{"):
        rows = []
        for number, line in enumerate(text.splitlines(), 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    if name.lower().endswith((".json", ".jsonl")):
                        raise ValueError(f"{number}행 JSON 오류: {exc.msg}") from None
                    return None
        return rows or None
    if name.lower().endswith(".json"):
        raise ValueError("JSON 형식이 올바르지 않습니다.")
    return None


def convert_value(value: Any, options: Dict[str, Any], name: str, text: str) -> Dict[str, Any]:
    if isinstance(value, dict):
        schema = value.get("schema")
        if schema == SCHEMA:
            bundle = copy.deepcopy(value)
            bundle.setdefault("import", {}).update(adapter="collection", format="ModelGrade 공통 수집 형식", source_name=name)
            return bundle
        if schema == web_adapter.TRANSCRIPT_SCHEMA:
            return web_adapter.transcript(value, options, name)
        if schema == agent_adapter.AGENT_SCHEMA:
            return agent_adapter.generic(value, options, name)
        if schema == API_RESPONSES_SCHEMA:
            return api_responses_file(value, options, name)
        if isinstance(schema, str) and schema.startswith("modelgrade-"):
            raise ValueError(f"지원하지 않는 형식 버전입니다: {schema}")
        if legacy_adapter.looks_like_run(value):
            return legacy_adapter.comparison_run(value, options, name)
    if web_adapter.looks_like_chatgpt(value):
        return web_adapter.chatgpt_export(value, options, name)
    if web_adapter.looks_like_claude(value):
        return web_adapter.claude_export(value, options, name)
    if agent_adapter.looks_like_agents_sdk(value):
        return agent_adapter.agents_sdk(value, options, name)
    if agent_adapter.looks_like_langsmith(value):
        return agent_adapter.langsmith(value, options, name)
    if agent_adapter.looks_like_otel(value):
        return agent_adapter.otel(value, options, name)
    if legacy_adapter.looks_like_provider_export(value):
        return legacy_adapter.provider_export(value, options, name)
    if detect_kind(value) or (isinstance(value, list) and value and detect_kind(value[0])):
        raise ValueError("API 응답 JSON 만으로는 어떤 질문의 답변인지 알 수 없습니다. "
                         f"'{API_RESPONSES_SCHEMA}' 형식으로 질문과 함께 넣어 주세요.")
    return legacy_adapter.qa_text(text, options, name)


def convert_file(name: str, text: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """파일 하나를 검증 전 공통 묶음으로. 형식을 알아볼 수 없으면 추측하지 않고 오류를 낸다."""
    options = options or {}
    if not isinstance(text, str) or not text.strip():
        raise ValueError("파일 내용이 비어 있습니다.")
    text = text.lstrip("﻿")
    value = _json_value(text, name)
    if value is not None:
        return convert_value(value, options, name, text)
    if name.lower().endswith((".html", ".htm")) or re.match(r"\s*<(?:!doctype|html|head|body|div|main|article|section|p)\b",
                                                            text, re.I):
        return web_adapter.html_input(text, options, name)
    return legacy_adapter.qa_text(text, options, name)


def import_files(files: List[Dict[str, Any]], options: Optional[Dict[str, Any]] = None, *,
                 resolve: bool = False, fetcher: Optional[Callable[[str], Dict[str, Any]]] = None,
                 questions_only: bool = False) -> Dict[str, Any]:
    """여러 파일(과 직접 입력)을 각각 변환·검증한 뒤 하나로 합친다.

    ``resolve`` 가 참일 때만 중계 URL 의 이동 주소를 네트워크로 확인한다 (기본은 네트워크를 쓰지 않음).
    """
    options = options or {}
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise ValueError(f"가져올 파일은 1~{MAX_FILES}개여야 합니다.")
    bundles = []
    for number, item in enumerate(files, 1):
        if not isinstance(item, dict):
            raise ValueError(f"{number}번 입력이 객체가 아닙니다.")
        name = str(item.get("name") or f"입력 {number}")[:200]
        local = {**options, **(item.get("options") if isinstance(item.get("options"), dict) else {})}
        try:
            if isinstance(item.get("manual"), dict):
                raw = web_adapter.manual(item["manual"], local)
            else:
                raw = convert_file(name, item.get("text"), local)
            bundle = validate_bundle(raw, questions_only=questions_only)
        except BundleError as exc:
            raise BundleError([{**e, "path": f"{name} › {e['path']}" if e["path"] else name} for e in exc.errors]) from None
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"{name}: {exc}") from None
        bundles.append(bundle)
    merged = merge_bundles(bundles)
    if resolve:
        for response in merged["responses"]:
            response["collection_quality"].extend(resolve_relays(response["sources"], fetcher=fetcher))
        merged["import"]["relays_resolved"] = True
    return merged
