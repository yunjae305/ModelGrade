"""Opt-in two-request check of the semantic evaluator's static-prefix cache.

Run --self-test for local parser/privacy/result checks, with no model resolution.
Run --live to make exactly two sequential requests at most. No retry, collection,
customer data, server access, or web search. Monetary reporting stays in stdout.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STATES = (
    {"question": "제공된 가상 제품 안내문만 기준으로 루멘-A의 색상과 보증 기간을 한 문장으로 요약해 주세요.",
     "answer": "가상 제품 루멘-A의 색상은 파란색이며 보증 기간은 12개월입니다.",
     "reference": "가상 제품 안내문: 루멘-A. 색상은 파란색입니다. 보증 기간은 12개월입니다."},
    {"question": "다음 가상 안내문에 적힌 네오-B의 보증 기간 및 색상을 한 문장으로 정리해 주세요.",
     "answer": "가상 제품 네오-B의 보증 기간은 24개월이고 색상은 초록색입니다.",
     "reference": "가상 제품 안내문: 네오-B. 보증 기간은 24개월입니다. 색상은 초록색입니다."},
)
CRITERIA = ("J1", "J5")


def now():
    return datetime.now(timezone.utc).isoformat()


def assert_private(value):
    """Token counts are allowed; prices, runtime accounting and secrets are not."""
    forbidden = {"accounting", "costparts", "costledger", "costledgers", "llmcost", "llmcosts", "costreport",
                 "apikey", "authorization", "cookie", "credentials", "usd", "totalusd", "usdtotal"}
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in forbidden or any(word in normalized for word in ("cost", "price", "billing", "usd")):
                raise AssertionError("Private field in artifact: " + str(key))
            assert_private(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_private(item)
    elif isinstance(value, str):
        if any(marker in value for marker in ("[LLM 비용]", "추정 합계: $", "USD | 공개 단가 기준")):
            raise AssertionError("Terminal-only report in artifact")
        if re.search(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}", value):
            raise AssertionError("Credential-like value in artifact")


def save_json(path, value):
    assert_private(value)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def positive_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def observe(record, index, elapsed):
    usage = record.get("usage") or {}
    details = usage.get("input_tokens_details") or {}
    verification = record.get("verification") or {}
    decisions = record.get("decisions") or {}
    failures = []
    if record.get("status") != "SUCCESS":
        failures.append("Expected SUCCESS")
    for criterion in CRITERIA:
        if (decisions.get(criterion) or {}).get("decision") != "PASS":
            failures.append(f"Expected {criterion} PASS")
    if record.get("attempts") != 1:
        failures.append("Expected exactly one request without retry")
    if verification.get("search_performed") is not False:
        failures.append("Reference evaluation must record search_performed=false")
    if verification.get("source_urls"):
        failures.append("Reference evaluation unexpectedly recorded web source URLs")
    if record.get("j5_basis") != "reference":
        failures.append("Expected J5 reference evidence")
    parts = verification.get("usage_requests") or []
    if len(parts) != 1 or parts[0].get("web_search_calls") != 0:
        failures.append("Expected one provider usage record with zero web searches")
    if index == 2 and not positive_count(details.get("cached_tokens")):
        failures.append("Second request did not report positive input_tokens_details.cached_tokens")
    return {"request": index, "status": record.get("status"), "model": record.get("model"),
            "attempts": record.get("attempts"), "latency_ms": record.get("latency_ms"),
            "wall_s": round(elapsed, 3), "timings_ms": verification.get("timings_ms"),
            "decisions": {cid: {"decision": (decisions.get(cid) or {}).get("decision"),
                                "status": (decisions.get(cid) or {}).get("status")} for cid in CRITERIA},
            "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
            "cached_tokens": details.get("cached_tokens"), "cache_write_tokens": details.get("cache_write_tokens"),
            "search_performed": verification.get("search_performed"), "j5_basis": record.get("j5_basis"),
            "failures": failures}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="Explicitly authorize at most two sequential actual calls")
    mode.add_argument("--self-test", action="store_true", help="Local checks only; no resolver or external request")
    result.add_argument("--output", type=Path, help="New directory only; existing directories are never overwritten")
    result.add_argument("--timeout", type=float, default=180, help="Per-request limit, greater than zero and at most 180 seconds")
    return result


def self_test():
    assert parser().parse_args(["--self-test"]).self_test
    assert parser().parse_args(["--live"]).timeout == 180
    assert all(STATES[0][key] != STATES[1][key] for key in ("question", "answer", "reference"))
    assert_private({"usage": {"input_tokens": 2300, "output_tokens": 25,
                             "input_tokens_details": {"cached_tokens": 2000, "cache_write_tokens": 0}}})
    for leak in ({"accounting": {}}, {"cost_usd": 1}, {"usage": {"price": 3}},
                 {"api_key": "hidden"}, {"nested": ["[LLM 비용] report"]}):
        try:
            assert_private(leak)
        except AssertionError:
            continue
        raise AssertionError("Privacy guard accepted forbidden data")
    record = {"status": "SUCCESS", "attempts": 1, "model": "fixture", "j5_basis": "reference",
              "decisions": {cid: {"decision": "PASS"} for cid in CRITERIA},
              "usage": {"input_tokens": 2300, "output_tokens": 25,
                        "input_tokens_details": {"cached_tokens": 2000, "cache_write_tokens": 0}},
              "verification": {"search_performed": False, "source_urls": [], "usage_requests": [{"web_search_calls": 0}]}}
    assert not observe(record, 2, .1)["failures"]
    for cached in (0, None, False):
        negative = copy.deepcopy(record)
        negative["usage"]["input_tokens_details"]["cached_tokens"] = cached
        assert observe(negative, 2, .1)["failures"]
    negative = copy.deepcopy(record)
    negative["decisions"]["J5"] = {"decision": None, "status": "UNVERIFIED"}
    assert observe(negative, 2, .1)["failures"]
    print("PASS: parser/privacy/cache-result checks; two distinct synthetic reference states; no model calls.")


def main():
    args = parser().parse_args()
    if not 0 < args.timeout <= 180:
        raise ValueError("--timeout must be greater than zero and at most 180 seconds")
    if args.self_test:
        self_test()
        return 0

    from code_eval.cost_reporting import CostLedger
    from code_eval.jev_client import resolve_jev_client
    from code_eval.jev_eval import evaluate_jev_case

    output = (args.output or ROOT / "results/semantic-cache-check" /
              datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")).resolve()
    output.mkdir(parents=True, exist_ok=False)
    ledger, cancel = CostLedger(), threading.Event()
    run_id = "semantic-cache-" + uuid.uuid4().hex[:12]
    summary = {"run_id": run_id, "status": "ERROR", "started_at": now(), "artifact_dir": str(output),
               "scope": "Two distinct synthetic reference evaluations only; no customer data or web search",
               "request_limit": 2, "concurrency": 1, "automatic_retries": False,
               "per_request_timeout_s": args.timeout, "criteria": list(CRITERIA), "requests": []}
    tick = time.monotonic()
    save_json(output / "summary.json", summary)
    try:
        def checked_resolver():
            client, config, reason = resolve_jev_client(env_path=ROOT / ".env")
            expected = ("openai_api", "gpt-6.1-sol", "medium")
            actual = tuple(getattr(config, key, None) for key in ("engine", "model", "reasoning_effort"))
            if actual != expected:
                raise ValueError("Expected openai_api / gpt-6.1-sol / medium")
            if client is None:
                raise ValueError("Semantic evaluator credential is not configured")
            config = replace(config, timeout=min(config.timeout, args.timeout))
            client.config = config
            manifest = config.to_manifest()
            if manifest.get("prompt_cache_policy") != "explicit_static_prefix_v1":
                raise ValueError("Expected explicit_static_prefix_v1 manifest")
            if manifest.get("max_attempts") != 1 or not getattr(client, "supports_cancellation", False):
                raise ValueError("Evaluator must support cancellation and forbid retries")
            return client, config, reason

        client, config, _ = ledger.wrap_resolver(checked_resolver)()
        summary["config"] = config.to_manifest()
        summary["status"] = "RUNNING"
        save_json(output / "summary.json", summary)
        print(json.dumps({"artifact_dir": str(output), "request_limit": 2, "concurrency": 1}), flush=True)
        for index, source in enumerate(STATES, 1):
            state = copy.deepcopy(source)
            save_json(output / f"request-{index}-input.json", state)
            cancel.clear()
            timer = threading.Timer(config.timeout, cancel.set)
            timer.daemon = True
            timer.start()
            started = time.monotonic()
            try:
                record = evaluate_jev_case(f"CACHE_REFERENCE_{index}", state["question"], state["answer"], client,
                                           criteria=CRITERIA, reference=state["reference"], cancel_event=cancel)
            finally:
                timer.cancel()
            assert state == source, "Original synthetic input changed"
            save_json(output / f"request-{index}-record.json", record)
            row = observe(record, index, time.monotonic() - started)
            summary["requests"].append(row)
            save_json(output / "summary.json", summary)
            print(json.dumps({"request": index, "status": row["status"], "cached_tokens": row["cached_tokens"],
                              "cache_write_tokens": row["cache_write_tokens"], "latency_ms": row["latency_ms"]}), flush=True)
        summary["status"] = "FAIL" if any(row["failures"] for row in summary["requests"]) else "PASS"
        summary["second_request_cache_hit"] = positive_count(summary["requests"][1]["cached_tokens"])
        return 0 if summary["status"] == "PASS" else 1
    except BaseException as exc:
        summary["status"] = "ERROR"
        summary["error_type"] = type(exc).__name__  # Do not serialize exception strings or credentials.
        raise
    finally:
        cancel.set()
        summary.update(finished_at=now(), wall_s=round(time.monotonic() - tick, 3))
        try:
            save_json(output / "summary.json", summary)
        finally:
            ledger.finish(run_id, summary["status"])


if __name__ == "__main__":
    raise SystemExit(main())
