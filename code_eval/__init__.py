"""AIONA 코드 평가기 v16 — 객관적 검증은 CODE, 의미 품질(J1~J6)은 Jev 가 따로 판단한다.

진입점
- ``code_eval.engine.evaluate_case`` / ``evaluate_batch`` : CODE 평가
- ``code_eval.jev_eval.jev_after_code`` : Jev 의미 평가 (CODE 결과를 바꾸지 않음)
- ``python -m code_eval`` : CLI
- ``python -m code_eval.llm_review`` : 기존 LLM 재검토 (기본 흐름 밖 별도 실행)
- ``webapp.server`` 의 ``/code-eval.html`` : 로컬 웹 화면

텍스트 파싱은 ``textparse``, 산술 계산은 ``arith``, LLM 호출은 ``llm_client``, Jev 호출은 ``jev_client``,
Claude/GPT CLI 호출은 ``claude_cli_client``/``codex_cli_client``, J1~J6 정의는 ``jev_criteria`` 가 맡는다.
"""

from .core import ENGINE_VERSION

__version__ = ENGINE_VERSION
