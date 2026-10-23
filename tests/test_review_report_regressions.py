"""Duplicate display IDs must never select another input's evaluation."""

from __future__ import annotations

import copy
from html.parser import HTMLParser

import pytest

from code_eval.combined import combined_summary
from code_eval.config import AionaConfig
from code_eval.engine import evaluate_batch, result_digest
from code_eval.jev_client import JevCall, JevClientConfig
from code_eval.jev_eval import evaluate_jev_batch
from code_eval.report_html import render_report


class _Judge:
    config = JevClientConfig()

    def system_one(self, state, questions):
        answers = {}
        for cid in questions:
            fail = cid == "J6" and state["question"].startswith("두 번째")
            answers[cid] = {"type": "choice", "choice": "FAIL" if fail else "PASS", "confidence": 0.8,
                            "probabilities": {"PASS": 0.1 if fail else 0.9, "FAIL": 0.9 if fail else 0.1}}
        return JevCall("OK", body={"model": "test", "answers": answers}, attempts=1, latency_ms=1)


@pytest.fixture
def duplicate_batch():
    batch = evaluate_batch([
        {"case_id": "same", "conversation_id": "C1", "turn": 1,
         "question": "앞으로 내 이름을 민수라고 불러줘.", "answer": "민수야, 안녕."},
        {"case_id": "same", "conversation_id": "C1", "turn": 2,
         "question": "두 번째 답을 확인해줘.", "answer": "2+2=5"},
        {"case_id": "same", "conversation_id": "C1", "turn": 3,
         "question": "세 번째 답을 확인해줘.", "answer": "3+3=7"},
        # A real user ID that collides with a possible generated anchor.
        {"case_id": "entry-2", "question": "인사해줘.", "answer": "안녕하세요."},
        {"case_id": "same", "question": "답변 필드가 없는 입력"},
    ], AionaConfig())
    batch["jev_eval"] = evaluate_jev_batch(batch, _Judge())
    batch["combined"] = combined_summary(batch)
    return batch


class _Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.ids = []
        self.links = []
        self.case_ids = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
            if attrs["id"].startswith("case-") and attrs.get("class", "").startswith("card"):
                self.case_ids.append(attrs["id"])
        if tag == "a" and attrs.get("href", "").startswith("#"):
            self.links.append(attrs["href"][1:])


def test_combined_keeps_each_duplicate_fail_and_its_input_index(duplicate_batch):
    batch = duplicate_batch
    before = copy.deepcopy(batch["cases"])
    s = combined_summary(batch)
    assert (s["questions"], s["pass"], s["fail"], s["excluded"]) == (4, 2, 2, 1)
    assert [(p["case_id"], p["case_index"]) for p in s["problems"]] == [("same", 1), ("same", 2)]
    assert batch["cases"] == before and result_digest(batch["cases"]) == batch["result_digest"]


def test_semantic_fail_without_judge_reason_says_so_instead_of_blank(duplicate_batch):
    # The fixture judge returns J6 FAIL without a reason; the report must not invent one.
    records = duplicate_batch["jev_eval"]["records"]
    assert any(r.get("decisions", {}).get("J6", {}).get("decision") == "FAIL" and "reason" not in r["decisions"]["J6"]
               for r in records if r)
    assert "평가자가 FAIL 근거를 남기지 않았습니다." in render_report(duplicate_batch)


def test_report_links_resolve_to_the_right_duplicate_without_changing_display_ids(duplicate_batch):
    html = render_report(duplicate_batch)
    parsed = _Links(html)
    assert len(parsed.ids) == len(set(parsed.ids))
    assert set(parsed.links) <= set(parsed.ids)
    assert len(parsed.case_ids) == 5  # Input errors also have an addressable card.
    assert parsed.case_ids[3] == "case-entry-2"  # Existing unique anchors remain stable.
    assert parsed.case_ids[1] != "case-entry-2"
    assert html.count("<b>same</b>") == 3 and "<h3>same</h3>" in html
    overall = html.split('id="overall"', 1)[1].split('id="results"', 1)[0]
    assert f'href="#{parsed.case_ids[1]}"' in overall
    assert f'href="#{parsed.case_ids[2]}"' in overall
    assert f'href="#{parsed.case_ids[0]}"' not in overall
    # Each CODE FAIL detail and answer location uses its own internal key too.
    for anchor in parsed.case_ids[1:3]:
        assert f'id="fail-{anchor[5:]}-1"' in html
        assert f'href="#fail-{anchor[5:]}-1"' in html


@pytest.mark.parametrize("old_saved_run", [False, True])
def test_conversation_and_separate_review_links_target_duplicate_occurrences(duplicate_batch, old_saved_run):
    batch = duplicate_batch
    if old_saved_run:
        for item in batch["jev_eval"]["conversations"]["items"]:
            for turn in item["turn_results"]:
                turn.pop("case_index", None)
    batch["llm_review"] = {"status": "PARTIAL", "source_digest": batch["result_digest"],
                           "completed_questions": 0, "records": [{"status": "ERROR"}, {"status": "ERROR"}]}
    html = render_report(batch)
    ids = _Links(html).case_ids
    conversation = html.split('id="conversations"', 1)[1].split("</section>", 1)[0]
    assert f'href="#{ids[1]}">2턴</a>' in conversation
    assert f'href="#{ids[1]}">2</a>' in conversation
    assert f'href="#{ids[2]}">3</a>' in conversation
    review = html.split('aria-labelledby="llm-heading"', 1)[1].split("</section>", 1)[0]
    assert _Links(review).links == [ids[1], ids[2]]


