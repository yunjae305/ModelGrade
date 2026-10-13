"""항목별 의미 평가 Mock E2E 시연 — CASE A~F 의 실제 코드 결과 검사, 네트워크 없음, Mock 표시와 HTML 이스케이프."""
import copy
import socket

import pytest

from tools.items_e2e_demo import ROOT, main, render_html, run_demo


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    return run_demo(tmp_path_factory.mktemp("items-e2e"))


def test_every_scenario_and_common_check_passes_on_the_real_code_path(demo):
    failed = [(s["id"], c["name"]) for s in demo["checks"] for c in s["checks"] if not c["ok"]]
    failed += [("공통", c["name"]) for c in demo["common_checks"] if not c["ok"]]
    assert failed == []
    assert demo["pipeline"] == "items" and demo["units"] == {"questions": 8, "turns": 10}
    assert demo["combined"] == {"questions": 10, "pass": 3, "fail": 6, "pending": 1,
                                "fail_code_only": 1, "fail_semantic_only": 5, "fail_both": 0}


def test_demo_opens_no_network_connection(tmp_path, monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("network connection attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    again = run_demo(tmp_path)
    assert all(c["ok"] for s in again["checks"] for c in s["checks"]) and list(tmp_path.glob("code-eval-*.json"))


def test_html_labels_mock_judgements_and_escapes_text(demo):
    tampered = copy.deepcopy(demo)
    tampered["scenarios"][0]["cases"][0]["question"] = "<script>alert(1)</script>"
    page = render_html(tampered)
    assert "<script" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "default-src 'none'" in page
    assert "Claude Opus 5.5 의 판정이 아닙니다" in page and "Mock Judge" in page and "실제 코드" in page


def test_cli_writes_the_report_without_touching_saved_runs(tmp_path, capsys):
    runs = ROOT / "data" / "code_eval_runs"
    before = set(runs.glob("*")) if runs.is_dir() else set()
    out = tmp_path / "demo.html"
    assert main(["--out", str(out)]) == 0
    assert "통과" in capsys.readouterr().out and "CASE E2" in out.read_text(encoding="utf-8")
    assert (set(runs.glob("*")) if runs.is_dir() else set()) == before   # 실행 기록은 임시 폴더에만 저장
