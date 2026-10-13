"""J8 첨부 이미지를 평가 모델에도 보낸다 (judge-attachment-images-v1, 10-14 4차) — 유료 호출 없이 모의 평가 모델로 확인.

이미지가 확인을 통과하면 평가 요청에 실제 그림이 실리고, 파일 누락·해시 불일치·형식 오류·이미지 미지원 평가 모델이면 그림이 있어야
하는 기준(J8)은 묻지 않고 판정 미완료로 남긴다. 다른 기준(J1 등)은 글자로 그대로 평가한다. 실제 멀티모달 판정 정확도는 실호출 전까지 미검증.
"""
import base64
import hashlib
import json

import httpx

from code_eval import attachment_images
from code_eval.anthropic_semantic_client import AnthropicSemanticClient, AnthropicSemanticConfig, FINAL_TOOL
from code_eval.jev_client import JevCall
from code_eval.jev_eval import evaluate_jev_batch, jev_request
from code_eval.semantic_cli_common import build_prompt_parts

PNG = b"\x89PNG\r\n\x1a\n" + b"red-car-pixels"
JPEG = b"\xff\xd8\xff\xe0" + b"bar-chart-pixels"
QUESTION = "첨부한 사진 속 자동차는 무슨 색인가요?"
ANSWER = "사진 속 자동차는 빨간색입니다."


class Judge:
    """모의 평가 모델 — 받은 state·questions·images 를 남기고 모두 PASS. ``images=False`` 면 이미지 입력을 지원하지 않는 연결."""

    def __init__(self, images=True):
        self.calls = []
        if images:
            self.supports_images = True

    def system_one(self, state, questions, **kwargs):
        if "images" in kwargs and not getattr(self, "supports_images", False):
            raise TypeError("이미지를 지원하지 않는 연결에 images 를 넘김")
        self.calls.append({"state": json.loads(json.dumps(state)), "questions": dict(questions), "images": kwargs.get("images")})
        return JevCall("OK", body={"model": "judge-fixture", "answers": {
            cid: {"choice": "PASS", "probabilities": {"PASS": .9, "FAIL": .1}, "confidence": .9, "reason": "모의 판정"}
            for cid in questions}})


def _entry(name, data=None, **extra):
    entry = {"name": name, "type": "image", "content": "자동차 사진", "path": name}
    if data is not None:
        entry["sha256"] = hashlib.sha256(data).hexdigest()
    return {**entry, **extra}


def _run(tmp_path, attachments, judge, question=QUESTION, answer=ANSWER):
    case = {"case_id": "Q1", "question": question, "answer": answer, "case_status": "EVALUATED"}
    if attachments:
        case["attachments"] = attachments
    batch = evaluate_jev_batch({"cases": [case]}, judge, None, None, context={"attachment_roots": [str(tmp_path)]})
    return batch, batch["records"][0]


def test_normal_image_reaches_the_judge_request_and_is_linked_to_the_record(tmp_path):
    (tmp_path / "car.png").write_bytes(PNG)
    judge = Judge()
    batch, record = _run(tmp_path, [_entry("car.png", PNG)], judge)
    call = judge.calls[0]
    assert "J8" in call["questions"] and call["state"]["attachments"][0]["image_attached"] is True
    assert call["images"] == [{"index": 0, "name": "car.png", "media_type": "image/png", "data": base64.b64encode(PNG).decode()}]
    assert record["case_id"] == "Q1" and record["decisions"]["J8"]["decision"] == "PASS"
    sent = record["attachment_images"]["sent"][0]
    assert (sent["sha256"], sent["sha256_verified"], sent["media_type"]) == (hashlib.sha256(PNG).hexdigest(), True, "image/png")
    assert "data" not in sent and batch["attachment_image_policy"] == attachment_images.JUDGE_IMAGE_POLICY_VERSION


def test_text_only_question_request_and_record_are_unchanged(tmp_path):
    judge = Judge()
    batch, record = _run(tmp_path, None, judge, question="파이썬 리스트를 정렬하는 법을 알려줘.", answer="sorted() 를 씁니다.")
    assert judge.calls[0]["images"] is None and "attachments" not in judge.calls[0]["state"]
    assert "attachment_images" not in record and "attachment_image_policy" not in batch
    request = jev_request("질문", "답", ("J1",), attachments=[{"name": "memo.txt", "type": "txt", "content": "메모"}])
    assert request["state"]["attachments"] == [{"name": "memo.txt", "type": "txt", "content": "메모"}]   # 글자 첨부는 예전 모양 그대로
    assert "첨부 이미지" not in build_prompt_parts(request["state"], request["questions"])[1]


def _held(record, code):
    j8 = record["decisions"]["J8"]
    assert j8["status"] == "INCOMPLETE" and j8["decision"] is None and j8["reason_code"] == "ATTACHMENT_IMAGE_UNAVAILABLE"
    assert [issue["code"] for issue in j8["attachment_issues"]] == [code]
    assert j8["decision_policy"]["factual_error_confirmed"] is False
    assert record["decisions"]["J1"]["decision"] == "PASS" and "J8" in record["criteria"]   # 글자로 되는 기준은 그대로 평가
    assert record["attachment_images"]["held_criteria"] == ["J8"]


def test_missing_image_file_leaves_j8_incomplete_and_still_judges_the_rest(tmp_path):
    judge = Judge()
    batch, record = _run(tmp_path, [_entry("gone.png", PNG)], judge)
    assert "J8" not in judge.calls[0]["questions"] and judge.calls[0]["images"] is None
    _held(record, "IMAGE_FILE_MISSING")
    assert batch["summary"]["J8"]["targets"] == 1 and batch["summary"]["J8"]["incomplete"] == 1
    assert batch["summary"]["J8"]["pass"] == batch["summary"]["J8"]["fail"] == 0


def test_hash_mismatch_is_not_sent(tmp_path):
    (tmp_path / "car.png").write_bytes(PNG)
    judge = Judge()
    _, record = _run(tmp_path, [_entry("car.png", b"another picture")], judge)
    assert judge.calls[0]["images"] is None and judge.calls[0]["state"]["attachments"][0]["image_attached"] is False
    _held(record, "IMAGE_HASH_MISMATCH")


def test_judge_without_image_support_gets_no_images_and_j8_is_incomplete(tmp_path):
    (tmp_path / "car.png").write_bytes(PNG)
    judge = Judge(images=False)
    _, record = _run(tmp_path, [_entry("car.png", PNG)], judge)
    assert record["status"] == "SUCCESS" and "J8" not in judge.calls[0]["questions"]
    _held(record, "JUDGE_IMAGES_UNSUPPORTED")
    assert record["attachment_images"]["judge_supports_images"] is False and record["attachment_images"]["sent"] == []


def test_wrong_mime_by_file_signature_is_rejected_for_judge_and_collection(tmp_path):
    from webapp.attachment_delivery import prepare
    (tmp_path / "car.png").write_bytes(JPEG)          # 확장자는 PNG, 내용은 JPEG
    (tmp_path / "note.png").write_bytes(b"not an image at all")
    for name, data in (("car.png", JPEG), ("note.png", b"not an image at all")):
        judge = Judge()
        _, record = _run(tmp_path, [_entry(name, data)], judge)
        _held(record, "IMAGE_TYPE_INVALID")
        _, images, delivery = prepare(QUESTION, [_entry(name, data)], [tmp_path])
        assert images == [] and delivery[0]["mode"] == "not_sent" and delivery[0]["code"] == "IMAGE_TYPE_INVALID"


def test_claim_that_needs_the_picture_is_judged_only_when_the_picture_is_delivered(tmp_path):
    """그림 설명(content)에 색이 없다 — '빨간색' 주장은 그림을 봐야 확인된다. 그림이 가면 J8 을 묻고, 못 가면 글자만으로 J8 을 판정하지 않는다."""
    (tmp_path / "car.png").write_bytes(PNG)
    judge = Judge()
    _run(tmp_path, [_entry("car.png", PNG)], judge)
    assert "J8" in judge.calls[0]["questions"] and judge.calls[0]["images"][0]["data"] == base64.b64encode(PNG).decode()
    blind = Judge()
    _, record = _run(tmp_path, [_entry("car.png", b"different file")], blind)
    assert "J8" not in blind.calls[0]["questions"]
    _held(record, "IMAGE_HASH_MISMATCH")
    note = build_prompt_parts(blind.calls[0]["state"], blind.calls[0]["questions"])[1]
    assert "그림을 받지 못한 것" in note   # 평가 모델에도 그 그림을 보지 못했다고 알린다


def test_multiple_attachments_keep_order_and_a_partial_failure_still_holds_j8(tmp_path):
    (tmp_path / "car.png").write_bytes(PNG)
    (tmp_path / "chart.jpg").write_bytes(JPEG)
    memo = {"name": "memo.txt", "type": "txt", "content": "주차장 B동"}
    judge = Judge()
    _, record = _run(tmp_path, [_entry("car.png", PNG), memo, _entry("chart.jpg", JPEG)], judge)
    call = judge.calls[0]
    assert [(i["index"], i["name"], i["media_type"]) for i in call["images"]] == [(0, "car.png", "image/png"), (2, "chart.jpg", "image/jpeg")]
    assert [a.get("image_attached") for a in call["state"]["attachments"]] == [True, None, True]
    assert call["state"]["attachments"][1] == memo and record["decisions"]["J8"]["decision"] == "PASS"
    partial = Judge()
    _, record = _run(tmp_path, [_entry("car.png", PNG), memo, _entry("lost.jpg", JPEG)], partial)
    assert [i["name"] for i in partial.calls[0]["images"]] == ["car.png"]   # 확인된 그림은 그대로 보낸다
    assert [a.get("image_attached") for a in partial.calls[0]["state"]["attachments"]] == [True, None, False]
    _held(record, "IMAGE_FILE_MISSING")


def test_content_only_image_entry_keeps_the_existing_j8_contract(tmp_path):
    """파일을 가리키지 않는 이미지 항목(팀원 J8 형식 — name·type·content 만)은 예전처럼 content 로 J8 을 판정하고 기록만 남긴다."""
    judge = Judge()
    _, record = _run(tmp_path, [{"name": "chart.png", "type": "image", "content": "1월 30 · 2월 45"}], judge)
    assert "J8" in judge.calls[0]["questions"] and judge.calls[0]["images"] is None
    assert "image_attached" not in judge.calls[0]["state"]["attachments"][0]
    assert record["decisions"]["J8"]["decision"] == "PASS"
    assert record["attachment_images"]["content_only"] == [{"index": 0, "name": "chart.png"}]


def test_anthropic_judge_puts_labelled_images_between_shared_rules_and_case_data(tmp_path):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "claude-opus-5-5", "stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "final", "name": FINAL_TOOL, "input": {
                cid: {"choice": "PASS", "confidence": .8, "reason": "그림과 일치"} for cid in ("J1", "J8")}}],
            "usage": {"input_tokens": 10, "output_tokens": 5, "server_tool_use": {"web_search_requests": 0}}})
    client = AnthropicSemanticClient(AnthropicSemanticConfig(web_search="disabled"), "secret-test-key",
                                     transport=httpx.MockTransport(handler))
    attachments = [_entry("car.png", PNG)]
    request = jev_request(QUESTION, ANSWER, ("J1", "J8"), attachments=attachments, image_attached={0: True})
    image = {"index": 0, "name": "car.png", "media_type": "image/png", "data": base64.b64encode(PNG).decode()}
    result = client.system_one(request["state"], request["questions"], images=[image])
    content = sent[0]["messages"][0]["content"]
    assert result.status == "OK" and content[0]["cache_control"] == {"type": "ephemeral"}   # 공통 지시문 캐시는 그대로
    assert content[1] == {"type": "text", "text": "[첨부 이미지 1: car.png]"}
    assert content[2] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image["data"]}}
    assert "image_attached 가 true 인 항목은 실제 그림이" in content[3]["text"] and len(content) == 4
    plain = AnthropicSemanticClient(AnthropicSemanticConfig(web_search="disabled"), "k").build_request(request["state"], request["questions"])[0]
    assert plain["messages"][0]["content"][0]["text"] == content[0]["text"]   # 그림이 없으면 예전 두 부분 그대로
    assert len(plain["messages"][0]["content"]) == 2


def test_items_pipeline_drops_j8_items_when_the_picture_cannot_be_judged(tmp_path):
    """items 방식도 같다 — 그림을 못 보내면 J8 점검 항목을 보내지 않고 J8 을 판정 미완료로, 보내면 J8 항목을 묻는다."""
    from tools.items_e2e_demo import MockJudge

    class SeeingJudge(MockJudge):
        supports_images = True

        def system_one(self, state, questions, *, cancel_event=None, images=None):
            self.images = images
            return super().system_one(state, questions, cancel_event=cancel_event)
    (tmp_path / "car.png").write_bytes(PNG)
    case = {"case_id": "Q1", "question": QUESTION, "answer": ANSWER, "case_status": "EVALUATED",
            "attachments": [_entry("car.png", PNG)]}
    for judge, asked in ((MockJudge({}, {}), False), (SeeingJudge({}, {}), True)):
        batch = evaluate_jev_batch({"cases": [dict(case)]}, judge, None, None, pipeline="items",
                                   context={"attachment_roots": [str(tmp_path)]})
        record = batch["records"][0]
        sent_items = {item_id for call in judge.calls for item_id in call["questions"]}
        assert any(item_id.startswith("J8") for item_id in sent_items) is asked
        if asked:
            assert judge.images[0]["name"] == "car.png" and record["decisions"]["J8"]["decision"] in ("PASS", "FAIL")
        else:
            assert record["decisions"]["J8"]["status"] == "INCOMPLETE"
            assert record["decisions"]["J8"]["attachment_issues"][0]["code"] == "JUDGE_IMAGES_UNSUPPORTED"
            assert record["decisions"]["J1"]["decision"] in ("PASS", "FAIL")
