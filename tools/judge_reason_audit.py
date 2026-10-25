# -*- coding: utf-8 -*-
"""판정 사유 점검 (judge-reason-audit-v1) — 판정 폴더의 FAIL · 보류 칸 사유를 모델 호출 없이 훑어 **기준이 공정하게 적용됐는지** 의심되는 칸을 찾는다
(사용자 10-25 밤: "목표는 pass 가 많이 생기는 것이 아니라 모두가 인정할 만한 평가기준").

찾는 것:
  ① 정의 밖 사유 — 사유가 그 항목이 아니라 다른 항목 몫의 오류를 말함(판정 프롬프트의 '다른 항목' 규칙 · 기준 v2 경계 · 예외에서 뽑은 단서어 표). 한 오류는 한 항목에서만.
  ② 같은 오류 두 번 — 한 턴에서 두 항목의 FAIL 이 같은 답 구절(answer_quote)이나 같은 사유 글을 씀.
  ③ 가정에 기댄 FAIL — 사유가 추측 · 가정 표현만 있고 답 · 자료 인용이 없음.
  ④ 보류 종류 — 심판 출력 누락 · 사유 모순 · 인용 불일치 · 확인 못함(우리 원인 vs 모델 원인).
  ⑤ 1차 ↔ 최종 — 1차 심판이 FAIL 로 걸었다가 Codex 가 PASS 로 뒤집은 수(항목마다): 1차 정의가 좁거나 프롬프트 규칙이 1차에 안 닿는 곳.
  ⑥ 모델 간 갈림 — 같은 (질문, 턴, 항목)에서 모델마다 판정이 갈린 수(정보).

    python -X utf8 tools/judge_reason_audit.py --run 50ca3ccccace --judge v4-r8 [--set 참고자료/질문세트_78] [--out 보고.md] [--top 8]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VERSION = "judge-reason-audit-v1"
RUNS = ROOT / "data" / "judge_runs"
KO = {"PASS": "통과", "FAIL": "FAIL", "UNVERIFIED": "보류", "NOT_APPLICABLE": "해당 없음", None: "-"}

#: ① 단서어 표 — (이 항목의 FAIL 사유에 나오면 다른 항목 몫인 표현, 그 오류를 맡은 항목). 판정 프롬프트 v4 의 '다른 항목' 줄과 기준 v2 6절 경계 · 예외에서.
#: 단서어가 있어도 항목 자체의 단서(OWN)가 함께 있으면 의심에서 뺀다(예: J5-2.b 사유에 '빠뜨렸다'와 '원문에 없는 문장을 더했다'가 같이 있으면 덧붙임이 주 사유).
MISROUTE: Dict[str, List[Tuple[str, str]]] = {
    "J1-4.a": [(r"반복|되풀이|다시 적|두 번 (나|적|쓰)", "J1-4.b"), (r"원문에 없는|덧붙|지어낸 (문장|내용)", "J5-2.b"), (r"조건을 어기|금지한|하지 말라", "J2-3.a")],
    "J1-4.b": [(r"무관한 (단락|내용|주제)|쓰이지 않는 단락|불필요한 (단락|안내)", "J1-4.a")],
    "J1-1.a": [(r"일부(만|를)? ?['\"]?확인할 수 없|표의 한 칸", "J4-2.a · J7")],
    "J1-2.a": [(r"일부(만|를)? ?['\"]?확인할 수 없", "J4-2.a · J7")],
    "J2-3.a": [(r"글자 수|자 이내|개수|\b\d+개(를|가)|코드 ?블록|표(를|로) (안|쓰지)|목록 형식", "CODE(2-1 · 2-2)"), (r"존댓말|반말|말투|어조", "J2-2.a"),
               (r"이전 턴|앞 턴|앞선 턴|1턴의 지시", "J6-1.a"), (r"인용 번호|출처 번호|\[1\]|\[2\]", "인용 지시 해석(정한 판정)")],
    "J2-2.a": [(r"이전 턴|앞 턴|앞선 턴|1턴의", "J6-1.a")],
    "J3-1.a": [(r"이전 턴|앞 턴|앞선 (턴|답변)|지난 턴", "J6-4.a"), (r"질문의 조건끼리|조건이 서로", "J1-3.a")],
    "J3-4.a": [(r"값(이|을) (틀|잘못)|다르게 옮|엉뚱한 (곳|부분)", "J5-2 · J7")],
    "J3-5.a": [(r"질문의 조건(끼리|이 서로)|접수 시작.*행사 시작", "J1-3.a")],
    "J4-4.a": [(r"원문에 없는|덧붙|지어낸|사실과 다르|의미(를|가) 바꾸", "J5-2.b"), (r"틀린 (값|수치)|숫자(가|를) (틀|다르)", "J5-2.a")],
    "J5-2.a": [(r"원문에 없는 (문장|내용)|덧붙|의미(를|가) 바꾸|지어낸", "J5-2.b"), (r"빠뜨|누락|빠졌", "J4-4.a · J4-2.a")],
    "J5-2.b": [(r"빠뜨|누락|빠졌|빠져", "J4-4.a"), (r"인용 번호|출처 번호|\[1\]|\[2\]|번호가 (빠|없)", "인용 지시 해석(정한 판정)"), (r"모순을 (짚|드러)|충돌을 (짚|드러)", "J1-3.a · J3-1.a")],
    "J5-3.a": [(r"(조회|검색|확인)했다고 (했|말)|도구(를|로) ", "안전장치 '도구 서술'(해당 없음)")],
    "J5-1.a": [(r"붙여 넣은 자료|첨부(파일)?에|참조 자료에|자료에 제시된", "J5-2(외부 사실 아님)")],
    "J6-1.a": [(r"표(를|로) (안|쓰지)|글자 수|형식(을|이) (안|다르)", "형식 요구는 그 턴(J2-3)만")],
    "J6-2.a": [(r"말투|존댓말|반말", "J6-1.a"), (r"빠뜨|누락|빠졌", "J6-2.b")],
    "J6-2.b": [(r"값(이|을) (틀|잘못)|다르게 옮", "J6-2.a")],
    "J6-4.a": [(r"근거 없이|반박(에|을)", "J6-4.b")],
    "J7-1.a": [(r"위치만|부 · 장 · 절|제\d부 제\d장", "J5-2.a(위치 오기)")],
    "J7-2.a": [(r"위치만|부 · 장 · 절|제\d부 제\d장", "J5-2.a(위치 오기)")],
}
#: 항목 자체의 주 단서 — 있으면 ① 에서 빼지 않는다(그 항목 몫의 오류를 말한 것)
OWN: Dict[str, str] = {
    "J1-4.a": r"무관|쓰이지 않|불필요|일반론|활용법", "J1-4.b": r"반복|되풀이|두 번", "J2-3.a": r"조건|제약|금지|~만|이내", "J3-1.a": r"모순|서로 다른 (값|결론)|같은 대상",
    "J4-4.a": r"빠(뜨|졌|져)|누락|없습니다", "J5-2.a": r"다르게 옮|틀린 값|원문(은|에는)|수치", "J5-2.b": r"덧붙|원문에 없는|의미(를|가) 바꾸|지어낸|확대|추가했",
    "J5-3.a": r"출처|조회|확인 범위|문헌|존재하지 않", "J6-1.a": r"지시|제약|말투|예산|범위", "J6-2.a": r"이전 (턴|정보|값)|바뀐 값|옛 값", "J6-2.b": r"필요한 이전|빠뜨|누락",
    "J6-4.a": r"뒤집|번복|이전 답변과", "J7-1.a": r"엉뚱한|묻는 정보 대신|근거로 삼", "J7-2.a": r"흩어진|연결|함께 써야",
}
ASSUMPTION = re.compile(r"추측|가정(하|해|한)|아마|것으로 보(인|임|입)|듯하|가능성이 (있|높)|수도 있")
QUOTE = re.compile(r"[\"“”'‘’「」]|원문[은:]|답변[은:]|라고 (적|썼|했)")


def last_lines(path: Path) -> Dict[Tuple[str, int], Dict[str, Any]]:
    out: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            x = json.loads(line)
            out[(x["provider"], int(x["question"]))] = x
    return out


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def overlap(a: str, b: str) -> float:
    """두 글의 겹침 — 짧은 쪽이 긴 쪽에 얼마나 들어 있나(글자 2-gram)."""
    A = {norm(a)[i:i + 2] for i in range(len(norm(a)) - 1)}
    B = {norm(b)[i:i + 2] for i in range(len(norm(b)) - 1)}
    if not A or not B:
        return 0.0
    return len(A & B) / min(len(A), len(B))


def audit(lines: Dict[Tuple[str, int], Dict[str, Any]], top: int = 8) -> Dict[str, Any]:
    misroute: List[Dict[str, Any]] = []
    double: List[Dict[str, Any]] = []
    assumed: List[Dict[str, Any]] = []
    holds: Counter = Counter()
    hold_cells: List[Dict[str, Any]] = []
    na_rule: List[Dict[str, Any]] = []   # ⑦ 해당 없음의 사유가 규칙 밖 — 대상 아닌 규칙 · 조건이 거짓인 규칙 · 제 판정을 PASS/FAIL 로 말함 (10-25 밤)
    try:
        from tools import judge_v4 as _jv
    except Exception:   # pragma: no cover
        _jv = None
    flip: Dict[str, Counter] = defaultdict(Counter)   # 항목 → {1차 FAIL→최종 PASS, 1차 PASS→최종 FAIL}
    by_turn: Dict[Tuple[int, int, str], Dict[str, str]] = defaultdict(dict)   # (질문, 턴, 항목) → {모델: 판정}
    n_cells = n_fail = 0
    try:
        from tools import report_html as R
        hold_kind = R.hold_kind
    except Exception:   # pragma: no cover
        hold_kind = lambda it: it.get("kind") or "?"
    for (p, q), x in sorted(lines.items()):
        if x.get("status") != "judged":
            continue
        for tn, t in (x.get("turn_results") or {}).items():
            items = {i: it for i, it in (t.get("items") or {}).items() if isinstance(it, dict)}
            fails = []
            decisions = {i: it.get("decision") for i, it in items.items()}
            error_turn = "에러 케이스 턴" in str((items.get("J1-1.a") or {}).get("how") or "")   # 에러 케이스 턴은 프로그램이 전부 해당 없음으로 — NA 사유 점검 대상 아님
            for iid, it in items.items():
                d = it.get("decision")
                n_cells += 1
                by_turn[(q, int(tn), iid)][p] = d
                first = it.get("jev")
                if first in ("PASS", "FAIL") and d in ("PASS", "FAIL") and first != d:
                    flip[iid][f"1차 {KO[first]}→최종 {KO[d]}"] += 1
                if d == "UNVERIFIED":
                    k = hold_kind(it)
                    holds[k] += 1
                    hold_cells.append({"cell": f"Q{q} {p} T{tn} {iid}", "kind": k, "reason": str(it.get("reason") or "")[:140]})
                if d == "NOT_APPLICABLE" and _jv is not None and not it.get("override") and not error_turn:
                    reason = str(it.get("reason") or "")
                    why = ("규칙의 대상 아님" if _jv.misapplied_rule(iid, reason) else
                           "규칙의 조건이 이 턴에서 거짓" if _jv.false_premise(reason, decisions, iid) else
                           "사유는 제 판정을 PASS/FAIL 로 말함" if _jv.reason_contradicts("NOT_APPLICABLE", reason, iid) else None)
                    if why:
                        na_rule.append({"cell": f"Q{q} {p} T{tn} {iid}", "why": why, "reason": reason[:150]})
                if d != "FAIL":
                    continue
                n_fail += 1
                reason = str(it.get("reason") or "")
                fails.append((iid, it))
                own = OWN.get(iid)
                for cue, owner in MISROUTE.get(iid, []):
                    if owner.startswith("인용 지시 해석") and re.search(r"번호", str(it.get("question_quote") or "")):
                        continue   # 질문이 '출처 번호 · 목록을 맞춰' 달라고 직접 말했으면 번호 표시는 명시 조건 — 정한 판정(번호 필수 아님)의 대상이 아님 (v4-r8-fix AIONA Q8 9턴)
                    if re.search(cue, reason) and not (own and re.search(own, reason)):
                        misroute.append({"cell": f"Q{q} {p} T{tn} {iid}", "owner": owner, "cue": cue, "reason": reason[:170]})
                        break
                if ASSUMPTION.search(reason) and not QUOTE.search(reason) and not (it.get("answer_quote") or "").strip():
                    assumed.append({"cell": f"Q{q} {p} T{tn} {iid}", "reason": reason[:170]})
            for i in range(len(fails)):
                for j in range(i + 1, len(fails)):
                    (ia, a), (ib, b) = fails[i], fails[j]
                    qa, qb = str(a.get("answer_quote") or ""), str(b.get("answer_quote") or "")
                    same_quote = len(norm(qa)) >= 10 and len(norm(qb)) >= 10 and overlap(qa, qb) >= 0.6
                    same_reason = overlap(str(a.get("reason") or ""), str(b.get("reason") or "")) >= 0.55 and min(len(norm(a.get("reason") or "")), len(norm(b.get("reason") or ""))) >= 30
                    if same_quote or same_reason:
                        double.append({"cell": f"Q{q} {p} T{tn}", "items": f"{ia} + {ib}", "why": "같은 답 구절" if same_quote else "같은 사유 글",
                                       "quote": (qa or str(a.get("reason") or ""))[:120]})
    split = Counter()
    split_cells = []
    for (q, tn, iid), per in by_turn.items():
        ds = {d for d in per.values() if d in ("PASS", "FAIL")}
        if ds == {"PASS", "FAIL"}:
            split[iid] += 1
            split_cells.append({"cell": f"Q{q} T{tn} {iid}", "per": per})
    mis_by_item = Counter(m["cell"].split()[-1] for m in misroute)
    mis_by_owner = Counter(m["owner"] for m in misroute)
    return {"version": VERSION, "cells": n_cells, "fail_cells": n_fail, "misroute": misroute, "mis_by_item": mis_by_item, "mis_by_owner": mis_by_owner,
            "double": double, "assumed": assumed, "holds": holds, "hold_cells": hold_cells, "flip": {k: dict(v) for k, v in flip.items()}, "na_rule": na_rule,
            "split": split, "split_cells": split_cells, "top": top}


def to_md(res: Dict[str, Any], title: str) -> str:
    top = res["top"]
    L = [f"# 판정 사유 점검 — {title} ({res['version']})", "",
         f"- 항목 칸 {res['cells']:,} · FAIL 칸 {res['fail_cells']} · 보류 칸 {sum(res['holds'].values())}",
         f"- ① 정의 밖 사유 의심 {len(res['misroute'])} · ② 같은 오류 두 번 {len(res['double'])} · ③ 가정에 기댄 FAIL {len(res['assumed'])} · ⑥ 모델 간 갈린 (질문, 턴, 항목) {sum(res['split'].values())}", ""]
    L += [f"- ⑦ 규칙 밖 해당 없음(NA 사유) {len(res.get('na_rule') or [])}", ""]
    L += ["## ① 정의 밖 사유 — 항목별 · 맡은 항목별", "", "| 항목 | 의심 칸 | → 맡은 항목(단서) |", "|---|---|---|"]
    owners_by_item: Dict[str, Counter] = defaultdict(Counter)
    for m in res["misroute"]:
        owners_by_item[m["cell"].split()[-1]][m["owner"]] += 1
    for iid, n in res["mis_by_item"].most_common():
        L.append(f"| {iid} | {n} | " + ", ".join(f"{o} {c}" for o, c in owners_by_item[iid].most_common()) + " |")
    L += ["", "예(각 항목 최대 %d개):" % top, ""]
    seen: Counter = Counter()
    for m in res["misroute"]:
        iid = m["cell"].split()[-1]
        if seen[iid] >= top:
            continue
        seen[iid] += 1
        L.append(f"- {m['cell']} → {m['owner']}: {m['reason'].replace(chr(10), ' ')}")
    L += ["", "## ② 같은 오류를 두 항목에서", ""]
    for d in res["double"][:top * 3]:
        L.append(f"- {d['cell']} {d['items']} ({d['why']}): {d['quote'].replace(chr(10), ' ')}")
    if not res["double"]:
        L.append("- 없음")
    L += ["", "## ③ 가정에 기댄 FAIL(인용 없음)", ""]
    for a in res["assumed"][:top * 2]:
        L.append(f"- {a['cell']}: {a['reason'].replace(chr(10), ' ')}")
    if not res["assumed"]:
        L.append("- 없음")
    L += ["", "## ④ 보류 종류", "", "| 종류 | 칸 |", "|---|---|"] + [f"| {k} | {v} |" for k, v in res["holds"].most_common()]
    for h in res["hold_cells"][:top]:
        L.append(f"- {h['cell']} [{h['kind']}] {h['reason'].replace(chr(10), ' ')}")
    L += ["", "## ⑤ 1차 ↔ 최종이 다른 칸 (항목별)", "", "| 항목 | 1차 FAIL→최종 통과 | 1차 통과→최종 FAIL |", "|---|---|---|"]
    for iid, c in sorted(res["flip"].items(), key=lambda kv: -sum(kv[1].values()))[:20]:
        L.append(f"| {iid} | {c.get('1차 FAIL→최종 통과', 0)} | {c.get('1차 통과→최종 FAIL', 0)} |")
    L += ["", "## ⑥ 모델 간 갈린 (질문, 턴, 항목) — 항목별 수(정보)", "", "| 항목 | 갈린 수 |", "|---|---|"] + [f"| {k} | {v} |" for k, v in res["split"].most_common(15)]
    L += ["", "## ⑦ 규칙 밖 해당 없음 — NA 사유 점검 (연결 규칙의 대상 아님 · 규칙의 조건이 그 턴에서 거짓 · 사유가 제 판정을 PASS/FAIL 로 말함)", ""]
    for m in (res.get("na_rule") or [])[:top * 3]:
        L.append(f"- {m['cell']} [{m['why']}] {m['reason'].replace(chr(10), ' ')}")
    if not res.get("na_rule"):
        L.append("- 없음")
    return "\n".join(L) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--judge", required=True, help="판정 폴더 이름")
    ap.add_argument("--out", help="보고 md 저장 경로")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    lines = last_lines(RUNS / a.run / a.judge / "results.jsonl")
    res = audit(lines, a.top)
    text = json.dumps(res, ensure_ascii=False, indent=1, default=str) if a.json else to_md(res, f"{a.run} / {a.judge}")
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"저장: {a.out} — 정의 밖 {len(res['misroute'])} · 두 번 {len(res['double'])} · 가정 {len(res['assumed'])} · 보류 {sum(res['holds'].values())}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
