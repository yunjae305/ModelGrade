"""업로드 직후 자동 수집 — 관리 설정·한도·중복 방지·멀티턴 문맥·부분 실패·재시도·FactChat·평가 연결.

제공자 HTTP 는 모두 MockTransport 대역이고 의미 평가는 고정 대역 판정이다. 유료 호출이 없으며, 실제 제공자 응답 형식이나
실제 Judge 판정 품질은 이 테스트가 확인하지 않는다.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

from semantic_judge_fixture import Judge
from webapp import auto_collection as auto
from webapp.code_eval_service import CodeEvalService
from webapp.comparison_service import ComparisonService

LIVE = ("gpt", "claude", "gemini", "factchat")
POLICY = {"allowed": {p: True for p in LIVE}, "prices": {p: {"input": 1.0, "output": 2.0} for p in LIVE},
          "limits": {"job_calls": 100, "job_cost_usd": 10.0, "daily_calls": 500, "daily_cost_usd": 50.0},
          "max_output_tokens": 1024}
CASES = [{"id": "S1", "question": "단일 질문", "answer": "AIONA 단일 답변"},
         {"id": "C1-1", "conversation_id": "C1", "turn": 1, "question": "첫 질문", "answer": "AIONA 첫 답"},
         {"id": "C1-2", "conversation_id": "C1", "turn": 2, "question": "이어지는 질문", "answer": "AIONA 둘째 답"}]


def provider_of(url):
    host = httpx.URL(url).host
    return {"api.openai.com": "gpt", "api.anthropic.com": "claude", "generativelanguage.googleapis.com": "gemini",
            "api.factchat.ai": "factchat", "factchat-cloud.mindlogic.ai": "factchat"}[host]


def reply(provider, text):
    if provider == "gpt":
        return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                "usage": {"input_tokens": 10, "output_tokens": 20}}
    if provider == "claude":
        return {"type": "message", "stop_reason": "end_turn", "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 10, "output_tokens": 20}}
    if provider == "gemini":
        return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20}}
    return {"object": "chat.completion", "model": "claude-sonnet-5-20261001",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20}}


class Providers:
    """제공자별 요청 기록과 응답 대역. fail={provider: status} 이면 그 제공자는 HTTP 오류를 돌려준다."""

    def __init__(self, fail=None):
        self.fail = dict(fail or {})
        self.requests = []

    def __call__(self, request):
        provider = provider_of(str(request.url))
        body = json.loads(request.content)
        self.requests.append((provider, str(request.url), body))
        if provider in self.fail:
            return httpx.Response(self.fail[provider], json={"error": "fixture"})
        count = sum(1 for p, _, _ in self.requests if p == provider)
        return httpx.Response(200, json=reply(provider, f"{provider} 답변 {count}"))

    def of(self, provider):
        return [body for p, _, body in self.requests if p == provider]


def make(tmp_path, providers=None, judge=None, configure=("gpt", "claude", "gemini"), policy=POLICY, workers=3):
    providers = providers or Providers()
    judge = judge or Judge()
    evaluator = CodeEvalService(save_dir=tmp_path / "eval", jev_resolver=lambda **_: (judge, judge.config, None))
    svc = ComparisonService(evaluator, save_dir=tmp_path / "runs", collection_workers=workers,
                            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(providers)))
    for provider in configure:
        if provider == "factchat":
            svc.configure("factchat", {"key": "sk-fc-test", "model": "claude-sonnet-5"})
        else:
            svc.configure(provider, {"key": "sk-test-not-real", "model": f"{provider}-fixture"})
    if policy is not None:
        svc.update_auto_policy(policy)
    return svc, providers, judge


def wait(svc, run_id):
    """작업이 완전히 끝날 때까지 — 상태가 READY 여도 작업 스레드가 아직 등록 해제되지 않았으면 collect_more 가 '수집 진행 중'으로
    거절한다 (상태 변경과 스레드 정리 사이의 짧은 틈, 전체 테스트 부하에서 간헐 실패하던 원인 · 10-14 4차). 상한 15초."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if svc.status(run_id)["status"] not in ("COLLECTING", "EVALUATING") and not svc._workers.get(run_id):
            return svc.result(run_id)
        time.sleep(0.01)
    raise AssertionError("run did not finish")


def upload(svc, **extra):
    return svc.start_auto_collection({"cases": CASES, "name": "업로드", "input_name": "aiona.json", **extra})


def test_upload_starts_collection_for_connected_allowed_providers_without_a_selection_step(tmp_path):
    svc, providers, _ = make(tmp_path)
    started = upload(svc)
    result = wait(svc, started["run"]["run_id"])
    assert started["reused"] is False and result["status"] == "READY" and result["phase"] == "COLLECT"
    assert result["providers"] == ["aiona", "gpt", "claude", "gemini"] and not result["evaluations"]
    assert {p for p, _, _ in providers.requests} == {"gpt", "claude", "gemini"} and len(providers.requests) == 9
    states = result["auto_collection"]["providers"]
    assert states["gpt"]["state"] == "completed" and states["factchat"]["state"] == "needs_connection"
    assert states["oosoo"] == {"state": "unsupported", "label": "연동 준비 중", "reason": auto.TARGETS["oosoo"]["note"]}
    assert result["datasets"]["aiona"][0]["answer"] == "AIONA 단일 답변"   # 원본 답변은 덮어쓰지 않는다
    assert all(row["collection"]["status"] == "SUCCESS" for p in ("gpt", "claude", "gemini") for row in result["datasets"][p])
    assert "sk-test" not in json.dumps(result, ensure_ascii=False) and "sk-test" not in json.dumps(svc.auto_collection_status())


def test_nothing_is_requested_until_the_operator_allows_it_and_sets_every_limit(tmp_path):
    svc, providers, _ = make(tmp_path, policy=None)
    status = {t["id"]: t["state"] for t in svc.auto_collection_status()["targets"]}
    assert status == {"gpt": "disabled", "claude": "disabled", "gemini": "disabled", "factchat": "needs_connection", "oosoo": "unsupported"}
    run = wait(svc, upload(svc)["run"]["run_id"])
    assert run["providers"] == ["aiona"] and run["status"] == "READY" and not providers.requests
    svc.update_auto_policy({"allowed": {"gpt": True}, "prices": {"gpt": {"input": 1, "output": 2}}})
    assert {t["id"]: t["state"] for t in svc.auto_collection_status()["targets"]}["gpt"] == "limits_missing"
    with pytest.raises(ValueError):
        svc.update_auto_policy({"limits": {"job_calls": 0}})
    with pytest.raises(ValueError):
        svc.update_auto_policy({"allowed": {"oosoo": True}})
    stored = json.loads((tmp_path / "auto_collection_policy.json").read_text(encoding="utf-8"))
    assert stored["allowed"]["gpt"] is True and "key" not in json.dumps(stored)


def test_multiturn_keeps_turn_order_and_each_providers_own_history(tmp_path):
    svc, providers, _ = make(tmp_path, configure=("gpt", "claude"))
    result = wait(svc, upload(svc)["run"]["run_id"])
    for provider in ("gpt", "claude"):
        turns = [body["input" if provider == "gpt" else "messages"] for body in providers.of(provider) if len(body.get("input") or body.get("messages")) > 1]
        assert len(turns) == 1
        history = turns[0]
        assert [m["role"] for m in history] == ["user", "assistant", "user"]
        assert history[0]["content"] == "첫 질문" and history[2]["content"] == "이어지는 질문"
        own = result["datasets"][provider][1]["answer"]
        assert history[1]["content"] == own and own.startswith(provider)   # 자기 답변만 이력으로
        assert "AIONA" not in json.dumps(history, ensure_ascii=False)
        assert "system" not in json.dumps(providers.of(provider)[0], ensure_ascii=False)


def test_one_provider_failing_keeps_the_others_and_a_settings_error_stops_only_that_provider(tmp_path):
    # 워커 1개: 대화 C1 → 단일 S1 순서로 요청 (동시 요청이면 이미 출발한 요청은 막지 못한다)
    svc, providers, _ = make(tmp_path, providers=Providers(fail={"claude": 401, "gemini": 500}), workers=1)
    result = wait(svc, upload(svc)["run"]["run_id"])
    assert result["status"] == "READY"
    assert all(row["collection"]["status"] == "SUCCESS" for row in result["datasets"]["gpt"])
    assert len(providers.of("claude")) == 1   # 401 뒤로는 같은 제공자에 다시 요청하지 않는다
    claude = result["datasets"]["claude"]
    assert "HTTP 401" in claude[1]["collection"]["error"] and "앞선 대화 응답이 없어" in claude[2]["collection"]["error"]
    assert "남은 요청을 멈췄습니다" in claude[0]["collection"]["error"] and claude[0]["collection"]["not_requested"]
    assert len(providers.of("gemini")) == 2   # 500 은 그 질문만 실패하고 다음 질문은 계속, 대화 뒤 턴은 건너뜀
    states = result["auto_collection"]["providers"]
    assert (states["gpt"]["state"], states["claude"]["state"], states["gemini"]["state"]) == ("completed", "failed", "failed")


def test_duplicate_upload_and_refresh_reuse_the_job_and_rerun_is_explicit(tmp_path):
    svc, providers, _ = make(tmp_path, configure=("gpt",))
    first = wait(svc, upload(svc)["run"]["run_id"])
    calls = len(providers.requests)
    again = upload(svc)
    assert again["reused"] is True and again["run"]["run_id"] == first["run_id"] and len(providers.requests) == calls
    restarted, _, _ = make(tmp_path, providers=providers, configure=("gpt",), policy=None)   # 서버 재시작 후 같은 파일
    assert upload(restarted)["run"]["run_id"] == first["run_id"] and len(providers.requests) == calls
    changed = [dict(CASES[0], answer="다른 AIONA 답변"), *CASES[1:]]
    other = svc.start_auto_collection({"cases": changed, "name": "다른 업로드"})
    assert other["reused"] is False and other["run"]["run_id"] != first["run_id"]
    wait(svc, other["run"]["run_id"])
    rerun = upload(svc, rerun=True)
    assert rerun["reused"] is False and wait(svc, rerun["run"]["run_id"])["auto_collection"]["rerun_of"] == first["run_id"]


def test_requests_outside_the_approved_call_and_cost_limits_are_never_started(tmp_path):
    tight = {**POLICY, "limits": {"job_calls": 4, "job_cost_usd": 10.0, "daily_calls": 500, "daily_cost_usd": 50.0}}
    svc, providers, _ = make(tmp_path, policy=tight)
    result = wait(svc, upload(svc)["run"]["run_id"])
    assert result["providers"] == ["aiona", "gpt"] and {p for p, _, _ in providers.requests} == {"gpt"}
    states = result["auto_collection"]["providers"]
    assert states["claude"]["state"] == "over_limit" and "작업당 호출 한도" in states["claude"]["reason"]
    budget = result["auto_collection"]["budget"]
    assert budget["spent_calls"] == 3 and budget["authorized_calls"] == 3 and budget["estimated_calls"] == 0
    assert budget["spent_cost_usd"] == pytest.approx(3 * (10 * 1.0 + 20 * 2.0) / 1e6)   # 응답 사용량으로 정산
    # 일일 한도: 오늘 이미 쓴 3회 + 새 작업 3회 > 5회 → 시작하지 않는다
    svc.update_auto_policy({"limits": {"daily_calls": 5}})
    other = svc.start_auto_collection({"cases": [dict(CASES[0], answer="다른 답변"), *CASES[1:]]})
    assert other["run"]["providers"] == ["aiona"] and "오늘 남은 호출 한도" in other["skipped"]["gpt"]
    assert len(providers.requests) == 3
    # 예상 상한 비용이 작업당 비용 한도를 넘으면 시작하지 않는다 (출력 1,024토큰 × 100만 토큰당 5,000달러 × 3회 > 10달러)
    svc.update_auto_policy({"limits": {"daily_calls": 500}, "prices": {"gpt": {"input": 1000, "output": 5000}}})
    priced = svc.start_auto_collection({"cases": [dict(CASES[0], answer="세 번째 답변"), *CASES[1:]]})
    assert "gpt" not in priced["run"]["providers"] and "작업당 비용 한도" in priced["skipped"]["gpt"]
    assert "claude" in priced["run"]["providers"]   # 한도 안에 드는 다른 제공자는 그대로 시작


def test_budget_guard_stops_before_exceeding_the_authorized_cost(tmp_path):
    run = {"run_id": "abcdefabcdef", "auto_collection": {"prices": {"gpt": {"input": 1.0, "output": 1.0}},
                               "budget": {**auto.empty_budget(auto.today()), "authorized_calls": 2, "authorized_cost_usd": 0.002,
                                          "providers": {"gpt": {"planned_calls": 2, "planned_cost_usd": 0.002, "spent_calls": 0, "spent_cost_usd": 0.0}}},
                               "providers": {}}}
    svc = ComparisonService(CodeEvalService(), save_dir=tmp_path)
    guard = svc._auto_guard(run, {"gpt": {"reserves": {0: 0.001, 1: 0.001, 2: 0.001}}}, ["gpt"])
    assert guard.before("gpt", 0) is None and guard.before("gpt", 1) is None   # 동시 요청은 예약으로 센다
    ledger = tmp_path / "auto-reservations" / "abcdefabcdef.json"
    requests = lambda: json.loads(ledger.read_text(encoding="utf-8"))["requests"] if ledger.exists() else {}
    first, second = sorted(requests())
    assert len({first, second}) == 2   # 요청을 보내기 전에 요청 번호별 예약을 디스크에 남긴다 (번호는 겹치지 않음)
    assert "승인된 호출·비용 범위" in guard.before("gpt", 2)
    guard.after("gpt", 0, {"status": "SUCCESS", "raw": None}, None)   # 사용량이 없으면 상한으로 정산
    assert run["auto_collection"]["budget"]["spent_cost_usd"] == pytest.approx(0.001)
    budget = run["auto_collection"]["budget"]
    settled = budget["settled_requests"]
    assert len(settled) == 1 and sorted(requests()) == sorted([first, second])   # 실행 기록 저장 전까지는 예약을 지우지 않는다
    guard.saved()
    assert list(requests()) == [r for r in (first, second) if r not in settled]
    assert budget["by_day"][auto.today()] == {"calls": 1, "cost_usd": pytest.approx(0.001)}
    guard.after("gpt", 1, {"status": "ERROR"}, 404)
    assert "HTTP 404" in guard.before("gpt", 2)


@pytest.mark.parametrize("status", ["COLLECTING", "CANCELLED"])   # 종료 처리로 취소 저장된 실행도
def test_unsettled_reservations_count_as_spend_once_after_a_restart(tmp_path, status):
    svc = ComparisonService(CodeEvalService(), save_dir=tmp_path)
    day = auto.today()
    assert svc._write_reservations("abcdefabcdef", {f"r{n}": {"cost_usd": 0.01, "day": day, "provider": "gpt"} for n in (1, 2, 3, 4)})
    run = {"run_id": "abcdefabcdef", "status": status, "progress": {}, "providers": ["aiona"], "auto_collection": {
        "budget": {**auto.empty_budget(day), "authorized_calls": 5, "authorized_cost_usd": 0.05, "spent_calls": 1,
                   "spent_cost_usd": 0.01, "settled_requests": ["r1"]}, "providers": {}}}
    svc._load_with_reservations(run)   # 재시작 — 응답을 받았는지 모르는 r2~r4 는 이미 청구됐을 수 있다 (r1 은 정산 저장됨)
    budget = run["auto_collection"]["budget"]
    assert (budget["spent_calls"], budget["spent_cost_usd"], budget["estimated_calls"]) == (4, pytest.approx(0.04), 3)
    assert budget["recovered_reservations"] == {"calls": 3, "cost_usd": 0.03}
    assert not (tmp_path / "auto-reservations" / "abcdefabcdef.json").exists()   # 저장한 뒤 지움
    assert svc._write_reservations("abcdefabcdef", {"r2": {"cost_usd": 0.01, "day": day}})   # 같은 번호가 다시 남아 있어도
    svc._load_with_reservations(run)
    assert budget["spent_calls"] == 4   # 두 번 세지 않는다


def test_legacy_spend_moves_to_its_start_day_before_new_daily_entries():
    budget = {k: v for k, v in auto.empty_budget("2026-10-10").items() if k != "by_day"}
    budget.update(spent_calls=5, spent_cost_usd=0.5, providers={})
    auto.settle(budget, "gpt", 0.1, "2026-10-13", estimated=False)
    assert budget["by_day"] == {"2026-10-10": {"calls": 5, "cost_usd": 0.5}, "2026-10-13": {"calls": 1, "cost_usd": 0.1}}
    assert auto.latest_day(budget) == "2026-10-13"   # 일일 한도 색인은 마지막으로 비용을 쓴 날


def test_a_job_running_past_midnight_still_counts_against_todays_limit():
    budget = {**auto.empty_budget("2026-10-12"), "authorized_calls": 10, "authorized_cost_usd": 1.0, "spent_calls": 6,
              "spent_cost_usd": 0.6, "by_day": {"2026-10-12": {"calls": 4, "cost_usd": 0.4}, "2026-10-13": {"calls": 2, "cost_usd": 0.2}}}
    assert auto.used_on(budget, "2026-10-13", active=True, started_today=False) == (6, pytest.approx(0.6))   # 오늘 2 + 남은 승인 4
    assert auto.used_on(budget, "2026-10-13", active=False, started_today=False) == (2, pytest.approx(0.2))
    legacy = {k: v for k, v in budget.items() if k != "by_day"}
    assert auto.used_on(legacy, "2026-10-13", active=False, started_today=False) == (0, 0.0)   # 예전 기록은 시작한 날에만


def test_collect_more_respects_the_concurrent_run_limit(tmp_path, monkeypatch):
    from webapp import comparison_service
    providers = Providers(fail={"claude": 500})
    svc, providers, _ = make(tmp_path, providers=providers, configure=("gpt", "claude"))
    run_id = upload(svc)["run"]["run_id"]
    wait(svc, run_id)
    monkeypatch.setattr(comparison_service, "MAX_RUNS", 0)
    with pytest.raises(comparison_service.JobCapacityError):
        svc.collect_more(run_id, {"providers": ["claude"]})


def test_retry_requests_only_failed_rows_and_reuses_collected_turns_as_history(tmp_path):
    providers = Providers(fail={"claude": 500})
    svc, providers, _ = make(tmp_path, providers=providers, configure=("gpt", "claude"))
    run_id = upload(svc)["run"]["run_id"]
    first = wait(svc, run_id)
    assert [row["collection"]["status"] for row in first["datasets"]["claude"]] == ["ERROR", "ERROR", "ERROR"]
    with pytest.raises(ValueError):
        svc.collect_more(run_id, {"providers": ["gpt"]})   # 다시 받을 답변이 없다
    providers.fail.clear()
    before = len(providers.of("gpt"))
    retried = wait(svc, svc.collect_more(run_id, {"providers": ["claude"]})["run"]["run_id"])
    # 처음엔 C1-1·S1 만 요청(C1-2 는 앞 턴 실패로 건너뜀), 재시도는 세 행 모두 — 성공한 GPT 는 다시 요청하지 않는다
    assert len(providers.of("gpt")) == before and len(providers.of("claude")) == 2 + 3
    assert all(row["collection"]["status"] == "SUCCESS" for row in retried["datasets"]["claude"])
    assert retried["auto_collection"]["budget"]["spent_calls"] == 3 + 2 + 3   # 실패한 요청도 호출 수에 남는다
    svc.evaluate(run_id, {})
    wait(svc, run_id)
    with pytest.raises(ValueError, match="평가를 시작한 실행"):
        svc.collect_more(run_id, {"providers": ["claude"]})


def test_a_provider_connected_later_is_added_only_on_request(tmp_path):
    svc, providers, _ = make(tmp_path, configure=("gpt",))
    run_id = upload(svc)["run"]["run_id"]
    wait(svc, run_id)
    svc.configure("gemini", {"key": "sk-test-not-real", "model": "gemini-fixture"})
    assert upload(svc)["reused"] is True and not providers.of("gemini")
    added = wait(svc, svc.collect_more(run_id, {"providers": ["gemini"]})["run"]["run_id"])
    assert added["providers"] == ["aiona", "gpt", "gemini"] and len(providers.of("gemini")) == 3
    assert added["auto_collection"]["providers"]["gemini"]["state"] == "completed"


def test_factchat_gateway_records_platform_and_real_model_and_is_not_ranked_with_direct_apis(tmp_path):
    svc, providers, _ = make(tmp_path, configure=("gpt", "factchat"))
    with pytest.raises(ValueError):
        svc.configure("factchat", {"key": "sk-fc-test", "model": "claude-sonnet-5", "base_url": "https://evil.example/v1"})
    result = wait(svc, upload(svc)["run"]["run_id"])
    url, body = next((u, b) for p, u, b in providers.requests if p == "factchat" and b["messages"][-1]["content"] == "단일 질문")
    assert url == "https://api.factchat.ai/v1/chat/completions" and body["model"] == "claude-sonnet-5" and body["max_tokens"] == 1024
    assert "tools" not in body and body["messages"] == [{"role": "user", "content": "단일 질문"}]
    entry = result["auto_collection"]["providers"]["factchat"]
    assert (entry["platform"], entry["model"], entry["vendor"], entry["access_mode"]) == ("FactChat API Gateway", "claude-sonnet-5", "anthropic", "api")
    assert entry["response_models"] == ["claude-sonnet-5-20261001"] and result["models"]["factchat"] == "FactChat · claude-sonnet-5"
    assert result["environment_comparison"]["same_conditions"] is False
    assert all(row["answer"].startswith("factchat 답변") for row in result["datasets"]["factchat"])


def test_collected_answers_go_through_the_existing_evaluation_and_restore_from_disk(tmp_path):
    svc, providers, judge = make(tmp_path)
    run_id = upload(svc)["run"]["run_id"]
    wait(svc, run_id)
    svc.evaluate(run_id, {})
    done = wait(svc, run_id)
    assert done["status"] == "COMPLETED" and set(done["evaluations"]) == {"aiona", "gpt", "claude", "gemini"}
    assert done["blind"]["enabled"] and done["evaluation_execution"]["pipeline"] in ("legacy", "items")
    assert all(case["case_status"] == "EVALUATED" for case in done["evaluations"]["gpt"]["cases"])
    assert judge.calls and "GPT" not in json.dumps([c["state"] for c in judge.calls], ensure_ascii=False)
    restored = ComparisonService(CodeEvalService(save_dir=tmp_path / "eval"), save_dir=tmp_path / "runs").result(run_id)
    assert restored["evaluations"] == done["evaluations"] and restored["auto_collection"] == done["auto_collection"]


def test_daily_index_includes_reservation_days_before_recovery(tmp_path):
    svc = ComparisonService(CodeEvalService(), save_dir=tmp_path)
    assert svc._write_reservations("abcdefabcdef", {"x1": {"cost_usd": 0.01, "day": "2026-10-13", "provider": "gpt"}})
    svc._ensure_auto_index()
    svc._remember_auto({"run_id": "abcdefabcdef", "auto_collection": {"job_key": "k", "budget": auto.empty_budget("2026-10-12")}})
    assert svc._auto_days["abcdefabcdef"] == "2026-10-13"   # 어제 시작했지만 오늘 보낸 요청이 예약 기록에만 있다
