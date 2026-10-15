"""업무 진행 현황의 산출물 자료(frontend/data/artifacts.json) — 실제 결과 파일과 코드에서 계산한다 (모델 호출 없음).

    python tools/progress_artifacts.py            # 다시 계산해 저장
    python tools/progress_artifacts.py --check    # 저장된 파일과 다시 계산한 값이 같은지 (다르면 1, 무엇이 다른지 출력)

- 공개 자료: 저장소 안 질문 세트 300·수집 결과(참고자료/수집결과/2026-10-14-질문세트300), 질문 생성 프롬프트(webapp/question_generation.py),
  기준 목록(code_eval/criteria_registry.py), 적용 규칙(tools/check_question_set.py·min_question_count.py).
- 비공개 자료: 저장 실행 data/comparison_runs/e3c60d3efd18.json (저장소 밖). 개수·비율 같은 공개 요약만 남기고 질문·답변 원문은 넣지 않는다.
  이 파일이 없는 PC 에서 다시 만들면 저장된 artifacts.json 의 그 산출물을 그대로 둔다 (원본 없이 지어내지 않는다).
- 이전 기록: 같은 실행의 재시도 전 AIONA 기록은 git 커밋 0346246 의 answers.json 에서 읽는다(이력으로만, git 이력이 없으면 저장된 요약 유지).
- 우영 통합 요약(overview): 위 산출물에서 A 질문 생성 · B 답변 수집 · C 평가 연동 세 묶음의 수치·상태를 모은다.
- 표·그래프 수치는 모두 여기서 계산한 값이다. 화면(frontend/src/progress-workspace.js)은 이 값을 그대로 그린다.
- 판정 정확도는 사람 검수 결과가 있어야 채운다 — 이 도구는 정확도를 만들지 않는다.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import random
import re
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ARTIFACTS_VERSION = "progress-artifacts-v1"
OUT = ROOT / "frontend" / "data" / "artifacts.json"
SET_DIR = "참고자료/수집결과/2026-10-14-질문세트300"
QUESTIONS = f"{SET_DIR}/questions.json"
ANSWERS = f"{SET_DIR}/answers.json"
SAVED_RUN_ID = "e3c60d3efd18"
SAVED_RUN = f"data/comparison_runs/{SAVED_RUN_ID}.json"
GITHUB = "https://github.com/yunjae305/ModelGrade/blob/"
KST = timezone(timedelta(hours=9))
TARGET_SHORT = 30          # 잠정 운영 기준(사전 점검 기본값) — 통계적 필요량이 아니다
LEVELS = ("none", "implemented", "auto_tested", "measured", "real_run", "accuracy_verified")


# --- git · 시각 ---------------------------------------------------------------------------------

def _git(*args: str) -> Optional[str]:
    try:
        done = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def revision(*paths: str) -> str:
    """그 파일들을 마지막으로 바꾼 커밋 (커밋하지 않은 수정이 있으면 표시)."""
    rev = _git("log", "-1", "--format=%h", "--", *paths) or "기록 없음"
    dirty = _git("status", "--porcelain", "--", *paths)
    return rev + (" + 커밋 전 수정" if dirty else "")


def committed_at(rev: str) -> Optional[str]:
    value = _git("show", "-s", "--format=%cI", rev.split(" ")[0]) if rev and rev != "기록 없음" else None
    return kst(value) if value else None


def github(path: str, rev: str) -> Optional[str]:
    """원격 브랜치에 있는 커밋만 링크한다 (푸시하지 않은 커밋은 열리지 않는다)."""
    rev = rev.split(" ")[0]
    if not rev or rev == "기록 없음" or not _git("branch", "-r", "--contains", rev):
        return None
    return GITHUB + rev + "/" + quote(path)


def kst(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")


def _json(path: str) -> Any:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _cut(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _pct(part: int, whole: int) -> Optional[float]:
    return round(part / whole, 4) if whole else None


def repo_ref(label: str, path: str, rev: Optional[str] = None) -> Dict[str, Any]:
    rev = rev or revision(path)
    return {"label": label, "kind": "repo_file", "path": path, "visibility": "public", "revision": rev, "url": github(path, rev)}


def saved_run_ref() -> Dict[str, Any]:
    return {"label": f"저장 실행 {SAVED_RUN_ID} (비공개 원본)", "kind": "saved_run", "path": SAVED_RUN, "run_id": SAVED_RUN_ID,
            "visibility": "private"}


def artifact(artifact_id: str, requirement_id: str, title: str, kind: str, *, description: str, result_summary: str,
             data_source: Dict[str, Any], created_at: Optional[str], code_revision: str, level: str, level_note: str,
             limitations: Sequence[str], references: Sequence[Dict[str, Any]], content: Dict[str, Any]) -> Dict[str, Any]:
    assert level in LEVELS, level
    return {"artifact_id": artifact_id, "requirement_id": requirement_id, "title": title, "type": kind,
            "description": description, "result_summary": result_summary, "data_source": data_source,
            "created_at": created_at, "code_revision": code_revision,
            "verification_status": {"level": level, "note": level_note},
            "limitations": list(limitations), "file_or_result_reference": list(references), "content": content}


# --- 질문 세트 300 ------------------------------------------------------------------------------

class QuestionSet:
    """질문 세트 300과 수집 결과, 평가 때와 같은 코드 규칙으로 센 적용 여부."""

    def __init__(self) -> None:
        from tools.check_question_set import check_items, load_items
        self.groups: List[Dict[str, Any]] = _json(QUESTIONS)
        self.answers: Dict[str, Any] = _json(ANSWERS)
        self.items = load_items(ROOT / QUESTIONS)
        self.report = check_items(self.items, pipeline="items")
        self.meta = self.answers["meta"]
        self.exported = kst(self.meta.get("exported_at"))
        self.data_rev = revision(QUESTIONS, ANSWERS)

    def source(self) -> Dict[str, Any]:
        return {"label": "질문 세트 300 · AIONA·GPT·Gemini 답변 원문 (팀원 수집, 평가 없음)", "kind": "repo_file", "path": SET_DIR,
                "visibility": "public", "revision": self.data_rev, "run_id": self.meta.get("run_id"),
                "generator": self.meta.get("question_generator"), "generator_model": self.meta.get("generator_model")}


def _group_row(group: Dict[str, Any]) -> Dict[str, Any]:
    turns = group.get("turns") or []
    kinds = sorted({a.get("type") for t in turns for a in t.get("attachments") or [] if a.get("type")})
    return {"id": group["id"], "type": group.get("type") or "", "topic": group.get("topic") or "", "difficulty": group.get("difficulty") or "",
            "turns": len(turns), "attachments": "·".join(kinds), "long_reference": any(t.get("reference") for t in turns),
            "targets": len(group.get("target_items") or []), "target_items": " ".join(group.get("target_items") or []),
            "trap": _cut(group.get("trap"), 120), "question": _cut(turns[0].get("question") if turns else "", 160)}


def _sample(group: Dict[str, Any], why: str) -> Dict[str, Any]:
    turns = group.get("turns") or []
    shown = turns if len(turns) <= 4 else turns[:3]
    out = []
    for t in shown:
        entry = {"turn": t.get("turn"), "question": _cut(t.get("question"), 600)}
        if t.get("attachments"):
            entry["attachments"] = [f"{a.get('name')} ({a.get('type')})" for a in t["attachments"]]
        if t.get("reference"):
            entry["reference_chars"] = len(t["reference"])
        out.append(entry)
    return {"id": group["id"], "why": why, "type": group.get("type"), "difficulty": group.get("difficulty"),
            "target_items": group.get("target_items") or [], "trap": group.get("trap"), "turns_total": len(turns),
            "turns": out, "omitted_turns": len(turns) - len(shown)}


def _samples(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rules = [
        ("빈 출력 지시 — 아무것도 쓰지 말아야 PASS", lambda g: "2-1.explicit_empty" in (g.get("target_items") or [])),
        ("계산 · 어려움", lambda g: g.get("type") == "계산" and g.get("difficulty") == "어려움"),
        ("표 형식 요구", lambda g: any(t.startswith("2-2.table") for t in g.get("target_items") or [])),
        ("첨부파일(PDF)", lambda g: any(a.get("type") == "pdf" for t in g["turns"] for a in t.get("attachments") or [])),
        ("첨부파일(이미지)", lambda g: any(a.get("type") == "image" for t in g["turns"] for a in t.get("attachments") or [])),
        ("짧은 멀티턴(2~4턴)", lambda g: 2 <= len(g["turns"]) <= 4),
        ("10턴 대화(J6)", lambda g: len(g["turns"]) >= 10),
        ("긴 참조 자료(J7)", lambda g: any(t.get("reference") for t in g["turns"])),
    ]
    picked, seen = [], set()
    for why, rule in rules:
        group = next((g for g in groups if g["id"] not in seen and rule(g)), None)
        if group is not None:
            seen.add(group["id"])
            picked.append(_sample(group, why))
    return picked


# --- W01 질문 생성 ------------------------------------------------------------------------------

GEN_HISTORY = [
    ("question-gen-v1", "362f8c4", "CODE 검사 인식 규칙을 프롬프트에 넣고 보충 때는 규칙 전체"),
    ("question-gen-v1.1", "d1bbcfd", "J6 붙여 넣은 자료에 끝 표시 — 자료를 지시로 읽지 않게"),
    ("question-gen-v1.2", "4781448", "세트 전체 질문 수 지정, 변별력(trap·난도), 지난 실행에서 전부 PASS 한 단위 되먹임 — 질문 세트 300 을 만든 판"),
    ("question-gen-v1.2", "1466c41", "질문 수백 개 지정 시 40개씩 나눠 계획 (상한 80 → 500)"),
    ("question-gen-v1.3", "78026ea", "10턴 대화·긴 문서 질문을 정한 수만큼 전용 단계로"),
    ("question-gen-v1.4", "240c3fd", "의미 평가 점검 항목 44개를 세부 기준·판정 근거·펼침·FAIL 예까지 프롬프트에"),
    ("question-gen-v1.4", "8648cff", "세부 평가 기준마다 적용 수를 채움(per_target), 멀티턴 분량은 질문에 쓰지 않음"),
]


def w01_prompt(qs: QuestionSet) -> Dict[str, Any]:
    from webapp import question_generation as qg
    catalog = qg.criteria_catalog()
    target = 300
    multi, long_ = qg.split_counts(target)
    first = min(qg.PLAN_BATCH, target - multi - long_)
    topics = [g.get("topic") or "" for g in qs.groups]
    never = [k for k, n in qg.target_counts(qs.report).items() if n == 0]
    low = qg._low_coverage(qs.report, 30, True, 30)
    texts = [("계획 — 첫 묶음", f"plan_prompt(target_questions={first}, dedicated=True)",
              qg.plan_prompt(catalog, "", target_questions=first, dedicated=True)),
             ("10턴 대화(J6) 전용 — 한 번에 4개", f"multi_turn_prompt({qg.MULTI_TURN_BATCH}, 기존 주제 없음)",
              qg.multi_turn_prompt(qg.MULTI_TURN_BATCH, [], catalog)),
             ("긴 문서(J7) 전용", f"long_context_prompt({long_})", qg.long_context_prompt(long_, catalog)),
             # 4~7: 앞 단계 결과로 내용이 달라지는 프롬프트 — 질문 세트 300 점검 결과·예시 주제를 넣어 만든 원문 (실제 호출 원문 아님)
             ("보충 — 예시 입력(질문 세트 300 에서 한 번도 적용 안 된 조건)", f"supplement_prompt({never}, 질문 세트 300 주제)",
              qg.supplement_prompt(never, topics, catalog)),
             ("추가 — 예시 입력(질문 세트 300 에서 기준마다 30개 적용까지 모자란 기준)", f"extra_prompt({qg.PLAN_BATCH}, 300, per_target=30)",
              qg.extra_prompt(qg.PLAN_BATCH, len(qs.groups), None, topics, qg.general_catalog(catalog), (), low, per_target=30)),
             ("긴 문서 본문 조각 — 예시 입력", "_material_prompt('가상 도시의 대중교통 개편 보고서', 1, 40)",
              qg._material_prompt("가상 도시의 대중교통 개편 보고서", 1, 40, qg.CHUNK_CHARS, ["(예시) 개편안의 시행일은 가상의 2031년 3월 1일이다."])),
             ("붙여 넣을 자료 — 예시 입력", "_paste_prompt({topic: '가상 박람회 준비 회의록'})",
              qg._paste_prompt({"topic": "가상 박람회 준비 회의록", "must_include": ["(예시) 참가자 36명", "(예시) 예산 상한 48만 원"]}, qg.PASTE_CHARS))]
    prompts = [{"name": name, "call": call, "chars": len(text), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                "text": text} for name, call, text in texts]
    paths = ("webapp/question_generation.py", "code_eval/criteria_registry.py", "code_eval/semantic_items.py",
             "code_eval/settings/code_check_conditions.json")
    rev = revision(*paths)
    settings = [("세트 질문 수 예시", target, "화면에서 정한 수(대화 1개 = 1개)"),
                ("10턴 대화 수", multi, f"질문 수 × {qg.MULTI_TURN_RATIO:.0%} (값을 정하면 그 값)"),
                ("긴 문서 질문 수", long_, f"질문 수 × {qg.LONG_CONTEXT_RATIO:.0%}, 긴 자료는 세트당 {qg.MAX_LONG_REFERENCES}개"),
                ("한 응답의 계획 질문 수", qg.PLAN_BATCH, "수백 개를 한 응답에 쓰면 출력 한도에 잘림"),
                ("질문 하나가 노리는 항목 상한", qg.MAX_ITEMS_PER_QUESTION, "질문이 부자연스럽지 않게"),
                ("보충 회차", qg.SUPPLEMENT_ROUNDS, "한 번도 적용되지 않은 항목·첨부 종류만"),
                ("세트 질문 수 상한", qg.MAX_PLAN_QUESTIONS, ""),
                ("GPT 호출 상한(기본)", qg.MAX_CALLS, "질문 수를 정하면 2 × 질문 수 + 150 까지")]
    return artifact(
        "W01-PROMPT", "W01", f"질문 생성 프롬프트 원문 ({qg.QUESTION_GEN_VERSION})", "prompt",
        description="질문 생성 버튼이 GPT 에 보내는 실제 프롬프트입니다. 질문 300개를 요청할 때의 첫 호출 세 가지를 코드로 다시 만들어 그대로 담았습니다.",
        result_summary=(f"{qg.QUESTION_GEN_VERSION}: 긴 문서(J7) {long_}개 → 10턴 대화(J6) {multi}개 → 일반 질문 {qg.PLAN_BATCH}개씩 계획 → "
                        "첨부·자료 생성 → 평가와 같은 코드 규칙으로 점검 → 빠진 항목 보충 → 목표 수까지 추가. "
                        f"프롬프트 {len(prompts)}종(질문 300개 요청 때 처음 보내는 대표 3종 + 상황에 따라 내용이 바뀌는 4종 예시) · 계획 프롬프트 {prompts[0]['chars']:,}자."),
        data_source={"label": "코드에서 다시 만든 프롬프트 (모델 호출 없음)", "kind": "code", "path": paths[0], "visibility": "public",
                     "revision": rev},
        created_at=committed_at(rev), code_revision=rev, level="auto_tested",
        level_note="프롬프트·생성 흐름은 tests/test_question_generation.py 가 모의 GPT 로 확인. 이 판(v1.4)으로 실제 생성한 결과는 저장소에 없음 — 실측 미완료.",
        limitations=["질문 세트 300 은 이전 판 question-gen-v1.2 로 만든 결과다. v1.4 프롬프트의 실제 생성 품질은 아직 확인하지 않았다.",
                     "생성 품질(중복·모호함·평가 가능성)을 사람이 검수한 기록이 없다.",
                     "4~7번(보충·추가·자료 본문)은 앞 단계 결과로 내용이 달라져, 질문 세트 300 점검 결과와 예시 주제를 넣어 만든 원문이다 — 실제로 보낸 원문이 아니다."],
        references=[repo_ref("생성기 코드", paths[0], revision(paths[0])), repo_ref("생성기 테스트", "tests/test_question_generation.py")],
        content={"version": qg.QUESTION_GEN_VERSION, "model_setting": "연결된 GPT (.env OPENAI_MODEL) — 질문 세트 300 은 gpt-6-luna",
                 "prompts": prompts,
                 "stages": [f"긴 문서 질문(J7): 272K 토큰 이상 문서 1개를 만들고 그 문서를 함께 쓰는 질문 {long_}개",
                            f"10턴 대화(J6): 한 번에 {qg.MULTI_TURN_BATCH}개씩 {multi}개, J6 항목을 대화마다 고르게",
                            f"계획: 일반 질문을 {qg.PLAN_BATCH}개씩 — 점검 목록(CODE 검사·인식 규칙, 의미 점검 항목)과 변별력 규칙(trap·난도)",
                            "만들기: 첨부(PDF·DOCX·TXT·이미지)·붙여 넣는 자료를 실제 파일·글로",
                            "점검: 평가와 같은 코드 규칙(tools/check_question_set.py)으로 질문마다 적용 기준 계산",
                            f"보충: 한 번도 적용되지 않은 항목·첨부 종류만 최대 {qg.SUPPLEMENT_ROUNDS}회",
                            f"추가: 목표 질문 수(또는 항목별 목표 적용 수)까지 {qg.PLAN_BATCH}개씩, 적게 덮인 항목·변별력 보강 우선"],
                 "settings": [{"name": n, "value": v, "meaning": m} for n, v, m in settings],
                 "output_fields": [{"field": f, "meaning": m} for f, m in (
                     ("id", "질문(대화) 번호"), ("type · topic", "질문 유형·주제"),
                     ("target_items", "노린 평가 기준 id (CODE 검사 또는 의미 점검 항목)"),
                     ("difficulty · trap", "난도(쉬움·보통·어려움)와 FAIL 이 되는 답 한 줄"),
                     ("turns[].question", "질문 원문 (대화면 턴마다)"),
                     ("turns[].attachments", "첨부파일 이름·종류·내용 (저장 때 파일 경로·sha256)"),
                     ("turns[].long_reference · long_paste", "긴 문서·붙여 넣는 자료 명세 (저장 때 reference·질문 앞 자료로)"))],
                 "history": [{"version": v, "commit": c, "summary": s, "url": github("webapp/question_generation.py", c)}
                             for v, c, s in GEN_HISTORY]})


def w01_set(qs: QuestionSet) -> Dict[str, Any]:
    groups = qs.groups
    rows = [_group_row(g) for g in groups]
    turns = collections.Counter(len(g["turns"]) for g in groups)
    difficulty = collections.Counter(g.get("difficulty") for g in groups)
    attach = collections.Counter(a.get("type") for g in groups for t in g["turns"] for a in t.get("attachments") or [])
    types = collections.Counter(g.get("type") for g in groups)
    hard = difficulty.get("어려움", 0)
    stats = [{"label": "질문(대화)", "value": len(groups)}, {"label": "턴", "value": sum(n * c for n, c in turns.items())},
             {"label": "어려움 비율", "value": f"{hard / len(groups):.0%}", "note": "생성 규칙: 3분의 1 이상"},
             {"label": "첨부 질문", "value": sum(1 for g in groups if any(t.get("attachments") for t in g["turns"]))},
             {"label": "10턴 대화", "value": turns.get(10, 0)}, {"label": "긴 문서 질문", "value": sum(r["long_reference"] for r in rows)}]
    return artifact(
        "W01-SET300", "W01", "실제 생성된 질문 세트 300 (question-gen-v1.2)", "data",
        description="팀원이 질문 생성기 v1.2 로 질문 수 300 을 지정해 GPT 가 만든 실제 결과입니다. 질문마다 노린 평가 항목·난도·FAIL 함정을 함께 보여 줍니다.",
        result_summary=(f"질문(대화) {len(groups)}개 · 턴 {sum(n * c for n, c in turns.items())}개 · 난도 쉬움 {difficulty.get('쉬움', 0)} · 보통 {difficulty.get('보통', 0)} · "
                        f"어려움 {hard}. 1턴 {turns.get(1, 0)} · 2~4턴 {sum(c for n, c in turns.items() if 2 <= n <= 4)} · 10턴 {turns.get(10, 0)}. "
                        f"첨부 {sum(attach.values())}개({', '.join(f'{k} {v}' for k, v in sorted(attach.items()))})."),
        data_source=qs.source(), created_at=qs.exported, code_revision=qs.data_rev, level="real_run",
        level_note=f"실제 GPT({qs.meta.get('generator_model')}) 호출로 만든 질문. 사람이 질문 품질을 검수한 기록은 없음.",
        limitations=["현재 생성기(v1.5)가 아니라 v1.2 결과다 — 기준별 목표 적용 수 채우기·10턴 대화·긴 문서 질문 수 지정 전.",
                     "질문이 의도한 대로 모델을 가르는지(변별력)는 평가를 돌려야 알 수 있다 — 이 세트는 아직 평가하지 않았다."],
        references=[repo_ref("질문 원본 questions.json", QUESTIONS, qs.data_rev), repo_ref("세트 설명 README", f"{SET_DIR}/README.md")],
        content={"stats": stats,
                 "table": {"columns": [{"key": "id", "label": "번호", "numeric": True}, {"key": "type", "label": "유형"},
                                       {"key": "difficulty", "label": "난도"}, {"key": "turns", "label": "턴", "numeric": True},
                                       {"key": "attachments", "label": "첨부"}, {"key": "targets", "label": "노린 항목 수", "numeric": True},
                                       {"key": "target_items", "label": "노린 항목"}, {"key": "trap", "label": "FAIL 함정"},
                                       {"key": "question", "label": "첫 질문(앞부분)"}],
                           "rows": rows, "filters": ["type", "difficulty", "turns"], "download": "question-set-300.csv"},
                 "distribution": {"turns": dict(sorted((str(k), v) for k, v in turns.items())),
                                  "difficulty": {k: difficulty.get(k, 0) for k in ("쉬움", "보통", "어려움")},
                                  "attachments": dict(sorted(attach.items())), "types_top": types.most_common(10)},
                 "samples": _samples(groups), "cases": _cases(qs)})


def w01_intent(qs: QuestionSet) -> Dict[str, Any]:
    """생성기가 질문마다 노린 항목(target_items)이 평가와 같은 코드 규칙에서 실제로 적용됐는지."""
    from tools.check_question_set import ANSWER_DEPENDENT, ANSWER_DEPENDENT_ITEMS
    cov = qs.report["coverage"]
    known = set(cov["checks"]) | set(cov["items"]) | {i.split(".")[0] for i in cov["items"]}
    answer_dependent = set(ANSWER_DEPENDENT) | set(ANSWER_DEPENDENT_ITEMS)
    applied: Dict[int, set] = collections.defaultdict(set)
    for row in qs.report["cases"]:
        gid = (((qs.items[row["index"] - 1].get("import_source") or {}).get("group") or {}).get("id"))
        found = set(row.get("checks") or []) | set(row.get("items") or [])
        found |= {i.split(".")[0] for i in row.get("items") or []}
        applied[gid] |= found
    families: Dict[str, Dict[str, int]] = collections.OrderedDict()
    misses: collections.Counter = collections.Counter()
    totals = collections.Counter()
    for g in qs.groups:
        for target in g.get("target_items") or []:
            family = target.split("-")[0] if target.startswith("J") else "CODE " + target.split("-")[0]
            box = families.setdefault(family, {"intended": 0, "applied": 0, "answer_dependent": 0, "unknown": 0})
            box["intended"] += 1
            totals["intended"] += 1
            if target not in known:
                box["unknown"] += 1
                totals["unknown"] += 1
            elif target in answer_dependent:
                box["answer_dependent"] += 1
                totals["answer_dependent"] += 1
            elif target in applied[g["id"]]:
                box["applied"] += 1
                totals["applied"] += 1
            else:
                misses[target] += 1
    judged = totals["intended"] - totals["answer_dependent"] - totals["unknown"]
    order = sorted(families, key=lambda f: (not f.startswith("CODE"), f))
    rows = []
    for family in order:
        box = families[family]
        base = box["intended"] - box["answer_dependent"] - box["unknown"]
        rows.append({"family": family, "intended": box["intended"], "applied": box["applied"], "missed": base - box["applied"],
                     "answer_dependent": box["answer_dependent"], "rate": _pct(box["applied"], base)})
    rate = _pct(totals["applied"], judged)
    j6 = families.get("J6", {"intended": 0, "applied": 0, "answer_dependent": 0, "unknown": 0})
    j6_base = j6["intended"] - j6["answer_dependent"] - j6["unknown"]
    rest_rate = _pct(totals["applied"] - j6["applied"], judged - j6_base)
    explained = _intent_explained(qs, rows, misses, answer_dependent, applied)
    return artifact(
        "W01-INTENT", "W01", "생성기가 노린 항목이 실제로 적용됐는가", "experiment",
        description=("질문마다 생성기가 노렸다고 적은 평가 항목(target_items)을, 평가 때와 같은 코드 규칙으로 계산한 실제 적용 항목과 대조했습니다. "
                     "질문 문장이 인식 규칙에 맞지 않으면 노린 항목이 적용되지 않습니다."),
        result_summary=(f"노린 항목 {totals['intended']}건 중 판단 가능한 {judged}건에서 {totals['applied']}건({rate:.1%})이 실제로 적용됨. "
                        f"적용 안 된 {judged - totals['applied']}건 중 {j6_base - j6['applied']}건이 J6 — 2~4턴 짧은 대화에 J6 를 노렸지만 J6 는 10턴 이상 대화에만 적용된다. "
                        f"J6 를 빼면 적용률 {rest_rate:.1%}."),
        data_source=qs.source(), created_at=qs.exported, code_revision=revision("tools/check_question_set.py"), level="measured",
        level_note="실제 생성 결과를 코드 규칙으로 계산 (모델 호출 없음). 의미 평가 항목 일부는 평가 모델이 적용 여부를 정해 실제 평가와 다를 수 있음.",
        limitations=["의미 평가 항목은 코드 규칙이 '보낼지'만 정한다 — 평가 모델이 해당 없음으로 볼 수 있어 실제 판정 수와 다를 수 있다.",
                     "노린 항목이 적용됐다고 질문이 좋은 질문인 것은 아니다(변별력·명확성은 별도 확인)."],
        references=[repo_ref("질문 원본 questions.json", QUESTIONS, qs.data_rev), repo_ref("적용 규칙", "tools/check_question_set.py")],
        content={"stats": [{"label": "노린 항목(건)", "value": totals["intended"]}, {"label": "실제 적용", "value": totals["applied"]},
                           {"label": "적용 안 됨", "value": judged - totals["applied"]}, {"label": "적용률", "value": f"{rate:.1%}"},
                           {"label": "J6 제외 적용률", "value": f"{rest_rate:.1%}"},
                           {"label": "답변 필요", "value": totals["answer_dependent"]}],
                 "charts": [{"kind": "bar", "title": "기준별 '노린 항목이 실제로 적용된 비율'", "unit": "%", "max": 100, "label_width": 170,
                             "categories": [r["family"] for r in rows], "category_labels": _family_labels([r["family"] for r in rows]),
                             "series": [{"name": "적용률", "values": [round(r["rate"] * 100, 1) if r["rate"] is not None else None for r in rows]}]}],
                 "table": {"columns": [{"key": "family", "label": "기준"}, {"key": "intended", "label": "노린 건수", "numeric": True},
                                       {"key": "applied", "label": "실제 적용", "numeric": True}, {"key": "missed", "label": "적용 안 됨", "numeric": True},
                                       {"key": "answer_dependent", "label": "답변 필요", "numeric": True}, {"key": "rate", "label": "적용률", "percent": True}],
                           "rows": rows, "download": "w01-intent-by-criterion.csv"},
                 "funnel": {"intended": totals["intended"], "answer_dependent": totals["answer_dependent"], "unknown": totals["unknown"],
                            "judged": judged, "applied": totals["applied"], "missed": judged - totals["applied"]},
                 **explained,
                 "notes": ["가장 자주 빗나간 항목: " + ", ".join(f"{t} {n}건" for t, n in misses.most_common(8))]})


def _intent_explained(qs: QuestionSet, rows: List[Dict[str, Any]], misses: collections.Counter, answer_dependent: set,
                      applied: Dict[int, set]) -> Dict[str, Any]:
    """적용률 표의 기준마다 한국어 이름·주요 원인, J6 불일치를 대화 길이·생성 단계로 나눈 값."""
    from code_eval import multi_turn
    from code_eval.semantic_items import ITEMS
    names = {f["id"]: f["name"] for f in family_glossary()}
    target_names = _target_names()
    applies = {i.id: i.applies for i in ITEMS}
    by_family: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for target, n in misses.items():
        by_family[target.split("-")[0] if target.startswith("J") else "CODE " + target.split("-")[0]][target] = n
    j6_groups = [g for g in qs.groups if any(t.startswith("J6") for t in g.get("target_items") or [])]
    bins = [("10턴 이상 대화", lambda n: n >= multi_turn.MIN_TURNS), ("2~4턴 짧은 대화", lambda n: 2 <= n < multi_turn.MIN_TURNS),
            ("1턴 단일 질문", lambda n: n == 1)]
    j6_bins = []
    for label, test in bins:
        groups = [g for g in j6_groups if test(len(g["turns"]))]
        targets = [(g, t) for g in groups for t in g["target_items"] if t.startswith("J6")]
        j6_bins.append({"label": label, "conversations": len(groups), "targets": len(targets),
                        "applied": sum(1 for g, t in targets if t in applied[g["id"]])})
    short = [g for g in j6_groups if 2 <= len(g["turns"]) < multi_turn.MIN_TURNS]
    from webapp.question_generation import PLAN_BATCH
    head = [g for g in qs.groups if g["id"] <= PLAN_BATCH]
    head_conv = sum(1 for g in head if len(g["turns"]) > 1)
    head_long = sum(1 for g in head if len(g["turns"]) >= multi_turn.MIN_TURNS)
    out_rows = []
    for row in rows:
        family = row["family"]
        top = by_family[family].most_common(1)
        if family == "J6":
            cause = (f"2~4턴 짧은 대화 {len(short)}개에 J6 를 노렸지만 평가기는 {multi_turn.MIN_TURNS}턴 이상 · 대화 전체 "
                     f"{multi_turn.MIN_TOTAL_TOKENS:,}토큰 이상인 대화에만 J6 를 적용한다 (생성 조건과 평가 조건의 불일치)")
        elif not top:
            cause = "빗나간 건 없음"
        elif family.startswith("CODE"):
            cause = f"질문 문장이 평가기 인식 규칙과 달라 검사가 켜지지 않음 — 가장 많이: {target_names.get(top[0][0], top[0][0])} {top[0][1]}건"
        else:
            cause = (f"질문에 평가기가 찾는 요구 표현이 없어 적용 안 됨 — 가장 많이: {target_names.get(top[0][0], top[0][0])} {top[0][1]}건 "
                     f"(적용 조건: {_cut(applies.get(top[0][0]), 40)})")
        out_rows.append({**row, "name": names.get(family, ""), "cause": cause})
    return {"families": out_rows,
            "j6": {"targets": sum(b["targets"] for b in j6_bins), "applied": sum(b["applied"] for b in j6_bins), "bins": j6_bins,
                   "short_conversations": len(short), "short_ids": [min(g["id"] for g in short), max(g["id"] for g in short)] if short else None,
                   "rule": f"{multi_turn.MIN_TURNS}턴 이상 · 대화 전체(질문+답변) {multi_turn.MIN_TOTAL_TOKENS:,}토큰 이상 ({multi_turn.MULTI_TURN_POLICY_VERSION})",
                   "where": (f"J6 를 노린 2~4턴 짧은 대화 {len(short)}개를 만들었습니다(Q{min(g['id'] for g in short)}~Q{max(g['id'] for g in short)}). "
                             f"첫 계획 {PLAN_BATCH}개(Q1~Q{PLAN_BATCH})에는 대화가 {head_conv}개(10턴 대화 {head_long}개)뿐이라, "
                             f"{'짧은 대화는 모두 첫 계획 뒤 보충·추가 단계에서 나왔습니다. ' if min(g['id'] for g in short) > PLAN_BATCH else ''}"
                             "v1.2 의 '질문 추가' 요청문에는 10턴 조건이 없었습니다(코드 776f3bd).")
                            if short else ""}}


def w01_code() -> Dict[str, Any]:
    paths = ["webapp/question_generation.py", "frontend/src/question-generation.js", "tools/check_question_set.py", "tools/min_question_count.py"]
    tests = ["tests/test_question_generation.py", "tests/test_check_question_set.py", "tests/test_min_question_count.py"]
    rev = revision(*paths)
    return artifact(
        "W01-CODE", "W01", "질문 생성 기능 구현과 자동 테스트", "code",
        description="질문 생성 버튼 → GPT 계획 → 첨부·자료 생성 → 코드 규칙 점검 → 보충·추가 → 질문 확인 → 평가 시작까지의 구현 위치와 테스트입니다.",
        result_summary=f"구현 파일 {len(paths)}개, 관련 테스트 {sum(_test_count(t) for t in tests)}개(모의 GPT·저장 데이터). 실제 GPT 호출은 테스트에 없음.",
        data_source={"label": "저장소 코드", "kind": "code", "path": paths[0], "visibility": "public", "revision": rev},
        created_at=committed_at(rev), code_revision=rev, level="auto_tested",
        level_note="자동 테스트는 모의 GPT 응답으로 흐름·형식·상한을 확인한다. 실제 생성 품질은 확인하지 않는다.",
        limitations=["질문 생성은 유료 GPT 호출이라 이번 작업에서 실행하지 않았다(승인 필요)."],
        references=[repo_ref(p, p) for p in paths],
        content={"features": ["화면 '새 비교 → 질문 생성': 질문 수·10턴 대화 수·긴 문서 질문 수·항목별 목표 적용 수·추가 요청",
                              "생성 뒤 이용자가 질문을 확인하고 '평가 시작'을 눌러야 수집·평가 (2단계)",
                              "질문 직접 넣기(question-input-v1): 붙여 넣은 질문·파일을 같은 질문 세트 형식으로",
                              "지난 실행에서 모든 모델이 PASS 한 기준을 다음 생성에 더 어렵게 요청(변별력 되먹임)"],
                 "files": [{"path": p, "url": github(p, revision(p))} for p in paths],
                 "tests": [{"path": t, "count": _test_count(t)} for t in tests],
                 "commits": [{"hash": c, "summary": f"{v} — {s}", "url": github("webapp/question_generation.py", c)} for v, c, s in GEN_HISTORY]})


def _test_count(path: str) -> int:
    file = ROOT / path
    return sum(1 for line in file.read_text(encoding="utf-8").splitlines() if line.startswith("def test_")) if file.is_file() else 0


# --- W01 설명 자료 — 처음 보는 사람이 '왜·어떻게·몇 개'를 이해하도록 (수치는 아래에서 계산, 쉬운 설명만 손으로 씀) ------------

#: 상위 기준 11개의 쉬운 설명 — (무엇을 확인하나, 어떤 질문이면 적용되나, 어떤 답이 FAIL 인가). 공식 이름·정의는 기준 목록에서 읽는다.
FAMILY_GUIDE = {
    "CODE 1": ("답이 깨지지 않고 제대로 나왔는지 프로그램이 확인합니다. 빈 답, 깨진 글자, 문법이 틀린 코드·JSON, 번호만 있고 연결이 끊긴 각주를 찾습니다.",
               "모든 답변(빈 답·깨진 글자) · 코드나 JSON 을 요청한 질문 · 각주·출처 번호를 달게 한 질문",
               "답이 비어 있거나, 요청한 JSON 이 문법 오류로 읽히지 않는 답"),
    "CODE 2": ("질문이 정한 개수·글자 수·꼭 넣을 말·표 모양 같은 조건을 지켰는지 프로그램이 셉니다.",
               "'3문장으로', '200자 이내', '열이 품목·가격인 표로', '아무것도 출력하지 마'처럼 조건을 숫자·형식으로 분명히 적은 질문",
               "'3문장'이라고 했는데 4문장을 쓰거나, '아무것도 출력하지 마'인데 인사말을 붙인 답"),
    "CODE 3": ("계산 결과나 자료를 정리한 결과가 맞는지 프로그램이 직접 다시 계산해 봅니다.",
               "'18,000원의 15%는 얼마야?', '이 목록에서 중복 없이 뽑아 줘'처럼 정답을 계산·대조할 수 있는 질문",
               "계산값이 틀리거나, 중복을 빼라고 했는데 같은 값이 남아 있는 답"),
    "J1": ("질문이 시킨 일을 실제로 했는지 봅니다. 비교하라면 비교했는지, 추천하라면 추천했는지.",
           "모든 질문", "비교해 달라는데 각각 설명만 하고 끝낸 답"),
    "J2": ("질문이 정한 대상·말투·조건에 맞게 답했는지 봅니다.",
           "'초등학생에게', '존댓말로', '~는 빼고'처럼 대상·말투·제약을 적은 질문",
           "초등학생에게 설명하라는데 전문 용어로 가득한 답, 빼라고 한 것을 넣은 답"),
    "J3": ("이유와 결론이 서로 맞는지, 앞뒤 말이 모순되지 않는지 봅니다.",
           "이유·원리·풀이·계획을 묻는 질문",
           "앞에서는 A가 낫다고 하고 결론에서 B를 고르는 답, 같은 사람을 같은 시간에 두 곳에 배정한 계획"),
    "J4": ("꼭 필요한 내용이 빠지지 않았는지 봅니다.",
           "비교·방법·절차·요약을 요청하거나 요구를 여러 개 함께 한 질문", "장단점을 물었는데 장점만 쓴 답"),
    "J5": ("숫자·날짜·이름 같은 사실이 맞는지, 질문에 준 자료를 옮길 때 값을 바꾸지 않았는지 봅니다.",
           "연도·수치·명칭 같은 사실을 묻거나, 자료를 주고 정리·요약하게 하는 질문",
           "틀린 연도를 쓰거나, 질문에 준 금액을 다르게 옮겨 적은 답"),
    "J6": ("긴 대화에서 앞에서 정한 조건과 내용을 끝까지 기억하고 지키는지 봅니다.",
           None,   # 적용 조건은 평가기 코드(multi_turn)에서 만든다 — family_glossary
           "앞 턴에서 '존댓말로'라고 했는데 뒤 턴에서 반말로 돌아가거나, 근거 없는 반박에 맞춰 정한 내용을 바꾼 답"),
    "J7": ("아주 긴 자료 속에서 필요한 정보를 위치와 상관없이 찾아, 여러 곳의 근거를 이어 답하는지 봅니다.",
           None, "자료 중간에 있는 사실을 '없다'고 하거나, 다른 부분을 근거로 든 답"),
    "J8": ("첨부한 파일(PDF·문서·이미지)을 실제로 읽고 답했는지 봅니다.",
           "PDF·DOCX·TXT·이미지를 첨부한 질문", "첨부를 무시하고 일반론만 쓰거나, 표의 행과 열을 섞어 읽은 답"),
}


def family_glossary() -> List[Dict[str, Any]]:
    """CODE 대분류 3개 + 의미 기준 J1~J8 — 공식 이름·정의는 기준 목록(criteria_registry·taxonomy)에서, 쉬운 설명은 FAMILY_GUIDE."""
    from code_eval import long_context, multi_turn
    from code_eval.criteria_registry import registry
    from code_eval.semantic_items import ITEMS, SUBS_BY_ID
    from code_eval.taxonomy import CATEGORIES
    criteria = [c for c in registry()["criteria"] if not c["id"].startswith("AIONA")]
    rows = []
    for cat in CATEGORIES:
        fid = "CODE " + cat["id"]
        easy, applies, fail = FAMILY_GUIDE[fid]
        checks = [c for c in criteria if c["layer"] == "CODE" and c["category"] == cat["id"]]
        rows.append({"id": fid, "layer": "CODE", "name": cat["name"], "official": cat["question"], "easy": easy, "applies": applies, "fail": fail,
                     "parts": " · ".join(s["name"] for s in cat["subs"]),
                     "size": f"소분류 {len(cat['subs'])} · 검사 {len(checks)}"})
    for c in (c for c in criteria if c["layer"] != "CODE"):
        easy, applies, fail = FAMILY_GUIDE[c["id"]]
        if c["id"] == "J6":   # 판정 코드(multi_turn) 기준 — 설명 문자열(APPLIES_WHEN)은 이전 판 표현이 남아 있다
            applies = (f"{multi_turn.MIN_TURNS}턴 이상이고 질문·답변을 합친 대화 전체가 {multi_turn.MIN_TOTAL_TOKENS:,}토큰 이상인 대화의 2번째 턴부터 "
                       f"({multi_turn.MULTI_TURN_POLICY_VERSION}, 모델마다 자기 답변으로 잼)")
        if c["id"] == "J7":
            applies = f"질문과 참조 자료를 합친 입력이 {long_context.MIN_TOKENS:,}토큰(한국어 약 38만 자) 이상인 질문"
        subs = [s for s in SUBS_BY_ID if s.startswith(c["id"] + "-")]
        rows.append({"id": c["id"], "layer": "LLM", "name": c["name"], "official": c["question"], "easy": easy, "applies": applies, "fail": fail,
                     "parts": " · ".join(SUBS_BY_ID[s].name for s in subs),
                     "size": f"세부 기준 {len(subs)} · 점검 항목 {sum(1 for i in ITEMS if i.id.startswith(c['id'] + '-'))}"})
    return rows


def _family_labels(ids: Sequence[str]) -> List[str]:
    """'J6' → 'J6 · 멀티턴 맥락 유지' — 그래프·표에 코드만 보이지 않게 (이름은 기준 목록)."""
    names = {f["id"]: f["name"] for f in family_glossary()}
    return [f"{i} · {names[i]}" if i in names else i for i in ids]


def _target_names() -> Dict[str, str]:
    """목표 id → 한국어 이름 ('2-1.explicit_empty' → 'CODE 2 · 명시 빈 출력', 'J2-3.a' → 'J2 내용 적절성 · 조건·제약 준수')."""
    from code_eval.criteria_registry import reference_details, registry
    from code_eval.semantic_items import ITEMS, SUBS_BY_ID
    checks = {c["id"]: c["name"] for c in reference_details()["code_checks"]}
    families = {c["id"]: c["name"] for c in registry()["criteria"] if c["layer"] != "CODE"}
    names = {cid: f"CODE {cid.split('-')[0]} · {name}" for cid, name in checks.items()}
    names.update({i.id: f"{i.id.split('-')[0]} {families.get(i.id.split('-')[0], '')} · {SUBS_BY_ID[i.sub].name}" for i in ITEMS})
    return names


def number_map() -> List[Dict[str, Any]]:
    """화면 곳곳의 기준 수(28·44·72·64·34 …)가 무엇을 센 것인지 — 모두 기준 목록과 점검 규칙에서 계산.
    평가 기준 단위는 하나: CODE 검사 28 + 의미 점검 항목 44 = 72 (사용자 결정 2026-10-15)."""
    from code_eval.criteria_registry import evaluation_targets
    from code_eval.semantic_items import ITEMS
    from tools.check_question_set import ANSWER_DEPENDENT, ANSWER_DEPENDENT_ITEMS
    from webapp import question_generation as qg
    targets = evaluation_targets()
    counts = targets["counts"]
    always, answer = len(targets["code"]["always_checks"]), len(targets["code"]["answer_checks"])
    question_checks = counts["code_checks"] - always - answer
    dedicated = sum(1 for i in ITEMS if qg._dedicated(i.id))
    general = len(qg.general_catalog(qg.criteria_catalog())["items"])
    items_countable = counts["semantic_items"] - len(ANSWER_DEPENDENT_ITEMS)
    fill = always + question_checks + items_countable
    assert len(ANSWER_DEPENDENT) == answer, "답변 구조 검사 수가 기준 목록과 점검 규칙에서 다르다"
    return [
        {"number": counts["code_checks"], "what": "CODE 검사", "level": "CODE 평가 기준",
         "meaning": f"프로그램이 규칙으로 판정하는 검사. 모든 답에 늘 적용 {always} + 질문 요구로 켜짐 {question_checks} + 답변 구조로 생김 {answer}.",
         "where": "생성 프롬프트 '# 목표' — 'CODE 검사 N개가 모두 최소 1개 질문에서 적용되게'"},
        {"number": counts["semantic_items"], "what": "의미 점검 항목", "level": "LLM 의미 평가 기준",
         "meaning": f"의미 기준 {counts['semantic_criteria']}개(J1~J8) → 세부 기준 {counts['semantic_subs']}개 → 점검 항목 {counts['semantic_items']}개.",
         "where": "질문 수를 정하지 않은 생성 프롬프트 '# 목표'"},
        {"number": counts["total"], "what": "평가 기준 전체", "level": "기준 단위",
         "meaning": f"CODE 검사 {counts['code_checks']} + 의미 점검 항목 {counts['semantic_items']}. 기준 수는 이 하나로 센다.",
         "where": "설계안 머리말 · 기준 목록(criteria_registry) · 서비스 화면"},
        {"number": general, "what": "일반 계획 프롬프트의 의미 항목", "level": "생성 단계 구분",
         "meaning": f"점검 항목 {counts['semantic_items']}개 − 10턴 대화·긴 문서 전용 단계가 맡는 J6·J7 항목 {dedicated}개. 질문 수를 정하면 일반 계획은 이 {general}개만 노린다.",
         "where": "생성 프롬프트(질문 수 지정) '# 목표' — 'CODE 검사 N개와 의미 평가 항목 34개'"},
        {"number": fill, "what": "질문만으로 셀 수 있는 기준", "level": "채울 대상",
         "meaning": f"평가 기준 {counts['total']}개 − 답변 구조로만 생기는 검사 {answer}개 − 답변이 있어야 정해지는 의미 항목 "
                    f"{', '.join(ANSWER_DEPENDENT_ITEMS)}. = CODE 검사 {always + question_checks} + 의미 항목 {items_countable}.",
         "where": f"W02 기준 표 · W03 질문 수 분석 · 생성기 per_target (질문 수를 정하면 일반 단계는 J6·J7 {dedicated}개를 뺀 {fill - dedicated}개)"},
    ]


def _prompt_line(text: str, keyword: str) -> str:
    """프롬프트에서 keyword 가 든 줄 — 프롬프트가 바뀌어 줄이 없어지면 설명도 고치도록 멈춘다."""
    line = next((line.strip() for line in text.splitlines() if keyword in line), None)
    assert line, f"생성 프롬프트에서 '{keyword}' 줄을 찾지 못함 — W01 설명(prompt_parts)을 고칠 것"
    return line


def w01_design(qs: QuestionSet, intent: Dict[str, Any], quantity: Dict[str, Any]) -> Dict[str, Any]:
    """W01 화면 ①②⑤ — 왜 질문을 만드나, 어떤 원리로 만드나, 무엇을 고쳐야 하나. 수치는 코드·실제 세트에서 계산한다."""
    from code_eval import multi_turn
    from code_eval.criteria_registry import evaluation_targets
    from webapp import question_generation as qg
    catalog = qg.criteria_catalog()
    multi, long_ = qg.split_counts(300)
    first = min(qg.PLAN_BATCH, 300 - multi - long_)
    prompt = qg.plan_prompt(catalog, "", target_questions=first, dedicated=True)
    sections = {s.splitlines()[0]: len(s) for s in re.split(r"(?m)^(?=# )", prompt) if s.startswith("# ")}
    checklist = next(n for head, n in sections.items() if head.startswith("# 점검 목록"))
    counts = evaluation_targets()["counts"]
    stats = {s["label"]: s["value"] for s in intent["content"]["stats"]}
    funnel = intent["content"]["funnel"]
    j6 = intent["content"]["j6"]
    gen = {s["label"]: s["value"] for s in w01_set_stats(qs)}
    never = [k for k, n in qg.target_counts(qs.report).items() if n == 0]
    names = _target_names()
    per_question = quantity["content"]["per_question"]
    growth = quantity["content"]["table"]["rows"]
    at50, last = next(r for r in growth if r["questions"] == 50), growth[-1]
    fill = quantity["content"]["growth_targets"]
    dup = quantity["content"]["duplicates"]
    hard_short = quantity["content"]["difficulty"]["hard_short_ids"]
    parts = [
        ("목표 평가 기준", _prompt_line(prompt, "[점검 목록]의 CODE 검사"),
         "GPT 가 무엇을 시험할 질문을 만들어야 하는지 정합니다. 기준 목록이 바뀌면 이 줄과 아래 점검 목록도 자동으로 바뀝니다.",
         "사용자 결정", "10-13 사용자 결정: 세부 평가 항목 전부를 시험할 수 있게, 가장 적은 개수로 (생성기 코드 머리말)"),
        ("질문 작성 규칙", _prompt_line(prompt, "'짧게' 대신"),
         "평가기는 '200자 이내'처럼 숫자·형식으로 적힌 요구만 인식합니다. 모호하게 쓰면 기준이 켜지지 않습니다.",
         "근거 있음", "실제 생성에서 확인 — 인식 규칙에 맞지 않는 표현은 CODE 검사가 적용되지 않았다 (예: 계산 목표 14건 빗나감, 아래 ④)"),
        ("평가기 인식 표현 그대로 쓰기", _prompt_line(prompt, "[인식] 규칙에 맞는 표현"),
         "평가기가 질문에서 요구를 읽는 규칙(인식 규칙)을 프롬프트에 함께 넣어, 그 문장 모양을 따르게 합니다.",
         "근거 있음", "생성기 v1 부터 적용. 그래도 v1.2 세트에서 CODE 3 목표 19건이 빗나갔다 — 완전히 해결되지 않음"),
        ("난이도 조건", _prompt_line(prompt, "difficulty 를"),
         "쉬운 질문만 있으면 모든 AI 가 통과해 차이가 보이지 않습니다. 그래서 어려운 질문을 일정 비율 이상 넣습니다.",
         "가정", f"'어려움 3분의 1 이상' 비율은 실험으로 정한 값이 아님. v1.2 세트의 어려움 비율 {gen['어려움 비율']} — GPT 가 붙인 난도라 사람 확인 필요"),
        ("틀리기 쉬운 지점(trap)", _prompt_line(prompt, "모든 모델이 PASS 하는 질문은"),
         "질문마다 'AI 가 실수하기 쉬운 지점'과 '어떤 답이 FAIL 인지'를 적게 해, 좋은 답과 부족한 답을 구별할 수 있게 합니다.",
         "사용자 결정", "10-14 사용자 결정 '전부 PASS 가 뜨는 질문은 좋은 질문이 아니다'. 실제로 구별되는지는 평가 전이라 미검증"),
        ("한 질문에 여러 기준", _prompt_line(prompt, "한 질문이 여러 항목을 함께"),
         "질문 하나로 여러 기준을 함께 시험하면 질문 수를 줄일 수 있습니다. 너무 많이 넣으면 질문이 부자연스러워져 상한을 둡니다.",
         "멘토 요청", f"10-13 멘토: '한 질문이 여러 항목을 평가하니 더 줄 수 있다'. 상한 {qg.MAX_ITEMS_PER_QUESTION}개는 가정. "
                     f"실제로는 질문 하나에 세부 기준 평균 {per_question['applied_mean']}개가 적용됨(목표로 적은 것은 평균 {per_question['target_mean']}개)"),
        ("기준끼리 충돌 막기", _prompt_line(prompt, "서로 막는 항목은"),
         "'아무것도 출력하지 마'와 '표로 정리해'를 한 질문에 넣으면 둘 다 지킬 수 없습니다. 이런 조합은 다른 질문으로 나눕니다.",
         "가정", "설계 규칙 — 따로 실험한 기록은 없음"),
        ("10턴 대화·긴 문서는 따로 만들기", _prompt_line(prompt, "10턴 대화(J6)와 긴 참조 자료 질문(J7)은 따로"),
         "10턴 대화와 27만 토큰 문서 질문은 만드는 방법이 달라 전용 단계가 정한 수만큼 만듭니다(v1.3~).",
         "근거 있음", f"v1.2 에서는 일반 단계가 J6 를 노린 짧은 대화 {j6['short_conversations']}개를 만들었고 모두 J6 조건에 맞지 않았다(④). "
                     "다만 이 줄은 여전히 짧은 2~4턴 대화를 허용한다 — v1.4 실제 생성으로 확인 필요"),
        ("질문 개수 못 박기", _prompt_line(prompt, "questions 배열에 정확히"),
         "프롬프트 끝에 개수를 다시 적고 '답하기 전에 세어 보라'고 합니다.",
         "근거 있음", "실제 사례: 첫 계획에 40개를 요청하자 31개만 왔다 (776f3bd · 변경내역 10-14 #10)"),
        ("JSON 출력 형식", _prompt_line(prompt, '{"questions": ['),
         "프로그램이 답을 자동으로 읽어 첨부파일을 만들고 점검하려면 정해진 모양이 필요합니다.",
         "근거 있음", "실제 사례: v1.4 화면 생성에서 GPT 가 끝 괄호 2개를 빠뜨려 생성 전체가 실패 → main 3fe29d5 에서 복구·재요청 추가 (이 브랜치 미반영)"),
    ]
    builders = [
        {"name": "plan_prompt", "role": "첫 계획 — 모든 기준을 덮는 질문 최대 40개", "when": "생성 시작 때 1번", "shown": "원문 보관 (1번)"},
        {"name": "multi_turn_prompt", "role": "10턴 대화(J6) 전용 — 한 번에 4개", "when": "질문 수를 정했을 때, 대화 수만큼", "shown": "원문 보관 (2번)"},
        {"name": "long_context_prompt", "role": "긴 문서(J7) 질문 — 문서 주제·사실 문장과 질문", "when": "질문 수를 정했을 때 (같은 문서로 질문을 더 만드는 변형 포함)",
         "shown": "원문 보관 (3번)"},
        {"name": "supplement_prompt", "role": "보충 — 한 번도 적용되지 않은 기준·첨부 종류만", "when": f"점검 뒤 빠진 기준이 있으면 최대 {qg.SUPPLEMENT_ROUNDS}번",
         "shown": "예시 입력으로 만든 원문 (4번)"},
        {"name": "extra_prompt", "role": "추가 — 목표 수까지 적게 적용된 기준을 노려 40개씩", "when": "목표 질문 수(또는 기준별 목표 수)에 못 미치면 반복",
         "shown": "예시 입력으로 만든 원문 (5번)"},
        {"name": "_material_prompt", "role": "질문이 아니라 긴 문서(J7) 본문 조각 쓰기", "when": "긴 문서 하나에 조각 수십 번", "shown": "예시 입력으로 만든 원문 (6번)"},
        {"name": "_paste_prompt", "role": "질문이 아니라 대화에 붙여 넣을 자료(J6) 쓰기", "when": f"자료가 필요한 턴마다 (대화당 최대 {qg.MAX_PASTE_TURNS}턴)",
         "shown": "예시 입력으로 만든 원문 (7번)"},
    ]
    flow = [
        ("평가 기준 불러오기", "기준이 바뀌면 질문도 바뀌어야 해서, 사람이 옮겨 적지 않고 기준 목록에서 직접 읽습니다.",
         f"CODE 검사 {counts['code_checks']}개 + 의미 점검 항목 {counts['semantic_items']}개 = 평가 기준 {counts['total']}개", "code"),
        ("시험할 기준 나누기", "10턴 대화(J6)·긴 문서(J7)는 만드는 방법이 달라 따로 맡기고, 나머지는 일반 질문으로 만듭니다.",
         f"현재 v1.4 로 300개를 요청하면 긴 문서 질문 {long_} · 10턴 대화 {multi} · 일반 {300 - multi - long_}. "
         f"(실제 300개 세트를 만든 v1.2 에는 이 단계가 없어 10턴 대화 {gen['10턴 대화']}개 · 긴 문서 질문 {gen['긴 문서 질문']}개뿐)", "code"),
        ("GPT 에 질문 요청", "수백 개 질문을 사람이 쓰기 어렵고, 같은 설정이면 같은 요청을 다시 보낼 수 있습니다.",
         f"한 번에 최대 {qg.PLAN_BATCH}개씩 요청(출력 길이 관리용 기술 설정). 300개 세트는 GPT {qs.meta.get('generator_model')} 로 생성", "real"),
        ("질문과 목표 기준 함께 받기", "질문마다 '무엇을 시험하려 했는지'와 'FAIL 조건'이 있어야 나중에 맞는지 확인할 수 있습니다.",
         f"300개 모두 목표 기준·난도·FAIL 조건 기록 — 질문당 목표 기준 평균 {per_question['target_mean']}개", "real"),
        ("평가기와 같은 규칙으로 점검", "GPT 가 '시험한다'고 적어도 평가기가 그 기준을 켜지 않으면 시험한 것이 아닙니다.",
         f"목표 {funnel['intended']}건 중 판단 가능 {funnel['judged']}건, 실제 적용 {funnel['applied']}건({stats['적용률']})", "real"),
        ("적용되지 않은 기준 찾기", "한 번도 시험되지 않은 기준은 점수를 낼 수 없습니다.",
         f"채울 기준 {fill}개 중 {len(never)}개는 300개 중 한 번도 적용 안 됨: "
         + ", ".join(names.get(k, k).split(" — ")[-1] for k in never), "real"),
        ("부족한 기준 보충·추가", "빠진 기준만 노려 다시 요청하면 적은 질문으로 채울 수 있습니다.",
         f"보충 최대 {qg.SUPPLEMENT_ROUNDS}번 + 목표 수까지 {qg.PLAN_BATCH}개씩 추가. v1.2 의 추가 단계에서 J6 를 노린 짧은 대화 {j6['short_conversations']}개가 "
         f"나왔지만 J6 조건({multi_turn.MIN_TURNS}턴 이상)에 맞지 않아 적용 안 됨", "real"),
        ("질문 세트 저장", "모든 AI 에 똑같은 질문을 보내 비교하려면 고정된 파일이 필요합니다.",
         f"questions.json — 질문 {gen['질문(대화)']}개 · 턴 {gen['턴']}개 · 첨부 질문 {gen['첨부 질문']}개 → AIONA·GPT·Gemini 답변 수집(실행 {qs.meta.get('run_id')})", "real"),
    ]
    improve = [
        {"title": "J6 생성 조건과 평가 조건 맞추기",
         "problem": f"v1.2 는 J6(긴 대화)를 2~4턴 대화 {j6['short_conversations']}개에 노렸지만 평가기는 {multi_turn.MIN_TURNS}턴 이상 · 대화 전체 "
                    f"{multi_turn.MIN_TOTAL_TOKENS:,}토큰 이상만 J6 로 본다. v1.4 는 J6 를 10턴 전용 단계로 옮겼지만 일반 계획이 여전히 짧은 대화를 허용하고, "
                    "GPT 가 거기에 J6 를 적어도 막지 않는다. 평가기 설명 문구(J6 적용 조건)에는 이전 판 표현이 남아 있다.",
         "next": "v1.4 실제 생성에서 J6 목표가 10턴 대화에만 붙는지 확인. 설명 문구 정리는 평가 기준 담당과 확인(이번 작업에서 고치지 않음)."},
        {"title": "v1.4 실제 생성 품질 확인",
         "problem": "지금 화면의 실측 수치는 모두 이전 판 v1.2 로 만든 세트에서 나왔다. v1.4 는 구현·모의 테스트만 확인했다.",
         "next": f"승인 후 소규모(예: {qg.PLAN_BATCH}개) 실제 생성 → 같은 적용률 점검으로 v1.2 와 비교 (유료 GPT 호출)."},
        {"title": "질문 수가 적정한지 확인",
         "problem": f"{qg.PLAN_BATCH}개(한 번에 요청하는 수)와 300개(지난 실험의 목표 수)는 둘 다 검증된 적정 수가 아니다. "
                    f"앞의 50개로 기준 {at50['order_k1']}/{fill}개가 시험됐고 나머지 250개가 더한 것은 {last['order_k1'] - at50['order_k1']}개뿐이다.",
         "next": "기준별 목표 적용 수로 생성(v1.4 per_target)하고, 몇 개가 있어야 결과를 믿을 수 있는지는 W03 에서 평가 결과로 실험."},
        {"title": "사람이 질문 품질 확인",
         "problem": f"사람 검수 0건. 첫 문장이 완전히 같은 질문 {dup['exact_questions']}개, 20자 이하 단일 질문인데 '어려움'으로 표시된 질문 "
                    f"{len(hard_short)}개({', '.join(f'Q{i}' for i in hard_short[:5])}) — GPT 가 붙인 난도를 그대로 믿기 어렵다.",
         "next": "표본을 뽑아 중복·모호함·평가 불가·난도 오표기를 두 사람이 따로 표시하고 합의."},
        {"title": "CODE 계산·중복 질문의 문장 모양",
         "problem": "같은 계산 질문이라도 평가기가 인식하는 문장 모양이 아니면 계산 검사가 켜지지 않는다(③ 계산 사례, ④ CODE 3 원인).",
         "next": "빗나간 표현을 모아 생성 프롬프트의 예시 문장에 반영 — 다음 실제 생성에서 CODE 3 적용률로 확인."},
    ]
    paths = ("webapp/question_generation.py", "code_eval/criteria_registry.py", "code_eval/multi_turn.py")
    rev = revision(*paths)
    return artifact(
        "W01-DESIGN", "W01", "질문 생성 설계와 근거 — 왜·어떤 기준으로·어떤 원리로", "research",
        description=("질문을 만드는 이유, 시험하는 평가 기준 11개(쉬운 설명), 화면에 나오는 기준 수 28·44·72·64·34 의 관계, 생성 8단계, 프롬프트 구성 요소별 이유와 "
                     "근거 수준(근거 있음·사용자 결정·멘토 요청·가정)을 코드와 실제 세트에서 모았습니다."),
        result_summary=(f"생성 프롬프트 {len(prompt):,}자 중 {checklist:,}자({checklist / len(prompt):.0%})가 기준 정의(점검 목록)다. 구성 요소 {len(parts)}개 중 "
                        f"'가정'(실험 근거 없음)이 {sum(1 for p in parts if p[3] == '가정')}개 — 난이도 비율·충돌 방지. 프롬프트 함수는 대표 3종이 아니라 "
                        f"{len(builders)}종(질문 계획 3 · 보충·추가 2 · 자료 본문 2)이다."),
        data_source={"label": "생성기 코드·기준 목록 (모델 호출 없음) + 질문 세트 300 점검 결과", "kind": "code", "path": paths[0],
                     "visibility": "public", "revision": rev},
        created_at=committed_at(rev), code_revision=rev, level="measured",
        level_note="코드와 실제 세트에서 계산한 설명. 프롬프트 구성 요소가 실제로 효과가 있는지는 v1.4 실제 생성이 없어 확인하지 않았음.",
        limitations=["쉬운 설명(FAMILY_GUIDE)은 공식 기준 정의를 사람이 풀어 쓴 것이다 — 판정은 공식 정의로 한다.",
                     "근거 수준은 코드 주석·변경내역·회의 기록에서 찾은 것만 '근거 있음'으로 표시했다. 효과 크기는 측정하지 않았다."],
        references=[repo_ref("생성기 코드", paths[0]), repo_ref("기준 목록", paths[1]), repo_ref("J6 멀티턴 규칙", paths[2])],
        content={"question": "평가 질문을 왜, 어떤 기준으로, 어떤 원리로 만드는가?",
                 "why": {
                     "lead": "AI 답변을 평가하려면 먼저 '시험 문제'가 있어야 합니다. 예를 들어 'AI 가 문장 수를 지키는가'를 보려면 '3문장으로 설명해 줘' 같은 질문이 필요합니다. "
                             "평가 기준은 정해졌으니, 그 기준을 하나하나 실제로 시험할 질문을 만드는 것이 이 작업입니다.",
                     "methods": [
                         {"name": "CODE 검사", "who": "프로그램이 규칙으로 셉니다", "checks": "개수·글자 수·표 모양·계산값처럼 정답이 정해진 것",
                          "good": "빠르고 비용이 들지 않으며 늘 같은 결과", "limit": "질문 의도를 했는지, 논리가 맞는지는 판단하지 못함"},
                         {"name": "의미 평가", "who": "AI 심사위원(Claude)이 읽고 판단합니다", "checks": "의도·대상·논리·사실·대화 맥락처럼 내용을 읽어야 아는 것",
                          "good": "사람처럼 내용을 판단", "limit": "비용이 들고, 판정이 맞는지 사람 검수로 확인해야 함"}],
                     "together": "한쪽만 쓰면 '형식은 맞는데 엉뚱한 답'(CODE 만) 이나 '내용은 좋은데 200자 제한을 넘긴 답'(의미 평가만)을 놓칩니다. 그래서 질문 하나가 두 종류 기준을 함께 시험하게 만듭니다.",
                     "by_criterion": "질문 '유형'이 아니라 '세부 기준'을 기준으로 만듭니다. 멘토는 10-13 미팅에서 '유형 7개 × 30개'로는 세부 기준마다 몇 번 시험되는지 알 수 없다고 지적했습니다.",
                     "multi": f"질문 하나는 여러 기준을 함께 시험합니다. 실제 300개 세트에서 질문 하나에 세부 기준이 평균 {per_question['applied_mean']}개 적용됐습니다"
                              "(모든 답에 늘 적용되는 기본 검사 제외). 그래서 '기준 수 × 30개'처럼 곱해서 질문 수를 정하지 않습니다."},
                 "glossary": family_glossary(), "numbers": number_map(),
                 "flow": [{"step": n, "title": t, "why": w, "result": r, "state": s} for n, (t, w, r, s) in enumerate(flow, 1)],
                 "prompt_parts": [{"name": n, "excerpt": _cut(e, 240), "why": w, "basis": b, "evidence": ev} for n, e, w, b, ev in parts],
                 "prompt_sections": [{"heading": h[2:], "chars": n} for h, n in sections.items()], "prompt_chars": len(prompt),
                 "prompt_builders": builders, "improve": improve,
                 "method": ["생성기 코드에서 실제 프롬프트를 다시 만들어 구성 요소별로 나눔 (모델 호출 없음)",
                            "기준 목록·점검 규칙에서 기준 수를 단계별로 계산 (28·44·72·64·34)",
                            "질문 세트 300 을 평가기와 같은 규칙으로 점검한 결과를 단계마다 연결"],
                 "findings": [f"프롬프트의 {checklist / len(prompt):.0%}는 기준 정의(점검 목록) — 기준이 바뀌면 프롬프트가 함께 바뀐다.",
                              f"질문 하나에 적용되는 세부 기준은 평균 {per_question['applied_mean']}개로, GPT 가 목표로 적은 평균 {per_question['target_mean']}개보다 훨씬 많다.",
                              "프롬프트 함수는 7종 — 화면의 원문 3종은 질문 수를 정했을 때 처음 보내는 대표 호출이다."],
                 "judgment": "질문 생성 방법은 코드로 고정돼 같은 설정이면 같은 프롬프트가 나온다. 하지만 구성 요소 일부(난이도 비율·충돌 방지·항목 상한)는 실험 근거가 없는 가정이고, "
                             "현재 판(v1.4)의 실제 생성 품질은 아직 확인하지 않았다.",
                 "next": [i["title"] + " — " + i["next"] for i in improve]})


def w01_set_stats(qs: QuestionSet) -> List[Dict[str, Any]]:
    turns = collections.Counter(len(g["turns"]) for g in qs.groups)
    difficulty = collections.Counter(g.get("difficulty") for g in qs.groups)
    hard = difficulty.get("어려움", 0)
    return [{"label": "질문(대화)", "value": len(qs.groups)}, {"label": "턴", "value": sum(n * c for n, c in turns.items())},
            {"label": "어려움 비율", "value": f"{hard / len(qs.groups):.0%}", "note": "생성 규칙: 3분의 1 이상"},
            {"label": "첨부 질문", "value": sum(1 for g in qs.groups if any(t.get("attachments") for t in g["turns"]))},
            {"label": "10턴 대화", "value": turns.get(10, 0)},
            {"label": "긴 문서 질문", "value": sum(1 for g in qs.groups if any(t.get("reference") for t in g["turns"]))}]


def _applied_by_group(qs: QuestionSet) -> Dict[int, set]:
    """질문(대화)마다 평가기와 같은 규칙으로 적용된 검사·의미 항목(과 그 상위 id)."""
    applied: Dict[int, set] = collections.defaultdict(set)
    for row in qs.report["cases"]:
        gid = (((qs.items[row["index"] - 1].get("import_source") or {}).get("group") or {}).get("id"))
        found = set(row.get("checks") or []) | set(row.get("items") or [])
        found |= {i.split(".")[0] for i in row.get("items") or []}
        applied[gid] |= found
    return applied


def _applied_per_question(qs: QuestionSet) -> Dict[str, float]:
    """질문 하나에 실제 적용된 기준 수(CODE 검사 + 의미 항목, 답변 필요 제외) 평균과, GPT 가 적은 목표 수 평균."""
    from tools.min_question_count import all_targets, question_units
    order, targets, _ = question_units(qs.report)
    names = set(all_targets(qs.report))
    applied = [len(targets[u] & names) for u in order]
    return {"applied_mean": round(statistics.mean(applied), 1),
            "target_mean": round(statistics.mean(len(g.get("target_items") or []) for g in qs.groups), 1)}


def _strip_paste(question: str) -> Dict[str, Any]:
    """붙여 넣은 자료([붙여 넣은 자료] … [자료 끝])는 글자 수만 남기고 요청 문장만 보인다."""
    from code_eval.analysis import PASTE_BEGIN, PASTE_END
    if PASTE_BEGIN in question and PASTE_END in question:
        head, tail = question.split(PASTE_END, 1)
        return {"question": tail.strip(), "pasted_chars": len(head.replace(PASTE_BEGIN, "").strip())}
    return {"question": question}


BATCH_REV = "1466c41"        # PLAN_BATCH = 40 을 넣은 커밋 (질문 수백 개 지정 시 40개씩 나눠 계획)
COUNT_RULE_REV = "776f3bd"   # '40개 요청에 31개를 준 사례'로 질문 개수를 프롬프트 끝에 다시 못 박은 커밋
GROWTH_SIZES = (10, 20, 30, 40, 50, 100, 150, 200, 250, 300)


def _plan_entry(group: Dict[str, Any]) -> Dict[str, Any]:
    """저장된 질문(대화)을 GPT 가 계획 응답에 썼을 모양으로 되돌린다 — 붙여 넣은 자료·긴 문서 본문은 시스템이 따로 만들어 뺀다."""
    turns = []
    for turn in group["turns"]:
        entry: Dict[str, Any] = {"question": _strip_paste(turn["question"])["question"]}
        if turn.get("attachments"):
            entry["attachments"] = [{"name": a.get("name"), "type": a.get("type"), "content": a.get("content")} for a in turn["attachments"]]
        turns.append(entry)
    return {"id": f"Q{group['id']:02d}", "type": group.get("type"), "topic": group.get("topic"), "target_items": group.get("target_items"),
            "difficulty": group.get("difficulty"), "trap": group.get("trap"), "turns": turns}


def _grams(text: str) -> set:
    text = " ".join(text.split())
    return {text[i:i + 3] for i in range(max(1, len(text) - 2))}


def w01_quantity(qs: QuestionSet, per_answer: Optional[float]) -> Dict[str, Any]:
    """왜 40개씩 계획하나(기술 설정), 왜 300개였나(실험 설정), 질문 수를 늘리면 기준이 얼마나 더 시험되나 — 실제 세트로 계산 (모델 호출 없음)."""
    from code_eval import long_context
    from tools.min_question_count import all_targets, counts_for, question_units
    from webapp import question_generation as qg
    # 1. 계획 한 번의 출력 크기 추정 — 저장된 질문을 계획 JSON 모양으로 되돌려 o200k_base 로 센다 (추론 토큰은 모른다)
    tokens = [long_context.count_tokens(json.dumps(_plan_entry(g), ensure_ascii=False)) for g in qs.groups]
    mean = statistics.mean(tokens)
    batches = [sum(tokens[i:i + qg.PLAN_BATCH]) for i in range(0, len(tokens), qg.PLAN_BATCH)]
    author, when = (_git("show", "-s", "--format=%an|%cI", BATCH_REV) or "기록 없음|").split("|")
    batch = {"value": qg.PLAN_BATCH, "plan_tokens": qg.PLAN_TOKENS, "multi_turn_batch": qg.MULTI_TURN_BATCH, "long_context_batch": qg.LONG_CONTEXT_BATCH,
             "introduced": {"commit": BATCH_REV, "author": author, "at": kst(when) if when else None,
                            "reason": "질문 300개 시험(사용자 요청 10-14) — 수백 개를 한 응답에 쓰게 하면 출력 한도에 잘린다(변경내역 10-14 #10)"},
             "model_limit": "확인되지 않음 — 생성 모델(gpt-6-luna)의 최대 출력 토큰은 코드·저장소에 기록이 없다",
             "per_question": {"mean": round(mean), "median": round(statistics.median(tokens)), "p90": round(statistics.quantiles(tokens, n=10)[-1]),
                              "max": max(tokens)},
             "estimates": [{"questions": n, "tokens": round(n * mean), "share": _pct(round(n * mean), qg.PLAN_TOKENS)} for n in (20, qg.PLAN_BATCH, 60, 80)],
             "first_batch_tokens": batches[0], "batch_tokens": batches,
             "measured": "20·40·60개를 같은 조건으로 비교한 실행 기록 없음 — 생성 실행 로그(plan.txt·사용 토큰)도 이 저장소에 없다",
             "incidents": [f"첫 계획에 40개를 요청하자 31개만 왔다 → 프롬프트 끝에 개수를 다시 못 박음 ({COUNT_RULE_REV})",
                           "v1.4 화면 생성(qs-685805060697)에서 계획 JSON 끝 괄호 2개가 빠져 생성 전체 실패 → main 3fe29d5 에서 괄호 복구·재요청 (이 브랜치 미반영)"],
             "verdict": "출력 길이 관리를 위한 임시 기술 설정이며 적정 개수는 미검증"}
    # 2. 질문 수에 따른 기준 증가 — 생성 순서와 무작위 순서(300번 평균)
    order, targets, turns = question_units(qs.report)
    names = all_targets(qs.report)

    def covered(units: Sequence[str], k: int) -> int:
        return sum(1 for v in counts_for(units, targets, names).values() if v >= k)
    rng = random.Random(7)
    growth = []
    for n in (s for s in GROWTH_SIZES if s <= len(order)):
        draws = [rng.sample(order, n) for _ in range(300)]
        k1 = [covered(d, 1) for d in draws]
        k5 = [covered(d, 5) for d in draws]
        prefix_turns = sum(turns[u] for u in order[:n])
        growth.append({"questions": n, "turns": prefix_turns, "order_k1": covered(order[:n], 1), "random_k1": round(statistics.mean(k1), 1),
                       "random_k1_range": f"{min(k1)}~{max(k1)}", "order_k5": covered(order[:n], 5), "random_k5": round(statistics.mean(k5), 1),
                       "eval_usd": round(per_answer * prefix_turns * 3, 1) if per_answer else None})
    seen: set = set()
    marginal = []
    for i in range(0, len(order), 10):
        before = len(seen)
        for unit in order[i:i + 10]:
            seen |= targets[unit] & set(names)
        marginal.append(len(seen) - before)
    # 3. 중복·난도
    first = [(g["id"], " ".join(g["turns"][0]["question"].split())) for g in qs.groups]
    same: Dict[str, List[int]] = collections.defaultdict(list)
    for gid, text in first:
        same[text].append(gid)
    exact = [{"question": text, "ids": ids, "count": len(ids)} for text, ids in same.items() if len(ids) > 1]
    grams = [(gid, _grams(text)) for gid, text in first]
    near = []
    for a in range(len(grams)):
        for b in range(a + 1, len(grams)):
            x, y = grams[a][1], grams[b][1]
            score = len(x & y) / len(x | y)
            if 0.5 <= score < 1:
                near.append({"ids": [grams[a][0], grams[b][0]], "similarity": round(score, 2)})
    difficulty = collections.Counter(g.get("difficulty") for g in qs.groups)
    hard_short = [g["id"] for g in qs.groups if g.get("difficulty") == "어려움" and len(g["turns"]) == 1
                  and not g["turns"][0].get("attachments") and len(g["turns"][0]["question"].strip()) <= 20]
    per_question = _applied_per_question(qs)
    k1_50 = next(r for r in growth if r["questions"] == 50)
    last = growth[-1]
    four = [
        {"name": "한 번에 계획하는 질문 수", "value": qg.PLAN_BATCH, "kind": "기술 설정",
         "what": "GPT 응답 하나에 계획하게 하는 질문 수 (코드 PLAN_BATCH)", "basis": "수백 개를 한 응답에 쓰면 출력 한도에 잘린다는 설계 이유",
         "verified": "적정 개수 미검증 — 20·60개와 비교한 실측 없음"},
        {"name": "전체 생성 목표 질문 수", "value": len(qs.groups), "kind": "실험 설정",
         "what": "지난 실험(질문 세트 300)에서 화면에 지정한 세트 크기", "basis": "사용자 요청 10-14 '질문 300개를 만들어 시험' — 필요한 수를 계산해 정한 값이 아님",
         "verified": "적정 수로 검증되지 않음"},
        {"name": "기준마다 필요한 독립 질문 수", "value": "미정", "kind": "운영 기준 후보",
         "what": "세부 기준 하나를 몇 개의 독립 질문·대화로 시험할지 (v1.4 '기준별 목표 적용 수', 사전 점검 기본값 30)",
         "basis": "30 은 팀 잠정 운영 기준(10-13) — 통계로 정한 값이 아님", "verified": "미검증 — W03 에서 실험"},
        {"name": "결과를 믿기 위한 표본 수", "value": "미측정", "kind": "통계·검수 문제",
         "what": "모델 사이 점수 차이와 판정 정확도를 믿을 수 있는 질문 수", "basis": "사람 검수·반복 평가·오차 범위가 있어야 정할 수 있다",
         "verified": "사람 검수 0건 — W03 에서 결정 보류"}]
    return artifact(
        "W01-QUANTITY", "W01", "질문을 몇 개씩, 몇 개까지 만드나 — 40·300 의 근거와 질문 수에 따른 기준 증가", "experiment",
        description=("'한 번에 40개'와 '전체 300개'가 어디서 나온 숫자인지 기록을 찾고, 질문 300개를 앞에서부터(생성 순서)·무작위로 N개씩 골라 "
                     "새로 시험되는 세부 기준이 얼마나 늘어나는지, 중복·난도 분포를 계산했습니다."),
        result_summary=(f"40개씩은 {batch['introduced']['at'] or '기록 없음'} 커밋 {BATCH_REV} 의 기술 설정 — 적정 개수 미검증. 질문 1개 계획은 약 {round(mean)}토큰"
                        f"(추정)이라 40개 ≈ {round(qg.PLAN_BATCH * mean):,}토큰으로 호출당 상한 {qg.PLAN_TOKENS:,}의 {qg.PLAN_BATCH * mean / qg.PLAN_TOKENS:.0%}. "
                        f"300개는 지난 실험의 지정값이다. 채울 기준 {len(names)}개 중 질문 50개(생성 순서)로 {k1_50['order_k1']}개가 한 번 이상 시험됐고, "
                        f"나머지 250개가 더한 것은 {last['order_k1'] - k1_50['order_k1']}개 — 5번 이상 시험된 기준은 300개에서도 {last['order_k5']}개."),
        data_source=qs.source(), created_at=qs.exported, code_revision=revision("webapp/question_generation.py", "tools/min_question_count.py"),
        level="measured",
        level_note="실제 세트와 코드에서 계산 (모델 호출 없음). 계획 토큰은 저장된 질문으로 되돌려 센 추정치이고 추론 토큰은 포함하지 않음.",
        limitations=["계획 토큰 추정은 GPT 가 실제로 쓴 응답이 아니라 저장된 질문을 같은 모양으로 되돌려 센 값이다 — 추론 토큰·긴 문서 명세는 빠져 있다.",
                     "생성 순서 곡선은 첫 계획이 모든 기준을 덮도록 요청된 순서라 앞쪽이 유리하다 — 무작위 순서 평균을 함께 보았다.",
                     "기준이 '적용된다'는 시험할 기회가 생긴다는 뜻일 뿐 결과가 믿을 만하다는 뜻이 아니다(W03)."],
        references=[repo_ref("생성기 코드", "webapp/question_generation.py"), repo_ref("질문 원본 questions.json", QUESTIONS, qs.data_rev),
                    repo_ref(f"40개씩 나눠 계획을 넣은 커밋 {BATCH_REV} 의 생성기 코드", "webapp/question_generation.py", BATCH_REV)],
        content={"stats": [{"label": "한 번에 계획", "value": qg.PLAN_BATCH, "note": "기술 설정 · 적정 수 미검증"},
                           {"label": "계획 1개 추정", "value": f"{round(mean)}토큰", "note": "o200k_base · 추론 토큰 제외"},
                           {"label": "40개 추정", "value": f"{round(qg.PLAN_BATCH * mean):,}토큰", "note": f"호출당 상한 {qg.PLAN_TOKENS:,}"},
                           {"label": "질문 50개로 시험된 기준", "value": f"{k1_50['order_k1']}/{len(names)}"},
                           {"label": "300개로 시험된 기준", "value": f"{last['order_k1']}/{len(names)}"}],
                 "four_numbers": four, "batch": batch,
                 "charts": [{"kind": "line", "title": "질문 수 N 에 따라 한 번 이상(k≥1)·다섯 번 이상(k≥5) 시험된 세부 기준 수", "unit": "개", "max": len(names),
                             "x_label": "질문 수", "categories": [r["questions"] for r in growth],
                             "series": [{"name": "생성 순서 k≥1", "values": [r["order_k1"] for r in growth]},
                                        {"name": "무작위 평균 k≥1", "values": [r["random_k1"] for r in growth]},
                                        {"name": "생성 순서 k≥5", "values": [r["order_k5"] for r in growth]},
                                        {"name": "무작위 평균 k≥5", "values": [r["random_k5"] for r in growth]}]}],
                 "table": {"columns": [{"key": "questions", "label": "질문 수", "numeric": True}, {"key": "turns", "label": "턴(생성 순서)", "numeric": True},
                                       {"key": "order_k1", "label": "1번 이상 · 생성 순서", "numeric": True},
                                       {"key": "random_k1", "label": "1번 이상 · 무작위 평균", "numeric": True},
                                       {"key": "random_k1_range", "label": "무작위 범위"},
                                       {"key": "order_k5", "label": "5번 이상 · 생성 순서", "numeric": True},
                                       {"key": "random_k5", "label": "5번 이상 · 무작위 평균", "numeric": True},
                                       {"key": "eval_usd", "label": "평가 비용 추정($, 3개 시스템)", "numeric": True}],
                           "rows": growth, "download": "w01-question-count-growth.csv"},
                 "growth_targets": len(names), "marginal_per_10": marginal,
                 "duplicates": {"exact_groups": exact, "exact_questions": sum(e["count"] for e in exact), "near_pairs": len(near),
                                "near_examples": sorted(near, key=lambda p: -p["similarity"])[:5]},
                 "difficulty": {"counts": {k: difficulty.get(k, 0) for k in ("쉬움", "보통", "어려움")},
                                "hard_per_batch": [sum(1 for g in qs.groups[i:i + qg.PLAN_BATCH] if g.get("difficulty") == "어려움")
                                                   for i in range(0, len(qs.groups), qg.PLAN_BATCH)],
                                "hard_short_ids": hard_short},
                 "per_question": per_question, "per_answer_usd": per_answer,
                 "notes": [f"10개씩 더할 때 새로 시험된 기준 수(생성 순서): {', '.join(str(n) for n in marginal)}",
                           "평가 비용 추정은 다른 질문 세트(저장 실행 e3c60d3efd18)의 답변당 평가 비용으로 계산한 참고값이다. 질문 생성 비용은 기록이 없어 계산하지 않았다."]})


#: 대표 사례 고르는 규칙 —(이름, 기준 종류, 생성 이유(쉬운 말), 고르는 규칙). 질문 원문·FAIL 조건·적용 여부는 실제 세트에서 읽는다.
CASE_RULES = [
    ("빈 출력 지시 준수", "CODE", "'아무것도 출력하지 마'처럼 아주 단순한 지시도 정확히 지키는지 보려고 만들었습니다. AI 는 '알겠습니다' 같은 말을 덧붙이기 쉬워서 틀리기 쉬운 지점입니다.",
     lambda g, applied: "2-1.explicit_empty" in (g.get("target_items") or [])),
    ("계산 정확성", "CODE", "프로그램이 정답을 직접 계산해 맞는지 확인할 수 있는 질문입니다. 퍼센트처럼 AI 가 자주 틀리는 계산을 넣고, 답 형식까지 정해 비교하기 쉽게 했습니다.",
     lambda g, applied: "3-1.question_calculation" in (g.get("target_items") or []) and "%" in g["turns"][0]["question"]
     and "3-1.question_calculation" in applied),
    ("표 형식 준수", "CODE", "열 이름·순서·행 수를 정확히 정해 주면, 답의 표가 그 모양인지 프로그램이 셀 수 있습니다. 하나라도 어긋나면 FAIL 입니다.",
     lambda g, applied: any(t.startswith("2-2.table") for t in g.get("target_items") or []) and len(g["turns"]) == 1
     and sum(1 for k in applied if k.startswith("2-2.table:")) >= 3),
    ("멀티턴 맥락 유지", "LLM", "긴 대화에서 앞에서 정한 조건을 끝까지 기억하는지 보려면, 조건을 정하고 · 바꾸고 · 근거 없이 되돌리려는 요청을 10턴에 걸쳐 넣어야 합니다.",
     lambda g, applied: len(g["turns"]) >= 10),
]


def _cases(qs: QuestionSet) -> List[Dict[str, Any]]:
    applied = _applied_by_group(qs)
    names = _target_names()
    out = []
    for title, layer, why, rule in CASE_RULES:
        group = next((g for g in qs.groups if rule(g, applied[g["id"]])), None)
        if group is None:
            continue
        turns = [dict(_strip_paste(t["question"]), turn=t["turn"]) for t in group["turns"]]
        targets = [{"id": t, "name": names.get(t, t), "applied": t in applied[group["id"]]} for t in group.get("target_items") or []]
        case = {"id": group["id"], "title": title, "layer": layer, "type": group.get("type"), "topic": group.get("topic"),
                "difficulty": group.get("difficulty"), "why": why, "fail": group.get("trap"), "targets": targets,
                "applied_total": len({k for k in applied[group["id"]] if ":" in k or (k.startswith("J") and "." in k)}),
                "turns_total": len(turns)}
        if len(turns) > 1:   # 대표 턴 — 첫 턴(조건 설정), 조건을 바꾸는 턴, 근거 없이 되돌리려는 턴
            picks = {0: "처음에 조건 정하기"}
            for cue, label in (("변경", "중간에 조건 바꾸기"), ("근거", "근거 없이 이전 값으로 되돌리라는 요구")):
                index = next((i for i, t in enumerate(turns) if cue in t["question"] and i not in picks), None)
                if index is not None:
                    picks[index] = label
            case["highlights"] = [{"index": i, "label": picks[i]} for i in sorted(picks)]
            case["turns"] = turns
        else:
            case["question"] = turns[0]["question"]
            if group["turns"][0].get("attachments"):
                case["attachments"] = [a.get("name") for a in group["turns"][0]["attachments"]]
        out.append(case)
    calc = [g for g in qs.groups if "3-1.question_calculation" in (g.get("target_items") or [])]
    missed = next((g for g in calc if "3-1.question_calculation" not in applied[g["id"]]), None)
    if missed is not None:   # 같은 목표인데 평가기가 인식하지 못한 질문 — 문장 모양의 중요성
        for case in out:
            if case["title"] == "계산 정확성":
                case["contrast"] = {"id": missed["id"], "question": missed["turns"][0]["question"],
                                    "missed": sum(1 for g in calc if "3-1.question_calculation" not in applied[g["id"]]), "total": len(calc)}
    return out


# --- W02 기준별 적용 -----------------------------------------------------------------------------

def _turn_counts(report: Dict[str, Any]) -> collections.Counter:
    counts: collections.Counter = collections.Counter()
    for row in report["cases"]:
        for key in set(row.get("checks") or []) | set(row.get("items") or []):
            counts[key] += 1
    return counts


def w02_matrix(qs: QuestionSet) -> Dict[str, Any]:
    from code_eval.criteria_registry import evaluation_targets, reference_details
    from code_eval.semantic_items import ITEMS, SUBS_BY_ID
    from tools.check_question_set import ANSWER_DEPENDENT_ITEMS
    targets = evaluation_targets()
    names = {c["id"]: c["name"] for c in reference_details()["code_checks"]}
    cov = qs.report["coverage"]
    turns = _turn_counts(qs.report)
    rows: List[Dict[str, Any]] = []
    family = dict(zip(*(lambda ids: (ids, _family_labels(ids)))([f["id"] for f in family_glossary()])))

    def add(group: str, parent: str, target: str, name: str, units: Optional[int]) -> None:
        rows.append({"group": group, "parent": family.get(parent, parent), "id": target, "name": name, "units": units,
                     "turns": turns.get(target, 0) if units is not None else None, "judged": 0,
                     "short": max(0, TARGET_SHORT - units) if units is not None else None})
    for check in targets["code"]["question_checks"]:
        add("CODE 질문 요구", "CODE " + check.split("-")[0], check, names.get(check, check), cov["checks"].get(check, 0))
    for check in targets["code"]["always_checks"]:
        add("CODE 늘 적용", "CODE " + check.split("-")[0], check, names.get(check, check), cov["checks"].get(check, 0))
    for check in targets["code"]["answer_checks"]:
        add("CODE 답변 구조", "CODE " + check.split("-")[0], check, names.get(check, check) + " (답변을 받아야 셀 수 있음)", None)
    for item in ITEMS:
        dependent = item.id in ANSWER_DEPENDENT_ITEMS
        add("의미 점검 항목", item.id.split("-")[0], item.id, f"{item.sub} {SUBS_BY_ID[item.sub].name} · {_cut(item.question, 70)}"
            + (" (답변을 받아야 셀 수 있음)" if dependent else ""), None if dependent else cov["items"].get(item.id, 0))
    countable = [r for r in rows if r["units"] is not None]
    buckets = [("0", 0, 0), ("1~4", 1, 4), ("5~9", 5, 9), ("10~29", 10, 29), ("30 이상", 30, 10 ** 9)]
    bucket_counts = {label: sum(1 for r in countable if lo <= r["units"] <= hi) for label, lo, hi in buckets}
    never = [r["id"] for r in countable if r["units"] == 0]
    return artifact(
        "W02-MATRIX", "W02", "최하위 기준별 적용 표 (질문 세트 300)", "data",
        description=("CODE 검사(질문 요구·늘 적용·답변 구조)와 의미 점검 항목 각각에, 질문 세트 300 의 어떤 질문이 몇 번 적용되는지 "
                     "평가와 같은 코드 규칙으로 센 표입니다. 독립 단위(단일 질문 1 · 대화 1), 턴 수, 실제 판정 수를 나눠 적었습니다."),
        result_summary=(f"셀 수 있는 최하위 기준 {len(countable)}개(늘 적용 검사 3개 포함) 중 한 번도 적용되지 않은 것 {len(never)}개, "
                        f"독립 단위 30개 이상 {bucket_counts['30 이상']}개, 10~29개 {bucket_counts['10~29']}개, 1~9개 "
                        f"{bucket_counts['1~4'] + bucket_counts['5~9']}개. 이 세트는 평가하지 않아 실제 판정 수는 모두 0."),
        data_source=qs.source(), created_at=qs.exported, code_revision=revision("tools/check_question_set.py", "code_eval/criteria_registry.py"),
        level="measured", level_note="실제 질문 세트를 평가와 같은 코드 규칙으로 계산 (모델 호출 없음). 실제 판정은 0건.",
        limitations=["의미 점검 항목의 적용 수는 '평가 모델에게 묻는 수'다. 평가 모델이 해당 없음으로 보면 실제 판정 수는 더 적다.",
                     "답변 구조 검사(각주·인용 링크 등)와 J5-3.a 는 답변을 받아야 적용 여부가 정해져 질문만으로는 세지 않았다.",
                     f"'부족' 열은 잠정 운영 기준 {TARGET_SHORT}개 대비다 — 통계적으로 정한 필요량이 아니다(W03 참고)."],
        references=[repo_ref("질문 원본 questions.json", QUESTIONS, qs.data_rev), repo_ref("적용 규칙", "tools/check_question_set.py"),
                    repo_ref("기준 목록", "code_eval/criteria_registry.py")],
        content={"stats": [{"label": "최하위 기준(셀 수 있음)", "value": len(countable)}, {"label": "답변 필요", "value": len(rows) - len(countable)},
                           {"label": "적용 0", "value": len(never)}, {"label": "30개 이상", "value": bucket_counts["30 이상"]},
                           {"label": "실제 판정", "value": 0, "note": "평가하지 않은 세트"}],
                 "charts": [{"kind": "bar", "title": "독립 단위 수 구간별 최하위 기준 개수", "unit": "개",
                             "categories": list(bucket_counts), "series": [{"name": "최하위 기준 수", "values": list(bucket_counts.values())}]}],
                 "table": {"columns": [{"key": "group", "label": "구분"}, {"key": "parent", "label": "상위 기준"}, {"key": "id", "label": "ID"},
                                       {"key": "name", "label": "이름"}, {"key": "units", "label": "독립 단위", "numeric": True},
                                       {"key": "turns", "label": "적용 턴", "numeric": True}, {"key": "judged", "label": "실제 판정", "numeric": True},
                                       {"key": "short", "label": f"{TARGET_SHORT} 대비 부족", "numeric": True}],
                           "rows": rows, "filters": ["group", "parent"], "download": "w02-lowest-criteria-coverage.csv"},
                 "notes": ["한 번도 적용되지 않은 기준: " + (", ".join(never) or "없음")]})


def w02_criteria(qs: QuestionSet) -> Dict[str, Any]:
    criteria = qs.report["criteria"]
    rows = [{"id": cid, "units": c["applicable_units"], "turns": c["applicable_turns"], "singles": c["units_singles"],
             "conversations": c["units_conversations"], "judged": 0} for cid, c in criteria.items()]
    return artifact(
        "W02-CRITERIA", "W02", "상위 기준별 독립 단위와 턴 (질문 세트 300)", "experiment",
        description="CODE 대분류 3개와 의미 기준 J1~J8 에 적용되는 독립 단위(질문·대화) 수와 턴 수입니다. 멀티턴 대화는 턴이 많아도 독립 단위 1개입니다.",
        result_summary=" · ".join(f"{r['id']} {r['units']}단위/{r['turns']}턴" for r in rows) + ". 실제 판정 0.",
        data_source=qs.source(), created_at=qs.exported, code_revision=revision("tools/check_question_set.py"), level="measured",
        level_note="코드 규칙으로 계산 (모델 호출 없음). J6 은 답변이 없어 대화 길이를 아직 잴 수 없는 10턴 대화를 적용으로 셈.",
        limitations=["J6 멀티턴은 대화 전체(질문+답변) 10K 토큰 이상이어야 한다 — 답변을 받은 뒤 모델마다 다시 정해진다.",
                     "턴 수로 세면 대화가 많은 기준이 부풀려진다. 충분성은 독립 단위로 본다."],
        references=[repo_ref("질문 원본 questions.json", QUESTIONS, qs.data_rev), repo_ref("적용 규칙", "tools/check_question_set.py")],
        content={"charts": [{"kind": "bar", "title": "기준별 독립 단위와 턴", "unit": "개", "categories": [r["id"] for r in rows],
                             "category_labels": _family_labels([r["id"] for r in rows]), "label_width": 170,
                             "series": [{"name": "독립 단위(질문·대화)", "values": [r["units"] for r in rows]},
                                        {"name": "적용 턴", "values": [r["turns"] for r in rows]}]}],
                 "table": {"columns": [{"key": "id", "label": "기준"}, {"key": "units", "label": "독립 단위", "numeric": True},
                                       {"key": "singles", "label": "단일 질문", "numeric": True}, {"key": "conversations", "label": "대화", "numeric": True},
                                       {"key": "turns", "label": "적용 턴", "numeric": True}, {"key": "judged", "label": "실제 판정", "numeric": True}],
                           "rows": rows, "download": "w02-criteria-units-turns.csv"}})


def w02_saved(run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if run is None:
        return None
    from webapp.run_summary import criteria_verification
    cv = criteria_verification(run)
    any_batch = next(iter((run.get("evaluations") or {}).values()), {}) or {}
    jev = any_batch.get("jev_eval") or {}
    rows = [{"id": c["id"], "name": c["name"], "applied_answers": c["applied_answers"], "applied_units": c["applied_units"],
             "judged": c["judged"], "pass": c["pass"], "fail": c["fail"], "limited_fail": c["limited_fail"], "unresolved": c["unresolved"],
             "human_reviewed": c["human_reviewed"]} for c in cv["criteria"]]
    total = sum(r["judged"] for r in rows)
    return artifact(
        "W02-SAVED-JUDGMENTS", "W02", f"실제 판정 수 — 저장 실행 {SAVED_RUN_ID} (다른 질문 세트)", "run",
        description=("실제로 평가까지 끝난 저장 실행에서 기준마다 판정이 몇 번 나왔는지입니다. 질문 세트 300 과는 다른 질문(68턴·30단위)이고, "
                     "평가 당시 기준 버전도 지금과 다릅니다."),
        result_summary=f"의미 기준 판정 {total}건 (4개 시스템 합). J7·J8 은 적용 0. 사람 검수 0건이라 정확도는 미측정.",
        data_source={"label": f"저장 실행 {SAVED_RUN_ID} — 공개 요약만", "kind": "saved_run", "path": SAVED_RUN, "visibility": "private",
                     "run_id": SAVED_RUN_ID, "models": run.get("models"), "criteria_version": jev.get("criteria_version"),
                     "applicability_version": jev.get("applicability_version"), "pipeline": jev.get("pipeline"), "engine": jev.get("engine")},
        created_at=kst(run.get("finished_at")), code_revision=revision("webapp/run_summary.py"), level="real_run",
        level_note="실제 Claude 평가 호출로 나온 판정의 개수. 판정이 맞는지는 사람 검수 0건이라 확인하지 않았음.",
        limitations=[f"평가 당시 기준 {jev.get('criteria_version')} · 적용 {jev.get('applicability_version')} — 지금 기준(J8 추가, J6 multi-turn-v2)과 다르다.",
                     "J6 판정은 당시 '2턴 이상' 규칙으로 나온 것이다. 지금 규칙(10턴 이상·대화 전체 10K 토큰)이면 대부분 해당 없음이다.",
                     "질문 세트 300 과 질문이 달라 위 적용 표와 합치거나 비교하면 안 된다."],
        references=[saved_run_ref()],
        content={"stats": [{"label": "의미 판정 합계", "value": total}, {"label": "사람 검수", "value": 0}, {"label": "정확도", "value": "미측정"}],
                 "table": {"columns": [{"key": "id", "label": "기준"}, {"key": "name", "label": "이름"},
                                       {"key": "applied_answers", "label": "적용 답변", "numeric": True},
                                       {"key": "applied_units", "label": "질문 단위", "numeric": True},
                                       {"key": "judged", "label": "실제 판정", "numeric": True}, {"key": "pass", "label": "PASS", "numeric": True},
                                       {"key": "fail", "label": "FAIL", "numeric": True}, {"key": "limited_fail", "label": "FAIL 중 검증 한계", "numeric": True},
                                       {"key": "unresolved", "label": "미완료", "numeric": True}, {"key": "human_reviewed", "label": "사람 검수", "numeric": True}],
                           "rows": rows, "download": "w02-saved-run-judgments.csv"}})


# --- W03 적정 질문 수 ---------------------------------------------------------------------------

def w03_coverage(qs: QuestionSet) -> Dict[str, Any]:
    from tools.min_question_count import VERSION, analyze
    result = analyze(qs.items)
    ks = [str(k) for k in (1, 5, 10, 30)]
    prefix = result["prefix"]
    rows = [{"questions": p["questions"], "min": p["min"], "median": p["median"], **{f"k{k}": p["covered"][k] for k in ks}} for p in prefix]
    minimum = [{"k": m["k"], "questions": m["questions"], "turns": m["turns"], "short": len(m["short"]),
                "estimated": m["estimated_questions_for_short"], "never_applied": ", ".join(m["never_applied"])} for m in result["minimum"]]
    last = prefix[-1]
    return artifact(
        "W03-COVERAGE", "W03", "질문 수를 늘릴 때 기준이 채워지는 속도 (질문 세트 300)", "experiment",
        description=("질문 세트 300 을 앞에서부터 N개로 잘랐을 때, 최하위 평가 기준 각각이 k개 이상의 질문에서 적용되는지 셌습니다(k = 1·5·10·30). "
                     "또 기준마다 k개를 채우는 가장 작은 질문 묶음을 탐욕 근사로 골랐습니다."),
        result_summary=(f"평가 기준 {result['targets']}개 기준, 300개 전체에서도 k≥1 {last['covered']['1']}개 · k≥5 {last['covered']['5']}개 · "
                        f"k≥10 {last['covered']['10']}개 · k≥30 {last['covered']['30']}개. 모든 기준을 1번씩 덮는 데 {minimum[0]['questions']}개, "
                        f"5번씩은 이 세트로 채울 수 없음(부족 {minimum[1]['short']}개, 같은 비율이면 약 {minimum[1]['estimated']:,}개 필요)."),
        data_source=qs.source(), created_at=qs.exported, code_revision=revision("tools/min_question_count.py", "tools/check_question_set.py"),
        level="measured", level_note=f"{VERSION} 를 실제 질문 세트에 실행 (모델 호출 없음). 적용 수만 보며 판정 안정성은 보지 않음.",
        limitations=["'적용된다'는 평가할 기회가 있다는 뜻이다 — 결과가 안정적인지는 평가 결과가 있어야 알 수 있다(아래 저장 실행 분석).",
                     "탐욕 근사는 실제 최소보다 클 수 있는 상한이다.",
                     "생성기 v1.2 결과라 세부 조건 3개(시작 문구·끝 문구·표 열 수)는 한 번도 적용되지 않았다 — v1.4 는 이 단위를 채우도록 바뀌었다."],
        references=[repo_ref("질문 원본 questions.json", QUESTIONS, qs.data_rev), repo_ref("분석 도구", "tools/min_question_count.py")],
        content={"stats": [{"label": "평가 기준", "value": result["targets"]}, {"label": "질문(대화)", "value": result["questions"]},
                           {"label": "300개에서 k≥30", "value": f"{last['covered']['30']}/{result['targets']}"},
                           {"label": "1번씩 덮는 최소(근사)", "value": minimum[0]["questions"]}],
                 "charts": [{"kind": "line", "title": "질문 수 N 에 따라 k번 이상 적용된 기준 수", "unit": "개", "max": result["targets"],
                             "x_label": "질문 수(앞에서부터)", "categories": [p["questions"] for p in prefix],
                             "series": [{"name": f"k≥{k}", "values": [p["covered"][k] for p in prefix]} for k in ks]}],
                 "table": {"columns": [{"key": "questions", "label": "질문 수 N", "numeric": True}, {"key": "min", "label": "최소 적용", "numeric": True},
                                       {"key": "median", "label": "중앙 적용", "numeric": True}] +
                                      [{"key": f"k{k}", "label": f"k≥{k} 기준 수", "numeric": True} for k in ks],
                           "rows": rows, "download": "w03-coverage-by-size.csv"},
                 "minimum": {"columns": [{"key": "k", "label": "k", "numeric": True}, {"key": "questions", "label": "고른 질문", "numeric": True},
                                         {"key": "turns", "label": "턴", "numeric": True}, {"key": "short", "label": "세트로 못 채운 기준", "numeric": True},
                                         {"key": "estimated", "label": "같은 비율일 때 필요 질문(추정)", "numeric": True},
                                         {"key": "never_applied", "label": "한 번도 적용 안 됨"}],
                             "rows": minimum}})


def w03_stability(run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if run is None:
        return None
    from tools.sample_size_study import STUDY_VERSION, judge_costs, question_level
    q = question_level(run)
    sizes = q["sizes"]
    rows = []
    for n, s in sizes.items():
        if s.get("status") != "ok":
            rows.append({"n": int(n), "status": f"데이터 부족 (가용 {s.get('available')}단위)", "width": None, "order": None,
                         "cost": None, "minutes": None, "overlap": None})
            continue
        rows.append({"n": int(n), "status": "가용 절반 초과 — 안정성 부풀려짐" if s["pool_dominated"] else "계산",
                     "width": round(s["max_width"] * 100), "order": round(s["order_kept"] * 100, 1),
                     "cost": s["judge_cost_usd"], "minutes": s["judge_minutes"], "overlap": s["pool_dominated"]})
    costs = judge_costs(run)
    ok = [r for r in rows if r["width"] is not None]
    return artifact(
        "W03-STABILITY", "W03", f"질문 수별 결과 안정성 — 저장 실행 {SAVED_RUN_ID}", "experiment",
        description=("실제 평가가 끝난 저장 실행(독립 단위 30개)에서 질문 n개를 2,000번 무작위로 뽑아, 시스템별 통과율이 얼마나 흔들리는지(95% 폭)와 "
                     "시스템 사이 순서가 유지되는 비율을 쟀습니다. 간편·정밀 후보 규칙을 그대로 적용했습니다."),
        result_summary=("; ".join(f"n={r['n']}: 통과율 폭 {r['width']}%p · 순서 유지 {r['order']}%" for r in ok)
                        + f". 간편·정밀 모두 '현재 데이터로 결정 불가' (사람 정답 0건)."),
        data_source={"label": f"저장 실행 {SAVED_RUN_ID} — 공개 요약만", "kind": "saved_run", "path": SAVED_RUN, "visibility": "private",
                     "run_id": SAVED_RUN_ID, "models": run.get("models")},
        created_at=kst(run.get("finished_at")), code_revision=revision("tools/sample_size_study.py"), level="real_run",
        level_note=f"{STUDY_VERSION} — 실제 평가 결과를 다시 뽑아 계산 (모델 호출 없음). 판정 정확도는 사람 검수 0건이라 반영 못 함.",
        limitations=["독립 단위가 30개뿐이라 n=20 부터는 같은 질문이 겹쳐 안정성이 부풀려진다(표에 표시).",
                     "평가 당시 기준 버전이 지금과 다르다(J8 없음, J6 2턴 이상 규칙).",
                     "판정이 맞는지(사람 정답 일치율)는 재지 못했다."],
        references=[saved_run_ref(), repo_ref("분석 도구", "tools/sample_size_study.py")],
        content={"stats": [{"label": "독립 단위", "value": q["units_total"]}, {"label": "답변당 평가 비용", "value": f"${costs['usd_per_answer']}"},
                           {"label": "간편 후보", "value": q["proposal"]["quick"] or "결정 불가"},
                           {"label": "정밀 후보", "value": q["proposal"]["precise"] or "결정 불가"}],
                 "charts": [{"kind": "bar", "title": "표본 질문 수별 시스템 순서 유지 비율 ('겹침' = 표본이 가용 단위 절반을 넘어 부풀려진 값)",
                             "unit": "%", "max": 100, "categories": [f"n={r['n']}" + (" (겹침)" if r["overlap"] else "") for r in ok],
                             "series": [{"name": "순서 유지", "values": [r["order"] for r in ok]}]}],
                 "table": {"columns": [{"key": "n", "label": "표본 질문 수", "numeric": True}, {"key": "status", "label": "상태"},
                                       {"key": "width", "label": "통과율 95% 폭(%p)", "numeric": True}, {"key": "order", "label": "순서 유지(%)", "numeric": True},
                                       {"key": "cost", "label": "평가 비용($, 4시스템)", "numeric": True}, {"key": "minutes", "label": "평가 시간(분)", "numeric": True}],
                           "rows": rows, "download": "w03-stability-saved-run.csv"},
                 "notes": [f"규칙: {q['proposal']['rule']}", q["proposal"]["note"]]})


def w03_conclusion(qs: QuestionSet, run: Optional[Dict[str, Any]], existing: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    from tools.min_question_count import analyze, required_n
    result = analyze(qs.items)
    last = result["prefix"][-1]
    per_answer = None
    if run is not None:
        from tools.sample_size_study import judge_costs
        per_answer = judge_costs(run)["usd_per_answer"]
    elif existing:
        per_answer = (existing.get("content") or {}).get("per_answer_usd")
    turns = result["turns"]
    estimate = round(per_answer * turns * 3, 1) if per_answer else None
    return artifact(
        "W03-CONCLUSION", "W03", "간편·정밀 검사 질문 수 — 지금 내릴 수 있는 결론", "research",
        description="두 분석(적용 수, 결과 안정성)과 이론 기준을 합쳐, 간편·정밀 검사에 필요한 질문 수를 지금 정할 수 있는지 판단했습니다.",
        result_summary=("지금 데이터로는 간편·정밀 질문 수를 정할 수 없다. 질문 300개로도 기준 "
                        f"{result['targets']}개 중 {result['targets'] - last['covered']['5']}개는 5번도 적용되지 않고, 평가 결과가 있는 실행은 독립 단위 30개뿐이다."),
        data_source={"label": "W03-COVERAGE · W03-STABILITY 결과를 합친 판단", "kind": "analysis", "path": QUESTIONS, "visibility": "public",
                     "revision": qs.data_rev},
        created_at=qs.exported, code_revision=revision("tools/min_question_count.py", "tools/sample_size_study.py"), level="measured",
        level_note="실제 데이터 계산을 근거로 한 판단. 새 실험(평가 호출)은 하지 않았음.",
        limitations=["평가 비용 추정은 다른 질문 세트·이전 기준 버전의 실제 사용량에서 나온 참고값이다."],
        references=[repo_ref("질문 원본 questions.json", QUESTIONS, qs.data_rev), saved_run_ref()],
        content={"question": "간편 검사와 정밀 검사에 각각 질문이 몇 개 있어야 결과를 믿을 수 있는가?",
                 "method": ["질문 세트 300 에서 질문 수 N 에 따라 최하위 기준마다 적용 질문이 몇 개 생기는지 계산 (W03-COVERAGE)",
                            f"평가가 끝난 저장 실행 {SAVED_RUN_ID} 에서 질문 n개를 다시 뽑아 통과율 폭·순서 유지 측정 (W03-STABILITY)",
                            "통과율을 원하는 정밀도로 재는 데 필요한 적용 수를 정규 근사로 계산 (아래 이론 기준)"],
                 "findings": [f"300개 전체에서도 k≥5 인 기준 {last['covered']['5']}/{result['targets']}, k≥30 {last['covered']['30']}/{result['targets']} — 적용 수가 고르지 않다.",
                              f"모든 기준을 1번씩 덮는 질문은 {result['minimum'][0]['questions']}개(근사)면 되지만, 그건 '평가할 기회'일 뿐 안정성이 아니다.",
                              "저장 실행(30단위)에서 n=10 은 통과율 폭 40%p·순서 유지 약 66% — 간편 검사로도 순위를 믿기 어렵다.",
                              f"이론 기준: 통과율 80%를 ±10%p 로 재려면 기준마다 {required_n(0.8, 0.10)}개, 50%면 {required_n(0.5, 0.10)}개 적용이 필요하다."],
                 "judgment": ("간편·정밀 질문 수를 숫자로 정하지 않는다(결정 보류). 30·20·500·9,600 같은 수는 아직 근거가 없다. "
                              "다만 '모든 기준 1회 이상 적용'은 간편 검사의 최소 조건으로 쓸 수 있다."),
                 "next": [f"질문 세트 300 의 답변(이미 수집됨)을 평가해 안정성을 다시 잰다 — 턴 {turns}개 × 3개 시스템"
                          + (f", 저장 실행의 답변당 비용 ${per_answer} 로 추정하면 약 ${estimate} (승인 필요, Batches 사용 시 약 절반)" if estimate else ""),
                          "v1.4 로 기준마다 목표 적용 수(per_target)를 정해 만든 세트로 같은 분석을 반복한다",
                          "사람 검수(정답) 표본을 만들어 판정 정확도까지 반영한다 — 지금은 0건"],
                 "precision": result["precision"], "per_answer_usd": per_answer})


# --- W04·W05·W08 답변 수집 ----------------------------------------------------------------------

def _collection_stats(answers: Dict[str, Any], system: str) -> Dict[str, Any]:
    entries = [i["answers"][system] for i in answers["items"] if system in i.get("answers", {})]
    status = collections.Counter(e.get("status") for e in entries)
    ok = [e for e in entries if e.get("status") == "SUCCESS"]
    latency = [e["latency_ms"] for e in ok if isinstance(e.get("latency_ms"), (int, float))]
    lengths = [len(e.get("answer") or "") for e in ok]
    stamps = sorted(e["collected_at"] for e in entries if e.get("collected_at"))
    errors = collections.Counter(_cut(e.get("error"), 80) for e in entries if e.get("status") != "SUCCESS")
    tokens = collections.Counter()
    for e in ok:
        for key in ("input_tokens", "output_tokens", "reasoning_tokens"):
            if isinstance((e.get("usage") or {}).get(key), int):
                tokens[key] += e["usage"][key]
    return {"system": system, "model": answers["meta"]["models"].get(system), "method": answers["meta"]["collection"].get(system),
            "turns": len(entries), "status": dict(status), "success": status.get("SUCCESS", 0),
            "success_rate": _pct(status.get("SUCCESS", 0), len(entries)), "retried": sum(1 for e in entries if e.get("retried")),
            "latency_median_ms": round(statistics.median(latency)) if latency else None,
            "answer_chars_median": round(statistics.median(lengths)) if lengths else None, "empty_answers": sum(1 for n in lengths if n == 0),
            "first": kst(stamps[0]) if stamps else None, "last": kst(stamps[-1]) if stamps else None,
            "errors": errors.most_common(5), "tokens": dict(tokens),
            "routed": collections.Counter(e["routed_model"] for e in ok if e.get("routed_model")).most_common()}


def _status_chart(stats: List[Dict[str, Any]]) -> Dict[str, Any]:
    states = ["SUCCESS", "INCOMPLETE", "ERROR"]
    return {"kind": "stacked", "title": f"시스템별 수집 상태 (턴 {stats[0]['turns']}개)", "unit": "턴", "status": True,
            "categories": [s["system"].upper() if s["system"] != "aiona" else "AIONA" for s in stats],
            "series": [{"name": {"SUCCESS": "성공", "INCOMPLETE": "미완료", "ERROR": "오류"}[k], "state": k,
                        "values": [s["status"].get(k, 0) for s in stats]} for k in states]}


def _log(qs: QuestionSet, system: str, limit: int = 6) -> List[Dict[str, Any]]:
    rows, errors = [], 0
    for item in qs.answers["items"]:
        e = item["answers"].get(system) or {}
        if e.get("status") != "SUCCESS" and errors < limit // 2:
            errors += 1
            rows.append({"case_id": item["case_id"], "status": e.get("status"), "at": kst(e.get("collected_at")), "retried": e.get("retried"),
                         "text": _cut(e.get("error"), 160), "question": _cut(item.get("question"), 80)})
    for item in qs.answers["items"]:
        e = item["answers"].get(system) or {}
        if e.get("status") == "SUCCESS" and (e.get("answer") or "").strip() and len(rows) < limit:
            rows.append({"case_id": item["case_id"], "status": "SUCCESS", "at": kst(e.get("collected_at")), "retried": e.get("retried"),
                         "text": _cut(e.get("answer"), 160), "question": _cut(item.get("question"), 80)})
    return rows


def _empty_asks(qs: QuestionSet, answers: Dict[str, Any]) -> int:
    """AIONA 오류 턴 중 빈 출력을 요구한 질문(2-1.explicit_empty)의 수."""
    groups = {f"Q{g['id']}": g for g in qs.groups}
    failed = [i for i in answers["items"] if (i["answers"].get("aiona") or {}).get("status") == "ERROR"]
    return sum(1 for i in failed if "2-1.explicit_empty" in (groups.get(i["case_id"].split("_")[0]) or {}).get("target_items", []))


def w04_aiona(qs: QuestionSet) -> Dict[str, Any]:
    s = _collection_stats(qs.answers, "aiona")
    failed = [i for i in qs.answers["items"] if (i["answers"].get("aiona") or {}).get("status") == "ERROR"]
    empty_asks = _empty_asks(qs, qs.answers)
    top = s["errors"][0] if s["errors"] else None
    failures = s["turns"] - s["success"]
    original = qs.meta.get("aiona_original") or {}
    routed = ", ".join(f"{m} {n}" for m, n in s["routed"])
    return artifact(
        "W04-AIONA-RUN", "W04", "AIONA 웹 자동화 수집 — 질문 세트 300 실제 실행", "run",
        description=("winz.aiona.kr 웹 화면을 브라우저 자동화로 열어 질문 450턴을 보내고 답변을 받은 실제 수집 결과입니다(팀원 실행, 실패한 턴만 다시 받음)."
                     + (f" 성공 답 {original['count']}개는 AIONA 대화 기록 원문(답변 아래 복사 버튼 원문과 같음)으로 바꿨습니다." if original.get("count") else "")),
        result_summary=(f"턴 {s['turns']}개 중 성공 {s['success']}({s['success_rate']:.1%}) · 오류 {s['status'].get('ERROR', 0)} · 미완료 {s['status'].get('INCOMPLETE', 0)}. "
                        + (f"오류 {len(failed)}턴 중 {empty_asks}턴은 '아무것도 출력하지 마' 같은 빈 출력 요구 질문. " if failed else "")
                        + (f"가장 많은 실패 사유: '{top[0]}' {top[1]}건. " if top else "")
                        + f"다시 받은 턴 {s['retried']}개, 성공 답변 응답 시간 중앙값 {s['latency_median_ms']:,}ms."),
        data_source=qs.source(), created_at=qs.exported, code_revision=revision("webapp/aiona_web.py"), level="real_run",
        level_note="실제 AIONA 웹 서비스에서 받은 답변. 이 답변들은 아직 평가하지 않았음.",
        limitations=[(f"AIONA 웹은 기본 '자동 추천'이라 답한 모델이 질문마다 다르다 — 기록된 모델: {routed}." if routed else
                      "AIONA 웹은 기본 '자동 추천' 모델이라 어떤 모델이 답했는지 기록되지 않는다."),
                     "빈 출력 요구 질문의 오류는 수집 실패인지 AIONA 의 실제 반응인지 평가 기준(CODE 2-1 빈 출력)으로 따로 정해야 한다.",
                     f"실패 {failures}턴은 답변이 없어 평가 대상에서 빠진다.",
                     "수집 → JSON 저장까지만 있고 평가기까지 이어진 실행은 아직 없다(T01)."],
        references=[repo_ref("답변 원문 answers.json", ANSWERS, qs.data_rev), repo_ref("수집 결과 설명 README", f"{SET_DIR}/README.md"),
                    repo_ref("AIONA 웹 수집 코드", "webapp/aiona_web.py")],
        content={"run_id": qs.meta.get("run_id"),
                 "inputs": [{"label": "질문", "value": f"{qs.meta['questions']}개 · 턴 {qs.meta['turns']}개 (question-gen-v1.2)"},
                            {"label": "수집 방식", "value": s["method"]}, {"label": "모델", "value": s["model"]},
                            {"label": "수집 시각", "value": f"{s['first']} ~ {s['last']}"}],
                 "outputs": [{"label": "성공", "value": s["success"]}, {"label": "오류", "value": s["status"].get("ERROR", 0)},
                             {"label": "미완료", "value": s["status"].get("INCOMPLETE", 0)},
                             {"label": "오류 중 빈 출력 요구 질문", "value": empty_asks},
                             {"label": "다시 받은 턴", "value": s["retried"]}, {"label": "답변 길이 중앙값", "value": f"{s['answer_chars_median']}자"}]
                            + ([{"label": "자동 추천이 고른 모델", "value": routed}] if routed else []),
                 "charts": [_status_chart([s])],
                 "errors": [{"reason": e, "count": n} for e, n in s["errors"]],
                 "log": _log(qs, "aiona")})


HISTORY_REV = "0346246"   # 같은 실행의 재시도 전 기록(AIONA 286/450)을 담은 커밋 — 이력으로만 보여 준다


def w04_history(qs: QuestionSet) -> Optional[Dict[str, Any]]:
    text = _git("show", f"{HISTORY_REV}:{ANSWERS}")
    if not text:   # git 이력이 없는 사본 — 저장된 요약을 그대로 둔다(build 의 KEEP_IDS)
        return None
    old = json.loads(text)
    s = _collection_stats(old, "aiona")
    top = s["errors"][0] if s["errors"] else None
    run_id = old["meta"].get("run_id")
    return artifact(
        "W04-AIONA-HISTORY", "W04", f"AIONA 수집 이전 기록 — 같은 실행 {run_id} 의 재시도 전 (이력)", "run",
        description=(f"같은 실행 {run_id} 의 처음 내보낸 기록입니다. 그 뒤 실패한 턴만 다시 받아 최신 기록(W04-AIONA-RUN)으로 바뀌었습니다. "
                     "성능 비교가 아니라 수집 과정의 이력입니다."),
        result_summary=(f"턴 {s['turns']}개 중 성공 {s['success']}({s['success_rate']:.1%}) · 오류 {s['status'].get('ERROR', 0)}."
                        + (f" 가장 많은 실패 사유: '{top[0]}' {top[1]}건." if top else "")),
        data_source={"label": f"이전 기록 — 커밋 {HISTORY_REV} 의 answers.json (재시도 전)", "kind": "repo_file", "path": ANSWERS,
                     "visibility": "public", "revision": HISTORY_REV, "run_id": run_id, "history": True},
        created_at=kst(old["meta"].get("exported_at")), code_revision=HISTORY_REV, level="real_run",
        level_note="실제 AIONA 웹 수집의 재시도 전 상태. 최신 기록으로 대체됐다.",
        limitations=["최신 기록과 같은 질문·같은 실행이다 — 성공률 차이는 실패한 턴을 다시 받은 결과이지 AIONA 가 나아진 것이 아니다."],
        references=[repo_ref(f"이전 answers.json (커밋 {HISTORY_REV})", ANSWERS, HISTORY_REV)],
        content={"run_id": run_id,
                 "outputs": [{"label": "성공", "value": s["success"]}, {"label": "오류", "value": s["status"].get("ERROR", 0)},
                             {"label": "미완료", "value": s["status"].get("INCOMPLETE", 0)}, {"label": "다시 받은 턴", "value": s["retried"]},
                             {"label": "내보낸 시각", "value": kst(old["meta"].get("exported_at"))}],
                 "errors": [{"reason": e, "count": n} for e, n in s["errors"]]})


def w05_api(qs: QuestionSet) -> Dict[str, Any]:
    stats = [_collection_stats(qs.answers, system) for system in ("gpt", "gemini")]
    rows = [{"system": s["system"].upper(), "model": s["model"], "method": s["method"], "success": s["success"], "turns": s["turns"],
             "latency": s["latency_median_ms"], "chars": s["answer_chars_median"], "empty": s["empty_answers"],
             "input_tokens": s["tokens"].get("input_tokens"), "output_tokens": s["tokens"].get("output_tokens")} for s in stats]
    return artifact(
        "W05-API-RUN", "W05", "GPT·Gemini API 수집 — 질문 세트 300 실제 실행", "run",
        description="같은 450턴을 GPT·Gemini API(웹 검색 도구 포함, 추론 강도 medium)로 받은 실제 수집 결과입니다(팀원 실행). Claude 는 계정 크레딧 부족으로 받지 못했습니다.",
        result_summary=" · ".join(f"{r['system']} {r['success']}/{r['turns']}" for r in rows) + " 성공. Claude 0 (크레딧 부족으로 수집 안 함).",
        data_source=qs.source(), created_at=qs.exported, code_revision=revision("webapp/agent_collection.py"), level="real_run",
        level_note="실제 API 호출로 받은 답변. 평가는 하지 않았음.",
        limitations=["Claude API 답변이 없다 — 세 모델 비교가 아니라 두 모델이다.",
                     "빈 답변 일부는 '아무것도 출력하지 마' 같은 빈 출력 질문의 정상 답일 수 있다(판정은 평가 후).",
                     "Gemini Q32 9턴은 출력 한도로 잘렸고(INCOMPLETE) 10턴은 건너뛰었다."],
        references=[repo_ref("답변 원문 answers.json", ANSWERS, qs.data_rev), repo_ref("API 수집 코드", "webapp/agent_collection.py")],
        content={"run_id": qs.meta.get("run_id"),
                 "inputs": [{"label": "질문", "value": f"{qs.meta['questions']}개 · 턴 {qs.meta['turns']}개"},
                            {"label": "추론 강도", "value": qs.meta.get("reasoning_effort")},
                            {"label": "최대 출력", "value": f"{qs.meta.get('max_output_tokens'):,} 토큰"},
                            {"label": "Claude", "value": "수집 안 함 — 계정 크레딧 부족"}],
                 "charts": [_status_chart(stats)],
                 "table": {"columns": [{"key": "system", "label": "시스템"}, {"key": "model", "label": "모델"}, {"key": "method", "label": "방식"},
                                       {"key": "success", "label": "성공", "numeric": True}, {"key": "turns", "label": "턴", "numeric": True},
                                       {"key": "latency", "label": "응답 시간 중앙값(ms)", "numeric": True},
                                       {"key": "chars", "label": "답변 길이 중앙값(자)", "numeric": True}, {"key": "empty", "label": "빈 답변", "numeric": True},
                                       {"key": "input_tokens", "label": "입력 토큰 합", "numeric": True}, {"key": "output_tokens", "label": "출력 토큰 합", "numeric": True}],
                           "rows": rows, "download": "w05-api-collection.csv"},
                 "errors": [{"reason": f"{s['system'].upper()}: {e}", "count": n} for s in stats for e, n in s["errors"]],
                 "log": _log(qs, "gemini", 4)})


def w05_saved(run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if run is None:
        return None
    counts = {p: len(rows or []) for p, rows in (run.get("datasets") or {}).items()}
    ok = {p: sum(1 for r in rows or [] if isinstance(r, dict) and str(r.get("answer") or "").strip()) for p, rows in (run.get("datasets") or {}).items()}
    rows = [{"system": p.upper() if p != "aiona" else "AIONA", "model": (run.get("models") or {}).get(p), "rows": counts[p], "answered": ok[p]}
            for p in counts]
    return artifact(
        "W05-SAVED-RUN", "W05", f"API 수집 + 평가까지 끝난 저장 실행 {SAVED_RUN_ID}", "run",
        description="AIONA 업로드 답변과 GPT·Claude·Gemini API 답변을 같은 68턴(30단위)으로 모아 평가까지 끝낸 이전 실행입니다.",
        result_summary=" · ".join(f"{r['system']} {r['answered']}/{r['rows']}" for r in rows) + " 답변 보존, 평가 완료.",
        data_source={"label": f"저장 실행 {SAVED_RUN_ID} — 공개 요약만", "kind": "saved_run", "path": SAVED_RUN, "visibility": "private",
                     "run_id": SAVED_RUN_ID, "models": run.get("models")},
        created_at=kst(run.get("created_at")), code_revision=revision("webapp/comparison_service.py"), level="real_run",
        level_note="실제 API 수집과 실제 평가 호출 결과. 판정 정확도는 사람 검수 0건이라 미측정.",
        limitations=["10-13 실행이라 지금 수집 방식(api-tools-v4: 웹 검색 도구·추론 강도)과 다르다.", "AIONA 는 업로드 원문(웹 수집 아님)이다."],
        references=[saved_run_ref()],
        content={"run_id": SAVED_RUN_ID,
                 "inputs": [{"label": "실행 이름", "value": run.get("name")}, {"label": "질문", "value": f"{run.get('question_count')}턴"}],
                 "outputs": [{"label": "상태", "value": run.get("status")}, {"label": "수집 완료", "value": kst(run.get("collection_completed_at"))},
                             {"label": "평가 시작", "value": kst(run.get("eval_started_at"))}, {"label": "끝", "value": kst(run.get("finished_at"))}],
                 "table": {"columns": [{"key": "system", "label": "시스템"}, {"key": "model", "label": "모델"},
                                       {"key": "rows", "label": "턴", "numeric": True}, {"key": "answered", "label": "답변 있음", "numeric": True}],
                           "rows": rows}})


def w08_paths(qs: QuestionSet) -> Dict[str, Any]:
    stats = [_collection_stats(qs.answers, system) for system in ("aiona", "gpt", "gemini")]
    rows = [{"system": "AIONA" if s["system"] == "aiona" else s["system"].upper(), "path": "웹 자동화" if s["system"] == "aiona" else "API + 검색 도구",
             "model": s["model"], "success_rate": s["success_rate"], "latency": s["latency_median_ms"], "chars": s["answer_chars_median"],
             "retried": s["retried"]} for s in stats]
    return artifact(
        "W08-PATHS", "W08", "같은 450턴, 수집 경로별 결과 (웹 자동화 vs API)", "experiment",
        description=("질문 세트 300 의 같은 질문을 AIONA 는 웹 자동화로, GPT·Gemini 는 API + 검색 도구로 받았습니다. "
                     "경로별 성공률·응답 시간·답변 길이를 나란히 놓았습니다. 점수 비교는 아직 평가하지 않아 할 수 없습니다."),
        result_summary=" · ".join(f"{r['system']}({r['path']}) 성공 {r['success_rate']:.1%}, 응답 {r['latency']:,}ms" for r in rows) + ".",
        data_source=qs.source(), created_at=qs.exported, code_revision=revision("webapp/aiona_web.py", "webapp/agent_collection.py"),
        level="measured", level_note="실제 수집 결과에서 계산. 모델 자체가 달라 '경로 차이'만의 효과로 볼 수 없음.",
        limitations=["시스템마다 모델이 달라 같은 모델의 API ↔ 웹 비교가 아니다 — 경로 효과와 모델 효과가 섞여 있다.",
                     "같은 모델(GPT 등)을 웹 서비스로 받은 결과가 없어(W06) 경로만 바꾼 비교는 아직 불가능하다.",
                     "평가하지 않아 점수 차이는 알 수 없다."],
        references=[repo_ref("답변 원문 answers.json", ANSWERS, qs.data_rev), repo_ref("한 실행 안 수집 방식 비교 코드", "webapp/run_summary.py")],
        content={"charts": [_status_chart(stats)],
                 "table": {"columns": [{"key": "system", "label": "시스템"}, {"key": "path", "label": "수집 경로"}, {"key": "model", "label": "모델"},
                                       {"key": "success_rate", "label": "성공률", "percent": True},
                                       {"key": "latency", "label": "응답 시간 중앙값(ms)", "numeric": True},
                                       {"key": "chars", "label": "답변 길이 중앙값(자)", "numeric": True}, {"key": "retried", "label": "다시 받은 턴", "numeric": True}],
                           "rows": rows, "download": "w08-collection-paths.csv"}})


# --- W06·W07·W09 ------------------------------------------------------------------------------

def w07_code() -> Dict[str, Any]:
    paths = ["webapp/auto_collection.py", "frontend/src/collection-settings.js", ".env.example"]
    rev = revision(*paths)
    return artifact(
        "W07-FACTCHAT-API", "W07", "FactChat API 게이트웨이 연결 (웹 채팅 아님)", "code",
        description="FactChat 의 OpenAI 호환 API 게이트웨이로 답변을 받는 설정입니다. 멘토가 요청한 FactChat '웹 채팅' 수집과는 다른 경로입니다.",
        result_summary="API 키·모델·주소(허용 주소 2개만) 설정과 자동 수집 연결 구현. 실제 FactChat 호출 기록과 웹 채팅 수집은 없음.",
        data_source={"label": "저장소 코드", "kind": "code", "path": paths[0], "visibility": "public", "revision": rev},
        created_at=committed_at(rev), code_revision=rev, level="auto_tested",
        level_note="자동 수집 테스트가 모의 응답으로 게이트웨이 경로를 확인. 실제 호출은 확인하지 않음 — 실측 미완료.",
        limitations=["멘토 요청은 웹 채팅 화면에서의 수집이다 — API 게이트웨이로는 요청을 충족하지 못한다.",
                     "FactChat 웹 접속 계정·허용 범위가 확인되지 않았다."],
        references=[repo_ref(p, p) for p in paths[:2]],
        content={"features": ["FACTCHAT_API_KEY · FACTCHAT_MODEL · FACTCHAT_BASE_URL (.env)",
                              "주소는 https://api.factchat.ai/v1 또는 https://factchat-cloud.mindlogic.ai/v1/gateway 만 허용",
                              "업로드 뒤 자동 수집 정책에서 다른 제공자와 같은 방식으로 선택"],
                 "files": [{"path": p, "url": github(p, revision(p))} for p in paths],
                 "tests": [{"path": "tests/test_auto_collection.py", "count": _test_count("tests/test_auto_collection.py")}]})


def w09_contract(qs: QuestionSet) -> Dict[str, Any]:
    group = next(g for g in qs.groups if len(g["turns"]) == 2)
    sample_group = {**{k: group.get(k) for k in ("id", "type", "topic", "target_items", "difficulty", "trap")},
                    "turns": [{"turn": t.get("turn"), "question": _cut(t.get("question"), 200)} for t in group["turns"]]}
    item = next(i for i in qs.answers["items"] if i["answers"]["gpt"].get("status") == "SUCCESS" and i["answers"]["aiona"].get("status") == "SUCCESS")
    sample_item = {**{k: item.get(k) for k in ("index", "case_id", "conversation_id", "turn", "reference_chars", "attachments")},
                   "question": _cut(item.get("question"), 120),
                   "answers": {s: {**{k: v for k, v in e.items() if k not in ("answer", "usage")}, "answer": _cut(e.get("answer"), 80)}
                               for s, e in item["answers"].items()}}
    rev = revision("webapp/question_generation.py", "webapp/dataset_import.py")
    return artifact(
        "W09-CONTRACT", "W09", "질문·답변 JSON 인계 형식 (실제 파일에서 뽑은 예)", "data",
        description="질문 세트(questions.json)와 수집 결과(answers.json)의 실제 형식입니다. 평가기는 같은 질문 세트 형식을 '질문 직접 넣기'·업로드로 받습니다.",
        result_summary="questions.json: 질문(대화)마다 id·type·topic·target_items·difficulty·trap·turns. answers.json: 턴마다 case_id·질문·시스템별 답변·상태·시각·방식·사용 토큰.",
        data_source=qs.source(), created_at=qs.exported, code_revision=rev, level="auto_tested",
        level_note="형식 읽기는 자동 테스트로 확인. 질문 세트 300 의 수집 결과를 평가기에 넣어 평가까지 간 기록은 없음 — 실측 미완료.",
        limitations=["answers.json 은 내보내기 형식이다. 평가기에 그대로 넣는 가져오기(답변 포함) 경로는 이 파일로 확인하지 않았다.",
                     "실수집 원문 → JSON → 평가 결과 식별자가 이어지는 한 건 시연(T01)이 아직 없다."],
        references=[repo_ref("questions.json", QUESTIONS, qs.data_rev), repo_ref("answers.json", ANSWERS, qs.data_rev),
                    repo_ref("가져오기 코드", "webapp/dataset_import.py")],
        content={"table": {"columns": [{"key": "file", "label": "파일"}, {"key": "field", "label": "필드"}, {"key": "meaning", "label": "뜻"}],
                           "rows": [{"file": "questions.json", "field": f, "meaning": m} for f, m in (
                               ("id", "질문(대화) 번호 — 수집 때 case_id Q{id}_T{turn}"), ("type · topic · difficulty · trap", "유형·주제·난도·FAIL 함정"),
                               ("target_items", "노린 평가 항목"), ("turns[].turn · question", "턴 번호·질문 원문"),
                               ("turns[].attachments[]", "name·type·content·sha256·size·path(files/ 아래)"), ("turns[].reference", "긴 참조 자료 원문"))] +
                                   [{"file": "answers.json", "field": f, "meaning": m} for f, m in (
                               ("meta", "run_id·생성기·모델·수집 방식·상태별 개수·평가 여부"), ("items[].case_id · conversation_id · turn", "질문 식별"),
                               ("items[].answers.{시스템}", "answer·status·error·latency_ms·collected_at·method·usage·retried"))]},
                 "examples": [{"label": "questions.json 의 2턴 대화 하나", "json": sample_group},
                              {"label": "answers.json 의 턴 하나 (답변은 앞부분만)", "json": sample_item}]})


# --- Y 항목 -----------------------------------------------------------------------------------

def y01_targets() -> Dict[str, Any]:
    from code_eval.criteria_registry import REGISTRY_VERSION, evaluation_targets
    counts = evaluation_targets()["counts"]
    rev = revision("code_eval/criteria_registry.py")
    labels = [("code_categories", "CODE 대분류"), ("code_subcategories", "CODE 소분류"), ("code_checks", "CODE 검사"),
              ("semantic_criteria", "의미 기준"), ("semantic_subs", "의미 세부 기준"), ("semantic_items", "의미 점검 항목"),
              ("total", "평가 기준 합계(CODE 검사 + 의미 항목)")]
    return artifact(
        "Y01-TARGETS", "Y01", f"평가 기준 목록 ({REGISTRY_VERSION})", "data",
        description="코드의 기준 목록(한 곳에서 관리)에서 센 단계별 기준 수입니다. 질문 생성기·사전 점검·최소 질문 수 도구가 같은 목록을 씁니다.",
        result_summary=" · ".join(f"{label} {counts[key]}" for key, label in labels),
        data_source={"label": "코드 기준 목록", "kind": "code", "path": "code_eval/criteria_registry.py", "visibility": "public", "revision": rev},
        created_at=committed_at(rev), code_revision=rev, level="auto_tested",
        level_note="문서·설계안 머리말과 수가 같은지 자동 테스트(test_docs_consistency)로 확인. 기준별 판정 정확도는 미측정.",
        limitations=["기준이 코드에 있다는 것과 그 기준의 판정이 맞다는 것은 다르다(Y02)."],
        references=[repo_ref("기준 목록", "code_eval/criteria_registry.py", rev)],
        content={"table": {"columns": [{"key": "level", "label": "단계"}, {"key": "count", "label": "개수", "numeric": True}],
                           "rows": [{"level": label, "count": counts[key]} for key, label in labels]}})


def y03_policy(run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if run is None:
        return None
    from code_eval.multi_turn import MIN_TOTAL_TOKENS, MIN_TURNS, MULTI_TURN_POLICY_VERSION
    from tools.j6j7_rules import j6
    report = j6(run)
    systems = list(report["rules"])
    measured = {s: report["rules"][s]["current"]["conversations"] for s in systems}
    two = {s: report["rules"][s]["two_plus"]["conversations"] for s in systems}
    shared = {s: report["rules"][s]["stress_shared"]["conversations"] for s in systems}
    fmt = lambda d: " · ".join(f"{('AIONA' if s == 'aiona' else s.upper())} {n}" for s, n in d.items())
    return artifact(
        "Y03-J6-POLICY", "Y03", "J6 멀티턴 적용 규칙 — 대안 비교와 결정 상태", "policy",
        description=(f"J6 를 어떤 대화에 적용할지 세 규칙을 같은 저장 실행({SAVED_RUN_ID}, 대화 {report['conversations']}개)에 적용해 비교했습니다. "
                     "길이를 각 모델의 자기 답변으로 재면 같은 대화도 모델마다 적용 여부가 갈립니다."),
        result_summary=f"현재 규칙({MULTI_TURN_POLICY_VERSION}) 적용 대화: {fmt(measured)}. 모델마다 갈린 대화 {len(report['divergent_conversations'])}개.",
        data_source={"label": f"저장 실행 {SAVED_RUN_ID} — 공개 요약만", "kind": "saved_run", "path": SAVED_RUN, "visibility": "private",
                     "run_id": SAVED_RUN_ID},
        created_at=kst(run.get("finished_at")), code_revision=revision("tools/j6j7_rules.py", "code_eval/multi_turn.py"), level="measured",
        level_note="저장된 실제 대화로 규칙별 적용 수를 계산 (모델 호출 없음). J6 판정 자체는 새로 하지 않았음.",
        limitations=["대화 2개짜리 실행이라 규칙 차이를 보여 주는 예시일 뿐 일반화할 수 없다.", "새 장문 시험 세트(4차)는 아직 답변·판정이 없다."],
        references=[saved_run_ref(), repo_ref("규칙 비교 도구", "tools/j6j7_rules.py"), repo_ref("정책 제안 문서", "참고자료/정책제안_J6J7_적용범위_2026-10-14.md")],
        content={"question": "J6(멀티턴 맥락 유지)를 어떤 대화에 적용해야 모델 비교가 공정한가?",
                 "alternatives": [
                     {"name": "예전: 2턴 이상", "rule": "2턴 이상 대화의 2번째 턴부터", "measured": fmt(two),
                      "pros": "J6 정의('앞 대화의 지시를 계속 지키는가')와 맞고 모든 모델에 같은 조건", "cons": "짧은 대화도 멀티턴으로 세어 '긴 대화 능력'을 따로 보지 못함"},
                     {"name": f"현재: {MULTI_TURN_POLICY_VERSION}", "rule": f"{MIN_TURNS}턴 이상 · 대화 전체(질문+답변) {MIN_TOTAL_TOKENS:,} 토큰 이상, 각 모델의 자기 답변으로 잼",
                      "measured": fmt(measured), "pros": "긴 대화만 J6 로 보아 멘토 요청(긴 대화)에 가까움",
                      "cons": "답변을 길게 쓰는 모델만 적용될 수 있어 모델마다 분모가 다름"},
                     {"name": "제안 A: 같은 시나리오로 잼", "rule": "길이를 질문 + 기준 답변(AIONA 업로드 답변)으로 재 모든 모델이 같은 판정",
                      "measured": fmt(shared), "pros": "같은 대화면 모든 모델이 같은 조건", "cons": "기준 답변이 없는 세트는 질문만으로 재야 함"}],
                 "decision": {"status": "일부 결정", "text": f"길이 조건은 사용자 결정으로 {MULTI_TURN_POLICY_VERSION}(10턴 이상 · 대화 전체 10K)로 바뀌었다(main 085345d·fa52f17).",
                              "reason": "v1(한 턴 10K × 10턴)은 턴마다 자료를 과하게 붙이게 했다.",
                              "open": "길이를 '자기 답변'으로 잴지 '같은 시나리오'로 잴지는 아직 결정되지 않았다."}})


# --- 우영 담당 통합 요약 (W01~W09 를 A 질문 생성 · B 답변 수집 · C 평가 연동으로) ----------------------------

def _value(source: Optional[Dict[str, Any]], label: str, key: str = "stats") -> Any:
    return next((s["value"] for s in ((source or {}).get("content") or {}).get(key) or [] if s["label"] == label), None)


def _service(service: str, method: str, requirement: str, state: str, *, requests: Optional[int] = None, success: Optional[int] = None,
             run_id: Optional[str] = None, when: Optional[str] = None, cause: str = "—", status: str = "",
             chart_label: Optional[str] = None) -> Dict[str, Any]:
    measured = requests is not None and success is not None
    return {"service": service, "method": method, "requirement": requirement, "state": state,
            "record": {"real": "실측", "history": "이력(같은 실행 재시도 전)", "none": "실측 없음 — 미완료"}[state],
            "requests": requests, "success": success, "failed": requests - success if measured else None,
            "rate": _pct(success, requests) if measured else None, "run_id": run_id, "when": when, "cause": cause, "status": status,
            "chart_label": chart_label}


def _causes(errors: Sequence[Sequence[Any]]) -> str:
    return "; ".join(f"{_cut(reason, 60)} ({count}턴)" for reason, count in list(errors)[:2]) or "—"


def woo_overview(by_id: Dict[str, Dict[str, Any]], qs: QuestionSet) -> Dict[str, Any]:
    """우영 담당 W01~W09 를 세 묶음으로 — 수치는 모두 위 산출물과 저장소 결과 파일에서 가져온다(새로 지어내지 않는다)."""
    from code_eval.multi_turn import MIN_TURNS
    from webapp.question_generation import QUESTION_GEN_VERSION
    intent, generated, matrix = by_id["W01-INTENT"], by_id["W01-SET300"], by_id["W02-MATRIX"]
    conclusion, saved, history = by_id["W03-CONCLUSION"], by_id.get("W05-SAVED-RUN"), by_id.get("W04-AIONA-HISTORY")
    meta, run_id, turns = qs.meta, qs.meta.get("run_id"), qs.meta["turns"]
    gen = str(meta.get("question_generator") or "버전 기록 없음").split(" ")[0]
    questions, rate, rest = _value(generated, "질문(대화)"), _value(intent, "적용률"), _value(intent, "J6 제외 적용률")
    applied, missed = _value(intent, "실제 적용"), _value(intent, "적용 안 됨")
    j6_missed = next((r["missed"] for r in intent["content"]["table"]["rows"] if r["family"] == "J6"), 0)
    countable, never = _value(matrix, "최하위 기준(셀 수 있음)"), _value(matrix, "적용 0")

    group_a = {
        "id": "A", "title": "질문 생성 및 품질 검증", "requirements": ["W01", "W02", "W03"],
        "headline": f"질문 {questions}개 실제 생성 · 목표 항목 적용 {rate}",
        "finding": (f"질문 생성기 {QUESTION_GEN_VERSION} 는 구현됐지만 실제 생성은 아직 하지 않았습니다. 이전 판({gen})으로 실제 만든 질문 {questions}개에서 "
                    f"'시험하려던 기준'이 실제로 적용된 것은 판단 가능한 {applied + missed}건 중 {applied}건({rate})이고, 빗나간 {missed}건 중 {j6_missed}건은 "
                    f"J6(긴 대화) 조건 불일치 — 짧은 대화를 J6 로 노렸지만 평가기는 {MIN_TURNS}턴 이상 대화에만 J6 를 적용합니다. AI 답변 품질과는 관계없는 수치입니다."),
        "scope": f"아래 수치는 모두 {gen} 실제 생성 결과(실행 {run_id}) · 현재 코드 {QUESTION_GEN_VERSION} 는 구현·모의 테스트만 확인",
        "metrics": [{"label": "실제 생성 질문", "value": questions, "note": f"{gen} · 실제 GPT 생성", "artifact_id": "W01-SET300"},
                    {"label": "목표 항목 실제 적용", "value": f"{applied}/{applied + missed}건", "note": f"{rate} · {gen} · 코드 규칙 계산",
                     "artifact_id": "W01-INTENT"},
                    {"label": "J6 제외 적용률", "value": rest, "note": f"{gen} · J6 를 뺀 집계", "artifact_id": "W01-INTENT"},
                    {"label": "최하위 기준 중 미적용", "value": f"{never}/{countable}개",
                     "note": f"질문 세트 300 에서 한 번도 적용 안 됨 · {countable}개 = 늘 적용 검사 "
                             f"{sum(r['group'] == 'CODE 늘 적용' for r in matrix['content']['table']['rows'])}개 포함",
                     "artifact_id": "W02-MATRIX"}],
        "charts": [{"label": "목표 항목 적용률", "kind": "intent", "artifact_id": "W01-INTENT", "version": gen},
                   {"label": "기준별 질문 커버리지", "kind": "chart", "artifact_id": "W02-MATRIX", "chart": 0},
                   {"label": "질문 수 연구", "kind": "chart", "artifact_id": "W03-COVERAGE", "chart": 0}],
        "open_issue": "간편·정밀 검사 적정 질문 수 미확정 — " + conclusion["result_summary"]}

    stats = {system: _collection_stats(qs.answers, system) for system in ("aiona", "gpt", "gemini")}
    a = stats["aiona"]
    when = lambda s: f"{s['first']} ~ {s['last']}"
    services = [_service("AIONA 웹", "웹 자동화 (aiona-web-v1)", "W04", "real", requests=a["turns"], success=a["success"], run_id=run_id,
                         when=when(a), cause=f"빈 출력 요구 질문 {_empty_asks(qs, qs.answers)}턴 포함 · " + _causes(a["errors"]),
                         status="구현 · 실제 수집", chart_label="AIONA 웹")]
    if history:
        ok, err, inc = (_value(history, label, "outputs") for label in ("성공", "오류", "미완료"))
        services.append(_service("AIONA 웹 (재시도 전 기록)", "웹 자동화 (aiona-web-v1)", "W04", "history", requests=ok + err + inc, success=ok,
                                 run_id=history["data_source"].get("run_id"), when=history["created_at"],
                                 cause=_causes([(e["reason"], e["count"]) for e in history["content"]["errors"]]),
                                 status=f"이력 — 실패한 턴만 다시 받아 위 기록으로 대체 (커밋 {HISTORY_REV})"))
    for system, name in (("gpt", "GPT API"), ("gemini", "Gemini API")):
        s = stats[system]
        services.append(_service(name, s["method"], "W05", "real", requests=s["turns"], success=s["success"], run_id=run_id, when=when(s),
                                 cause=_causes(s["errors"]), status="구현 · 실제 수집", chart_label=name))
    services.append(_service("Claude API (질문 세트 300)", "API (api-tools-v4)", "W05", "none", cause="계정 크레딧 부족으로 수집하지 않음",
                             status="수집 코드 있음 · 이 세트 실측 없음"))
    claude = next((r for r in ((saved or {}).get("content") or {}).get("table", {}).get("rows", []) if r["system"] == "CLAUDE"), None)
    if claude:
        services.append(_service(f"Claude API (저장 실행 {SAVED_RUN_ID})", "API", "W05", "real", requests=claude["rows"], success=claude["answered"],
                                 run_id=SAVED_RUN_ID, when=saved["created_at"], status=f"다른 질문 {claude['rows']}턴 · 평가까지 끝난 실행",
                                 chart_label="Claude API (다른 실행)"))
    for name in ("GPT 웹", "Claude 웹", "Gemini 웹"):
        services.append(_service(name, "웹 자동화", "W06", "none", cause="수집 코드 없음",
                                 status="로그인 창을 여는 도구만 — 질문을 보내 답을 받는 코드 없음"))
    services.append(_service("FactChat 웹", "웹 자동화", "W07", "none", cause="이 브랜치에 수집 코드 없음",
                             status="main 039a4f0 에 웹 수집 코드(질문 1개 시험 성공 — 커밋 기록) · 이 브랜치 미반영 · 저장된 수집 기록 없음"))
    services.append(_service("FactChat API 게이트웨이", "API", "W07", "none", cause="실제 호출 기록 없음", status="설정·모의 테스트만 (W07-FACTCHAT-API)"))
    charted = [r for r in services if r["state"] == "real"]
    missing = [r["service"] for r in services if r["state"] == "none"]
    conditions = [f"AIONA·GPT·Gemini — 같은 질문 세트 300 · {turns}턴 · 실행 {run_id} · 수집 "
                  f"{min(s['first'] for s in stats.values())} ~ {max(s['last'] for s in stats.values())}"]
    if claude:
        conditions.append(f"Claude — 다른 실행: 저장 실행 {SAVED_RUN_ID} · 다른 질문 {claude['rows']}턴 · {saved['created_at']} (위 세트와 같은 조건이 아님)")
    rates = {system: f"{s['success']}/{s['turns']}" for system, s in stats.items()}
    group_b = {
        "id": "B", "title": "모델별 답변 수집", "requirements": ["W04", "W05", "W06", "W07"],
        "headline": " · ".join(f"{n} {stats[k]['success_rate']:.1%}" for k, n in (("aiona", "AIONA"), ("gpt", "GPT"), ("gemini", "Gemini"))) + " 실측",
        "finding": (f"같은 질문 세트 300({turns}턴, 실행 {run_id})에서 AIONA 웹 {rates['aiona']}({a['success_rate']:.1%}), GPT API {rates['gpt']}, "
                    f"Gemini API {rates['gemini']}를 실제로 받았습니다. AIONA 실패 {a['turns'] - a['success']}턴 중 "
                    f"{_empty_asks(qs, qs.answers)}턴은 빈 출력을 요구한 질문에 AIONA 가 오류로 멈춘 경우입니다."),
        "scope": " / ".join(conditions),
        "metrics": [{"label": "AIONA 웹", "value": rates["aiona"], "artifact_id": "W04-AIONA-RUN",
                     "note": f"{a['success_rate']:.1%} · 최신 기록" + (f" (재시도 전 {services[1]['success']}/{services[1]['requests']})" if history else "")},
                    {"label": "GPT API", "value": rates["gpt"], "note": f"{stats['gpt']['success_rate']:.1%} · {stats['gpt']['model']}", "artifact_id": "W05-API-RUN"},
                    {"label": "Gemini API", "value": rates["gemini"], "note": f"{stats['gemini']['success_rate']:.1%} · {stats['gemini']['model']}",
                     "artifact_id": "W05-API-RUN"},
                    {"label": "실측 없는 경로", "value": len(missing), "note": "웹 3종(W06) · FactChat(W07) · 이 세트의 Claude API"}],
        "chart": {"kind": "bar", "title": "서비스별 수집 완료율 — 성공 턴 ÷ 요청 턴 (실측이 있는 것만)", "unit": "%", "max": 100, "label_width": 150,
                  "categories": [r["chart_label"] for r in charted], "series": [{"name": "수집 완료율", "values": [round(r["rate"] * 100, 1) for r in charted]}]},
        "conditions": conditions, "missing": missing,
        "table": {"columns": [{"key": "service", "label": "모델·서비스"}, {"key": "method", "label": "수집 방식"}, {"key": "requirement", "label": "요구사항"},
                              {"key": "record", "label": "기록"}, {"key": "requests", "label": "요청 턴", "numeric": True},
                              {"key": "success", "label": "성공", "numeric": True}, {"key": "failed", "label": "실패·미완료", "numeric": True},
                              {"key": "rate", "label": "완료율", "percent": True}, {"key": "run_id", "label": "실행 ID"}, {"key": "when", "label": "시점"},
                              {"key": "cause", "label": "실패 원인"}, {"key": "status", "label": "현재 구현 상태"}],
                  "rows": services, "download": "woo-collection-services.csv"},
        "open_issue": "GPT·Claude·Gemini 웹 수집(W06)과 FactChat 웹 수집(W07)은 실측이 없습니다 — 같은 모델을 API 와 웹으로 받아 비교(W08)할 수 없습니다."}

    total_ok = sum(s["success"] for s in stats.values())
    per_answer = (conclusion.get("content") or {}).get("per_answer_usd")
    estimate = f"추정 약 ${round(per_answer * turns * 3, 1)}, Batches 사용 시 약 절반" if per_answer else "비용 추정 자료 없음"
    evaluated = meta.get("evaluation") not in (None, "하지 않음")
    reviewed = _value(by_id.get("W02-SAVED-JUDGMENTS"), "사람 검수")
    saved_turns = _value(saved, "질문", "inputs") if saved else None
    saved_note = f" 끝까지 이어진 실행은 다른 질문 세트(저장 실행 {SAVED_RUN_ID}, {saved_turns})뿐입니다." if saved else ""
    steps = [
        {"name": "질문 생성", "state": "real", "requirement": "W01",
         "detail": f"질문 {questions}개 · 턴 {turns}개 ({gen}, {meta.get('generator_model')}). 현재 {QUESTION_GEN_VERSION} 는 구현·모의 테스트만"},
        {"name": "모델별 답변 수집", "state": "partial", "requirement": "W04",
         "detail": f"AIONA 웹 {rates['aiona']} · GPT API {rates['gpt']} · Gemini API {rates['gemini']}. 실측 없음: {', '.join(missing)}"},
        {"name": "JSON 저장", "state": "real", "requirement": "W09",
         "detail": f"questions.json · answers.json — 턴 {turns} × 시스템 3, 성공 답변 {total_ok:,}개 (실행 {run_id})"},
        {"name": "평가 기준 적용", "state": "tested", "requirement": "W02",
         "detail": f"질문만으로 정해지는 기준은 평가와 같은 코드 규칙으로 계산(모델 호출 없음) — 최하위 기준 {countable}개 중 {never}개 미적용. 답변이 필요한 기준은 평가 때 정해짐"},
        {"name": "CODE·LLM 평가", "state": "real" if evaluated else "pending", "requirement": "W09",
         "detail": ("질문 세트 300 평가 완료" if evaluated else f"질문 세트 300 답변 평가 미실행 — LLM 평가는 유료라 승인 필요({estimate}).") + saved_note},
        {"name": "결과 비교", "state": "real" if evaluated else "pending", "requirement": "W08",
         "detail": ("질문 세트 300 비교 결과 있음" if evaluated else "질문 세트 300 의 모델별 점수 비교는 아직 없음.")
                   + (f" 저장 실행 {SAVED_RUN_ID} 는 결과 화면에서 모델별 비교를 볼 수 있음" if saved else "")}]
    group_c = {
        "id": "C", "title": "수집 결과 비교 및 평가 연동", "requirements": ["W08", "W09"],
        "headline": "생성 → 수집 → JSON 저장 실제 연결 · " + ("300세트 평가 완료" if evaluated else "300세트 평가 미실행"),
        "finding": ("질문 생성 → 답변 수집 → JSON 저장까지는 질문 세트 300 으로 실제 연결됐습니다. 평가 기준 적용은 코드 규칙으로 계산만 했고, "
                    + ("CODE·LLM 평가와 결과 비교도 끝났습니다." if evaluated else "CODE·LLM 평가와 결과 비교는 이 세트로 아직 실행하지 않았습니다.") + saved_note),
        "metrics": [{"label": "300세트 성공 답변", "value": total_ok, "note": "JSON 저장 · 평가 전" if not evaluated else "JSON 저장"},
                    {"label": "300세트 평가", "value": "완료" if evaluated else "미실행", "note": "유료 · 승인 필요" if not evaluated else ""},
                    {"label": "평가까지 끝난 저장 실행", "value": 1 if saved else 0, "note": f"{SAVED_RUN_ID} · 다른 질문 {saved_turns}" if saved else "이 PC 기록 없음"},
                    {"label": "사람 검수", "value": reviewed if reviewed is not None else "기록 없음",
                     "note": "판정 정확도 미측정" if not reviewed else "검수 결과 확인"}],
        "steps": steps, "saved_run": saved_run_ref() if saved else None,
        "open_issue": "질문 세트 300 답변을 평가기에 넣어 결과 식별자까지 잇는 한 건 시연(T01)이 없습니다 — LLM 평가는 유료라 승인이 필요합니다."}

    plan = {"remaining": [
                {"requirement": "W09", "text": "질문 세트 300 답변 → 평가기 → 결과 식별자까지 한 건 시연 (T01)"},
                {"requirement": "W03", "text": "300세트 평가 결과로 간편·정밀 질문 수를 다시 판단 — 지금은 결정 보류"},
                {"requirement": "W01", "text": f"{QUESTION_GEN_VERSION} 로 실제 생성해 목표 항목 적용률을 다시 측정"},
                {"requirement": "W06", "text": "GPT·Claude·Gemini 웹 수집 코드와 실측"},
                {"requirement": "W07", "text": "FactChat 웹 수집 — main 039a4f0 코드를 반영한 뒤 질문 세트로 실측"}],
            "approvals": [
                {"what": "질문 세트 300 답변 평가 (유료 Claude 호출)", "why": f"턴 {turns} × 시스템 3 — {estimate}"},
                {"what": f"{QUESTION_GEN_VERSION} 실제 질문 생성 (유료 GPT 호출)", "why": f"v1.4 실측이 없어 지금 판단은 {gen} 결과로만 함"},
                {"what": "최신 main 병합", "why": "충돌이 있어 보류 — 이 브랜치에는 질문 세트 300 수집 결과 파일만 반영"},
                {"what": "FactChat 웹 계정·허용 범위 확인", "why": "W07 실측 전에 필요"}]}
    return {"owner": "우영", "title": "우영 담당 통합 성과 (W01~W09)", "requirements": [f"W0{i}" for i in range(1, 10)],
            "data_as_of": qs.exported, "data_revision": qs.data_rev, "human_reviewed": reviewed,
            "groups": [group_a, group_b, group_c], "plan": plan}


# --- 모으기 -----------------------------------------------------------------------------------

PRIVATE_IDS = ("W02-SAVED-JUDGMENTS", "W03-STABILITY", "W05-SAVED-RUN", "Y03-J6-POLICY")
KEEP_IDS = PRIVATE_IDS + ("W04-AIONA-HISTORY",)   # 원본(비공개 실행·git 이력)이 없을 때 저장된 요약을 그대로 두는 산출물


def build(existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    existing_by_id = {a["artifact_id"]: a for a in (existing or {}).get("artifacts") or []}
    run_path = ROOT / SAVED_RUN
    run = json.loads(run_path.read_text(encoding="utf-8")) if run_path.is_file() else None
    qs = QuestionSet()
    if run is not None:
        from tools.sample_size_study import judge_costs
        per_answer = judge_costs(run)["usd_per_answer"]
    else:   # 비공개 실행이 없는 PC — 저장된 값(같은 실행에서 계산해 둔 답변당 비용)을 쓴다
        per_answer = ((existing_by_id.get("W01-QUANTITY") or {}).get("content") or {}).get("per_answer_usd")
    intent, quantity = w01_intent(qs), w01_quantity(qs, per_answer)
    made = [w01_prompt(qs), w01_design(qs, intent, quantity), w01_set(qs), intent, quantity, w01_code(),
            w02_matrix(qs), w02_criteria(qs), w02_saved(run),
            w03_coverage(qs), w03_stability(run), w03_conclusion(qs, run, existing_by_id.get("W03-CONCLUSION")),
            w04_aiona(qs), w04_history(qs), w05_api(qs), w05_saved(run), w07_code(), w08_paths(qs), w09_contract(qs),
            y01_targets(), y03_policy(run)]
    kept: List[str] = []
    out = [value for value in made if value is not None]
    for artifact_id in KEEP_IDS:   # 비공개 원본(또는 git 이력)이 없는 PC — 저장된 공개 요약을 그대로 둔다
        if not any(a["artifact_id"] == artifact_id for a in out) and artifact_id in existing_by_id:
            out.append(existing_by_id[artifact_id])
            kept.append(artifact_id)
    order = {a["artifact_id"]: i for i, a in enumerate(a for a in made if a is not None)}
    out.sort(key=lambda a: (a["requirement_id"], order.get(a["artifact_id"], 999), a["artifact_id"]))
    result = {"version": ARTIFACTS_VERSION, "generated_by": "tools/progress_artifacts.py",
            "levels": {"none": "미확인", "implemented": "구현 완료", "auto_tested": "자동 테스트 확인", "measured": "실제 데이터 계산",
                       "real_run": "실제 모델 실행", "accuracy_verified": "판정 정확도 검증"},
            "private_sources": {SAVED_RUN: "present" if run is not None else "missing"},
            "kept_without_source": kept, "artifacts": out,
            "overview": woo_overview({a["artifact_id"]: a for a in out}, qs)}
    return json.loads(json.dumps(result, ensure_ascii=False))   # 저장 파일과 같은 모양(튜플 → 목록)으로 비교한다


def _diff(saved: Dict[str, Any], fresh: Dict[str, Any]) -> List[str]:
    a = {x["artifact_id"]: x for x in saved.get("artifacts") or []}
    b = {x["artifact_id"]: x for x in fresh.get("artifacts") or []}
    lines = [f"없어짐: {k}" for k in a.keys() - b.keys()] + [f"새로 생김: {k}" for k in b.keys() - a.keys()]
    for key in sorted(a.keys() & b.keys()):
        for field in sorted(set(a[key]) | set(b[key])):
            if a[key].get(field) != b[key].get(field):
                lines.append(f"{key}.{field} 가 다름")
    if saved.get("overview") != fresh.get("overview"):
        lines.append("overview(우영 통합 요약) 가 다름")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="저장된 artifacts.json 이 다시 계산한 값과 같은지만 확인")
    args = parser.parse_args(argv)
    existing = json.loads(OUT.read_text(encoding="utf-8")) if OUT.is_file() else None
    fresh = build(existing)
    if args.check:
        if existing is None:
            print("artifacts.json 이 없습니다 — python tools/progress_artifacts.py 로 만드세요.")
            return 1
        lines = _diff(existing, fresh)
        print("\n".join(lines) if lines else f"일치 — 산출물 {len(fresh['artifacts'])}개")
        return 1 if lines else 0
    OUT.write_text(json.dumps(fresh, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"저장: {OUT.relative_to(ROOT)} — 산출물 {len(fresh['artifacts'])}개"
          + (f" (비공개 원본 없어 저장된 요약 유지: {', '.join(fresh['kept_without_source'])})" if fresh["kept_without_source"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
