"""Opt-in, one-request J5 evidence review of an existing single-question run.

Requires --live. Saved runs are only read; the supplemental judgment is written
to a new results/j5-recovery-review directory. Never recollects model answers,
retries HTTP requests, changes original evaluations, or persists monetary data.
"""
from __future__ import annotations

import argparse
import copy
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


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_artifact_safe(value):
    """Retain evidence and token counts; monetary and credential fields stay out."""
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if (normalized in {"accounting", "apikey", "authorization", "cookie", "credentials"}
                    or any(word in normalized for word in ("cost", "price", "billing", "usd"))):
                raise ValueError("Disallowed private field in review artifact")
            assert_artifact_safe(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_artifact_safe(item)
    elif isinstance(value, str) and re.search(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}", value):
        raise ValueError("Credential-like value in review artifact")


def save(path, value):
    assert_artifact_safe(value)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def load_original(run_id, provider):
    from code_eval.jev_eval import jev_request

    if not re.fullmatch(r"[a-f0-9]{12}", run_id):
        raise ValueError("Expected a 12-character hexadecimal run ID")
    source = ROOT / "data" / "comparison_runs" / (run_id + ".json")
    raw = source.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    run = json.loads(raw.decode("utf-8"))
    if run.get("run_id") != run_id or run.get("status") != "COMPLETED" or run.get("question_count") != 1:
        raise ValueError("Expected a completed single-question saved run")
    rows = run.get("datasets", {}).get(provider)
    batch = run.get("evaluations", {}).get(provider) or {}
    cases = batch.get("cases")
    records = (batch.get("jev_eval") or {}).get("records")
    if not all(isinstance(items, list) and len(items) == 1 for items in (rows, cases, records)):
        raise ValueError("Expected exactly one saved input, evaluated case, and semantic record")
    original, case, record = rows[0], cases[0], records[0]
    if record.get("status") != "SUCCESS" or case.get("case_status") != "EVALUATED":
        raise ValueError("Expected a completed semantic record with an evaluated original case")
    if not all(original.get(key) == case.get(key) for key in ("question", "answer", "case_id")):
        raise ValueError("Saved original and evaluated case do not match")
    if record.get("case_id") != case.get("case_id"):
        raise ValueError("Semantic record does not identify the original case")
    if not all(isinstance(original.get(key), str) for key in ("question", "answer")):
        raise ValueError("Expected original question and answer strings")
    # A single-case run has no generated prior-turn history. Never manufacture
    # history from a summary or a different model's answers.
    context = record.get("context") or {}
    history = case.get("history")
    if context.get("history_turns", 0) and not history:
        raise ValueError("Exact original history is unavailable; refusing reconstruction")
    if history is not None and not isinstance(history, list):
        raise ValueError("Saved history must be an exact list of original turns")
    reference = case.get("reference")
    if original.get("reference") is not None and reference != original["reference"]:
        raise ValueError("Saved original reference and evaluated reference do not match")
    request = jev_request(original["question"], original["answer"], ("J5",), reference=reference, history=history)
    state = request["state"]
    initial = {"model": record.get("model"), "answers": {}, "verification": copy.deepcopy(record.get("verification") or {})}
    for cid, decision in (record.get("decisions") or {}).items():
        if not isinstance(decision, dict):
            raise ValueError("Malformed saved semantic decision")
        entry = {key: copy.deepcopy(value) for key, value in decision.items()
                 if key not in ("criterion_id", "name", "decision", "status")}
        entry["choice"] = decision.get("decision") or decision.get("status")
        initial["answers"][cid] = entry
    if initial["answers"].get("J5", {}).get("choice") != "UNVERIFIED":
        raise ValueError("Expected a saved J5 UNVERIFIED judgment")
    checked_at = initial["verification"].get("checked_at")
    if not isinstance(checked_at, str):
        raise ValueError("Expected the saved verification timestamp")
    datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    return source, source_hash, state, request["questions"], initial, checked_at


def claim_counts(entry):
    claims = (entry.get("fact_check") or {}).get("claims") or []
    return {"choice": entry.get("choice"), "claims": len(claims),
            **{value: sum(claim.get("verdict") == value for claim in claims) for value in ("PASS", "FAIL", "UNVERIFIED")}}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--live", action="store_true", required=True, help="Explicitly enable one supplemental external evaluator request")
    result.add_argument("--run-id", required=True, help="Completed saved single-question comparison run ID")
    result.add_argument("--provider", required=True, choices=("aiona", "gpt", "claude", "gemini"))
    return result


def main():
    args = parser().parse_args()
    from code_eval.cost_reporting import CostLedger
    from code_eval.jev_client import resolve_jev_client
    from code_eval.semantic_api_client import SemanticApiClient
    from code_eval.semantic_recovery import evidence_recovery_prompt, merge_evidence_recovery
    from code_eval.semantic_setup import semantic_setup

    source, source_hash, state, questions, initial, evaluation_time = load_original(args.run_id, args.provider)
    prompt = evidence_recovery_prompt(initial, state)
    if not prompt:
        raise ValueError("Saved J5 is not eligible for evidence recovery")
    setup = semantic_setup(env_path=ROOT / ".env")
    if setup.get("engine") != "openai_api" or setup.get("can_evaluate") is not True:
        raise ValueError("An explicitly configured OpenAI semantic evaluator is required")
    client, config, reason = resolve_jev_client(env_path=ROOT / ".env")
    if not isinstance(client, SemanticApiClient) or config.engine != "openai_api" or reason:
        raise ValueError("OpenAI semantic evaluator is unavailable")
    if config.web_search != "live":
        raise ValueError("J5 evidence recovery requires configured live web search")
    config = replace(config, timeout=config.evidence_recovery_timeout,
                     max_tool_calls=config.evidence_recovery_max_tool_calls, evidence_recovery_rounds=0)
    client.config = config
    if config.to_manifest()["max_requests"] != 1 or config.to_manifest()["max_attempts"] != 1:
        raise ValueError("Review must permit only one request without retries")
    output = ROOT / "results" / "j5-recovery-review" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, exist_ok=False)
    summary = {"status": "RUNNING", "started_at": now(), "source_run_id": args.run_id, "provider": args.provider,
        "source_sha256_before": source_hash, "source_sha256_after": None, "source_unchanged": None,
        "request_limit": 1, "automatic_retries": False, "evaluation_time": evaluation_time,
        "request": {"criteria": ["J5"], "state": copy.deepcopy(state), "config": config.to_manifest()},
        "original_initial": copy.deepcopy(initial), "supplemental": None, "merged_j5": None}
    save(output / "summary.json", summary)
    ledger, cancel = CostLedger(), threading.Event()
    timer = threading.Timer(config.timeout, cancel.set)
    timer.daemon = True
    started = time.monotonic()
    print(json.dumps({"artifact_path": str(output / "summary.json"), "request_limit": 1,
                      "before": claim_counts(initial["answers"]["J5"])}), flush=True)
    try:
        timer.start()
        # This private single-pass method is intentional: it cannot recursively
        # perform recovery, and is called exactly once by this review script.
        call = client._system_one_once(state, questions, cancel_event=cancel,
                                      evidence_prompt=prompt, evaluation_time=evaluation_time,
                                      prior_verification=initial["verification"])
        ledger._semantic(config, call)  # This request only; saved usage is never re-billed.
        if call.attempts > 1:
            raise AssertionError("Supplemental review exceeded one request")
        merged = merge_evidence_recovery(initial, call.body, state, questions) if call.status == "OK" else copy.deepcopy(initial)
        summary["supplemental"] = {"status": call.status, "body": call.body, "attempts": call.attempts,
            "usage": call.usage, "latency_ms": call.latency_ms, "http_status": call.http_status, "error": call.error}
        summary["merged_j5"] = merged["answers"]["J5"]
        summary["status"] = ("RESOLVED" if merged["answers"]["J5"].get("choice") in ("PASS", "FAIL")
                             else "UNRESOLVED" if call.status == "OK" else "ERROR")
        print(json.dumps({"before": claim_counts(initial["answers"]["J5"]), "after": claim_counts(summary["merged_j5"]),
                          "latency_ms": call.latency_ms, "attempts": call.attempts,
                          "artifact_path": str(output / "summary.json")}), flush=True)
        return 0 if summary["status"] == "RESOLVED" else 1
    except BaseException as exc:
        summary.update(status="ERROR", error_type=type(exc).__name__)
        raise
    finally:
        timer.cancel()
        cancel.set()
        after = digest(source)
        summary.update(finished_at=now(), wall_s=round(time.monotonic() - started, 3),
                       source_sha256_after=after, source_unchanged=after == source_hash)
        if after != source_hash:
            summary["status"] = "SOURCE_CHANGED"
        try:
            save(output / "summary.json", summary)
        finally:
            ledger.finish("j5-recovery-" + args.run_id, summary["status"])
        if after != source_hash:
            raise RuntimeError("Source run changed during read-only review")


if __name__ == "__main__":
    raise SystemExit(main())
