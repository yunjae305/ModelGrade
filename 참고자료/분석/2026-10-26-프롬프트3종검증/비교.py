"""판정 프롬프트 3종(stances) 검증 — 사람이 정한 칸(사람 표시 43 · 팀원 판정 중 LLM 항목)에 옛 흐름(v4-f1, 사람 판정 전)과 새 흐름(v4-stances-val)이
얼마나 맞는지 · Codex 로 넘긴 항목 수 · 1차 표 갈림 · 시간 (사용자 10-26 저녁 · 10-26 미팅 '세 형태로 판단 로직 검증').

    python -X utf8 참고자료/분석/2026-10-26-프롬프트3종검증/비교.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools import report_html as rh  # noqa: E402

HERE = Path(__file__).resolve().parent
RUN = ROOT / "data" / "judge_runs" / "50ca3ccccace"
OLD, NEW = "v4-f1", "v4-stances-val"
KO = {"PASS": "통과", "FAIL": "FAIL", "NOT_APPLICABLE": "해당 없음", "UNVERIFIED": "보류", None: "없음"}


def last_lines(path: Path) -> dict:
    out = {}
    for x in path.read_text(encoding="utf-8").splitlines():
        if x.strip():
            l = json.loads(x)
            out[(l["provider"], int(l["question"]))] = l
    return out


def cells() -> list:
    """사람이 정한 칸 — (모델, 질문, 턴, 항목, 출처, 사람 값). 사람 표시가 먼저, 팀원 판정은 LLM 항목의 PASS · FAIL · 해당 없음만."""
    marks = json.loads((ROOT / "참고자료/분석/2026-10-26-사람표시/표시결과_wuyung027.json").read_text(encoding="utf-8"))["rows"]
    team = json.loads((RUN / OLD / "human_decisions.json").read_text(encoding="utf-8"))["decisions"]
    got = {}
    for r in marks:
        got[(r["provider"], r["question"], r["turn"], r["item"])] = ("사람 표시", r["mark"])
    for d in team:
        if str(d.get("item", "")).startswith("J") and d.get("new") in ("PASS", "FAIL", "NOT_APPLICABLE"):
            got.setdefault((d["provider"], d["question"], d["turn"], d["item"]), ("팀원 판정", d["new"]))
    return [(*k, *v) for k, v in sorted(got.items())]


def decision(line: dict, turn: int, item: str):
    for it in rh.normalize_items(line):
        if it["turn"] == turn and it["item"] == item:
            return it["decision"]
    return None


def same(a, h) -> bool:
    """맞음 — 같은 값, 또는 사람이 통과 · 해당 없음이고 판정도 통과 · 해당 없음(둘 다 'FAIL 아님')."""
    return a == h or (a in ("PASS", "NOT_APPLICABLE") and h in ("PASS", "NOT_APPLICABLE"))


def flagged(line: dict) -> int:
    return sum(1 for it in rh.normalize_items(line) if it.get("flagged"))


def main() -> int:
    old, new = last_lines(RUN / OLD / "results.jsonl"), last_lines(RUN / NEW / "results.jsonl")
    rows, score = [], Counter()
    for p, q, t, item, src, h in cells():
        if (p, q) not in new:
            continue
        o, n = decision(old[(p, q)], t, item), decision(new[(p, q)], t, item)
        if o is None or n is None:   # 한쪽이 그 칸을 판정하지 못함(연결 오류 등) — 맞음을 셀 때 뺀다
            score["판정 못 한 칸"] += 1
            rows.append((p, q, t, item, src, h, o, n))
            continue
        score["칸"] += 1
        score["옛 맞음"] += same(o, h)
        score["새 맞음"] += same(n, h)
        score["옛 보류"] += o == "UNVERIFIED"
        score["새 보류"] += n == "UNVERIFIED"
        score[f"사람 {h}"] += 1
        if h == "FAIL":
            score["FAIL 칸"] += 1
            score["옛 FAIL 잡음"] += o == "FAIL"
            score["새 FAIL 잡음"] += n == "FAIL"
        rows.append((p, q, t, item, src, h, o, n))
    pairs = sorted(new)
    fo = sum(flagged(old[k]) for k in pairs)
    fn = sum(flagged(new[k]) for k in pairs)
    so = sum(float(old[k].get("seconds") or 0) for k in pairs)
    sn = sum(float(new[k].get("seconds") or 0) for k in pairs)
    vo = Counter(old[k].get("verdict") for k in pairs)
    vn = Counter(new[k].get("verdict") for k in pairs)
    split = Counter()
    for k in pairs:
        for tn, block in (new[k].get("turn_results") or {}).items():
            for i, v in ((block or {}).get("items") or {}).items():
                if isinstance(v, dict) and v.get("first_vote_detail"):
                    split["항목"] += 1
                    split["표 갈림"] += bool(v.get("first_vote_split"))
                    split["버린 표 있음"] += any(not d.get("counted") for d in v["first_vote_detail"])
    n = score["칸"]
    lines = [
        "# 판정 프롬프트 3종 검증 — 사람이 정한 칸과 맞춰 보기 (10-26 저녁)",
        "",
        f"대상: 사람이 정한 칸 {n}개(사람 표시 · 팀원 판정의 LLM 항목)가 있는 (모델, 질문) {len(pairs)}개. 옛 흐름 = `{OLD}`(10-26 회의 흐름, 사람 판정 전) · "
        f"새 흐름 = `{NEW}`(크기로 Jev 또는 Decisions, 프롬프트 3종 가중 다수결 → FAIL · 애매만 Codex). 같은 답 · 같은 판정 프롬프트.",
        "",
        "| | 옛 흐름 | 새 흐름 |",
        "|---|---|---|",
        f"| 사람과 맞음 (통과 · 해당 없음은 같은 쪽으로) | {score['옛 맞음']} / {n} ({score['옛 맞음'] / max(1, n) * 100:.0f}%) | {score['새 맞음']} / {n} ({score['새 맞음'] / max(1, n) * 100:.0f}%) |",
        f"| 사람 FAIL 칸을 FAIL 로 | {score['옛 FAIL 잡음']} / {score['FAIL 칸']} | {score['새 FAIL 잡음']} / {score['FAIL 칸']} |",
        f"| 보류로 남은 칸 | {score['옛 보류']} | {score['새 보류']} |",
        f"| Codex 로 넘긴 항목(질문 {len(pairs)}개 합) | {fo} | {fn} |",
        f"| 판정 시간 합 | {so / 60:.0f}분 | {sn / 60:.0f}분 |",
        f"| 한쪽이 판정하지 못해 뺀 칸 | {score['판정 못 한 칸']} | |",
        f"| 질문 결론 (통과 · FAIL · 보류) | {vo['PASS']} · {vo['FAIL']} · {vo['HOLD']} | {vn['PASS']} · {vn['FAIL']} · {vn['HOLD']} |",
        "",
        f"새 흐름 1차 표: 항목 {split['항목']}개 중 태도마다 다른 값이 나온 항목 {split['표 갈림']}개 · 확률 0.6 미만이라 버린 표가 있는 항목 {split['버린 표 있음']}개.",
        "",
        "## 칸마다",
        "",
        "| 모델 | 문항 | 기준 | 출처 | 사람 | 옛 흐름 | 새 흐름 |",
        "|---|---|---|---|---|---|---|",
    ]
    for p, q, t, item, src, h, o, nn in rows:
        mark = lambda a: KO.get(a, a) + ("" if a is None or same(a, h) else " ✗")
        lines.append(f"| {rh.LABEL.get(p, p)} | {rh.unit_label(q, t) if t and len(str(t)) else 'Q' + str(q)} | {item} | {src} | {KO.get(h, h)} | {mark(o)} | {mark(nn)} |")
    (HERE / "정리.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:16]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
