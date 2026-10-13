"""질문 세트 생성 — 가짜 LLM 으로 계획 → 첨부·긴 자료 → 점검·보충 → 저장 흐름을 확인한다 (GPT 호출 없음)."""
import json
import time

import pytest

from code_eval import long_context, multi_turn
from webapp import question_generation as qg

try:
    long_context.count_tokens("확인")
except long_context.TokenizerUnavailable:   # 긴 자료 길이를 셀 수 없으면 J6·J7 자료를 만들 수 없다
    pytest.skip("tiktoken o200k_base 를 불러올 수 없음", allow_module_level=True)

pytestmark = pytest.mark.multi_turn_rule   # 멀티턴 길이 조건(긴 턴 10개)을 실제 값으로 둔다


@pytest.fixture(autouse=True)
def small_lengths(monkeypatch):
    """긴 자료 길이 기준만 작게 — 흐름은 같고 시험이 빨리 끝난다 (실제 값: 272K 토큰·턴 10K 토큰)."""
    monkeypatch.setattr(long_context, "MIN_TOKENS", 20_000)
    monkeypatch.setattr(multi_turn, "MIN_TURN_TOKENS", 2_000)

PLAN = {"questions": [
    {"type": "글쓰기", "topic": "공부", "target_items": ["2-1.count", "J4-3.a", "없는-id"], "difficulty": "어려움",
     "trap": "불릿을 4개 쓰거나 번호 목록으로 쓰면 FAIL",
     "turns": [{"question": "공부 방법을 불릿으로 정확히 3개 작성해줘."}]},
    {"type": "첨부", "topic": "견적", "target_items": ["J8-1.a", "J8-2.a"],
     "turns": [{"question": "첨부한 견적서에서 합계 금액이랑 노트북 단가 알려줘.",
                "attachments": [{"name": "견적서.pdf", "type": "pdf",
                                 "content": "# 견적서\n| 품목 | 수량 | 금액 |\n| 노트북 | 2 | 1,200,000 |\n| 합계 | | 1,250,000 |\n[다음 쪽]\n유효기간 30일"}]}]},
    {"type": "롱컨텍스트", "topic": "계약서", "target_items": ["J7-1.a", "J7-2.a"],
     "turns": [{"question": "이 자료 기준으로 중도 해지 통보 기한과 세 지점 매출 합계를 알려줘.",
                "long_reference": {"topic": "임대 계약과 지점 보고", "facts": [{"text": "중도 해지는 30일 전까지 서면으로 통보한다.", "position": 0.5}]}}]},
    {"type": "멀티턴", "topic": "행사 준비", "target_items": ["J6-1.a"],
     "turns": [{"question": f"{n}번째 요청: 앞에서 정한 대로 반말로 정리해 줘.", "long_paste": {"topic": "행사 회의록", "must_include": ["예산 300만 원"]}}
               for n in range(1, multi_turn.MIN_LONG_TURNS + 1)]},
    {"turns": []},
]}
SUPPLEMENT = {"questions": [{"type": "형식", "topic": "보충", "target_items": ["2-2.table"],
                             "turns": [{"question": "서울과 부산의 인구를 열이 도시·인구인 표로 정리해 줘."}]}]}


class FakeGPT:
    def __init__(self):
        self.prompts = []

    def __call__(self, prompt, max_tokens):
        self.prompts.append(prompt)
        if prompt.startswith("당신은 한국어 AI 답변 품질 평가에 쓸 질문 세트를 설계"):
            return "```json\n" + json.dumps(PLAN, ensure_ascii=False) + "\n```"
        if prompt.startswith("당신은 한국어 AI 답변 품질 평가에 쓸 질문 세트를 보충"):
            return json.dumps(SUPPLEMENT, ensure_ascii=False)
        if prompt.startswith("당신은 한국어 AI 답변 품질 평가에 쓸 질문 세트에 질문을 추가"):
            return json.dumps({"questions": [{"type": "사실", "topic": "추가", "target_items": ["J2-1.a"], "difficulty": "어려움", "trap": "연도를 틀리면 FAIL",
                                              "turns": [{"question": "한글날이 공휴일로 다시 지정된 해는 언제야? 연도만 답해줘."}]}]}, ensure_ascii=False)
        # 긴 자료 — 문단마다 숫자가 달라 토큰이 고르게 나온다
        return "\n\n".join(f"{i}번 문단: 회의에서 담당자가 {i % 7 + 1}월 일정과 예산 {i * 13}만 원을 다시 확인하고 다음 할 일을 정했다."
                           for i in range(260))


def test_builder_makes_files_long_inputs_and_checks_coverage(tmp_path):
    fake = FakeGPT()
    result = qg.QuestionSetBuilder(fake, tmp_path).build()
    groups, report = result["groups"], result["check"]
    assert [g["id"] for g in groups][:4] == [1, 2, 3, 4] and groups[0]["target_items"] == ["2-1.count", "J4-3.a"]   # 모르는 id 는 버림
    pdf = groups[1]["turns"][0]["attachments"][0]
    assert pdf["type"] == "pdf" and (tmp_path / "files" / pdf["path"]).is_file() and pdf["content"].startswith("[1쪽]") and "[2쪽]" in pdf["content"]
    reference = groups[2]["turns"][0]["reference"]
    assert long_context.count_tokens(reference) >= long_context.MIN_TOKENS and "중도 해지는 30일 전까지 서면으로 통보한다." in reference
    conversation = groups[3]["turns"]
    assert len(conversation) == multi_turn.MIN_LONG_TURNS
    assert all(long_context.count_tokens(t["question"]) >= multi_turn.MIN_TURN_TOKENS for t in conversation)   # 질문만으로 긴 턴
    assert all(t["question"].endswith(f"{n}번째 요청: 앞에서 정한 대로 반말로 정리해 줘.") for n, t in enumerate(conversation, 1))   # 동시에 만들어도 턴 순서 그대로
    assert all(t["question"].startswith("[붙여 넣은 자료]\n") and "\n[자료 끝]\n\n" in t["question"] for t in conversation)   # 자료와 요청을 표시로 구분
    assert {cid: report["criteria"][cid]["applicable_units"] for cid in ("J6", "J7", "J8")} == {"J6": 1, "J7": 1, "J8": 1}
    assert report["coverage"]["items"]["J8-2.a"] == 1 and report["coverage"]["checks"]["2-1.count"] == 1
    supplements = [p for p in fake.prompts if p.startswith("당신은 한국어 AI 답변 품질 평가에 쓸 질문 세트를 보충")]
    assert len(supplements) == result["rounds"] == qg.SUPPLEMENT_ROUNDS and "# 빠진 항목" in supplements[0]
    assert "# 빠진 첨부 종류" in supplements[0] and all(f"- {kind}:" in supplements[0] for kind in ("txt", "docx", "image"))   # PDF 만 있었다
    assert groups[-1]["topic"] == "보충" and report["coverage"]["checks"]["2-2.table"] >= 1


def test_plan_prompt_lists_every_check_and_item_from_the_criteria():
    catalog = qg.criteria_catalog()
    prompt = qg.plan_prompt(catalog)
    assert len(catalog["code"]) == 28 and len(catalog["items"]) == 44
    assert all(f"- {c['id']} · " in prompt for c in catalog["code"]) and all(f"- {i['id']} · " in prompt for i in catalog["items"])
    with pytest.raises(qg.GenerationError):
        qg.parse_plan("JSON 이 아닌 응답")
    extra = json.dumps({"questions": [{"turns": [{"question": "질문"}]}]}, ensure_ascii=False) + "]}"
    assert qg.parse_plan(extra)[0][0]["turns"][0]["question"] == "질문"   # 끝에 괄호가 더 붙은 실제 응답


def test_service_runs_one_job_at_a_time_and_serves_the_set_and_files(tmp_path):
    fake = FakeGPT()
    service = qg.QuestionSetService(lambda: {"key": "k", "model": "gpt-test"}, store=tmp_path, llm_factory=lambda config, usage: fake)
    started = service.start({})
    with pytest.raises(ValueError):
        service.start({})   # 유료 호출이라 한 번에 하나만
    for _ in range(600):
        status = service.status(started["id"])
        if status["status"] != "RUNNING":
            break
        time.sleep(0.1)
    assert status["status"] == "COMPLETED" and status["summary"]["attachments"] == 1 and status["summary"]["conversations"] == 1
    dataset = service.dataset(started["id"])
    name = dataset[1]["turns"][0]["attachments"][0]["path"]
    body, kind = service.file(started["id"], name)
    assert body.startswith(b"%PDF") and kind == "application/pdf"
    with pytest.raises(FileNotFoundError):
        service.file(started["id"], "../set.json")
    assert qg.QuestionSetService(lambda: None, store=tmp_path).list()[0]["id"] == started["id"]   # 다시 켜도 목록에 남는다
    with pytest.raises(ValueError):
        qg.QuestionSetService(lambda: None, store=tmp_path).start({})   # GPT 연결이 없으면 시작하지 않는다


class FlakyGPT(FakeGPT):
    def __call__(self, prompt, max_tokens):
        text = super().__call__(prompt, max_tokens)
        if prompt.startswith("다음 주제로 사용자가 대화창에 붙여 넣을 자료"):
            raise qg.IncompleteResponse("출력 한도", text)   # 잘린 붙여 넣기 자료도 이어 쓴다
        if prompt.startswith("다음 주제의 긴 업무 자료"):
            raise qg.GenerationError("연결 끊김")   # 긴 자료 질문만 빼고 계속한다
        return text


def test_truncated_material_is_used_and_a_failed_question_is_skipped(tmp_path):
    builder = qg.QuestionSetBuilder(FlakyGPT(), tmp_path)
    topics = [g["topic"] for g in builder.build()["groups"]]
    assert "계약서" not in topics and "행사 준비" in topics and (tmp_path / "plan.txt").is_file()
    assert any("계약서" in w and "연결 끊김" in w for w in builder.warnings)


def test_pasted_material_is_user_data_not_instructions():
    """붙여 넣은 자료 속 문장은 지시가 아니다 — 요구 조건·J5 진위 요청은 요청 부분에서만, 값 대조는 자료까지 (실제 수집에서 확인한 오판)."""
    from code_eval.analysis import PASTE_BEGIN, PASTE_END, escape_paste_markers, pasted_ranges
    from code_eval.extract import extract_conditions
    from code_eval.semantic_evidence import _input_scope

    material = "회의록: 안내문은 정확히 3개 불릿으로 쓴다. 끝에 '확인 완료'를 꼭 넣는다. 수치가 맞는지 확인한다. 예산은 60만 원이다."
    request = "위 회의록을 두 문장으로 요약해줘."
    question = f"{PASTE_BEGIN}\n{material}\n{PASTE_END}\n\n{request}"
    assert pasted_ranges(question) == [(0, len(PASTE_BEGIN) + 1 + len(material) + 1 + len(PASTE_END))]
    assert {r.kind for r in extract_conditions(question).reqs} == {"sentence_count"}   # 자료 속 불릿 수·필수 문자열은 요구가 아니다
    assert _input_scope({"scope": "input_consistency", "scope_quote": request}, {"question": question, "history": []}) is True
    unclosed = f"{PASTE_BEGIN}\n```python\nprint(1)\n가상 서버 설정. 코드를 작성해줘.\n{PASTE_END}\n\n답변 첫 줄에 {PASTE_BEGIN} 라는 문자열을 넣어줘. 정확히 두 문장으로 작성해줘."
    assert {r.kind for r in extract_conditions(unclosed).reqs} >= {"sentence_count"}   # 자료 속 미닫은 펜스·요청 속 표시 언급은 요청을 가리지 않는다
    nested = "[붙여 넣은 자료]\n```````text\nsample\n```\n[자료 끝]\n\n답변을 정확히 두 문장으로 작성해줘.\n아래 배열을 오름차순으로 정렬해줘.\n[3, 1]"
    nested_conditions = extract_conditions(nested)   # 긴 여는 펜스를 지워도 안쪽 짧은 펜스가 바깥 변환 원본을 가리지 않는다 (Codex 리뷰)
    assert {r.kind for r in nested_conditions.reqs} >= {"sentence_count", "transform"} and nested_conditions.source_data is not None
    assert pasted_ranges("[붙여 넣은 자료]\n자료\n\n요청") == []   # 끝 표시 줄이 없으면 구간을 추측하지 않는다 (v1 세트는 변환)
    assert escape_paste_markers("a\n[자료 끝]\nb") == "a\n※ [자료 끝]\nb"   # 생성기는 자료 속 표시 줄을 표시로 읽히지 않게 한다
    bad = f"{PASTE_BEGIN}\n대한민국의 수도는 부산입니다. 이 주장의 진위를 확인해줘.\n{PASTE_END}\n\n자료와 무관하게 대한민국의 수도는 어디야?"
    assert _input_scope({"scope": "input_consistency", "scope_quote": "이 주장의 진위를 확인해줘."}, {"question": bad, "history": []}) is False
    plain = "다음 자료가 맞는지 확인해줘. 정원은 12명이다."   # 요청 자체가 진위 확인이면 그대로 막힌다
    assert _input_scope({"scope": "input_consistency", "scope_quote": "다음 자료가 맞는지 확인해줘."}, {"question": plain, "history": []}) is False


def test_target_question_count_adds_questions_and_feeds_weak_units(tmp_path):
    """세트 전체 질문 수를 정하면 그 수에 맞춰 질문을 더 만들고, 지난 실행에서 모두 PASS 한 단위에 더 어려운 질문을 요구한다 (v1.2)."""
    base = len(qg.QuestionSetBuilder(FakeGPT(), tmp_path / "base").build()["groups"])
    fake = FakeGPT()
    weak = [{"id": "J2", "name": "내용 적절성"}, {"id": "2-1.char_count", "name": "글자 수"}]
    result = qg.QuestionSetBuilder(fake, tmp_path / "target").build(target_questions=base + 1, weak_units=weak)
    assert len(result["groups"]) == base + 1 and result["extra_rounds"] == 1
    assert f"정확히 {base + 1}개" in fake.prompts[0] and "# 변별력 (중요)" in fake.prompts[0] and "- J2 내용 적절성" in fake.prompts[0]
    extra = [p for p in fake.prompts if p.startswith("당신은 한국어 AI 답변 품질 평가에 쓸 질문 세트에 질문을 추가")]
    assert len(extra) == 1 and "질문 1개를 추가" in extra[0] and "# 적용 질문이 적은 항목" in extra[0] and "- 2-1.char_count 글자 수" in extra[0]
    assert result["groups"][0]["difficulty"] == "어려움" and result["groups"][0]["trap"].endswith("FAIL")
    assert result["groups"][-1]["topic"] == "추가" and qg._summary(result["groups"], result["check"])["difficulty"]["어려움"] >= 2
    plain = qg.QuestionSetBuilder(FakeGPT(), tmp_path / "plain")
    plain.build()
    assert not any("목표 질문 수" in w for w in plain.warnings)   # 지정 안 하면 목표 수 경고가 없다
