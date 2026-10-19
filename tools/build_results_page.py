"""326 평가 결과 한 장 HTML — PASS:FAIL 비율(확정 질문 중) + 보류 비율(326 중), 정밀 326 / 최소 78, 어디서 틀렸나, 보류 사유.
발표 자료와 같은 결과 파일을 읽는다(보류 정리 반영본 `k별_갈림_<run>_보류반영.json` 이 있으면 우선). AI 호출 없음.

    python -X utf8 tools/build_results_page.py            # → 참고자료/결과/326결과_2026-10-19.html
"""
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "참고자료" / "결과" / "326결과_2026-10-19.html"
E = html.escape
RUN = "8c61f117e661"
MODEL_NAME = {"gpt": "GPT-6 Luna", "aiona": "AIONA", "claude": "Claude Sonnet 5.5", "gemini": "Gemini 3.6 Flash"}
ORDER = ("gpt", "aiona", "claude", "gemini")

_gold = ROOT / "참고자료/최소평가" / f"k별_갈림_{RUN}_보류반영.json"
_base = ROOT / "참고자료/최소평가" / f"k별_갈림_{RUN}.json"
K = json.load(open(_gold if _gold.exists() else _base, encoding="utf-8"))
CF = json.load(open(ROOT / "참고자료/최소평가/criterion_fail_rates_8c61f117e661.json", encoding="utf-8"))   # 질문 단위 (tools/criterion_fail_by_question.py)
FBG = json.load(open(ROOT / "참고자료/최소평가/fail_by_group_min78_vs_full.json", encoding="utf-8"))


def pf(p, f, h):
    d = p + f
    return (p / d * 100 if d else 0, f / d * 100 if d else 0, h / (p + f + h) * 100 if p + f + h else 0)


def table(counts, n):
    rows = []
    for m in ORDER:
        p, f, h = counts[m]["PASS"], counts[m]["FAIL"], counts[m]["HOLD"]
        a, b, c = pf(p, f, h)
        rows.append(f'<tr><th>{E(MODEL_NAME[m])}</th><td><div class="bar"><i class="p" style="width:{a:.1f}%"></i><i class="f" style="width:{b:.1f}%"></i></div></td>'
                    f'<td class="num"><b>{a:.0f}</b> : {b:.0f}</td><td class="num muted">{p} · {f}</td><td class="num">{c:.0f}% <span class="muted">({h})</span></td></tr>')
    return (f'<table><thead><tr><th>모델</th><th>PASS : FAIL (확정 질문 중)</th><th>비율</th><th>PASS · FAIL 수</th><th>보류 (/{n})</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


GROUP_LABEL = {"CODE 1": "CODE 1 출력 무결성", "CODE 2": "CODE 2 형식·지시", "CODE 3": "CODE 3 계산·자료", "J1": "J1 의도 충족", "J2": "J2 맥락 적합", "J3": "J3 논리 일관",
               "J4": "J4 완결성", "J5": "J5 DATA 신뢰도", "J6": "J6 멀티턴 맥락", "J7": "J7 롱컨텍스트", "J8": "J8 첨부 해석"}
fbg_rows = "".join(
    f'<tr><th>{E(GROUP_LABEL[g])}</th>' + "".join(
        (f'<td class="num">{FBG[g][m][0]} <span class="muted">/ {FBG[g][m][1]} (최소 78)</span></td>' if m in FBG.get(g, {}) else '<td class="num muted">—</td>') for m in ORDER) + '</tr>'
    for g in GROUP_LABEL)
cf_rows = "".join(f'<tr><th><code>{E(r["id"])}</code> {E(r["name"])}</th><td class="num">{r["applied"]}</td>'
                  f'<td>{" · ".join(f"{MODEL_NAME[k].split()[0]} {r["by_model"][k]}/{v}" for k, v in r["applied_by_model"].items() if v)}</td>'
                  f'<td class="num"><b>{r["rate"] * 100:.0f}%</b></td></tr>' for r in [x for x in CF if x["applied"] >= 10 and x["fail"] > 0][:12])
zero = ", ".join(f'<code>{E(r["id"])}</code>' for r in CF if r["fail"] == 0)
gold_note = " · <b>보류 정리 반영</b>" if K["gold"] else " · 보류 정리 전"

page = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>326 평가 결과 — 2026-10-19</title>
<style>
:root {{ --bg:#f2f4f7; --surface:#fff; --ink:#111b26; --muted:#566272; --line:#d5dce5; --accent:#1e5d96; --ok:#1d7748; --risk:#b3291f; --warn:#965a00; --warn-soft:#faeed4; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0e141b; --surface:#16202a; --ink:#e5ebf1; --muted:#98a4b3; --line:#2a3644; --accent:#7fb5e3; --ok:#6fcf97; --risk:#f08a80; --warn:#e2b25a; --warn-soft:#33270f; }} }}
* {{ box-sizing: border-box; }} body {{ margin:0; background:var(--bg); color:var(--ink); font-family:"IBM Plex Sans KR","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif; font-size:16px; line-height:1.6; }}
.wrap {{ max-width: 1000px; margin: 0 auto; padding: 32px 16px 64px; display:flex; flex-direction:column; gap:22px; }}
h1 {{ font-size:1.7rem; margin:0; }} h2 {{ font-size:1.15rem; margin:0 0 8px; }} p {{ margin:0; color:var(--muted); }}
.card {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:18px 20px; }}
table {{ width:100%; border-collapse:collapse; font-size:0.95rem; }} th, td {{ padding:10px 12px; border-top:1px solid var(--line); text-align:left; vertical-align:middle; }}
thead th {{ font-size:0.8rem; color:var(--muted); border-top:0; }} tbody th {{ font-weight:600; white-space:nowrap; }}
td.num {{ font-family: ui-monospace, Menlo, monospace; font-variant-numeric: tabular-nums; white-space:nowrap; }} .muted {{ color:var(--muted); font-weight:400; }}
.bar {{ display:flex; height:18px; border-radius:4px; overflow:hidden; background:var(--bg); min-width:160px; }} .bar i {{ display:block; height:100%; }} .bar .p {{ background:var(--ok); }} .bar .f {{ background:var(--risk); opacity:.75; }}
code {{ font-family: ui-monospace, Menlo, monospace; font-size:0.85rem; color:var(--accent); }}
.note {{ background:var(--warn-soft); border-radius:8px; padding:10px 14px; font-size:0.9rem; }}
</style></head><body><main class="wrap">
<div><h1>326 평가 결과 — AIONA · GPT · Claude · Gemini</h1>
<p>실행 {RUN}{gold_note} · 질문 단위(대화 1개 = 1개) · 기준 72개(CODE 28 + 의미 44, Jev + GPT-6 Luna 병행) · 결과는 <b>확정된 질문의 PASS : FAIL 비율</b>과 <b>보류 비율</b>로 봅니다. 보류 = 확인 못 해 사람이 PASS/FAIL 을 정할 질문.</p></div>
<div class="card"><h2>정밀 평가 — 326개</h2>{table(K["models"], 326)}</div>
<div class="card"><h2>최소 평가 — 78개 (정밀에서 골라냄, 기준마다 10번↑)</h2>{table(K["min78"], 78)}<p style="margin-top:8px">같은 판정을 78개만 세었을 때. 어려운 질문 위주라 FAIL 쪽으로 기울지만 순위는 정밀과 같음.</p></div>
<div class="card"><h2>기준 묶음별 FAIL 질문 수 — 정밀 / 최소 78</h2><table><thead><tr><th>기준 묶음</th>{"".join(f"<th>{E(MODEL_NAME[m])}</th>" for m in ORDER)}</tr></thead><tbody>{fbg_rows}</tbody></table></div>
<div class="card"><h2>많이 틀린 기준 12개 (질문 단위 — 대화는 어느 턴이든 FAIL 이면 그 질문 FAIL)</h2><table><thead><tr><th>기준</th><th>판정된 질문 (326 중)</th><th>모델별 FAIL 질문 수 / 판정된 질문 수</th><th>FAIL 률 (네 모델 평균)</th></tr></thead><tbody>{cf_rows}</tbody></table>
<p style="margin-top:8px">한 번도 FAIL 이 안 난 기준: {zero} — 대부분 깨진 출력을 잡는 안전망 검사.</p></div>
<div class="note">보류 목록(참고자료/보류목록/{RUN}-held.csv)의 gold 를 채운 뒤 <code>tools/k_split_from_run.py {RUN} --gold …</code> 와 이 스크립트를 다시 돌리면 숫자가 갱신됩니다. 전체 발표 자료: 참고자료/발표/ModelGrade_진행보고_2026-10-19.html</div>
</main></body></html>
"""
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(page, encoding="utf-8")
print("저장:", OUT, f"{len(page):,}자")
