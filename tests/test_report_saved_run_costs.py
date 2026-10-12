"""Saved usage can be repriced offline without fabricating missing billing data."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from report_saved_run_costs import BROKEN_CHAIN_SKIP, main, rebuild_ledger


def usage(**changes):
    return {"input_tokens": 1000, "output_tokens": 100,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            "service_tier": "standard", "inference_geo": "global", **changes}


def part(**changes):
    return {"model": "claude-sonnet-5-5", "usage": usage(), "web_search_calls": 0,
            "service_tier": "standard", "inference_geo": "global", **changes}


def record(parts=None, **changes):
    result = {"status": "SUCCESS", "attempts": 1, "model": "claude-sonnet-5-5",
              "usage": usage(), "verification": {}, **changes}
    if parts is not None:
        result["verification"]["usage_requests"] = parts
    return result


def run(records=(), rows=()):
    return {"run_id": "fixture", "status": "COMPLETED",
            "models": {"claude": "claude-sonnet-5-5"}, "datasets": {"aiona": [], "claude": list(rows)},
            "evaluations": {"aiona": {"jev_eval": {"engine": "anthropic_api", "records": list(records)}}}}


def printed(data, capsys):
    original = copy.deepcopy(data)
    ledger = rebuild_ledger(data)
    ledger.finish(data["run_id"], data["status"])
    ledger.finish(data["run_id"], data["status"])
    assert data == original
    text = capsys.readouterr().out
    assert text.count("[LLM 비용]") == 1
    return text


def test_collection_raw_and_multiple_semantic_requests_are_counted_once(tmp_path, capsys):
    # First request caches 200 tokens at 1h; second hits 100 tokens in US.
    first = part(usage=usage(cache_creation_input_tokens=200,
                            cache_creation={"ephemeral_1h_input_tokens": 200, "ephemeral_5m_input_tokens": 0}),
                 web_search_calls=2)
    second = part(usage=usage(cache_read_input_tokens=100), inference_geo="us")
    raw = {"model": "claude-sonnet-5-5", "usage": usage(), "content": [{"text": "PRIVATE ORIGINAL"}]}
    data = run([record([first, second], attempts=2, usage=usage(input_tokens=999999999))],
               [{"question": "PRIVATE QUESTION", "collection": {"status": "SUCCESS", "raw": raw}}])
    # These duplicate display/log representations must never be priced again.
    data["evaluation_log"] = [{"usage": usage(input_tokens=999999999)}]
    text = printed(data, capsys)
    assert "수집 | claude | claude-sonnet-5-5 | 추정액 $0.003000 | 호출 1건" in text
    assert "평가 | anthropic_api | claude-sonnet-5-5 | 추정액 $0.027111 | 호출 2건" in text
    assert "추정 합계: $0.030111" in text
    assert "PRIVATE" not in text and "미확인" not in text


@pytest.mark.parametrize("changes,reason", [
    ({"web_search_calls": None}, "검색 사용량 미확인"),
    ({"usage": usage(cache_creation_input_tokens=200)}, "캐시 저장 기간별 사용량 미확인"),
    ({"usage": usage(inference_geo=None), "inference_geo": None}, "추론 지역별 단가 미확인"),
    ({"service_tier": "priority"}, "서비스 등급 단가 미확인"),
    ({"usage": usage(service_tier=None), "service_tier": None}, "서비스 등급 미기록"),
    ({"usage": {}}, "사용량 미확인"),
])
def test_missing_provider_fields_never_become_zero_or_standard(capsys, changes, reason):
    text = printed(run([record([part(), part(**changes)], attempts=2)]), capsys)
    assert "추정액 $0.003000 | 호출 2건" in text
    assert "확인 가능한 부분 합계: $0.003000 | 미확인 1건 제외" in text and reason in text


def test_legacy_successful_aggregate_cannot_recover_missing_request_metadata(capsys):
    old = record(attempts=2)
    old["verification"] = {"search_performed": False, "search_calls": 0}
    text = printed(run([old]), capsys)
    assert "확인 불가 | 호출 2건 | 미확인 2건 (요청별 사용량 미기록)" in text
    assert "확인 가능한 부분 합계: $0.000000 | 미확인 2건 제외" in text


def test_cancelled_partial_requests_and_absent_parts_remain_unknown(capsys):
    data = run([record([part(), part(usage={}, web_search_calls=None)], status="SKIPPED", attempts=3),
                record([], status="SKIPPED", attempts=0)])
    text = printed(data, capsys)
    assert "추정액 $0.003000 | 호출 3건" in text
    assert "미확인 2건 제외" in text


def test_nonstarted_and_broken_dialogue_rows_do_not_invent_calls(capsys):
    text = printed(run([record(status="NOT_CONFIGURED", attempts=0)], [None,
        {"collection": {"status": "ERROR", "error": BROKEN_CHAIN_SKIP}},
        {"collection": {"status": "ERROR", "error": "HTTP 429"}}]), capsys)
    assert "수집 | claude" in text and "호출 1건 | 미확인 1건" in text
    assert "평가 |" not in text


def test_cli_only_prints_terminal_output_and_never_changes_saved_file(tmp_path):
    path = tmp_path / "original.json"
    path.write_text(json.dumps(run([record([part()])]), ensure_ascii=False), encoding="utf-8")
    original = path.read_bytes()
    script = Path(__file__).with_name("report_saved_run_costs.py")
    finished = subprocess.run([sys.executable, "-B", "-X", "utf8", str(script), "--run", str(path)],
                              capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert finished.returncode == 0 and not finished.stderr
    assert "추정 합계: $0.003000" in finished.stdout and "더할 금액이 아닙니다" in finished.stdout
    assert path.read_bytes() == original and list(tmp_path.iterdir()) == [path]


def test_cli_does_not_echo_malformed_private_contents(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text('PRIVATE-CREDENTIAL-DO-NOT-LOG', encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["--run", str(path)])
    captured = capsys.readouterr()
    assert exc.value.code == 2 and "PRIVATE" not in captured.err and not captured.out


def openai_run(parts=None, **changes):
    entry = record(parts, model="gpt-6.1-sol", **changes)
    data = run([entry])
    data["evaluations"]["aiona"]["jev_eval"]["engine"] = "openai_api"
    return data


def openai_part(**changes):
    return {"model": "gpt-6.1-sol", "service_tier": "default", "web_search_calls": 2,
            "usage": {"input_tokens": 1000, "output_tokens": 100,
                "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 100},
                "output_tokens_details": {"reasoning_tokens": 75}}, **changes}


def test_openai_saved_parts_reprice_tokens_and_search_once(capsys):
    text = printed(openai_run([openai_part()]), capsys)
    assert "평가 | openai_api | gpt-6.1-sol | 추정액 $0.022670 | 호출 1건" in text
    assert "추정 합계: $0.022670" in text and "미확인" not in text


@pytest.mark.parametrize("changes,reason", [
    ({"web_search_calls": None}, "검색 사용량 미확인"),
    ({"service_tier": None}, "서비스 등급 미기록"),
    ({"service_tier": "priority"}, "서비스 등급 단가 미확인"),
    ({"model": "unknown-snapshot"}, "모델 단가 미확인"),
    ({"usage": {}}, "사용량 미확인"),
])
def test_openai_saved_missing_metadata_is_unknown(capsys, changes, reason):
    text = printed(openai_run([openai_part(**changes)]), capsys)
    assert "미확인 1건" in text and reason in text


def test_openai_legacy_aggregate_and_cancelled_unreported_parts_are_not_guessed(capsys):
    old = printed(openai_run(), capsys)
    assert "요청별 사용량 미기록" in old and "미확인 1건" in old
    partial = printed(openai_run([openai_part()], status="SKIPPED", attempts=2), capsys)
    assert "호출 2건" in partial and "미확인 1건 제외" in partial and "$0.022670" in partial
    not_started = printed(openai_run([], status="SKIPPED", attempts=0), capsys)
    assert "이번 작업의 LLM 호출 없음" in not_started
