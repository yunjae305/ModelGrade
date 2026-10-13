"""ModelGrade 공통 수집 스키마 ``modelgrade-collection/v1`` 와 검증기.

API·웹·에이전트·외부 플랫폼에서 받은 답변을 같은 평가 입력으로 쓰기 위한 묶음(bundle) 형식이다.

- 질문(questions)·수집 대상 시스템(systems)·답변(responses)을 나눠 둔다. 같은 질문에 대한 여러 시스템의 답변은
  같은 ``case_id`` 를 가리키므로 서로 다른 질문으로 잘못 묶이지 않는다.
- 확인할 수 없는 값은 추정하지 않는다. 모르는 값은 ``null`` 이고, 제공자·플랫폼·모델처럼 반드시 밝혀야 하는
  값은 모르면 ``"unknown"`` 으로 명시해야 한다.
- 답변 원문은 바꾸지 않는다. 평가용 표현(``answer``)이 원본과 다르면 원본을 ``original`` 에 함께 둔다.
- 수집 대상 모델(systems)과 평가 Judge 는 다른 역할이다. 이 묶음에는 Judge 설정을 담지 않는다.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from code_eval.core import LIMITS

from .sources import normalize_sources

SCHEMA = "modelgrade-collection/v1"
ACCESS_MODES = {"api": "API", "web": "웹", "agent": "에이전트", "platform": "외부 플랫폼"}
RESPONSE_STATUSES = ("SUCCESS", "INCOMPLETE", "ERROR", "NOT_COLLECTED")
COLLECTION_METHODS = {
    "api_live": "API 실시간 수집",
    "api_response_import": "API 응답 파일 가져오기",
    "official_export": "공식 내보내기 파일",
    "user_file_json": "사용자 제공 JSON",
    "user_file_html": "사용자 제공 HTML",
    "manual_copy": "직접 복사 입력",
    "agent_trace": "에이전트 실행 기록",
    "legacy_import": "기존 ModelGrade 형식",
}
ANSWER_FORMATS = ("markdown", "text", "html_converted")
#: 기존 4개 모델 비교의 공급자 키. 외부 시스템 키로 쓰면 기존 결과 화면·이력과 섞이므로 막는다.
RESERVED_SYSTEM_KEYS = frozenset({"aiona", "gemini", "claude", "gpt"})
SYSTEM_KEY = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
CASE_ID_MAX = 200
QUALITY_SEVERITIES = ("info", "warning", "error")
MAX_ERRORS = 50


class BundleError(ValueError):
    """검증 오류 목록. ``errors`` 는 [{"path", "message"}]."""

    def __init__(self, errors: List[Dict[str, str]]):
        self.errors = errors[:MAX_ERRORS]
        shown = " / ".join(f"{e['path']}: {e['message']}" if e["path"] else e["message"] for e in self.errors[:5])
        more = f" 외 {len(errors) - 5}건" if len(errors) > 5 else ""
        super().__init__(f"수집 데이터 검증 오류 {len(errors)}건 — {shown}{more}")


def quality(code: str, message: str, severity: str = "warning") -> Dict[str, str]:
    """수집 품질 기록 한 건. 평가 판정이 아니라 수집·변환 과정에서 알게 된 사실이다."""
    return {"code": code, "severity": severity, "message": message}


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _finite(value: Any) -> bool:
    return not (isinstance(value, float) and not math.isfinite(value))


def _check_numbers(value: Any) -> bool:
    pending = [value]
    while pending:
        item = pending.pop()
        if not _finite(item):
            return False
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return True


def parse_time(value: Any) -> Optional[str]:
    """ISO 8601 시각(또는 유닉스 초)을 ISO 문자열로. 해석할 수 없으면 None — 시각을 지어내지 않는다."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value) and 0 < value < 1e11:
        from datetime import timezone
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError:
            return None
        return parsed.isoformat()
    return None


class _Errors:
    def __init__(self):
        self.items: List[Dict[str, str]] = []

    def add(self, path: str, message: str) -> None:
        self.items.append({"path": path, "message": message})


def _text(value: Any, path: str, errors: _Errors, *, required: bool = False, limit: int = 2000,
          name: str = "") -> Optional[str]:
    if value is None:
        if required:
            errors.add(path, f"{name or path} 값이 없습니다.")
        return None
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        errors.add(path, f"{name or path} 는 {limit:,}자 이하의 비어 있지 않은 문자열이어야 합니다.")
        return None
    return value


def _bool_or_none(value: Any, path: str, errors: _Errors) -> Optional[bool]:
    if value is None or isinstance(value, bool):
        return value
    errors.add(path, "true·false 또는 null(모름)이어야 합니다.")
    return None


def _names_or_none(value: Any, path: str, errors: _Errors) -> Optional[List[str]]:
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in value):
        errors.add(path, "도구 이름 문자열 배열 또는 null(모름)이어야 합니다.")
        return None
    return list(dict.fromkeys(v.strip() for v in value))


def _conversation(item: Dict[str, Any], path: str, errors: _Errors) -> Tuple[Optional[str], Optional[int]]:
    cid, turn = item.get("conversation_id"), item.get("turn")
    if cid is None and turn is None:
        return None, None
    if cid is None or turn is None:
        errors.add(path, "conversation_id 와 turn 은 함께 있어야 합니다.")
        return None, None
    if isinstance(cid, bool) or not isinstance(cid, (str, int)) or not str(cid).strip() or len(str(cid)) > 200:
        errors.add(path + ".conversation_id", "비어 있지 않은 200자 이하 문자열이어야 합니다.")
        return None, None
    if isinstance(turn, bool) or not isinstance(turn, int) or not 1 <= turn <= LIMITS["batch_cases"]:
        errors.add(path + ".turn", f"1 이상 {LIMITS['batch_cases']:,} 이하의 정수여야 합니다.")
        return None, None
    return str(cid).strip(), turn


def _question(item: Any, index: int, errors: _Errors) -> Optional[Dict[str, Any]]:
    path = f"questions[{index}]"
    if not isinstance(item, dict):
        errors.add(path, "질문은 객체여야 합니다.")
        return None
    case_id = item.get("case_id")
    if isinstance(case_id, bool) or not isinstance(case_id, (str, int)) or not str(case_id).strip() or len(str(case_id)) > CASE_ID_MAX:
        errors.add(path + ".case_id", f"질문 고유 ID(case_id)는 {CASE_ID_MAX}자 이하의 비어 있지 않은 값이어야 합니다.")
        return None
    question = item.get("question")
    if not isinstance(question, str) or not question.strip():
        errors.add(path + ".question", "질문 원문이 비어 있거나 문자열이 아닙니다.")
        return None
    if len(question) > LIMITS["question_chars"]:
        errors.add(path + ".question", f"질문이 입력 한도({LIMITS['question_chars']:,}자)를 넘습니다.")
        return None
    cid, turn = _conversation(item, path, errors)
    reference = item.get("reference")
    if reference is not None and not isinstance(reference, (str, dict, list)):
        errors.add(path + ".reference", "참조 자료는 문자열 또는 JSON 객체·배열이어야 합니다.")
        reference = None
    out = {"case_id": str(case_id).strip(), "question": question,
           "question_type": _text(item.get("question_type"), path + ".question_type", errors, limit=200),
           "scenario": _text(item.get("scenario"), path + ".scenario", errors, limit=2000),
           "dataset_version": _text(item.get("dataset_version"), path + ".dataset_version", errors, limit=200),
           "conversation_id": cid, "turn": turn, "reference": reference if reference not in ("", {}, []) else None}
    out["dialogue"] = "multi" if cid is not None else "single"
    if isinstance(item.get("aliases"), list):
        out["aliases"] = [str(a) for a in item["aliases"] if isinstance(a, (str, int))]
    return out


def _system(item: Any, index: int, errors: _Errors) -> Optional[Dict[str, Any]]:
    path = f"systems[{index}]"
    if not isinstance(item, dict):
        errors.add(path, "시스템 정보는 객체여야 합니다.")
        return None
    before = len(errors.items)
    key = item.get("system_key")
    if not isinstance(key, str) or not SYSTEM_KEY.fullmatch(key):
        errors.add(path + ".system_key", "어느 AI 시스템의 답변인지 구분하는 system_key(영문 소문자·숫자·._-, 64자 이하)가 필요합니다.")
    elif key in RESERVED_SYSTEM_KEYS:
        errors.add(path + ".system_key", f"'{key}' 는 기존 4개 모델 비교의 예약 키입니다. 예: '{key}-web', '{key}-api'.")
    out: Dict[str, Any] = {"system_key": key}
    for field, label in (("provider", "제공자(provider)"), ("platform", "플랫폼(platform)"), ("model", "모델(model)")):
        value = item.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            errors.add(path + "." + field, f"{label} 정보가 없습니다. 확인할 수 없으면 'unknown' 으로 명시하세요.")
        else:
            out[field] = value.strip()
    mode = item.get("access_mode")
    if mode not in ACCESS_MODES:
        errors.add(path + ".access_mode", "실행 방식(access_mode)은 api·web·agent·platform 중 하나여야 합니다.")
    if len(errors.items) > before:
        return None
    out["access_mode"] = mode
    out["model_version"] = _text(item.get("model_version"), path + ".model_version", errors, limit=200)
    label = _text(item.get("label"), path + ".label", errors, limit=200)
    out["label"] = label.strip() if label and label.strip() else default_label(out)
    settings = item.get("settings", {})
    if not isinstance(settings, dict) or not _check_numbers(settings):
        errors.add(path + ".settings", "확인된 모델 설정만 담은 객체여야 합니다.")
        settings = {}
    out["settings"] = copy.deepcopy(settings)
    env = item.get("environment", {})
    if not isinstance(env, dict):
        errors.add(path + ".environment", "실행 환경은 객체여야 합니다.")
        env = {}
    instruction = env.get("system_instruction", {"known": False})
    if not isinstance(instruction, dict) or not isinstance(instruction.get("known", False), bool):
        errors.add(path + ".environment.system_instruction", "{known: true|false, text?, note?} 형식이어야 합니다.")
        instruction = {"known": False}
    instruction = {"known": bool(instruction.get("known")),
                   **({k: instruction[k] for k in ("text", "note") if isinstance(instruction.get(k), str)})}
    notes = env.get("notes", [])
    if not isinstance(notes, list) or any(not isinstance(n, str) for n in notes):
        errors.add(path + ".environment.notes", "문자열 배열이어야 합니다.")
        notes = []
    out["environment"] = {
        "web_search_available": _bool_or_none(env.get("web_search_available"), path + ".environment.web_search_available", errors),
        "tools_available": _names_or_none(env.get("tools_available"), path + ".environment.tools_available", errors),
        "system_instruction": instruction,
        "notes": notes,
    }
    for extra in ("locale", "region", "account_tier"):
        if isinstance(env.get(extra), str):
            out["environment"][extra] = env[extra]
    return out


def default_label(system: Dict[str, Any]) -> str:
    platform, model = system.get("platform"), system.get("model")
    mode = ACCESS_MODES.get(system.get("access_mode"), "")
    parts = [p for p in (platform if platform != "unknown" else None, model if model != "unknown" else None) if p]
    return (" · ".join(parts) or system.get("system_key", "unknown")) + (f" ({mode})" if mode else "")


def _usage(value: Any, path: str, errors: _Errors) -> Dict[str, Any]:
    out = {"input_tokens": None, "output_tokens": None, "total_tokens": None, "reported_cost": None, "latency_ms": None}
    if value is None:
        return out
    if not isinstance(value, dict):
        errors.add(path, "사용량은 객체여야 합니다.")
        return out
    for key in ("input_tokens", "output_tokens", "total_tokens", "latency_ms"):
        v = value.get(key)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not _finite(v) or v < 0:
            errors.add(f"{path}.{key}", "0 이상의 수 또는 null 이어야 합니다.")
        else:
            out[key] = v
    cost = value.get("reported_cost")
    if cost is not None:
        if (not isinstance(cost, dict) or isinstance(cost.get("amount"), bool)
                or not isinstance(cost.get("amount"), (int, float)) or not _finite(cost["amount"]) or cost["amount"] < 0
                or not isinstance(cost.get("currency"), str)):
            errors.add(path + ".reported_cost", "{amount, currency} — 제공자가 응답에 밝힌 비용만 기록합니다.")
        else:
            out["reported_cost"] = {"amount": cost["amount"], "currency": cost["currency"],
                                    "source": cost.get("source") if isinstance(cost.get("source"), str) else "provider_response"}
    for extra in ("search_requests", "reasoning_tokens", "cached_input_tokens"):
        v = value.get(extra)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            out[extra] = v
    return out


def _quality_list(value: Any, path: str, errors: _Errors) -> List[Dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.add(path, "수집 품질 기록은 배열이어야 합니다.")
        return []
    out = []
    for i, item in enumerate(value):
        if (not isinstance(item, dict) or not isinstance(item.get("code"), str) or not isinstance(item.get("message"), str)
                or item.get("severity", "warning") not in QUALITY_SEVERITIES):
            errors.add(f"{path}[{i}]", "{code, message, severity(info|warning|error)} 형식이어야 합니다.")
            continue
        out.append(quality(item["code"], item["message"], item.get("severity", "warning")))
    return out


def _response(item: Any, index: int, errors: _Errors, questions: Dict[str, Dict[str, Any]],
              systems: Dict[str, Dict[str, Any]], invalid_systems: bool = False) -> Optional[Dict[str, Any]]:
    path = f"responses[{index}]"
    if not isinstance(item, dict):
        errors.add(path, "답변은 객체여야 합니다.")
        return None
    if not _check_numbers(item):
        errors.add(path, "JSON 숫자는 유한한 값이어야 합니다 (NaN·Infinity 불가).")
        return None
    case_id = item.get("case_id")
    case_id = str(case_id).strip() if isinstance(case_id, (str, int)) and not isinstance(case_id, bool) else None
    if case_id not in questions:
        errors.add(path + ".case_id", f"질문 목록에 없는 case_id 입니다: {item.get('case_id')!r}")
        return None
    key = item.get("system_key")
    if key not in systems:
        if systems or not invalid_systems:
            errors.add(path + ".system_key", f"시스템 목록에 없는 system_key 입니다: {key!r}")
        return None
    status = item.get("status", "SUCCESS")
    if status not in RESPONSE_STATUSES:
        errors.add(path + ".status", "status 는 SUCCESS·INCOMPLETE·ERROR·NOT_COLLECTED 중 하나여야 합니다.")
        return None
    answer = item.get("answer", "" if status != "SUCCESS" else None)
    if not isinstance(answer, str):
        errors.add(path + ".answer", "답변 원문(answer)은 문자열이어야 합니다.")
        return None
    if len(answer) > LIMITS["answer_chars"]:
        errors.add(path + ".answer", f"답변이 입력 한도({LIMITS['answer_chars']:,}자)를 넘습니다.")
        return None
    if status == "SUCCESS" and not answer.strip() and item.get("empty_answer_confirmed") is not True:
        errors.add(path + ".answer", "빈 답변입니다. 수집 실패라면 status 를 ERROR·INCOMPLETE 로, "
                                     "모델이 실제로 빈 응답을 했다면 empty_answer_confirmed: true 로 표시하세요.")
        return None
    method = item.get("collection_method")
    if method not in COLLECTION_METHODS:
        errors.add(path + ".collection_method", "수집 방식(collection_method)이 없거나 알 수 없는 값입니다: "
                                                + ", ".join(COLLECTION_METHODS))
        return None
    fmt = item.get("answer_format", "markdown")
    if fmt not in ANSWER_FORMATS:
        errors.add(path + ".answer_format", "answer_format 은 markdown·text·html_converted 중 하나여야 합니다.")
        return None
    original = item.get("original")
    if original is not None and (not isinstance(original, dict) or not isinstance(original.get("content"), str)
                                 or not isinstance(original.get("format"), str)):
        errors.add(path + ".original", "{format, content} 형식이어야 합니다.")
        original = None
    if fmt == "html_converted" and original is None:
        errors.add(path + ".original", "HTML 에서 변환한 답변은 원본 HTML(original)을 함께 보관해야 합니다.")
    collected_raw = item.get("collected_at")
    collected = parse_time(collected_raw)
    if collected_raw is not None and collected is None:
        errors.add(path + ".collected_at", "ISO 8601 시각 또는 null(모름)이어야 합니다.")
    env = item.get("environment", {})
    if not isinstance(env, dict):
        errors.add(path + ".environment", "객체여야 합니다.")
        env = {}
    error_text = item.get("error")
    if error_text is not None and not isinstance(error_text, str):
        errors.add(path + ".error", "오류 설명은 문자열이어야 합니다.")
        error_text = None
    if status != "SUCCESS" and not error_text:
        error_text = {"INCOMPLETE": "응답이 완료되지 않았습니다.", "ERROR": "응답 수집에 실패했습니다.",
                      "NOT_COLLECTED": "이 질문의 답변이 수집되지 않았습니다."}[status]
    sources, source_quality = normalize_sources(item.get("sources"), answer, f"{path}.sources")
    out = {
        "case_id": case_id, "system_key": key, "answer": answer, "answer_format": fmt,
        "original": copy.deepcopy(original), "status": status, "error": error_text,
        "collected_at": collected, "collection_method": method,
        "environment": {"web_search_used": _bool_or_none(env.get("web_search_used"), path + ".environment.web_search_used", errors),
                        "tools_used": _names_or_none(env.get("tools_used"), path + ".environment.tools_used", errors)},
        "sources": sources,
        "usage": _usage(item.get("usage"), path + ".usage", errors),
        "collection_quality": _quality_list(item.get("collection_quality"), path + ".collection_quality", errors) + source_quality,
    }
    if item.get("empty_answer_confirmed") is True:
        out["empty_answer_confirmed"] = True
    for optional in ("platform_ref", "agent", "raw", "normalization", "finish_reason", "retrieved_sources"):
        if item.get(optional) is not None:
            out[optional] = copy.deepcopy(item[optional])
    return out


def _question_keys(questions: List[Dict[str, Any]]) -> None:
    """같은 질문을 알아보는 열쇠. 대화 턴은 같은 대화의 앞 사용자 질문(빠진 턴은 빈 자리로)까지 포함한다.

    플랫폼마다 대화 ID 가 달라도 사용자 질문 순서가 같으면 같은 질문이다. 대화 ID 자체는 열쇠에 넣지 않는다.
    """
    by_conversation: Dict[str, Dict[int, str]] = {}
    for q in questions:
        if q["conversation_id"] is not None:
            by_conversation.setdefault(q["conversation_id"], {})[q["turn"]] = q["question"]
    for q in questions:
        if q["conversation_id"] is None:
            chain: List[Any] = [q["question"]]
        else:
            turns = by_conversation[q["conversation_id"]]
            chain = [{"turn": t, "question": turns.get(t)} for t in range(1, q["turn"] + 1)]
        q["question_key"] = canonical_hash({"chain": chain, "reference": q["reference"]})


def conversation_gaps(questions: List[Dict[str, Any]]) -> Dict[str, List[int]]:
    """대화별 빠진 턴 번호. 빠진 턴을 지어내지 않고 평가기가 '앞 대화 누락'으로 다루도록 그대로 둔다."""
    turns: Dict[str, List[int]] = {}
    for q in questions:
        if q["conversation_id"] is not None:
            turns.setdefault(q["conversation_id"], []).append(q["turn"])
    gaps = {}
    for cid, values in turns.items():
        missing = sorted(set(range(1, max(values) + 1)) - set(values))
        if missing:
            gaps[cid] = missing
    return gaps


def response_key(response: Dict[str, Any], question_key: str) -> str:
    return canonical_hash({"question": question_key, "system": response["system_key"],
                           "status": response["status"], "answer": response["answer"]})


def validate_bundle(value: Any, *, questions_only: bool = False) -> Dict[str, Any]:
    """묶음을 검증하고 기본값을 채운 새 묶음을 돌려준다. 오류가 있으면 BundleError 로 모두 알린다.

    ``questions_only`` 는 API 로 새로 수집할 질문 목록만 가져올 때다 (시스템·답변이 없어도 됨).
    """
    errors = _Errors()
    if not isinstance(value, dict):
        raise BundleError([{"path": "", "message": "수집 데이터는 JSON 객체여야 합니다."}])
    if value.get("schema") != SCHEMA:
        raise BundleError([{"path": "schema", "message": f"schema 는 '{SCHEMA}' 여야 합니다."}])
    for field in ("questions", "systems", "responses"):
        if not isinstance(value.get(field), list):
            errors.add(field, "배열이어야 합니다.")
    if errors.items:
        raise BundleError(errors.items)
    if not value["questions"] or len(value["questions"]) > LIMITS["batch_cases"]:
        errors.add("questions", f"질문은 1~{LIMITS['batch_cases']:,}개여야 합니다.")
    if not value["systems"] and not questions_only:
        errors.add("systems", "수집 대상 시스템이 1개 이상 있어야 합니다.")
    questions: Dict[str, Dict[str, Any]] = {}
    ordered: List[Dict[str, Any]] = []
    seen_turns: Dict[Tuple[str, int], str] = {}
    for index, item in enumerate(value["questions"]):
        q = _question(item, index, errors)
        if q is None:
            continue
        if q["case_id"] in questions:
            errors.add(f"questions[{index}].case_id", f"case_id {q['case_id']} 가 중복되었습니다.")
            continue
        if q["conversation_id"] is not None:
            slot = (q["conversation_id"], q["turn"])
            if slot in seen_turns:
                errors.add(f"questions[{index}].turn", f"대화 {q['conversation_id']} 의 {q['turn']}턴이 중복되었습니다 "
                                                      f"({seen_turns[slot]}). 어느 쪽이 맞는지 추측하지 않습니다.")
                continue
            seen_turns[slot] = q["case_id"]
        questions[q["case_id"]] = q
        ordered.append(q)
    systems: Dict[str, Dict[str, Any]] = {}
    for index, item in enumerate(value["systems"]):
        s = _system(item, index, errors)
        if s is None:
            continue
        if s["system_key"] in systems:
            errors.add(f"systems[{index}].system_key", f"system_key {s['system_key']} 가 중복되었습니다.")
            continue
        systems[s["system_key"]] = s
    responses: List[Dict[str, Any]] = []
    slots: Dict[Tuple[str, str], int] = {}
    duplicates = 0
    for index, item in enumerate(value["responses"]):
        r = _response(item, index, errors, questions, systems, invalid_systems=len(systems) < len(value["systems"]))
        if r is None:
            continue
        slot = (r["case_id"], r["system_key"])
        if slot in slots:
            earlier = responses[slots[slot]]
            if earlier["answer"] == r["answer"] and earlier["status"] == r["status"]:
                duplicates += 1   # 같은 답변이 두 번 들어온 중복 Import — 하나만 남긴다
                continue
            errors.add(f"responses[{index}]", f"{r['system_key']} 의 {r['case_id']} 답변이 서로 다르게 두 번 있습니다. "
                                              "의도적인 재실행은 별도 Import(재실행)로 평가하세요.")
            continue
        slots[slot] = len(responses)
        responses.append(r)
    if errors.items:
        raise BundleError(errors.items)
    if not responses and not questions_only:
        raise BundleError([{"path": "responses", "message": "평가할 답변이 없습니다."}])
    _question_keys(ordered)
    for r in responses:
        r["response_key"] = response_key(r, questions[r["case_id"]]["question_key"])
    dataset = value.get("dataset") if isinstance(value.get("dataset"), dict) else {}
    info = value.get("import") if isinstance(value.get("import"), dict) else {}
    bundle = {
        "schema": SCHEMA,
        "dataset": {k: dataset.get(k) if isinstance(dataset.get(k), str) else None for k in ("id", "name", "version")},
        "questions": ordered,
        "systems": list(systems.values()),
        "responses": responses,
        "import": copy.deepcopy(info),
        "report": {"duplicate_responses_dropped": duplicates, "conversation_gaps": conversation_gaps(ordered)},
    }
    bundle["import_fingerprint"] = import_fingerprint(bundle)
    return bundle


def import_fingerprint(bundle: Dict[str, Any]) -> str:
    """같은 내용을 다시 가져왔는지 판단하는 지문 — 시스템 정의와 질문·답변 내용만 본다 (파일 이름·가져온 시각 제외)."""
    systems = [{k: s[k] for k in ("system_key", "provider", "platform", "model", "model_version", "access_mode")}
               for s in sorted(bundle["systems"], key=lambda s: s["system_key"])]
    return canonical_hash({"systems": systems, "responses": sorted(r["response_key"] for r in bundle["responses"])})


def merge_bundles(bundles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """검증된 여러 묶음을 하나로 합친다. 같은 질문(question_key)은 하나로, 같은 case_id 에 다른 질문은 오류다."""
    if len(bundles) == 1:
        return bundles[0]
    errors = _Errors()
    questions: Dict[str, Dict[str, Any]] = {}
    by_key: Dict[str, str] = {}
    ordered: List[Dict[str, Any]] = []
    systems: Dict[str, Dict[str, Any]] = {}
    responses: List[Dict[str, Any]] = []
    slots: Dict[Tuple[str, str], Dict[str, Any]] = {}
    duplicates = sum(b["report"]["duplicate_responses_dropped"] for b in bundles)
    notes: List[str] = []
    for number, bundle in enumerate(bundles, 1):
        remap: Dict[str, str] = {}
        groups: Dict[Optional[str], List[Dict[str, Any]]] = {}
        for q in bundle["questions"]:
            groups.setdefault(q["conversation_id"], []).append(q)
        # 대화는 통째로만 합친다. 모든 턴이 이미 있는 한 대화와 같을 때만 그 대화로 보고, 하나라도 다르면 새 대화다.
        plans: Dict[str, Optional[str]] = {}
        for cid, members in groups.items():
            if cid is None:
                continue
            targets = {questions[by_key[q["question_key"]]]["conversation_id"] if q["question_key"] in by_key else None
                       for q in members}
            plans[cid] = targets.pop() if len(targets) == 1 and None not in targets else None
        used = {q["conversation_id"] for q in ordered if q["conversation_id"] is not None}
        fresh: Dict[str, str] = {}
        for q in bundle["questions"]:
            cid = q["conversation_id"]
            existing = by_key.get(q["question_key"]) if cid is None or plans.get(cid) is not None else None
            if existing is not None:
                remap[q["case_id"]] = existing
                if q["case_id"] != existing:
                    questions[existing].setdefault("aliases", [])
                    if q["case_id"] not in questions[existing]["aliases"]:
                        questions[existing]["aliases"].append(q["case_id"])
                continue
            if q["case_id"] in questions:
                errors.add(f"파일 {number}", f"case_id {q['case_id']} 가 다른 파일의 다른 질문과 겹칩니다. "
                                             "같은 질문이면 원문·대화 순서를 맞추고, 다른 질문이면 case_id 를 바꾸세요.")
                continue
            row = copy.deepcopy(q)
            if cid is not None:
                if cid not in fresh:
                    target = cid if cid not in used else f"{cid}~{number}"
                    fresh[cid] = target
                    used.add(target)
                    if target != cid:
                        notes.append(f"파일 {number}의 대화 {cid} 는 다른 파일의 같은 ID 대화와 질문이 달라 {target} 로 구분했습니다.")
                row["conversation_id"] = fresh[cid]
            questions[row["case_id"]] = row
            by_key.setdefault(row["question_key"], row["case_id"])
            remap[q["case_id"]] = row["case_id"]
            ordered.append(row)
        for s in bundle["systems"]:
            known = systems.get(s["system_key"])
            if known is not None and known != s:
                errors.add(f"파일 {number}", f"system_key {s['system_key']} 의 정보가 다른 파일과 다릅니다. "
                                             "다른 시스템이면 키를 바꾸세요.")
                continue
            systems[s["system_key"]] = s
        for r in bundle["responses"]:
            row = copy.deepcopy(r)
            row["case_id"] = remap.get(r["case_id"], r["case_id"])
            slot = (row["case_id"], row["system_key"])
            if slot in slots:
                if slots[slot]["response_key"] == row["response_key"]:
                    duplicates += 1
                    continue
                errors.add(f"파일 {number}", f"{row['system_key']} 의 {row['case_id']} 답변이 다른 파일과 다릅니다. "
                                             "의도적인 재실행은 별도 Import(재실행)로 평가하세요.")
                continue
            slots[slot] = row
            responses.append(row)
        info = bundle.get("import") or {}
        notes.extend(info.get("notes") or [])
    if errors.items:
        raise BundleError(errors.items)
    merged = {"schema": SCHEMA, "dataset": bundles[0]["dataset"], "questions": ordered, "systems": list(systems.values()),
              "responses": responses,
              "import": {"format": "여러 파일", "files": [b.get("import") or {} for b in bundles], "notes": notes},
              "report": {"duplicate_responses_dropped": duplicates, "conversation_gaps": conversation_gaps(ordered)}}
    merged["import_fingerprint"] = import_fingerprint(merged)
    return merged
