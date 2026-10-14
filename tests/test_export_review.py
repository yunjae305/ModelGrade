"""사람 정답 검수용 추출 — 원인 유형·원문 공유 범위·PASS 대조군·검수 파일을 정답으로 다시 읽기 (API 호출 없음)."""
import csv
import json

from tools.export_review import export_rows, main
from tools.sample_size_study import load_gold

LONG = "가" * 300


def _run():
    def record(n, decision):
        j5 = {"decision": decision}
        if decision == "FAIL":
            j5.update(reason_code="SOURCE_ORIGINAL_UNAVAILABLE", j5_outcome={
                "code": "source_unavailable", "label": "검증 기준 미충족 — 출처 원문 확인 실패", "factual_error_confirmed": False,
                "search": {"calls": 2, "limit": 4},
                "source_failure": {"kind": "access_failed", "label": "출처 원문 접근 실패",
                                   "access_failures": [{"status": "timeout", "url": "https://example.org/a"}]}})
        return {"case_id": f"Q{n}", "status": "SUCCESS", "criteria": ["J1", "J5"], "decisions": {"J1": {"decision": "PASS"}, "J5": j5}}
    cases = [{"case_id": f"Q{n}", "question": "질문 " + LONG, "answer": "답변 " + LONG} for n in range(1, 6)]
    records = [record(1, "FAIL")] + [record(n, "PASS") for n in range(2, 6)]
    return {"run_id": "r1", "datasets": {"gpt": [{"case_id": c["case_id"]} for c in cases]},
            "evaluations": {"gpt": {"cases": cases, "jev_eval": {"records": records, "client": {"requested_model": "judge"}}}}}


def test_fail_rows_keep_cause_and_text_scope_is_limited():
    rows = export_rows(_run(), include_pass=2)
    assert [r["decision"] for r in rows] == ["FAIL", "PASS", "PASS"]
    fail = rows[0]
    assert (fail["cause"], fail["factual_error_confirmed"], fail["failed_sources"]) == (
        "source_unavailable:access_failed", False, "timeout https://example.org/a")
    assert fail["cause_label"] == "검증 기준 미충족 — 출처 원문 확인 실패 (출처 원문 접근 실패)"
    assert fail["question"].startswith("질문 가") and "303자 중 120자" in fail["question"] and len(fail["answer"]) < 260
    hidden = export_rows(_run(), text="none")[0]
    assert hidden["question"].startswith("sha256:") and "가" not in hidden["answer"] and hidden["text_scope"] == "none"
    assert export_rows(_run(), text="full")[0]["answer"] == "답변 " + LONG


def test_filled_review_csv_is_read_back_as_gold(tmp_path, capsys):
    run = tmp_path / "run.json"
    run.write_text(json.dumps(_run(), ensure_ascii=False), encoding="utf-8")
    assert main([str(run), "--include-pass", "1", "--out-dir", str(tmp_path)]) == 0
    meta = json.loads(capsys.readouterr().out)
    assert meta["rows"] == 2 and meta["causes"] == {"source_unavailable:access_failed": 1, "pass": 1} and meta["judge_model"] == "judge"
    path = tmp_path / "r1-J5.csv"
    with open(path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows[0]["gold"] = "UNVERIFIABLE"                      # 검수자가 첫 행만 채움 — 둘째 행은 아직 검수 전
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    assert load_gold(path) == {("gpt", "Q1", "J5"): "UNVERIFIABLE"}
