"""AIONA 특화 평가 — 내부 마커 노출 / 금지 표현 출현.

- 두 항목 모두 원문 MD 의 전제(실제 표시본, 코드 구획·인라인 코드 제외, 강조 마커 제거)를 따른다.
- 실제 표시본(``displayed_text``)이 없으면 원문으로 대신 판정하지 않는다 (DISPLAY_MISSING).
- 설정이 없거나 비어 있으면 검출 0건으로 바꾸지 않는다 (CONFIG_MISSING).
- 공통 통과율에 더하지 않는다. 모델명으로 공통 판정을 바꾸지 않는다.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .checks_requirements import forbidden_key
from .config import MARKER_CATEGORIES, AionaConfig, question_sha256
from .core import MD_POLICY_VERSION, Ctx, cmp_text, excerpt, nfc, result, verdict_of
from .model import Evidence, FAIL, PASS, Status

__all__ = ["check_aiona"]

SUB_M, SUB_F = "AIONA-M", "AIONA-F"


def _scope(ctx: Ctx, cfg: AionaConfig, sub: str, cid: str, title: str) -> Optional[Any]:
    applies = cfg.applies_to(ctx.system_id)
    if applies:
        return None
    reason = ("system_id 가 없어 AIONA 정책 적용 대상인지 확인할 수 없음" if applies is None else
              f"system_id ‘{ctx.system_id}’ 는 AIONA 특화 정책 적용 대상이 아님 (적용 대상: {list(cfg.applies_to_system_ids)})")
    return result(cid, sub, title, "aiona_config", Status.OUT_OF_SCOPE, reason=reason,
                  applies_to="aiona", policy_version=MD_POLICY_VERSION)


def _display_scan(ctx: Ctx):
    if ctx.da is None:
        return None
    text, offsets = ctx.da.projection
    return nfc(text), offsets


def _markers(ctx: Ctx, cfg: AionaConfig) -> List:
    cid, title = "AIONA-M.internal_marker", "내부 마커 노출"
    scoped = _scope(ctx, cfg, SUB_M, cid, title)
    if scoped:
        return [scoped]
    base = dict(applies_to="aiona", policy_version=MD_POLICY_VERSION, source="aiona_config")
    if not cfg.marker_patterns:
        return [result(cid, SUB_M, title, status=Status.CONFIG_MISSING, **base,
                       required={"categories": list(MARKER_CATEGORIES.values())},
                       reason=f"등록된 내부 마커 패턴이 없음 (확인 경로: {cfg.marker_source or '설정 없음'}"
                              f"{', 상태 ' + cfg.marker_status if cfg.marker_status else ''}) — 검출 0건으로 처리하지 않음"
                              + (f"; 설정 오류: {'; '.join(cfg.errors)}" if cfg.errors else ""))]
    scan = _display_scan(ctx)
    if scan is None:
        proj, _ = ctx.projection
        local_hits = sorted({p.rule.id for p in cfg.marker_patterns if p.rule.compiled().search(proj)})
        return [result(cid, SUB_M, title, status=Status.DISPLAY_MISSING, **base,
                       required={"patterns": len(cfg.marker_patterns)},
                       measured={"local_projection_hits_observation": local_hits},
                       reason="실제 사용자 표시본(displayed_text)이 없음 — 정제 전 원문·로컬 투영으로 판정하지 않음 "
                              "(로컬 투영 검출은 관측값으로만 기록)")]
    text, offsets = scan
    hits = []
    for p in cfg.marker_patterns:
        for m in p.rule.compiled().finditer(text):
            if m.group(0):
                s = offsets[m.start()] if m.start() < len(offsets) else None
                hits.append({"rule_id": p.rule.id, "category": p.category, "matched": m.group(0),
                             "start": s, "end": None if s is None else s + len(m.group(0))})
    missing = cfg.missing_categories
    measured = {"hits": len(hits), "categories_checked": [MARKER_CATEGORIES[c] for c in cfg.covered_categories],
                "categories_not_registered": [MARKER_CATEGORIES[c] for c in missing]}
    ev = [Evidence("displayed_text", h["start"], h["end"], h["matched"],
                   MARKER_CATEGORIES.get(h["category"], "범주 미지정")) for h in hits[:20]]
    if hits:
        status = Status.EXECUTED if not missing else Status.INCOMPLETE
        return [result(cid, SUB_M, title, status=status, verdict=FAIL, **base, required="등록 마커·잔재 0건",
                       measured=measured, comparison=cmp_text(len(hits), "==", 0), evidence=ev,
                       scope="displayed_text (코드 제외·강조 제거)",
                       reason=f"등록 마커·잔재 {len(hits)}건이 표시 본문에 남음"
                              + (f" · 미등록 범주 {measured['categories_not_registered']} 는 검사하지 못함" if missing else ""))]
    if missing:
        return [result(cid, SUB_M, title, status=Status.INCOMPLETE, **base, required="등록 마커·잔재 0건",
                       measured=measured, scope="displayed_text (코드 제외·강조 제거)",
                       reason=f"등록된 범주에서는 검출 0건이지만 미등록 범주 {measured['categories_not_registered']} 가 있어 "
                              "전체 항목 PASS 로 처리하지 않음")]
    return [result(cid, SUB_M, title, status=Status.EXECUTED, verdict=PASS, **base, required="등록 마커·잔재 0건",
                   measured=measured, comparison=cmp_text(0, "==", 0), scope="displayed_text (코드 제외·강조 제거)",
                   reason="여섯 범주 등록 패턴 모두 표시 본문에서 검출 0건")]


def _forbidden(ctx: Ctx, cfg: AionaConfig, common_results: List) -> List:
    cid, title = "AIONA-F.forbidden_expression", "금지 표현 출현"
    scoped = _scope(ctx, cfg, SUB_F, cid, title)
    if scoped:
        return [scoped]
    base = dict(applies_to="aiona", policy_version=MD_POLICY_VERSION, source="aiona_config")
    if cfg.forbidden_entries is None:
        return [result(cid, SUB_F, title, status=Status.CONFIG_MISSING, **base,
                       reason=f"질문별 금지 표현 목록이 제공되지 않음 (확인 경로: {cfg.forbidden_source or '설정 없음'}) "
                              "— 금지 표현을 추론·생성하지 않음")]
    entries = [e for e in cfg.forbidden_entries if e["case_id"] == ctx.case_id]
    if not entries:
        return [result(cid, SUB_F, title, status=Status.CONFIG_MISSING, **base,
                       reason=f"case_id ‘{ctx.case_id}’ 에 대응하는 금지 표현 목록이 없음")]
    qhash = question_sha256(ctx.question)
    matched = [e for e in entries if e.get("question_sha256") == qhash or
               (isinstance(e.get("question"), str) and nfc(e["question"]).strip() == nfc(ctx.question).strip())]
    if not matched:
        return [result(cid, SUB_F, title, status=Status.CONFIG_MISSING, **base,
                       reason="case_id 는 같지만 등록 질문과 현재 질문이 일치하지 않음 (질문 대응 불일치)")]
    exprs = [nfc(x) for x in matched[0]["forbidden_expressions"]]
    if not exprs:
        return [result(cid, SUB_F, title, status=Status.CONFIG_MISSING, **base,
                       reason="이 질문의 금지 표현 목록이 비어 있음 — 검출 0건 PASS 로 처리하지 않음")]
    scan = _display_scan(ctx)
    if scan is None:
        return [result(cid, SUB_F, title, status=Status.DISPLAY_MISSING, **base, required={"expressions": exprs},
                       reason="실제 사용자 표시본(displayed_text)이 없음 — 원문으로 대신 판정하지 않음")]
    text, offsets = scan
    out = []
    by_key = {r.dedupe_key: r for r in common_results if r.dedupe_key}
    for i, expr in enumerate(exprs, start=1):
        key = forbidden_key(expr, text)
        hits, pos = [], text.find(expr)
        while pos >= 0:
            hits.append(pos)
            pos = text.find(expr, pos + 1)
        ok = not hits
        r = result(f"{cid}:{i}", SUB_F, f"금지 표현 ‘{expr}’", status=Status.EXECUTED, verdict=verdict_of(ok), **base,
                   required={"forbidden": expr}, measured={"occurrences": len(hits)},
                   comparison=cmp_text(len(hits), "==", 0), scope="displayed_text (코드 제외·강조 제거, NFC)",
                   dedupe_key=key,
                   reason=("등록 금지 표현 검출 0건 (등록되지 않은 환각까지 탐지한 것은 아님)" if ok else
                           f"이 질문에 등록된 금지 표현이 {len(hits)}회 출현 (부정문 여부와 무관한 단순 출현 조건)"),
                   evidence=[Evidence("displayed_text", offsets[h] if h < len(offsets) else None, None,
                                      excerpt(text, h, h + len(expr)), "검출") for h in hits[:10]])
        twin = by_key.get(key)
        if twin is not None:
            r.status, r.verdict, r.duplicate_of = Status.DUPLICATE, None, twin.condition_id
            r.reason = f"질문에 직접 명시한 같은 금지 문자열(같은 대상·범위·정규화)의 2-1 결과를 참조 — 중복 집계하지 않음 ({twin.verdict})"
        out.append(r)
    return out


def check_aiona(ctx: Ctx, cfg: AionaConfig, common_results: List) -> List:
    return _markers(ctx, cfg) + _forbidden(ctx, cfg, common_results)
