"""의미 평가 기준표(semantic_items)와 세부 기준 적용 규칙(sub_applicability) — 모델 호출 없음.

설계 '의미 평가 기준표'의 수(6/25/39)·펼치기 12개·누락형 13개·예시 인용과, 설계 예시 질문이 어느 세부 기준으로
가는지를 고정한다. legacy RULES 는 패턴을 상수로 쪼갠 뒤에도 글자 단위로 같아야 한다.
"""
import pytest

from code_eval import semantic_items as si
from code_eval.jev_applicability import RULES, SUB_RULES, sub_applicability

EXPAND = {"J2-2.a", "J2-3.a", "J4-1.a", "J4-1.b", "J4-2.a", "J4-3.a", "J4-4.a", "J5-1.a", "J5-2.a", "J6-1.a", "J6-2.a", "J6-3.a"}
MISSING_TYPE = {"J1-1.a", "J1-3.a", "J1-3.b", "J2-4.a", "J3-2.a", "J3-4.a", "J4-1.a", "J4-1.b", "J4-2.a", "J4-3.a", "J4-4.a",
                "J6-2.b", "J6-3.b"}
#: 패턴 분리 전 legacy RULES 리터럴 (규칙 이름, 패턴) — 정규식 결과 불변의 근거
LEGACY_RULES = {
    "J2": (
        ("대상", r"(?:초등학생|중학생|고등학생|대학생|어린이|고객|팀장|상사|동료|사용자|친구|동기|초보자|부모님)(?:에게|한테|도|이|가|을|를|용)|면접"),
        ("말투·수준", r"쉽게\s*(?:설명|알려|풀어)|이해할\s*수\s*있게|자연스럽|정중|격식|말투|어조|친근|유쾌|기술\s*용어"),
        ("제약", r"(?:하|쓰|넣|약속하|사용하)지\s*(?:마|말)|않고|않으면서|않도록|않게|말고|만\s*사용|만\s*넣|이\s*정보만|이\s*경험만|주어지지\s*않은|최소화"),
        ("설명 방식", r"(?<!\S)(?!기준)\S+(?:으로|로)\s*설명해\s*줘|비유|예시를\s*들어|연결해서\s*설명|상황을\s*기준|상황에\s*비유"),
        ("글의 목적", r"(?:안내문|메시지|답글|카드|광고\s*문구|문구|회고|일기|동화|산문|줄거리|이야기|답변|글)(?:의|을|를|으로|로|에)?\s*(?:\S+\s*){0,2}(?:작성|써|만들)"),
        ("추천·제안", r"추천|제안"),
    ),
    "J3": (
        ("이유·원리", r"이유|왜|원리|까닭|원인(?:은|을)|어떻게\s*\S+\s*(?:수\s*있|유지)"),
        ("추론·판단", r"추론|맞을까|단정|일반화|볼\s*수\s*있는지|믿어도\s*되는지|설명할\s*수\s*있는지|문제가\s*생길|조건을\s*만족"),
        ("풀이", r"계산|구해|풀어|몇\s*(?:번|초|m|L|가지|개|시간|명|권)|얼마(?:야|인지)|평균\s*(?:속력|점수)"),
    ),
    "J4": (
        ("비교", r"비교|(?:어떻게|무엇이|어떤\s*점(?:이|에서))\s*달라|다른\s*(?:점|변화)|차이(?:는|가)?\s*(?:뭐|무엇|있어)|어떤\s*\S*\s*차이|구분해|구분하고|구분할"),
        ("추가 요구", r"\S+도\s*(?:설명|알려|짚어|구해|만들어|포함|구분|함께)|각각|장단점|포함해|포함한|기준으로"),
        ("방법·절차", r"방법|(?:어떤|작업|이동)\s*순서|순서를\s*정해|단계|절차|과정을\s*보여|과정도"),
        ("요약", r"요약"),
    ),
}
MISSING_QUESTION = "두 제품을 비교해서 알려 줘."   # test_jev_eval.MISSING 과 같은 성격 — J2 세부 없음
TURN2 = {"id": "C1", "turn": 2, "history_turns": 1}


def applied(subs):
    return {sid: entry["items"] for sid, entry in subs.items() if entry["items"]}


def test_registry_counts_ids_and_expand_marks():
    assert len(si.SUBS) == 25 and len(si.ITEMS) == 39 and len(si.SPECS_BY_ID) == 39 and len(si.SUBS_BY_ID) == 25
    per_criterion = {}
    for item in si.ITEMS:
        per_criterion[item.criterion] = per_criterion.get(item.criterion, 0) + 1
    assert per_criterion == {"J1": 6, "J2": 8, "J3": 8, "J4": 5, "J5": 4, "J6": 8}
    assert {it.id for it in si.ITEMS if it.expand} == EXPAND == set(si.EXPAND_IDS)
    assert {it.id for it in si.ITEMS if it.missing_type} == MISSING_TYPE
    assert si.SPECS_BY_ID["J5-1.a"].expand_limit == 8 and si.SPECS_BY_ID["J5-1.a"].fact_block
    assert {it.id for it in si.ITEMS if it.origin == "answer"} == {"J3-1.a", "J5-1.a"}
    assert {it.id for it in si.ITEMS if it.always} == {"J1-1.a", "J1-2.a", "J1-3.a", "J1-3.b", "J1-4.a", "J1-4.b", "J3-1.a"}
    assert {it.id for it in si.ITEMS if it.model_applies} >= {"J1-3.a", "J1-3.b", "J2-4.b", "J3-3.a", "J6-1.a", "J6-2.a", "J6-2.b",
                                                               "J6-3.a", "J6-3.b", "J6-4.a", "J6-4.b"}
    # 설계 표의 예시 한 쌍(PASS, FAIL) — FAIL 쪽 '평가 모델 인용'은 예시 답변 안에 글자 그대로 있다
    for item in si.ITEMS:
        assert [ex.kind for ex in item.examples] == ["PASS", "FAIL"], item.id
        assert item.examples[1].quote and item.examples[1].quote in item.examples[1].answer, item.id
        if item.criterion == "J6":
            assert all(ex.history for ex in item.examples), item.id
    described = si.describe()
    assert described["version"] == "semantic-items-v1" == si.SEMANTIC_ITEMS_VERSION
    assert len(described["subs"]) == 25 and len(described["items"]) == 39
    assert described["reason_codes"] == ["FACT_FALSE_CONFIRMED", "FACT_UNVERIFIABLE", "SOURCE_NOT_FOUND", "ITEM_UNVERIFIABLE"]
    assert si.DECISIONS == ("YES", "NO", "NOT_APPLICABLE")


def test_item_prompt_dict_carries_applicability_evidence_and_search_flag():
    data = si.SPECS_BY_ID["J2-2.a"].to_prompt_dict({"rules": ["말투"], "matches": ["반말"], "excerpt": "…반말로 써 줘", "source": "question"})
    assert data["type"] == "item" and data["criterion"] == "J2" and data["sub"] == "J2-2" and data["expand"] == "지시별"
    assert data["matches"] == ["반말"] and data["missing_type"] is False and "needs_search" not in data
    fact = si.SPECS_BY_ID["J5-1.a"].to_prompt_dict({"needs_search": True, "answer_tools": "none"})
    assert fact["needs_search"] is True and fact["fact_block"] is True and fact["origin"] == "answer" and fact["answer_tools"] == "none"
    assert si.SPECS_BY_ID["J1-1.a"].to_prompt_dict(None)["expand_limit"] is None


def test_legacy_rules_are_byte_identical_after_constant_split():
    for cid, rules in LEGACY_RULES.items():
        assert [(name, pattern) for name, pattern, _ in RULES[cid]] == list(rules), cid
    assert not any(sid.startswith("J5") for sid, *_ in SUB_RULES)
    assert all(item_id in si.SPECS_BY_ID and si.SPECS_BY_ID[item_id].sub == sid for sid, _, _, items in SUB_RULES for item_id in items)


@pytest.mark.parametrize("question, expected", [
    ("우리 독서 모임 단톡방에 이번 달 모임 장소가 바뀌었다고 알리는 글을 써 줘. 모임원들끼리는 다 반말 쓰니까 편한 반말로 써 줘.",
     {"J2-2": ["J2-2.a"], "J2-5": ["J2-5.b"], "J4-3": ["J4-3.a"]}),
    ("팀 전체에 보낼 퇴사 인사 메일을 써 줘. 이직하는 회사 이름이랑 퇴사 이유는 절대 쓰지 말고, 그동안 고마웠다는 내용 위주로 써 줘.",
     {"J2-3": ["J2-3.a"]}),
    ("컴퓨터에서 '캐시'가 뭔지 비유를 들어서 설명해 줘.", {"J2-4": ["J2-4.a", "J2-4.b"]}),
    ("다음 달에 결혼하는 대학 동기한테 결혼 선물을 하려고 해. 신혼집은 원룸이고 둘 다 요리는 거의 안 하고 주로 배달시켜 먹는대. 뭐가 좋을까?",
     {"J2-1": ["J2-1.a", "J2-1.b"], "J2-5": ["J2-5.a"], "J3-2": ["J3-2.a", "J3-2.b"]}),
    ("출퇴근용으로 전기자전거, 전동킥보드, 일반 자전거 중 뭐가 나을지 비교해 줘. 편도 6km 정도고 언덕이 좀 있어.",
     {"J2-5": ["J2-5.a"], "J3-2": ["J3-2.a", "J3-2.b"], "J4-1": ["J4-1.a"], "J5-2": ["J5-2.a"]}),
    ("이사 업체에 포장이사와 반포장이사 중 뭘 맡길지 고민이야. 비용과 이사 당일 내가 해야 할 일 기준으로 비교해 줘.",
     {"J4-1": ["J4-1.a", "J4-1.b"]}),
    ("신입사원 온보딩에 멘토 제도를 도입하려고 해. 좋은 점과 주의할 점을 알려주고, 멘토에게 줄 만한 보상 예시도 들어 줘.",
     {"J4-2": ["J4-2.a"]}),
    ("아파트 입주민에게 보낼 소방 대피 훈련 안내문을 써 줘. 10월 24일 금요일 오후 2시에 101동 앞 광장에서 하고, "
     "훈련 중에는 승강기가 멈추니까 계단으로 대피해야 해. 문의는 관리사무소로.",
     {"J2-1": ["J2-1.a", "J2-1.b"], "J2-5": ["J2-5.b"], "J4-3": ["J4-3.a"], "J5-2": ["J5-2.a"]}),
    ("아래 사내 공지를 팀 채팅방에 올릴 수 있게 요약해 줘.\n「총무팀 공지: 11월 3일(월)부터 본사 주차장 2층 방수 공사를 시작합니다.」",
     {"J4-4": ["J4-4.a"], "J5-2": ["J5-2.a", "J5-2.b"]}),
    ("저녁에 커피 마시면 잠을 잘 못 자는 이유가 뭐야? 카페인이 몸에 얼마나 오래 남아?", {"J3-3": ["J3-3.a", "J3-3.b"], "J5-1": ["J5-1.a"]}),
    ("70대 어머니가 스마트폰에서 와이파이랑 데이터가 뭐가 다른지 자꾸 물어보셔. 어머니가 이해하실 수 있게 설명해 줘.",
     {"J2-1": ["J2-1.a", "J2-1.b"]}),
    ("이번 달 식비 예산 상한을 40만원으로 잡았습니다. 지금까지 실제로 쓴 돈은 28만원이고, 남은 2주 동안 예상 지출은 15만원입니다. 예산 안에서 끝낼 수 있을까요?",
     {"J3-5": ["J3-5.b"], "J5-2": ["J5-2.a"]}),
    ("주말에 동네 플리마켓에 혼자 부스를 내려고 합니다. 오후 1시부터 5시까지 운영하고, 물건은 차에 실어 둘 건데 주차장은 부스에서 걸어서 15분 거리예요. 당일 운영 계획을 짜 주세요.",
     {"J3-5": ["J3-5.a"], "J5-2": ["J5-2.a"]}),
    ("자전거로 집에서 회사까지 평균 시속 15km로 가면 1시간 20분이 걸립니다. 평균 시속 20km로 가면 얼마나 걸리는지 풀이 과정과 함께 알려 주세요.",
     {"J3-4": ["J3-4.a"], "J5-2": ["J5-2.a"]}),
])
def test_sub_applicability_maps_design_examples_to_subs(question, expected):
    subs = sub_applicability(question)
    always = {"J1-1": ["J1-1.a"], "J1-2": ["J1-2.a"], "J1-3": ["J1-3.a", "J1-3.b"], "J1-4": ["J1-4.a", "J1-4.b"], "J3-1": ["J3-1.a"]}
    assert applied(subs) == {**always, **expected}
    assert list(subs) == [sub.id for sub in si.SUBS]
    for sid, entry in subs.items():
        assert entry["source"] in ("question", "answer", "reference", "conversation")
        if not entry["items"]:
            assert entry["reason"], sid


def test_sub_applicability_matches_and_reasons_follow_design_wording():
    bookclub = sub_applicability("우리 독서 모임 단톡방에 이번 달 모임 장소가 바뀌었다고 알리는 글을 써 줘. 모임원들끼리는 다 반말 쓰니까 편한 반말로 써 줘.")
    assert bookclub["J2-2"]["rules"] == ["말투"] and bookclub["J2-2"]["matches"] == ["반말"] and "반말로" in bookclub["J2-2"]["excerpt"]
    moving = sub_applicability("이사 업체에 포장이사와 반포장이사 중 뭘 맡길지 고민이야. 비용과 이사 당일 내가 해야 할 일 기준으로 비교해 줘.")
    assert moving["J4-1"]["rules"] == ["비교", "비교 기준"] and moving["J4-1"]["matches"] == ["비교", "기준으로"]
    commute = sub_applicability("출퇴근용으로 전기자전거, 전동킥보드, 일반 자전거 중 뭐가 나을지 비교해 줘.")
    assert commute["J4-1"]["rules"] == ["비교"] and "J4-1.b" not in commute["J4-1"]["items"]
    assert sub_applicability("A 의 장단점을 기준으로 알려 줘.")["J4-1"]["items"] == []   # 비교 요청 없이 기준만 있으면 J4-1 아님
    delivery = sub_applicability("한빛물산 박지훈 과장님께 보낼 납품 일정 확인 메일을 써 줘. 납품 예정일은 5월 12일(월) 오전이고, 납품 장소는 평택 제2물류센터야.")
    assert "5월 12일" in delivery["J5-2"]["matches"] and any(v.startswith("박지훈 과장") for v in delivery["J5-2"]["matches"])
    assert delivery["J3-5"]["items"] == []   # '납품 일정 확인 메일' 은 계획·일정 요청이 아니다
    party = sub_applicability("아까 두 번째 거 진행 방식 좀 자세히 알려줘.", conversation=TURN2)
    assert party["J6-2"]["items"] == ["J6-2.a", "J6-2.b", "J6-2.c"] and party["J6-2"]["rules"] == ["대화", "지시어"] and party["J6-2"]["matches"] == ["아까"]
    assert party["J6-1"]["items"] == ["J6-1.a"] and party["J6-1"]["model_applies"] is True
    # 반례: 큰따옴표 안의 '이유' 는 지시가 아니다. 요청 문장에 J2 표현이 없으면 J2 세부 기준도 없다
    quoted = sub_applicability('“왜 늦었는지 이유를 말해”를 영어로 번역해줘.')
    assert quoted["J3-3"]["items"] == [] and quoted["J5-1"]["items"] == []
    assert not any(sid.startswith("J2") and entry["items"] for sid, entry in sub_applicability(MISSING_QUESTION).items())
    assert sub_applicability(MISSING_QUESTION)["J2-2"]["reason"] == "요청 문장에 말투·어조 지정이 없음"


def test_applicable_items_follow_question_not_answer():
    question = "출퇴근용으로 전기자전거, 전동킥보드, 일반 자전거 중 뭐가 나을지 비교해 줘. 편도 6km 정도고 언덕이 좀 있어."
    answers = ["전기자전거가 낫습니다.", "조회해 보니 전동킥보드가 가장 쌉니다. 출처: https://example.com/a",
               "```python\nprint(1)\n```", "일반 자전거는 유지비가 거의 없습니다. 참고 문헌: 자전거 백서"]
    chosen = [si.applicable_items({"question": question, "answer": answer, "answer_tools": "none"}) for answer in answers]
    ids = [[item.id for item, _ in picked] for picked in chosen]
    without_j53 = [[i for i in picked if i != "J5-3.a"] for picked in ids]
    assert all(picked == without_j53[0] for picked in without_j53)
    assert "J5-3.a" in ids[1] and "J5-3.a" not in ids[0] and "J5-3.a" in ids[3]
    assert not any(i.startswith("J6") for i in ids[0])   # 단일 질문에는 J6 항목 없음
    for item, evidence in chosen[0]:
        assert item.id in evidence["items"] and evidence["source"]
    assert [i for i in ids[0] if i.startswith("J1") or i == "J3-1.a"] == ["J1-1.a", "J1-2.a", "J1-3.a", "J1-3.b", "J1-4.a", "J1-4.b", "J3-1.a"]
    assert ids[0] == [item.id for item in si.ITEMS if item.id in ids[0]]   # 기준표 순서
    # 대화 2턴: J6 항목 8개 중 J6-2.c 는 지시어가 있을 때만
    turn2 = [item.id for item, _ in si.applicable_items({"question": "다시 정리해 줘.", "answer": "답"}, conversation=TURN2)]
    assert [i for i in turn2 if i.startswith("J6")] == ["J6-1.a", "J6-2.a", "J6-2.b", "J6-3.a", "J6-3.b", "J6-4.a", "J6-4.b"]
    deictic = [item.id for item, _ in si.applicable_items({"question": "아까 그거 다시 정리해 줘.", "answer": "답"}, conversation=TURN2)]
    assert "J6-2.c" in deictic
    assert not any(i.startswith("J6") for i, _ in
                   ((item.id, e) for item, e in si.applicable_items({"question": "다시 정리해 줘.", "answer": "답"},
                                                                     conversation={"id": "C1", "turn": 1, "history_turns": 0})))
    # 미리 계산한 sub_applicability 결과(또는 subs 키에 담은 dict)를 그대로 받는다
    subs = sub_applicability(question, answers[1], answer_tools="none")
    assert [i.id for i, _ in si.applicable_items({"question": question}, subs)] == ids[1]
    assert [i.id for i, _ in si.applicable_items({"question": question}, {"subs": subs})] == ids[1]
