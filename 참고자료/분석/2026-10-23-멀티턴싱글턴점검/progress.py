"""멀티턴 · 싱글턴 점검 진행 현황 — 진행.json 과 질문 세트(78문항)로 유형마다 끝남 · 확인 중 · 남음을 표로 낸다.

    python -X utf8 참고자료/분석/2026-10-23-멀티턴싱글턴점검/progress.py            # 표 출력
    python -X utf8 참고자료/분석/2026-10-23-멀티턴싱글턴점검/progress.py --write    # 점검기록.md 의 '진행 현황' 절도 바꿈
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TYPES = [("단일 질문", "단일 질문"), ("롱컨텍스트", "롱컨텍스트"), ("첨부자료 확인·요약", "첨부 자료"), ("명시적 빈 출력", "빈 출력 요구 (규칙 처리)")]


def table() -> str:
    qs = json.loads((ROOT / "참고자료/질문세트_78/questions.json").read_text(encoding="utf-8"))
    st = {(it["q"], it["turn"]): it["state"] for it in json.loads((HERE / "진행.json").read_text(encoding="utf-8"))["items"]}
    lines = ["| 유형 | 끝남 | 확인 중 | 남음 |", "|---|---|---|---|"]
    tot_done = tot_all = 0
    for ty, label in TYPES:
        ids = sorted(q["id"] for q in qs if q["type"] == ty)
        done = [i for i in ids if st.get((i, 1)) == "done"]
        prop = [i for i in ids if st.get((i, 1)) == "proposed"]
        rest = [i for i in ids if (i, 1) not in st]
        tot_done += len(done); tot_all += len(ids)
        fmt = lambda xs: " ".join(f"Q{i}" for i in xs) or "—"
        lines.append(f"| {label} {len(ids)} | {len(done)} — {fmt(done)} | {fmt(prop)} | {len(rest)} — {fmt(rest)} |")
    mt = sorted((q for q in qs if q["type"].startswith("10턴")), key=lambda q: q["id"])
    t_done = t_all = 0
    cells = []
    for q in mt:
        n = len(q["turns"]); t_all += n
        d = sorted(t for (i, t), s in st.items() if i == q["id"] and s == "done"); t_done += len(d)
        p = sorted(t for (i, t), s in st.items() if i == q["id"] and s == "proposed")
        mark = "".join("■" if t in d else "◧" if t in p else "□" for t in range(1, n + 1))
        cells.append(f"Q{q['id']} {mark}")
    lines.append(f"| 멀티턴 {len(mt)}대화 · {t_all}턴 | {t_done}턴 | ◧ 표시 | {t_all - t_done}턴 — " + " · ".join(cells) + " |")
    head = (f"싱글형 {tot_all}문항 중 끝 {tot_done} · 멀티턴 {t_all}턴 중 끝 {t_done} "
            f"(■ 끝남 · ◧ 확인 중 · □ 남음)")
    return head + "\n\n" + "\n".join(lines)


MODELS = [("aiona", "AIONA"), ("gpt", "GPT"), ("claude", "Claude"), ("gemini", "Gemini")]


def judgments() -> str:
    """판단 기록표 — 질문(멀티턴은 턴)마다 한 줄, 모델마다 '원래 → 판단대로 (기준 판단)'. 판단.json 에서."""
    qs = {q["id"]: q for q in json.loads((ROOT / "참고자료/질문세트_78/questions.json").read_text(encoding="utf-8"))}
    st = {(it["q"], it["turn"]): it["state"] for it in json.loads((HERE / "진행.json").read_text(encoding="utf-8"))["items"]}
    rows = json.loads((HERE / "판단.json").read_text(encoding="utf-8"))["rows"]
    by: dict = {}
    for r in rows:
        by.setdefault((r["sample"], r["q"], r["turn"]), {})[r["model"]] = r
    cnt: dict = {}
    dup = changed = total = 0
    lines = ["| 표본 | 질문 · 턴 | 상태 | " + " | ".join(n for _p, n in MODELS) + " |", "|---|---|---|" + "---|" * len(MODELS)]
    for (smp, q, t), ms in sorted(by.items()):
        multi = qs[q]["type"].startswith("10턴")
        cells = []
        for p, _n in MODELS:
            r = ms.get(p)
            if not r:
                cells.append("—"); continue
            total += 1
            moved = r["after"] != r["original"]
            changed += moved
            for _c, _o, lab in r["items"]:
                cnt[lab.split("·")[0]] = cnt.get(lab.split("·")[0], 0) + 1
                dup += "중복" in lab
            what = ", ".join(f"{c} {lab}" for c, _o, lab in r["items"]) or "통과 정당"
            cells.append(f"{r['original']} → {'**' + r['after'] + '**' if moved else r['after']} ({what})")
        state = {"done": "정함", "proposed": "제안"}.get(st.get((q, t)), "—")
        lines.append(f"| {smp} | Q{q}" + (f" {t}턴" if multi else "") + f" | {state} | " + " | ".join(cells) + " |")
    order = ["오판", "타당", "애매", "놓침"]
    head = ("항목 판단 — " + " · ".join(f"{k} {cnt.get(k, 0)}" for k in order) + f" (그중 중복 {dup})"
            f" · 칸 {total}개 중 판단대로면 결과가 바뀌는 칸 {changed}")
    return head + "\n\n" + "\n".join(lines)


def put(s: str, title: str, body: str, before: str) -> str:
    block = f"## {title}\n\n{body}\n"
    if f"## {title}" in s:
        return re.sub(rf"## {re.escape(title)}\n.*?(?=\n## |\Z)", block, s, flags=re.S)
    return s.replace(f"## {before}", block + f"\n## {before}", 1)


if __name__ == "__main__":
    out, jt = table(), judgments()
    print(out + "\n\n" + jt)
    if "--write" in sys.argv:
        doc = HERE / "점검기록.md"
        s = doc.read_text(encoding="utf-8")
        s = put(s, "진행 현황", out, "판정 구성")
        s = put(s, "판단 기록표", jt, "판정 구성")
        doc.write_text(s, encoding="utf-8")
