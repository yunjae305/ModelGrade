"""답변 출처 정규화 — 플랫폼 중계 URL 과 원래 URL 을 구분하고, 출처가 붙은 답변 구절(주장)을 찾는다.

- 표시·제공된 URL(display_url)은 그대로 보존한다. 평가용 URL(url)은 원래 URL 을 알 때 그것을 쓴다.
- 중계 URL 의 원래 주소를 모르면 지어내지 않는다. 그 출처는 J5 원문 대조에서 빼고(skip_reason) 기록만 한다.
  중계 URL 을 그대로 대조하면 다른 호스트로 이동해 '원문 확인 실패'가 되므로, 플랫폼의 전달 방식 때문에
  답변이 FAIL 하지 않게 하려는 것이다. 주장 자체는 평가자가 웹 검색으로 확인한다.
- 원래 URL 확인(resolve_relays)은 명시적으로 요청할 때만 네트워크를 쓴다. 공개 주소·포트·TLS 검사는
  기존 출처 수집기(code_eval.semantic_sources)를 그대로 쓰며, 이동 주소만 읽고 본문은 받지 않는다.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from code_eval.answer_citations import _sentence
from code_eval.semantic_evidence import source_url

MAX_SOURCES = 200
MAX_CLAIMS = 3
#: AI 플랫폼이 링크에 덧붙이는 유입 표시. 이 값일 때만 utm_* 를 걷어 원래 URL 로 본다 (다른 utm 은 그대로 둔다).
PLATFORM_UTM_SOURCES = frozenset({"chatgpt.com", "openai", "chatgpt", "perplexity", "perplexity.ai", "copilot.com",
                                  "copilot.microsoft.com", "gemini.google.com", "claude.ai", "grok.com", "you.com",
                                  "genspark.ai", "liner.com", "wrtn.ai"})
#: 쿼리에 원래 URL 을 담는 중계 주소 (호스트, 경로 접두, 쿼리 키).
QUERY_RELAYS = (
    (re.compile(r"(?:www\.)?google\.[a-z.]+"), "/url", ("q", "url")),
    (re.compile(r"l\.facebook\.com|lm\.facebook\.com"), "/l.php", ("u",)),
    (re.compile(r"duckduckgo\.com"), "/l/", ("uddg",)),
    (re.compile(r"(?:www\.)?youtube\.com"), "/redirect", ("q",)),
)
#: 원래 URL 을 쿼리에 담지 않는 중계 주소 — 이동 주소를 실제로 읽어야만 알 수 있다.
OPAQUE_RELAYS = (
    (re.compile(r"vertexaisearch\.cloud\.google\.com"), "/grounding-api-redirect/"),
    (re.compile(r"t\.co"), "/"),
    (re.compile(r"(?:www\.)?bing\.com"), "/ck/a"),
    (re.compile(r"news\.google\.com"), "/rss/articles/"),
    (re.compile(r"lnkd\.in"), "/"),
)
URL_STATUS_LABELS = {
    "original": "원래 URL",
    "tracking_removed": "플랫폼 유입 표시만 제거",
    "relay_unwrapped": "중계 URL 에 담긴 원래 URL",
    "relay_resolved": "중계 URL 이동 주소 확인",
    "relay_unresolved": "중계 URL — 원래 주소 미확인",
    "invalid": "올바르지 않은 URL",
    "missing": "URL 없음",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _host_path(url: str) -> Tuple[str, str]:
    try:
        parts = urlsplit(url)
        return (parts.hostname or "").lower(), parts.path or "/"
    except ValueError:
        return "", "/"


def relay_kind(url: Optional[str]) -> Optional[str]:
    """'query' (쿼리에 원래 URL 이 있음), 'opaque' (이동해야 앎), None (중계 아님)."""
    if not isinstance(url, str):
        return None
    host, path = _host_path(url)
    for pattern, prefix, _ in QUERY_RELAYS:
        if pattern.fullmatch(host) and path.startswith(prefix):
            return "query"
    for pattern, prefix in OPAQUE_RELAYS:
        if pattern.fullmatch(host) and path.startswith(prefix):
            return "opaque"
    return None


def unwrap_query_relay(url: str) -> Optional[str]:
    host, path = _host_path(url)
    for pattern, prefix, keys in QUERY_RELAYS:
        if pattern.fullmatch(host) and path.startswith(prefix):
            query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
            for key in keys:
                candidate = query.get(key)
                if candidate and source_url(candidate):
                    return candidate
    return None


def strip_platform_tracking(url: str) -> str:
    """utm_source 가 AI 플랫폼 값일 때만 utm_* 를 걷는다. 나머지 쿼리·순서는 그대로 둔다."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    source = next((v for k, v in pairs if k.lower() == "utm_source"), None)
    if source is None or source.lower() not in PLATFORM_UTM_SOURCES:
        return url
    kept = [(k, v) for k, v in pairs if not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept, doseq=True), parts.fragment))


def classify_url(display: Optional[str], declared_original: Optional[str] = None,
                 declared_relay: Optional[str] = None) -> Dict[str, Any]:
    """제공된 URL 을 원래 URL·중계 URL 로 나눈다. 원래 URL 을 모르면 None 으로 둔다."""
    out: Dict[str, Any] = {"display_url": display, "original_url": None, "relay_url": None, "redirect": None}
    if declared_relay:
        out["relay_url"] = declared_relay
    if display is None and declared_original is None and declared_relay is None:
        out["url_status"] = "missing"
        return out
    candidate = display if display is not None else declared_original
    kind = relay_kind(candidate)
    if kind:
        out["relay_url"] = candidate
        unwrapped = unwrap_query_relay(candidate) if kind == "query" else None
        if declared_original and declared_original != candidate:
            out["original_url"] = strip_platform_tracking(declared_original)
            out["redirect"] = {"method": "declared_by_platform"}
            out["url_status"] = "relay_resolved"
        elif unwrapped:
            out["original_url"] = strip_platform_tracking(unwrapped)
            out["redirect"] = {"method": "query_parameter"}
            out["url_status"] = "relay_unwrapped"
        else:
            out["url_status"] = "relay_unresolved"
        return out
    original = declared_original or candidate
    stripped = strip_platform_tracking(original)
    out["original_url"] = stripped
    if stripped != original:
        out["redirect"] = {"method": "tracking_param_removed"}
        out["url_status"] = "tracking_removed"
    else:
        out["url_status"] = "original"
    if source_url(stripped) is None:
        out["url_status"] = "invalid"
    return out


def _claim_at(answer: str, start: int, end: int) -> Optional[str]:
    if not (0 <= start <= end <= len(answer)):
        return None
    s, e = _sentence(answer, start)
    e = max(e, min(end, len(answer)))
    text = answer[s:e].strip()
    return text or None


def link_claims(answer: str, *, spans: Any = None, marker: Any = None, claims: Any = None,
                segment_texts: Any = None) -> Tuple[List[str], List[List[int]]]:
    """출처가 붙은 답변 구절. 위치(span)·인용 표시(marker)·플랫폼이 준 구절(segment) 순으로 찾는다.

    구절은 항상 답변 원문에 실제로 있는 문장이다. 원문에서 찾지 못한 구절은 쓰지 않는다.
    """
    found: List[str] = []
    located: List[List[int]] = []

    def add(claim: Optional[str]) -> None:
        if claim and claim not in found:
            found.append(claim)

    for span in spans if isinstance(spans, list) else []:
        if (isinstance(span, (list, tuple)) and len(span) == 2 and all(isinstance(v, int) and not isinstance(v, bool) for v in span)):
            claim = _claim_at(answer, span[0], span[1])
            if claim:
                located.append([span[0], span[1]])
                add(claim)
    if isinstance(marker, str) and marker:
        for match in re.finditer(re.escape(marker), answer):
            add(_claim_at(answer, match.start(), match.end()))
    for text in segment_texts if isinstance(segment_texts, list) else []:
        if isinstance(text, str) and text.strip():
            position = answer.find(text)
            if position >= 0:
                located.append([position, position + len(text)])
                add(_claim_at(answer, position, position + len(text)))
    for text in claims if isinstance(claims, list) else []:
        if isinstance(text, str) and text.strip() and text in answer:
            add(text.strip())
    return found[:MAX_CLAIMS], located


def normalize_sources(value: Any, answer: str, path: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """구조화 출처 목록을 정규화한다. (출처 목록, 수집 품질 기록). 형식이 틀린 항목도 버리지 않고 기록한다."""
    from .schema import quality
    if value is None:
        return [], []
    notes: List[Dict[str, str]] = []
    if not isinstance(value, list):
        return [], [quality("sources_invalid", f"{path}: 출처는 배열이어야 해서 출처 정보를 쓰지 못했습니다.", "error")]
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(value[:MAX_SOURCES]):
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict):
            notes.append(quality("source_invalid", f"{path}[{index}]: 출처 항목이 객체가 아니어서 제외했습니다.", "warning"))
            continue
        text = lambda key: item.get(key) if isinstance(item.get(key), str) and item.get(key).strip() else None
        display = text("display_url") or text("url")
        classified = classify_url(display, text("original_url"), text("relay_url"))
        if isinstance(item.get("redirect"), dict) and classified["url_status"] in ("relay_unresolved", "relay_resolved"):
            redirect = item["redirect"]
            resolved = redirect.get("resolved_url") if isinstance(redirect.get("resolved_url"), str) else None
            if resolved and source_url(resolved):
                classified.update(original_url=resolved, url_status="relay_resolved")
            classified["redirect"] = {k: redirect[k] for k in ("method", "checked_at", "status", "resolved_url", "chain")
                                      if k in redirect}
        url = classified["original_url"] if classified["url_status"] not in ("relay_unresolved", "missing") else None
        claims, spans = link_claims(answer, spans=item.get("spans"), marker=text("marker"), claims=item.get("claims"),
                                    segment_texts=item.get("segments"))
        source: Dict[str, Any] = {
            "id": text("id") or f"S{len(out) + 1}", "url": url, **classified,
            "title": text("title"), "marker": text("marker"), "claims": claims, "spans": spans,
            "origin": text("origin") or "structured",
            "platform_retrieved_at": text("platform_retrieved_at") or text("retrieved_at"),
            "published_at": text("published_at"),
            "snapshot": item["snapshot"] if isinstance(item.get("snapshot"), dict) else None,
        }
        if classified["url_status"] == "relay_unresolved":
            source["skip_reason"] = "relay_unresolved"
            source["url"] = None
            notes.append(quality("relay_unresolved", f"출처 {source['id']}: 플랫폼 중계 URL 의 원래 주소를 알 수 없어 "
                                                     "J5 원문 대조에서 제외했습니다 (주장은 평가자 검색으로 확인).", "warning"))
        elif classified["url_status"] == "missing":
            source["skip_reason"] = "no_url"
            notes.append(quality("source_url_missing", f"출처 {source['id']}: URL 이 없어 원문을 대조할 수 없습니다.", "info"))
        elif classified["url_status"] == "invalid":
            notes.append(quality("source_url_invalid", f"출처 {source['id']}: 올바른 http(s) URL 이 아닙니다 "
                                                       f"({str(display)[:120]}).", "warning"))
        out.append(source)
    if isinstance(value, list) and len(value) > MAX_SOURCES:
        notes.append(quality("sources_truncated", f"출처가 {len(value)}개라 앞 {MAX_SOURCES}개만 보관했습니다.", "warning"))
    return out, notes


def evaluation_sources(sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """평가기(answer_sources)로 넘기는 최소 필드. URL 이 없는 출처는 대조할 수 없으므로 넘기지 않는다.

    중계 URL 만 아는 출처는 표시 URL 을 별칭으로 넘겨, 본문에 같은 중계 링크가 있어도 다시 대조하지 않게 한다.
    """
    out = []
    for s in sources:
        if s.get("skip_reason") == "relay_unresolved":
            out.append({"id": s["id"], "url": s["relay_url"], "display_url": s.get("display_url"),
                        "relay_url": s["relay_url"], "claims": list(s["claims"]), "origin": s["origin"],
                        "skip_reason": "relay_unresolved"})
            continue
        if not s.get("url"):
            continue
        item = {"id": s["id"], "url": s["url"], "claims": list(s["claims"]), "origin": s["origin"]}
        for key in ("display_url", "relay_url", "original_url", "title"):
            if s.get(key):
                item[key] = s[key]
        out.append(item)
    return out


def text_link_sources(answer: str) -> List[Dict[str, Any]]:
    """답변 본문에 직접 있는 링크를 구조화 출처로 (플랫폼 중계·유입 표시 구분을 위해). 판정은 하지 않는다."""
    from code_eval.answer_citations import answer_citations
    out = []
    for citation in answer_citations(answer):
        out.append({"url": citation["url"], "claims": citation["claims"], "origin": "answer_text"})
    return out


def resolve_relays(sources: List[Dict[str, Any]], *, fetcher: Optional[Callable[[str], Dict[str, Any]]] = None,
                   limit: int = 8) -> List[Dict[str, str]]:
    """중계 URL 의 이동 주소만 읽어 원래 URL 을 기록한다 (명시적 요청 시에만). 수집 품질 기록을 돌려준다.

    ``fetcher(url)`` 는 {"status": "redirect"|오류, "location"?} 를 돌려준다. 기본은 공개 주소 검사·TLS 검증을 하는
    ``single_hop`` 이다. 원래 URL 도 공개 http(s) 형식이어야 하며, 그렇지 않으면 미확인으로 남긴다.
    """
    from .schema import quality
    fetch = fetcher or single_hop
    notes, done = [], 0
    for source in sources:
        if source.get("url_status") != "relay_unresolved" or done >= limit:
            continue
        done += 1
        relay = source["relay_url"]
        result = fetch(relay)
        location = result.get("location") if isinstance(result, dict) else None
        resolved = urljoin(relay, location) if isinstance(location, str) and location.strip() else None
        checked = result.get("checked_at") if isinstance(result, dict) and isinstance(result.get("checked_at"), str) else _now()
        if resolved and source_url(resolved) and relay_kind(resolved) is None:
            original = strip_platform_tracking(resolved)
            source.update(original_url=original, url=original, url_status="relay_resolved",
                          redirect={"method": "server_redirect_read", "checked_at": checked, "status": "ok",
                                    "resolved_url": resolved})
            source.pop("skip_reason", None)
        else:
            status = result.get("status") if isinstance(result, dict) else "fetch_error"
            source["redirect"] = {"method": "server_redirect_read", "checked_at": checked,
                                  "status": status if isinstance(status, str) else "fetch_error"}
            notes.append(quality("relay_resolve_failed", f"출처 {source['id']}: 중계 URL 의 이동 주소를 확인하지 못했습니다 "
                                                         f"({source['redirect']['status']}).", "warning"))
    return notes


def single_hop(url: str) -> Dict[str, Any]:
    """중계 URL 에 한 번 요청해 이동 주소(Location)만 읽는다. 본문은 받지 않는다.

    주소 형식·표준 포트·공개 IP 확인과 TLS 인증서 검증은 J5 출처 수집기와 같은 함수를 쓴다.
    """
    import time
    from code_eval.semantic_sources import FETCH_TIMEOUT, SourceError, _request, _resolve_public, _target
    deadline = time.monotonic() + FETCH_TIMEOUT
    try:
        target = _target(url)
        addresses = _resolve_public(target[1], target[2], deadline)
        result = _request(target, addresses[0], deadline)
    except SourceError as exc:
        return {"status": exc.status, "checked_at": _now()}
    except Exception:
        return {"status": "fetch_error", "checked_at": _now()}
    if "redirect" in result:
        return {"status": "redirect", "location": result["redirect"], "checked_at": _now()}
    return {"status": "no_redirect", "checked_at": _now()}
