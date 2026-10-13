"""업로드 직후 자동 수집 — 수집 대상·관리 설정(허용 제공자·호출 수·비용 한도)·요청 계획·비용 정산.

자동 실행은 운영자가 연결(키·모델 ID)하고 관리 설정에서 허용한 제공자에만 한다. 비용은 이 저장소에 단가표를 두지 않고
운영자가 입력한 단가(USD / 100만 토큰)로 계산한다. 요청마다 시작 전 상한을 잡고, 작업 전체 상한이 작업당·일일 한도
안에 들 때만 그 제공자의 수집을 시작한다 (승인 범위를 넘는 제공자는 시작하지 않음).

요청 1회의 상한 = 입력 상한 × 입력 단가 + 최대 출력 토큰 × 출력 단가
  입력 상한 = 같은 대화의 앞 질문·이번 질문의 UTF-8 바이트 수(바이트 단위 BPE 토큰 수의 상한)
            + 앞 턴 답변마다 최대 출력 토큰 + 메시지마다 8토큰
응답에 사용량이 있으면 실제 입력·출력 토큰으로 정산하고, 없으면 상한을 그대로 쓴다.
"""
from __future__ import annotations

import copy
import json
import math
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

#: 수집 대상. kind=api 는 제공자 공식 API 직접 호출, gateway 는 여러 모델을 중계하는 API, unsupported 는 연동 안 함.
TARGETS = {
    "gpt": {"name": "GPT", "vendor": "openai", "vendor_label": "OpenAI", "platform": "OpenAI API", "kind": "api"},
    "claude": {"name": "Claude", "vendor": "anthropic", "vendor_label": "Anthropic", "platform": "Anthropic API", "kind": "api"},
    "gemini": {"name": "Gemini", "vendor": "google", "vendor_label": "Google", "platform": "Gemini API", "kind": "api"},
    "factchat": {"name": "FactChat", "vendor": None, "vendor_label": "모델별 제공자", "platform": "FactChat API Gateway",
                 "kind": "gateway",
                 "note": "OpenAI 호환 Chat Completions 게이트웨이. FactChat 은 모델이 아니라 여러 제공자 모델을 중계하는 플랫폼이며, "
                         "실제 모델은 연결할 때 고른 모델 ID 와 응답의 model 값으로 기록합니다."},
    "oosoo": {"name": "우수AI", "vendor": None, "vendor_label": "(주)모블디", "platform": "우수AI (oosoo.kr)", "kind": "unsupported",
              "note": "공개 API·공식 대화 내보내기를 확인하지 못했습니다 (2026-10-13 공식 사이트·이용약관 확인). 화면 자동화나 우회 "
                      "로그인은 하지 않습니다. 직접 받은 답변 파일은 '기타 응답 파일 가져오기'로 평가할 수 있습니다."},
}
LIVE_TARGETS = ("gpt", "claude", "gemini", "factchat")
#: FactChat 게이트웨이 주소. 공식 소개 페이지(factchat.bot)의 api.factchat.ai/v1 과 개발 문서(docs.mindlogic.ai)의 주소만 허용한다.
FACTCHAT_BASE_URLS = ("https://api.factchat.ai/v1", "https://factchat-cloud.mindlogic.ai/v1/gateway")
DEFAULT_MAX_OUTPUT_TOKENS = 8192
MESSAGE_OVERHEAD_TOKENS = 8
#: 응답을 계속 보내도 같은 결과가 나올 설정·권한 오류 — 자동 수집에서는 그 제공자의 남은 요청을 멈춘다 (자동 재시도 없음).
HALTING_STATUS = {401: "API 키", 403: "키 또는 모델 접근 권한", 404: "모델 ID", 429: "사용량·요청 한도"}

_LIMIT_FIELDS = {"job_calls": "작업당 최대 호출 수", "job_cost_usd": "작업당 비용 한도(USD)",
                 "daily_calls": "일일 최대 호출 수", "daily_cost_usd": "일일 비용 한도(USD)"}


def default_policy():
    return {"version": 1, "allowed": {p: False for p in LIVE_TARGETS}, "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            "prices": {p: {"input": None, "output": None} for p in LIVE_TARGETS},
            "limits": {key: None for key in _LIMIT_FIELDS}, "updated_at": None}


def _number(value, label, *, integer=False, minimum=0.0, maximum=1e6, allow_none=True):
    if value is None or value == "":
        if allow_none:
            return None
        raise ValueError(f"{label}을(를) 입력해 주세요.")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label}은(는) 숫자여야 합니다.")
    if integer and (not float(value).is_integer()):
        raise ValueError(f"{label}은(는) 정수여야 합니다.")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label}은(는) {minimum:g}~{maximum:g} 사이여야 합니다.")
    return int(value) if integer else float(value)


def validate_policy(payload, current=None):
    """관리 설정 갱신 — 보낸 항목만 바꾼다. 한도는 양수, 단가는 0 이상이어야 한다."""
    if not isinstance(payload, dict):
        raise ValueError("관리 설정은 JSON 객체여야 합니다.")
    policy = copy.deepcopy(current or default_policy())
    allowed = payload.get("allowed", {})
    if not isinstance(allowed, dict) or any(p not in LIVE_TARGETS or not isinstance(v, bool) for p, v in allowed.items()):
        raise ValueError("자동 수집 허용 설정은 제공자별 true/false 여야 합니다.")
    policy["allowed"].update(allowed)
    prices = payload.get("prices", {})
    if not isinstance(prices, dict) or any(p not in LIVE_TARGETS or not isinstance(v, dict) for p, v in prices.items()):
        raise ValueError("단가 설정 형식이 올바르지 않습니다.")
    for provider, price in prices.items():
        for side, label in (("input", "입력"), ("output", "출력")):
            if side in price:
                policy["prices"][provider][side] = _number(price[side], f"{TARGETS[provider]['name']} {label} 단가", maximum=10000)
    limits = payload.get("limits", {})
    if not isinstance(limits, dict) or any(k not in _LIMIT_FIELDS for k in limits):
        raise ValueError("한도 설정 형식이 올바르지 않습니다.")
    for key, value in limits.items():
        calls = key.endswith("_calls")
        policy["limits"][key] = _number(value, _LIMIT_FIELDS[key], integer=calls, minimum=1 if calls else 0.000001,
                                        maximum=1_000_000 if calls else 100_000)
    if "max_output_tokens" in payload:
        policy["max_output_tokens"] = _number(payload["max_output_tokens"], "최대 출력 토큰", integer=True,
                                              minimum=256, maximum=32768, allow_none=False)
    policy["updated_at"] = datetime.now(timezone.utc).isoformat()
    return policy


class PolicyStore:
    """관리 설정 파일 — 키 값은 넣지 않는다. 파일이 없으면 모든 제공자의 자동 수집이 꺼진 기본값."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            try:
                stored = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return default_policy()
            except (OSError, ValueError):
                raise ValueError("자동 수집 관리 설정 파일을 읽지 못했습니다. data/auto_collection_policy.json 을 확인해 주세요.") from None
        base = default_policy()
        try:
            policy = validate_policy({k: stored.get(k) for k in ("allowed", "prices", "limits", "max_output_tokens") if k in stored}, base)
        except ValueError as exc:
            raise ValueError("자동 수집 관리 설정 파일 값이 올바르지 않습니다: " + str(exc)) from None
        policy["updated_at"] = stored.get("updated_at")
        return policy

    def save(self, policy):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)


def limits_ready(policy):
    return all(policy["limits"].get(key) is not None for key in _LIMIT_FIELDS)


def priced(policy, provider):
    price = policy["prices"].get(provider) or {}
    return price.get("input") is not None and price.get("output") is not None


def target_state(provider, policy, keys):
    """제공자 하나의 자동 수집 준비 상태 — (state, label, reason)."""
    target = TARGETS[provider]
    if target["kind"] == "unsupported":
        return "unsupported", "연동 준비 중", target["note"]
    if provider not in keys:
        return "needs_connection", "연결 필요", "API 키와 모델 ID를 등록하면 자동 수집할 수 있습니다."
    if not policy["allowed"].get(provider):
        return "disabled", "자동 수집 꺼짐", "관리 설정에서 이 제공자의 자동 수집을 허용해야 합니다."
    if not limits_ready(policy):
        return "limits_missing", "한도 미설정", "관리 설정에서 작업당·일일 호출 수와 비용 한도를 모두 정해야 자동 수집합니다."
    if not priced(policy, provider):
        return "unpriced", "단가 미설정", "비용 한도를 확인하려면 이 제공자의 입력·출력 단가가 필요합니다."
    return "ready", "자동 수집", None


def gateway_vendor(model):
    """게이트웨이 모델 ID → 실제 제공자 (모르면 unknown). 'FactChat' 자체를 제공자·모델로 기록하지 않는다."""
    name = (model or "").lower()
    rules = (("gpt-", "openai"), ("chatgpt", "openai"), ("o1", "openai"), ("o3", "openai"), ("o4", "openai"),
             ("claude", "anthropic"), ("gemini", "google"), ("gemma", "google"), ("google/", "google"),
             ("grok", "xai"), ("sonar", "perplexity"), ("llama", "meta"), ("meta-llama/", "meta"),
             ("deepseek", "deepseek"), ("mistral", "mistral"), ("qwen", "alibaba"))
    for prefix, vendor in rules:
        if name.startswith(prefix) or name.split("/")[-1].startswith(prefix):
            return vendor
    return "unknown"


def validate_base_url(value):
    if value is None:
        return FACTCHAT_BASE_URLS[0]
    if not isinstance(value, str):
        raise ValueError("FactChat 게이트웨이 주소가 올바르지 않습니다.")
    url = value.strip().rstrip("/")
    parsed = urlparse(url)
    if url not in FACTCHAT_BASE_URLS or parsed.scheme != "https":
        raise ValueError("FactChat 게이트웨이 주소는 " + " 또는 ".join(FACTCHAT_BASE_URLS) + " 중 하나여야 합니다.")
    return url


def today():
    """일일 한도의 날짜 — 서버 시간대 기준."""
    return datetime.now().astimezone().date().isoformat()


def _bytes(text):
    return len(text.encode("utf-8"))


def request_bounds(cases, chains, indices, max_tokens):
    """요청할 행마다 입력 토큰 상한. 같은 대화의 앞 턴은 그대로 이력이 되므로 앞 질문 바이트와 앞 답변 상한을 더한다."""
    wanted, bounds = set(indices), {}
    for chain in chains:
        question_bytes = 0
        for position, index in enumerate(chain):
            question_bytes += _bytes(cases[index]["question"])
            if index in wanted:
                bounds[index] = question_bytes + position * max_tokens + MESSAGE_OVERHEAD_TOKENS * (2 * position + 1)
    return bounds


def cost_usd(price, input_tokens, output_tokens):
    return (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000


def plan_provider(cases, chains, indices, max_tokens, price):
    """한 제공자의 요청 계획 — 행별 예약 비용과 합계."""
    bounds = request_bounds(cases, chains, indices, max_tokens)
    reserves = {index: cost_usd(price, bounds[index], max_tokens) for index in indices}
    return {"calls": len(indices), "cost_usd": sum(reserves.values()), "reserves": reserves}


def usage_tokens(kind, raw):
    """응답 원본의 실제 사용량 (입력, 출력). 없으면 None — 정산은 예약 상한으로 한다."""
    if not isinstance(raw, dict):
        return None
    def count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    if kind == "gemini":
        usage = raw.get("usageMetadata") if isinstance(raw.get("usageMetadata"), dict) else {}
        prompt = count(usage.get("promptTokenCount"))
        output = count(usage.get("candidatesTokenCount"))
        if prompt is None or output is None:
            return None
        return prompt + (count(usage.get("toolUsePromptTokenCount")) or 0), output + (count(usage.get("thoughtsTokenCount")) or 0)
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    if kind == "factchat":
        prompt, output = count(usage.get("prompt_tokens")), count(usage.get("completion_tokens"))
    else:
        prompt, output = count(usage.get("input_tokens")), count(usage.get("output_tokens"))
        if kind == "claude" and prompt is not None:
            prompt += (count(usage.get("cache_creation_input_tokens")) or 0) + (count(usage.get("cache_read_input_tokens")) or 0)
    return None if prompt is None or output is None else (prompt, output)


def empty_budget(day):
    return {"day": day, "authorized_calls": 0, "authorized_cost_usd": 0.0, "spent_calls": 0, "spent_cost_usd": 0.0,
            "estimated_calls": 0, "providers": {}}


def committed(budget, active):
    """일일 한도에 묶인 양 — 진행 중이면 승인한 양 전체, 끝났으면 실제 사용량 (남은 예약은 돌려받음)."""
    if active:
        return max(budget["authorized_calls"], budget["spent_calls"]), max(budget["authorized_cost_usd"], budget["spent_cost_usd"])
    return budget["spent_calls"], budget["spent_cost_usd"]


def admit(order, plans, policy, budget, today_used):
    """한도 안에 드는 제공자만 정해진 순서로 승인한다. 일부만 들어가면 질문을 잘라 보내지 않고 그 제공자 전체를 시작하지 않는다."""
    limits = policy["limits"]
    job_calls, job_cost = budget["spent_calls"], budget["spent_cost_usd"]
    day_calls, day_cost = today_used
    admitted, skipped = [], {}
    for provider in order:
        plan = plans[provider]
        reason = None
        if job_calls + plan["calls"] > limits["job_calls"]:
            reason = f"작업당 호출 한도 {limits['job_calls']:,}회를 넘습니다 (필요 {plan['calls']:,}회, 이 작업 사용 {job_calls:,}회)."
        elif job_cost + plan["cost_usd"] > limits["job_cost_usd"] + 1e-12:
            reason = f"작업당 비용 한도 ${limits['job_cost_usd']:,.2f}를 넘을 수 있습니다 (예상 상한 ${plan['cost_usd']:,.4f})."
        elif day_calls + job_calls - budget["spent_calls"] + plan["calls"] > limits["daily_calls"]:
            reason = f"오늘 남은 호출 한도가 부족합니다 (일일 {limits['daily_calls']:,}회 중 {day_calls:,}회 사용·예약)."
        elif day_cost + job_cost - budget["spent_cost_usd"] + plan["cost_usd"] > limits["daily_cost_usd"] + 1e-12:
            reason = f"오늘 남은 비용 한도가 부족합니다 (일일 ${limits['daily_cost_usd']:,.2f} 중 ${day_cost:,.4f} 사용·예약)."
        if reason:
            skipped[provider] = reason
            continue
        admitted.append(provider)
        job_calls += plan["calls"]
        job_cost += plan["cost_usd"]
    return admitted, skipped


_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")


def valid_gateway_model(model):
    return isinstance(model, str) and bool(_MODEL_ID.fullmatch(model)) and "//" not in model and not model.endswith("/")
