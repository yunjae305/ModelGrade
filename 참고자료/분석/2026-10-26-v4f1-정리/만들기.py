"""v4-f1(10-26 전체 다시 판정) 보류 · FAIL · PASS 내역 정리 — 정리.md 를 만든다. 심판 호출 없음."""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools import report_html as rh  # noqa: E402

RUN, OUT = "50ca3ccccace", "v4-f1"
ORDER = ("claude", "aiona", "gpt", "gemini")
SHORT = {"claude": "Claude", "aiona": "AIONA", "gpt": "GPT", "gemini": "Gemini"}
last = {}
for l in (ROOT / "data" / "judge_runs" / RUN / OUT / "results.jsonl").read_text(encoding="utf-8").splitlines():
    if l.strip():
        r = json.loads(l)
        last[(r["provider"], int(r["question"]))] = r


def pm(counter):
    return " · ".join(f"{SHORT[p]} {counter.get(p, 0)}" for p in ORDER)


verdict, items_of = {}, {}
for k, r in last.items():
    v, its, _ = rh.effective(r, rh.normalize_items(r), {})
    verdict[k], items_of[k] = v, its
L = [f"# 10-26 전체 다시 판정(v4-f1) — 보류 · FAIL · PASS 내역", "",
     f"실행 `{RUN}` · 판정 `{OUT}` · 질문 78개 × 네 모델. 숫자는 모두 **모델별 질문 수 / 78**. 사람이 보류를 정하기 전 잠정.", "",
     "| | " + " | ".join(SHORT[p] for p in ORDER) + " |", "|---|" + "---|" * len(ORDER)]
for v, lab in (("PASS", "통과"), ("FAIL", "FAIL"), ("HOLD", "보류")):
    L.append(f"| {lab} | " + " | ".join(str(sum(1 for (p, q), x in verdict.items() if p == pp and x == v)) for pp in ORDER) + " |")

# 1. 보류
L += ["", "## 1. 보류 — 사람이 정할 질문", ""]
kinds = defaultdict(Counter)
rows = []
for (p, q), r in sorted(last.items(), key=lambda kv: (kv[0][1], ORDER.index(kv[0][0]))):
    if verdict[(p, q)] != "HOLD":
        continue
    if r.get("status") != "judged":
        kinds["빈 출력 요구(질문 전체)"][p] += 1
        continue
    held = [it for it in items_of[(p, q)] if it["decision"] == "UNVERIFIED"]
    for k in {it["kind"] for it in held}:
        kinds[k][p] += 1
    for it in held:
        rows.append(f"| Q{q} | {SHORT[p]} | {it['turn']} | {it['item']} | {it['kind']} | {(it['reason'] or it['how'])[:110].replace('|', '/')} |")
L += ["| 보류 종류 | " + " | ".join(SHORT[p] for p in ORDER) + " |", "|---|" + "---|" * len(ORDER)]
for k, c in sorted(kinds.items(), key=lambda kv: -sum(kv[1].values())):
    L.append(f"| {k} | " + " | ".join(str(c.get(p, 0)) for p in ORDER) + " |")
L += ["", "(한 질문에 보류 종류가 둘 이상이면 각각 셈)", "", "<details><summary>보류 칸 목록</summary>", "",
      "| 질문 | 모델 | 턴 | 항목 | 종류 | 사유 |", "|---|---|---|---|---|---|"] + rows + ["", "</details>"]

# 2. FAIL
L += ["", "## 2. FAIL — 어긴 기준", ""]
fail_q = defaultdict(Counter)
for (p, q), its in items_of.items():
    if verdict[(p, q)] == "FAIL" or any(it["decision"] == "FAIL" for it in its):
        for i in {it["item"] for it in its if it["decision"] == "FAIL"} | set(last[(p, q)].get("code_fails") or []):
            fail_q[i.split(":")[0]][p] += 1
L += ["기준을 어긴 질문 수(그 질문이 보류여도 FAIL 칸이 있으면 셈).", "", "| 기준 | " + " | ".join(SHORT[p] for p in ORDER) + " |", "|---|" + "---|" * len(ORDER)]
for i, c in sorted(fail_q.items(), key=lambda kv: -sum(kv[1].values()))[:20]:
    L.append(f"| {i} | " + " | ".join(str(c.get(p, 0)) for p in ORDER) + " |")

# 3. PASS 점검 — 1차가 FAIL 인데 Codex 가 PASS 로 뒤집은 칸 · '이 항목에서 판정하지 않음' 류 PASS
flip, skipped_pass = Counter(), defaultdict(Counter)
NOT_MINE = re.compile(r"(이 항목에서|여기서는?)\s*(판정|고려|보)지?\s*않|판정 대상이 아닙|코드가 (이미 )?판정")
for (p, q), r in last.items():
    for tn, t in (r.get("turn_results") or {}).items():
        for i, it in ((t or {}).get("items") or {}).items():
            if not isinstance(it, dict) or it.get("decision") != "PASS":
                continue
            if it.get("jev") == "FAIL" and it.get("codex") == "PASS":
                flip[p] += 1
            if NOT_MINE.search(str(it.get("reason") or "")):
                skipped_pass[i][p] += 1
L += ["", "## 3. PASS 점검", "",
      f"- 1차 심판 FAIL → Codex PASS 로 뒤집힌 칸(질문 × 턴 × 항목): {pm(flip)}",
      "- 사유가 '이 항목에서 판정하지 않음 · 코드가 판정'인 PASS — 판정을 남에게 넘기고 PASS 로 둔 칸:", "",
      "| 항목 | " + " | ".join(SHORT[p] for p in ORDER) + " |", "|---|" + "---|" * len(ORDER)]
for i, c in sorted(skipped_pass.items(), key=lambda kv: -sum(kv[1].values()))[:10]:
    L.append(f"| {i} | " + " | ".join(str(c.get(p, 0)) for p in ORDER) + " |")
flip_by, sure = Counter(), []
for (p, q), r in last.items():
    for tn, t in (r.get("turn_results") or {}).items():
        for i, it in ((t or {}).get("items") or {}).items():
            if isinstance(it, dict) and it.get("decision") == "PASS" and it.get("jev") == "FAIL" and it.get("codex") == "PASS":
                flip_by[i] += 1
                pf = (it.get("decisions_prob") or {}).get("FAIL")
                if pf is not None and pf >= 0.8:
                    sure.append(f"| Q{q} | {SHORT[p]} | {tn} | {i} | {pf:.2f} | {str(it.get('reason') or '')[:120].replace('|', '/')} |")
L += ["", "- 뒤집힌 칸이 많은 항목: " + " · ".join(f"{i} {n}" for i, n in flip_by.most_common(8))
      + " — 대부분 1차가 확신 없이 낸 FAIL(설계대로 Codex 가 정함).",
      f"- 1차가 FAIL 확률 0.8 이상으로 확신했는데 Codex 가 PASS 로 뒤집은 칸 {len(sure)}개 — 사람이 볼 재료:", "",
      "<details><summary>목록</summary>", "", "| 질문 | 모델 | 턴 | 항목 | 1차 FAIL 확률 | Codex PASS 사유 |", "|---|---|---|---|---|---|"] + sure + ["", "</details>"]
(Path(__file__).parent / "정리.md").write_text("\n".join(L) + "\n", encoding="utf-8")
print("ok", len(rows), "보류 칸")
