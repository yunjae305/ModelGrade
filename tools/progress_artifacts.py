"""업무 진행 현황의 산출물 자료(frontend/data/artifacts.json) — 실제 결과 파일과 코드에서 계산한다 (모델 호출 없음).

    python tools/progress_artifacts.py            # 다시 계산해 저장
    python tools/progress_artifacts.py --check    # 저장된 파일과 다시 계산한 값이 같은지 (다르면 1, 무엇이 다른지 출력)

- 공개 자료: 저장소 안 질문 세트 300·수집 결과(참고자료/수집결과/2026-10-14-질문세트300), 질문 생성 프롬프트(webapp/question_generation.py),
  기준 목록(code_eval/criteria_registry.py), 적용 규칙(tools/check_question_set.py·min_question_count.py).
- 비공개 자료: 저장 실행 data/comparison_runs/e3c60d3efd18.json (저장소 밖). 개수·비율 같은 공개 요약만 남기고 질문·답변 원문은 넣지 않는다.
  이 파일이 없는 PC 에서 다시 만들면 저장된 artifacts.json 의 그 산출물을 그대로 둔다 (원본 없이 지어내지 않는다).
- 표·그래프 수치는 모두 여기서 계산한 값이다. 화면(frontend/src/progress-workspace.js)은 이 값을 그대로 그린다.
- 판정 정확도는 사람 검수 결과가 있어야 채운다 — 이 도구는 정확도를 만들지 않는다.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
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


def w01_prompt() -> Dict[str, Any]:
    from webapp import question_generation as qg
    catalog = qg.criteria_catalog()
    target = 300
    multi, long_ = qg.split_counts(target)
    first = min(qg.PLAN_BATCH, target - multi - long_)
    texts = [("계획 — 첫 묶음", f"plan_prompt(target_questions={first}, dedicated=True)",
              qg.plan_prompt(catalog, "", target_questions=first, dedicated=True)),
             ("10턴 대화(J6) 전용 — 한 번에 4개", f"multi_turn_prompt({qg.MULTI_TURN_BATCH}, 기존 주제 없음)",
              qg.multi_turn_prompt(qg.MULTI_TURN_BATCH, [], catalog)),
             ("긴 문서(J7) 전용", f"long_context_prompt({long_})", qg.long_context_prompt(long_, catalog))]
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
                        f"프롬프트 {len(prompts)}종 · 계획 프롬프트 {prompts[0]['chars']:,}자."),
        data_source={"label": "코드에서 다시 만든 프롬프트 (모델 호출 없음)", "kind": "code", "path": paths[0], "visibility": "public",
                     "revision": rev},
        created_at=committed_at(rev), code_revision=rev, level="auto_tested",
        level_note="프롬프트·생성 흐름은 tests/test_question_generation.py 가 모의 GPT 로 확인. 이 판(v1.4)으로 실제 생성한 결과는 저장소에 없음 — 실측 미완료.",
        limitations=["질문 세트 300 은 이전 판 question-gen-v1.2 로 만든 결과다. v1.4 프롬프트의 실제 생성 품질은 아직 확인하지 않았다.",
                     "생성 품질(중복·모호함·평가 가능성)을 사람이 검수한 기록이 없다.",
                     "보충·추가 프롬프트는 앞 단계 결과(빠진 항목·적게 덮인 항목)로 달라져 여기에는 담지 않았다."],
        references=[repo_ref("생성기 코드", paths[0], revision(paths[0])), repo_ref("생성기 테스트", "tests/test_question_generation.py")],
        content={"version": qg.QUESTION_GEN_VERSION, "model_setting": "연결된 GPT (.env OPENAI_MODEL) — 질문 세트 300 은 gpt-6-luna",
                 "prompts": prompts,
                 "stages": [f"긴 문서 질문(J7): 272K 토큰 이상 문서 1개를 만들고 그 문서를 함께 쓰는 질문 {long_}개",
                            f"10턴 대화(J6): 한 번에 {qg.MULTI_TURN_BATCH}개씩 {multi}개, J6 항목을 대화마다 고르게",
                            f"계획: 일반 질문을 {qg.PLAN_BATCH}개씩 — 점검 목록(CODE 세부 조건·인식 규칙, 의미 점검 항목)과 변별력 규칙(trap·난도)",
                            "만들기: 첨부(PDF·DOCX·TXT·이미지)·붙여 넣는 자료를 실제 파일·글로",
                            "점검: 평가와 같은 코드 규칙(tools/check_question_set.py)으로 질문마다 적용 기준 계산",
                            f"보충: 한 번도 적용되지 않은 항목·첨부 종류만 최대 {qg.SUPPLEMENT_ROUNDS}회",
                            f"추가: 목표 질문 수(또는 항목별 목표 적용 수)까지 {qg.PLAN_BATCH}개씩, 적게 덮인 항목·변별력 보강 우선"],
                 "settings": [{"name": n, "value": v, "meaning": m} for n, v, m in settings],
                 "output_fields": [{"field": f, "meaning": m} for f, m in (
                     ("id", "질문(대화) 번호"), ("type · topic", "질문 유형·주제"),
                     ("target_items", "노린 평가 항목 id (CODE 세부 조건 '검사:종류' 또는 의미 점검 항목)"),
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
        limitations=["현재 생성기(v1.4)가 아니라 v1.2 결과다 — 세부 조건 단위 채우기·10턴 대화·긴 문서 질문 수 지정 전.",
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
                 "samples": _samples(groups)})


def w01_intent(qs: QuestionSet) -> Dict[str, Any]:
    """생성기가 질문마다 노린 항목(target_items)이 평가와 같은 코드 규칙에서 실제로 적용됐는지."""
    from tools.check_question_set import ANSWER_DEPENDENT, ANSWER_DEPENDENT_ITEMS
    cov = qs.report["coverage"]
    known = set(cov["checks"]) | set(cov["kinds"]) | set(cov["items"]) | {i.split(".")[0] for i in cov["items"]}
    answer_dependent = set(ANSWER_DEPENDENT) | set(ANSWER_DEPENDENT_ITEMS)
    applied: Dict[int, set] = collections.defaultdict(set)
    for row in qs.report["cases"]:
        gid = (((qs.items[row["index"] - 1].get("import_source") or {}).get("group") or {}).get("id"))
        found = set(row.get("checks") or []) | set(row.get("kinds") or []) | set(row.get("items") or [])
        found |= {i.split(".")[0] for i in row.get("items") or []} | {k.split(":")[0] for k in row.get("kinds") or []}
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
                 "charts": [{"kind": "bar", "title": "기준별 '노린 항목이 실제로 적용된 비율'", "unit": "%", "max": 100,
                             "categories": [r["family"] for r in rows],
                             "series": [{"name": "적용률", "values": [round(r["rate"] * 100, 1) if r["rate"] is not None else None for r in rows]}]}],
                 "table": {"columns": [{"key": "family", "label": "기준"}, {"key": "intended", "label": "노린 건수", "numeric": True},
                                       {"key": "applied", "label": "실제 적용", "numeric": True}, {"key": "missed", "label": "적용 안 됨", "numeric": True},
                                       {"key": "answer_dependent", "label": "답변 필요", "numeric": True}, {"key": "rate", "label": "적용률", "percent": True}],
                           "rows": rows, "download": "w01-intent-by-criterion.csv"},
                 "notes": ["가장 자주 빗나간 항목: " + ", ".join(f"{t} {n}건" for t, n in misses.most_common(8))]})


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


# --- W02 기준별 적용 -----------------------------------------------------------------------------

def _turn_counts(report: Dict[str, Any]) -> collections.Counter:
    counts: collections.Counter = collections.Counter()
    for row in report["cases"]:
        for key in set(row.get("checks") or []) | set(row.get("kinds") or []) | set(row.get("items") or []):
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

    def add(group: str, parent: str, target: str, name: str, units: Optional[int]) -> None:
        rows.append({"group": group, "parent": parent, "id": target, "name": name, "units": units,
                     "turns": turns.get(target, 0) if units is not None else None, "judged": 0,
                     "short": max(0, TARGET_SHORT - units) if units is not None else None})
    for cond in targets["code"]["conditions"]:
        add("CODE 질문 조건", "CODE " + cond["check"].split("-")[0], cond["id"], f"{names.get(cond['check'], cond['check'])} · {cond['name']}",
            cov["kinds"].get(cond["id"], 0))
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
        description=("CODE 질문 조건·늘 적용 검사·답변 구조 검사와 의미 점검 항목 각각에, 질문 세트 300 의 어떤 질문이 몇 번 적용되는지 "
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

def _collection_stats(qs: QuestionSet, system: str) -> Dict[str, Any]:
    entries = [i["answers"][system] for i in qs.answers["items"] if system in i.get("answers", {})]
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
    return {"system": system, "model": qs.meta["models"].get(system), "method": qs.meta["collection"].get(system),
            "turns": len(entries), "status": dict(status), "success": status.get("SUCCESS", 0),
            "success_rate": _pct(status.get("SUCCESS", 0), len(entries)), "retried": sum(1 for e in entries if e.get("retried")),
            "latency_median_ms": round(statistics.median(latency)) if latency else None,
            "answer_chars_median": round(statistics.median(lengths)) if lengths else None, "empty_answers": sum(1 for n in lengths if n == 0),
            "first": kst(stamps[0]) if stamps else None, "last": kst(stamps[-1]) if stamps else None,
            "errors": errors.most_common(5), "tokens": dict(tokens)}


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


def w04_aiona(qs: QuestionSet) -> Dict[str, Any]:
    s = _collection_stats(qs, "aiona")
    groups = {f"Q{g['id']}": g for g in qs.groups}
    failed = [i for i in qs.answers["items"] if (i["answers"].get("aiona") or {}).get("status") == "ERROR"]
    empty_asks = sum(1 for i in failed if "2-1.explicit_empty" in (groups.get(i["case_id"].split("_")[0]) or {}).get("target_items", []))
    top = s["errors"][0] if s["errors"] else None
    failures = s["turns"] - s["success"]
    return artifact(
        "W04-AIONA-RUN", "W04", "AIONA 웹 자동화 수집 — 질문 세트 300 실제 실행", "run",
        description="winz.aiona.kr 웹 화면을 브라우저 자동화로 열어 질문 450턴을 보내고 답변 원문을 받은 실제 수집 결과입니다(팀원 실행, 실패한 턴만 다시 받음).",
        result_summary=(f"턴 {s['turns']}개 중 성공 {s['success']}({s['success_rate']:.1%}) · 오류 {s['status'].get('ERROR', 0)} · 미완료 {s['status'].get('INCOMPLETE', 0)}. "
                        + (f"오류 {len(failed)}턴 중 {empty_asks}턴은 '아무것도 출력하지 마' 같은 빈 출력 요구 질문. " if failed else "")
                        + (f"가장 많은 실패 사유: '{top[0]}' {top[1]}건. " if top else "")
                        + f"다시 받은 턴 {s['retried']}개, 성공 답변 응답 시간 중앙값 {s['latency_median_ms']:,}ms."),
        data_source=qs.source(), created_at=qs.exported, code_revision=revision("webapp/aiona_web.py"), level="real_run",
        level_note="실제 AIONA 웹 서비스에서 받은 답변. 이 답변들은 아직 평가하지 않았음.",
        limitations=["AIONA 웹은 기본 '자동 추천' 모델이라 어떤 모델이 답했는지 기록되지 않는다.",
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
                             {"label": "다시 받은 턴", "value": s["retried"]}, {"label": "답변 길이 중앙값", "value": f"{s['answer_chars_median']}자"}],
                 "charts": [_status_chart([s])],
                 "errors": [{"reason": e, "count": n} for e, n in s["errors"]],
                 "log": _log(qs, "aiona")})


def w05_api(qs: QuestionSet) -> Dict[str, Any]:
    stats = [_collection_stats(qs, system) for system in ("gpt", "gemini")]
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
    stats = [_collection_stats(qs, system) for system in ("aiona", "gpt", "gemini")]
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
              ("code_conditions", "CODE 질문 조건"), ("semantic_criteria", "의미 기준"), ("semantic_subs", "의미 세부 기준"),
              ("semantic_items", "의미 점검 항목")]
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


# --- 모으기 -----------------------------------------------------------------------------------

PRIVATE_IDS = ("W02-SAVED-JUDGMENTS", "W03-STABILITY", "W05-SAVED-RUN", "Y03-J6-POLICY")


def build(existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    existing_by_id = {a["artifact_id"]: a for a in (existing or {}).get("artifacts") or []}
    run_path = ROOT / SAVED_RUN
    run = json.loads(run_path.read_text(encoding="utf-8")) if run_path.is_file() else None
    qs = QuestionSet()
    made = [w01_prompt(), w01_set(qs), w01_intent(qs), w01_code(),
            w02_matrix(qs), w02_criteria(qs), w02_saved(run),
            w03_coverage(qs), w03_stability(run), w03_conclusion(qs, run, existing_by_id.get("W03-CONCLUSION")),
            w04_aiona(qs), w05_api(qs), w05_saved(run), w07_code(), w08_paths(qs), w09_contract(qs),
            y01_targets(), y03_policy(run)]
    kept: List[str] = []
    out = [value for value in made if value is not None]
    for artifact_id in PRIVATE_IDS:   # 비공개 원본이 없는 PC — 저장된 공개 요약을 그대로 둔다
        if not any(a["artifact_id"] == artifact_id for a in out) and artifact_id in existing_by_id:
            out.append(existing_by_id[artifact_id])
            kept.append(artifact_id)
    order = {a["artifact_id"]: i for i, a in enumerate(a for a in made if a is not None)}
    out.sort(key=lambda a: (a["requirement_id"], order.get(a["artifact_id"], 999), a["artifact_id"]))
    result = {"version": ARTIFACTS_VERSION, "generated_by": "tools/progress_artifacts.py",
            "levels": {"none": "미확인", "implemented": "구현 완료", "auto_tested": "자동 테스트 확인", "measured": "실제 데이터 계산",
                       "real_run": "실제 모델 실행", "accuracy_verified": "판정 정확도 검증"},
            "private_sources": {SAVED_RUN: "present" if run is not None else "missing"},
            "kept_without_source": kept, "artifacts": out}
    return json.loads(json.dumps(result, ensure_ascii=False))   # 저장 파일과 같은 모양(튜플 → 목록)으로 비교한다


def _diff(saved: Dict[str, Any], fresh: Dict[str, Any]) -> List[str]:
    a = {x["artifact_id"]: x for x in saved.get("artifacts") or []}
    b = {x["artifact_id"]: x for x in fresh.get("artifacts") or []}
    lines = [f"없어짐: {k}" for k in a.keys() - b.keys()] + [f"새로 생김: {k}" for k in b.keys() - a.keys()]
    for key in sorted(a.keys() & b.keys()):
        for field in sorted(set(a[key]) | set(b[key])):
            if a[key].get(field) != b[key].get(field):
                lines.append(f"{key}.{field} 가 다름")
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
