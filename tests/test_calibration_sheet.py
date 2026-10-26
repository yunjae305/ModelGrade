"""사람 표시용 표 — 정해 둔 칸 목록(--pick-file)으로 만들기: 질문 · 답변 원본, 견줄 AI 판정 판, 고른 까닭, PASS · FAIL · 에러 케이스 고르기 (할일 9번, 10-26)."""
from __future__ import annotations

import json
from pathlib import Path

from tools import build_calibration_sheet as cs


def _setup(root: Path) -> Path:
    (root / "data" / "comparison_runs").mkdir(parents=True)
    (root / "data" / "comparison_runs" / "r1.json").write_text(json.dumps({"datasets": {"gpt": [
        {"case_id": "Q11_T1", "question": "붙인 자료 [자료 끝] 마지막 요청", "answer": "첫 문장입니다.\n**접수는 09:30 에 시작합니다.**"}]}}, ensure_ascii=False), encoding="utf-8")
    for out, dec, quote in (("v4-a", "FAIL", "접수는 09:30 에 시작합니다."), ("v4-b", "PASS", "답변에 없는 문장을 인용함")):
        jd = root / "data" / "judge_runs" / "r1" / out
        jd.mkdir(parents=True)
        line = {"provider": "gpt", "question": 11, "status": "judged", "turns": 1, "turn_results": {"1": {"items": {
            "J2-3.a": {"decision": dec, "reason": f"{out} 사유", "answer_quote": quote}}}}}
        (jd / "results.jsonl").write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
    sd = root / "set"
    sd.mkdir()
    (sd / "questions.json").write_text(json.dumps([{"id": 11}]), encoding="utf-8")
    return sd


def test_pick_file_shows_originals_both_results_and_three_marks(tmp_path: Path, monkeypatch):
    sd = _setup(tmp_path)
    monkeypatch.setattr(cs, "ROOT", tmp_path)
    cs._context.cache_clear()
    import tools.report_html as R
    monkeypatch.setattr(R, "criteria_table", lambda s: {"J2-3.a": {"name": "조건·제약 준수 — 질문이 정한 조건을 지켰는가"}})
    picks = tmp_path / "picks.json"
    picks.write_text(json.dumps([
        {"provider": "gpt", "question": 11, "turn": 1, "item": "J2-3.a", "kind": "판마다 뒤집힘", "why": "같은 질문을 연달아 두 번 판정했더니 FAIL → PASS 로 바뀜",
         "results": [{"out": "v4-a", "label": "앞 판정"}, {"out": "v4-b", "label": "뒤 판정"}]},
        {"provider": "gpt", "question": 11, "turn": 1, "item": "J9-9.z", "out": "v4-a", "why": "없는 칸"}], ensure_ascii=False), encoding="utf-8")
    assert cs.main(["--run", "r1", "--set", str(sd), "--pick-file", str(picks), "--dest", str(tmp_path / "out")]) == 0
    page = (tmp_path / "out" / "표시용_표.html").read_text(encoding="utf-8")
    assert page.count('class="cell"') == 1
    assert "같은 질문을 연달아 두 번 판정했더니" in page and "이 기준이 보는 것: 질문이 정한 조건을 지켰는가" in page
    assert "앞 판정" in page and "뒤 판정" in page and "v4-a 사유" in page and "v4-b 사유" in page
    assert "마지막 요청" in page and "붙인 자료" in page   # 요청과 질문 원본
    assert "<mark>접수는 09:30 에 시작합니다.</mark>" in page   # AI 가 든 인용을 답변 원본에 표시(마크다운 기호 무시)
    assert page.count('class="miss"') == 1   # 답변에 없는 인용
    for v in ("PASS", "FAIL", "에러 케이스"):
        assert f'value="{v}"' in page
    assert "맞음" not in page and "틀림" not in page   # 심판이 맞는지 고르던 칸은 없음


def test_highlight_merges_overlaps_and_ignores_spacing():
    html_, found = cs.highlight("가나다 라마바사\r\n아자차", ["가나다 라마바", "라마바사  아자차"])
    assert html_.count("<mark>") == 1 and len(found) == 2
