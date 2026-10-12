"""2. 요구 조건 준수 — 2-1 분량·표현 조건 / 2-2 형식·구성 조건.

표 고정 최소 행 수(원문 MD 164행 ``표 행이 2개 미만``)는 사용자 결정으로 폐기했다. 이 모듈은
그 조건을 만들지 않는다. 헤더·데이터·전체 행 수는 측정만 하고, 질문이 명시한 행 수만 비교한다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

from .checks_integrity import json_targets
from .core import Ctx, cmp_text, compare, excerpt, nfc, q_evidence, result, skip_condition, skip_req, verdict_of
from .model import Evidence, FAIL, PASS, Status, stable_hash
from .structure_counts import heading_series
from .textparse import _json_type_name, count_words, strip_non_content

__all__ = ["check_2_1", "check_2_2", "table_observation"]

SUB21, SUB22 = "2-1", "2-2"

_COUNT_TITLES = {"bullet_count": "불릿 개수", "sentence_count": "문장 수", "paragraph_count": "문단 수",
                 "line_count": "줄 수", "word_count": "단어(어절) 수", "list_count": "항목 개수"}
_COUNT_RULES = {
    "bullet_count": "코드 밖 최상위 불릿(-·*·+) 줄 수, 중첩 불릿 제외",
    "sentence_count": "코드 밖 본문, 인라인 코드 가림, 문장 분리(마침표 뒤 공백·줄끝, 등록 약어 예외, 소수점·URL 내부 마침표 제외)",
    "paragraph_count": "빈 줄로 구분한 코드 밖 문단",
    "line_count": "코드 밖 비어 있지 않은 줄",
    "word_count": "코드 밖 공백 구분 어절",
    "list_count": "연속 번호 제목·목록의 시작 위치와 요구 대상을 연결하고, 모호한 구조는 판정하지 않음",
}


def _measure_count(ctx: Ctx, kind: str) -> Dict[str, Any]:
    aa = ctx.aa
    if kind == "bullet_count":
        items = aa.top_level_bullets
        return {"value": len(items), "items": [i.line.number for i in items],
                "nested_bullets": len(aa.bullet_items) - len(items),
                "evidence": [(i.start, i.end) for i in items]}
    if kind == "sentence_count":
        return {"value": len(aa.sentences), "sentences": aa.sentences[:50]}
    if kind == "paragraph_count":
        return {"value": len(aa.paragraphs), "evidence": [(p.start, p.end) for p in aa.paragraphs]}
    if kind == "line_count":
        return {"value": len(aa.content_nonempty_lines)}
    if kind == "word_count":
        return {"value": count_words(ctx.answer, aa.lines)}
    if kind == "list_count":
        return _list_items_measure(ctx, numbered_only=False)
    raise KeyError(kind)


_NUMBERED_HEADING = re.compile(r"^(\d{1,3})[.)]\s")


def _sequences(markers_items):
    """번호가 1로 다시 시작하거나 줄어들면 다른 목록으로 나눈다."""
    seqs: List[list] = []
    prev = None
    for marker, item in markers_items:
        if prev is None or marker <= prev:
            seqs.append([])
        seqs[-1].append(item)
        prev = marker
    return seqs


def _bullet_blocks(ctx: Ctx, items) -> List[list]:
    blocks: List[list] = []
    if not items:
        return blocks
    indent = items[0].indent
    item_lines = {i.line.number: i for i in items}
    current: list = []
    for line in ctx.aa.lines:
        if line.number in item_lines:
            current.append(item_lines[line.number])
        elif current and line.text.strip() and not line.in_code and \
                len(line.text.expandtabs(4)) - len(line.text.expandtabs(4).lstrip()) <= indent:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _requested_item_noun(question: str, start: int) -> Optional[str]:
    match = re.search(r"([가-힣]{2,})(?:을|를)\s*$", question[max(0, start - 40):start])
    return match.group(1) if match else None


def _list_items_measure(ctx: Ctx, numbered_only: bool, target_noun: Optional[str] = None) -> Dict[str, Any]:
    """요구 항목 수를 셀 구조 하나를 정한다. 후보가 여럿이면 합산하지 않고 None."""
    aa = ctx.aa
    numbered = [i for i in aa.top_level_items if i.marker not in ("-", "*", "+")]
    if not numbered and aa.numbered_items:
        numbered = [i for i in aa.numbered_items if i.indent == min(x.indent for x in aa.numbered_items)]
    seqs = _sequences([(int(re.match(r"\d+", i.marker).group(0)), i) for i in numbered])
    hseqs = heading_series(aa.headings)
    if any(run["ambiguous_boundary"] for run in hseqs):
        return {"value": None, "structure": "numbered_headings",
                "why": "같은 번호 제목 패턴이 중단된 뒤 다시 나타나 항목 경계를 확정하지 못함"}
    if len(hseqs) == 1 and (not seqs or (not numbered_only and hseqs[0]["count"] >= 2 and
                                            hseqs[0]["start"] < min(items[0].start for items in seqs))):
        run = hseqs[0]
        return {"value": run["count"], "structure": "numbered_headings", "start": run["start"],
                "end": run["end"], "excluded_next_heading": run["excluded_next_heading"],
                "evidence": [(h.start, h.end) for h in run["items"]]}
    if seqs and hseqs:
        return {"value": None, "structure": f"numbered_lists×{len(seqs)}+numbered_headings×{len(hseqs)}",
                "why": "번호 목록과 번호 제목이 함께 있어 요구 대상 구조를 확정하지 못함"}
    if len(seqs) == 1:
        items = seqs[0]
        return {"value": len(items), "structure": "numbered_list", "evidence": [(i.start, i.end) for i in items]}
    if len(seqs) > 1:
        return {"value": None, "structure": f"numbered_lists×{len(seqs)}",
                "why": f"번호 목록이 {len(seqs)}개로 나뉘어 있어 요구 대상 목록을 확정하지 못함"}
    if len(hseqs) > 1:
        return {"value": None, "structure": f"numbered_headings×{len(hseqs)}",
                "why": f"번호 제목 묶음이 {len(hseqs)}개라 요구 대상을 확정하지 못함"}
    if numbered_only:
        return {"value": None, "structure": None, "why": "번호를 붙인 항목 구조를 찾지 못함"}
    blocks = _bullet_blocks(ctx, aa.top_level_bullets)
    if len(blocks) == 1:
        items = blocks[0]
        before = [h for h in aa.headings if h.start < items[0].start]
        if target_noun and before and target_noun not in before[-1].title:
            return {"value": None, "structure": "bullet_list",
                    "why": f"불릿이 ‘{before[-1].title}’ 아래에 있어 요구 대상 ‘{target_noun}’과의 연결이 불명확함"}
        return {"value": len(items), "structure": "bullet_list", "start": items[0].start,
                "end": items[-1].end, "evidence": [(i.start, i.end) for i in items]}
    if len(blocks) > 1:
        return {"value": None, "structure": f"bullet_lists×{len(blocks)}",
                "why": f"불릿 목록이 {len(blocks)}개로 나뉘어 있어 요구 대상 목록을 확정하지 못함"}
    if len(aa.tables) == 1:
        t = aa.tables[0]
        return {"value": t.data_rows, "structure": "table_data_rows", "table": t.to_dict()}
    return {"value": None, "structure": None, "why": "답변에서 셀 항목 구조(목록·단일 표)를 확인하지 못함"}


def _literal_hits(scan: str, literal: str) -> List[int]:
    hits, start = [], scan.find(literal)
    while start >= 0:
        hits.append(start)
        start = scan.find(literal, start + 1)
    return hits


def _fold_whitespace(scan: str, offsets: List[int]) -> Tuple[str, List[int]]:
    """연속 공백(str.isspace: 공백·탭·줄바꿈·NBSP·전각 공백)을 공백 하나로 접는다.

    접힌 공백의 원문 위치는 구간 첫 글자로 둔다. 제로폭 문자(Cf)는 공백이 아니므로 접지 않는다.
    """
    out, out_off, i, n = [], [], 0, len(scan)
    while i < n:
        if scan[i].isspace():
            j = i
            while j < n and scan[j].isspace():
                j += 1
            out.append(" ")
            out_off.append(offsets[i])
            i = j
        else:
            out.append(scan[i])
            out_off.append(offsets[i])
            i += 1
    return "".join(out), out_off


def _fold_literal(literal: str) -> str:
    return " ".join(literal.split())


def forbidden_key(literal: str, scan: str) -> str:
    """같은 대상·조건·범위·정규화이면 같은 키 — 2-1 과 AIONA 금지 표현의 중복 집계 방지."""
    return stable_hash({"cond": "forbidden_literal", "literal": nfc(literal), "normalization": "NFC",
                        "scope": "code_excluded_emphasis_removed", "target": stable_hash(scan)})


_HANGUL = re.compile(r"[ᄀ-ᇿ㄰-㆏ꥠ-꥿가-힣ힰ-퟿]")


def _charset_violations(scan: str, charset: str) -> List[int]:
    bad = []
    for i, ch in enumerate(scan):
        if not unicodedata.category(ch).startswith("L"):
            continue
        if charset == "한글":
            ok = bool(_HANGUL.match(ch))
        elif charset in ("영문", "영어알파벳", "알파벳"):
            ok = ch.isascii()
        elif charset == "대문자":
            ok = ch.isascii() and ch.isupper()
        elif charset == "소문자":
            ok = ch.isascii() and ch.islower()
        else:
            return []
        if not ok:
            bad.append(i)
    return bad


def check_2_1(ctx: Ctx) -> List:
    out = []
    conds = ctx.conds
    scan, offsets = ctx.projection

    def origin(i: int) -> Optional[int]:
        return offsets[i] if 0 <= i < len(offsets) else None

    req = conds.first("explicit_empty")
    if req:
        content = strip_non_content(ctx.answer)
        ok = not content
        out.append(result("2-1.explicit_empty", SUB21, "빈 출력 요청", "question", Status.EXECUTED, verdict_of(ok),
                          required="출력 없음", measured={"content_chars": len(content)},
                          comparison=cmp_text(len(content), "==", 0),
                          reason="요청대로 출력이 비어 있음" if ok else f"빈 출력을 요청했으나 실질 문자 {len(content)}자 출력",
                          evidence=[q_evidence(ctx, req)]))

    for req in conds.of(*_COUNT_TITLES):
        title = _COUNT_TITLES[req.kind]
        cid = f"2-1.count:{req.kind}"
        if req.status != "ok":
            skip_req(ctx, req, cid, SUB21)
            continue
        m = (_list_items_measure(ctx, bool(req.value.get("numbered")),
                                 _requested_item_noun(ctx.question, req.start)) if req.kind == "list_count"
             else _measure_count(ctx, req.kind))
        op, n = req.value["op"], req.value["n"]
        if m["value"] is None:
            skip_condition(ctx, cid, SUB21, m.get("why", "측정 구조 없음") + " — 0개로 간주하지 않음")
            continue
        ok = compare(m["value"], op, n)
        ev = [q_evidence(ctx, req)] + [Evidence("answer", s, e, ctx.answer[s:e][:60]) for s, e in m.get("evidence", [])[:30]]
        out.append(result(cid, SUB21, title, "question", Status.EXECUTED, verdict_of(ok),
                          required={"op": op, "n": n}, measured={k: v for k, v in m.items() if k != "evidence"},
                          comparison=cmp_text(m["value"], op, n), scope=_COUNT_RULES[req.kind],
                          reason=(f"{title} {m['value']} — 요구({op} {n}) 충족" if ok else
                                  f"{title} 요구 {op} {n}, 실제 {m['value']}"),
                          evidence=ev))

    for req in conds.of("char_count"):
        if req.status != "ok":
            skip_req(ctx, req, "2-1.char_count", SUB21)
            continue
        body = ctx.aa.body_without_code
        if req.value["exclude_whitespace"]:
            body = "".join(ch for ch in body if not ch.isspace())
        value, op, n = len(body), req.value["op"], req.value["n"]
        ok = compare(value, op, n)
        out.append(result("2-1.char_count", SUB21, "글자 수", "question", Status.EXECUTED, verdict_of(ok),
                          required={"op": op, "n": n, "exclude_whitespace": req.value["exclude_whitespace"]},
                          measured={"chars": value, "unit": "Unicode 코드 포인트"},
                          comparison=cmp_text(value, op, n),
                          scope="코드 펜스 밖 본문, 줄바꿈 제외" + (", 공백 제외" if req.value["exclude_whitespace"] else ""),
                          reason=f"글자 수 {value} — 요구({op} {n}) " + ("충족" if ok else "위반"),
                          evidence=[q_evidence(ctx, req)]))

    for req in conds.of("required_literal", "forbidden_literal", "starts_with", "ends_with"):
        if req.status != "ok":
            skip_req(ctx, req, f"2-1.literal:{req.kind}", SUB21)
            continue
        lit = nfc(req.value["literal"])
        titles = {"required_literal": "필수 문자열", "forbidden_literal": "금지 문자열",
                  "starts_with": "시작 표현", "ends_with": "끝 표현"}
        title = f"{titles[req.kind]} ‘{lit}’"
        # 정확 일치 요구가 아니면 연속 공백·줄바꿈을 공백 하나로 보고 대조한다 (G-1).
        exact = bool(req.value.get("exact"))
        if exact:
            text, offs, needle = scan, offsets, lit
        else:
            text, offs = ctx.shared.setdefault("folded_projection", _fold_whitespace(scan, offsets))
            needle = _fold_literal(lit)
        hits = _literal_hits(text, needle)
        stripped = text.strip()
        dedupe_key = None
        if req.kind == "required_literal":
            ok, measured, comp = bool(hits), {"occurrences": len(hits)}, cmp_text(len(hits), ">=", 1)
        elif req.kind == "forbidden_literal":
            ok, measured, comp = not hits, {"occurrences": len(hits)}, cmp_text(len(hits), "==", 0)
            dedupe_key = forbidden_key(lit, scan)  # AIONA 금지 표현과 같은 키를 유지하려고 접기 전 투영으로 계산
        elif req.kind == "starts_with":
            ok, measured, comp = stripped.startswith(needle), {"answer_start": stripped[:len(needle) + 10]}, "startswith"
        else:
            ok, measured, comp = stripped.endswith(needle), {"answer_end": stripped[-(len(needle) + 10):]}, "endswith"
        measured["whitespace"] = "exact" if exact else "folded"
        if not exact and req.kind in ("required_literal", "forbidden_literal"):
            # 접어서만 찾은 출현 수 — 원문 그대로는 없고 공백·줄바꿈 차이만 있던 곳
            normalized = len(hits) - len(_literal_hits(scan, lit))
            if normalized > 0:
                measured["whitespace_normalized_occurrences"] = normalized
        ev = [q_evidence(ctx, req)]
        for h in hits[:10]:
            start, end = offs[h], offs[h + len(needle) - 1] + 1
            ev.append(Evidence("answer", start, end, excerpt(ctx.answer, start, end), "검출"))
        out.append(result(f"2-1.literal:{req.kind}", SUB21, title, "question", Status.EXECUTED, verdict_of(ok),
                          required={"literal": lit, "exact": exact}, measured=measured, comparison=comp,
                          scope="코드 구획·인라인 코드 제외, 강조 마커 제거, NFC" + ("" if exact else ", 공백·줄바꿈 접기"),
                          dedupe_key=dedupe_key,
                          reason=(f"{titles[req.kind]} 조건 충족" if ok else
                                  {"required_literal": f"‘{lit}’이(가) 답변에 없음",
                                   "forbidden_literal": f"금지한 ‘{lit}’이(가) {len(hits)}회 나옴",
                                   "starts_with": f"답변이 ‘{lit}’(으)로 시작하지 않음",
                                   "ends_with": f"답변이 ‘{lit}’(으)로 끝나지 않음"}[req.kind]),
                          evidence=ev))

    req = conds.first("charset")
    if req:
        bad = _charset_violations(scan, req.value["charset"])
        ok = not bad
        out.append(result("2-1.charset", SUB21, f"허용 문자 ({req.value['charset']}만)", "question", Status.EXECUTED,
                          verdict_of(ok), required={"charset": req.value["charset"]},
                          measured={"violating_letters": len(bad), "examples": sorted({scan[i] for i in bad})[:20]},
                          comparison=cmp_text(len(bad), "==", 0), scope="문자 범주 L* 만 대상, 숫자·부호·공백 제외",
                          reason="허용 문자만 사용" if ok else f"허용 범위 밖 문자 {len(bad)}자",
                          evidence=[q_evidence(ctx, req)] + [Evidence("answer", origin(i), (origin(i) or 0) + 1, scan[i])
                                                             for i in bad[:10]]))
    return out


# =====================================================================================
# 2-2 형식·구성 조건
# =====================================================================================

def table_observation(ctx: Ctx) -> Dict[str, Any]:
    """표 행 수 관측값. PASS/FAIL 이 아니다 (고정 최소 행 수 규칙 폐기)."""
    return {
        "type": "observation",
        "observation_id": "table_rows",
        "tables": [t.to_dict() for t in ctx.aa.tables],
        "pipe_lines_without_table_syntax": len(ctx.aa.pipe_lines_without_table),
        "note": "header_rows·data_rows·total_rows 는 측정값이며 판정이 아님. 질문이 명시한 행 수만 2-2에서 비교함. "
                "Markdown 구분선은 행 수에 넣지 않음.",
        "counted_in_pass_rate": False,
    }


def _norm_cell(s: str) -> str:
    return re.sub(r"\s+", " ", nfc(s)).strip().strip("*`").strip()


def _table_checks(ctx: Ctx) -> List:
    out = []
    conds, tables = ctx.conds, ctx.aa.tables
    tables_measured = [t.to_dict() for t in tables]
    forbid = conds.first("table_forbidden")
    if forbid and forbid.status != "ok":
        skip_req(ctx, forbid, "2-2.table:forbidden", SUB22)
        return out
    if forbid:
        ok = not tables
        out.append(result("2-2.table:forbidden", SUB22, "표 사용 금지", "question", Status.EXECUTED, verdict_of(ok),
                          required="표 0개", measured={"tables": tables_measured},
                          comparison=cmp_text(len(tables), "==", 0),
                          reason="표 없음" if ok else f"금지한 표 {len(tables)}개 사용",
                          evidence=[q_evidence(ctx, forbid)]))
        return out
    req = conds.first("table_required")
    if req is not None and req.status != "ok":
        skip_req(ctx, req, "2-2.table:required", SUB22)
        req = None
    detail_reqs = conds.of("table_count", "table_columns", "table_column_count", "table_header", "table_data_rows",
                           "table_total_rows", "table_rows_ambiguous")
    if req:
        ok = bool(tables)
        pipes = len(ctx.aa.pipe_lines_without_table)
        out.append(result("2-2.table:required", SUB22, "표 존재", "question", Status.EXECUTED, verdict_of(ok),
                          required="표 구조 1개 이상 (GFM 표 또는 HTML 표)", measured={"tables": tables_measured,
                                                                             "pipe_lines_without_table_syntax": pipes},
                          comparison=cmp_text(len(tables), ">=", 1),
                          reason=("표 구조 확인" if ok else
                                  "표를 요구했으나 표 구조가 없음" + (f" (표 문법이 아닌 파이프 줄 {pipes}개는 표로 인정하지 않음)" if pipes else "")),
                          evidence=[q_evidence(ctx, req)]))
    if not detail_reqs:
        return out
    for r in [item for item in detail_reqs if item.kind == "table_count"]:
        if r.status != "ok":
            skip_req(ctx, r, "2-2.table:count", SUB22)
        elif not tables and req:
            skip_condition(ctx, "2-2.table:count", SUB22, "표 존재 조건이 FAIL 이라 개수 행을 만들지 않음")
        else:
            op, n = r.value["op"], r.value["n"]
            ok = compare(len(tables), op, n)
            out.append(result("2-2.table:count", SUB22, "표 개수", "question", Status.EXECUTED,
                              verdict_of(ok), required=r.value, measured={"tables": len(tables)},
                              comparison=cmp_text(len(tables), op, n),
                              reason=f"질문 요구 표 {op} {n}개, 실제 {len(tables)}개",
                              evidence=[q_evidence(ctx, r)]))
    detail_reqs = [r for r in detail_reqs if r.kind != "table_count"]
    if not detail_reqs:
        return out
    if not tables:
        # 표가 없으면 열·헤더·행 수 행을 만들지 않는다 (표 존재 FAIL 한 건만 남김).
        for r in detail_reqs:
            if r.status != "ok":
                skip_req(ctx, r, f"2-2.table:{r.kind}", SUB22)
            else:
                skip_condition(ctx, f"2-2.table:{r.kind}", SUB22,
                               "표가 없어 열·헤더·행 수를 측정할 수 없음 (표 존재 결과 참조)")
        return out
    if len(tables) > 1:
        for r in detail_reqs:
            skip_condition(ctx, f"2-2.table:{r.kind}", SUB22,
                           f"표가 {len(tables)}개라 요구가 가리키는 표를 확정할 수 없음 — 행 수를 합산하지 않음")
        return out
    t = tables[0]
    # 마크다운은 헤더 줄 없이 표를 쓸 수 없다. 헤더 없이 요청했는데 한 줄뿐이면 그 줄을 내용 행으로 본다.
    as_content = (t.kind == "markdown" and t.header_rows == 1 and not t.data_rows and
                  any(r.kind == "table_header" and r.status == "ok" and "header_rows" in r.value for r in detail_reqs))
    if as_content:
        t = replace(t, header_rows=0, data_rows=1, headers=(), rows=(t.headers,))
    note = " (한 줄뿐인 마크다운 표는 그 줄을 내용 행으로 봄)" if as_content else ""
    ev_t = Evidence("answer", t.start if t.start >= 0 else None, t.end if t.end >= 0 else None,
                    (ctx.answer[t.start:t.end][:120] if t.start >= 0 else "HTML 표"), f"{t.kind} 표")
    for r in detail_reqs:
        if r.status != "ok":
            skip_req(ctx, r, f"2-2.table:{r.kind}", SUB22)
            continue
        if r.kind == "table_columns":
            want = [_norm_cell(x) for x in r.value["names"]]
            have = [_norm_cell(x) for x in t.headers]
            if not have:
                out.append(result("2-2.table:columns", SUB22, "표 열 이름", "question", Status.EXECUTED, FAIL,
                                  required={"columns": want, "ordered": r.value["ordered"]},
                                  measured={"headers": [], "header_rows": t.header_rows},
                                  comparison="헤더 행 없음", reason="열 이름을 요구했으나 헤더 행이 없음",
                                  evidence=[q_evidence(ctx, r), ev_t]))
                continue
            if r.value["ordered"]:
                ok = have == want
                comp = f"{have} == {want}"
            else:
                ok = all(w in have for w in want)
                comp = f"{want} ⊆ {have}"
            out.append(result("2-2.table:columns", SUB22, "표 열 이름" + ("·순서" if r.value["ordered"] else ""),
                              "question", Status.EXECUTED, verdict_of(ok),
                              required={"columns": want, "ordered": r.value["ordered"]}, measured={"headers": have},
                              comparison=comp, reason="열 구성이 요구와 일치" if ok else f"요구 열 {want}, 실제 헤더 {have}",
                              evidence=[q_evidence(ctx, r), ev_t]))
        elif r.kind == "table_column_count":
            cols = len(t.headers) if t.headers else (len(t.rows[0]) if t.rows else 0)
            ok = compare(cols, r.value["op"], r.value["n"])
            out.append(result("2-2.table:column_count", SUB22, "표 열 수", "question", Status.EXECUTED, verdict_of(ok),
                              required=r.value, measured={"columns": cols}, comparison=cmp_text(cols, r.value["op"], r.value["n"]),
                              reason=f"열 {cols}개", evidence=[q_evidence(ctx, r), ev_t]))
        elif r.kind == "table_header":
            if "header_rows" in r.value:
                ok, comp = t.header_rows == 0, cmp_text(t.header_rows, "==", 0)
                reason = "요청대로 헤더 없음" if ok else f"헤더 없이 요청했으나 헤더 {t.header_rows}행"
            else:
                ok, comp = t.header_rows >= 1, cmp_text(t.header_rows, ">=", 1)
                reason = "헤더 있음" if ok else "헤더를 요구했으나 헤더 행 없음"
            out.append(result("2-2.table:header", SUB22, "표 헤더", "question", Status.EXECUTED, verdict_of(ok),
                              required=r.value, measured={"header_rows": t.header_rows}, comparison=comp,
                              reason=reason + note, evidence=[q_evidence(ctx, r), ev_t]))
        elif r.kind in ("table_data_rows", "table_total_rows"):
            measured = t.data_rows if r.kind == "table_data_rows" else t.total_rows
            label = "데이터 행 수 (헤더 제외)" if r.kind == "table_data_rows" else "전체 행 수 (헤더 포함)"
            op, n = r.value["op"], r.value["n"]
            ok = compare(measured, op, n)
            out.append(result(f"2-2.table:{r.kind}", SUB22, f"표 {label}", "question", Status.EXECUTED, verdict_of(ok),
                              required={"op": op, "n": n, "basis": r.kind.replace("table_", "")},
                              measured={"header_rows": t.header_rows, "data_rows": t.data_rows, "total_rows": t.total_rows},
                              comparison=cmp_text(measured, op, n),
                              reason=(f"{label} {measured} — 질문 요구({op} {n}) 충족" if ok else
                                      f"질문 요구 {label} {op} {n}, 실제 {measured}") + note,
                              evidence=[q_evidence(ctx, r), ev_t]))
    return out


def _list_heading_checks(ctx: Ctx) -> List:
    out = []
    conds, aa = ctx.conds, ctx.aa
    forbid = conds.first("list_forbidden")
    if forbid and forbid.status != "ok":
        skip_req(ctx, forbid, "2-2.list:forbidden", SUB22)
    elif forbid:
        ok = not aa.list_items
        out.append(result("2-2.list:forbidden", SUB22, "목록 사용 금지", "question", Status.EXECUTED, verdict_of(ok),
                          required="목록 줄 0개", measured={"list_items": len(aa.list_items)},
                          comparison=cmp_text(len(aa.list_items), "==", 0),
                          reason="목록 없음" if ok else f"금지한 목록 줄 {len(aa.list_items)}개",
                          evidence=[q_evidence(ctx, forbid)]))
    req = conds.first("list_required")
    # 불릿 개수 요구는 2-1 이 형식과 개수를 함께 본다 — 같은 요구를 2-2 에 다시 만들지 않는다.
    if req and req.status != "ok":
        skip_req(ctx, req, "2-2.list:required", SUB22)
    elif req and not (req.value["style"] == "bullet" and conds.first("bullet_count")):
        style = req.value["style"]
        numbered_heads = [h for h in aa.headings if _NUMBERED_HEADING.match(h.title)]
        items = {"bullet": aa.bullet_items, "numbered": aa.numbered_items or numbered_heads}.get(style, aa.list_items)
        ok = bool(items)
        out.append(result("2-2.list:required", SUB22, {"bullet": "불릿 목록", "numbered": "번호 목록"}.get(style, "목록") + " 형식",
                          "question", Status.EXECUTED, verdict_of(ok), required={"style": style},
                          measured={"bullet_items": len(aa.bullet_items), "numbered_items": len(aa.numbered_items),
                                    "numbered_headings": len(numbered_heads)},
                          comparison=cmp_text(len(items), ">=", 1),
                          reason="요구한 목록 형식 사용" if ok else "요구한 목록 형식이 없음",
                          evidence=[q_evidence(ctx, req)]))
    req = conds.first("heading_count") or conds.first("heading_required")
    if req and req.status != "ok":
        skip_req(ctx, req, "2-2.heading", SUB22)
    elif req:
        headings = aa.headings
        levels = sorted({h.level for h in headings})
        if not headings and aa.bold_only_lines:
            skip_condition(ctx, "2-2.heading", SUB22,
                           f"굵은 글씨 줄 {len(aa.bold_only_lines)}개는 지원하는 제목 문법(ATX #)이 아니라 판정하지 않음")
        elif req.kind == "heading_count" and len(levels) > 1:
            skip_condition(ctx, "2-2.heading", SUB22,
                           "제목 단계가 여러 개라 요구가 가리키는 단계를 확정하지 못함 — 전체 제목 수로 대체하지 않음")
        elif req.kind == "heading_count":
            op, n = req.value["op"], req.value["n"]
            ok = compare(len(headings), op, n)
            out.append(result("2-2.heading", SUB22, "제목 수", "question", Status.EXECUTED, verdict_of(ok),
                              required=req.value, measured={"headings": [h.title for h in headings]},
                              comparison=cmp_text(len(headings), op, n), reason=f"제목 {len(headings)}개",
                              evidence=[q_evidence(ctx, req)]))
        else:
            ok = bool(headings)
            out.append(result("2-2.heading", SUB22, "제목 존재", "question", Status.EXECUTED, verdict_of(ok),
                              required="제목 1개 이상", measured={"headings": [h.title for h in headings]},
                              comparison=cmp_text(len(headings), ">=", 1),
                              reason="제목 있음" if ok else "제목을 요구했으나 제목(ATX #)이 없음",
                              evidence=[q_evidence(ctx, req)]))
    return out


_JSON_TYPE_CHECK = {
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def _json_type(v: Any) -> str:
    return _json_type_name(v)


def _json_structure(ctx: Ctx) -> List:
    out = []
    conds = ctx.conds
    req = conds.first("json_required")
    if not req:
        return out
    detail_reqs = conds.of("json_required_keys", "json_field_type")
    targets = json_targets(ctx)
    parsed = ctx.shared.get("json_parsed", {})
    ok_targets = [t for t in targets if parsed.get(t["id"], {}).get("status") == "ok"]
    blocked = [t for t in targets if parsed.get(t["id"], {}).get("status") != "ok"]

    if not targets:
        out.append(result("2-2.json_structure:present", SUB22, "JSON 출력", "question", Status.EXECUTED, FAIL,
                          required="JSON 출력", measured={"json_targets": 0}, comparison="JSON 대상 0개",
                          reason="JSON 형식을 요구했으나 JSON 출력(코드블록·JSON 본문)이 없음",
                          evidence=[q_evidence(ctx, req)]))
        # JSON 출력 FAIL 에 딸린 필드 행은 만들지 않는다.
        for r in detail_reqs:
            skip_condition(ctx, f"2-2.json_structure:{r.kind}", SUB22, "JSON 출력이 없어 필드를 측정할 수 없음 (JSON 출력 결과 참조)")
        return out
    if not ok_targets:
        # 구문 FAIL 은 1-2 에서 이미 셌고, 예외·한도·지원 밖 대상은 판정 불가 — 행 없음.
        skip_condition(ctx, "2-2.json_structure:present", SUB22,
                       "JSON 대상이 1-2 구문 검사를 통과하지 못해 구조를 읽을 수 없음 — 필드 누락으로 추정하지 않음")
        for r in detail_reqs:
            skip_condition(ctx, f"2-2.json_structure:{r.kind}", SUB22, "JSON 구문 실패로 필드를 읽지 못함 — 누락으로 추정하지 않음")
        return out
    if len(ok_targets) > 1:
        for r in [req] + detail_reqs:
            skip_condition(ctx, f"2-2.json_structure:{r.kind}", SUB22,
                           f"유효한 JSON 대상이 {len(ok_targets)}개라 요구 대상을 확정하지 못함")
        return out
    value = parsed[ok_targets[0]["id"]]["value"]
    top = req.value.get("top_level")
    if top:
        ok = _json_type(value) == top
        out.append(result("2-2.json_structure:top_level", SUB22, "JSON 최상위 자료형", "question", Status.EXECUTED,
                          verdict_of(ok), required={"top_level": top}, measured={"top_level": _json_type(value)},
                          comparison=f"{_json_type(value)} == {top}",
                          reason="최상위 자료형 일치" if ok else f"최상위 {top} 요구, 실제 {_json_type(value)}",
                          evidence=[q_evidence(ctx, req)]))
    elif not detail_reqs:
        out.append(result("2-2.json_structure:present", SUB22, "JSON 출력", "question", Status.EXECUTED, PASS,
                          required="유효한 JSON 출력", measured={"json_target": ok_targets[0]["id"],
                                                             "top_level": _json_type(value)},
                          comparison="JSON 대상 1개", reason="요구한 JSON 출력 있음", evidence=[q_evidence(ctx, req)]))
    objects = [value] if isinstance(value, dict) else (
        [v for v in value] if isinstance(value, list) and value and all(isinstance(v, dict) for v in value) else None)
    required_keys = {k for r in detail_reqs if r.kind == "json_required_keys" for k in r.value["keys"]}
    for r in detail_reqs:
        if objects is None:
            out.append(result(f"2-2.json_structure:{r.kind}", SUB22, "JSON 필드", "question", Status.EXECUTED, FAIL,
                              required=r.value, measured={"top_level": _json_type(value)},
                              comparison="객체(또는 객체 배열) 아님",
                              reason="필드를 요구했으나 JSON 이 객체·객체 배열이 아님", evidence=[q_evidence(ctx, r)]))
            continue
        if r.kind == "json_required_keys":
            missing = [(i, k) for i, o in enumerate(objects) for k in r.value["keys"] if k not in o]
            ok = not missing
            out.append(result("2-2.json_structure:keys", SUB22, "JSON 필수 키", "question", Status.EXECUTED,
                              verdict_of(ok), required={"keys": r.value["keys"]},
                              measured={"objects": len(objects), "missing": [{"index": i, "key": k} for i, k in missing[:20]]},
                              comparison="required_keys ⊆ keys(각 객체)",
                              reason="필수 키가 모두 있음" if ok else f"필수 키 누락 {len(missing)}건 (예: {missing[0][1]})",
                              evidence=[q_evidence(ctx, r)]))
        else:
            key, want = r.value["key"], r.value["type"]
            present = [(i, o[key]) for i, o in enumerate(objects) if key in o]
            bad = [(i, _json_type(v)) for i, v in present if not _JSON_TYPE_CHECK[want](v)]
            if not present:
                # 키가 없으면 자료형 행을 만들지 않는다 — 필수 키 목록에 있으면 keys FAIL 이 세고, 없으면 판정 불가.
                skip_condition(ctx, f"2-2.json_structure:type:{key}", SUB22,
                               "키가 없어 자료형을 측정할 수 없음" + (" (필수 키 결과 참조)" if key in required_keys else ""))
                continue
            ok = not bad
            out.append(result(f"2-2.json_structure:type:{key}", SUB22, f"JSON ‘{key}’ 자료형", "question",
                              Status.EXECUTED, verdict_of(ok), required=r.value,
                              measured={"types": sorted({_json_type(v) for _, v in present})},
                              comparison=f"type({key}) == {want}",
                              reason="자료형 일치" if ok else f"‘{key}’ {want} 요구, 실제 {bad[0][1]}",
                              evidence=[q_evidence(ctx, r)]))
    return out


def check_2_2(ctx: Ctx) -> List:
    return _table_checks(ctx) + _list_heading_checks(ctx) + _json_structure(ctx)
