"""업무 진행 현황 산출물 — artifacts.json 형식, 표·그래프 수치가 실제 결과 파일과 같은지, 비공개 원본이 없을 때, 파일 확인 API (모델 호출 없음)."""
import json
import re
from pathlib import Path

import pytest

from tools import progress_artifacts as pa
from webapp.progress_files import file_status, files_report

# 화면 수치는 실제 J6 규칙(10턴 이상 · 대화 전체 10K 토큰)으로 센 값이다 — conftest 의 기본(길이 조건 끔) 대신 실제 값으로
pytestmark = pytest.mark.multi_turn_rule

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = json.loads((ROOT / "frontend/data/artifacts.json").read_text(encoding="utf-8"))
REQUIREMENTS = json.loads((ROOT / "frontend/data/requirements.json").read_text(encoding="utf-8"))
BY_ID = {a["artifact_id"]: a for a in ARTIFACTS["artifacts"]}
FIELDS = ("artifact_id", "requirement_id", "title", "type", "description", "result_summary", "data_source", "created_at",
          "code_revision", "verification_status", "limitations", "file_or_result_reference")


def _strip_links(value):
    """GitHub 주소는 커밋을 푸시했는지에 따라 달라진다 — 수치 비교에서는 뺀다."""
    if isinstance(value, dict):
        return {k: _strip_links(v) for k, v in value.items() if k != "url"}
    if isinstance(value, list):
        return [_strip_links(v) for v in value]
    return value


def test_every_artifact_has_the_contract_fields_and_points_to_a_requirement():
    ids = {item["id"] for item in REQUIREMENTS["items"]}
    assert len(BY_ID) == len(ARTIFACTS["artifacts"])
    for artifact in ARTIFACTS["artifacts"]:
        assert all(key in artifact for key in FIELDS), artifact["artifact_id"]
        assert artifact["requirement_id"] in ids and artifact["type"] in {"prompt", "data", "experiment", "run", "code", "research", "policy"}
        assert artifact["verification_status"]["level"] in pa.LEVELS and artifact["limitations"]
    for item in REQUIREMENTS["items"]:
        assert item["meeting"]["sessions"] and item["purpose"] and set(item["interpretation"]) == {"goal", "method", "result", "meaning", "gaps"}
        assert {stage["state"] for stage in item["stages"].values()} <= {"done", "partial", "none", "na"}
        assert set(item["stages"]) == {"implemented", "auto_tested", "real_run", "accuracy_verified"}
    assert {a["requirement_id"] for a in ARTIFACTS["artifacts"]} >= {"W01", "W02", "W03"}


def test_saved_file_matches_a_fresh_calculation_from_the_result_files():
    """표·그래프·요약 수치는 저장소의 실제 결과 파일과 코드 규칙으로 다시 계산한 값과 같다 (다르면 tools/progress_artifacts.py 로 다시 만든다)."""
    fresh = {a["artifact_id"]: a for a in pa.build(ARTIFACTS)["artifacts"]}
    assert fresh.keys() == BY_ID.keys()
    for artifact_id, saved in BY_ID.items():
        for key in ("content", "result_summary", "title", "verification_status"):
            assert _strip_links(saved[key]) == _strip_links(fresh[artifact_id][key]), f"{artifact_id}.{key}"


def test_charts_and_tables_use_the_same_numbers_as_the_question_set_and_answers():
    from tools.check_question_set import check_items, load_items
    from tools.min_question_count import analyze
    items = load_items(ROOT / pa.QUESTIONS)
    criteria = check_items(items, pipeline="items")["criteria"]
    w02 = BY_ID["W02-CRITERIA"]["content"]
    assert w02["charts"][0]["categories"] == list(criteria)
    assert w02["charts"][0]["series"][0]["values"] == [c["applicable_units"] for c in criteria.values()]
    assert [r["turns"] for r in w02["table"]["rows"]] == [c["applicable_turns"] for c in criteria.values()]
    prefix = analyze(items)["prefix"]
    line = BY_ID["W03-COVERAGE"]["content"]["charts"][0]
    assert line["categories"] == [p["questions"] for p in prefix]
    assert [s["values"] for s in line["series"]] == [[p["covered"][k] for p in prefix] for k in ("1", "5", "10", "30")]
    meta = json.loads((ROOT / pa.ANSWERS).read_text(encoding="utf-8"))["meta"]["status_counts"]
    stacked = BY_ID["W08-PATHS"]["content"]["charts"][0]
    for column, system in enumerate(("aiona", "gpt", "gemini")):
        assert {s["state"]: s["values"][column] for s in stacked["series"] if s["values"][column]} == meta[system]
    matrix = BY_ID["W02-MATRIX"]["content"]
    buckets = matrix["charts"][0]["series"][0]["values"]
    assert sum(buckets) == sum(1 for r in matrix["table"]["rows"] if r["units"] is not None)


def test_without_the_private_run_the_saved_public_summary_is_kept_not_invented(monkeypatch, tmp_path):
    monkeypatch.setattr(pa, "SAVED_RUN", "data/comparison_runs/없는-실행.json")
    kept = pa.build(ARTIFACTS)
    assert set(kept["kept_without_source"]) == set(pa.PRIVATE_IDS) & BY_ID.keys()
    assert kept["private_sources"] == {"data/comparison_runs/없는-실행.json": "missing"}
    alone = {a["artifact_id"] for a in pa.build(None)["artifacts"]}
    assert not alone & set(pa.PRIVATE_IDS) and "W03-CONCLUSION" in alone   # 원본도 저장 요약도 없으면 만들지 않는다


def test_published_files_carry_no_keys_and_private_artifacts_no_raw_answers():
    text = json.dumps(ARTIFACTS, ensure_ascii=False) + json.dumps(REQUIREMENTS, ensure_ascii=False)
    assert not re.search(r"sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|(API_KEY|PASSWORD)\s*=\s*\S", text)
    run_path = ROOT / pa.SAVED_RUN
    if not run_path.is_file():
        pytest.skip("비공개 저장 실행이 없는 PC")
    run = json.loads(run_path.read_text(encoding="utf-8"))
    samples = [row["answer"][:40] for rows in run["datasets"].values() for row in rows[:5] if len(row.get("answer") or "") > 40]
    private = json.dumps([BY_ID[i] for i in pa.PRIVATE_IDS if i in BY_ID], ensure_ascii=False)
    assert samples and not any(sample in private for sample in samples)


def test_progress_file_check_reports_presence_only_and_rejects_paths_outside_the_repo(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    assert file_status(tmp_path, "tools/a.py") == {"path": "tools/a.py", "status": "present", "size": 1}
    assert file_status(tmp_path, "data/comparison_runs/x.json")["status"] == "missing"
    for bad in ("../x", ".env", "tools/../.env", "/etc/passwd", "C:/x", "tools\\a.py", "secret/a.py", "", "tools/.hidden"):
        assert file_status(tmp_path, bad)["status"] == "invalid", bad
    assert "SECRET" not in json.dumps(files_report(tmp_path, ["tools/a.py", ".env"]))
    with pytest.raises(ValueError):
        files_report(tmp_path, ["tools/a.py"] * 51)
