# -*- coding: utf-8 -*-
"""기준 재확인 (criteria-consistency-v1) — 기준 정의가 적힌 곳이 서로 같은 말을 하는지 모델 호출 없이 대조한다 (사용자 10-25 밤: "질문 생성 프롬프트 ·
평가기준 · 평가프롬프트 안의 판단 기준들을 통틀어 계속 재확인하면서 개선").

대조하는 곳 — LLM 기준 44개:
  ① 기준 v2 문서 6절 표(점검 질문 · 적용 · 경계 · 예외)   ② 판정 프롬프트 v4(Codex 가 읽는 글: 항목 줄 · 적용 · 덧붙인 규칙)
  ③ 코드 code_eval/semantic_items.py(1차 심판 J5~J8 · 적용 조건의 원본)   ④ 1차 항목 정의 jev_questions_v4.json(J1~J4)
CODE 기준 28개: 문서 5절 표의 번호 · 이름 ↔ code_eval.criteria_registry 의 이름.

    python -X utf8 tools/criteria_consistency.py            # 보고를 화면에 (어긋남이 있으면 종료 코드 1)
    python -X utf8 tools/criteria_consistency.py --out 참고자료/분석/2026-10-25-기준재확인/보고.md

판정: '어긋남'(hard) = 같은 항목의 점검 질문 · 적용 조건이 다른 뜻 / 항목 수 · 번호가 다름. '덜 적힘'(soft) = 문서의 경계 · 예외가 프롬프트에 없거나
프롬프트의 규칙이 문서에 없음(한쪽에만 있는 규칙은 심판과 사람이 다른 기준을 보게 되므로 고칠 후보). 글자 겹침(2-gram Jaccard)으로 재니 표현만 다른 것은 통과.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VERSION = "criteria-consistency-v1"
DOC = ROOT / "참고자료" / "기준" / "채팅품질 평가기준_v2.md"
GEN_PROMPT = ROOT / "참고자료" / "프롬프트" / "질문생성_프롬프트.md"
CATALOG = ROOT / "참고자료" / "질문세트_78" / "criteria_catalog.json"
ID_RE = re.compile(r"J[1-8]-\d\.[a-z]|(?<![\w.])[123]-[1-3]\.[a-z_]+")   # 기준 id — J1-1.a · 2-2.table 꼴(날짜 10-25 와 구별)
PROMPT = ROOT / "참고자료" / "프롬프트" / "judge_prompt_v4.txt"
JEV = ROOT / "참고자료" / "프롬프트" / "jev_questions_v4.json"
NOTE_SKIP = ("적용", "근거", "표시")   # 규칙이 아닌 줄(예시 줄은 ' 예' 로 끝남)
Q_SAME, APPLIES_SAME, CODE_PROMPT_SAME, COVER_MIN, COVER_MIN_DOC = 0.6, 0.35, 0.8, 0.5, 0.4   # 문서는 프롬프트 규칙을 짧게 바꿔 적으니 문서 쪽 덮임 기준은 낮춤


# ------------------------------------------------------------------ 읽기 ------------------------------------------------------------------
def norm(s: str) -> str:
    s = re.sub(r"[\s·•]+", "", s or "")
    return re.sub(r"[\"'“”‘’()（）\[\]{}.,;:!?~…\-—/]", "", s).lower()


def grams(s: str) -> set:
    n = norm(s)
    return {n[i:i + 2] for i in range(len(n) - 1)} if len(n) > 1 else ({n} if n else set())


def sim(a: str, b: str) -> float:
    A, B = grams(a), grams(b)
    return len(A & B) / len(A | B) if A and B else 0.0


def cover(piece: str, pool: List[str]) -> float:
    """piece 의 2-gram 가운데 pool 의 어느 글에 들어 있는 비율(최대) — 문서 규칙이 프롬프트 어딘가에 있는지."""
    P = grams(piece)
    if not P:
        return 1.0
    return max((len(P & grams(t)) / len(P) for t in pool), default=0.0)


def parse_doc(text: str) -> Dict[str, Dict[str, str]]:
    """6절 표 — | **J1-1.a** 이름 | 점검 질문 | 적용 · 해당 없음 | 경계 · 예외 |"""
    out: Dict[str, Dict[str, str]] = {}
    for line in text.splitlines():
        m = re.match(r"\| \*\*(J\d-\d\.[a-z])\*\* ([^|]*)\| ([^|]*)\| ([^|]*)\| ([^|]*)\|", line)
        if m:
            out[m.group(1)] = {"name": m.group(2).strip(), "question": m.group(3).strip(), "applies": m.group(4).strip(), "boundary": m.group(5).strip()}
    return out


def parse_doc_code(text: str) -> Dict[str, str]:
    """5절 CODE 표 — | 1-1 응답 존재 · 문자 | … 첫 칸의 번호와 이름(번호 뒤 글)."""
    out: Dict[str, str] = {}
    i = text.find("## 5. CODE 기준 28")
    j = text.find("\n## 6.", i)
    for line in text[i:j].splitlines():
        m = re.match(r"\| (\d-\d) ([^|]*)\|", line)
        if m:
            out.setdefault(m.group(1), m.group(2).strip())
    return out


def parse_prompt(text: str) -> Dict[str, Dict[str, Any]]:
    """판정 프롬프트 항목 블록 — '- **J1-1.a** · 점검 질문' 과 '  · 열쇠: 값' 줄들."""
    out: Dict[str, Dict[str, Any]] = {}
    cur: Optional[str] = None
    for line in text.splitlines():
        m = re.match(r"- \*\*(J\d-\d\.[a-z])\*\* · (.*)", line)
        if m:
            cur = m.group(1)
            out[cur] = {"question": m.group(2).strip(), "notes": {}, "rules": []}
            continue
        if cur and line.startswith("  · "):
            key, _, val = line[4:].partition(":")
            key, val = key.strip(), val.strip()
            out[cur]["notes"][key] = val
            if key not in NOTE_SKIP and not key.endswith(" 예") and not key.startswith(("PASS", "FAIL")):
                out[cur]["rules"].append(f"{key}: {val}")
        elif cur and not line.startswith("  "):
            cur = None
    return out


def parse_jev(doc: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """1차 항목 정의 — '[J1-1.a] 이름 — 점검 질문 / 적용: … / 근거: … / FAIL 예: …'"""
    out: Dict[str, Dict[str, str]] = {}
    for iid, q in (doc.get("items") or {}).items():
        ins = str(q.get("instructions") or "")
        head, *rest = ins.split(" / ")
        m = re.match(r"\[(J\d-\d\.[a-z])\] [^—]*— (.*)", head)
        fields = {k.strip(): v.strip() for k, _, v in (p.partition(":") for p in rest) if k.strip()}
        out[iid] = {"question": (m.group(2) if m else head).strip(), "applies": fields.get("적용", ""), "evidence": fields.get("근거", "")}
    return out


def semantic() -> Dict[str, Dict[str, str]]:
    from code_eval import semantic_items as si
    return {it.id: {"question": it.question, "applies": it.applies or "", "evidence": it.evidence or ""} for it in si.ITEMS}


def registry_code_names() -> Dict[str, str]:
    from code_eval import criteria_registry as cr
    out: Dict[str, str] = {}
    for e in cr._code_entries():
        cid = str(e.get("id") or "")
        m = re.match(r"(\d-\d)", cid)
        if m:
            out.setdefault(m.group(1), str(e.get("name") or e.get("title") or ""))
    return out


# ------------------------------------------------------------------ 대조 ------------------------------------------------------------------
def audit(doc_text: Optional[str] = None, prompt_text: Optional[str] = None, jev_doc: Optional[Dict[str, Any]] = None,
          sem: Optional[Dict[str, Dict[str, str]]] = None, code_names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    doc_text = doc_text if doc_text is not None else DOC.read_text(encoding="utf-8")
    prompt_text = prompt_text if prompt_text is not None else PROMPT.read_text(encoding="utf-8")
    D = parse_doc(doc_text)
    P = parse_prompt(prompt_text)
    prompt_lines = [l.strip(" -·|") for l in prompt_text.splitlines() if len(norm(l)) > 10]   # 규칙이 다른 절(상황표 · 판정 방법)에 있어도 찾게
    doc_lines = [l.strip(" -·|") for l in doc_text.splitlines() if len(norm(l)) > 10]
    J = parse_jev(jev_doc if jev_doc is not None else json.loads(JEV.read_text(encoding="utf-8")))
    S = sem if sem is not None else semantic()
    hard: List[Dict[str, Any]] = []
    soft: List[Dict[str, Any]] = []
    ids = sorted(set(D) | set(P) | set(S))
    if not (set(D) == set(P) == set(S)):
        hard.append({"kind": "항목 집합", "detail": f"문서 {len(D)} · 프롬프트 {len(P)} · 코드 {len(S)} — 문서에만 {sorted(set(D) - set(P) - set(S))} · 프롬프트에만 {sorted(set(P) - set(D))} · 코드에만 {sorted(set(S) - set(P))}"})
    for iid in ids:
        d, p, s, j = D.get(iid), P.get(iid), S.get(iid), J.get(iid)
        if not (d and p and s):
            continue
        q_dp = max(sim(d["question"], p["question"]), cover(d["question"], [p["question"]] + list(p["notes"].values())))   # 프롬프트는 괄호 설명이 붙어 길다
        q_ps = sim(p["question"], s["question"])
        if q_dp < Q_SAME:
            hard.append({"kind": "점검 질문", "item": iid, "detail": f"문서 ↔ 프롬프트 겹침 {q_dp:.2f}", "doc": d["question"], "prompt": p["question"]})
        if q_ps < CODE_PROMPT_SAME:
            hard.append({"kind": "점검 질문", "item": iid, "detail": f"프롬프트 ↔ 코드 겹침 {q_ps:.2f}", "prompt": p["question"], "code": s["question"]})
        pa = p["notes"].get("적용", "")
        if pa and s["applies"] and sim(pa, s["applies"]) < CODE_PROMPT_SAME:
            hard.append({"kind": "적용 조건", "item": iid, "detail": f"프롬프트 ↔ 코드 겹침 {sim(pa, s['applies']):.2f}", "prompt": pa, "code": s["applies"]})
        if pa and d["applies"] and sim(pa, d["applies"]) < APPLIES_SAME and cover(d["applies"], [pa, p["question"]]) < 0.5:
            soft.append({"kind": "적용 조건(문서 ↔ 프롬프트)", "item": iid, "detail": f"겹침 {sim(pa, d['applies']):.2f}", "doc": d["applies"], "prompt": pa})
        if j and sim(j["question"], s["question"]) < CODE_PROMPT_SAME:
            hard.append({"kind": "1차 항목 정의", "item": iid, "detail": f"jev ↔ 코드 겹침 {sim(j['question'], s['question']):.2f}", "jev": j["question"], "code": s["question"]})
        for piece in re.split(r"\. (?=[가-힣A-Z'\"“])", d["boundary"]):
            piece = piece.strip(" .")
            if len(norm(piece)) < 8 or piece == "—":
                continue
            c = cover(piece, prompt_lines)
            if c < COVER_MIN:
                soft.append({"kind": "문서에만 있는 경계 · 예외", "item": iid, "detail": f"프롬프트 덮임 {c:.2f}", "doc": piece})
        for rule in p["rules"]:
            body = rule.split(":", 1)[1] if ":" in rule else rule
            body = re.sub(r"\(20\d\d-\d\d-\d\d[^)]*\)", "", body)   # 날짜 꼬리표는 문서에 없음
            c = cover(body, doc_lines)
            if c < COVER_MIN_DOC:
                soft.append({"kind": "프롬프트에만 있는 규칙", "item": iid, "detail": f"문서 덮임 {c:.2f}", "prompt": rule})
    # CODE 28 — 번호 · 이름
    dc = parse_doc_code(doc_text)
    rc = code_names if code_names is not None else registry_code_names()
    if set(dc) != set(rc):
        hard.append({"kind": "CODE 묶음 번호", "detail": f"문서 {sorted(dc)} ↔ 코드 {sorted(rc)}"})
    for k in sorted(set(dc) & set(rc)):
        if sim(dc[k], rc[k]) < 0.3:
            soft.append({"kind": "CODE 묶음 이름", "item": k, "detail": f"겹침 {sim(dc[k], rc[k]):.2f}", "doc": dc[k], "code": rc[k]})
    gen = audit_generation_prompt() if GEN_PROMPT.exists() and CATALOG.exists() else None
    if gen:
        if gen["unknown"]:
            hard.append({"kind": "질문생성 프롬프트의 없는 기준 id", "detail": ", ".join(gen["unknown"])})
        if gen["missing_countable"]:
            soft.append({"kind": "질문생성 프롬프트에 안 나오는 기준", "detail": f"질문으로 셀 수 있는 {gen['countable']}개 중 {len(gen['missing_countable'])}개", "doc": ", ".join(gen["missing_countable"])})
    return {"version": VERSION, "items": len(ids), "hard": hard, "soft": soft, "generation_prompt": gen,
            "counts": {"doc": len(D), "prompt": len(P), "code": len(S), "jev": len(J), "code_doc": len(dc), "code_registry": len(rc)}}


def audit_generation_prompt(text: Optional[str] = None, catalog: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """질문생성 프롬프트가 말하는 기준 id — 없는 id(오타 · 옛 이름)는 어긋남, 질문으로 셀 수 있는 기준(72 − 답에 따라 보는 8 = 64) 가운데 한 번도 안 나오는 것은 덜 적힘."""
    text = text if text is not None else GEN_PROMPT.read_text(encoding="utf-8")
    cat = catalog if catalog is not None else json.loads(CATALOG.read_text(encoding="utf-8"))
    known = {e["id"] for e in (cat.get("catalog") or {}).get("code") or []} | {e["id"] for e in (cat.get("catalog") or {}).get("items") or []}
    countable = known - set(cat.get("answer_dependent_ids") or [])
    used = set(ID_RE.findall(text))
    unknown = sorted(used - known)
    missing = sorted(countable - used)
    return {"used": len(used & known), "unknown": unknown, "missing_countable": missing, "countable": len(countable)}


def to_md(res: Dict[str, Any]) -> str:
    c = res["counts"]
    L = [f"# 기준 재확인 — {res['version']}", "",
         f"- LLM 항목: 문서 {c['doc']} · 프롬프트 {c['prompt']} · 코드 {c['code']} · 1차 정의 {c['jev']}(J1~J4) / CODE 묶음: 문서 {c['code_doc']} · 코드 {c['code_registry']}",
         f"- 어긋남(hard) {len(res['hard'])} · 덜 적힘(soft) {len(res['soft'])}"
         + (f" · 질문생성 프롬프트: 아는 기준 id {res['generation_prompt']['used']}개 언급, 없는 id {len(res['generation_prompt']['unknown'])}, 안 나오는 셀 수 있는 기준 {len(res['generation_prompt']['missing_countable'])}" if res.get("generation_prompt") else ""), ""]
    if res["hard"]:
        L += ["## 어긋남 — 같은 항목이 다른 뜻", "", "| 종류 | 항목 | 설명 | 한쪽 | 다른 쪽 |", "|---|---|---|---|---|"]
        for h in res["hard"]:
            vals = [h.get(k) for k in ("doc", "prompt", "code", "jev") if h.get(k)]
            L.append(f"| {h['kind']} | {h.get('item', '')} | {h['detail']} | {(vals[0] if vals else '')[:120]} | {(vals[1] if len(vals) > 1 else '')[:120]} |".replace("\n", " "))
    if res["soft"]:
        L += ["", "## 덜 적힘 — 한쪽에만 있는 규칙 (심판과 사람이 다른 기준을 보게 됨)", "", "| 종류 | 항목 | 설명 | 글 |", "|---|---|---|---|"]
        for s_ in res["soft"]:
            text = s_.get("doc") or s_.get("prompt") or s_.get("code") or ""
            other = (" ↔ " + (s_.get("prompt") or s_.get("code") or "")[:80]) if s_["kind"].startswith(("적용", "CODE")) else ""
            L.append(f"| {s_['kind']} | {s_.get('item', '')} | {s_['detail']} | {text[:160]}{other} |".replace("\n", " "))
    return "\n".join(L) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="보고 md 저장 경로")
    ap.add_argument("--json", action="store_true", help="JSON 으로")
    a = ap.parse_args(argv)
    res = audit()
    text = json.dumps(res, ensure_ascii=False, indent=1) if a.json else to_md(res)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"저장: {a.out} — 어긋남 {len(res['hard'])} · 덜 적힘 {len(res['soft'])}")
    else:
        print(text)
    return 1 if res["hard"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
