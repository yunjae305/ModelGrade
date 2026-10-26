"""쉬운 보고서 — 일반인이 바로 읽는 한 장 (사용자 10-27: '일반인이 봐도 이해하기 쉽도록 보고서를 만들어').

결과 보고서(report.html)와 같은 데이터(tools.build_result_summary.load)로 결론 · 순위 · 질문 종류별 통과율 · 자주 어긴 것 · 보류 ·
평가 방법 · 걸린 시간을 쉬운 말로 보인다. 숫자는 문항 단위 / 177(멀티턴은 턴마다 1문항)이고, 빈 출력 요구 질문은 싱글턴에 넣되 보류로 센다
(기준 v2 1절, 사용자 10-27). 결과 폴더마다 <폴더>/쉬운보고서.html — build_result_summary.build 가 결과 보고서와 함께 쓴다.
"""
from __future__ import annotations

import html
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

FILE = "쉬운보고서.html"
TYPES = (("싱글턴", "한 번 묻고 한 번 답하는 질문", "첨부 파일 · '아무것도 출력하지 마' 같은 질문 포함"),
         ("멀티턴", "같은 대화에서 10번 이어 묻는 질문", "대화 11개 × 10턴 — 턴마다 1문항"),
         ("롱컨텍스트", "아주 긴 문서(약 27만 토큰)를 읽고 답하는 질문", ""))
NAMES = {"aiona": "AIONA", "aiona2": "AIONA 2", "gpt": "ChatGPT", "claude": "Claude", "gemini": "Gemini"}
GROUP_PLAIN = {"J1": "요청을 제대로 들어줌", "J2": "지시를 따름", "J3": "앞뒤가 맞음", "J4": "빠짐없이 답함", "J5": "사실 · 자료가 정확함",
               "J6": "대화 내용을 기억함", "J7": "긴 문서를 제대로 활용함", "J8": "첨부 파일을 제대로 씀"}


def E(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def model_name(D: Dict[str, Any], p: str) -> Tuple[str, str]:
    """('ChatGPT', 'GPT-6') — 실행에 적힌 모델 이름에서 괄호 안 첫 낱말. AIONA 에 모델이 없으면 '자동 추천'."""
    label = str((D["d"].get("models") or {}).get(p) or "")
    m = re.search(r"\((.+?)(?: · |\))", label)
    model = m.group(1) if m else ("자동 추천" if p.startswith("aiona") else "")
    if p.startswith("aiona") and "·" in label and not m:   # 'AIONA · Claude Sonnet 5.5'
        model = label.split("·", 1)[1].strip()
    return NAMES.get(p, p), model


def type_tally(D: Dict[str, Any], p: str) -> Dict[str, Counter]:
    out: Dict[str, Counter] = {t: Counter() for t, _, _ in TYPES}
    for q, t in D["units"]:
        v = D["uverdict"].get((p, q, t))
        if v:
            out.setdefault(D["utype"][(q, t)], Counter())[v] += 1
    return out


def item_question(cid: str) -> str:
    """심판이 그 기준에서 묻는 질문 — 일반인이 '무엇을 봤는지' 알게. CODE 검사는 이름 그대로."""
    try:
        from code_eval.semantic_items import ITEMS
        item = next((i for i in ITEMS if i.id == cid.split(":")[0]), None)
        text = str(getattr(item, "question", "") or "") if item else ""
        return re.sub(r"\s*\([^()]*\bJ\d[^()]*\)", "", text).strip()   # '(… J3-5.a)' 같은 내부 기준 번호 괄호는 뺀다
    except Exception:
        return ""


def pct(a: int, b: int) -> str:
    return f"{round(100 * a / b)}%" if b else "—"


def hm(sec: float) -> str:
    sec = int(round(sec or 0))
    return f"{sec // 3600}시간 {sec % 3600 // 60}분" if sec >= 3600 else f"{sec // 60}분"


CSS = """
:root{--bg:#f7f7f5;--surface:#fff;--ink:#1f1f1d;--ink2:#4a4a46;--muted:#77756f;--line:#e4e2dc;--good:#0ca30c;--warn:#fab219;--bad:#d03b3b;--accent:#2f6fde;--chip:#f0efeb}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141413;--surface:#1c1c1a;--ink:#ededea;--ink2:#c9c7c0;--muted:#9a978f;--line:#33322e;--accent:#7aa7f5;--chip:#262623}}
:root[data-theme="dark"]{--bg:#141413;--surface:#1c1c1a;--ink:#ededea;--ink2:#c9c7c0;--muted:#9a978f;--line:#33322e;--accent:#7aa7f5;--chip:#262623}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.65 "Pretendard","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif}
main{max-width:920px;margin:0 auto;padding:28px 16px 56px}h1{font-size:1.7rem;margin:0 0 4px;line-height:1.3}h2{font-size:1.18rem;margin:0 0 12px}
.sub{color:var(--muted);margin:0 0 22px}.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:20px 20px;margin:0 0 16px}
.hero{border-left:6px solid var(--accent)}.hero .big{font-size:1.5rem;font-weight:700;line-height:1.4;margin:0 0 6px}.hero p{margin:4px 0;color:var(--ink2)}
.rank{display:grid;grid-template-columns:34px minmax(120px,170px) 1fr 64px;gap:10px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}
.rank:first-of-type{border-top:0}.no{font-weight:700;font-size:1.15rem;color:var(--muted);text-align:center}.who b{display:block}.who small{color:var(--muted)}
.bar{display:flex;gap:2px;height:22px}.bar span{display:block;height:100%}.bar span:first-child{border-radius:4px 0 0 4px}.bar span:last-child{border-radius:0 4px 4px 0}
.s-pass{background:var(--good)}.s-hold{background:repeating-linear-gradient(45deg,var(--warn) 0 6px,#e9a40f 6px 9px)}.s-fail{background:var(--bad)}
.nums{font-size:.84rem;color:var(--ink2);margin-top:4px}.rate{font-weight:700;text-align:right;font-size:1.1rem}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:.88rem;color:var(--ink2);margin:6px 0 2px}.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-1px}
table{width:100%;border-collapse:collapse;font-size:.93rem}th,td{padding:9px 8px;border-top:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--muted);font-weight:600;font-size:.85rem}
td.n{text-align:right;white-space:nowrap}.ty small{display:block;color:var(--muted);font-size:.82rem}.chip{display:inline-block;background:var(--chip);border-radius:6px;padding:0 6px;font-size:.8rem;color:var(--ink2)}
ol.steps{margin:0;padding-left:20px}ol.steps li{margin:6px 0}dl{margin:0}dt{font-weight:700;margin-top:10px}dd{margin:2px 0 0;color:var(--ink2)}
.note{font-size:.86rem;color:var(--muted)}a{color:var(--accent)}.scroll{overflow-x:auto}
@media (max-width:640px){.rank{grid-template-columns:28px 1fr 56px}.rank .rate{grid-row:1;grid-column:3}.rank .barcell{grid-column:1 / -1;grid-row:2}h1{font-size:1.4rem}.hero .big{font-size:1.25rem}}
"""


def build_html(D: Dict[str, Any], links: Dict[str, str]) -> str:
    from tools import build_result_summary as brs
    order, n = D["order"], D["n"]
    tally = D["tally"]
    top, second = order[0], (order[1] if len(order) > 1 else None)
    nm = {p: model_name(D, p) for p in order}

    def label(p: str) -> str:
        a, m = nm[p]
        return f"{a}{f' ({m})' if m else ''}"

    # 결론
    gap = tally[top]["PASS"] - tally[second]["PASS"] if second else 0
    can_flip = second and tally[second]["PASS"] + tally[second]["HOLD"] > tally[top]["PASS"]
    hero = (f'<section class="card hero"><p class="big">{E(label(top))}가 가장 많이 통과했습니다 — {n}문항 중 {tally[top]["PASS"]}문항({pct(tally[top]["PASS"], n)})</p>'
            + (f'<p>2위 {E(label(second))}와 {gap}문항 차이입니다. '
               + ('아직 사람이 확인하지 않은 보류 문항이 있어, 보류를 정하면 순위가 바뀔 수 있습니다.' if can_flip else '보류를 어떻게 정해도 1위는 바뀌지 않습니다.') + '</p>'
               if second else '')
            + f'<p>같은 질문 {D["nq"]}개를 {len(order)}개 AI 에 똑같이 묻고, 답을 정해 둔 기준으로 채점했습니다. '
            f'10번 이어 묻는 대화 질문은 턴마다 따로 채점해 질문 {D["nq"]}개가 {n}문항이 됩니다.</p></section>')

    # 순위
    qt = D.get("qtally") or {}
    rows = []
    for i, p in enumerate(order, 1):
        t = tally[p]
        seg = "".join(f'<span class="s-{k}" style="flex:{t[v]}" title="{lab} {t[v]}문항"></span>'
                      for k, v, lab in (("pass", "PASS", "통과"), ("hold", "HOLD", "보류"), ("fail", "FAIL", "FAIL")) if t[v])
        a, m = nm[p]
        rows.append(f'<div class="rank"><div class="no">{i}</div><div class="who"><b>{E(a)}</b><small>{E(m)}</small></div>'
                    f'<div class="barcell"><div class="bar" role="img" aria-label="{E(a)} 통과 {t["PASS"]} · 보류 {t["HOLD"]} · FAIL {t["FAIL"]}">{seg}</div>'
                    f'<div class="nums">통과 <b>{t["PASS"]}</b> · 보류 {t["HOLD"]} · FAIL {t["FAIL"]} <span class="note">(/ {n}문항)</span>'
                    + (f'<br><span class="note">질문으로 세면 {qt[p]["PASS"]} / {D["nq"]}개 통과(대화는 10턴 모두 통과해야 1개)</span>' if qt.get(p) else "")
                    + '</div></div>'
                    f'<div class="rate">{pct(t["PASS"], n)}</div></div>')
    legend = ('<div class="legend"><span><i class="s-pass"></i>통과 — 그 문항에서 지켜야 할 것을 모두 지킴</span>'
              '<span><i class="s-hold"></i>보류 — 사람이 확인해야 정해짐</span><span><i class="s-fail"></i>FAIL — 하나라도 어김</span></div>')
    ranking = (f'<section class="card"><h2>순위 — 통과한 문항 수</h2>{legend}{"".join(rows)}'
               f'<p class="note">통과율 = 통과 ÷ 전체 {n}문항. 보류는 아직 통과도 FAIL 도 아닙니다.</p></section>')

    # 질문 종류별
    tt = {p: type_tally(D, p) for p in order}
    tot = Counter(D["utype"].values())
    head = "".join(f"<th>{E(nm[p][0])}</th>" for p in order)
    trs = []
    for ty, what, more in TYPES:
        if not tot.get(ty):
            continue
        cells = "".join(f'<td class="n">{pct(tt[p][ty]["PASS"], tot[ty])}<br><span class="note">{tt[p][ty]["PASS"]} / {tot[ty]}</span></td>' for p in order)
        trs.append(f'<tr><td class="ty"><b>{E(ty)}</b> <span class="chip">{tot[ty]}문항</span><small>{E(what)}{" — " + E(more) if more else ""}</small></td>{cells}</tr>')
    types = (f'<section class="card"><h2>질문 종류별 통과율</h2><div class="scroll"><table><tr><th>종류</th>{head}</tr>{"".join(trs)}</table></div>'
             '<p class="note">멀티턴은 대화가 길어질수록 앞에서 정한 조건을 잊거나 바뀐 조건을 놓치기 쉬워 통과율이 낮게 나옵니다.</p></section>')

    # 자주 어긴 것
    cf = brs.crit_fail(D)
    tops = sorted(cf.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))[:5]
    crs = []
    for c, cnt in tops:
        base = c.split(":")[0]
        grp = GROUP_PLAIN.get(base.split("-")[0], (D["names"].get(base) or {}).get("group") or "")
        qtext = item_question(base)
        crs.append(f'<tr><td><b>{E(brs.crit_name(D["names"], c))}</b> <span class="chip">{E(grp)}</span>'
                   + (f'<br><span class="note">심판이 본 것: {E(qtext)}</span>' if qtext else "") + "</td>"
                   + "".join(f'<td class="n">{cnt.get(p, 0)}</td>' for p in order) + "</tr>")
    misses = (f'<section class="card"><h2>가장 많이 어긴 것 — 상위 5개</h2><div class="scroll"><table><tr><th>기준</th>{head}</tr>{"".join(crs)}</table></div>'
              '<p class="note">숫자는 그 기준을 어긴 질문 수입니다(한 질문에서 여러 번 어겨도 1). 작을수록 좋습니다.</p></section>') if crs else ""

    # 보류
    empty_q = sorted(q for q, t in D["qtypes"].items() if t == getattr(brs, "EMPTY_TYPE", "명시적 빈 출력"))
    hold_rows = "".join(f'<tr><td>{E(label(p))}</td><td class="n">{tally[p]["HOLD"]}</td><td class="n">{len(empty_q)}</td><td class="n">{max(0, tally[p]["HOLD"] - len(empty_q))}</td></tr>'
                        for p in order)
    holds = (f'<section class="card"><h2>보류 — 사람이 정할 문항</h2>'
             f'<div class="scroll"><table><tr><th>모델</th><th>보류</th><th>그중 빈 출력 요구</th><th>그 밖</th></tr>{hold_rows}</table></div>'
             f'<p class="note">빈 출력 요구 = "아무것도 출력하지 마" 같은 질문 {len(empty_q)}개입니다. 답이 비어 있어야 정상이라 화면에 아무것도 없을 때 '
             '정상인지 오류인지 프로그램이 가릴 수 없어, 사람이 확인하기 전까지 보류로 둡니다. 그 밖의 보류는 심판이 확신하지 못한 문항입니다.</p></section>')

    # 평가 방법
    how = ('<section class="card"><h2>어떻게 평가했나</h2><ol class="steps">'
           f'<li><b>같은 질문을 똑같이</b> — 질문 {D["nq"]}개(대화형은 턴마다 세어 {n}문항)를 {len(order)}개 AI 의 웹 화면에 그대로 넣고, 답을 복사 버튼으로 받았습니다.</li>'
           '<li><b>프로그램이 잴 수 있는 것은 프로그램이</b> — 글자 수 · 표 · 코드블록 · 계산값처럼 정확히 셀 수 있는 것을 확인합니다.</li>'
           '<li><b>뜻을 봐야 하는 것은 AI 심판이</b> — 요청을 들어줬는지 · 사실이 맞는지 · 앞뒤가 맞는지를 까다로운 눈 · 너그러운 눈 · 하나하나 짚어 가는 눈 '
           '세 가지로 한 번씩 보고 다수결로 정합니다(1차 심판). 어겼다고 보거나 애매한 것은 다른 AI 심판이 답을 인용해 가며 한 번 더 확인합니다(2차 심판).</li>'
           '<li><b>하나라도 어기면 FAIL</b> — 문항마다 질문이 요구한 기준(72개 중 해당하는 것)을 모두 지켜야 통과입니다. 확신할 수 없으면 억지로 정하지 않고 보류로 사람에게 넘깁니다.</li>'
           '</ol></section>')

    # 시간 · 심판 호출
    jw = brs.judge_work(D)
    collect = {p: sum(brs.turn_time(t) for (pp, _q, _t), t in D["turns"].items() if pp == p) for p in order}
    trows = "".join(f'<tr><td>{E(label(p))}</td><td class="n">{hm(collect[p])}</td><td class="n">{hm(jw.get(p, Counter())["seconds"])}</td></tr>' for p in order)
    jt = brs.judge_tokens(D)
    role = {"Jev": "1차 심판 Jev", "Decisions": "1차 심판 Decisions", "Codex": "2차 심판 Codex"}
    judges = " · ".join(f'{E(role.get(k, k))} {v["calls"]:,}번' for k, v in jt.items()) if jt else ""
    times = (f'<section class="card"><h2>걸린 시간</h2><div class="scroll"><table><tr><th>모델</th><th>답 받기</th><th>평가</th></tr>{trows}</table></div>'
             f'<p class="note">네 모델은 동시에 받고 동시에 평가합니다. 답 받기 = 질문을 보내고 답이 끝나기까지 기다린 시간의 합, 평가 = 그 모델의 답을 채점한 시간의 합.'
             + (f' 심판 호출: {judges}.' if judges else "") + '</p></section>')

    terms = ('<section class="card"><h2>용어</h2><dl>'
             '<dt>문항</dt><dd>채점 단위. 한 번 묻는 질문은 1문항, 10번 이어 묻는 대화는 턴마다 1문항(10문항)으로 셉니다.</dd>'
             '<dt>기준 72개</dt><dd>답을 볼 때 쓰는 점검 목록 — 답이 성립하는지 2개(빈 답 · 거절 문구 등), 프로그램이 재는 것 26개, AI 심판이 보는 것 44개. 문항마다 해당하는 것만 봅니다.</dd>'
             '<dt>멀티턴 · 롱컨텍스트</dt><dd>멀티턴은 같은 대화에서 이어 묻는 것, 롱컨텍스트는 아주 긴 문서를 주고 묻는 것입니다.</dd>'
             '<dt>보류</dt><dd>프로그램과 심판이 확신하지 못해 사람이 정할 문항. 정하면 통과나 FAIL 로 바뀝니다.</dd></dl></section>')

    more = " · ".join(f'<a href="{E(href)}">{E(lab)}</a>' for lab, href in links.items())
    when = str(D["d"].get("created_at") or "")[:10]
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>채팅 품질 평가 결과</title><style>{CSS}</style></head><body><main>'
            f'<h1>AI 채팅 품질 평가 결과</h1><p class="sub">같은 질문을 {len(order)}개 AI 에 묻고 답을 채점한 결과입니다'
            f'{" · 실행 시작 " + E(when) if when else ""} · 실행 {E(D["run_id"])}</p>'
            + hero + ranking + types + misses + holds + how + times + terms
            + (f'<p class="note">더 자세히: {more}</p>' if more else "") + "</main></body></html>")


def write(D: Dict[str, Any], report_dir: Path) -> Path:
    """<결과 폴더>/쉬운보고서.html — 같은 폴더의 결과 보고서 · 판정 보고서 · 질문별 시간 표로 이어 준다."""
    report_dir = Path(report_dir)
    links = {lab: f for lab, f in (("자세한 결과 보고서", "report.html"), ("판정 기록(보류 정하기)", "판정보고서.html"),
                                   ("질문별 시간 · 호출 표", "질문별_시간_호출.csv")) if (report_dir / f).exists() or f == "report.html"}
    out = report_dir / FILE
    out.write_text(build_html(D, links), encoding="utf-8")
    return out
