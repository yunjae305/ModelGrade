"""적정 질문 수 검토 도구 — 독립 단위 분모·단위 판정·데이터 부족 표시·사람 정답 일치율 (API 호출 없음)."""
import json

from tools.sample_size_study import detect_n, main, study, unit_verdict, zero_fail_upper


def _run(n_single=12, convo_turns=3, j5_fail=("Q1", "Q2"), j6_fail_turn=2):
    rows = [{"case_id": f"Q{i}"} for i in range(1, n_single + 1)]
    rows += [{"case_id": f"C_T{t}", "conversation_id": "C", "turn": t} for t in range(1, convo_turns + 1)]

    def batch(system):
        cases, records = [], []
        for row in rows:
            cases.append({"case_id": row["case_id"], "case_status": "EVALUATED",
                          "results": [{"area": "common", "counted": True, "category": "1", "verdict": "PASS"}]})
            decisions = {"J1": {"decision": "PASS"}}
            if "conversation_id" not in row:
                decisions["J5"] = {"decision": "FAIL" if system == "a" and row["case_id"] in j5_fail else "PASS"}
            elif row["turn"] > 1:
                decisions["J6"] = {"decision": "FAIL" if system == "a" and row["turn"] == j6_fail_turn else "PASS"}
            records.append({"status": "SUCCESS", "criteria": list(decisions), "decisions": decisions})
        return {"cases": cases, "jev_eval": {"engine": "anthropic_api", "records": records}}

    return {"run_id": "t", "question_count": len(rows), "datasets": {"aiona": rows},
            "evaluation_execution": {"pipeline": "legacy"}, "evaluations": {"a": batch("a"), "b": batch("b")}}


def test_units_count_conversations_once_and_unit_fails_if_any_turn_fails():
    report = study(_run(), sizes=(5, 10, 20), draws=200)
    assert report["units_total"] == 13
    j1, j5, j6 = (report["criteria"][c] for c in ("J1", "J5", "J6"))
    assert j1["applicable_units"] == {"a": 13, "b": 13} and j1["applicable_units_conversations"]["a"] == 1
    assert j5["applicable_units"]["a"] == 12 and j5["fails"] == {"a": 2, "b": 0}
    assert j6["applicable_units"]["a"] == 1 and j6["fails"]["a"] == 1     # 대화 3턴 중 한 턴 FAIL → 대화 1개 FAIL
    assert j5["sizes"]["20"] == {"status": "insufficient", "available": 12}   # 외삽하지 않음
    assert j5["sizes"]["10"]["pool_dominated"] is True and j5["sizes"]["5"]["pool_dominated"] is False
    assert report["criteria"]["CODE 1"]["sizes"]["5"]["judge_cost_usd_per_system"] is None   # CODE 는 호출 비용 없음
    assert unit_verdict(["PASS", "UNJUDGED"]) is None and unit_verdict(["UNJUDGED", "FAIL"]) == "FAIL"


def test_gold_agreement_and_error_types(tmp_path, capsys):
    gold = tmp_path / "gold.jsonl"
    labels = [{"system": "a", "case_id": "Q1", "criterion": "J5", "gold": "FAIL"},          # 맞힌 FAIL
              {"system": "a", "case_id": "Q2", "criterion": "J5", "gold": "UNVERIFIABLE"},  # 검증 불가를 FAIL 처리
              {"system": "b", "case_id": "Q1", "criterion": "J5", "gold": "FAIL"}]          # 놓친 FAIL
    labels += [{"system": s, "case_id": f"Q{i}", "criterion": "J5", "gold": "PASS"} for s in "ab" for i in range(3, 13)]
    gold.write_text("\n".join(json.dumps(r) for r in labels), encoding="utf-8")
    run = tmp_path / "run.json"
    run.write_text(json.dumps(_run(), ensure_ascii=False), encoding="utf-8")
    assert main([str(run), "--gold", str(gold), "--sizes", "12", "--draws", "50", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)["criteria"]["J5"]["sizes"]["12"]["gold"]
    assert row["agreement_mean"] == round(21 / 23, 4)   # b 의 Q2 는 정답 없음 → 23개 중 21개 일치
    assert row["first_draw_errors"] == {"false_fail": 0, "missed_fail": 1, "unverifiable_as_fail": 1}


def test_theory_reference_values():
    assert round(zero_fail_upper(30), 3) == 0.095          # 30개 모두 PASS 여도 실제 실패율 9.5% 가능 (3/n 근사 10%)
    assert detect_n(0.9, 0.10) > detect_n(0.9, 0.20) > 0
    assert detect_n(0.05, 0.10) is None                     # 음수 비율은 계산하지 않음


def test_question_level_sample_counts_overlapping_criteria_and_never_extrapolates():
    """4차 P1-8 — 질문 단위로 n 개를 뽑으면 여러 기준이 함께 따라온다: 기준별 적용률·적용 단위 수, 전체 통과율·순위, 정보 증가,
    가용 단위보다 큰 n 은 데이터 부족, 가용 단위 절반을 넘는 표본은 후보에서 뺀다. 사람 정답이 없으면 일치율 '미측정'."""
    from tools.sample_size_study import question_level
    run = _run(n_single=40)
    q = question_level(run, sizes=(10, 20, 30, 50), draws=300)
    assert q["units_total"] == 41 and q["gold_agreement"] == "미측정"
    assert q["applicability_rate"]["J1"] == 1.0 and round(q["applicability_rate"]["J5"], 3) == round(40 / 41, 3)
    assert q["applicability_rate"]["J6"] == round(1 / 41, 4)                       # 대화 1개 = 단위 1개
    assert q["sizes"]["50"] == {"status": "insufficient", "available": 41}       # 외삽하지 않음
    ten, twenty, thirty = q["sizes"]["10"], q["sizes"]["20"], q["sizes"]["30"]
    assert ten["applied_units_mean"]["J1"] == 10.0 and 9 <= ten["applied_units_mean"]["J5"] <= 10
    assert thirty["pool_dominated"] is True and twenty["pool_dominated"] is False
    assert twenty["gain"]["max_width_drop"] is not None
    assert q["proposal"]["precise"] != 30 and q["proposal"]["quick"] != 30         # 절반 넘는 표본은 후보가 아니다
