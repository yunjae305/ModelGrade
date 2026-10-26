"""실행 결과를 저장소에 JSON 으로 남긴다 (사용자 요청 2026-10-20: "json 형식으로 저장해둬야지").

    python -X utf8 tools/export_results_json.py --run 8c61f117e661 --label 정밀326          # 326 결과(10-19, 답변 + CODE + 의미 평가)
    python -X utf8 tools/export_results_json.py --run ac6507c00276 --label 간단78 --only-78 # 78 답변(10-20 앱·웹 수집)

→ 참고자료/결과/<실행>_<label>.json — 실행 파일(data/comparison_runs, git 제외)이 없는 다른 PC 에서도 결과를 볼 수 있게.
담는 것(질문 글·첨부·참조 자료는 questions.json 에 있으므로 빼고 질문 번호로 잇는다):
- 모델별 턴마다: 답변 원문, 수집 상태·방법·시간·오류(빈 답·복사 실패 사유), 옛 질문에 대한 옛 답(previous, 질문 수정 전)
- CODE 평가: 적용된 검사의 check_id·판정·사유
- 의미 평가: 옛 파이프라인(10-19, jev_eval.records)의 J1~J8 결정·확률·사유, 또는 새 판정(judge_runs/<실행>/results.jsonl·summary.json·timing.jsonl)
- 요약: 모델별 CODE·의미·종합 집계
`modelgrade report` 도 판정이 끝나면 이 파일을 다시 쓴다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FORMAT = "modelgrade-results-v1"
OUT_DIR = ROOT / "참고자료" / "결과"
RUNS = ROOT / "data" / "comparison_runs"
JUDGE = ROOT / "data" / "judge_runs"
IDS78 = ROOT / "참고자료" / "질문세트_최소78" / "ids.json"


def _qno(case_id: str) -> int:
    return int(str(case_id).split("_T")[0][1:])


def _turn(case_id: str) -> int:
    return int(str(case_id).split("_T")[1]) if "_T" in str(case_id) else 1


def _clip(text: Any, n: int = 300) -> Any:
    return text[:n] if isinstance(text, str) and len(text) > n else text


def collection_row(r: Dict[str, Any]) -> Dict[str, Any]:
    c = r.get("collection") or {}
    out = {"case_id": r["case_id"], "question": _qno(r["case_id"]), "turn": _turn(r["case_id"]), "answer": r.get("answer") or "",
           "status": c.get("status", "SUCCESS" if r.get("answer") else None), "capture": c.get("capture"), "latency_ms": c.get("latency_ms"),
           "error": _clip(c.get("error")), "model": c.get("model"), "method": c.get("method")}
    if c.get("screenshot"):   # 실행 폴더 기준 대화 화면(답 영역) — 빈 출력 요구에 복사 아이콘이 안 뜬 턴 (10-26)
        out["screenshot"] = c["screenshot"]
    if c.get("draft_blocks"):
        out["draft_blocks"] = c["draft_blocks"]
    if c.get("artifact"):
        out["artifact"] = True
    if r.get("previous"):
        out["previous"] = [{"answer": p.get("answer") or "", "status": (p.get("collection") or {}).get("status"),
                            "capture": (p.get("collection") or {}).get("capture"), "question_revision": p.get("question_revision")} for p in r["previous"]]
    return {k: v for k, v in out.items() if v not in (None, "")} | {"answer": out["answer"]}


def code_results(case: Dict[str, Any]) -> List[Dict[str, Any]]:
    """적용된 CODE 검사만 — 판정(PASS/FAIL/…)과 사유."""
    out = []
    for x in case.get("results") or []:
        if not isinstance(x, dict) or not x.get("check_id"):
            continue
        if x.get("status") not in ("EXECUTED", "FAILED", "INCOMPLETE") and not x.get("verdict"):
            continue
        out.append({"check_id": x["check_id"], "verdict": x.get("verdict"), "status": x.get("status"), "reason": _clip(x.get("reason"), 240)})
    return out


def legacy_semantic(record: Dict[str, Any]) -> Dict[str, Any]:
    """옛 의미 평가(jev_eval.records 한 건) — 기준마다 결정·확신·사유."""
    dec = {}
    for cid, d in (record.get("decisions") or {}).items():
        if isinstance(d, dict):
            dec[cid] = {k: (_clip(v, 400) if k == "reason" else v) for k, v in d.items() if k in ("decision", "confidence", "probabilities", "reason")}
    out = {"status": record.get("status"), "criteria": record.get("criteria"), "decisions": dec, "model": record.get("model")}
    if record.get("review_required"):
        out["review_required"] = record["review_required"]
    return out


def compact_judgment(line: Dict[str, Any]) -> Dict[str, Any]:
    """판정 줄 하나를 보기용으로 줄인다 — 선별 결과(turn_results)는 항목마다 판정·사유·인용만 남기고 심판 원자료(entries·확률 전체)는 뺀다.
    (전체를 넣으면 78×4 에 76MB — 저장소에 넣을 수 없어서, 2026-10-21.) 원자료는 data/judge_runs/<실행>/<out>/ 에 있다."""
    keep = {k: line.get(k) for k in ("version", "mode", "provider", "question", "turns", "status", "verdict", "semantic", "code_fails", "seconds",
                                     "screen", "judge_error", "reason", "collection_note", "at") if line.get(k) not in (None, "", [])}
    calls = line.get("calls") or {}
    if isinstance(calls, dict):
        keep["calls"] = {k: {kk: vv for kk, vv in v.items() if kk != "output"} if isinstance(v, dict) else v for k, v in calls.items()}
    src = line.get("turn_results") or line.get("results") or {}
    out_turns: Dict[str, Any] = {}
    for turn, block in src.items():
        items = block.get("items") if isinstance(block, dict) and "items" in block else block
        compact: Dict[str, Any] = {}
        for item, v in (items or {}).items():
            if not isinstance(v, dict):
                continue
            entries = v.get("entries") or []
            e = next((x for x in entries if x.get("decision") == v.get("decision")), entries[0] if entries else {})
            c = {"decision": v.get("decision"), "how": v.get("how"), "reason": _clip(v.get("reason") or e.get("reason"), 400),
                 "answer_quote": _clip(v.get("answer_quote") or e.get("answer_quote"), 300), "question_quote": _clip(v.get("question_quote") or e.get("question_quote"), 200),
                 "codex": v.get("codex"), "decisions": v.get("jev"), "decisions_fail_prob": (v.get("decisions_prob") or {}).get("FAIL") if isinstance(v.get("decisions_prob"), dict) else None,
                 "decisions_confidence": v.get("decisions_confidence"), "flagged": v.get("flagged"), "problems": v.get("problems"), "notes": v.get("notes"),
                 "kind": v.get("kind")}   # 판정이 붙인 보류 종류(확인 못함 · 사유 모순) — 보고서가 '기타'로 세지 않게 (10-22)
            compact[item] = {k: x for k, x in c.items() if x not in (None, "", [])}
        out_turns[str(turn)] = {"situations": (block.get("situations_judge") if isinstance(block, dict) else None) or [], "items": compact}
    keep["turn_results"] = out_turns
    return keep


def export(run_id: str, label: str, only: Optional[List[int]] = None, out_dir: Path = OUT_DIR,
           runs_dir: Path = RUNS, judge_dir: Optional[Path] = None) -> Path:
    run = json.loads((runs_dir / f"{run_id}.json").read_text(encoding="utf-8"))
    scope = set(only) if only else None
    doc: Dict[str, Any] = {"format": FORMAT, "exported_at": datetime.now(timezone.utc).isoformat(), "run_id": run_id, "label": label,
                           "name": run.get("name"), "created_at": run.get("created_at"), "question_set": run.get("question_set"),
                           "question_revision": run.get("question_revision") or "v2.0", "eval_kind": run.get("eval_kind"),
                           "question_numbers": only or sorted({_qno(r["case_id"]) for rows in (run.get("datasets") or {}).values() for r in rows if r}),
                           "models": run.get("models") or {}, "providers": {}}
    for p, rows in (run.get("datasets") or {}).items():
        ev = (run.get("evaluations") or {}).get(p) or {}
        cases = {c.get("case_id"): c for c in ev.get("cases") or [] if isinstance(c, dict)}
        records = {x.get("case_id"): x for x in ((ev.get("jev_eval") or {}).get("records") or []) if isinstance(x, dict)}
        turns = []
        for r in rows:
            if not r or (scope and _qno(r["case_id"]) not in scope):
                continue
            row = collection_row(r)
            if r["case_id"] in cases:
                row["code"] = code_results(cases[r["case_id"]])
            if r["case_id"] in records:
                row["semantic_legacy"] = legacy_semantic(records[r["case_id"]])
            turns.append(row)
        summary = {"code": (ev.get("summary") or {}).get("totals")}
        if ev.get("combined"):
            summary["combined"] = {k: ev["combined"].get(k) for k in ("questions", "pass", "fail", "pending", "rate", "fail_code_only",
                                                                        "fail_semantic_only", "fail_both", "semantic_engine")}
        if ev.get("jev_eval"):
            je = ev["jev_eval"]
            summary["semantic_legacy"] = {"engine": je.get("engine_label"), "version": je.get("version"), "status": je.get("status"),
                                          "by_criterion": je.get("summary"), "conversations": je.get("conversations")}
        doc["providers"][p] = {"label": (run.get("models") or {}).get(p) or p, "turns": turns, "summary": summary}
    jd = judge_dir or (JUDGE / run_id)
    if (jd / "results.jsonl").exists():
        latest: Dict[str, Dict[str, Any]] = {}
        for l in (jd / "results.jsonl").read_text(encoding="utf-8").splitlines():
            if l.strip():
                x = json.loads(l)
                latest[f"{x.get('provider')}:{x.get('question')}"] = x   # 같은 (모델, 질문)은 마지막 줄
        lines = [compact_judgment(l) for l in latest.values() if not scope or int(l.get("question") or 0) in scope]
        used_jev = any("jev" in ((((l.get("calls") or {}).get("decisions") or {}).get("first_judge")) or {}).values() for l in latest.values())
        pipeline = ("judge-v5-screen (1차 Jev(3만 2천 토큰 이하 · 첨부 없음) · Decisions(그 밖) → FAIL · 애매만 Codex, Codex 확정)" if used_jev   # 10-22
                    else "judge-v5-screen (Decisions 전수 → 걸린 항목만 Codex, Codex 확정)")
        doc["judgment"] = {"pipeline": pipeline, "results": lines,
                           "summary": json.loads((jd / "summary.json").read_text(encoding="utf-8")) if (jd / "summary.json").exists() else None,
                           "timing": [json.loads(l) for l in (jd / "timing.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
                           if (jd / "timing.jsonl").exists() else []}
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_id}_{label}.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--only-78", action="store_true", help="최소 78 질문만")
    args = ap.parse_args(argv)
    only = json.loads(IDS78.read_text(encoding="utf-8"))["ids"] if args.only_78 else None
    path = export(args.run, args.label, only)
    size = path.stat().st_size / 1e6
    doc = json.loads(path.read_text(encoding="utf-8"))
    counts = {p: len(v["turns"]) for p, v in doc["providers"].items()}
    print(f"저장: {path} ({size:.1f}MB) | 질문 {len(doc['question_numbers'])}개 | 모델별 턴 {counts} | 새 판정 {'있음' if 'judgment' in doc else '없음'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
