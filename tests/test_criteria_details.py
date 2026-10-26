"""기준 목록(code_eval.criteria_registry) — 의미 항목 44개 · 세부 기준 30개가 한 정의에서 나오고, 기준 화면용 짧은 설명이 모두 있다. 모델 · 네트워크 호출 없음."""
import json


from code_eval.criteria_registry import REGISTRY_VERSION, registry
from code_eval.semantic_items import SEMANTIC_ITEMS_VERSION, describe


def test_registry_lists_semantic_items_from_the_single_definition():
    data = registry()
    assert REGISTRY_VERSION == "criteria-registry-v13" and data["version"] == REGISTRY_VERSION
    assert data["semantic_items"] == describe()
    assert len(data["semantic_items"]["subs"]) == 30 and len(data["semantic_items"]["items"]) == 44
    assert data["semantic_items"]["version"] == SEMANTIC_ITEMS_VERSION and data["semantic_items"]["reason_codes"][0] == "FACT_FALSE_CONFIRMED"
    assert [c["id"] for c in data["criteria"] if c["layer"] == "LLM"] == ["J1", "J2", "J3", "J4", "J5", "J6", "J7", "J8"]
    json.dumps(data, ensure_ascii=False, allow_nan=False)





def test_criteria_screen_gets_short_details_for_every_check_sub_and_item():
    from code_eval.criteria_registry import reference_details
    details = reference_details()
    groups = {c["group"] for c in details["code_checks"]}
    assert len(details["code_checks"]) == 28 and groups == {"0-1", "1-1", "1-2", "1-3", "2-1", "2-2", "3-1", "3-2", "3-3"}
    assert all(c["name"] and c["applies"] for c in details["code_checks"])
    subs, items = details["semantic"]["subs"], details["semantic"]["items"]
    assert len(subs) == 30 and len(items) == 44 and {s["criterion"] for s in subs} == {f"J{i}" for i in range(1, 9)}
    first = next(i for i in items if i["id"] == "J1-1.a")
    assert first["note"] == "나머지 요청의 누락은 J4-2" and not first["question"].endswith(")")   # 끝 괄호는 경계 메모로
    assert all(i["question"] and i["applies"] and i["pass"] and i["fail"] for i in items)
    assert all("answer" not in i and "examples" not in i for i in items)   # 긴 예시 원문은 보내지 않는다


