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

pytestmark = pytest.mark.multi_turn_rule   # 멀티턴 길이 조건(10턴 이상 · 대화 전체 10K 토큰)을 실제 값으로 둔다


@pytest.fixture(autouse=True)
def small_lengths(monkeypatch):
    """긴 자료 길이 기준만 작게 — 흐름은 같고 시험이 빨리 끝난다 (실제 값: 272K 토큰)."""
    monkeypatch.setattr(long_context, "MIN_TOKENS", 20_000)

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
               for n in range(1, multi_turn.MIN_TURNS + 1)]},
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
    builder = qg.QuestionSetBuilder(fake, tmp_path)
    result = builder.build()
    groups, report = result["groups"], result["check"]
    assert [g["id"] for g in groups][:4] == [1, 2, 3, 4] and groups[0]["target_items"] == ["2-1.count", "J4-3.a"]   # 모르는 id 는 버림
    pdf = groups[1]["turns"][0]["attachments"][0]
    assert pdf["type"] == "pdf" and (tmp_path / "files" / pdf["path"]).is_file() and pdf["content"].startswith("[1쪽]") and "[2쪽]" in pdf["content"]
    reference = groups[2]["turns"][0]["reference"]
    assert long_context.count_tokens(reference) >= long_context.MIN_TOKENS and "중도 해지는 30일 전까지 서면으로 통보한다." in reference
    conversation = groups[3]["turns"]
    assert len(conversation) == multi_turn.MIN_TURNS
    assert all(t["question"].endswith(f"{n}번째 요청: 앞에서 정한 대로 반말로 정리해 줘.") for n, t in enumerate(conversation, 1))   # 동시에 만들어도 턴 순서 그대로
    pasted, plain = conversation[:qg.MAX_PASTE_TURNS], conversation[qg.MAX_PASTE_TURNS:]   # 자료는 대화당 MAX_PASTE_TURNS 턴까지만 (multi-turn-v2)
    assert all(t["question"].startswith("[붙여 넣은 자료]\n") and "\n[자료 끝]\n\n" in t["question"] for t in pasted)   # 자료와 요청을 표시로 구분
    assert all("[붙여 넣은 자료]" not in t["question"] for t in plain) and any("long_paste 를 뺐습니다" in w for w in builder.warnings)
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
    assert status["auto_run"] is False and status["run_id"] is None   # 기본은 만들기만 — 질문을 확인한 뒤 평가 시작
    preview = service.preview(started["id"])
    assert preview[1]["turns"][0]["attachments"] == ["견적서.pdf"] and "content" not in json.dumps(preview[1], ensure_ascii=False)
    assert preview[2]["turns"][0]["reference_chars"] > 0 and "서면으로 통보한다" not in json.dumps(preview[2], ensure_ascii=False)   # 긴 자료 본문은 빼고 글자 수만
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
    closed_then_open = "[붙여 넣은 자료]\n```python\nvalues = [3, 1]\n```\n```````text\n메모\n[자료 끝]\n\n아래 배열을 오름차순으로 정렬해줘.\n[2, 1]"
    kept = extract_conditions(closed_then_open)   # 앞의 닫힌 블록은 그대로 두고 열린 긴 펜스만 다룬다 (Codex 리뷰)
    assert {r.kind for r in kept.reqs} >= {"transform"} and kept.source_data is not None
    literal = "[붙여 넣은 자료]\n```````json\n[\"A\u2028```literal\u2028B\", \"C\"]\n[자료 끝]\n\n위 JSON 배열을 오름차순으로 정렬해줘."
    source = extract_conditions(literal).source_data   # 열린 블록 안의 값도 원문 그대로 — 본문을 고치지 않고 펜스만 구간별로 계산 (Codex 리뷰)
    assert source and source["value"] == ["A\u2028```literal\u2028B", "C"]
    assert pasted_ranges("[붙여 넣은 자료]\n자료\n\n요청") == []   # 끝 표시 줄이 없으면 구간을 추측하지 않는다 (v1 세트는 변환)
    assert escape_paste_markers("a\n[자료 끝]\nb") == "a\n※ [자료 끝]\nb"   # 생성기는 자료 속 표시 줄을 표시로 읽히지 않게 한다
    bad = f"{PASTE_BEGIN}\n대한민국의 수도는 부산입니다. 이 주장의 진위를 확인해줘.\n{PASTE_END}\n\n자료와 무관하게 대한민국의 수도는 어디야?"
    assert _input_scope({"scope": "input_consistency", "scope_quote": "이 주장의 진위를 확인해줘."}, {"question": bad, "history": []}) is False
    plain = "다음 자료가 맞는지 확인해줘. 정원은 12명이다."   # 요청 자체가 진위 확인이면 그대로 막힌다
    assert _input_scope({"scope": "input_consistency", "scope_quote": "다음 자료가 맞는지 확인해줘."}, {"question": plain, "history": []}) is False


def test_large_target_is_planned_in_batches(tmp_path, monkeypatch):
    """질문 수백 개를 지정하면 첫 계획은 PLAN_BATCH 개, 나머지는 PLAN_BATCH 개씩 여러 번 추가한다 (한 응답에 다 쓰게 하면 출력 한도에 잘림)."""
    base = len(qg.QuestionSetBuilder(FakeGPT(), tmp_path / "base").build()["groups"])
    monkeypatch.setattr(qg, "PLAN_BATCH", 2)
    fake = FakeGPT()   # 추가 요청마다 질문 1개만 돌려준다
    builder = qg.QuestionSetBuilder(fake, tmp_path / "big")
    result = builder.build(target_questions=base + 3, multi_turn_count=0, long_context_count=0)   # 전용 단계 없이 일반 계획의 묶음만 본다
    assert "정확히 2개" in fake.prompts[0] and len(result["groups"]) == base + 3 and result["extra_rounds"] == 3
    extra = [p for p in fake.prompts if p.startswith("당신은 한국어 AI 답변 품질 평가에 쓸 질문 세트에 질문을 추가")]
    assert fake.prompts[0].endswith("2개인지 확인하고, 모자라면 채운 뒤 답합니다.")   # 개수는 프롬프트 맨 끝에 다시 못 박는다
    assert [p.rsplit("questions 배열에 정확히 ", 1)[1][:2] for p in extra] == ["2개", "2개", "1개"]
    assert builder.max_calls == max(qg.MAX_CALLS, 2 * (base + 3) + 150)


def test_target_question_count_adds_questions_and_feeds_weak_units(tmp_path):
    """세트 전체 질문 수를 정하면 그 수에 맞춰 질문을 더 만들고, 지난 실행에서 모두 PASS 한 단위에 더 어려운 질문을 요구한다 (v1.2)."""
    base = len(qg.QuestionSetBuilder(FakeGPT(), tmp_path / "base").build()["groups"])
    fake = FakeGPT()
    weak = [{"id": "J2", "name": "내용 적절성"}, {"id": "2-1.char_count", "name": "글자 수"}]
    result = qg.QuestionSetBuilder(fake, tmp_path / "target").build(target_questions=base + 1, weak_units=weak,
                                                                    multi_turn_count=0, long_context_count=0)   # 일반 계획만
    assert len(result["groups"]) == base + 1 and result["extra_rounds"] == 1
    assert f"정확히 {base + 1}개" in fake.prompts[0] and "# 변별력 (중요)" in fake.prompts[0] and "- J2 내용 적절성" in fake.prompts[0]
    extra = [p for p in fake.prompts if p.startswith("당신은 한국어 AI 답변 품질 평가에 쓸 질문 세트에 질문을 추가")]
    assert len(extra) == 1 and "질문 1개를 추가" in extra[0] and "# 적용 질문이 적은 항목" in extra[0] and "- 2-1.char_count 글자 수" in extra[0]
    assert result["groups"][0]["difficulty"] == "어려움" and result["groups"][0]["trap"].endswith("FAIL")
    assert result["groups"][-1]["topic"] == "추가" and qg._summary(result["groups"], result["check"])["difficulty"]["어려움"] >= 2
    plain = qg.QuestionSetBuilder(FakeGPT(), tmp_path / "plain")
    plain.build()
    assert not any("목표 질문 수" in w for w in plain.warnings)   # 지정 안 하면 목표 수 경고가 없다


class DedicatedGPT(FakeGPT):
    """전용 단계(긴 문서 질문·10턴 대화) 프롬프트에도 답하는 가짜 GPT — 요청한 개수를 프롬프트에서 읽어 그만큼 돌려준다."""
    def __call__(self, prompt, max_tokens):
        import re
        if prompt.startswith("당신은 한국어 AI 답변 품질 평가에 쓸 긴 참조 자료 질문을 설계"):
            self.prompts.append(prompt)
            n = int(re.findall(r"questions 배열에 정확히 (\d+)개", prompt)[-1])
            first = len([p for p in self.prompts if p.startswith("당신은 한국어 AI 답변 품질 평가에 쓸 긴 참조 자료 질문을 설계")]) == 1
            body = {"questions": [{"type": "롱컨텍스트", "topic": f"문서 질문 {i}", "target_items": ["J7-1.a", "J7-2.a"], "difficulty": "어려움",
                                   "trap": "자료에 없는 값을 지어내면 FAIL",
                                   "turns": [{"question": f"자료 기준으로 {i}번째 지점의 해지 통보 기한과 세 지점 매출 합계를 알려줘.",
                                              "attachments": [{"name": "x.txt", "type": "txt", "content": "빼야 함"}]}]} for i in range(n)]}
            if first:
                body["long_reference"] = {"topic": "임대 계약과 지점 보고", "facts": [{"text": "중도 해지는 30일 전까지 서면으로 통보한다.", "position": 0.6}]}
            return json.dumps(body, ensure_ascii=False)
        if prompt.startswith("당신은 한국어 AI 답변 품질 평가에 쓸 10턴 대화를 설계"):
            self.prompts.append(prompt)
            n = int(re.findall(r"대화를 정확히 (\d+)개", prompt)[-1])
            convs = [{"type": "멀티턴", "topic": f"대화 {len(self.prompts)}-{i}", "target_items": ["J6-1.a", "J6-2.a"],
                      "turns": [{"question": "앞으로 존댓말로 답해 주세요. 행사 계획을 정리해 주세요."}]
                               + [{"question": f"{t}번째 요청: 앞에서 정한 계획을 고쳐 표로 전체를 다시 써 주세요."} for t in range(2, multi_turn.MIN_TURNS)]
                               + [{"question": "지금까지 내용을 세 문장으로 요약해 주세요."}]} for i in range(n)]
            convs.insert(0, {"type": "멀티턴", "topic": "짧은 대화", "turns": [{"question": "안녕"}, {"question": "다시"}]})   # 턴이 모자라 빠져야 함
            return json.dumps({"questions": convs}, ensure_ascii=False)
        return super().__call__(prompt, max_tokens)


def test_split_counts_defaults_and_limits():
    assert qg.split_counts(None) == (None, None) and qg.split_counts(300) == (30, 15) and qg.split_counts(300, 40, 20) == (40, 20)
    assert qg.split_counts(5) == (1, 1) and qg.split_counts(2) == (0, 1) and qg.split_counts(10, 9, 9) == (4, 5)   # 다른 질문 1개 이상 남김
    with pytest.raises(ValueError):
        qg.split_counts(10, -1)


def test_dedicated_steps_make_ten_turn_conversations_and_share_one_long_document(tmp_path):
    fake = DedicatedGPT()
    builder = qg.QuestionSetBuilder(fake, tmp_path)
    result = builder.build(target_questions=12, multi_turn_count=3, long_context_count=2)
    groups = result["groups"]
    assert len(groups) == 12
    long_qs = [g for g in groups if g["turns"][0].get("reference")]
    assert len(long_qs) == 2 and long_qs[0]["turns"][0]["reference"] is long_qs[1]["turns"][0]["reference"] or \
        long_qs[0]["turns"][0]["reference"] == long_qs[1]["turns"][0]["reference"]
    assert builder.long_references == 1 and "중도 해지는 30일 전까지 서면으로 통보한다." in long_qs[0]["turns"][0]["reference"]
    assert all("attachments" not in t for g in long_qs for t in g["turns"])   # 긴 문서 질문은 단일 턴·자료 필드 없음
    convs = [g for g in groups if len(g["turns"]) >= multi_turn.MIN_TURNS]
    assert [g["topic"] for g in groups[2:5]] == [g["topic"] for g in convs[:3]] and any("턴이라 뺐습니다" in w for w in builder.warnings)
    assert len(convs) == 4   # 전용 단계 3개 + 일반 계획이 만든 10턴 대화 1개 (이것도 분량 요구를 붙인다)
    # 답변 분량은 생성 프롬프트의 설계 안내로만 — 모델에게 보내는 질문 문장에는 분량을 쓰지 않는다 (사용자 결정 2026-10-14)
    ten_turn = next(p for p in fake.prompts if p.startswith("당신은 한국어 AI 답변 품질 평가에 쓸 10턴 대화를 설계"))
    assert f"약 {qg.TURN_ANSWER_CHARS:,}자 이상 나오도록" in ten_turn and "질문에 쓰지 않습니다" in ten_turn
    assert not any("자 이상으로 자세히 답해" in t["question"] for g in groups for t in g["turns"])
    for conv in convs[:3]:
        assert conv["turns"][0]["question"] == "앞으로 존댓말로 답해 주세요. 행사 계획을 정리해 주세요."
    plan = next(p for p in fake.prompts if p.startswith("당신은 한국어 AI 답변 품질 평가에 쓸 질문 세트를 설계"))
    assert "# 멀티턴 대화 (J6)" not in plan and "J6-1.a" not in plan and "J7-1.a" not in plan and "따로 만듭니다" in plan
    coverage = result["check"]["coverage"]["items"]
    assert coverage["J7-1.a"] == 2 and coverage["J6-1.a"] >= 3   # 질문만으로 센 적용 수 (답변 전 10턴 대화는 J6 적용으로 센다)


def test_service_validates_dedicated_counts(tmp_path):
    service = qg.QuestionSetService(lambda: {"key": "k", "model": "gpt-test"}, store=tmp_path, llm_factory=lambda config, usage: FakeGPT())
    for bad in ({"multi_turn_conversations": 3}, {"target_questions": 20, "long_context_questions": -1},
                {"target_questions": 20, "multi_turn_conversations": "3"}):
        with pytest.raises(ValueError):
            service.start(bad)


def test_catalog_targets_are_checks_and_items_with_condition_hints():
    """기준 단위는 72(CODE 검사 + 의미 항목) — 검사가 알아보는 질문 조건은 id 없이 표현 안내로만 싣는다 (사용자 결정 2026-10-15)."""
    from code_eval.criteria_registry import condition_kinds
    prompt = qg.plan_prompt()
    hints = [line for line in prompt.splitlines() if "알아보는 질문 조건(섞어 씀)" in line]
    assert len(hints) >= 15 and not any(c["id"] in line for c in condition_kinds() for line in hints)   # '검사:종류' id 는 노릴 대상이 아니다
    assert "세부 조건 id" not in prompt and "'검사:종류'" not in prompt
    lines = "\n".join(qg._catalog_lines(qg.criteria_catalog(), {"2-1.count"}))
    assert "- 2-1.count · " in lines and "어절 수" in lines and "단어(어절) 수" in lines and "2-2.table" not in lines
    plans, _ = qg.parse_plan(json.dumps({"questions": [{"target_items": ["2-1.count", "2-1.count:word_count", "J2-1.a"], "turns": [{"question": "q"}]}]}))
    assert plans[0]["target_items"] == ["2-1.count", "J2-1.a"]


def test_per_target_adds_questions_for_criteria_below_the_target(tmp_path):
    """질문 수 없이 기준마다 목표 적용 수만 주면, 모자란 세부 기준을 노린 질문을 더 만든다 (모자란 기준 목록을 프롬프트에)."""
    fake = FakeGPT()
    builder = qg.QuestionSetBuilder(fake, tmp_path)
    result = builder.build(per_target=2, multi_turn_count=0, long_context_count=0)
    extra = [p for p in fake.prompts if p.startswith("당신은 한국어 AI 답변 품질 평가에 쓸 질문 세트에 질문을 추가")]
    assert extra and "기준(CODE 검사·의미 점검 항목)마다 적용 질문이 2개 이상이 되도록" in extra[0] and "목표 None" not in extra[0]
    low = extra[0].split("# 적용 질문이 적은 항목 (id · 현재 적용 질문 수)\n", 1)[1].split("\n\n", 1)[0]
    assert low and not any(":" in line.split(" · ")[0] for line in low.splitlines())   # 검사·항목 id 만 — 질문 조건 종류 단위는 없다
    assert result["extra_rounds"] <= qg.MAX_EXTRA_ROUNDS and len(result["groups"]) > 4
    assert all(n >= 0 for n in qg.target_counts(result["check"]).values())


def test_service_rejects_dedicated_counts_that_exhaust_the_cap(tmp_path):
    """전용 단계 수의 합이 상한(질문 수, 없으면 MAX_PLAN_QUESTIONS)에 닿으면 거절 — 다른 항목에 쓸 질문이 남아야 한다 (Codex 리뷰)."""
    service = qg.QuestionSetService(lambda: {"key": "k", "model": "gpt-test"}, store=tmp_path, llm_factory=lambda config, usage: FakeGPT())
    half = qg.MAX_PLAN_QUESTIONS // 2 + 1   # 상한(지금 800)을 바꿔도 합이 상한에 닿게
    for bad in ({"per_target": 1, "multi_turn_conversations": half, "long_context_questions": half},
                {"target_questions": 10, "multi_turn_conversations": 5, "long_context_questions": 5}):
        with pytest.raises(ValueError):
            service.start(bad)
    cap = qg.MAX_PLAN_QUESTIONS
    assert qg.split_counts(None, cap, cap, per_target=1) == (cap // 2 - 1, cap // 2)   # 직접 부를 때도 상한 안으로 줄인다


class FlakyMultiTurnGPT(DedicatedGPT):
    """10턴 대화 요청의 처음 몇 번은 턴이 모자란 대화만 준다 — 전용 단계가 목표에 못 미치는 경우."""
    def __init__(self, failures):
        super().__init__()
        self.failures = failures

    def __call__(self, prompt, max_tokens):
        if prompt.startswith("당신은 한국어 AI 답변 품질 평가에 쓸 10턴 대화를 설계") and self.failures > 0:
            self.failures -= 1
            self.prompts.append(prompt)
            return json.dumps({"questions": [{"type": "멀티턴", "topic": "짧음", "turns": [{"question": "안녕"}, {"question": "다시"}]}]},
                              ensure_ascii=False)
        return super().__call__(prompt, max_tokens)


def test_per_target_tops_up_j6_through_the_dedicated_step_and_reports_shortfalls(tmp_path):
    """전용 단계가 처음에 10턴 대화를 못 만들어도, J6 항목이 목표에 못 미치면 전용 단계로 다시 만든다. 못 채운 기준은 숨기지 않는다."""
    fake = FlakyMultiTurnGPT(failures=4)   # 첫 전용 단계(최대 4회 요청)는 전부 실패
    builder = qg.QuestionSetBuilder(fake, tmp_path)
    result = builder.build(per_target=1, long_context_count=0)
    assert any("10턴 대화를 1개 중 0개만" in w for w in builder.warnings)
    assert sum(1 for g in result["groups"] if len(g["turns"]) >= multi_turn.MIN_TURNS) >= 1
    assert result["check"]["coverage"]["items"]["J6-1.a"] >= 1
    assert isinstance(result["short"], dict)
    if result["short"]:
        assert any("채우지 못한 기준" in w for w in builder.warnings)


def test_dedicated_recovery_runs_before_general_questions_fill_the_cap(tmp_path):
    """질문 수와 per_target 을 함께 줘도, 전용 단계가 처음에 실패하면 일반 질문이 자리를 다 쓰기 전에 다시 만든다 (Codex 리뷰 2회)."""
    fake = FlakyMultiTurnGPT(failures=4)
    builder = qg.QuestionSetBuilder(fake, tmp_path)
    result = builder.build(target_questions=4, per_target=1, long_context_count=0)
    assert len(result["groups"]) == 4
    assert sum(1 for g in result["groups"] if len(g["turns"]) >= multi_turn.MIN_TURNS) >= 1 and result["check"]["coverage"]["items"]["J6-1.a"] >= 1


def test_question_input_reads_text_json_conversations_and_generated_sets():
    """질문 직접 넣기 — 글은 '---' 줄로 나누고, JSON 은 질문 목록·대화(conversation_id·turn)·생성 세트를 읽는다. 답변·첨부는 쓰지 않는다."""
    groups, warnings = qg.parse_question_input("146 + 279는 얼마야?\n---\n공부 방법을 불릿으로 정확히 3개 작성해줘.\n")
    assert [g["turns"][0]["question"] for g in groups] == ["146 + 279는 얼마야?", "공부 방법을 불릿으로 정확히 3개 작성해줘."] and not warnings
    rows = [{"question": "두 번째", "conversation_id": "c", "turn": 2, "answer": "쓰지 않음"}, {"question": "첫 번째", "conversation_id": "c", "turn": 1},
            "단일 질문", {"answer": "질문 없음"}]
    groups, warnings = qg.parse_question_input(json.dumps(rows, ensure_ascii=False), "q.json")
    assert [[t["question"] for t in g["turns"]] for g in groups] == [["첫 번째", "두 번째"], ["단일 질문"]] and len(warnings) == 1
    generated = [{"id": 7, "type": "첨부", "topic": "견적", "target_items": ["J8-1.a"], "turns": [{"turn": 1, "question": "합계는?", "attachments": [{"name": "a.pdf"}]}]}]
    groups, warnings = qg.parse_question_input(json.dumps(generated, ensure_ascii=False), "set.json")
    assert groups[0]["id"] == 1 and groups[0]["topic"] == "견적" and "attachments" not in groups[0]["turns"][0] and "첨부파일" in warnings[0]
    with pytest.raises(ValueError):
        qg.parse_question_input("   ")


def test_imported_questions_make_a_completed_set_ready_for_review(tmp_path):
    service = qg.QuestionSetService(lambda: None, store=tmp_path)
    job = service.import_questions({"text": "146 + 279는 얼마야?\n---\n오늘 할 일을 표로 정리해줘.", "source": "manual"})
    assert job["status"] == "COMPLETED" and job["source"] == "manual" and job["run_id"] is None and job["summary"]["questions"] == 2
    assert job["coverage"]["taxonomy"]["total"] == 72 and service.list()[0]["id"] == job["id"]
    assert [row["turns"][0]["question"] for row in service.preview(job["id"])] == ["146 + 279는 얼마야?", "오늘 할 일을 표로 정리해줘."]


def test_plan_with_missing_closing_brackets_is_read_and_reported():
    """GPT 계획 끝 괄호가 빠진 응답(2026-10-14 실제) — 모자란 괄호만 채워 읽고 경고로 남긴다. 짝이 어긋난 괄호는 고치지 않는다."""
    text = json.dumps(PLAN, ensure_ascii=False)
    plans, warnings = qg.parse_plan(text[:-2])
    assert len(plans) == 4 and any("닫는 괄호 2개" in w for w in warnings)
    fenced, warnings = qg.parse_plan("```json\n" + text[:-2] + "\n```")   # 코드 펜스로 감싼 응답도 같게 (Codex)
    assert len(fenced) == 4 and any("닫는 괄호 2개" in w for w in warnings)
    prose, _ = qg.parse_plan("계획입니다:\n```json\n" + text[:-2] + "\n```")   # 펜스 앞에 설명 글이 있어도 (Codex)
    assert len(prose) == 4
    with pytest.raises(qg.GenerationError):
        qg.parse_plan(text[:-2] + "}]")


def test_builder_reports_made_and_planned_questions_while_generating(tmp_path):
    """생성 중 화면 목록 — 만든 질문은 made, 지금 만드는 질문은 making, 남은 계획은 planned (사용자 요청 2026-10-14)."""
    seen = []
    builder = qg.QuestionSetBuilder(FakeGPT(), tmp_path, progress=lambda phase, data: seen.append(data["questions"]) if "questions" in data else None)
    groups = builder.build()["groups"]
    first = next(rows for rows in seen if rows)
    assert [r["state"] for r in first][:2] == ["making", "planned"] and first[0]["turns"][0]["question"].startswith("공부 방법")
    assert [r["state"] for r in seen[-1]] == ["made"] * len(groups) and seen[-1][0]["id"] == 1
    assert all(len(t["question"]) <= qg.PREVIEW_CHARS + 2 for rows in seen for r in rows for t in r["turns"])


def test_fixed_question_sets_become_completed_sets_with_files(tmp_path):
    """고정 질문 세트(간단·정밀) — 모델 호출 없이 완료된 세트, 첨부 파일까지 복사 (사용자 결정 2026-10-14)."""
    service = qg.QuestionSetService(lambda: None, store=tmp_path)
    listed = {item["kind"]: item for item in service.fixed_sets()}
    assert set(listed) == {"simple", "precise"} and listed["simple"]["questions"] < listed["precise"]["questions"]
    job = service.start_fixed({"kind": "simple"})
    assert job["status"] == "COMPLETED" and job["version"] == qg.FIXED_SET_VERSION and job["source"] == "fixed" and job["run_id"] is None
    rows = service.preview(job["id"])
    assert len(rows) == listed["simple"]["questions"] and job["summary"]["questions"] == len(rows)
    names = {a for r in rows for t in r["turns"] for a in t["attachments"]}
    assert not names or any(service.files_dir(job["id"]).iterdir())   # 첨부 질문이 있으면 파일도 함께 복사
    with pytest.raises(ValueError):
        service.start_fixed({"kind": "huge"})


def test_plan_prompt_only_lists_the_criteria_still_needed():
    """공개 질문이 이미 덮는 기준은 첫 계획의 점검 목록에서 빠진다 — 검사·항목은 그 id 만 (10-15 사용자 결정)."""
    catalog = qg.criteria_catalog()
    prompt = qg.plan_prompt(catalog, only={"2-2.table", "J2-1.a"})
    assert "- 2-2.table · " in prompt and "- 2-2.list · " not in prompt
    assert "- J2-1.a · " in prompt and "- J3-1.a · " not in prompt and "- 2-1.count · " not in prompt
    assert "이미 다른 질문이 충분히 덮는 기준을 뺀 것입니다" in prompt
    assert qg.plan_prompt(catalog) == qg.plan_prompt(catalog, only=None)   # only 가 없으면 전과 같다


def test_base_counts_skip_covered_criteria_and_count_toward_the_target(tmp_path):
    """밖에서 확보한 적용 수(base_counts)는 빠진 항목·모자란 기준을 셀 때 더해진다 — 이미 덮인 기준은 첫 계획에 묻지 않고 부족 목록에도 없다."""
    names = list(qg.target_counts(qg.check([])))
    assert len(names) == 64   # 72 − 답변으로만 정해지는 검사 7 · J5-3.a
    covered = {t: 3 for t in names if t not in ("2-2.table", "J2-1.a")}
    fake = FakeGPT()
    builder = qg.QuestionSetBuilder(fake, tmp_path)
    result = builder.build(per_target=2, multi_turn_count=0, long_context_count=0, base_counts=covered)
    first = next(p for p in fake.prompts if p.startswith("당신은 한국어 AI 답변 품질 평가에 쓸 질문 세트를 설계"))
    assert "- 2-2.table · " in first and "- J2-1.a · " in first and "- J3-1.a · " not in first
    assert not any(t in result["short"] for t in covered)   # 밖에서 3개씩 확보한 기준은 목표 2개를 넘어 부족 목록에 없다
    low = [p for p in fake.prompts if "# 적용 질문이 적은 항목" in p]
    assert all("- J3-1.a · " not in p.split("# 적용 질문이 적은 항목", 1)[1].split("\n\n", 1)[0] for p in low)
