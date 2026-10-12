"""Public-source retrieval is tested with fake DNS, sockets, HTTP, and fetchers."""
import copy
from concurrent.futures import CancelledError, ThreadPoolExecutor
from email.message import Message
import gzip
import io
import json
import socket
import threading
import time
import zlib

import pytest

from code_eval import semantic_sources as sources

URL = "https://official.example/facts"
STAMP = "2026-10-09T00:00:00+00:00"


def body(quotes=("A verified fact.",), urls=None, *, searched=True):
    urls = urls or [URL] * len(quotes)
    return {"model": "fixture", "verification": {"search_performed": searched, "source_urls": list(set(urls)),
            "source_validation": "invented", "source_checks": [{"status": "invented"}]},
            "answers": {"J1": {"choice": "PASS"}, "J5": {"choice": "PASS", "fact_check": {"basis": "web", "claims": [
                {"claim": "Original answer", "verdict": "PASS", "sources": [{"url": url, "title": "A title", "quote": text,
                 "quote_verified": True, "verification_reason": "model invented this", "verified_at": "invented", "resolved_url": "file:///secret"}]}
                for text, url in zip(quotes, urls)]}}}}


def fetched(text="A verified fact.", *, status="ok", url=URL):
    return {"status": status, "text": text, "final_url": url, "checked_at": STAMP, "content_type": "text/plain"}


def all_sources(value):
    return [s for c in value["answers"]["J5"]["fact_check"]["claims"] for s in c["sources"]]


def ipv4(ip="93.184.216.34", port=443):
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))


def test_quotes_are_verified_per_claim_with_one_fetch_and_originals_unchanged():
    original = body(("A verified fact.", "A false quote", "Cafe\u0301\u00a0\n한글"))
    snapshot = copy.deepcopy(original)
    calls = []
    def fetch(url):
        calls.append(url)
        return fetched("A verified fact. Café 한글. PRIVATE PAGE TEXT NOT RETAINED")
    result = sources.verify_web_sources(original, fetcher=fetch)
    assert original == snapshot and result is not original
    assert calls == [URL]
    assert [s["quote_verified"] for s in all_sources(result)] == [True, False, True]
    assert all_sources(result)[1]["verified_at"] is None
    assert all_sources(result)[0]["verified_at"] == STAMP
    assert all_sources(result)[0]["resolved_url"] == URL
    assert "PRIVATE PAGE TEXT NOT RETAINED" not in json.dumps(result)
    assert result["answers"]["J5"]["choice"] == "PASS"  # Finalizer owns verdict changes.
    check = result["verification"]["source_checks"][0]
    assert check["quote_count"] == 3 and check["verified_quotes"] == 2 and check["unverified_quotes"] == 1
    assert result["verification"]["source_validation"] == "provider_and_content"


@pytest.mark.parametrize("source_text,quote", [
    ("The customer\u2019s bank.", "The customer's bank."),
    ("The customer's bank.", "The customer\u2019s bank."),
    ("It says \u2018pending\u2019.", "It says 'pending'."),
    ('It says \u201capproved\u201d.', 'It says "approved".'),
    ('It says "approved".', 'It says \u201capproved\u201d.'),
    ("The customer&rsquo;s bank.", "The customer's bank."),
])
def test_typographic_quote_style_matches_without_changing_saved_quotes(source_text, quote):
    original = body((quote,))
    snapshot = copy.deepcopy(original)
    result = sources.verify_web_sources(original, fetcher=lambda _: fetched(source_text))
    assert all_sources(result)[0]["quote_verified"] is True
    assert all_sources(result)[0]["quote"] == quote
    assert original == snapshot


@pytest.mark.parametrize("source_text,quote", [
    ("The customer\u2019s bank does not refund it.", "The customer's bank does refund it."),
    ("The customer\u2019s fee is 3.5%.", "The customer's fee is 35%."),
    ("It says \u201cdenied\u201d.", 'It says "approved".'),
    ("It says \u2018pending\u2019.", "It says pending."),
    ("The customer\u2019s bank.", "The customers bank."),
    ("The length is 5\u2032.", "The length is 5'."),
    ("The length is 5\u2033.", 'The length is 5".'),
    ("The customer\u02bcs bank.", "The customer's bank."),
    ("It says `pending`.", "It says 'pending'."),
    ("It says \u201epending\u201c.", 'It says "pending".'),
])
def test_typographic_quote_normalization_does_not_erase_substantive_differences(source_text, quote):
    result = sources.verify_web_sources(body((quote,)), fetcher=lambda _: fetched(source_text))
    assert all_sources(result)[0]["quote_verified"] is False
    assert all_sources(result)[0]["verification_reason"] == sources._REASONS["quote_not_found"]


@pytest.mark.parametrize("original", [None, {}, {"answers": {"J5": {"fact_check": {"basis": "reference"}}}}, body()])
def test_pre_cancelled_verification_raises_without_fetching_or_returning_body(original):
    cancel = threading.Event()
    cancel.set()
    snapshot = copy.deepcopy(original)
    with pytest.raises(CancelledError):
        sources.verify_web_sources(original, fetcher=lambda _: pytest.fail("cancelled work must not fetch"), cancel_event=cancel)
    assert original == snapshot


@pytest.mark.parametrize("raises_fetch_error", [False, True])
def test_cancellation_during_fetch_discards_partial_work_and_never_opens_next_url(raises_fetch_error):
    original = body(("A verified fact.", "Another fact."), [URL, URL + "2"])
    snapshot = copy.deepcopy(original)
    cancel, calls = threading.Event(), []
    def fetch(url):
        calls.append(url)
        cancel.set()
        if raises_fetch_error:
            raise OSError("request interrupted")
        return fetched()
    with pytest.raises(CancelledError):
        sources.verify_web_sources(original, fetcher=fetch, cancel_event=cancel)
    assert calls == [URL]
    assert original == snapshot


def test_fetcher_cancellation_is_not_converted_to_an_unverified_result():
    def fetch(url):
        raise CancelledError("caller cancelled")
    with pytest.raises(CancelledError, match="caller cancelled"):
        sources.verify_web_sources(body(), fetcher=fetch)


@pytest.mark.parametrize("quote_count", [1, 2])
def test_cancellation_after_quote_comparison_never_returns_partially_verified_body(monkeypatch, quote_count):
    original = body(["A verified fact."] * quote_count)
    snapshot = copy.deepcopy(original)
    cancel = threading.Event()
    normalize = sources._normalize
    def cancelling_normalize(text):
        result = normalize(text)
        if text == "A verified fact. PAGE END":
            cancel.set()
        return result
    monkeypatch.setattr(sources, "_normalize", cancelling_normalize)
    with pytest.raises(CancelledError):
        sources.verify_web_sources(original, fetcher=lambda _: fetched("A verified fact. PAGE END"), cancel_event=cancel)
    assert original == snapshot


def test_non_web_early_return_also_observes_cancellation(monkeypatch):
    cancel = threading.Event()
    deepcopy = sources.copy.deepcopy
    def cancel_during_copy(value):
        result = deepcopy(value)
        cancel.set()
        return result
    monkeypatch.setattr(sources.copy, "deepcopy", cancel_during_copy)
    with pytest.raises(CancelledError):
        sources.verify_web_sources({"answers": {}}, cancel_event=cancel)


@pytest.fixture
def public_text_cache(monkeypatch):
    """Exercise the real default fetch path with fake public DNS/HTTP only."""
    cache = sources._SourceTextCache()
    monkeypatch.setattr(sources, "_SOURCE_TEXT_CACHE", cache)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [ipv4(port=port)])
    return cache


def test_public_page_is_reused_but_each_quote_and_original_text_are_checked(monkeypatch, public_text_cache):
    requests = []
    def request(target, *_):
        requests.append(target[0])
        return {"text": "A verified fact. FULL PAGE MUST NOT BE SAVED", "content_type": "text/plain"}
    monkeypatch.setattr(sources, "_request", request)
    monkeypatch.setattr(sources, "_now", lambda: STAMP)
    first = sources.verify_web_sources(body())
    monkeypatch.setattr(sources, "_now", lambda: "2026-10-09T00:03:00+00:00")
    original = body(("A false quote.", "A verified fact."))
    untouched = copy.deepcopy(original)
    second = sources.verify_web_sources(original)
    assert requests == [URL]
    assert original == untouched
    assert all_sources(first)[0]["quote_verified"]
    assert [s["quote_verified"] for s in all_sources(second)] == [False, True]
    assert all_sources(second)[1]["verified_at"] == STAMP
    assert second["verification"]["source_checks"][0]["checked_at"] == STAMP
    assert "FULL PAGE MUST NOT BE SAVED" not in json.dumps(first) + json.dumps(second)


@pytest.mark.parametrize("searched,observed", [(False, True), (True, False)])
def test_cached_page_never_bypasses_this_calls_search_proof(monkeypatch, public_text_cache, searched, observed):
    monkeypatch.setattr(sources, "_request", lambda *a: {"text": "A verified fact.", "content_type": "text/plain"})
    assert all_sources(sources.verify_web_sources(body()))[0]["quote_verified"]
    original = body(searched=searched)
    if not observed:
        original["verification"]["source_urls"] = []
    monkeypatch.setattr(public_text_cache, "fetch", lambda *a, **k: pytest.fail("authorization must precede cache lookup"))
    result = sources.verify_web_sources(original)
    assert not all_sources(result)[0]["quote_verified"]
    assert result["verification"]["source_checks"] == []


def test_injected_fetchers_never_read_or_fill_the_shared_cache(monkeypatch, public_text_cache):
    requests, injected = [], []
    def request(target, *_):
        requests.append(target[0])
        return {"text": "A verified fact.", "content_type": "text/plain"}
    monkeypatch.setattr(sources, "_request", request)
    assert all_sources(sources.verify_web_sources(body()))[0]["quote_verified"]
    def different(url):
        injected.append(url)
        return fetched("A changed fact.")
    for _ in range(2):
        assert not all_sources(sources.verify_web_sources(body(), fetcher=different))[0]["quote_verified"]
    assert all_sources(sources.verify_web_sources(body()))[0]["quote_verified"]
    assert requests == [URL] and injected == [URL, URL]
    assert all_sources(sources.verify_web_sources(body(), fetcher=lambda _: fetched()))[0]["quote_verified"]


def test_page_expires_at_five_minutes_without_extending_ttl_on_hits(monkeypatch, public_text_cache):
    clock, requests = [0.0], []
    public_text_cache._clock = lambda: clock[0]
    def request(target, *_):
        requests.append(target[0])
        text = "A verified fact." if len(requests) == 1 else "The source has changed."
        return {"text": text, "content_type": "text/plain"}
    monkeypatch.setattr(sources, "_request", request)
    assert all_sources(sources.verify_web_sources(body()))[0]["quote_verified"]
    clock[0] = 299.999
    assert all_sources(sources.verify_web_sources(body()))[0]["quote_verified"]
    clock[0] = 300.0
    assert not all_sources(sources.verify_web_sources(body()))[0]["quote_verified"]
    assert requests == [URL, URL]


def test_expired_public_page_rechecks_dns_and_never_returns_stale_on_failure(monkeypatch, public_text_cache):
    clock, requests = [0.0], []
    public_text_cache._clock = lambda: clock[0]
    def request(target, *_):
        requests.append(target[0])
        return {"text": "A verified fact.", "content_type": "text/plain"}
    monkeypatch.setattr(sources, "_request", request)
    assert all_sources(sources.verify_web_sources(body()))[0]["quote_verified"]
    clock[0] = 300.0
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [ipv4("127.0.0.1")])
    refused = sources.verify_web_sources(body())
    assert not all_sources(refused)[0]["quote_verified"]
    assert refused["verification"]["source_checks"][0]["status"] == "blocked_address"
    assert requests == [URL]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [ipv4()])
    assert all_sources(sources.verify_web_sources(body()))[0]["quote_verified"]
    assert requests == [URL, URL]


@pytest.mark.parametrize("status", ["http_error", "timeout", "decode_error", "redirect_host_changed"])
def test_failed_download_is_retried_by_a_later_evaluation(monkeypatch, public_text_cache, status):
    requests = []
    def request(target, *_):
        requests.append(target[0])
        if len(requests) == 1:
            raise sources.SourceError(status)
        return {"text": "A verified fact.", "content_type": "text/plain"}
    monkeypatch.setattr(sources, "_request", request)
    assert not all_sources(sources.verify_web_sources(body()))[0]["quote_verified"]
    assert all_sources(sources.verify_web_sources(body()))[0]["quote_verified"]
    assert all_sources(sources.verify_web_sources(body()))[0]["quote_verified"]
    assert requests == [URL, URL]


def test_cache_hit_still_requires_valid_url_and_available_deadline(monkeypatch, public_text_cache):
    requests = []
    def request(target, *_):
        requests.append(target[0])
        return {"text": "A verified fact.", "content_type": "text/plain"}
    monkeypatch.setattr(sources, "_request", request)
    assert all_sources(sources.verify_web_sources(body()))[0]["quote_verified"]
    assert public_text_cache.fetch(URL, deadline=time.monotonic() - 1)["status"] == "timeout"
    assert public_text_cache.fetch("https://official.example:8080/facts", deadline=time.monotonic() + 1)["status"] == "blocked_port"
    assert requests == [URL]


def test_cache_evicts_least_recent_page_at_url_limit(monkeypatch, public_text_cache):
    public_text_cache.max_urls = 2
    requests = []
    def request(target, *_):
        requests.append(target[0])
        return {"text": "A verified fact.", "content_type": "text/plain"}
    monkeypatch.setattr(sources, "_request", request)
    def check(url):
        assert all_sources(sources.verify_web_sources(body(urls=[url])))[0]["quote_verified"]
    for url in (URL, URL + "2", URL, URL + "3", URL, URL + "2"):
        check(url)
    assert requests == [URL, URL + "2", URL + "3", URL + "2"]
    assert len(public_text_cache._entries) == 2


def test_memory_limit_evicts_pages_and_does_not_cache_oversize_text(monkeypatch, public_text_cache):
    public_text_cache.max_bytes = 2000
    requests = []
    def request(target, *_):
        requests.append(target[0])
        text = "A verified fact." + "x" * (4000 if target[0].endswith("huge") else 600)
        return {"text": text, "content_type": "text/plain"}
    monkeypatch.setattr(sources, "_request", request)
    for url in (URL, URL + "2", URL, URL + "huge", URL + "huge"):
        assert all_sources(sources.verify_web_sources(body(urls=[url])))[0]["quote_verified"]
        assert public_text_cache._bytes <= public_text_cache.max_bytes
    assert requests == [URL, URL + "2", URL, URL + "huge", URL + "huge"]


def test_returned_fetch_metadata_cannot_poison_the_next_cache_hit(monkeypatch, public_text_cache):
    monkeypatch.setattr(sources, "_request", lambda *a: {"text": "A verified fact.", "content_type": "text/plain"})
    first = public_text_cache.fetch(URL, deadline=time.monotonic() + 1)
    first.update(text="An invented quote.", final_url="http://127.0.0.1/", checked_at="invented")
    second = public_text_cache.fetch(URL, deadline=time.monotonic() + 1)
    assert second["text"] == "A verified fact." and second["final_url"] == URL
    assert second["checked_at"] != "invented"


def test_concurrent_models_share_one_download_and_keep_distinct_quote_results(monkeypatch, public_text_cache):
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    requests = []
    def request(target, *_):
        requests.append(target[0])
        entered.set()
        assert release.wait(2)
        return {"text": "A verified fact.", "content_type": "text/plain"}
    monkeypatch.setattr(sources, "_request", request)
    with ThreadPoolExecutor(max_workers=4) as pool:
        leader = pool.submit(sources.verify_web_sources, body())
        try:
            assert entered.wait(1)
            ready = public_text_cache._flights[URL].ready
            real_wait, wait_count, count_lock = ready.wait, [0], threading.Lock()
            def wait(timeout=None):
                with count_lock:
                    wait_count[0] += 1
                    if wait_count[0] == 3:
                        waiting.set()
                return real_wait(timeout)
            monkeypatch.setattr(ready, "wait", wait)
            followers = [pool.submit(sources.verify_web_sources, body((quote,)))
                         for quote in ("A false quote.", "A verified fact.", "A false quote.")]
            assert waiting.wait(1)
        finally:
            release.set()
        outcomes = [leader.result(timeout=2), *(f.result(timeout=2) for f in followers)]
    assert requests == [URL]
    assert [all_sources(value)[0]["quote_verified"] for value in outcomes] == [True, False, True, False]


def test_cancelled_caller_does_not_abort_shared_fetch_or_return_forged_fields(monkeypatch, public_text_cache):
    entered, release, waiting, cancel = (threading.Event() for _ in range(4))
    requests = []
    original = body(("A verified fact.", "Another fact."), [URL, URL + "2"])
    snapshot = copy.deepcopy(original)
    def request(target, *_):
        requests.append(target[0])
        entered.set()
        assert release.wait(2)
        return {"text": "A verified fact.", "content_type": "text/plain"}
    monkeypatch.setattr(sources, "_request", request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelled_leader = pool.submit(sources.verify_web_sources, original, cancel_event=cancel)
        try:
            assert entered.wait(1)
            ready = public_text_cache._flights[URL].ready
            real_wait = ready.wait
            def wait(timeout=None):
                waiting.set()
                return real_wait(timeout)
            monkeypatch.setattr(ready, "wait", wait)
            follower = pool.submit(sources.verify_web_sources, body())
            assert waiting.wait(1)
            cancel.set()
        finally:
            release.set()
        with pytest.raises(CancelledError):
            cancelled_leader.result(timeout=2)
        assert all_sources(follower.result(timeout=2))[0]["quote_verified"] is True
    assert requests == [URL]
    assert original == snapshot
    assert not public_text_cache._flights
    assert all_sources(sources.verify_web_sources(body()))[0]["quote_verified"] is True
    assert requests == [URL]


def test_waiter_deadline_does_not_cancel_the_shared_download(monkeypatch, public_text_cache):
    entered, release = threading.Event(), threading.Event()
    requests = []
    def request(target, *_):
        requests.append(target[0])
        entered.set()
        assert release.wait(2)
        return {"text": "A verified fact.", "content_type": "text/plain"}
    monkeypatch.setattr(sources, "_request", request)
    with ThreadPoolExecutor(max_workers=1) as pool:
        leader = pool.submit(public_text_cache.fetch, URL, deadline=time.monotonic() + 2)
        try:
            assert entered.wait(1)
            assert public_text_cache.fetch(URL, deadline=time.monotonic() + .02)["status"] == "timeout"
        finally:
            release.set()
        assert leader.result(timeout=2)["status"] == "ok"
    assert public_text_cache.fetch(URL, deadline=time.monotonic() + 1)["status"] == "ok"
    assert requests == [URL]


def test_inflight_limit_wait_is_bounded_and_does_not_start_extra_fetch(monkeypatch, public_text_cache):
    public_text_cache.max_urls = 1
    entered, release = threading.Event(), threading.Event()
    requests = []
    def request(target, *_):
        requests.append(target[0])
        entered.set()
        assert release.wait(2)
        return {"text": "A verified fact.", "content_type": "text/plain"}
    monkeypatch.setattr(sources, "_request", request)
    with ThreadPoolExecutor(max_workers=1) as pool:
        leader = pool.submit(public_text_cache.fetch, URL, deadline=time.monotonic() + 2)
        try:
            assert entered.wait(1)
            assert public_text_cache.fetch(URL + "2", deadline=time.monotonic() + .02)["status"] == "timeout"
        finally:
            release.set()
        assert leader.result(timeout=2)["status"] == "ok"
    assert requests == [URL]
    assert not public_text_cache._flights


def test_concurrent_failed_fetch_releases_waiters_and_next_call_can_retry(monkeypatch, public_text_cache):
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    requests = []
    def request(target, *_):
        requests.append(target[0])
        if len(requests) == 1:
            entered.set()
            assert release.wait(2)
            raise sources.SourceError("http_error")
        return {"text": "A verified fact.", "content_type": "text/plain"}
    monkeypatch.setattr(sources, "_request", request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(sources.verify_web_sources, body())
        try:
            assert entered.wait(1)
            ready = public_text_cache._flights[URL].ready
            real_wait = ready.wait
            def wait(timeout=None):
                waiting.set()
                return real_wait(timeout)
            monkeypatch.setattr(ready, "wait", wait)
            follower = pool.submit(sources.verify_web_sources, body())
            assert waiting.wait(1)
        finally:
            release.set()
        assert not all_sources(leader.result(timeout=2))[0]["quote_verified"]
        assert not all_sources(follower.result(timeout=2))[0]["quote_verified"]
    assert requests == [URL]
    assert all_sources(sources.verify_web_sources(body()))[0]["quote_verified"]
    assert requests == [URL, URL]


@pytest.mark.parametrize("searched", [False, None, "true", 1])
def test_search_proof_is_required_and_model_verified_flags_are_overwritten(searched):
    original = body(searched=searched)
    result = sources.verify_web_sources(original, fetcher=lambda _: pytest.fail("must not fetch"))
    assert all_sources(result)[0]["quote_verified"] is False
    assert all_sources(result)[0]["verified_at"] is None
    assert result["verification"]["source_checks"] == []


def test_unobserved_urls_are_never_requested_even_with_valid_model_quote():
    original = body()
    original["verification"]["source_urls"] = ["https://different.example/"]
    result = sources.verify_web_sources(original, fetcher=lambda _: pytest.fail("must not fetch"))
    assert not all_sources(result)[0]["quote_verified"]


def test_fragment_identity_and_quote_escaping():
    original = body(("A &amp; B",), [URL + "#section"])
    original["verification"]["source_urls"] = [URL]
    requested = []
    def fetch(url):
        requested.append(url)
        return fetched("A & B")
    assert all_sources(sources.verify_web_sources(original, fetcher=fetch))[0]["quote_verified"]
    assert requested == [URL]


@pytest.mark.parametrize("status", ["timeout", "http_error", "unsupported_content_type", "response_too_large", "blocked_address"])
def test_failed_fetch_cannot_validate_a_quote_even_if_fetcher_has_partial_text(status):
    result = sources.verify_web_sources(body(), fetcher=lambda _: fetched(status=status))
    assert not all_sources(result)[0]["quote_verified"]
    assert result["verification"]["source_checks"][0]["status"] == status


@pytest.mark.parametrize("status,fragment", [
    ("unsupported_content_encoding", "압축 형식"), ("unsupported_content_type", "HTML"),
    ("http_error", "정상 응답"), ("response_too_large", "수집 한도"),
    ("decode_error", "문자 인코딩"), ("fetch_error", "연결 또는 본문 수신"),
    ("incomplete_compressed_body", "압축 본문이 완전하지"),
    ("invalid_compressed_body", "손상"), ("trailing_compressed_data", "추가 스트림"),
])
def test_failed_source_explains_the_specific_collection_problem(status, fragment):
    result = sources.verify_web_sources(body(), fetcher=lambda _: fetched(status=status))
    evidence = all_sources(result)[0]
    assert not evidence["quote_verified"] and fragment in evidence["verification_reason"]


def test_empty_quote_and_reference_basis_do_not_fetch():
    assert not all_sources(sources.verify_web_sources(body((" \n ",)), fetcher=lambda _: pytest.fail("must not fetch")))[0]["quote_verified"]
    original = body()
    original["answers"]["J5"]["fact_check"]["basis"] = "reference"
    result = sources.verify_web_sources(original, fetcher=lambda _: pytest.fail("must not fetch"))
    assert result == original and result is not original


def test_url_count_cap_and_fetch_exception():
    urls = [f"https://official.example/{i}" for i in range(10)]
    calls = []
    def fetch(url):
        calls.append(url)
        raise RuntimeError("sensitive exception must not be recorded")
    result = sources.verify_web_sources(body(["fact"] * 10, urls), fetcher=fetch)
    assert len(calls) == sources.MAX_SOURCE_URLS
    assert all(not s["quote_verified"] for s in all_sources(result))
    assert "sensitive exception" not in json.dumps(result)
    assert result["verification"]["source_checks"][-1]["status"] == "source_limit"


def test_total_deadline_prevents_later_fetches(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(sources.time, "monotonic", lambda: clock[0])
    calls = []
    def fetch(url):
        calls.append(url)
        clock[0] += sources.TOTAL_TIMEOUT + 1
        return fetched(url=url)
    result = sources.verify_web_sources(body(["fact", "fact"], [URL, URL + "2"]), fetcher=fetch)
    assert len(calls) == 1
    assert result["verification"]["source_checks"][-1]["status"] == "timeout"


@pytest.mark.parametrize("url", ["file:///secret", "ftp://example.org/a", "http://user:pass@example.org/", "https://@example.org/",
    "https://example.org:80/", "http://example.org:443/", "http://example.org:8080/", "http://example.org:0/",
    "https://example.org/a\r\nInjected:yes", "https://example.org/\x7f", "https://example.org\\@127.0.0.1/"])
def test_rejected_urls_never_reach_dns(monkeypatch, url):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("DNS must not be consulted"))
    assert sources.fetch_public_source(url)["status"] in {"blocked_url", "blocked_port"}


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.100.100.200",
    "0.0.0.0", "224.0.0.1", "255.255.255.255", "168.63.129.16", "::1", "fe80::1", "fc00::1", "ff02::1",
    "::ffff:127.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::"])
def test_nonpublic_and_translation_addresses_are_rejected(ip):
    assert not sources._public_address(ip)


def test_all_dns_results_must_be_public_not_only_the_first(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [ipv4(), ipv4("127.0.0.1")])
    monkeypatch.setattr(sources, "_request", lambda *a: pytest.fail("must not connect"))
    assert sources.fetch_public_source(URL)["status"] == "blocked_address"


def test_dns_timeout_is_bounded(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(sources, "DNS_TIMEOUT", 0.01)
    def lookup(*args, **kwargs):
        release.wait(1)
        return [ipv4()]
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    try:
        assert sources.fetch_public_source(URL)["status"] == "dns_timeout"
    finally:
        release.set()


def test_fifth_dns_request_waits_for_a_slot_instead_of_immediate_unverified(monkeypatch):
    slots, waiting = threading.BoundedSemaphore(4), threading.Event()
    for _ in range(4):
        assert slots.acquire(blocking=False)
    class ObservedSlots:
        def acquire(self, **kwargs):
            assert kwargs.get("timeout", 0) > 0
            waiting.set()
            return slots.acquire(**kwargs)
        def release(self):
            slots.release()
    monkeypatch.setattr(sources, "_DNS_SLOTS", ObservedSlots())
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [ipv4()])
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(sources._resolve_public, "official.example", 443, time.monotonic() + 2)
        try:
            assert waiting.wait(1)
            assert not request.done()
        finally:
            slots.release()
        assert request.result(timeout=2) == [(socket.AF_INET, socket.IPPROTO_TCP, ("93.184.216.34", 443))]
    for _ in range(3):
        slots.release()
    # The DNS worker must have returned the fourth acquired slot too.
    assert all(slots.acquire(blocking=False) for _ in range(4))
    assert not slots.acquire(blocking=False)


@pytest.mark.parametrize("remaining,expected", [(1, "dns_busy"), (.01, "timeout")])
def test_dns_slot_wait_respects_dns_and_total_deadlines(monkeypatch, remaining, expected):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(sources, "_DNS_SLOTS", slots)
    monkeypatch.setattr(sources, "DNS_TIMEOUT", .02)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("no DNS without a slot"))
    started = time.monotonic()
    result = sources.fetch_public_source(URL, deadline=started + remaining)
    assert result["status"] == expected
    assert time.monotonic() - started < .5


def test_dns_slot_wait_spends_the_existing_dns_budget(monkeypatch):
    clock, looked_up = [100.0], []
    class SlowSlots:
        releases = 0
        def acquire(self, *, timeout):
            assert timeout == 3.0
            clock[0] += timeout
            return True
        def release(self):
            self.releases += 1
    slots = SlowSlots()
    monkeypatch.setattr(sources, "_DNS_SLOTS", slots)
    monkeypatch.setattr(sources.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: looked_up.append(True))
    result = sources.fetch_public_source(URL)
    assert result["status"] == "timeout"
    assert not looked_up and slots.releases == 1


def test_expired_deadline_never_starts_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("deadline already expired"))
    assert sources.fetch_public_source(URL, deadline=time.monotonic() - 1)["status"] == "timeout"


def test_ipv6_resolver_scope_and_port_cannot_change_the_pinned_destination(monkeypatch):
    entry = (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2606:4700:4700::1111", 443, 0, 7))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [entry])
    monkeypatch.setattr(sources, "_request", lambda *a: pytest.fail("must not connect"))
    assert sources.fetch_public_source(URL)["status"] == "blocked_address"


def test_redirect_to_another_host_is_blocked_before_any_destination_lookup(monkeypatch):
    hosts, requests = [], []
    def lookup(hostname, port, **kwargs):
        hosts.append(hostname)
        return [ipv4("127.0.0.1" if hostname == "private.example" else "93.184.216.34", port)]
    def request(target, address, deadline):
        requests.append(target[0])
        return {"redirect": "https://private.example/metadata"}
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    monkeypatch.setattr(sources, "_request", request)
    result = sources.fetch_public_source(URL)
    assert result["status"] == "redirect_host_changed" and result["text"] == ""
    assert hosts == ["official.example"] and requests == [URL]


@pytest.mark.parametrize("destination", ["https://other.example/facts", "https://www.other.example/facts",
                                         "https://docs.official.example/facts"])
def test_redirected_publisher_cannot_inherit_authority_even_from_injected_fetcher(destination):
    result = sources.verify_web_sources(body(), fetcher=lambda _: fetched(url=destination))
    assert not all_sources(result)[0]["quote_verified"]
    assert result["verification"]["source_checks"][0]["status"] == "redirect_host_changed"


def test_www_and_bare_domain_are_the_same_publisher(monkeypatch):
    # F5: 'www.' 만 다른 이동은 같은 발행처다. 단계마다 DNS·공개 주소 검사는 그대로 한다.
    result = sources.verify_web_sources(body(), fetcher=lambda _: fetched(url="https://www.official.example/facts"))
    assert all_sources(result)[0]["quote_verified"] is True
    lookups = []
    def lookup(hostname, port, **kwargs):
        lookups.append(hostname)
        return [ipv4("127.0.0.1" if hostname == "www.private.example" else "93.184.216.34", port)]
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    hops = {"https://www.official.example/facts": {"redirect": "https://official.example/facts"},
            "https://official.example/facts": {"text": "A verified fact.", "content_type": "text/plain"},
            "https://private.example/x": {"redirect": "https://www.private.example/x"}}
    monkeypatch.setattr(sources, "_request", lambda target, *_: hops[target[0]])
    assert sources.fetch_public_source("https://www.official.example/facts")["final_url"] == URL
    assert sources.fetch_public_source("https://private.example/x")["status"] == "blocked_address"
    assert lookups == ["www.official.example", "official.example", "private.example", "www.private.example"]


def test_access_failure_cause_is_recorded_without_changing_the_status():
    pages = {URL: {"status": "http_error", "text": "", "detail": "http_403"},
             "https://two.example/a": {"status": "fetch_error", "text": "", "detail": "<b>raw error</b>"}}
    result = sources.verify_web_sources(body(("A verified fact.",) * 2, [URL, "https://two.example/a"]),
                                        fetcher=lambda url: pages[url])
    checks = {c["url"]: c for c in result["verification"]["source_checks"]}
    assert checks[URL]["status"] == "http_error" and checks[URL]["detail"] == "http_403"
    assert "detail" not in checks["https://two.example/a"]   # only fetcher-defined diagnostic values


def test_same_hostname_http_to_https_redirect_is_allowed(monkeypatch):
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **k: [ipv4(port=port)])
    def request(target, *_):
        calls.append((target[1], target[2]))
        return {"redirect": URL} if not target[5] else {"text": "A verified fact.", "content_type": "text/plain"}
    monkeypatch.setattr(sources, "_request", request)
    result = sources.fetch_public_source("http://official.example/facts")
    assert result["status"] == "ok" and result["final_url"] == URL
    assert calls == [("official.example", 80), ("official.example", 443)]


def test_redirect_rechecks_even_same_hostname_dns_rebinding(monkeypatch):
    lookups = []
    def lookup(hostname, port, **kwargs):
        lookups.append(hostname)
        return [ipv4("93.184.216.34" if len(lookups) == 1 else "127.0.0.1", port)]
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    monkeypatch.setattr(sources, "_request", lambda *a: {"redirect": "/different"})
    assert sources.fetch_public_source(URL)["status"] == "blocked_address"
    assert len(lookups) == 2


def test_redirect_limit_and_relative_success(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **k: [ipv4(port=port)])
    calls = []
    def redirect(*args):
        calls.append(args)
        return {"redirect": "/next"}
    monkeypatch.setattr(sources, "_request", redirect)
    assert sources.fetch_public_source(URL)["status"] == "redirect_limit"
    assert len(calls) == sources.MAX_REDIRECTS + 1
    monkeypatch.setattr(sources, "_request", lambda target, *_: {"redirect": "/next"} if target[0] == URL else {"text": "Source", "content_type": "text/plain"})
    result = sources.fetch_public_source(URL)
    assert result["status"] == "ok" and result["final_url"] == "https://official.example/next"


def test_connection_pins_ip_and_preserves_hostname_for_tls(monkeypatch):
    calls = []
    class FakeSocket:
        def settimeout(self, value):
            calls.append(("timeout", value))
        def connect(self, address):
            calls.append(("connect", address))
        def do_handshake(self):
            calls.append(("handshake",))
        def close(self):
            calls.append(("close",))
    sock = FakeSocket()
    monkeypatch.setattr(socket, "socket", lambda *a: sock)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("must not resolve again"))
    class Context:
        def wrap_socket(self, original, **kwargs):
            assert original is sock
            calls.append(("tls", kwargs))
            return original
    monkeypatch.setattr(sources.ssl, "create_default_context", lambda: Context())
    pinned = (socket.AF_INET, socket.IPPROTO_TCP, ("93.184.216.34", 443))
    conn = sources._PinnedConnection("official.example", 443, pinned, True, time.monotonic() + 3)
    conn.connect()
    assert ("connect", ("93.184.216.34", 443)) in calls
    assert ("tls", {"server_hostname": "official.example", "do_handshake_on_connect": False}) in calls
    assert ("handshake",) in calls
    conn.close()


class Response:
    def __init__(self, data=b"A verified fact.", status=200, content_type="text/plain; charset=utf-8", **headers):
        self.data, self.status, self.headers = data, status, Message()
        self.closed = False
        self.headers["Content-Type"] = content_type
        for name, value in headers.items():
            self.headers[name.replace("_", "-")] = str(value)
    def getheader(self, name):
        return self.headers.get(name)
    def read(self, size):
        value, self.data = self.data[:size], self.data[size:]
        return value
    def close(self):
        self.closed = True


def fake_http(monkeypatch, response):
    calls = []
    class Connection:
        def __init__(self, *args):
            calls.append(("construct", args))
        def request(self, *args, **kwargs):
            calls.append(("request", args, kwargs))
        def getresponse(self):
            return response
        def abort(self):
            calls.append(("abort",))
        def close(self):
            calls.append(("close",))
    monkeypatch.setattr(sources, "_PinnedConnection", Connection)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **k: [ipv4(port=port)])
    return calls


def test_http_headers_have_original_host_and_no_auth_cookies_or_proxy(monkeypatch):
    response = Response()
    calls = fake_http(monkeypatch, response)
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@127.0.0.1:9999")
    result = sources.fetch_public_source(URL)
    assert result["status"] == "ok"
    request = next(c for c in calls if c[0] == "request")
    assert request[1] == ("GET", "/facts")
    assert request[2]["headers"]["Host"] == "official.example"
    assert not {"Authorization", "Cookie", "Proxy-Authorization"} & request[2]["headers"].keys()
    assert response.closed


@pytest.mark.parametrize("response,status", [
    (Response(status=403), "http_error"), (Response(content_type="application/pdf"), "unsupported_content_type"),
    (Response(Content_Encoding="br"), "unsupported_content_encoding"),
    (Response(Content_Length=999999999), "response_too_large"),
    (Response(data=b"\xff", content_type="text/plain; charset=utf-8"), "decode_error"),
    (Response(content_type="text/plain; charset=unknown"), "unsupported_encoding")])
def test_blocked_or_unsupported_responses_return_no_text(monkeypatch, response, status):
    fake_http(monkeypatch, response)
    result = sources.fetch_public_source(URL)
    assert result["status"] == status and result["text"] == ""
    assert response.closed


def test_streamed_oversize_returns_no_truncated_text(monkeypatch):
    monkeypatch.setattr(sources, "MAX_RESPONSE_BYTES", 16)
    fake_http(monkeypatch, Response(data=b"A verified fact. TOO LONG"))
    result = sources.fetch_public_source(URL)
    assert result["status"] == "response_too_large" and result["text"] == ""


@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
def test_supported_compression_decodes_complete_unicode_text(monkeypatch, encoding, compress):
    text = "A verified fact. Café 한글\n" * 20
    data = compress(text.encode())
    class ChunkedResponse(Response):
        def read(self, size):
            return super().read(min(size, 3))
    response = ChunkedResponse(data=data, Content_Encoding=encoding, Content_Length=len(data))
    fake_http(monkeypatch, response)
    result = sources.fetch_public_source(URL)
    assert result["status"] == "ok" and result["text"] == text
    assert response.closed


@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
def test_compressed_wire_limit_is_independent_from_decoded_limit(monkeypatch, encoding, compress):
    data = compress(b"Short text")
    monkeypatch.setattr(sources, "MAX_COMPRESSED_BYTES", len(data) - 1)
    monkeypatch.setattr(sources, "MAX_RESPONSE_BYTES", 100)
    response = Response(data=data, Content_Encoding=encoding)
    fake_http(monkeypatch, response)
    result = sources.fetch_public_source(URL)
    assert result["status"] == "response_too_large" and result["text"] == "" and response.closed


@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
def test_compression_bomb_is_stopped_by_decompressor_output_cap(monkeypatch, encoding, compress):
    data = compress(b"A" * 1_000_000)
    monkeypatch.setattr(sources, "MAX_RESPONSE_BYTES", 64)
    real_decoder, limits = zlib.decompressobj, []
    class LimitedDecoder:
        def __init__(self, *args):
            self.decoder = real_decoder(*args)
        def decompress(self, data, max_length=0):
            limits.append(max_length)
            assert 0 < max_length <= 65, "Unbounded inflation permits a compression bomb"
            return self.decoder.decompress(data, max_length)
        def __getattr__(self, name):
            assert name != "flush", "Unbounded flush must not inflate remaining bomb data"
            return getattr(self.decoder, name)
    monkeypatch.setattr(zlib, "decompressobj", LimitedDecoder)
    response = Response(data=data, Content_Encoding=encoding)
    fake_http(monkeypatch, response)
    result = sources.fetch_public_source(URL)
    assert result["status"] == "response_too_large" and result["text"] == "" and response.closed
    assert limits == [65]


@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
def test_exact_decoded_size_boundary_succeeds_and_one_extra_byte_is_rejected(monkeypatch, encoding, compress):
    monkeypatch.setattr(sources, "MAX_RESPONSE_BYTES", 16)
    for length, status in [(16, "ok"), (17, "response_too_large")]:
        response = Response(data=compress(b"A" * length), Content_Encoding=encoding)
        fake_http(monkeypatch, response)
        result = sources.fetch_public_source(URL)
        assert result["status"] == status and response.closed
        assert result["text"] == ("A" * 16 if status == "ok" else "")


@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
def test_truncated_compressed_stream_never_returns_partial_decoded_text(monkeypatch, encoding, compress):
    response = Response(data=compress(b"A verified fact.")[:-3], Content_Encoding=encoding)
    fake_http(monkeypatch, response)
    result = sources.fetch_public_source(URL)
    assert result["status"] == "incomplete_compressed_body" and result["text"] == "" and response.closed


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_invalid_compressed_body_never_decodes_as_plain_text(monkeypatch, encoding):
    response = Response(data=b"A verified fact.", Content_Encoding=encoding)
    fake_http(monkeypatch, response)
    result = sources.fetch_public_source(URL)
    assert result["status"] == "invalid_compressed_body" and result["text"] == "" and response.closed


@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
@pytest.mark.parametrize("trailing", [b"junk", None])
def test_trailing_data_or_additional_compressed_member_is_rejected(monkeypatch, encoding, compress, trailing):
    data = compress(b"A verified fact.") + (compress(b"Another member.") if trailing is None else trailing)
    response = Response(data=data, Content_Encoding=encoding)
    fake_http(monkeypatch, response)
    result = sources.fetch_public_source(URL)
    assert result["status"] == "trailing_compressed_data" and result["text"] == "" and response.closed


def test_gzip_checksum_is_required(monkeypatch):
    data = bytearray(gzip.compress(b"A verified fact."))
    data[-8] ^= 1
    response = Response(data=bytes(data), Content_Encoding="gzip")
    fake_http(monkeypatch, response)
    result = sources.fetch_public_source(URL)
    assert result["status"] == "invalid_compressed_body" and result["text"] == "" and response.closed


def test_http_body_shorter_than_declared_compressed_length_is_incomplete(monkeypatch):
    data = gzip.compress(b"A verified fact.")
    response = Response(data=data, Content_Encoding="gzip", Content_Length=len(data) + 10)
    fake_http(monkeypatch, response)
    result = sources.fetch_public_source(URL)
    assert result["status"] == "incomplete_body" and result["text"] == "" and response.closed


def test_compressed_html_is_parsed_only_after_full_restoration(monkeypatch):
    data = gzip.compress(b"<p>A verified fact.</p><script>Fake source</script>")
    response = Response(data=data, content_type="text/html; charset=utf-8", Content_Encoding="gzip")
    fake_http(monkeypatch, response)
    result = sources.fetch_public_source(URL)
    assert result["status"] == "ok" and sources._normalize(result["text"]) == "A verified fact."
    assert response.closed


def test_html_visible_text_excludes_scripts_styles_and_comments(monkeypatch):
    content = '<html><head><title>Not visible</title></head><body><script>Fake fact</script><style>Fake fact</style><!-- Fake fact --><p>A <b>verified</b> fact.</p><div>Caf&eacute;&nbsp;한글</div></body></html>'
    fake_http(monkeypatch, Response(data=content.encode(), content_type="text/html; charset=utf-8"))
    result = sources.fetch_public_source(URL)
    assert result["status"] == "ok"
    assert "Fake fact" not in result["text"] and "Not visible" not in result["text"]
    assert sources._normalize(result["text"]) == "A verified fact. Café 한글"


def test_full_timeout_aborts_slow_request(monkeypatch):
    aborted = threading.Event()
    class SlowConnection:
        def __init__(self, *args):
            pass
        def request(self, *args, **kwargs):
            pass
        def getresponse(self):
            assert aborted.wait(0.5)
            raise TimeoutError()
        def abort(self):
            aborted.set()
        def close(self):
            pass
    monkeypatch.setattr(sources, "_PinnedConnection", SlowConnection)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **k: [ipv4(port=port)])
    result = sources.fetch_public_source(URL, deadline=time.monotonic() + 0.03)
    assert result["status"] == "timeout" and aborted.is_set()


@pytest.mark.parametrize("secure", [False, True])
@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
def test_deadline_interrupts_detached_connection_close_response_body(monkeypatch, secure, encoding):
    """Use real HTTPConnection.getresponse/read with a socket-backed fake stream.

    The stdlib sets connection.sock=None but keeps response.fp open. The timer
    must shutdown the retained transport socket and finally close response.fp.
    """
    aborted, reading = threading.Event(), threading.Event()
    connections = []
    headers = ("HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n"
               f"Content-Encoding: {encoding}\r\nContent-Length: 10000\r\n\r\n").encode()
    class SlowBody(io.RawIOBase):
        def __init__(self):
            super().__init__()
            self.headers = headers
        def readable(self):
            return True
        def readinto(self, buffer):
            if self.headers:
                chunk, self.headers = self.headers[:len(buffer)], self.headers[len(buffer):]
                buffer[:len(chunk)] = chunk
                return len(chunk)
            assert connections[0].sock is None
            reading.set()
            assert aborted.wait(1), "Timer failed to interrupt detached response socket"
            return 0
    raw = SlowBody()
    class FakeSocket:
        def settimeout(self, value):
            pass
        def connect(self, address):
            pass
        def sendall(self, value):
            pass
        def makefile(self, mode):
            assert mode == "rb"
            return io.BufferedReader(raw)
        def do_handshake(self):
            pass
        def shutdown(self, how):
            assert how == socket.SHUT_RDWR
            aborted.set()
        def close(self):
            pass  # An outstanding makefile retains the underlying descriptor.
    sock = FakeSocket()
    monkeypatch.setattr(socket, "socket", lambda *args: sock)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **k: [ipv4(port=port)])
    class Context:
        def wrap_socket(self, original, **kwargs):
            return original
    monkeypatch.setattr(sources.ssl, "create_default_context", lambda: Context())
    pinned = sources._PinnedConnection
    def record_connection(*args):
        conn = pinned(*args)
        connections.append(conn)
        return conn
    monkeypatch.setattr(sources, "_PinnedConnection", record_connection)
    url = URL if secure else URL.replace("https:", "http:")
    result = sources.fetch_public_source(url, deadline=time.monotonic() + 0.15)
    assert result["status"] == "timeout"
    assert reading.is_set() and aborted.is_set() and raw.closed
