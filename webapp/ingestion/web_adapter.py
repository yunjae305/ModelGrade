"""웹서비스 답변 가져오기 — 공식 내보내기·사용자 제공 파일·직접 복사만 다룬다.

로그인 우회·자동 화면 수집은 하지 않는다. ChatGPT·Claude.ai·Perplexity 등 소비자 서비스 약관은 자동 추출을
금지하므로(2026-10-13 확인), 사용자가 공식 내보내기나 직접 저장한 파일만 읽는다.

- ChatGPT 공식 내보내기 ``conversations.json`` — OpenAI 의 공식 스키마 문서가 없어 실제 파일 구조(관찰)를 따른다.
  ``mapping`` 트리에서 ``current_node`` 까지의 현재 분기만 쓰고, 편집·재생성으로 생긴 다른 분기는 제외해 기록한다.
- Claude.ai 내보내기 ``conversations.json`` — 공식 문서는 JSON 형식만 밝히고 필드명은 관찰 기반이다.
- ``modelgrade-transcript/v1`` — 사용자가 직접 정리한 대화(역할별 메시지) JSON.
- HTML — 메시지 역할 표시(data-message-author-role)가 있는 저장 페이지, 또는 질문을 따로 받은 단일 답변 HTML.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .builder import BundleBuilder, safe_id, system_from
from .html_markdown import html_to_markdown, parse_html
from .schema import parse_time, quality

TRANSCRIPT_SCHEMA = "modelgrade-transcript/v1"
_PUA_MARKER = re.compile("[^]{0,400}")
_OLD_CITATION = re.compile(r"【\d+(?::\d+)?†[^】]{0,80}】")
_CITATION_REF_TYPES = {"grouped_webpages", "webpage", "grouped_webpages_model_predicted_fallback", "sources_footnote",
                       "webpage_extended", "attribution"}
_WEB_RECIPIENTS = ("browser", "web", "web.run", "web.search", "search")


class ImportError_(ValueError):
    pass


# --- ChatGPT 공식 내보내기 -------------------------------------------------------------------

def looks_like_chatgpt(value: Any) -> bool:
    items = value if isinstance(value, list) else [value]
    return bool(items) and all(isinstance(c, dict) and isinstance(c.get("mapping"), dict) for c in items[:5])


def _path(mapping: Dict[str, Any], current: Any) -> Tuple[List[Dict[str, Any]], int]:
    """current_node 에서 뿌리까지 올라간 현재 분기 (뿌리→현재 순). 버린 분기 수도 센다."""
    node_id = current if current in mapping else None
    if node_id is None:   # current_node 가 없으면 자식이 없는 노드 중 마지막 (추측이 아니라 유일할 때만)
        leaves = [k for k, v in mapping.items() if isinstance(v, dict) and not v.get("children")]
        if len(leaves) != 1:
            raise ImportError_("current_node 가 없고 대화 끝이 여러 갈래라 어느 분기를 쓸지 정할 수 없습니다.")
        node_id = leaves[0]
    path, seen, branches = [], set(), 0
    while node_id is not None and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        node = mapping[node_id]
        path.append(node)
        parent = node.get("parent")
        if parent in mapping and len(mapping[parent].get("children") or []) > 1:
            branches += len(mapping[parent]["children"]) - 1
        node_id = parent
    return list(reversed(path)), branches


def _parts_text(content: Dict[str, Any]) -> Tuple[str, int]:
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    texts = [p for p in parts if isinstance(p, str)]
    others = sum(1 for p in parts if not isinstance(p, str))
    if not parts and isinstance(content.get("text"), str):
        texts = [content["text"]]
    return "\n".join(texts), others


def _chatgpt_part(text: str, meta: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, str]]]:
    """플랫폼 내부 인용 표시를 화면 표시 글자(alt)로 바꾸거나 지우고, 그 자리를 출처의 위치로 남긴다."""
    notes: List[Dict[str, str]] = []
    edits: List[Tuple[int, int, str, List[Dict[str, Any]]]] = []
    taken: List[Tuple[int, int]] = []

    def free(s: int, e: int) -> bool:
        return all(e <= a or s >= b for a, b in taken)

    for ref in meta.get("content_references") or []:
        if not isinstance(ref, dict) or not isinstance(ref.get("matched_text"), str) or not ref["matched_text"]:
            continue
        matched = ref["matched_text"]
        s, e = ref.get("start_idx"), ref.get("end_idx")
        if not (isinstance(s, int) and isinstance(e, int) and text[s:e] == matched and free(s, e)):
            s = next((m.start() for m in re.finditer(re.escape(matched), text) if free(m.start(), m.end())), -1)
            e = s + len(matched)
        if s < 0:
            continue
        urls = []
        for item in (ref.get("items") or []) + [ref]:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                urls.append({"url": item["url"], "title": item.get("title") if isinstance(item.get("title"), str) else None,
                             "published_at": item.get("pub_date") if isinstance(item.get("pub_date"), str) else None})
        alt = ref.get("alt") if isinstance(ref.get("alt"), str) else None
        if alt is None and not urls and ref.get("type") not in _CITATION_REF_TYPES:
            continue   # 무엇을 보여 주는 표시인지 모르면 글자를 그대로 둔다
        edits.append((s, e, alt if alt is not None else "", urls))
        taken.append((s, e))
    for cite in meta.get("citations") or []:
        if not isinstance(cite, dict):
            continue
        s, e = cite.get("start_ix"), cite.get("end_ix")
        body = cite.get("metadata") if isinstance(cite.get("metadata"), dict) else {}
        if isinstance(s, int) and isinstance(e, int) and 0 <= s <= e <= len(text) and free(s, e) and isinstance(body.get("url"), str):
            edits.append((s, e, "", [{"url": body["url"], "title": body.get("title") if isinstance(body.get("title"), str) else None}]))
            taken.append((s, e))
    for pattern, code in ((_OLD_CITATION, "citation_marker_removed"), (_PUA_MARKER, "platform_marker_removed")):
        for m in pattern.finditer(text):
            if free(m.start(), m.end()):
                edits.append((m.start(), m.end(), "", []))
                taken.append((m.start(), m.end()))
                if code == "platform_marker_removed":
                    notes.append(quality(code, "출처 정보를 알 수 없는 플랫폼 내부 표시 문자를 평가용 답변에서 지웠습니다 (원본은 보존). "
                                               "화면에 보였던 글자를 복원하지 못했을 수 있습니다.", "warning"))
    out, sources, cursor = [], [], 0
    for s, e, replacement, urls in sorted(edits):
        if replacement == "" and s > 0 and text[s - 1] == " " and e < len(text) and text[e] == " ":
            e += 1   # 표시를 지운 자리에 공백 두 개가 남지 않게 한다
        out.append(text[cursor:s])
        position = sum(len(x) for x in out)
        out.append(replacement)
        anchor = position
        while anchor > 0 and "".join(out)[anchor - 1].isspace():
            anchor -= 1
        for entry in urls:
            sources.append({**entry, "spans": [[max(0, anchor - 1), max(0, anchor - 1) + max(1, len(replacement))]],
                            "origin": "export_metadata"})
        cursor = e
    out.append(text[cursor:])
    if edits:
        notes.append(quality("citation_markers_normalized", f"인용 표시 {len(edits)}개를 화면 표시 글자로 바꾸거나 지웠습니다. "
                                                             "원본 글자는 original 에 그대로 있습니다.", "info"))
    return "".join(out), sources, notes


def chatgpt_export(value: Any, options: Dict[str, Any], name: str) -> Dict[str, Any]:
    conversations = value if isinstance(value, list) else [value]
    wanted = set(options.get("conversation_ids") or [])
    models: set = set()
    builder = BundleBuilder(system_from(options, {
        "system_key": "chatgpt-web", "provider": "openai", "platform": "ChatGPT", "model": "unknown", "access_mode": "web",
        "environment": {"web_search_available": None, "tools_available": None,
                        "system_instruction": {"known": False},
                        "notes": ["ChatGPT 공식 내보내기에는 웹검색 허용 여부·도구 설정이 들어 있지 않습니다."]}}),
        adapter="chatgpt_export", format_label="ChatGPT 공식 내보내기", source_name=name, collection_method="official_export")
    custom_instructions = False
    for number, conv in enumerate(conversations, 1):
        if not isinstance(conv, dict) or not isinstance(conv.get("mapping"), dict):
            builder.note(f"{number}번 항목은 대화 형식이 아니어서 제외했습니다.")
            continue
        cid = str(conv.get("conversation_id") or conv.get("id") or f"conversation-{number}")
        if wanted and cid not in wanted:
            continue
        path, branches = _path(conv["mapping"], conv.get("current_node"))
        if branches:
            builder.note(f"대화 {cid}: 편집·재생성으로 생긴 다른 분기 {branches}개는 제외하고 화면의 현재 분기만 사용했습니다.")
        turns: List[Dict[str, Any]] = []
        times: List[float] = []
        for node in path:
            message = node.get("message")
            if not isinstance(message, dict):
                continue
            author = message.get("author") if isinstance(message.get("author"), dict) else {}
            role = author.get("role")
            meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), dict) else {}
            ctype = content.get("content_type")
            if ctype == "user_editable_context":
                custom_instructions = True
                continue
            if meta.get("is_visually_hidden_from_conversation") or role == "system":
                continue
            created = message.get("create_time")
            if isinstance(created, (int, float)) and not isinstance(created, bool):
                if times and created < times[-1]:
                    raise ImportError_(f"대화 {cid}: 메시지 시각이 뒤바뀌어 있습니다. 턴 순서를 추측하지 않습니다.")
                times.append(created)
            if role == "user":
                text, others = _parts_text(content)
                if others:
                    builder.note(f"대화 {cid}: 질문의 첨부(이미지·파일 {others}개)는 평가 입력에 넣지 않고 글자만 사용했습니다.")
                if not text.strip():
                    continue
                turns.append({"question": text, "parts": [], "tools": [], "searched": False, "slugs": set(),
                              "created": created, "ids": [message.get("id")]})
            elif not turns:
                if role == "assistant" and _parts_text(content)[0].strip():
                    builder.note(f"대화 {cid}: 첫 질문 전의 AI 메시지(인사말 등)는 제외했습니다.")
            elif role == "assistant":
                recipient = message.get("recipient", "all")
                if recipient != "all":
                    turns[-1]["tools"].append(str(recipient))
                    turns[-1]["searched"] |= str(recipient).split(".")[0] in ("browser", "web", "search")
                    continue
                if ctype not in ("text", "multimodal_text"):
                    if ctype in ("code", "execution_output"):
                        turns[-1]["tools"].append("python")
                    continue
                text, _ = _parts_text(content)
                if text:
                    turns[-1]["parts"].append((text, meta))
                    turns[-1]["ids"].append(message.get("id"))
                if isinstance(meta.get("model_slug"), str):
                    turns[-1]["slugs"].add(meta["model_slug"])
            elif role == "tool":
                tool = str(author.get("name") or "tool")
                turns[-1]["tools"].append(tool)
                turns[-1]["searched"] |= tool.split(".")[0] in ("browser", "web", "search")
        built = []
        for turn in turns:
            if not turn["parts"]:
                built.append({"question": turn["question"], "response": None})
                continue
            answer, sources, notes = "", [], []
            for text, meta in turn["parts"]:
                normalized, found, part_notes = _chatgpt_part(text, meta)
                base = len(answer) + (2 if answer else 0)
                for source in found:
                    source["spans"] = [[a + base, b + base] for a, b in source["spans"]]
                answer = answer + ("\n\n" if answer else "") + normalized
                sources.extend(found)
                notes.extend(part_notes)
            original = "\n\n".join(text for text, _ in turn["parts"])
            models.update(turn["slugs"])
            tools = list(dict.fromkeys(turn["tools"]))
            response = {"answer": answer, "answer_format": "markdown", "status": "SUCCESS",
                        "collected_at": parse_time(turn["created"]),
                        "environment": {"web_search_used": True if turn["searched"] or sources else None,
                                        "tools_used": tools or None},
                        "sources": sources, "collection_quality": notes,
                        "platform_ref": {"conversation_id": cid, "message_ids": [i for i in turn["ids"] if i],
                                         "model_slugs": sorted(turn["slugs"]), "title": conv.get("title")}}
            if original != answer:
                response["original"] = {"format": "chatgpt_export_text", "content": original}
                response["normalization"] = {"rule": "chatgpt-citation-markers-v1"}
            if not answer.strip():
                response.update(status="INCOMPLETE", error="답변에 평가할 글자가 없습니다 (인용 표시·내부 표시만 있음).")
            built.append({"question": turn["question"], "response": response})
        builder.add_conversation(cid, built)
    if not builder.questions:
        raise ImportError_("ChatGPT 내보내기에서 질문과 답변을 찾지 못했습니다.")
    if builder.system.get("model") == "unknown" and models:
        builder.system["model"] = models.pop() if len(models) == 1 else "mixed"
        if builder.system["model"] == "mixed":
            builder.note("대화마다 사용된 모델이 달라 시스템 모델을 'mixed' 로 두고 답변별 model_slugs 에 기록했습니다.")
    if custom_instructions:
        builder.system.setdefault("environment", {})["system_instruction"] = {
            "known": True, "note": "ChatGPT 사용자 맞춤 지침이 대화에 적용되어 있었습니다 (내용은 개인정보 보호를 위해 보관하지 않음)."}
    return builder.bundle()


# --- Claude.ai 내보내기 ---------------------------------------------------------------------

def looks_like_claude(value: Any) -> bool:
    items = value if isinstance(value, list) else [value]
    return bool(items) and all(isinstance(c, dict) and isinstance(c.get("chat_messages"), list) for c in items[:5])


def claude_export(value: Any, options: Dict[str, Any], name: str) -> Dict[str, Any]:
    conversations = value if isinstance(value, list) else [value]
    wanted = set(options.get("conversation_ids") or [])
    builder = BundleBuilder(system_from(options, {
        "system_key": "claude-web", "provider": "anthropic", "platform": "Claude.ai", "model": "unknown", "access_mode": "web",
        "environment": {"web_search_available": None, "tools_available": None, "system_instruction": {"known": False},
                        "notes": ["Claude.ai 내보내기에는 사용 모델·웹검색 설정이 들어 있지 않습니다."]}}),
        adapter="claude_export", format_label="Claude.ai 내보내기", source_name=name, collection_method="official_export")
    for number, conv in enumerate(conversations, 1):
        if not isinstance(conv, dict) or not isinstance(conv.get("chat_messages"), list):
            continue
        cid = str(conv.get("uuid") or f"conversation-{number}")
        if wanted and cid not in wanted:
            continue
        turns: List[Dict[str, Any]] = []
        last_time = None
        for message in conv["chat_messages"]:
            if not isinstance(message, dict):
                continue
            created = parse_time(message.get("created_at"))
            if created and last_time and created < last_time:
                raise ImportError_(f"대화 {cid}: 메시지 시각이 뒤바뀌어 있습니다. 턴 순서를 추측하지 않습니다.")
            last_time = created or last_time
            blocks = message.get("content") if isinstance(message.get("content"), list) else []
            texts = [b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
            text = "".join(texts) if texts else message.get("text") if isinstance(message.get("text"), str) else ""
            if message.get("sender") == "human":
                if message.get("attachments") or message.get("files"):
                    builder.note(f"대화 {cid}: 질문의 첨부 파일은 평가 입력에 넣지 않고 글자만 사용했습니다.")
                if text.strip():
                    turns.append({"question": text, "parts": [], "tools": [], "sources": [], "created": created})
            elif message.get("sender") == "assistant" and turns:
                offset = sum(len(p) for p in turns[-1]["parts"])
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                        turns[-1]["tools"].append(block["name"])
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        for citation in block.get("citations") or []:
                            if isinstance(citation, dict) and isinstance(citation.get("url"), str):
                                turns[-1]["sources"].append({"url": citation["url"], "title": citation.get("title"),
                                                             "spans": [[offset, offset + len(block["text"])]],
                                                             "origin": "export_metadata"})
                        offset += len(block["text"])
                if text:
                    turns[-1]["parts"].append(text)
                turns[-1]["created"] = created or turns[-1]["created"]
        built = []
        for turn in turns:
            if not turn["parts"]:
                built.append({"question": turn["question"], "response": None})
                continue
            tools = list(dict.fromkeys(turn["tools"]))
            built.append({"question": turn["question"], "response": {
                "answer": "".join(turn["parts"]), "status": "SUCCESS", "collected_at": turn["created"],
                "sources": turn["sources"],
                "environment": {"web_search_used": True if any("search" in t for t in tools) else None, "tools_used": tools or None},
                "platform_ref": {"conversation_id": cid, "title": conv.get("name")}}})
        builder.add_conversation(cid, built)
    if not builder.questions:
        raise ImportError_("Claude.ai 내보내기에서 질문과 답변을 찾지 못했습니다.")
    return builder.bundle()


# --- 사용자 정리 대화 (modelgrade-transcript/v1) ----------------------------------------------

def _answer_from_message(message: Dict[str, Any], where: str) -> Dict[str, Any]:
    fmt = message.get("format", "markdown")
    content = message.get("content")
    html = message.get("html")
    if fmt == "html" or isinstance(html, str):
        source = html if isinstance(html, str) else content
        if not isinstance(source, str):
            raise ImportError_(f"{where}: HTML 답변 원문이 문자열이 아닙니다.")
        converted = html_to_markdown(source)
        response = {"answer": converted["markdown"], "answer_format": "html_converted",
                    "original": {"format": "html", "content": source}, "collection_quality": list(converted["quality"]),
                    "normalization": {"rule": converted["converter"], "stats": converted["stats"]}}
        if any(q["severity"] == "error" for q in converted["quality"]):
            response.update(status="INCOMPLETE", error="HTML 을 평가용 Markdown 으로 변환하지 못했습니다. 원본 HTML 은 보존했습니다.")
    else:
        if not isinstance(content, str):
            raise ImportError_(f"{where}: 답변 content 는 문자열이어야 합니다.")
        response = {"answer": content, "answer_format": "text" if fmt == "text" else "markdown"}
    for key in ("status", "error", "sources", "collected_at", "usage", "empty_answer_confirmed"):
        if key in message:
            response[key] = message[key]
    response.setdefault("collected_at", message.get("created_at"))
    env = {}
    if "web_search_used" in message:
        env["web_search_used"] = message["web_search_used"]
    if "tools_used" in message:
        env["tools_used"] = message["tools_used"]
    response["environment"] = env
    return response


def transcript(value: Dict[str, Any], options: Dict[str, Any], name: str) -> Dict[str, Any]:
    system = system_from({"system": {**(value.get("system") or {}), **((options or {}).get("system") or {})}}, {})
    method = value.get("collection_method") or options.get("collection_method") or "user_file_json"
    builder = BundleBuilder(system, adapter="transcript", format_label="ModelGrade 대화 기록", source_name=name,
                            collection_method=method)
    conversations = value.get("conversations")
    if not isinstance(conversations, list) or not conversations:
        raise ImportError_("conversations 배열이 필요합니다.")
    for number, conv in enumerate(conversations, 1):
        if not isinstance(conv, dict) or not isinstance(conv.get("messages"), list):
            raise ImportError_(f"conversations[{number - 1}]: messages 배열이 필요합니다.")
        cid = str(conv.get("id") or f"conversation-{number}")
        missing = conv.get("missing_turns_before", 0)
        if isinstance(missing, bool) or not isinstance(missing, int) or missing < 0:
            raise ImportError_(f"대화 {cid}: missing_turns_before 는 0 이상의 정수여야 합니다.")
        if missing:
            builder.note(f"대화 {cid}: 앞 {missing}턴이 빠진 기록입니다. 빠진 턴은 만들지 않고 평가기가 '앞 대화 누락'으로 다룹니다.")
        turns: List[Dict[str, Any]] = []
        last_time, last_turn = None, missing
        system_text = None
        for index, message in enumerate(conv["messages"]):
            where = f"대화 {cid} {index + 1}번 메시지"
            if not isinstance(message, dict) or message.get("role") not in ("user", "assistant", "system", "developer", "tool"):
                raise ImportError_(f"{where}: role 은 user·assistant·system·developer·tool 중 하나여야 합니다.")
            created = parse_time(message.get("created_at"))
            if created and last_time and created < last_time:
                raise ImportError_(f"{where}: 메시지 시각이 앞 메시지보다 이릅니다. 대화 순서를 추측하지 않습니다.")
            last_time = created or last_time
            role = message["role"]
            if role in ("system", "developer"):
                if isinstance(message.get("content"), str):
                    system_text = message["content"]
                continue   # 시스템 지시는 사용자 질문에 섞지 않는다
            if role == "tool":
                if turns:
                    turns[-1]["tools"].append(str(message.get("name") or "tool"))
                continue
            if role == "user":
                if not isinstance(message.get("content"), str) or not message["content"].strip():
                    raise ImportError_(f"{where}: 사용자 질문이 비어 있습니다.")
                turn = message.get("turn")
                if turn is not None:
                    if isinstance(turn, bool) or not isinstance(turn, int) or turn <= last_turn:
                        raise ImportError_(f"{where}: turn 번호가 앞 질문보다 커야 합니다 (순서 오류).")
                    if turn != last_turn + 1:
                        builder.note(f"대화 {cid}: {last_turn + 1}~{turn - 1}턴이 빠진 기록입니다. 빠진 턴은 만들지 않고 "
                                     "평가기가 '앞 대화 누락'으로 다룹니다.")
                    last_turn = turn
                else:
                    last_turn += 1
                turns.append({"question": message["content"], "responses": [], "tools": [], "turn": last_turn})
                continue
            if not turns:
                raise ImportError_(f"{where}: 첫 사용자 질문보다 AI 답변이 먼저 있습니다 (순서 오류).")
            turns[-1]["responses"].append(_answer_from_message(message, where))
        built = []
        for turn in turns:
            if not turn["responses"]:
                built.append({"question": turn["question"], "response": None, "turn": turn["turn"]})
                continue
            response = turn["responses"][0]
            if len(turn["responses"]) > 1:
                raise ImportError_(f"대화 {cid} {turn['turn']}턴: 한 질문에 AI 답변이 여러 개입니다. 화면에 남은 답변 하나만 넣어 주세요.")
            if turn["tools"]:
                env = response.setdefault("environment", {})
                env["tools_used"] = list(dict.fromkeys((env.get("tools_used") or []) + turn["tools"]))
            built.append({"question": turn["question"], "response": response, "turn": turn["turn"]})
        builder.add_conversation(cid, built, start_turn=missing + 1, reference=conv.get("reference"))
        if system_text is not None:
            builder.system.setdefault("environment", {})["system_instruction"] = {"known": True, "text": system_text}
    return builder.bundle()


# --- HTML ---------------------------------------------------------------------------------

def _role_blocks(root) -> List[Tuple[str, Any]]:
    out = []

    def walk(node):
        for child in node.children:
            if isinstance(child, str):
                continue
            role = child.attrs.get("data-message-author-role")
            if role in ("user", "assistant"):
                out.append((role, child))
            else:
                walk(child)
    walk(root)
    return out


def _inner_html(node) -> str:
    from html import escape

    def render(n):
        if isinstance(n, str):
            return escape(n, quote=False)
        attrs = "".join(f' {k}="{escape(v)}"' for k, v in n.attrs.items())
        inner = "".join(render(c) for c in n.children)
        return f"<{n.tag}{attrs}>{inner}</{n.tag}>"
    return "".join(render(c) for c in node.children)


def html_input(text: str, options: Dict[str, Any], name: str) -> Dict[str, Any]:
    embedded = re.search(r"jsonData\s*=\s*(\[.*?\]);\s*(?:\n|</script>)", text, re.S)
    if embedded:
        try:
            value = json.loads(embedded.group(1))
        except json.JSONDecodeError:
            value = None
        if looks_like_chatgpt(value):
            bundle = chatgpt_export(value, options, name)
            bundle["import"].update(format="ChatGPT 내보내기 chat.html", adapter="chatgpt_export_html")
            return bundle
    system = system_from(options, {})
    root, _, _ = parse_html(text)
    blocks = _role_blocks(root)
    builder = BundleBuilder(system, adapter="html", format_label="HTML 파일", source_name=name,
                            collection_method="user_file_html")
    if blocks:
        turns: List[Dict[str, Any]] = []
        for role, node in blocks:
            if role == "user":
                question = html_to_markdown(_inner_html(node))["markdown"].strip()
                if question:
                    turns.append({"question": question, "response": None})
            elif turns:
                body = next((n for n in _walk_nodes(node) if "markdown" in n.attrs.get("class", "").split()), node)
                message = {"role": "assistant", "format": "html", "content": _inner_html(body)}
                if turns[-1]["response"] is not None:
                    raise ImportError_("한 질문에 AI 답변이 여러 개 있는 HTML 입니다. 화면에 남은 답변 하나만 저장해 주세요.")
                turns[-1]["response"] = _answer_from_message(message, "HTML 답변")
            else:
                builder.note("첫 질문 앞의 AI 메시지는 제외했습니다.")
        if not turns:
            raise ImportError_("HTML 에서 사용자 질문을 찾지 못했습니다.")
        builder.add_conversation(options.get("conversation_id") or safe_id(name), turns)
        builder.note("HTML 의 메시지 역할 표시(data-message-author-role)로 질문과 답변을 나눴습니다.")
        return builder.bundle()
    question = options.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ImportError_("이 HTML 에서 질문과 답변의 경계를 찾지 못했습니다. 추측하지 않으니 질문 원문을 함께 입력해 주세요.")
    response = _answer_from_message({"role": "assistant", "format": "html", "content": text}, "HTML 답변")
    builder.add_conversation(options.get("case_id") or safe_id(name), [{"question": question, "response": response}])
    return builder.bundle()


def _walk_nodes(node):
    for child in node.children:
        if not isinstance(child, str):
            yield child
            yield from _walk_nodes(child)


# --- 직접 복사 입력 -------------------------------------------------------------------------

def manual(value: Dict[str, Any], options: Dict[str, Any]) -> Dict[str, Any]:
    """화면에서 질문·답변을 복사해 넣은 한 쌍. 답변은 Markdown·텍스트·HTML 중 하나로 받는다."""
    question, answer = value.get("question"), value.get("answer")
    if not isinstance(question, str) or not question.strip():
        raise ImportError_("질문 원문을 입력해 주세요.")
    message = {"role": "assistant", "content": answer, "format": value.get("answer_format", "markdown")}
    for key in ("sources", "collected_at", "web_search_used", "tools_used"):
        if key in value:
            message[key] = value[key]
    if value.get("answer_format") == "html":
        message["html"] = answer
    data = {"schema": TRANSCRIPT_SCHEMA, "system": value.get("system") or {}, "collection_method": "manual_copy",
            "conversations": [{"id": value.get("case_id") or "manual",
                               "messages": [{"role": "user", "content": question}, message]}]}
    bundle = transcript(data, options, "직접 입력")
    bundle["import"].update(adapter="manual", format="직접 복사 입력")
    return bundle
