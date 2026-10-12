"""Opt-in two-request regression review; never changes saved comparison results.

The original AIONA cancellation email exercises J5 input grounding. GPT's seventh
file-plan turn exercises J6 relevant-history quotations. --live is mandatory;
each target has one evaluator request, no recollection, retries or recovery.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from live_j5_recovery_check import digest, now, save

TARGETS = (("aiona", 1, "Q2_T1", "J5"), ("gpt", 34, "Q29_T7", "J6"))


def load_targets(run_id):
    from code_eval.jev_eval import build_history, conversation_contexts, jev_request

    if not re.fullmatch(r"[a-f0-9]{12}", run_id):
        raise ValueError("Expected a 12-character hexadecimal run ID")
    source = ROOT / "data" / "comparison_runs" / (run_id + ".json")
    raw = source.read_bytes()
    run = json.loads(raw)
    if run.get("run_id") != run_id or run.get("status") != "COMPLETED":
        raise ValueError("Expected a completed saved comparison run")
    targets = []
    for provider, index, case_id, criterion in TARGETS:
        original = run["datasets"][provider][index]
        batch = run["evaluations"][provider]
        case = batch["cases"][index]
        if case.get("case_id") != case_id or case.get("case_status") != "EVALUATED":
            raise ValueError("Expected the intended original regression case")
        if not all(original.get(key) == case.get(key) for key in ("question", "answer", "case_id")):
            raise ValueError("Saved original and evaluated case do not match")
        matches = [r for r in batch["jev_eval"]["records"] if r.get("case_id") == case_id]
        if len(matches) != 1:
            raise ValueError("Expected one semantic record for the original case")
        record = matches[0]
        initial = record["decisions"][criterion]
        if initial.get("status") != "UNVERIFIED":
            raise ValueError("Expected an original unverified regression target")
        context = conversation_contexts(batch["cases"]).get(index)
        history = []
        if context:
            if not context["complete"] or context["issues"]:
                raise ValueError("Cannot review incomplete original history")
            history = build_history(context["prior"])[0]
            saved_context = record.get("context") or {}
            if saved_context.get("history_turns") != len(history):
                raise ValueError("Original history length does not match the saved evaluation")
            # Validate every recovered turn against the original uploaded data,
            # not merely against a generated case summary.
            for previous in history:
                rows = [r for r in run["datasets"][provider]
                        if r.get("conversation_id") == context["id"] and r.get("turn") == previous["turn"]]
                if len(rows) != 1 or any(rows[0].get(k) != previous[k] for k in ("question", "answer")):
                    raise ValueError("Recovered history does not preserve original text")
        reference = case.get("reference")
        if original.get("reference") is not None and original["reference"] != reference:
            raise ValueError("Original reference does not match")
        request = jev_request(case["question"], case["answer"], (criterion,), reference=reference, history=history or None)
        if criterion == "J5" and not any("자료 요약" in c.get("reason", "")
                for c in initial.get("fact_check", {}).get("claims", [])):
            raise ValueError("J5 target does not reproduce the input-scope defect")
        if criterion == "J6" and not any(c.get("decision") == "UNVERIFIED" and not c.get("answer_quote")
                for c in initial.get("context_checks", [])):
            raise ValueError("J6 target does not reproduce empty current-answer quotations")
        targets.append({"provider": provider, "index": index, "case_id": case_id, "criterion": criterion,
                        "history_turns": len(history), "request": request, "initial": copy.deepcopy(initial),
                        "evaluation_time": record["verification"]["checked_at"]})
    return source, hashlib.sha256(raw).hexdigest(), targets


def one_call(client, config, target):
    cancel = threading.Event()
    timer = threading.Timer(config.timeout + 35, cancel.set)
    timer.daemon = True
    try:
        timer.start()
        return client._system_one_once(**target["request"], cancel_event=cancel,
                                       evaluation_time=target["evaluation_time"])
    finally:
        timer.cancel()
        cancel.set()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", required=True, action="store_true", help="Explicitly enable exactly two evaluator requests")
    parser.add_argument("--run-id", default="7c13f8299015")
    args = parser.parse_args()
    from code_eval.cost_reporting import CostLedger
    from code_eval.jev_client import resolve_jev_client
    from code_eval.semantic_api_client import SemanticApiClient

    source, source_hash, targets = load_targets(args.run_id)
    client, config, reason = resolve_jev_client(env_path=ROOT / ".env")
    if reason or not isinstance(client, SemanticApiClient) or config.model != "gpt-6.1-sol":
        raise ValueError("The configured gpt-6.1-sol semantic API evaluator is required")
    config = replace(config, evidence_recovery_rounds=0)
    client.config = config
    manifest = config.to_manifest()
    if manifest["max_requests"] != 1 or manifest["max_attempts"] != 1:
        raise ValueError("Each regression target must permit exactly one request")
    output = ROOT / "results" / "evaluation-status-review" / ("live-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    output.mkdir(parents=True, exist_ok=False)
    summary = {"status": "RUNNING", "started_at": now(), "source_run_id": args.run_id,
               "source_sha256_before": source_hash, "source_sha256_after": None, "source_unchanged": None,
               "request_limit": 2, "automatic_retries": False, "automatic_recovery": False,
               "config": manifest, "targets": copy.deepcopy(targets), "results": []}
    save(output / "summary.json", summary)
    print(json.dumps({"artifact_path": str(output / "summary.json"), "request_limit": 2,
                      "targets": [{k: t[k] for k in ("provider", "case_id", "criterion", "history_turns")} for t in targets]}), flush=True)
    ledger = CostLedger()
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(one_call, client, config, target): target for target in targets}
            for future in as_completed(futures):
                target = futures[future]
                call = future.result()
                ledger._semantic(config, call)
                if call.attempts > 1:
                    raise AssertionError("Regression review exceeded its per-target request limit")
                entry = (call.body or {}).get("answers", {}).get(target["criterion"], {})
                item = {k: target[k] for k in ("provider", "index", "case_id", "criterion", "history_turns")}
                item.update(status=call.status, choice=entry.get("choice"), body=call.body,
                            attempts=call.attempts, usage=call.usage, latency_ms=call.latency_ms,
                            http_status=call.http_status, error=call.error)
                summary["results"].append(item)
                save(output / "summary.json", summary)
                print(json.dumps({"provider": target["provider"], "criterion": target["criterion"],
                                  "status": call.status, "choice": entry.get("choice"),
                                  "attempts": call.attempts, "latency_ms": call.latency_ms}), flush=True)
        summary["status"] = "RESOLVED" if len(summary["results"]) == 2 and all(
            r["status"] == "OK" and r["choice"] in ("PASS", "FAIL") for r in summary["results"]) else "UNRESOLVED"
        return 0 if summary["status"] == "RESOLVED" else 1
    except BaseException as exc:
        summary.update(status="ERROR", error_type=type(exc).__name__)
        raise
    finally:
        after = digest(source)
        summary.update(finished_at=now(), wall_s=round(time.monotonic() - started, 3),
                       source_sha256_after=after, source_unchanged=after == source_hash)
        if after != source_hash:
            summary["status"] = "SOURCE_CHANGED"
        try:
            save(output / "summary.json", summary)
        finally:
            ledger.finish("evaluation-regression-" + args.run_id, summary["status"])
        if after != source_hash:
            raise RuntimeError("Source run changed during read-only review")


if __name__ == "__main__":
    raise SystemExit(main())
