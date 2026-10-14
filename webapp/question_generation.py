"""질문 세트 생성 — 웹 버튼 하나로 GPT 가 우리 세부 기준(CODE 검사·의미 평가 항목)을 모두 평가할 수 있는 질문을 최소 개수로 만든다.

사용자 결정(2026-10-13): 질문 목록은 우리가 세운 세세한 평가 항목 전부를 평가할 수 있게, 가장 적은 개수로, 주제·유형은 다양하게
만든다. 생성 모델은 GPT. 첨부파일(이미지·TXT·DOCX·PDF)도 질문을 만들 때 함께 만들고, 질문과 같이 각 모델에 보내 답변을 받는다.

흐름 (한 번의 생성 = ``QuestionSetBuilder.build``):
1. 계획 — GPT 가 질문·노리는 항목·첨부 명세·긴 자료 명세를 JSON 으로 낸다 (``plan_prompt``).
2. 첨부파일 — 명세대로 파일을 만든다 (``attachment_files``). 내용을 우리가 정했으므로 평가 모델에 정답 내용을 줄 수 있다.
3. 긴 자료 — J7(입력 272K 토큰 이상)은 참조 자료를 여러 번 나눠 쓰게 해 이어 붙이고 사실 문장을 정한 위치에 넣는다.
   J6(턴마다 질문+답변 10K 토큰 이상, 10턴 이상)은 턴마다 붙여 넣을 자료를 만들어 질문 앞에 붙인다 — 질문만으로 10K 토큰을 넘긴다.
   자료는 '[붙여 넣은 자료] … [자료 끝]' 으로 감싼다(``code_eval.analysis``) — 평가기는 그 안을 사용자가 준 자료로만 보고 지시는 요청
   부분에서 찾는다 (사용자 결정 2026-10-14: 붙여 넣은 글은 첨부가 아니라 질문 내용이며, 자료 속 문장을 지시로 읽으면 안 된다).
4. 점검 — 질문 세트 점검(``tools.check_question_set``)과 같은 규칙으로 검사·항목별 적용 수를 세고, 빠진 것만 GPT 에 보충을
   요청한다 (최대 ``SUPPLEMENT_ROUNDS`` 회). 답변 구조로만 생기는 검사는 질문만으로 셀 수 없어 GPT 가 적은 노리는 항목으로 표시한다.
5. 저장 — ``data/question_sets/<id>/questions.json`` (업로드 화면과 같은 id/turns JSON) 과 ``files/`` (첨부파일).
"""
from __future__ import annotations

import json
import shutil
import math
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from code_eval import long_context, multi_turn
from code_eval.analysis import PASTE_BEGIN, PASTE_END, escape_paste_markers
from code_eval.criteria_registry import CONDITION_KIND_NAMES, reference_details
from code_eval.semantic_items import ITEMS, SUBS_BY_ID

from .attachment_files import ATTACHMENT_TYPES, AttachmentError, render_attachment
from .code_eval_service import ROOT

__all__ = ["QUESTION_GEN_VERSION", "GenerationError", "IncompleteResponse", "criteria_catalog", "plan_prompt", "supplement_prompt", "parse_plan",
           "QuestionSetBuilder", "QuestionSetService"]

#: v1.1: 붙여 넣은 자료에 끝 표시([자료 끝]) — 평가기가 자료와 요청을 구분
#: v1.2: 세트 전체 질문 수 지정(target_questions)·변별력(질문마다 trap·difficulty)·앞선 실행에서 모두 PASS 한 단위 되먹임(weak_units)
#:       — 사용자 결정 2026-10-14: 전부 PASS 가 뜨는 질문은 좋은 질문이 아니다, 질문 수를 바꿔 가며 최소·충분 개수를 찾는다
#: v1.3: 질문 수를 지정하면 10턴 대화(J6)와 긴 문서 질문(J7)을 전용 단계에서 정한 수만큼 — 멀티턴 턴마다 답변 분량을 프롬프트로 요구,
#:       긴 문서 하나를 여러 J7 질문이 함께 쓴다 (사용자 결정 2026-10-14: 항목마다 판정에 충분한 질문 수)
#: v1.4: 세부 기준(검사 안의 질문 조건 종류 37 + 의미 점검 항목)마다 적용 수를 세고 모자란 기준을 노려 추가 — 항목별 목표 적용 수(per_target),
#:       멀티턴 답변 분량은 질문 문장에 쓰지 않고 설계 안내로만 (사용자 결정 2026-10-14)
QUESTION_GEN_VERSION = "question-gen-v1.4"
STORE = ROOT / "data" / "question_sets"
CONDITIONS_PATH = ROOT / "code_eval" / "settings" / "code_check_conditions.json"
PLAN_TOKENS = 60_000          # 계획 응답 최대 출력 토큰
MATERIAL_TOKENS = 32_000      # 긴 자료 한 조각의 최대 출력 토큰 (추론 토큰까지 함께 세는 모델이라 넉넉히)
CHUNK_CHARS = 10_000          # 긴 자료 한 조각에 요청하는 글자 수 (한국어 약 0.72토큰/자)
PASTE_CHARS = 3_000           # J6 자료가 필요한 턴에 붙여 넣을 자료 글자 수 (약 2K 토큰)
MAX_PASTE_TURNS = 3           # 대화 하나에서 자료를 붙이는 턴 수 상한 — 사용자 결정 2026-10-14: 턴마다 과하게 붙이지 않는다
SUPPLEMENT_ROUNDS = 2
# 비용 상한 — GPT 계획이 지나치게 커도 호출이 끝없이 늘지 않게 (질문 수 지정의 상한이기도 하다, 사용자 요청 2026-10-14: 300개 시험)
MAX_PLAN_QUESTIONS = 500
PLAN_BATCH = 40               # GPT 응답 하나에 계획하게 하는 질문 수 — 수백 개를 한 응답에 쓰게 하면 출력 한도에 잘린다
MAX_EXTRA_ROUNDS = MAX_PLAN_QUESTIONS // PLAN_BATCH + 3
DIFFICULTIES = ("쉬움", "보통", "어려움")
MAX_PLAN_TURNS = 15
MAX_LONG_REFERENCES = 1       # 긴 참조 자료(J7, 272K 토큰 이상)는 세트당 1개 — 생성·수집·평가를 합쳐 하나에 약 $5~8 (사용자 결정 2026-10-14)
MAX_CALLS = 250               # 작업 하나의 GPT 호출 상한 (질문 수를 지정하면 그 수에 맞춰 늘린다 — QuestionSetBuilder.build)
MATERIAL_WORKERS = 4
MAX_ITEMS_PER_QUESTION = 8
MAX_ATTACHMENT_CHARS = 4_000
#: 질문 수를 지정했을 때 10턴 대화·긴 문서 질문의 기본 비율 (설계안 v1.2 질문 세트 구성 초안: 멀티턴 10%, 롱컨텍스트 5%)
MULTI_TURN_RATIO = 0.10
LONG_CONTEXT_RATIO = 0.05
#: 10턴 대화의 턴마다 노리는 답변 글자 수 — 10턴 × 1,500자 × 약 0.72토큰/자 ≈ 10.8K 토큰 (멀티턴 조건: 대화 전체 10K 토큰).
#: 멀티턴 여부는 모델마다 실제 답변으로 재므로 답이 길어지는 요청으로 설계하게 한다 — 질문 문장에 분량을 쓰지는 않는다 (사용자 결정 2026-10-14)
TURN_ANSWER_CHARS = 1_500
MULTI_TURN_BATCH = 4          # GPT 응답 하나에 계획하는 10턴 대화 수 (대화 하나가 질문 10개라 출력이 길다)
LONG_CONTEXT_BATCH = 20       # 긴 문서 질문을 한 응답에 계획하는 수
DEDICATED_ITEMS = ("J6-", "J7-")   # 전용 단계가 맡는 항목 — 일반 계획에서는 빼고 묻는다
#: 멀티턴 답변 분량 안내 — 생성 프롬프트에만 넣는다. 모델에게 보내는 질문 문장에는 분량을 쓰지 않는다 (사용자 결정 2026-10-14).
_LENGTH_RULE = (f"멀티턴으로 인정되려면 대화 전체(모든 턴의 질문 + 답변)가 {multi_turn.MIN_TOTAL_TOKENS:,}토큰 이상이어야 하고, 이 길이는 모델마다 "
                f"실제 답변으로 잽니다. 그래서 턴마다 답변이 약 {TURN_ANSWER_CHARS:,}자 이상 나오도록 요청 자체를 설계합니다 — 표·단계별 설명·초안 작성·"
                f"수정본 전체 다시 쓰기·여러 안 비교처럼 답이 길어지는 요청. 답변 분량을 요구하는 문구('N자 이상')는 질문에 쓰지 않습니다(모델에게 보내는 "
                f"질문은 평소 사용자 질문 그대로). 분량을 줄이는 지시(한 문장으로, N자 이내, 짧게, 세 줄로 요약)도 쓰지 않고, 조건 변경·수정 요청도 분량을 "
                f"줄이지 않는 방식으로 씁니다(예: '표의 열을 바꿔 전체를 다시 써 줘', '존댓말로 바꿔 수정본 전체를 다시 보여 줘').")

LLM = Callable[[str, int], str]


class GenerationError(RuntimeError):
    pass


class IncompleteResponse(GenerationError):
    """응답이 출력 한도 등으로 끝까지 오지 않음 — ``text`` 는 받은 데까지의 글 (긴 자료는 이것도 쓴다)."""

    def __init__(self, message: str, text: str = ""):
        super().__init__(message)
        self.text = text



class Cancelled(GenerationError):
    pass

# --- 1. 점검 목록과 프롬프트 ---------------------------------------------------------------

#: 세부 조건(검사 안에서 질문이 만드는 조건 종류)의 이름 — 정의는 criteria_registry 한 곳 (생성기가 이 단위로 적용 수를 채운다)
KIND_NAMES = dict(CONDITION_KIND_NAMES)

def criteria_catalog() -> Dict[str, List[Dict[str, str]]]:
    """프롬프트에 넣을 점검 목록 — CODE 검사(AIONA 특화 제외)와 의미 평가 항목. 기준 정의에서 만들어 기준이 바뀌면 같이 바뀐다."""
    from tools.check_question_set import KIND_UNIT
    examples: Dict[str, str] = {}
    rules: Dict[str, List[str]] = {}
    question_rules: Dict[str, int] = {}
    try:
        for check in json.loads(CONDITIONS_PATH.read_text(encoding="utf-8")).get("checks") or []:
            sample = ((check.get("examples") or {}).get("PASS") or {}).get("question")
            if isinstance(sample, str):
                examples[check["id"]] = " ".join(sample.split())[:160]
            # 엔진이 질문에서 요구를 읽는 규칙 — 이 표현·자료 형식이 아니면 검사가 적용되지 않는다 (실제 생성에서 확인)
            conditions = [c for c in check.get("conditions") or [] if isinstance(c, dict) and c.get("title")]
            # 질문 요구로 생기는 세부 조건을 앞에 — 생성기가 질문에 넣어야 할 표현이다 (답변 자체·고정 정책 조건은 뒤)
            conditions.sort(key=lambda c: c.get("source") != "question")
            rules[check["id"]] = [f"{c.get('title')}: {' '.join(str(c.get('text') or '').split())}" for c in conditions]
            question_rules[check["id"]] = sum(1 for c in conditions if c.get("source") == "question")
    except (OSError, ValueError):
        pass
    codes = [{"id": c["id"], "name": c["name"], "applies": c["applies"], "example": examples.get(c["id"], ""),
              "rules": rules.get(c["id"], []), "question_rules": question_rules.get(c["id"], 0),
              "kinds": [{"id": f"{unit}:{kind}", "name": KIND_NAMES.get(kind, kind)} for kind, unit in KIND_UNIT.items() if unit == c["id"]]}
             for c in reference_details()["code_checks"] if not c["id"].startswith("AIONA")]
    def fail_note(item: Any) -> str:
        fail = next((ex for ex in item.examples if ex.kind == "FAIL"), None)
        return " ".join(str(getattr(fail, "note", "") or "").split())[:200] if fail else ""

    def example(item: Any) -> str:
        sample = next((ex for ex in item.examples if ex.kind == "PASS"), None)
        return " ".join(str(getattr(sample, "question", "") or "").split())[:160] if sample else ""

    # 의미 평가 점검 항목 — 세부 기준(sub)·판정 근거·펼침(요소별 판정)·적용을 평가 모델이 정하는지·FAIL 예시까지 (사용자 결정 2026-10-14:
    # 의미 평가 기준도 세세하게 평가할 수 있게 질문을 만든다)
    items = [{"id": item.id, "sub_id": item.sub, "sub": SUBS_BY_ID[item.sub].name, "question": item.question, "applies": item.applies,
              "evidence": item.evidence, "expand": item.expand, "model_applies": item.model_applies, "example": example(item),
              "fail": fail_note(item)} for item in ITEMS]
    return {"code": codes, "items": items}


#: 변별력 — 전부 PASS 가 나오는 질문은 모델을 가르지 못한다 (사용자 결정 2026-10-14)
_DISCRIMINATION = """# 변별력 (중요)
- 모든 모델이 PASS 하는 질문은 평가에 쓸모가 없습니다. 질문마다 '틀리기 쉬운 지점'을 하나 이상 넣고, trap 에 어떤 답이 FAIL 이 되는지 한 줄로 씁니다.
  예: 함께 지키기 어려운 조건 둘(글자 수 + 필수 문구 셋), 정확한 개수·글자 수, 주제상 자연스럽게 나오는 금지어, 흔히 잘못 아는 사실(연도·수치·명칭),
  긴 자료에서 멀리 떨어진 근거 3곳 연결, 앞 턴에서 바꾼 조건을 뒤 턴에 다시 묻기, 자료에 없는 정보 묻기(지어내면 FAIL), 상충하는 요구에서 가정을 밝혀야 하는 질문.
- difficulty 를 쉬움·보통·어려움으로 적고 섞습니다(어려움이 3분의 1 이상). 어려움도 요구는 명확해야 합니다 — 모호해서 어려운 질문은 안 됩니다."""

_FORMAT = """# 출력 형식 (JSON 하나만, 설명·코드 펜스 없이)
{"questions": [
  {"id": "Q01", "type": "질문 유형", "topic": "주제", "target_items": ["점검 목록의 id", "..."],
   "difficulty": "쉬움|보통|어려움", "trap": "어떤 답이 FAIL 이 되는지 한 줄",
   "turns": [{"question": "질문 원문",
              "attachments": [{"name": "견적서.pdf", "type": "pdf", "content": "파일에 들어갈 내용 전체"}],
              "long_reference": {"topic": "자료 주제", "facts": [{"text": "자료에 넣을 사실 문장", "position": 0.5}]},
              "long_paste": {"topic": "붙여 넣을 자료 주제", "must_include": ["꼭 들어갈 내용"]}}]}
]}
- 필요 없는 attachments·long_reference·long_paste 는 빼거나 null 로 둡니다. 단일 질문은 turns 가 1개, 멀티턴 대화는 여러 개입니다.
- target_items 에는 [점검 목록]의 id 만(CODE 는 세부 조건 id '검사:종류'를 우선), 그 질문(대화)에서 적용되도록 의도한 것을 씁니다."""


def _catalog_lines(catalog: Dict[str, List[Dict[str, str]]], only: Optional[set] = None, detailed: bool = False) -> List[str]:
    """점검 목록 줄. CODE 검사는 세부 조건(id '검사:종류')과 질문 쪽 인식 규칙 전부(``detailed`` 면 나머지 규칙까지)를 함께 —
    질문에 그 표현을 그대로 써야 적용된다. ``only`` 에 세부 조건 id 가 있으면 그 검사와 해당 세부 조건만."""
    lines = ["## CODE 검사 (id · 이름 · 적용 · 예시 질문 · 세부 조건 · 인식 규칙)"]
    for c in catalog["code"]:
        kinds = [k for k in c.get("kinds") or [] if only is None or k["id"] in only or c["id"] in only]
        if only is None or c["id"] in only or kinds:
            lines.append(f"- {c['id']} · {c['name']} · {c['applies']}" + (f" · 예: {c['example']}" if c["example"] else ""))
            if kinds:
                lines.append("    · 세부 조건: " + ", ".join(f"{k['id']}({k['name']})" for k in kinds))
            shown = len(c.get("rules") or []) if detailed else max(1, c.get("question_rules") or 0)
            for rule in (c.get("rules") or [])[:shown]:
                lines.append(f"    · 인식: {rule[:600 if detailed else 260]}")
    lines.append("## 의미 평가 점검 항목 (id · 세부 기준 · 점검 질문(예 = PASS) · 적용 · 판정 근거 · 펼침 · FAIL 예)")
    for item in catalog["items"]:
        if only is None or item["id"] in only:
            line = f"- {item['id']} · {item.get('sub_id', '')} {item['sub']} · {item['question']} · 적용: {item['applies']}"
            if item.get("evidence"):
                line += f" · 근거: {item['evidence']}"
            if item.get("expand"):   # 요소마다 따로 판정 — 질문에 그 요소가 여럿 있어야 세세하게 갈린다
                line += f" · 펼침: {item['expand']}(질문에 이 요소를 2개 이상 넣음)"
            if item.get("model_applies"):
                line += " · 적용 여부는 평가 모델이 정함(그 상황이 분명히 생기게 설계)"
            if item.get("example") and (detailed or only is not None):
                line += f" · 예시 질문: {item['example']}"
            if item.get("fail"):
                line += f" · FAIL 예: {item['fail']}"
            lines.append(line)
    return lines


def _weak_block(weak_units: Sequence[Dict[str, str]]) -> str:
    """앞선 실행에서 모든 모델이 PASS 한 검사·기준 — 더 어려운 질문을 요구하는 블록 (없으면 빈 문자열)."""
    if not weak_units:
        return ""
    lines = "\n".join((f"- {u.get('id')} {u.get('name') or ''}".rstrip()
                       + (f" (일부 모델이 답을 내지 못해 평가되지 않은 질문 {u['unevaluated']}건 제외)" if u.get("unevaluated") else ""))
                      for u in weak_units)
    return ("\n# 변별력 보강\n앞선 실행에서 평가된 모든 질문에서 모든 모델이 PASS 한 검사·기준입니다(답을 내지 못해 평가되지 않은 질문은 제외한 결과). "
            "각각에 더 어려운 질문(trap 이 분명한)을 1개 이상 넣습니다:\n" + lines + "\n")


def plan_prompt(catalog: Optional[Dict[str, List[Dict[str, str]]]] = None, note: str = "", *, target_questions: Optional[int] = None,
                weak_units: Sequence[Dict[str, str]] = (), dedicated: bool = False) -> str:
    """계획 프롬프트. ``target_questions`` 가 있으면 세트 전체 질문 수(대화 1개 = 1개)를 그 수에 맞추게 하고, 없으면 가능한 한 적게."""
    catalog = catalog or criteria_catalog()
    if dedicated:   # 10턴 대화(J6)·긴 문서(J7)는 전용 단계가 만든다 — 여기서는 그 항목을 빼고 묻는다
        catalog = general_catalog(catalog)
    n_code, n_items = len(catalog["code"]), len(catalog["items"])
    count_line = (f"질문은 정확히 {target_questions}개 만듭니다(멀티턴 대화 1개 = 1개). 모든 검사·항목을 넣고 남는 질문은 틀리기 쉬운 항목과 "
                  f"적용 질문이 적은 항목에 배분해 항목마다 적용 질문이 고르게 여러 개가 되게 합니다."
                  if target_questions else "질문 수는 가능한 한 적게 합니다.")
    _long_sections = ("# 멀티턴 대화·롱컨텍스트\n- 10턴 대화(J6)와 긴 참조 자료 질문(J7)은 따로 만듭니다. 이 계획에는 넣지 않습니다(long_reference 금지, "
                      "대화는 앞 턴 지시 승계처럼 짧은 2~4턴만).\n\n") if dedicated else _MULTI_LONG.format(
                          min_turns=multi_turn.MIN_TURNS, min_total=f"{multi_turn.MIN_TOTAL_TOKENS:,}", paste=f"{PASTE_CHARS:,}",
                          max_paste=MAX_PASTE_TURNS, min_tokens=f"{long_context.MIN_TOKENS:,}")
    head = f"""당신은 한국어 AI 답변 품질 평가에 쓸 질문 세트를 설계합니다. 결과는 JSON 하나로만 답합니다.

# 목표
아래 [점검 목록]의 CODE 검사 {n_code}개와 의미 평가 항목 {n_items}개가 모두 최소 1개 질문(또는 대화)에서 적용되게 합니다. {count_line}
- 한 질문이 여러 항목을 함께 적용받게 설계합니다(예: 대상 지정 + 말투 지정 + 비교 + 표 형식 + 글자 수 조건 + 출처 번호). 다만 한 질문이 노리는 항목은 {MAX_ITEMS_PER_QUESTION}개 이하로 하고 질문이 부자연스럽지 않게 합니다.
- 서로 막는 항목은 다른 질문으로 나눕니다. 빈 출력 요구(2-1.explicit_empty)와 모순·빠진 자료(J1-3)는 다른 요구와 섞지 않습니다.
- 주제(생활·업무·학습·건강·금융·여행·과학·역사·코딩 등)와 질문 유형(설명·사실 확인·분석·코딩·글쓰기·요약·추천·계획·계산)을 다양하게 섞고 같은 주제를 반복하지 않습니다.

# 작성 규칙
1. 답변이 요구를 지켰는지 판단할 수 있게 구체적으로 씁니다('짧게' 대신 '200자 이내', '표로' 대신 '열이 품목·가격인 표로').
2. CODE 검사는 [인식] 규칙에 맞는 표현과 자료 형식을 그대로 씁니다(예시 질문의 문장 구조를 따르고 숫자·주제만 바꿈). 규칙에 맞지 않으면 검사가 적용되지 않습니다. 예: 계산은 '8,000원의 15%는 얼마야?'·'146 + 279는 얼마야?', 중복 제거는 JSON 배열·표·목록 자료와 '중복 없이'.
3. 답변 구조 검사(각주·인용 번호 출처·문서 내부 링크·HTML 태그·코드와 수식 펜스·계산식·'다음 N가지' 선언·한자나 일본어 표기)는 답변이 그 구조를 쓰도록 요청합니다(예: '각주를 [^1] 형식으로 달아 줘', '출처를 [1] 번호로 달고 끝에 [1] 출처 줄을 적어 줘', '목차를 만들고 각 항목을 #앵커 링크로 연결해 줘').
4. 사실 확인(J5-1)은 수치·날짜·명칭처럼 확인할 수 있는 사실을 묻습니다. 시점에 따라 바뀌는 사실은 기준 시점을 적습니다.
5. 질문에 준 자료(표·JSON 배열·목록·글)를 정렬·필터·집계·요약하게 하는 질문은 자료를 질문 안에 그대로 넣습니다.
6. 실제 개인의 개인정보나 위험한 요청은 넣지 않습니다. 가상의 인물·회사는 가상이라고 밝힙니다.

{_long_sections}# 첨부파일 (J8)
- 첨부 질문을 만들고 attachments 에 파일 명세를 씁니다. 종류는 {"·".join(ATTACHMENT_TYPES)}. content 에는 파일에 들어갈 실제 내용 전체를 씁니다({MAX_ATTACHMENT_CHARS:,}자 이하). 줄 표기: '# 제목', '## 소제목', '- 항목', '| a | b |' 표 행, '[다음 쪽]' 쪽 나눔.
- image 는 막대그래프면 "chart": {{"title": "...", "labels": ["..."], "values": [숫자], "unit": "단위"}} 를 쓰고, 그 밖에는 content 의 글·표를 그립니다.
- 첨부 질문은 파일 종류마다 1개 이상 만듭니다({"·".join(ATTACHMENT_TYPES)} — 첨부 질문 {len(ATTACHMENT_TYPES)}개 이상). image 는 막대그래프와 표·글 그림을 섞습니다. 첨부 질문도 다른 항목을 함께 노리게 해 전체 질문 수를 줄입니다.
- J8-2.a 가 적용되게 표·도표·쪽·목록 구조를 읽어야 답할 수 있는 질문을 넣습니다.

{_DISCRIMINATION}

{_FORMAT}
"""
    extra = f"\n# 추가 요청\n{note.strip()}\n" if note and note.strip() else ""
    tail = "\n\n" + _count_rule(target_questions) if target_questions else ""
    return head + _weak_block(weak_units) + extra + "\n# 점검 목록\n" + "\n".join(_catalog_lines(catalog)) + tail


_MULTI_LONG_BASE = '# 멀티턴 대화 (J6)\n- 대화 1개 이상, 대화마다 턴 {min_turns}개 이상. J6 항목이 모두 적용되게 턴을 설계합니다: 앞 턴의 지시·제약 유지, 이전 정보 사용, 지시어("아까 두 번째"), 조건 변경·취소, 같은 결과물 2회 이상 수정, 이전 결정에 이어지는 요청, 근거 없는 반박.\n- 멀티턴으로 인정되려면 대화 전체(모든 턴의 질문+답변)가 {min_total}토큰 이상이어야 합니다. 표·단계별 설명·초안 작성·수정본 전체 다시 쓰기처럼 답변이 충분히 길어지는 요청으로 채웁니다.\n- 자료가 꼭 필요한 턴에만 long_paste(붙여 넣을 자료의 주제와 꼭 들어갈 내용)를 씁니다. 시스템이 약 {paste}자 자료를 만들어 질문 앞에 붙입니다. 대화 하나에 최대 {max_paste}턴, 턴마다 붙이지 않습니다.\n\n# 롱컨텍스트 (J7)\n- 질문 정확히 1개에만 {min_tokens}토큰(한국어 약 38만 자) 이상의 참조 자료를 씁니다(세트 전체에서 긴 참조 자료는 1개). long_reference 에 자료 주제와, 자료 속에 넣을 사실 문장(facts)과 위치(position 0~1)를 씁니다. 시스템이 자료를 만들고 사실을 그 위치에 넣습니다.\n- J7-1.a 는 자료 중간·뒷부분의 사실을 질문과 다른 표현으로 묻고, J7-2.a 는 3곳 이상에 흩어진 사실을 연결·비교·집계하게 묻습니다. 그 한 질문으로 둘 다 묻습니다.\n\n'


_TOKENS_LINE = "- 멀티턴으로 인정되려면 대화 전체(모든 턴의 질문+답변)가 {min_total}토큰 이상이어야 합니다. "
assert _TOKENS_LINE in _MULTI_LONG_BASE
#: 대화 길이 줄을 분량 규칙으로 바꾼다 — 턴마다 질문에 'N자 이상으로'를 적게 (사용자 결정 2026-10-14)
_MULTI_LONG = _MULTI_LONG_BASE.replace(_TOKENS_LINE, "- " + _LENGTH_RULE.replace("{", "{{").replace("}", "}}") + " ", 1)


def _count_rule(n: int) -> str:
    """질문 수를 지정한 요청의 맨 끝에 다시 못 박는다 — 앞에만 적으면 GPT 가 40개 요청에 31개를 줬다 (사용자 요청 2026-10-14)."""
    return (f"# 질문 개수 (반드시 지킴)\nquestions 배열에 정확히 {n}개를 넣습니다. {n}개보다 적거나 많으면 안 됩니다(멀티턴 대화 1개 = 1개). "
            f"답하기 전에 questions 의 개수를 세어 {n}개인지 확인하고, 모자라면 채운 뒤 답합니다.")


def extra_prompt(count: int, have: int, target: Optional[int], existing_topics: List[str], catalog: Optional[Dict[str, List[Dict[str, str]]]] = None,
                 weak_units: Sequence[Dict[str, str]] = (), low_coverage: Sequence[Tuple[str, int]] = (), note: str = "",
                 long_reference_ok: bool = True, per_target: Optional[int] = None) -> str:
    """세트가 목표 질문 수에 못 미칠 때 질문을 더 만든다 — 변별력 보강 단위와 적용 질문이 적은 항목을 우선 노리게."""
    catalog = catalog or criteria_catalog()
    parts = [f"당신은 한국어 AI 답변 품질 평가에 쓸 질문 세트에 질문을 추가합니다. 결과는 JSON 하나로만 답합니다.\n\n"
             f"# 목표\n앞서 만든 질문 세트는 {have}개입니다. "
             + (f"목표 {target}개에 맞추려 질문 {count}개를 추가합니다(멀티턴 대화 1개 = 1개). " if target else
                f"세부 기준마다 적용 질문이 {per_target}개 이상이 되도록, [적용 질문이 적은 항목]의 모자란 기준을 노린 질문 {count}개를 추가합니다"
                f"(멀티턴 대화 1개 = 1개). 질문 하나가 모자란 기준 여러 개에 함께 적용되게 설계해 전체 질문 수를 줄입니다. ")
             + f"추가 질문은 [변별력 보강]과 [적용 질문이 적은 항목]을 우선 노리고, 기존 주제({', '.join(t for t in existing_topics if t)[:4000] or '없음'})와 겹치지 않게 합니다. "
             f"CODE 검사는 인식 규칙의 표현·자료 형식을 그대로 따라야 적용됩니다."
             + ("" if long_reference_ok else " 긴 참조 자료(long_reference)는 이미 다 만들어 이번에는 쓰지 않습니다."),
             _DISCRIMINATION, _FORMAT]
    weak = _weak_block(weak_units).strip()
    if weak:
        parts.append(weak)
    if low_coverage:
        parts.append("# 적용 질문이 적은 항목 (id · 현재 적용 질문 수)\n" + "\n".join(f"- {uid} · {n}" for uid, n in low_coverage))
    wanted = {u.get("id") for u in weak_units} | {uid for uid, _ in low_coverage}
    parts.append("# 점검 목록 (해당 항목만)\n" + "\n".join(_catalog_lines(catalog, wanted or None, detailed=True)))
    if _note_block(note):
        parts.append(_note_block(note))
    parts.append(_count_rule(count))
    return "\n\n".join(parts)


def _note_block(note: str) -> str:
    """사용자 추가 요청('금융·법률 주제는 빼 줘') — 모든 생성 호출에 같이 보낸다 (호출마다 독립이라 처음 프롬프트만으로는 모른다, Codex 리뷰)."""
    return f"# 추가 요청\n{note.strip()}" if note and note.strip() else ""


def supplement_prompt(missing: List[str], existing_topics: List[str], catalog: Optional[Dict[str, List[Dict[str, str]]]] = None,
                      missing_types: Tuple[str, ...] = (), note: str = "") -> str:
    """앞서 만든 질문 세트에서 적용되지 않은 검사·항목, 없는 첨부 종류만 보충한다."""
    catalog = catalog or criteria_catalog()
    parts = ["당신은 한국어 AI 답변 품질 평가에 쓸 질문 세트를 보충합니다. 결과는 JSON 하나로만 답합니다.\n\n"
             "# 목표\n앞서 만든 질문 세트에서 아래 [빠진 항목]이 어느 질문에도 적용되지 않았거나 [빠진 첨부 종류]의 첨부 질문이 없습니다. "
             "이것들이 적용되는 질문만 가능한 한 적게 추가합니다. 적용 문구에 맞는 표현을 질문에 분명히 넣습니다. 이미 쓴 주제와 겹치지 않게 합니다.\n"
             f"이미 쓴 주제: {', '.join(t for t in existing_topics if t)[:1500] or '없음'}\n"
             "멀티턴 대화·긴 참조 자료·첨부파일 규칙은 처음과 같습니다(대화는 턴 10개 이상·대화 전체 1만 토큰 이상, 자료가 필요한 턴에만 long_paste — 대화당 최대 3턴, 긴 자료는 long_reference, 첨부는 attachments).\n\n"
             f"{_FORMAT}"]
    if missing_types:
        parts.append("# 빠진 첨부 종류\n" + "\n".join(f"- {kind}: 이 종류의 파일을 첨부한 질문을 1개 이상 추가합니다 (빠진 항목도 함께 노리게)."
                                                   for kind in missing_types))
    if missing:
        parts.append("# 빠진 항목 (CODE 검사는 인식 규칙의 표현·자료 형식을 그대로 따라야 적용됩니다)\n"
                     + "\n".join(_catalog_lines(catalog, set(missing), detailed=True)))
    if _note_block(note):
        parts.append(_note_block(note))
    return "\n\n".join(parts)


def attachment_types(groups: List[Dict[str, Any]]) -> set:
    """질문 세트에 실제로 만든 첨부파일 종류."""
    return {a.get("type") for g in groups for t in g["turns"] for a in t.get("attachments") or []}


def _material_prompt(topic: str, part: int, parts: int, chars: int, facts: List[str]) -> str:
    avoid = "\n".join(f"- {fact}" for fact in facts) or "- (없음)"
    return (f"다음 주제의 긴 업무 자료 가운데 {part}번째 부분(전체 {parts}부분)을 한국어 본문으로 약 {chars:,}자 씁니다.\n"
            f"주제: {topic}\n"
            "- 보고서·회의록·매뉴얼·사례 기록처럼 자연스러운 문서로, 소제목과 문단을 섞어 씁니다. 앞뒤 부분과 이어지는 내용으로 씁니다.\n"
            "- 아래 문장들은 시스템이 자료의 정해진 위치에 직접 넣습니다. 이 문장을 쓰지 말고, 같은 대상에 대해 다른 값이나 모순되는 내용도 쓰지 않습니다.\n"
            f"{avoid}\n- 머리말·설명 없이 본문만 씁니다.")


def _paste_prompt(spec: Dict[str, Any], chars: int) -> str:
    raw = spec.get("must_include")
    must = "\n".join(f"- {x}" for x in (raw if isinstance(raw, list) else [])) or "- (없음)"
    return (f"다음 주제로 사용자가 대화창에 붙여 넣을 자료(회의록·보고서·기사·메모 등)를 한국어 본문으로 약 {chars:,}자 씁니다.\n"
            f"주제: {spec.get('topic') or '업무 자료'}\n꼭 들어갈 내용:\n{must}\n- 머리말·설명 없이 자료 본문만 씁니다.")


def split_counts(target: Optional[int], multi_turn: Optional[int] = None, long_context: Optional[int] = None,
                 per_target: Optional[int] = None) -> Tuple[Optional[int], Optional[int]]:
    """세트 질문 수 → (10턴 대화 수, 긴 문서 질문 수). 질문 수도 항목별 목표 적용 수도 없으면 (None, None) — 전용 단계 없이 예전처럼.

    값을 주면 그대로, 비우면 항목별 목표 적용 수(``per_target``)만큼(J6·J7 항목이 그 수만큼 적용되게), 그것도 없으면 질문 수의 기본 비율
    (최소 1개). 질문 수가 있으면 둘을 합쳐 그 수를 넘지 않게 줄인다(나머지 질문 1개 이상)."""
    if not target and not per_target:
        return None, None
    m = (int(multi_turn) if multi_turn is not None else per_target if per_target else max(1, round(target * MULTI_TURN_RATIO)))
    l = (int(long_context) if long_context is not None else per_target if per_target else max(1, round(target * LONG_CONTEXT_RATIO)))
    if m < 0 or l < 0:
        raise ValueError("10턴 대화 수·긴 문서 질문 수는 0 이상이어야 합니다.")
    target = target or MAX_PLAN_QUESTIONS   # 질문 수 없이 per_target 만이면 전체 상한 안에서 (Codex 리뷰: 전용 단계가 상한을 우회했다)
    while m + l >= target and (m or l):   # 다른 항목에 쓸 질문을 1개 이상 남긴다 — 큰 쪽부터 줄인다
        if m >= l:
            m -= 1
        else:
            l -= 1
    return m, l


def _dedicated(item_id: str) -> bool:
    return item_id.startswith(DEDICATED_ITEMS)


def general_catalog(catalog: Dict[str, List[Dict[str, str]]]) -> Dict[str, List[Dict[str, str]]]:
    """전용 단계(J6·J7)가 맡는 항목을 뺀 점검 목록 — 일반 계획이 10턴 대화·긴 자료를 따로 만들지 않게."""
    return {"code": catalog["code"], "items": [i for i in catalog["items"] if not _dedicated(i["id"])]}


def multi_turn_prompt(count: int, existing_topics: List[str], catalog: Optional[Dict[str, List[Dict[str, str]]]] = None, note: str = "") -> str:
    """10턴 대화를 정확히 ``count`` 개 — J6 항목을 대화들에 고르게 나눈다. 턴마다 답변 분량 요구는 시스템이 붙인다."""
    catalog = catalog or criteria_catalog()
    j6 = {i["id"] for i in catalog["items"] if i["id"].startswith("J6-")}
    parts = [f"당신은 한국어 AI 답변 품질 평가에 쓸 10턴 대화를 설계합니다. 결과는 JSON 하나로만 답합니다.\n\n"
             f"# 목표\n멀티턴 대화를 정확히 {count}개 만듭니다. 대화마다 턴은 정확히 {multi_turn.MIN_TURNS}개입니다. "
             f"아래 J6 항목이 모든 대화에 걸쳐 고르게, 항목마다 가능한 한 많은 대화에서 적용되게 턴을 설계합니다: 앞 턴의 지시·제약 유지, 이전 정보 사용, "
             f"지시어(\"아까 두 번째\"), 조건 변경·취소, 같은 결과물 2회 이상 수정, 이전 결정에 이어지는 요청, 근거 없는 반박. "
             f"주제는 서로 겹치지 않게 하고, 기존 주제({', '.join(t for t in existing_topics if t)[:2000] or '없음'})와도 겹치지 않게 합니다.\n\n"
             f"# 답변 분량\n{_LENGTH_RULE}\n\n"
             f"# 붙여 넣는 자료\n자료가 꼭 필요한 턴에만 long_paste(붙여 넣을 자료의 주제와 꼭 들어갈 내용)를 씁니다. 시스템이 약 {PASTE_CHARS:,}자 자료를 만들어 "
             f"질문 앞에 붙입니다. 대화 하나에 최대 {MAX_PASTE_TURNS}턴. long_reference·attachments 는 쓰지 않습니다.",
             _DISCRIMINATION, _FORMAT,
             "# 점검 목록 (J6 항목 — target_items 에는 이 id 와, 대화가 함께 노리는 다른 항목 id)\n" + "\n".join(_catalog_lines(catalog, j6))]
    if _note_block(note):
        parts.append(_note_block(note))
    parts.append(f"# 대화 개수 (반드시 지킴)\nquestions 배열에 대화를 정확히 {count}개, 대화마다 turns 를 정확히 {multi_turn.MIN_TURNS}개 넣습니다. "
                 "답하기 전에 개수를 세어 확인합니다.")
    return "\n\n".join(parts)


def long_context_prompt(count: int, catalog: Optional[Dict[str, List[Dict[str, str]]]] = None, note: str = "",
                        fixed: Optional[Dict[str, Any]] = None, existing: Sequence[str] = ()) -> str:
    """긴 문서 하나와 그 문서를 함께 쓰는 질문 ``count`` 개 — 문서는 한 번만 만든다 (생성 비용 1회).
    ``fixed`` (이미 정한 문서 주제·facts)를 주면 그 문서로 질문만 더 만든다 — 앞서 만든 질문(``existing``)과 겹치지 않게."""
    catalog = catalog or criteria_catalog()
    if fixed:
        j7 = {i["id"] for i in catalog["items"] if i["id"].startswith("J7-")}
        facts = "\n".join(f"- ({f.get('position')}) {f.get('text')}" for f in fixed.get("facts") or [])
        asked = "\n".join(f"- {q[:200]}" for q in existing) or "- (없음)"
        return "\n\n".join([
            f"당신은 한국어 AI 답변 품질 평가에 쓸 긴 참조 자료 질문을 설계합니다. 결과는 JSON 하나로만 답합니다.\n\n"
            f"# 목표\n이미 정한 긴 문서(주제: {fixed.get('topic')})의 아래 사실 문장(facts, 괄호는 문서 속 위치 0~1)만으로 답이 정해지는 질문을 "
            f"정확히 {count}개 더 만듭니다. 질문마다 J7-1.a(중간·뒷부분 사실을 다른 표현으로 묻기)와 J7-2.a(3곳 이상 사실 연결·비교·집계)를 함께 묻고, "
            f"앞서 만든 질문과 다른 사실 묶음을 씁니다. 단일 턴, attachments·long_paste·long_reference 는 쓰지 않습니다.\n\n# facts\n{facts}\n\n# 앞서 만든 질문\n{asked}",
            _DISCRIMINATION, _FORMAT, "# 점검 목록 (J7 항목)\n" + "\n".join(_catalog_lines(catalog, j7)),
            *([_note_block(note)] if _note_block(note) else []),
            f"# 질문 개수 (반드시 지킴)\nquestions 배열에 정확히 {count}개를 넣습니다."])
    j7 = {i["id"] for i in catalog["items"] if i["id"].startswith("J7-")}
    facts = min(80, max(6, 3 * count))
    parts = [f"당신은 한국어 AI 답변 품질 평가에 쓸 긴 참조 자료 질문을 설계합니다. 결과는 JSON 하나로만 답합니다.\n\n"
             f"# 목표\n{long_context.MIN_TOKENS:,}토큰(한국어 약 38만 자) 이상의 긴 문서 하나를 시스템이 만들고, 그 같은 문서를 참조 자료로 주는 질문을 "
             f"정확히 {count}개 만듭니다. 문서 주제와, 문서 속 정해진 위치(position 0~1)에 넣을 사실 문장(facts)을 약 {facts}개 씁니다. "
             f"질문은 이 facts 만으로 답이 정해지게 씁니다(문서의 나머지 본문은 시스템이 지어 씁니다).\n"
             f"- 질문마다 J7-1.a(자료 중간·뒷부분의 사실을 질문과 다른 표현으로 묻기)와 J7-2.a(3곳 이상에 흩어진 사실을 연결·비교·집계)를 함께 묻습니다.\n"
             f"- 질문마다 서로 다른 사실 묶음을 쓰고, 문서 앞·중간·뒤를 고르게 씁니다. 자료에 없는 정보를 묻는 함정(지어내면 FAIL)도 일부 넣습니다.\n"
             f"- 질문은 단일 턴입니다. attachments·long_paste 는 쓰지 않습니다.\n\n"
             "# 출력 형식 (JSON 하나만, 설명·코드 펜스 없이)\n"
             '{"long_reference": {"topic": "문서 주제", "facts": [{"text": "사실 문장", "position": 0.37}, ...]},\n'
             ' "questions": [{"id": "Q01", "type": "롱컨텍스트", "topic": "주제", "target_items": ["J7-1.a", "J7-2.a"], "difficulty": "쉬움|보통|어려움",\n'
             '   "trap": "어떤 답이 FAIL 이 되는지 한 줄", "turns": [{"question": "질문 원문"}]}, ...]}',
             _DISCRIMINATION,
             "# 점검 목록 (J7 항목)\n" + "\n".join(_catalog_lines(catalog, j7))]
    if _note_block(note):
        parts.append(_note_block(note))
    parts.append(f"# 질문 개수 (반드시 지킴)\nquestions 배열에 정확히 {count}개를 넣습니다. 답하기 전에 개수를 세어 확인합니다.")
    return "\n\n".join(parts)


# --- 2. 계획 응답 읽기 -------------------------------------------------------------------

def _missing_closers(text: str) -> Optional[str]:
    """끝에서 닫히지 않은 괄호를 순서대로 닫는 글자 — 문자열 안은 건너뛴다. 괄호 짝이 어긋나 있으면 None (고치지 않음)."""
    stack: List[str] = []
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack.pop() != ch:
                return None
    return None if in_string or not stack else "".join(reversed(stack))


def _json_object(text: str, warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    text = (text or "").strip()
    start = text.find("{")
    if start < 0:
        raise GenerationError("GPT 응답에서 JSON 객체를 찾지 못했습니다.")
    try:   # 첫 완전한 객체만 읽는다 — 펜스·앞뒤 글·끝에 더 붙은 괄호(실제 GPT 응답에서 확인)가 있어도 앞 객체는 쓸 수 있다
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError as exc:
        # 끝 괄호만 모자란 응답(2026-10-14 실제 GPT 계획: 마지막 '}' 하나 빠짐)은 모자란 괄호만 닫아 읽는다 — 내용은 바꾸지 않는다.
        # 바깥 코드 펜스(앞에 설명 글이 있어도)는 여는 펜스 바로 뒤의 '{' 부터 응답 끝의 닫는 펜스 앞까지 — JSON 안의 ``` 는 자르지 않는다 (Codex)
        body = text[start:]
        opening = re.search(r"```(?:json)?\s*(?=\{)", text)
        if opening:
            body = text[opening.end():]
            body = body[:-3].rstrip() if body.endswith("```") else body
        closers = _missing_closers(body)
        try:
            if closers is None:
                raise ValueError
            value, _ = json.JSONDecoder().raw_decode(body + closers)
        except ValueError:
            raise GenerationError(f"GPT 응답 JSON 을 읽지 못했습니다: {exc}") from None
        if warnings is not None:
            warnings.append(f"GPT 응답 끝에 닫는 괄호 {len(closers)}개가 빠져 있어 채워 읽었습니다.")
    if not isinstance(value, dict):
        raise GenerationError("GPT 응답이 JSON 객체가 아닙니다.")
    return value


def _clean_spec(key: str, spec: Dict[str, Any], where: str, warnings: List[str]) -> Dict[str, Any]:
    """긴 자료 명세의 중첩 필드 모양을 맞춘다 — 목록이 아닌 must_include·facts, 글이 아닌 text, 유한한 수가 아닌 position 은 빼고
    경고로 남긴다 (Codex 리뷰: 그대로 두면 질문 하나의 틀린 값이 생성 전체를 중단시켰다)."""
    out: Dict[str, Any] = {"topic": str(spec.get("topic") or "")}
    if key == "long_paste":
        raw = spec.get("must_include")
        if raw is not None and not isinstance(raw, list):
            warnings.append(f"{where}: long_paste.must_include 가 목록이 아니라 뺐습니다.")
        out["must_include"] = [str(x).strip() for x in raw if isinstance(x, (str, int, float)) and str(x).strip()] if isinstance(raw, list) else []
        return out
    raw = spec.get("facts")
    if raw is not None and not isinstance(raw, list):
        warnings.append(f"{where}: long_reference.facts 가 목록이 아니라 뺐습니다.")
    facts = []
    for n, fact in enumerate(raw if isinstance(raw, list) else [], 1):
        text = fact.get("text") if isinstance(fact, dict) else None
        position = fact.get("position", 0.5) if isinstance(fact, dict) else None
        try:
            value = float(position) if not isinstance(position, bool) else math.nan
        except (TypeError, ValueError):
            value = math.nan
        if not isinstance(text, str) or not text.strip() or not math.isfinite(value):
            warnings.append(f"{where}: 사실 문장 {n}번의 모양(text 글·position 0~1 수)이 틀려 뺐습니다.")
            continue
        facts.append({"text": text.strip(), "position": min(max(value, 0.0), 1.0)})
    out["facts"] = facts
    return out


def parse_plan(text: str, known_ids: Optional[set] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """GPT 계획 → (질문 목록, 경고). 모양이 틀린 질문·턴·첨부는 버리고 경고로 남긴다 (지어내 채우지 않는다)."""
    repaired: List[str] = []
    value = _json_object(text, repaired)
    raw = value.get("questions")
    if not isinstance(raw, list) or not raw:
        raise GenerationError("GPT 응답에 questions 목록이 없습니다.")
    if known_ids is None:
        catalog = criteria_catalog()
        known = {c["id"] for c in catalog["code"]} | {k["id"] for c in catalog["code"] for k in c["kinds"]} | {i.id for i in ITEMS}
    else:
        known = known_ids
    questions, warnings = [], list(repaired)
    for index, entry in enumerate(raw, 1):
        if len(questions) >= MAX_PLAN_QUESTIONS:   # 비용 상한 — 계획이 지나치게 크면 앞에서부터만 쓴다
            warnings.append(f"질문이 {MAX_PLAN_QUESTIONS}개를 넘어 {index}번부터는 뺐습니다.")
            break
        if not isinstance(entry, dict) or not isinstance(entry.get("turns"), list) or not entry["turns"]:
            warnings.append(f"{index}번 질문: turns 가 없어 뺐습니다.")
            continue
        turns = []
        pastes = 0
        for t_index, turn in enumerate(entry["turns"][:MAX_PLAN_TURNS], 1):
            question = turn.get("question") if isinstance(turn, dict) else None
            if not isinstance(question, str) or not question.strip():
                warnings.append(f"{index}번 질문 {t_index}턴: question 이 비어 뺐습니다.")
                continue
            clean: Dict[str, Any] = {"question": question.strip()}
            raw_files = turn.get("attachments")
            if raw_files is not None and not isinstance(raw_files, list):
                warnings.append(f"{index}번 질문 {t_index}턴: attachments 가 목록이 아니라 뺐습니다.")
            files = [a for a in raw_files if isinstance(a, dict)] if isinstance(raw_files, list) else []
            if files:
                clean["attachments"] = files
            for key in ("long_reference", "long_paste"):
                if key == "long_paste" and isinstance(turn.get(key), dict) and pastes >= MAX_PASTE_TURNS:
                    warnings.append(f"{index}번 질문 {t_index}턴: 자료를 붙이는 턴이 {MAX_PASTE_TURNS}개를 넘어 long_paste 를 뺐습니다.")
                elif isinstance(turn.get(key), dict):
                    clean[key] = _clean_spec(key, turn[key], f"{index}번 질문 {t_index}턴", warnings)
                    pastes += key == "long_paste"
                elif turn.get(key) is not None:
                    warnings.append(f"{index}번 질문 {t_index}턴: {key} 가 객체가 아니라 뺐습니다.")
            turns.append(clean)
        if not turns:
            continue
        raw_targets = entry.get("target_items")
        targets = [str(x) for x in raw_targets if str(x) in known] if isinstance(raw_targets, list) else []
        difficulty = entry.get("difficulty") if entry.get("difficulty") in DIFFICULTIES else ""   # 셋 중 하나가 아니면 비움(지어내지 않음)
        trap = str(entry.get("trap") or "").strip()[:300] if isinstance(entry.get("trap"), str) else ""
        questions.append({"type": str(entry.get("type") or ""), "topic": str(entry.get("topic") or ""), "target_items": targets,
                          "difficulty": difficulty, "trap": trap, "turns": turns})
    if not questions:
        raise GenerationError("GPT 응답에 쓸 수 있는 질문이 없습니다.")
    return questions, warnings


# --- 3. 질문 세트 만들기 ------------------------------------------------------------------

class QuestionSetBuilder:
    """계획 → 첨부·긴 자료 → 점검·보충 → 질문 세트. ``llm(prompt, max_tokens) -> text`` 만 바꾸면 시험할 수 있다."""

    def __init__(self, llm: LLM, out_dir: Path, *, progress: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 cancelled: Optional[Callable[[], bool]] = None, token_count: Callable[[str], int] = long_context.count_tokens):
        self.llm, self.out_dir, self.files_dir = llm, out_dir, out_dir / "files"
        self.progress = progress or (lambda phase, data: None)
        self.cancelled = cancelled or (lambda: False)
        self.count = token_count
        self.warnings: List[str] = []
        self.calls = 0
        self.max_calls = MAX_CALLS
        self.shared_reference: Optional[str] = None   # 긴 문서 질문(J7)이 함께 쓰는 문서 — 한 번만 만든다
        self.shared_spec: Optional[Dict[str, Any]] = None
        self.shared_questions: List[str] = []
        self.long_references = 0
        self.made: List[Dict[str, Any]] = []      # 만든 질문 — 생성 중 화면에 보인다 (사용자 요청 2026-10-14: 어떤 질문이 만들어지는지)
        self.pending: List[Dict[str, Any]] = []   # 계획만 된 질문(만드는 중 포함)
        self._calls_lock = threading.Lock()   # 긴 자료는 여러 스레드가 동시에 부른다

    def _ask(self, prompt: str, max_tokens: int, partial_ok: bool = False) -> str:
        if self.cancelled():
            raise Cancelled("생성을 중단했습니다.")
        with self._calls_lock:   # 작업 전체 호출 상한 — 요청 전에 센다 (재시도는 gpt_llm 안에서 1회)
            if self.calls >= self.max_calls:
                raise GenerationError(f"GPT 호출이 작업 한도({self.max_calls}회)에 닿았습니다.")
            self.calls += 1
        try:
            return self.llm(prompt, max_tokens)
        except IncompleteResponse as exc:
            if partial_ok and exc.text.strip():   # 긴 자료 조각은 잘려도 이어 붙이면 된다 — 길이는 뒤에서 다시 잰다
                return exc.text
            raise

    # 긴 자료 -------------------------------------------------------------------------
    def long_reference(self, spec: Dict[str, Any]) -> str:
        """J7 — 입력이 MIN_TOKENS 이상이 되도록 조각을 나눠 쓰게 하고, 사실 문장을 정한 위치(0~1)에 넣는다."""
        topic = str(spec.get("topic") or "업무 자료")
        with self._calls_lock:   # 비용 상한 — 긴 참조 자료(조각 수십 회 호출)는 질문 세트마다 MAX_LONG_REFERENCES 개까지
            if self.long_references >= MAX_LONG_REFERENCES:
                raise GenerationError(f"긴 참조 자료는 {MAX_LONG_REFERENCES}개까지만 만듭니다.")
            self.long_references += 1

        def position(value: Any) -> float:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return 0.5
            return min(max(number, 0.0), 1.0) if math.isfinite(number) else 0.5   # NaN·무한은 자리 계산을 깨뜨린다
        raw_facts = spec.get("facts") if isinstance(spec.get("facts"), list) else []
        facts = sorted(((position(f.get("position", 0.5)), str(f.get("text") or "").strip()) for f in raw_facts
                        if isinstance(f, dict) and str(f.get("text") or "").strip()), key=lambda x: x[0])
        target = int(long_context.MIN_TOKENS * 1.03)
        parts = max(1, math.ceil(target / (CHUNK_CHARS * 0.7)))
        chunks: List[str] = []
        with ThreadPoolExecutor(max_workers=MATERIAL_WORKERS) as pool:
            chunks = list(pool.map(lambda n: self._ask(_material_prompt(topic, n, parts, CHUNK_CHARS, [t for _, t in facts]), MATERIAL_TOKENS, True),
                                   range(1, parts + 1)))
        text = "\n\n".join(c.strip() for c in chunks)
        extra = 0
        while self.count(text) < target and extra < parts:   # 모자라면 조각을 더 쓴다
            extra += 1
            text += "\n\n" + self._ask(_material_prompt(topic, parts + extra, parts + extra, CHUNK_CHARS, [t for _, t in facts]),
                                       MATERIAL_TOKENS, True).strip()
        if self.count(text) < long_context.MIN_TOKENS:
            raise GenerationError(f"긴 참조 자료가 {long_context.MIN_TOKENS:,}토큰에 못 미칩니다 ({self.count(text):,}).")
        paragraphs = [p for p in text.split("\n\n") if p.strip()]
        for position, fact in reversed(facts):   # 뒤에서부터 넣어야 앞 위치가 밀리지 않는다
            paragraphs.insert(min(len(paragraphs), max(0, round(len(paragraphs) * min(max(position, 0.0), 1.0)))), fact)
        return "\n\n".join(paragraphs)

    def long_paste(self, spec: Dict[str, Any], question: str) -> str:
        """J6 — 자료가 필요한 턴에 붙여 넣을 자료(약 PASTE_CHARS 자)를 만들어 표시로 감싸고, 요청은 그 뒤에 둔다.
        멀티턴 길이(대화 전체 MIN_TOTAL_TOKENS)는 답변까지 합쳐 재므로 턴 하나를 자료로 채우지 않는다 (multi-turn-v2)."""
        material = self._ask(_paste_prompt(spec, PASTE_CHARS), MATERIAL_TOKENS, True).strip()
        return f"{PASTE_BEGIN}\n{escape_paste_markers(material)}\n{PASTE_END}\n\n{question}"

    # 질문 한 개 → id/turns 묶음 -------------------------------------------------------
    def materialize(self, number: int, plan: Dict[str, Any]) -> Dict[str, Any]:
        # 턴마다 붙여 넣을 자료(J6)는 서로 기다릴 이유가 없어 동시에 만든다 — 10턴 대화가 한 턴씩일 때보다 훨씬 빨리 끝난다
        pasted: Dict[int, str] = {}
        jobs = [(i, turn) for i, turn in enumerate(plan["turns"]) if turn.get("long_paste")]
        if jobs:
            with ThreadPoolExecutor(max_workers=MATERIAL_WORKERS) as pool:
                texts = pool.map(lambda job: self.long_paste(job[1]["long_paste"], job[1]["question"]), jobs)
                pasted = dict(zip((i for i, _ in jobs), texts))
        turns = []
        for t_index, turn in enumerate(plan["turns"], 1):
            question = pasted.get(t_index - 1, turn["question"])
            out: Dict[str, Any] = {"turn": t_index}
            if turn.get("shared_reference"):
                out["reference"] = self.shared_reference
            elif turn.get("long_reference"):
                out["reference"] = self.long_reference(turn["long_reference"])
            files = []
            for spec in turn.get("attachments") or []:
                content = spec.get("content")
                if isinstance(content, str) and len(content) > MAX_ATTACHMENT_CHARS:
                    spec = dict(spec, content=content[:MAX_ATTACHMENT_CHARS])
                try:
                    files.append(render_attachment(spec, self.files_dir))
                except (AttachmentError, OSError, ValueError) as exc:
                    self.warnings.append(f"Q{number:02d} {t_index}턴 첨부 {spec.get('name')}: {exc}")
            if files:
                out["attachments"] = files
            out["question"] = question
            turns.append(out)
        group = {"id": number, "type": plan.get("type") or "", "topic": plan.get("topic") or "",   # 업로드 변환이 문항 ID Q{id}_T{턴} 을 만든다
                 "target_items": plan.get("target_items") or [], "difficulty": plan.get("difficulty") or "", "trap": plan.get("trap") or "",
                 "turns": turns}
        self.made.append(preview_row(group, "made"))
        self.show()
        return group

    def show(self) -> None:
        """생성 중 화면용 질문 목록(만든 질문 + 계획 질문)을 진행 보고로 넘긴다 — 단계·문구는 바꾸지 않는다."""
        self.progress(None, {"questions": self.made + self.pending})

    # 전용 단계 ------------------------------------------------------------------------
    @staticmethod
    def _only(plans: List[Dict[str, Any]], keep: Tuple[str, ...], turns: Optional[int] = None) -> List[Dict[str, Any]]:
        """계획의 턴에서 ``keep`` 밖의 자료 필드(첨부·긴 자료·붙여 넣기)를 뺀다 — 전용 단계가 정한 모양만 남긴다."""
        fields = ("attachments", "long_reference", "long_paste")
        return [dict(plan, turns=[{k: v for k, v in t.items() if k not in fields or k in keep} for t in plan["turns"][:turns]]) for plan in plans]

    def long_context_questions(self, count: int, catalog: Dict[str, List[Dict[str, str]]], note: str, start: int) -> List[Dict[str, Any]]:
        """긴 문서 하나 + 그 문서를 함께 쓰는 단일 턴 질문 ``count`` 개 (J7). 문서를 못 만들면 경고만 남기고 빈 목록."""
        plans: List[Dict[str, Any]] = []
        spec: Optional[Dict[str, Any]] = self.shared_spec   # 이미 만든 문서가 있으면 그 facts 로 질문만 더 만든다 (문서는 세트당 1개)
        rounds = 0
        while len(plans) < count and rounds < count // LONG_CONTEXT_BATCH + 3:
            rounds += 1
            want = min(LONG_CONTEXT_BATCH, count - len(plans))
            self.progress("long_context", {"message": f"긴 문서 질문 {want}개 계획 요청 (지금 {len(plans)}개 / {count}개)"})
            try:
                asked = self.shared_questions + [p["turns"][0]["question"] for p in plans]
                value = _json_object(self._ask(long_context_prompt(want, catalog, note, spec, asked), PLAN_TOKENS))
                if spec is None:
                    if not isinstance(value.get("long_reference"), dict):
                        raise GenerationError("긴 문서 명세(long_reference)가 없습니다.")
                    spec = _clean_spec("long_reference", value["long_reference"], "긴 문서", self.warnings)
                got, warnings = parse_plan(json.dumps({"questions": value.get("questions")}, ensure_ascii=False))
            except Cancelled:
                raise
            except GenerationError as exc:
                self.warnings.append(f"긴 문서 질문 계획 {rounds}회차: {exc}")
                continue
            self.warnings += warnings
            plans += self._only(got, (), turns=1)[:want]
        if not plans or spec is None:
            self.warnings.append("긴 문서 질문을 만들지 못했습니다 (J7).")
            return []
        self.pending = [preview_row(p, "planned") for p in plans]   # 긴 문서를 만드는 몇 분 동안에도 계획된 질문이 보이게 (Codex)
        self.show()
        if self.shared_reference is None:
            self.progress("long_context", {"message": "긴 문서 만드는 중 (조각 여러 개)"})
            try:
                self.shared_reference = self.long_reference(spec)
            except Cancelled:
                raise
            except GenerationError as exc:
                self.warnings.append(f"긴 문서를 만들지 못해 긴 문서 질문을 뺐습니다: {exc}")
                self.pending = []
                self.show()
                return []
            self.shared_spec = spec
        self.shared_questions += [p["turns"][0]["question"] for p in plans]
        groups = []
        for index, plan in enumerate(plans):
            plan["turns"][0]["shared_reference"] = True
            self.pending = [preview_row(p, "planned") for p in plans[index + 1:]]
            groups.append(self.materialize(start + len(groups), plan))
        self.pending = []
        self.show()
        return groups

    def multi_turn_conversations(self, count: int, catalog: Dict[str, List[Dict[str, str]]], note: str, topics: List[str],
                                 start: int) -> List[Dict[str, Any]]:
        """10턴 대화 ``count`` 개 (J6) — 턴이 모자란 대화는 멀티턴이 아니므로 빼고 다시 요청한다. 턴마다 답변 분량 요구를 붙인다."""
        groups: List[Dict[str, Any]] = []
        rounds = 0
        while len(groups) < count and rounds < count // MULTI_TURN_BATCH + 4:
            rounds += 1
            want = min(MULTI_TURN_BATCH, count - len(groups))
            self.progress("multi_turn", {"message": f"10턴 대화 {want}개 계획 요청 (지금 {len(groups)}개 / {count}개)", "done": len(groups)})
            try:
                got, warnings = parse_plan(self._ask(multi_turn_prompt(want, topics + [g["topic"] for g in groups], catalog, note), PLAN_TOKENS))
            except Cancelled:
                raise
            except GenerationError as exc:
                self.warnings.append(f"10턴 대화 계획 {rounds}회차: {exc}")
                continue
            self.warnings += warnings
            planned = self._only(got, ("long_paste",))
            for index, plan in enumerate(planned):
                self.pending = [preview_row(plan, "making")] + [preview_row(p, "planned") for p in planned[index + 1:]]   # (Codex)
                self.show()
                if len(groups) >= count:
                    break
                if len(plan["turns"]) < multi_turn.MIN_TURNS:
                    self.warnings.append(f"'{plan.get('topic')}' 대화가 {len(plan['turns'])}턴이라 뺐습니다 (멀티턴은 {multi_turn.MIN_TURNS}턴 이상).")
                    continue
                try:
                    groups.append(self.materialize(start + len(groups), plan))
                except Cancelled:
                    raise
                except GenerationError as exc:
                    self.warnings.append(f"'{plan.get('topic')}' 대화를 만들지 못해 뺐습니다: {exc}")
            self.pending = []   # 다음 회차 계획을 받는 동안 앞 회차 질문이 '만드는 중'으로 남지 않게 (Codex)
            self.show()
        self.pending = []
        self.show()
        if len(groups) < count:
            self.warnings.append(f"10턴 대화를 {count}개 중 {len(groups)}개만 만들었습니다.")
        return groups

    # 전체 ---------------------------------------------------------------------------
    def build(self, note: str = "", target_questions: Optional[int] = None, weak_units: Sequence[Dict[str, str]] = (),
              multi_turn_count: Optional[int] = None, long_context_count: Optional[int] = None,
              per_target: Optional[int] = None) -> Dict[str, Any]:
        """계획 → 만들기 → 빠진 항목·첨부 종류 보충 → (목표 질문 수에 못 미치면) 변별력·적게 덮인 항목으로 질문 추가."""
        catalog = criteria_catalog()
        if target_questions:   # 질문 하나에 계획·자료 호출이 몇 번씩 든다 — 수백 개를 지정해도 호출 상한에 먼저 막히지 않게
            self.max_calls = max(MAX_CALLS, 2 * target_questions + 150)
        if per_target and not target_questions:   # 질문 수 없이 항목별 목표 적용 수만 — 다 채울 때까지 (상한 MAX_PLAN_QUESTIONS)
            self.max_calls = max(MAX_CALLS, 2 * MAX_PLAN_QUESTIONS + 150)
        multi_count, long_count = split_counts(target_questions, multi_turn_count, long_context_count, per_target)
        dedicated = multi_count is not None   # 질문 수를 지정하면 10턴 대화·긴 문서 질문을 전용 단계에서 정한 수만큼 먼저 만든다
        # 세트 질문 수 상한 — 질문 수를 정했으면 그 수, per_target 만이면 MAX_PLAN_QUESTIONS. 전용·계획·보충·추가 모든 단계가 이 하나를 나눠 쓴다
        cap = target_questions or (MAX_PLAN_QUESTIONS if per_target else None)
        general = general_catalog(catalog) if dedicated else catalog
        self.out_dir.mkdir(parents=True, exist_ok=True)
        groups: List[Dict[str, Any]] = []
        if dedicated:
            if long_count:
                groups += self.long_context_questions(long_count, catalog, note, len(groups) + 1)
            if multi_count:
                groups += self.multi_turn_conversations(multi_count, catalog, note, [g["topic"] for g in groups], len(groups) + 1)
            # J6·J7 부족분은 일반 질문이 자리를 채우기 전에 전용 단계로 다시 만든다 — 일반 계획은 J6·J7 을 만들지 않으므로 지금 센 수가 끝까지
            # 같다 (Codex 리뷰 2회: 뒤에서 채우면 일반 질문이 상한을 다 써 버려 복구가 돌지 못했다)
            for _ in range(3):
                if not per_target or not cap or cap - len(groups) <= 1:   # 다른 항목에 쓸 질문 1개는 남긴다
                    break
                full = target_counts(check(groups)) if groups else {}
                need = {prefix: max(0, per_target - min((n for i, n in full.items() if i.startswith(prefix)), default=0))
                        for prefix in DEDICATED_ITEMS}
                if (multi_count == 0 and long_count == 0) or not ((need["J6-"] and multi_count) or (need["J7-"] and long_count)):
                    break
                before = len(groups)
                if need["J7-"] and long_count:   # 사용자가 0 으로 정한 단계는 다시 돌리지 않는다
                    groups += self.long_context_questions(min(need["J7-"], cap - len(groups) - 1), catalog, note, len(groups) + 1)
                if need["J6-"] and multi_count and cap - len(groups) > 1:
                    groups += self.multi_turn_conversations(min(need["J6-"], cap - len(groups) - 1), catalog, note,
                                                            [g["topic"] for g in groups], len(groups) + 1)
                if len(groups) == before:
                    break
        rounds = 0

        def budget() -> Optional[int]:   # 상한까지 남은 수 — 계획·보충 단계마다 만들기 전에 제한한다 (Codex 리뷰: 사후 경고로는 비용을 못 막음)
            return None if not cap else max(0, cap - len(groups))

        def general_only(plans: List[Dict[str, Any]]) -> List[Dict[str, Any]]:   # 전용 단계가 있으면 일반 계획의 긴 자료는 쓰지 않는다 (세트당 1개)
            if dedicated and any(t.get("long_reference") for p in plans for t in p["turns"]):
                self.warnings.append("일반 계획의 긴 참조 자료(long_reference)는 뺐습니다 — 긴 문서 질문은 전용 단계가 만듭니다.")
            return self._only(plans, ("attachments", "long_paste")) if dedicated else plans

        self.progress("plan", {"message": "GPT 가 질문 계획을 쓰는 중"})
        # 첫 계획은 PLAN_BATCH 개까지 — 모든 항목을 덮게 하고, 나머지는 아래 '질문 추가'가 PLAN_BATCH 개씩 채운다
        first = min(budget(), PLAN_BATCH) if cap else None
        if first == 0:   # 전용 단계만으로 목표 수를 채움
            plans = []
        else:
            for attempt in (1, 2):   # 계획 JSON 이 깨지면 한 번 더 요청한다 — 첫 계획이 없으면 세트를 만들 수 없다
                raw = self._ask(plan_prompt(catalog, note, target_questions=first, weak_units=weak_units, dedicated=dedicated), PLAN_TOKENS)
                (self.out_dir / ("plan.txt" if attempt == 1 else "plan-retry.txt")).write_text(raw, encoding="utf-8")   # GPT 가 쓴 계획 원문
                try:
                    plans, warnings = parse_plan(raw)
                    break
                except GenerationError as exc:
                    if attempt == 2:
                        raise
                    self.warnings.append(f"첫 계획을 읽지 못해 한 번 더 요청했습니다: {exc}")
            self.warnings += warnings
            plans = general_only(plans)

        while True:
            if budget() is not None and len(plans) > budget():
                self.warnings.append(f"질문 수 상한 {cap}개를 넘는 계획 질문 {len(plans) - budget()}개는 만들지 않았습니다.")
                plans = plans[:budget()]
            for index, plan in enumerate(plans):
                self.pending = [preview_row(plan, "making")] + [preview_row(p, "planned") for p in plans[index + 1:]]
                self.show()
                self.progress("materialize", {"message": f"질문 {len(groups) + 1} 만드는 중 (첨부·긴 자료)", "done": len(groups)})
                try:
                    groups.append(self.materialize(len(groups) + 1, plan))
                except Cancelled:
                    raise
                except GenerationError as exc:   # 그 질문만 빼고 계속한다 — 빠진 항목은 점검·보충이 다시 잡는다
                    self.warnings.append(f"'{plan.get('topic') or plan['turns'][0]['question'][:30]}' 질문을 만들지 못해 뺐습니다: {exc}")
            self.pending = []
            self.show()
            report = check(groups)
            missing = [t for t, n in target_counts(report, dedicated).items() if n == 0]   # 한 번도 적용되지 않은 세부 기준
            have = attachment_types(groups)
            missing_types = tuple(kind for kind in ATTACHMENT_TYPES if kind not in have)   # 첨부 종류마다 질문 1개 이상 (사용자 요청 '다양하게')
            if (not missing and not missing_types) or rounds >= SUPPLEMENT_ROUNDS:
                break
            if budget() == 0:   # 지정한 수 안에 모든 항목을 덮지 못함 — 더 만들지 않고 빠진 것을 보고한다
                self.warnings.append(f"질문 수 {cap}개 안에 모든 항목을 덮지 못했습니다 — 빠진 항목: {', '.join(missing) or '없음'}"
                                     + (f", 빠진 첨부 종류: {', '.join(missing_types)}" if missing_types else ""))
                break
            rounds += 1
            self.progress("supplement", {"message": f"빠진 항목 {len(missing)}개·첨부 종류 {len(missing_types)}개 보충 요청 ({rounds}회차)",
                                         "missing": missing + [f"첨부:{kind}" for kind in missing_types]})
            try:
                plans, warnings = parse_plan(self._ask(supplement_prompt(missing, [g["topic"] for g in groups], catalog, missing_types, note=note),
                                                       PLAN_TOKENS))
                plans = general_only(plans)
            except Cancelled:
                raise
            except GenerationError as exc:
                self.warnings.append(f"보충 {rounds}회차: {exc}")
                break
            self.warnings += warnings
        if not groups:
            raise GenerationError("만든 질문이 없습니다: " + " / ".join(self.warnings[-3:]))
        extra_rounds = 0
        # 목표 수 채우기 — 변별력 보강·적게 덮인 항목 우선, 응답 하나에 PLAN_BATCH 개씩
        def short() -> int:   # 항목별 목표 적용 수에 모자란 적용 수 합
            return sum(per_target - n for n in target_counts(report, dedicated).values() if n < per_target) if per_target else 0

        def more() -> bool:
            if target_questions:
                return len(groups) < target_questions
            return bool(per_target) and short() > 0 and bool(budget())

        while more() and extra_rounds < MAX_EXTRA_ROUNDS:
            limit = budget() or 0
            # 질문 하나가 기준 여러 개(평균 약 3개)에 적용되므로 모자란 수의 3분의 1 정도씩 — 목표 질문 수가 있으면 그 수까지
            count = min(PLAN_BATCH, limit) if target_questions else min(PLAN_BATCH, limit, max(5, math.ceil(short() / 3)))
            before = len(groups)
            self.progress("extra", {"message": (f"목표 {target_questions}개에 맞춰" if target_questions else f"기준마다 {per_target}개 적용까지 (모자란 적용 {short()})")
                                               + f" 질문 {count}개 추가 요청 (지금 {len(groups)}개)", "count": count,
                                    "done": len(groups)})
            try:
                low = _low_coverage(report, 30 if per_target else 15, dedicated, per_target)
                plans, warnings = parse_plan(self._ask(extra_prompt(count, len(groups), target_questions, [g["topic"] for g in groups], general,
                                                                    weak_units, low, note=note,
                                                                    long_reference_ok=not dedicated and self.long_references < MAX_LONG_REFERENCES,
                                                                    per_target=per_target), PLAN_TOKENS))
                plans = general_only(plans)
                self.warnings += warnings
                extra_rounds += 1
                planned = plans[:count]
                for index, plan in enumerate(planned):
                    self.pending = [preview_row(plan, "making")] + [preview_row(p, "planned") for p in planned[index + 1:]]   # (Codex)
                    self.show()
                    try:
                        groups.append(self.materialize(len(groups) + 1, plan))
                    except Cancelled:
                        raise
                    except GenerationError as exc:
                        self.warnings.append(f"'{plan.get('topic') or plan['turns'][0]['question'][:30]}' 질문을 만들지 못해 뺐습니다: {exc}")
                self.pending = []
                self.show()
                report = check(groups)
            except Cancelled:
                raise
            except GenerationError as exc:
                self.warnings.append(f"질문 추가 {extra_rounds + 1}회차: {exc}")
                break
            if len(groups) == before:   # 이번 회차에 하나도 못 만듦 — 같은 요청을 되풀이하지 않는다
                break
        short_targets = ({t: n for t, n in target_counts(report).items() if n < per_target} if per_target else {})
        if short_targets:   # 상한·실패로 다 채우지 못한 기준 — 숨기지 않고 알린다
            self.warnings.append(f"기준마다 {per_target}개 적용을 채우지 못한 세부 기준 {len(short_targets)}개: "
                                 + ", ".join(f"{t}={n}" for t, n in sorted(short_targets.items(), key=lambda x: (x[1], x[0]))[:40]))
        if target_questions and len(groups) != target_questions:
            self.warnings.append(f"목표 질문 수 {target_questions}개와 달리 {len(groups)}개를 만들었습니다"
                                 + (" (모든 항목을 덮는 데 더 필요했습니다)." if len(groups) > target_questions else "."))
        return {"groups": groups, "check": report, "rounds": rounds, "extra_rounds": extra_rounds, "short": short_targets}


def target_counts(report: Dict[str, Any], dedicated: bool = False) -> Dict[str, int]:
    """생성기가 채우는 세부 기준별 적용 질문 수 — CODE 는 질문 조건 종류('검사:종류'), 의미 평가는 점검 항목.
    답변으로만 정해지는 항목은 빼고, ``dedicated`` 면 전용 단계가 맡는 J6·J7 도 뺀다."""
    coverage = report.get("coverage") or {}
    skip = set(coverage.get("answer_dependent_items") or [])
    counts = dict(coverage.get("kinds") or {})
    counts.update({iid: n for iid, n in (coverage.get("items") or {}).items() if iid not in skip and not (dedicated and _dedicated(iid))})
    return counts


def _low_coverage(report: Dict[str, Any], limit: int = 15, dedicated: bool = False, per_target: Optional[int] = None) -> List[Tuple[str, int]]:
    """적용 질문이 가장 적은 세부 기준 — 추가 질문이 노릴 곳. ``per_target`` 이 있으면 그 수에 못 미친 기준만."""
    counts = sorted(target_counts(report, dedicated).items(), key=lambda x: (x[1], x[0]))
    if per_target:
        counts = [(t, n) for t, n in counts if n < per_target]
    return counts[:limit]


PREVIEW_CHARS = 500   # 생성 중 목록의 턴 글자 수 상한 — 2초마다 받는 상태가 커지지 않게 (다 만든 뒤 확인 목록은 원문 전체)


def preview_row(item: Dict[str, Any], state: str) -> Dict[str, Any]:
    """만든 질문(id/turns 묶음)이나 계획(plan) → 화면 질문 목록 한 줄 (확인 목록과 같은 모양 + state: made·making·planned)."""
    turns = []
    for turn in item.get("turns") or []:
        text = str(turn.get("question") or "")
        ref = turn.get("reference")
        turns.append({"question": text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + " …",
                      "reference_chars": len(ref) if isinstance(ref, str) else 0, "long_reference": bool(turn.get("long_reference")),
                      "attachments": [str(a.get("name")) for a in turn.get("attachments") or [] if isinstance(a, dict)]})
    return {**{key: item.get(key) for key in ("id", "type", "topic", "difficulty", "trap", "target_items")}, "turns": turns, "state": state}


def flatten(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """id/turns 묶음 → 업로드 화면과 같은 행 목록 (dataset_import.id_turn_rows)."""
    from .dataset_import import id_turn_rows
    return id_turn_rows([{"id": g["id"], "turns": [dict(t, answer=t.get("answer", "")) for t in g["turns"]]} for g in groups])


def check(groups: List[Dict[str, Any]]) -> Dict[str, Any]:
    """질문 세트 점검과 같은 규칙의 기준·검사·항목별 적용 수 (모델 호출 없음)."""
    from tools.check_question_set import check_items
    from code_eval.criteria_registry import registry
    report = check_items(flatten(groups), "items")
    # 평가 항목 구조(CODE 대분류·소분류·검사 ⇒ 질문 조건, 의미 기준·세부 기준 ⇒ 점검 항목) — 화면의 '총 N개 항목 검사' 줄
    report["coverage"]["taxonomy"] = registry()["targets"]["counts"]
    return report


# --- 3-1. 질문 직접 넣기 (사용자 결정 2026-10-14: 질문만 넣으면 AIONA 는 웹 자동화, 타 모델은 API 로 답을 받아 평가) ------
QUESTION_INPUT_VERSION = "question-input-v1"
# 고정 질문 세트 (사용자 결정 2026-10-14) — tools/build_fixed_sets.py 가 우리 질문 세트 300·IFEval-Ko·KoMT-Bench 에서 고른 세트
FIXED_SET_VERSION = "fixed-set-v1"
FIXED_SETS = ROOT / "examples" / "fixed_sets"
FIXED_KINDS = ("simple", "precise")
_QUESTION_KEYS = ("question", "질문", "Q", "q", "prompt")
MAX_INPUT_TURNS_PER_CONVERSATION = 50   # 대화 하나의 턴 상한 — 점검이 앞 턴 이력을 턴마다 만들어 아주 긴 대화는 서버를 멈출 수 있다 (Codex 리뷰)
MAX_INPUT_TURNS = 2_000                 # 한 번에 넣는 전체 턴 상한


def parse_question_input(text: str, name: str = "직접 입력") -> Tuple[List[Dict[str, Any]], List[str]]:
    """질문만 담긴 입력 → id/turns 묶음과 경고. 답변은 받지 않는다(들어 있어도 쓰지 않음 — 답변은 수집에서 받는다).

    - JSON: 질문 글 목록, {question|질문|Q} 객체 목록(conversation_id 가 같으면 turn 순서대로 한 대화), {"questions": [...]},
      또는 질문 생성기가 내려 준 세트(id/turns 묶음 — 내려받아 고친 뒤 다시 올릴 수 있음). JSONL 은 줄마다 같은 모양.
    - 글(TXT·MD·직접 입력): '---' 만 있는 줄로 질문을 나눈다. 나누는 줄이 없으면 전체가 질문 하나.
    첨부파일은 파일이 함께 오지 않아 빼고 경고로 남긴다. 긴 참조 자료(reference)는 그대로 둔다."""
    warnings: List[str] = []
    text = (text or "").lstrip("﻿")
    if not text.strip():
        raise ValueError("질문이 비어 있습니다.")
    raw_groups: List[Dict[str, Any]] = []
    if text.lstrip().startswith(("[", "{")) or Path(name).suffix.lower() in (".json", ".jsonl"):
        try:
            value: Any = json.loads(text)
        except json.JSONDecodeError:
            try:
                value = [json.loads(line) for line in text.splitlines() if line.strip()]
            except json.JSONDecodeError as exc:
                raise ValueError(f"{name}: JSON 을 읽지 못했습니다 ({exc.lineno}번째 줄).") from None
        if isinstance(value, dict):
            value = value["questions"] if isinstance(value.get("questions"), list) else [value]
        if not isinstance(value, list):
            raise ValueError(f"{name}: 질문 목록(JSON 배열)이 아닙니다.")
        conversations: Dict[str, Dict[str, Any]] = {}
        for n, entry in enumerate(value, 1):
            if isinstance(entry, str):
                raw_groups.append({"turns": [{"question": entry}]})
                continue
            if not isinstance(entry, dict):
                warnings.append(f"{n}번째 항목은 질문이 아니라 뺐습니다.")
                continue
            if isinstance(entry.get("turns"), list):   # 질문 생성기 세트 형식
                raw_groups.append({**entry, "turns": [t for t in entry["turns"] if isinstance(t, dict)]})
                continue
            question = next((entry[k] for k in _QUESTION_KEYS if isinstance(entry.get(k), str)), None)
            if question is None:
                warnings.append(f"{n}번째 항목에 질문(question)이 없어 뺐습니다.")
                continue
            turn = {"question": question, **({"reference": entry["reference"]} if entry.get("reference") is not None else {})}
            cid = entry.get("conversation_id")
            if cid is None or not str(cid).strip():
                raw_groups.append({"turns": [turn]})
                continue
            group = conversations.get(str(cid))
            if group is None:
                group = conversations[str(cid)] = {"turns": [], "_order": []}
                raw_groups.append(group)
            number = entry.get("turn") if type(entry.get("turn")) is int else len(group["_order"]) + 1
            group["_order"].append((number, len(group["_order"]), turn))
        for group in conversations.values():   # 대화는 turn 번호 순서로 (같은 번호는 넣은 순서)
            group["turns"] = [turn for _, _, turn in sorted(group.pop("_order"), key=lambda x: (x[0], x[1]))]
    else:
        blocks = re.split(r"(?m)^[ \t]*-{3,}[ \t]*$", text.replace("\r\n", "\n"))
        raw_groups = [{"turns": [{"question": block.strip("\n")}]} for block in blocks if block.strip()]
    groups: List[Dict[str, Any]] = []
    for n, group in enumerate(raw_groups, 1):
        turns = []
        for turn in group["turns"]:
            question = turn.get("question")
            if not isinstance(question, str) or not question.strip():
                warnings.append(f"{n}번째 질문에 빈 턴이 있어 뺐습니다.")
                continue
            if turn.get("attachments"):
                warnings.append(f"{n}번째 질문의 첨부파일은 파일이 함께 오지 않아 뺐습니다 — 질문 글만 씁니다.")
            turns.append({"turn": len(turns) + 1, "question": question,
                          **({"reference": turn["reference"]} if turn.get("reference") is not None else {})})
        if not turns:
            continue
        items = group.get("target_items")
        groups.append({"id": len(groups) + 1, "type": str(group.get("type") or "직접 입력"), "topic": str(group.get("topic") or ""),
                       "target_items": [str(x) for x in items] if isinstance(items, list) else [],
                       "difficulty": group.get("difficulty") if group.get("difficulty") in DIFFICULTIES else "",
                       "trap": str(group.get("trap") or ""), "turns": turns})
    if not groups:
        raise ValueError("넣을 질문이 없습니다.")
    long = [g["id"] for g in groups if len(g["turns"]) > MAX_INPUT_TURNS_PER_CONVERSATION]
    if long:
        raise ValueError(f"대화 하나는 {MAX_INPUT_TURNS_PER_CONVERSATION}턴까지 넣을 수 있습니다 (넘는 질문: {', '.join(map(str, long[:10]))}번).")
    if sum(len(g["turns"]) for g in groups) > MAX_INPUT_TURNS:
        raise ValueError(f"한 번에 넣는 턴은 {MAX_INPUT_TURNS:,}개까지입니다.")
    if len(groups) > MAX_PLAN_QUESTIONS:
        raise ValueError(f"질문은 한 번에 {MAX_PLAN_QUESTIONS}개까지 넣을 수 있습니다 (지금 {len(groups)}개).")
    return groups, warnings


# --- 4. 웹 서비스 ------------------------------------------------------------------------

def gpt_llm(config: Dict[str, Any], usage: Dict[str, int]) -> LLM:
    """연결된 GPT(.env OPENAI_API_KEY·OPENAI_MODEL) 로 부르는 함수. 사용 토큰을 ``usage`` 에 더한다."""
    import httpx
    from .comparison_service import ProviderError, request_answer
    lock = threading.Lock()

    def observe(raw: Any) -> None:
        part = (raw or {}).get("usage") if isinstance(raw, dict) else None
        if isinstance(part, dict):
            with lock:
                usage["input_tokens"] = usage.get("input_tokens", 0) + int(part.get("input_tokens") or 0)
                usage["output_tokens"] = usage.get("output_tokens", 0) + int(part.get("output_tokens") or 0)

    def call(prompt: str, max_tokens: int) -> str:
        for attempt in (1, 2):   # 일시 오류(연결·과부하)는 한 번 더
            try:
                with httpx.Client(timeout=900, follow_redirects=False) as client:
                    outcome = request_answer("gpt", config, [{"role": "user", "content": prompt}], max_tokens, client, usage_observer=observe)
                break
            except (ProviderError, httpx.HTTPError) as exc:
                if attempt == 2:
                    raise GenerationError(f"GPT 호출 실패: {exc}") from None
                time.sleep(5)
        answer = outcome.get("answer") or ""
        if outcome.get("status") != "SUCCESS" or not answer.strip():
            raise IncompleteResponse(f"GPT 응답이 끝까지 오지 않았습니다 ({outcome.get('finish_reason') or outcome.get('status')}).", answer)
        return answer
    return call


class QuestionSetService:
    """질문 세트 생성 작업 — 한 번에 하나만 돈다(유료 호출). 상태는 메모리와 ``set.json`` 에 둔다."""

    def __init__(self, config_source: Callable[[], Optional[Dict[str, Any]]], store: Path = STORE,
                 llm_factory: Optional[Callable[[Dict[str, Any], Dict[str, int]], LLM]] = None,
                 run_starter: Optional[Callable[[str], str]] = None, run_canceller: Optional[Callable[[str], Any]] = None,
                 weak_source: Optional[Callable[[str], List[Dict[str, str]]]] = None):
        self.config_source, self.store = config_source, store
        self.run_starter = run_starter   # 질문 세트 id → 수집·평가 실행 id (서버가 비교 서비스와 이어 준다)
        self.run_canceller = run_canceller   # 실행 id → 중단 — 생성을 중단한 사이에 시작된 수집 실행도 함께 중단한다 (Codex 리뷰)
        self.weak_source = weak_source   # 실행 id → 그 실행에서 모든 모델이 PASS 한 검사·기준 [{id, name}] (변별력 보강 되먹임)
        self.llm_factory = llm_factory or gpt_llm
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._cancel: Dict[str, threading.Event] = {}
        self._starting: set = set()   # 수집 실행을 시작하는 중인 세트 — 같은 세트의 동시 시작을 막는다 (Codex 리뷰)

    def _path(self, set_id: str) -> Path:
        if not re.fullmatch(r"qs-[0-9a-f]{12}", set_id or ""):
            raise FileNotFoundError(set_id)
        return self.store / set_id

    def _save(self, job: Dict[str, Any]) -> None:
        path = self._path(job["id"])
        path.mkdir(parents=True, exist_ok=True)
        temp = path / "set.json.tmp"
        temp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
        temp.replace(path / "set.json")

    def start(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        config = self.config_source()
        if not config:
            raise ValueError("GPT 연결 설정(OPENAI_API_KEY·OPENAI_MODEL)이 없어 질문을 만들 수 없습니다.")
        note = payload.get("note") if isinstance(payload.get("note"), str) else ""
        # 기본은 만들기만 — 사용자가 화면에서 질문을 확인한 뒤 '평가 시작'(start_run)으로 수집·평가 (사용자 결정 2026-10-14,
        # 그 전에는 만든 뒤 바로 수집·평가). auto_run=true 를 직접 보내면 예전처럼 바로 이어서 한다
        auto_run = payload.get("auto_run") is True
        target = payload.get("target_questions")   # 세트 전체 질문 수(대화 1개 = 1개) — 비우면 가능한 한 적게 (사용자 결정 2026-10-14)
        if target in (None, ""):
            target = None
        elif type(target) is not int or not 1 <= target <= MAX_PLAN_QUESTIONS:
            raise ValueError(f"질문 수는 1~{MAX_PLAN_QUESTIONS} 사이의 정수여야 합니다.")
        per_target = payload.get("per_target")   # 세부 기준마다 채울 적용 질문 수 — 비우면 쓰지 않음
        if per_target in (None, ""):
            per_target = None
        elif type(per_target) is not int or not 1 <= per_target <= 200:
            raise ValueError("per_target 은 1~200 사이의 정수여야 합니다.")
        counts = {}
        for key in ("multi_turn_conversations", "long_context_questions"):   # 10턴 대화·긴 문서 질문 수 — 비우면 질문 수의 기본 비율
            value = payload.get(key)
            if value in (None, ""):
                counts[key] = None
            elif type(value) is not int or not 0 <= value <= MAX_PLAN_QUESTIONS:
                raise ValueError(f"{key} 는 0~{MAX_PLAN_QUESTIONS} 사이의 정수여야 합니다.")
            elif target is None and per_target is None:
                raise ValueError("10턴 대화·긴 문서 질문 수는 세트 질문 수(target_questions)나 per_target 을 정했을 때만 지정할 수 있습니다.")
            else:
                counts[key] = value
        limit = target or MAX_PLAN_QUESTIONS
        if sum(v for v in counts.values() if v) >= limit:   # 다른 항목에 쓸 질문이 1개 이상 남아야 한다 (Codex 리뷰: 상한 우회)
            raise ValueError(f"10턴 대화 수와 긴 문서 질문 수의 합은 {limit}개보다 작아야 합니다.")
        weak_from = payload.get("weak_from_run") or None   # 앞선 실행에서 모두 PASS 한 단위에 더 어려운 질문을 요구
        weak_units: List[Dict[str, str]] = []
        if weak_from:
            if not isinstance(weak_from, str) or self.weak_source is None:
                raise ValueError("변별력 보강에 쓸 실행을 읽을 수 없습니다.")
            try:
                weak_units = list(self.weak_source(weak_from))
            except (FileNotFoundError, KeyError, ValueError) as exc:
                raise ValueError(f"변별력 보강에 쓸 실행 결과를 읽지 못했습니다: {exc}") from None
        with self._lock:
            if any(job["status"] == "RUNNING" for job in self._jobs.values()):
                raise ValueError("이미 질문 세트를 만드는 중입니다. 끝난 뒤 다시 시도해 주세요.")
            set_id = "qs-" + uuid.uuid4().hex[:12]
            job = {"id": set_id, "version": QUESTION_GEN_VERSION, "status": "RUNNING", "phase": "plan", "message": "시작",
                   "created_at": datetime.now(timezone.utc).isoformat(), "model": config.get("model"), "note": note[:2000],
                   "usage": {}, "warnings": [], "summary": None, "coverage": None, "error": None,
                   "target_questions": target, "per_target": per_target, "weak_from_run": weak_from, "weak_units": weak_units, **counts,
                   "auto_run": auto_run and self.run_starter is not None, "run_id": None, "run_error": None}
            self._jobs[set_id] = job
            self._cancel[set_id] = threading.Event()
        try:
            self._save(job)
            threading.Thread(target=self._run, args=(set_id, config, note), daemon=True).start()
        except Exception:   # 저장·시작에 실패하면 등록을 되돌린다 — 남겨 두면 다음 생성이 계속 '만드는 중'으로 막힌다
            with self._lock:
                self._jobs.pop(set_id, None)
                self._cancel.pop(set_id, None)
            raise
        return self.status(set_id)

    def _run(self, set_id: str, config: Dict[str, Any], note: str) -> None:
        job, cancel = self._jobs[set_id], self._cancel[set_id]
        out = self._path(set_id)

        def progress(phase: Optional[str], data: Dict[str, Any]) -> None:
            with self._lock:
                if phase:
                    job.update(phase=phase, message=data.get("message", ""))
                if "questions" in data:   # 생성 중 화면의 질문 목록 (다 만들면 확인 목록으로 대신하고 뺀다)
                    job["preview"] = data["questions"]

        try:
            builder = QuestionSetBuilder(self.llm_factory(config, job["usage"]), out, progress=progress, cancelled=cancel.is_set)
            result = builder.build(note, target_questions=job.get("target_questions"), weak_units=job.get("weak_units") or [],
                                   multi_turn_count=job.get("multi_turn_conversations"), long_context_count=job.get("long_context_questions"),
                                   per_target=job.get("per_target"))
            if cancel.is_set():   # 마지막 단계에서 취소했으면 완료로 남기지 않는다
                raise Cancelled("생성을 중단했습니다.")
            groups, report = result["groups"], result["check"]
            (out / "questions.json").write_text(json.dumps(groups, ensure_ascii=False, indent=1), encoding="utf-8")
            with self._lock:
                job.update(warnings=builder.warnings, calls=builder.calls, summary=_summary(groups, report), coverage=report["coverage"],
                           rounds=result["rounds"], extra_rounds=result.get("extra_rounds", 0))
            if job.get("auto_run"):   # 수집 실행이 잡힌 뒤에 완료로 — 먼저 완료를 알리면 화면이 run_id 없이 폴링을 멈춘다 (Codex 리뷰)
                if cancel.is_set():   # 질문 저장 사이에 접수된 중단 — 유료 수집을 시작하지 않는다
                    raise Cancelled("생성을 중단했습니다.")
                with self._lock:
                    job.update(phase="run", message="답변 수집을 시작하는 중")
                self._start_run(job)
                if cancel.is_set():   # 시작하는 사이에 중단 요청 — 만들어진 수집 실행도 중단하고 완료로 남기지 않는다
                    self._cancel_run(job)
                    raise Cancelled("생성을 중단했습니다.")
            with self._lock:
                job.update(status="COMPLETED", phase="done", message="완료")
                job.pop("preview", None)
        except Exception as exc:   # 생성 실패는 이 작업의 오류로만 남긴다 (키·요청 원문은 남기지 않음)
            with self._lock:
                job.update(status="CANCELLED" if cancel.is_set() else "ERROR", error=str(exc)[:500])
        finally:
            with self._lock:
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
            self._save(job)

    def _start_run(self, job: Dict[str, Any]) -> None:
        set_id = job["id"]
        with self._lock:
            if set_id in self._starting:
                raise ValueError("이미 이 질문 세트의 답변 수집을 시작하는 중입니다.")
            self._starting.add(set_id)
        try:
            run_id = self.run_starter(set_id)
            with self._lock:
                job.update(run_id=run_id, run_error=None)
        except Exception as exc:   # 수집 시작 실패는 질문 세트를 실패로 만들지 않는다 — 화면에서 다시 시작할 수 있다
            with self._lock:
                job["run_error"] = str(exc)[:300]
        finally:
            with self._lock:
                self._starting.discard(set_id)

    def start_run(self, set_id: str) -> Dict[str, Any]:
        """완료된 질문 세트로 수집·평가 실행을 (다시) 시작한다."""
        job = self.status(set_id)
        if job.get("status") != "COMPLETED":
            raise ValueError("질문 세트를 다 만든 뒤에 답변을 수집할 수 있습니다.")
        if self.run_starter is None:
            raise ValueError("수집 실행을 시작할 수 없습니다.")
        with self._lock:
            live = self._jobs.get(set_id)
        target = live if live is not None else job
        self._start_run(target)
        self._save(target)
        if target.get("run_error"):
            raise ValueError(target["run_error"])
        return self.status(set_id) if live is not None else target

    def import_questions(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """질문 직접 넣기 — 직접 입력한 글이나 질문 파일로 완료된 질문 세트를 만든다(모델 호출 없음). 수집·평가는
        만든 질문을 확인한 뒤 ``start_run``(평가 시작)으로 — 생성한 세트와 같은 경로다."""
        text, name = payload.get("text"), str(payload.get("name") or "직접 입력")[:200]
        if not isinstance(text, str):
            raise ValueError("질문(text)은 글이어야 합니다.")
        groups, warnings = parse_question_input(text, name)
        report = check(groups)
        if report.get("errors"):   # 평가 엔진이 받지 못하는 입력(예: 참조 자료가 숫자) — 완료로 저장하지 않는다 (Codex 리뷰)
            rows = flatten(groups)
            details = [f"{rows[e['index'] - 1]['case_id'] if 0 < e.get('index', 0) <= len(rows) else e.get('index')}: {e.get('message')}"
                       for e in report["errors"][:5]]
            raise ValueError("질문을 평가할 수 없는 형식입니다 — " + " / ".join(details))
        set_id = "qs-" + uuid.uuid4().hex[:12]
        out = self._path(set_id)
        (out / "files").mkdir(parents=True, exist_ok=True)
        (out / "questions.json").write_text(json.dumps(groups, ensure_ascii=False, indent=1), encoding="utf-8")
        job = {"id": set_id, "version": QUESTION_INPUT_VERSION, "source": "file" if payload.get("source") == "file" else "manual",
               "source_name": name, "status": "COMPLETED", "phase": "done", "message": "완료",
               "created_at": datetime.now(timezone.utc).isoformat(), "model": None, "note": "", "usage": {}, "warnings": warnings,
               "summary": _summary(groups, report), "coverage": report["coverage"], "error": None, "target_questions": None,
               "weak_from_run": None, "weak_units": [], "auto_run": False, "run_id": None, "run_error": None, "calls": 0, "rounds": 0}
        self._save(job)
        return job

    def fixed_sets(self) -> List[Dict[str, Any]]:
        """화면 버튼용 — 고정 세트마다 이름·질문 수·턴 수·k·출처·모자란 기준."""
        out = []
        for kind in FIXED_KINDS:
            path = FIXED_SETS / kind / "selection.json"
            if path.is_file():
                sel = json.loads(path.read_text(encoding="utf-8"))
                out.append({key: sel.get(key) for key in ("kind", "label", "k", "questions", "turns", "sources", "short", "licenses")})
        return out

    def start_fixed(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """고정 질문 세트(간단·정밀)로 완료된 질문 세트를 만든다 — 모델 호출 없음. 수집은 화면의 '답변 수집'(start_run), 평가는 '평가 진행'."""
        kind = payload.get("kind")
        if kind not in FIXED_KINDS:
            raise ValueError("고정 질문 세트는 simple(간단 평가)·precise(정밀 평가) 중 하나여야 합니다.")
        source = FIXED_SETS / kind
        if not (source / "questions.json").is_file() or not (source / "selection.json").is_file():
            raise ValueError("고정 질문 세트 파일이 없습니다 — python tools/build_fixed_sets.py 로 만드세요.")
        groups = json.loads((source / "questions.json").read_text(encoding="utf-8"))
        selection = json.loads((source / "selection.json").read_text(encoding="utf-8"))
        set_id = "qs-" + uuid.uuid4().hex[:12]
        out = self._path(set_id)
        shutil.copytree(source / "files", out / "files") if (source / "files").is_dir() else (out / "files").mkdir(parents=True, exist_ok=True)
        (out / "questions.json").write_text(json.dumps(groups, ensure_ascii=False, indent=1), encoding="utf-8")
        short = selection.get("short") or {}
        warnings = [f"{selection['label']} — 세부 기준마다 {selection.get('k')}번을 목표로 골랐고, 후보에 모자라 덜 채운 기준 {len(short)}개: "
                    + ", ".join(f"{t} {n}" for t, n in short.items())] if short else []
        job = {"id": set_id, "version": FIXED_SET_VERSION, "source": "fixed", "fixed_kind": kind, "source_name": selection["label"],
               "status": "COMPLETED", "phase": "done", "message": "완료", "created_at": datetime.now(timezone.utc).isoformat(), "model": None,
               "note": "", "usage": {}, "warnings": warnings, "summary": selection.get("summary") or {}, "coverage": selection.get("coverage") or {},
               "fixed_sources": selection.get("sources"), "error": None, "target_questions": None, "weak_from_run": None, "weak_units": [],
               "auto_run": False, "run_id": None, "run_error": None, "calls": 0, "rounds": 0}
        self._save(job)
        return job

    def preview(self, set_id: str) -> List[Dict[str, Any]]:
        """화면에서 확인할 질문 목록 — 질문 원문은 그대로, 긴 참조 자료 본문·첨부 내용은 빼고 글자 수·파일 이름만."""
        rows = []
        for group in self.dataset(set_id):
            turns = []
            for turn in group.get("turns") or []:
                ref = turn.get("reference")
                size = len(ref) if isinstance(ref, str) else (len(json.dumps(ref, ensure_ascii=False)) if ref else 0)
                turns.append({"question": str(turn.get("question") or ""), "reference_chars": size,
                              "attachments": [str(a.get("name")) for a in turn.get("attachments") or [] if isinstance(a, dict)]})
            rows.append({**{key: group.get(key) for key in ("id", "type", "topic", "difficulty", "trap", "target_items")}, "turns": turns})
        return rows

    def files_dir(self, set_id: str) -> Path:
        return self._path(set_id) / "files"

    def status(self, set_id: str) -> Dict[str, Any]:
        with self._lock:
            if set_id in self._jobs:
                return json.loads(json.dumps(self._jobs[set_id], ensure_ascii=False))
        path = self._path(set_id) / "set.json"
        if not path.is_file():
            raise FileNotFoundError(set_id)
        job = json.loads(path.read_text(encoding="utf-8"))
        if job.get("status") == "RUNNING":   # 이 서버가 돌리는 작업이 아님 — 서버가 다시 시작돼 끊긴 작업
            job.update(status="ERROR", error="서버가 다시 시작돼 생성이 중단됐습니다.")
            try:
                self._save(job)
            except OSError:
                pass
        return job

    def list(self) -> List[Dict[str, Any]]:
        sets = []
        if self.store.is_dir():
            for path in sorted(self.store.glob("qs-*/set.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
                try:
                    sets.append(self.status(path.parent.name))
                except (OSError, ValueError, FileNotFoundError):
                    continue
        return sets

    def _cancel_run(self, job: Dict[str, Any]) -> None:
        with self._lock:
            run_id = job.get("run_id")
        if run_id and self.run_canceller is not None:
            try:
                self.run_canceller(run_id)
            except Exception:   # 이미 끝났거나 없는 실행 — 생성 중단 자체는 그대로 진행한다
                pass

    def cancel(self, set_id: str) -> Dict[str, Any]:
        event = self._cancel.get(set_id)
        if event is None:
            raise FileNotFoundError(set_id)
        event.set()
        with self._lock:
            job = self._jobs.get(set_id)
            starting = job is not None and job["status"] == "RUNNING" and job.get("phase") == "run"
        if starting:   # 수집 실행을 막 시작한 뒤라면 그 실행도 중단한다 (완료된 세트의 실행은 실행 화면에서 따로 중단)
            self._cancel_run(job)
        return self.status(set_id)

    def dataset(self, set_id: str) -> List[Dict[str, Any]]:
        path = self._path(set_id) / "questions.json"
        if not path.is_file():
            raise FileNotFoundError(set_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def file(self, set_id: str, name: str) -> Tuple[bytes, str]:
        if not re.fullmatch(r"[0-9a-f]{64}\.(txt|docx|pdf|png)", name or ""):
            raise FileNotFoundError(name)
        path = self._path(set_id) / "files" / name
        if not path.is_file():
            raise FileNotFoundError(name)
        kind = {"txt": "text/plain; charset=utf-8", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "pdf": "application/pdf", "png": "image/png"}[name.rsplit(".", 1)[1]]
        return path.read_bytes(), kind


def _summary(groups: List[Dict[str, Any]], report: Dict[str, Any]) -> Dict[str, Any]:
    turns = [t for g in groups for t in g["turns"]]
    return {"questions": len(groups), "single_questions": sum(1 for g in groups if len(g["turns"]) == 1),
            "conversations": sum(1 for g in groups if len(g["turns"]) > 1), "turns": len(turns),
            "attachments": sum(len(t.get("attachments") or []) for t in turns),
            "long_references": sum(1 for t in turns if t.get("reference")),
            "criteria": {cid: row["applicable_units"] for cid, row in report["criteria"].items()},
            "difficulty": {level: sum(1 for g in groups if g.get("difficulty") == level) for level in DIFFICULTIES},
            "multi_turn": report.get("multi_turn")}
