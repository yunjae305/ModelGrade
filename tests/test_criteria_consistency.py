# -*- coding: utf-8 -*-
"""기준 재확인(tools/criteria_consistency.py) — 문서 · 판정 프롬프트 · 코드의 기준 정의가 같은 말을 하는지. 모델 호출 없음.
어긋남(hard)은 0 이어야 하고, 덜 적힘(soft)은 지금 수준보다 늘지 않아야 한다(고치면 상한을 내린다)."""
import json

from tools import criteria_consistency as cc

SOFT_MAX = 20   # 2026-10-25 밤: 15 (표현이 다른 바꿔 적기 — 규칙 자체는 양쪽에 있음)


def test_real_files_have_no_hard_mismatch_and_soft_does_not_grow():
    res = cc.audit()
    assert res["counts"]["doc"] == res["counts"]["prompt"] == res["counts"]["code"] == 44
    assert res["counts"]["code_doc"] == res["counts"]["code_registry"] == 8
    assert res["hard"] == [], json.dumps(res["hard"], ensure_ascii=False)[:800]
    assert len(res["soft"]) <= SOFT_MAX, json.dumps(res["soft"], ensure_ascii=False)[:800]


def test_parsers_read_doc_prompt_and_jev_shapes():
    doc = "| **J1-1.a** 요청 작업 수행 | 결과물을 제시했는가 | 모든 질문 | 나머지는 J4-2 |\n| 전제 | 기준 |\n## 5. CODE 기준 28\n| 1-1 응답 · 문자 | x |\n## 6. LLM\n"
    d = cc.parse_doc(doc)
    assert d["J1-1.a"] == {"name": "요청 작업 수행", "question": "결과물을 제시했는가", "applies": "모든 질문", "boundary": "나머지는 J4-2"}
    assert cc.parse_doc_code(doc) == {"1-1": "응답 · 문자"}
    prompt = "### J1-1\n- **J1-1.a** · 결과물을 제시했는가 (나머지는 J4-2)\n  · 적용: 모든 질문\n  · 일부 항목: 표의 한 칸은 J4-2\n  · 근거: 요청 문장\n  · FAIL 예: \"x\"\n- **J1-2.a** · 거절하지 않았는가\n"
    p = cc.parse_prompt(prompt)
    assert p["J1-1.a"]["question"] == "결과물을 제시했는가 (나머지는 J4-2)" and p["J1-1.a"]["notes"]["적용"] == "모든 질문"
    assert p["J1-1.a"]["rules"] == ["일부 항목: 표의 한 칸은 J4-2"] and "J1-2.a" in p
    j = cc.parse_jev({"items": {"J1-1.a": {"instructions": "[J1-1.a] 요청 작업 수행 — 결과물을 제시했는가 / 적용: 모든 질문 / 근거: 요청 문장 / FAIL 예: x"}}})
    assert j["J1-1.a"] == {"question": "결과물을 제시했는가", "applies": "모든 질문", "evidence": "요청 문장"}


def test_similarity_ignores_spacing_and_punctuation_and_cover_finds_rule_in_pool():
    assert cc.sim("질문이 직접 말한 조건·제약을 어긴 문장이 없는가", "질문이 직접 말한 조건 · 제약을 어긴 문장이 없는가") > 0.95
    assert cc.cover("이전 턴 자기 답과의 모순은 J6-4.a", ["다른 항목: 이전 턴의 자기 답과 어긋나는 것은 J6-4.a 에서 본다"]) > 0.5
    assert cc.cover("완전히 다른 규칙 문장입니다", ["무관한 글"]) < 0.3


def test_audit_flags_question_drift_and_code_applies_drift():
    doc = "| **J1-1.a** 요청 | 결과물을 제시했는가 | 모든 질문 | — |\n## 5. CODE 기준 28\n| 1-1 응답 | x |\n## 6. LLM\n"
    prompt = "- **J1-1.a** · 추천이 배경에 맞는가\n  · 적용: 추천 질문\n"
    sem = {"J1-1.a": {"question": "추천이 배경에 맞는가", "applies": "모든 답변", "evidence": ""}}
    res = cc.audit(doc_text=doc, prompt_text=prompt, jev_doc={"items": {}}, sem=sem, code_names={"1-1": "응답"})
    kinds = [h["kind"] for h in res["hard"]]
    assert "점검 질문" in kinds and "적용 조건" in kinds


def test_generation_prompt_ids_are_known_and_countable_criteria_mostly_mentioned():
    g = cc.audit_generation_prompt()
    assert g["unknown"] == [], g["unknown"]
    assert len(g["missing_countable"]) <= 2, g["missing_countable"]   # 2026-10-25: 0 — 늘어나면 생성 프롬프트가 기준을 놓친 것


def test_generation_prompt_audit_flags_unknown_and_missing_ids():
    cat = {"catalog": {"code": [{"id": "2-1.count"}], "items": [{"id": "J1-1.a"}, {"id": "J2-3.a"}]}, "answer_dependent_ids": ["2-1.count"]}
    g = cc.audit_generation_prompt("J1-1.a 와 J2-9.z 를 노린다 (10-25)", cat)
    assert g["unknown"] == ["J2-9.z"] and g["missing_countable"] == ["J2-3.a"] and g["countable"] == 2
