"""쉬운 보고서(tools/easy_report) — 결론 · 순위 · 빈 출력 보류 · 쉬운 말 (사용자 10-27)."""
from collections import Counter


def _D():
    units = [(1, 0), (2, 0), (3, 1), (3, 2)]
    uv = {("aiona", 1, 0): "HOLD", ("aiona", 2, 0): "PASS", ("aiona", 3, 1): "PASS", ("aiona", 3, 2): "FAIL",
          ("gpt", 1, 0): "HOLD", ("gpt", 2, 0): "FAIL", ("gpt", 3, 1): "PASS", ("gpt", 3, 2): "FAIL"}
    tally = {p: Counter(v for (pp, _q, _t), v in uv.items() if pp == p) for p in ("aiona", "gpt")}
    item = {"item": "J3-5.a", "decision": "FAIL", "reason": "", "turn": 2}
    return {"d": {"models": {"aiona": "AIONA 웹브라우저", "gpt": "ChatGPT 웹 (GPT-6 · Medium)"}, "created_at": "2026-10-21T23:00:00"},
            "run_id": "no-such-run", "order": ["aiona", "gpt"], "tally": tally, "n": 4, "nq": 3, "units": units,
            "utype": {(1, 0): "싱글턴", (2, 0): "싱글턴", (3, 1): "멀티턴", (3, 2): "멀티턴"}, "uverdict": uv,
            "qtypes": {1: "명시적 빈 출력", 2: "단일 질문", 3: "10턴 멀티턴 대화"}, "names": {"J3-5.a": {"name": "실행 가능성", "group": "J3"}},
            "lines": {("aiona", 3): {"verdict": "FAIL", "items": [item], "line": {}}, ("gpt", 3): {"verdict": "FAIL", "items": [item], "line": {}}},
            "turns": {}}


def test_easy_report_states_the_winner_ranks_and_keeps_empty_output_as_hold(tmp_path):
    from tools import easy_report
    out = easy_report.write(_D(), tmp_path)
    text = out.read_text(encoding="utf-8")
    assert out.name == "쉬운보고서.html" and "<title>채팅 품질 평가 결과</title>" in text
    assert "AIONA (자동 추천)가 가장 많이 통과했습니다 — 4문항 중 2문항(50%)" in text and "ChatGPT (GPT-6)와 1문항 차이" in text
    assert text.index("<b>AIONA</b>") < text.index("<b>ChatGPT</b>")   # 통과 많은 순
    assert "그중 빈 출력 요구" in text and "<td class=\"n\">1</td><td class=\"n\">0</td>" in text   # 보류 1 = 빈 출력 1 · 그 밖 0
    assert "심판이 본 것: 답변 안에 적힌 시간·인원·순서끼리 맞는가</span>" in text   # 내부 기준 번호 괄호는 뺌
    assert "prefers-color-scheme:dark" in text and "max-width:640px" in text   # 어두운 화면 · 휴대폰 폭
