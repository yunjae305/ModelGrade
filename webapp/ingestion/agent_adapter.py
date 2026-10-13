"""에이전트 실행 기록 → 공통 답변. 평가기에는 최종 답변만 넘기고, 도구 사용 기록은 근거·실행 환경으로 보존한다.

도구 사용 과정 자체를 평가하는 공식 기준은 없다 (이번 단계에서 새 기준을 만들지 않음). 실행이 실패해 최종 답변이
없으면 수집 실패(ERROR)로 두어 품질 FAIL 로 세지 않는다. 실패·중단으로 끝났는데 글자가 남은 경우는 완성된 답변인지
알 수 없으므로 미완료(INCOMPLETE)로 두고 평가에서 뺀다.

지원 형식:
- ``modelgrade-agent-run/v1`` — 프레임워크와 무관한 일반 형식
- OpenAI Agents SDK trace·span 내보내기 (span_data: agent·function·generation·response·handoff·guardrail·custom)
- LangSmith run (run_type: chain·llm·tool·retriever …, child_runs 또는 parent_run_id)
- OpenTelemetry GenAI span (gen_ai.operation.name: invoke_agent·execute_tool·chat …, OTLP JSON 포함)
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .builder import BundleBuilder, safe_id, system_from
from .schema import parse_time, quality

AGENT_SCHEMA = "modelgrade-agent-run/v1"
PREVIEW_CHARS = 4000
_FINISHED = {"completed", "success", "succeeded", "ok", "done"}
_WEB_WORDS = ("search", "browse", "browser", "web", "fetch", "crawl")


class AgentImportError(ValueError):
    pass


def _preview(value: Any) -> Tuple[Any, bool]:
    if value is None:
        return None, False
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return (text[:PREVIEW_CHARS], True) if len(text) > PREVIEW_CHARS else (text, False)


def _ms(start: Optional[str], end: Optional[str]) -> Optional[float]:
    if not start or not end:
        return None
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000, 1)
    except ValueError:
        return None


def _step(index: int, kind: str, name: Any, *, status: str = "ok", error: Any = None, started: Any = None, ended: Any = None,
          input_value: Any = None, output_value: Any = None, sources: Any = None) -> Dict[str, Any]:
    started, ended = parse_time(started), parse_time(ended)
    inp, cut_in = _preview(input_value)
    out, cut_out = _preview(output_value)
    step = {"index": index, "type": kind, "name": str(name) if name else kind, "status": status,
            "error": (error if isinstance(error, str) else json.dumps(error, ensure_ascii=False, default=str)) if error else None,
            "started_at": started, "ended_at": ended, "duration_ms": _ms(started, ended), "input": inp, "output": out}
    if cut_in or cut_out:
        step["truncated"] = True
    found = [{"url": s["url"], "title": s.get("title")} for s in sources or [] if isinstance(s, dict) and isinstance(s.get("url"), str)]
    if found:
        step["sources"] = found
    return step


def _agent_record(framework: str, run_id: Any, status: str, steps: List[Dict[str, Any]], started: Any, ended: Any,
                  tools_available: Optional[List[str]] = None) -> Dict[str, Any]:
    started, ended = parse_time(started), parse_time(ended)
    tools = [s for s in steps if s["type"] == "tool"]
    failed = [s["index"] for s in steps if s["status"] != "ok"]
    retrieved = [src for s in steps for src in s.get("sources", [])]
    return {"framework": framework, "run_id": str(run_id) if run_id is not None else None, "status": status,
            "started_at": started, "ended_at": ended, "duration_ms": _ms(started, ended), "steps": steps,
            "tool_calls": len(tools), "tool_errors": sum(s["status"] != "ok" for s in tools), "failed_steps": failed,
            "retrieved_sources": retrieved, "tools_available": tools_available,
            "evaluation_scope": "final_answer_only",
            "note": "도구 사용 과정은 평가 기준이 아니며 근거·실행 환경 정보로만 보존합니다."}


def _response(final: Any, record: Dict[str, Any], *, error: Any = None, usage: Optional[Dict[str, Any]] = None,
              sources: Any = None) -> Dict[str, Any]:
    status = record["status"]
    finished = status in _FINISHED
    text = final if isinstance(final, str) else None
    notes = []
    if record["tool_errors"]:
        notes.append(quality("agent_tool_failures", f"도구 실행 실패 {record['tool_errors']}건이 있었습니다 "
                                                    f"(단계 {', '.join(map(str, record['failed_steps']))}).", "warning"))
    if text is None or not text.strip():
        state, message = "ERROR", "에이전트 실행에서 최종 답변을 찾지 못했습니다" + (f" ({status})" if status else "") + "."
    elif not finished:
        state, message = "INCOMPLETE", f"에이전트 실행이 '{status}' 상태로 끝나 완성된 답변인지 확인할 수 없어 평가에서 제외합니다."
    else:
        state, message = "SUCCESS", None
    if error and state != "SUCCESS":
        message = f"{message} 오류: {_preview(error)[0][:300]}"
    names = [s["name"] for s in record["steps"] if s["type"] == "tool"]
    searched = any(any(w in n.lower() for w in _WEB_WORDS) for n in names) or bool(record["retrieved_sources"])
    return {"answer": text or "", "status": state, "error": message, "collected_at": record["ended_at"],
            "environment": {"web_search_used": True if searched else (False if record["steps"] else None),
                            "tools_used": list(dict.fromkeys(names))},
            "usage": {k: v for k, v in (usage or {}).items() if v is not None} | (
                {"latency_ms": record["duration_ms"]} if record["duration_ms"] is not None else {}),
            "sources": sources or [], "agent": record, "collection_quality": notes}


# --- 일반 형식 ------------------------------------------------------------------------------

def generic(value: Dict[str, Any], options: Dict[str, Any], name: str) -> Dict[str, Any]:
    system = system_from({"system": {**(value.get("system") or {}), **((options or {}).get("system") or {})}},
                         {"access_mode": "agent"})
    builder = BundleBuilder(system, adapter="agent_generic", format_label="에이전트 실행 기록", source_name=name,
                            collection_method="agent_trace")
    runs = value.get("runs") if isinstance(value.get("runs"), list) else [value]
    for number, run in enumerate(runs, 1):
        if not isinstance(run, dict):
            raise AgentImportError(f"runs[{number - 1}]: 객체여야 합니다.")
        request = run.get("request")
        if isinstance(request, dict):
            request = request.get("content") or request.get("text")
        if not isinstance(request, str) or not request.strip():
            raise AgentImportError(f"runs[{number - 1}]: 사용자 요청(request)이 없습니다.")
        steps = []
        for index, item in enumerate(run.get("steps") or [], 1):
            if not isinstance(item, dict):
                raise AgentImportError(f"runs[{number - 1}].steps[{index - 1}]: 객체여야 합니다.")
            steps.append(_step(index, str(item.get("type") or "tool"), item.get("name"),
                               status="ok" if item.get("status", "ok") in ("ok", "success", "completed") else "error",
                               error=item.get("error"), started=item.get("started_at"), ended=item.get("ended_at"),
                               input_value=item.get("input"), output_value=item.get("output"), sources=item.get("sources")))
        record = _agent_record("generic", run.get("run_id"), str(run.get("status") or "unknown").lower(), steps,
                               run.get("started_at"), run.get("ended_at"), run.get("tools_available"))
        response = _response(run.get("final_answer"), record, error=run.get("error"), usage=run.get("usage"),
                             sources=run.get("answer_sources"))
        key = str(run.get("conversation_id") or run.get("case_id") or run.get("run_id") or f"run-{number}")
        turn = run.get("turn")
        builder.add_conversation(key, [{"question": request, "response": response,
                                        **({"turn": turn} if isinstance(turn, int) and not isinstance(turn, bool) else {})}])
    return builder.bundle()


# --- OpenAI Agents SDK ----------------------------------------------------------------------

def looks_like_agents_sdk(value: Any) -> bool:
    spans = value.get("spans") if isinstance(value, dict) else value
    return isinstance(spans, list) and bool(spans) and all(isinstance(s, dict) and isinstance(s.get("span_data"), dict)
                                                          for s in spans[:5])


def _message_text(messages: Any, role: str, last: bool) -> Optional[str]:
    items = messages if isinstance(messages, list) else []
    for message in (reversed(items) if last else items):
        if not isinstance(message, dict) or message.get("role") != role:
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [c.get("text") for c in content if isinstance(c, dict) and isinstance(c.get("text"), str)]
            if texts:
                return "".join(texts)
    return None


def agents_sdk(value: Any, options: Dict[str, Any], name: str) -> Dict[str, Any]:
    wrapper = value if isinstance(value, dict) else {}
    spans = sorted([s for s in (wrapper.get("spans") if wrapper else value) if isinstance(s, dict)],
                   key=lambda s: str(s.get("started_at") or ""))
    trace = wrapper.get("trace") if isinstance(wrapper.get("trace"), dict) else {}
    steps, tools_available, usage, model = [], None, {"input_tokens": 0, "output_tokens": 0}, None
    first_input, last_output, counted = None, None, False
    for index, span in enumerate(spans, 1):
        data = span["span_data"]
        kind = data.get("type")
        status = "error" if span.get("error") else "ok"
        if kind == "function":
            steps.append(_step(len(steps) + 1, "tool", data.get("name"), status=status, error=span.get("error"),
                               started=span.get("started_at"), ended=span.get("ended_at"),
                               input_value=data.get("input"), output_value=data.get("output")))
        elif kind == "generation":
            model = data.get("model") or model
            first_input = first_input or _message_text(data.get("input"), "user", last=False)
            last_output = _message_text(data.get("output"), "assistant", last=True) or last_output
            spent = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            for key in ("input_tokens", "output_tokens"):
                if isinstance(spent.get(key), int):
                    usage[key] += spent[key]
                    counted = True
            steps.append(_step(len(steps) + 1, "llm", data.get("model") or "generation", status=status, error=span.get("error"),
                               started=span.get("started_at"), ended=span.get("ended_at")))
        elif kind == "agent":
            if isinstance(data.get("tools"), list):
                tools_available = list(dict.fromkeys((tools_available or []) + [str(t) for t in data["tools"]]))
        elif kind in ("handoff", "guardrail", "mcp_tools", "custom"):
            label = data.get("name") or (f"{data.get('from_agent')}→{data.get('to_agent')}" if kind == "handoff" else kind)
            triggered = kind == "guardrail" and data.get("triggered") is True
            steps.append(_step(len(steps) + 1, kind, label, status="error" if status == "error" or triggered else "ok",
                               error=span.get("error") or ("가드레일 작동" if triggered else None),
                               started=span.get("started_at"), ended=span.get("ended_at")))
    final = wrapper.get("final_output") if isinstance(wrapper.get("final_output"), str) else last_output
    request = wrapper.get("input") if isinstance(wrapper.get("input"), str) else first_input
    if not isinstance(request, str) or not request.strip():
        raise AgentImportError("Agents SDK 기록에서 사용자 요청을 찾지 못했습니다. input 을 함께 넣어 주세요.")
    errored = any(s.get("error") for s in spans)
    status = str(wrapper.get("status") or ("failed" if errored and not final else "completed")).lower()
    record = _agent_record("openai-agents-sdk", trace.get("id") or (spans[0].get("trace_id") if spans else None), status, steps,
                           spans[0].get("started_at") if spans else None, spans[-1].get("ended_at") if spans else None,
                           tools_available)
    response = _response(final, record, usage=usage if counted else None)
    if not isinstance(wrapper.get("final_output"), str) and final:
        response["collection_quality"].append(quality("agent_final_from_last_generation", "최종 답변을 마지막 생성 단계의 출력에서 가져왔습니다 "
                                                                                          "(final_output 이 없음).", "info"))
    system = system_from(options, {"system_key": safe_id(trace.get("workflow_name") or "openai-agent").lower(),
                                   "provider": "openai", "platform": "OpenAI Agents SDK", "model": model or "unknown",
                                   "access_mode": "agent",
                                   "environment": {"tools_available": tools_available, "web_search_available": None}})
    builder = BundleBuilder(system, adapter="agents_sdk", format_label="OpenAI Agents SDK trace", source_name=name,
                            collection_method="agent_trace")
    builder.add_conversation(str(record["run_id"] or "agent-run"), [{"question": request, "response": response}])
    return builder.bundle()


# --- LangSmith ------------------------------------------------------------------------------

def looks_like_langsmith(value: Any) -> bool:
    items = value if isinstance(value, list) else [value]
    return bool(items) and all(isinstance(r, dict) and "run_type" in r for r in items[:5])


def _flatten_runs(value: Any) -> List[Dict[str, Any]]:
    out, pending = [], list(value if isinstance(value, list) else [value])
    while pending:
        run = pending.pop(0)
        if isinstance(run, dict):
            out.append(run)
            pending.extend(run.get("child_runs") or [])
    return out


def _first_text(value: Any, keys: Tuple[str, ...]) -> Optional[str]:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    for key in keys:
        if isinstance(value.get(key), str):
            return value[key]
        if isinstance(value.get(key), dict) and isinstance(value[key].get("content"), str):
            return value[key]["content"]
    messages = value.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict) and isinstance(last.get("content"), str):
            return last["content"]
    return None


def langsmith(value: Any, options: Dict[str, Any], name: str) -> Dict[str, Any]:
    runs = _flatten_runs(value)
    ids = {r.get("id") for r in runs}
    roots = [r for r in runs if not r.get("parent_run_id") or r.get("parent_run_id") not in ids]
    if len(roots) != 1:
        raise AgentImportError(f"LangSmith 기록의 최상위 실행이 {len(roots)}개입니다. 한 실행 트리씩 가져와 주세요.")
    root = roots[0]
    steps = []
    for run in sorted((r for r in runs if r is not root), key=lambda r: str(r.get("start_time") or "")):
        kind = {"tool": "tool", "llm": "llm", "retriever": "retrieval"}.get(run.get("run_type"), str(run.get("run_type") or "other"))
        sources = []
        if kind == "retrieval":
            for doc in (run.get("outputs") or {}).get("documents") or []:
                meta = doc.get("metadata") if isinstance(doc, dict) and isinstance(doc.get("metadata"), dict) else {}
                url = meta.get("source") or meta.get("url")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    sources.append({"url": url, "title": meta.get("title")})
        steps.append(_step(len(steps) + 1, kind, run.get("name"), status="error" if run.get("error") else "ok",
                           error=run.get("error"), started=run.get("start_time"), ended=run.get("end_time"),
                           input_value=run.get("inputs"), output_value=run.get("outputs"), sources=sources))
    request = _first_text(root.get("inputs"), ("input", "question", "query", "prompt"))
    if not isinstance(request, str) or not request.strip():
        raise AgentImportError("LangSmith 최상위 실행의 inputs 에서 사용자 요청을 찾지 못했습니다.")
    final = _first_text(root.get("outputs"), ("output", "answer", "final_answer", "result"))
    status = str(root.get("status") or ("error" if root.get("error") else "success")).lower()
    record = _agent_record("langsmith", root.get("id"), status, steps, root.get("start_time"), root.get("end_time"))
    usage = {"input_tokens": root.get("prompt_tokens"), "output_tokens": root.get("completion_tokens"),
             "total_tokens": root.get("total_tokens")}
    response = _response(final, record, error=root.get("error"), usage=usage)
    system = system_from(options, {"system_key": safe_id(root.get("name") or "langsmith-agent").lower(), "provider": "unknown",
                                   "platform": "LangSmith", "model": "unknown", "access_mode": "agent"})
    builder = BundleBuilder(system, adapter="langsmith", format_label="LangSmith run", source_name=name,
                            collection_method="agent_trace")
    builder.add_conversation(str(root.get("id") or "agent-run"), [{"question": request, "response": response}])
    return builder.bundle()


# --- OpenTelemetry GenAI ------------------------------------------------------------------

def _otel_value(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if key in value:
                return int(value[key]) if key == "intValue" and isinstance(value[key], str) and value[key].isdigit() else value[key]
        if "arrayValue" in value:
            return [_otel_value(v) for v in (value["arrayValue"].get("values") or [])]
    return value


def _otel_spans(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, dict) and isinstance(value.get("resourceSpans"), list):
        raw = [s for r in value["resourceSpans"] for scope in r.get("scopeSpans") or [] for s in scope.get("spans") or []]
    else:
        raw = value if isinstance(value, list) else []
    spans = []
    for span in raw:
        if not isinstance(span, dict):
            continue
        attrs = span.get("attributes")
        if isinstance(attrs, list):
            attrs = {a.get("key"): _otel_value(a.get("value")) for a in attrs if isinstance(a, dict)}
        def nano(key_ns, key):
            if span.get(key_ns):
                try:
                    return datetime.fromtimestamp(int(span[key_ns]) / 1e9).astimezone().isoformat()
                except (TypeError, ValueError, OverflowError):
                    return None
            return span.get(key)
        status = span.get("status") if isinstance(span.get("status"), dict) else {}
        spans.append({"id": span.get("spanId") or span.get("span_id"), "parent": span.get("parentSpanId") or span.get("parent_span_id"),
                      "name": span.get("name"), "attributes": attrs or {}, "start": nano("startTimeUnixNano", "start_time"),
                      "end": nano("endTimeUnixNano", "end_time"),
                      "error": status.get("code") in (2, "STATUS_CODE_ERROR", "ERROR")})
    return spans


def looks_like_otel(value: Any) -> bool:
    return any(any(str(k).startswith("gen_ai.") for k in s["attributes"]) for s in _otel_spans(value)[:20])


def _otel_text(value: Any, role: str, last: bool) -> Optional[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    items = value if isinstance(value, list) else []
    for message in (reversed(items) if last else items):
        if isinstance(message, dict) and message.get("role") == role:
            parts = [p.get("content") for p in message.get("parts") or [] if isinstance(p, dict) and p.get("type") == "text"]
            if parts and all(isinstance(p, str) for p in parts):
                return "".join(parts)
    return None


def otel(value: Any, options: Dict[str, Any], name: str) -> Dict[str, Any]:
    spans = sorted(_otel_spans(value), key=lambda s: str(s["start"] or ""))
    roots = [s for s in spans if s["attributes"].get("gen_ai.operation.name") == "invoke_agent"] or \
            [s for s in spans if not s["parent"]]
    if not roots:
        raise AgentImportError("OpenTelemetry 기록에서 에이전트 실행(invoke_agent) span 을 찾지 못했습니다.")
    root = roots[0]
    steps, model, tools = [], root["attributes"].get("gen_ai.request.model"), []
    for span in spans:
        if span is root:
            continue
        attrs = span["attributes"]
        operation = attrs.get("gen_ai.operation.name")
        if operation == "execute_tool":
            steps.append(_step(len(steps) + 1, "tool", attrs.get("gen_ai.tool.name") or span["name"],
                               status="error" if span["error"] else "ok", error="span status ERROR" if span["error"] else None,
                               started=span["start"], ended=span["end"], input_value=attrs.get("gen_ai.tool.call.arguments"),
                               output_value=attrs.get("gen_ai.tool.call.result")))
        elif operation in ("chat", "text_completion", "generate_content"):
            model = model or attrs.get("gen_ai.request.model")
            steps.append(_step(len(steps) + 1, "llm", attrs.get("gen_ai.request.model") or span["name"],
                               status="error" if span["error"] else "ok", started=span["start"], ended=span["end"]))
    attrs = root["attributes"]
    request = _otel_text(attrs.get("gen_ai.input.messages"), "user", last=False)
    if not request:
        raise AgentImportError("invoke_agent span 의 gen_ai.input.messages 에서 사용자 요청을 찾지 못했습니다.")
    final = _otel_text(attrs.get("gen_ai.output.messages"), "assistant", last=True)
    record = _agent_record("opentelemetry-genai", attrs.get("gen_ai.agent.id") or root["id"],
                           "failed" if root["error"] else "completed", steps, root["start"], root["end"])
    usage = {"input_tokens": attrs.get("gen_ai.usage.input_tokens"), "output_tokens": attrs.get("gen_ai.usage.output_tokens")}
    response = _response(final, record, usage={k: v for k, v in usage.items() if isinstance(v, int)})
    system = system_from(options, {"system_key": safe_id(attrs.get("gen_ai.agent.name") or "otel-agent").lower(),
                                   "provider": str(attrs.get("gen_ai.provider.name") or attrs.get("gen_ai.system") or "unknown"),
                                   "platform": "OpenTelemetry GenAI", "model": model or "unknown", "access_mode": "agent"})
    builder = BundleBuilder(system, adapter="otel_genai", format_label="OpenTelemetry GenAI span", source_name=name,
                            collection_method="agent_trace")
    conversation = attrs.get("gen_ai.conversation.id")
    builder.add_conversation(str(conversation or record["run_id"] or "agent-run"), [{"question": request, "response": response}])
    return builder.bundle()
