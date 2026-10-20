"""답변 수집·평가에 걸린 시간 요약 (time-summary-v1) — 10-20 보고 07장용 (사용자 10-20: "답변 수집과 평가에 걸린 시간").
AI 호출 없음, 기록만 읽는다. 결과는 저장소에 남겨 다른 PC 에서도 보고서를 다시 만들 수 있게 한다.

    python -X utf8 tools/time_summary.py
    → 참고자료/결과/시간_요약_ac6507c00276.json

- 수집 10-20(간단 78, 앱·웹): 실행 파일(data/comparison_runs/ac6507c00276.json, 없으면 참고자료/결과/ac6507c00276_간단78_답변.json)의 턴마다 걸린 시간.
  질문 하나 = 그 질문 모든 턴의 합. 종류: 긴 문서(참조 자료) · 대화(턴 2개↑) · 단일.
- 수집 10-19(정밀 326, API): 참고자료/결과/8c61f117e661_정밀326_2026-10-19.json 의 턴마다 걸린 시간 — 비교용.
- CODE 평가: 이 도구가 간단 78 답으로 직접 재어 본 시간(모델마다).
- 의미 평가: 10-20 오전 판정 시험(v3 흐름, data/judge_runs/<실행>/results.jsonl) · 판정 v4 시험(data/judge_runs/<실행>/v4/results.jsonl, Codex 를
  실제로 부른 마지막 줄). 78×4 전체 판정 시간은 종류별 평균 × 질문 수 ÷ 동시 2건으로 어림한 추정치.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.question_generation import flatten  # noqa: E402

VERSION = "time-summary-v2"   # v2: 종류별 평균 토큰(질문 입력·답변, tiktoken o200k_base) 열 + --run/--set/--ids/--out (2026-10-20)
RUN = "ac6507c00276"
SET_DIR = ROOT / "참고자료" / "질문세트_326_생성프롬프트v2"
RESULT_1019 = ROOT / "참고자료" / "결과" / "8c61f117e661_정밀326_2026-10-19.json"
IDS78 = ROOT / "참고자료" / "질문세트_최소78" / "ids.json"
OUT = ROOT / "참고자료" / "결과" / f"시간_요약_{RUN}.json"
PROVIDERS = ("gpt", "claude", "gemini", "aiona")
WORKERS = 2
KINDS = ("단일", "대화", "긴 문서")


def kinds() -> Dict[int, str]:
    groups = json.loads((SET_DIR / "questions.json").read_text(encoding="utf-8"))
    return {int(g["id"]): ("긴 문서" if (g["turns"][0].get("reference") or g["turns"][0].get("reference_file")) else "대화" if len(g["turns"]) > 1 else "단일")
            for g in groups}


# --- 토큰 (사용자 10-20: time_summary 에 평균 토큰 열) ---------------------------------------------------------------------
def _tokens(text: str) -> Optional[int]:
    """tiktoken o200k_base 토큰 수 — 토크나이저가 없으면 None (글자 수만 남긴다)."""
    from code_eval import long_context
    try:
        return long_context.count_tokens(text or "")
    except long_context.TokenizerUnavailable:
        return None


def question_tokens_by_case() -> Dict[str, Optional[int]]:
    """턴(case_id)마다 모델이 받은 입력의 토큰 — 질문 + 첨부 내용(글로 옮긴 것) + 참조 자료. 앞 턴은 넣지 않는다(긴 문서 판정과 같은 기준)."""
    groups = json.loads((SET_DIR / "questions.json").read_text(encoding="utf-8"))
    out: Dict[str, Optional[int]] = {}
    for r in flatten(groups):
        text = (r.get("question") or "") + "".join("\n" + (a.get("content") or "") for a in r.get("attachments") or [] if isinstance(a, dict))
        ref = r.get("reference")
        out[r["case_id"]] = _tokens(text + ("\n" + ref if isinstance(ref, str) and ref else ""))
    return out


def _sec(ms: Any) -> Optional[float]:
    try:
        return float(ms) / 1000
    except (TypeError, ValueError):
        return None


def per_question(turns: List[Dict[str, Any]], scope: Optional[set], q_tokens: Optional[Dict[str, Optional[int]]] = None) -> Dict[int, Dict[str, Any]]:
    """질문마다 걸린 시간(모든 턴 합)과 모든 턴을 받았는지 — 턴마다 질문 입력 토큰(세트에서)·답변 토큰(저장된 답 원문에서)도 더한다."""
    out: Dict[int, Dict[str, Any]] = defaultdict(lambda: {"seconds": 0.0, "turns": 0, "ok": True, "timed": True, "chars": 0,
                                                          "q_tokens": 0, "a_tokens": 0, "tokened": True})
    for t in turns:
        q = int(t["question"])
        if scope and q not in scope:
            continue
        s = _sec(t.get("latency_ms"))
        out[q]["turns"] += 1
        answer = t.get("answer")
        out[q]["chars"] += int(t.get("answer_chars") or len(answer or ""))
        out[q]["ok"] &= t.get("status") == "SUCCESS"
        if s is None:
            out[q]["timed"] = False
        else:
            out[q]["seconds"] += s
        qt = (q_tokens or {}).get(t.get("case_id") or "")
        at = _tokens(answer) if isinstance(answer, str) else None
        if qt is None or at is None:
            out[q]["tokened"] = False
        else:
            out[q]["q_tokens"] += qt
            out[q]["a_tokens"] += at
    return dict(out)


def stats(pq: Dict[int, Dict[str, Any]], kind: Dict[int, str]) -> Dict[str, Any]:
    done = {q: v for q, v in pq.items() if v["ok"] and v["timed"]}
    by: Dict[str, List[float]] = defaultdict(list)
    for q, v in done.items():
        by[kind[q]].append(v["seconds"])
    top = max(done.items(), key=lambda x: x[1]["seconds"]) if done else None
    # 종류별 평균 토큰 — 질문 하나(모든 턴 합)와 턴 하나 기준, 질문 입력·답변 따로
    tok: Dict[str, Dict[str, Any]] = {}
    for k in KINDS:
        rows = [v for q, v in done.items() if kind[q] == k and v["tokened"]]
        if rows:
            turns_n = sum(v["turns"] for v in rows)
            tok[k] = {"questions": len(rows), "turns": turns_n,
                      "question_tokens_mean": round(sum(v["q_tokens"] for v in rows) / len(rows)),
                      "answer_tokens_mean": round(sum(v["a_tokens"] for v in rows) / len(rows)),
                      "question_tokens_per_turn": round(sum(v["q_tokens"] for v in rows) / turns_n),
                      "answer_tokens_per_turn": round(sum(v["a_tokens"] for v in rows) / turns_n)}
    tokened = [v for v in done.values() if v["tokened"]]
    return {"questions_done": len(done), "questions_seen": len(pq),
            "mean": {k: round(statistics.mean(v), 1) for k, v in by.items()}, "count": {k: len(v) for k, v in by.items()},
            "max": {"question": top[0], "seconds": round(top[1]["seconds"], 1), "kind": kind[top[0]]} if top else None,
            "total_seconds": round(sum(v["seconds"] for v in pq.values()), 1),
            "chars_per_turn": round(sum(v.get("chars", 0) for v in done.values()) / max(1, sum(v["turns"] for v in done.values()))),
            "chars_per_second": round(sum(v.get("chars", 0) for v in done.values()) / max(1.0, sum(v["seconds"] for v in done.values())), 1),
            "tokens_by_kind": tok,
            "answer_tokens_per_turn": round(sum(v["a_tokens"] for v in tokened) / max(1, sum(v["turns"] for v in tokened))) if tokened else None,
            "answer_tokens_per_second": round(sum(v["a_tokens"] for v in tokened) / max(1.0, sum(v["seconds"] for v in tokened)), 1) if tokened else None,
            "tokenizer": "tiktoken o200k_base" if tokened else None}


def collect_1020(kind: Dict[int, str], ids: set) -> Dict[str, Any]:
    run_path = ROOT / "data" / "comparison_runs" / f"{RUN}.json"
    if run_path.exists():
        run = json.loads(run_path.read_text(encoding="utf-8"))
        src = str(run_path.relative_to(ROOT))
        turns = {p: [{"case_id": r["case_id"], "question": r["case_id"].split("_T")[0][1:], "status": (r.get("collection") or {}).get("status"),
                      "latency_ms": (r.get("collection") or {}).get("latency_ms"), "answer": r.get("answer") or ""}
                     for r in (run.get("datasets") or {}).get(p) or [] if r and (r.get("collection") or {}).get("method") not in (None, "pending")]
                 for p in PROVIDERS}
        labels = run.get("models") or {}
    else:
        cands = sorted((ROOT / "참고자료" / "결과").glob(f"{RUN}_*답변.json"))
        if not cands:
            return {"source": None, "models": {}, "note": f"실행 파일도 답변 export 도 없음 ({RUN})"}
        exp = json.loads(cands[0].read_text(encoding="utf-8"))
        src = str(cands[0].relative_to(ROOT))
        turns = {p: exp["providers"][p]["turns"] for p in PROVIDERS if p in exp["providers"]}
        labels = exp.get("models") or {}
    qt = question_tokens_by_case()
    return {"source": src, "models": {p: {"label": labels.get(p), **stats(per_question(turns.get(p) or [], ids, qt), kind)} for p in PROVIDERS if turns.get(p)}}


def collect_1019(kind: Dict[int, str]) -> Dict[str, Any]:
    if not RESULT_1019.exists():
        return {}
    data = json.loads(RESULT_1019.read_text(encoding="utf-8"))
    groups = json.loads((ROOT / "참고자료" / "질문세트_326_생성프롬프트v2" / "questions.json").read_text(encoding="utf-8"))
    kind326 = {int(g["id"]): ("긴 문서" if g["turns"][0].get("reference") else "대화" if len(g["turns"]) > 1 else "단일") for g in groups}
    qt = {r["case_id"]: _tokens((r.get("question") or "") + "".join("\n" + (a.get("content") or "") for a in r.get("attachments") or [] if isinstance(a, dict))
                                + ("\n" + r["reference"] if isinstance(r.get("reference"), str) and r["reference"] else "")) for r in flatten(groups)}
    return {"source": str(RESULT_1019.relative_to(ROOT)),
            "models": {p: {"label": (data.get("models") or {}).get(p), "method": next((t.get("method") for t in data["providers"][p]["turns"]), None),
                           **stats(per_question(data["providers"][p]["turns"], None, qt), kind326)} for p in PROVIDERS if p in data.get("providers", {})}}


def code_time() -> Dict[str, Any]:
    from tools.modelgrade import code_evaluate
    run_path = ROOT / "data" / "comparison_runs" / f"{RUN}.json"
    if not run_path.exists():
        return {}
    run = json.loads(run_path.read_text(encoding="utf-8"))
    out = {}
    for p in PROVIDERS:
        rows = [r for r in (run.get("datasets") or {}).get(p) or [] if r and (r.get("collection") or {}).get("method") not in (None, "pending")]
        t0 = time.perf_counter()
        ev = code_evaluate(rows, p)
        out[p] = {"turns": ev["run"]["evaluated_cases"], "seconds": round(time.perf_counter() - t0, 2)}
    return out


def judge_v3(kind: Dict[int, str]) -> Dict[str, Any]:
    path = ROOT / "data" / "judge_runs" / RUN / "results.jsonl"
    if not path.exists():
        return {}
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    judged = [l for l in lines if l.get("status") == "judged"]
    by: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    for l in judged:
        c = l.get("calls") or {}
        by[kind[int(l["question"])]]["codex"].append((c.get("gpt") or {}).get("seconds") or 0)
        by[kind[int(l["question"])]]["jev"].append((c.get("jev") or {}).get("seconds") or 0)
        by[kind[int(l["question"])]]["question"].append(l.get("seconds") or 0)
    return {"source": str(path.relative_to(ROOT)), "judgments": len(judged), "questions": sorted({int(l["question"]) for l in judged}),
            "by_kind": {k: {m: {"n": len(v), "mean": round(statistics.mean(v), 1), "max": round(max(v), 1)} for m, v in d.items()} for k, d in by.items()}}


def judge_v4(kind: Dict[int, str]) -> Dict[str, Any]:
    path = ROOT / "data" / "judge_runs" / RUN / "v4" / "results.jsonl"
    if not path.exists():
        return {}
    last: Dict[tuple, Dict[str, Any]] = {}
    for x in path.read_text(encoding="utf-8").splitlines():
        if not x.strip():
            continue
        l = json.loads(x)
        if (l.get("calls") or {}).get("codex", {}).get("seconds") is not None:
            last[(l["question"], l["provider"])] = l
    rows = [{"question": q, "provider": p, "kind": kind[q], "turns": l["turns"], "verdict": l["verdict"],
             "codex_verdict": (l.get("codex_conclusion") or {}).get("verdict"), "codex_reason": (l.get("codex_conclusion") or {}).get("reason"),
             "agree": l.get("jev_codex_agree"),
             "only_judge": sum(len(t.get("only_judge") or []) for t in (l.get("turn_results") or {}).values()),
             "only_table": sum(len(t.get("only_table") or []) for t in (l.get("turn_results") or {}).values()),
             "codex_seconds": l["calls"]["codex"]["seconds"], "codex_calls": l["calls"]["codex"].get("calls", 1),
             "jev_seconds": (l["calls"].get("jev") or {}).get("seconds"), "jev_calls": (l["calls"].get("jev") or {}).get("calls", 1),
             "decisions_seconds": (l["calls"].get("decisions") or {}).get("seconds"), "decisions_input_tokens": (l["calls"].get("decisions") or {}).get("input_tokens"),
             "mode": l.get("mode") or "dual", "screen": l.get("screen")} for (q, p), l in sorted(last.items())]
    by: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        for key in ("codex_seconds", "jev_seconds", "decisions_seconds", "decisions_input_tokens"):
            if isinstance(r.get(key), (int, float)):
                by[r["kind"]][key].append(float(r[key]))
    return {"source": str(path.relative_to(ROOT)), "rows": rows,
            "by_kind": {k: {m: {"n": len(v), "mean": round(statistics.mean(v), 1), "max": round(max(v), 1)} for m, v in d.items()} for k, d in by.items()}}


def estimate(v3: Dict[str, Any], v4: Dict[str, Any], kind: Dict[int, str], ids: set) -> Dict[str, Any]:
    """78 × 4모델 판정 시간 어림 — 종류별 질문 하나 시간(v4 시험이 있으면 v4, 없으면 v3 시험) × 질문 수 ÷ 동시 2건."""
    per = {}
    for k in ("단일", "대화", "긴 문서"):
        v4s = [r["codex_seconds"] for r in v4.get("rows") or [] if r["kind"] == k]
        if v4s:
            per[k] = {"seconds": round(statistics.mean(v4s), 1), "basis": f"판정 v4 시험 {len(v4s)}건 평균"}
        elif (v3.get("by_kind") or {}).get(k):
            per[k] = {"seconds": v3["by_kind"][k]["question"]["mean"], "basis": f"10-20 오전 시험(v3 흐름) {v3['by_kind'][k]['question']['n']}건 평균"}
    counts = {k: sum(1 for q in ids if kind[q] == k) for k in per}
    one_model = sum(per[k]["seconds"] * counts[k] for k in per)
    return {"per_question": per, "counts_78": counts, "workers": WORKERS, "models": len(PROVIDERS),
            "seconds_one_model": round(one_model), "seconds_all": round(one_model * len(PROVIDERS) / WORKERS)}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    global RUN, SET_DIR, OUT
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", default=RUN, help="실행 id (data/comparison_runs/<id>.json, 없으면 참고자료/결과/<id>_*답변.json)")
    ap.add_argument("--set", default=str(SET_DIR), help="질문 세트 폴더 (questions.json)")
    ap.add_argument("--ids", default=str(IDS78), help="질문 번호 파일(ids.json) 또는 'all' = 세트의 모든 질문")
    ap.add_argument("--out", default=None, help="저장 경로 (기본 참고자료/결과/시간_요약_<실행>.json)")
    ap.add_argument("--no-1019", action="store_true", help="10-19 정밀 326 비교를 빼기")
    args = ap.parse_args(argv)
    RUN, SET_DIR = args.run, Path(args.set)
    OUT = Path(args.out) if args.out else ROOT / "참고자료" / "결과" / f"시간_요약_{RUN}.json"
    kind = kinds()
    ids = set(kind) if args.ids == "all" or not Path(args.ids).exists() else set(json.loads(Path(args.ids).read_text(encoding="utf-8"))["ids"])
    v3, v4 = judge_v3(kind), judge_v4(kind)
    doc = {"version": VERSION, "made_at": datetime.now(timezone.utc).isoformat(), "run_id": RUN, "set": str(SET_DIR.relative_to(ROOT)) if SET_DIR.is_relative_to(ROOT) else str(SET_DIR),
           "kinds_78": {k: sum(1 for q in ids if kind[q] == k) for k in KINDS},
           "collect_1020": collect_1020(kind, ids), "collect_1019": {} if args.no_1019 else collect_1019(kind), "code": code_time(),
           "judge_v3": v3, "judge_v4": v4, "estimate_78": estimate(v3, v4, kind, ids)}
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"저장: {OUT}")
    print("| 모델 | 받은 질문 | 종류별 평균 초 | 종류별 평균 토큰 (질문 입력 / 답변, 질문 하나) |\n|---|---|---|---|")
    for p, m in doc["collect_1020"]["models"].items():
        tk = m.get("tokens_by_kind") or {}
        print(f"| {p} | {m['questions_done']} | {m['mean']} | " + " · ".join(f"{k}: {v['question_tokens_mean']:,} / {v['answer_tokens_mean']:,}" for k, v in tk.items()) + " |")
    print("추정", doc["estimate_78"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
