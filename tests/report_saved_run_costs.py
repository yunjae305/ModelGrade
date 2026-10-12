"""Print estimates from a saved comparison run; never call providers or write files.

Usage: python -B -X utf8 tests/report_saved_run_costs.py --run PATH
Only collection response usage and saved per-request semantic usage are priced.
Legacy aggregate semantic usage cannot establish request-specific billing terms.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval.cost_reporting import CostLedger


BROKEN_CHAIN_SKIP = "앞선 대화 응답이 없어 이 턴의 수집을 건너뛰었습니다."
NO_CALL_STATUSES = {"SKIPPED", "NOT_CONFIGURED", "NOT_APPLICABLE", "INPUT_ERROR"}


def rebuild_ledger(run):
    """Reconstruct only the saved request records, without changing the run."""
    if not isinstance(run, dict) or not isinstance(run.get("datasets"), dict):
        raise ValueError("비교 실행 JSON의 datasets가 없습니다.")
    ledger = CostLedger()
    models = run.get("models") if isinstance(run.get("models"), dict) else {}
    for provider, rows in run["datasets"].items():
        if provider == "aiona":
            continue
        if not isinstance(rows, list):
            raise ValueError("수집 기록 배열의 형식이 올바르지 않습니다.")
        for row in rows:
            if row is None:  # No saved request or response; never invent one.
                continue
            if not isinstance(row, dict):
                raise ValueError("수집 기록의 형식이 올바르지 않습니다.")
            collection = row.get("collection")
            collection = collection if isinstance(collection, dict) else {}
            raw = collection.get("raw")
            if isinstance(raw, dict):
                ledger.record_collection(provider, models.get(provider) or row.get("source"), raw)
            elif collection.get("status") == "ERROR" and collection.get("error") == BROKEN_CHAIN_SKIP:
                # This exact stored application marker means no request was sent.
                continue
            else:
                ledger._record("수집", provider, models.get(provider) or row.get("source"),
                               reason="수집 요청별 사용량 미기록")

    evaluations = run.get("evaluations", {})
    if not isinstance(evaluations, dict):
        raise ValueError("평가 기록의 형식이 올바르지 않습니다.")
    for batch in evaluations.values():
        if not isinstance(batch, dict):
            raise ValueError("평가 묶음의 형식이 올바르지 않습니다.")
        semantic = batch.get("jev_eval")
        if semantic is None:
            continue
        if not isinstance(semantic, dict) or not isinstance(semantic.get("records", []), list):
            raise ValueError("의미 평가 기록의 형식이 올바르지 않습니다.")
        manifest = semantic.get("client")
        manifest = manifest if isinstance(manifest, dict) else {}
        engine = semantic.get("engine") or manifest.get("engine") or "unknown"
        for record in semantic.get("records", []):
            if record is None:
                continue
            if not isinstance(record, dict):
                raise ValueError("의미 평가 요청 기록의 형식이 올바르지 않습니다.")
            model = record.get("model") or manifest.get("requested_model") or "unknown"
            attempts = record.get("attempts")
            attempts = attempts if type(attempts) is int and 0 <= attempts <= 100 else None
            verification = record.get("verification")
            verification = verification if isinstance(verification, dict) else {}
            parts = verification.get("usage_requests")
            if engine in ("anthropic_api", "openai_api") and isinstance(parts, list):
                # An explicit empty list with zero attempts is a never-started call.
                # Missing parts remain unknown, even when a verdict was successful.
                if attempts == 0 and not parts:
                    continue
                count = attempts if attempts is not None else max(1, len(parts))
                eligible, missing_tier = [], 0
                for part in parts:
                    raw_usage = part.get("usage") if isinstance(part, dict) else None
                    tier = part.get("service_tier") if isinstance(part, dict) else None
                    if tier is None and isinstance(raw_usage, dict):
                        tier = raw_usage.get("service_tier")
                    if isinstance(part, dict) and tier is None:
                        # Runtime requests can force standard_only. A saved record
                        # without a tier does not establish that request setting.
                        ledger._record("평가", engine, part.get("model") or model,
                                       reason="서비스 등급 미기록")
                        missing_tier += 1
                    else:
                        eligible.append(part)
                count = max(0, count - missing_tier)
                call = SimpleNamespace(status=record.get("status"), attempts=count, usage={},
                    accounting={"model": model, "cost_parts": eligible, "unreported_attempts": 0})
                ledger._semantic(SimpleNamespace(engine=engine, model=model), call)
            elif attempts == 0 and record.get("status") in NO_CALL_STATUSES:
                continue
            else:
                # Aggregate usage loses per-request model, cache TTL, tier, region,
                # and search counts. A successful verdict cannot fill those gaps.
                for _ in range(max(1, attempts or 0)):
                    ledger._record("평가", engine, model, reason="요청별 사용량 미기록")
    return ledger


def main(argv=None):
    parser = argparse.ArgumentParser(description="저장된 요청 사용량으로 USD 비용을 터미널에만 재집계합니다.")
    parser.add_argument("--run", required=True, type=Path, help="저장된 comparison_runs JSON 경로")
    args = parser.parse_args(argv)
    try:
        run = json.loads(args.run.read_text(encoding="utf-8-sig"))
        ledger = rebuild_ledger(run)
    except (OSError, UnicodeError, ValueError, TypeError):
        # Do not echo provider payloads, questions, credentials, or malformed text.
        parser.exit(2, "실행 JSON을 읽거나 요청별 사용량을 해석하지 못했습니다.\n")
    print("저장 기록 비용 재집계: 현재 파일에 남은 요청만 포함합니다. 기록되지 않은 호출·재시도는 복원할 수 없습니다.")
    print("이전 터미널 출력에 더할 금액이 아닙니다. 기록이 부족한 요청은 미확인으로 표시합니다.")
    ledger.finish(run.get("run_id", "saved-run"), run.get("status", "unknown"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
