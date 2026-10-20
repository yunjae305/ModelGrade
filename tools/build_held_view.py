"""보류 함께 보기 데이터 (held-view-v1) — 10-20 보고 08장: 10-19 정밀 326 실행에서 질문 전체가 '보류'로 끝난 것을 질문·답변 원문과 쉬운 사유로
(사용자 10-20: "실제로 보류데이터로 판단한 것들이 무엇이 있는지 다같이 볼거야 — 일반인도 바로 이해할 수 있게 사유를 붙여서 질문 원본, 답변 원본 모두").
AI 호출 없음, 저장소 파일만 읽는다.

    python -X utf8 tools/build_held_view.py
    → 참고자료/발표/보류_8c61f117e661.js  (window.HELD_DATA — 보고 HTML 이 <script src> 로 읽음)

- 어느 것: 04장과 같은 규칙(tools/subset_pass_rates.verdicts_from_results)으로 질문 단위 판정이 보류인 (모델, 질문) — 모델별 수가 04장 표와 같아야 한다.
- 보류 이유: 참고자료/보류목록/8c61f117e661-held.csv(10-19 보류 1,034행)의 그 질문 행들 — 기준·사유 종류·심판이 쓴 글·확인한 문장·열지 못한 출처.
  보류 행이 없는 보류 질문은 결과 JSON 의 의미 평가 상태로 사유를 만든다(판정 미완료).
- 질문 원문: 10-19 에 던진 v2.0 글 — git 기록(질문세트 폴더 이름을 바꾼 커밋 0b0d6de, v2.1 수정 전)에서 읽는다. git 이 없으면 지금 글에서 v2.2 덧붙인 문장을 뗀다.
- 답변 원문: 참고자료/결과/8c61f117e661_정밀326_2026-10-19.json 의 전문. 긴 문서(참조 자료)는 글자 수와 앞부분만.
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from tools.subset_pass_rates import turn_outcome, verdicts_from_results  # noqa: E402
from webapp.question_generation import flatten  # noqa: E402

VERSION = "held-view-v1"
RUN = "8c61f117e661"
SET_DIR = ROOT / "참고자료" / "질문세트_326_생성프롬프트v2"
RESULTS = ROOT / "참고자료" / "결과" / f"{RUN}_정밀326_2026-10-19.json"
HELD_CSV = ROOT / "참고자료" / "보류목록" / f"{RUN}-held.csv"
OUT = ROOT / "참고자료" / "발표" / f"보류_{RUN}.js"
V20_COMMIT = "0b0d6de"
V22_SUFFIX = re.compile(r" 결과는 아티팩트·캔버스 없이 채팅 답변 안에 텍스트로 바로 보여 (주세요|줘)\.$")
EXPECTED = {"gpt": 59, "aiona": 76, "claude": 83, "gemini": 82}   # 04장 표의 326 보류 수
MODEL_NAMES = {"gpt": "GPT-6 Luna", "aiona": "AIONA", "claude": "Claude Sonnet 5.5", "gemini": "Gemini 3.6 Flash"}
REF_HEAD = 600
V22_EMPTY = re.compile(r"$^")   # 자리만 — 빈 출력 요구 턴은 보류목록 CSV 에 이미 행이 있다

#: 기준 이름 — 일반인이 읽는 말
CRITERIA = {
    "J1": ("요청 충실", "요청한 일을 해 주었는가"),
    "J2": ("지시 준수", "대상·말투·조건 같은 지시를 지켰는가"),
    "J3": ("일관성·논리", "앞뒤가 맞고 근거와 결론이 이어지는가"),
    "J4": ("완결성", "요청한 내용을 빠짐없이 담았는가"),
    "J5": ("DATA 신뢰도", "사실·숫자·출처가 정확한가"),
    "J6": ("대화 맥락 유지", "대화 앞부분의 지시·정보를 기억하고 따랐는가"),
    "J7": ("긴 문서 활용", "긴 문서에서 맞는 곳을 찾아 답했는가"),
    "J8": ("첨부 활용", "첨부파일을 읽고 답했는가"),
    "2-1.explicit_empty": ("빈 출력 요구", "'아무것도 출력하지 말라'는 지시를 지켰는가"),
}
#: 보류 사유 — (짧은 이름, 일반인용 설명, 질문 요약에 쓰는 말)
CAUSES = {
    "held": ("확인 못 함",
             "심판이 답에 나온 내용이 맞는지 확인할 근거를 찾지 못했습니다. 틀렸다는 증거도 없어서 실패로 치지 않고, 사람이 확인하도록 남겼습니다.",
             "심판이 내용이 맞는지 확인하지 못한 항목"),
    "source_unavailable": ("출처 확인 실패",
                           "심판은 사실을 확인하려고 웹 출처를 찾아 인용했지만, 프로그램이 그 출처를 다시 열어 인용문과 맞춰 보지 못했습니다"
                           "(접속 오류·시간 초과, 또는 열렸는데 인용한 문장이 원문에 없음). 심판 판단의 근거를 확인할 수 없어 사람이 확인합니다.",
                           "심판이 쓴 출처를 확인하지 못한 항목"),
    "judge_disagreement": ("심판 갈림",
                           "심판끼리 결론이 달랐습니다. 한쪽은 통과, 다른 쪽은 실패로 봐서 어느 쪽으로도 정하지 않고 사람이 정합니다.",
                           "심판끼리 결론이 갈린 항목"),
    "unverified": ("근거 부족",
                   "심판이 판단에 쓸 근거를 충분히 남기지 못했습니다. 근거 없이 통과·실패를 정할 수 없어서 사람이 확인합니다.",
                   "근거가 부족해 정하지 못한 항목"),
    "incomplete": ("판정 미완료",
                   "심판이 실패라고 했지만 근거를 대지 못했고, 한 번 더 물어도 근거 있는 판정을 받지 못했습니다.",
                   "다시 물어도 판정이 끝나지 않은 항목"),
    "empty_output_hold": ("빈 출력 요구",
                          "'아무것도 출력하지 말라'는 질문입니다. 빈 답이 지시를 지킨 것인지, 오류로 답을 못 받은 것인지 기계가 가리기 어려워 사람이 정하기로 했습니다(10-19 결정).",
                          "빈 출력 요구 질문"),
    "pending": ("판정 미완료",
                "이 질문은 의미 평가가 끝나지 않은 턴이 있어 통과로도 실패로도 정해지지 않았습니다.",
                "판정이 끝나지 않은 턴"),
}
PREFIX = re.compile(r"^(보류 — [^·]*?\(사람이 PASS/FAIL 을 정함\)\s*·?\s*)")
#: 출처 확인 상태 — 프로그램이 심판이 인용한 출처를 다시 열었을 때
SOURCE_STATUS = {"http_error": "접속 오류", "timeout": "시간 초과", "redirect_host_changed": "다른 사이트로 넘어감", "fetch_error": "가져오기 실패",
                 "unsupported_content_type": "읽을 수 없는 형식", "redirect_limit": "넘김이 너무 많음", "response_too_large": "문서가 너무 큼",
                 "unverified": "확인 못 함", "ok": "열렸지만 인용한 문장이 원문에 없음"}
CLAIM_SPLIT = re.compile(r"\s\|\s(?=\[(?:PASS|FAIL|UNVERIFIED|TRUE|FALSE)\])")


def v20_questions() -> Dict[str, str]:
    """10-19 에 던진 질문 글(v2.0)."""
    try:
        raw = subprocess.run(["git", "show", f"{V20_COMMIT}:참고자료/질문세트_326_생성프롬프트v2/questions.json"], cwd=ROOT,
                             capture_output=True, check=True).stdout.decode("utf-8")
        return {r["case_id"]: r.get("question") or "" for r in flatten(json.loads(raw))}
    except (OSError, subprocess.CalledProcessError, ValueError):
        rows = flatten(json.loads((SET_DIR / "questions.json").read_text(encoding="utf-8")))
        return {r["case_id"]: V22_SUFFIX.sub("", r.get("question") or "") for r in rows}


def claims(text: str) -> List[Dict[str, str]]:
    out = []
    for part in CLAIM_SPLIT.split(text or ""):
        m = re.match(r"\[(PASS|FAIL|UNVERIFIED|TRUE|FALSE)\]\s*(.*)", part.strip(), re.S)
        if m and m.group(2).strip(" |"):
            out.append({"tag": m.group(1), "text": m.group(2).strip(" |")})
    return out


def hold_item(r: Dict[str, str]) -> Dict[str, Any]:
    crit = r["criterion"]
    reason = PREFIX.sub("", (r.get("judge_reason") or "").strip())
    return {"criterion": crit, "criterion_name": CRITERIA.get(crit, (crit, ""))[0], "criterion_q": CRITERIA.get(crit, ("", ""))[1],
            "cause": r["cause"], "reason": reason, "original_choice": r.get("judge_original_choice") or "",
            "original_reason": (r.get("judge_original_reason") or "").strip(), "claims": claims(r.get("claims") or ""),
            "failed_sources": [{"status": SOURCE_STATUS.get(x.split()[0], x.split()[0]), "url": " ".join(x.split()[1:])}
                               for x in (s.strip() for s in re.split(r"\s*\|\s*|\n", r.get("failed_sources") or "")) if x]}


def headline(holds: List[Dict[str, Any]]) -> str:
    """질문 하나의 보류 이유 한두 문장 — 사유별 항목 수와 기준 이름."""
    if any(h["cause"] == "empty_output_hold" for h in holds):
        return CAUSES["empty_output_hold"][1]
    by: Dict[str, List[str]] = defaultdict(list)
    for h in holds:
        by[h["cause"]].append(h["criterion_name"])
    parts = [f"{CAUSES.get(c, (c, '', c))[2]} {len(v)}개({', '.join(sorted(set(v)))})" for c, v in sorted(by.items(), key=lambda x: -len(x[1]))]
    return "실패로 확정된 항목은 없지만, " + " · ".join(parts) + "가 남아 질문 전체를 보류했습니다."


def build() -> Dict[str, Any]:
    groups = json.loads((SET_DIR / "questions.json").read_text(encoding="utf-8"))
    rows = {r["case_id"]: r for r in flatten(groups)}
    kind = {int(g["id"]): ("긴 문서" if g["turns"][0].get("reference") else "대화" if len(g["turns"]) > 1 else "단일") for g in groups}
    data = json.loads(RESULTS.read_text(encoding="utf-8"))
    verdicts = verdicts_from_results(data, groups)
    held_rows: Dict[tuple, List[Dict[str, str]]] = defaultdict(list)
    with HELD_CSV.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            held_rows[(r["system"], r["case_id"])].append(r)
    qtext = v20_questions()
    items = []
    for provider in ("gpt", "aiona", "claude", "gemini"):
        turns_by_q: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for t in data["providers"][provider]["turns"]:
            turns_by_q[int(t["question"])].append(t)
        for q, v in sorted(verdicts[provider].items()):
            if v != "HOLD":
                continue
            turns = []
            all_holds = []
            for t in sorted(turns_by_q[q], key=lambda t: int(t["turn"])):
                cid = t["case_id"]
                row = rows[cid]
                holds = [hold_item(r) for r in held_rows.get((provider, cid), [])]
                sem = t.get("semantic_legacy") or {}
                if not holds and t.get("status") == "SUCCESS" and turn_outcome(t) == "PENDING" and not V22_EMPTY.search(qtext.get(cid, "")):
                    holds = [{"criterion": "", "criterion_name": "의미 평가", "criterion_q": "", "cause": "pending",
                              "reason": f"의미 평가 상태: {sem.get('status') or '없음'}", "original_choice": "", "original_reason": "",
                              "claims": [], "failed_sources": []}]
                ref = row.get("reference") if isinstance(row.get("reference"), str) else ""
                turns.append({"n": int(t["turn"]), "question": qtext.get(cid, row.get("question") or ""),
                              "attachments": [{"name": a.get("name"), "type": a.get("type"), "content": a.get("content") or ""}
                                              for a in row.get("attachments") or [] if isinstance(a, dict)],
                              "ref_chars": len(ref), "ref_head": ref[:REF_HEAD], "answer": t.get("answer") or "",
                              "collected": t.get("status"), "holds": holds})
                all_holds += holds
            if not all_holds:
                all_holds = [{"cause": "pending", "criterion_name": "의미 평가"}]
            causes = Counter(h["cause"] for h in all_holds)
            items.append({"id": f"{provider}-{q}", "model": provider, "q": q, "kind": kind[q], "turns_total": len(turns),
                          "held_turns": [t["n"] for t in turns if t["holds"]], "causes": dict(causes),
                          "criteria": sorted({h["criterion_name"] for h in all_holds}), "headline": headline(all_holds),
                          "turns": turns})
    counts = Counter(i["model"] for i in items)
    if dict(counts) != EXPECTED:
        print("경고: 모델별 보류 수가 04장과 다릅니다", dict(counts), EXPECTED)
    return {"version": VERSION, "run_id": RUN, "made_at": datetime.now(timezone.utc).isoformat(), "models": MODEL_NAMES,
            "causes": {k: {"label": v[0], "plain": v[1]} for k, v in CAUSES.items()},
            "criteria": {k: {"name": v[0], "q": v[1]} for k, v in CRITERIA.items()},
            "total_questions": 326, "items": items}


def main() -> int:
    doc = build()
    text = "window.HELD_DATA = " + json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + ";\n"
    OUT.write_text(text, encoding="utf-8")
    c = Counter(i["model"] for i in doc["items"])
    cause_q = Counter(k for i in doc["items"] for k in i["causes"])
    print(f"저장: {OUT} ({len(text.encode('utf-8')) / 1e6:.1f}MB) · 보류 {len(doc['items'])}건 {dict(c)} · 사유가 든 질문 {dict(cause_q)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
