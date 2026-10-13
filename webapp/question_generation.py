"""질문 세트 생성 — 웹 버튼 하나로 GPT 가 우리 세부 기준(CODE 검사·의미 평가 항목)을 모두 평가할 수 있는 질문을 최소 개수로 만든다.

사용자 결정(2026-10-13): 질문 목록은 우리가 세운 세세한 평가 항목 전부를 평가할 수 있게, 가장 적은 개수로, 주제·유형은 다양하게
만든다. 생성 모델은 GPT. 첨부파일(이미지·TXT·DOCX·PDF)도 질문을 만들 때 함께 만들고, 질문과 같이 각 모델에 보내 답변을 받는다.

흐름 (한 번의 생성 = ``QuestionSetBuilder.build``):
1. 계획 — GPT 가 질문·노리는 항목·첨부 명세·긴 자료 명세를 JSON 으로 낸다 (``plan_prompt``).
2. 첨부파일 — 명세대로 파일을 만든다 (``attachment_files``). 내용을 우리가 정했으므로 평가 모델에 정답 내용을 줄 수 있다.
3. 긴 자료 — J7(입력 272K 토큰 이상)은 참조 자료를 여러 번 나눠 쓰게 해 이어 붙이고 사실 문장을 정한 위치에 넣는다.
   J6(턴마다 질문+답변 10K 토큰 이상, 10턴 이상)은 턴마다 붙여 넣을 자료를 만들어 질문 앞에 붙인다 — 질문만으로 10K 토큰을 넘긴다.
4. 점검 — 질문 세트 점검(``tools.check_question_set``)과 같은 규칙으로 검사·항목별 적용 수를 세고, 빠진 것만 GPT 에 보충을
   요청한다 (최대 ``SUPPLEMENT_ROUNDS`` 회). 답변 구조로만 생기는 검사는 질문만으로 셀 수 없어 GPT 가 적은 노리는 항목으로 표시한다.
5. 저장 — ``data/question_sets/<id>/questions.json`` (업로드 화면과 같은 id/turns JSON) 과 ``files/`` (첨부파일).
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from code_eval import long_context, multi_turn
from code_eval.criteria_registry import reference_details
from code_eval.semantic_items import ITEMS, SUBS_BY_ID

from .attachment_files import ATTACHMENT_TYPES, AttachmentError, render_attachment
from .code_eval_service import ROOT

__all__ = ["QUESTION_GEN_VERSION", "GenerationError", "IncompleteResponse", "criteria_catalog", "plan_prompt", "supplement_prompt", "parse_plan",
           "QuestionSetBuilder", "QuestionSetService"]

QUESTION_GEN_VERSION = "question-gen-v1"
STORE = ROOT / "data" / "question_sets"
CONDITIONS_PATH = ROOT / "code_eval" / "settings" / "code_check_conditions.json"
PLAN_TOKENS = 60_000          # 계획 응답 최대 출력 토큰
MATERIAL_TOKENS = 32_000      # 긴 자료 한 조각의 최대 출력 토큰 (추론 토큰까지 함께 세는 모델이라 넉넉히)
CHUNK_CHARS = 10_000          # 긴 자료 한 조각에 요청하는 글자 수 (한국어 약 0.72토큰/자)
PASTE_CHARS = 15_000          # J6 턴마다 붙여 넣을 자료 글자 수 (약 10.8K 토큰)
SUPPLEMENT_ROUNDS = 2
# 비용 상한 — GPT 계획이 지나치게 커도 호출이 끝없이 늘지 않게
MAX_PLAN_QUESTIONS = 40
MAX_PLAN_TURNS = 15
MAX_LONG_REFERENCES = 2
MAX_CALLS = 250
MATERIAL_WORKERS = 4
MAX_ITEMS_PER_QUESTION = 8
MAX_ATTACHMENT_CHARS = 4_000

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

def criteria_catalog() -> Dict[str, List[Dict[str, str]]]:
    """프롬프트에 넣을 점검 목록 — CODE 검사(AIONA 특화 제외)와 의미 평가 항목. 기준 정의에서 만들어 기준이 바뀌면 같이 바뀐다."""
    examples: Dict[str, str] = {}
    rules: Dict[str, List[str]] = {}
    try:
        for check in json.loads(CONDITIONS_PATH.read_text(encoding="utf-8")).get("checks") or []:
            sample = ((check.get("examples") or {}).get("PASS") or {}).get("question")
            if isinstance(sample, str):
                examples[check["id"]] = " ".join(sample.split())[:160]
            # 엔진이 질문에서 요구를 읽는 규칙 — 이 표현·자료 형식이 아니면 검사가 적용되지 않는다 (실제 생성에서 확인)
            rules[check["id"]] = [f"{c.get('title')}: {' '.join(str(c.get('text') or '').split())}"
                                  for c in check.get("conditions") or [] if isinstance(c, dict) and c.get("title")]
    except (OSError, ValueError):
        pass
    codes = [{"id": c["id"], "name": c["name"], "applies": c["applies"], "example": examples.get(c["id"], ""),
              "rules": rules.get(c["id"], [])}
             for c in reference_details()["code_checks"] if not c["id"].startswith("AIONA")]
    items = [{"id": item.id, "sub": SUBS_BY_ID[item.sub].name, "question": item.question, "applies": item.applies} for item in ITEMS]
    return {"code": codes, "items": items}


_FORMAT = """# 출력 형식 (JSON 하나만, 설명·코드 펜스 없이)
{"questions": [
  {"id": "Q01", "type": "질문 유형", "topic": "주제", "target_items": ["점검 목록의 id", "..."],
   "turns": [{"question": "질문 원문",
              "attachments": [{"name": "견적서.pdf", "type": "pdf", "content": "파일에 들어갈 내용 전체"}],
              "long_reference": {"topic": "자료 주제", "facts": [{"text": "자료에 넣을 사실 문장", "position": 0.5}]},
              "long_paste": {"topic": "붙여 넣을 자료 주제", "must_include": ["꼭 들어갈 내용"]}}]}
]}
- 필요 없는 attachments·long_reference·long_paste 는 빼거나 null 로 둡니다. 단일 질문은 turns 가 1개, 멀티턴 대화는 여러 개입니다.
- target_items 에는 [점검 목록]의 id 만, 그 질문(대화)에서 적용되도록 의도한 것을 씁니다."""


def _catalog_lines(catalog: Dict[str, List[Dict[str, str]]], only: Optional[set] = None, detailed: bool = False) -> List[str]:
    """점검 목록 줄. CODE 검사는 인식 규칙 첫 줄(``detailed`` 면 규칙 전체)을 함께 — 질문에 그 표현을 그대로 써야 적용된다."""
    lines = ["## CODE 검사 (id · 이름 · 적용 · 예시 질문 · 인식 규칙)"]
    for c in catalog["code"]:
        if only is None or c["id"] in only:
            lines.append(f"- {c['id']} · {c['name']} · {c['applies']}" + (f" · 예: {c['example']}" if c["example"] else ""))
            for rule in (c.get("rules") or [])[: None if detailed else 1]:
                lines.append(f"    · 인식: {rule[:600 if detailed else 220]}")
    lines.append("## 의미 평가 항목 (id · 세부 기준 · 점검 질문(예 = PASS) · 적용)")
    for item in catalog["items"]:
        if only is None or item["id"] in only:
            lines.append(f"- {item['id']} · {item['sub']} · {item['question']} · 적용: {item['applies']}")
    return lines


def plan_prompt(catalog: Optional[Dict[str, List[Dict[str, str]]]] = None, note: str = "") -> str:
    catalog = catalog or criteria_catalog()
    n_code, n_items = len(catalog["code"]), len(catalog["items"])
    head = f"""당신은 한국어 AI 답변 품질 평가에 쓸 질문 세트를 설계합니다. 결과는 JSON 하나로만 답합니다.

# 목표
아래 [점검 목록]의 CODE 검사 {n_code}개와 의미 평가 항목 {n_items}개가 모두 최소 1개 질문(또는 대화)에서 적용되게 하되, 질문 수는 가능한 한 적게 합니다.
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

# 멀티턴 대화 (J6)
- 대화 1개 이상, 대화마다 턴 {multi_turn.MIN_LONG_TURNS}개 이상. J6 항목이 모두 적용되게 턴을 설계합니다: 앞 턴의 지시·제약 유지, 이전 정보 사용, 지시어("아까 두 번째"), 조건 변경·취소, 같은 결과물 2회 이상 수정, 이전 결정에 이어지는 요청, 근거 없는 반박.
- 멀티턴으로 인정되려면 턴마다 질문+답변이 {multi_turn.MIN_TURN_TOKENS:,}토큰 이상이어야 합니다. 턴마다 long_paste(붙여 넣을 긴 자료의 주제와 꼭 들어갈 내용)를 쓰면 시스템이 자료를 만들어 질문 앞에 붙입니다. 질문 문장 자체는 짧게 씁니다.

# 롱컨텍스트 (J7)
- 질문 1개 이상에 {long_context.MIN_TOKENS:,}토큰(한국어 약 38만 자) 이상의 참조 자료가 필요합니다. long_reference 에 자료 주제와, 자료 속에 넣을 사실 문장(facts)과 위치(position 0~1)를 씁니다. 시스템이 자료를 만들고 사실을 그 위치에 넣습니다.
- J7-1.a 는 자료 중간·뒷부분의 사실을 질문과 다른 표현으로 묻고, J7-2.a 는 3곳 이상에 흩어진 사실을 연결·비교·집계하게 묻습니다. 한 질문으로 둘 다 물어도 됩니다.

# 첨부파일 (J8)
- 첨부 질문을 만들고 attachments 에 파일 명세를 씁니다. 종류는 {"·".join(ATTACHMENT_TYPES)}. content 에는 파일에 들어갈 실제 내용 전체를 씁니다({MAX_ATTACHMENT_CHARS:,}자 이하). 줄 표기: '# 제목', '## 소제목', '- 항목', '| a | b |' 표 행, '[다음 쪽]' 쪽 나눔.
- image 는 막대그래프면 "chart": {{"title": "...", "labels": ["..."], "values": [숫자], "unit": "단위"}} 를 쓰고, 그 밖에는 content 의 글·표를 그립니다.
- 첨부 질문은 파일 종류마다 1개 이상 만듭니다({"·".join(ATTACHMENT_TYPES)} — 첨부 질문 {len(ATTACHMENT_TYPES)}개 이상). image 는 막대그래프와 표·글 그림을 섞습니다. 첨부 질문도 다른 항목을 함께 노리게 해 전체 질문 수를 줄입니다.
- J8-2.a 가 적용되게 표·도표·쪽·목록 구조를 읽어야 답할 수 있는 질문을 넣습니다.

{_FORMAT}
"""
    extra = f"\n# 추가 요청\n{note.strip()}\n" if note and note.strip() else ""
    return head + extra + "\n# 점검 목록\n" + "\n".join(_catalog_lines(catalog))


def supplement_prompt(missing: List[str], existing_topics: List[str], catalog: Optional[Dict[str, List[Dict[str, str]]]] = None,
                      missing_types: Tuple[str, ...] = ()) -> str:
    """앞서 만든 질문 세트에서 적용되지 않은 검사·항목, 없는 첨부 종류만 보충한다."""
    catalog = catalog or criteria_catalog()
    parts = ["당신은 한국어 AI 답변 품질 평가에 쓸 질문 세트를 보충합니다. 결과는 JSON 하나로만 답합니다.\n\n"
             "# 목표\n앞서 만든 질문 세트에서 아래 [빠진 항목]이 어느 질문에도 적용되지 않았거나 [빠진 첨부 종류]의 첨부 질문이 없습니다. "
             "이것들이 적용되는 질문만 가능한 한 적게 추가합니다. 적용 문구에 맞는 표현을 질문에 분명히 넣습니다. 이미 쓴 주제와 겹치지 않게 합니다.\n"
             f"이미 쓴 주제: {', '.join(t for t in existing_topics if t)[:1500] or '없음'}\n"
             "멀티턴 대화·긴 참조 자료·첨부파일 규칙은 처음과 같습니다(대화는 턴 10개 이상과 턴마다 long_paste, 긴 자료는 long_reference, 첨부는 attachments).\n\n"
             f"{_FORMAT}"]
    if missing_types:
        parts.append("# 빠진 첨부 종류\n" + "\n".join(f"- {kind}: 이 종류의 파일을 첨부한 질문을 1개 이상 추가합니다 (빠진 항목도 함께 노리게)."
                                                   for kind in missing_types))
    if missing:
        parts.append("# 빠진 항목 (CODE 검사는 인식 규칙의 표현·자료 형식을 그대로 따라야 적용됩니다)\n"
                     + "\n".join(_catalog_lines(catalog, set(missing), detailed=True)))
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
    must = "\n".join(f"- {x}" for x in spec.get("must_include") or []) or "- (없음)"
    return (f"다음 주제로 사용자가 대화창에 붙여 넣을 자료(회의록·보고서·기사·메모 등)를 한국어 본문으로 약 {chars:,}자 씁니다.\n"
            f"주제: {spec.get('topic') or '업무 자료'}\n꼭 들어갈 내용:\n{must}\n- 머리말·설명 없이 자료 본문만 씁니다.")


# --- 2. 계획 응답 읽기 -------------------------------------------------------------------

def _json_object(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start < 0:
        raise GenerationError("GPT 응답에서 JSON 객체를 찾지 못했습니다.")
    try:   # 첫 완전한 객체만 읽는다 — 끝에 괄호가 더 붙은 응답(실제 GPT 응답에서 확인)도 앞 객체는 쓸 수 있다
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError as exc:
        raise GenerationError(f"GPT 응답 JSON 을 읽지 못했습니다: {exc}") from None
    if not isinstance(value, dict):
        raise GenerationError("GPT 응답이 JSON 객체가 아닙니다.")
    return value


def parse_plan(text: str, known_ids: Optional[set] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """GPT 계획 → (질문 목록, 경고). 모양이 틀린 질문·턴·첨부는 버리고 경고로 남긴다 (지어내 채우지 않는다)."""
    value = _json_object(text)
    raw = value.get("questions")
    if not isinstance(raw, list) or not raw:
        raise GenerationError("GPT 응답에 questions 목록이 없습니다.")
    known = known_ids if known_ids is not None else {c["id"] for c in criteria_catalog()["code"]} | {i.id for i in ITEMS}
    questions, warnings = [], []
    for index, entry in enumerate(raw, 1):
        if len(questions) >= MAX_PLAN_QUESTIONS:   # 비용 상한 — 계획이 지나치게 크면 앞에서부터만 쓴다
            warnings.append(f"질문이 {MAX_PLAN_QUESTIONS}개를 넘어 {index}번부터는 뺐습니다.")
            break
        if not isinstance(entry, dict) or not isinstance(entry.get("turns"), list) or not entry["turns"]:
            warnings.append(f"{index}번 질문: turns 가 없어 뺐습니다.")
            continue
        turns = []
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
                if isinstance(turn.get(key), dict):
                    clean[key] = turn[key]
            turns.append(clean)
        if not turns:
            continue
        raw_targets = entry.get("target_items")
        targets = [str(x) for x in raw_targets if str(x) in known] if isinstance(raw_targets, list) else []
        questions.append({"type": str(entry.get("type") or ""), "topic": str(entry.get("topic") or ""), "target_items": targets, "turns": turns})
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
        self.long_references = 0
        self._calls_lock = threading.Lock()   # 긴 자료는 여러 스레드가 동시에 부른다

    def _ask(self, prompt: str, max_tokens: int, partial_ok: bool = False) -> str:
        if self.cancelled():
            raise Cancelled("생성을 중단했습니다.")
        with self._calls_lock:   # 작업 전체 호출 상한 — 요청 전에 센다 (재시도는 gpt_llm 안에서 1회)
            if self.calls >= MAX_CALLS:
                raise GenerationError(f"GPT 호출이 작업 한도({MAX_CALLS}회)에 닿았습니다.")
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
                return min(max(float(value), 0.0), 1.0)
            except (TypeError, ValueError):
                return 0.5
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
        """J6 — 질문만으로 턴 하나가 MIN_TURN_TOKENS 이상이 되도록 붙여 넣을 자료를 만든다."""
        material = self._ask(_paste_prompt(spec, PASTE_CHARS), MATERIAL_TOKENS, True).strip()
        tries = 0
        while self.count(f"[붙여 넣은 자료]\n{material}\n\n{question}") < multi_turn.MIN_TURN_TOKENS and tries < 3:
            tries += 1
            material += "\n\n" + self._ask(_paste_prompt(dict(spec, topic=f"{spec.get('topic') or '업무 자료'} (이어서)"), PASTE_CHARS // 2),
                                           MATERIAL_TOKENS, True).strip()
        return f"[붙여 넣은 자료]\n{material}\n\n{question}"

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
            if turn.get("long_reference"):
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
        return {"id": number, "type": plan.get("type") or "", "topic": plan.get("topic") or "",   # 업로드 변환이 문항 ID Q{id}_T{턴} 을 만든다
                "target_items": plan.get("target_items") or [], "turns": turns}

    # 전체 ---------------------------------------------------------------------------
    def build(self, note: str = "") -> Dict[str, Any]:
        catalog = criteria_catalog()
        self.progress("plan", {"message": "GPT 가 질문 계획을 쓰는 중"})
        raw = self._ask(plan_prompt(catalog, note), PLAN_TOKENS)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "plan.txt").write_text(raw, encoding="utf-8")   # GPT 가 쓴 계획 원문 — 실패해도 확인할 수 있게
        plans, warnings = parse_plan(raw)
        self.warnings += warnings
        groups: List[Dict[str, Any]] = []
        rounds = 0
        while True:
            for plan in plans:
                self.progress("materialize", {"message": f"질문 {len(groups) + 1} 만드는 중 (첨부·긴 자료)", "done": len(groups)})
                try:
                    groups.append(self.materialize(len(groups) + 1, plan))
                except Cancelled:
                    raise
                except GenerationError as exc:   # 그 질문만 빼고 계속한다 — 빠진 항목은 점검·보충이 다시 잡는다
                    self.warnings.append(f"'{plan.get('topic') or plan['turns'][0]['question'][:30]}' 질문을 만들지 못해 뺐습니다: {exc}")
            report = check(groups)
            missing = report["coverage"]["missing_checks"] + report["coverage"]["missing_items"]
            have = attachment_types(groups)
            missing_types = tuple(kind for kind in ATTACHMENT_TYPES if kind not in have)   # 첨부 종류마다 질문 1개 이상 (사용자 요청 '다양하게')
            if (not missing and not missing_types) or rounds >= SUPPLEMENT_ROUNDS:
                break
            rounds += 1
            self.progress("supplement", {"message": f"빠진 항목 {len(missing)}개·첨부 종류 {len(missing_types)}개 보충 요청 ({rounds}회차)",
                                         "missing": missing + [f"첨부:{kind}" for kind in missing_types]})
            try:
                plans, warnings = parse_plan(self._ask(supplement_prompt(missing, [g["topic"] for g in groups], catalog, missing_types),
                                                       PLAN_TOKENS))
            except Cancelled:
                raise
            except GenerationError as exc:
                self.warnings.append(f"보충 {rounds}회차: {exc}")
                break
            self.warnings += warnings
        if not groups:
            raise GenerationError("만든 질문이 없습니다: " + " / ".join(self.warnings[-3:]))
        return {"groups": groups, "check": report, "rounds": rounds}


def flatten(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """id/turns 묶음 → 업로드 화면과 같은 행 목록 (dataset_import.id_turn_rows)."""
    from .dataset_import import id_turn_rows
    return id_turn_rows([{"id": g["id"], "turns": [dict(t, answer=t.get("answer", "")) for t in g["turns"]]} for g in groups])


def check(groups: List[Dict[str, Any]]) -> Dict[str, Any]:
    """질문 세트 점검과 같은 규칙의 기준·검사·항목별 적용 수 (모델 호출 없음)."""
    from tools.check_question_set import check_items
    return check_items(flatten(groups), "items")


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
                 run_starter: Optional[Callable[[str], str]] = None):
        self.config_source, self.store = config_source, store
        self.run_starter = run_starter   # 질문 세트 id → 수집·평가 실행 id (서버가 비교 서비스와 이어 준다)
        self.llm_factory = llm_factory or gpt_llm
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._cancel: Dict[str, threading.Event] = {}

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
        auto_run = payload.get("auto_run", True) is not False   # 만든 뒤 바로 답변 수집·평가 (사용자 요청 2026-10-14)
        with self._lock:
            if any(job["status"] == "RUNNING" for job in self._jobs.values()):
                raise ValueError("이미 질문 세트를 만드는 중입니다. 끝난 뒤 다시 시도해 주세요.")
            set_id = "qs-" + uuid.uuid4().hex[:12]
            job = {"id": set_id, "version": QUESTION_GEN_VERSION, "status": "RUNNING", "phase": "plan", "message": "시작",
                   "created_at": datetime.now(timezone.utc).isoformat(), "model": config.get("model"), "note": note[:2000],
                   "usage": {}, "warnings": [], "summary": None, "coverage": None, "error": None,
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

        def progress(phase: str, data: Dict[str, Any]) -> None:
            with self._lock:
                job.update(phase=phase, message=data.get("message", ""))

        try:
            builder = QuestionSetBuilder(self.llm_factory(config, job["usage"]), out, progress=progress, cancelled=cancel.is_set)
            result = builder.build(note)
            if cancel.is_set():   # 마지막 단계에서 취소했으면 완료로 남기지 않는다
                raise Cancelled("생성을 중단했습니다.")
            groups, report = result["groups"], result["check"]
            (out / "questions.json").write_text(json.dumps(groups, ensure_ascii=False, indent=1), encoding="utf-8")
            with self._lock:
                job.update(status="COMPLETED", phase="done", message="완료", warnings=builder.warnings, calls=builder.calls,
                           summary=_summary(groups, report), coverage=report["coverage"], rounds=result["rounds"])
            if job.get("auto_run"):
                self._start_run(job)
        except Exception as exc:   # 생성 실패는 이 작업의 오류로만 남긴다 (키·요청 원문은 남기지 않음)
            with self._lock:
                job.update(status="CANCELLED" if cancel.is_set() else "ERROR", error=str(exc)[:500])
        finally:
            with self._lock:
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
            self._save(job)

    def _start_run(self, job: Dict[str, Any]) -> None:
        try:
            run_id = self.run_starter(job["id"])
            with self._lock:
                job.update(run_id=run_id, run_error=None)
        except Exception as exc:   # 수집 시작 실패는 질문 세트를 실패로 만들지 않는다 — 화면에서 다시 시작할 수 있다
            with self._lock:
                job["run_error"] = str(exc)[:300]

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

    def cancel(self, set_id: str) -> Dict[str, Any]:
        event = self._cancel.get(set_id)
        if event is None:
            raise FileNotFoundError(set_id)
        event.set()
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
            "multi_turn": report.get("multi_turn")}
