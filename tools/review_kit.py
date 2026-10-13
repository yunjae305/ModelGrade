"""사람 검수 키트 — 블라인드 검수표를 만들고(make), 검수자 시트를 합쳐 일치율·불일치·사람 합의 정답을 낸다(merge). API 호출 없음.

    python tools/review_kit.py make data/comparison_runs/<run>.json --include-pass 3      # results/review/<run>-J5-kit/
    python tools/review_kit.py merge results/review/<run>-J5-kit 검수자A.csv 검수자B.csv   # 합의 → gold.jsonl

make 가 만드는 파일 (results/ 는 git 에 올라가지 않는다 — 내부 검수용, 외부 공유 금지):
- review_blind.csv — 검수자가 채우는 표. 시스템(모델) 이름·AI 판정·AI 사유·AI 가 찾은 출처는 넣지 않고, FAIL 과 PASS 대조군을 섞어
  무작위 순서로 둔다. 확인할 문장(평가자가 답변에서 뽑은 주장, 판정 없이)과 답변이 든 URL 만 보조로 준다.
  검수자는 reviewer · reviewed_at · label(정상 / 실제 오류 / 검증 불가) · evidence(근거 URL·인용) · note 를 채운다.
- review_open.csv — 블라인드 검수를 마친 뒤 의견을 맞출 때 쓰는 표 (AI 판정·원인·사유·출처 포함). 블라인드 검수 전에는 열지 않는다.
- key.json — 검수 항목 ID ↔ 실행·시스템·질문 ID·AI 판정 (merge 가 쓴다).
merge 는 사람이 채운 label 만 쓴다. 비어 있는 항목에 AI 판정으로 정답을 채우지 않는다. 검수자가 둘 이상이면 모두 같을 때만 합의,
다르면 '불일치'로 남기고 정답을 만들지 않는다. 한 명만 본 항목은 --allow-single 일 때만 정답에 넣는다(단독 검수로 표시).
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval.jev_criteria import CRITERIA  # noqa: E402
from tools.export_review import _claims, _failed_sources, _text, cause_of  # noqa: E402

KIT_VERSION = "review-kit-v1"
LABELS = {"정상": "PASS", "실제 오류": "FAIL", "검증 불가": "UNVERIFIABLE"}
REVIEWER_FIELDS = ("reviewer", "reviewed_at", "label", "evidence", "note")
BLIND = ("item_id", "criterion", "criterion_question", "pass_rule", "fail_rule", "question", "answer", "check_sentences",
         "answer_urls") + REVIEWER_FIELDS
OPEN = BLIND[:-len(REVIEWER_FIELDS)] + ("system", "case_id", "ai_decision", "ai_cause", "ai_cause_label", "ai_reason",
                                        "ai_original_choice", "ai_failed_sources") + REVIEWER_FIELDS
_URL = re.compile(r"https?://[^\s)>\]\"'`]+")


def _sentences(decision: Dict[str, Any]) -> str:
    """평가자가 답변에서 뽑은 확인 대상 문장 — 판정([PASS] 등)은 떼고 문장만."""
    return " | ".join(re.sub(r"^\[[A-Z]+\]\s*", "", part) for part in _claims(decision).split(" | ") if part)


def make(run: Dict[str, Any], out: Path, *, criterion: str = "J5", include_pass: int = 0, text: str = "full", seed: int = 0) -> Dict[str, Any]:
    spec = next(c for c in CRITERIA if c.id == criterion)
    rng = random.Random(f"{seed}:{criterion}")   # PASS 대조군은 tools/export_review.py 와 같은 시드 — 같은 검수 대상
    order = random.Random(f"{seed}:{criterion}:{run.get('run_id')}:order")
    items = []
    for system, batch in sorted((run.get("evaluations") or {}).items()):
        cases = batch.get("cases") or []
        records = (batch.get("jev_eval") or {}).get("records") or []
        fails, passes = [], []
        for index, record in enumerate(records):
            if isinstance(record, dict) and criterion in (record.get("criteria") or []):
                decision = (record.get("decisions") or {}).get(criterion) or {}
                (fails if decision.get("decision") == "FAIL" else passes if decision.get("decision") == "PASS" else []).append(index)
        for index in fails + sorted(rng.sample(passes, min(include_pass, len(passes)))):
            record, case = records[index], cases[index] if index < len(cases) else {}
            decision = record["decisions"][criterion]
            cause = cause_of(decision)
            answer = case.get("answer") or ""
            items.append({"system": system, "case_id": case.get("case_id") or record.get("case_id"), "index": index,
                          "criterion": criterion, "criterion_question": spec.question, "pass_rule": spec.pass_rule, "fail_rule": spec.fail_rule,
                          "question": _text(case.get("question") or "", "question", text), "answer": _text(answer, "answer", text),
                          "check_sentences": _sentences(decision) if text != "none" else "", "answer_urls": " ".join(dict.fromkeys(_URL.findall(answer))),
                          "ai_decision": decision.get("decision"), "ai_cause": cause["cause"], "ai_cause_label": cause["label"],
                          "ai_reason": decision.get("reason"), "ai_original_choice": (decision.get("model_original_assessment") or {}).get("choice"),
                          "ai_failed_sources": _failed_sources(decision)})
    order.shuffle(items)   # FAIL 이 앞에 몰려 있으면 순서가 판정을 알려 준다
    for n, item in enumerate(items, 1):
        item["item_id"] = f"R{n:03d}"
        item.update({field: "" for field in REVIEWER_FIELDS})
    out.mkdir(parents=True, exist_ok=True)
    for name, columns in (("review_blind.csv", BLIND), ("review_open.csv", OPEN)):
        with open(out / name, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(items)
    key = {"version": KIT_VERSION, "run_id": run.get("run_id"), "criterion": criterion, "text_scope": text, "seed": seed,
           "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "items": {i["item_id"]: {k: i[k] for k in ("system", "case_id", "index", "ai_decision", "ai_cause")} for i in items}}
    (out / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "README.txt").write_text(
        "사람 검수 안내 (내부용 — 외부 공유 금지)\n"
        "1. review_blind.csv 를 검수자마다 복사해 각자 채운다 (다른 검수자 시트·review_open.csv 는 보지 않는다).\n"
        "2. label 은 '정상'(답변의 사실이 맞음) · '실제 오류'(근거로 틀렸다고 확인) · '검증 불가'(확인할 수 없음) 중 하나만.\n"
        "3. reviewer(이름), reviewed_at(예: 2026-10-14 13:20), evidence(확인한 출처 URL·인용)를 함께 적는다. 모르면 비워 둔다 — 추측해 채우지 않는다.\n"
        "4. 다 채운 뒤 python tools/review_kit.py merge <이 폴더> <시트들> 로 합친다. 의견이 다른 항목은 review_open.csv 를 보고 다시 논의한다.\n",
        encoding="utf-8")
    return {"version": KIT_VERSION, "out": str(out), "items": len(items),
            "ai_decisions": dict(Counter(i["ai_decision"] for i in items)), "text_scope": text}


def _kappa(pairs: List[tuple]) -> Optional[float]:
    """두 검수자 Cohen's kappa — 둘 다 label 을 단 항목만."""
    if not pairs:
        return None
    labels = sorted({x for pair in pairs for x in pair})
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    first, second = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(first[l] * second[l] for l in labels) / (n * n)
    return round((observed - expected) / (1 - expected), 3) if expected < 1 else 1.0


def merge(kit: Path, sheets: List[Path], *, allow_single: bool = False) -> Dict[str, Any]:
    key = json.loads((kit / "key.json").read_text(encoding="utf-8"))
    by_item: Dict[str, Dict[str, Dict[str, str]]] = {item: {} for item in key["items"]}
    problems = []
    for sheet in sheets:
        with open(sheet, encoding="utf-8-sig", newline="") as handle:
            for line, row in enumerate(csv.DictReader(handle), 2):
                item, label = (row.get("item_id") or "").strip(), (row.get("label") or "").strip()
                if not label:
                    continue   # 미검수 — 정답으로 쓰지 않는다
                if item not in by_item:
                    problems.append(f"{sheet.name}:{line} 알 수 없는 항목 {item}")
                    continue
                reviewer, when = (row.get("reviewer") or "").strip(), (row.get("reviewed_at") or "").strip()
                if label not in LABELS or not reviewer or not when:
                    problems.append(f"{sheet.name}:{line} {item} — label 은 정상/실제 오류/검증 불가, reviewer·reviewed_at 필수")
                    continue
                if reviewer in by_item[item]:
                    problems.append(f"{sheet.name}:{line} {item} — 검수자 {reviewer} 의 판정이 두 번 있음")
                    continue
                by_item[item][reviewer] = {"label": label, "reviewed_at": when, "evidence": (row.get("evidence") or "").strip(),
                                           "note": (row.get("note") or "").strip()}
    if problems:
        raise ValueError("검수 시트 오류 — 고친 뒤 다시 합친다:\n" + "\n".join(problems[:20]))
    gold, disagreements, statuses = [], [], Counter()
    reviewers = sorted({r for marks in by_item.values() for r in marks})
    for item, marks in by_item.items():
        labels = {m["label"] for m in marks.values()}
        if not marks:
            statuses["미검수"] += 1
            continue
        if len(labels) > 1:
            statuses["불일치"] += 1
            disagreements.append({"item_id": item, **{r: marks[r]["label"] for r in marks},
                                  "notes": " / ".join(f"{r}: {m['note']}" for r, m in marks.items() if m["note"])})
            continue
        if len(marks) == 1 and not allow_single:
            statuses["단독 검수(정답 제외)"] += 1
            continue
        statuses["합의" if len(marks) > 1 else "단독 검수"] += 1
        meta = key["items"][item]
        label = labels.pop()
        gold.append({"system": meta["system"], "case_id": meta["case_id"], "criterion": key["criterion"], "gold": LABELS[label],
                     "label": label, "item_id": item, "reviewers": sorted(marks), "reviewed_at": sorted(m["reviewed_at"] for m in marks.values()),
                     "single_reviewer": len(marks) == 1, "evidence": [m["evidence"] for m in marks.values() if m["evidence"]]})
    pair_stats = {}
    for i, a in enumerate(reviewers):
        for b in reviewers[i + 1:]:
            pairs = [(m[a]["label"], m[b]["label"]) for m in by_item.values() if a in m and b in m]
            pair_stats[f"{a}~{b}"] = {"items": len(pairs), "agreement": round(sum(x == y for x, y in pairs) / len(pairs), 3) if pairs else None,
                                      "kappa": _kappa(pairs)}
    ai_vs_human = Counter((key["items"][g["item_id"]]["ai_decision"], g["label"]) for g in gold)
    (kit / "gold.jsonl").write_text("".join(json.dumps(g, ensure_ascii=False) + "\n" for g in gold), encoding="utf-8")
    with open(kit / "disagreements.csv", "w", encoding="utf-8-sig", newline="") as handle:
        fields = ["item_id"] + reviewers + ["notes"]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(disagreements)
    return {"version": KIT_VERSION, "items": len(by_item), "reviewers": reviewers, "status": dict(statuses), "gold": len(gold),
            "pairs": pair_stats, "ai_vs_human": {f"AI {a} → 사람 {h}": n for (a, h), n in sorted(ai_vs_human.items())}}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("make")
    m.add_argument("run", type=Path)
    m.add_argument("--criterion", default="J5", choices=[c.id for c in CRITERIA])
    m.add_argument("--include-pass", type=int, default=0)
    m.add_argument("--text", choices=("full", "excerpt", "none"), default="full")
    m.add_argument("--seed", type=int, default=0)
    m.add_argument("--out-dir", type=Path)
    g = sub.add_parser("merge")
    g.add_argument("kit", type=Path)
    g.add_argument("sheets", type=Path, nargs="+")
    g.add_argument("--allow-single", action="store_true")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.command == "make":
        run = json.loads(args.run.read_text(encoding="utf-8-sig"))
        out = args.out_dir or ROOT / "results" / "review" / f"{run.get('run_id')}-{args.criterion}-kit"
        if (out / "key.json").exists():
            parser.error(f"{out} 에 이미 검수 키트가 있습니다 — 검수 중인 시트를 덮어쓰지 않도록 다른 --out-dir 을 쓰세요")
        report = make(run, out, criterion=args.criterion, include_pass=args.include_pass, text=args.text, seed=args.seed)
    else:
        report = merge(args.kit, args.sheets, allow_single=args.allow_single)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
