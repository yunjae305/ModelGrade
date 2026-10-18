"""평가가 끝난 비교 실행에서 의미 평가가 실패한 판정(시간 초과·API 오류·응답 형식 오류)만 다시 평가해 끼워 넣는다 (평가 API 호출 있음).

    python tools/reeval_failed.py <run_id>                 # 무엇을 다시 할지 보여 주기만
    python tools/reeval_failed.py <run_id> --apply         # 다시 평가하고 실행 파일에 반영 (서버가 멈춘 상태에서)
    옵션: --timeout 6000 (GPT-6 Luna 제한 시간, 초 — 기본은 .env SEMANTIC_API_TIMEOUT), --max-output 0 (GPT-6 Luna 출력 한도, 0 = 한도 없음 — 기본은 .env SEMANTIC_API_MAX_OUTPUT_TOKENS), --workers 6

- 대상: run.evaluations[provider].jev_eval.records 중 status 가 TIMEOUT·API_ERROR·INVALID_RESPONSE 인 턴.
- 대화(conversation_id)는 앞 턴 이력이 판정에 들어가므로 그 대화의 모든 턴을 한 배치로 다시 보내고, 실패했던 턴의 레코드만 바꾼다.
- 바꾼 뒤 그 모델의 jev_eval 요약(summary·completed·review_required…)과 combined 를 다시 계산한다. 원본은 <run>.before-reeval.json.
- 같은 병행 판정 엔진(.env SEMANTIC_ENGINE)·같은 의미 평가 방식(legacy)·같은 semantic_context 규칙(질문 세트 실행: answer_tools unknown)을 쓴다.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RUNS = ROOT / "data" / "comparison_runs"
FAILED = ("TIMEOUT", "API_ERROR", "INVALID_RESPONSE")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--timeout", type=float, default=None)
    ap.add_argument("--max-output", type=int, default=None)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)
    if args.timeout:
        os.environ["SEMANTIC_API_TIMEOUT"] = str(int(args.timeout))   # resolve_jev_client 는 환경변수를 .env 보다 먼저 읽는다
    if args.max_output is not None:
        os.environ["SEMANTIC_API_MAX_OUTPUT_TOKENS"] = str(args.max_output)   # 추론이 출력 한도를 다 써 '응답 미완료'가 난 판정용 (2026-10-19 GPT Q11_T4: 32,000 중 추론 29,766)
    path = RUNS / f"{args.run_id}.json"
    run = json.loads(path.read_text(encoding="utf-8"))
    plan = {}
    for provider, batch in (run.get("evaluations") or {}).items():
        if (batch.get("jev_eval") or {}).get("pipeline", "legacy") != "legacy":
            raise SystemExit(f"{provider}: legacy 가 아닌 의미 평가 방식은 다시 평가하지 않습니다 (판정 형식이 섞임).")
        records = (batch.get("jev_eval") or {}).get("records") or []
        cases = batch.get("cases") or []
        failed = [i for i, r in enumerate(records) if isinstance(r, dict) and r.get("status") in FAILED]
        if not failed:
            continue
        # 대화는 모든 턴을 함께 — 실패한 턴의 대화 id 를 모은다
        rows = run["datasets"][provider]   # 대화 번호는 데이터셋 행의 conversation_id (평가 케이스는 conversation.id 로 바뀌어 있다 — Codex 검토)
        conv_of = lambda i: (rows[i] or {}).get("conversation_id") if i < len(rows) else None
        convs = {conv_of(i) for i in failed if conv_of(i)}
        send = sorted({i for i in failed} | {j for j in range(len(rows)) if conv_of(j) in convs})
        plan[provider] = {"failed": failed, "send": send}
        print(f"{provider}: 실패 {len(failed)}턴 {[cases[i].get('case_id') for i in failed][:12]} → 보낼 턴 {len(send)}")
    if not plan:
        print("다시 평가할 판정이 없습니다."); return 0
    if not args.apply:
        return 0
    from code_eval.jev_client import resolve_jev_client
    from code_eval.jev_eval import summarize_records, conversation_contexts, _overall
    from code_eval.combined import combined_summary
    from webapp.code_eval_service import CodeEvalService
    from code_eval import attachment_images
    backup_dir = ROOT / "data" / "comparison_runs_backup"   # 실행 폴더 밖 — 서버가 실행 폴더의 *.json 을 실행으로 읽는다
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{args.run_id}.before-reeval.json"
    if not backup.exists():
        backup.write_bytes(path.read_bytes())
    # 원래 평가와 같은 평가자(엔진·모델·추론 수준)일 때만 — 다른 평가자 판정이 섞이면 실행 표시와 어긋난다 (Codex 검토). 제한 시간은 달라도 됨
    def signature(manifest):
        m = manifest or {}
        if m.get("engine") == "dual":
            luna, jev = m.get("luna") or {}, m.get("jev") or {}
            return ("dual", luna.get("requested_model"), luna.get("reasoning_effort"), jev.get("requested_model"))
        return (m.get("engine"), m.get("requested_model"), m.get("reasoning_effort"))
    _, current_config, reason = resolve_jev_client(env_path=ROOT / ".env")
    if reason:
        raise SystemExit(f"평가자를 준비할 수 없습니다: {reason}")
    current = signature(current_config.to_manifest())
    for provider in plan:
        saved = signature((run["evaluations"][provider].get("jev_eval") or {}).get("client"))
        if saved != current:
            raise SystemExit(f"{provider}: 원래 평가자 {saved} 와 지금 설정 {current} 이 달라 다시 평가하지 않습니다.")
    set_id = (run.get("question_set") or {}).get("set_id")
    roots = [str(ROOT / "data" / "question_sets" / str(set_id) / "files"), *map(str, attachment_images.DEFAULT_ROOTS)]
    service = CodeEvalService(root=ROOT, save_dir=ROOT / "results" / "reeval",
                              jev_resolver=lambda **kw: resolve_jev_client(env_path=ROOT / ".env", **kw))
    for provider, p in plan.items():
        batch = run["evaluations"][provider]
        cases = batch["cases"]
        src = run["datasets"][provider]
        # 원래 행(질문·답·첨부)으로 다시 만든 케이스 — 평가 결과 batch.cases 는 CODE 결과가 붙은 모양이라 데이터셋 행을 쓴다
        send_rows = [src[i] for i in p["send"]]
        ctx = {"answer_tools": "unknown", "collected_at": (run.get("collection_completed_at") or "")[:10] or None, "attachment_roots": roots}
        started = time.time()
        fresh = service.evaluate({"cases": send_rows, "aiona_specialized": False}, semantic_workers=args.workers, pipeline="legacy",
                                 semantic_context=ctx)
        new_records = (fresh.get("jev_eval") or {}).get("records") or []
        by_case = {r.get("case_id"): r for r in new_records if isinstance(r, dict)}
        replaced, still = 0, []
        for i in p["failed"]:
            cid = cases[i].get("case_id")
            r = by_case.get(cid)
            if r is None or r.get("status") not in ("SUCCESS",) and r.get("status") not in FAILED:
                still.append(cid); continue   # 준비 실패(NOT_CONFIGURED)·중단(SKIPPED)은 원본 실패 레코드를 그대로 둔다 — 다음 실행이 다시 잡게 (Codex 검토)
            batch["jev_eval"]["records"][i] = r
            replaced += 1
            if r.get("status") in FAILED:
                still.append(cid)
        final = [r for r in batch["jev_eval"]["records"] if isinstance(r, dict)]
        summarized = summarize_records(final, conversation_contexts(cases), cases, pipeline=batch["jev_eval"].get("pipeline") or "legacy")
        statuses = defaultdict(int)
        for r in final:
            statuses[r["status"]] += 1
        batch["jev_eval"]["status"] = _overall(final)   # 모두 성공하면 PARTIAL 이 풀린다 (Codex 검토)
        batch["jev_eval"].update(summary=summarized["summary"], completed=summarized["completed"], review_required=summarized["review_required"],
                                 unverified=summarized["unverified"], incomplete=summarized["incomplete"], reevaluated=summarized["reevaluated"],
                                 conversations=summarized["conversations"], statuses=dict(statuses))
        batch["jev_eval"].setdefault("reeval", []).append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "replaced": replaced, "still_failed": still,
                                                          "timeout_s": args.timeout, "max_output_tokens": args.max_output})
        batch["combined"] = combined_summary(batch)
        print(f"{provider}: 바꿈 {replaced} · 여전히 실패 {len(still)} {still[:10]} · {int(time.time() - started)}초", flush=True)
    run["revision"] = run.get("revision", 0) + 1
    path.write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
    print("저장:", path, "| 원본:", backup.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
