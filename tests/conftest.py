"""테스트 실행 환경 보정. 평가 의미나 검사 결과는 건드리지 않는다.

pytest 의 `tmp_path` 는 시스템 임시 디렉터리 아래 `pytest-of-<user>/` 를 쓴다. 그 디렉터리가
이전 실행에서 접근할 수 없는 권한으로 남으면(Windows 에서 관측), 테스트 내용과 무관하게
fixture setup 이 PermissionError 로 죽는다. 그때만 사용 가능한 basetemp 로 우회한다.

- 기본 경로를 실제로 쓸 수 있으면 아무것도 바꾸지 않는다 (pytest 기본 동작 유지).
- `--basetemp` 를 직접 준 실행도 그대로 둔다.
"""

from __future__ import annotations

import getpass
import os
import tempfile
import uuid
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_real_jev_call(monkeypatch):
    """테스트 중에는 환경변수·.env 설정과 무관하게 실제 Jev API·Claude/Codex CLI 를 부르지 않는다.

    기본 흐름(CLI·웹)은 키 없음(NOT_CONFIGURED)으로 돈다. Jev 동작은 테스트가 대역 client 를 직접 넘겨 확인한다.
    """
    from code_eval import jev_client, jev_eval

    monkeypatch.setattr(jev_eval, "resolve_jev_client",
                        lambda **_: jev_client.resolve_jev_client(environment={}, env_path=Path(__file__).parent / ".no-env"))
    # 개발자 .env 의 SEMANTIC_PIPELINE=items 가 기존 테스트를 흔들지 않도록 의미 평가 방식은 legacy 로 고정한다.
    monkeypatch.setattr(jev_eval, "resolve_semantic_pipeline", lambda *_, **__: "legacy")


@pytest.fixture(autouse=True)
def _no_real_web_logins(monkeypatch):
    """개발자 .env 에 FactChat 등 웹 로그인 정보가 있어도 테스트는 실제 웹 브라우저 수집을 실행 대상에 넣지 않는다 —
    테스트가 진짜 사이트에 로그인하지 않게 (AIONA 는 테스트마다 대역 AionaChat 을 쓴다)."""
    from webapp import comparison_service
    monkeypatch.setattr(comparison_service, "factchat_login_status", lambda *_, **__: {"credentials": False})
    monkeypatch.setattr(comparison_service, "oosoo_login_status", lambda *_, **__: {"credentials": False})
    monkeypatch.setattr(comparison_service, "cdp_login_status", lambda *_, **__: {"credentials": False, "tabs": {}})


@pytest.fixture(autouse=True)
def _multi_turn_length_rule_off(request, monkeypatch):
    """기존 J6 테스트는 짧은 대화로 J6 판정 내용(인용·이전 지시 유지)을 확인한다 — 멀티턴 길이 조건(10턴 이상 · 대화 전체 10K 토큰)은
    끄고(턴 0개·토큰 0 필요) 돌린다. 길이 조건 자체는 ``@pytest.mark.multi_turn_rule`` 테스트(test_multi_turn.py)가 실제 값으로 확인한다."""
    if request.node.get_closest_marker("multi_turn_rule"):
        return
    from code_eval import multi_turn
    monkeypatch.setattr(multi_turn, "MIN_TURNS", 0)
    monkeypatch.setattr(multi_turn, "MIN_TURN_TOKENS", 0)
    monkeypatch.setattr(multi_turn, "MIN_TOTAL_TOKENS", 0)



def _usable(path: Path) -> bool:
    """pytest 가 basetemp 뿌리로 쓸 수 있는 디렉터리인지."""
    try:
        if not path.exists():
            return True  # pytest 가 새로 만든다
        if not path.is_dir():
            return False
        list(path.iterdir())
        probe = path / f".pytest-write-probe-{uuid.uuid4().hex}"
        probe.mkdir()
        probe.rmdir()
        return True
    except OSError:
        return False


def pytest_configure(config):
    config.addinivalue_line("markers", "multi_turn_rule: 멀티턴 길이 조건(한 턴 10K 토큰 이상인 턴 10개 이상 · 대화 전체 100K 토큰)을 실제 값으로 둔다")
    if config.option.basetemp:
        return
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    root = Path(tempfile.gettempdir()) / f"pytest-of-{user}"
    if _usable(root):
        return
    fallback = Path(tempfile.gettempdir()) / f"pytest-basetemp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    config.option.basetemp = str(fallback)
    config.issue_config_time_warning(
        UserWarning(f"{root} 를 쓸 수 없어 basetemp 를 {fallback} 로 우회합니다 (환경 문제, 테스트 내용과 무관)."),
        stacklevel=2,
    )


@pytest.fixture(autouse=True)
def _single_first_judge_vote(monkeypatch):
    """판정 시험은 1차 심판을 턴마다 한 번 부르는 전제로 쓴 것 — 운영 기본(3표 다수결, 10-25)은 tests/test_judge_votes.py 가 따로 본다."""
    try:
        import tools.judge_v4 as jv
    except Exception:
        return
    monkeypatch.setattr(jv, "FIRST_JUDGE_VOTES", 1)
