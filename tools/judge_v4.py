"""판정 v4 시험 — 고정 프롬프트 v4(상황 규칙 전부) + 수집 JSON(질문·첨부·참조 자료·답변)만 Codex CLI 와 Jev 에 던진다.
심판이 턴마다 상황을 직접 찾고(상황표 1), 항목을 판정하고, 질문 하나의 결론까지 낸다 (사용자 2026-10-20).
질문은 세트의 questions.json 을 그대로 쓴다 — 질문을 다시 검사하거나 해석하는 단계는 없다 (사용자 10-20: 이미 확인해 만든 질문).

    python -X utf8 tools/judge_v4.py --run ac6507c00276 --pick 143:claude,115:aiona,17:gemini

→ data/judge_runs/<실행>/v4/ — inputs/Q143_claude.json(심판이 받은 JSON) · codex/·jev/(심판 출력 원문) · results.jsonl · report.md
합치기(J1~J4, 두 심판이 모두 봄): Codex FAIL → FAIL · Jev FAIL 인데 Codex 는 FAIL 아님 → 보류(심판 갈림) · 그 밖은 Codex.
J5~J8 은 Codex 만. 질문 결론: FAIL 항목 하나라도 → FAIL, 보류 하나라도 → HOLD, 그 밖 PASS. CODE FAIL 이 있으면 최종 FAIL.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
import json
import hashlib
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval import error_cases, long_context, multi_turn  # noqa: E402
from code_eval import semantic_items as si  # noqa: E402
from tools import judge_inputs as ji  # noqa: E402
from tools import judge_split as js  # noqa: E402
from tools.build_judge_prompt_v4 import JEV_V4, V4 as PROMPT_V4  # noqa: E402
from tools.judge_run import judges_from_env, say  # noqa: E402
from webapp.question_generation import flatten  # noqa: E402
from webapp.run_summary import question_outcome  # noqa: E402

VERSION = "judge-v4-1"   # trial-1: 3건 시험 · v4-1: 78 전체(빈 출력·못 받은 답 처리, Jev 판정할_턴·긴 자료 줄이기)
SET_DIR = ROOT / "참고자료" / "질문세트_326_생성프롬프트v2"
KO = {"PASS": "PASS", "FAIL": "FAIL", "NOT_APPLICABLE": "해당 없음", "UNVERIFIED": "보류", None: "-"}


# --- 입력 JSON --------------------------------------------------------------------------------------------------------

def input_tokens(row: Dict[str, Any]) -> Optional[int]:
    """그 턴의 질문 + 첨부 내용 + 참조 자료 토큰 수(J7 판단용) — 토크나이저가 없으면 None."""
    text = (row.get("question") or "") + "".join("\n" + (a.get("content") or "") for a in row.get("attachments") or [] if isinstance(a, dict))
    try:
        return long_context.measure(text, row.get("reference"))["tokens"]
    except long_context.TokenizerUnavailable:
        return None


#: 1차 심판 나누기 기준(사용자 10-21 넘겨받기 9번 · 10-22 확인) — 질문 전체 입력이 이 토큰 수 이하면 Jev, 넘으면 Decisions
JEV_MAX_TOKENS = 32_000


def question_tokens(turns: List[Dict[str, Any]], answers: Dict[str, str]) -> Optional[int]:
    """질문 하나 전체(모든 턴의 질문 · 첨부 내용 · 참조 자료 + 답변)의 토큰 수 — 1차 심판(Jev · Decisions)을 고르는 데 쓴다."""
    total = 0
    for row in turns:
        t = input_tokens(row)
        if t is None:
            return None
        total += t
        try:
            total += long_context.measure(answers.get(row["case_id"]) or "", None)["tokens"]
        except long_context.TokenizerUnavailable:
            return None
    return total


def first_judge(part: Dict[str, Any], questions: Dict[str, Any], jev: Any, decisions: Any, use_jev: bool) -> Tuple[str, Any, str, Optional[str]]:
    """1차 심판 한 턴 — use_jev(질문이 3만 2천 토큰 이하 · 그 턴에 첨부 없음)면 Jev, Jev 가 실패하면(입력 한도 · 오류) Decisions.
    첨부가 있는 턴은 Decisions(이미지 원본을 본다, 사용자 10-20). 돌려주는 값: (status, body, error, 쓴 심판 'jev' · 'decisions' · None)."""
    errors: List[str] = []
    if FIRST_JUDGE_MODE == "jev":   # 심판 비교 실험 — 모든 턴 Jev, 실패해도 Decisions 로 넘기지 않는다 (그 표는 빠짐)
        if jev is None:
            return "ERROR", None, "Jev 없음 (--first-judge jev)", None
        status, body, error = jev.judge(part, questions)
        return (status, body, "", "jev") if status == "OK" else (status, None, f"Jev {status} {str(error)[:160]}", None)
    if FIRST_JUDGE_MODE == "decisions":   # 심판 비교 실험 — 모든 턴 Decisions
        use_jev = False
    if use_jev and jev is not None:
        status, body, error = jev.judge(part, questions)
        if status == "OK":
            return status, body, "", "jev"
        errors.append(f"Jev {status} {str(error)[:100]} → Decisions")
    if decisions is not None:
        status, body, error = decisions.judge(part, questions)
        return status, body, "; ".join(errors + ([str(error)] if error else [])), "decisions"
    return "ERROR", None, "; ".join(errors) or "1차 심판 없음", None


def merge_votes(bodies: List[Dict[str, Any]], keys: List[str]) -> Dict[str, Any]:
    """여러 번 판정한 1차 심판 답(bodies 의 answers)을 항목(key)마다 하나로 — 다수결 choice, 평균 확률 · 확신, votes · vote_split. 유효한 표가 없으면 None."""
    merged: Dict[str, Any] = {}
    for key in keys:
        entries = [b.get("answers", {}).get(key) for b in bodies if isinstance(b, dict)]
        entries = [e for e in entries if isinstance(e, dict) and e.get("choice") is not None]
        if not entries:
            merged[key] = None
            continue
        choices = [e["choice"] for e in entries]
        top = Counter(choices).most_common()
        avg: Dict[str, float] = defaultdict(float)
        for e in entries:
            for k, v in (e.get("probabilities") or {}).items():
                if isinstance(v, (int, float)):
                    avg[k] += float(v) / len(entries)
        if len(top) > 1 and top[0][1] == top[1][1]:   # 동률(세 표가 다 다름) → 평균 확률이 가장 높은 값
            tied = [c for c, n in top if n == top[0][1]]
            choice = max(tied, key=lambda c: avg.get(c, 0.0))
        else:
            choice = top[0][0]
        confs = [float(e["confidence"]) for e in entries if isinstance(e.get("confidence"), (int, float))]
        pick = next((e for e in entries if e["choice"] == choice), entries[0])   # 사유 · 인용은 다수결 값을 낸 표의 것
        merged[key] = {**pick, "choice": choice, "probabilities": ({k: round(v, 3) for k, v in avg.items()} or pick.get("probabilities")),
                       "confidence": (round(sum(confs) / len(confs), 3) if confs else pick.get("confidence")),
                       "votes": choices, "vote_split": len(set(choices)) > 1}
    return merged


def first_judge_votes(part: Dict[str, Any], questions: Dict[str, Any], jev: Any, decisions: Any, use_jev: bool,
                      votes: int = 1) -> Tuple[str, Any, str, Optional[str], List[Tuple[Optional[str], Dict[str, Any]]], int]:
    """first_judge 를 votes 번 불러 다수결로 합친다. 돌려주는 값: (status, 합친 body, error, 대표 심판(표가 가장 많은 심판),
    성공한 (심판, 답) 목록 — 사용량을 심판마다 따로 더하려고, 실제 시도 수). votes 가 1 이면 first_judge 그대로.
    일부만 성공하면 성공한 표로만 다수결, 모두 실패면 마지막 오류. 표마다 심판이 다를 수 있어(Jev 실패 → Decisions) 합친 body 에 vote_judges 를 남긴다."""
    if votes <= 1:
        status, body, error, used = first_judge(part, questions, jev, decisions, use_jev)
        return status, body, error, used, ([(used, body)] if status == "OK" and isinstance(body, dict) else []), 1
    oks: List[Tuple[Optional[str], Dict[str, Any]]] = []
    errors: List[str] = []
    attempts = 0
    for _ in range(votes):
        attempts += 1
        status, body, error, used = first_judge(part, questions, jev, decisions, use_jev)
        if status == "OK" and isinstance(body, dict):
            oks.append((used, body))
        else:
            errors.append(f"{status} {str(error)[:80]}")
    if not oks:
        return "ERROR", None, "; ".join(errors) or "1차 심판 모두 실패", None, [], attempts
    judges = [u for u, _ in oks]
    used_judge = Counter(judges).most_common(1)[0][0]
    merged = {"answers": merge_votes([b for _, b in oks], list(questions.keys())), "votes": len(oks), "vote_judges": judges, "vote_errors": errors}
    return "OK", merged, "; ".join(errors), used_judge, oks, attempts


#: 1차 심판 판정 프롬프트 3종 — 태도 글 · 가중치 · 확신 하한 (사용자 10-26 저녁). .env FIRST_JUDGE_STANCES 로 다른 파일
STANCES_PATH = ROOT / "참고자료" / "프롬프트" / "1차심판_프롬프트3종.json"
_STANCES: Dict[str, Any] = {}


def first_stances(path: Optional[Path] = None) -> Dict[str, Any]:
    """판정 프롬프트 3종 설정 — {version, min_choice_prob, stances: [{id, name, text, weights}]} (한 번 읽어 둔다)."""
    from code_eval.llm_client import env_value
    p = Path(path or env_value("FIRST_JUDGE_STANCES") or STANCES_PATH)
    key = str(p)
    if key not in _STANCES:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        if not cfg.get("stances"):
            raise ValueError(f"판정 프롬프트 3종 설정에 stances 가 없습니다: {p}")
        _STANCES[key] = cfg
    return _STANCES[key]


def stance_questions(questions: Dict[str, Any], stance: Dict[str, Any]) -> Dict[str, Any]:
    """항목 정의마다 instructions 앞에 판정 태도 한 문단 — 정의 · 키는 그대로라 답을 같은 자리로 합칠 수 있다."""
    return {k: ({**v, "instructions": stance["text"] + chr(10) + str(v.get("instructions") or "")} if isinstance(v, dict) else v)
            for k, v in questions.items()}


def stance_merge(votes: List[Tuple[Dict[str, Any], Dict[str, Any]]], keys: List[str], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """태도마다 받은 1차 답을 항목마다 하나로 — 표마다 '태도 × 고른 값' 가중치를 더해 가장 큰 값(같으면 평균 확률이 높은 값).
    고른 값의 확률이 min_choice_prob 미만인 표는 버리고(모두 미만이면 모두 셈), 확률 · 확신은 센 표의 평균. 표 셋은 vote_detail 에 남긴다."""
    floor = float(cfg.get("min_choice_prob") or 0.0)
    merged: Dict[str, Any] = {}
    for key in keys:
        entries = [(st, b.get("answers", {}).get(key)) for st, b in votes if isinstance(b, dict)]
        entries = [(st, e) for st, e in entries if isinstance(e, dict) and e.get("choice") is not None]
        if not entries:
            merged[key] = None
            continue
        counted = [(st, e) for st, e in entries if entry_top(e) >= floor] or entries
        score: Dict[str, float] = defaultdict(float)
        for st, e in counted:
            score[e["choice"]] += float((st.get("weights") or {}).get(e["choice"], 1.0))
        avg: Dict[str, float] = defaultdict(float)
        for _, e in counted:
            for k, v in (e.get("probabilities") or {}).items():
                if isinstance(v, (int, float)):
                    avg[k] += float(v) / len(counted)
        best = max(score.values())
        choice = max((c for c, v in score.items() if abs(v - best) < 1e-9), key=lambda c: avg.get(c, 0.0))
        confs = [float(e["confidence"]) for _, e in counted if isinstance(e.get("confidence"), (int, float))]
        pick = next(e for _, e in counted if e["choice"] == choice)   # 사유 · 인용은 고른 값을 낸 첫 표의 것
        choices = [e["choice"] for _, e in entries]
        merged[key] = {**pick, "choice": choice, "probabilities": ({k: round(v, 3) for k, v in avg.items()} or pick.get("probabilities")),
                       "confidence": (round(sum(confs) / len(confs), 3) if confs else pick.get("confidence")),
                       "votes": choices, "vote_split": len(set(choices)) > 1,
                       "vote_detail": [{"stance": st.get("id"), "choice": e["choice"], "top": round(entry_top(e), 3), "counted": any(e is c for _, c in counted)}
                                       for st, e in entries],
                       "vote_score": {c: round(v, 3) for c, v in score.items()}}
    return merged


def stance_judge_votes(part: Dict[str, Any], questions: Dict[str, Any], jev: Any, decisions: Any, use_jev: bool,
                       cfg: Optional[Dict[str, Any]] = None) -> Tuple[str, Any, str, Optional[str], List[Tuple[Optional[str], Dict[str, Any]]], int]:
    """판정 프롬프트 3종으로 1차 심판을 한 번씩 — 심판은 크기로(use_jev, Jev 가 실패하면 그 표는 Decisions), 가중 다수결로 합친다.
    돌려주는 값은 first_judge_votes 와 같다. 일부 태도만 성공하면 성공한 표로만, 모두 실패면 오류."""
    cfg = cfg or first_stances()
    oks: List[Tuple[Dict[str, Any], Optional[str], Dict[str, Any]]] = []
    errors: List[str] = []
    attempts = 0
    for st in cfg["stances"]:
        attempts += 1
        status, body, error, used = first_judge(part, stance_questions(questions, st), jev, decisions, use_jev)
        if status == "OK" and isinstance(body, dict):
            oks.append((st, used, body))
        else:
            errors.append(f"{st.get('name') or st.get('id')} {status} {str(error)[:80]}")
    if not oks:
        return "ERROR", None, "; ".join(errors) or "1차 심판 모두 실패", None, [], attempts
    judges = [u for _, u, _ in oks]
    merged = {"answers": stance_merge([(st, b) for st, _, b in oks], list(questions.keys()), cfg), "votes": len(oks), "vote_judges": judges,
              "vote_errors": errors, "stances": cfg.get("version")}
    return "OK", merged, "; ".join(errors), Counter(judges).most_common(1)[0][0], [(u, b) for _, u, b in oks], attempts


def entry_top(e: Optional[Dict[str, Any]]) -> float:
    """1차 답이 고른 값의 확률(없으면 확신)."""
    if not isinstance(e, dict) or e.get("choice") is None:
        return 0.0
    probs = e.get("probabilities") or {}
    v = probs.get(e["choice"]) if isinstance(probs, dict) else None
    if not isinstance(v, (int, float)):
        v = e.get("confidence")
    return float(v) if isinstance(v, (int, float)) else 0.0


def jev_votes_adaptive(part: Dict[str, Any], questions: Dict[str, Any], jev: Any) -> Tuple[str, Any, str, List[Dict[str, Any]], int]:
    """Jev 2번 — 갈린 항목이 하나라도 있으면(또는 한 번이 실패하면) 3번째, 항목마다 다수결 (10-23 회의 '두 번까지는 무조건 돌리고 처음과 두 번째가
    일치하지 않으면 세 번째'). 10-26 비교: 3,701칸 중 3,668칸이 두 표에서 같음 → 결과는 3표와 같고 호출은 약 1/3 줄어듦. 돌려주는 값: (status, 합친 body, error, 성공한 답들, 시도 수)."""
    oks: List[Dict[str, Any]] = []
    errors: List[str] = []
    attempts = 0

    def once() -> None:
        nonlocal attempts
        attempts += 1
        status, body, error = jev.judge(part, questions)
        if status == "OK" and isinstance(body, dict):
            oks.append(body)
        else:
            errors.append(f"{status} {str(error)[:80]}")
    once()
    once()
    split = len(oks) < 2 or any(((oks[0].get("answers") or {}).get(k) or {}).get("choice") != ((oks[1].get("answers") or {}).get(k) or {}).get("choice")
                                for k in questions)
    if split and (oks or len(errors) < 2):
        once()
    if not oks:
        return "ERROR", None, "; ".join(errors) or "Jev 모두 실패", [], attempts
    merged = {"answers": merge_votes(oks, list(questions.keys())), "votes": len(oks), "vote_judges": ["jev"] * len(oks), "vote_errors": errors}
    return "OK", merged, "; ".join(errors), oks, attempts


def jev_once(part: Dict[str, Any], questions: Dict[str, Any], jev: Any) -> Tuple[str, Any, str, List[Dict[str, Any]], int]:
    """Jev 한 번(JEV_VOTES=1) — jev_votes_adaptive 와 같은 모양으로 돌려준다."""
    status, body, error = jev.judge(part, questions)
    if status == "OK" and isinstance(body, dict):
        return "OK", body, "", [body], 1
    return "ERROR", None, f"{status} {str(error)[:80]}", [], 1


def cross_flag(item: str, j: Optional[Dict[str, Any]], d: Optional[Dict[str, Any]], sure: float = None) -> str:
    """교차 확인 — Codex 로 넘길 까닭(빈 문자열이면 1차에서 끝). 둘 다 답했을 때: 하나라도 FAIL · 둘이 다름 · 하나라도 애매(고른 답 확률 < sure).
    한 심판만 답했으면 그 답으로 지금 선별 규칙(screen_flag)."""
    sure = CROSS_SURE if sure is None else sure
    jc = (j or {}).get("choice") if isinstance(j, dict) else None
    dc = (d or {}).get("choice") if isinstance(d, dict) else None
    if jc is None or dc is None:
        one = d if dc is not None else j
        flag, reason = screen_flag(item, one)
        return (reason or "1차 답 없음") if flag else ""
    if "FAIL" in (jc, dc):
        return f"FAIL 확인 — Jev {KO.get(jc, jc)} · Decisions {KO.get(dc, dc)}"
    if jc != dc:
        return f"Jev {KO.get(jc, jc)} · Decisions {KO.get(dc, dc)} — 다름"
    tj, td = entry_top(j), entry_top(d)
    if tj < sure or td < sure:
        return f"애매 — Jev {tj:.2f} · Decisions {td:.2f} (확실 {sure})"
    if item in SCREEN_ALWAYS_CODEX and jc == "PASS":
        return "사실 확인은 Codex(웹 검색)가 봄"
    return ""


def cross_entry(item: str, j: Optional[Dict[str, Any]], d: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """교차 확인한 칸의 1차 대표 답 — 같으면 그 답, 하나라도 FAIL 이면 FAIL 쪽(Codex 가 못 보면 보류로 가게), 아니면 Decisions. 두 심판의 답 · 넘긴 까닭을 남긴다."""
    jc = (j or {}).get("choice") if isinstance(j, dict) else None
    dc = (d or {}).get("choice") if isinstance(d, dict) else None
    if jc is None and dc is None:
        return None
    if jc == "FAIL" and dc != "FAIL":
        pick = j
    elif dc is not None:
        pick = d
    else:
        pick = j
    e = {k: v for k, v in pick.items() if k not in ("votes", "vote_split")}
    e["votes"] = (j or {}).get("votes") if isinstance(j, dict) else None
    e["vote_split"] = bool((j or {}).get("vote_split")) if isinstance(j, dict) else False
    e["cross"] = {"jev": {"choice": jc, "top": round(entry_top(j), 3), "votes": (j or {}).get("votes") if isinstance(j, dict) else None},
                  "decisions": {"choice": dc, "top": round(entry_top(d), 3)}}
    e["cross_flag"] = cross_flag(item, j, d)
    return e


def situational_judge(part: Dict[str, Any], questions: Dict[str, Any], jev: Any, decisions: Any,
                      cross: bool) -> Tuple[str, Any, str, Optional[str], List[Tuple[Optional[str], Dict[str, Any]]], int]:
    """회의 흐름의 1차 심판 한 턴 — cross(단일 질문 · 첨부 없음 · Jev 가 받는 크기)면 Jev(2+1) + Decisions(1) 교차, 아니면 Decisions 1번.
    한쪽이 실패하면 다른 쪽 혼자(지금 선별 규칙). 돌려주는 값은 first_judge_votes 와 같은 모양 — 쓴 심판은 'jev+decisions' · 'decisions' · 'jev'."""
    if cross and jev is not None and decisions is not None:
        js_, jb, je, joks, jatt = jev_votes_adaptive(part, questions, jev) if JEV_VOTES > 1 else jev_once(part, questions, jev)
        ds_, db, de = decisions.judge(part, questions)
        voted: List[Tuple[Optional[str], Dict[str, Any]]] = [("jev", b) for b in joks] + ([("decisions", db)] if ds_ == "OK" and isinstance(db, dict) else [])
        attempts = jatt + 1
        if js_ == "OK" and ds_ == "OK" and isinstance(db, dict):
            ja, da = jb.get("answers") or {}, db.get("answers") or {}
            ans = {k: cross_entry(k.split(":", 1)[-1], ja.get(k), da.get(k)) for k in questions}
            return "OK", {"answers": ans, "votes": len(joks)}, "", "jev+decisions", voted, attempts
        if ds_ == "OK" and isinstance(db, dict):
            return "OK", db, f"Jev {je}"[:200], "decisions", voted, attempts
        if js_ == "OK":
            return "OK", jb, f"Decisions {ds_} {str(de)[:100]}", "jev", voted, attempts
        return "ERROR", None, f"Jev {je}; Decisions {ds_} {str(de)[:100]}", None, voted, attempts
    if decisions is not None:
        status, body, error = decisions.judge(part, questions)
        ok = status == "OK" and isinstance(body, dict)
        return status, (body if ok else None), ("" if ok else str(error)), "decisions", ([("decisions", body)] if ok else []), 1
    if jev is not None:   # Decisions 가 없을 때만 — Jev 혼자(큰 입력이면 실패)
        js_, jb, je, joks, jatt = jev_votes_adaptive(part, questions, jev) if JEV_VOTES > 1 else jev_once(part, questions, jev)
        return js_, jb, je, ("jev" if js_ == "OK" else None), [("jev", b) for b in joks], jatt
    return "ERROR", None, "1차 심판 없음", None, [], 0


def question_json(q: int, turns: List[Dict[str, Any]], answers: Dict[str, str], deliveries: Dict[str, str],
                  judge_turns: Optional[List[int]] = None) -> Dict[str, Any]:
    """심판에게 주는 질문 하나 — 질문·첨부파일·참조 자료·답변만(모델 이름·판정할 항목 없음). judge_turns: 이번에 판정할 턴(기본 모든 턴)."""
    out = []
    for row in turns:
        t: Dict[str, Any] = {"턴": ji._turn(row), "질문": row.get("question") or ""}
        files = [a for a in row.get("attachments") or [] if isinstance(a, dict)]
        if files:
            t["첨부파일"] = [{"이름": a.get("name"), "종류": a.get("type"), "내용": a.get("content") or "(내용 없음)",
                           **({"경로": a.get("path")} if a.get("path") else {})} for a in files]   # 경로: Decisions 가 이미지 원본을 붙일 때 씀
        if isinstance(row.get("reference"), str) and row["reference"].strip():
            t["참조자료"] = row["reference"]
        if deliveries.get(row["case_id"]):
            t["전달방식"] = deliveries[row["case_id"]]
        t["입력_추정토큰"] = input_tokens(row)
        plan = TURN_PLAN.get((q, ji._turn(row)))
        if plan:   # 질문을 만들 때 정한 그 턴의 확인 목록 (10-22)
            t["확인_목록"] = {"상황": plan.get("situation"), "확인할_것": plan.get("checks") or [], "이어지는_조건": plan.get("keep") or [],
                           **({"정한_판정": plan["decided"]} if plan.get("decided") else {})}   # 사용자가 정한 이 질문의 판정 기준 (10-22)
        t["답변"] = answers[row["case_id"]]
        out.append(t)
    return {"질문번호": q, "턴_수": len(out), "판정할_턴": judge_turns or [t["턴"] for t in out], "턴": out}


def output_schema() -> Dict[str, Any]:
    """Codex --output-schema — v3 의 항목 모양 그대로 + 턴마다 situations + 질문 결론."""
    base = js.gpt_output_schema()
    item = base["properties"]["turns"]["items"]["properties"]["items"]["items"]
    situation = {"type": "object", "additionalProperties": False, "required": ["id", "evidence", "quote_turn"],
                 "properties": {"id": {"type": "string"}, "evidence": {"type": "string"}, "quote_turn": {"type": ["integer", "null"]}}}
    turn = {"type": "object", "additionalProperties": False, "required": ["turn", "situations", "items"],
            "properties": {"turn": {"type": "integer"}, "situations": {"type": "array", "items": situation},
                           "items": {"type": "array", "items": item}}}
    conclusion = {"type": "object", "additionalProperties": False, "required": ["verdict", "reason"],
                  "properties": {"verdict": {"type": "string", "enum": ["PASS", "FAIL", "HOLD"]}, "reason": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False, "required": ["turns", "conclusion"],
            "properties": {"turns": {"type": "array", "items": turn}, "conclusion": conclusion}}


# --- 심판 출력 읽기 ---------------------------------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    from code_eval.llm_client import env_value
    try:
        return float(env_value(name) or default)
    except ValueError:
        return default


#: 둘째 심판의 FAIL 이 Codex 의 PASS 를 보류로 끌어내리려면 FAIL 확률이 이 값 이상이어야 한다 (사용자 결정 2026-10-20, .env SECOND_JUDGE_CONFIDENT) — 시험하며 바꾼다
SECOND_JUDGE_CONFIDENT = _env_float("SECOND_JUDGE_CONFIDENT", 0.7)
#: 선별 판정(B, 2026-10-20): Decisions 전수 판정 뒤 Codex 에 넘기는 조건 — FAIL 확률 이 값 이상 또는 확신(confidence) 이 값 미만
SCREEN_FAIL_PROB = _env_float("SCREEN_FAIL_PROB", 0.3)
SCREEN_CONFIDENT = _env_float("SCREEN_CONFIDENT", 0.7)
#: 1차 심판 투표 수 (사용자 10-25 "3번 돌려"): 같은 턴을 이 수만큼 판정해 항목마다 다수결(동률이면 평균 확률이 높은 값), 확률 · 확신은 평균,
#: 표(votes)는 항목에 남긴다. 1 이면 전과 같음. .env FIRST_JUDGE_VOTES 또는 --votes. 근거: 같은 규칙 두 판정에서 Decisions 15% · Jev 0.8% 흔들림(10-25)
FIRST_JUDGE_VOTES = max(1, int(_env_float("FIRST_JUDGE_VOTES", 3)))


def _env_str(name: str, default: str) -> str:
    from code_eval.llm_client import env_value
    return (env_value(name) or default).strip().lower()


#: 1차 심판 흐름 — stances(기본, 사용자 10-26 저녁): 크기로 Jev 또는 Decisions(auto 와 같은 나눔), 판정 프롬프트 3종(STANCES_PATH)으로 한 번씩 3표 →
#:   가중 다수결(stance_merge). 같은 프롬프트를 되풀이하면 캐시로 같은 답이 와 표가 되지 않는다(10-26: Decisions 10,526칸 3번 100% 같음).
#: situational(10-23 회의 흐름 · 사용자 10-26 낮): 상황으로 첫 심판을 정하고 결과로 다음 단계.
#:   단일 질문(그 턴에 첨부 없음 · 질문 전체 JEV_MAX_TOKENS 이하)은 Jev(2번, 갈린 항목이 있으면 3번째) + Decisions(1번) 교차 —
#:   같은 답이고 둘 다 CROSS_SURE 이상이면 끝, 다르거나 애매하거나 FAIL 이면 Codex. 멀티턴 · 첨부 · 롱컨텍스트는 Decisions 1번 + 선별 규칙(screen_flag).
#:   auto: 옛 흐름(질문 크기로 Jev · Decisions 하나 + FIRST_JUDGE_VOTES 표) · jev · decisions: 모든 턴을 한 심판으로(심판 비교 실험).
#:   .env FIRST_JUDGE_MODE 또는 --first-judge
FIRST_JUDGE_MODE = _env_str("FIRST_JUDGE_MODE", "stances")
#: 교차 확인에서 Jev 를 몇 번 부를지 — 1(기본, 사용자 10-26): 한 번. 3: 2번 부르고 갈린 항목이 있으면 3번째(다수결).
#: 10-26 비교: 단일 질문 2,356칸 중 첫 표와 3표 다수결이 다른 칸 2개, 둘 다 어느 쪽이든 Codex 로 감 — 반복이 결과를 바꾸지 않음. .env JEV_VOTES
JEV_VOTES = max(1, int(_env_float("JEV_VOTES", 1)))
#: 교차 확인의 '확실' — 고른 답의 확률이 이 값 이상(사용자 10-26: 0.8 — 모의 계산 놓친 FAIL 1 · Codex 646칸, 0.7 이면 6 · 487칸). .env CROSS_SURE
CROSS_SURE = _env_float("CROSS_SURE", 0.8)
#: 1차 심판만 — Codex 를 부르지 않고 1차 판정(decisions/<질문>.json)만 남긴다 (--first-only, 심판 비교 실험 10-26)
FIRST_ONLY = False
#: Decisions 가 '해당 없음'이라 하고 FAIL 확률 < SCREEN_FAIL_PROB 이면 확신이 낮아도 Codex 에 안 보낸다 — 실행 a1642702fccc 전체(걸린 12,967개)에서
#: 이 묶음 4,954개 중 Codex FAIL 은 55개(1%)였고 그것을 빼도 질문 판정은 하나도 안 바뀌었다. .env SCREEN_SKIP_NA_LOWCONF=0 이면 옛 규칙.
SCREEN_SKIP_NA_LOWCONF = _env_float("SCREEN_SKIP_NA_LOWCONF", 1) != 0
#: 질문별 목표 기준만 판정 (사용자 10-21 저녁) — 질문 세트 target_items 의 의미 기준. 0 이면 예전처럼 44개 전부를 심판이 골라 판정
JUDGE_TARGET_ONLY = _env_float("JUDGE_TARGET_ONLY", 1) != 0
#: --repair 에서 입력 지문이 아예 없는 옛 저장(10-22 전)을 믿을지 — 1(기본)이면 지금까지처럼 빠진 항목만 묻고, 0 이면 그 턴을 다시 판정
#: (옛 저장은 그 뒤 바뀐 항목 정의 · 프롬프트로 판정했는지 확인할 수 없다 — 비용과 정확도 사이 선택, Codex 검토 10-22)
REPAIR_TRUST_OLD = _env_float("JUDGE_REPAIR_TRUST_OLD", 1) != 0
#: 질문 세트의 첨부 원본 폴더 — main 이 정한다. 심판 입력 지문에 원본 파일 내용을 넣을 때 쓴다
FILES_DIR: Optional[Path] = None
#: 지문 없는 옛 저장을 믿고 쓴 항목의 표시 — 정의 지문 자리에 남겨 다음 복구에서도 REPAIR_TRUST_OLD 로 정한다(확인한 지문과 구별)
TRUSTED_OLD = "trusted-old"
#: 질문으로 적용 조건이 정해지는 항목 — 목표 기준인데 Decisions 가 해당 없음이라 하면 확신과 상관없이 Codex 가 다시 본다 (Codex 검토 10-21의 26개)
QUESTION_DETERMINED = frozenset({"J2-1.a", "J2-1.b", "J2-2.a", "J2-3.a", "J2-4.a", "J2-5.a", "J2-5.b",
                                 "J3-3.a", "J3-3.b", "J3-4.a", "J3-5.a", "J3-5.b", "J4-1.a", "J4-1.b", "J4-2.a", "J4-3.a", "J4-4.a",   # J4 는 10-22 밤(빠져 있던 것)
                                 "J5-2.b", "J6-1.a", "J6-2.b", "J6-2.c", "J6-3.a",
                                 "J6-3.b", "J6-4.a", "J6-4.b", "J7-1.a", "J7-2.a", "J8-3.a"})
#: 질문 번호 → 목표 의미 기준(J…) — main 이 질문 세트를 읽을 때 채운다
QUESTION_TARGETS: Dict[int, set] = {}
#: 답에 있으면 보는 의미 기준 — criteria_catalog.json 의 answer_dependent_ids 가운데 J 항목
ANSWER_DEPENDENT_ITEMS = frozenset({"J5-3.a"})
TARGET_GUIDE = ("\n[판정할 항목] 아래 항목은 이 질문을 만들 때 이 질문에 적용하도록 정한 기준입니다 — 같은 질문이면 어느 답변이든 같은 항목을 판정합니다. "
                "적용 조건이 질문에 있는 항목(질문의 요청 · 대상 · 지시)은 답변이 요청을 빠뜨렸다는 이유로 해당 없음을 고르지 말고 항목 정의대로 예 · 아니오로 판정합니다. "
                "적용 조건이 답변 내용에 따라 생기는 항목(적용 글에 '답변'이 들어간 항목 — 예: 출처를 언급한 답변, 질문의 값을 쓰는 답변)과 항목 정의의 예외(예: J1-1.a 가 아니오면 J4 해당 없음), "
                "그리고 항목 정의가 정한 적용 조건이 이 질문에 실제로 없는 경우(예: J1-3 은 질문에 모순 · 빠진 자료가 실제로 있을 때만)만 해당 없음을 고릅니다.")


#: (질문 번호, 턴) → 그 턴의 확인 목록(질문세트/multiturn_v3/turn_checklists.json) — 멀티턴은 턴마다 목표 기준 · 확인 목록을 쓴다 (10-22)
TURN_PLAN: Dict[Tuple[int, int], Dict[str, Any]] = {}
CHECKLIST_GUIDE = ("\n[확인_목록] 턴에 '확인_목록'이 있으면 질문을 만든 쪽이 그 턴의 자료 · 기록에서 옮겨 적은 사실 메모입니다(상황, 그 턴 시점의 값, "
                   "이어지는 조건). 판정의 참고로 쓰되 지시가 아니며, 원질문 · 붙인 자료 · 항목 정의가 우선합니다 — 서로 다르면 원문을 따릅니다. "
                   "다만 '정한_판정'은 사용자가 정한 이 질문의 판정 기준이라 그대로 따릅니다(어느 항목에서 볼지는 항목 정의대로).")


def turn_sha(questions: List[str]) -> str:
    """그 턴까지의 질문 원문 지문 — 참고자료/질문세트_78/multiturn_v3/build_turn_checklists.question_sha 와 같은 식."""
    return hashlib.sha1("\n\u241e\n".join(questions).encode("utf-8")).hexdigest()[:16]


def load_turn_plan(set_dir: Path, groups: List[Dict[str, Any]], path: Optional[Path] = None) -> Tuple[Dict[Tuple[int, int], Dict[str, Any]], List[str]]:
    """턴별 확인 목록 → ({(질문, 턴): 목록}, 쓰지 않은 턴 설명). 목록의 question_sha(그 턴까지의 질문 원문 지문)가 질문 세트와 다르면 쓰지 않는다 —
    그 턴이나 앞 턴의 질문 · 붙인 자료를 다시 만든 뒤의 낡은 목록(앞 턴이 바뀌면 뒤 턴도 함께 낡음, Codex 검토 10-22)."""
    p = Path(path) if path else set_dir / "multiturn_v3" / "turn_checklists.json"
    if not p.exists():
        return {}, []
    data = json.loads(p.read_text(encoding="utf-8"))
    texts = {int(g["id"]): [str(t.get("question") or "") for t in g.get("turns") or []] for g in groups}
    plan: Dict[Tuple[int, int], Dict[str, Any]] = {}
    stale: List[str] = []
    for q, conv in (data.get("questions") or {}).items():
        for t in conv.get("turns") or []:
            key = (int(q), int(t["turn"]))
            qs = texts.get(key[0]) or []
            if key[1] <= len(qs) and t.get("question_sha") and t["question_sha"] == turn_sha(qs[:key[1]]):
                plan[key] = t
            else:
                stale.append(f"Q{key[0]} T{key[1]}")
    return plan, stale


def files_fp(turns: List[Dict[str, Any]]) -> Dict[str, str]:
    """그 턴들의 첨부 원본 내용 지문 {경로: sha1 앞 16자 · 'missing'} + 쪽 이미지 전달 설정 — 원본 파일이 바뀌거나 pdf · docx 쪽 이미지를
    붙이는지가 바뀌면 저장된 판정을 쓰지 않게 (Codex 검토 10-22). 첨부가 없으면 빈 값."""
    out: Dict[str, str] = {}
    for row in turns:
        for a in row.get("attachments") or []:
            if isinstance(a, dict):
                rel = str(a.get("path") or a.get("name") or "")
                p = Path(FILES_DIR) / rel if FILES_DIR else None
                out[rel] = hashlib.sha1(p.read_bytes()).hexdigest()[:16] if p is not None and p.is_file() else "missing"
    if out:
        from code_eval import attachment_pages as ap
        out["<쪽 이미지>"] = "on" if ap.enabled() else "off"
    return out


def doc_fp(obj: Any) -> str:
    """심판 입력 지문 — 저장된 판정을 다시 쓸지 정할 때 (Codex 검토 10-22)."""
    return hashlib.sha1(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def targets_for(q: int, n: int) -> Optional[set]:
    """그 턴의 목표 의미 기준 — 턴별 확인 목록(질문 원문 지문으로 낡지 않은 것만 실림)이 그 턴의 target_items 를 주면 그것을 질문 목표 안에서만
    쓴다(10-23, 사용자: 턴마다 보는 기준을 그 턴의 요청 · 함정 기준으로 — 턴당 34개 → 14~21개). 목록에 목표가 없으면 질문 목표에서 흐름상
    해당 없음이 확실한 J6(not_applicable)만 뺀다. 질문 목표 밖의 항목은 쓰지 않는다 — 목표가 바뀌어도 낡은 목록의 목표를 쓰지 않게 (Codex 검토 10-22)."""
    base = QUESTION_TARGETS.get(q)
    t = TURN_PLAN.get((q, n))
    if base is None or t is None:
        return base
    plan_j = {str(x).split(":")[0] for x in t.get("target_items") or [] if str(x).startswith("J")}
    if plan_j:
        return plan_j & base
    return base - set(t.get("not_applicable") or {})


def question_targets(groups: List[Dict[str, Any]]) -> Dict[int, set]:
    """질문 세트의 target_items 에서 의미 기준(J…)만 — {질문 번호: {항목}}. 목표가 없는 질문은 넣지 않는다(그 질문은 44개 전부)."""
    out: Dict[int, set] = {}
    for g in groups:
        items = {str(t).split(":")[0] for t in g.get("target_items") or [] if str(t).startswith("J")}
        if items:
            out[int(g["id"])] = items
    return out
def judge_input_files(prompt: Optional[str], jev_questions: Optional[str], turn_checklists: Optional[str], set_dir: Path) -> Dict[str, Path]:
    """판정이 읽는 세 입력 파일 — 기본값 또는 기준 다듬기 루프가 준 임시 파일(--prompt · --jev-questions · --turn-checklists). 없는 파일은 바로 알린다."""
    files = {"prompt": Path(prompt) if prompt else PROMPT_V4, "jev_questions": Path(jev_questions) if jev_questions else JEV_V4,
             "turn_checklists": Path(turn_checklists) if turn_checklists else set_dir / "multiturn_v3" / "turn_checklists.json"}
    for k in ("prompt", "jev_questions"):
        if not files[k].is_file():
            raise SystemExit(f"판정 입력 파일이 없습니다 ({k}): {files[k]}")
    return files


def input_fingerprints(files: Dict[str, Path]) -> Dict[str, Any]:
    """summary.json 에 남기는 입력 파일 경로 · 내용 지문 — 어느 프롬프트 · 항목 정의 · 확인 목록으로 판정했는지 뒤에 알 수 있게 (10-25 밤)."""
    out: Dict[str, Any] = {}
    for k, p in files.items():
        out[k] = {"file": str(p), "sha": hashlib.sha1(p.read_bytes()).hexdigest()[:12] if p.is_file() else None}
    return out


def init_question_set(set_dir: Path, groups: List[Dict[str, Any]], turn_checklists: Optional[Path] = None) -> Tuple[Dict[Tuple[int, int], Dict[str, Any]], List[str]]:
    """질문 세트로 판정 준비 — 질문별 목표(QUESTION_TARGETS) · 턴별 확인 목록(TURN_PLAN) · 첨부 원본 폴더(FILES_DIR).
    판정(screen_one)을 부르는 모든 도구가 이 함수를 써서 같은 목표 · 확인 목록 · 지문을 쓴다 (Codex 검토 10-22). 돌려주는 값: (턴별 목록, 낡은 턴)."""
    global FILES_DIR
    QUESTION_TARGETS.clear()
    QUESTION_TARGETS.update(question_targets(groups))   # 질문별 목표 기준 (10-21 저녁)
    plan, stale = load_turn_plan(set_dir, groups, turn_checklists)   # 멀티턴 턴별 목표 기준 · 확인 목록 (10-22)
    TURN_PLAN.clear()
    TURN_PLAN.update(plan)
    FILES_DIR = set_dir / "files"   # 심판 입력 지문에 첨부 원본 내용을 넣을 때 (10-22)
    return plan, stale


#: 사실 확인 항목은 Decisions 가 적용된다고 보면(PASS/FAIL) 웹 검색이 있는 Codex 가 확인한다
SCREEN_ALWAYS_CODEX = ("J5-1.a",)


def fail_prob(second: Optional[Dict[str, Any]]) -> float:
    """둘째 심판이 낸 FAIL 확률 — probabilities 가 {값: 확률} 이든 [{value, probability}] 이든. 없으면 1.0(확신한 것으로 봄 — 옛 Jev 응답 호환)."""
    probs = (second or {}).get("probabilities")
    if isinstance(probs, dict) and probs:
        v = probs.get("FAIL")
        return float(v) if isinstance(v, (int, float)) else 0.0   # 확률표에 FAIL 이 없으면 FAIL 확률 0
    if isinstance(probs, list) and probs:
        for p in probs:
            if isinstance(p, dict) and p.get("value") == "FAIL" and isinstance(p.get("probability"), (int, float)):
                return float(p["probability"])
        return 0.0
    return 1.0 if (second or {}).get("decision") == "FAIL" else 0.0


def combine_weighted(codex_v: Optional[Dict[str, Any]], second_v: Optional[Dict[str, Any]], label: str = "Jev",
                     confident: float = SECOND_JUDGE_CONFIDENT, jev_trimmed: bool = False) -> Tuple[Optional[str], str]:
    """0.7 규칙(사용자 결정 2026-10-20): Codex FAIL → FAIL · 둘째 심판 FAIL 이고 그 FAIL 확률 ≥ confident → 보류(심판 갈림) ·
    둘째 심판 FAIL 인데 확률 < confident → Codex 판정을 따르되 '약한 FAIL' 로 기록 · 그 밖은 Codex. 줄인 자료로 본 둘째 심판은 참고만."""
    c = (codex_v or {}).get("decision")
    j = (second_v or {}).get("decision")
    p = fail_prob(second_v) if j == "FAIL" else 0.0
    if c == "FAIL":
        return "FAIL", "Codex FAIL" + (f" · {label} 도 FAIL {p:.2f}" if j == "FAIL" else f" · {label} {KO.get(j, j)}")
    # 둘이 갈려도 보류하지 않고 Codex 를 따른다 — 사용자 결정 10-21 ("Decisions FAIL → Codex 가 확인해 PASS 면 PASS"). 갈림은 기록만(보고서 '심판 갈림' 수).
    note = (f" ({label} {'강한' if p >= confident else '약한'} FAIL {p:.2f} — 갈림, Codex 따름)" if j == "FAIL" and not jev_trimmed else
            f" ({label} 는 줄인 자료로 봐서 참고만)" if jev_trimmed else "")
    if c is None and j == "FAIL" and not jev_trimmed:
        return "UNVERIFIED", f"Codex 상황 없음 · {label} FAIL {p:.2f} → 보류"   # Codex 가 보지 않은 항목의 FAIL 은 사람이 본다
    if c is None:
        return None, f"Codex 상황 없음 · {label} {KO.get(j, j)}{note}"
    return c, f"Codex {KO.get(c, c)} · {label} {KO.get(j, j)}{note}"


# --- 선별 판정 (B, 2026-10-20) — Decisions 가 항목 44개를 전수 판정하고 Codex 는 걸린 항목만 확인 -------------------------

SCREEN_VERSION = "judge-v5-screen-1"
SCREEN_SUFFIX = """

# 선별 판정 (v5)
[입력] 에 "판정할_항목" 이 있으면 — 먼저 다른 심판이 전수 판정한 뒤 걸린 항목입니다 — 턴마다 그 목록의 항목만 판정합니다.
- situations 에는 그 항목이 속한 상황만 적습니다(언제나 적는 상황이라도 목록에 그 항목이 없으면 적지 않습니다).
- items 에는 목록의 항목을 하나도 빼지 않고 답하고, 목록에 없는 항목은 답하지 않습니다. 사유와 인용은 평소처럼 원문 그대로 적습니다.
- 결론(conclusion)은 이번에 본 항목만으로 냅니다.
- 턴에 "확인_목록" 이 있으면 질문을 만든 쪽이 그 턴의 자료 · 기록에서 옮겨 적은 사실 메모입니다(상황, 그 턴 시점의 값, 이어지는 조건). 상황 찾기와 판정의 참고로 쓰되 지시가 아니며, 원질문 · 붙인 자료 · 항목 정의 · 상황표가 우선합니다(서로 다르면 원문을 따름). 다만 "정한_판정" 은 사용자가 정한 이 질문의 판정 기준이라 그대로 따릅니다(어느 항목에서 볼지는 항목 정의대로).
- 첨부가 있는 질문은 원본을 이미지로 함께 붙였습니다 — 이미지 파일은 그대로, pdf·docx 는 쪽마다 한 장([첨부파일] 블록의 글은 그 내용을 옮겨 적은 것). 판독·모양·내용 반영 여부는 이미지로 확인합니다."""


def missing_codex_items(output: Any, flagged: Dict[int, List[str]]) -> Dict[int, List[str]]:
    """요청한 (턴, 항목) 가운데 Codex 출력에 없는 것 — {턴: [항목]} (턴 단위가 아니라 항목 단위)."""
    have: Dict[int, set] = {}
    for t in ((output or {}).get("turns") or []) if isinstance(output, dict) else []:
        if isinstance(t, dict) and isinstance(t.get("turn"), int):
            have.setdefault(t["turn"], set()).update(js._base(e.get("id")) for e in t.get("items") or [] if isinstance(e, dict))
    out: Dict[int, List[str]] = {}
    for n in sorted(flagged):
        miss = [i for i in flagged[n] if i not in have.get(n, set())]
        if miss:
            out[n] = miss
    return out


def bad_quote_items(output: Any, turns: List[Dict[str, Any]], answers: Dict[str, str], flagged: Dict[int, List[str]]) -> Dict[Tuple[int, str], List[str]]:
    """인용이 원문에 없는 (턴, 항목) → 문제 목록. 판정 없음은 missing_codex_items 가 맡는다."""
    out: Dict[Tuple[int, str], List[str]] = {}
    for n, items in js.check_gpt(output, turns, answers, flagged).items():
        for i, v in items.items():
            quote_probs = [p for p in (v.get("problems") or []) + (v.get("notes") or []) if "원문에 없음" in p]
            if quote_probs:
                out[(n, i)] = quote_probs
    return out


def quote_locations(output: Any, turns: List[Dict[str, Any]], answers: Dict[str, str], keys: List[Tuple[int, str]]) -> Dict[str, str]:
    """인용 불일치로 다시 묻는 (턴, 항목)의 인용이 실제로 어디(다른 턴의 질문 · 답변)에 있는지 — 다시 묻을 때 알려 준다
    (10-25 밤 보류 분석: 인용 불일치 6칸이 뒤 턴 답 · 이 턴 답 · 다른 턴 질문에서 따온 것)."""
    rows = sorted(turns, key=ji._turn)
    out: Dict[str, str] = {}
    want = set(keys)
    for t in (output or {}).get("turns") or [] if isinstance(output, dict) else []:
        if not isinstance(t, dict):
            continue
        n = t.get("turn")
        for e in t.get("items") or []:
            if not isinstance(e, dict) or (n, js._base(e.get("id"))) not in want:
                continue
            for field, own in (("answer_quote", "답변"), ("question_quote", "질문")):
                q = str(e.get(field) or "").strip()
                if not q:
                    continue
                hits = [f"T{ji._turn(r)} {kind}" for r in rows for kind, text in (("질문", r.get("question") or ""), ("답변", answers.get(r["case_id"], "")))
                        if not (ji._turn(r) == n and kind == own) and js.quoted_in(q, [text])]
                if hits and not (field == "answer_quote" and f"T{n} 답변" in hits):
                    out[f"T{n}:{e.get('id')}"] = (f"{field} 의 글은 {' · '.join(hits[:3])}에 있습니다 — answer_quote 는 T{n} 답변에서만, "
                                                  f"question_quote 는 T{n} 까지의 질문(앞 턴이면 quote_turn)에서만 글자 그대로 인용하세요")
    return out


QUOTE_SCOPE_NOTE = ("인용 범위: answer_quote 는 판정하는 그 턴의 답변에서만(앞 턴 · 뒤 턴 답변 금지), question_quote 는 그 턴까지의 질문 · 자료에서만"
                    "(답변 문장 · 뒤 턴 질문 금지, 앞 턴이면 quote_turn) 글자 그대로 복사합니다. 바꿔 쓴 문장은 인용이 아닙니다 — '인용_위치' 에 그 글이 실제로 있는 곳을 적었습니다.")


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def attachment_images(turns: List[Dict[str, Any]], files_dir: Optional[Path]) -> List[Path]:
    """질문 첨부의 원본 이미지 — 이미지 파일은 그대로, pdf·docx 는 쪽 이미지(code_eval.attachment_pages) — Codex 에 -i 로 붙인다 (사용자 10-21).
    CODEX_ATTACH_PAGES=0 이면 이미지 파일만."""
    if not files_dir:
        return []
    from code_eval import attachment_pages as ap
    out: List[Path] = []
    for row in turns:
        for a in row.get("attachments") or []:
            if not isinstance(a, dict):
                continue
            p = Path(files_dir) / str(a.get("path") or a.get("name") or "")
            if not p.is_file():
                continue
            pages = [p] if p.suffix.lower() in IMAGE_EXT else (ap.page_images(p, Path(files_dir))[0] if ap.enabled() else [])
            out += [x for x in pages if x not in out]
    return out


def unverified_fact_items(output: Any, turns: List[Dict[str, Any]], answers: Dict[str, str], flagged: Dict[int, List[str]]) -> List[Tuple[int, str]]:
    """Codex 가 '확인 못함'(UNVERIFIED)이라 한 J5-1.a, 또는 반박 출처 없이 FAIL 한 J5-1.a — 형식 문제 없이 그렇게 답한 것만.
    출처 없는 FAIL 은 안전장치가 어차피 보류로 돌리므로 일반 지식인지 한 번 다시 묻는다 (10-25 밤 보류 분석: 출처 없는 FAIL 6칸이 모두 교과서 정의)."""
    out: List[Tuple[int, str]] = []
    for n, items in js.check_gpt(output, turns, answers, flagged).items():
        v = items.get("J5-1.a")
        if not v or v.get("problems"):
            continue
        fails = [e for e in v.get("entries") or [] if isinstance(e, dict) and e.get("decision") == "FAIL"]
        if v.get("decision") == "UNVERIFIED" or (fails and not any(backed_fail(e) for e in fails)):
            out.append((n, "J5-1.a"))
    return out


def undecided_items(output: Any, turns: List[Dict[str, Any]], answers: Dict[str, str], flagged: Dict[int, List[str]]) -> List[Tuple[int, str]]:
    """Codex 가 J5-1.a 가 아닌 항목에 UNVERIFIED('판단 못 함')를 쓴 (턴, 항목) — 그 항목은 PASS · FAIL · 해당 없음 중 하나여야 한다 (10-21 밤: 보류 9건)."""
    out: List[Tuple[int, str]] = []
    for n, items in js.check_gpt(output, turns, answers, flagged).items():
        for i, v in items.items():
            if i != "J5-1.a" and v and v.get("decision") == "UNVERIFIED" and not v.get("problems"):
                out.append((n, i))
    return out


#: Codex 가 FAIL 로 본 것을 정의의 예외를 짚어 한 번 다시 묻는 항목 — J1-4 는 기본(사용자 10-22: J1-4 가 정의보다 엄하게 매겨짐, '이미 적용'),
#: --reask-fail 로 더할 수 있다. 10-22 밤 다시 판정(v4-r2) 때 옵션을 빠뜨려 J1-4.a FAIL 이 14 → 79 로 부풀어 기본값으로 올림
REASK_FAIL_ITEMS: set = {"J1-4.a", "J1-4.b"}
REASK_NOTE = ("J1-4 를 다시 묻습니다: J1-4.a 는 질문과 무관한 주제 · 활용법 · 일반론 단락만 FAIL 이고, 같은 내용의 반복은 J1-4.a 가 아니라 J1-4.b 에서만 봅니다. "
              "J1-4.b 에서 '표와 그 표를 풀어 쓴 설명', '결론 · 정리 한 번', '질문이 요구한 대상 · 구분에 맞춘 다시 정리' 는 반복이 아닙니다 — "
              "같은 문장 · 같은 목록을 새 관점 · 새 정보 없이 별도 항목 · 단락으로 두 번 쓴 경우만 FAIL 입니다. 이 예외에 해당하면 PASS 로 정하고, "
              "FAIL 을 유지하면 반복된 두 구간(또는 무관한 단락)을 answer_quote 로 정확히 옮기세요.")


def fail_items(output: Any, turns: List[Dict[str, Any]], answers: Dict[str, str], flagged: Dict[int, List[str]], ids: set) -> List[Tuple[int, str]]:
    """Codex 가 FAIL 로 본 (턴, 항목) 가운데 ids 에 든 것 — --reask-fail 다시 묻기 대상."""
    if not ids:
        return []
    return [(n, i) for n, items in js.check_gpt(output, turns, answers, flagged).items() for i, v in items.items()
            if i in ids and v and v.get("decision") == "FAIL"]


WEB_SEARCH_ITEMS = frozenset({"J5-1.a", "J5-3.a"})   # Codex 웹 검색을 켜는 항목 (10-21 밤)
FACT_NOTE = ("J5-1.a 를 다시 묻습니다: 주장에 실제 세상의 고유한 대상(기관 · 인물 · 제품 · 법령 · 통계 · 날짜 · 가격 · 지명)이나 정확한 수치가 쟁점이 아니면 "
             "교과서 · 사전 수준의 일반 지식으로 PASS(반박 출처가 있으면 FAIL)로 정하세요. UNVERIFIED 를 유지하려면 쟁점인 고유 대상 · 수치를 reason 에 적으세요. "
             "질문의 자료 · 첨부는 평가용 가상 자료라 검색하지 않습니다. FAIL 은 검색으로 찾은 반박 출처를 fact.sources 에 넣을 때만 씁니다 — "
             "교과서 · 사전 수준의 통용 정의(발효/부패 · 양력/항력 · 음정/음량 · 이슬/서리)를 더 엄밀한 전문 정의가 있다는 이유로 FAIL 하지 않습니다. "
             "fact.verdict 가 TRUE 인 주장은 FAIL 로 적지 않습니다.")   # 10-21 밤: 프롬프트를 고쳐도 강화·처벌 정의를 보류한 일
UNDECIDED_NOTE = "UNVERIFIED 는 J5-1.a(사실 확인)에서만 씁니다. 다시 묻는 항목은 정의의 점검 질문으로 PASS · FAIL · NOT_APPLICABLE 중 하나로 정하세요 — 사유가 '아니오'면 FAIL, '예'면 PASS."


def merge_codex_output(base: Any, extra: Any, replace: Optional[set] = None) -> Dict[str, Any]:
    """다시 물은 출력을 앞 출력에 합친다 — 같은 턴에는 항목을 더하고(replace 에 든 (턴, 항목)은 새 답으로 바꿈), 없던 턴은 붙인다."""
    base = dict(base) if isinstance(base, dict) else {"turns": [], "conclusion": None}
    replace = replace or set()
    turns: Dict[int, Dict[str, Any]] = {t["turn"]: dict(t) for t in base.get("turns") or [] if isinstance(t, dict) and isinstance(t.get("turn"), int)}
    for t in ((extra or {}).get("turns") or []) if isinstance(extra, dict) else []:
        if not (isinstance(t, dict) and isinstance(t.get("turn"), int)):
            continue
        n = t["turn"]
        cur = turns.setdefault(n, {"turn": n, "situations": [], "items": []})
        old_items = [e for e in cur.get("items") or [] if isinstance(e, dict)]
        new_items = [e for e in t.get("items") or [] if isinstance(e, dict)]
        new_ids = {str(e.get("id")) for e in new_items}
        old_elem_ids: Dict[str, set] = {}
        for e in old_items:
            if "#" in str(e.get("id")):
                old_elem_ids.setdefault(js._base(e.get("id")), set()).add(str(e.get("id")))
        # 다시 물은 항목은 새 답으로 바꾸되, 앞 답이 펼친 요소(#번호)였으면 그 번호가 모두 돌아온 답만 받는다 — 일부 요소만 오거나 번호 없이
        # 하나로 오면 어느 요소의 답인지 코드로 맞출 수 없어 앞 답을 그대로 둔다(번호가 바뀐 일부 답이 정상 FAIL 을 지워 PASS 가 되던 것, Codex 검토 10-22)
        rep = {b for (m, b) in replace if m == n and old_elem_ids.get(b, set()) <= new_ids}
        kept = [e for e in old_items if js._base(e.get("id")) not in rep]
        have = {js._base(e.get("id")) for e in kept}
        cur["items"] = kept + [e for e in new_items if js._base(e.get("id")) not in have or js._base(e.get("id")) in rep]
        sids = {s.get("id") for s in cur.get("situations") or [] if isinstance(s, dict)}
        cur["situations"] = list(cur.get("situations") or []) + [s for s in t.get("situations") or [] if isinstance(s, dict) and s.get("id") not in sids]
    base["turns"] = [turns[n] for n in sorted(turns)]
    if base.get("conclusion") is None:
        base["conclusion"] = (extra or {}).get("conclusion") if isinstance(extra, dict) else None
    return base


_PROMPT_ITEM_CACHE: Dict[str, Dict[str, Any]] = {}


def prompt_item_lines(item_id: str) -> Dict[str, Any]:
    """판정 프롬프트(judge_prompt_v4.txt, Codex 가 읽는 글)의 그 항목 블록 — {'적용': 글 또는 None, 'notes': ['초보자: …', …]}.
    Decisions · Jev 가 Codex 와 같은 기준 문구를 보게 한다 (10-21: 초보자 · 외부 사실만 · 존댓말 설명이 Codex 에만 들어가 있었음)."""
    if not _PROMPT_ITEM_CACHE:
        cur = None
        for line in PROMPT_V4.read_text(encoding="utf-8").splitlines():
            m = re.match(r"- \*\*(J\d-\d+\.[a-z])\*\* · ", line)
            if m:
                cur = _PROMPT_ITEM_CACHE.setdefault(m.group(1), {"적용": None, "notes": []})
                continue
            if cur is None or not line.startswith("  · "):
                cur = None if not line.startswith("  · ") else cur
                continue
            key, _, val = line[4:].partition(":")
            key = key.strip()
            if key == "적용":
                cur["적용"] = val.strip()
            elif key in ("근거", "표시") or key.endswith(" 예") or key.startswith(("PASS", "FAIL")):
                continue
            else:
                cur["notes"].append(f"{key}: {val.strip()}")
    return _PROMPT_ITEM_CACHE.get(item_id) or {"적용": None, "notes": []}


def all_item_questions(fixed: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Decisions 에 보낼 항목 44개의 choice 질문 — J1~J4 는 Jev 묶음(fixed["items"]), J5~J8 은 semantic_items 정의로 같은 모양을 만들고,
    판정 프롬프트의 적용 조건(J5~J8) · 덧붙인 설명(전체)을 붙여 Codex 와 같은 기준 문구로 맞춘다 (10-21)."""
    out: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in fixed["items"].items()}
    for it in si.ITEMS:
        if it.id in out:
            continue
        sub = si.SUBS_BY_ID[it.sub]
        applies = prompt_item_lines(it.id)["적용"] or it.applies
        out[it.id] = {"type": "choice",
                      "instructions": f"[{it.id}] {sub.name} — {it.question} / 적용: {applies} / 근거: {it.evidence}",
                      "criteria": {"PASS": f"예 — {it.question}", "FAIL": f"아니오 — {it.question}에 어긋남",
                                   "NOT_APPLICABLE": f"해당 없음 — 이 턴의 질문에 적용 조건이 없음 (적용: {applies})"}}
    for k, q in out.items():
        notes = prompt_item_lines(k)["notes"]
        if notes:
            q["instructions"] = str(q.get("instructions") or k) + " / " + " / ".join(notes)
    return out


def conversation_is_multi_turn(turns: List[Dict[str, Any]], answers: Dict[str, str]) -> Optional[bool]:
    """그 모델이 실제로 주고받은 대화가 멀티턴인가 — 한 턴(질문 + 답변) 10K 토큰 이상인 턴 10개 이상 + 대화 전체 100K 토큰 이상 (code_eval.multi_turn,
    10-21 밤). 모르면(토크나이저 없음) None — 그때는 J6 를 빼지 않는다."""
    m = multi_turn.measure_conversation([(ji._turn(r), r.get("question") or "", answers.get(r.get("case_id")) or "") for r in turns])
    return m.get("multi_turn")


def turn_item_ids(row: Dict[str, Any], n: int, all_ids: List[str], multi: Optional[bool] = True) -> List[str]:
    """입력으로 정해지는 조건(프롬프트 상황표 1): J8 은 첨부가 있는 턴만, J7 은 입력 272K 토큰 이상만, J6 은 멀티턴 대화(한 턴 10K 토큰
    이상인 턴 10개 이상 · 대화 전체 100K 토큰 이상)의 2턴부터 — 멀티턴이 아니면(multi=False) J6 를 판정하지 않는다."""
    has_files = bool(row.get("attachments"))
    long_input = (input_tokens(row) or 0) >= long_context.MIN_TOKENS
    out = []
    for i in all_ids:
        if i.startswith("J8") and not has_files:
            continue
        if i.startswith("J7") and not long_input:
            continue
        if i.startswith("J6") and (n < 2 or multi is False):
            continue
        out.append(i)
    return out


def screen_flag(item: str, entry: Optional[Dict[str, Any]], fail_prob_min: float = SCREEN_FAIL_PROB, confident: float = SCREEN_CONFIDENT) -> Tuple[bool, str]:
    """Decisions 답 하나를 Codex 에 넘길지 — (넘김?, 이유). 답이 없으면 넘긴다."""
    if not isinstance(entry, dict) or entry.get("choice") is None:
        return True, "Decisions 답 없음"
    choice = entry.get("choice")
    p = fail_prob({"decision": choice, "probabilities": entry.get("probabilities")})
    conf = entry.get("confidence")
    if choice == "FAIL":
        return True, f"Decisions FAIL {p:.2f}"
    if p >= fail_prob_min:
        return True, f"FAIL 확률 {p:.2f} ≥ {fail_prob_min}"
    if isinstance(conf, (int, float)) and conf < confident:
        if choice == "NOT_APPLICABLE" and SCREEN_SKIP_NA_LOWCONF:
            return False, ""
        return True, f"확신 {conf:.2f} < {confident}"
    if item in SCREEN_ALWAYS_CODEX and choice == "PASS":
        return True, "사실 확인은 Codex(웹 검색)가 봄"
    return False, ""


_NOT_EXTERNAL = re.compile(r"외부\s*사실(?:\s*확인)?\s*(?:항목|주장)?[이은]\s*아[니닙]|J5-2[.a-z]*\s*에서\s*(?:판단|본다|다룬)"
                           r"|자료\s*내\s*사실|입력값\s*기반|J5-1\s*적용\s*대상이\s*아니|외부\s*출처로\s*확인할\s*사실\s*주장이\s*아[니닙]|붙여\s*넣은\s*자료를\s*그대로"
                           r"|자료\s*사실이지만|제공된\s*기록의\s*운영\s*세부와\s*부합|회의록의\s*입력값|입력\s*수치로\s*산출|외부\s*사실\s*주장(?:은|이)?\s*(?:없|발견되지\s*않)"   # 10-21 밤: 보류 사례의 실제 사유 문구
                           r"|외부\s*사실(?:\s*주장)?(?:은|이|가)?\s*아니|입력\s*자료의\s*(?:수치|값)|자료에\s*제시된\s*(?:값|수치)")   # 10-22 — 아래에 한 번 더 정의돼 위 목록이 쓰이지 않던 것을 합침(기준 점검)

#: _NOT_EXTERNAL 이 잡은 말 뒤의 판단 보류('… 아니라고 단정할 수 없') · 부정('자료 내 사실이 아니라 …') — 이런 사유로는 해당 없음을 정하지 않는다 (Codex 검토 10-22)
_NOT_EXT_UNSURE = re.compile(r"^ ?(?:이|가|은|는|이라고|라고|로|으로)? ?(?:단정|확정|보기 ?어렵|볼 ?수 ?없|하기 ?어렵|할 ?수 ?없)")   # 외부 사실 여부 자체를 유보
_NOT_EXT_NEGATED = re.compile(r"^ ?(?:이|가|은|는|로|으로)? ?(?:아[니닙님]|볼 ?수 ?없|보기 ?어렵)|^ ?(?:과|와)(?:는|도)? ?(?:별개|다른|다르|무관|달리|구별)")


def not_external(reason: Any) -> bool:
    """사유가 '그 주장은 외부 사실이 아니다(입력 자료의 값이다)'라고 결론 내렸는가 — 뒤에 부정 · 판단 보류가 붙은 말은 뺀다."""
    r = re.sub(r"\s+", " ", str(reason or ""))
    for m in _NOT_EXTERNAL.finditer(r):
        tail = re.split(r"[.!?\n]", r[m.end():m.end() + 20], maxsplit=1)[0]
        if _NOT_EXT_UNSURE.match(tail):
            continue
        if not re.search(r"(?:아[니닙]|없|않)$", m.group(0)) and _NOT_EXT_NEGATED.match(tail):
            continue
        return True
    return False


def material_text(doc: Dict[str, Any], upto: int) -> str:
    """그 턴까지 질문 쪽 자료 — 질문 · 첨부 내용 · 참조 자료(답변은 넣지 않음: 앞선 AI 답변은 사실 근거가 아님)."""
    parts = []
    for t in doc.get("턴") or []:
        if int(t.get("턴") or 0) > upto:
            continue
        parts.append(str(t.get("질문") or ""))
        parts += [str(a.get("내용") or "") for a in t.get("첨부파일") or [] if isinstance(a, dict)]
        parts.append(str(t.get("참조자료") or ""))
    return re.sub(r"\s+", " ", "\n".join(parts))


def _quote_in(quote: Any, material: str) -> bool:
    """인용이 질문 쪽 자료에 그대로 있는가 — 강조 · 표 표시와 목록 글머리는 빼고, 공백은 하나로."""
    q = re.sub(r"[*_`#>|]+", "", str(quote or ""))
    q = re.sub(r"(?m)^\s*[-•·]\s+", "", q).strip()   # 목록 글머리는 빼고 대조 (10-21 밤)
    q = re.sub(r"\s+", " ", q)
    return len(q) >= 4 and q in material


def backed_fail(e: Dict[str, Any]) -> bool:
    """반박 출처까지 든 FAIL 주장인가 — 심판이 외부 사실로 보고 검색해 틀렸다고 확인한 것."""
    f = e.get("fact")
    return e.get("decision") == "FAIL" and isinstance(f, dict) and f.get("verdict") == "FALSE" and bool(f.get("sources"))


def drop_claims(it: Dict[str, Any], drop: Any, note: str) -> None:
    """J5-1.a 의 주장(펼친 요소)마다 drop 이 참인 것만 해당 없음으로 빼고, 남은 주장으로 항목 판정을 다시 모은다 — 사유 하나가 '자료 안의
    사실'이라고 해서 다른 외부 사실 주장의 FAIL 까지 지우지 않게 (Codex 검토 10-22). 펼친 요소가 없으면(대표 사유만) 예전처럼 항목 전체로."""
    entries = [e for e in it.get("entries") or [] if isinstance(e, dict)]
    if not entries:
        if drop(it):
            it["decision"] = "NOT_APPLICABLE"
            it["how"] = (it.get("how") or "") + note
        return
    problems = list(it.get("problems") or [])
    if it.get("decision") != ("UNVERIFIED" if problems else js.fold(entries)):
        return   # 항목 판정이 요소에서 온 것이 아니면(합치기 규칙 등) 건드리지 않는다
    gone = [e for e in entries if e.get("decision") != "NOT_APPLICABLE" and drop(e)]
    if not gone:
        return
    ids = {str(e.get("id")) for e in gone}
    for e in gone:
        e["dropped_from"], e["decision"] = e.get("decision"), "NOT_APPLICABLE"
    it["problems"] = [p for p in problems if str(p).split(":", 1)[0] not in ids]
    it["decision"] = "UNVERIFIED" if it["problems"] else js.fold(entries)
    live = [e for e in entries if not any(e is g for g in gone)]
    named = {str(p).split(":", 1)[0] for p in it["problems"]}
    # 대표 사유는 빼지 않은 주장에서 — 남은 문제를 낸 주장, 없으면 항목 판정과 같은 주장 (Codex 검토 10-22: 뺀 주장의 '일치합니다'로 보류가 풀리던 것)
    rep = (next((e for e in live if str(e.get("id")) in named), None) or next((e for e in live if e.get("decision") == it["decision"]), None)
           or (live or entries)[0])
    it["reason"], it["answer_quote"], it["question_quote"] = rep.get("reason"), rep.get("answer_quote"), rep.get("question_quote")
    it["how"] = (it.get("how") or "") + f"{note} — 주장 {len(gone)}/{len(entries)}개"


def material_fact_guard(items: Dict[str, Dict[str, Any]], doc: Dict[str, Any], n: int) -> None:
    """사실 확인(J5-1.a)이 '확인 못 함'인데 그 주장이 질문에 붙은 자료(질문 · 첨부 · 참조 자료 · 앞 턴 질문)에 그대로 있거나
    심판 사유가 '외부 사실이 아님'이면 해당 없음으로 — 자료 대조는 J5-2 가 한다 (사용자 10-21: Q40 '푸른자료실'). 주장마다 (10-22)."""
    it = items.get("J5-1.a")
    if not it or it.get("decision") != "UNVERIFIED":
        return
    material = material_text(doc, n)
    drop_claims(it, lambda e: not backed_fail(e) and (_quote_in(e.get("answer_quote"), material) or not_external(e.get("reason"))),
                " → 입력 자료 안의 사실(J5-1 해당 없음, 자료 대조는 J5-2)")


def fact_reason_na_guard(items: Dict[str, Dict[str, Any]]) -> None:
    """사실 확인(J5-1.a)의 FAIL · 보류인데 심판 사유가 스스로 '입력 자료의 값이라 외부 사실 주장이 아니다'라고 하면 그 주장은 해당 없음
    (10-22 보류 점검: Gemini Q1 10턴). 주장마다 — 다른 외부 사실 주장의 FAIL 은 남긴다 (Codex 검토 10-22)."""
    it = items.get("J5-1.a")
    if not it or it.get("decision") not in ("FAIL", "UNVERIFIED"):
        return
    drop_claims(it, lambda e: not backed_fail(e) and not_external(e.get("reason")), " → 사유가 '입력 자료의 값 · 외부 사실 아님' → 해당 없음")
    # 반박 출처까지 든 FAIL 인데 사유는 '외부 사실 아님' — 결론이 서로 반대라 그 주장은 근거에서 빼고 보류(사유 모순) (Codex 검토 10-22: 사유 문구만으로
    # 검색으로 확인한 오류를 해당 없음으로 지우지 않게)
    entries = [e for e in it.get("entries") or [] if isinstance(e, dict)]
    odd = [e for e in entries if backed_fail(e) and not_external(e.get("reason"))]
    if odd and it.get("decision") == ("UNVERIFIED" if it.get("problems") else js.fold(entries)):
        for e in odd:
            e["contradiction_from"], e["decision"] = "FAIL", "UNVERIFIED"
        it["decision"] = "UNVERIFIED" if it.get("problems") else js.fold(entries)
        if it["decision"] == "UNVERIFIED":
            it["kind"] = "사유 모순"
        it["how"] = (it.get("how") or "") + f" → 반박 출처가 있는데 사유는 '외부 사실 아님'(사유 모순) — 주장 {len(odd)}개 보류"


def fact_fail_guard(items: Dict[str, Dict[str, Any]]) -> None:
    """사실 확인(J5-1.a) FAIL 은 반박 출처가 있을 때만 — 출처 없이 '거짓'으로 본 주장뿐이면 '확인 못 함'으로 (10-21: 일반 지식만으로 꼬투리를 잡은 FAIL)."""
    it = items.get("J5-1.a")
    if not it or it.get("decision") != "FAIL":
        return
    entries = [e for e in it.get("entries") or [] if isinstance(e, dict)]
    # 사실 판정(fact.verdict)이 TRUE 인 주장의 FAIL 은 사유와 판정이 반대 — FAIL 근거에서 뺀다 (10-25 밤 보류 분석: GPT Q7 4턴 #3)
    contradicted = [e for e in entries if e.get("decision") == "FAIL" and isinstance(e.get("fact"), dict) and e["fact"].get("verdict") == "TRUE"]
    for e in contradicted:
        e["contradiction_from"], e["decision"] = "FAIL", "UNVERIFIED"
    fails = [e for e in entries if e.get("decision") == "FAIL"]
    # 지금 FAIL 인 주장마다 그 주장의 반박 출처로 — 해당 없음으로 뺀 주장의 출처가 다른 주장의 FAIL 을 받치지 않게 (Codex 검토 10-22)
    backed = [e for e in fails if isinstance(e.get("fact"), dict) and e["fact"].get("verdict") == "FALSE" and e["fact"].get("sources")]
    if backed:
        if contradicted:
            it["reason"], it["answer_quote"], it["question_quote"] = backed[0].get("reason"), backed[0].get("answer_quote"), backed[0].get("question_quote")
        return
    conf = float(it.get("decisions_confidence") or 0.0)
    if it.get("jev") == "PASS" and conf >= SECOND_JUDGE_CONFIDENT:
        # 반박 출처 없는 FAIL + 1차 심판(3표)이 확신한 PASS — 거짓은 출처로만이므로 FAIL 이 성립하지 않고, 두 번째 신호가 PASS (10-25 밤)
        it["decision"] = "PASS"
        it["how"] = (it.get("how") or "") + f" → 반박 출처 없음 · 1차 심판 PASS 확신 {conf:.2f} → PASS"
        return
    rep = (fails or contradicted or [{}])[0]   # 대표 사유는 출처 없는 FAIL 주장으로 (Codex 검토 10-22)
    if rep:
        it["reason"], it["answer_quote"], it["question_quote"] = rep.get("reason"), rep.get("answer_quote"), rep.get("question_quote")
    it["decision"] = "UNVERIFIED"
    it["kind"] = "사유 모순" if contradicted and not fails else "확인 못함"
    it["how"] = (it.get("how") or "") + (" → 사실 판정 TRUE 인 주장을 FAIL(사유 모순)" if contradicted and not fails else
                                         " → 반박 출처 없음(거짓은 검색 출처로만) → 확인 못 함")


#: 사유의 결론이 PASS 인 말. 강한 말(결론을 직접 말함)은 다시 물어도 남으면 보류, 약한 말(일부 사정만 말함)은 다시 묻기만 한다 — 자유 서술의
#: 부분 문자열만으로 판정을 바꾸지 않게 (Codex 검토 10-22: '모순은 없지만 … 빠져 있습니다' 를 결론으로 읽어 보류하던 것)
_OK_STRONG = re.compile(r"FAIL ?(?:항목)? ?없음|PASS ?(?:여야|이어야|입니다|가 ?맞|로 ?(?:정정|판정|처리|봅니다|본다|둡니다|봐야|보아야)|처리합니다)|판정은 ?PASS"
                        r"|이 ?항목은 ?PASS|FAIL ?로 ?보지 ?않|FAIL이 ?아닙니다"
                        r"|FAIL ?(?:을|로)? ?(?:유지할|둘|볼|낼) ?(?:근거|이유)가 ?없"
                        r"|FAIL ?(?:의 ?)?근거가 ?(?:되지 ?않|아닙)"
                        r"|FAIL ?(?:판정)?(?:은|을|를)? ?철회|(?:FAIL|모순|오류)(?:으로|로) ?판정할 ?(?:오류|문제|근거|이유|것)(?:는|은|이|가)? ?없")   # 10-26 다듬기 J3-1.a: 'FAIL 판정은 철회합니다' · '내부 모순으로 판정할 오류는 없습니다' 인데 FAIL   # 10-26 1차 심판 비교 Claude Q53 J5-2.a '… FAIL 근거가 되지 않습니다' 인데 FAIL   # 10-25 밤 v4-r9-chk Gemini Q6 10턴 J3-5.a 'FAIL을 유지할 근거가 없습니다'    # 10-22 기준 점검: 'J3-1은 PASS여야 합니다' 등 15건
#: 모순 항목(J3-1.a · J6-4.a)의 '모순 아님' — 10-26 다듬기 J3-1.a: '모순되는 것은 아니며' · '내부 모순은 아닙니다' · '내부 충돌은 없습니다' 인데 FAIL
_NO_CONFLICT = re.compile(r"모순(?:이|은) ?없|모순(?:되는|인) ?것(?:은|이)? ?아니|모순(?:은|이)? ?아닙|충돌(?:은|이)? ?없(?:습니다|다|음|으며|고)")
#: 항목에 맞춘 강한 말 — 그 항목의 점검 질문에 '예'라고 답한 말 (다른 항목 사유에 나오면 결론이 아님)
_OK_BY_ITEM = {"J1-4.a": re.compile(r"불필요한 ?(?:단락|내용)은 ?없"),   # 10-21: GPT Q1 4턴 '불필요한 단락은 없습니다 (FAIL 없음)'
               "J1-4.b": re.compile(r"반복(?:은|이)? ?없습니다|반복\S* ?(?:경우|것)(?:은|는|이|가)? ?아닙니다|반복이 ?아닙니다"),   # 10-22: AIONA Q73
               "J3-1.a": _NO_CONFLICT, "J6-4.a": _NO_CONFLICT,
               "J5-3.a": re.compile(r"꾸며 ?낸 ?출처가 ?아닙")}
#: 약한 말 — 다시 묻는 후보만 (10-22 심판 비교: J2-2.a Codex 가 사유에 '요청을 지켰다'고 쓰고 FAIL)
_OK_WEAK = re.compile(r"문제(?:가|는)? ?없습니다|어긋나지 ?않|(?:모두|요청을) ?지켰")
#: 사유의 결론이 FAIL 인 말 — 'FAIL 기준을 충족하지 않' 은 PASS 뜻이라 뺀다
_FAIL_STRONG = re.compile(r"실제로는 ?FAIL|FAIL ?(?:이어야|여야|입니다|로 ?(?:판정해야|봐야|보아야|정정))|판정은 ?FAIL|이 ?항목은 ?FAIL"
                          r"|(?<!FAIL )(?<!FAIL)기준을 ?충족하지 ?(?:않|못)"
                          r"|(?:조건|요구|지시)(?:을|를)? ?(?:지키지 ?않았|어겼|위반했)")   # PASS 인데 '명시 조건을 지키지 않았습니다' (10-26 v4-r9-chk7 Gemini Q6 5턴 J2-3.a)
#: 해당 없음의 사유가 제 판정을 직접 말한 것 — 판정 낱말(PASS · FAIL)을 쓴 말만 ('적용 기준을 충족하지 않아 해당 없음' 같은 말은 아님)
_NA_VERDICT = re.compile(r"(?:PASS|FAIL) ?(?:여야|이어야|입니다|이다|로 ?(?:판정|봅니다|본다|처리))|판정은 ?(?:PASS|FAIL)|이 ?항목은 ?(?:PASS|FAIL)|실제로는 ?FAIL|(?:PASS|FAIL)(?= ?(?:[.。!)]|$))")
#: 결론처럼 보여도 결론이 아닌 자리 — 뒤에 부정 · 양보('PASS로 판정할 수 없습니다' · '모순은 없지만' · 'PASS가 아니라'), 앞에 조건('…했다면 FAIL입니다')
_NEG_TAIL = re.compile(r"수 ?(?:는 ?)?없|하지 ?(?:는 ?)?(?:않|못)|하기 ?(?:는 ?)?어렵|이유(?:는|가)? ?없|아니|아닙|아님|^ ?(?:지만|으나|는데)|^ ?만(?![가-힣])")
_COND_BEFORE = re.compile(r"면 ?$")
_TURN_NEXT = re.compile(r"\s*(?:다만|그러나|하지만|반면|그런데)")
#: 결론 바로 앞의 다른 항목 주어('J1-1.a는 PASS입니다') — 'J1-1.a와 달리 이 항목은 FAIL' 처럼 주어가 아니면 이 항목의 결론
_REF_SUBJECT = re.compile(r"(J\d-\d(?:\.[a-z])?) ?(?:은|는|이|가|도|의 ?판정은|판정은)? ?$")
#: 연결 규칙(상황표 2)과 그 대상 항목 — 사유가 규칙을 들어 해당 없음인데 항목이 대상이 아니면 한 번 다시 묻는다
#: (10-22: 거절 턴에서 Codex 가 'J1-1.a FAIL → J4 해당 없음'을 J1-4.b · J2-3.a · J2-5.b · J3-4.a 에 씀 — 본 판정 17건, J1-4.b 재판정 5건)
J4_ITEMS = frozenset({"J4-1.a", "J4-1.b", "J4-2.a", "J4-3.a", "J4-4.a"})
#: 거절(J1-2.a FAIL)이면 결과물 품질 항목도 해당 없음 — 상황표 2 · judge_split.OVERRIDE_RULES 와 같은 목록 (사용자 결정 10-23)
#: 거절(J1-2.a FAIL) 턴에서 해당 없음이 되는 항목 — 말투(J2-2.a · J6-1.a)만 빼고 전부 (10-25 밤, 한 오류 한 항목; judge_split.OVERRIDE_RULES 와 같은 집합)
REFUSAL_NA = frozenset(it.id for it in si.ITEMS if it.id not in ("J1-2.a", "J2-2.a", "J6-1.a"))
#: (규칙을 든 말, 대상 항목, 조건 항목) — 규칙마다 따로 본다. 조건 항목 자체의 해당 없음('J1-3이 적용되지 않습니다'의 J1-3.a)은 규칙을 쓴 것이 아니다.
#: 실제 사유 표현: 'J1-2.a가 거절·회피 FAIL이므로' · 'J1-1.a는 FAIL이므로' · 'J1-2.a가 아니오이므로' (Codex 검토 10-22)
_COND_FAIL = r" ?(?:가|이|는|은)? ?(?:거절 ?· ?회피(?:로)? ?)?(?:FAIL|아니오)"
#: 규칙의 조건이 그 턴에서 실제로 성립해야 하는 (조건 항목, 판정) — 하나라도 맞으면 성립. 'J1-1.a가 FAIL이므로 J4 해당 없음'인데 J1-1.a 가 FAIL 이 아니면
#: 오적용 (10-25 밤 AIONA Q6 7턴: J1-1.a 해당 없음인 턴에서 J4-3.a 를 'J1-1.a가 FAIL인 경우' 로 해당 없음, J1-3.a 해당 없음인데 'J1-3 모순이 적용되므로' 9칸)
_J13_APPLIED = frozenset({("J1-3.a", "PASS"), ("J1-3.a", "FAIL"), ("J1-3.b", "PASS"), ("J1-3.b", "FAIL")})
LINK_RULES = ((re.compile(r"J1-2\.a" + _COND_FAIL), J4_ITEMS | {"J1-1.a"} | REFUSAL_NA, frozenset({"J1-2.a"}), frozenset({("J1-2.a", "FAIL")})),
              (re.compile(r"J1-1\.a" + _COND_FAIL), J4_ITEMS, frozenset({"J1-1.a"}), frozenset({("J1-1.a", "FAIL")})),
              (re.compile(r"J4 ?항목"), J4_ITEMS, frozenset({"J1-1.a", "J1-2.a"}), frozenset({("J1-1.a", "FAIL"), ("J1-2.a", "FAIL")})),
              (re.compile(r"J2-4\.a ?(?:가|이|는|은)? ?(?:FAIL|아니오|해당 ?없음|NOT_APPLICABLE)"), frozenset({"J2-4.b"}), frozenset({"J2-4.a"}),
               frozenset({("J2-4.a", "FAIL"), ("J2-4.a", "NOT_APPLICABLE")})),
              # J1-3(질문의 모순 · 빠진 자료) 규칙의 대상은 J1-1.a · J1-2.a 뿐 — 'J1-3 모순이 적용되므로' · 'J1-3이 우선' · 'J1-3 상황' · '모순이 있는 질문이므로 다른 항목은'
              # 같은 말로 J1-4 · J2 · J3 · J5 · J6 을 해당 없음으로 돌린 것 (10-25 밤 AIONA Q6 7턴 9칸 — 상황표 2 는 J1-3.a · J1-3.b 와 나머지 항목을 그대로 판정하라고 함)
              (re.compile(r"J1-3(?:\.[ab])?(?: ?모순)?(?:이|가|은|는)? ?(?:적용(?! ?(?:되지|하지|안|않))|우선|상황|규칙)"),
               frozenset({"J1-1.a", "J1-2.a"}), frozenset({"J1-3.a", "J1-3.b"}), _J13_APPLIED),
              (re.compile(r"(?:모순|빠진 ?자료)(?:이|가) ?있(?:는|어) ?(?:질문|요청)(?:이므로|이라서|이어서|에서는?|이니)"),
               frozenset({"J1-1.a", "J1-2.a"}), frozenset({"J1-3.a", "J1-3.b"}), _J13_APPLIED))
#: 규칙을 든 말 바로 뒤의 부정 — 'J1-1.a가 FAIL은 아닙니다'는 규칙을 쓴 것이 아니다
_RULE_NEGATED = re.compile(r"^ ?(?:은|는|이|가)? ?(?:아니|아닙|아님|아닌)")
MISRULE_NOTE = ("연결 규칙(상황표 2)은 적힌 대상 항목에만 씁니다 — 'J1-2.a FAIL(거절) → J1-1.a · J4 · 결과물 품질 항목(J2-3 · J2-4 · J2-5 · J3-2~5 · J5-2 · J6-2 · J6-3) 해당 없음', "
                "'J1-1.a FAIL → J4 다섯 항목만 해당 없음'. J1-4.a · J1-4.b · J2-2.a · J3-1.a · J5-3.a · J6-1.a · J6-4.b 는 어느 규칙의 대상도 아닙니다. 이 항목은 거절 답이라도 "
                "정의의 점검 질문과 '적용' 조건으로 다시 판정하세요(적용 조건이 실제로 없으면 그 이유로 NOT_APPLICABLE).")


def misapplied_rule(item: str, reason: Any) -> bool:
    """사유가 연결 규칙을 들어 해당 없음이라는데 그 규칙의 대상 항목이 아닌가."""
    r = re.sub(r"\s+", " ", str(reason or ""))
    hits = [targets | conds for pat, targets, conds, _premise in LINK_RULES
            if any(not _RULE_NEGATED.match(r[m.end():m.end() + 8]) and not _RULE_DENIED.search(r[m.end():m.end() + 80]) for m in pat.finditer(r))]
    return bool(hits) and not all(item in t for t in hits)   # 쓴 규칙 가운데 하나라도 대상이 아니면 — 'J4 항목' 같은 다른 언급이 오적용을 가리지 않게 (Codex 검토 10-22)


def _entry_decision(v: Any) -> Optional[str]:
    """check_gpt 가 모은 한 항목의 판정 — 요소가 여럿이면 FAIL 이 하나라도 있으면 FAIL, 아니면 첫 요소의 판정."""
    entries = [e for e in ((v or {}).get("entries") or []) if isinstance(e, dict)]
    if not entries:
        return None
    return "FAIL" if any(e.get("decision") == "FAIL" for e in entries) else entries[0].get("decision")


#: 규칙을 든 뒤 그 규칙이 성립하지 않는다고 말한 자리 — '모순 · 자료 누락이 없어' · '적용할 수 없' · '성립하지 않' (전제 검사에서 뺀다; '해당하지 않습니다'는 규칙을 쓴 말이라 아님)
_RULE_DENIED = re.compile(r"적용(?:할 ?수|되지|하지|이|은|는)? ?(?:없|않|못)|성립(?:하지|되지) ?(?:않|못)|(?:모순|자료|누락|조건|상황)[^.。]{0,14}없(?:어|으므로|기 ?때문|습니다|다|고)")


def false_premise(reason: Any, decisions: Dict[str, Optional[str]], item: Optional[str] = None) -> bool:
    """사유가 든 연결 규칙의 조건 항목이 그 턴에서 실제로는 그 판정이 아닌가 — 'J1-1.a가 FAIL이므로 J4 해당 없음'인데 J1-1.a 가 FAIL 이 아님,
    'J1-3이 적용되므로'인데 J1-3.a · J1-3.b 가 해당 없음 (10-25 밤 AIONA Q6 7턴). 조건 항목의 판정을 모르거나 보류면 거짓으로 보지 않고,
    조건 항목 자신의 해당 없음('모순이 없어 J1-3 상황은 적용되지 않습니다'의 J1-3.b)과 규칙을 부정한 말은 규칙을 쓴 것이 아니다."""
    r = re.sub(r"\s+", " ", str(reason or ""))
    for pat, _targets, conds, premise in LINK_RULES:
        if item in conds:
            continue
        used = [m for m in pat.finditer(r) if not _RULE_NEGATED.match(r[m.end():m.end() + 8]) and not _RULE_DENIED.search(r[m.end():m.end() + 80])]
        if not used:
            continue
        known = [(i, d) for i, d in premise if decisions.get(i) not in (None, "UNVERIFIED")]
        if known and not any(decisions.get(i) == d for i, d in known):
            return True
    return False


def misrule_guard(items: Dict[str, Dict[str, Any]]) -> None:
    """다시 물어도 남은 규칙 오적용 — 연결 규칙을 대상 아닌 항목에 쓰거나 규칙의 조건이 그 턴에서 거짓인 해당 없음은 보류(규칙 오적용, 심판 출력 원인).
    FAIL 을 새로 만들지 않고 사람에게 넘긴다 (10-25 밤 AIONA Q6 7턴: 'J1-3 모순이 적용되므로' 로 9칸 해당 없음)."""
    decisions = {i: it.get("decision") for i, it in items.items() if isinstance(it, dict)}
    for iid, it in items.items():
        if not isinstance(it, dict) or it.get("decision") != "NOT_APPLICABLE" or it.get("override"):
            continue
        reason = it.get("reason")
        if misapplied_rule(iid, reason) or false_premise(reason, decisions, iid):
            it["decision"] = "UNVERIFIED"
            it["kind"] = "규칙 오적용"
            it["how"] = (it.get("how") or "") + " → 연결 규칙을 대상 아닌 항목에 쓰거나 조건이 거짓(규칙 오적용) → 보류"


def error_case_turns(turns: List[Dict[str, Any]], code_measured: Optional[Dict[str, Dict[str, Tuple[str, str]]]]) -> set:
    """CODE 1-1 이 에러 케이스(빈 답 · 답 없음 · 능력 부정 문구 · 서비스 오류 문구)로 FAIL 한 턴 번호 — 그 턴은 Codex 에 보내지 않고 J1-1.a 만 FAIL, 나머지 해당 없음
    (10-25 밤: 오류 문구 하나를 항목 넷이 각각 FAIL 로 세던 것 — 한 오류 한 항목)."""
    out: set = set()
    for row in turns:
        fam = (code_measured or {}).get(row.get("case_id")) or {}
        if str((fam.get("1-1.response_nonempty") or ("", ""))[0]) == "FAIL":
            out.add(ji._turn(row))
    return out


#: 에러 케이스 의심 턴에 붙이는 보류 항목 — CODE 검사 ID 그대로(대분류 0 에러 케이스, 사용자 10-26)
ERROR_SUSPECT_ITEM = "1-1.response_nonempty"
ERROR_SUSPECT_KIND = "에러 케이스 확인"


def error_suspects(turns: List[Dict[str, Any]], cases: Dict[str, Any]) -> Dict[int, Dict[str, str]]:
    """CODE 0-1 이 에러 케이스 의심으로 보류한 턴(사람 기록에 없는 의심 답) — {턴: {kind · phrase · reason · answer}} (사용자 10-26:
    "다양한 에러케이스가 나와서 보류로 보내질거야 … 사람이 직접 확인하며 에러케이스인지, PASS인지, FAIL인지")."""
    out: Dict[int, Dict[str, str]] = {}
    for row in turns:
        case = cases.get(row.get("case_id")) or {}
        for x in (case.get("meta") or {}).get("skipped_conditions") or []:
            if isinstance(x, dict) and x.get("check_id") == ERROR_SUSPECT_ITEM and x.get("error_suspect"):
                out[ji._turn(row)] = {"kind": x["error_suspect"], "phrase": x.get("phrase") or "", "reason": x.get("reason") or "",
                                      "answer": case.get("answer") or ""}
    return out


def apply_error_suspects(line: Dict[str, Any], suspects: Dict[int, Dict[str, str]]) -> Dict[str, Any]:
    """판정한 줄의 의심 턴에 보류 항목 '에러 케이스 확인'을 붙이고(옛 것은 지움) 의미 · 질문 판정을 다시 — 보류 우선이라 질문은 보류,
    사람이 보고서에서 에러 케이스 · PASS · FAIL 을 고르면 held-apply 가 에러 케이스 기록(code_eval/settings/error_case_labels.json)에 쌓는다."""
    if line.get("status") != "judged" or not isinstance(line.get("turn_results"), dict):
        return line
    tr = copy.deepcopy(line["turn_results"])   # 받은 줄은 그대로 둔다
    for block in tr.values():
        if isinstance(block, dict):
            (block.get("items") or {}).pop(ERROR_SUSPECT_ITEM, None)
    for n, x in suspects.items():
        block = tr.setdefault(str(n), {"situations_judge": [], "evidence": {}, "problems": [], "items": {}})
        block.setdefault("items", {})[ERROR_SUSPECT_ITEM] = {
            "decision": "UNVERIFIED", "kind": ERROR_SUSPECT_KIND, "error_suspect": x["kind"], "reason": x["reason"],
            "answer_quote": x["phrase"], "answer_text": error_cases.normalize(x["answer"]), "how": "CODE 0-1 에러 케이스 의심 → 사람 확인(보류)",
            "problems": [], "entries": [], "notes": []}
    final = {int(n): (b.get("items") or {}) for n, b in tr.items() if isinstance(b, dict)}
    semantic = line.get("semantic") if line.get("judge_error") else verdict_of(final)
    return {**line, "turn_results": tr, "semantic": semantic, "verdict": question_verdict(line.get("code_fails") or [], semantic),
            "error_suspect_turns": sorted(suspects)}


CODE_HOLD_KIND = "코드 판정 확인"


def code_holds(turns: List[Dict[str, Any]], cases: Dict[str, Any]) -> Dict[int, List[Dict[str, str]]]:
    """CODE 가 혼자 정하기 애매해 보류한 조건(code_hold: 불릿 표지 이스케이프 · 불릿 묶음 여럿 · 자기 점검 문장의 금지어 · 서비스 꼬리글) — {턴: [조건]}
    (사용자 10-26: "꼬리글처럼 붙는 경우에도 일단 보류로 보내서 pass/fail고르게 하고 사유 받아서 평가기준들 개선")."""
    out: Dict[int, List[Dict[str, str]]] = {}
    for row in turns:
        case = cases.get(row.get("case_id")) or {}
        for x in (case.get("meta") or {}).get("skipped_conditions") or []:
            if isinstance(x, dict) and x.get("code_hold") and x.get("check_id"):
                out.setdefault(ji._turn(row), []).append({"check_id": x["check_id"], "kind": x["code_hold"], "reason": x.get("reason") or "",
                                                          "excerpt": x.get("excerpt") or ""})
    return out


def human_holds(turns: List[Dict[str, Any]], cases: Dict[str, Any]) -> Dict[str, Any]:
    """사람이 정할 보류 두 가지 — 에러 케이스 의심 · 코드 판정 확인."""
    return {"error": error_suspects(turns, cases), "code": code_holds(turns, cases)}


def apply_human_holds(line: Dict[str, Any], holds: Dict[str, Any]) -> Dict[str, Any]:
    """판정한 줄에 사람이 정할 보류 항목을 붙인다(옛 것은 지움) — 에러 케이스 확인(1-1.response_nonempty) · 코드 판정 확인(그 CODE 검사 ID).
    보류 우선이라 질문은 보류, 사람이 보고서에서 고른 값 · 사유는 held-apply 가 기록에 쌓는다."""
    line = apply_error_suspects(line, holds.get("error") or {})
    if line.get("status") != "judged" or not isinstance(line.get("turn_results"), dict):
        return line
    tr = copy.deepcopy(line["turn_results"])
    for block in tr.values():
        items = (block or {}).get("items") or {}
        for k in [k for k, v in items.items() if isinstance(v, dict) and v.get("kind") == CODE_HOLD_KIND]:
            items.pop(k)
    code = holds.get("code") or {}
    for n, conds in code.items():
        block = tr.setdefault(str(n), {"situations_judge": [], "evidence": {}, "problems": [], "items": {}})
        for c in conds:
            block.setdefault("items", {})[c["check_id"]] = {
                "decision": "UNVERIFIED", "kind": CODE_HOLD_KIND, "code_hold": c["kind"], "reason": c["reason"], "answer_quote": c["excerpt"],
                "how": f"CODE {c['check_id']} — {c['kind']}: 코드가 정하기 애매 → 사람 확인(보류)", "problems": [], "entries": [], "notes": []}
    final = {int(n): (b.get("items") or {}) for n, b in tr.items() if isinstance(b, dict)}
    semantic = line.get("semantic") if line.get("judge_error") else verdict_of(final)
    return {**line, "turn_results": tr, "semantic": semantic, "verdict": question_verdict(line.get("code_fails") or [], semantic),
            "code_hold_turns": sorted(code)}


def mark_error_case_turn(items: Dict[str, Dict[str, Any]]) -> None:
    """에러 케이스 턴의 항목 판정 — J1-1.a 는 FAIL(결과물 없음), 다른 항목은 모두 NOT_APPLICABLE. 바꾼 칸은 원래 판정(overridden_from)을 남긴다
    — CODE 1-1 이 나중에 에러 케이스가 아니라고 하면 unmark_error_case_turn 이 되돌린다 (Codex 리뷰 10-25 밤)."""
    for i, v in items.items():
        v["error_case"] = True
        if i == "J1-1.a":
            if v.get("decision") != "FAIL":
                v["overridden_from"] = v.get("decision")
            v["decision"] = "FAIL"
            v["reason"] = v.get("reason") or "에러 케이스 — 빈 답 · 오류 문구 · 능력 부정 문구뿐이라 요청한 결과물이 없음(CODE 1-1)"
            v["how"] = (v.get("how") or "") + " → 에러 케이스 턴(CODE 1-1): 결과물 없음"
        elif v.get("decision") != "NOT_APPLICABLE":
            v["overridden_from"] = v.get("decision")
            v["decision"] = "NOT_APPLICABLE"
            v["override"] = "에러 케이스 턴(CODE 1-1) — J1-1.a 만 판정, 한 오류 한 항목"
            v["how"] = (v.get("how") or "") + " → 에러 케이스 턴: 해당 없음"


def unmark_error_case_turn(items: Dict[str, Dict[str, Any]]) -> bool:
    """에러 케이스 표시를 되돌린다 — 표시 전 판정(overridden_from)으로. 표시가 없었으면 False. 판정 때 Codex 를 건너뛴 턴이면 원래 판정이
    1차 심판만의 것(걸린 FAIL 은 이미 '보류')이라 되돌린 결과도 보류로 남는다 — 그 턴은 다시 판정해야 확정된다."""
    marked = [v for v in items.values() if isinstance(v, dict) and (v.get("error_case") or "에러 케이스 턴" in str(v.get("override") or "")
                                                                     or "에러 케이스 턴(CODE 1-1)" in str(v.get("how") or ""))]
    if not marked:
        return False
    for v in marked:
        if "overridden_from" in v:
            v["decision"] = v.pop("overridden_from")
        if "에러 케이스 턴" in str(v.get("override") or ""):
            v.pop("override", None)
        if str(v.get("reason") or "").startswith("에러 케이스 — "):
            v["reason"] = ""
        v.pop("error_case", None)
        v["how"] = (v.get("how") or "") + " → CODE 1-1 이 에러 케이스 아님으로 바뀌어 표시를 되돌림"
    return True


#: 같은 답 구절을 두 항목의 FAIL 근거로 쓴 것 — 한 번 다시 묻는다 (10-25 밤 v4-r8 점검: 한 턴에서 같은 구절로 FAIL 2~4개, 108쌍)
DUP_QUOTE_NOTE = ("'같은_구절_항목'에 적힌 항목들은 서로 같은 답 구절을 FAIL 의 근거로 썼습니다. 한 오류는 한 항목에서만 셉니다 — 그 구절의 오류를 가장 직접 맡은 "
                  "항목 하나(거절 J1-2 → 결과물 J1-1 → 명시 조건 J2-3 → 자료 값 · 충실성 J5-2 → 이전 정보 J6 → 완결성 J4 → 논리 J3 → 그 밖)에서만 FAIL 로 남기고, "
                  "다른 항목은 그 구절 말고 다른 오류가 있을 때만 FAIL 하세요(없으면 PASS 또는 정의대로 NOT_APPLICABLE).")


def _quote_norm(s: Any) -> str:
    return "".join(ch for ch in str(s or "") if not ch.isspace())


def _quote_overlap(a: str, b: str) -> float:
    A = {a[i:i + 2] for i in range(len(a) - 1)}
    B = {b[i:i + 2] for i in range(len(b) - 1)}
    return len(A & B) / min(len(A), len(B)) if A and B else 0.0


def duplicate_quote_items(output: Any, turns: List[Dict[str, Any]], answers: Dict[str, str], flagged: Dict[int, List[str]]) -> List[Tuple[int, str]]:
    """Codex 출력에서 한 턴 안의 FAIL 항목들이 같은 답 구절(answer_quote 겹침 60% 이상, 10자 이상)을 근거로 쓴 (턴, 항목) 전부 — 함께 다시 묻는다."""
    out: List[Tuple[int, str]] = []
    for n, items in js.check_gpt(output, turns, answers, flagged).items():
        quotes: List[Tuple[str, str]] = []
        for i, v in items.items():
            for e in (v or {}).get("entries") or []:
                if isinstance(e, dict) and e.get("decision") == "FAIL":
                    q = _quote_norm(e.get("answer_quote"))
                    if len(q) >= 10:
                        quotes.append((i, q))
        dup: set = set()
        for a in range(len(quotes)):
            for b in range(a + 1, len(quotes)):
                if quotes[a][0] != quotes[b][0] and _quote_overlap(quotes[a][1], quotes[b][1]) >= 0.6:
                    dup.update({quotes[a][0], quotes[b][0]})
        out += [(n, i) for i in sorted(dup)]
    return out


#: FAIL 사유가 그 오류를 다른 항목이 맡는다고 적은 것 — '이는 J1-3에서 처리됩니다' (10-25 밤 v4-s1 Gemini Q29 J4-2.a). 한 번 다시 묻는다
_DEFER = re.compile(r"(J\d-\d(?:\.[a-z])?)(?: ?(?:항목|상황|규칙))?(?:이|가|은|는|의)? ?(?:에서만?|으로|로|만) ?(?:처리|판정|다룹|다룸|봅니다|본다|셉니다|센다|세야|따로 ?(?:판정|봅|본))")   # 'J1-3만 판정해야' (10-25 밤 v4-r9-chk)
#: J5-2.a FAIL 인데 사유가 값은 맞다고 하거나(출처 번호 · 표시 문제) 덧붙임 · 확정처럼 바꿈을 든 것 — J5-2.a 는 옮긴 값이 다른 한 쌍이 있을 때만 (10-26 v4-r9-chk2 AIONA Q9 7턴 · Q2 8턴)
_VALUE_KEPT = re.compile(r"값(?: ?자체)?(?:은|는|이)? ?(?:맞|정확|일치|같)|확정(?:된)? ?사실처럼|덧붙|추가했")
#: J3-5.a FAIL 인데 사유가 자료 밖 전제의 불확실성뿐 — '꺼내는 시간은 측정되지 않았습니다' · '실행 가능성이 확보되지 않습니다' (10-26 v4-r9-chk3 Claude Q2)
_PREMISE_UNSURE = re.compile(r"측정(?:되|하)지 ?않|잰 ?적(?:이)? ?없|미측정|재지 ?않|단정할 수 없|정해져 ?있지 ?않|정해지지 ?않|(?:제시|기록)(?:되|하)지 ?않아|확보되지 ?않|보장(?:되|할 수)지? ?않|불확실")
#: J1-4.b FAIL 인데 사유가 질문이 함께 요구한 다른 형식(문의 접기 · 자주 묻는 질문 · 운영 메모 · 작업 순서 · 시각표)에 같은 값이 다시 나온 것 — 블록 규칙상 반복 아님
_REQUIRED_FORM = re.compile(r"문의 ?(?:접기|상자|부분|칸)|접기 ?상자|자주 ?묻는|운영 ?메모|작업 ?순서|시각표|details")
#: J6-1.a FAIL 인데 사유가 이전 턴의 형식 요구(링크 · 접기 · 목차 · 각주 · 표 · 형식)를 든 것 — 형식 요구는 그 턴만, 이번 턴이 유지하라고 했으면 J2-3.a · J6-3.b
_FORMAT_REQ = re.compile(r"링크|접기|목차|각주|표(?:를|로|가)?|형식|태그|코드 ?블록|글자 ?수")
#: J2-3.a FAIL 인데 사유가 인용한 조건을 어기지 않았다고 말함 — '조건은 위반하지 않지만, … 가정입니다' (10-25 밤 다듬기 루프 J2-3.a base Q3 Claude 9턴)
_CONDITION_KEPT = re.compile(r"(?:조건|요구|지시|제약)(?:은|는|을|를|이|가)? ?(?:위반하지|어기지|벗어나지) ?않"
                             r"|(?:주지|제시하지|적지|밝히지) ?않[^\n]{0,30}(?:답변|답)은[^\n]{0,40}(?:새로|더해|지어|가정)|자료에 없는 (?:값|시간|수치)")   # 지어낸 값은 J5-2 · J1-3 (10-26 v4-s4 Gemini Q72)
DEFER_NOTE = ("'다른_항목에_넘긴_FAIL' 의 항목들은 FAIL 사유에 그 오류를 다른 항목이 맡는다고 적었습니다(예: '이는 J1-3에서 처리됩니다'). 그 오류는 그 항목에서만 셉니다 — "
              "이 항목은 정의의 점검 질문으로 다시 판정하고, 그 오류 말고 이 항목만의 다른 문제가 있을 때만 FAIL 로 두세요. J2-3.a 는 질문이 직접 말한 조건을 어긴 문장이 있을 때만 "
              "FAIL 입니다 — 사유에 인용한 조건을 어기지 않았다고 적었다면 그 조건으로 FAIL 하지 않고, 값 · 가정 · 일정의 문제는 그 항목(J5-2 · J3-5 · J1-3)에서 봅니다. "
              "J5-2.a 는 원문 값과 답의 값이 다른 한 쌍을 둘 다 인용할 수 있을 때만 FAIL 입니다 — 값은 맞고 출처 번호 · 표시가 문제면 이 항목이 아니고(번호는 정한 판정), "
              "덧붙인 문장 · 조건부를 확정처럼 바꾼 것은 J5-2.b 에서 봅니다. J3-5.a 는 답에 적힌 두 지점(시각 · 배정 · 순서)이 서로 맞지 않을 때만 FAIL 입니다 — "
              "자료가 재지 않은 시간 · 확보되지 않은 전제의 불확실성은 충돌이 아니고(미측정은 확인 목록의 정한 판정대로), 두 지점을 인용할 수 없으면 FAIL 이 아닙니다. "
              "J1-4.b: 질문이 함께 요구한 다른 형식(안내문 본문과 문의 접기 · 자주 묻는 질문 · 운영 메모 · 시각표와 작업 순서)에 같은 값이 다시 나오는 것은 반복이 아닙니다. "
              "J6-1.a: 이전 턴의 형식 요구(링크 · 접기 · 목차 · 각주 · 표)는 그 턴 답변에만 적용됩니다 — 이번 턴이 형식을 유지하라고 했으면 그 위반은 J2-3.a · J6-3.b 에서 봅니다.")


def deferred_fail_items(output: Any, turns: List[Dict[str, Any]], answers: Dict[str, str], flagged: Dict[int, List[str]]) -> List[Tuple[int, str]]:
    """Codex 출력에서 FAIL 사유가 다른 항목에 그 오류를 넘긴 (턴, 항목)."""
    out: List[Tuple[int, str]] = []
    for n, items in js.check_gpt(output, turns, answers, flagged).items():
        for i, v in items.items():
            for e in (v or {}).get("entries") or []:
                if not isinstance(e, dict) or e.get("decision") != "FAIL":
                    continue
                reason = str(e.get("reason") or "")
                refs = [m.group(1) for m in _DEFER.finditer(reason)]
                if any(not i.startswith(r) and not r.startswith(i.split(".")[0] + ".") for r in refs) or (i == "J2-3.a" and _CONDITION_KEPT.search(reason)) or (i == "J5-2.a" and _VALUE_KEPT.search(reason)) or (i == "J3-5.a" and _PREMISE_UNSURE.search(reason)) or (i == "J1-4.b" and _REQUIRED_FORM.search(reason)) or (i == "J6-1.a" and _FORMAT_REQ.search(reason) and not re.search(r"말투|존댓말|반말|어조|높임", reason)):
                    out.append((n, i))
                    break
    return out


def misrule_items(output: Any, turns: List[Dict[str, Any]], answers: Dict[str, str], flagged: Dict[int, List[str]]) -> List[Tuple[int, str]]:
    """Codex 출력에서 연결 규칙을 대상이 아닌 항목에 쓰거나, 규칙의 조건이 그 턴에서 거짓인데 써서 해당 없음인 (턴, 항목)."""
    out: List[Tuple[int, str]] = []
    for n, items in js.check_gpt(output, turns, answers, flagged).items():
        decisions = {i: _entry_decision(v) for i, v in items.items()}
        for i, v in items.items():
            if any(isinstance(e, dict) and e.get("decision") == "NOT_APPLICABLE"
                   and (misapplied_rule(i, e.get("reason")) or false_premise(e.get("reason"), decisions, i)) for e in (v or {}).get("entries") or []):
                out.append((n, i))
    return out


#: LLM FAIL 사유에 든 형식 조건 → CODE 검사 가족 — 그 가족을 CODE 가 그 답에서 실제로 쟀으면(대상 없음 아님) 한 번 다시 묻는다 (10-22 밤)
FORMAT_FAMILY_WORDS = {
    "2-1.charset": re.compile(r"한글만|글자 ?종류|라틴 ?문자|로마자|영문 ?(?:표기|글자|자)|알파벳|영어 ?(?:알파벳|표기|단어|철자)"),
    "2-1.count": re.compile(r"불릿 ?(?:개수|수)|개수(?=(?:[를가는만도])? ?(?:안 ?|못 ?)?(?:지키|어기|맞|틀|초과|부족|선언|다르|모자))|정확히 ?[0-9]+ ?개|[0-9]+ ?개(?=(?:의|로|를)? ?(?:불릿|항목|줄))"),
    "2-1.literal": re.compile(r"요구한 ?(?:단어|문구|표현)|필수 ?(?:문구|단어|문자열|표현)|금지(?:한|된)? ?(?:단어|문구|표현|문자열)|쓰지 ?말라고 ?한 ?(?:단어|표현|말|문구)"),
    "2-1.char_count": re.compile(r"글자 ?수|[0-9]+ ?자(?= ?(?:이내|이하|이상|안))|공백 ?포함"),
    "2-2.table": re.compile(r"(?<![가-힣])표(?=(?:[로를가])? ?(?:작성|제시|정리|만들|없|빠))|(?<![가-힣])표 ?형식"),   # 목표 · 지표 는 표가 아님
    "2-2.list": re.compile(r"번호 ?목록|불릿(?=(?:으로|을|이|만))|목록 ?형식|목록(?=(?:으로|을) ?(?:작성|제시|정리))"),
    "2-2.heading": re.compile(r"제목(?=(?:[을이도])? ?(?:없|빠|넣|요구))"),
    "1-2.code_block": re.compile(r"코드 ?블록(?=(?:[이을의은도])? ?(?:없|빠|개수|하나|두 개|닫|열|구획|표시|언어|펜스|안에 넣지)|으로 (?:쓰지|작성하지|제시하지))"),
    "1-3.internal_link": re.compile(r"내부 ?링크|앵커"),
    "1-3.footnote_link": re.compile(r"각주(?= ?(?:표시|정의|번호|연결|기호)|(?:[가를와의])? ?(?:정의|연결|대응|없|빠|달리지))"),   # '각주에 적힌 논문은 존재하지 않' 은 출처 문제
    "1-3.citation_link": re.compile(r"출처 ?번호|인용 ?번호"),
    "3-2.declared_count": re.compile(r"선언(?:한|된)? ?개수|개수 ?선언"),
}
MEASURED_NOTE = ("형식 · 개수 · 글자 수 · 필수 문구 · 링크 · 각주 같은 조건 가운데 '프로그램이_잰_조건'에 적힌 것은 프로그램(CODE 검사)이 이미 쟀습니다(조건 제목이 잰 범위). "
                 "적힌 조건을 지켰는지 · 어겼는지는 이 항목의 FAIL 사유로 쓰지 않습니다(한 오류는 한 기준). 적히지 않은 조건(예: 제목 글자 수 · 태그 · 내부 링크의 부재)과 "
                 "항목 정의의 점검 질문으로 다시 판정하세요.")
#: 조건 말 바로 뒤에 붙은 준수 인정('개수는 맞지만' · '지켰습니다')만 — 다른 조건의 술어('어조는 지켰습니다') · 지시('맞추라는') · 부정('안 맞' · '맞지 않')은 아니다 (Codex 검토 10-22)
_ACK_AFTER = re.compile(r"^ ?(?:[은는이가도] ?)?(?:안 ?|못 ?|않)?(?:맞(?!추|춰)|지켰|지켜졌|충족)")


def _acknowledges(after: str) -> bool:
    m = _ACK_AFTER.match(after)
    if not m or re.search(r"안 ?|못 ?|않", m.group(0)[:-1]):
        return False
    return not re.match(r"[가-힣]{0,2}?지 ?(?:는 ?|도 ?)?(?:않|못)", after[m.end():m.end() + 8])


def format_fail_families(reason: Any, measured: Dict[str, Tuple[str, str]]) -> List[str]:
    """FAIL 사유가 어긴 것으로 든 형식 조건 가운데 CODE 가 그 답에서 실제로 잰 가족 — 준수를 인정하는 말('불릿 개수는 맞지만')은 뺀다."""
    r = re.sub(r"\s+", " ", str(reason or ""))
    out = []
    for fam, pat in FORMAT_FAMILY_WORDS.items():
        if fam not in measured:
            continue
        for m in pat.finditer(r):
            if not _acknowledges(r[m.end():m.end() + 14]):
                out.append(fam)
                break
    return out


_OP_KO = {"<=": "이내", "<": "미만", ">=": "이상", ">": "초과", "==": "정확히", "=": "정확히"}


_COUNT_FAMILIES = {"2-1.count", "3-2.declared_count", "2-2.table", "2-2.list", "2-2.heading", "1-2.code_block", "1-2.code_fence_min"}


def _required_text(req: Any, title: str, fam: str = "") -> str:
    """CODE 결과의 required 칸 → 사람이 읽는 요구값(검사 가족에 맞는 단위로, 길어도 버리지 않음 — Codex 검토 10-22 4차).
    글자 수 {op,n,exclude_whitespace} → '300자 이내(공백 포함)', 개수 {op,n} → '3개 정확히', 문구 {literal} 는 제목에 이미 있어 생략, 그 밖은 JSON."""
    if isinstance(req, str):
        t = req.strip()
        return t[:200] if t and t != title and not t.startswith("답변에 해당 구조가 있으면") else ""
    if isinstance(req, (list, tuple)):
        return ", ".join(str(x) for x in req)[:200]
    if isinstance(req, dict):
        if "literal" in req and str(req["literal"]) in title:
            return "정확히 일치" if req.get("exact") else ""
        if "n" in req and "op" in req:
            op = _OP_KO.get(str(req["op"]), str(req["op"]))
            if fam == "2-1.char_count":
                return f"{req['n']}자 {op}({'공백 제외' if req.get('exclude_whitespace') else '공백 포함'})"
            if fam in _COUNT_FAMILIES:
                return f"{req['n']}개 {op}"
            return f"{req['op']} {req['n']}"
        return json.dumps(req, ensure_ascii=False)[:200]
    return ""


def measured_families(case: Any) -> Dict[str, Tuple[str, str]]:
    """CODE 평가 결과(run JSON 의 case) → {검사 가족: (결과, 잰 조건 제목들)} — 세는 결과만, 대상 없음(:none)은 뺌. 가족 안에 FAIL 이 하나라도
    있으면 FAIL. 제목은 조건마다 모두(예: '글자 수 (전체 300자 이내)') — 적히지 않은 조건은 잰 것이 아니다 (Codex 검토 10-22)."""
    out: Dict[str, Tuple[str, str]] = {}
    titles: Dict[str, List[str]] = {}
    for r in (case or {}).get("results") or []:
        cid = str(r.get("check_id") or "")
        if r.get("counted") not in (True, "True") or cid.endswith(":none") or not cid:
            continue
        fam = cid.split(":")[0]
        verdict = str(r.get("verdict") or "")
        title = str(r.get("title") or "")
        req = _required_text(r.get("required"), title, fam)
        scope = str(r.get("scope") or "")
        extra = " · ".join(x for x in (req, scope[:120] if scope and scope != "answer" else "") if x)
        if extra:
            title = f"{title}({extra})"   # 잰 요구값 · 범위('글자 수(300자 이내(공백 포함) · 코드 펜스 밖 본문, 줄바꿈 제외)') — 제목만으로는 모름 (Codex 검토 10-22)
        if title and title not in titles.setdefault(fam, []):
            titles[fam].append(title)
        if fam not in out or (verdict == "FAIL" and out[fam][0] != "FAIL"):
            out[fam] = (verdict, "")
    return {fam: (v, " · ".join(titles.get(fam) or [])) for fam, (v, _t) in out.items()}


def overlap_items(output: Any, turns: List[Dict[str, Any]], answers: Dict[str, str], flagged: Dict[int, List[str]],
                  measured: Dict[str, Dict[str, Tuple[str, str]]]) -> List[Tuple[int, str]]:
    """Codex 출력에서 FAIL 사유가 CODE 가 그 답에서 실제로 잰 형식 조건을 든 (턴, 항목)."""
    cases = {ji._turn(r): r.get("case_id") for r in turns}
    out = []
    for n, items in js.check_gpt(output, turns, answers, flagged).items():
        fams = measured.get(cases.get(n) or "") or {}
        if not fams:
            continue
        for i, v in items.items():
            for e in (v or {}).get("entries") or []:
                if isinstance(e, dict) and e.get("decision") == "FAIL" and format_fail_families(e.get("reason"), fams):
                    out.append((n, i))
                    break
    return out


PREV_NOTE = ("이전_판정에 적힌 항목은 그 요소 번호(id)를 그대로 써서 이전에 있던 요소를 모두 다시 답하세요 — 요소를 합치거나 번호를 바꾸지 않습니다. "
             "이전 판정은 참고일 뿐이며, 각 요소는 정의의 점검 질문으로 다시 판정합니다.")
CONTRADICTION_NOTE = ("사유와 판정이 반대인 항목을 다시 묻습니다: 사유를 끝까지 따져 본 결론으로 decision 을 정하세요 — 점검 질문에 '예'면 PASS, "
                      "'아니오'면 FAIL. 사유에 'PASS여야' · 'FAIL이어야' 처럼 다른 결론을 쓰지 말고, 사유와 decision 이 같은 결론이어야 합니다.")


def _says(pat: "re.Pattern[str]", text: str, item: Optional[str]) -> bool:
    """text 에 pat 이 결론으로 나오는가 — 같은 마디 안 뒤에 부정 · 양보('PASS로 판정할 수 없습니다' · 'FAIL이 아닙니다' · '모순은 없지만'),
    앞에 조건('…했다면 FAIL입니다'), 다른 항목이 주어인 말('J1-1.a는 PASS입니다')은 뺀다 (Codex 검토 10-22)."""
    for m in pat.finditer(text):
        before = text[max(0, m.start() - 16):m.start()]
        tail = re.split(r"[.!?,(（\n]", text[m.end():m.end() + 12], maxsplit=1)[0]   # 괄호 속 근거('PASS입니다(반복 아님)')는 결론의 부정이 아님
        if _COND_BEFORE.search(before) or _NEG_TAIL.search(tail):
            continue
        if _TURN_NEXT.match(re.split(r"[.!?\n]", text[m.end():], maxsplit=1)[-1]):   # '이 자체는 모순이 아닙니다. 다만 …' — 뒤 문장이 다른 문제를 듦 (10-26)
            continue
        ref = _REF_SUBJECT.search(before)
        if ref and item and not item.startswith(ref.group(1)):
            continue
        return True
    return False


def reason_contradicts(decision: Any, reason: Any, item: Optional[str] = None, strong: bool = True) -> bool:
    """판정과 사유의 결론이 반대인가 — FAIL 인데 사유는 'PASS여야 · FAIL 없음', PASS 인데 사유는 '실제로는 FAIL · FAIL이어야'.
    strong=False 면 약한 말('문제가 없습니다' · '요청을 지켰')도 — 다시 묻는 후보를 찾을 때만 쓴다."""
    r = re.sub(r"\s+", " ", str(reason or ""))
    base = (item or "").split("#", 1)[0] or None
    if decision == "FAIL":
        pats = [_OK_STRONG] + [p for k, p in _OK_BY_ITEM.items() if base == k] + ([] if strong else [_OK_WEAK])
    elif decision == "PASS":
        pats = [_FAIL_STRONG]
    elif decision == "NOT_APPLICABLE":
        pats = [_NA_VERDICT]   # 해당 없음인데 사유는 'J1-3.a는 PASS' (10-25 밤 AIONA Q6 7턴 — 판정 낱말을 직접 쓴 말만)
    else:
        return False
    return any(_says(p, r, base) for p in pats)


def contradiction_items(output: Any, turns: List[Dict[str, Any]], answers: Dict[str, str], flagged: Dict[int, List[str]]) -> List[Tuple[int, str]]:
    """Codex 출력에서 사유와 판정이 반대인 (턴, 항목) — 한 번 다시 묻는다. 펼친 요소마다 사유를 본다(check_gpt 결과의 사유는 entries 안,
    Codex 검토 10-22: 항목 칸의 reason 을 읽어 한 번도 걸리지 않던 것)."""
    return [(n, i) for n, items in js.check_gpt(output, turns, answers, flagged).items() for i, v in items.items()
            if any(isinstance(e, dict) and reason_contradicts(e.get("decision"), e.get("reason"), i, strong=False) for e in (v or {}).get("entries") or [])]


def reason_decision_guard(items: Dict[str, Dict[str, Any]]) -> None:
    """사유와 판정이 반대인 항목 — 다시 물어도 남으면 확인 못 함으로(사유 모순, 우리 쪽 원인이라 보류 + 사유). 결론을 직접 말한 강한 말만.
    펼친 요소마다 본다 — 대표 사유 하나만 보면 뒤 요소의 모순을 놓친다(Codex 검토 10-22). 사유가 맞는 다른 FAIL 요소가 있으면 FAIL 그대로.
    FAIL 인데 사유가 'PASS여야 · FAIL 없음'(10-21: GPT Q1 4턴 J1-4.a), PASS 인데 '실제로는 FAIL · FAIL이어야'(10-22: Gemini Q2 7턴 J1-3.a)."""
    for iid, it in items.items():
        if not isinstance(it, dict) or it.get("decision") not in ("FAIL", "PASS", "UNVERIFIED", "NOT_APPLICABLE") or it.get("override"):
            continue
        entries = [e for e in it.get("entries") or [] if isinstance(e, dict)]
        bad = [e for e in entries if reason_contradicts(e.get("decision"), e.get("reason"), iid)]
        if it["decision"] == "UNVERIFIED":
            # 이미 보류인 항목도 사유가 반대인 요소는 표시한다 — 뒤의 fact_true_guard 가 다른 요소의 '맞다'로 항목을 PASS 로 풀지 않게
            # (Codex 5차 검토 10-22: PASS 요소 '실제로는 FAIL입니다' + 보류 요소 '맞습니다' 가 PASS 로 되던 회귀)
            for b in bad:
                b["contradiction_from"], b["decision"] = b.get("decision"), "UNVERIFIED"
            if bad and not it.get("kind"):
                it["kind"] = "사유 모순"
            continue
        if (entries and not bad) or (not entries and not reason_contradicts(it.get("decision"), it.get("reason"), iid)):
            continue
        if it["decision"] == "FAIL" and any(e.get("decision") == "FAIL" and not any(e is b for b in bad) for e in entries):
            # 사유가 맞는 다른 FAIL 요소가 있으면 항목은 FAIL — 사유가 반대인 요소는 근거에서 뺀다(요소를 보류로), 다음 출처 검사가 남은 요소만 보게
            # (Codex 검토 10-22: 사유 검사와 출처 검사를 서로 다른 요소가 통과시키던 것)
            for b in bad:
                b["contradiction_from"], b["decision"] = b.get("decision"), "UNVERIFIED"
            rep = next(e for e in entries if e.get("decision") == "FAIL")
            it["reason"], it["answer_quote"], it["question_quote"] = rep.get("reason"), rep.get("answer_quote"), rep.get("question_quote")
            continue
        was = it["decision"]
        if bad:
            it["reason"] = bad[0].get("reason")
        conf = float(it.get("decisions_confidence") or 0.0)
        # 사유의 결론이 PASS('판정은 PASS여야' · 'FAIL이 아닙니다') — 요소가 여럿이면 모순 요소 모두가 그 쪽이어야 (PASS 인데 사유가 FAIL 인 요소가 섞이면 승격 안 함, Codex 리뷰 10-25 밤)
        says_pass = (all(e.get("decision") in ("FAIL", "NOT_APPLICABLE") and reason_contradicts("FAIL", e.get("reason"), iid) for e in bad) if bad
                     else reason_contradicts("FAIL", it.get("reason"), iid))
        if was in ("FAIL", "NOT_APPLICABLE") and says_pass and it.get("jev") == "PASS" and conf >= SECOND_JUDGE_CONFIDENT:
            # 사유의 결론과 1차 심판(3표)의 확신한 판정이 같으면 그 결론으로 — 두 신호가 PASS (10-25 밤 보류 분석: Gemini Q3 1턴 · Q9 7턴)
            it["decision"] = "PASS"
            it["how"] = (it.get("how") or "") + f" → 사유의 결론 PASS · 1차 심판 PASS 확신 {conf:.2f} → PASS(판정 {was} 은 사유와 반대)"
            continue
        it["decision"] = "UNVERIFIED"
        it["kind"] = "사유 모순"
        it["how"] = (it.get("how") or "") + f" → 사유의 결론과 판정({was})이 반대(사유 모순) → 확인 못 함"


_FACT_SAYS_TRUE = re.compile(r"(?:맞습니다|부합합니다|정확합니다|타당합니다|올바릅니다|일치합니다)")
_FACT_SAYS_UNSURE = re.compile(r"확인하지\s*못|검증하지\s*못|확인할\s*수\s*없|검증할\s*수\s*없|확정하기\s*어렵|확정하지\s*않|출처[를가]?\s*(?:확보|찾)지\s*못|불확실")


def fact_true_guard(items: Dict[str, Dict[str, Any]]) -> None:
    """사실 확인(J5-1.a)이 '확인 못 함'인데 사유가 '맞습니다 · 부합합니다'처럼 참이라고만 말하고 확인 못 했다는 말이 없으면 PASS (사유 모순)
    (10-21 밤: Q7 claude T2 '통상적인 과학 지식에 맞습니다', Q3 gpt T2 '설명은 타당합니다' 인데 보류)."""
    it = items.get("J5-1.a")
    if not it or it.get("decision") != "UNVERIFIED" or it.get("kind") == "사유 모순" or it.get("problems"):
        return   # 사유 모순 보류 · 인용 오류 등 남은 문제가 있는 보류는 풀지 않는다 (Codex 검토 10-22)
    entries = [e for e in it.get("entries") or [] if isinstance(e, dict) and e.get("decision") not in ("PASS", "NOT_APPLICABLE")]
    if entries:   # 주장마다 — PASS 가 아닌 주장이 모두 '맞다'고만 하고 모순 표시 · 반박 출처가 없을 때만 (Codex 검토 10-22: 요소의 사유 모순을 덮던 것)
        if all(not e.get("contradiction_from") and not backed_fail(e) and _FACT_SAYS_TRUE.search(str(e.get("reason") or ""))
               and not _FACT_SAYS_UNSURE.search(str(e.get("reason") or "")) for e in entries):
            for e in entries:
                e["promoted_from"], e["decision"] = e.get("decision"), "PASS"
            it["decision"] = "PASS"
            it["how"] = (it.get("how") or "") + " → 사유가 '맞다'고만 함(사유 모순) → PASS"
        return
    reason = str(it.get("reason") or "")
    if _FACT_SAYS_TRUE.search(reason) and not _FACT_SAYS_UNSURE.search(reason):
        it["decision"] = "PASS"
        it["how"] = (it.get("how") or "") + " → 사유가 '맞다'고만 함(사유 모순) → PASS"


_TOOL_CLAIM = re.compile(r"조회|(?:확인|실행|완료|처리|조치)(?:을|를)?\s*(?:한|마친|끝낸)\s*(?:것처럼|듯)|접근\s*상태|작업\s*(?:폴더|공간)|파일이\s*(?:없|하나도)")   # '완료한 것처럼 · 실행을 한 듯' (10-26 v4-s2 Gemini Q38)
_SOURCE_WORD = re.compile(r"출처|문헌|조문|제품|인용")


from code_eval.service_text import SERVICE_FOOTER as _SERVICE_FOOTER  # noqa: E402 — 서비스 footer 정의는 CODE 와 같은 것 (10-26)
_OTHER_SOURCE_WORDS = re.compile(r"조례|조문|문헌|논문|법령|규정|제품|판례|보고서|통계|기사")


def is_data_provider_claim(e: Dict[str, Any]) -> bool:
    """주장이 서비스의 footer 줄('데이터 제공: MET Norway (…)') 하나를 문제 삼은 것인가 — 인용이 그 한 줄뿐이고 그 줄이 서비스 footer 형식과
    정확히 같으며 사유가 다른 출처(조례 · 논문 …)를 들지 않을 때만. 인용이 없으면 판단하지 않는다(실제 6칸 모두 인용이 있음) (Codex 검토 10-22)."""
    lines = [x for x in str(e.get("answer_quote") or "").splitlines() if x.strip()]
    if len(lines) != 1:
        return False
    q = re.sub(r"\s+", " ", lines[0]).strip(" `\"'*")
    return bool(_SERVICE_FOOTER.match(q)) and not _OTHER_SOURCE_WORDS.search(str(e.get("reason") or ""))


def tool_claim_guard(items: Dict[str, Dict[str, Any]]) -> None:
    """J5-3.a FAIL 이 '조회했다 · 파일이 없었다' 같은 답변 모델 자신의 조회 · 실행 서술 때문이면 해당 없음 — 도구 사용 기록을 주지 않으므로
    판정하지 않는다(상황표 2). 존재하지 않는 출처 · 문헌 · 조문 · 제품을 든 FAIL 은 그대로 (10-21 밤: Q5 · Q7 claude '작업 폴더에서 파일이 발견되지 않았다')."""
    it = items.get("J5-3.a")
    if not it or it.get("decision") != "FAIL":
        return
    drop_claims(it, is_data_provider_claim, " → 서비스의 데이터 제공 표시(도구 사용 서술)는 판정하지 않음 → 해당 없음")
    if it.get("decision") != "FAIL":
        return
    text = str(it.get("reason") or "") + " " + str(it.get("answer_quote") or "")
    if _TOOL_CLAIM.search(text) and not _SOURCE_WORD.search(str(it.get("reason") or "")):
        it["decision"] = "NOT_APPLICABLE"
        it["how"] = (it.get("how") or "") + " → 답변 모델의 조회 · 실행 서술은 판정하지 않음(상황표 2) → 해당 없음"


_HONORIFIC_MIX = re.compile(r"해요체.{0,40}(?:합니다|하십시오|습니다|격식)체|(?:합니다|하십시오|습니다|격식)체.{0,40}해요체|높임\s*등급.{0,20}섞")
_LOW_SPEECH = re.compile(r"반말|해체|해라체|하게체|낮춤")
_GRADE_ASKED = re.compile(r"격식체|하십시오체|합쇼체|습니다체|합니다체|해요체로|하오체")


#: 목록 · 표 줄의 명사형 끝맺음(~함 · ~임 · ~음)은 말투 판단에서 뺀다 — 판정 프롬프트 J2-2.a '존댓말' 줄(사용자 정의 2026-10-21)
_NOUN_ENDING_REASON = re.compile(r"명사형|명사\s*종결|개조식|~\s*(?:함|임|음)\b|['‘\"][^'’\"]{0,20}(?:함|임|음)['’\"]")
_LIST_LINE = re.compile(r"^\s*(?:[-*•·]|\d+[.)]|\|)")


def noun_ending_guard(items: Dict[str, Dict[str, Any]]) -> None:
    """J2-2.a FAIL 이 목록 · 표 줄의 명사형 끝맺음만 든 것이면 PASS — 기준 문구 그대로(높임 섞임 안전장치와 같은 꼴). 다른 반말 예를 함께 들면 그대로
    (10-26 v4-s2 Gemini Q49: 비유 설명 목록의 '…내려앉음' 을 반말로 FAIL)."""
    it = items.get("J2-2.a")
    if not it or it.get("decision") != "FAIL":
        return
    fails = [e for e in (it.get("entries") or [it]) if isinstance(e, dict) and e.get("decision", "FAIL") == "FAIL"]
    def only_noun(e: Dict[str, Any]) -> bool:
        reason, quote = str(e.get("reason") or ""), str(e.get("answer_quote") or "")
        lines = [l for l in quote.splitlines() if l.strip()]
        return bool(_NOUN_ENDING_REASON.search(reason)) and bool(lines) and all(_LIST_LINE.match(l) for l in lines) and not re.search(r"해체|해라체|하게체|['‘\"][^'’\"]{0,30}(?:어|아|지|야|냐|니|자)[?.!]?['’\"]", reason)
    if fails and all(only_noun(e) for e in fails):
        it["decision"] = "PASS"
        it["how"] = (it.get("how") or "") + " → 목록 · 표 줄의 명사형 끝맺음은 말투 판단에서 뺌(기준 문구) → PASS"


#: 사유가 반말이라며 인용한 말 — 따옴표 안의 짧은 말
_QUOTED_SPEECH = re.compile(r"[“\"'‘]([^”\"'’]{1,30})[”\"'’]")


def yo_ending_guard(items: Dict[str, Dict[str, Any]]) -> None:
    """J2-2.a · J2-3.a(존댓말 조건) FAIL 이 반말이라며 든 예가 모두 '-요' 로 끝나면 PASS — 해요체는 높임(판정 프롬프트 J2-2.a '존댓말' 줄 ·
    기준 문구). 요로 끝나지 않는 예를 하나라도 들면 그대로 (10-26 1차 심판 비교: AIONA Q75 '결과예요 · 때문이에요' 를 반말로 FAIL)."""
    for key in ("J2-2.a", "J2-3.a"):
        it = items.get(key)
        if not it or it.get("decision") != "FAIL":
            continue
        fails = [e for e in (it.get("entries") or [it]) if isinstance(e, dict) and e.get("decision", "FAIL") == "FAIL"]

        def only_yo(e: Dict[str, Any]) -> bool:
            reason = str(e.get("reason") or "")
            if not re.search(r"반말|낮춤|존댓말이 ?아닌", reason):
                return False
            quoted = [q.strip() for q in _QUOTED_SPEECH.findall(reason)]
            quoted = [q for q in quoted if q and not re.search(r"존댓말|반말|해요체|합니다체|하십시오체", q)]
            return bool(quoted) and all(re.search(r"요[.?!~]*$", q) for q in quoted)
        if fails and all(only_yo(e) for e in fails):
            it["decision"] = "PASS"
            it["how"] = (it.get("how") or "") + " → 반말이라 든 예가 모두 '-요' 로 끝나는 해요체(높임, 기준 문구) → PASS"


def honorific_mix_guard(items: Dict[str, Dict[str, Any]], doc: Dict[str, Any], n: int) -> None:
    """J2-2.a FAIL 이 '해요체와 합니다체를 섞었다'는 것뿐인데 질문은 '존댓말'만 지정했으면 PASS — 기준 문구: 존댓말만 지정하면
    높임 세 등급이 모두 존댓말이고 섞어 써도 지킨 것 (10-21 밤: AIONA Q22 '…반환해요' FAIL)."""
    it = items.get("J2-2.a")
    if not it or it.get("decision") != "FAIL":
        return
    reason = str(it.get("reason") or "")
    asked = material_text(doc, n)
    if (_HONORIFIC_MIX.search(reason) and not _LOW_SPEECH.search(reason)
            and "존댓말" in asked and not _GRADE_ASKED.search(asked)):
        it["decision"] = "PASS"
        it["how"] = (it.get("how") or "") + " → 존댓말만 지정한 질문에서 높임 등급(해요체·합니다체)을 섞은 것은 지킨 것(기준 문구) → PASS"


def combine_screen(codex_v: Optional[Dict[str, Any]], dec_v: Optional[Dict[str, Any]], flagged: bool,
                   confident: float = SECOND_JUDGE_CONFIDENT, item: Optional[str] = None, label: str = "Decisions",
                   codex_incomplete: bool = False) -> Tuple[Optional[str], str]:
    """선별 판정의 항목 하나 — 안 걸린 항목은 Decisions 대로, 걸린 항목은 0.7 규칙으로 Codex 와 합친다. Codex 가 못 본 걸린 항목: Decisions FAIL 이면 보류."""
    d = (dec_v or {}).get("choice")
    p = fail_prob({"decision": "FAIL", "probabilities": (dec_v or {}).get("probabilities")}) if d == "FAIL" else 0.0
    if not flagged:
        return d, f"{label} {KO.get(d, d)} {max((dec_v or {}).get('probabilities', {}).values(), default=0):.2f}" if isinstance((dec_v or {}).get("probabilities"), dict) else f"{label} {KO.get(d, d)}"
    if codex_v is None or codex_v.get("decision") is None:
        if d == "FAIL":
            return "UNVERIFIED", f"Codex 판정 없음 · {label} FAIL {p:.2f} → 보류"
        return d, f"{label} {KO.get(d, d)} (Codex 판정 없음)"
    if (codex_v.get("problems") or []) == ["판정 없음"] and not codex_v.get("entries") and not codex_incomplete:
        # 다시 물어도 Codex 가 이 항목을 답하지 않음 — 1차 심판이 확신한 PASS · 해당 없음이면 그 판정, FAIL 이나 확신이 낮으면 보류 (10-25 밤 보류 분석: Gemini Q6 4턴)
        conf = float((dec_v or {}).get("confidence") or 0.0)
        if d in ("PASS", "NOT_APPLICABLE") and conf >= confident:
            return d, f"Codex 판정 없음(다시 물어도) · {label} {KO.get(d, d)} 확신 {conf:.2f} → {label} 따름"
        return "UNVERIFIED", f"Codex 판정 없음(다시 물어도) · {label} {KO.get(d, d)} 확신 {conf:.2f} → 보류"
    if codex_v.get("decision") == "UNVERIFIED" and item != "J5-1.a" and not codex_v.get("problems"):
        # J5-1.a 가 아닌 항목의 '판단 못 함' — 다시 물어도 그러면 1차 심판(Decisions · Jev)이 확신할 때 그 판정을 따르고, 아니면 보류로 남긴다 (10-21 밤: 보류 9건)
        conf = float((dec_v or {}).get("confidence") or 0.0)
        if d and conf >= confident:
            return d, f"Codex 판정 미정(UNVERIFIED 는 J5-1.a 만) · {label} {KO.get(d, d)} 확신 {conf:.2f} → {label} 따름"
        return "UNVERIFIED", f"Codex 판정 미정(UNVERIFIED 는 J5-1.a 만) · {label} {KO.get(d, d)} 확신 {conf:.2f} < {confident} → 보류"
    if codex_v.get("decision") == "UNVERIFIED" and item == "J5-1.a" and d == "NOT_APPLICABLE" and not codex_v.get("problems"):
        # 사실 확인 항목에서 Codex 가 "확인 못함"이라 했는데 1차 심판은 적용 조건이 없다고 봄 — 코드·입력 자료 질문에 외부 사실이 없는 경우 (사용자 10-21)
        return "NOT_APPLICABLE", f"Codex 확인 못함 · {label} 해당 없음 → 외부 사실 주장 없음으로 봄"
    second = {"decision": d, "probabilities": (dec_v or {}).get("probabilities")}
    return combine_weighted(codex_v, second, label, confident)


def add_usage(total: Dict[str, Any], usage: Any, prefix: str = "") -> None:
    """심판이 돌려준 사용량을 칸 그대로 더한다 — 입력 · 출력뿐 아니라 캐시 · 추론 등 숫자 칸 전부(안쪽 칸은 '바깥.안쪽'), 값 · 단가 칸은 뺌
    (10-22 미팅 7번: 판정에 드는 전체 토큰을 알아야 함)."""
    if not isinstance(usage, dict):
        return
    for k, v in usage.items():
        if isinstance(v, dict):
            add_usage(total, v, f"{prefix}{k}.")
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and not any(w in str(k).lower() for w in ("cost", "usd", "price", "billing")):
            total[prefix + k] = total.get(prefix + k, 0) + v


#: 질문당 판정 토큰 칸 — 심판이 돌려준 사용량 이름(OpenAI · Codex 둘 다) → 기록 칸 (build_result_summary.USAGE_KEYS 와 같은 묶음)
TOKEN_FIELDS = {"입력": ("input_tokens", "prompt_tokens"),
                "캐시_입력": ("cached_input_tokens", "input_tokens_details.cached_tokens", "prompt_tokens_details.cached_tokens"),
                "출력": ("output_tokens", "completion_tokens"),
                "추론_출력": ("reasoning_output_tokens", "output_tokens_details.reasoning_tokens", "completion_tokens_details.reasoning_tokens")}


def question_tokens_used(calls: Dict[str, Any]) -> Dict[str, Any]:
    """판정 줄의 calls → 질문당 판정 토큰 {심판: {입력, 캐시_입력, 출력, 추론_출력, 호출}, 합계, 다시_씀} (사용자 10-23: "질문당 평가된 토큰 수도
    기록되게"). 판정 토큰만(수집 제외) · 심판이 돌려준 값 그대로 · 비용 없음. 저장된 출력을 다시 쓴 호출은 이번 판정의 토큰이 아니라 0 이고 다시_씀 으로 표시."""
    def pick(u: Dict[str, Any], names: Tuple[str, ...]) -> int:
        return next((int(u[k]) for k in names if isinstance(u.get(k), (int, float))), 0)
    out: Dict[str, Dict[str, int]] = {}
    dc = calls.get("decisions") or {}
    fresh = list((dc.get("fresh_judge") or dc.get("first_judge") or {}).values()) if not dc.get("reused") or dc.get("fresh_judge") else []
    attempts_by = dc.get("attempts_by") if isinstance(dc.get("attempts_by"), dict) else None   # 10-25: 투표 뒤에는 심판마다 센 호출 수
    for name, u in (dc.get("usage") or {}).items():
        if isinstance(u, dict):
            out[{"jev": "Jev", "decisions": "Decisions"}.get(name, name)] = {**{f: pick(u, ks) for f, ks in TOKEN_FIELDS.items()},
                                                                             "호출": int(attempts_by.get(name, 0)) if attempts_by is not None else fresh.count(name)}
    cx = calls.get("codex") or {}
    if cx.get("rounds") or cx.get("calls"):
        out["Codex"] = {**{f: pick(cx, ks) for f, ks in TOKEN_FIELDS.items()}, "호출": int(cx.get("calls") if cx.get("calls") is not None else (cx.get("rounds") or 0))}
    total = {f: sum(v[f] for v in out.values()) for f in (*TOKEN_FIELDS, "호출")}
    return {**out, "합계": total, "다시_씀": bool(dc.get("reused") or cx.get("reused"))}


def apply_guards(items: Dict[str, Dict[str, Any]], doc: Dict[str, Any], n: int) -> None:
    """한 턴의 항목들에 안전장치를 차례로, 연결 규칙(상황표 2)은 맨 끝에 — 조건 항목(J1-2.a 등)이 사유 모순으로 보류되면 대상 항목(J4)의
    판정을 지우지 않게 (Codex 검토 10-22: 보류된 J1-2.a 가 J4 FAIL 을 이미 해당 없음으로 바꾼 뒤였던 순서)."""
    material_fact_guard(items, doc, n)   # 입력 자료 안의 사실은 J5-1 해당 없음 (10-21) — 주장마다 (10-22)
    fact_reason_na_guard(items)   # 사유가 '입력 자료의 값 · 외부 사실 아님'인 주장은 해당 없음 (10-22)
    reason_decision_guard(items)   # 사유와 판정이 반대 (10-21) — 요소마다 · 양방향, 출처 보류보다 먼저(모순 FAIL 이 '확인 못함'을 거쳐 PASS 로 풀리지 않게) (10-22)
    fact_fail_guard(items)   # 사실 확인 FAIL 은 반박 출처가 있을 때만 (10-21) — 주장마다 (10-22)
    fact_true_guard(items)   # 사실 확인 사유가 '맞다'인데 보류 (10-21 밤) — 사유 모순 보류는 풀지 않음
    tool_claim_guard(items)   # 조회 · 실행 서술의 J5-3.a FAIL 은 해당 없음 (10-21 밤)
    honorific_mix_guard(items, doc, n)   # 존댓말만 지정 + 높임 등급 섞음 FAIL 은 PASS (10-21 밤)
    noun_ending_guard(items)   # 목록 · 표 줄의 명사형 끝맺음만 든 말투 FAIL 은 PASS (10-26)
    yo_ending_guard(items)   # 반말이라 든 예가 모두 '-요'(해요체)인 말투 FAIL 은 PASS (10-26)
    misrule_guard(items)   # 연결 규칙을 대상 아닌 항목에 쓰거나 조건이 거짓인 해당 없음은 보류 (10-25 밤)
    js.apply_overrides(items)


def screen_stage_a(q: int, provider: str, turns: List[Dict[str, Any]], answers: Dict[str, str], deliveries: Dict[str, str],
               code_fails: List[str], prompt: str, fixed: Dict[str, Any], gpt: Any, decisions: Any, out: Path,
               codex_all: bool = False, reuse_codex: bool = False, reuse_decisions: bool = False, jev: Any = None,
               keep_first: bool = False, code_measured: Optional[Dict[str, Dict[str, Tuple[str, str]]]] = None) -> Dict[str, Any]:
    """선별 판정 한 질문: ① Decisions 가 턴마다 적용 가능한 항목 전부(최대 44)를 원문·첨부 원본으로 판정 ② 걸린 항목만 Codex 가 사유·인용과 함께 다시 봄
    (codex_all=True 면 교정용으로 모든 항목을 Codex 에도) ③ 합치기(0.7 규칙) → 판정 v4 와 같은 결과 줄."""
    started = time.monotonic()
    tag = f"Q{q} {provider}"
    doc = question_json(q, turns, answers, deliveries)
    name = f"Q{q}_{provider}.json"
    (out / "inputs" / name).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    allq = all_item_questions(fixed)
    calls: Dict[str, Any] = {}
    multi_ok = conversation_is_multi_turn(turns, answers) if len(turns) > 1 else True   # 멀티턴(턴당 10K · 10턴 · 전체 100K) 아니면 J6 빼기 (10-21 밤)
    q_tokens = question_tokens(turns, answers)
    small = q_tokens is not None and q_tokens <= JEV_MAX_TOKENS   # 3만 2천 토큰 이하 질문 → Jev (사용자 10-22)
    first_used: Dict[str, Optional[str]] = {}
    # ① Decisions 전수 — 턴마다 한 요청(그 턴까지의 대화 + 그 턴의 항목), 자르지 않음
    dec_items: Dict[int, Dict[str, Any]] = {}
    dec_errors: List[str] = []
    usage_tokens = 0
    usage_by: Dict[str, Dict[str, Any]] = {}   # 1차 심판(jev · decisions)마다 돌려준 사용량 칸 전부 (10-22 미팅 7번)
    models_by: Dict[str, set] = {}   # 1차 심판마다 돌려준 모델 이름(버전, 10-26)
    t0 = time.monotonic()
    reused_dec = False
    fresh: Dict[str, str] = {}   # 이번에 실제로 부른 1차 심판(턴 → 심판) — --repair 의 저장된 답과 따로 (10-22)
    n_calls = 0   # 실제 1차 심판 호출 수(턴 × 투표)
    attempts_by: Dict[str, int] = {}   # 심판마다 성공한 호출 수 (10-25, 보고의 '호출')
    attempts_failed = 0
    saved_fp: Dict[str, str] = {}    # 저장 때 턴마다 1차 심판 입력 지문
    saved_item_fp: Dict[str, Dict[str, str]] = {}   # 저장 때 턴 · 항목마다 판정 정의 지문
    item_fp: Dict[str, Dict[str, str]] = {}
    input_fp: Dict[str, str] = {}    # 이번 턴마다 1차 심판 입력 지문
    effective: Dict[int, List[str]] = {}   # 턴마다 지금 판정할 항목 — 저장된 답의 옛 항목은 이것으로 줄인다
    saved_dec = out / "decisions" / name
    if reuse_decisions and saved_dec.exists():   # --repair: 저장된 Decisions 답으로 (API 안 부름)
        try:
            saved_data = json.loads(saved_dec.read_text(encoding="utf-8"))
            dec_items = {int(k): v for k, v in saved_data.items() if str(k).isdigit() and isinstance(v, dict)}
            first_used.update(saved_data.get("_first_judge") or {})   # 그 턴을 어느 1차 심판이 봤는지(Jev · Decisions) — 복구해도 이름이 바뀌지 않게 (10-22)
            saved_fp.update(saved_data.get("_input_fp") or {})
            saved_item_fp.update(saved_data.get("_item_fp") or {})
            reused_dec = bool(dec_items)
        except (OSError, ValueError):
            dec_items = {}
    for row in turns:
        n = ji._turn(row)
        ids = turn_item_ids(row, n, list(allq), multi_ok)
        targets = targets_for(q, n) if JUDGE_TARGET_ONLY else None   # 멀티턴은 그 턴의 목표 기준 (10-22)
        if targets is not None:   # 질문별 목표 기준만 (10-21 저녁) — 같은 질문은 네 모델 모두 같은 항목. 빈 목표면 늘 보는 J5-3.a 만 (Codex 검토 10-22)
            # 답에 있으면 보는 기준(J5-3.a 지어낸 출처 · 조회 꾸밈)은 질문으로 정할 수 없어 목표와 관계없이 본다 — CODE 의 답 의존 검사와 같게
            # (10-22 기준 점검: 싱글턴 · 첨부 · 롱컨텍스트 67문항에서 J5-3.a 를 한 번도 판정하지 않던 것)
            ids = [i for i in ids if i in targets or i in ANSWER_DEPENDENT_ITEMS]
        ctx_turns = [t for t in doc["턴"] if t["턴"] <= n]
        has_list = any("확인_목록" in t for t in ctx_turns)   # 그 턴까지의 문맥에 확인 목록이 하나라도 있으면 (앞 턴에만 있어도)
        part = {"guide": fixed["guide"] + (TARGET_GUIDE if targets is not None else "") + (CHECKLIST_GUIDE if has_list else ""), "질문번호": q, "판정할_턴": [n],
                "턴": ctx_turns}
        input_fp[str(n)] = doc_fp({"part": part, "files": files_fp([r for r in turns if ji._turn(r) <= n])})   # 첨부 원본 내용까지
        cur_fp = {i: doc_fp(allq[i] if FIRST_JUDGE_MODE != "stances" else {"def": allq[i], "stances": first_stances().get("version")})
                  for i in ids}   # 항목마다 판정 정의 지문 — 프롬프트 3종이면 그 판도(한 프롬프트로 받은 저장 판정을 다시 쓰지 않게)
        effective[n] = list(ids)
        if reused_dec and n in dec_items and keep_first:
            # 저장된 1차 판정을 그대로 — 1차 심판을 부르지 않고 2차 심판만 바꿔 같은 1차 결과로 비교하는 도구(judge_item_rerun ·
            # judge_claude_compare)는 지문 확인 · 빠진 항목 묻기를 하지 않는다(지우면 1차 판정이 '답 없음'이 됨, Codex 검토 10-22)
            if str(n) in saved_fp:
                input_fp[str(n)] = saved_fp[str(n)]
            else:
                input_fp.pop(str(n), None)
            item_fp[str(n)] = dict(saved_item_fp.get(str(n)) or {})
            continue
        if reused_dec and n in dec_items:
            old = saved_fp.get(str(n))
            if (old and old != input_fp[str(n)]) or (not old and (has_list or not REPAIR_TRUST_OLD)):
                # 1차 심판 입력이 저장 때와 다르면(질문 · 답 · 첨부 원본 · 확인 목록 · 안내가 바뀜) 그 턴은 저장된 답을 버리고 다시 묻는다 — 지문 없는 옛 저장은
                # 문맥에 확인 목록이 붙는 턴, 또는 JUDGE_REPAIR_TRUST_OLD=0 일 때 (Codex 검토 10-22: 새 확인 목록을 받지 않은 판정을 다시 쓰던 것)
                dec_items.pop(n)
                first_used.pop(str(n), None)
            else:
                # --repair: 저장된 답에 없는 항목(10-22 부터 늘 보는 J5-3.a 등)과 판정 정의가 저장 때와 달라진 항목만 1차 심판에 묻는다 —
                # 턴을 통째로 건너뛰면 새 항목이 빠진다. 새 방식 저장인데 정의 지문이 없으면 확인할 수 없어 모두 다시, 지문 없는 옛 저장은
                # 믿되(REPAIR_TRUST_OLD) 그 항목에는 정의 지문을 붙이지 않는다 — 확인하지 않은 것을 확인한 것처럼 남기지 않게 (Codex 검토 10-22)
                old_items = saved_item_fp.get(str(n))
                saved_n = dec_items[n]
                valid = {i for i in ids if isinstance(saved_n.get(i), dict) and saved_n[i].get("choice") is not None}   # 답이 유효한 저장 항목만
                if old_items is not None:   # 새 방식 저장 — 정의 지문이 같으면 확인, 'trusted-old' 는 다시 REPAIR_TRUST_OLD 로
                    verified = {i for i in valid if old_items.get(i) == cur_fp[i]}
                    trusted = {i for i in valid if old_items.get(i) == TRUSTED_OLD and REPAIR_TRUST_OLD}
                elif old:                   # 새 방식 저장인데 정의 지문이 없음 — 확인할 수 없어 모두 다시
                    verified, trusted = set(), set()
                else:                       # 지문 없는 옛 저장(REPAIR_TRUST_OLD 일 때만 여기 옴) — 믿고 쓰되 표시를 남긴다
                    verified, trusted = set(), set(valid)
                item_fp[str(n)] = {**{i: cur_fp[i] for i in verified}, **{i: TRUSTED_OLD for i in trusted}}
                ids = [i for i in ids if i not in verified and i not in trusted]
                if not ids:
                    continue
        qd = {f"T{n}:{i}": allq[i] for i in ids}
        if FIRST_JUDGE_MODE == "stances":   # 사용자 10-26 저녁 — 크기로 Jev 또는 Decisions, 판정 프롬프트 3종 가중 다수결
            status, body, error, used, voted, attempts = stance_judge_votes(part, qd, jev, decisions, use_jev=small and not turn_has_files(part, n))
        elif FIRST_JUDGE_MODE == "situational":   # 회의 흐름(10-26) — 단일 질문 · 첨부 없음 · Jev 가 받는 크기면 교차, 아니면 Decisions 1번
            status, body, error, used, voted, attempts = situational_judge(part, qd, jev, decisions, cross=len(turns) == 1 and small and not turn_has_files(part, n))
        else:
            status, body, error, used, voted, attempts = first_judge_votes(part, qd, jev, decisions, use_jev=small and not turn_has_files(part, n), votes=FIRST_JUDGE_VOTES)
        fresh[str(n)] = used or "?"
        n_calls += attempts   # 실패한 시도도 호출 수에 넣는다 (Codex 검토 10-25)
        cached_turn = n in dec_items
        first_used[str(n)] = first_used.get(str(n)) or used
        if status == "OK":
            ans = (body or {}).get("answers") or {}
            got = {i: ans.get(f"T{n}:{i}") for i in ids}
            if cached_turn:   # 저장된 답에 더한 항목은 판정한 심판을 항목에 남긴다 — 턴의 심판(first_judge)은 저장된 답의 것 (Codex 검토 10-22)
                got = {i: ({**v, "judge": used} if isinstance(v, dict) else v) for i, v in got.items()}
            dec_items[n] = {**dec_items.get(n, {}), **got}
            item_fp.setdefault(str(n), {}).update({i: cur_fp[i] for i in ids if isinstance(got.get(i), dict) and got[i].get("choice") is not None})   # 유효한 답을 받은 항목만
            for used_k, b in voted:   # 심판이 실제로 돌려준 모델 이름(버전) — 별칭이 다른 버전으로 바뀌어도 알 수 있게 (10-26)
                if isinstance(b, dict) and isinstance(b.get("model"), str):
                    models_by.setdefault(used_k or "?", set()).add(b["model"])
            for used_k, b in voted:   # 표마다 쓴 사용량 · 호출 수를 그 표의 심판에 더한다 (Jev 실패 → Decisions 로 섞여도 제 심판에)
                usage_tokens += int(((b or {}).get("usage") or {}).get("input_tokens") or 0) if isinstance((b or {}).get("usage"), dict) else 0
                add_usage(usage_by.setdefault(used_k or "?", {}), (b or {}).get("usage"))
                attempts_by[used_k or "?"] = attempts_by.get(used_k or "?", 0) + 1
            attempts_failed += max(0, attempts - len(voted))
        else:
            dec_items[n] = {**dec_items.get(n, {}), **{i: None for i in ids}}
            dec_errors.append(f"T{n} {status} {str(error)[:120]}")
    for n in list(dec_items):   # 저장된 답에 지금 판정하지 않는 옛 항목 · 대화에서 없어진 턴이 있으면 뺀다 — 선별 · 합치기 모두 지금 것만 (Codex 검토 10-22)
        if n not in effective:
            dec_items.pop(n)
            first_used.pop(str(n), None)
        else:
            dec_items[n] = {i: v for i, v in dec_items[n].items() if i in effective[n]}
    calls["decisions"] = {"status": "OK" if not dec_errors else ("PARTIAL" if any(any(v for v in d.values()) for d in dec_items.values()) else "ERROR"),
                          "error": "; ".join(dec_errors), "seconds": round(time.monotonic() - t0, 1), "calls": n_calls, "votes": FIRST_JUDGE_VOTES, "first_judge_mode": FIRST_JUDGE_MODE,
                          **({"stances": first_stances().get("version"), "votes": len(first_stances()["stances"])} if FIRST_JUDGE_MODE == "stances" else {}), "attempts_by": attempts_by, "attempts_failed": attempts_failed, "input_tokens": usage_tokens,
                          "reused": reused_dec, "fresh_judge": fresh, "question_tokens": q_tokens, "first_judge": first_used, "usage": usage_by,
                          "models": {k: sorted(v) for k, v in models_by.items()}}
    (out / "decisions" / name).write_text(json.dumps({**{str(n): v for n, v in dec_items.items()}, "_first_judge": first_used, "_input_fp": input_fp, "_item_fp": item_fp}, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    # ② 걸린 항목 → Codex
    flagged: Dict[int, List[str]] = {}
    why: Dict[str, str] = {}
    for n, items in dec_items.items():
        for i, entry in items.items():
            if multi_ok is False and i.startswith("J6"):
                continue   # 멀티턴이 아닌 대화의 J6 는 Codex 에도 넘기지 않음 (10-21 밤)
            if codex_all:
                flag, reason = True, "교정용 전수"
            elif isinstance(entry, dict) and "cross_flag" in entry:   # 교차 확인한 칸(회의 흐름) — 두 심판이 같고 확실하면 끝
                flag, reason = bool(entry["cross_flag"]), entry["cross_flag"]
            else:
                flag, reason = screen_flag(i, entry)
            if (not flag and JUDGE_TARGET_ONLY and i in QUESTION_DETERMINED and isinstance(entry, dict)
                    and entry.get("choice") == "NOT_APPLICABLE" and i in (targets_for(q, n) or set())):
                flag, reason = True, "목표 기준인데 해당 없음 — 질문으로 정해지는 항목"   # (10-21)
            if flag:
                flagged.setdefault(n, []).append(i)
                why[f"T{n}:{i}"] = reason
    err_turns = error_case_turns(turns, code_measured)   # 에러 케이스 턴은 Codex 에 보내지 않는다 — 판정은 J1-1.a FAIL · 나머지 해당 없음 (10-25 밤)
    for n in err_turns:
        flagged.pop(n, None)
    n_items = sum(len(v) for v in dec_items.values())
    n_flagged = sum(len(v) for v in flagged.values())
    who = "·".join(sorted({u for u in first_used.values() if u})) or ("저장된 답" if reused_dec else "없음")
    if err_turns:
        say(f"   ⛔ {tag} 에러 케이스 턴 {sorted(err_turns)} — Codex 에 보내지 않음(J1-1.a 만 FAIL)")
    say(f"   🔎 {tag} 1차 {who} {calls['decisions']['status']} {calls['decisions']['seconds']}초 · 질문 {q_tokens or 0:,}토큰 · 항목 {n_items}개 중 Codex 로 {n_flagged}개"
        + (f" — {'; '.join(dec_errors)[:160]}" if dec_errors else ""))
    codex_items: Dict[int, Dict[str, Any]] = {}
    codex_sits: Dict[int, Dict[str, Any]] = {}
    saved = out / "codex" / name
    codex_section = bool(flagged and (gpt is not None or (reuse_codex and saved.exists())))   # --repair 는 Codex CLI 없이도 저장된 출력으로 합친다
    codex_plan: Optional[Dict[str, Any]] = None
    status: Any = None
    output: Any = None
    error = ""
    rounds, reused = 0, False
    search = False
    body_doc: Dict[str, Any] = {}
    tokens: Dict[str, Any] = {}
    images: List[Path] = []
    t1 = time.monotonic()
    saved_fp_file = out / "codex" / f"{name}.fp"
    codex_fp = ""
    trusted_codex = False
    counter = {"calls": 0}

    def ask_codex(ask_doc: Dict[str, Any]) -> Tuple[str, Any, str]:   # Codex 구간이 아니면 안 불린다
        return "ERROR", None, "Codex 구간 아님"
    if codex_section:
        # 웹 검색은 필요할 때만 — 질문의 자료 · 첨부는 이 평가를 위해 만든 가상 자료라 웹에 없다. 실제 세상의 사실(J5-1.a)이나
        # 출처 존재(J5-3.a)를 Codex 에 넘길 때만 켠다 (사용자 2026-10-21 밤)
        search = (not any((t.get("reference") or "").strip() for t in turns)
                  and any(i in WEB_SEARCH_ITEMS for v in flagged.values() for i in v))
        output: Any = None
        t1 = time.monotonic()
        body_doc = {**doc, "판정할_턴": sorted(flagged), "판정할_항목": {str(n): flagged[n] for n in sorted(flagged)}}
        tokens: Dict[str, Any] = {"input_tokens": 0, "output_tokens": 0}   # + Codex 가 주는 칸 전부(cached_input_tokens · reasoning_output_tokens …, 10-22 미팅 7번)
        images = attachment_images(turns, getattr(gpt, "files_dir", None))

        counter = {"calls": 0}   # 이 질문에 실제로 쓴 codex exec 횟수(묶음 호출은 대표 질문에만 1, 10-25)

        def ask_codex(ask_doc: Dict[str, Any]) -> Tuple[str, Any, str]:
            st, outp, err = gpt.judge(prompt + SCREEN_SUFFIX, json.dumps(ask_doc, ensure_ascii=False, indent=1), search, tag, output_schema=output_schema(), images=images)
            counter["calls"] += 1
            add_usage(tokens, getattr(gpt, "last_usage", None))
            return st, outp, err

        rounds, reused, error = 0, False, ""
        saved_fp_file = out / "codex" / f"{name}.fp"
        codex_fp = doc_fp({"doc": doc, "prompt": prompt + SCREEN_SUFFIX, "files": files_fp(turns),
                           "measured": {k: sorted(v.items()) for k, v in sorted((code_measured or {}).items())}})   # 질문 · 답 · 첨부 원본 · 확인 목록 · 프롬프트 · 잰 조건 (Codex 검토 10-22)
        # 지문 없는 옛 저장은 확인 목록이 붙지 않는 질문이고 REPAIR_TRUST_OLD 일 때만 믿고 쓴다 — 믿고 쓴 출력에는 지문을 붙이지 않는다 (Codex 검토 10-22)
        trusted_codex = not saved_fp_file.exists() and REPAIR_TRUST_OLD and not any("확인_목록" in t for t in doc["턴"])
        same_input = trusted_codex or (saved_fp_file.exists() and saved_fp_file.read_text(encoding="utf-8").strip() == codex_fp)
        if reuse_codex and saved.exists() and same_input and json.loads(saved.read_text(encoding="utf-8") or "null"):
            output, status, reused = json.loads(saved.read_text(encoding="utf-8")), "OK", True
            say(f"   🖥 {tag} Codex — 저장된 출력 다시 씀 ({saved.name}); 답하지 않았거나 인용이 틀린 항목만 다시 묻는다")
        elif gpt is None:   # Codex 없이 복구하는데 저장 때와 입력이 다름 — 저장된 출력을 쓰지 않고 오류로 남긴다(저장 파일은 그대로)
            status, output, error = "ERROR", None, "Codex 없음 — 저장된 출력은 질문 · 답 · 확인 목록 · 프롬프트가 달라 다시 쓸 수 없음"
            say(f"   🖥 {tag} {error}")
        else:
            codex_plan = {"doc": body_doc, "search": search, "images": images, "n_flagged": n_flagged, "multi": len(turns) > 1}
    elif flagged:
        calls["codex"] = {"status": "ERROR", "error": "Codex 없음", "seconds": 0, "items": n_flagged}
    else:
        calls["codex"] = {"status": "SKIPPED", "error": "", "seconds": 0, "items": 0}
    a_seconds = round(time.monotonic() - started, 1)   # 1차 심판 · 선별에 실제로 쓴 시간 (묶음 때 다른 질문을 기다린 시간은 넣지 않음, Codex 검토 10-25)
    return {"q": q, "provider": provider, "tag": tag, "turns": turns, "answers": answers, "doc": doc, "name": name, "out": out, "gpt": gpt,
            "a_seconds": a_seconds, "outside_seconds": 0.0,
            "flagged": flagged, "why": why, "n_items": n_items, "n_flagged": n_flagged, "dec_items": dec_items, "first_used": first_used,
            "calls": calls, "multi_ok": multi_ok, "code_fails": code_fails, "code_measured": code_measured, "codex_all": codex_all, "started": started,
            "codex_section": codex_section, "codex_plan": codex_plan, "status": status, "output": output, "error": error, "rounds": rounds, "reused": reused,
            "search": search, "body_doc": body_doc, "tokens": tokens, "images": images, "t1": t1, "saved": saved, "saved_fp_file": saved_fp_file,
            "codex_fp": codex_fp, "trusted_codex": trusted_codex, "ask_codex": ask_codex, "counter": counter, "codex_items": codex_items, "codex_sits": codex_sits,
            "batch": None}


def screen_stage_b(st: Dict[str, Any], status: Any, output: Any, error: str, rounds: int, reused: bool) -> Dict[str, Any]:
    """선별 판정 한 질문의 뒤 단계: 첫 Codex 결과(status · output)를 받아 빠진 항목 · 인용 불일치를 되묻고, 저장하고, ③ 합치기 → 결과 줄."""
    q, provider, tag, turns, answers, doc, name, out, gpt = (st[k] for k in ("q", "provider", "tag", "turns", "answers", "doc", "name", "out", "gpt"))
    flagged, why, n_items, n_flagged, dec_items, first_used = (st[k] for k in ("flagged", "why", "n_items", "n_flagged", "dec_items", "first_used"))
    calls, multi_ok, code_fails, code_measured, codex_all, started = (st[k] for k in ("calls", "multi_ok", "code_fails", "code_measured", "codex_all", "started"))
    search, body_doc, tokens, t1, saved, saved_fp_file, codex_fp, trusted_codex = (st[k] for k in ("search", "body_doc", "tokens", "t1", "saved", "saved_fp_file", "codex_fp", "trusted_codex"))
    ask_codex, counter, codex_items, codex_sits = st["ask_codex"], st["counter"], st["codex_items"], st["codex_sits"]
    b0 = time.monotonic()   # 이 단계의 실제 시간 — 되묻기 · 저장 · 합치기
    if st["codex_section"]:
        # 답하지 않은 (턴, 항목)과 인용이 원문에 없는 (턴, 항목)만 다시 묻는다 — 턴에 항목 하나만 있어도 완료로 보던 것(보류의 64%)과
        # 인용 불일치(27%)를 줄인다 (Codex 지적 10-20). 인용 불일치는 항목마다 한 번만 다시.
        reasked: set = set()
        misrule_asked: set = set()   # 연결 규칙 안내를 주고 다시 물은 (턴, 항목) — 다른 다시 묻기 이력과 따로
        dup_asked: set = set()   # 같은 답 구절로 FAIL 한 항목들을 다시 물은 (턴, 항목) (10-25 밤)
        overlap_asked: set = set()   # CODE 가 잰 형식 조건을 든 FAIL 을 다시 물은 (턴, 항목) (10-22 밤)
        multi_doc = len(turns) > 1
        plan = st.get("codex_plan")
        if multi_doc and status == "OK" and gpt is not None and plan is not None and not reused and not answered_items(output):
            # 멀티턴 첫 답이 비면 턴마다 다시 묻기 전에 대화 전체를 한 번 더 — 턴마다 묻기는 앞 대화를 매번 다시 보내 입력이 약 5배 (10-25 밤)
            say(f"   ↻ {tag} Codex 첫 답에 판정이 없음 — 대화 전체를 한 번 더 묻는다")
            t_call = time.monotonic()
            st2, out2, err2 = ask_codex({**plan["doc"], "주의": EMPTY_FIRST_NOTE})
            st["outside_seconds"] = st.get("outside_seconds", 0) + time.monotonic() - t_call
            rounds += 1
            if st2 == "OK" and answered_items(out2):
                output = out2
        # 멀티턴: 한 턴을 2번 다시 물어도 빈 항목이 줄지 않으면 그 턴은 건너뛰고 다음 빈 턴으로 — 한 턴만 상한까지 묻고 뒤 턴은 못 묻던 일
        # (10-22 Claude CLI 비교 Q2 gemini: 7턴만 15번 → 8~10턴 미질문, 109개 보류). 건너뛴 턴의 남은 항목은 다른 끝내 답하지 않은 항목처럼(판정 없음 → 보류).
        turn_asks: Dict[int, int] = {}   # 턴마다 다시 물은 횟수
        stalls: Dict[int, int] = {}      # 턴마다 다시 물어도 빈 항목이 줄지 않은 횟수
        skipped: set = set()
        cap = 3 * len(flagged) + 8 if multi_doc else len(flagged) + 4   # 일반 · 규칙 · 형식 세 종류의 다시 묻기(Codex 검토 10-22: 2N+6 으로는 10턴에서 뒤 턴이 남음)
        while status == "OK" and gpt is not None and rounds < cap:
            missing = missing_codex_items(output, flagged)
            bad = {k: v for k, v in bad_quote_items(output, turns, answers, flagged).items() if k not in reasked}
            quote_bad = list(bad)   # 인용 불일치로 다시 묻는 (턴, 항목) — 인용 위치 안내용 (10-25 밤)
            # J5-1.a '확인 못함'은 일반 지식인지 한 번 다시 묻는다 — 복구(옛 프롬프트 출력)뿐 아니라 새 판정에도 (10-21 밤)
            fact_unv = [k for k in unverified_fact_items(output, turns, answers, flagged) if k not in reasked]
            bad.update({k: ["J5-1.a 확인 못함 — 일반 지식인지 다시"] for k in fact_unv})
            undecided = [k for k in undecided_items(output, turns, answers, flagged) if k not in reasked]
            bad.update({k: ["J5-1.a 가 아닌 항목의 UNVERIFIED — 다시"] for k in undecided})   # 10-21 밤
            refail = [k for k in fail_items(output, turns, answers, flagged, REASK_FAIL_ITEMS) if k not in reasked]
            bad.update({k: ["정의의 예외를 짚어 FAIL 다시"] for k in refail})   # --reask-fail (10-22)
            contra = [k for k in contradiction_items(output, turns, answers, flagged) if k not in reasked]
            bad.update({k: ["사유와 판정이 반대 — 다시"] for k in contra})   # 10-22 기준 점검
            misrule = [k for k in misrule_items(output, turns, answers, flagged) if k not in misrule_asked]   # 다른 이유로 다시 물은 항목도 (Codex 검토 10-22)
            general = set(bad)   # 연결 규칙 오적용 말고 다른 이유로 다시 묻는 (턴, 항목) — 두 다시 묻기 이력을 서로 쓰지 않게 (Codex 검토 10-22)
            bad.update({k: ["연결 규칙을 대상이 아닌 항목에 씀 — 다시"] for k in misrule})   # 10-22 J1-4.b 재판정
            overlap = [k for k in overlap_items(output, turns, answers, flagged, code_measured or {}) if k not in overlap_asked]
            bad.update({k: ["CODE 가 잰 형식 조건을 FAIL 사유로 씀 — 다시"] for k in overlap})   # 10-22 밤
            deferred = [k for k in deferred_fail_items(output, turns, answers, flagged) if k not in reasked]   # 사유가 다른 항목에 넘긴 FAIL (10-25 밤)
            bad.update({k: ["사유가 그 오류를 다른 항목에 넘김 — 다시"] for k in deferred})
            dups = [k for k in duplicate_quote_items(output, turns, answers, flagged) if k not in dup_asked]   # 같은 답 구절로 FAIL 여럿 (10-25 밤)
            bad.update({k: ["같은 답 구절을 다른 항목의 FAIL 근거로도 씀 — 한 항목만"] for k in dups})
            targets: Dict[int, List[str]] = {}
            # 건너뛴 턴(빈 항목이 두 번 물어도 줄지 않음)은 빈 항목만 더 묻지 않고, 이미 답한 항목의 다시 묻기(인용 · 규칙 · 형식)는 계속 (Codex 검토 10-22)
            for n, items in [(n, v) for n, v in missing.items() if not (multi_doc and n in skipped)] + [(n, [i]) for (n, i) in bad]:
                targets.setdefault(n, [])
                targets[n] += [i for i in items if i not in targets[n]]
            if not targets:
                break
            rounds += 1
            # 멀티턴은 한 번에 한 턴씩, 그 턴까지의 대화만 보낸다 — 대화 전체를 주면 Codex 가 판정할 턴을 헷갈려 뒤 턴의 답으로 판정했다
            # (10-22 보류 점검: 인용 불일치 보류 41건 중 38건이 뒤 턴 답의 문장). 단일 질문은 예전처럼 반씩.
            ask_turns = [min(targets)] if multi_doc else sorted(targets)[:max(1, len(targets) // 2)]
            # 측정 안내를 한 번 준 항목은 다른 이유로 다시 물을 때도 계속 붙인다 — 안내가 빠지면 형식 FAIL 이 되살아나던 것 (Codex 검토 10-22)
            measured_now = [k for k in set(overlap) | overlap_asked if k[0] in ask_turns and k[1] in targets.get(k[0], [])]
            ask_base = {**body_doc, "턴": [t for t in doc["턴"] if t["턴"] <= max(ask_turns)]} if multi_doc else body_doc
            say(f"   ↪ {tag} Codex 다시 묻기({rounds}번째) — 답 없음 {sum(len(v) for v in missing.values())}개 · 인용 불일치 {len(bad)}개 → 턴 {ask_turns}")
            prev: Dict[str, List[Dict[str, Any]]] = {}   # 다시 묻는 항목의 이전 요소 번호 · 판정 — 같은 번호로 모든 요소를 다시 답하게 (Codex 검토 10-22)
            for t in (output or {}).get("turns") or []:
                if isinstance(t, dict) and t.get("turn") in ask_turns:
                    for e in t.get("items") or []:
                        if isinstance(e, dict) and "#" in str(e.get("id")) and js._base(e.get("id")) in targets.get(t["turn"], []):
                            prev.setdefault(f"T{t['turn']}:{js._base(e.get('id'))}", []).append(
                                {"id": e.get("id"), "decision": e.get("decision"), "reason": e.get("reason")})
            st2, out2, err2 = ask_codex({**ask_base, "판정할_턴": ask_turns, "판정할_항목": {str(n): targets[n] for n in ask_turns},
                                         **({"이전_판정": prev, "이전_판정_안내": PREV_NOTE} if prev else {}),
                                         **({"주의": " ".join(x for x, ks in ((UNDECIDED_NOTE, undecided), (FACT_NOTE, fact_unv), (REASK_NOTE, refail), (CONTRADICTION_NOTE, contra),
                                                                                 (MISRULE_NOTE, misrule), (MEASURED_NOTE, measured_now), (DUP_QUOTE_NOTE, dups),
                                                                                 (DEFER_NOTE, deferred))
                                                             if any(k[0] in ask_turns for k in ks))}
                                            if any(k[0] in ask_turns for k in undecided + fact_unv + refail + contra + misrule + measured_now + dups + deferred) else {}),
                                         **({"다른_항목에_넘긴_FAIL": {f"T{n}": [i for (m, i) in deferred if m == n] for n in ask_turns if any(m == n for (m, _i) in deferred)}}
                                            if any(k[0] in ask_turns for k in deferred) else {}),
                                         **({"같은_구절_항목": {f"T{n}": [i for (m, i) in dups if m == n] for n in ask_turns if any(m == n for (m, _i) in dups)}}
                                            if any(k[0] in ask_turns for k in dups) else {}),
                                         **({"인용_위치": locs, "인용_안내": QUOTE_SCOPE_NOTE}
                                            if (locs := quote_locations(output, turns, answers, [k for k in quote_bad if k[0] in ask_turns])) else {}),
                                         **({"프로그램이_잰_조건": {f"T{n}": {fam: {"결과": v, "조건": t} for fam, (v, t) in ((code_measured or {}).get(next((r.get("case_id") for r in turns if ji._turn(r) == n), "")) or {}).items()}
                                                                for n in ask_turns}}
                                            if measured_now else {})})
            if st2 != "OK":
                error = err2
                break
            replace = {(n, i) for n in ask_turns for i in targets[n]}
            reasked |= {k for k in general if k in replace}
            misrule_asked |= {k for k in misrule if k in replace}
            overlap_asked |= {k for k in overlap if k in replace}
            dup_asked |= {k for k in dups if k in replace}
            reasked |= {k for k in deferred if k in replace}   # 넘긴 FAIL 은 한 번만 다시 (10-25 밤)
            output = merge_codex_output(output, out2, replace)
            if multi_doc:
                n = ask_turns[0]
                turn_asks[n] = turn_asks.get(n, 0) + 1
                left = len(missing_codex_items(output, flagged).get(n, []))
                # 정체는 빈 항목이 실제로 남아 있는데 줄지 않을 때만 — 인용 · 규칙 · 형식 다시 묻기만 두 번 해도 턴을 건너뛰던 것 (Codex 검토 10-22)
                stalls[n] = (stalls.get(n, 0) + 1 if left >= len(missing[n]) else 0) if missing.get(n) else stalls.get(n, 0)
                if stalls[n] >= 2:
                    skipped.add(n)
                    say(f"   ↪ {tag} 턴 {n} 은 2번 물어도 답이 늘지 않아 건너뜀 — 빈 항목 {left}개 남김")
        missing = missing_codex_items(output, flagged) if status == "OK" else {}
        if status == "OK" and not any((t or {}).get("items") for t in ((output or {}).get("turns") or []) if isinstance(t, dict)):
            # 결과를 통째로 비워 냄(판정을 아예 안 함) — '확인 못 함' 보류가 아니라 판정 실행 오류로 (10-21: 파일을 못 읽은 Codex 가 빈 결과를 낸 일)
            status, missing = "ERROR", {}
            error = "Codex 가 판정 결과를 비워 냄: " + str(((output or {}).get("conclusion") or {}).get("reason") or "")[:150]
        calls["codex"] = {"status": status, "error": error if status != "OK" else (f"끝내 답하지 않은 항목 {sum(len(v) for v in missing.values())}개" if missing else ""),
                          "seconds": round(st["outside_seconds"] + (time.monotonic() - b0), 1), "search": search, "items": n_flagged, "rounds": rounds, "reused": reused,
                          "missing_turns": sorted(missing), "missing_items": {str(n): v for n, v in missing.items()},
                          "turn_asks": {str(n): c for n, c in turn_asks.items()}, "skipped_turns": sorted(skipped),
                          "rounds_cap": cap if gpt is not None else None, "cap_hit": bool(gpt is not None and status == "OK" and rounds >= cap),
                          "calls": counter["calls"], **({"batch": st["batch"]} if st.get("batch") else {}), **tokens}   # calls = 실제 codex exec 횟수(묶음은 대표 질문에 1)
        say(f"   🖥 {tag} Codex {status} {calls['codex']['seconds']}초 · 호출 {counter['calls']}번(라운드 {rounds})" + (f" — {str(calls['codex']['error'])[:160]}" if calls['codex']['error'] else ""))
        if output is not None or not saved.exists():   # 저장된 출력을 빈 값으로 덮지 않는다
            (out / "codex" / name).write_text(json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8")
        if output is not None and not (reused and trusted_codex):
            saved_fp_file.write_text(codex_fp, encoding="utf-8")   # 이 출력이 받은 질문 · 답 · 첨부 원본 · 확인 목록 · 프롬프트의 지문
        if calls["codex"]["status"] == "OK":
            codex_items = js.check_gpt(output, turns, answers, flagged)
            for t in (output or {}).get("turns") or []:
                if isinstance(t, dict) and isinstance(t.get("turn"), int):
                    codex_sits[t["turn"]] = {"situations": [str(s.get("id")) for s in t.get("situations") or [] if isinstance(s, dict)],
                                             "evidence": {str(s.get("id")): s.get("evidence") for s in t.get("situations") or [] if isinstance(s, dict)}}
    # ③ 합치기
    final: Dict[int, Dict[str, Dict[str, Any]]] = {}
    turn_out: Dict[str, Any] = {}
    agree = total = 0
    for row in turns:
        n = ji._turn(row)
        items: Dict[str, Dict[str, Any]] = {}
        for i, entry in (dec_items.get(n) or {}).items():
            if multi_ok is False and i.startswith("J6"):
                continue   # 저장된 Decisions 답(--repair)에 J6 가 있어도 멀티턴이 아니면 판정하지 않음 (10-21 밤)
            is_flagged = i in (flagged.get(n) or [])
            cv = (codex_items.get(n) or {}).get(i) if is_flagged else None
            judge = (entry.get("judge") if isinstance(entry, dict) else None) or first_used.get(str(n))   # 항목을 판정한 1차 심판(--repair 로 더한 항목은 따로, 10-22)
            label = {"jev": "Jev", "jev+decisions": "Jev·Decisions"}.get(judge or "", "Decisions")
            dec, how = combine_screen(cv, entry, is_flagged, item=i, label=label, codex_incomplete=bool(error))   # 재질문이 실패했으면 '끝내 답 없음'이 아님 (Codex 리뷰 10-25 밤)
            if dec is None:
                continue
            if is_flagged and cv and cv.get("decision") is not None and isinstance(entry, dict):
                total += 1
                agree += (cv.get("decision") == entry.get("choice"))
            e = ((cv or {}).get("entries") or [{}])
            fail_e = next((x for x in e if x.get("decision") == "FAIL"), e[0])
            items[i] = {"decision": dec, "how": how, "codex": (cv or {}).get("decision"), "jev": (entry or {}).get("choice") if isinstance(entry, dict) else None,
                        "second_judge": judge or "decisions", "decisions_prob": (entry or {}).get("probabilities") if isinstance(entry, dict) else None,
                        "decisions_confidence": (entry or {}).get("confidence") if isinstance(entry, dict) else None,
                        "first_votes": (entry or {}).get("votes") if isinstance(entry, dict) else None,
                        "first_vote_split": bool((entry or {}).get("vote_split")) if isinstance(entry, dict) else False,
                        **({"first_vote_detail": entry["vote_detail"]} if isinstance(entry, dict) and entry.get("vote_detail") else {}),
                        **({"first_cross": entry["cross"]} if isinstance(entry, dict) and entry.get("cross") else {}),
                        "flagged": why.get(f"T{n}:{i}") or "", "jev_reason": None, "problems": (cv or {}).get("problems") or [], "notes": (cv or {}).get("notes") or [],
                        "reason": fail_e.get("reason"), "answer_quote": fail_e.get("answer_quote"), "question_quote": fail_e.get("question_quote"),
                        "entries": (cv or {}).get("entries") or []}
        apply_guards(items, doc, n)
        if n in error_case_turns(turns, code_measured):
            mark_error_case_turn(items)   # 에러 케이스 턴 — J1-1.a 만 FAIL, 나머지 해당 없음 (10-25 밤)
        final[n] = items
        turn_out[str(n)] = {"situations_judge": (codex_sits.get(n) or {}).get("situations") or [], "evidence": (codex_sits.get(n) or {}).get("evidence") or {},
                            "problems": [], "items": items}
    judge_error = ""
    if calls["decisions"]["status"] == "ERROR" and calls["codex"]["status"] != "OK":
        semantic, judge_error = "HOLD", "Decisions·Codex 모두 실패 — 다시 판정 필요"
    elif flagged and calls["codex"]["status"] not in ("OK", "SKIPPED"):   # 걸린 항목을 Codex 가 못 봤으면 Decisions 만으로 PASS 를 내지 않는다 (Codex 지적 10-20)
        semantic, judge_error = "HOLD", f"Codex 판정 실행 오류 — 다시 판정 필요: {str(calls['codex'].get('error') or '')[:120]}"
    else:
        semantic = verdict_of(final)
    verdict = question_verdict(code_fails, semantic)
    line = {"version": SCREEN_VERSION, "mode": "screen", "provider": provider, "question": q, "turns": len(turns), "verdict": verdict, "semantic": semantic,
            "judge_error": judge_error or None, "reason": judge_error or None,
            "code_fails": code_fails, "codex_conclusion": None, "jev_codex_agree": [agree, total],
            "seconds": round(st["a_seconds"] + st["outside_seconds"] + (time.monotonic() - b0), 1),   # 이 질문에 실제로 쓴 시간(묶음 호출은 대표 질문에만)
            "screen": {"items": n_items, "flagged": n_flagged, "fail_prob_min": SCREEN_FAIL_PROB, "confident": SCREEN_CONFIDENT,
                       "second_judge_confident": SECOND_JUDGE_CONFIDENT, "codex_all": codex_all},
            "calls": calls, "tokens": question_tokens_used(calls), "turn_results": turn_out}   # 질문당 판정 토큰 (10-23)
    fails = [f"T{n}:{i}" for n, p in final.items() for i, v in p.items() if v["decision"] == "FAIL"]
    holds = [f"T{n}:{i}" for n, p in final.items() for i, v in p.items() if v["decision"] == "UNVERIFIED"]
    say(f"■ {tag}: {verdict} · {line['seconds']:.0f}초 · 항목 {n_items} → Codex {n_flagged}"
        + (f" · CODE FAIL {' '.join(code_fails)}" if code_fails else "") + (f" · FAIL {' '.join(fails)}" if fails else "") + (f" · 보류 {' '.join(holds)}" if holds else ""))
    return line


def screen_one(q: int, provider: str, turns: List[Dict[str, Any]], answers: Dict[str, str], deliveries: Dict[str, str],
               code_fails: List[str], prompt: str, fixed: Dict[str, Any], gpt: Any, decisions: Any, out: Path,
               codex_all: bool = False, reuse_codex: bool = False, reuse_decisions: bool = False, jev: Any = None,
               keep_first: bool = False, code_measured: Optional[Dict[str, Dict[str, Tuple[str, str]]]] = None) -> Dict[str, Any]:
    """선별 판정 한 질문(질문마다 Codex 를 따로 부르는 기본 흐름): stage A → 첫 Codex 호출 → stage B. 묶음 호출은 main 의 codex_batch_round."""
    st = screen_stage_a(q, provider, turns, answers, deliveries, code_fails, prompt, fixed, gpt, decisions, out, codex_all, reuse_codex, reuse_decisions,
                        jev, keep_first, code_measured)
    if FIRST_ONLY:
        return first_only_line(st)
    status, output, error, rounds, reused = st["status"], st["output"], st["error"], st["rounds"], st["reused"]
    plan = st["codex_plan"]
    if plan is not None:
        say(f"   🖥 {st['tag']} Codex CLI — 걸린 항목 {plan['n_flagged']}개(턴 {sorted(st['flagged'])}) · 웹 검색 {'켬' if plan['search'] else '끔'} …")
        t_call = time.monotonic()
        status, output, error = st["ask_codex"](plan["doc"])
        st["outside_seconds"] += time.monotonic() - t_call
        rounds = 1
    return screen_stage_b(st, status, output, error, rounds, reused)


def first_only_line(st: Dict[str, Any]) -> Dict[str, Any]:
    """--first-only 의 결과 줄 — 1차 판정은 decisions/<질문>.json 에 있고, 질문 판정(verdict)은 없다(Codex 를 부르지 않음)."""
    return {"version": SCREEN_VERSION, "provider": st["provider"], "question": st["q"], "turns": len(st["turns"]), "status": "first_only", "verdict": None,
            "first_judge_mode": FIRST_JUDGE_MODE, "votes": (len(first_stances()["stances"]) if FIRST_JUDGE_MODE == "stances" else FIRST_JUDGE_VOTES),
            **({"first_judge_stances": first_stances().get("version")} if FIRST_JUDGE_MODE == "stances" else {}), "first_used": st["first_used"], "calls": st["calls"],
            "n_items": st["n_items"], "n_flagged": st["n_flagged"], "seconds": st["a_seconds"]}


#: 멀티턴 첫 답이 빈 경우의 다시 묻기 안내 (10-25 밤: v4-r8 멀티턴 37개 중 18개의 첫 답이 4~7초 · 출력 100토큰 미만 — 크기와 상관없음)
EMPTY_FIRST_NOTE = ("앞선 답에 판정이 하나도 없었습니다. '판정할_턴' 의 모든 턴에 대해 '판정할_항목' 의 항목을 하나도 빼지 않고 답하세요 — "
                    "turns 를 비워 두면 안 됩니다.")


def answered_items(output: Any) -> int:
    """Codex 출력에 든 항목 판정 수 — 0 이면 빈 답."""
    if not isinstance(output, dict):
        return 0
    return sum(len(t.get("items") or []) for t in output.get("turns") or [] if isinstance(t, dict))


def first_call_alone(st: Dict[str, Any]) -> None:
    """질문 하나의 첫 Codex 호출(묶지 않고) — 예외는 그 질문의 오류로만 남기고 다음 질문으로 (Codex 검토 10-25)."""
    plan = st["codex_plan"]
    t_call = time.monotonic()
    try:
        st["status"], st["output"], st["error"] = st["ask_codex"](plan["doc"])
    except Exception as exc:
        st["status"], st["output"], st["error"] = "ERROR", None, f"Codex 호출 오류: {type(exc).__name__}: {str(exc)[:160]}"
    st["outside_seconds"] += time.monotonic() - t_call
    st["rounds"] = 1


BATCH_NOTE = """

# 묶음 입력 (이 호출에만 해당)
- 이번 [입력] 은 질문 하나가 아니라 **질문 여러 개**입니다: {"질문_수": N, "질문들": [<입력 형식의 JSON 하나>, ...]}.
- 질문마다 위 지시문대로 **독립적으로** 판정하세요(다른 질문의 내용 · 판정을 끌어오지 않음). 각 질문의 "판정할_항목" · "판정할_턴" 은 그 질문 것만 봅니다.
- 출력은 {"questions": [{"질문번호": <그 질문의 질문번호>, "turns": [...], "conclusion": {...}}, ...]} 하나 — 질문 수와 순서가 [입력] 과 같아야 하고,
  빠지는 질문이 없어야 합니다. 각 원소의 turns · conclusion 은 위 '출력 형식' 그대로입니다.
"""
#: 첫 Codex 호출 묶음 크기 — 단일 질문(첨부 이미지 없음)만 묶는다. 1 이면 질문마다(전과 같음). .env CODEX_BATCH 또는 --codex-batch (10-25)
CODEX_BATCH = max(1, int(_env_float("CODEX_BATCH", 10)))   # 기본 10 — Q11~Q30 × 4 비교(10-25 밤)에서 Codex 호출 53→35 · 입력 토큰 4.6M→3.4M · 항목 일치 97.6%
#: 묶음 하나에 넣는 질문 문서(JSON) 글자 합의 상한 — 10-25 밤 Q21~Q30 묶음(Q29 · Q30 이 각 53만 자, 합 225만 자)을 Codex 가 "Input exceeds the maximum" 으로
#: 거절해 질문마다 따로 묻는 길로 빠짐. 단독 호출은 53만 자(입력 33.5만 토큰)까지 됐으므로 40만 자(≈ 25만 토큰)에서 끊는다. 혼자서도 넘는 질문은 혼자 간다.
CODEX_BATCH_CHARS = max(50_000, int(_env_float("CODEX_BATCH_CHARS", 400_000)))


def batch_chunks(pending: List[int], is_multi: Any, size: int) -> List[List[int]]:
    """묶음 흐름의 덩어리 — 멀티턴 질문은 어차피 혼자 Codex 에 가므로 제 덩어리로 두어 끝나는 대로 저장되게 하고(10-25 밤: 묶음 10 때문에 멀티턴 10개의
    결과가 한꺼번에 늦게 저장되던 것), 단일 질문만 size 개씩 묶는다. 순서는 그대로."""
    out: List[List[int]] = []
    cur: List[int] = []
    for q in pending:
        if is_multi(q):
            if cur:
                out.append(cur)
                cur = []
            out.append([q])
            continue
        cur.append(q)
        if len(cur) >= max(1, size):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def batch_groups(plain: List[Dict[str, Any]], size: int, max_chars: int) -> List[List[Dict[str, Any]]]:
    """묶을 질문들을 개수(size)와 글자 합(max_chars)으로 나눈다 — 순서는 그대로, 상한을 넘기는 질문부터 새 묶음."""
    groups: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    cur_chars = 0
    for st in plain:
        n = len(json.dumps(st["codex_plan"]["doc"], ensure_ascii=False))
        st["codex_plan"]["chars"] = n
        if cur and (len(cur) >= max(1, size) or cur_chars + n > max_chars):
            groups.append(cur)
            cur, cur_chars = [], 0
        cur.append(st)
        cur_chars += n
    if cur:
        groups.append(cur)
    return groups


def batch_output_schema() -> Dict[str, Any]:
    single = output_schema()
    item = {"type": "object", "additionalProperties": False, "required": ["질문번호", "turns", "conclusion"],
            "properties": {"질문번호": {"type": "integer"}, **single["properties"]}}
    return {"type": "object", "additionalProperties": False, "required": ["questions"], "properties": {"questions": {"type": "array", "items": item}}}


def codex_batch_round(states: List[Dict[str, Any]], gpt: Any, prompt: str, size: int) -> None:
    """여러 질문(stage A 끝난 상태)의 **첫 Codex 호출**을 묶는다. 묶는 것은 첨부 이미지가 없는 단일 질문뿐 — 멀티턴 · 첨부는 따로 1번씩.
    결과는 각 상태의 status · output · error · rounds 에 넣는다(되묻기는 stage B 가 질문마다). 묶음 호출의 사용량 · 호출 1번은 대표(첫) 질문에."""
    need = [st for st in states if st["codex_plan"] is not None]
    alone = [st for st in need if st["codex_plan"]["multi"] or st["codex_plan"]["images"]]
    plain = [st for st in need if st not in alone]
    for st in alone:
        plan = st["codex_plan"]
        say(f"   🖥 {st['tag']} Codex CLI — 걸린 항목 {plan['n_flagged']}개(턴 {sorted(st['flagged'])}) · 웹 검색 {'켬' if plan['search'] else '끔'} …")
        first_call_alone(st)
    schema = batch_output_schema()
    for group in batch_groups(plain, size, CODEX_BATCH_CHARS):   # 개수와 글자 합(CODEX_BATCH_CHARS)으로 나눔 — 큰 질문(롱컨텍스트)은 혼자
        if len(group) == 1:
            st = group[0]
            plan = st["codex_plan"]
            say(f"   🖥 {st['tag']} Codex CLI — 걸린 항목 {plan['n_flagged']}개 · 웹 검색 {'켬' if plan['search'] else '끔'} …")
            first_call_alone(st)
            continue
        lead = group[0]
        search = any(st["codex_plan"]["search"] for st in group)
        payload = {"질문_수": len(group), "질문들": [st["codex_plan"]["doc"] for st in group]}
        tag = f"묶음 {lead['provider']} Q{group[0]['q']}~Q{group[-1]['q']}"
        say(f"   🖥 {tag} Codex CLI — 질문 {len(group)}개를 한 번에({sum(st['codex_plan'].get('chars') or 0 for st in group):,}자) · 걸린 항목 {sum(st['codex_plan']['n_flagged'] for st in group)}개 · 웹 검색 {'켬' if search else '끔'} …")
        t_call = time.monotonic()
        try:
            status, body, error = gpt.judge(prompt + SCREEN_SUFFIX + BATCH_NOTE, json.dumps(payload, ensure_ascii=False, indent=1), search, tag, output_schema=schema)
        except Exception as exc:   # 묶음 호출이 죽어도 질문마다 따로 묻는 길로 (아래 '묶음 답에 없음')
            status, body, error = "ERROR", None, f"묶음 호출 오류: {type(exc).__name__}: {str(exc)[:160]}"
        usage = getattr(gpt, "last_usage", None)
        add_usage(lead["tokens"], usage)   # 묶음 사용량 · 호출 1번 · 시간은 대표 질문에 (합계는 맞고, 질문별 배분은 하지 않음)
        lead["counter"]["calls"] += 1
        lead["outside_seconds"] += time.monotonic() - t_call
        got: Dict[int, Dict[str, Any]] = {}
        if status == "OK" and isinstance(body, dict):
            for r in body.get("questions") or []:
                if isinstance(r, dict) and isinstance(r.get("질문번호"), int):
                    got[r["질문번호"]] = {"turns": r.get("turns") or [], "conclusion": r.get("conclusion")}
        for st in group:
            st["batch"] = {"size": len(group), "lead": st is lead, "questions": [x["q"] for x in group], "chars": sum(x["codex_plan"].get("chars") or 0 for x in group)}
            if st["q"] in got:
                st["status"], st["output"], st["error"], st["rounds"] = "OK", got[st["q"]], "", 1
            else:
                # 묶음 답에 이 질문이 없음 — 질문마다 따로 한 번 더 (되묻기 상한에 포함)
                say(f"   ↪ {st['tag']} 묶음 답에 없음({status} {str(error)[:80]}) — 따로 묻는다")
                first_call_alone(st)
#: 첫 Codex 호출 한 번(--codex-one-call) 안내 — 묶음(BATCH_NOTE)과 같되 모델이 달라도 같은 질문번호가 여러 번 나오므로 판정_번호로 맞춘다 (사용자 10-26)
ONE_CALL_NOTE = """

# 한 번에 판정 (이 호출에만 해당)
- 이번 [입력] 은 판정할 문서 **여러 개**입니다: {"문서_수": N, "문서들": [{"판정_번호": k, <입력 형식의 JSON 하나>}, ...]}.
- 같은 질문번호가 여러 번 나올 수 있습니다(같은 질문에 서로 다른 AI 가 한 답). 문서마다 위 지시문대로 **독립적으로** 판정하세요 — 다른 문서의 답 · 판정을
  끌어오지 않습니다. 각 문서의 "판정할_항목" · "판정할_턴" 은 그 문서 것만 봅니다.
- 출력은 {"questions": [{"판정_번호": <그 문서의 판정_번호>, "질문번호": <그 문서의 질문번호>, "turns": [...], "conclusion": {...}}, ...]} 하나 —
  문서 수와 순서가 [입력] 과 같아야 하고 빠지는 문서가 없어야 합니다. 각 원소의 turns · conclusion 은 위 '출력 형식' 그대로입니다.
"""
def one_call_file_note(n: int) -> str:
    """파일 방식 한 번 호출의 [입력] 자리 안내 — 첫 시도(10-26)는 지시문 · 입력을 모두 파일로 주었더니 셸 출력이 잘린 줄 모르고 앞 11개 문서만 판정했다.
    지시문은 표준 입력으로 통째로 주고, 입력 파일은 문서 수를 밝혀 10개씩 끝까지 읽게 한다. 둘째 시도(10-26)는 PowerShell 로 읽어 한글이 깨졌다
    (한국어 Windows 의 PowerShell 5.1 출력은 CP949 · 샌드박스의 제한 언어 모드라 출력 인코딩도 못 바꿈) — 판정을 돌리는 Python 으로 읽게 한다."""
    py = sys.executable or "python"
    return (f"[입력] 은 이 작업 폴더의 `판정_입력.json` 파일입니다(UTF-8 · 문서 {n}개 · 판정_번호 1~{n}). 셸로 읽되, PowerShell 의 Get-Content 출력은 한글이 깨지므로 "
            "Python 으로 읽으세요. 한 번에 읽은 출력이 길면 잘리므로 문서를 10개씩 나눠 읽습니다 — 예) "
            f"`\"{py}\" -X utf8 -c \"import json; d=json.load(open('판정_입력.json', encoding='utf-8')); print(json.dumps(d['문서들'][0:10], ensure_ascii=False, indent=1))\"`, "
            "다음은 [10:20], … "
            f"마지막 문서까지. 판정_번호 1~{n} 을 모두 읽기 전에는 답하지 말고, 출력의 questions 는 정확히 {n}개여야 합니다. "
            "읽은 내용이 잘렸거나 문서 번호가 건너뛰었으면 그 범위를 다시 읽으세요. 파일을 고치거나 새 파일을 만들지 마세요.")
#: 한 번 호출의 시간 제한(초) — 묶음 10개가 2~4분이라 문서 100개면 한 시간 안쪽. .env CODEX_ONE_CALL_TIMEOUT
ONE_CALL_TIMEOUT = _env_float("CODEX_ONE_CALL_TIMEOUT", 3600)


def one_call_output_schema() -> Dict[str, Any]:
    single = output_schema()
    item = {"type": "object", "additionalProperties": False, "required": ["판정_번호", "질문번호", "turns", "conclusion"],
            "properties": {"판정_번호": {"type": "integer"}, "질문번호": {"type": "integer"}, **single["properties"]}}
    return {"type": "object", "additionalProperties": False, "required": ["questions"], "properties": {"questions": {"type": "array", "items": item}}}


def codex_one_call(states: List[Dict[str, Any]], gpt: Any, prompt: str, out: Path, mode: str = "file") -> Dict[str, Any]:
    """첫 Codex 호출을 **한 번**으로 (사용자 10-26 시험) — 묶음(CODEX_BATCH 개씩) 대신 첨부 이미지 없는 단일 질문 전부(모델이 달라도)를 한 입력으로.
    mode 'file': 판정 폴더 codex_io/한번에/ 에 판정_지시문.txt · 판정_입력.json 을 저장하고 Codex 가 그 파일을 읽어 판정 · 'stdin': 같은 내용을 표준 입력으로.
    멀티턴 · 첨부 이미지 질문은 묶음 흐름과 같이 따로 1번씩. 답에 빠진 문서는 따로 한 번 더. 되묻기는 stage B 가 질문마다. 호출 기록은 codex_io/한번에_기록.json."""
    need = [st for st in states if st["codex_plan"] is not None]
    alone = [st for st in need if st["codex_plan"]["multi"] or st["codex_plan"]["images"]]
    plain = [st for st in need if st not in alone]
    for st in alone:
        plan = st["codex_plan"]
        say(f"   🖥 {st['tag']} Codex CLI(따로 — {'멀티턴' if plan['multi'] else '첨부 이미지'}) — 걸린 항목 {plan['n_flagged']}개 …")
        first_call_alone(st)
    record: Dict[str, Any] = {"mode": mode, "documents": len(plain), "alone": len(alone)}
    if len(plain) <= 1:
        for st in plain:
            first_call_alone(st)
        return record
    payload = {"문서_수": len(plain), "문서들": [{"판정_번호": k, **st["codex_plan"]["doc"]} for k, st in enumerate(plain, 1)]}
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    search = any(st["codex_plan"]["search"] for st in plain)
    workdir = out / "codex_io" / "한번에" if mode == "file" else None
    lead = plain[0]
    say(f"   🖥 한 번 호출({'파일' if mode == 'file' else '표준 입력'}) — 문서 {len(plain)}개({len(text):,}자) · 걸린 항목 {sum(st['codex_plan']['n_flagged'] for st in plain)}개"
        f" · 웹 검색 {'켬' if search else '끔'}" + (f" · 입력 파일 {workdir / '판정_입력.json'}" if workdir else "") + " …")
    t_call = time.monotonic()
    try:
        status, body, error = gpt.judge(prompt + SCREEN_SUFFIX + ONE_CALL_NOTE, text, search, "한번에", output_schema=one_call_output_schema(), workdir=workdir,
                                        timeout=ONE_CALL_TIMEOUT, input_note=one_call_file_note(len(plain)) if mode == "file" else None)
    except Exception as exc:   # 한 번 호출이 죽어도 문서마다 따로 묻는 길로
        status, body, error = "ERROR", None, f"한 번 호출 오류: {type(exc).__name__}: {str(exc)[:160]}"
    seconds = round(time.monotonic() - t_call, 1)
    usage = getattr(gpt, "last_usage", None)
    add_usage(lead["tokens"], usage)   # 사용량 · 호출 1번 · 시간은 대표(첫) 문서에 — 묶음과 같음
    lead["counter"]["calls"] += 1
    lead["outside_seconds"] += seconds
    got: Dict[int, Dict[str, Any]] = {}
    if status == "OK" and isinstance(body, dict):
        for r in body.get("questions") or []:
            if isinstance(r, dict) and isinstance(r.get("판정_번호"), int):
                got[r["판정_번호"]] = r
    missing = []
    for k, st in enumerate(plain, 1):
        st["batch"] = {"size": len(plain), "lead": st is lead, "one_call": mode, "chars": len(text)}
        r = got.get(k)
        if r is not None and r.get("질문번호") == st["q"]:
            st["status"], st["output"], st["error"], st["rounds"] = "OK", {"turns": r.get("turns") or [], "conclusion": r.get("conclusion")}, "", 1
        else:
            missing.append(st["tag"])
            say(f"   ↪ {st['tag']} 한 번 호출 답에 없음({status} {str(error)[:80]}) — 따로 묻는다")
            first_call_alone(st)
    record.update({"chars": len(text), "status": status, "error": str(error or "")[:300], "seconds": seconds, "usage": usage,
                   "answered": len(got), "missing": missing, "input_file": str(workdir / "판정_입력.json") if workdir else None})
    try:
        (out / "codex_io").mkdir(parents=True, exist_ok=True)
        (out / "codex_io" / "한번에_기록.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    say(f"   ✅ 한 번 호출 {status} — {seconds:.0f}초 · 답한 문서 {len(got)}/{len(plain)}" + (f" · 따로 다시 {len(missing)}개" if missing else ""))
    return record


JEV_TRIM_CHARS = 35_000   # Jev 입력 예산(글자) — 약 5만 자부터 Jev 가 max_tokens_exceeded (10-19·10-20)
TRIM_MARK = "…[중략: {n:,}자 생략 — 전문은 Codex 가 본다]…"


def _cut(holder: Dict[str, Any], key: str, keep: int) -> int:
    text = holder[key]
    if len(text) <= keep:
        return 0
    head, tail = int(keep * 0.6), keep - int(keep * 0.6)
    cut = len(text) - head - tail
    holder[key] = text[:head] + TRIM_MARK.format(n=cut) + text[-tail:]
    return cut


def turn_has_files(part: Dict[str, Any], n: int) -> bool:
    return any(t.get("첨부파일") for t in part.get("턴") or [] if t.get("턴") == n)


def second_judge(part: Dict[str, Any], questions: Dict[str, Any], jev: Any, decisions: Any,
                 budget: int = JEV_TRIM_CHARS, prefer_decisions: bool = False) -> Tuple[str, Any, str, Optional[str], int]:
    """J1~J4 둘째 심판 고르기 (사용자 결정 2026-10-20): ⓪ 첨부가 있는 턴(prefer_decisions)은 Decisions(OpenAI) 먼저 — 글로 옮긴 내용에 더해 이미지 원본도 봄
    ① 예산 안이면 Jev 원문 ② Jev 가 한도를 넘거나 실패하면 Decisions 원문 ③ Decisions 가 없거나 실패하면 Jev 에 줄인 자료(참고만)
    ④ 그것도 안 되면 둘째 심판 없음 — Codex 판정만. 돌려주는 값: (status, body, error, 쓴 심판 'jev'·'decisions'·'jev-trimmed'·None, 줄인 글자 수)."""
    chars = js.state_chars(part)
    errors: List[str] = []
    if prefer_decisions and decisions is not None:
        status, body, error = decisions.judge(part, questions)
        if status == "OK":
            return status, body, "", "decisions", 0
        errors.append(f"Decisions(첨부 우선) {status} {error}")
        decisions = None   # 아래서 또 부르지 않는다
    if jev is not None and chars <= budget:
        status, body, error = jev.judge(part, questions)
        if status == "OK":
            return status, body, "", "jev", 0
        errors.append(f"Jev {status} {error}")
    elif jev is None:
        errors.append("Jev 없음")
    else:
        errors.append(f"Jev 예산 초과 {chars:,}자 > {budget:,}자")
    if decisions is not None:
        status, body, error = decisions.judge(part, questions)
        if status == "OK":
            return status, body, "", "decisions", 0
        errors.append(f"Decisions {status} {error}")
    if jev is not None and chars > budget:
        cut = trim_jev_state(part, budget)
        status, body, error = jev.judge(part, questions)
        if status == "OK":
            return status, body, "", "jev-trimmed", cut
        errors.append(f"Jev(줄임) {status} {error}")
    return "ERROR", None, " / ".join(errors), None, 0


def trim_jev_state(state: Dict[str, Any], budget: int = JEV_TRIM_CHARS) -> int:
    """Jev 에 보낼 state 가 예산을 넘으면 ① 첨부 내용·참조 자료 ② 판정할 턴보다 앞 턴의 답변 순으로 앞 60%·뒤 40% 만 남긴다.
    판정할 턴의 질문·답변은 줄이지 않는다. 돌려주는 값은 줄인 글자 수(0 = 그대로)."""
    if not budget or js.state_chars(state) <= budget:
        return 0
    now = max(state.get("판정할_턴") or [0])
    trimmed = 0
    for pool in ("material", "history"):
        slots = []
        for t in state.get("턴") or []:
            if pool == "material":
                slots += [(a, "내용") for a in t.get("첨부파일") or [] if isinstance(a.get("내용"), str)]
                if isinstance(t.get("참조자료"), str):
                    slots.append((t, "참조자료"))
            elif t.get("턴") != now and isinstance(t.get("답변"), str):
                slots.append((t, "답변"))
        over = js.state_chars(state) - budget
        size = sum(len(h[k]) for h, k in slots)
        if over <= 0 or not size:
            continue
        for h, k in slots:   # 길이에 비례해 줄인다 — 조각마다 최소 400자는 남긴다
            keep = max(400, len(h[k]) - int(over * len(h[k]) / size) - 80)
            trimmed += _cut(h, k, keep)
    return trimmed


def verdict_of(items: Dict[int, Dict[str, Dict[str, Any]]]) -> str:
    """의미 평가 판정 — 확인 못 한(UNVERIFIED) 항목이 하나라도 있으면 HOLD, 아니면 FAIL 이 있으면 FAIL, 그 밖 PASS (사용자 10-21: 보류 우선)."""
    decs = [v.get("decision") for part in items.values() for v in part.values()]
    return "HOLD" if "UNVERIFIED" in decs else ("FAIL" if "FAIL" in decs else "PASS")


def question_verdict(code_fails: List[str], semantic: Optional[str]) -> str:
    """질문 판정 (사용자 10-21: 보류가 하나라도 있으면 최종 판정은 보류) — 의미 평가에 보류(확인 못 함 · 판정 실행 오류)가 있으면 HOLD,
    아니면 CODE · 의미 FAIL 이 하나라도 있으면 FAIL, 그 밖 PASS. 보류를 사람이 모두 정한 뒤에 FAIL 이 남으면 FAIL."""
    if semantic == "HOLD":
        return "HOLD"
    return "FAIL" if code_fails or semantic == "FAIL" else (semantic or "PASS")


# --- 질문 하나 ---------------------------------------------------------------------------------------------------------


# --- 보고 -------------------------------------------------------------------------------------------------------------

def report_md(lines: List[Dict[str, Any]]) -> str:
    md = [f"# 평가 결과 ({datetime.now().strftime('%Y-%m-%d %H:%M')})", "",
          "심판이 받은 것: 고정 프롬프트 v4 + 질문·첨부·참조 자료·답변 JSON (판정할 항목 목록 없음).", "",
          "| 질문 | 모델 | 턴 | 최종 | Codex 결론 | CODE FAIL | 둘째 심판·Codex 같은 판정 | Codex 초 | 둘째 심판 초 | 선별(항목→Codex) |", "|---|---|---|---|---|---|---|---|---|---|"]
    for l in lines:
        c = l.get("calls") or {}
        second = c.get("jev") or c.get("decisions") or {}
        sc = l.get("screen") or {}
        md.append(f"| Q{l['question']} | {l['provider']} | {l['turns']} | {l['verdict']} | {(l.get('codex_conclusion') or {}).get('verdict', '-')} | "
                  f"{' '.join(l.get('code_fails') or []) or '-'} | {(l.get('jev_codex_agree') or [0, 0])[0]}/{(l.get('jev_codex_agree') or [0, 0])[1]} | "
                  f"{(c.get('codex') or {}).get('seconds', '-')} | {second.get('seconds', '-')} | {f'{sc.get(chr(105)+chr(116)+chr(101)+chr(109)+chr(115))}→{sc.get(chr(102)+chr(108)+chr(97)+chr(103)+chr(103)+chr(101)+chr(100))}' if sc else '-'} |")
    for l in lines:
        md += ["", f"## Q{l['question']} {l['provider']} — {l['verdict']}", f"Codex 결론 이유: {(l['codex_conclusion'] or {}).get('reason', '-')}"]
        for n, t in l["turn_results"].items():
            md.append(f"- {n}턴 심판이 찾은 상황: {' '.join(t['situations_judge'])}")
            for sid, ev in (t.get("evidence") or {}).items():
                if ev:
                    md.append(f"  - {sid} 근거: 「{ev[:120]}」")
            for p in t["problems"]:
                md.append(f"  - 출력 문제: {p}")
            for item, v in t["items"].items():
                if v["decision"] in ("FAIL", "UNVERIFIED"):
                    md.append(f"  - {KO[v['decision']]} {item} ({v['how']}) — {v.get('reason') or ''}"
                              + (f" 「{(v.get('answer_quote') or '')[:100]}」" if v.get("answer_quote") else "")
                              + (f" / Jev: {v['jev_reason'][:100]}" if v["decision"] == "UNVERIFIED" and v.get("jev_reason") else "")
                              + (f" / 문제: {'; '.join(v['problems'])[:120]}" if v.get("problems") else ""))
    return "\n".join(md) + "\n"


PROVIDERS = ("gpt", "claude", "gemini", "aiona")
LABEL = {"gpt": "GPT", "claude": "Claude", "gemini": "Gemini", "aiona": "AIONA", "aiona2": "AIONA2"}
IDS78 = ROOT / "참고자료" / "질문세트_최소78" / "ids.json"
_LOG: Dict[str, Any] = {"path": None}
_LOG_LOCK = threading.Lock()


def log_say(text: str) -> None:
    """터미널에 찍고 로그 파일에도 남긴다 — Codex 실행기(judge_run)의 진행 글도 이 함수로 모인다."""
    print(text, flush=True)
    if _LOG["path"]:
        with _LOG_LOCK, Path(_LOG["path"]).open("a", encoding="utf-8") as f:
            f.write(text + "\n")


class Board:
    """모델마다 '지금 몇 번째 질문을 평가 중인지 / 전체' — 시작·끝날 때마다 다시 찍는다 (사용자 10-20)."""

    def __init__(self, totals: Dict[str, int], done: Dict[str, int]) -> None:
        self.totals, self.done = totals, dict(done)
        self.now: Dict[str, Optional[int]] = {p: None for p in totals}
        self.lock = threading.Lock()

    def _state(self, p: str) -> str:
        total = self.totals[p]
        if self.now[p] is not None:
            return f"{min(self.done[p] + 1, total)}/{total}  (Q{self.now[p]} 평가 중)"
        return f"{self.done[p]}/{total}  ({'끝' if self.done[p] >= total else '대기'})"

    def show(self) -> None:
        lines = [f"── 평가 현황 {datetime.now():%H:%M:%S} ──"] + [f"  {LABEL.get(p, p):<7}: {self._state(p)}" for p in self.totals]
        log_say("\n".join(lines))

    def start(self, p: str, q: int) -> None:
        # 시작은 그 모델 한 줄만 — 네 모델이 같은 초에 시작하면 같은 판이 다섯 번 쌓였다 (사용자 10-20 밤). 전체 판은 질문이 끝날 때마다.
        with self.lock:
            self.now[p] = q
            log_say(f"▶ {LABEL.get(p, p):<7}: {self._state(p)}")

    def finish(self, p: str) -> None:
        with self.lock:
            self.now[p] = None
            self.done[p] += 1
            self.show()


def pre_status(turns: List[Dict[str, Any]], data: Dict[str, Any], answers: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """심판에게 보내지 않고 정하는 경우 — 판정 v3 와 같은 규칙(tools/judge_inputs.empty_output_entry, 사용자 규칙 10-20).
    빈 출력 요구: 복사할 답이 없으면 보류 · 글을 쓰면 FAIL / 복사 실패·빈 답: 보류 / 답을 못 받은 턴: FAIL."""
    from webapp.run_summary import asks_empty_output
    note = "; ".join(f"{t['case_id']}: {ji.collection_note(data.get(t['case_id']))}" for t in turns)
    if any(asks_empty_output(None, t.get("question")) for t in turns):
        return ji.empty_output_entry(turns, {t["case_id"]: data.get(t["case_id"]) for t in turns}, answers)
    empty = {t["case_id"] for t in turns if ji.empty_reply(data.get(t["case_id"]), answers.get(t["case_id"]))}   # 빈 응답 = 에러 케이스 (사용자 10-26)
    statuses = [((data.get(t["case_id"]) or {}).get("collection") or {}).get("status") for t in turns if t["case_id"] not in empty]
    if "COPY_FAILED" in statuses:
        return {"status": "held_copy_failed", "reason": "답을 복사해 오지 못함 — 보류", "collection_note": note}
    # 답 없음 · 빈 응답은 바로 FAIL 이 아니라 보류 — 결과 보고서에 수집 글 로그를 보이고 사람이 에러 케이스 · PASS · FAIL (사용자 10-26)
    if not all(ji.collected(data.get(t["case_id"])) for t in turns if t["case_id"] not in empty):
        return {"status": "held_no_answer", "reason": "답을 받지 못한 턴이 있음 — 에러 케이스 의심, 보류(수집 로그를 보고 사람이 정함)", "collection_note": note}
    if empty and len(empty) == len(turns):
        return {"status": "held_error_case", "reason": "빈 응답 — 에러 케이스 의심, 보류(수집 로그를 보고 사람이 정함)", "collection_note": note}
    return None   # 일부 턴만 빈 응답이면 심판이 나머지 턴을 보고, 빈 턴은 CODE 0-1 이 보류 칸 '에러 케이스 확인'을 붙인다


PRE_VERDICT = {"held_empty_output": "HOLD", "held_copy_failed": "HOLD", "empty_output_fail": "FAIL", "no_answer": "FAIL", "empty_output_pass": "PASS",
               "error_case": "FAIL", "held_no_answer": "HOLD", "held_error_case": "HOLD"}   # no_answer · error_case 는 옛 판정 줄 읽기용


def recode(run: Dict[str, Any], rows: List[Dict[str, Any]], results_path: Path, providers: set, questions: set) -> int:
    """--recode: 심판을 다시 부르지 않고, 실행 파일의 지금 CODE 평가와 저장된 항목 판정으로 code_fails · semantic · verdict 를 지금 규칙으로
    다시 계산해 바뀐 줄을 덧붙인다 (CODE 기준을 바꾼 뒤 — 10-21 코드블록 언어 표지, 질문 판정 규칙을 바꾼 뒤 — 10-21 보류 우선)."""
    latest: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for x in results_path.read_text(encoding="utf-8").splitlines():
        if x.strip():
            l = json.loads(x)
            if l.get("version") in (VERSION, SCREEN_VERSION):
                latest[(l["provider"], int(l["question"]))] = l
    changed = 0
    with results_path.open("a", encoding="utf-8") as f:
        for (p, q), l in sorted(latest.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            if p not in providers or q not in questions or l.get("status") != "judged":
                continue
            cases = {c.get("case_id"): c for c in ((run.get("evaluations") or {}).get(p) or {}).get("cases") or [] if isinstance(c, dict)}
            turns = [r for r in rows if ji._question(r["case_id"]) == q]
            code_fails = sorted({c for t in turns for c in question_outcome(cases.get(t["case_id"]), None).get("code_fails") or []})
            semantic = l.get("semantic") or ("FAIL" if l.get("verdict") == "FAIL" and not l.get("code_fails") else l.get("verdict"))
            tr = l.get("turn_results") or {}
            marked: List[int] = []
            unmarked: List[int] = []   # 표시를 되돌린 턴 (Codex 리뷰 10-25 밤)
            if tr and not l.get("judge_error"):   # 의미 판정도 지금 규칙으로 다시 계산 (10-21: 보류 우선)
                code_measured = {t["case_id"]: measured_families(cases.get(t["case_id"])) for t in turns}
                err_now = error_case_turns(turns, code_measured)
                for tn, t in tr.items():   # 지금 CODE 1-1 이 에러 케이스가 아니라는 턴의 옛 표시는 되돌린다 (Codex 리뷰 10-25 밤)
                    if int(tn) not in err_now and unmark_error_case_turn((t or {}).get("items") or {}):
                        unmarked.append(int(tn))
                for n in sorted(err_now):   # 지금 CODE 1-1 이 잡은 에러 케이스 턴(서비스 오류 문구 ④ 등)은 J1-1.a 만 FAIL (10-25 밤)
                    its = (tr.get(str(n)) or {}).get("items") or {}
                    before = {i: v.get("decision") for i, v in its.items() if isinstance(v, dict)}
                    mark_error_case_turn(its)
                    if any(v.get("decision") != before.get(i) for i, v in its.items() if isinstance(v, dict)):
                        marked.append(n)
                def held_now(t_res):   # 사람이 정할 보류 칸(에러 케이스 확인 · 코드 판정 확인)
                    return sorted((k, i) for k, v in t_res.items() for i, it in ((v or {}).get("items") or {}).items()
                                  if isinstance(it, dict) and it.get("kind") in (ERROR_SUSPECT_KIND, CODE_HOLD_KIND))
                had = held_now(tr)
                # 지금 CODE 가 보류한 의심 턴 · 애매한 조건 — 사람 기록이 생긴 글은 빠진다 (사용자 10-26)
                tr = apply_human_holds({**l, "turn_results": tr, "code_fails": code_fails}, human_holds(turns, cases))["turn_results"]
                suspect_moved = had != held_now(tr)
                semantic = verdict_of({int(k): (v.get("items") or {}) for k, v in tr.items()})
            else:
                suspect_moved = False
            verdict = question_verdict(code_fails, semantic)
            if not marked and not unmarked and not suspect_moved and code_fails == sorted(l.get("code_fails") or []) and verdict == l.get("verdict") and semantic == l.get("semantic"):
                continue
            new = {**l, "turn_results": tr, "code_fails": code_fails, "semantic": semantic, "verdict": verdict,
                   "recoded": {"at": datetime.now(timezone.utc).isoformat(), "code_fails_before": l.get("code_fails"), "verdict_before": l.get("verdict"),
                               "semantic_before": l.get("semantic"), "error_case_turns": marked, "error_case_unmarked": unmarked}}
            f.write(json.dumps(new, ensure_ascii=False) + "\n")
            changed += 1
            say(f"↻ Q{q} {p}: CODE FAIL {l.get('code_fails') or '-'} → {code_fails or '-'} · 판정 {l.get('verdict')} → {verdict}"
                + (f" · 에러 케이스 턴 {marked}" if marked else "") + (f" · 표시 되돌림 {unmarked}" if unmarked else ""))
    say(f"다시 계산 끝 — 바뀐 줄 {changed}개 (심판 호출 없음)")
    sp = results_path.parent / "summary.json"
    if changed and sp.exists():   # summary.json 의 모델별 숫자도 지금 판정으로
        final: Dict[Tuple[str, int], Dict[str, Any]] = {}
        for x in results_path.read_text(encoding="utf-8").splitlines():
            if x.strip():
                l = json.loads(x)
                if l.get("version") in (VERSION, SCREEN_VERSION):
                    final[(l["provider"], int(l["question"]))] = l
        sm = json.loads(sp.read_text(encoding="utf-8"))
        sm["models"] = {p: {**{v: sum(1 for (pp, _q), l in final.items() if pp == p and l.get("verdict") == v) for v in ("PASS", "FAIL", "HOLD")},
                            "n": sum(1 for (pp, _q) in final if pp == p)} for p in sorted({k[0] for k in final})}
        sp.write_text(json.dumps(sm, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0



def already_done(line: Dict[str, Any], repair: bool = False, redo: bool = False) -> bool:
    """results.jsonl 의 한 줄을 '이미 한 질문'으로 셀지. 선별(v5) 줄도 이미 한 건 (Codex 지적 10-20) · 판정 실행 오류는 다시.
    --repair: 판정한 줄만 저장된 심판 출력으로 다시 합친다 · --redo: 판정 전 줄(빈 답 · 답 없음 보류 등)까지 처음부터 다시 (10-26)."""
    if line.get("version") not in (VERSION, SCREEN_VERSION) or line.get("judge_error"):
        return False
    if redo:
        return False
    return not (repair and line.get("status") == "judged")

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--votes", type=int, help="1차 심판 투표 수 — 같은 턴을 N 번 판정해 다수결 (기본 .env FIRST_JUDGE_VOTES 또는 3, 1 이면 한 번)")
    ap.add_argument("--codex-batch", type=int, help="첫 Codex 호출 묶음 크기 — 첨부 없는 단일 질문 N개를 한 번에 (기본 .env CODEX_BATCH 또는 10, 1 = 질문마다)")
    ap.add_argument("--run", required=True)
    ap.add_argument("--pick", help="질문:모델 쉼표 목록 — 예: 143:claude,115:aiona (없으면 --questions)")
    ap.add_argument("--set", default=str(SET_DIR), help="질문 세트 폴더 (questions.json) — 기본은 326 세트")
    ap.add_argument("--questions", default="all", help="'all' = 세트의 모든 질문, '78' = 질문세트_최소78/ids.json, 또는 질문 번호 쉼표 목록")
    ap.add_argument("--providers", default=None, help="판정할 모델 자리(쉼표) — 비우면 실행에 있는 자리 모두(AIONA 두 번째 자리 aiona2 포함, 10-26)")
    ap.add_argument("--out", default="v4-78", help="결과 폴더 이름 (data/judge_runs/<실행>/<이름>)")
    ap.add_argument("--reuse-codex", action="store_true", help="저장된 Codex 출력이 있으면 다시 부르지 않고 쓴다 (Jev 만 다시)")
    ap.add_argument("--show-thinking", action="store_true", help="Codex 생각 요약 줄도 찍기 (네 모델 동시면 많음)")
    ap.add_argument("--dry", action="store_true", help="심판을 부르지 않고 질문마다 어떻게 처리할지만 보기")
    ap.add_argument("--codex-all", action="store_true", help="교정용 — 걸린 항목만이 아니라 모든 항목을 Codex 에도 보내 일치율을 잰다")
    ap.add_argument("--recode", action="store_true", help="심판을 부르지 않고, 지금 실행 파일의 CODE 평가 결과로 판정 줄의 CODE FAIL·질문 판정만 다시 계산해 덧붙인다(CODE 기준을 바꾼 뒤)")
    ap.add_argument("--redo", action="store_true", help="--questions 로 고른 질문은 이미 판정했어도 처음부터 다시 판정한다(새 줄을 덧붙임, 보고서는 마지막 줄을 씀)")
    ap.add_argument("--reask-fail", default="", help="쉼표로 나눈 항목 — Codex 가 FAIL 로 본 그 항목을 정의의 예외를 짚어 한 번 다시 묻는다(J1-4.a · J1-4.b 는 기본, 여기에 더함)")
    ap.add_argument("--repair", action="store_true", help="이미 판정한 줄을 저장된 Decisions·Codex 출력으로 다시 합치고, Codex 가 답하지 않았거나 인용이 틀린 항목만 다시 묻는다")
    # 기준 다듬기 루프(tools/refine_loop.py)의 시험용 — 고칠 안을 적용한 임시 파일로 같은 칸을 다시 판정한다 (10-25 밤, harness2 의 제안 → 시험 흐름)
    ap.add_argument("--prompt", help="판정 프롬프트 파일 — 기본 참고자료/프롬프트/judge_prompt_v4.txt")
    ap.add_argument("--jev-questions", help="1차 심판 항목 정의(J1~J4) JSON — 기본 참고자료/프롬프트/jev_questions_v4.json")
    ap.add_argument("--turn-checklists", help="멀티턴 턴별 확인 목록 JSON — 기본 <세트>/multiturn_v3/turn_checklists.json")
    # 심판 비교 · 호출 방식 시험 (사용자 10-26)
    ap.add_argument("--first-judge", choices=("stances", "situational", "auto", "jev", "decisions"),
                    help="1차 심판 흐름 — situational(기본, 회의 흐름: 단일 질문 Jev+Decisions 교차 · 그 밖 Decisions 1번) · auto(옛 크기 나눔 + --votes 표) · jev · decisions (.env FIRST_JUDGE_MODE)")
    ap.add_argument("--first-only", action="store_true", help="1차 심판만 — Codex 를 부르지 않고 decisions/<질문>.json 만 남긴다 (심판 비교)")
    ap.add_argument("--reuse-first", help="쉼표로 나눈 판정 폴더 이름 — 그 폴더들의 1차 판정(decisions/)을 그대로 쓰고 1차 심판을 부르지 않는다 (2차 호출 방식 비교)")
    ap.add_argument("--codex-one-call", choices=("file", "stdin"), help="첫 Codex 호출을 한 번으로 — 첨부 없는 단일 질문 전부(모델이 달라도)를 파일 하나(codex_io/한번에/판정_입력.json)로 · stdin 은 표준 입력으로")
    args = ap.parse_args(argv)
    global FIRST_JUDGE_MODE, FIRST_ONLY
    if args.first_judge:
        FIRST_JUDGE_MODE = args.first_judge
    FIRST_ONLY = bool(args.first_only)
    if args.votes:
        global FIRST_JUDGE_VOTES
        FIRST_JUDGE_VOTES = max(1, int(args.votes))
    if args.codex_batch:
        global CODEX_BATCH
        CODEX_BATCH = max(1, int(args.codex_batch))
    REASK_FAIL_ITEMS.update(x.strip() for x in (args.reask_fail or "").split(",") if x.strip())

    import tools.judge_run as jr
    jr.say = log_say   # Codex 실행기의 진행 글도 같은 곳으로
    jr.SHOW_THINKING = args.show_thinking
    global say
    say = log_say

    run = json.loads((ROOT / "data" / "comparison_runs" / f"{args.run}.json").read_text(encoding="utf-8"))
    if not args.providers:   # 실행에 있는 자리 모두 — 기본 넷 + AIONA 두 번째 자리 (10-26)
        from tools.providers import ordered
        args.providers = ",".join(ordered(p for p in (run.get("providers") or PROVIDERS) if (run.get("datasets") or {}).get(p))) or ",".join(PROVIDERS)
    set_dir = Path(args.set)
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    inputs = judge_input_files(args.prompt, args.jev_questions, args.turn_checklists, set_dir)
    plan, stale = init_question_set(set_dir, groups, inputs["turn_checklists"])   # 질문별 목표 · 턴별 확인 목록 · 첨부 원본 폴더 (10-22)
    if plan or stale:   # 모두 낡아도 알린다 (Codex 검토 10-22)
        say(f"📋 턴별 확인 목록 {len(plan)}턴" + (f" — 질문과 맞지 않아 안 쓴 턴 {len(stale)}개: {', '.join(stale[:10])}" if stale else ""))
    if any((args.prompt, args.jev_questions, args.turn_checklists)):
        say("🧪 시험용 입력 파일 — " + " · ".join(f"{k} {v.name}" for k, v in inputs.items()))
    rows = flatten(groups)
    prompt = inputs["prompt"].read_text(encoding="utf-8")
    fixed = json.loads(inputs["jev_questions"].read_text(encoding="utf-8"))
    out = ROOT / "data" / "judge_runs" / args.run / args.out
    # 판정 방식은 하나(선별: Decisions 가 항목 전부 판정 → 걸린 항목만 Codex 확인) — 모드 선택 없음 (사용자 결정 2026-10-20 밤)
    for sub in ("inputs", "codex", "jev", "decisions"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    _LOG["path"] = str(out / "run.log")
    if args.reuse_first:   # 저장된 1차 판정을 그대로 (2차 호출 방식만 바꿔 비교) — 이미 있는 파일은 두고 없는 것만 복사
        import shutil
        for src in [x.strip() for x in args.reuse_first.split(",") if x.strip()]:
            for f in (ROOT / "data" / "judge_runs" / args.run / src / "decisions").glob("*.json"):
                if not (out / "decisions" / f.name).exists():
                    shutil.copy2(f, out / "decisions" / f.name)
    reuse_dec, keep_first = bool(args.repair or args.reuse_first), bool(args.reuse_first)
    say(f"⚖ 1차 심판 {FIRST_JUDGE_MODE}"
        + (f" · 판정 프롬프트 {' · '.join(st.get('name') or st['id'] for st in first_stances()['stances'])} 가중 다수결({first_stances().get('version')})"
           if FIRST_JUDGE_MODE == "stances" else "") + (f" · 교차 확실 {CROSS_SURE} · Jev {'1' if JEV_VOTES == 1 else '2(+1)'} · Decisions 1" if FIRST_JUDGE_MODE == "situational" else f" · 투표 {FIRST_JUDGE_VOTES}")
        + (" · 1차만(Codex 안 부름)" if FIRST_ONLY else ""))

    if args.pick:
        plan = [(int(x.split(":")[0]), x.split(":")[1]) for x in args.pick.split(",") if x.strip()]
    else:
        qs = ([int(g["id"]) for g in groups] if args.questions == "all" else
              json.loads(IDS78.read_text(encoding="utf-8"))["ids"] if args.questions == "78" else [int(x) for x in args.questions.split(",") if x.strip()])
        plan = [(q, p) for p in args.providers.split(",") for q in qs]
    results_path = out / "results.jsonl"
    if args.recode:
        return recode(run, rows, results_path, {p for _, p in plan}, {q for q, _ in plan})
    done = set()
    if results_path.exists():
        for x in results_path.read_text(encoding="utf-8").splitlines():
            if x.strip():
                l = json.loads(x)
                if already_done(l, args.repair, args.redo):
                    done.add((l["provider"], int(l["question"])))
    queues: Dict[str, List[int]] = {}
    for q, p in plan:
        queues.setdefault(p, []).append(q)
    totals = {p: len(v) for p, v in queues.items()}
    board = Board(totals, {p: sum(1 for q in v if (p, q) in done) for p, v in queues.items()})
    gpt = jev = decisions = None
    if not args.dry:
        gpt, jev, notes = judges_from_env()
        if FIRST_ONLY:
            gpt = None   # 1차만 — Codex 를 부르지 않는다
        if gpt is not None:
            gpt.files_dir = set_dir / "files"   # 첨부 원본(이미지·pdf·docx 쪽 이미지)을 -i 로
            gpt.io_dir = out / "codex_io"   # Codex 입력 · 결과 파일 (10-21 저녁: 파일로 읽고 파일로 결과)
        from code_eval import attachment_pages as ap
        if gpt is not None or decisions is not None:
            for name, note in ap.prerender(set_dir / "files").items():   # 스레드 전에 한 번 그려 둔다 (Word 는 한 번에 하나)
                say(f"   📎 첨부 쪽 이미지 — {note}")
        for n in notes:
            say(f"⚠ {n}")
        from tools.judge_run import decisions_from_env
        decisions, d_reason = decisions_from_env()
        if decisions is not None:
            decisions.client.files_dir = set_dir / "files"   # 이미지 첨부 원본을 input_image 로
        if decisions is None:
            say(f"⚠ Decisions 심판 없음 — {d_reason}")
    write_lock = threading.Lock()

    saved_keys: set = set()   # 이번 실행에서 줄을 남긴 (모델, 질문) — 빠진 질문이 있으면 종료 코드 1 (Codex 검토 10-25)

    def save(line: Dict[str, Any]) -> None:
        with write_lock, results_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({**line, "at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")
            saved_keys.add((line.get("provider"), line.get("question")))

    def provider_data(provider: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        data = {r.get("case_id"): r for r in (run.get("datasets") or {}).get(provider) or [] if isinstance(r, dict)}
        cases = {c.get("case_id"): c for c in ((run.get("evaluations") or {}).get(provider) or {}).get("cases") or [] if isinstance(c, dict)}
        return data, cases

    def work(provider: str) -> None:
        data, cases = provider_data(provider)
        if CODEX_BATCH > 1 and gpt is not None and not args.dry:
            work_batched(provider, data, cases)
            return
        for q in queues[provider]:
            if (provider, q) in done:
                continue
            turns = sorted([r for r in rows if ji._question(r["case_id"]) == q], key=ji._turn)
            answers = {t["case_id"]: (data.get(t["case_id"]) or {}).get("answer") or "" for t in turns}
            pre = pre_status(turns, data, answers)
            board.start(provider, q)
            if pre is not None:
                line = {"version": VERSION, "provider": provider, "question": q, "turns": len(turns), "status": pre["status"],
                        "verdict": PRE_VERDICT.get(pre["status"], "HOLD"), "reason": pre.get("reason"), "collection_note": pre.get("collection_note")}
                say(f"■ Q{q} {provider}: {KO.get('UNVERIFIED') if line['verdict'] == 'HOLD' else line['verdict']} (심판 없이) — {pre.get('reason')}")
            elif args.dry:
                line = None
                say(f"· Q{q} {provider}: 심판에게 보냄 ({len(turns)}턴)")
            else:
                deliveries = {t["case_id"]: ji.delivery_note(data.get(t["case_id"])) for t in turns}
                code_fails = sorted({c for t in turns for c in question_outcome(cases.get(t["case_id"]), None).get("code_fails") or []})
                code_measured = {t["case_id"]: measured_families(cases.get(t["case_id"])) for t in turns}   # CODE 가 실제로 잰 형식 조건 (10-22 밤)
                try:
                    line = screen_one(q, provider, turns, answers, deliveries, code_fails, prompt, fixed, gpt, decisions, out, args.codex_all,
                                      args.reuse_codex or args.repair, reuse_decisions=reuse_dec, jev=jev, keep_first=keep_first, code_measured=code_measured)
                    line = line if line.get("status") == "first_only" else apply_human_holds({**line, "status": "judged"}, human_holds(turns, cases))
                except Exception as exc:   # 한 질문의 오류로 그 모델 전체가 멈추지 않게 — 기록하고 다음 질문으로
                    line = {"version": VERSION, "provider": provider, "question": q, "turns": len(turns), "status": "error", "verdict": "HOLD",
                            "reason": f"판정 실행 오류: {type(exc).__name__}: {str(exc)[:200]}"}
                    say(f"⚠ Q{q} {provider}: {line['reason']}")
            if line is not None and not args.dry:
                save(line)
            board.finish(provider)

    def work_batched(provider: str, data: Dict[str, Any], cases: Dict[str, Any]) -> None:
        """묶음 흐름(--codex-batch N): 질문 N개씩 stage A → 첫 Codex 호출 묶음(codex_batch_round) → stage B. 심판 없이 끝나는 질문(pre_status)은 그대로."""
        pending = [q for q in queues[provider] if (provider, q) not in done]
        n_turns = {q: sum(1 for r in rows if ji._question(r["case_id"]) == q) for q in pending}
        for chunk in batch_chunks(pending, lambda q: n_turns.get(q, 1) > 1, CODEX_BATCH):   # 멀티턴은 혼자(묶지 않으니 바로 저장되게), 단일은 N개씩
            states = [st for st in (stage_a_for(provider, q, data, cases) for q in chunk) if st is not None]
            try:
                codex_batch_round(states, gpt, prompt, CODEX_BATCH)
            except Exception as exc:   # 묶음 단계 자체가 죽으면 아직 첫 호출을 못 받은 질문만 따로 (first_call_alone 이 질문마다 예외를 삼킨다)
                say(f"⚠ {provider} 묶음 단계 오류: {type(exc).__name__}: {str(exc)[:160]} — 남은 질문은 따로 봅니다")
                for st in states:
                    if st["codex_plan"] is not None and st["status"] is None:
                        first_call_alone(st)
            for st in states:
                stage_b_save(st)

    def stage_a_for(provider: str, q: int, data: Dict[str, Any], cases: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """질문 하나의 stage A — 심판 없이 끝나는 질문(pre_status) · 오류는 줄을 저장하고 None, 아니면 Codex 앞까지 간 상태."""
        turns = sorted([r for r in rows if ji._question(r["case_id"]) == q], key=ji._turn)
        answers = {t["case_id"]: (data.get(t["case_id"]) or {}).get("answer") or "" for t in turns}
        pre = pre_status(turns, data, answers)
        board.start(provider, q)
        if pre is not None:
            line = {"version": VERSION, "provider": provider, "question": q, "turns": len(turns), "status": pre["status"],
                    "verdict": PRE_VERDICT.get(pre["status"], "HOLD"), "reason": pre.get("reason"), "collection_note": pre.get("collection_note")}
            say(f"■ Q{q} {provider}: {KO.get('UNVERIFIED') if line['verdict'] == 'HOLD' else line['verdict']} (심판 없이) — {pre.get('reason')}")
            save(line)
            board.finish(provider)
            return None
        deliveries = {t["case_id"]: ji.delivery_note(data.get(t["case_id"])) for t in turns}
        code_fails = sorted({c for t in turns for c in question_outcome(cases.get(t["case_id"]), None).get("code_fails") or []})
        code_measured = {t["case_id"]: measured_families(cases.get(t["case_id"])) for t in turns}
        try:
            st = screen_stage_a(q, provider, turns, answers, deliveries, code_fails, prompt, fixed, gpt, decisions, out, args.codex_all,
                                args.reuse_codex or args.repair, reuse_dec, jev, keep_first, code_measured)
            if isinstance(st, dict):
                st["human_holds"] = human_holds(turns, cases)
            return st
        except Exception as exc:
            save({"version": VERSION, "provider": provider, "question": q, "turns": len(turns), "status": "error", "verdict": "HOLD",
                  "reason": f"판정 실행 오류: {type(exc).__name__}: {str(exc)[:200]}"})
            say(f"⚠ Q{q} {provider}: 판정 실행 오류: {type(exc).__name__}: {str(exc)[:200]}")
            board.finish(provider)
            return None

    def stage_b_save(st: Dict[str, Any]) -> None:
        try:
            line = apply_human_holds({**screen_stage_b(st, st["status"], st["output"], st["error"], st["rounds"], st["reused"]), "status": "judged"},
                                     st.get("human_holds") or {})
        except Exception as exc:
            line = {"version": VERSION, "provider": st["provider"], "question": st["q"], "turns": len(st["turns"]), "status": "error", "verdict": "HOLD",
                    "reason": f"판정 실행 오류: {type(exc).__name__}: {str(exc)[:200]}"}
            say(f"⚠ Q{st['q']} {st['provider']}: {line['reason']}")
        save(line)
        board.finish(st["provider"])

    def run_one_call() -> None:
        """--codex-one-call: 모든 (모델, 질문)의 stage A(모델마다 동시) → 첫 Codex 호출 한 번(codex_one_call) → stage B(모델마다 동시)."""
        states: List[Dict[str, Any]] = []
        lock = threading.Lock()

        def gather(provider: str) -> None:
            data, cases = provider_data(provider)
            for q in queues[provider]:
                if (provider, q) in done:
                    continue
                st = stage_a_for(provider, q, data, cases)
                if st is not None:
                    with lock:
                        states.append(st)
        ts = [threading.Thread(target=gather, args=(p,), name=f"stageA-{p}") for p in queues]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        order = list(queues)
        states.sort(key=lambda st: (order.index(st["provider"]), st["q"]))
        try:
            codex_one_call(states, gpt, prompt, out, args.codex_one_call)
        except Exception as exc:   # 한 번 호출 단계 자체가 죽으면 아직 첫 호출을 못 받은 질문만 따로
            say(f"⚠ 한 번 호출 단계 오류: {type(exc).__name__}: {str(exc)[:160]} — 남은 질문은 따로 봅니다")
            for st in states:
                if st["codex_plan"] is not None and st["status"] is None:
                    first_call_alone(st)

        def finish(provider: str) -> None:
            for st in states:
                if st["provider"] == provider:
                    stage_b_save(st)
        ts = [threading.Thread(target=finish, args=(p,), name=f"stageB-{p}") for p in queues]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

    say(f"평가 진행합니다 — {sum(totals.values())}건(질문 × 모델, 이미 한 {sum(board.done.values())}건 건너뜀) · 모델마다 한 줄로 동시에"
        + (" · 점검만(심판 안 부름)" if args.dry else "") + f" · 로그 {out / 'run.log'}")
    board.show()
    wall0 = time.monotonic()
    if args.codex_one_call and gpt is not None and not args.dry:
        run_one_call()
    else:
        threads = [threading.Thread(target=work, args=(p,), name=f"judge-{p}") for p in queues]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    wall = round(time.monotonic() - wall0, 1)
    if args.dry:
        return 0
    latest: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for x in results_path.read_text(encoding="utf-8").splitlines():
        if x.strip():
            l = json.loads(x)
            if l.get("version") in (VERSION, SCREEN_VERSION):
                latest[(l["provider"], int(l["question"]))] = l   # 같은 (모델, 질문) 은 마지막 줄 (다시 판정한 것)
    lines = list(latest.values())
    with (out / "timing.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "judge-v4", "at": datetime.now(timezone.utc).isoformat(), "seconds": wall,
                            "judgments": sum(totals.values()) - sum(1 for k in done if k[1] in set(sum(queues.values(), []))),
                            **({"redo": True} if args.redo else {})},   # 다시 판정 — 보고서 판정 시간·실행 기록에서 뺀다 (사용자 10-21)
                           ensure_ascii=False) + "\n")
    judge_s = sum(r.get("seconds") or 0 for r in (json.loads(x) for x in (out / "timing.jsonl").read_text(encoding="utf-8").splitlines() if x.strip())
                  if str(r.get("kind") or "").startswith("judge") and not r.get("redo"))
    summary = {"version": VERSION, "run_id": args.run, "prompt": PROMPT_V4.name, "jev_questions": fixed.get("version"),
               "codex": {"model": getattr(gpt, "model", None), "reasoning": getattr(gpt, "reasoning", None)} if gpt is not None else None,
               "finished_at": datetime.now(timezone.utc).isoformat(), "wall_seconds": round(judge_s, 1), "inputs": input_fingerprints(inputs), "models": {}}   # 폴더 전체 판정 시간(다시 판정 뺌)
    say("\n결과 — 질문 단위 (보류는 사람이 정함)")
    for p in queues:
        mine = [l for l in lines if l["provider"] == p and int(l["question"]) in set(queues[p])]
        c = {v: sum(1 for l in mine if l.get("verdict") == v) for v in ("PASS", "FAIL", "HOLD")}
        say(f"  {LABEL.get(p, p):<7}: PASS {c['PASS']} · FAIL {c['FAIL']} · 보류 {c['HOLD']}  / {len(queues[p])}")
    for p in sorted({l["provider"] for l in lines}):   # summary 는 이 폴더 전체 — 몇 질문만 다시 판정해도 전체 숫자가 남는다 (10-21)
        allp = [l for l in lines if l["provider"] == p]
        summary["models"][p] = {**{v: sum(1 for l in allp if l.get("verdict") == v) for v in ("PASS", "FAIL", "HOLD")}, "n": len(allp)}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "report.md").write_text(report_md([l for l in lines if l.get("status") == "judged"]), encoding="utf-8")
    say(f"\n평가 끝 — 벽시계 {wall / 60:.0f}분 · 결과 {results_path}")
    expected = {(p, q) for p, qs_ in queues.items() for q in qs_ if (p, q) not in done}
    left = sorted(expected - saved_keys)   # 스레드가 중간에 죽어 줄을 못 남긴 질문 — 0 으로 끝내지 않는다 (Codex 검토 10-25)
    if left:
        say(f"⚠ 줄을 남기지 못한 (모델, 질문) {len(left)}건: {left[:8]}{' …' if len(left) > 8 else ''} — 같은 명령으로 이어 돌리세요")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
