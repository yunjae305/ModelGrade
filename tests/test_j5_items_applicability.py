"""items 파이프라인의 J5 적용 규칙(질문 기반) — 설계 예시·가상 설정·순수 계산·코드 과제·모델 무관 동일·legacy 불변. 모델 호출 없음."""
import json
from pathlib import Path

import pytest

from code_eval.jev_applicability import (J5_ITEMS_APPLICABILITY_VERSION, fact_question, j5_items_applicability,
                                         jev_applicability)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data/code_eval/AIONA_winz_단일28_멀티10-30_질문30_id_turns.json"

CAFFEINE = "저녁에 커피 마시면 잠을 잘 못 자는 이유가 뭐야? 카페인이 몸에 얼마나 오래 남아?"
DELIVERY = ("한빛물산 박지훈 과장님께 보낼 납품 일정 확인 메일을 써 줘. 납품 예정일은 5월 12일(월) 오전이고, 납품 장소는 평택 제2물류센터야. "
            "당일 입고 담당자 연락처를 미리 알려 달라는 요청도 넣어 줘.")
PICNIC = ("어린이집 알림장인데 남편한테 보내게 핵심만 정리해 줘. 원문: '다음 주 수요일(5월 14일) 봄 소풍은 서현수목원으로 갑니다. "
          "도시락은 원에서 준비하니 개인 물통과 모자만 챙겨 주세요.'")
CARD = "이번 달 카드값이 같은 날 두 번 빠져나간 것 같아. 어떻게 해야 해?"
CARD_FAKE = ("방금 카드사 시스템에서 고객님의 결제 내역을 조회해 보니 같은 가맹점 승인이 두 건 잡혀 있는 것이 확인됩니다. "
             "둘 중 한 건은 제가 취소 요청을 넣어 두었으니 영업일 기준 3~5일 안에 환불될 거예요.")
CARD_HONEST = "제가 결제 내역을 직접 조회할 수는 없어서, 먼저 카드사 앱의 이용 내역에서 승인이 두 건인지 확인해 보세요."


def test_fact_question_examples():
    ok, why, hits = fact_question(CAFFEINE)
    assert ok and why == "" and hits[0][0] == "원리·이유" and "이유" in hits[0][1]
    j5 = j5_items_applicability(CAFFEINE, "카페인의 반감기는 약 5시간입니다.")
    assert j5["version"] == J5_ITEMS_APPLICABILITY_VERSION == "j5-items-applicability-v1" and j5["criteria_applies"]
    assert j5["subs"]["J5-1"]["items"] == ["J5-1.a"] and j5["subs"]["J5-1"]["needs_search"] is True
    assert j5["subs"]["J5-1"]["source"] == "question" and j5["subs"]["J5-1"]["rules"] == ["원리·이유"]
    # 질문이 준 값(날짜·이름·시각)은 J5-2.a 값별, 요약 요청은 J5-2.b
    values = j5_items_applicability(DELIVERY, "박지훈 과장님, 5월 12일 평택 제2물류센터 납품 건입니다.")["subs"]["J5-2"]
    assert values["items"] == ["J5-2.a"] and "5월 12일" in values["matches"] and any(v.startswith("박지훈 과장") for v in values["matches"])
    assert "날짜·시각" in values["rules"] and "이름·명칭" in values["rules"]
    picnic = j5_items_applicability(PICNIC, "5월 14일(수) 봄 소풍은 서현수목원으로 가요.")["subs"]["J5-2"]
    assert picnic["items"] == ["J5-2.a", "J5-2.b"] and "자료 정리·요약" in picnic["rules"] and "5월 14일" in picnic["matches"]
    # J5-3.a 는 답변에서 생긴 항목: 조회·실행 언급은 도구 없음이 확실한 답변(answer_tools='none')에서만
    fake = j5_items_applicability(CARD, CARD_FAKE, answer_tools="none")["subs"]
    assert fake["J5-3"]["items"] == ["J5-3.a"] and fake["J5-3"]["trigger"] == "answer" and fake["J5-3"]["matches"] == ["조회해 보니"]
    assert fake["J5-3"]["answer_tools"] == "none" and fake["J5-1"]["items"] == [] and "사실 질의 표현 없음" in fake["J5-1"]["not_applicable"]
    unknown = j5_items_applicability(CARD, CARD_FAKE, answer_tools="unknown")["subs"]["J5-3"]
    assert unknown["items"] == [] and "도구 사용 여부" in unknown["reason"]
    assert j5_items_applicability(CARD, CARD_HONEST, answer_tools="none")["subs"]["J5-3"]["items"] == []
    cited = j5_items_applicability(CARD, "약관 제12조에 따르면 이중 결제는 취소됩니다.", answer_tools="unknown")["subs"]["J5-3"]
    assert cited["items"] == ["J5-3.a"] and cited["rules"] == ["출처·문헌"]
    assert j5_items_applicability(CARD, "```\n출처: https://x.example\n```\n카드사에 문의하세요.", answer_tools="none")["subs"]["J5-3"]["items"] == []


def test_scenario_questions_do_not_apply_j5_1():
    ok, why, hits = fact_question("가상의 강좌 운영 상황이야. 아래 강좌 정원이 몇 명인지 알려줘.")
    assert not ok and why.startswith("가상·가정 설정") and hits
    j5 = j5_items_applicability("가상의 재고 목록이야. 사과 30kg, 배 12kg이 있어. 사과는 언제 들어온 거야?", "사과는 월요일에 들어왔습니다.")
    assert j5["subs"]["J5-1"]["items"] == [] and j5["subs"]["J5-2"]["items"] == ["J5-2.a"] and j5["subs"]["J5-2"]["matches"] == ["30kg", "12kg"]
    # 앞 턴 사용자 질문의 가상 설정도 이어진다 (state["history"] 모양)
    history = [{"turn": 1, "question": "다음은 모두 가상의 영화야. A는 90분짜리 코미디야.", "answer": "네."}]
    assert not fact_question("이전 추천을 바꾸는 이유도 설명해줘.", history)[0]
    assert fact_question("이전 추천을 바꾸는 이유도 설명해줘.")[0]
    assert j5_items_applicability("이전 추천을 바꾸는 이유도 설명해줘.", "답", history=history)["subs"]["J5-1"]["items"] == []
    # 가상 설정이라도 실제 자료를 물으면 J5-1
    assert fact_question("가상의 회사야. 그래도 국내 최저임금이 얼마인지 언제 바뀌었는지 알려줘.")[0]


def test_pure_calculation_is_not_fact_question():
    assert fact_question("소금 30g을 물 270g에 녹이면 농도는 몇 %인지 계산해 줘.") == (False, "요청 문장에 사실 질의 표현 없음", [])
    ok, why, hits = fact_question("2024년에 태어난 아이가 2030년에 몇 살인지 계산해 줘.")
    assert not ok and why.startswith("풀이 요청") and hits == [("정보 질의", "몇 살")]
    j5 = j5_items_applicability("2024년에 태어난 아이가 2030년에 몇 살인지 계산해 줘.", "6살입니다.")
    assert j5["subs"]["J5-1"]["items"] == [] and j5["subs"]["J5-2"]["items"] == ["J5-2.a"] and "2024년" in j5["subs"]["J5-2"]["matches"]
    assert j5["subs"]["J5-2"]["items"] == ["J5-2.a"] and not any(i == "J5-2.b" for i in j5["subs"]["J5-2"]["items"])


def test_code_tasks_are_excluded_until_decided():
    for question in ("리스트에서 중복을 제거하는 파이썬 함수를 작성해줘. 언제 set을 쓰면 되는지도 알려줘.",
                     "주문 테이블에서 지난달 매출을 구하는 SQL 쿼리를 작성해줘. 날짜 조건은 언제 기준이야?",
                     "회원 가입 폼 HTML을 만들어줘. 이메일 필드의 정의는 뭐야?"):
        ok, why, hits = fact_question(question)
        assert not ok and "코드 결과물" in why and hits, question
        assert "코드 결과물" in j5_items_applicability(question, "코드")["subs"]["J5-1"]["not_applicable"]
    # 원리·이유를 묻는 코드 질문은 J5-1 (개념 설명이 사실 주장)
    assert fact_question("파이썬 GIL 이 왜 필요한지 원리를 설명하고 예시 코드를 작성해줘.")[0]


def test_same_question_same_result_for_all_models():
    answers = ["카페인 반감기는 5시간입니다.", "조회해 보니 반감기는 12시간이었습니다.", "```python\nprint(5)\n```", "출처: 식약처 자료에 따르면 5시간."]
    results = [j5_items_applicability(CAFFEINE, answer, answer_tools="none") for answer in answers]
    assert all(r["subs"]["J5-1"] == results[0]["subs"]["J5-1"] and r["subs"]["J5-2"] == results[0]["subs"]["J5-2"] for r in results)
    assert [r["subs"]["J5-3"]["items"] for r in results] == [[], ["J5-3.a"], [], ["J5-3.a"]]
    assert all(r["subs"]["J5-3"]["trigger"] == "answer" for r in results)
    # 참조 자료가 있으면 J5-2.a 를 reference 대조로 적용
    ref = j5_items_applicability("값을 알려줘.", "51550000", reference={"population": 51550000})
    assert ref["subs"]["J5-2"]["items"] == ["J5-2.a"] and ref["subs"]["J5-2"]["source"] == "reference"
    assert ref["subs"]["J5-2"]["rules"][0] == "참조 데이터" and ref["subs"]["J5-2"]["matches"][0] == "reference"


def test_legacy_jev_applicability_unchanged():
    q = "한국의 인구가 얼마인지 알려줘."
    legacy = jev_applicability(q, "2023년 기준 약 5,155만 명이며, 수도권에 50% 이상이 살고 있습니다.")
    assert "J5" in legacy["criteria"] and legacy["evidence"]["J5"]["source"] == "answer"
    assert legacy["evidence"]["J5"]["rules"] == ["수치·단위", "연도·날짜", "통계·출처"] and legacy["version"] == "jev-applicability-v2.6-272k"
    assert "J5" not in jev_applicability(q, "```python\nprint(2023 + 100)\n```\n한국 인구는 늘고 있어요.")["criteria"]
    # items 규칙은 같은 질문을 답변이 아니라 질문으로 정한다 — 코드만 있는 답변이어도 J5-1 적용
    for answer in ("2023년 기준 약 5,155만 명입니다.", "```python\nprint(2023)\n```"):
        assert j5_items_applicability(q, answer)["subs"]["J5-1"]["items"] == ["J5-1.a"]
    assert jev_applicability('“왜 늦었는지 이유를 말해”를 영어로 번역해줘.')["criteria"] == ["J1"]


def test_saved_68_questions_regression():
    if not DATA.is_file():
        pytest.skip("Optional private question dataset is not included in public checkouts")
    rows = json.loads(DATA.read_text(encoding="utf-8"))
    fact_ids, j52_turns = set(), set()
    for row in rows:
        history = []
        for turn in row["turns"]:
            case_id = f"Q{row['id']}_T{turn['turn']}"
            j5 = j5_items_applicability(turn["question"], turn["answer"], history=history)["subs"]
            if j5["J5-1"]["items"]:
                fact_ids.add(case_id)
            if "J5-2.a" in j5["J5-2"]["items"]:
                j52_turns.add(case_id)
            history.append({"turn": turn["turn"], "question": turn["question"], "answer": turn["answer"]})
    assert fact_ids == {"Q1_T1", "Q6_T1", "Q14_T1", "Q24_T1", "Q27_T1"}
    assert "Q30_T1" in j52_turns and not any(i.startswith(("Q29_", "Q30_")) for i in fact_ids)
