"""수집용 Chrome 탭 차지 — 같은 사이트를 두 실행이 동시에 받을 때 실행마다 자기 탭 (할일 1번 · Codex 리뷰, 10-25 밤)."""
from __future__ import annotations

import time


from tools import chrome_cdp as cc


class FakePage:
    def __init__(self, url: str, name: str = ""):
        self.url, self.name, self.closed = url, name, False

    def is_closed(self) -> bool:
        return self.closed

    def evaluate(self, js: str, arg=None):
        if arg is None:
            return self.name
        old, new = arg   # _CAS_JS — 본 값과 같을 때만 바꾸기
        if self.name != old:
            return False
        self.name = new
        return True


class FakeContext:
    def __init__(self, pages):
        self.pages = pages


class FakeBrowser:
    def __init__(self, pages):
        self.contexts = [FakeContext(pages)]


def _opener(opened):
    def open_owned(context, url):
        page = FakePage(url)
        context.pages.append(page)
        opened.append(page)
        return page
    return open_owned


def test_two_runs_of_the_same_model_get_separate_tabs_and_release_only_their_own(monkeypatch):
    shared = FakePage("https://dev.aiona.kr/chat?cid=1")
    other_site = FakePage("https://chatgpt.com/")
    browser = FakeBrowser([other_site, shared])
    opened = []
    monkeypatch.setattr(cc, "open_owned_window", _opener(opened))
    t1, t2 = cc.claim_token("aiona:자동 추천"), cc.claim_token("aiona:자동 추천")
    assert t1 != t2                                                     # 같은 모델이라도 실행마다 다른 표시
    a = cc.find_or_open(browser, "dev.aiona.kr", "https://dev.aiona.kr/chat", claim=t1)
    b = cc.find_or_open(browser, "dev.aiona.kr", "https://dev.aiona.kr/chat", claim=t2)
    assert a is shared and b is opened[0] and a is not b
    assert cc.find_or_open(browser, "dev.aiona.kr", "x", claim=t2) is b   # 같은 실행이 다시 붙으면 자기 탭
    cc.release_tab(a, t2)                                                # 남의 표시는 풀지 못함
    assert cc.tab_claim(a) == "modelgrade:" + t1
    cc.release_tab(a, t1)
    assert cc.tab_claim(a) == "" and other_site.name == ""
    assert cc.find_or_open(browser, "dev.aiona.kr", "x") is shared        # 차지 표시 없는 호출은 예전처럼 첫 탭


def test_stale_claim_from_a_crashed_run_is_reclaimed(monkeypatch):
    old = f"modelgrade:aiona:자동 추천|dead|{int(time.time()) - cc.CLAIM_STALE_S - 10}"
    fresh = f"modelgrade:aiona:자동 추천|live|{int(time.time())}"
    stale_tab, busy_tab = FakePage("https://dev.aiona.kr/a", old), FakePage("https://dev.aiona.kr/b", fresh)
    browser = FakeBrowser([busy_tab, stale_tab])
    monkeypatch.setattr(cc, "open_owned_window", _opener([]))
    token = cc.claim_token("aiona:Claude Sonnet 5.5")
    assert cc.find_or_open(browser, "dev.aiona.kr", "x", claim=token) is stale_tab
    assert busy_tab.name == fresh                                       # 살아 있는 실행의 탭은 건드리지 않음


def test_claim_is_compare_and_swap():
    page = FakePage("https://dev.aiona.kr/a")
    assert cc.claim_tab(page, "aiona:x|1|1", seen="")
    assert not cc.claim_tab(page, "aiona:y|2|2", seen="")                # 그새 바뀌었으면 실패
