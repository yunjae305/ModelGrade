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
    {"type": "글쓰기", "topic": "공부", "target_items": ["2-1.count", "J4-3.a", "없는-id"],
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
    assert {cid: report["criteria"][cid]["applicable_units"] for cid in ("J6", "J7", "J8")} == {"J6": 1, "J7": 1, "J8": 1}
    assert report["coverage"]["items"]["J8-2.a"] == 1 and report["coverage"]["checks"]["2-1.count"] == 1
    supplements = [p for p in fake.prompts if p.startswith("당신은 한국어 AI 답변 품질 평가에 쓸 질문 세트를 보충")]
    assert len(supplements) == result["rounds"] == qg.SUPPLEMENT_ROUNDS and "# 빠진 항목" in supplements[0]
    assert groups[-1]["topic"] == "보충" and report["coverage"]["checks"]["2-2.table"] >= 1


def test_plan_prompt_lists_every_check_and_item_from_the_criteria():
    catalog = qg.criteria_catalog()
    prompt = qg.plan_prompt(catalog)
    assert len(catalog["code"]) == 28 and len(catalog["items"]) == 44
    assert all(f"- {c['id']} · " in prompt for c in catalog["code"]) and all(f"- {i['id']} · " in prompt for i in catalog["items"])
    with pytest.raises(qg.GenerationError):
        qg.parse_plan("JSON 이 아닌 응답")


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
