"""HTML → 마크다운 변환(webapp.ingestion.html_markdown) — AIONA 웹 수집기가 화면 HTML 을 원문 글로 바꿀 때 쓴다. 모델 · 네트워크 호출 없음."""
from __future__ import annotations

from code_eval.engine import evaluate_case
from webapp.ingestion.html_markdown import html_to_markdown

HTML_ANSWER = """<div class="markdown"><h2>요약</h2><p>서울 인구는 <strong>약 930만</strong> 명입니다.<br>출처는 아래에 있습니다.</p>
<ul><li>하나<ul><li>하위</li></ul></li><li>둘</li></ul>
<table><thead><tr><th>도시</th><th style="text-align:right">인구</th></tr></thead>
<tbody><tr><td>서울</td><td>930만</td></tr><tr><td>부산</td><td>330만</td></tr></tbody></table>
<pre><div>python<button>Copy code</button></div><code class="language-python">def total(a, b):
    return a + b
</code></pre><blockquote><p>인용문</p></blockquote>
<p><a href="https://kosis.kr/a">통계청</a> 자료입니다<sup><a href="#fn1">1</a></sup>. 식 <span class="katex"><annotation encoding="application/x-tex">x^2</annotation><span aria-hidden="true">x2</span></span></p>
<section class="footnotes"><ol><li id="fn1"><p>2025년 기준 <a href="#fnref1" class="footnote-backref">↩</a></p></li></ol></section></div>"""


def test_html_structure_is_preserved_in_markdown():
    result = html_to_markdown(HTML_ANSWER)
    md = result["markdown"]
    for piece in ("## 요약", "**약 930만**", "- 하나\n  - 하위\n- 둘", "| 도시 | 인구 |\n| --- | ---: |\n| 서울 | 930만 |",
                  "```python\ndef total(a, b):\n    return a + b\n```", "> 인용문", "[통계청](https://kosis.kr/a)",
                  "자료입니다[^1]", "[^1]: 2025년 기준", "$x^2$", "명입니다.\n출처는"):
        assert piece in md
    assert "Copy code" not in md and [q["code"] for q in result["quality"]] == ["code_block_chrome_removed"]


def test_converted_tables_and_code_blocks_do_not_fail_code_checks():
    md = html_to_markdown(HTML_ANSWER)["markdown"]
    case = evaluate_case({"case_id": "H1", "question": "서울과 부산 인구를 표로 정리하고, 합계 함수를 python 코드 블록으로 보여줘.",
                          "answer": md})
    fails = [r["check_id"] for r in case["results"] if r["verdict"] == "FAIL"]
    assert case["case_status"] == "EVALUATED" and fails == []
    assert any(r["check_id"].startswith("1-2") and r["verdict"] == "PASS" for r in case["results"])


def test_unrepresentable_or_broken_html_is_recorded_as_a_collection_quality_issue():
    spans = html_to_markdown("<table><tr><th colspan='2'>합계</th></tr><tr><td>1</td><td>2</td></tr></table>")
    assert spans["markdown"].startswith("| 합계 |  |") and spans["quality"][0]["code"] == "table_span_unrepresentable"
    broken = html_to_markdown("<div><p>문단</div></span><b>굵게")
    assert "html_structure_repaired" in {q["code"] for q in broken["quality"]}
    empty = html_to_markdown("<div><button>복사</button><script>x()</script></div>")
    assert empty["markdown"] == "" and empty["quality"][-1]["severity"] == "error"
