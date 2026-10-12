"""입력 숫자와 CLI 원문·출력 경로를 검증한다. 실제 모델 호출은 하지 않는다."""

import json
import threading
from urllib.request import Request, urlopen

import pytest

from code_eval import __main__ as cli
from code_eval.engine import evaluate_batch, evaluate_case
from code_eval.io import parse_input_text, write_json, write_jsonl
from webapp.code_eval_service import CodeEvalService
from webapp.server import Server


def _reject_constant(value):
    raise ValueError(value)


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e400", "-1e400"])
@pytest.mark.parametrize("jsonl", [False, True])
def test_nonfinite_input_rejects_only_bad_case_and_serializes_valid_results(number, jsonl, tmp_path):
    good = '{"id":"good","question":"q","answer":"a"}'
    bad = '{"id":"bad","question":"q","answer":"a","reference":{"nested":[{"value":' + number + '}]}}'
    text = f"{good}\n{bad}" if jsonl else f"[{good},{bad}]"
    items, _ = parse_input_text(text)
    batch = evaluate_batch(items)

    assert [c["case_status"] for c in batch["cases"]] == ["EVALUATED", "INPUT_ERROR"]
    assert batch["cases"][1]["case_id"] == "bad"
    assert "JSON 숫자" in batch["cases"][1]["input_error"]
    assert batch["summary"]["totals"]["input_errors"] == 1
    saved = write_json(tmp_path / "results.json", batch)
    assert json.loads(saved.read_text(encoding="utf-8"), parse_constant=_reject_constant)["summary"] == batch["summary"]
    lines = write_jsonl(tmp_path / "results.jsonl", batch["cases"]).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line, parse_constant=_reject_constant)["case_status"] for line in lines] == ["EVALUATED", "INPUT_ERROR"]


def test_direct_service_input_and_metadata_reject_nonfinite_values(tmp_path):
    service = CodeEvalService(save_dir=tmp_path / "runs")
    batch = service.evaluate({"cases": [
        {"question": "q", "answer": "a", "reference": {"value": float("nan")}},
        {"question": "q", "answer": "a", "source": {"score": float("inf")}},
        {"question": "q", "answer": "NaN and Infinity are examples.", "reference": {"value": "NaN"}},
    ]})
    assert [c["case_status"] for c in batch["cases"]] == ["INPUT_ERROR", "INPUT_ERROR", "EVALUATED"]
    assert "save_error" not in batch["run"]
    assert json.loads(json.dumps(batch, allow_nan=False))["cases"][2]["reference"] == {"value": "NaN"}


def test_http_result_with_overflow_input_is_valid_json_and_keeps_good_case(tmp_path):
    service = CodeEvalService(save_dir=tmp_path / "runs")
    server = Server(("127.0.0.1", 0), code_eval_service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        data = (b'{"cases":[{"question":"q","answer":"a"},'
                b'{"question":"q","answer":"a","reference":{"value":1e400}}]}')
        request = Request(f"http://127.0.0.1:{server.server_port}/api/code-eval/evaluate", data=data,
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            assert response.status == 200
            result = json.loads(response.read(), parse_constant=_reject_constant)
        assert [c["case_status"] for c in result["cases"]] == ["EVALUATED", "INPUT_ERROR"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_auto_save_failure_keeps_results_and_exposes_the_save_error(tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("keep this file", encoding="utf-8")
    batch = CodeEvalService(save_dir=blocked).evaluate({"question": "q", "answer": "a"})
    assert batch["cases"][0]["case_status"] == "EVALUATED"
    assert batch["run"]["save_error"]
    assert "saved_to" not in batch["run"]
    assert blocked.read_text(encoding="utf-8") == "keep this file"


@pytest.mark.parametrize("writer", [write_json, write_jsonl])
def test_strict_writers_do_not_replace_existing_file_with_nonfinite_json(writer, tmp_path):
    target = tmp_path / "preserved.json"
    target.write_text("existing result", encoding="utf-8")
    with pytest.raises(ValueError):
        writer(target, [{"score": float("nan")}], overwrite=True)
    assert target.read_text(encoding="utf-8") == "existing result"


@pytest.mark.parametrize("answer", [
    '{"text":"first\\nsecond"}',
    '```python\ntext = "first\\nsecond"\n```',
    'first\nsecond',
])
def test_cli_single_preserves_literal_escapes_and_actual_newlines(answer, monkeypatch):
    question = "JSON 또는 Python 예시를 보여줘."
    output = []
    monkeypatch.setattr(cli, "_print", output.append)
    assert cli.main(["single", "--question", question, "--answer", answer, "--json"]) == 0
    case = json.loads(output[0])["cases"][0]
    direct = evaluate_case({"question": question, "answer": answer})
    assert case["answer"] == answer
    assert case["results"] == direct["results"]
    assert not any(r["verdict"] == "FAIL" for r in case["results"] if "json_syntax" in r["check_id"])


@pytest.mark.parametrize("name", ["results.json", "results.jsonl", "eval_log.jsonl", "report.html"])
def test_cli_rejects_existing_output_before_code_or_model_call(name, monkeypatch, tmp_path):
    source = tmp_path / "input.json"
    source.write_text('{"question":"q","answer":"a"}', encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    existing = out / name
    existing.write_text("preserve me", encoding="utf-8")

    def unexpected(*args, **kwargs):
        pytest.fail("evaluation must not run when the output already exists")

    monkeypatch.setattr(cli, "evaluate_batch", unexpected)
    monkeypatch.setattr(cli, "jev_after_code", unexpected)
    with pytest.raises(FileExistsError):
        cli.main(["run", "--input", str(source), "--out", str(out)])
    assert existing.read_text(encoding="utf-8") == "preserve me"
    assert list(out.iterdir()) == [existing]


def test_cli_explicit_overwrite_still_writes_complete_results(monkeypatch, tmp_path):
    source = tmp_path / "input.json"
    source.write_text('{"question":"q","answer":"a"}', encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "results.json").write_text("old result", encoding="utf-8")
    monkeypatch.setattr(cli, "_print", lambda *_: None)

    assert cli.main(["run", "--input", str(source), "--out", str(out), "--overwrite"]) == 0

    assert {p.name for p in out.iterdir()} == {"results.json", "results.jsonl", "eval_log.jsonl", "report.html"}
    assert json.loads((out / "results.json").read_text(encoding="utf-8"))["cases"][0]["answer"] == "a"
