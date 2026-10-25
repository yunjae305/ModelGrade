"""수집용 Chrome 탭 차지 — AIONA 두 모델 동시 수집 때 실행마다 자기 탭 (할일 1번, 10-25 밤)."""
from __future__ import annotations

import pytest

from tools import chrome_cdp as cc


class FakePage:
    def __init__(self, url: str, name: str = ""):
        self.url, self.name, self.closed = url, name, False

    def is_closed(self) -> bool:
        return self.closed

    def evaluate(self, js: str, arg=None):
        if arg is None:
            return self.name
        if len(arg) == 1:   # release_tab
            if self.name == arg[0]:
                self.name = ""
            return None
        key, prefix = arg   # _CLAIM_JS 와 같은 비교 후 쓰기
        if self.name == key:
            return True
        if self.name.startswith(prefix):
            return False
        self.name = key
        return True


class FakeContext:
    def __init__(self, pages):
        self.pages = pages


class FakeBrowser:
    def __init__(self, pages):
        self.contexts = [FakeContext(pages)]


def test_two_runs_get_their_own_aiona_tabs(monkeypatch):
    shared = FakePage("https://dev.aiona.kr/chat?cid=1")
    other_site = FakePage("https://chatgpt.com/")
    browser = FakeBrowser([other_site, shared])
    opened = []

    def new_window(context, url):
        page = FakePage(url)
        context.pages.append(page)
        opened.append(page)
        return page
    monkeypatch.setattr(cc, "new_window_page", new_window)
    a = cc.find_or_open(browser, "dev.aiona.kr", "https://dev.aiona.kr/chat", claim="aiona:자동 추천")
    b = cc.find_or_open(browser, "dev.aiona.kr", "https://dev.aiona.kr/chat", claim="aiona:Claude Sonnet 5.5")
    assert a is shared and b is opened[0] and a is not b
    assert cc.tab_claim(a) == "modelgrade:aiona:자동 추천" and cc.tab_claim(b) == "modelgrade:aiona:Claude Sonnet 5.5"
    # 같은 실행이 다시 붙으면 자기 탭 · 차지 표시 없는 호출은 예전처럼 첫 탭
    assert cc.find_or_open(browser, "dev.aiona.kr", "x", claim="aiona:Claude Sonnet 5.5") is b
    assert cc.find_or_open(browser, "dev.aiona.kr", "x") is shared
    assert other_site.name == ""   # 다른 사이트 탭은 건드리지 않음


def test_claim_gives_up_when_blank_tabs_keep_getting_taken(monkeypatch):
    browser = FakeBrowser([])
    monkeypatch.setattr(cc, "new_window_page", lambda context, url: FakePage(url, "modelgrade:aiona:다른 실행"))
    with pytest.raises(RuntimeError):
        cc.find_or_open(browser, "dev.aiona.kr", "https://dev.aiona.kr/chat", claim="aiona:자동 추천")


def test_release_clears_only_my_claim():
    page = FakePage("https://dev.aiona.kr/chat", "modelgrade:aiona:자동 추천")
    cc.release_tab(page, "aiona:Claude Sonnet 5.5")
    assert page.name == "modelgrade:aiona:자동 추천"
    cc.release_tab(page, "aiona:자동 추천")
    assert page.name == ""

