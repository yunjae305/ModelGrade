"""Opt-in nine-case GPT judge check; never a full 272-answer re-evaluation.

Local validation only: python -X utf8 tests/live_gpt_judge_switch_check.py --self-test
Actual calls (explicit opt-in): add --live instead. Costs print only to stdout.
Saved answers and complete preceding conversation turns are sent without rewriting.
"""
from __future__ import annotations

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from code_eval.jev_eval import build_history, conversation_contexts, evaluate_jev_case

REAL_CASES = (("claude", "Q9_T1"), ("aiona", "Q25_T1"), ("gpt", "Q23_T1"),
              ("gemini", "Q30_T26"), ("gpt", "Q17_T1"), ("gemini", "Q28_T1"))
DEFAULT_SOURCE = ROOT / "data/comparison_runs/65d50fe3b23d.json"


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def assert_private(value):
    """Reject runtime money/credentials, preserving currency in original Q/A text."""
    forbidden = {"accounting", "costparts", "costledger", "costledgers", "llmcost", "llmcosts",
                 "costreport", "apikey", "authorization", "cookie", "credentials"}
    markers = ("[LLM 비용]", "추정 합계: $", "USD | 공개 단가 기준", "RUNTIME_ONLY_MONEY_METADATA")
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in forbidden or any(part in normalized for part in ("costusd", "totalusd", "usdtotal")):
                raise AssertionError("Private runtime field in artifact: " + str(key))
            if normalized in {"usage", "usagerequests"}:
                serialized_keys = _all_keys(item)
                if any(any(word in k.lower() for word in ("cost", "usd", "price", "billing")) for k in serialized_keys):
                    raise AssertionError("Monetary usage metadata in artifact")
            assert_private(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_private(item)
    elif isinstance(value, str):
        if any(marker in value for marker in markers) or re.search(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}", value):
            raise AssertionError("Terminal report or credential in artifact")


def _all_keys(value):
    if isinstance(value, dict):
        return list(map(str, value)) + [key for item in value.values() for key in _all_keys(item)]
    if isinstance(value, (list, tuple)):
        return [key for item in value for key in _all_keys(item)]
    return []


def save_json(path, value):
    assert_private(value)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def unique_by_id(rows, case_id):
    found = [(i, row) for i, row in enumerate(rows) if row.get("case_id") == case_id]
    if len(found) != 1:
        raise ValueError(f"Expected exactly one {case_id}; found {len(found)}")
    return found[0]


def select_cases(source):
    selected = []
    for provider, case_id in REAL_CASES:
        dataset = source["datasets"][provider]
        batch = source["evaluations"][provider]
        index, saved = unique_by_id(dataset, case_id)
        code_index, code_case = unique_by_id(batch["cases"], case_id)
        _, old = unique_by_id(batch["jev_eval"]["records"], case_id)
        if any(saved.get(key) != code_case.get(key) for key in ("question", "answer")):
            raise ValueError(f"Saved dataset/CODE original mismatch: {provider}/{case_id}")
        ctx = conversation_contexts(batch["cases"]).get(code_index)
        history, truncated = build_history(ctx["prior"] if ctx else [])
        if truncated or (ctx and not ctx["complete"]):
            raise ValueError(f"Incomplete original history: {provider}/{case_id}")
        # Check every history string against the saved dataset, not only CODE output.
        if ctx:
            prior = sorted((row for row in dataset if row.get("conversation_id") == ctx["id"]
                            and row.get("turn", 0) < ctx["turn"]), key=lambda row: row["turn"])
            expected = [{key: row[key] for key in ("turn", "question", "answer")} for row in prior]
            if history != expected or len(history) != ctx["turn"] - 1:
                raise ValueError(f"History differs from saved original: {provider}/{case_id}")
        criteria = list(dict.fromkeys(old.get("criteria") or []))
        if "J5" not in criteria:
            criteria.append("J5")  # Re-screen a historical NOT_APPLICABLE decision.
        if ctx and "J6" not in criteria:
            criteria.append("J6")
        selected.append({"id": f"{provider}-{case_id}", "kind": "saved", "provider": provider,
                         "case_id": case_id, "source_index": index, "saved_dataset": copy.deepcopy(saved),
                         "question": saved["question"], "answer": saved["answer"],
                         "reference": saved.get("reference"), "history": history,
                         "history_truncated": False, "criteria": criteria,
                         "code_case": copy.deepcopy(code_case), "old_record": copy.deepcopy(old),
                         "expected_j5": None, "expect_web": False})
    for case_id, question, answer, expected, web in (
        ("SYNTH_PYTHON_LIST_TRUE", "Python의 list 객체는 변경 가능한 자료형인가요?",
         "Python의 list는 변경 가능한(mutable) 자료형입니다. 생성한 뒤에도 요소를 바꿀 수 있습니다.", "PASS", True),
        ("SYNTH_PYTHON_LIST_FALSE", "Python의 list 객체는 변경 가능한 자료형인가요?",
         "Python의 list는 변경 불가능한(immutable) 자료형입니다. 생성한 뒤에는 요소를 바꿀 수 없습니다.", "FAIL", True),
        ("SYNTH_PRIVATE_97", "공개되지 않은 사내 가상 제품 Project Nacre-Private-97의 실측 정확도는 얼마인가요? "
         "성능 측정 자료는 제공되지 않았습니다.",
         "Project Nacre-Private-97의 실측 정확도는 97%입니다.", "UNVERIFIED", False),
    ):
        selected.append({"id": case_id, "kind": "synthetic", "provider": "fixture", "case_id": case_id,
                         "question": question, "answer": answer, "reference": None, "history": [],
                         "history_truncated": False, "criteria": ["J5"], "code_case": None,
                         "old_record": None, "expected_j5": expected, "expect_web": web})
    assert_private(selected)
    return selected


def observation(item, record, elapsed):
    decisions = record.get("decisions") or {}
    j5 = decisions.get("J5") or {}
    fact = j5.get("fact_check") or {}
    claims = fact.get("claims") or []
    sources = [source for claim in claims for source in claim.get("sources", [])]
    verification = record.get("verification") or {}
    actual = j5.get("decision") or j5.get("status") or "MISSING"
    failures, notes = [], []
    if record.get("status") != "SUCCESS":
        failures.append("Evaluation did not return SUCCESS")
    expected = item["expected_j5"]
    if expected and actual != expected:
        if expected == "UNVERIFIED" and actual == "NOT_APPLICABLE":
            notes.append("Coverage difference: unsupported internal-performance assertion was NOT_APPLICABLE, expected UNVERIFIED")
        else:
            failures.append(f"J5 expected {expected}, got {actual}")
    if item["expect_web"]:
        if verification.get("search_performed") is not True:
            failures.append("Expected provider-recorded web search")
        supported = any(claim.get("verdict") == expected and any(
            source.get("quote_verified") is True and source.get("origin", "web") == "web"
            for source in claim.get("sources", [])) for claim in claims)
        if not supported:
            failures.append("Expected a matching claim with a verified web quotation")
    if item["history"] and "J6" not in decisions:
        failures.append("Multi-turn case is missing J6")
    if record.get("attempts", 0) > 1:
        failures.append("Unexpected automatic retry")
    return {"id": item["id"], "kind": item["kind"], "provider": item["provider"], "case_id": item["case_id"],
            "status": record.get("status"), "model": record.get("model"), "attempts": record.get("attempts"),
            "latency_ms": record.get("latency_ms"), "wall_s": round(elapsed, 3),
            "decisions": {key: {"decision": val.get("decision"), "status": val.get("status"),
                                 "reason": val.get("reason")} for key, val in decisions.items()},
            "expected_j5": expected, "j5_actual": actual, "j5_basis": record.get("j5_basis"),
            "search_performed": verification.get("search_performed"),
            "source_validation": verification.get("source_validation"), "timings_ms": verification.get("timings_ms"),
            "source_count": len(sources), "body_quote_verified": sum(s.get("quote_verified") is True for s in sources),
            "web_quote_verified": sum(s.get("quote_verified") is True and s.get("origin", "web") == "web" for s in sources),
            "history_length": len(item["history"]),
            "history_chars": sum(len(h["question"]) + len(h["answer"]) for h in item["history"]),
            "history_truncated": False, "criteria_requested": item["criteria"], "failures": failures, "notes": notes}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="Authorize exactly these nine actual evaluator calls")
    mode.add_argument("--self-test", action="store_true", help="Local parse/select/privacy tests only; no client resolution")
    result.add_argument("--source-run", type=Path, default=DEFAULT_SOURCE)
    result.add_argument("--output", type=Path)
    result.add_argument("--concurrency", type=int, choices=(1, 2, 3), default=3)
    result.add_argument("--call-timeout", type=float, default=180)
    result.add_argument("--timeout", type=float, default=900, help="Whole-check cancellation deadline in seconds")
    return result


def self_test(source):
    before = digest(json.dumps(source, ensure_ascii=False, sort_keys=True).encode())
    selected = select_cases(source)
    assert len(selected) == 9 and len([item for item in selected if item["kind"] == "saved"]) == 6
    multi = next(item for item in selected if item["id"] == "gemini-Q30_T26")
    assert len(multi["history"]) == 25 and multi["history"][-1]["turn"] == 25 and "J6" in multi["criteria"]
    assert all(set(h) == {"turn", "question", "answer"} for h in multi["history"])
    assert before == digest(json.dumps(source, ensure_ascii=False, sort_keys=True).encode())
    test_source = copy.deepcopy(source)
    first = test_source["evaluations"]["claude"]["jev_eval"]["records"]
    _, old = unique_by_id(first, "Q9_T1")
    old["criteria"] = [c for c in old["criteria"] if c != "J5"]
    assert "J5" in select_cases(test_source)[0]["criteria"]
    assert_private({"answer": "USD 97 / $97 원문", "usage": {"input_tokens": 4}})
    for leak in ({"accounting": {}}, {"cost_usd": 1}, {"usage": {"total_cost": 3}},
                 {"api_key": "hidden"}, {"nested": ["[LLM 비용] report"]}):
        try:
            assert_private(leak)
        except AssertionError:
            continue
        raise AssertionError("Privacy guard accepted a forbidden field")
    assert parser().parse_args(["--self-test"]).self_test
    assert parser().parse_args(["--live"]).concurrency == 3
    print("PASS: parse/select/privacy; 6 saved + 3 synthetic, Gemini full 25-turn history, original preservation. No model calls.")


def main():
    args = parser().parse_args()
    if not 0 < args.call_timeout <= 600 or not 0 < args.timeout <= 3600:
        raise ValueError("Bounded limits required: call-timeout <= 600s and timeout <= 3600s")
    source_path = args.source_run.resolve(strict=True)
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    if args.self_test:
        self_test(source)
        assert source_bytes == source_path.read_bytes()
        return 0
    selected = select_cases(source)
    from code_eval.cost_reporting import CostLedger
    from code_eval.jev_client import resolve_jev_client

    ledger = CostLedger()
    cancel = threading.Event()
    run_id = "gpt-check-" + uuid.uuid4().hex[:12]
    status = "ERROR"
    timer = None
    output = None
    started = time.monotonic()
    try:
        def checked_resolver():
            client, config, reason = resolve_jev_client(env_path=ROOT / ".env")
            actual = (config.engine, config.model, config.reasoning_effort, config.web_search)
            expected = ("openai_api", "gpt-6.1-sol", "medium", "live")
            if actual != expected:
                raise ValueError(f"Judge configuration mismatch: {actual!r}; expected {expected!r}")
            if client is None:
                raise ValueError(reason or "Judge is not configured")
            config = replace(config, timeout=min(config.timeout, args.call_timeout))
            client.config = config
            if not getattr(client, "supports_cancellation", False):
                raise ValueError("Bounded cancellation requires a cancellable client")
            if config.to_manifest().get("max_attempts") != 1:
                raise ValueError("Automatic retries must be disabled")
            return client, config, reason

        client, config, _ = ledger.wrap_resolver(checked_resolver)()
        output = args.output or (ROOT / "results/gpt-semantic-check" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        output = output.resolve()
        output.mkdir(parents=True, exist_ok=False)
        summary = {"run_id": run_id, "status": "RUNNING", "scope": "Six saved answers and three synthetic checks only; not the full 272-answer evaluation",
                   "started_at": now(), "source_path": str(source_path), "source_run_id": source.get("run_id"),
                   "source_sha256_before": digest(source_bytes), "config": config.to_manifest(),
                   "concurrency": args.concurrency, "timeout_s": args.timeout, "automatic_retries": False,
                   "artifact_dir": str(output), "cases": []}
        for item in selected:
            folder = output / item["id"]
            folder.mkdir()
            save_json(folder / "input.json", {k: v for k, v in item.items() if k not in ("old_record", "code_case")})
            save_json(folder / "old-record.json", item["old_record"])
            save_json(folder / "code-case.json", item["code_case"])
        save_json(output / "summary.json", summary)
        print(json.dumps({"artifact_dir": str(output), "selected": 9, "concurrency": args.concurrency}, ensure_ascii=False), flush=True)
        timer = threading.Timer(args.timeout, cancel.set)
        timer.daemon = True
        timer.start()

        def evaluate(item):
            tick = time.monotonic()
            record = evaluate_jev_case(item["case_id"], item["question"], item["answer"], client,
                                       criteria=tuple(item["criteria"]), reference=item["reference"],
                                       history=item["history"] or None, code_case=item["code_case"], cancel_event=cancel)
            save_json(output / item["id"] / "new-record.json", record)
            return observation(item, record, time.monotonic() - tick)

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {pool.submit(evaluate, item): item for item in selected}
            try:
                for future in as_completed(futures):
                    row = future.result()
                    summary["cases"].append(row)
                    save_json(output / "summary.json", summary)
                    print(f'{row["id"]}: {row["status"]} / J5 {row["j5_actual"]} / {row["wall_s"]}s', flush=True)
            except BaseException:
                cancel.set()
                raise
        status = "TIMEOUT" if cancel.is_set() else "FAIL" if any(row["failures"] for row in summary["cases"]) else "PASS"
        summary["status"] = status
        return 0 if status == "PASS" else 1
    finally:
        cancel.set()
        if timer:
            timer.cancel()
        unchanged = source_bytes == source_path.read_bytes()
        if output is not None and "summary" in locals():
            summary.update(status=status, finished_at=now(), wall_s=round(time.monotonic() - started, 3),
                           source_sha256_after=digest(source_path.read_bytes()), source_unchanged=unchanged)
            save_json(output / "summary.json", summary)
        ledger.finish(run_id, status)
        if not unchanged:
            raise AssertionError("Source run changed during the check")


if __name__ == "__main__":
    raise SystemExit(main())
