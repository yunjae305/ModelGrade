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
