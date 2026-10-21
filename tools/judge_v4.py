"""판정 v4 시험 — 고정 프롬프트 v4(상황 규칙 전부) + 수집 JSON(질문·첨부·참조 자료·답변)만 Codex CLI 와 Jev 에 던진다.
심판이 턴마다 상황을 직접 찾고(상황표 1), 항목을 판정하고, 질문 하나의 결론까지 낸다 (사용자 2026-10-20).
질문은 세트의 questions.json 을 그대로 쓴다 — 질문을 다시 검사하거나 해석하는 단계는 없다 (사용자 10-20: 이미 확인해 만든 질문).

    python -X utf8 tools/judge_v4.py --run ac6507c00276 --pick 143:claude,115:aiona,17:gemini

→ data/judge_runs/<실행>/v4/ — inputs/Q143_claude.json(심판이 받은 JSON) · codex/·jev/(심판 출력 원문) · results.jsonl · report.md
합치기(J1~J4, 두 심판이 모두 봄): Codex FAIL → FAIL · Jev FAIL 인데 Codex 는 FAIL 아님 → 보류(심판 갈림) · 그 밖은 Codex.
J5~J8 은 Codex 만. 질문 결론: FAIL 항목 하나라도 → FAIL, 보류 하나라도 → HOLD, 그 밖 PASS. CODE FAIL 이 있으면 최종 FAIL.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval import long_context, multi_turn  # noqa: E402
from code_eval import semantic_items as si  # noqa: E402
from tools import judge_inputs as ji  # noqa: E402
from tools import judge_split as js  # noqa: E402
from tools.build_judge_prompt_v4 import ALWAYS_SUBS, JEV_V4, V4 as PROMPT_V4  # noqa: E402
from tools.judge_run import judges_from_env, say  # noqa: E402
from webapp.question_generation import flatten  # noqa: E402
from webapp.run_summary import question_outcome  # noqa: E402

VERSION = "judge-v4-1"   # trial-1: 3건 시험 · v4-1: 78 전체(빈 출력·못 받은 답 처리, Jev 판정할_턴·긴 자료 줄이기)
SET_DIR = ROOT / "참고자료" / "질문세트_326_생성프롬프트v2"
SUB_ITEMS: Dict[str, List[str]] = {sub.id: [it.id for it in si.ITEMS if it.sub == sub.id] for sub in si.SUBS}
SUB_ORDER = list(SUB_ITEMS)
#: 상황표 1 에서 괄호로 조건이 따로 붙은 항목 — 상황을 적었어도 그 조건이 없으면 답하지 않는다(빠져도 보류가 아님)
CONDITIONAL_ITEMS = {"J2-5.a", "J2-5.b", "J3-3.a", "J3-5.a", "J3-5.b", "J4-1.b", "J5-2.a", "J5-2.b",
                     "J6-2.a", "J6-2.b", "J6-2.c", "J6-3.a", "J6-3.b"}
KO = {"PASS": "PASS", "FAIL": "FAIL", "NOT_APPLICABLE": "해당 없음", "UNVERIFIED": "보류", None: "-"}


# --- 입력 JSON --------------------------------------------------------------------------------------------------------

def input_tokens(row: Dict[str, Any]) -> Optional[int]:
    """그 턴의 질문 + 첨부 내용 + 참조 자료 토큰 수(J7 판단용) — 토크나이저가 없으면 None."""
    text = (row.get("question") or "") + "".join("\n" + (a.get("content") or "") for a in row.get("attachments") or [] if isinstance(a, dict))
    try:
        return long_context.measure(text, row.get("reference"))["tokens"]
    except long_context.TokenizerUnavailable:
        return None


def question_json(q: int, turns: List[Dict[str, Any]], answers: Dict[str, str], deliveries: Dict[str, str],
                  judge_turns: Optional[List[int]] = None) -> Dict[str, Any]:
    """심판에게 주는 질문 하나 — 질문·첨부파일·참조 자료·답변만(모델 이름·판정할 항목 없음). judge_turns: 이번에 판정할 턴(기본 모든 턴)."""
    out = []
    for row in turns:
        t: Dict[str, Any] = {"턴": ji._turn(row), "질문": row.get("question") or ""}
        files = [a for a in row.get("attachments") or [] if isinstance(a, dict)]
        if files:
            t["첨부파일"] = [{"이름": a.get("name"), "종류": a.get("type"), "내용": a.get("content") or "(내용 없음)",
                           **({"경로": a.get("path")} if a.get("path") else {})} for a in files]   # 경로: Decisions 가 이미지 원본을 붙일 때 씀
        if isinstance(row.get("reference"), str) and row["reference"].strip():
            t["참조자료"] = row["reference"]
        if deliveries.get(row["case_id"]):
            t["전달방식"] = deliveries[row["case_id"]]
        t["입력_추정토큰"] = input_tokens(row)
        t["답변"] = answers[row["case_id"]]
        out.append(t)
    return {"질문번호": q, "턴_수": len(out), "판정할_턴": judge_turns or [t["턴"] for t in out], "턴": out}


def output_schema() -> Dict[str, Any]:
    """Codex --output-schema — v3 의 항목 모양 그대로 + 턴마다 situations + 질문 결론."""
    base = js.gpt_output_schema()
    item = base["properties"]["turns"]["items"]["properties"]["items"]["items"]
    situation = {"type": "object", "additionalProperties": False, "required": ["id", "evidence", "quote_turn"],
                 "properties": {"id": {"type": "string"}, "evidence": {"type": "string"}, "quote_turn": {"type": ["integer", "null"]}}}
    turn = {"type": "object", "additionalProperties": False, "required": ["turn", "situations", "items"],
            "properties": {"turn": {"type": "integer"}, "situations": {"type": "array", "items": situation},
                           "items": {"type": "array", "items": item}}}
    conclusion = {"type": "object", "additionalProperties": False, "required": ["verdict", "reason"],
                  "properties": {"verdict": {"type": "string", "enum": ["PASS", "FAIL", "HOLD"]}, "reason": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False, "required": ["turns", "conclusion"],
            "properties": {"turns": {"type": "array", "items": turn}, "conclusion": conclusion}}


# --- 심판 출력 읽기 ---------------------------------------------------------------------------------------------------

def check_codex(output: Any, turns: List[Dict[str, Any]], answers: Dict[str, str]) -> Dict[int, Dict[str, Any]]:
    """Codex 출력 → 턴마다 {situations, problems, items}. 심판이 찾은 상황의 항목이 판정할 항목이 된다.
    언제나 적는 상황이 빠졌으면 더해 '판정 없음'(보류)로, 상황표에 없는 id·원문에 없는 근거 인용은 problems 에 남긴다."""
    by_turn = {t["turn"]: t for t in (output or {}).get("turns") or [] if isinstance(t, dict) and isinstance(t.get("turn"), int)} \
        if isinstance(output, dict) else {}
    rows = {ji._turn(r): r for r in turns}
    expected: Dict[int, List[str]] = {}
    info: Dict[int, Dict[str, Any]] = {}
    for n, row in rows.items():
        sits = [s for s in (by_turn.get(n) or {}).get("situations") or [] if isinstance(s, dict)]
        subs, problems = [], []
        sources = [row.get("question") or "", row.get("reference") or "", ji.attachments_block(row)]
        sources += [p.get("question") or "" for m, p in rows.items() if m < n]
        long_input = (input_tokens(row) or 0) >= long_context.MIN_TOKENS
        answered_ids = [js._base(e.get("id")) for e in (by_turn.get(n) or {}).get("items") or [] if isinstance(e, dict)]
        cands = []
        for s in sits:
            sid = str(s.get("id") or "").split(".")[0]
            if sid in SUB_ITEMS:
                cands.append({**s, "id": sid})
                continue
            # 대분류만 적은 경우('J6', 2026-10-20 시험) — 그 대분류에서 실제로 답한 항목의 세부 기준으로 푼다
            subs_of = list(dict.fromkeys(i.rsplit(".", 1)[0] for i in answered_ids if i.startswith(sid + "-") and i.rsplit(".", 1)[0] in SUB_ITEMS))
            if subs_of:
                problems.append(f"상황 id {sid} 를 답한 항목으로 풀어 씀: {' '.join(subs_of)}")
                cands += [{**s, "id": x} for x in subs_of]
            else:
                problems.append(f"상황표에 없는 상황 {s.get('id')!r}")
        for s in cands:
            sid = s["id"]
            # 입력으로 정해지는 조건(프롬프트 상황표 1) — 어긋나면 그 상황은 버리고 기록한다
            if sid.startswith("J8") and not row.get("attachments"):
                problems.append(f"{sid}: 첨부파일이 없는데 적음(버림)")
                continue
            if sid.startswith("J7") and not long_input:
                problems.append(f"{sid}: 입력이 272K 토큰 미만인데 적음(버림)")
                continue
            if sid.startswith("J6") and n < 2:
                problems.append(f"{sid}: 1턴에 적음(버림)")
                continue
            if sid not in subs:
                subs.append(sid)
            if (s.get("evidence") or "").strip() and not js.quoted_in(s["evidence"], sources):
                problems.append(f"{sid}: 근거 인용이 질문 원문에 없음")
        for sid in ALWAYS_SUBS:
            if sid not in subs:
                problems.append(f"언제나 적는 상황 {sid} 가 빠짐")
                subs.append(sid)
        subs.sort(key=SUB_ORDER.index)
        answered = {js._base(e.get("id")) for e in (by_turn.get(n) or {}).get("items") or [] if isinstance(e, dict)}
        expected[n] = [i for sid in subs for i in SUB_ITEMS[sid] if i not in CONDITIONAL_ITEMS or i in answered]
        for sid in subs:
            if not any(i in answered for i in SUB_ITEMS[sid]):
                problems.append(f"{sid}: 상황만 적고 항목 판정 없음")
        extra = sorted(answered - set(expected[n]))
        if extra:
            problems.append("찾은 상황 밖의 항목(무시): " + " ".join(extra))
        info[n] = {"situations": subs, "evidence": {s["id"]: s.get("evidence") for s in cands}, "problems": problems}
    checked = js.check_gpt(output if isinstance(output, dict) else None, turns, answers, expected)
    for n in info:
        info[n]["items"] = checked.get(n, {})
    return info


def combine(codex_v: Optional[Dict[str, Any]], jev_v: Optional[Dict[str, Any]], jev_trimmed: bool = False, label: str = "Jev") -> Tuple[Optional[str], str]:
    """J1~J4 한 항목 — (최종 판정, 어떻게 정했는지). None = 두 심판 모두 상황 없음으로 봄.
    jev_trimmed: Jev 가 그 턴을 줄인 자료로 봤으면 Jev FAIL 로 보류하지 않고 원문 전체를 본 Codex 판정을 따른다 (흐름 v2 와 같은 원칙, 10-19)."""
    c = (codex_v or {}).get("decision")
    j = (jev_v or {}).get("decision")
    if c == "FAIL":
        return "FAIL", "Codex FAIL" + (f" · {label} 도 FAIL" if j == "FAIL" else f" · {label} {KO.get(j, j)}")
    if j == "FAIL" and not jev_trimmed:
        return "UNVERIFIED", f"심판 갈림 — {label} FAIL · Codex {KO.get(c, c) if c else '상황 없음'}"
    note = f" ({label} 는 줄인 자료로 봐서 참고만)" if jev_trimmed else ""
    if c is None:
        return None, f"Codex 상황 없음 · {label} {KO.get(j, j)}{note}"
    return c, f"Codex {KO.get(c, c)} · {label} {KO.get(j, j)}{note}"


SECOND_LABEL = {"jev": "Jev", "decisions": "Decisions(OpenAI)", "jev-trimmed": "Jev"}


def _env_float(name: str, default: float) -> float:
    from code_eval.llm_client import env_value
    try:
        return float(env_value(name) or default)
    except ValueError:
        return default


#: 둘째 심판의 FAIL 이 Codex 의 PASS 를 보류로 끌어내리려면 FAIL 확률이 이 값 이상이어야 한다 (사용자 결정 2026-10-20, .env SECOND_JUDGE_CONFIDENT) — 시험하며 바꾼다
SECOND_JUDGE_CONFIDENT = _env_float("SECOND_JUDGE_CONFIDENT", 0.7)
#: 선별 판정(B, 2026-10-20): Decisions 전수 판정 뒤 Codex 에 넘기는 조건 — FAIL 확률 이 값 이상 또는 확신(confidence) 이 값 미만
SCREEN_FAIL_PROB = _env_float("SCREEN_FAIL_PROB", 0.3)
SCREEN_CONFIDENT = _env_float("SCREEN_CONFIDENT", 0.7)
#: Decisions 가 '해당 없음'이라 하고 FAIL 확률 < SCREEN_FAIL_PROB 이면 확신이 낮아도 Codex 에 안 보낸다 — 실행 a1642702fccc 전체(걸린 12,967개)에서
#: 이 묶음 4,954개 중 Codex FAIL 은 55개(1%)였고 그것을 빼도 질문 판정은 하나도 안 바뀌었다. .env SCREEN_SKIP_NA_LOWCONF=0 이면 옛 규칙.
SCREEN_SKIP_NA_LOWCONF = _env_float("SCREEN_SKIP_NA_LOWCONF", 1) != 0
#: 질문별 목표 기준만 판정 (사용자 10-21 저녁) — 질문 세트 target_items 의 의미 기준. 0 이면 예전처럼 44개 전부를 심판이 골라 판정
JUDGE_TARGET_ONLY = _env_float("JUDGE_TARGET_ONLY", 1) != 0
#: 질문으로 적용 조건이 정해지는 항목 — 목표 기준인데 Decisions 가 해당 없음이라 하면 확신과 상관없이 Codex 가 다시 본다 (Codex 검토 10-21의 26개)
QUESTION_DETERMINED = frozenset({"J2-1.a", "J2-1.b", "J2-2.a", "J2-3.a", "J2-4.a", "J2-5.a", "J2-5.b",
                                 "J3-3.a", "J3-3.b", "J3-4.a", "J3-5.a", "J3-5.b", "J5-2.b", "J6-1.a", "J6-2.b", "J6-2.c", "J6-3.a",
                                 "J6-3.b", "J6-4.a", "J6-4.b", "J7-1.a", "J7-2.a", "J8-3.a"})
#: 질문 번호 → 목표 의미 기준(J…) — main 이 질문 세트를 읽을 때 채운다
QUESTION_TARGETS: Dict[int, set] = {}
TARGET_GUIDE = ("\n[판정할 항목] 아래 항목은 이 질문을 만들 때 이 질문에 적용하도록 정한 기준입니다 — 같은 질문이면 어느 답변이든 같은 항목을 판정합니다. "
                "적용 조건이 질문에 있는 항목(질문의 요청 · 대상 · 지시)은 답변이 요청을 빠뜨렸다는 이유로 해당 없음을 고르지 말고 항목 정의대로 예 · 아니오로 판정합니다. "
                "적용 조건이 답변 내용에 따라 생기는 항목(적용 글에 '답변'이 들어간 항목 — 예: 출처를 언급한 답변, 질문의 값을 쓰는 답변)과 항목 정의의 예외(예: J1-1.a 가 아니오면 J4 해당 없음), "
                "그리고 항목 정의가 정한 적용 조건이 이 질문에 실제로 없는 경우(예: J1-3 은 질문에 모순 · 빠진 자료가 실제로 있을 때만)만 해당 없음을 고릅니다.")


def question_targets(groups: List[Dict[str, Any]]) -> Dict[int, set]:
    """질문 세트의 target_items 에서 의미 기준(J…)만 — {질문 번호: {항목}}. 목표가 없는 질문은 넣지 않는다(그 질문은 44개 전부)."""
    out: Dict[int, set] = {}
    for g in groups:
        items = {str(t).split(":")[0] for t in g.get("target_items") or [] if str(t).startswith("J")}
        if items:
            out[int(g["id"])] = items
    return out
#: 사실 확인 항목은 Decisions 가 적용된다고 보면(PASS/FAIL) 웹 검색이 있는 Codex 가 확인한다
SCREEN_ALWAYS_CODEX = ("J5-1.a",)


def fail_prob(second: Optional[Dict[str, Any]]) -> float:
    """둘째 심판이 낸 FAIL 확률 — probabilities 가 {값: 확률} 이든 [{value, probability}] 이든. 없으면 1.0(확신한 것으로 봄 — 옛 Jev 응답 호환)."""
    probs = (second or {}).get("probabilities")
    if isinstance(probs, dict) and probs:
        v = probs.get("FAIL")
        return float(v) if isinstance(v, (int, float)) else 0.0   # 확률표에 FAIL 이 없으면 FAIL 확률 0
    if isinstance(probs, list) and probs:
        for p in probs:
            if isinstance(p, dict) and p.get("value") == "FAIL" and isinstance(p.get("probability"), (int, float)):
                return float(p["probability"])
        return 0.0
    return 1.0 if (second or {}).get("decision") == "FAIL" else 0.0


def combine_weighted(codex_v: Optional[Dict[str, Any]], second_v: Optional[Dict[str, Any]], label: str = "Jev",
                     confident: float = SECOND_JUDGE_CONFIDENT, jev_trimmed: bool = False) -> Tuple[Optional[str], str]:
    """0.7 규칙(사용자 결정 2026-10-20): Codex FAIL → FAIL · 둘째 심판 FAIL 이고 그 FAIL 확률 ≥ confident → 보류(심판 갈림) ·
    둘째 심판 FAIL 인데 확률 < confident → Codex 판정을 따르되 '약한 FAIL' 로 기록 · 그 밖은 Codex. 줄인 자료로 본 둘째 심판은 참고만."""
    c = (codex_v or {}).get("decision")
    j = (second_v or {}).get("decision")
    p = fail_prob(second_v) if j == "FAIL" else 0.0
    if c == "FAIL":
        return "FAIL", "Codex FAIL" + (f" · {label} 도 FAIL {p:.2f}" if j == "FAIL" else f" · {label} {KO.get(j, j)}")
    # 둘이 갈려도 보류하지 않고 Codex 를 따른다 — 사용자 결정 10-21 ("Decisions FAIL → Codex 가 확인해 PASS 면 PASS"). 갈림은 기록만(보고서 '심판 갈림' 수).
    note = (f" ({label} {'강한' if p >= confident else '약한'} FAIL {p:.2f} — 갈림, Codex 따름)" if j == "FAIL" and not jev_trimmed else
            f" ({label} 는 줄인 자료로 봐서 참고만)" if jev_trimmed else "")
    if c is None and j == "FAIL" and not jev_trimmed:
        return "UNVERIFIED", f"Codex 상황 없음 · {label} FAIL {p:.2f} → 보류"   # Codex 가 보지 않은 항목의 FAIL 은 사람이 본다
    if c is None:
        return None, f"Codex 상황 없음 · {label} {KO.get(j, j)}{note}"
    return c, f"Codex {KO.get(c, c)} · {label} {KO.get(j, j)}{note}"


# --- 선별 판정 (B, 2026-10-20) — Decisions 가 항목 44개를 전수 판정하고 Codex 는 걸린 항목만 확인 -------------------------

SCREEN_VERSION = "judge-v5-screen-1"
SCREEN_SUFFIX = """

# 선별 판정 (v5)
[입력] 에 "판정할_항목" 이 있으면 — 먼저 다른 심판이 전수 판정한 뒤 걸린 항목입니다 — 턴마다 그 목록의 항목만 판정합니다.
- situations 에는 그 항목이 속한 상황만 적습니다(언제나 적는 상황이라도 목록에 그 항목이 없으면 적지 않습니다).
- items 에는 목록의 항목을 하나도 빼지 않고 답하고, 목록에 없는 항목은 답하지 않습니다. 사유와 인용은 평소처럼 원문 그대로 적습니다.
- 결론(conclusion)은 이번에 본 항목만으로 냅니다.
- 첨부가 있는 질문은 원본을 이미지로 함께 붙였습니다 — 이미지 파일은 그대로, pdf·docx 는 쪽마다 한 장([첨부파일] 블록의 글은 그 내용을 옮겨 적은 것). 판독·모양·내용 반영 여부는 이미지로 확인합니다."""


def missing_codex_items(output: Any, flagged: Dict[int, List[str]]) -> Dict[int, List[str]]:
    """요청한 (턴, 항목) 가운데 Codex 출력에 없는 것 — {턴: [항목]} (턴 단위가 아니라 항목 단위)."""
    have: Dict[int, set] = {}
    for t in ((output or {}).get("turns") or []) if isinstance(output, dict) else []:
        if isinstance(t, dict) and isinstance(t.get("turn"), int):
            have.setdefault(t["turn"], set()).update(js._base(e.get("id")) for e in t.get("items") or [] if isinstance(e, dict))
    out: Dict[int, List[str]] = {}
    for n in sorted(flagged):
        miss = [i for i in flagged[n] if i not in have.get(n, set())]
        if miss:
            out[n] = miss
    return out


def bad_quote_items(output: Any, turns: List[Dict[str, Any]], answers: Dict[str, str], flagged: Dict[int, List[str]]) -> Dict[Tuple[int, str], List[str]]:
    """인용이 원문에 없는 (턴, 항목) → 문제 목록. 판정 없음은 missing_codex_items 가 맡는다."""
    out: Dict[Tuple[int, str], List[str]] = {}
    for n, items in js.check_gpt(output, turns, answers, flagged).items():
        for i, v in items.items():
            quote_probs = [p for p in (v.get("problems") or []) + (v.get("notes") or []) if "원문에 없음" in p]
            if quote_probs:
                out[(n, i)] = quote_probs
    return out


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def attachment_images(turns: List[Dict[str, Any]], files_dir: Optional[Path]) -> List[Path]:
    """질문 첨부의 원본 이미지 — 이미지 파일은 그대로, pdf·docx 는 쪽 이미지(code_eval.attachment_pages) — Codex 에 -i 로 붙인다 (사용자 10-21).
    CODEX_ATTACH_PAGES=0 이면 이미지 파일만."""
    if not files_dir:
        return []
    from code_eval import attachment_pages as ap
    out: List[Path] = []
    for row in turns:
        for a in row.get("attachments") or []:
            if not isinstance(a, dict):
                continue
            p = Path(files_dir) / str(a.get("path") or a.get("name") or "")
            if not p.is_file():
                continue
            pages = [p] if p.suffix.lower() in IMAGE_EXT else (ap.page_images(p, Path(files_dir))[0] if ap.enabled() else [])
            out += [x for x in pages if x not in out]
    return out


def unverified_fact_items(output: Any, turns: List[Dict[str, Any]], answers: Dict[str, str], flagged: Dict[int, List[str]]) -> List[Tuple[int, str]]:
    """Codex 가 '확인 못함'(UNVERIFIED)이라 한 J5-1.a — 형식 문제 없이 그렇게 답한 것만."""
    out: List[Tuple[int, str]] = []
    for n, items in js.check_gpt(output, turns, answers, flagged).items():
        v = items.get("J5-1.a")
        if v and v.get("decision") == "UNVERIFIED" and not v.get("problems"):
            out.append((n, "J5-1.a"))
    return out


def undecided_items(output: Any, turns: List[Dict[str, Any]], answers: Dict[str, str], flagged: Dict[int, List[str]]) -> List[Tuple[int, str]]:
    """Codex 가 J5-1.a 가 아닌 항목에 UNVERIFIED('판단 못 함')를 쓴 (턴, 항목) — 그 항목은 PASS · FAIL · 해당 없음 중 하나여야 한다 (10-21 밤: 보류 9건)."""
    out: List[Tuple[int, str]] = []
    for n, items in js.check_gpt(output, turns, answers, flagged).items():
        for i, v in items.items():
            if i != "J5-1.a" and v and v.get("decision") == "UNVERIFIED" and not v.get("problems"):
                out.append((n, i))
    return out


WEB_SEARCH_ITEMS = frozenset({"J5-1.a", "J5-3.a"})   # Codex 웹 검색을 켜는 항목 (10-21 밤)
FACT_NOTE = ("J5-1.a 를 다시 묻습니다: 주장에 실제 세상의 고유한 대상(기관 · 인물 · 제품 · 법령 · 통계 · 날짜 · 가격 · 지명)이나 정확한 수치가 쟁점이 아니면 "
             "교과서 · 사전 수준의 일반 지식으로 PASS(반박 출처가 있으면 FAIL)로 정하세요. UNVERIFIED 를 유지하려면 쟁점인 고유 대상 · 수치를 reason 에 적으세요. "
             "질문의 자료 · 첨부는 평가용 가상 자료라 검색하지 않습니다.")   # 10-21 밤: 프롬프트를 고쳐도 강화·처벌 정의를 보류한 일
UNDECIDED_NOTE = "UNVERIFIED 는 J5-1.a(사실 확인)에서만 씁니다. 다시 묻는 항목은 정의의 점검 질문으로 PASS · FAIL · NOT_APPLICABLE 중 하나로 정하세요 — 사유가 '아니오'면 FAIL, '예'면 PASS."


def merge_codex_output(base: Any, extra: Any, replace: Optional[set] = None) -> Dict[str, Any]:
    """다시 물은 출력을 앞 출력에 합친다 — 같은 턴에는 항목을 더하고(replace 에 든 (턴, 항목)은 새 답으로 바꿈), 없던 턴은 붙인다."""
    base = dict(base) if isinstance(base, dict) else {"turns": [], "conclusion": None}
    replace = replace or set()
    turns: Dict[int, Dict[str, Any]] = {t["turn"]: dict(t) for t in base.get("turns") or [] if isinstance(t, dict) and isinstance(t.get("turn"), int)}
    for t in ((extra or {}).get("turns") or []) if isinstance(extra, dict) else []:
        if not (isinstance(t, dict) and isinstance(t.get("turn"), int)):
            continue
        n = t["turn"]
        cur = turns.setdefault(n, {"turn": n, "situations": [], "items": []})
        kept = [e for e in cur.get("items") or [] if isinstance(e, dict) and (n, js._base(e.get("id"))) not in replace]
        have = {js._base(e.get("id")) for e in kept}
        cur["items"] = kept + [e for e in t.get("items") or [] if isinstance(e, dict) and (js._base(e.get("id")) not in have or (n, js._base(e.get("id"))) in replace)]
        sids = {s.get("id") for s in cur.get("situations") or [] if isinstance(s, dict)}
        cur["situations"] = list(cur.get("situations") or []) + [s for s in t.get("situations") or [] if isinstance(s, dict) and s.get("id") not in sids]
    base["turns"] = [turns[n] for n in sorted(turns)]
    if base.get("conclusion") is None:
        base["conclusion"] = (extra or {}).get("conclusion") if isinstance(extra, dict) else None
    return base


_PROMPT_ITEM_CACHE: Dict[str, Dict[str, Any]] = {}


def prompt_item_lines(item_id: str) -> Dict[str, Any]:
    """판정 프롬프트(judge_prompt_v4.txt, Codex 가 읽는 글)의 그 항목 블록 — {'적용': 글 또는 None, 'notes': ['초보자: …', …]}.
    Decisions · Jev 가 Codex 와 같은 기준 문구를 보게 한다 (10-21: 초보자 · 외부 사실만 · 존댓말 설명이 Codex 에만 들어가 있었음)."""
    if not _PROMPT_ITEM_CACHE:
        cur = None
        for line in PROMPT_V4.read_text(encoding="utf-8").splitlines():
            m = re.match(r"- \*\*(J\d-\d+\.[a-z])\*\* · ", line)
            if m:
                cur = _PROMPT_ITEM_CACHE.setdefault(m.group(1), {"적용": None, "notes": []})
                continue
            if cur is None or not line.startswith("  · "):
                cur = None if not line.startswith("  · ") else cur
                continue
            key, _, val = line[4:].partition(":")
            key = key.strip()
            if key == "적용":
                cur["적용"] = val.strip()
            elif key in ("근거", "표시") or key.endswith(" 예") or key.startswith(("PASS", "FAIL")):
                continue
            else:
                cur["notes"].append(f"{key}: {val.strip()}")
    return _PROMPT_ITEM_CACHE.get(item_id) or {"적용": None, "notes": []}


def all_item_questions(fixed: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Decisions 에 보낼 항목 44개의 choice 질문 — J1~J4 는 Jev 묶음(fixed["items"]), J5~J8 은 semantic_items 정의로 같은 모양을 만들고,
    판정 프롬프트의 적용 조건(J5~J8) · 덧붙인 설명(전체)을 붙여 Codex 와 같은 기준 문구로 맞춘다 (10-21)."""
    out: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in fixed["items"].items()}
    for it in si.ITEMS:
        if it.id in out:
            continue
        sub = si.SUBS_BY_ID[it.sub]
        applies = prompt_item_lines(it.id)["적용"] or it.applies
        out[it.id] = {"type": "choice",
                      "instructions": f"[{it.id}] {sub.name} — {it.question} / 적용: {applies} / 근거: {it.evidence}",
                      "criteria": {"PASS": f"예 — {it.question}", "FAIL": f"아니오 — {it.question}에 어긋남",
                                   "NOT_APPLICABLE": f"해당 없음 — 이 턴의 질문에 적용 조건이 없음 (적용: {applies})"}}
    for k, q in out.items():
        notes = prompt_item_lines(k)["notes"]
        if notes:
            q["instructions"] = str(q.get("instructions") or k) + " / " + " / ".join(notes)
    return out


def conversation_is_multi_turn(turns: List[Dict[str, Any]], answers: Dict[str, str]) -> Optional[bool]:
    """그 모델이 실제로 주고받은 대화가 멀티턴인가 — 한 턴(질문 + 답변) 10K 토큰 이상인 턴 10개 이상 + 대화 전체 100K 토큰 이상 (code_eval.multi_turn,
    10-21 밤). 모르면(토크나이저 없음) None — 그때는 J6 를 빼지 않는다."""
    m = multi_turn.measure_conversation([(ji._turn(r), r.get("question") or "", answers.get(r.get("case_id")) or "") for r in turns])
    return m.get("multi_turn")


def turn_item_ids(row: Dict[str, Any], n: int, all_ids: List[str], multi: Optional[bool] = True) -> List[str]:
    """입력으로 정해지는 조건(프롬프트 상황표 1): J8 은 첨부가 있는 턴만, J7 은 입력 272K 토큰 이상만, J6 은 멀티턴 대화(한 턴 10K 토큰
    이상인 턴 10개 이상 · 대화 전체 100K 토큰 이상)의 2턴부터 — 멀티턴이 아니면(multi=False) J6 를 판정하지 않는다."""
    has_files = bool(row.get("attachments"))
    long_input = (input_tokens(row) or 0) >= long_context.MIN_TOKENS
    out = []
    for i in all_ids:
        if i.startswith("J8") and not has_files:
            continue
        if i.startswith("J7") and not long_input:
            continue
        if i.startswith("J6") and (n < 2 or multi is False):
            continue
        out.append(i)
    return out


def screen_flag(item: str, entry: Optional[Dict[str, Any]], fail_prob_min: float = SCREEN_FAIL_PROB, confident: float = SCREEN_CONFIDENT) -> Tuple[bool, str]:
    """Decisions 답 하나를 Codex 에 넘길지 — (넘김?, 이유). 답이 없으면 넘긴다."""
    if not isinstance(entry, dict) or entry.get("choice") is None:
        return True, "Decisions 답 없음"
    choice = entry.get("choice")
    p = fail_prob({"decision": choice, "probabilities": entry.get("probabilities")})
    conf = entry.get("confidence")
    if choice == "FAIL":
        return True, f"Decisions FAIL {p:.2f}"
    if p >= fail_prob_min:
        return True, f"FAIL 확률 {p:.2f} ≥ {fail_prob_min}"
    if isinstance(conf, (int, float)) and conf < confident:
        if choice == "NOT_APPLICABLE" and SCREEN_SKIP_NA_LOWCONF:
            return False, ""
        return True, f"확신 {conf:.2f} < {confident}"
    if item in SCREEN_ALWAYS_CODEX and choice == "PASS":
        return True, "사실 확인은 Codex(웹 검색)가 봄"
    return False, ""


_NOT_EXTERNAL = re.compile(r"외부\s*사실(?:\s*확인)?\s*(?:항목|주장)?[이은]\s*아[니닙]|J5-2[.a-z]*\s*에서\s*(?:판단|본다|다룬)"
                           r"|자료\s*내\s*사실|입력값\s*기반|J5-1\s*적용\s*대상이\s*아니|외부\s*출처로\s*확인할\s*사실\s*주장이\s*아[니닙]|붙여\s*넣은\s*자료를\s*그대로"
                           r"|자료\s*사실이지만|제공된\s*기록의\s*운영\s*세부와\s*부합|회의록의\s*입력값|입력\s*수치로\s*산출|외부\s*사실\s*주장(?:은|이)?\s*(?:없|발견되지\s*않)")   # 10-21 밤: 보류 사례의 실제 사유 문구


def material_text(doc: Dict[str, Any], upto: int) -> str:
    """그 턴까지 질문 쪽 자료 — 질문 · 첨부 내용 · 참조 자료(답변은 넣지 않음: 앞선 AI 답변은 사실 근거가 아님)."""
    parts = []
    for t in doc.get("턴") or []:
        if int(t.get("턴") or 0) > upto:
            continue
        parts.append(str(t.get("질문") or ""))
        parts += [str(a.get("내용") or "") for a in t.get("첨부파일") or [] if isinstance(a, dict)]
        parts.append(str(t.get("참조자료") or ""))
    return re.sub(r"\s+", " ", "\n".join(parts))


def material_fact_guard(items: Dict[str, Dict[str, Any]], doc: Dict[str, Any], n: int) -> None:
    """사실 확인(J5-1.a)이 '확인 못 함'인데 그 주장이 질문에 붙은 자료(질문 · 첨부 · 참조 자료 · 앞 턴 질문)에 그대로 있거나
    심판 사유가 '외부 사실이 아님'이면 해당 없음으로 — 자료 대조는 J5-2 가 한다 (사용자 10-21: Q40 '푸른자료실')."""
    it = items.get("J5-1.a")
    if not it or it.get("decision") != "UNVERIFIED":
        return
    quote = re.sub(r"[*_`#>|]+", "", str(it.get("answer_quote") or ""))
    quote = re.sub(r"(?m)^\s*[-•·]\s+", "", quote).strip()   # 목록 글머리는 빼고 대조 (10-21 밤)
    quote = re.sub(r"\s+", " ", quote)
    in_material = len(quote) >= 4 and quote in material_text(doc, n)
    if in_material or _NOT_EXTERNAL.search(str(it.get("reason") or "")):
        it["decision"] = "NOT_APPLICABLE"
        it["how"] = (it.get("how") or "") + " → 입력 자료 안의 사실(J5-1 해당 없음, 자료 대조는 J5-2)"


def fact_fail_guard(items: Dict[str, Dict[str, Any]]) -> None:
    """사실 확인(J5-1.a) FAIL 은 반박 출처가 있을 때만 — 출처 없이 '거짓'으로 본 주장뿐이면 '확인 못 함'으로 (10-21: 일반 지식만으로 꼬투리를 잡은 FAIL)."""
    it = items.get("J5-1.a")
    if not it or it.get("decision") != "FAIL":
        return
    facts = [e.get("fact") for e in it.get("entries") or [] if isinstance(e, dict) and isinstance(e.get("fact"), dict)]
    if any(f.get("verdict") == "FALSE" and f.get("sources") for f in facts):
        return
    it["decision"] = "UNVERIFIED"
    it["kind"] = "확인 못함"
    it["how"] = (it.get("how") or "") + " → 반박 출처 없음(거짓은 검색 출처로만) → 확인 못 함"


_REASON_SAYS_OK = re.compile(r"FAIL\s*(?:항목)?\s*없음|문제(?:가|는)?\s*없습니다|어긋나지\s*않|모두\s*지켰|불필요한\s*(?:단락|내용)은\s*없|반복(?:은|이)?\s*없습니다")


def reason_decision_guard(items: Dict[str, Dict[str, Any]]) -> None:
    """사유와 판정이 반대인 항목 — FAIL 인데 사유가 '문제 없음 · FAIL 없음'이면 확인 못 함으로(사유 모순) (10-21: GPT Q1 4턴 J1-4.a '불필요한 단락은 없습니다 (FAIL 없음)' 인데 FAIL)."""
    for it in items.values():
        if isinstance(it, dict) and it.get("decision") == "FAIL" and _REASON_SAYS_OK.search(str(it.get("reason") or "")):
            it["decision"] = "UNVERIFIED"
            it["kind"] = "사유 모순"
            it["how"] = (it.get("how") or "") + " → 사유는 '문제 없음'인데 판정은 FAIL(사유 모순) → 확인 못 함"


_FACT_SAYS_TRUE = re.compile(r"(?:맞습니다|부합합니다|정확합니다|타당합니다|올바릅니다|일치합니다)")
_FACT_SAYS_UNSURE = re.compile(r"확인하지\s*못|검증하지\s*못|확인할\s*수\s*없|검증할\s*수\s*없|확정하기\s*어렵|확정하지\s*않|출처[를가]?\s*(?:확보|찾)지\s*못|불확실")


def fact_true_guard(items: Dict[str, Dict[str, Any]]) -> None:
    """사실 확인(J5-1.a)이 '확인 못 함'인데 사유가 '맞습니다 · 부합합니다'처럼 참이라고만 말하고 확인 못 했다는 말이 없으면 PASS (사유 모순)
    (10-21 밤: Q7 claude T2 '통상적인 과학 지식에 맞습니다', Q3 gpt T2 '설명은 타당합니다' 인데 보류)."""
    it = items.get("J5-1.a")
    if not it or it.get("decision") != "UNVERIFIED":
        return
    reason = str(it.get("reason") or "")
    if _FACT_SAYS_TRUE.search(reason) and not _FACT_SAYS_UNSURE.search(reason):
        it["decision"] = "PASS"
        it["how"] = (it.get("how") or "") + " → 사유가 '맞다'고만 함(사유 모순) → PASS"


_TOOL_CLAIM = re.compile(r"조회|확인한\s*것처럼|실행한\s*것처럼|접근\s*상태|작업\s*(?:폴더|공간)|파일이\s*(?:없|하나도)")
_SOURCE_WORD = re.compile(r"출처|문헌|조문|제품|인용")


def tool_claim_guard(items: Dict[str, Dict[str, Any]]) -> None:
    """J5-3.a FAIL 이 '조회했다 · 파일이 없었다' 같은 답변 모델 자신의 조회 · 실행 서술 때문이면 해당 없음 — 도구 사용 기록을 주지 않으므로
    판정하지 않는다(상황표 2). 존재하지 않는 출처 · 문헌 · 조문 · 제품을 든 FAIL 은 그대로 (10-21 밤: Q5 · Q7 claude '작업 폴더에서 파일이 발견되지 않았다')."""
    it = items.get("J5-3.a")
    if not it or it.get("decision") != "FAIL":
        return
    text = str(it.get("reason") or "") + " " + str(it.get("answer_quote") or "")
    if _TOOL_CLAIM.search(text) and not _SOURCE_WORD.search(str(it.get("reason") or "")):
        it["decision"] = "NOT_APPLICABLE"
        it["how"] = (it.get("how") or "") + " → 답변 모델의 조회 · 실행 서술은 판정하지 않음(상황표 2) → 해당 없음"


_HONORIFIC_MIX = re.compile(r"해요체.{0,40}(?:합니다|하십시오|습니다|격식)체|(?:합니다|하십시오|습니다|격식)체.{0,40}해요체|높임\s*등급.{0,20}섞")
_LOW_SPEECH = re.compile(r"반말|해체|해라체|하게체|낮춤")
_GRADE_ASKED = re.compile(r"격식체|하십시오체|합쇼체|습니다체|합니다체|해요체로|하오체")


def honorific_mix_guard(items: Dict[str, Dict[str, Any]], doc: Dict[str, Any], n: int) -> None:
    """J2-2.a FAIL 이 '해요체와 합니다체를 섞었다'는 것뿐인데 질문은 '존댓말'만 지정했으면 PASS — 기준 문구: 존댓말만 지정하면
    높임 세 등급이 모두 존댓말이고 섞어 써도 지킨 것 (10-21 밤: AIONA Q22 '…반환해요' FAIL)."""
    it = items.get("J2-2.a")
    if not it or it.get("decision") != "FAIL":
        return
    reason = str(it.get("reason") or "")
    asked = material_text(doc, n)
    if (_HONORIFIC_MIX.search(reason) and not _LOW_SPEECH.search(reason)
            and "존댓말" in asked and not _GRADE_ASKED.search(asked)):
        it["decision"] = "PASS"
        it["how"] = (it.get("how") or "") + " → 존댓말만 지정한 질문에서 높임 등급(해요체·합니다체)을 섞은 것은 지킨 것(기준 문구) → PASS"


def combine_screen(codex_v: Optional[Dict[str, Any]], dec_v: Optional[Dict[str, Any]], flagged: bool,
                   confident: float = SECOND_JUDGE_CONFIDENT, item: Optional[str] = None) -> Tuple[Optional[str], str]:
    """선별 판정의 항목 하나 — 안 걸린 항목은 Decisions 대로, 걸린 항목은 0.7 규칙으로 Codex 와 합친다. Codex 가 못 본 걸린 항목: Decisions FAIL 이면 보류."""
    d = (dec_v or {}).get("choice")
    p = fail_prob({"decision": "FAIL", "probabilities": (dec_v or {}).get("probabilities")}) if d == "FAIL" else 0.0
    if not flagged:
        return d, f"Decisions {KO.get(d, d)} {max((dec_v or {}).get('probabilities', {}).values(), default=0):.2f}" if isinstance((dec_v or {}).get("probabilities"), dict) else f"Decisions {KO.get(d, d)}"
    if codex_v is None or codex_v.get("decision") is None:
        if d == "FAIL":
            return "UNVERIFIED", f"Codex 판정 없음 · Decisions FAIL {p:.2f} → 보류"
        return d, f"Decisions {KO.get(d, d)} (Codex 판정 없음)"
    if codex_v.get("decision") == "UNVERIFIED" and item != "J5-1.a" and not codex_v.get("problems"):
        # J5-1.a 가 아닌 항목의 '판단 못 함' — 다시 물어도 그러면 Decisions 가 확신할 때 그 판정을 따르고, 아니면 보류로 남긴다 (10-21 밤: 보류 9건)
        conf = float((dec_v or {}).get("confidence") or 0.0)
        if d and conf >= confident:
            return d, f"Codex 판정 미정(UNVERIFIED 는 J5-1.a 만) · Decisions {KO.get(d, d)} 확신 {conf:.2f} → Decisions 따름"
        return "UNVERIFIED", f"Codex 판정 미정(UNVERIFIED 는 J5-1.a 만) · Decisions {KO.get(d, d)} 확신 {conf:.2f} < {confident} → 보류"
    if codex_v.get("decision") == "UNVERIFIED" and item == "J5-1.a" and d == "NOT_APPLICABLE" and not codex_v.get("problems"):
        # 사실 확인 항목에서 Codex 가 "확인 못함"이라 했는데 Decisions 는 적용 조건이 없다고 봄 — 코드·입력 자료 질문에 외부 사실이 없는 경우 (사용자 10-21)
        return "NOT_APPLICABLE", "Codex 확인 못함 · Decisions 해당 없음 → 외부 사실 주장 없음으로 봄"
    second = {"decision": d, "probabilities": (dec_v or {}).get("probabilities")}
    return combine_weighted(codex_v, second, "Decisions", confident)


def screen_one(q: int, provider: str, turns: List[Dict[str, Any]], answers: Dict[str, str], deliveries: Dict[str, str],
               code_fails: List[str], prompt: str, fixed: Dict[str, Any], gpt: Any, decisions: Any, out: Path,
               codex_all: bool = False, reuse_codex: bool = False, reuse_decisions: bool = False) -> Dict[str, Any]:
    """선별 판정 한 질문: ① Decisions 가 턴마다 적용 가능한 항목 전부(최대 44)를 원문·첨부 원본으로 판정 ② 걸린 항목만 Codex 가 사유·인용과 함께 다시 봄
    (codex_all=True 면 교정용으로 모든 항목을 Codex 에도) ③ 합치기(0.7 규칙) → 판정 v4 와 같은 결과 줄."""
    started = time.monotonic()
    tag = f"Q{q} {provider}"
    doc = question_json(q, turns, answers, deliveries)
    name = f"Q{q}_{provider}.json"
    (out / "inputs" / name).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    allq = all_item_questions(fixed)
    calls: Dict[str, Any] = {}
    multi_ok = conversation_is_multi_turn(turns, answers) if len(turns) > 1 else True   # 멀티턴(턴당 10K · 10턴 · 전체 100K) 아니면 J6 빼기 (10-21 밤)
    # ① Decisions 전수 — 턴마다 한 요청(그 턴까지의 대화 + 그 턴의 항목), 자르지 않음
    dec_items: Dict[int, Dict[str, Any]] = {}
    dec_errors: List[str] = []
    usage_tokens = 0
    t0 = time.monotonic()
    reused_dec = False
    saved_dec = out / "decisions" / name
    if reuse_decisions and saved_dec.exists():   # --repair: 저장된 Decisions 답으로 (API 안 부름)
        try:
            dec_items = {int(k): v for k, v in json.loads(saved_dec.read_text(encoding="utf-8")).items() if isinstance(v, dict)}
            reused_dec = bool(dec_items)
        except (OSError, ValueError):
            dec_items = {}
    for row in turns:
        n = ji._turn(row)
        if reused_dec and n in dec_items:
            continue
        ids = turn_item_ids(row, n, list(allq), multi_ok)
        targets = QUESTION_TARGETS.get(q) if JUDGE_TARGET_ONLY else None
        if targets:   # 질문별 목표 기준만 (10-21 저녁) — 같은 질문은 네 모델 모두 같은 항목
            ids = [i for i in ids if i in targets]
        part = {"guide": fixed["guide"] + (TARGET_GUIDE if targets else ""), "질문번호": q, "판정할_턴": [n], "턴": [t for t in doc["턴"] if t["턴"] <= n]}
        qd = {f"T{n}:{i}": allq[i] for i in ids}
        status, body, error = decisions.judge(part, qd) if decisions else ("ERROR", None, "Decisions 없음")
        if status == "OK":
            ans = (body or {}).get("answers") or {}
            dec_items[n] = {i: ans.get(f"T{n}:{i}") for i in ids}
            usage_tokens += int(((body or {}).get("usage") or {}).get("input_tokens") or 0) if isinstance((body or {}).get("usage"), dict) else 0
        else:
            dec_items[n] = {i: None for i in ids}
            dec_errors.append(f"T{n} {status} {str(error)[:120]}")
    calls["decisions"] = {"status": "OK" if not dec_errors else ("PARTIAL" if any(any(v for v in d.values()) for d in dec_items.values()) else "ERROR"),
                          "error": "; ".join(dec_errors), "seconds": round(time.monotonic() - t0, 1), "calls": len(turns), "input_tokens": usage_tokens,
                          "reused": reused_dec}
    (out / "decisions" / name).write_text(json.dumps({str(n): v for n, v in dec_items.items()}, ensure_ascii=False, indent=1), encoding="utf-8")
    # ② 걸린 항목 → Codex
    flagged: Dict[int, List[str]] = {}
    why: Dict[str, str] = {}
    for n, items in dec_items.items():
        for i, entry in items.items():
            if multi_ok is False and i.startswith("J6"):
                continue   # 멀티턴이 아닌 대화의 J6 는 Codex 에도 넘기지 않음 (10-21 밤)
            flag, reason = (True, "교정용 전수") if codex_all else screen_flag(i, entry)
            if (not flag and JUDGE_TARGET_ONLY and i in QUESTION_DETERMINED and isinstance(entry, dict)
                    and entry.get("choice") == "NOT_APPLICABLE" and i in (QUESTION_TARGETS.get(q) or set())):
                flag, reason = True, "목표 기준인데 해당 없음 — 질문으로 정해지는 항목"   # (10-21)
            if flag:
                flagged.setdefault(n, []).append(i)
                why[f"T{n}:{i}"] = reason
    n_items = sum(len(v) for v in dec_items.values())
    n_flagged = sum(len(v) for v in flagged.values())
    say(f"   🔎 {tag} Decisions {calls['decisions']['status']} {calls['decisions']['seconds']}초 · 항목 {n_items}개 중 Codex 로 {n_flagged}개"
        + (f" — {'; '.join(dec_errors)[:160]}" if dec_errors else ""))
    codex_items: Dict[int, Dict[str, Any]] = {}
    codex_sits: Dict[int, Dict[str, Any]] = {}
    saved = out / "codex" / name
    if flagged and (gpt is not None or (reuse_codex and saved.exists())):   # --repair 는 Codex CLI 없이도 저장된 출력으로 합친다
        # 웹 검색은 필요할 때만 — 질문의 자료 · 첨부는 이 평가를 위해 만든 가상 자료라 웹에 없다. 실제 세상의 사실(J5-1.a)이나
        # 출처 존재(J5-3.a)를 Codex 에 넘길 때만 켠다 (사용자 2026-10-21 밤)
        search = (not any((t.get("reference") or "").strip() for t in turns)
                  and any(i in WEB_SEARCH_ITEMS for v in flagged.values() for i in v))
        output: Any = None
        t1 = time.monotonic()
        body_doc = {**doc, "판정할_턴": sorted(flagged), "판정할_항목": {str(n): flagged[n] for n in sorted(flagged)}}
        tokens = {"input_tokens": 0, "output_tokens": 0}
        images = attachment_images(turns, getattr(gpt, "files_dir", None))

        def ask_codex(ask_doc: Dict[str, Any]) -> Tuple[str, Any, str]:
            st, outp, err = gpt.judge(prompt + SCREEN_SUFFIX, json.dumps(ask_doc, ensure_ascii=False, indent=1), search, tag, output_schema=output_schema(), images=images)
            u = getattr(gpt, "last_usage", None) or {}
            for k in tokens:
                tokens[k] += int(u.get(k) or 0)
            return st, outp, err

        rounds, reused, error = 0, False, ""
        if reuse_codex and saved.exists() and json.loads(saved.read_text(encoding="utf-8") or "null"):
            output, status, reused = json.loads(saved.read_text(encoding="utf-8")), "OK", True
            say(f"   🖥 {tag} Codex — 저장된 출력 다시 씀 ({saved.name}); 답하지 않았거나 인용이 틀린 항목만 다시 묻는다")
        else:
            say(f"   🖥 {tag} Codex CLI — 걸린 항목 {n_flagged}개(턴 {sorted(flagged)}) · 웹 검색 {'켬' if search else '끔'} …")
            status, output, error = ask_codex(body_doc)
            rounds = 1
        # 답하지 않은 (턴, 항목)과 인용이 원문에 없는 (턴, 항목)만 다시 묻는다 — 턴에 항목 하나만 있어도 완료로 보던 것(보류의 64%)과
        # 인용 불일치(27%)를 줄인다 (Codex 지적 10-20). 인용 불일치는 항목마다 한 번만 다시.
        reasked: set = set()
        while status == "OK" and gpt is not None and rounds < len(flagged) + 3:
            missing = missing_codex_items(output, flagged)
            bad = {k: v for k, v in bad_quote_items(output, turns, answers, flagged).items() if k not in reasked}
            # J5-1.a '확인 못함'은 일반 지식인지 한 번 다시 묻는다 — 복구(옛 프롬프트 출력)뿐 아니라 새 판정에도 (10-21 밤)
            fact_unv = [k for k in unverified_fact_items(output, turns, answers, flagged) if k not in reasked]
            bad.update({k: ["J5-1.a 확인 못함 — 일반 지식인지 다시"] for k in fact_unv})
            undecided = [k for k in undecided_items(output, turns, answers, flagged) if k not in reasked]
            bad.update({k: ["J5-1.a 가 아닌 항목의 UNVERIFIED — 다시"] for k in undecided})   # 10-21 밤
            targets: Dict[int, List[str]] = {}
            for n, items in list(missing.items()) + [(n, [i]) for (n, i) in bad]:
                targets.setdefault(n, [])
                targets[n] += [i for i in items if i not in targets[n]]
            if not targets:
                break
            rounds += 1
            ask_turns = sorted(targets)[:max(1, len(targets) // 2)]
            say(f"   ↪ {tag} Codex 다시 묻기({rounds}번째) — 답 없음 {sum(len(v) for v in missing.values())}개 · 인용 불일치 {len(bad)}개 → 턴 {ask_turns}")
            st2, out2, err2 = ask_codex({**body_doc, "판정할_턴": ask_turns, "판정할_항목": {str(n): targets[n] for n in ask_turns},
                                         **({"주의": " ".join(x for x, ks in ((UNDECIDED_NOTE, undecided), (FACT_NOTE, fact_unv)) if any(k[0] in ask_turns for k in ks))}
                                            if any(k[0] in ask_turns for k in undecided + fact_unv) else {})})
            if st2 != "OK":
                error = err2
                break
            replace = {(n, i) for n in ask_turns for i in targets[n]}
            reasked |= {k for k in bad if k in replace}
            output = merge_codex_output(output, out2, replace)
        missing = missing_codex_items(output, flagged) if status == "OK" else {}
        if status == "OK" and not any((t or {}).get("items") for t in ((output or {}).get("turns") or []) if isinstance(t, dict)):
            # 결과를 통째로 비워 냄(판정을 아예 안 함) — '확인 못 함' 보류가 아니라 판정 실행 오류로 (10-21: 파일을 못 읽은 Codex 가 빈 결과를 낸 일)
            status, missing = "ERROR", {}
            error = "Codex 가 판정 결과를 비워 냄: " + str(((output or {}).get("conclusion") or {}).get("reason") or "")[:150]
        calls["codex"] = {"status": status, "error": error if status != "OK" else (f"끝내 답하지 않은 항목 {sum(len(v) for v in missing.values())}개" if missing else ""),
                          "seconds": round(time.monotonic() - t1, 1), "search": search, "items": n_flagged, "rounds": rounds, "reused": reused,
                          "missing_turns": sorted(missing), "missing_items": {str(n): v for n, v in missing.items()}, **tokens}
        say(f"   🖥 {tag} Codex {status} {calls['codex']['seconds']}초 · 호출 {rounds}번" + (f" — {str(calls['codex']['error'])[:160]}" if calls['codex']['error'] else ""))
        (out / "codex" / name).write_text(json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8")
        if calls["codex"]["status"] == "OK":
            codex_items = js.check_gpt(output, turns, answers, flagged)
            for t in (output or {}).get("turns") or []:
                if isinstance(t, dict) and isinstance(t.get("turn"), int):
                    codex_sits[t["turn"]] = {"situations": [str(s.get("id")) for s in t.get("situations") or [] if isinstance(s, dict)],
                                             "evidence": {str(s.get("id")): s.get("evidence") for s in t.get("situations") or [] if isinstance(s, dict)}}
    elif flagged:
        calls["codex"] = {"status": "ERROR", "error": "Codex 없음", "seconds": 0, "items": n_flagged}
    else:
        calls["codex"] = {"status": "SKIPPED", "error": "", "seconds": 0, "items": 0}
    # ③ 합치기
    final: Dict[int, Dict[str, Dict[str, Any]]] = {}
    turn_out: Dict[str, Any] = {}
    agree = total = 0
    for row in turns:
        n = ji._turn(row)
        items: Dict[str, Dict[str, Any]] = {}
        for i, entry in (dec_items.get(n) or {}).items():
            if multi_ok is False and i.startswith("J6"):
                continue   # 저장된 Decisions 답(--repair)에 J6 가 있어도 멀티턴이 아니면 판정하지 않음 (10-21 밤)
            is_flagged = i in (flagged.get(n) or [])
            cv = (codex_items.get(n) or {}).get(i) if is_flagged else None
            dec, how = combine_screen(cv, entry, is_flagged, item=i)
            if dec is None:
                continue
            if is_flagged and cv and cv.get("decision") is not None and isinstance(entry, dict):
                total += 1
                agree += (cv.get("decision") == entry.get("choice"))
            e = ((cv or {}).get("entries") or [{}])
            fail_e = next((x for x in e if x.get("decision") == "FAIL"), e[0])
            items[i] = {"decision": dec, "how": how, "codex": (cv or {}).get("decision"), "jev": (entry or {}).get("choice") if isinstance(entry, dict) else None,
                        "second_judge": "decisions", "decisions_prob": (entry or {}).get("probabilities") if isinstance(entry, dict) else None,
                        "decisions_confidence": (entry or {}).get("confidence") if isinstance(entry, dict) else None,
                        "flagged": why.get(f"T{n}:{i}") or "", "jev_reason": None, "problems": (cv or {}).get("problems") or [], "notes": (cv or {}).get("notes") or [],
                        "reason": fail_e.get("reason"), "answer_quote": fail_e.get("answer_quote"), "question_quote": fail_e.get("question_quote"),
                        "entries": (cv or {}).get("entries") or []}
        js.apply_overrides(items)
        material_fact_guard(items, doc, n)   # 입력 자료 안의 사실은 J5-1 해당 없음 (10-21)
        fact_fail_guard(items)   # 사실 확인 FAIL 은 반박 출처가 있을 때만 (10-21)
        reason_decision_guard(items)   # 사유와 판정이 반대 (10-21)
        fact_true_guard(items)   # 사실 확인 사유가 '맞다'인데 보류 (10-21 밤)
        tool_claim_guard(items)   # 조회 · 실행 서술의 J5-3.a FAIL 은 해당 없음 (10-21 밤)
        honorific_mix_guard(items, doc, n)   # 존댓말만 지정 + 높임 등급 섞음 FAIL 은 PASS (10-21 밤)
        final[n] = items
        turn_out[str(n)] = {"situations_judge": (codex_sits.get(n) or {}).get("situations") or [], "evidence": (codex_sits.get(n) or {}).get("evidence") or {},
                            "problems": [], "items": items}
    judge_error = ""
    if calls["decisions"]["status"] == "ERROR" and calls["codex"]["status"] != "OK":
        semantic, judge_error = "HOLD", "Decisions·Codex 모두 실패 — 다시 판정 필요"
    elif flagged and calls["codex"]["status"] not in ("OK", "SKIPPED"):   # 걸린 항목을 Codex 가 못 봤으면 Decisions 만으로 PASS 를 내지 않는다 (Codex 지적 10-20)
        semantic, judge_error = "HOLD", f"Codex 판정 실행 오류 — 다시 판정 필요: {str(calls['codex'].get('error') or '')[:120]}"
    else:
        semantic = verdict_of(final)
    verdict = question_verdict(code_fails, semantic)
    line = {"version": SCREEN_VERSION, "mode": "screen", "provider": provider, "question": q, "turns": len(turns), "verdict": verdict, "semantic": semantic,
            "judge_error": judge_error or None, "reason": judge_error or None,
            "code_fails": code_fails, "codex_conclusion": None, "jev_codex_agree": [agree, total], "seconds": round(time.monotonic() - started, 1),
            "screen": {"items": n_items, "flagged": n_flagged, "fail_prob_min": SCREEN_FAIL_PROB, "confident": SCREEN_CONFIDENT,
                       "second_judge_confident": SECOND_JUDGE_CONFIDENT, "codex_all": codex_all},
            "calls": calls, "turn_results": turn_out}
    fails = [f"T{n}:{i}" for n, p in final.items() for i, v in p.items() if v["decision"] == "FAIL"]
    holds = [f"T{n}:{i}" for n, p in final.items() for i, v in p.items() if v["decision"] == "UNVERIFIED"]
    say(f"■ {tag}: {verdict} · {line['seconds']:.0f}초 · 항목 {n_items} → Codex {n_flagged}"
        + (f" · CODE FAIL {' '.join(code_fails)}" if code_fails else "") + (f" · FAIL {' '.join(fails)}" if fails else "") + (f" · 보류 {' '.join(holds)}" if holds else ""))
    return line


JEV_TRIM_CHARS = 35_000   # Jev 입력 예산(글자) — 약 5만 자부터 Jev 가 max_tokens_exceeded (10-19·10-20)
TRIM_MARK = "…[중략: {n:,}자 생략 — 전문은 Codex 가 본다]…"


def _cut(holder: Dict[str, Any], key: str, keep: int) -> int:
    text = holder[key]
    if len(text) <= keep:
        return 0
    head, tail = int(keep * 0.6), keep - int(keep * 0.6)
    cut = len(text) - head - tail
    holder[key] = text[:head] + TRIM_MARK.format(n=cut) + text[-tail:]
    return cut


def turn_has_files(part: Dict[str, Any], n: int) -> bool:
    return any(t.get("첨부파일") for t in part.get("턴") or [] if t.get("턴") == n)


def second_judge(part: Dict[str, Any], questions: Dict[str, Any], jev: Any, decisions: Any,
                 budget: int = JEV_TRIM_CHARS, prefer_decisions: bool = False) -> Tuple[str, Any, str, Optional[str], int]:
    """J1~J4 둘째 심판 고르기 (사용자 결정 2026-10-20): ⓪ 첨부가 있는 턴(prefer_decisions)은 Decisions(OpenAI) 먼저 — 글로 옮긴 내용에 더해 이미지 원본도 봄
    ① 예산 안이면 Jev 원문 ② Jev 가 한도를 넘거나 실패하면 Decisions 원문 ③ Decisions 가 없거나 실패하면 Jev 에 줄인 자료(참고만)
    ④ 그것도 안 되면 둘째 심판 없음 — Codex 판정만. 돌려주는 값: (status, body, error, 쓴 심판 'jev'·'decisions'·'jev-trimmed'·None, 줄인 글자 수)."""
    chars = js.state_chars(part)
    errors: List[str] = []
    if prefer_decisions and decisions is not None:
        status, body, error = decisions.judge(part, questions)
        if status == "OK":
            return status, body, "", "decisions", 0
        errors.append(f"Decisions(첨부 우선) {status} {error}")
        decisions = None   # 아래서 또 부르지 않는다
    if jev is not None and chars <= budget:
        status, body, error = jev.judge(part, questions)
        if status == "OK":
            return status, body, "", "jev", 0
        errors.append(f"Jev {status} {error}")
    elif jev is None:
        errors.append("Jev 없음")
    else:
        errors.append(f"Jev 예산 초과 {chars:,}자 > {budget:,}자")
    if decisions is not None:
        status, body, error = decisions.judge(part, questions)
        if status == "OK":
            return status, body, "", "decisions", 0
        errors.append(f"Decisions {status} {error}")
    if jev is not None and chars > budget:
        cut = trim_jev_state(part, budget)
        status, body, error = jev.judge(part, questions)
        if status == "OK":
            return status, body, "", "jev-trimmed", cut
        errors.append(f"Jev(줄임) {status} {error}")
    return "ERROR", None, " / ".join(errors), None, 0


def trim_jev_state(state: Dict[str, Any], budget: int = JEV_TRIM_CHARS) -> int:
    """Jev 에 보낼 state 가 예산을 넘으면 ① 첨부 내용·참조 자료 ② 판정할 턴보다 앞 턴의 답변 순으로 앞 60%·뒤 40% 만 남긴다.
    판정할 턴의 질문·답변은 줄이지 않는다. 돌려주는 값은 줄인 글자 수(0 = 그대로)."""
    if not budget or js.state_chars(state) <= budget:
        return 0
    now = max(state.get("판정할_턴") or [0])
    trimmed = 0
    for pool in ("material", "history"):
        slots = []
        for t in state.get("턴") or []:
            if pool == "material":
                slots += [(a, "내용") for a in t.get("첨부파일") or [] if isinstance(a.get("내용"), str)]
                if isinstance(t.get("참조자료"), str):
                    slots.append((t, "참조자료"))
            elif t.get("턴") != now and isinstance(t.get("답변"), str):
                slots.append((t, "답변"))
        over = js.state_chars(state) - budget
        size = sum(len(h[k]) for h, k in slots)
        if over <= 0 or not size:
            continue
        for h, k in slots:   # 길이에 비례해 줄인다 — 조각마다 최소 400자는 남긴다
            keep = max(400, len(h[k]) - int(over * len(h[k]) / size) - 80)
            trimmed += _cut(h, k, keep)
    return trimmed


def verdict_of(items: Dict[int, Dict[str, Dict[str, Any]]]) -> str:
    """의미 평가 판정 — 확인 못 한(UNVERIFIED) 항목이 하나라도 있으면 HOLD, 아니면 FAIL 이 있으면 FAIL, 그 밖 PASS (사용자 10-21: 보류 우선)."""
    decs = [v.get("decision") for part in items.values() for v in part.values()]
    return "HOLD" if "UNVERIFIED" in decs else ("FAIL" if "FAIL" in decs else "PASS")


def question_verdict(code_fails: List[str], semantic: Optional[str]) -> str:
    """질문 판정 (사용자 10-21: 보류가 하나라도 있으면 최종 판정은 보류) — 의미 평가에 보류(확인 못 함 · 판정 실행 오류)가 있으면 HOLD,
    아니면 CODE · 의미 FAIL 이 하나라도 있으면 FAIL, 그 밖 PASS. 보류를 사람이 모두 정한 뒤에 FAIL 이 남으면 FAIL."""
    if semantic == "HOLD":
        return "HOLD"
    return "FAIL" if code_fails or semantic == "FAIL" else (semantic or "PASS")


# --- 질문 하나 ---------------------------------------------------------------------------------------------------------

def judge_one(q: int, provider: str, turns: List[Dict[str, Any]], answers: Dict[str, str], deliveries: Dict[str, str],
              code_fails: List[str], prompt: str, fixed: Dict[str, Any], gpt: Any, jev: Any,
              out: Path, reuse_codex: bool = False, decisions: Any = None) -> Dict[str, Any]:
    tag = f"Q{q} {provider}"
    doc = question_json(q, turns, answers, deliveries)
    text = json.dumps(doc, ensure_ascii=False, indent=1)
    name = f"Q{q}_{provider}.json"
    (out / "inputs" / name).write_text(text, encoding="utf-8")
    jev_items = list(fixed["items"])
    state = {"guide": fixed["guide"], **doc}
    questions = {f"T{ji._turn(r)}:{i}": fixed["items"][i] for r in turns for i in jev_items}
    calls: Dict[str, Any] = {}

    def run_codex() -> None:
        search = not any((t.get("reference") or "").strip() for t in turns)
        saved = out / "codex" / name
        if reuse_codex and saved.exists() and json.loads(saved.read_text(encoding="utf-8") or "null"):
            calls["codex"] = {"status": "OK", "error": "", "seconds": None, "search": search, "reused": True,
                              "output": json.loads(saved.read_text(encoding="utf-8"))}
            say(f"   🖥 {tag} Codex — 앞 시험의 출력 다시 씀 ({saved.name})")
            return
        t0 = time.monotonic()
        all_turns = [ji._turn(r) for r in turns]
        got: Dict[int, Any] = {}
        conclusions: List[Any] = []
        status, error, rounds = "ERROR", "Codex 없음", 0
        # 긴 대화는 Codex 가 "출력 공간이 부족하다"며 turns 를 비우고 보류 결론만 내거나(Q12 10턴, 2026-10-20), 앞 몇 턴만 답하고 만다(Q17).
        # → 한 번에 묻는 턴 수를 줄여 가며(전체 → 절반 → 1턴씩) 빠진 턴만 '판정할_턴' 으로 다시 묻는다. 호출은 턴 수 + 2 번까지.
        chunk = len(all_turns)
        while gpt and rounds < len(all_turns) + 2:
            pending = [n for n in all_turns if n not in got]
            if not pending:
                break
            ask = pending[:chunk]
            rounds += 1
            body = text if (rounds == 1 and chunk == len(all_turns)) else json.dumps(question_json(q, turns, answers, deliveries, ask), ensure_ascii=False, indent=1)
            say(f"   🖥 {tag} Codex CLI{'' if rounds == 1 else f' 다시({rounds}번째) — 턴 {ask}'} — 고정 프롬프트 v4 {len(prompt):,}자 + 입력 JSON {len(body):,}자"
                f" · 웹 검색 {'켬' if search else '끔'} …")
            status, output, error = gpt.judge(prompt, body, search, tag, output_schema=output_schema())
            if status != "OK":
                break
            new = 0
            for t in (output or {}).get("turns") or []:
                if isinstance(t, dict) and t.get("turn") in ask and t["turn"] not in got and (t.get("items") or t.get("situations")):
                    got[t["turn"]] = t
                    new += 1
            if new:
                conclusions.append((output or {}).get("conclusion"))
            if new < len(ask):   # 다 못 답했으면 다음엔 더 적게 묻는다
                chunk = max(1, min(len(ask) // 2, new or len(ask) // 2))
                if new == 0:
                    say(f"   ↪ {tag} Codex 가 턴 {ask} 를 답하지 않음 — {((output or {}).get('conclusion') or {}).get('reason', '')[:90]} → {chunk}턴씩 다시")
        missing = [n for n in all_turns if n not in got]
        output = {"turns": [got[n] for n in all_turns if n in got], "conclusion": conclusions[0] if conclusions else None,
                  "conclusions": conclusions} if got else None
        calls["codex"] = {"status": "OK" if got else status, "error": error if not got else (f"끝내 답하지 않은 턴 {missing}" if missing else ""),
                          "seconds": round(time.monotonic() - t0, 1), "search": search, "calls": rounds, "missing_turns": missing, "output": output}
        say(f"   🖥 {tag} Codex {calls['codex']['status']} {calls['codex']['seconds']}초 · 호출 {rounds}번"
            + (f" — {str(calls['codex']['error'])[:100]}" if calls['codex']['error'] else ""))

    def run_jev() -> None:
        # 턴마다 한 번씩 — 270문항(10턴 × 27)을 한 요청에 넣으면 Jev 가 HTTP 400 (Q17, 2026-10-20). 그 턴까지의 대화만 state 로 (뒤 턴은 판정에 안 씀)
        say(f"   🔎 {tag} Jev — J1~J4 {len(questions)}문항(턴 {len(turns)} × {len(jev_items)}, 턴마다 한 번) · 입력 {js.state_chars(state):,}자 …")
        t0 = time.monotonic()
        merged: Dict[str, Any] = {}
        errors: List[str] = []
        trimmed: Dict[str, int] = {}
        second: Dict[str, str] = {}   # 턴 → 둘째 심판(jev · decisions · jev-trimmed)
        for row in turns:
            n = ji._turn(row)
            # 판정할_턴 을 적어 준다 — 앞 턴(1·2턴 거절)에 끌려 3턴 이후 정상 답을 '거절' FAIL 로 본 일(Q17 Gemini, 10-20)
            part = json.loads(json.dumps({"guide": fixed["guide"], "질문번호": q, "판정할_턴": [n], "턴": [t for t in doc["턴"] if t["턴"] <= n]},
                                         ensure_ascii=False))
            qd = {f"T{n}:{i}": fixed["items"][i] for i in jev_items}
            status, body, error, used, cut = second_judge(part, qd, jev, decisions, prefer_decisions=turn_has_files(part, n))
            if cut:
                trimmed[str(n)] = cut
            if status == "OK":
                merged.update((body or {}).get("answers") or {})
                second[str(n)] = used or ""
            else:
                errors.append(f"T{n} {status} {str(error)[:120]}")
        status = "OK" if not errors else ("PARTIAL" if merged else "ERROR")
        calls["jev"] = {"status": status, "error": "; ".join(errors), "seconds": round(time.monotonic() - t0, 1), "calls": len(turns),
                        "trimmed": trimmed, "second": second, "body": {"answers": merged}}
        by_dec = [k for k, v in second.items() if v == "decisions"]
        say(f"   🔎 {tag} Jev {status} {calls['jev']['seconds']}초" + (f" · Decisions 로 본 턴 {by_dec}" if by_dec else "")
            + (f" · 줄인 턴 {list(trimmed)}" if trimmed else "") + (f" — {'; '.join(errors)[:160]}" if errors else ""))

    started = time.monotonic()
    threads = [threading.Thread(target=run_codex), threading.Thread(target=run_jev)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    (out / "codex" / name).write_text(json.dumps(calls["codex"].get("output"), ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "jev" / name).write_text(json.dumps(calls["jev"].get("body"), ensure_ascii=False, indent=1), encoding="utf-8")

    codex = check_codex(calls["codex"].get("output") if calls["codex"]["status"] == "OK" else None, turns, answers)
    jev_res = js.check_jev(calls["jev"].get("body"), {ji._turn(r): jev_items for r in turns}, fixed) if calls["jev"]["status"] in ("OK", "PARTIAL") else {}
    final: Dict[int, Dict[str, Dict[str, Any]]] = {}
    turn_out: Dict[str, Any] = {}
    agree = total = 0
    for row in turns:
        n = ji._turn(row)
        c_items, j_items = codex[n]["items"], jev_res.get(n, {})
        items: Dict[str, Dict[str, Any]] = {}
        for item in dict.fromkeys(list(c_items) + [i for i in jev_items if i in j_items]):
            cv, jv = c_items.get(item), j_items.get(item)
            if item in jev_items:
                used = (calls["jev"].get("second") or {}).get(str(n)) or "jev"
                if jv is not None:
                    jv["judge"] = used
                dec, how = combine_weighted(cv, jv, SECOND_LABEL.get(used, "Jev"), SECOND_JUDGE_CONFIDENT, str(n) in (calls["jev"].get("trimmed") or {}))
                if jv and (cv or {}).get("decision") is not None:
                    total += 1
                    agree += (jv.get("decision") == cv.get("decision"))
            else:
                dec, how = (cv or {}).get("decision"), "Codex"
            if dec is None:
                continue
            e = ((cv or {}).get("entries") or [{}])
            fail_e = next((x for x in e if x.get("decision") == "FAIL"), e[0])
            items[item] = {"decision": dec, "how": how, "codex": (cv or {}).get("decision"), "jev": (jv or {}).get("decision"),
                           "second_judge": (jv or {}).get("judge"), "jev_reason": (jv or {}).get("reason"), "problems": (cv or {}).get("problems") or [],
                           "reason": fail_e.get("reason"), "answer_quote": fail_e.get("answer_quote"), "question_quote": fail_e.get("question_quote"),
                           "entries": (cv or {}).get("entries") or []}
        js.apply_overrides(items)
        final[n] = items
        turn_out[str(n)] = {"situations_judge": codex[n]["situations"],
                            "evidence": codex[n]["evidence"], "problems": codex[n]["problems"], "items": items}
    semantic = verdict_of(final) if calls["codex"]["status"] == "OK" else "HOLD"
    verdict = question_verdict(code_fails, semantic)
    cs = [c for c in ((calls["codex"].get("output") or {}).get("conclusions") or [(calls["codex"].get("output") or {}).get("conclusion")]) if isinstance(c, dict)]         if isinstance(calls["codex"].get("output"), dict) else []
    concl = None if not cs else {"verdict": "FAIL" if any(c.get("verdict") == "FAIL" for c in cs) else ("HOLD" if any(c.get("verdict") == "HOLD" for c in cs) else "PASS"),
                                 "reason": " / ".join(c.get("reason") or "" for c in cs)}   # 턴을 나눠 물었으면 결론도 합친다
    line = {"version": VERSION, "provider": provider, "question": q, "turns": len(turns), "verdict": verdict, "semantic": semantic,
            "code_fails": code_fails, "codex_conclusion": concl, "jev_codex_agree": [agree, total], "seconds": round(time.monotonic() - started, 1),
            "calls": {k: {kk: vv for kk, vv in v.items() if kk not in ("output", "body")} for k, v in calls.items()}, "turn_results": turn_out}
    fails = [f"T{n}:{i}" for n, p in final.items() for i, v in p.items() if v["decision"] == "FAIL"]
    holds = [f"T{n}:{i}" for n, p in final.items() for i, v in p.items() if v["decision"] == "UNVERIFIED"]
    say(f"■ {tag}: {verdict} (Codex 결론 {(concl or {}).get('verdict', '-')}) · {line['seconds']:.0f}초"
        + (f" · CODE FAIL {' '.join(code_fails)}" if code_fails else "") + (f" · FAIL {' '.join(fails)}" if fails else "")
        + (f" · 보류 {' '.join(holds)}" if holds else ""))
    return line


# --- 보고 -------------------------------------------------------------------------------------------------------------

def report_md(lines: List[Dict[str, Any]]) -> str:
    md = [f"# 평가 결과 ({datetime.now().strftime('%Y-%m-%d %H:%M')})", "",
          "심판이 받은 것: 고정 프롬프트 v4 + 질문·첨부·참조 자료·답변 JSON (판정할 항목 목록 없음).", "",
          "| 질문 | 모델 | 턴 | 최종 | Codex 결론 | CODE FAIL | 둘째 심판·Codex 같은 판정 | Codex 초 | 둘째 심판 초 | 선별(항목→Codex) |", "|---|---|---|---|---|---|---|---|---|---|"]
    for l in lines:
        c = l.get("calls") or {}
        second = c.get("jev") or c.get("decisions") or {}
        sc = l.get("screen") or {}
        md.append(f"| Q{l['question']} | {l['provider']} | {l['turns']} | {l['verdict']} | {(l.get('codex_conclusion') or {}).get('verdict', '-')} | "
                  f"{' '.join(l.get('code_fails') or []) or '-'} | {(l.get('jev_codex_agree') or [0, 0])[0]}/{(l.get('jev_codex_agree') or [0, 0])[1]} | "
                  f"{(c.get('codex') or {}).get('seconds', '-')} | {second.get('seconds', '-')} | {f'{sc.get(chr(105)+chr(116)+chr(101)+chr(109)+chr(115))}→{sc.get(chr(102)+chr(108)+chr(97)+chr(103)+chr(103)+chr(101)+chr(100))}' if sc else '-'} |")
    for l in lines:
        md += ["", f"## Q{l['question']} {l['provider']} — {l['verdict']}", f"Codex 결론 이유: {(l['codex_conclusion'] or {}).get('reason', '-')}"]
        for n, t in l["turn_results"].items():
            md.append(f"- {n}턴 심판이 찾은 상황: {' '.join(t['situations_judge'])}")
            for sid, ev in (t.get("evidence") or {}).items():
                if ev:
                    md.append(f"  - {sid} 근거: 「{ev[:120]}」")
            for p in t["problems"]:
                md.append(f"  - 출력 문제: {p}")
            for item, v in t["items"].items():
                if v["decision"] in ("FAIL", "UNVERIFIED"):
                    md.append(f"  - {KO[v['decision']]} {item} ({v['how']}) — {v.get('reason') or ''}"
                              + (f" 「{(v.get('answer_quote') or '')[:100]}」" if v.get("answer_quote") else "")
                              + (f" / Jev: {v['jev_reason'][:100]}" if v["decision"] == "UNVERIFIED" and v.get("jev_reason") else "")
                              + (f" / 문제: {'; '.join(v['problems'])[:120]}" if v.get("problems") else ""))
    return "\n".join(md) + "\n"


PROVIDERS = ("gpt", "claude", "gemini", "aiona")
LABEL = {"gpt": "GPT", "claude": "Claude", "gemini": "Gemini", "aiona": "AIONA"}
IDS78 = ROOT / "참고자료" / "질문세트_최소78" / "ids.json"
_LOG: Dict[str, Any] = {"path": None}
_LOG_LOCK = threading.Lock()


def log_say(text: str) -> None:
    """터미널에 찍고 로그 파일에도 남긴다 — Codex 실행기(judge_run)의 진행 글도 이 함수로 모인다."""
    print(text, flush=True)
    if _LOG["path"]:
        with _LOG_LOCK, Path(_LOG["path"]).open("a", encoding="utf-8") as f:
            f.write(text + "\n")


class Board:
    """모델마다 '지금 몇 번째 질문을 평가 중인지 / 전체' — 시작·끝날 때마다 다시 찍는다 (사용자 10-20)."""

    def __init__(self, totals: Dict[str, int], done: Dict[str, int]) -> None:
        self.totals, self.done = totals, dict(done)
        self.now: Dict[str, Optional[int]] = {p: None for p in totals}
        self.lock = threading.Lock()

    def _state(self, p: str) -> str:
        total = self.totals[p]
        if self.now[p] is not None:
            return f"{min(self.done[p] + 1, total)}/{total}  (Q{self.now[p]} 평가 중)"
        return f"{self.done[p]}/{total}  ({'끝' if self.done[p] >= total else '대기'})"

    def show(self) -> None:
        lines = [f"── 평가 현황 {datetime.now():%H:%M:%S} ──"] + [f"  {LABEL.get(p, p):<7}: {self._state(p)}" for p in self.totals]
        log_say("\n".join(lines))

    def start(self, p: str, q: int) -> None:
        # 시작은 그 모델 한 줄만 — 네 모델이 같은 초에 시작하면 같은 판이 다섯 번 쌓였다 (사용자 10-20 밤). 전체 판은 질문이 끝날 때마다.
        with self.lock:
            self.now[p] = q
            log_say(f"▶ {LABEL.get(p, p):<7}: {self._state(p)}")

    def finish(self, p: str) -> None:
        with self.lock:
            self.now[p] = None
            self.done[p] += 1
            self.show()


def pre_status(turns: List[Dict[str, Any]], data: Dict[str, Any], answers: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """심판에게 보내지 않고 정하는 경우 — 판정 v3 와 같은 규칙(tools/judge_inputs.empty_output_entry, 사용자 규칙 10-20).
    빈 출력 요구: 복사할 답이 없으면 보류 · 글을 쓰면 FAIL / 복사 실패·빈 답: 보류 / 답을 못 받은 턴: FAIL."""
    from webapp.run_summary import asks_empty_output
    note = "; ".join(f"{t['case_id']}: {ji.collection_note(data.get(t['case_id']))}" for t in turns)
    if any(asks_empty_output(None, t.get("question")) for t in turns):
        return ji.empty_output_entry(turns, {t["case_id"]: data.get(t["case_id"]) for t in turns}, answers)
    statuses = [((data.get(t["case_id"]) or {}).get("collection") or {}).get("status") for t in turns]
    if "COPY_FAILED" in statuses:
        return {"status": "held_copy_failed", "reason": "답을 복사해 오지 못함 — 보류", "collection_note": note}
    if not all(ji.collected(data.get(t["case_id"])) for t in turns):
        return {"status": "no_answer", "reason": "답을 받지 못한 턴이 있음 — FAIL 로 셈", "collection_note": note}
    if any(not (answers.get(t["case_id"]) or "").strip() for t in turns):
        return {"status": "held_copy_failed", "reason": "받은 답이 비어 있음 — 보류", "collection_note": note}
    return None


PRE_VERDICT = {"held_empty_output": "HOLD", "held_copy_failed": "HOLD", "empty_output_fail": "FAIL", "no_answer": "FAIL", "empty_output_pass": "PASS"}


def recode(run: Dict[str, Any], rows: List[Dict[str, Any]], results_path: Path, providers: set, questions: set) -> int:
    """--recode: 심판을 다시 부르지 않고, 실행 파일의 지금 CODE 평가와 저장된 항목 판정으로 code_fails · semantic · verdict 를 지금 규칙으로
    다시 계산해 바뀐 줄을 덧붙인다 (CODE 기준을 바꾼 뒤 — 10-21 코드블록 언어 표지, 질문 판정 규칙을 바꾼 뒤 — 10-21 보류 우선)."""
    latest: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for x in results_path.read_text(encoding="utf-8").splitlines():
        if x.strip():
            l = json.loads(x)
            if l.get("version") in (VERSION, SCREEN_VERSION):
                latest[(l["provider"], int(l["question"]))] = l
    changed = 0
    with results_path.open("a", encoding="utf-8") as f:
        for (p, q), l in sorted(latest.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            if p not in providers or q not in questions or l.get("status") != "judged":
                continue
            cases = {c.get("case_id"): c for c in ((run.get("evaluations") or {}).get(p) or {}).get("cases") or [] if isinstance(c, dict)}
            turns = [r for r in rows if ji._question(r["case_id"]) == q]
            code_fails = sorted({c for t in turns for c in question_outcome(cases.get(t["case_id"]), None).get("code_fails") or []})
            semantic = l.get("semantic") or ("FAIL" if l.get("verdict") == "FAIL" and not l.get("code_fails") else l.get("verdict"))
            tr = l.get("turn_results") or {}
            if tr and not l.get("judge_error"):   # 의미 판정도 지금 규칙으로 다시 계산 (10-21: 보류 우선)
                semantic = verdict_of({int(k): (v.get("items") or {}) for k, v in tr.items()})
            verdict = question_verdict(code_fails, semantic)
            if code_fails == sorted(l.get("code_fails") or []) and verdict == l.get("verdict") and semantic == l.get("semantic"):
                continue
            new = {**l, "code_fails": code_fails, "semantic": semantic, "verdict": verdict,
                   "recoded": {"at": datetime.now(timezone.utc).isoformat(), "code_fails_before": l.get("code_fails"), "verdict_before": l.get("verdict"),
                               "semantic_before": l.get("semantic")}}
            f.write(json.dumps(new, ensure_ascii=False) + "\n")
            changed += 1
            say(f"↻ Q{q} {p}: CODE FAIL {l.get('code_fails') or '-'} → {code_fails or '-'} · 판정 {l.get('verdict')} → {verdict}")
    say(f"다시 계산 끝 — 바뀐 줄 {changed}개 (심판 호출 없음)")
    sp = results_path.parent / "summary.json"
    if changed and sp.exists():   # summary.json 의 모델별 숫자도 지금 판정으로
        final: Dict[Tuple[str, int], Dict[str, Any]] = {}
        for x in results_path.read_text(encoding="utf-8").splitlines():
            if x.strip():
                l = json.loads(x)
                if l.get("version") in (VERSION, SCREEN_VERSION):
                    final[(l["provider"], int(l["question"]))] = l
        sm = json.loads(sp.read_text(encoding="utf-8"))
        sm["models"] = {p: {**{v: sum(1 for (pp, _q), l in final.items() if pp == p and l.get("verdict") == v) for v in ("PASS", "FAIL", "HOLD")},
                            "n": sum(1 for (pp, _q) in final if pp == p)} for p in sorted({k[0] for k in final})}
        sp.write_text(json.dumps(sm, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--pick", help="질문:모델 쉼표 목록 — 예: 143:claude,115:aiona (없으면 --questions)")
    ap.add_argument("--set", default=str(SET_DIR), help="질문 세트 폴더 (questions.json) — 기본은 326 세트")
    ap.add_argument("--questions", default="all", help="'all' = 세트의 모든 질문, '78' = 질문세트_최소78/ids.json, 또는 질문 번호 쉼표 목록")
    ap.add_argument("--providers", default=",".join(PROVIDERS))
    ap.add_argument("--out", default="v4-78", help="결과 폴더 이름 (data/judge_runs/<실행>/<이름>)")
    ap.add_argument("--reuse-codex", action="store_true", help="저장된 Codex 출력이 있으면 다시 부르지 않고 쓴다 (Jev 만 다시)")
    ap.add_argument("--show-thinking", action="store_true", help="Codex 생각 요약 줄도 찍기 (네 모델 동시면 많음)")
    ap.add_argument("--dry", action="store_true", help="심판을 부르지 않고 질문마다 어떻게 처리할지만 보기")
    ap.add_argument("--codex-all", action="store_true", help="교정용 — 걸린 항목만이 아니라 모든 항목을 Codex 에도 보내 일치율을 잰다")
    ap.add_argument("--recode", action="store_true", help="심판을 부르지 않고, 지금 실행 파일의 CODE 평가 결과로 판정 줄의 CODE FAIL·질문 판정만 다시 계산해 덧붙인다(CODE 기준을 바꾼 뒤)")
    ap.add_argument("--redo", action="store_true", help="--questions 로 고른 질문은 이미 판정했어도 처음부터 다시 판정한다(새 줄을 덧붙임, 보고서는 마지막 줄을 씀)")
    ap.add_argument("--repair", action="store_true", help="이미 판정한 줄을 저장된 Decisions·Codex 출력으로 다시 합치고, Codex 가 답하지 않았거나 인용이 틀린 항목만 다시 묻는다")
    args = ap.parse_args(argv)

    import tools.judge_run as jr
    jr.say = log_say   # Codex 실행기의 진행 글도 같은 곳으로
    jr.SHOW_THINKING = args.show_thinking
    global say
    say = log_say

    run = json.loads((ROOT / "data" / "comparison_runs" / f"{args.run}.json").read_text(encoding="utf-8"))
    set_dir = Path(args.set)
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    QUESTION_TARGETS.clear()
    QUESTION_TARGETS.update(question_targets(groups))   # 질문별 목표 기준 (10-21 저녁)
    rows = flatten(groups)
    prompt = PROMPT_V4.read_text(encoding="utf-8")
    fixed = json.loads(JEV_V4.read_text(encoding="utf-8"))
    out = ROOT / "data" / "judge_runs" / args.run / args.out
    # 판정 방식은 하나(선별: Decisions 가 항목 전부 판정 → 걸린 항목만 Codex 확인) — 모드 선택 없음 (사용자 결정 2026-10-20 밤)
    for sub in ("inputs", "codex", "jev", "decisions"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    _LOG["path"] = str(out / "run.log")

    if args.pick:
        plan = [(int(x.split(":")[0]), x.split(":")[1]) for x in args.pick.split(",") if x.strip()]
    else:
        qs = ([int(g["id"]) for g in groups] if args.questions == "all" else
              json.loads(IDS78.read_text(encoding="utf-8"))["ids"] if args.questions == "78" else [int(x) for x in args.questions.split(",") if x.strip()])
        plan = [(q, p) for p in args.providers.split(",") for q in qs]
    results_path = out / "results.jsonl"
    if args.recode:
        return recode(run, rows, results_path, {p for _, p in plan}, {q for q, _ in plan})
    done = set()
    if results_path.exists():
        for x in results_path.read_text(encoding="utf-8").splitlines():
            if x.strip():
                l = json.loads(x)
                if l.get("version") in (VERSION, SCREEN_VERSION) and not l.get("judge_error"):   # 선별(v5) 줄도 이미 한 건 (Codex 지적 10-20) · 판정 실행 오류는 다시
                    if (args.repair or args.redo) and l.get("status") == "judged":
                        continue   # --repair: 판정한 줄은 저장된 심판 출력으로 다시 합친다 · --redo: 처음부터 다시
                    done.add((l["provider"], int(l["question"])))
    queues: Dict[str, List[int]] = {}
    for q, p in plan:
        queues.setdefault(p, []).append(q)
    totals = {p: len(v) for p, v in queues.items()}
    board = Board(totals, {p: sum(1 for q in v if (p, q) in done) for p, v in queues.items()})
    gpt = jev = decisions = None
    if not args.dry:
        gpt, jev, notes = judges_from_env()
        if gpt is not None:
            gpt.files_dir = set_dir / "files"   # 첨부 원본(이미지·pdf·docx 쪽 이미지)을 -i 로
            gpt.io_dir = out / "codex_io"   # Codex 입력 · 결과 파일 (10-21 저녁: 파일로 읽고 파일로 결과)
        from code_eval import attachment_pages as ap
        if gpt is not None or decisions is not None:
            for name, note in ap.prerender(set_dir / "files").items():   # 스레드 전에 한 번 그려 둔다 (Word 는 한 번에 하나)
                say(f"   📎 첨부 쪽 이미지 — {note}")
        for n in notes:
            say(f"⚠ {n}")
        from tools.judge_run import decisions_from_env
        decisions, d_reason = decisions_from_env()
        if decisions is not None:
            decisions.client.files_dir = set_dir / "files"   # 이미지 첨부 원본을 input_image 로
        if decisions is None:
            say(f"⚠ Decisions 심판 없음 — {d_reason}")
    write_lock = threading.Lock()

    def save(line: Dict[str, Any]) -> None:
        with write_lock, results_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({**line, "at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")

    def work(provider: str) -> None:
        data = {r.get("case_id"): r for r in (run.get("datasets") or {}).get(provider) or [] if isinstance(r, dict)}
        cases = {c.get("case_id"): c for c in ((run.get("evaluations") or {}).get(provider) or {}).get("cases") or [] if isinstance(c, dict)}
        for q in queues[provider]:
            if (provider, q) in done:
                continue
            turns = sorted([r for r in rows if ji._question(r["case_id"]) == q], key=ji._turn)
            answers = {t["case_id"]: (data.get(t["case_id"]) or {}).get("answer") or "" for t in turns}
            pre = pre_status(turns, data, answers)
            board.start(provider, q)
            if pre is not None:
                line = {"version": VERSION, "provider": provider, "question": q, "turns": len(turns), "status": pre["status"],
                        "verdict": PRE_VERDICT.get(pre["status"], "HOLD"), "reason": pre.get("reason"), "collection_note": pre.get("collection_note")}
                say(f"■ Q{q} {provider}: {KO.get('UNVERIFIED') if line['verdict'] == 'HOLD' else line['verdict']} (심판 없이) — {pre.get('reason')}")
            elif args.dry:
                line = None
                say(f"· Q{q} {provider}: 심판에게 보냄 ({len(turns)}턴)")
            else:
                deliveries = {t["case_id"]: ji.delivery_note(data.get(t["case_id"])) for t in turns}
                code_fails = sorted({c for t in turns for c in question_outcome(cases.get(t["case_id"]), None).get("code_fails") or []})
                try:
                    line = {**screen_one(q, provider, turns, answers, deliveries, code_fails, prompt, fixed, gpt, decisions, out, args.codex_all,
                                        args.reuse_codex or args.repair, reuse_decisions=args.repair),
                            "status": "judged"}
                except Exception as exc:   # 한 질문의 오류로 그 모델 전체가 멈추지 않게 — 기록하고 다음 질문으로
                    line = {"version": VERSION, "provider": provider, "question": q, "turns": len(turns), "status": "error", "verdict": "HOLD",
                            "reason": f"판정 실행 오류: {type(exc).__name__}: {str(exc)[:200]}"}
                    say(f"⚠ Q{q} {provider}: {line['reason']}")
            if line is not None and not args.dry:
                save(line)
            board.finish(provider)

    say(f"평가 진행합니다 — {sum(totals.values())}건(질문 × 모델, 이미 한 {sum(board.done.values())}건 건너뜀) · 모델마다 한 줄로 동시에"
        + (" · 점검만(심판 안 부름)" if args.dry else "") + f" · 로그 {out / 'run.log'}")
    board.show()
    wall0 = time.monotonic()
    threads = [threading.Thread(target=work, args=(p,), name=f"judge-{p}") for p in queues]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = round(time.monotonic() - wall0, 1)
    if args.dry:
        return 0
    latest: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for x in results_path.read_text(encoding="utf-8").splitlines():
        if x.strip():
            l = json.loads(x)
            if l.get("version") in (VERSION, SCREEN_VERSION):
                latest[(l["provider"], int(l["question"]))] = l   # 같은 (모델, 질문) 은 마지막 줄 (다시 판정한 것)
    lines = list(latest.values())
    with (out / "timing.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "judge-v4", "at": datetime.now(timezone.utc).isoformat(), "seconds": wall,
                            "judgments": sum(totals.values()) - sum(1 for k in done if k[1] in set(sum(queues.values(), []))),
                            **({"redo": True} if args.redo else {})},   # 다시 판정 — 보고서 판정 시간·실행 기록에서 뺀다 (사용자 10-21)
                           ensure_ascii=False) + "\n")
    judge_s = sum(r.get("seconds") or 0 for r in (json.loads(x) for x in (out / "timing.jsonl").read_text(encoding="utf-8").splitlines() if x.strip())
                  if str(r.get("kind") or "").startswith("judge") and not r.get("redo"))
    summary = {"version": VERSION, "run_id": args.run, "prompt": PROMPT_V4.name, "jev_questions": fixed.get("version"),
               "codex": {"model": getattr(gpt, "model", None), "reasoning": getattr(gpt, "reasoning", None)} if gpt is not None else None,
               "finished_at": datetime.now(timezone.utc).isoformat(), "wall_seconds": round(judge_s, 1), "models": {}}   # 폴더 전체 판정 시간(다시 판정 뺌)
    say("\n결과 — 질문 단위 (보류는 사람이 정함)")
    for p in queues:
        mine = [l for l in lines if l["provider"] == p and int(l["question"]) in set(queues[p])]
        c = {v: sum(1 for l in mine if l.get("verdict") == v) for v in ("PASS", "FAIL", "HOLD")}
        say(f"  {LABEL.get(p, p):<7}: PASS {c['PASS']} · FAIL {c['FAIL']} · 보류 {c['HOLD']}  / {len(queues[p])}")
    for p in sorted({l["provider"] for l in lines}):   # summary 는 이 폴더 전체 — 몇 질문만 다시 판정해도 전체 숫자가 남는다 (10-21)
        allp = [l for l in lines if l["provider"] == p]
        summary["models"][p] = {**{v: sum(1 for l in allp if l.get("verdict") == v) for v in ("PASS", "FAIL", "HOLD")}, "n": len(allp)}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "report.md").write_text(report_md([l for l in lines if l.get("status") == "judged"]), encoding="utf-8")
    say(f"\n평가 끝 — 벽시계 {wall / 60:.0f}분 · 결과 {results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
