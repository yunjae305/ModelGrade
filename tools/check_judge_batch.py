"""판정 묶음 점검 — 단위테스트처럼 항목마다 통과/실패를 찍는다 (사용자 요청 2026-10-20: "단위테스트 형식으로 10~20개").

    python -X utf8 tools/check_judge_batch.py --run ac6507c00276 --only 65,272,119,...

점검(질문 × 모델마다):
  T1 결과 줄이 있다                    T2 상태가 정상(판정됨·빈 출력 PASS/FAIL·복사 실패 보류) — 답 없음은 실패
  T3 Codex CLI 호출 성공              T4 Jev 호출 성공(또는 Jev 가 못 받아 Codex 가 대신 — 그 사실이 기록됨)
  T5 판정상황표의 항목이 빠짐없이 결정됨  T6 FAIL 항목마다 사유와 인용이 있음
  T7 보류(UNVERIFIED) 항목마다 사유가 있음 T8 Jev 혼자 내린 FAIL 이 없음(흐름 v2: Jev FAIL 은 Codex 가 확정)
  T9 질문 판정이 항목 결정과 맞음(FAIL ⇔ FAIL 항목·CODE FAIL, 보류 ⇔ 보류 항목)
묶음 전체: B1 판정 시간 기록(timing.jsonl) B2 summary.json B3 report.html B4 결과 JSON(참고자료/결과)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
SET = ROOT / "참고자료" / "질문세트_326_생성프롬프트v2"
OK_STATUS = {"judged", "empty_output_pass", "empty_output_fail", "held_copy_failed", "held_empty_output"}


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--only", required=True)
    ap.add_argument("--providers", default="gpt,claude,gemini,aiona")
    args = ap.parse_args(argv)
    qs = [int(x) for x in args.only.split(",") if x.strip()]
    providers = args.providers.split(",")
    d = ROOT / "data" / "judge_runs" / args.run
    lines = [json.loads(l) for l in (d / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    by = {(l["provider"], int(l["question"])): l for l in lines}
    table = {r["case_id"]: r["items"] for r in json.loads((SET / "판정상황표_v2.json").read_text(encoding="utf-8"))["rows"]}
    fails: Dict[str, List[str]] = defaultdict(list)
    total = Counter()

    def check(name: str, ok: bool, where: str, why: str = "") -> None:
        total[name] += 1
        if not ok:
            fails[name].append(f"{where}{(' — ' + why) if why else ''}")

    for q in qs:
        for p in providers:
            where = f"Q{q} {p}"
            l = by.get((p, q))
            check("T1 결과 줄", l is not None, where)
            if l is None:
                continue
            check("T2 상태 정상", l.get("status") in OK_STATUS, where, str(l.get("status")))
            if l.get("status") != "judged":
                if str(l.get("status", "")).startswith("empty_output") or l.get("status") == "held_empty_output":
                    check("T2 상태 정상", bool(l.get("collection_note")), where, "빈 출력 판정에 수집 상태 기록 없음")
                continue
            calls = l.get("calls") or {}
            check("T3 Codex 성공", (calls.get("gpt") or {}).get("status") == "OK", where, str((calls.get("gpt") or {}).get("error"))[:80])
            jev = calls.get("jev") or {}
            check("T4 Jev 성공·대신 기록", jev.get("status") == "OK" or (calls.get("gpt_fallback") or {}).get("status") == "OK", where,
                  f"Jev {jev.get('status')} {str(jev.get('error'))[:60]}")
            res = l.get("results") or {}
            missing = []
            for n in range(1, int(l.get("turns") or 1) + 1):
                planned = table.get(f"Q{q}_T{n}") or []
                got = res.get(str(n)) or {}
                missing += [f"T{n}:{i}" for i in planned if i not in got]
            check("T5 항목 빠짐 없음", not missing, where, " ".join(missing[:6]))
            has_fail = has_hold = False
            for n, items in res.items():
                for item, v in items.items():
                    dec = v.get("decision")
                    if dec == "FAIL":
                        has_fail = True
                        e = (v.get("entries") or [{}])[0]
                        check("T6 FAIL 사유·인용", bool(e.get("reason")) and bool(e.get("answer_quote") or e.get("question_quote")), where, f"T{n}:{item}")
                        check("T8 Jev 혼자 FAIL 없음", v.get("judge") != "jev", where, f"T{n}:{item}")
                    elif dec == "UNVERIFIED":
                        has_hold = True
                        check("T7 보류 사유", bool(v.get("problems")) or bool((v.get("entries") or [{}])[0].get("reason")), where, f"T{n}:{item}")
            want = "FAIL" if (has_fail or l.get("code_fails")) else ("HOLD" if has_hold else "PASS")
            check("T9 판정 일치", l.get("verdict") == want, where, f"verdict {l.get('verdict')} · 항목으로는 {want}")
    timing = [json.loads(x) for x in (d / "timing.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()] if (d / "timing.jsonl").exists() else []
    check("B1 판정 시간 기록", any(set(qs) & set(t.get("questions") or []) for t in timing), "묶음")
    check("B2 summary.json", (d / "summary.json").exists(), "묶음")
    check("B3 report.html", (d / "report.html").exists(), "묶음")
    check("B4 결과 JSON", any((ROOT / "참고자료" / "결과").glob(f"{args.run}_*판정결과.json")), "묶음")

    print(f"점검 대상: 질문 {len(qs)}개 × 모델 {len(providers)}개")
    bad = 0
    for name in sorted(total):
        n_fail = len(fails.get(name, []))
        bad += n_fail
        print(f"  {'✅' if not n_fail else '❌'} {name}: {total[name] - n_fail}/{total[name]} 통과" + (f" — {'; '.join(fails[name][:4])}" if n_fail else ""))
    mine = [l for l in lines if int(l["question"]) in qs and l["provider"] in providers]
    print("\n| 모델 | PASS | FAIL | 보류 | Jev→Codex 확정 항목 | Codex 평균(초) | Jev 평균(초) |\n|---|---|---|---|---|---|---|")
    for p in providers:
        m = [l for l in mine if l["provider"] == p]
        c = Counter(l.get("verdict") for l in m)
        flagged = sum(len(v) for l in m for v in ((l.get("calls") or {}).get("jev_flagged") or {}).values())
        gs = [(l.get("calls") or {}).get("gpt", {}).get("seconds") or 0 for l in m if l.get("status") == "judged"]
        js_ = [(l.get("calls") or {}).get("jev", {}).get("seconds") or 0 for l in m if l.get("status") == "judged"]
        print(f"| {p} | {c['PASS']} | {c['FAIL']} | {c['HOLD']} | {flagged} | {sum(gs) / max(1, len(gs)):.0f} | {sum(js_) / max(1, len(js_)):.1f} |")
    if timing:
        t = [x for x in timing if set(qs) & set(x.get("questions") or [])]
        if t:
            print(f"\n판정 벽시계: {sum(x.get('seconds') or 0 for x in t):.0f}초 · {sum(x.get('judgments') or 0 for x in t)}건")
    print(f"\n결과: {'모두 통과' if not bad else f'실패 {bad}건'}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
