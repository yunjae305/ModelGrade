"""이미 만든 질문 세트에 질문당 기준 수(k_var) 고치기 단계만 다시 돌린다 — 재생성 없이 범위 밖 단일 질문의 조건만 고친다 (GPT 호출 있음).

    python tools/repair_k_range.py <세트 id> [--dry-run] [--rounds 8] [--batch 30]

- 세트 폴더(data/question_sets/<id>)의 questions.json 을 읽고, QuestionSetBuilder.repair_k_range(생성기와 같은 규칙)로 고친 뒤
  questions.json 을 제자리에 저장한다(원본은 questions.before-k-repair.json 으로 백업, 이미 있으면 덮어쓰지 않음). set.json 의 요약·경고도 갱신.
- 첨부·참조 자료는 그대로 두고 질문 본문·target_items 만 바뀐다. 커버리지가 떨어지는 고침은 되돌린다.
- --dry-run 이면 현재 k 분포만 보여 주고 호출하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp import question_generation as qg  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("set_id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rounds", type=int, default=qg.K_REPAIR_ROUNDS)
    ap.add_argument("--batch", type=int, default=qg.K_REPAIR_BATCH)
    args = ap.parse_args(argv)
    folder = qg.STORE / args.set_id
    qpath, spath = folder / "questions.json", folder / "set.json"
    groups = json.loads(qpath.read_text(encoding="utf-8"))
    job = json.loads(spath.read_text(encoding="utf-8")) if spath.exists() else {}
    report = qg.check(groups)
    before = qg.k_var_summary(groups, report)
    print("고치기 전:", {k: before[k] for k in ("singles", "in_range", "out_of_range", "exempt", "cap", "by_k")})
    if args.dry_run:
        return 0
    from code_eval.llm_client import env_value
    # 서버의 연결 설정(comparison_service._keys['gpt'])과 같은 모양 — key·model
    config = {"key": env_value("OPENAI_API_KEY", None, ROOT / ".env"), "model": env_value("OPENAI_MODEL", None, ROOT / ".env") or "gpt-6-luna"}
    if not config["key"]:
        raise SystemExit("OPENAI_API_KEY 가 없습니다 (.env)")
    usage: dict = {}
    builder = qg.QuestionSetBuilder(qg.gpt_llm(config, usage), folder)
    qg.K_REPAIR_ROUNDS, qg.K_REPAIR_BATCH = args.rounds, args.batch
    backup = folder / "questions.before-k-repair.json"
    if not backup.exists():
        backup.write_text(qpath.read_text(encoding="utf-8"), encoding="utf-8")
    catalog = qg.criteria_catalog()
    per_target = job.get("per_target") if isinstance(job.get("per_target"), int) else None
    report, rounds, after = builder.repair_k_range(groups, report, catalog, job.get("note") or "", per_target,
                                                   lambda rep: qg.target_counts(rep, True))
    qpath.write_text(json.dumps(groups, ensure_ascii=False, indent=1), encoding="utf-8")
    if job:
        job["summary"] = qg._summary(groups, report)
        job["coverage"] = report["coverage"]
        job["warnings"] = [w for w in job.get("warnings") or [] if "질문당 기준 수" not in w] + builder.warnings
        job["k_repair"] = {"rounds": rounds, "calls": builder.calls, "usage": usage, "before": {k: before[k] for k in ("in_range", "out_of_range", "by_k")}}
        spath.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    print("고치기 후:", {k: after[k] for k in ("singles", "in_range", "out_of_range", "exempt", "cap", "by_k")}, "| 회차", rounds, "| 호출", builder.calls, "| 사용량", usage)
    for w in builder.warnings:
        print("경고:", w[:300])
    return 0 if after["out_of_range"] <= after["cap"] else 1


if __name__ == "__main__":
    sys.exit(main())
