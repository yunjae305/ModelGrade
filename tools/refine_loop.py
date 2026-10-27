# -*- coding: utf-8 -*-
"""기준 다듬기 루프 (refine-loop-v1) — 판정 결과 · 사람 표시를 재료로 판정 프롬프트 · 1차 항목 정의 · 확인 목록의 고칠 안을 내고, 같은 칸을
고치기 전 · 후로 다시 판정해 비교한 뒤, 근거가 있을 때만 채택하고 교훈을 남긴다 (기준 v2 10절의 ①~⑥을 도구로).

흐름은 Harness²(Google Research, 2026, Apache-2.0)의 제안(Propose) → 시험(Probe) → 합성 · 채택(Compose) → 교훈(Reflect)을 따온 것이고
코드는 가져오지 않았다. 다른 점: 우리는 사람 표시(정답)가 있으면 그것을 1순위 증거로 쓰고, 없으면 '두 번 판정의 뒤집힘'과 '1차 ↔ 2차 갈림'을
증거로 쓴다(정답 없이 고치면 심판이 틀린 쪽으로 굳을 수 있어 그때는 '사람 확인 필요'로 표시만 한다).

    python -X utf8 tools/refine_loop.py collect --run 50ca3ccccace --judge v4-b10 --item J1-4.b [--fail 12 --pass 12 --types 싱글]
    python -X utf8 tools/refine_loop.py propose --cases <폴더> [--channel claude|codex --model … --effort …]
    python -X utf8 tools/refine_loop.py probe   --cases <폴더> [--proposal all --rounds 2]
    python -X utf8 tools/refine_loop.py score   --cases <폴더> [--marks 표시결과.json]
    python -X utf8 tools/refine_loop.py reflect --cases <폴더>
    python -X utf8 tools/refine_loop.py apply   --cases <폴더> --proposal P1
    python -X utf8 tools/refine_loop.py run     --run … --judge … --item … [--apply]   # collect → propose → probe → score → reflect
    python -X utf8 tools/refine_loop.py auto    --run … --judge v4 [--items 2]          # 평가 끝에 자동(start 가 부름) — 기준 고르기 → run --apply

→ data/refine/<실행>/<항목>/<시각>/ — cases.json · 표시용_표.html · proposals.json · probes/ · score.md · lesson.json
  채택된 안은 apply 가 실제 파일(판정 프롬프트 · 1차 항목 정의 · 턴별 확인 목록)에 넣고, 교훈은 참고자료/기준/채팅품질 평가기준_v2.md 12절에.

auto (사용자 10-27 '평가가 완료되면 하네스2도 자동연계' · '알아서 잘못된 부분, 확장하면 좋을 부분들을 찾아서 개선 및 수정'):
  판정 폴더의 사람 판정(human_decisions.json)을 정답 표시로, 사람 표시 · 보류 · 심판 갈림 · FAIL 이 많은 LLM 기준부터 골라 run --apply 를 차례로 돌린다.
  채택된 안은 실제 파일에 넣고 변경내역(참고자료/변경내역/<날짜>-변경내역.md)에 적는다. 고치는 대상: 판정 프롬프트(태도 3종 포함) · 평가 기준
  (기준 문서 행 + 심판이 읽는 정의를 함께) · 질문 리스트(질문 글 · 턴별 정답 메모) · 질문생성 프롬프트. 질문 글을 고치면 question_revisions.json 에
  남아 다음 수집 때 그 질문만 다시 받고 다시 판정한다. CODE 검사 코드를 고치자는 안만 참고자료/문서/하네스2_제안목록.md 에 쌓는다.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import judge_v4 as jv  # noqa: E402

FOLLOWUPS = ROOT / "참고자료" / "문서" / "하네스2_제안목록.md"   # 자동으로 넣지 못한 개선 · 확장 제안 (CODE 검사 코드)
CHANGELOG_DIR = ROOT / "참고자료" / "변경내역"
MIN_CELLS = 3   # auto 가 고르는 기준 — FAIL · 보류 칸과 통과 칸이 각각 이만큼은 있어야 전 · 후를 견줄 수 있다

VERSION = "refine-loop-v1"
RUNS = ROOT / "data" / "judge_runs"
REFINE = ROOT / "data" / "refine"
CRITERIA_DOC = ROOT / "참고자료" / "기준" / "채팅품질 평가기준_v2.md"
LESSON_HEADING = "## 12. 기준 다듬기 루프의 교훈"
LESSON_CHARS = 220   # 12절 표 칸 하나의 글자 상한 — 전문은 data/refine/…/lesson.json (사용자: 문서는 짧고 요점만)
KO = {"PASS": "통과", "FAIL": "FAIL", "UNVERIFIED": "보류", "NOT_APPLICABLE": "해당 없음", None: "-"}
TARGETS = ("판정 프롬프트", "1차 항목 정의", "판정 태도 3종", "확인 목록", "기준 문서", "질문", "질문생성 프롬프트", "CODE")
PROBE_TARGETS = ("판정 프롬프트", "1차 항목 정의", "판정 태도 3종", "확인 목록")   # 같은 칸을 고치기 전 · 후로 다시 판정해 잰다
TEXT_TARGETS = ("질문", "질문생성 프롬프트", "기준 문서")   # 다시 판정으로 잴 수 없음 — 검사 통과 + 이 기준 칸의 흔들림 · 사람 판정 불일치가 근거 (10-27)
AUTO_TARGETS = PROBE_TARGETS + TEXT_TARGETS   # apply 가 실제 파일에 넣는 대상 — CODE(검사 코드)만 제안목록에
QGEN_PROMPT = ROOT / "참고자료" / "프롬프트" / "질문생성_프롬프트.md"
MAX_Q_EDITS = 3   # 한 안에서 고칠 질문 수 — 고친 질문은 네 모델 답을 다시 받아야 해서
SIDE_EFFECT_MAX = 0.05   # 같은 질문의 다른 항목이 이 비율 넘게 바뀌면 채택하지 않는다 (v2 10절 ⑤ '다른 기준의 결과가 그대로')


def say(text: str) -> None:
    print(text, flush=True)


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def read_jsonl_last(path: Path) -> Dict[Tuple[str, int], Dict[str, Any]]:
    out: Dict[Tuple[str, int], Dict[str, Any]] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            x = json.loads(line)
            out[(x["provider"], int(x["question"]))] = x
    return out


def cell_key(c: Dict[str, Any]) -> Tuple[str, int, int, str]:
    return (c["provider"], int(c["question"]), int(c["turn"]), c["item"])


# ------------------------------------------------------------------ ① 모으기 ------------------------------------------------------------------
def load_cells(run_id: str, judge_out: str, set_dir: Path, item: Optional[str] = None) -> List[Dict[str, Any]]:
    """판정 폴더의 항목 칸 전부(PASS · FAIL · 보류) — 답변 원문 · 요청 문장 · 심판 사유 · 1차 · 2차 · 확인 목록 사실 메모와 함께."""
    from tools import report_html as R
    run = json.loads((ROOT / "data" / "comparison_runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    answers = {(p, r["case_id"]): r for p, rows in (run.get("datasets") or {}).items() for r in rows if isinstance(r, dict)}
    names = R.criteria_table(set_dir)
    qs = {int(q["id"]): q for q in json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))}
    plan_path = set_dir / "multiturn_v3" / "turn_checklists.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")).get("questions", {}) if plan_path.exists() else {}
    cells: List[Dict[str, Any]] = []
    for (p, q), v in sorted(read_jsonl_last(RUNS / run_id / judge_out / "results.jsonl").items()):
        if v.get("status") != "judged":
            continue
        multi = int(v.get("turns") or 1) > 1
        for tn, t in (v.get("turn_results") or {}).items():
            case_id = f"Q{q}_T{tn}" if multi else f"Q{q}"
            row = answers.get((p, case_id)) or answers.get((p, f"Q{q}_T{tn}")) or {}
            facts = ((plan.get(str(q)) or {}).get("turns") or [{}] * 20)[int(tn) - 1] if multi else {}
            for iid, it in (t.get("items") or {}).items():
                if not isinstance(it, dict) or (item and iid != item):
                    continue
                if it.get("decision") not in ("PASS", "FAIL", "UNVERIFIED"):
                    continue
                cells.append({"type": "멀티턴" if multi else "싱글", "question": q, "provider": p, "turn": int(tn), "item": iid,
                              "name": (names.get(iid) or {}).get("name") or "", "decision": it["decision"], "how": it.get("how") or "",
                              "first": it.get("jev"), "codex": it.get("codex"), "flagged": it.get("flagged") or "",
                              "reason": it.get("reason") or "", "answer_quote": it.get("answer_quote") or "", "question_quote": it.get("question_quote") or "",
                              "request": str(row.get("question") or "").split("[자료 끝]")[-1].strip(), "answer": row.get("answer") or "",
                              "question_text": str(row.get("question") or ""),   # 표시용 표가 질문 원문을 보인다 (10-26)
                              "facts": facts.get("checks") or [], "decided": facts.get("decided") or [], "situation": facts.get("situation") or "",
                              "flow": facts.get("flow") or "", "difficulty": (qs.get(q) or {}).get("difficulty")})
    return cells


def select_cases(cells: Sequence[Dict[str, Any]], n_fail: int, n_pass: int, seed: int = 1, types: Optional[Iterable[str]] = None,
                 max_lines: Optional[int] = None, prefer: Optional[set] = None) -> List[Dict[str, Any]]:
    """균형 표본 — FAIL · 보류 n_fail 개와 통과 n_pass 개(v2 10절 ①: 같은 수의 통과 칸). 모델 · 질문이 고루 섞이게 (같은 (모델, 질문)은 한 칸씩 먼저).
    max_lines 를 주면 FAIL · 보류 칸이 많은 (모델, 질문) 순으로 그 수만큼만 — 시험(재판정)은 (모델, 질문) 단위로 비용이 들므로(멀티턴은 대화 전체) 상한."""
    rnd = random.Random(seed)
    pool = [c for c in cells if not types or c["type"] in set(types)]
    if max_lines:
        bad_by_line: Counter = Counter((c["provider"], c["question"]) for c in pool if c["decision"] in ("FAIL", "UNVERIFIED"))
        all_lines = sorted({(c["provider"], c["question"]) for c in pool}, key=lambda k: (-bad_by_line[k], k[1], k[0]))
        keep = set(all_lines[:max_lines]) | ({k[:2] for k in prefer} if prefer else set())   # 사람 표시가 있는 (모델, 질문)은 남긴다
        pool = [c for c in pool if (c["provider"], c["question"]) in keep]

    def spread(cs: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
        cs = list(cs)
        rnd.shuffle(cs)
        if prefer:   # 사람 표시(정답)가 있는 칸을 먼저 — 채택은 정답률로만 한다 (auto, 10-27)
            cs.sort(key=lambda c: cell_key(c) not in prefer)
        got: List[Dict[str, Any]] = []
        seen: Counter = Counter()
        for rounds in range(3):   # 1회차: (모델, 질문) 하나씩 → 2회차: 모델마다 고루 → 3회차: 나머지
            for c in cs:
                if len(got) >= n:
                    break
                if c in got:
                    continue
                key = (c["provider"], c["question"]) if rounds == 0 else c["provider"]
                cap = 1 if rounds == 0 else (n // 2 if rounds == 1 else n)
                if seen[("r", rounds, key)] >= cap:
                    continue
                seen[("r", rounds, key)] += 1
                got.append(c)
        return got[:n]
    bad = [c for c in pool if c["decision"] in ("FAIL", "UNVERIFIED")]
    good = [c for c in pool if c["decision"] == "PASS"]
    out = spread(bad, n_fail) + spread(good, n_pass)
    for i, c in enumerate(out, 1):
        c["no"] = i
    return out


def load_marks(path: Path) -> Dict[Tuple[str, int, int, str], Dict[str, Any]]:
    """사람 표시 — 표시용 표의 내보내기(표시결과_*.json: rows[mark PASS · FAIL · 에러 케이스, 옛 표 맞음 · 틀림 · 애매]) 또는 human_decisions.json(decisions[decision])."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    out: Dict[Tuple[str, int, int, str], Dict[str, Any]] = {}
    for r in doc.get("rows") or []:
        out[(r["provider"], int(r["question"]), int(r.get("turn") or 1), r["item"])] = {"mark": r.get("mark"), "cause": r.get("cause"), "decision": r.get("decision")}
    for d in doc.get("decisions") or []:   # human_decisions.json — 사람이 고른 값은 new (옛 내보내기는 decision)
        v = d.get("new") or d.get("decision")
        out[(d["provider"], int(d["question"]), int(d.get("turn") or 1), d["item"])] = {"decision": v, "truth": v}
    return out


def truth_of(cell: Dict[str, Any], mark: Optional[Dict[str, Any]]) -> Optional[str]:
    """사람 표시 → 그 칸의 정답(PASS · FAIL). PASS · FAIL 표시는 그대로(사람이 직접 고른 결과), 옛 표의 '맞음'은 심판 판정 그대로,
    '틀림'은 반대, 에러 케이스 · '애매' · 없음은 None(정답률에서 뺌)."""
    if not mark:
        return None
    if mark.get("truth") in ("PASS", "FAIL"):
        return mark["truth"]
    m, d = mark.get("mark"), mark.get("decision") or cell.get("decision")
    if m in ("PASS", "FAIL"):
        return m
    if m == "맞음" and d in ("PASS", "FAIL"):
        return d
    if m == "틀림" and d in ("PASS", "FAIL"):
        return "FAIL" if d == "PASS" else "PASS"
    return None


def cmd_collect(a: argparse.Namespace) -> Path:
    set_dir = Path(a.set)
    cells = load_cells(a.run, a.judge, set_dir, a.item)
    if not cells:
        raise SystemExit(f"{a.judge} 에 {a.item} 칸이 없습니다")
    types = [t.strip() for t in (a.types or "").split(",") if t.strip()] or None
    marks = load_marks(Path(a.marks)) if a.marks else {}
    cases = select_cases(cells, a.fail, a.n_pass, a.seed, types, a.max_lines, set(marks) or None)
    for c in cases:
        c["truth"] = truth_of(c, marks.get(cell_key(c)))
    d = REFINE / a.run / a.item / now_stamp()
    d.mkdir(parents=True, exist_ok=True)
    meta = {"version": VERSION, "run": a.run, "judge": a.judge, "item": a.item, "set": str(set_dir), "seed": a.seed, "types": types,
            "pool": Counter(c["decision"] for c in cells), "created": datetime.now(timezone.utc).isoformat()}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "cases.md").write_text(cases_md(cases, meta), encoding="utf-8")
    try:   # 사람 표시용 표 — 표시용 표 도구의 화면 그대로 (내보내기 JSON 을 --marks 로 받는다)
        from tools import build_calibration_sheet as cs
        cs.KO.setdefault("UNVERIFIED", "보류")   # 표시용 표는 통과 · FAIL 만 알던 것 — 보류 칸도 표시 대상
        sheet = f"refine-{a.item}-{d.name}"
        (d / "표시용_표.html").write_text(cs.render([{**c, "facts": c.get("facts") or []} for c in cases], sheet, a.run, a.judge), encoding="utf-8")
    except Exception as exc:   # 표가 안 만들어져도 루프는 돈다
        say(f"⚠ 표시용 표 생략: {type(exc).__name__}: {str(exc)[:120]}")
    say(f"모음: {d} — {a.item} 칸 {len(cases)}개(FAIL · 보류 {sum(1 for c in cases if c['decision'] != 'PASS')} · 통과 {sum(1 for c in cases if c['decision'] == 'PASS')}),"
        f" 전체 {dict(meta['pool'])}, 사람 표시 {sum(1 for c in cases if c.get('truth'))}칸")
    return d


def cases_md(cases: Sequence[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    L = [f"# 모은 칸 — {meta['item']} · 실행 {meta['run']} · 판정 {meta['judge']}", "",
         "| # | 질문 | 모델 | 턴 | 판정 | 1차 | 2차 | 걸린 이유 | 사유 |", "|---|---|---|---|---|---|---|---|---|"]
    for c in cases:
        L.append(f"| {c['no']} | Q{c['question']} | {c['provider']} | {c['turn']} | {KO.get(c['decision'])} | {KO.get(c.get('first'))} | {KO.get(c.get('codex'))} | "
                 f"{str(c.get('flagged') or '')[:30]} | {str(c.get('reason') or '').replace('|', '/')[:140]} |")
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ 기준 정의 모으기 ------------------------------------------------------------------
def prompt_block(item: str, prompt_path: Path = jv.PROMPT_V4) -> str:
    """판정 프롬프트의 그 항목 블록(- **J1-4.b** … 와 아래 · 줄들) 원문."""
    lines = prompt_path.read_text(encoding="utf-8").splitlines()
    out: List[str] = []
    on = False
    for line in lines:
        if re.match(rf"- \*\*{re.escape(item)}\*\* · ", line):
            on = True
            out.append(line)
            continue
        if on:
            if line.startswith("  · "):
                out.append(line)
            else:
                break
    return "\n".join(out)


def doc_row(item: str, doc_path: Path = CRITERIA_DOC) -> str:
    for line in doc_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"| **{item}**"):
            return line
    return ""


def first_judge_definition(item: str, jev_path: Path = jv.JEV_V4) -> str:
    fixed = json.loads(jev_path.read_text(encoding="utf-8"))
    return str((jv.all_item_questions(fixed).get(item) or {}).get("instructions") or "")


def principles(prompt_path: Path = jv.PROMPT_V4) -> str:
    """판정 프롬프트의 '지켜야 할 원칙' · '판정 방법' 절 — 제안자가 흐름 규칙(불변)을 고칠 때 보는 부분."""
    text = prompt_path.read_text(encoding="utf-8")
    out = []
    for head in ("# 지켜야 할 원칙", "# 판정 방법"):
        i = text.find(head)
        if i == -1:
            continue
        j = text.find("\n# ", i + len(head))
        out.append(text[i:j if j != -1 else None].strip())
    return "\n\n".join(out)


# ------------------------------------------------------------------ ③ 고칠 안 (제안) ------------------------------------------------------------------
PROPOSE_GUIDE = """당신은 한국어 AI 답변 품질 평가의 **기준 다듬기 제안자**입니다. 심판(모델)은 그대로 두고, 판정을 흔들리게 한 글 — 판정 프롬프트 · 1차 심판 항목 정의 ·
멀티턴 턴별 확인 목록(정답 메모) · 평가 기준 원본 · 질문 글 · 질문생성 프롬프트 — 을 고칠 안을 냅니다. 아래 [재료]에 한 기준의 정의 세 가지(기준 문서 행 · 판정 프롬프트 블록 · 1차 항목 정의), 판정 원칙, 그리고
그 기준의 판정 칸(FAIL · 보류와 같은 수의 통과 칸: 요청 · 답 · 심판 사유 · 1차/2차 · 있으면 사람 표시)이 있습니다.

규칙 (Harness² 의 '증거가 뒷받침하는 것만 고친다 · 고칠 것이 없으면 KEEP_BASE 도 답이다' 를 따름):
1. 안은 많아야 3개. 안마다 **대상 하나**(판정 프롬프트 / 1차 항목 정의 / 판정 태도 3종 / 확인 목록 / 기준 문서 / 질문 / 질문생성 프롬프트 / CODE)와
   범위(불변 = 모든 기준에 통하는 판정 방법 · 절차, 이 기준 = 이 항목의 정의 · 경계)를 정합니다.
   - 질문: 칸이 흔들린 원인이 **질문 글**(모호함 · 서로 어긋나는 조건 · 붙인 자료와 정답 메모의 어긋남 · 빠진 정보)일 때만. {"question": 번호, "turn": 턴(싱글은 1),
     "old": "…", "new": "…"} 로 그 턴의 질문 글을 고칩니다(한 안에 질문 3개까지). 질문이 노리는 기준 · 함정 · 길이는 그대로 두고 잘못만 고칩니다 — [재료] 칸의 질문_원문에서
     old 를 고르세요. 고친 질문은 다음 수집 때 네 모델 답을 다시 받으므로, 판정 프롬프트로 풀 수 있는 문제면 질문을 고치지 않습니다.
   - 질문생성 프롬프트: 위 같은 질문 잘못이 다음에 만들 질문에서 다시 생기지 않게 [재료]의 질문생성_프롬프트에서 한 줄을 old → new 로 고치거나(덧붙이기는 old = 그 줄의 끝 구절).
   - 기준 문서: 평가 기준 원본(기준_문서_행)의 정의 · 예외 · 경계를 old → new 로. 기준 ID · 개수(72)는 바꾸지 않습니다.
   - doc_sync: 판정 프롬프트 · 1차 항목 정의로 **기준의 뜻**(정의 · 예외 · 경계)을 바꾸면, 기준 원본이 심판 글과 어긋나지 않게 기준_문서_행을 같은 뜻으로 고친
     [{"old": "…", "new": "…"}] 를 함께 적습니다. 표현만 고치거나 대상이 질문 · 질문생성 프롬프트 · 기준 문서 · CODE 면 빈 배열.
2. edits 는 **지금 글에 그대로 있는 old 를 new 로** 바꾸는 꼴로 — old 는 그 파일에서 한 번만 나오는 구절(한 줄 이내)로 고릅니다. 덧붙이기만 할 때는 old 를 그 줄의
   끝 구절로 잡고 new 에 그 구절 + 덧붙일 글을 씁니다. 1차 항목 정의는 {"item": "J1-4.b", "append": "…"} 로 지시문 끝에 덧붙일 수도 있습니다. 1차 항목 정의 파일에는 **J1~J4 항목만** 있습니다 — J5~J8 항목은 판정 프롬프트나 확인 목록을 고칩니다.
   확인 목록은 {"question": 2, "turn": 2, "add_decided": "…"}(심판이 따르는 정한 판정) 또는 {"question", "turn", "add_check"}(자료의 사실만, 판정 지시 금지).
   판정 태도 3종(1차 심판이 같은 질문을 비판적 · 친화적 · 유도적 세 태도로 한 번씩 보고 가중 다수결 — [재료]의 판정_태도_3종)은 {"item": "critical" · "friendly" · "guided"
   (태도 id, 비우면 파일 전체), "old": "…", "new": "…"} 로 그 태도의 글 · 가중치(예: old '"FAIL": 1.2' → new '"FAIL": 1.1')나 min_choice_prob 을 바꿉니다 — 모든 항목에 걸리므로 범위는 '불변'.
3. 증거가 말하는 것만 적습니다 — 어느 칸(#번호)이 왜 잘못 판정됐는지 why 에 쓰고, expected 에 그 안으로 바뀔 칸을 "#3 FAIL→PASS" 꼴로 적습니다.
   추측한 일반론("더 꼼꼼히 보라")은 행동을 바꾸지 못하니 쓰지 않습니다. 기준 카드는 한 줄 길이를 지킵니다(길어지면 다른 안으로 나눕니다).
4. 고칠 근거가 없으면 keep_base 를 true 로 두고 proposals 를 비웁니다 — 그것도 답입니다. 사람 표시가 없는 칸의 판정은 정답이 아니라 심판의 의견이므로,
   사람 표시가 없을 때는 심판 사유가 **정의와 어긋난** 칸(정의에 없는 이유로 FAIL · 정의의 예외를 무시)만 근거로 삼습니다.
5. 출력은 JSON 하나뿐입니다(설명 글 없음)."""


def proposal_schema() -> Dict[str, Any]:
    edit = {"type": "object", "additionalProperties": False,
            "required": ["item", "question", "turn", "old", "new", "append", "add_decided", "add_check"],
            "properties": {"item": {"type": ["string", "null"]}, "question": {"type": ["integer", "null"]}, "turn": {"type": ["integer", "null"]},
                           "old": {"type": ["string", "null"]}, "new": {"type": ["string", "null"]}, "append": {"type": ["string", "null"]},
                           "add_decided": {"type": ["string", "null"]}, "add_check": {"type": ["string", "null"]}}}
    sync = {"type": "object", "additionalProperties": False, "required": ["old", "new"],
            "properties": {"old": {"type": "string"}, "new": {"type": "string"}}}
    prop = {"type": "object", "additionalProperties": False, "required": ["id", "scope", "target", "edits", "doc_sync", "why", "expected", "risk"],
            "properties": {"id": {"type": "string"}, "scope": {"type": "string", "enum": ["불변", "이 기준"]},
                           "target": {"type": "string", "enum": list(TARGETS)}, "edits": {"type": "array", "items": edit},
                           "doc_sync": {"type": "array", "items": sync},
                           "why": {"type": "string"}, "expected": {"type": "array", "items": {"type": "string"}}, "risk": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False, "required": ["keep_base", "notes", "proposals"],
            "properties": {"keep_base": {"type": "boolean"}, "notes": {"type": "string"}, "proposals": {"type": "array", "items": prop}}}


def lesson_schema() -> Dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["cue", "lesson", "evidence", "confidence"],
            "properties": {"cue": {"type": "string"}, "lesson": {"type": "string"}, "evidence": {"type": "string"},
                           "confidence": {"type": "string", "enum": ["높음", "중간", "낮음"]}}}


def make_channel(name: str, model: Optional[str], effort: Optional[str], log_dir: Path):
    """제안 · 교훈을 쓸 모델 통로 — claude(기본, Claude CLI) 또는 codex(Codex CLI). judge_v4 의 심판 모양(judge(prompt, input, search, tag, output_schema))."""
    log_dir.mkdir(parents=True, exist_ok=True)
    if name == "codex":
        from tools.judge_run import judges_from_env
        gpt, _jev, notes = judges_from_env()
        if gpt is None:
            raise SystemExit("Codex CLI 를 쓸 수 없습니다: " + "; ".join(notes))
        if model:
            gpt.model = model
        if effort:
            gpt.reasoning = effort
        gpt.io_dir = log_dir
        return gpt
    from tools.judge_claude_compare import ClaudeJudge, find_claude
    # 기본 Opus 5.5 · effort high(사용자 10-25 밤). Opus 5.5 는 Claude Code 2.1.280 이상이 필요 — 옛 CLI 면 호출이 400 으로 실패하고 ask() 가 Sonnet 5.5 로 한 번 대체한다
    judge = ClaudeJudge(find_claude(), model=model or "claude-opus-5-5", effort=effort or "high", calls_log=log_dir / "llm_calls.jsonl")
    judge.io_dir = log_dir
    return judge


FALLBACK_MODEL = "claude-sonnet-5-5"


def ask(ch: Any, guide: str, payload: Dict[str, Any], tag: str, schema: Dict[str, Any]) -> Tuple[str, Any, str]:
    """모델 통로 호출 — 지정 모델을 CLI 가 모르면(옛 Claude Code) Sonnet 5.5 로 한 번 대체하고 알린다."""
    status, body, error = ch.judge(guide, json.dumps(payload, ensure_ascii=False, indent=1), False, tag, output_schema=schema)
    if status != "OK" and "does not support this model" in str(error) and getattr(ch, "model", "") != FALLBACK_MODEL:
        say(f"⚠ {ch.model} 은 지금 Claude Code 가 지원하지 않음({str(error)[:90]}) → {FALLBACK_MODEL} 로 대체 (claude update 뒤 다시 Opus)")
        ch.model = FALLBACK_MODEL
        status, body, error = ch.judge(guide, json.dumps(payload, ensure_ascii=False, indent=1), False, tag, output_schema=schema)
    return status, body, error


def propose_input(cases: Sequence[Dict[str, Any]], item: str, set_dir: Path) -> Dict[str, Any]:
    return {"항목": item,
            "정의": {"기준_문서_행": doc_row(item), "판정_프롬프트_블록": prompt_block(item), "1차_항목_정의": first_judge_definition(item),
                   "판정_태도_3종": json.loads(jv.STANCES_PATH.read_text(encoding="utf-8")) if jv.STANCES_PATH.exists() else None},
            "판정_원칙": principles(),
            "칸": [{"#": c["no"], "질문": c["question"], "모델": c["provider"], "턴": c["turn"], "유형": c["type"], "판정": c["decision"],
                   "1차": c.get("first"), "2차": c.get("codex"), "걸린_이유": c.get("flagged"), "사유": c.get("reason"),
                   "답의_인용": c.get("answer_quote"), "질문의_인용": c.get("question_quote"), "사람_표시": c.get("truth"),
                   "요청": c.get("request", "")[:1500], "질문_원문": str(c.get("question_text") or "")[-4000:], "답": c.get("answer", "")[:2500],
                   **({"확인_목록_사실": c["facts"][:8], "정한_판정": c.get("decided") or []} if c.get("facts") else {})} for c in cases],
            "질문생성_프롬프트": QGEN_PROMPT.read_text(encoding="utf-8")[:60000] if QGEN_PROMPT.exists() else ""}


def cmd_propose(a: argparse.Namespace) -> Dict[str, Any]:
    d = Path(a.cases)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    cases = json.loads((d / "cases.json").read_text(encoding="utf-8"))
    ch = make_channel(a.channel, a.model, a.effort, d / "llm")
    payload = propose_input(cases, meta["item"], Path(meta["set"]))
    (d / "propose_input.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    say(f"제안 요청 — {a.channel} {ch.model} · {ch.reasoning} · 칸 {len(cases)}개 …")
    t0 = time.monotonic()
    status, body, error = ask(ch, PROPOSE_GUIDE, payload, f"propose {meta['item']}", proposal_schema())
    secs = round(time.monotonic() - t0, 1)
    if status != "OK" or not isinstance(body, dict):
        raise SystemExit(f"제안 실패: {status} {error}")
    body = {**body, "channel": a.channel, "model": ch.model, "effort": ch.reasoning, "seconds": secs, "usage": getattr(ch, "last_usage", {})}
    for i, p in enumerate(body.get("proposals") or [], 1):
        p["id"] = p.get("id") or f"P{i}"
        if not re.fullmatch(r"P\d+", p["id"]):
            p["id"] = f"P{i}"
    (d / "proposals.json").write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    say(f"제안 {len(body.get('proposals') or [])}개{' · KEEP_BASE' if body.get('keep_base') else ''} ({secs}초) → {d / 'proposals.json'}")
    for p in body.get("proposals") or []:
        say(f"  {p['id']} [{p['scope']} · {p['target']}] {p['why'][:120]}")
    return body


# ------------------------------------------------------------------ 안 적용(임시 파일) ------------------------------------------------------------------
class EditError(ValueError):
    pass


def replace_once(text: str, old: str, new: str, where: str) -> str:
    n = text.count(old)
    if n != 1:
        raise EditError(f"{where}: old 가 {n}번 나옵니다(정확히 한 번이어야 함): {old[:60]!r}")
    return text.replace(old, new, 1)


def item_block(text: str, item: str) -> Optional[Tuple[int, int]]:
    """판정 프롬프트에서 그 항목 정의 블록('- **J2-3.a** · …' 부터 다음 항목 · 제목 앞까지)의 (시작, 끝) 위치."""
    m = re.search(rf"(?m)^- \*\*{re.escape(item)}\*\*", text)
    if not m:
        return None
    nxt = re.search(r"(?m)^(?:- \*\*J\d-\d\.[a-z]\*\*|#{1,3} )", text[m.end():])
    return m.start(), (m.end() + nxt.start()) if nxt else len(text)


def replace_in_prompt(text: str, old: str, new: str, item: Optional[str]) -> str:
    """판정 프롬프트 한 곳 바꾸기 — old 가 한 번이면 그곳, 여러 번이면 그 항목의 정의 블록 안에서 한 번일 때만 (10-25 밤: 상황표와 항목 정의에
    같은 괄호 구절이 있어 J2-3.a P1 이 '적용 불가'로 시험되지 못한 것)."""
    if text.count(old) == 1 or not item:
        return replace_once(text, old, new, "판정 프롬프트")
    span = item_block(text, item)
    if not span:
        return replace_once(text, old, new, "판정 프롬프트")
    a, b = span
    return text[:a] + replace_once(text[a:b], old, new, f"판정 프롬프트 {item} 블록") + text[b:]


def apply_proposal(prop: Dict[str, Any], work: Path, prompt_path: Path, jev_path: Path, checklists_path: Optional[Path],
                   item: Optional[str] = None) -> Dict[str, Any]:
    """안의 edits 를 임시 복사본에 넣는다 → {"prompt": 경로, "jev_questions": 경로, "turn_checklists": 경로 또는 None, "applied": [설명]}."""
    work.mkdir(parents=True, exist_ok=True)
    out = {"prompt": prompt_path, "jev_questions": jev_path, "turn_checklists": checklists_path, "stances": jv.STANCES_PATH, "applied": []}
    target = prop.get("target")
    edits = prop.get("edits") or []
    if target == "판정 프롬프트":
        text = prompt_path.read_text(encoding="utf-8")
        for e in edits:
            if not (e.get("old") and e.get("new") is not None):
                raise EditError("판정 프롬프트 edit 에는 old · new 가 있어야 합니다")
            text = replace_in_prompt(text, e["old"], e["new"], e.get("item") or item)
            out["applied"].append(f"프롬프트: {e['old'][:40]!r} → {e['new'][:40]!r}")
        p = work / "judge_prompt.txt"
        p.write_text(text, encoding="utf-8")
        out["prompt"] = p
    elif target == "1차 항목 정의":
        fixed = json.loads(jev_path.read_text(encoding="utf-8"))
        for e in edits:
            iid = e.get("item")
            if iid not in fixed.get("items", {}):
                raise EditError(f"1차 항목 정의에 {iid} 가 없습니다(J1~J4 만 파일에 있음)")
            q = fixed["items"][iid]
            if e.get("append"):
                q["instructions"] = str(q.get("instructions") or "") + " / " + e["append"].strip()
                out["applied"].append(f"{iid} 지시문 끝에 덧붙임: {e['append'][:40]!r}")
            elif e.get("old") and e.get("new") is not None:
                q["instructions"] = replace_once(str(q.get("instructions") or ""), e["old"], e["new"], f"{iid} instructions")
                for k, v in list((q.get("criteria") or {}).items()):
                    if e["old"] in str(v):
                        q["criteria"][k] = str(v).replace(e["old"], e["new"], 1)
                out["applied"].append(f"{iid}: {e['old'][:40]!r} → {e['new'][:40]!r}")
            else:
                raise EditError("1차 항목 정의 edit 에는 append 또는 old · new 가 있어야 합니다")
        p = work / "jev_questions.json"
        p.write_text(json.dumps(fixed, ensure_ascii=False, indent=1), encoding="utf-8")
        out["jev_questions"] = p
    elif target == "판정 태도 3종":
        doc = json.loads(jv.STANCES_PATH.read_text(encoding="utf-8"))
        for e in edits:
            if not (e.get("old") and e.get("new") is not None):
                raise EditError("판정 태도 3종 edit 에는 old · new 가 있어야 합니다")
            sid = e.get("item")
            if sid:
                k = next((i for i, st in enumerate(doc.get("stances") or []) if st.get("id") == sid), None)
                if k is None:
                    raise EditError(f"판정 태도 3종에 {sid} 가 없습니다(critical · friendly · guided)")
                doc["stances"][k] = json.loads(replace_once(json.dumps(doc["stances"][k], ensure_ascii=False), e["old"], e["new"], f"태도 {sid}"))
            else:
                doc = json.loads(replace_once(json.dumps(doc, ensure_ascii=False), e["old"], e["new"], "판정 태도 3종"))
            out["applied"].append(f"태도 {sid or '전체'}: {e['old'][:40]!r} → {e['new'][:40]!r}")
        p = work / "stances.json"
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        out["stances"] = p
    elif target == "확인 목록":
        if not checklists_path or not checklists_path.exists():
            raise EditError("턴별 확인 목록 파일이 없습니다")
        doc = json.loads(checklists_path.read_text(encoding="utf-8"))
        for e in edits:
            q, tn = str(e.get("question")), e.get("turn")
            turns = (doc.get("questions") or {}).get(q, {}).get("turns") or []
            t = next((x for x in turns if int(x.get("turn") or 0) == int(tn or 0)), None)
            if t is None:
                raise EditError(f"확인 목록에 Q{q} T{tn} 이 없습니다")
            if e.get("add_decided"):
                t.setdefault("decided", []).append(e["add_decided"].strip())
                out["applied"].append(f"Q{q} T{tn} 정한 판정 추가")
            elif e.get("add_check"):
                if re.search(r"PASS|FAIL|아니오|틀림", e["add_check"]):
                    raise EditError("확인할 것에는 판정 지시(PASS · FAIL · 틀림)를 쓰지 않습니다 — add_decided 로")
                t.setdefault("checks", []).append(e["add_check"].strip())
                out["applied"].append(f"Q{q} T{tn} 확인할 것 추가")
            elif e.get("old") and e.get("new") is not None:
                joined = json.dumps(t, ensure_ascii=False)
                t.clear()
                t.update(json.loads(replace_once(joined, json.dumps(e["old"], ensure_ascii=False)[1:-1], json.dumps(e["new"], ensure_ascii=False)[1:-1], f"Q{q} T{tn}")))
                out["applied"].append(f"Q{q} T{tn}: {e['old'][:40]!r} → {e['new'][:40]!r}")
            else:
                raise EditError("확인 목록 edit 에는 add_decided · add_check 또는 old · new 가 있어야 합니다")
        p = work / "turn_checklists.json"
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        out["turn_checklists"] = p
    else:
        raise EditError(f"시험으로 돌릴 수 없는 대상: {target} (기준 문서 · CODE · 질문은 사람이 반영)")
    return out


_SET_PROBLEMS: Dict[str, List[str]] = {}   # 질문 세트 → 고치기 전 검사에서 이미 있던 문제 (고친 뒤 새로 생긴 문제만 막는다)


def check_doc_sync(prop: Dict[str, Any], doc: Path = CRITERIA_DOC) -> str:
    """doc_sync(기준 문서 행을 심판 글과 같은 뜻으로)를 기준 문서에 넣은 글 — 넣을 수 없으면 EditError."""
    text = doc.read_text(encoding="utf-8")
    for e in prop.get("doc_sync") or []:
        text = replace_once(text, e["old"], e["new"], "기준 문서(doc_sync)")
    return text


def apply_text(prop: Dict[str, Any], set_dir: Path, dry: bool, item: str = "", cases_dir: str = "") -> List[str]:
    """다시 판정으로 잴 수 없는 대상을 고친다 — 질문(글 · 턴별 정답 메모의 지문) · 질문생성 프롬프트 · 기준 문서. dry 면 임시 복사본에서 검사만.
    질문은 세트 검사(new_question_set.check_set — 구성 · k 6~10 · 적용 수 · 토큰 길이 · 첨부)에서 새 문제가 생기면 넣지 않는다. 바뀐 것 설명 목록."""
    import tempfile
    target, edits = prop.get("target"), prop.get("edits") or []
    applied: List[str] = []
    if target in ("질문생성 프롬프트", "기준 문서"):
        f = QGEN_PROMPT if target == "질문생성 프롬프트" else CRITERIA_DOC
        text = f.read_text(encoding="utf-8")
        for e in edits:
            if not (e.get("old") and e.get("new") is not None):
                raise EditError(f"{target} edit 에는 old · new 가 있어야 합니다")
            text = replace_once(text, e["old"], e["new"], target)
            applied.append(f"{target}: {e['old'][:40]!r} → {e['new'][:40]!r}")
        if not dry:
            f.write_text(text, encoding="utf-8")
        return applied
    if target != "질문":
        raise EditError(f"글로 고치는 대상이 아님: {target}")
    from tools import new_question_set, question_revisions
    qpath = set_dir / "questions.json"
    groups = json.loads(qpath.read_text(encoding="utf-8"))
    by_id = {int(g["id"]): g for g in groups}
    touched: Dict[int, set] = defaultdict(set)
    for e in edits:
        q, tn = e.get("question"), int(e.get("turn") or 1)
        g = by_id.get(int(q)) if q is not None else None
        if g is None:
            raise EditError(f"질문 Q{q} 이 세트에 없습니다")
        turns = g.get("turns") or []
        if not 1 <= tn <= len(turns):
            raise EditError(f"Q{q} 에 턴 {tn} 이 없습니다")
        if not (e.get("old") and e.get("new") is not None):
            raise EditError("질문 edit 에는 old · new 가 있어야 합니다")
        turns[tn - 1]["question"] = replace_once(str(turns[tn - 1].get("question") or ""), e["old"], e["new"], f"Q{q} T{tn}")
        touched[int(q)].add(tn)
        applied.append(f"Q{q} T{tn}: {e['old'][:40]!r} → {e['new'][:40]!r}")
    if len(touched) > MAX_Q_EDITS:
        raise EditError(f"한 안에서 고칠 질문은 {MAX_Q_EDITS}개까지 ({len(touched)}개)")
    new_text = json.dumps(groups, ensure_ascii=False, indent=2)
    key = str(set_dir.resolve())
    if key not in _SET_PROBLEMS:
        _SET_PROBLEMS[key] = list(new_question_set.check_set(set_dir).get("problems") or [])
    with tempfile.TemporaryDirectory(prefix="refine-set-") as tmp:   # 세트 검사는 고친 복사본으로
        shadow = Path(tmp) / set_dir.name
        shutil.copytree(set_dir, shadow, ignore=shutil.ignore_patterns("생성기록*", "*.bak"))
        (shadow / "questions.json").write_text(new_text, encoding="utf-8")
        added = [x for x in new_question_set.check_set(shadow).get("problems") or [] if x not in _SET_PROBLEMS[key]]
    if added:
        raise EditError("세트 검사에서 새 문제 — " + " / ".join(added[:3]))
    if dry:
        return applied
    qpath.write_text(new_text, encoding="utf-8")
    plan_path = set_dir / "multiturn_v3" / "turn_checklists.json"
    if plan_path.exists():   # 정답 메모는 그대로 두고 지문만 새 질문 글로 — 판정기가 낡은 목록으로 버리지 않게
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        for q in touched:
            texts = [str(t.get("question") or "") for t in by_id[q].get("turns") or []]
            for t in ((plan.get("questions") or {}).get(str(q)) or {}).get("turns") or []:
                if int(t["turn"]) >= min(touched[q]) and int(t["turn"]) <= len(texts):
                    t["question_sha"] = jv.turn_sha(texts[:int(t["turn"])])
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    for q, tns in touched.items():
        question_revisions.add(set_dir, q, tns, str(prop.get("why") or "")[:300], by=f"하네스2 {item} {prop.get('id')}", cases=cases_dir)
    applied.append(f"다음 수집 때 다시 받을 질문: {', '.join(f'Q{q}' for q in sorted(touched))}")
    return applied


# ------------------------------------------------------------------ ④ 시험 (같은 칸 다시 판정) ------------------------------------------------------------------
def probe_label_dir(d: Path, label: str, rnd: int) -> Path:
    return d / "probes" / label / f"r{rnd}"


def run_judge(run_id: str, set_dir: Path, picks: Sequence[Tuple[int, str]], out_name: str, files: Dict[str, Any], log_path: Path,
              votes: Optional[int] = None, extra: Sequence[str] = ()) -> Dict[str, Any]:
    """judge_v4 를 따로 띄워 같은 (질문, 모델)을 새 폴더에 판정한다 — 저장 출력 재사용 없음(흔들림을 재려면 새로 판정해야 함)."""
    cmd = [sys.executable, "-X", "utf8", str(ROOT / "tools" / "judge_v4.py"), "--run", run_id, "--set", str(set_dir),
           "--pick", ",".join(f"{q}:{p}" for q, p in picks), "--out", out_name]
    if files.get("prompt") and Path(files["prompt"]) != jv.PROMPT_V4:
        cmd += ["--prompt", str(files["prompt"])]
    if files.get("jev_questions") and Path(files["jev_questions"]) != jv.JEV_V4:
        cmd += ["--jev-questions", str(files["jev_questions"])]
    if files.get("turn_checklists") and Path(files["turn_checklists"]) != set_dir / "multiturn_v3" / "turn_checklists.json":
        cmd += ["--turn-checklists", str(files["turn_checklists"])]
    if votes:
        cmd += ["--votes", str(votes)]
    cmd += list(extra)
    env = dict(os.environ)
    if files.get("stances") and Path(files["stances"]) != jv.STANCES_PATH:
        env["FIRST_JUDGE_STANCES"] = str(files["stances"])   # 고친 판정 태도 3종으로 시험 (10-26 저녁)
    t0 = time.monotonic()
    with log_path.open("w", encoding="utf-8") as f:
        proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(ROOT), text=True, encoding="utf-8", errors="replace", env=env)
    return {"out": out_name, "returncode": proc.returncode, "seconds": round(time.monotonic() - t0, 1), "cmd": cmd}


def cmd_probe(a: argparse.Namespace) -> Dict[str, Any]:
    d = Path(a.cases)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    cases = json.loads((d / "cases.json").read_text(encoding="utf-8"))
    set_dir = Path(meta["set"])
    props = json.loads((d / "proposals.json").read_text(encoding="utf-8")).get("proposals") or [] if (d / "proposals.json").exists() else []
    want = [p.strip() for p in (a.proposal or "all").split(",") if p.strip()]
    labels: List[Tuple[str, Optional[Dict[str, Any]]]] = []
    if "base" in want or "all" in want:
        labels.append(("base", None))
    for p in props:
        if "all" in want or p["id"] in want:
            labels.append((p["id"], p))
    picks = sorted({(int(c["question"]), c["provider"]) for c in cases})
    probes = json.loads((d / "probes.json").read_text(encoding="utf-8")) if (d / "probes.json").exists() else {"runs": []}
    base_files = {"prompt": jv.PROMPT_V4, "jev_questions": jv.JEV_V4, "turn_checklists": set_dir / "multiturn_v3" / "turn_checklists.json"}
    for label, prop in labels:
        files = dict(base_files)
        if prop is not None and prop.get("target") in TEXT_TARGETS:   # 다시 판정으로 잴 수 없음 — 고친 복사본 검사만 (10-27)
            probes["runs"] = [r for r in probes["runs"] if r.get("label") != label]
            try:
                done = apply_text(prop, set_dir, dry=True, item=meta.get("item") or "")
                probes["runs"].append({"label": label, "text_check": "ok", "applied": done})
                say(f"{label}: 검사 통과 — " + " · ".join(done))
            except EditError as exc:
                probes["runs"].append({"label": label, "error": str(exc)})
                say(f"✗ {label} 적용 불가 — {exc}")
            (d / "probes.json").write_text(json.dumps(probes, ensure_ascii=False, indent=1), encoding="utf-8")
            continue
        if prop is not None:
            try:
                files = apply_proposal(prop, d / "probes" / label / "files", jv.PROMPT_V4, jv.JEV_V4, base_files["turn_checklists"], item=meta.get("item"))
                check_doc_sync(prop)
            except EditError as exc:
                say(f"✗ {label} 적용 불가 — {exc}")
                probes["runs"].append({"label": label, "error": str(exc)})
                (d / "probes.json").write_text(json.dumps(probes, ensure_ascii=False, indent=1), encoding="utf-8")   # 마지막 안이어도 기록이 남게 (10-25 밤 J5-2.a P2)
                continue
            say(f"{label}: " + " · ".join(files["applied"]))
            probes["runs"] = [r for r in probes["runs"] if not (r.get("label") == label and r.get("error"))]   # 이번에 적용됐으면 옛 '적용 불가' 기록은 지움
        n_rounds = max(a.rounds, int(getattr(a, "base_rounds", 2) or 1)) if label == "base" else a.rounds   # base 는 흔들림 바닥을 재게 2회 이상 (10-25 밤)
        for rnd in range(1, n_rounds + 1):
            pd = probe_label_dir(d, label, rnd)
            pd.mkdir(parents=True, exist_ok=True)
            out_name = f"refine-{meta['item']}-{d.name}-{label}-r{rnd}".replace(" ", "")
            if any(r.get("out") == out_name and r.get("returncode") == 0 for r in probes["runs"]):
                say(f"{label} r{rnd}: 이미 있음({out_name})")
                continue
            say(f"{label} r{rnd}: 판정 {len(picks)}건 → {out_name} …")
            r = run_judge(meta["run"], set_dir, picks, out_name, files, pd / "judge.log", a.votes)
            r.update({"label": label, "round": rnd, "files": {k: str(v) for k, v in files.items() if k != "applied"}})
            probes["runs"].append(r)
            (d / "probes.json").write_text(json.dumps(probes, ensure_ascii=False, indent=1), encoding="utf-8")
            say(f"   끝 — 종료 코드 {r['returncode']} · {r['seconds']}초")
    return probes


# ------------------------------------------------------------------ ⑤ 점수 · 채택 ------------------------------------------------------------------
def probe_cells(run_id: str, out_name: str) -> Dict[Tuple[str, int, int, str], Dict[str, Any]]:
    """한 시험 판정 폴더의 항목 칸 전부 → {(모델, 질문, 턴, 항목): {decision, first, codex, reason}} + 질문 줄의 호출 · 시간은 '_line' 키에."""
    out: Dict[Tuple[str, int, int, str], Dict[str, Any]] = {}
    for (p, q), v in read_jsonl_last(RUNS / run_id / out_name / "results.jsonl").items():
        cx = (v.get("calls") or {}).get("codex") or {}
        out[(p, q, 0, "_line")] = {"verdict": v.get("verdict"), "codex_calls": int(cx.get("calls") or cx.get("rounds") or 0),
                                   "codex_seconds": float(cx.get("seconds") or 0), "seconds": float(v.get("seconds") or 0),
                                   "codex_in": int(((v.get("tokens") or {}).get("Codex") or {}).get("입력") or 0)}
        for tn, t in (v.get("turn_results") or {}).items():
            for iid, it in (t.get("items") or {}).items():
                if isinstance(it, dict):
                    out[(p, q, int(tn), iid)] = {"decision": it.get("decision"), "first": it.get("jev"), "codex": it.get("codex"),
                                                 "reason": it.get("reason") or "", "flagged": it.get("flagged") or ""}
    return out


def score_label(cases: Sequence[Dict[str, Any]], rounds: Sequence[Dict[Tuple[str, int, int, str], Dict[str, Any]]],
                base_r1: Optional[Dict[Tuple[str, int, int, str], Dict[str, Any]]]) -> Dict[str, Any]:
    """한 안(또는 base)의 점수 — 사람 표시 대비 정답률, 회차 사이 뒤집힘, 1차 ↔ 최종 갈림, base 대비 바뀐 칸, 같은 질문의 다른 항목 변화, 호출 · 시간."""
    keys = [cell_key(c) for c in cases]
    truth = {cell_key(c): c.get("truth") for c in cases}
    r1 = rounds[0] if rounds else {}
    acc_n = acc_ok = 0
    for k in keys:
        if truth.get(k) and r1.get(k, {}).get("decision") in ("PASS", "FAIL"):
            acc_n += 1
            acc_ok += r1[k]["decision"] == truth[k]
    flips = 0
    if len(rounds) > 1:
        for k in keys:
            ds = {r.get(k, {}).get("decision") for r in rounds}
            flips += len(ds) > 1
    split = sum(1 for k in keys if r1.get(k, {}).get("first") in ("PASS", "FAIL") and r1.get(k, {}).get("decision") in ("PASS", "FAIL")
                and r1[k]["first"] != r1[k]["decision"])
    changed: List[Dict[str, Any]] = []
    side = {"total": 0, "changed": 0, "verdict": 0}   # 같은 질문의 다른 항목: 바뀐 칸 전부 · 그중 FAIL · 보류가 끼어 질문 판정에 닿는 것
    ref = base_r1 if base_r1 is not None else (rounds[1] if len(rounds) > 1 else None)   # base 자신은 1회차 ↔ 2회차로 흔들림 바닥을 잰다
    if ref is not None:
        cmp = r1
        if base_r1 is not None:
            for k in keys:
                b, n = base_r1.get(k, {}).get("decision"), r1.get(k, {}).get("decision")
                if b != n:
                    changed.append({"cell": k, "전": b, "후": n, "사유": r1.get(k, {}).get("reason", "")[:160]})
        lines = {(k[0], k[1]) for k in keys}
        for k, v in ref.items():
            if k[3] == "_line" or (k[0], k[1]) not in lines or k in truth:
                continue
            b, n = v.get("decision"), cmp.get(k, {}).get("decision")
            side["total"] += 1
            if b != n:
                side["changed"] += 1
                side["verdict"] += bool({"FAIL", "UNVERIFIED"} & {b, n})
    lines = {(k[0], k[1]) for k in keys}
    calls = sum(r1.get((p, q, 0, "_line"), {}).get("codex_calls", 0) for p, q in lines)
    secs = sum(r1.get((p, q, 0, "_line"), {}).get("seconds", 0.0) for p, q in lines)
    tok = sum(r1.get((p, q, 0, "_line"), {}).get("codex_in", 0) for p, q in lines)
    dist = Counter(r1.get(k, {}).get("decision") for k in keys)
    return {"n": len(keys), "accuracy": (acc_ok / acc_n) if acc_n else None, "truth_n": acc_n, "flips": flips, "rounds": len(rounds),
            "first_final_split": split, "changed_vs_base": changed,
            "side_effect": (side["verdict"] / side["total"]) if side["total"] else 0.0,        # 다른 항목 가운데 FAIL · 보류가 바뀐 비율 (질문 판정에 닿는 변화)
            "side_effect_all": (side["changed"] / side["total"]) if side["total"] else 0.0,    # 해당 없음 ↔ 통과 같은 변화까지 전부
            "side_total": side["total"], "codex_calls": calls, "seconds": round(secs, 1), "codex_in": tok,
            "dist": {KO.get(k, str(k)): v for k, v in dist.items()}}


def adoption(base: Dict[str, Any], prop: Dict[str, Any]) -> Tuple[str, str]:
    """채택 판단 (v2 10절 ⑤): 정답률↑ · 뒤집힘↓(같거나) · 다른 항목 그대로. 사람 표시가 없으면 1차 ↔ 최종 갈림이 줄고 뒤집힘이 안 늘 때 '사람 확인 필요'.
    '다른 항목 그대로'의 잣대는 base 자신의 흔들림 바닥(1회차 ↔ 2회차, 있으면)을 넘지 않는 것 — 멀티턴은 다시 판정만 해도 칸이 움직이므로 고정 5% 만으로는 안을 다 기각한다."""
    if prop.get("error"):
        return "기각", f"적용 불가: {prop['error']}"
    if prop.get("text_target"):   # 질문 · 질문생성 프롬프트 · 기준 문서 — 답은 옛 글에 대한 것이라 다시 판정으로 못 잼 (10-27)
        acc = base.get("accuracy")
        if acc is not None and acc < 1:
            return "채택", f"검사 통과 · 이 기준 칸이 사람 판정과 어긋남(정답률 {acc:.0%}) — {prop['text_target']} 고침"
        if base.get("first_final_split", 0) >= 2 or base.get("flips", 0) >= 1:
            return "채택", (f"검사 통과 · 이 기준 칸이 흔들림(1차 ↔ 최종 갈림 {base.get('first_final_split', 0)} · 뒤집힘 {base.get('flips', 0)})"
                          f" — {prop['text_target']} 고침")
        return "보류", "이 기준 칸이 흔들리지 않고 사람 판정과도 맞아 고칠 근거 부족"
    floor = base.get("side_effect", 0.0) if base.get("rounds", 1) > 1 else 0.0
    limit = max(SIDE_EFFECT_MAX, floor * 1.25)
    if prop["side_effect"] > limit:
        return "기각", f"같은 질문의 다른 항목(FAIL · 보류)이 {prop['side_effect']:.0%} 바뀜(> {limit:.0%}" + (f", base 흔들림 {floor:.0%}" if floor else "") + ")"
    if prop["rounds"] > 1 and prop["flips"] > base.get("flips", 0):
        return "기각", f"회차 사이 뒤집힘 {base.get('flips', 0)} → {prop['flips']} 늘어남"
    if prop.get("accuracy") is not None and base.get("accuracy") is not None:
        if prop["accuracy"] > base["accuracy"]:
            return "채택", f"사람 표시 대비 정답률 {base['accuracy']:.0%} → {prop['accuracy']:.0%}"
        if prop["accuracy"] == base["accuracy"]:
            return "보류", "정답률 같음 — 바꿀 근거 없음"
        return "기각", f"정답률 {base['accuracy']:.0%} → {prop['accuracy']:.0%} 떨어짐"
    if prop["first_final_split"] < base.get("first_final_split", 0) or (prop["changed_vs_base"] and prop["flips"] <= base.get("flips", 0)):
        return "사람 확인 필요", (f"사람 표시 없음 — 1차 ↔ 최종 갈림 {base.get('first_final_split', 0)} → {prop['first_final_split']}, "
                              f"바뀐 칸 {len(prop['changed_vs_base'])}개(채택은 사람이 표시한 뒤)")
    return "보류", "사람 표시 없음 · 바뀐 것도 없음"


def cmd_score(a: argparse.Namespace) -> Dict[str, Any]:
    d = Path(a.cases)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    cases = json.loads((d / "cases.json").read_text(encoding="utf-8"))
    if a.marks:
        marks = load_marks(Path(a.marks))
        for c in cases:
            c["truth"] = truth_of(c, marks.get(cell_key(c))) or c.get("truth")
        (d / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    probes = json.loads((d / "probes.json").read_text(encoding="utf-8")) if (d / "probes.json").exists() else {"runs": []}
    props = json.loads((d / "proposals.json").read_text(encoding="utf-8")) if (d / "proposals.json").exists() else {"proposals": []}
    by_label: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    errors: Dict[str, str] = {}
    for r in probes["runs"]:
        if r.get("error"):
            errors[r["label"]] = r["error"]
        elif r.get("returncode") == 0:
            by_label[r["label"]].append(r)
    for label in list(errors):   # 고친 뒤 다시 시험해 성공한 안은 옛 '적용 불가' 기록을 쓰지 않는다 (10-25 밤 J2-3.a P1)
        if by_label.get(label):
            errors.pop(label)
    text_ok = {r["label"] for r in probes["runs"] if r.get("text_check") == "ok"}
    rounds_of = {label: [probe_cells(meta["run"], r["out"]) for r in sorted(rs, key=lambda x: x["round"])] for label, rs in by_label.items()}
    base_r1 = rounds_of.get("base", [None])[0]
    scores: Dict[str, Any] = {}
    if base_r1 is not None:
        scores["base"] = score_label(cases, rounds_of["base"], None)
    for p in props.get("proposals") or []:
        label = p["id"]
        if label in errors:
            scores[label] = {"error": errors[label]}
            continue
        if p.get("target") in TEXT_TARGETS and label in text_ok:
            scores[label] = {"text_target": p["target"]}
            continue
        if label in rounds_of and base_r1 is not None:
            scores[label] = score_label(cases, rounds_of[label], base_r1)
    verdicts = {label: adoption(scores.get("base", {}), s) for label, s in scores.items() if label != "base"}
    res = {"version": VERSION, "item": meta["item"], "scores": scores, "adoption": {k: {"판단": v[0], "이유": v[1]} for k, v in verdicts.items()},
           "proposals": {p["id"]: {k: p.get(k) for k in ("scope", "target", "why", "expected", "risk", "edits")} for p in props.get("proposals") or []},
           "keep_base": props.get("keep_base"), "scored_at": datetime.now(timezone.utc).isoformat()}
    (d / "score.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    md = score_md(res, cases)
    (d / "score.md").write_text(md, encoding="utf-8")
    say(md)
    return res


def score_md(res: Dict[str, Any], cases: Sequence[Dict[str, Any]]) -> str:
    L = [f"# 기준 다듬기 — {res['item']} 시험 결과", "",
         f"- 칸 {len(cases)}개(FAIL · 보류 {sum(1 for c in cases if c['decision'] != 'PASS')} · 통과 {sum(1 for c in cases if c['decision'] == 'PASS')}),"
         f" 사람 표시 {sum(1 for c in cases if c.get('truth'))}칸" + (" · 제안자: KEEP_BASE" if res.get("keep_base") else ""), "",
         "| 안 | 범위 · 대상 | 판정 분포 | 정답률(사람 표시) | 뒤집힘(회차) | 1차↔최종 갈림 | base 대비 바뀐 칸 | 다른 항목 변화(FAIL·보류 / 전부) | Codex 호출 | 시간 | 판단 |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for label, s in res["scores"].items():
        if s.get("error"):
            L.append(f"| {label} | — | — | — | — | — | — | — | — | — | 적용 불가: {s['error'][:80]} |")
            continue
        p = res["proposals"].get(label, {})
        acc = f"{s['accuracy']:.0%} ({s['truth_n']}칸)" if s.get("accuracy") is not None else "-"
        verdict = res["adoption"].get(label, {})
        L.append(f"| {label} | {p.get('scope', '')} · {p.get('target', '')} | {s['dist']} | {acc} | {s['flips']}/{s['n']} ({s['rounds']}회) | {s['first_final_split']} | "
                 f"{len(s['changed_vs_base'])} | {s['side_effect']:.0%} / {s.get('side_effect_all', 0):.0%} ({s['side_total']}칸{', base 는 1↔2회차' if label == 'base' else ''}) | {s['codex_calls']} | {s['seconds'] / 60:.1f}분 | "
                 f"{verdict.get('판단', 'base')} {('— ' + verdict['이유']) if verdict else ''} |")
    for label, p in res["proposals"].items():
        L += ["", f"## {label} — {p.get('scope')} · {p.get('target')}", "", f"- 왜: {p.get('why')}", f"- 기대: {', '.join(p.get('expected') or [])}",
              f"- 위험: {p.get('risk')}", "- 고침:"]
        for e in p.get("edits") or []:
            L.append("  - " + json.dumps({k: v for k, v in e.items() if v not in (None, "")}, ensure_ascii=False)[:300])
        s = res["scores"].get(label) or {}
        if s.get("changed_vs_base"):
            L += ["- 바뀐 칸:"]
            for ch in s["changed_vs_base"][:12]:
                k = ch["cell"]
                L.append(f"  - Q{k[1]} {k[0]} T{k[2]} {k[3]}: {KO.get(ch['전'])} → {KO.get(ch['후'])} — {str(ch['사유'])[:120]}")
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ ⑥ 교훈 · 반영 ------------------------------------------------------------------
REFLECT_GUIDE = """당신은 기준 다듬기 루프의 기록자입니다. 아래 [기록](한 기준의 칸 · 고칠 안 · 전후 시험 점수 · 채택 판단)을 읽고 **교훈 하나**를 남깁니다.
Harness² 의 교훈 형식을 따릅니다: cue(다음에 이 상황을 알아볼 신호 — 어떤 질문 · 답 · 사유 모양), lesson(그때 심판 글을 어떻게 둬야 하는가, 한두 문장),
evidence(어느 칸 · 어떤 수치가 이를 뒷받침하는가 — 기록에 있는 것만), confidence(높음 · 중간 · 낮음: 사람 표시가 있고 수치가 분명하면 높음, 심판끼리의 일치만이면 중간,
칸이 몇 개 안 되면 낮음). 추측 · 일반론은 쓰지 않습니다. 채택되지 않은 안에서도 배울 것(왜 안 됐나)이 있으면 그것을 교훈으로 삼아도 됩니다. 출력은 JSON 하나뿐입니다."""


def cmd_reflect(a: argparse.Namespace) -> Dict[str, Any]:
    d = Path(a.cases)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    res = json.loads((d / "score.json").read_text(encoding="utf-8"))
    cases = json.loads((d / "cases.json").read_text(encoding="utf-8"))
    ch = make_channel(a.channel, a.model, a.effort, d / "llm")
    record = {"항목": meta["item"], "칸_요약": [{"#": c["no"], "질문": c["question"], "모델": c["provider"], "판정": c["decision"], "사유": str(c.get("reason") or "")[:200],
                                            "사람_표시": c.get("truth")} for c in cases],
              "제안": res["proposals"], "점수": {k: {kk: vv for kk, vv in v.items() if kk != "changed_vs_base"} for k, v in res["scores"].items()},
              "바뀐_칸": {k: v.get("changed_vs_base") for k, v in res["scores"].items() if v.get("changed_vs_base")}, "채택_판단": res["adoption"]}
    status, body, error = ask(ch, REFLECT_GUIDE, record, f"reflect {meta['item']}", lesson_schema())
    if status != "OK" or not isinstance(body, dict):
        raise SystemExit(f"교훈 실패: {status} {error}")
    lesson = {**body, "item": meta["item"], "cases_dir": str(d), "adoption": res["adoption"], "at": datetime.now(timezone.utc).isoformat(),
              "channel": a.channel, "model": ch.model}
    (d / "lesson.json").write_text(json.dumps(lesson, ensure_ascii=False, indent=1), encoding="utf-8")
    if not a.no_doc:
        append_lesson(lesson, CRITERIA_DOC)
    say(f"교훈: [{lesson['confidence']}] {lesson['cue']} → {lesson['lesson']}")
    return lesson


def append_lesson(lesson: Dict[str, Any], doc: Path) -> None:
    """기준 v2 문서 12절(없으면 만듦)에 교훈 한 줄 — 날짜 · 항목 · 채택 · cue · lesson · evidence · confidence."""
    text = doc.read_text(encoding="utf-8")
    if LESSON_HEADING not in text:
        text = text.rstrip("\n") + f"\n\n---\n\n{LESSON_HEADING}\n\n기준 다듬기 루프(`tools/refine_loop.py`, Harness² 의 제안 → 시험 → 채택 → 교훈 흐름)가 남긴 교훈. 줄마다 날짜 · 항목 · 채택 판단 · 신호(cue) · 교훈 · 근거 · 확신.\n\n| 날짜 | 항목 | 채택 | 신호 | 교훈 | 근거 | 확신 |\n|---|---|---|---|---|---|---|\n"
    adopted = ", ".join(f"{k} {v['판단']}" for k, v in (lesson.get("adoption") or {}).items()) or "안 없음"
    cut = lambda t: (str(t or "").replace("\n", " ").strip()[:LESSON_CHARS] + ("…" if len(str(t or "")) > LESSON_CHARS else ""))   # 표는 짧게, 전문은 lesson.json
    row = f"| {lesson['at'][:10]} | {lesson['item']} | {adopted} | {cut(lesson['cue'])} | {cut(lesson['lesson'])} | {cut(lesson['evidence'])} | {lesson['confidence']} |"
    doc.write_text(text.rstrip("\n") + "\n" + row + "\n", encoding="utf-8")


def cmd_apply(a: argparse.Namespace) -> Dict[str, Any]:
    """채택된 안을 실제 파일에 넣는다 — 판정 프롬프트 · 1차 항목 정의 · 턴별 확인 목록(다른 대상은 글로만). 변경내역에 적을 줄을 돌려준다."""
    d = Path(a.cases)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    props = {p["id"]: p for p in json.loads((d / "proposals.json").read_text(encoding="utf-8")).get("proposals") or []}
    prop = props.get(a.proposal)
    if prop is None:
        raise SystemExit(f"안이 없습니다: {a.proposal}")
    if prop.get("target") not in AUTO_TARGETS:
        say(f"{a.proposal} 은 {prop.get('target')} 대상 — 파일에 자동으로 넣지 않습니다. 고칠 글:\n" + json.dumps(prop.get("edits"), ensure_ascii=False, indent=1))
        return {"applied": [], "manual": prop}
    if prop.get("target") in TEXT_TARGETS:   # 질문 · 질문생성 프롬프트 · 기준 문서 (10-27)
        done = apply_text(prop, Path(meta["set"]), dry=False, item=meta.get("item") or "", cases_dir=str(d))
        for x in done:
            say(f"반영: {x}")
        line = f"- 하네스2 {meta['item']} {a.proposal}({prop.get('scope')} · {prop.get('target')}): " + "; ".join(done) + f" — 근거 {d / 'score.md'}"
        return {"applied": done, "changelog": line}
    set_dir = Path(meta["set"])
    checklists = set_dir / "multiturn_v3" / "turn_checklists.json"
    tmp = apply_proposal(prop, d / "apply", jv.PROMPT_V4, jv.JEV_V4, checklists if checklists.exists() else None, item=meta.get("item"))
    for key, real in (("prompt", jv.PROMPT_V4), ("jev_questions", jv.JEV_V4), ("turn_checklists", checklists), ("stances", jv.STANCES_PATH)):
        if tmp.get(key) and Path(tmp[key]) != real:
            shutil.copy2(tmp[key], real)
            say(f"반영: {real}")
    if prop.get("doc_sync"):   # 기준의 뜻을 바꾼 안 — 기준 원본(v2 문서) 행도 같은 뜻으로 (기준은 문서 → 코드 순서, 10-27)
        CRITERIA_DOC.write_text(check_doc_sync(prop), encoding="utf-8")
        tmp["applied"].append(f"기준 문서 {meta['item']} 행도 같은 뜻으로({len(prop['doc_sync'])}곳)")
        say(f"반영: {CRITERIA_DOC}")
    line = f"- 기준 다듬기 루프 {meta['item']} {a.proposal}({prop.get('scope')} · {prop.get('target')}): " + "; ".join(tmp["applied"]) + f" — 근거 {d / 'score.md'}"
    say("변경내역에 적을 줄:\n" + line)
    return {"applied": tmp["applied"], "changelog": line}


def cmd_run(a: argparse.Namespace) -> int:
    d = cmd_collect(a)
    a.cases = str(d)
    body = cmd_propose(a)
    if body.get("keep_base") and not body.get("proposals"):
        say("제안자: 고칠 근거 없음(KEEP_BASE) — base 만 시험해 흔들림을 잰다")
    a.proposal = "all"
    cmd_probe(a)
    res = cmd_score(a)
    cmd_reflect(a)
    adopted = [k for k, v in res["adoption"].items() if v["판단"] == "채택"]
    a.adoption, a.applied = res["adoption"], []
    a.manual = [{"id": k, **v} for k, v in (res.get("proposals") or {}).items() if v.get("target") not in AUTO_TARGETS]
    if adopted and a.apply:
        for k in adopted:
            a.proposal = k
            a.applied.append({"id": k, **cmd_apply(a)})
    elif adopted:
        say(f"채택 가능한 안 {adopted} — 넣으려면: refine_loop.py apply --cases {d} --proposal {adopted[0]}")
    return 0


def rank_items(cells: Sequence[Dict[str, Any]], marks: Dict[Tuple[str, int, int, str], Any], n: int) -> List[Tuple[str, Dict[str, int]]]:
    """다듬을 LLM 기준 고르기 — 사람 표시(정답)가 많은 것 → 보류 → 심판 갈림 → FAIL 순. FAIL · 보류와 통과가 각각 MIN_CELLS 칸은 있어야 한다."""
    st: Dict[str, Counter] = defaultdict(Counter)
    for c in cells:
        if not str(c["item"]).startswith("J"):
            continue
        s = st[c["item"]]
        s[c["decision"]] += 1
        s["marked"] += cell_key(c) in marks
        s["split"] += "갈림" in (c.get("how") or "")
    ok = [i for i, s in st.items() if s["FAIL"] + s["UNVERIFIED"] >= MIN_CELLS and s["PASS"] >= MIN_CELLS]
    ok.sort(key=lambda i: (-st[i]["marked"], -st[i]["UNVERIFIED"], -st[i]["split"], -st[i]["FAIL"], i))
    return [(i, dict(st[i])) for i in ok[:n]]


def cmd_auto(a: argparse.Namespace) -> Dict[str, Any]:
    """평가 끝에 자동 (사용자 10-27) — 기준을 골라 run --apply 를 차례로, 시간 한도 안에서. 한 기준이 실패해도 다음 기준으로.
    끝에 변경내역 · 제안목록을 적고 한 줄씩 요약한다."""
    if a.channel == "claude" and not shutil.which("claude"):
        say("하네스2 건너뜀 — Claude CLI(claude)가 없습니다. 설치 뒤: aiona-chat-eval setup")
        return {"skipped": "no claude"}
    set_dir = Path(a.set)
    marks_path = RUNS / a.run / a.judge / "human_decisions.json"
    marks = load_marks(marks_path) if marks_path.exists() else {}
    picks = rank_items(load_cells(a.run, a.judge, set_dir), marks, a.items)
    if not picks:
        say("하네스2 — 다듬을 기준이 없습니다(FAIL · 보류와 통과가 함께 있는 LLM 기준 없음)")
        return {"results": []}
    say("하네스2 — 다듬을 기준: " + " · ".join(f"{i}(사람 표시 {s.get('marked', 0)} · 보류 {s.get('UNVERIFIED', 0)} · 갈림 {s.get('split', 0)} · FAIL {s.get('FAIL', 0)})"
                                        for i, s in picks))
    started, results = time.monotonic(), []
    for item, _st in picks:
        if (time.monotonic() - started) / 60 > a.budget_min:
            say(f"하네스2 — 시간 한도 {a.budget_min}분을 넘어 {item} 부터는 다음 평가 때")
            break
        ns = argparse.Namespace(run=a.run, judge=a.judge, item=item, set=str(set_dir), fail=a.fail, n_pass=a.fail, types="", seed=1,
                                max_lines=a.max_lines, marks=str(marks_path) if marks else None, channel=a.channel, model=a.model, effort=a.effort,
                                proposal="all", rounds=2, base_rounds=2, votes=None, no_doc=False, apply=True)
        try:
            cmd_run(ns)
            results.append({"item": item, "cases": ns.cases, "adoption": ns.adoption, "applied": ns.applied, "manual": ns.manual})
        except (SystemExit, Exception) as exc:   # 한 기준이 막혀도 다음 기준으로 — 평가 결과는 그대로
            say(f"⚠ 하네스2 {item} 건너뜀: {type(exc).__name__}: {str(exc)[:200]}")
            results.append({"item": item, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
    write_followups(results, a.run, a.judge)
    for r in results:
        if r.get("error"):
            say(f"  {r['item']}: 멈춤 — {r['error']}")
            continue
        verdicts = " · ".join(f"{k} {v['판단']}" for k, v in (r["adoption"] or {}).items()) or "안 없음(지금 그대로가 낫다고 봄)"
        say(f"  {r['item']}: {verdicts}" + (f" → 반영 {', '.join(x['id'] for x in r['applied'])}" if r["applied"] else "")
            + (f" · 사람이 볼 제안 {len(r['manual'])}개(제안목록)" if r["manual"] else ""))
    return {"results": results}


def write_followups(results: Sequence[Dict[str, Any]], run_id: str, judge_out: str) -> None:
    """반영한 안 → 변경내역, 자동으로 넣지 못한 대상(기준 문서 · 질문 · CODE)의 개선 · 확장 제안 → 하네스2_제안목록.md."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [x["changelog"] for r in results for x in r.get("applied") or [] if x.get("changelog")]
    if lines:   # 판정이 달라지는 변경 — 시맨틱 버저닝의 부 버전을 올리고 CHANGELOG 에 (사용자 10-27)
        from tools import version
        new = version.bump("minor", [f"하네스2 자동 반영(실행 {run_id} · 판정 {judge_out}): " + "; ".join(
            str(x.get("changelog") or "").lstrip("- ")[:200] for r in results for x in r.get("applied") or [] if x.get("changelog"))])
        lines.append(f"- 버전 {new} (부 버전 — 판정 프롬프트 · 기준 정의가 바뀌어 다음 평가부터 판정이 달라질 수 있음)")
        f = CHANGELOG_DIR / f"{datetime.now():%Y-%m-%d}-변경내역.md"
        head = "" if f.exists() else f"# {datetime.now():%Y-%m-%d} 변경내역\n"
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as h:
            h.write(head + f"\n## 하네스2 자동 반영 — 실행 {run_id} · 판정 {judge_out} ({stamp})\n\n" + "\n".join(lines) + "\n")
    manual = [(r["item"], m, r["cases"]) for r in results for m in r.get("manual") or []]
    if manual:
        if not FOLLOWUPS.exists():
            FOLLOWUPS.parent.mkdir(parents=True, exist_ok=True)
            FOLLOWUPS.write_text("# 하네스2 제안목록 — 자동으로 넣지 못한 개선 · 확장\n\n평가 끝 하네스2(`tools/refine_loop.py auto`)가 낸 안 가운데 "
                                 "CODE 검사 코드를 고치거나 넓히자는 것(판정 프롬프트 · 평가 기준 · 질문 · 질문생성 프롬프트는 자동으로 반영됨). "
                                 "기준은 문서 → 코드 순서로 반영한다.\n", encoding="utf-8")
        body = [f"\n## {stamp} — 실행 {run_id} · 판정 {judge_out}\n"]
        for item, m, cases in manual:
            edits = "; ".join(str(e.get("new") or e.get("append") or e.get("add_check") or e.get("add_decided") or "")[:160]
                              for e in m.get("edits") or [] if isinstance(e, dict))
            body.append(f"- **{item} {m['id']}** ({m.get('target')} · {m.get('scope')}) — {str(m.get('why') or '')[:300]}"
                        + (f" · 고칠 글: {edits}" if edits else "") + f" · 근거 `{cases}`")
        with FOLLOWUPS.open("a", encoding="utf-8") as h:
            h.write("\n".join(body) + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common_collect(p: argparse.ArgumentParser) -> None:
        p.add_argument("--run", required=True, help="수집 실행 id (data/comparison_runs/<id>.json)")
        p.add_argument("--judge", required=True, help="재료로 쓸 판정 폴더 이름 (data/judge_runs/<실행>/<이름>)")
        p.add_argument("--item", required=True, help="다듬을 기준 항목 (예: J1-4.b)")
        p.add_argument("--set", default=str(ROOT / "참고자료" / "질문세트_78"))
        p.add_argument("--fail", type=int, default=12, help="FAIL · 보류 칸 수")
        p.add_argument("--pass", dest="n_pass", type=int, default=12, help="통과 칸 수(같은 수가 기본, v2 10절 ①)")
        p.add_argument("--types", default="", help="싱글,멀티턴 — 비우면 둘 다 (멀티턴은 대화 전체를 다시 판정하므로 비쌈)")
        p.add_argument("--seed", type=int, default=1)
        p.add_argument("--max-lines", type=int, help="(모델, 질문) 상한 — FAIL · 보류가 많은 순으로 이만큼만(멀티턴 재판정 비용 조절)")
        p.add_argument("--marks", help="사람 표시 파일(표시결과_*.json 또는 human_decisions.json)")

    def common_llm(p: argparse.ArgumentParser) -> None:
        p.add_argument("--channel", default="claude", choices=["claude", "codex"], help="제안 · 교훈을 쓸 모델 통로 — 기본 Claude CLI (사용자 10-25)")
        p.add_argument("--model", help="모델 이름 (Claude 기본 claude-opus-5-5 — Claude Code 2.1.280 이상, 옛 CLI 면 Sonnet 5.5 로 대체; Codex 기본 .env CODEX_CLI_MODEL)")
        p.add_argument("--effort", help="추론 노력 (Claude 기본 high)")

    def common_probe(p: argparse.ArgumentParser) -> None:
        p.add_argument("--proposal", default="all", help="base · P1 · all (쉼표로 여러 개)")
        p.add_argument("--rounds", type=int, default=2, help="안마다 다시 판정하는 횟수 — 2 이상이면 회차 사이 뒤집힘을 잰다 (v2 10절 ④)")
        p.add_argument("--base-rounds", type=int, default=2, help="base 를 다시 판정하는 횟수 — 안의 효과를 base 자신의 흔들림과 견주게 (기본 2, 10-25 밤)")
        p.add_argument("--votes", type=int, help="1차 심판 투표 수 (기본 judge_v4 와 같음)")

    p = sub.add_parser("collect", help="① 모으기 — FAIL · 보류 칸과 같은 수의 통과 칸")
    common_collect(p)
    p = sub.add_parser("propose", help="③ 고칠 안 — Claude CLI(기본) 가 2~3개 제안")
    p.add_argument("--cases", required=True)
    common_llm(p)
    p = sub.add_parser("probe", help="④ 시험 — 안을 임시 파일에 넣고 같은 칸을 전 · 후로 다시 판정")
    p.add_argument("--cases", required=True)
    common_probe(p)
    p = sub.add_parser("score", help="⑤ 점수 · 채택 판단")
    p.add_argument("--cases", required=True)
    p.add_argument("--marks")
    p = sub.add_parser("reflect", help="⑥ 교훈 — 기준 v2 12절에 한 줄")
    p.add_argument("--cases", required=True)
    p.add_argument("--no-doc", action="store_true", help="문서에 쓰지 않고 lesson.json 만")
    common_llm(p)
    p = sub.add_parser("apply", help="채택된 안을 실제 파일에")
    p.add_argument("--cases", required=True)
    p.add_argument("--proposal", required=True)
    p = sub.add_parser("run", help="① → ⑥ 한 번에")
    common_collect(p)
    common_llm(p)
    common_probe(p)
    p.add_argument("--no-doc", action="store_true")
    p.add_argument("--apply", action="store_true", help="채택된 안을 실제 파일에 바로 넣는다")
    p = sub.add_parser("auto", help="평가 끝에 자동 — 기준을 골라 run --apply 를 차례로 (start 가 부름)")
    p.add_argument("--run", required=True)
    p.add_argument("--judge", default="v4")
    p.add_argument("--set", default=str(ROOT / "참고자료" / "질문세트_78"))
    p.add_argument("--items", type=int, default=2, help="다듬을 기준 수 (기본 2)")
    p.add_argument("--fail", type=int, default=12, help="기준마다 FAIL · 보류 칸 수(통과도 같은 수)")
    p.add_argument("--max-lines", type=int, default=8, help="기준마다 다시 판정할 (모델, 질문) 상한 — 비용 조절")
    p.add_argument("--budget-min", type=int, default=180, help="이 시간(분)을 넘으면 다음 기준은 다음 평가 때")
    common_llm(p)
    a = ap.parse_args(argv)
    if a.cmd == "collect":
        cmd_collect(a)
    elif a.cmd == "propose":
        cmd_propose(a)
    elif a.cmd == "probe":
        cmd_probe(a)
    elif a.cmd == "score":
        cmd_score(a)
    elif a.cmd == "reflect":
        cmd_reflect(a)
    elif a.cmd == "apply":
        cmd_apply(a)
    elif a.cmd == "run":
        return cmd_run(a)
    elif a.cmd == "auto":
        cmd_auto(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
