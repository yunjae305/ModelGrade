"""aiona-chat-eval 프로그램 버전 — 시맨틱 버저닝 X.Y.Z (사용자 10-27 '버전은 이것을 따르자').

- X (Major): 하위 호환이 깨지는 변경 — 결과 폴더 · 결과 JSON 양식, 명령 · 옵션 이름, 기준 수(72)나 판정 규칙처럼 전 결과와 바로 견줄 수 없게 되는 것
- Y (Minor): 하위 호환을 지키며 기능이 늘 때 — 새 명령 · 옵션 · 보고서 장면, 새로 고를 수 있는 모델
- Z (Patch): 기능 추가 없이 버그 수정 · 문구 · 내부 정리만

올릴 때는 이 숫자와 CHANGELOG.md 를 같이 고치고 git 태그 vX.Y.Z 를 단다. 결과 JSON(program_version)과 보고서 머리에 이 숫자가 남는다.
"""

__version__ = "1.0.0"
NAME = "aiona-chat-eval"


def label() -> str:
    """'aiona-chat-eval 1.0.0'"""
    return f"{NAME} {__version__}"
