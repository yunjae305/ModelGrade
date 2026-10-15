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


def test_owner_overview_numbers_come_from_the_artifacts_and_the_answers_file():
    """우영 통합 요약 A·B·C 는 산출물·answers.json 과 같은 수치이고, 실측이 없는 경로에는 수치를 쓰지 않으며, 재시도 전 기록은 이력으로 따로 둔다."""
    overview = ARTIFACTS["overview"]
    assert _strip_links(overview) == _strip_links(pa.build(ARTIFACTS)["overview"])
    groups = {g["id"]: g for g in overview["groups"]}
    assert list(groups) == ["A", "B", "C"] and sum((g["requirements"] for g in groups.values()), []) == overview["requirements"]
    intent = {s["label"]: s["value"] for s in BY_ID["W01-INTENT"]["content"]["stats"]}
    metrics = {m["label"]: m["value"] for m in groups["A"]["metrics"]}
    assert metrics["목표 항목 실제 적용"] == f"{intent['실제 적용']}/{intent['실제 적용'] + intent['적용 안 됨']}건"
    assert metrics["J6 제외 적용률"] == intent["J6 제외 적용률"]
    answers = json.loads((ROOT / pa.ANSWERS).read_text(encoding="utf-8"))["meta"]
    rows = {r["service"]: r for r in groups["B"]["table"]["rows"]}
    for name, system in (("AIONA 웹", "aiona"), ("GPT API", "gpt"), ("Gemini API", "gemini")):
        assert (rows[name]["success"], rows[name]["requests"], rows[name]["run_id"]) == (
            answers["status_counts"][system]["SUCCESS"], answers["turns"], answers["run_id"])
    chart = groups["B"]["chart"]
    real = [r for r in rows.values() if r["state"] == "real"]
    assert chart["categories"] == [r["chart_label"] for r in real]
    assert chart["series"][0]["values"] == [round(r["success"] / r["requests"] * 100, 1) for r in real]
    assert all(r["requests"] is None and r["rate"] is None for r in rows.values() if r["state"] == "none")
    old = json.loads(pa._git("show", f"{pa.HISTORY_REV}:{pa.ANSWERS}"))["meta"]["status_counts"]["aiona"]
    history = rows["AIONA 웹 (재시도 전 기록)"]
    assert history["state"] == "history" and history["success"] == old["SUCCESS"] and history["chart_label"] is None
    steps = {s["name"]: s["state"] for s in groups["C"]["steps"]}
    if answers["evaluation"] == "하지 않음":
        assert steps["CODE·LLM 평가"] == steps["결과 비교"] == "pending"


def test_w01_criterion_counts_and_names_come_from_the_registry_and_the_real_prompt():
    """화면의 기준 수 28·44·72·64·34 와 기준 이름은 기준 목록·실제 생성 프롬프트·다른 산출물과 같은 값이다 (손으로 적지 않는다)."""
    from code_eval.criteria_registry import evaluation_targets, registry
    design = BY_ID["W01-DESIGN"]["content"]
    numbers = {row["what"]: row["number"] for row in design["numbers"]}
    counts = evaluation_targets()["counts"]
    assert numbers["CODE 검사"] == counts["code_checks"] and numbers["의미 점검 항목"] == counts["semantic_items"]
    goal = next(p["excerpt"] for p in design["prompt_parts"] if p["name"] == "목표 평가 기준")
    assert f"CODE 검사 {numbers['CODE 검사']}개와 의미 평가 항목 {numbers['일반 계획 프롬프트의 의미 항목']}개" in goal
    matrix = {s["label"]: s["value"] for s in BY_ID["W02-MATRIX"]["content"]["stats"]}
    assert numbers["평가 기준 전체"] == counts["total"] == counts["code_checks"] + counts["semantic_items"]   # 72 단위 하나 (10-15)
    assert numbers["질문만으로 셀 수 있는 기준"] == matrix["질문으로 셀 수 있는 기준"] == BY_ID["W03-COVERAGE"]["content"]["stats"][0]["value"]
    official = {c["id"]: c["name"] for c in registry()["criteria"] if c["layer"] != "CODE"}
    glossary = {g["id"]: g for g in design["glossary"]}
    assert list(glossary) == ["CODE 1", "CODE 2", "CODE 3"] + list(official)
    assert all(glossary[i]["name"] == name for i, name in official.items()) and all(g["easy"] and g["applies"] and g["fail"] for g in glossary.values())


def test_w01_intent_funnel_and_j6_split_add_up():
    """적용률 그래프의 분모·분자가 맞고, J6 를 대화 길이로 나눈 값이 J6 행과 같다."""
    content = BY_ID["W01-INTENT"]["content"]
    funnel, stats = content["funnel"], {s["label"]: s["value"] for s in content["stats"]}
    assert funnel["intended"] == funnel["applied"] + funnel["missed"] + funnel["answer_dependent"] + funnel["unknown"]
    assert funnel["judged"] == funnel["applied"] + funnel["missed"] and f"{funnel['applied'] / funnel['judged']:.1%}" == stats["적용률"]
    j6 = next(row for row in content["families"] if row["family"] == "J6")
    assert sum(b["targets"] for b in content["j6"]["bins"]) == j6["intended"] - j6["answer_dependent"]
    assert sum(b["applied"] for b in content["j6"]["bins"]) == j6["applied"]
    assert all(row["name"] and row["cause"] for row in content["families"])


def test_w01_quantity_keeps_the_batch_size_apart_from_the_set_size_and_cases_are_real_questions():
    """40(한 번에 계획) · 300(지난 실험) · 기준별 필요 수 · 신뢰 표본 수를 다른 숫자로 두고, 대표 사례는 실제 질문 원문이다."""
    from webapp.question_generation import PLAN_BATCH
    quantity = BY_ID["W01-QUANTITY"]["content"]
    four = quantity["four_numbers"]
    groups = json.loads((ROOT / pa.QUESTIONS).read_text(encoding="utf-8"))
    assert [n["value"] for n in four[:2]] == [PLAN_BATCH, len(groups)] and len({n["name"] for n in four}) == 4
    assert quantity["batch"]["value"] == PLAN_BATCH and "미검증" in quantity["batch"]["verdict"]
    last = BY_ID["W03-COVERAGE"]["content"]["table"]["rows"][-1]
    assert quantity["table"]["rows"][-1]["order_k1"] == last["k1"] and quantity["table"]["rows"][-1]["order_k5"] == last["k5"]
    by_id = {g["id"]: g for g in groups}
    for case in BY_ID["W01-SET300"]["content"]["cases"]:
        original = [t["question"] for t in by_id[case["id"]]["turns"]]
        shown = [t["question"] for t in case["turns"]] if "turns" in case else [case["question"]]
        assert all(text in source for text, source in zip(shown, original)) and len(shown) == len(original)
        assert case["fail"] == by_id[case["id"]]["trap"]


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
