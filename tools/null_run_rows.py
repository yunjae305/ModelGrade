"""비교 실행의 특정 질문 행을 비운다 — 질문 본문이 바뀐 질문은 받은 답이 더 이상 그 질문의 답이 아니므로 모든 모델의 그 행(대화는 모든 턴)을
비워 '이어서 수집'이 다시 받게 한다 (모델 호출 없음, 서버가 멈춘 상태에서).

    python tools/null_run_rows.py <run_id> --ids 54,65,82,288            # 질문 id (Q 없이)
    python tools/null_run_rows.py <run_id> --from-set qs-…               # 세트의 fixed_after_repair.json 의 changed 목록

원본은 <run>.before-null.json 으로 남긴다. 실행의 question_set·dataset_fingerprint 는 세트의 새 지문으로 갱신한다(이어서 수집이 같은 세트로 인식하게).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RUNS = ROOT / "data" / "comparison_runs"
SETS = ROOT / "data" / "question_sets"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id")
    ap.add_argument("--ids", default="")
    ap.add_argument("--from-set", default="")
    args = ap.parse_args(argv)
    ids = {int(x) for x in args.ids.split(",") if x.strip()}
    if args.from_set:
        fixed = json.loads((SETS / args.from_set / "fixed_after_repair.json").read_text(encoding="utf-8"))
        ids |= set(fixed.get("changed") or [])
    if not ids:
        raise SystemExit("비울 질문 id 가 없습니다.")
    path = RUNS / f"{args.run_id}.json"
    run = json.loads(path.read_text(encoding="utf-8"))
    backup_dir = ROOT / "data" / "comparison_runs_backup"   # 실행 폴더 밖 — 서버는 실행 폴더의 *.json 을 모두 실행으로 읽는다 (Codex 검토)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{args.run_id}.before-null.json"
    if not backup.exists():
        backup.write_bytes(path.read_bytes())
    cleared = {}
    for provider, rows in run["datasets"].items():
        n = 0
        for i, r in enumerate(rows):
            if r and int(str(r["case_id"]).split("_T")[0][1:]) in ids:
                rows[i] = None; n += 1
        cleared[provider] = n
        prog = run.get("progress", {}).get(provider)
        if isinstance(prog, dict):   # 남은 행에서 상태별로 다시 센다 (INCOMPLETE 는 failed 에 넣지 않는다 — Codex 검토)
            statuses = [(r.get("collection") or {}).get("status") for r in rows if r]
            prog["done"] = len(statuses)
            prog["success"] = statuses.count("SUCCESS")
            prog["incomplete"] = statuses.count("INCOMPLETE")
            prog["failed"] = len(statuses) - prog["success"] - prog["incomplete"]
    run["done"] = sum(1 for rows in run["datasets"].values() for r in rows if r)
    # 세트의 새 지문 — 이어서 수집(_prefill_from)은 지문이 같아야 앞 실행의 답을 가져온다
    set_id = (run.get("question_set") or {}).get("set_id")
    if set_id:
        from webapp.comparison_service import fingerprint
        from webapp.question_generation import flatten
        groups = json.loads((SETS / set_id / "questions.json").read_text(encoding="utf-8"))
        cases = [{k: r[k] for k in ("case_id", "question", "reference", "attachments", "conversation_id", "turn") if k in r} for r in flatten(groups)]
        run["dataset_fingerprint"] = fingerprint(cases)
    run["revision"] = run.get("revision", 0) + 1
    path.write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
    print("비운 행:", cleared, "| 질문", sorted(ids), "| 원본:", backup.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
