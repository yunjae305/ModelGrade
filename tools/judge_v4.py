"""판정 v4 시험 — 고정 프롬프트 v4(상황 규칙 전부) + 수집 JSON(질문·첨부·참조 자료·답변)만 Codex CLI 와 Jev 에 던진다.
심판이 턴마다 상황을 직접 찾고(상황표 1), 항목을 판정하고, 질문 하나의 결론까지 낸다 (사용자 2026-10-20).
코드가 미리 정한 '판정할 항목 목록'(판정상황표)은 심판에게 주지 않는다 — 결과를 비교하는 데에만 쓴다.

    python -X utf8 tools/judge_v4.py --run ac6507c00276 --pick 143:claude,115:aiona,17:gemini

→ data/judge_runs/<실행>/v4/ — inputs/Q143_claude.json(심판이 받은 JSON) · codex/·jev/(심판 출력 원문) · results.jsonl · report.md
합치기(J1~J4, 두 심판이 모두 봄): Codex FAIL → FAIL · Jev FAIL 인데 Codex 는 FAIL 아님 → 보류(심판 갈림) · 그 밖은 Codex.
J5~J8 은 Codex 만. 질문 결론: FAIL 항목 하나라도 → FAIL, 보류 하나라도 → HOLD, 그 밖 PASS. CODE FAIL 이 있으면 최종 FAIL.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval import long_context  # noqa: E402
from code_eval import semantic_items as si  # noqa: E402
from tools import judge_inputs as ji  # noqa: E402
from tools import judge_split as js  # noqa: E402
from tools.build_judge_prompt_v4 import ALWAYS_SUBS, JEV_V4, V4 as PROMPT_V4  # noqa: E402
from tools.judge_run import judges_from_env, say  # noqa: E402
from webapp.question_generation import flatten  # noqa: E402
from webapp.run_summary import question_outcome  # noqa: E402

VERSION = "judge-v4-trial-1"
SET_DIR = ROOT / "참고자료" / "질문세트_326_생성프롬프트v2"
SUB_ITEMS: Dict[str, List[str]] = {sub.id: [it.id for it in si.ITEMS if it.sub == sub.id] for sub in si.SUBS}
SUB_ORDER = list(SUB_ITEMS)
#: 상황표 1 에서 괄호로 조건이 따로 붙은 항목 — 상황을 적었어도 그 조건이 없으면 답하지 않는다(빠져도 보류가 아님)
CONDITIONAL_ITEMS = {"J2-5.a", "J2-5.b", "J3-3.a", "J3-5.a", "J3-5.b", "J4-1.b", "J5-2.a", "J5-2.b",
                     "J6-2.a", "J6-2.b", "J6-2.c", "J6-3.a", "J6-3.b"}
KO = {"PASS": "PASS", "FAIL": "FAIL", "NOT_APPLICABLE": "해당 없음", "UNVERIFIED": "보류", None: "-"}


# --- 입력 JSON --------------------------------------------------------------------------------------------------------

def input_tokens(row: Dict[str, Any]) -> Optional[int]:
    """그 턴의 질문 + 첨부 내용 + 참조 자료 토큰 수(J7 판단용) — 토크나이저가 없으면 None."""
    text = (row.get("question") or "") + "".join("\n" + (a.get("content") or "") for a in row.get("attachments") or [] if isinstance(a, dict))
    try:
        return long_context.measure(text, row.get("reference"))["tokens"]
    except long_context.TokenizerUnavailable:
        return None


def question_json(q: int, turns: List[Dict[str, Any]], answers: Dict[str, str], deliveries: Dict[str, str],
                  judge_turns: Optional[List[int]] = None) -> Dict[str, Any]:
    """심판에게 주는 질문 하나 — 질문·첨부파일·참조 자료·답변만(모델 이름·판정할 항목 없음). judge_turns: 이번에 판정할 턴(기본 모든 턴)."""
    out = []
    for row in turns:
        t: Dict[str, Any] = {"턴": ji._turn(row), "질문": row.get("question") or ""}
        files = [a for a in row.get("attachments") or [] if isinstance(a, dict)]
        if files:
            t["첨부파일"] = [{"이름": a.get("name"), "종류": a.get("type"), "내용": a.get("content") or "(내용 없음)"} for a in files]
        if isinstance(row.get("reference"), str) and row["reference"].strip():
            t["참조자료"] = row["reference"]
        if deliveries.get(row["case_id"]):
            t["전달방식"] = deliveries[row["case_id"]]
        t["입력_추정토큰"] = input_tokens(row)
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

def check_codex(output: Any, turns: List[Dict[str, Any]], answers: Dict[str, str]) -> Dict[int, Dict[str, Any]]:
    """Codex 출력 → 턴마다 {situations, problems, items}. 심판이 찾은 상황의 항목이 판정할 항목이 된다.
    언제나 적는 상황이 빠졌으면 더해 '판정 없음'(보류)로, 상황표에 없는 id·원문에 없는 근거 인용은 problems 에 남긴다."""
    by_turn = {t["turn"]: t for t in (output or {}).get("turns") or [] if isinstance(t, dict) and isinstance(t.get("turn"), int)} \
        if isinstance(output, dict) else {}
    rows = {ji._turn(r): r for r in turns}
    expected: Dict[int, List[str]] = {}
    info: Dict[int, Dict[str, Any]] = {}
    for n, row in rows.items():
        sits = [s for s in (by_turn.get(n) or {}).get("situations") or [] if isinstance(s, dict)]
        subs, problems = [], []
        sources = [row.get("question") or "", row.get("reference") or "", ji.attachments_block(row)]
        sources += [p.get("question") or "" for m, p in rows.items() if m < n]
        long_input = (input_tokens(row) or 0) >= long_context.MIN_TOKENS
        answered_ids = [js._base(e.get("id")) for e in (by_turn.get(n) or {}).get("items") or [] if isinstance(e, dict)]
        cands = []
        for s in sits:
            sid = str(s.get("id") or "").split(".")[0]
            if sid in SUB_ITEMS:
                cands.append({**s, "id": sid})
                continue
            # 대분류만 적은 경우('J6', 2026-10-20 시험) — 그 대분류에서 실제로 답한 항목의 세부 기준으로 푼다
            subs_of = list(dict.fromkeys(i.rsplit(".", 1)[0] for i in answered_ids if i.startswith(sid + "-") and i.rsplit(".", 1)[0] in SUB_ITEMS))
            if subs_of:
                problems.append(f"상황 id {sid} 를 답한 항목으로 풀어 씀: {' '.join(subs_of)}")
                cands += [{**s, "id": x} for x in subs_of]
            else:
                problems.append(f"상황표에 없는 상황 {s.get('id')!r}")
        for s in cands:
            sid = s["id"]
            # 입력으로 정해지는 조건(프롬프트 상황표 1) — 어긋나면 그 상황은 버리고 기록한다
            if sid.startswith("J8") and not row.get("attachments"):
                problems.append(f"{sid}: 첨부파일이 없는데 적음(버림)")
                continue
            if sid.startswith("J7") and not long_input:
                problems.append(f"{sid}: 입력이 272K 토큰 미만인데 적음(버림)")
                continue
            if sid.startswith("J6") and n < 2:
                problems.append(f"{sid}: 1턴에 적음(버림)")
                continue
            if sid not in subs:
                subs.append(sid)
            if (s.get("evidence") or "").strip() and not js.quoted_in(s["evidence"], sources):
                problems.append(f"{sid}: 근거 인용이 질문 원문에 없음")
        for sid in ALWAYS_SUBS:
            if sid not in subs:
                problems.append(f"언제나 적는 상황 {sid} 가 빠짐")
                subs.append(sid)
        subs.sort(key=SUB_ORDER.index)
        answered = {js._base(e.get("id")) for e in (by_turn.get(n) or {}).get("items") or [] if isinstance(e, dict)}
        expected[n] = [i for sid in subs for i in SUB_ITEMS[sid] if i not in CONDITIONAL_ITEMS or i in answered]
        for sid in subs:
            if not any(i in answered for i in SUB_ITEMS[sid]):
                problems.append(f"{sid}: 상황만 적고 항목 판정 없음")
        extra = sorted(answered - set(expected[n]))
        if extra:
            problems.append("찾은 상황 밖의 항목(무시): " + " ".join(extra))
        info[n] = {"situations": subs, "evidence": {s["id"]: s.get("evidence") for s in cands}, "problems": problems}
    checked = js.check_gpt(output if isinstance(output, dict) else None, turns, answers, expected)
    for n in info:
        info[n]["items"] = checked.get(n, {})
    return info


def combine(codex_v: Optional[Dict[str, Any]], jev_v: Optional[Dict[str, Any]]) -> Tuple[Optional[str], str]:
    """J1~J4 한 항목 — (최종 판정, 어떻게 정했는지). None = 두 심판 모두 상황 없음으로 봄."""
    c = (codex_v or {}).get("decision")
    j = (jev_v or {}).get("decision")
    if c == "FAIL":
        return "FAIL", "Codex FAIL" + (" · Jev 도 FAIL" if j == "FAIL" else f" · Jev {KO.get(j, j)}")
    if j == "FAIL":
        return "UNVERIFIED", f"심판 갈림 — Jev FAIL · Codex {KO.get(c, c) if c else '상황 없음'}"
    if c is None:
        return None, f"Codex 상황 없음 · Jev {KO.get(j, j)}"
    return c, f"Codex {KO.get(c, c)} · Jev {KO.get(j, j)}"


def verdict_of(items: Dict[int, Dict[str, Dict[str, Any]]]) -> str:
    decs = [v.get("decision") for part in items.values() for v in part.values()]
    return "FAIL" if "FAIL" in decs else ("HOLD" if "UNVERIFIED" in decs else "PASS")


# --- 질문 하나 ---------------------------------------------------------------------------------------------------------

def judge_one(q: int, provider: str, turns: List[Dict[str, Any]], answers: Dict[str, str], deliveries: Dict[str, str],
              table: Dict[str, Dict[str, Any]], code_fails: List[str], prompt: str, fixed: Dict[str, Any], gpt: Any, jev: Any,
              out: Path, reuse_codex: bool = False) -> Dict[str, Any]:
    tag = f"Q{q} {provider}"
    doc = question_json(q, turns, answers, deliveries)
    text = json.dumps(doc, ensure_ascii=False, indent=1)
    name = f"Q{q}_{provider}.json"
    (out / "inputs" / name).write_text(text, encoding="utf-8")
    jev_items = list(fixed["items"])
    state = {"guide": fixed["guide"], **doc}
    questions = {f"T{ji._turn(r)}:{i}": fixed["items"][i] for r in turns for i in jev_items}
    calls: Dict[str, Any] = {}

    def run_codex() -> None:
        search = not any((t.get("reference") or "").strip() for t in turns)
        saved = out / "codex" / name
        if reuse_codex and saved.exists() and json.loads(saved.read_text(encoding="utf-8") or "null"):
            calls["codex"] = {"status": "OK", "error": "", "seconds": None, "search": search, "reused": True,
                              "output": json.loads(saved.read_text(encoding="utf-8"))}
            say(f"   🖥 {tag} Codex — 앞 시험의 출력 다시 씀 ({saved.name})")
            return
        t0 = time.monotonic()
        all_turns = [ji._turn(r) for r in turns]
        got: Dict[int, Any] = {}
        conclusions: List[Any] = []
        status, error, rounds = "ERROR", "Codex 없음", 0
        while gpt and rounds < 3:   # 출력이 잘려 턴이 빠지면(Q17 10턴 중 2턴만, 2026-10-20) 빠진 턴만 '판정할_턴' 으로 다시 묻는다
            pending = [n for n in all_turns if n not in got]
            if not pending:
                break
            rounds += 1
            body = text if rounds == 1 else json.dumps(question_json(q, turns, answers, deliveries, pending), ensure_ascii=False, indent=1)
            say(f"   🖥 {tag} Codex CLI{'' if rounds == 1 else f' 다시({rounds}번째) — 빠진 턴 {pending}'} — 고정 프롬프트 v4 {len(prompt):,}자 + 입력 JSON {len(body):,}자"
                f" · 웹 검색 {'켬' if search else '끔'} …")
            status, output, error = gpt.judge(prompt, body, search, tag, output_schema=output_schema())
            if status != "OK":
                break
            for t in (output or {}).get("turns") or []:
                if isinstance(t, dict) and t.get("turn") in pending and t["turn"] not in got:
                    got[t["turn"]] = t
            conclusions.append((output or {}).get("conclusion"))
        missing = [n for n in all_turns if n not in got]
        output = {"turns": [got[n] for n in all_turns if n in got], "conclusion": conclusions[0] if conclusions else None,
                  "conclusions": conclusions} if got else None
        calls["codex"] = {"status": "OK" if got else status, "error": error if not got else (f"끝내 답하지 않은 턴 {missing}" if missing else ""),
                          "seconds": round(time.monotonic() - t0, 1), "search": search, "calls": rounds, "missing_turns": missing, "output": output}
        say(f"   🖥 {tag} Codex {calls['codex']['status']} {calls['codex']['seconds']}초 · 호출 {rounds}번"
            + (f" — {str(calls['codex']['error'])[:100]}" if calls['codex']['error'] else ""))

    def run_jev() -> None:
        # 턴마다 한 번씩 — 270문항(10턴 × 27)을 한 요청에 넣으면 Jev 가 HTTP 400 (Q17, 2026-10-20). 그 턴까지의 대화만 state 로 (뒤 턴은 판정에 안 씀)
        say(f"   🔎 {tag} Jev — J1~J4 {len(questions)}문항(턴 {len(turns)} × {len(jev_items)}, 턴마다 한 번) · 입력 {js.state_chars(state):,}자 …")
        t0 = time.monotonic()
        merged: Dict[str, Any] = {}
        errors: List[str] = []
        for row in turns:
            n = ji._turn(row)
            part = {"guide": fixed["guide"], "질문번호": q, "턴": [t for t in doc["턴"] if t["턴"] <= n]}
            status, body, error = jev.judge(part, {f"T{n}:{i}": fixed["items"][i] for i in jev_items}) if jev else ("ERROR", None, "Jev 없음")
            if status == "OK":
                merged.update((body or {}).get("answers") or {})
            else:
                errors.append(f"T{n} {status} {str(error)[:80]}")
        status = "OK" if not errors else ("PARTIAL" if merged else "ERROR")
        calls["jev"] = {"status": status, "error": "; ".join(errors), "seconds": round(time.monotonic() - t0, 1), "calls": len(turns),
                        "body": {"answers": merged}}
        say(f"   🔎 {tag} Jev {status} {calls['jev']['seconds']}초" + (f" — {'; '.join(errors)[:160]}" if errors else ""))

    started = time.monotonic()
    threads = [threading.Thread(target=run_codex), threading.Thread(target=run_jev)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    (out / "codex" / name).write_text(json.dumps(calls["codex"].get("output"), ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "jev" / name).write_text(json.dumps(calls["jev"].get("body"), ensure_ascii=False, indent=1), encoding="utf-8")

    codex = check_codex(calls["codex"].get("output") if calls["codex"]["status"] == "OK" else None, turns, answers)
    jev_res = js.check_jev(calls["jev"].get("body"), {ji._turn(r): jev_items for r in turns}, fixed) if calls["jev"]["status"] in ("OK", "PARTIAL") else {}
    final: Dict[int, Dict[str, Dict[str, Any]]] = {}
    turn_out: Dict[str, Any] = {}
    agree = total = 0
    for row in turns:
        n = ji._turn(row)
        c_items, j_items = codex[n]["items"], jev_res.get(n, {})
        items: Dict[str, Dict[str, Any]] = {}
        for item in dict.fromkeys(list(c_items) + [i for i in jev_items if i in j_items]):
            cv, jv = c_items.get(item), j_items.get(item)
            if item in jev_items:
                dec, how = combine(cv, jv)
                if jv and (cv or {}).get("decision") is not None:
                    total += 1
                    agree += (jv.get("decision") == cv.get("decision"))
            else:
                dec, how = (cv or {}).get("decision"), "Codex"
            if dec is None:
                continue
            e = ((cv or {}).get("entries") or [{}])
            fail_e = next((x for x in e if x.get("decision") == "FAIL"), e[0])
            items[item] = {"decision": dec, "how": how, "codex": (cv or {}).get("decision"), "jev": (jv or {}).get("decision"),
                           "jev_reason": (jv or {}).get("reason"), "problems": (cv or {}).get("problems") or [],
                           "reason": fail_e.get("reason"), "answer_quote": fail_e.get("answer_quote"), "question_quote": fail_e.get("question_quote"),
                           "entries": (cv or {}).get("entries") or []}
        js.apply_overrides(items)
        final[n] = items
        table_subs = [s["sub"] for s in table[row["case_id"]]["situations"]]
        turn_out[str(n)] = {"situations_judge": codex[n]["situations"], "situations_table": table_subs,
                            "only_judge": [s for s in codex[n]["situations"] if s not in table_subs],
                            "only_table": [s for s in table_subs if s not in codex[n]["situations"]],
                            "evidence": codex[n]["evidence"], "problems": codex[n]["problems"], "items": items}
    semantic = verdict_of(final) if calls["codex"]["status"] == "OK" else "HOLD"
    verdict = "FAIL" if code_fails or semantic == "FAIL" else semantic
    cs = [c for c in ((calls["codex"].get("output") or {}).get("conclusions") or [(calls["codex"].get("output") or {}).get("conclusion")]) if isinstance(c, dict)]         if isinstance(calls["codex"].get("output"), dict) else []
    concl = None if not cs else {"verdict": "FAIL" if any(c.get("verdict") == "FAIL" for c in cs) else ("HOLD" if any(c.get("verdict") == "HOLD" for c in cs) else "PASS"),
                                 "reason": " / ".join(c.get("reason") or "" for c in cs)}   # 턴을 나눠 물었으면 결론도 합친다
    line = {"version": VERSION, "provider": provider, "question": q, "turns": len(turns), "verdict": verdict, "semantic": semantic,
            "code_fails": code_fails, "codex_conclusion": concl, "jev_codex_agree": [agree, total], "seconds": round(time.monotonic() - started, 1),
            "calls": {k: {kk: vv for kk, vv in v.items() if kk not in ("output", "body")} for k, v in calls.items()}, "turn_results": turn_out}
    fails = [f"T{n}:{i}" for n, p in final.items() for i, v in p.items() if v["decision"] == "FAIL"]
    holds = [f"T{n}:{i}" for n, p in final.items() for i, v in p.items() if v["decision"] == "UNVERIFIED"]
    say(f"■ {tag}: {verdict} (Codex 결론 {(concl or {}).get('verdict', '-')}) · {line['seconds']:.0f}초"
        + (f" · CODE FAIL {' '.join(code_fails)}" if code_fails else "") + (f" · FAIL {' '.join(fails)}" if fails else "")
        + (f" · 보류 {' '.join(holds)}" if holds else ""))
    return line


# --- 보고 -------------------------------------------------------------------------------------------------------------

def report_md(lines: List[Dict[str, Any]]) -> str:
    md = [f"# 판정 v4 시험 ({datetime.now().strftime('%Y-%m-%d %H:%M')})", "",
          "심판이 받은 것: 고정 프롬프트 v4 + 질문·첨부·참조 자료·답변 JSON (판정할 항목 목록 없음).", "",
          "| 질문 | 모델 | 턴 | 최종 | Codex 결론 | CODE FAIL | Jev·Codex 같은 판정 | Codex 초 | Jev 초 |", "|---|---|---|---|---|---|---|---|---|"]
    for l in lines:
        c = l["calls"]
        md.append(f"| Q{l['question']} | {l['provider']} | {l['turns']} | {l['verdict']} | {(l['codex_conclusion'] or {}).get('verdict', '-')} | "
                  f"{' '.join(l['code_fails']) or '-'} | {l['jev_codex_agree'][0]}/{l['jev_codex_agree'][1]} | "
                  f"{(c.get('codex') or {}).get('seconds', '-')} | {(c.get('jev') or {}).get('seconds', '-')} |")
    for l in lines:
        md += ["", f"## Q{l['question']} {l['provider']} — {l['verdict']}", f"Codex 결론 이유: {(l['codex_conclusion'] or {}).get('reason', '-')}"]
        for n, t in l["turn_results"].items():
            md.append(f"- {n}턴 심판이 찾은 상황: {' '.join(t['situations_judge'])}")
            if t["only_judge"] or t["only_table"]:
                md.append(f"  - 판정상황표와 차이 — 심판만: {' '.join(t['only_judge']) or '없음'} · 표만: {' '.join(t['only_table']) or '없음'}")
            for sid in t["only_judge"]:
                if t["evidence"].get(sid):
                    md.append(f"  - {sid} 근거: 「{t['evidence'][sid][:120]}」")
            for p in t["problems"]:
                md.append(f"  - 출력 문제: {p}")
            for item, v in t["items"].items():
                if v["decision"] in ("FAIL", "UNVERIFIED"):
                    md.append(f"  - {KO[v['decision']]} {item} ({v['how']}) — {v.get('reason') or ''}"
                              + (f" 「{(v.get('answer_quote') or '')[:100]}」" if v.get("answer_quote") else "")
                              + (f" / Jev: {v['jev_reason'][:100]}" if v["decision"] == "UNVERIFIED" and v.get("jev_reason") else "")
                              + (f" / 문제: {'; '.join(v['problems'])[:120]}" if v.get("problems") else ""))
    return "\n".join(md) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--pick", required=True, help="질문:모델 쉼표 목록 — 예: 143:claude,115:aiona,17:gemini")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--reuse-codex", action="store_true", help="v4/codex/ 에 저장된 Codex 출력이 있으면 다시 부르지 않고 쓴다 (Jev 만 다시)")
    args = ap.parse_args(argv)
    picks = [(int(p.split(":")[0]), p.split(":")[1]) for p in args.pick.split(",") if p.strip()]
    run = json.loads((ROOT / "data" / "comparison_runs" / f"{args.run}.json").read_text(encoding="utf-8"))
    rows = flatten(json.loads((SET_DIR / "questions.json").read_text(encoding="utf-8")))
    table = {r["case_id"]: r for r in json.loads((SET_DIR / ji.SITUATIONS).read_text(encoding="utf-8"))["rows"]}
    prompt = PROMPT_V4.read_text(encoding="utf-8")
    fixed = json.loads(JEV_V4.read_text(encoding="utf-8"))
    gpt, jev, notes = judges_from_env()
    for n in notes:
        say(f"⚠ {n}")
    out = ROOT / "data" / "judge_runs" / args.run / "v4"
    for sub in ("inputs", "codex", "jev"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    tasks = []
    for q, provider in picks:
        turns = sorted([r for r in rows if ji._question(r["case_id"]) == q], key=ji._turn)
        data = {r.get("case_id"): r for r in (run.get("datasets") or {}).get(provider) or [] if isinstance(r, dict)}
        if not all(ji.collected(data.get(t["case_id"])) and (data[t["case_id"]].get("answer") or "").strip() for t in turns):
            say(f"⚠ Q{q} {provider}: 답을 다 받지 못한 턴이 있어 건너뜀")
            continue
        answers = {t["case_id"]: data[t["case_id"]]["answer"] for t in turns}
        deliveries = {t["case_id"]: ji.delivery_note(data.get(t["case_id"])) for t in turns}
        cases = {c.get("case_id"): c for c in ((run.get("evaluations") or {}).get(provider) or {}).get("cases") or [] if isinstance(c, dict)}
        code_fails = sorted({c for t in turns for c in question_outcome(cases.get(t["case_id"]), None).get("code_fails") or []})
        tasks.append((q, provider, turns, answers, deliveries, code_fails))
    say(f"판정 v4 시험 — {len(tasks)}건 · Codex CLI {getattr(gpt, 'model', '-')}·{getattr(gpt, 'reasoning', '-')} + Jev · 프롬프트 {PROMPT_V4.name}")
    wall0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        lines = list(pool.map(lambda t: judge_one(*t[:5], table, t[5], prompt, fixed, gpt, jev, out, args.reuse_codex), tasks))
    with (out / "results.jsonl").open("a", encoding="utf-8") as f:
        for l in lines:
            f.write(json.dumps({**l, "at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")
    md = report_md(lines)
    (out / "report.md").write_text(md, encoding="utf-8")
    say(f"\n판정 v4 시험 끝 — 벽시계 {time.monotonic() - wall0:.0f}초 · 보고 {out / 'report.md'}")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
