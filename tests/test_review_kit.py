"""사람 검수 키트 — 블라인드 검수표(시스템·AI 판정 숨김)·사람이 합의한 항목만 정답·검수자 불일치 기록 (API 호출 없음)."""
import csv
import json

import pytest

from tests.test_export_review import _run
from tools.review_kit import main, make, merge
from tools.sample_size_study import load_gold


def _sheet(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _blind(kit):
    with open(kit / "review_blind.csv", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_blind_sheet_hides_system_and_ai_verdict_and_mixes_controls(tmp_path):
    report = make(_run(), tmp_path / "kit", include_pass=2, seed=1)
    rows = _blind(tmp_path / "kit")
    assert report["items"] == 3 and report["ai_decisions"] == {"FAIL": 1, "PASS": 2}
    assert set(rows[0]) >= {"item_id", "question", "answer", "criterion_question", "label", "reviewer", "reviewed_at", "evidence"}
    assert not set(rows[0]) & {"system", "case_id", "ai_decision", "ai_reason", "ai_cause", "ai_failed_sources"}
    assert all(not r["label"] and not r["reviewer"] for r in rows)                     # 판정 칸은 비어 있다
    assert "gpt" not in json.dumps(rows, ensure_ascii=False)                          # 모델 이름도 없다
    key = json.loads((tmp_path / "kit" / "key.json").read_text(encoding="utf-8"))
    assert sorted(v["ai_decision"] for v in key["items"].values()) == ["FAIL", "PASS", "PASS"]
    with open(tmp_path / "kit" / "review_open.csv", encoding="utf-8-sig", newline="") as handle:
        assert {"system", "ai_decision", "ai_reason"} <= set(next(csv.DictReader(handle)))
    with pytest.raises(SystemExit):   # 검수 중인 키트를 덮어쓰지 않는다
        run = tmp_path / "run.json"
        run.write_text(json.dumps(_run(), ensure_ascii=False), encoding="utf-8")
        main(["make", str(run), "--out-dir", str(tmp_path / "kit")])


def test_merge_uses_only_human_labels_and_records_disagreement(tmp_path):
    kit = tmp_path / "kit"
    make(_run(), kit, include_pass=2, seed=1)
    rows = _blind(kit)
    a = [dict(r, reviewer="A", reviewed_at="2026-10-14 13:00", label=l) for r, l in zip(rows, ["정상", "실제 오류", "검증 불가"])]
    b = [dict(r, reviewer="B", reviewed_at="2026-10-14 13:30", label=l) for r, l in zip(rows, ["정상", "검증 불가", ""])]   # 셋째는 B 미검수
    _sheet(tmp_path / "a.csv", a)
    _sheet(tmp_path / "b.csv", b)
    report = merge(kit, [tmp_path / "a.csv", tmp_path / "b.csv"])
    assert report["status"] == {"합의": 1, "불일치": 1, "단독 검수(정답 제외)": 1} and report["gold"] == 1
    assert report["pairs"]["A~B"]["items"] == 2 and report["pairs"]["A~B"]["agreement"] == 0.5
    gold = load_gold(kit / "gold.jsonl")
    assert list(gold.values()) == ["PASS"]                                             # 사람이 합의한 '정상' 하나만
    with open(kit / "disagreements.csv", encoding="utf-8-sig", newline="") as handle:
        assert [(r["A"], r["B"]) for r in csv.DictReader(handle)] == [("실제 오류", "검증 불가")]
    single = merge(kit, [tmp_path / "a.csv"], allow_single=True)
    assert single["gold"] == 3 and single["status"] == {"단독 검수": 3}
    _sheet(tmp_path / "bad.csv", [dict(rows[0], reviewer="C", reviewed_at="", label="아마 정상")])
    with pytest.raises(ValueError, match="정상/실제 오류/검증 불가"):
        merge(kit, [tmp_path / "bad.csv"])


def test_adjudication_resolves_disagreements_and_compare_splits_agreement_errors_unverifiable_and_excluded(tmp_path):
    """지시서 10-14 3차 2단계 — 불일치는 합의 기록으로만 풀고, 대조는 적용 대상 일치율·오판정·검증 불가·적용 제외를 따로 센다."""
    from tools.review_kit import compare
    kit = tmp_path / "kit"
    make(_run(), kit, include_pass=2, seed=1)
    rows = _blind(kit)
    key = json.loads((kit / "key.json").read_text(encoding="utf-8"))["items"]
    by_ai = {key[r["item_id"]]["ai_decision"]: r["item_id"] for r in rows}            # FAIL 1·PASS 2 중 하나씩
    fail_id = by_ai["FAIL"]
    pass_ids = [r["item_id"] for r in rows if key[r["item_id"]]["ai_decision"] == "PASS"]
    labels_a = {fail_id: "정상", pass_ids[0]: "정상", pass_ids[1]: "적용 제외"}
    labels_b = {fail_id: "검증 불가", pass_ids[0]: "정상", pass_ids[1]: "적용 제외"}
    for name, labels in (("a", labels_a), ("b", labels_b)):
        _sheet(tmp_path / f"{name}.csv", [dict(r, reviewer=name.upper(), reviewed_at="2026-10-14 13:00", label=labels[r["item_id"]]) for r in rows])
    first = merge(kit, [tmp_path / "a.csv", tmp_path / "b.csv"])
    assert first["status"] == {"합의": 2, "불일치": 1} and first["adjudication_template"].endswith("adjudication.csv")
    assert compare(kit)["gold"] == 2                                                    # 불일치 항목은 아직 정답 없음
    with open(kit / "adjudication.csv", encoding="utf-8-sig", newline="") as handle:
        adjudication = list(csv.DictReader(handle))
    assert [a["item_id"] for a in adjudication] == [fail_id] and adjudication[0]["final_label"] == ""
    adjudication[0].update(final_label="정상", decided_by="A·B", decided_at="2026-10-14 14:00", rationale="공식 문서에서 확인")
    _sheet(kit / "adjudication.csv", adjudication)
    second = merge(kit, [tmp_path / "a.csv", tmp_path / "b.csv"], adjudication=kit / "adjudication.csv")
    assert second["status"] == {"합의": 2, "합의(조정)": 1} and second["gold"] == 3
    result = compare(kit)
    assert result["applicable"] == 2 and result["excluded"] == 1 and result["unreviewed"] == 0
    assert result["agreement"] == {"decided": 2, "agree": 1, "rate": 0.5}
    assert result["misjudgments"] == {"false_fail": 1, "missed_error": 0}                # AI FAIL 인데 사람은 정상
    assert result["unverifiable"] == {"count": 0, "share_of_applicable": 0.0, "ai_fail": 0, "ai_pass": 0}
    assert result["by_ai_cause"]["source_unavailable:access_failed"] == {"정상": 1}
    assert load_gold(kit / "gold.jsonl") and set(load_gold(kit / "gold.jsonl").values()) == {"PASS", "NOT_APPLICABLE"}


def test_compare_without_human_labels_reports_zero_gold(tmp_path):
    from tools.review_kit import compare
    make(_run(), tmp_path / "kit", include_pass=1)
    report = compare(tmp_path / "kit")
    assert report["gold"] == 0 and report["agreement"] is None and report["unreviewed"] == 2
