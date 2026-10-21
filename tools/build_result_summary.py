"""결과 요약 발표 페이지 (result-summary-v1) — 78개 실행 하나를 발표 6장면 + 부록으로 (사용자 10-21: report.html 은 정보가 너무 많아
발표하기 어려움 → 10.20 미팅 기준으로 흐름도 · 결과 · 유형별 · 많이 어긴 기준 · 보류 · 걸린 시간, 원문은 부록).
AI 호출 없음. 내보낸 결과(참고자료/결과/<실행>_결과_판정결과.json)와 질문 세트만 읽고, 판정 셈은 tools/report_html 과 같은 함수를 쓴다
(숫자가 그 실행의 report.html 과 같아야 한다 — 끝에서 맞춰 본다). 디자인은 10-19 발표 덱 CSS 를 그대로 읽어 쓴다(10-20 보고와 같은 모양).

    python -X utf8 tools/build_result_summary.py [--run a1642702fccc]
    → 참고자료/발표/ModelGrade_결과요약_<실행>.html
"""
from __future__ import annotations

import argparse
import html
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import report_html as R  # noqa: E402

VERSION = "result-summary-v4"   # v2 (10-21 오후): 흐름도·장면을 그림 위주로 — 멘토·소장님 발표용
RUN = "a1642702fccc"
RESULTS = ROOT / "참고자료" / "결과"
OUT_DIR = ROOT / "참고자료" / "발표"
DECK = OUT_DIR / "ModelGrade_진행보고_2026-10-19.html"
TIME_1020 = RESULTS / "시간_요약_ac6507c00276.json"   # 10-20 보고 때(다른 78개 세트) 수집 시간 — 참고 한 줄
E = html.escape
SHORT = {"aiona": "AIONA", "gpt": "GPT", "claude": "Claude", "gemini": "Gemini"}
# 미팅(10.20)에서 정한 이름 — 싱글턴 · 멀티턴 · 롱컨텍스트
TYPE_NAME = {"단일 질문": "싱글턴", "10턴 멀티턴 대화": "멀티턴", "첨부자료 확인·요약": "첨부 자료",
             "롱컨텍스트": "롱컨텍스트", "명시적 빈 출력": "빈 출력 요구"}
GROUP = {"J1": "요청 충실", "J2": "지시 준수", "J3": "일관성·논리", "J4": "완결성", "J5": "DATA 신뢰도",
         "J6": "대화 맥락 유지", "J7": "긴 문서 활용", "J8": "첨부 활용"}
SUFFIX = {"bullet_count": "불릿 수", "required": "필수", "count": "개수", "block1": "첫 코드블록", "required_literal": "필수 문구",
          "present": "있어야 함", "1": "첫 등식"}
# 같은 답을 다시 판정할 때의 흔들림 — 10-21 오전 A/B(Codex low vs medium, 표본 10질문 × 4모델), 2026-10-20 변경내역
WOBBLE = {"n": 40, "changed": 6}


LOGO_FILES = {"aiona": "aiona-logo.png", "gpt": "gpt-logo.svg", "claude": "claude-logo.svg", "gemini": "gemini-logo.webp"}   # 팀 앱(frontend/assets)과 같은 로고


def logo_style() -> str:
    """모델 로고 CSS — 그림은 48px PNG(svg 는 그대로)로 줄여 data URI 로 한 번만 넣는다. 흰 칸 위에 두어 어두운 화면에서도 보이게."""
    import base64
    import io
    rules = []
    for p, name in LOGO_FILES.items():
        f = ROOT / "frontend" / "assets" / name
        if not f.exists():
            continue
        if f.suffix == ".svg":
            uri = "data:image/svg+xml;base64," + base64.b64encode(f.read_bytes()).decode()
        else:
            from PIL import Image
            im = Image.open(f).convert("RGBA")
            im.thumbnail((48, 48), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "PNG", optimize=True)
            uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        rules.append(f'.lg-{p} {{ background-image: url("{uri}"); }}')
    return ("<style>.lg { display: inline-block; width: 18px; height: 18px; border-radius: 5px; background: #fff center / 74% no-repeat; "
            "border: 1px solid rgba(0, 0, 0, 0.1); flex: none; vertical-align: -4px; box-sizing: border-box; }\n"
            ".mn { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; vertical-align: middle; }\n"
            + "\n".join(rules) + "</style>")


def logo(p: str) -> str:
    return f'<i class="lg lg-{p}"></i>'   # 빈 i 라 읽기 프로그램은 건너뜀 — 이름은 옆 글자


def mname(p: str) -> str:
    """로고 + 모델 이름 (표 · 머리글 · 목록 어디서나)."""
    return f'<span class="mn">{logo(p)}{SHORT.get(p, p)}</span>'


# ------------------------------------------------------------------ 데이터 ------------------------------------------------------------------
def load(run_id: str) -> Dict[str, Any]:
    d = json.loads((RESULTS / f"{run_id}_결과_판정결과.json").read_text(encoding="utf-8"))
    set_dir = R.set_dir_for(d)
    qtypes = R.question_types(set_dir)
    names = R.criteria_table(set_dir)
    qset = {int(q["id"]): q for q in json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))}
    lines: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for r in d["judgment"]["results"]:
        items = R.normalize_items(r)
        for it in items:
            if it["decision"] == "UNVERIFIED":
                it["kind"] = hold_kind(it)
        verdict, items, _ = R.effective(r, items, {})
        lines[(r["provider"], int(r["question"]))] = {"verdict": verdict, "items": items, "line": r}
    turns = {(p, int(t["question"]), int(t["turn"])): t for p, sec in d["providers"].items() for t in sec["turns"]}
    providers = [p for p in R.ORDER if p in d["providers"]]
    tally = {p: Counter(lines[(p, q)]["verdict"] for q in qtypes if (p, q) in lines) for p in providers}
    order = sorted(providers, key=lambda p: (-tally[p]["PASS"], R.ORDER.index(p)))   # 결과 순 — 모든 장면에서 같은 순서
    return {"d": d, "run_id": run_id, "set_dir": set_dir, "qtypes": qtypes, "names": names, "qset": qset, "lines": lines,
            "turns": turns, "order": order, "tally": tally, "n": len(qtypes)}


def hold_kind(it: Dict[str, Any]) -> str:
    """report_html.hold_kind 와 같은 규칙 — 내보낸 JSON 은 항목의 entries 를 빼서 '판정이 없음'은 problems 로만 본다."""
    how, probs = it.get("how") or "", it.get("problems") or []
    if "심판 갈림" in how:
        return "심판 갈림"
    if any("판정 없음" in x for x in probs):
        return "심판 출력 누락"
    if any("원문에 없음" in x or "선택지" in x for x in probs):
        return "인용 불일치"
    if it.get("codex") == "UNVERIFIED" or "보류" in how:
        return "확인 못함"
    return "기타"


def crit_name(names: Dict[str, Dict[str, str]], cid: str, short: bool = True) -> str:
    base, _, suf = cid.partition(":")
    nm = (names.get(base) or {}).get("name") or ""
    if short:
        nm = nm.split(" — ")[0]
    return nm + (f" ({SUFFIX.get(suf, suf)})" if suf else "")


def code_fail_rows(D: Dict[str, Any], p: str, q: int) -> List[Dict[str, Any]]:
    """그 질문에서 FAIL 로 센 CODE 검사(판정 줄의 code_fails)의 턴·사유."""
    want = set(D["lines"][(p, q)]["line"].get("code_fails") or [])
    out = []
    for (pp, qq, tn), t in sorted(D["turns"].items()):
        if pp == p and qq == q:
            for c in t.get("code") or []:
                if c.get("verdict") == "FAIL" and c.get("check_id") in want:
                    out.append({"turn": tn, "check_id": c["check_id"], "reason": c.get("reason") or ""})
    return out


EMPTY_REQ = "명시적 빈 출력"
EMPTY_WHY = "빈 출력 요구 질문 — 채점하지 않고 바로 보류, 사람이 정함 (10-20 결정)"


def hold_why(D: Dict[str, Any], p: str, q: int) -> List[Dict[str, Any]]:
    """보류 사유 (사용자 10-21: 답변 원문만 있고 사유가 없었다) — 판정에서 확인 못 한(UNVERIFIED) 항목마다
    기준 · 턴 · 심판 사유 · 답의 해당 문장 · 두 심판 판단 · Codex 로 보낸 이유."""
    out = []
    for it in D["lines"][(p, q)]["items"]:
        if it["decision"] == "UNVERIFIED":
            how = (it.get("how") or "").replace("Codex 보류", "Codex 확인 못 함").replace("Decisions PASS", "Decisions 통과")
            out.append({"item": it["item"], "turn": it["turn"], "reason": it.get("reason") or "사유 없음",
                        "quote": it.get("answer_quote") or "", "how": how, "flagged": it.get("flagged") or ""})
    return out


def hold_label(why: List[Dict[str, Any]]) -> str:
    return "사실 확인 못 함" if why and all(w["item"].startswith("J5-1") for w in why) else "확인 못 함"


def judge_timing(D: Dict[str, Any]) -> List[Dict[str, Any]]:
    """판정 실행 — 다시 판정(redo)은 뺀다 (사용자 10-21: 처음부터 제대로 복사된 것으로 보고)."""
    return [t for t in D["d"]["judgment"].get("timing") or [] if not t.get("redo")]


def crit_qs(D: Dict[str, Any], p: str, c: str) -> List[int]:
    """그 모델이 그 기준을 어긴 질문 번호 (crit_fail 과 같은 셈)."""
    return sorted(q for (pp, q), L in D["lines"].items() if pp == p
                  and c in ({i["item"] for i in L["items"] if i["decision"] == "FAIL"} | set(L["line"].get("code_fails") or [])))


def card_id(p: str, c: str) -> str:
    """'확인할 것'에서 바로 가는 카드 id (사용자 10-21)."""
    return f"k-{p}-" + re.sub(r"[^a-z0-9]+", "-", c.lower()).strip("-")


def q_chips(qs: List[int]) -> str:
    return '<div class="rs-qchips">' + "".join(f'<a href="#q-{q}">Q{q}</a>' for q in qs) + "</div>"


def crit_fail(D: Dict[str, Any]) -> Dict[str, Counter]:
    """기준 → 모델별 어긴 질문 수 (질문 하나에서 여러 턴이어도 1) — report.html 3장과 같은 셈."""
    out: Dict[str, Counter] = defaultdict(Counter)
    for (p, q), L in D["lines"].items():
        for c in {i["item"] for i in L["items"] if i["decision"] == "FAIL"} | set(L["line"].get("code_fails") or []):
            out[c][p] += 1
    return out


def report_extras(D: Dict[str, Any]) -> Dict[str, Any]:
    """그 실행의 report.html 에만 있는 값 — 수집 벽시계 · 판정 토큰(Codex 는 run.log 에서 모은 값이라 내보낸 JSON 에 없음) · 429 대기 ·
    추론 수준 비교(진행 중인지). 보고서가 없거나 모양이 다르면 빈 값(그 칸은 안 그림)."""
    f = RESULTS / f"판정_{D['run_id']}" / "report.html"
    out: Dict[str, Any] = {"wall": {}, "judge": {}, "waits": None, "medium_running": False}
    if not f.exists():
        return out
    s = f.read_text(encoding="utf-8")
    by_label = {lab: p for p, lab in D["d"]["models"].items()}
    cell = lambda r: [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", c))).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", r, flags=re.S)]
    num = lambda x: int(re.sub(r"[^\d]", "", x) or 0)
    for r in re.findall(r"<tr>(.*?)</tr>", s, flags=re.S):
        c = cell(r)
        if len(c) == 9 and c[0] in by_label and "초" in c[8]:            # 답변 수집 표 — 마지막 칸이 수집 벽시계
            out["wall"][by_label[c[0]]] = c[8]
        elif len(c) == 8 and c[0] in by_label and c[3].endswith("초") and re.fullmatch(r"[\d,]+", c[4] or ""):   # 판정 표 (보류 표도 8칸이라 '질문당 n초' 칸으로 가림)
            out["judge"][by_label[c[0]]] = {"time": c[2], "per_q": c[3], "dec_in": num(c[4]), "codex_calls": num(c[5]), "codex_in": num(c[6]), "codex_out": num(c[7])}
    m = re.search(r"429 대기 (\d+)회", s)
    out["waits"] = int(m.group(1)) if m else None
    out["process"] = {}   # report.html '수집·판정 과정 기록 — 판정 실행' 표 (10-21): 항목 → (값, 설명)
    for r in re.findall(r"<tr>(.*?)</tr>", s, flags=re.S):
        c = cell(r)
        if len(c) == 3 and c[0] in ("판정 벽시계", "Codex 호출", "다시 묻기", "429 대기", "판정 실행 오류"):
            out["process"][c[0]] = (c[1], c[2])
    i = s.find("추론 수준 비교")
    out["medium_running"] = i >= 0 and "미평가" in s[i:i + 4000]
    return out


def first_reason(D: Dict[str, Any], p: str, cid: str) -> Tuple[int, str]:
    for q in sorted(D["qtypes"]):
        L = D["lines"].get((p, q))
        if not L:
            continue
        for it in L["items"]:
            if it["item"] == cid and it["decision"] == "FAIL":
                return q, it["reason"]
        if cid in (L["line"].get("code_fails") or []):
            for c in code_fail_rows(D, p, q):
                if c["check_id"] == cid:
                    return q, c["reason"]
    return 0, ""


# ------------------------------------------------------------------ 조각 ------------------------------------------------------------------
def table(caption: str, head: List[str], rows: List[List[str]], widths: List[int], cls: str = "") -> str:
    """10-20 보고와 같은 표 — 좁은 화면에서 줄이 세로로 쌓여도 칸 이름(data-label)이 보인다. 칸은 HTML 그대로."""
    cols = "<colgroup>" + "".join(f'<col style="width:{w}%">' for w in widths) + "</colgroup>"
    thead = "<thead><tr>" + "".join(f"<th>{h}</th>" for h in head) + "</tr></thead>"
    body = "".join(f'<tr{"" if not r[0].startswith("@") else " class=rs-dim"}>' + f'<th scope="row">{r[0].lstrip("@")}</th>'
                   + "".join(f'<td data-label="{E(re.sub("<[^>]+>", "", h))}">{c}</td>' for h, c in zip(head[1:], r[1:])) + "</tr>" for r in rows)
    cap = f"<caption>{caption}</caption>" if caption else ""
    return f'<div class="cmpt steps {cls}"><table>{cap}{cols}{thead}<tbody>{body}</tbody></table></div>'


def badge(v: str) -> str:
    return {"PASS": '<span class="rs-b rs-pass">통과</span>', "FAIL": '<span class="rs-b rs-fail">FAIL</span>',
            "HOLD": '<span class="rs-b rs-hold">보류</span>'}.get(v, E(v))


def note(title: str, body: str, warn: bool = False) -> str:
    return f'<div class="note{" warnnote" if warn else ""}"><h3>{title}</h3>{body}</div>'


# ------------------------------------------------------------------ 장면 ------------------------------------------------------------------
# v2 (사용자 10-21 오후: 멘토 최종 발표 → 소장님 보고용으로 "흐름도부터 깔끔하고 가독성 있게") — 한 장면에 메시지 하나 · 그림 하나 · 짧은 글
ROW_ORDER = ["단일 질문", "첨부자료 확인·요약", "롱컨텍스트", "10턴 멀티턴 대화", "명시적 빈 출력"]
CHANGES = [   # 10-20 미팅 때 설명 → 지금 (이름, 그때, 지금, 덧붙임)
    ("의미 평가 심판", "Jev — J1~J4 만", "Decisions API — 44개 항목 전부", "미팅 제안 반영"),
    ("Codex CLI", "나머지 기준을 판정", "의심 항목만 다시 판정 · 갈리면 Codex 를 따름", ""),
    ("첨부 파일", "입력 한도 때문에 잘라서 봄", "두 심판 모두 원본을 쪽 이미지로 봄", ""),
    # 10-21 사용자 결정 — ChatGPT 앱 복사 버튼이 언어 표지를 지움 (2026-10-20 변경내역 '10-21 오후', GPT 완전 통과 23 → 33)
    ("코드블록 언어 표지", "요구와 다르면 FAIL", "비어 있어도 코드블록이면 통과", "ChatGPT 앱 복사가 표지를 지움 — 그것만으로 FAIL 이던 GPT 10문항이 빠짐"),
    ("질문 세트", "326개에서 고른 78개", "78개 확정 세트", ""),
]


def hm(seconds: float) -> str:
    m = int(round((seconds or 0) / 60))
    return f"{m // 60}시간 {m % 60}분" if m >= 60 else f"{m}분"


def tint(r: float, var: str, cap: int = 42) -> str:
    """칸 배경 — 값이 클수록 진하게(한 색을 표면색과 섞음). 글자는 늘 본문 색."""
    return f"background: color-mix(in srgb, var({var}) {max(0.0, min(1.0, r)) * cap:.0f}%, var(--surface));"


def method_of(D: Dict[str, Any], p: str) -> str:
    lab = D["d"]["models"].get(p, "")
    return "데스크톱 앱" if "앱" in lab else "웹브라우저" if "웹" in lab else ""


def turn_ok(D: Dict[str, Any], qs: List[int]) -> Tuple[Dict[str, int], int]:
    """대화 질문들의 턴 가운데 FAIL(의미 항목 · CODE)이 없는 턴 수."""
    ok, tot = {}, 0
    for p in D["order"]:
        o = t = 0
        for q in qs:
            L = D["lines"][(p, q)]
            nturn = len(D["qset"][q]["turns"])
            bad = {i["turn"] for i in L["items"] if i["decision"] == "FAIL"} | {c["turn"] for c in code_fail_rows(D, p, q)}
            o += nturn - len(bad & set(range(1, nturn + 1)))
            t += nturn
        ok[p], tot = o, t
    return ok, tot


def scene_flow(D: Dict[str, Any]) -> str:
    types = Counter(D["qtypes"].values())
    chip_name = {"첨부자료 확인·요약": "첨부"}
    chips = "".join(f'<span class="rs-chip">{E(chip_name.get(t, TYPE_NAME.get(t, t)))} <b>{types[t]}</b></span>' for t in R.TYPE_ORDER if types[t])
    chips += '<span class="rs-chip-note">롱컨텍스트 = 27만 토큰 이상 문서</span>'
    app = " · ".join(mname(p) for p in D["order"] if method_of(D, p) == "데스크톱 앱")
    web = " · ".join(mname(p) for p in D["order"] if method_of(D, p) == "웹브라우저")
    flow = (
        '<div class="rs-flow">'
        f'<div class="rs-st"><span class="rs-sn">1</span><h3>질문</h3><div class="rs-sbig">{D["n"]}<small>개</small></div><div class="rs-chips">{chips}</div></div>'
        f'<div class="rs-st"><span class="rs-sn">2</span><h3>답변 수집</h3><div class="rs-sbig">4<small>모델</small></div>'
        f'<ul class="rs-sl"><li><b>데스크톱 앱</b>{app}</li><li><b>웹브라우저</b>{web}</li><li class="muted">복사 버튼으로 받아 JSON 으로 저장</li></ul></div>'
        '<div class="rs-st"><span class="rs-sn">3</span><h3>채점 — 기준 72개</h3>'
        '<div class="rs-box"><b>CODE 28</b><span>형식 · 개수 · 글자 수 · 계산</span><em>프로그램이 검사</em></div>'
        '<div class="rs-box"><b>의미 44</b><span>Decisions API 가 전부 판정 → 의심 항목만 Codex 가 다시 판정</span><em>AI 심판 둘 · 갈리면 Codex · 어느 모델 답인지 모름(블라인드)</em></div></div>'
        '<div class="rs-st"><span class="rs-sn">4</span><h3>질문 판정</h3><ul class="rs-rules">'
        '<li><i class="rs-dot rs-fail"></i><b>FAIL</b><span>기준을 하나라도 어김</span></li>'
        '<li><i class="rs-dot rs-hold"></i><b>보류</b><span>FAIL 은 없고 확인 못 한 것이 남음</span></li>'
        '<li><i class="rs-dot rs-pass"></i><b>완전 통과</b><span>그 밖</span></li></ul></div>'
        f'<div class="rs-st"><span class="rs-sn">5</span><h3>결과</h3><div class="rs-sbig rs-sbig-t">완전 통과<small>/ {D["n"]}</small></div>'
        '<ul class="rs-sl"><li>모델마다 질문 수로 비교</li><li>보류는 사람이 정함</li></ul></div>'
        '</div>'
        f'<div class="rs-bypass"><i class="rs-dot rs-hold"></i><span><b>빈 출력 요구 {types.get("명시적 빈 출력", 0)}문항</b>은 채점하지 않고 바로 보류 — 빈 답 · 복사 실패 · 짧은 글 모두 (10-20 결정)</span></div>')
    chg = ('<div class="rs-chg"><div class="rs-chg-h"><span>단계</span><span>10-20 미팅 때</span><span></span><span>지금</span></div>'
           + "".join(f'<div class="rs-chg-r"><b>{E(a)}</b><span class="rs-old">{E(b)}</span><span class="rs-arr" aria-hidden="true">→</span>'
                     f'<span class="rs-new">{E(c)}{f"<small>{E(d)}</small>" if d else ""}</span></div>' for a, b, c, d in CHANGES) + "</div>")
    return flow + '<h3 class="rs-sh">어제 설명에서 바뀐 점</h3>' + chg


def scene_criteria(D: Dict[str, Any]) -> str:
    order = D["order"]
    cf = crit_fail(D)
    top = sorted(cf, key=lambda c: (-sum(cf[c].values()), c))[:10]
    mx = max([cf[c][p] for c in top for p in order] or [1]) or 1
    cols = f"minmax(150px, 1.6fr) repeat({len(order)}, minmax(0, 1fr)) minmax(0, 0.8fr)"
    cells = ['<div class="rs-hh"></div>'] + [f'<div class="rs-hh">{mname(p)}</div>' for p in order] + ['<div class="rs-hh muted">합</div>']
    for c in top:
        worst = max(cf[c][p] for p in order)
        cells.append(f'<div class="rs-hl"><b>{E(crit_name(D["names"], c))}</b><span class="rs-id">{E(c)}</span></div>')
        cells.extend(f'<div class="rs-hc rs-hc-s{" rs-worst-c" if cf[c][p] == worst and worst else ""}" style="{tint(cf[c][p] / mx, "--risk")}"><b>{cf[c][p]}</b></div>' for p in order)
        cells.append(f'<div class="rs-hc rs-hc-s rs-sum"><b>{sum(cf[c].values())}</b></div>')
    grid = f'<div class="rs-heat" style="grid-template-columns: {cols}">' + "".join(cells) + "</div>"
    # 한 모델에만 몰린 기준 — 모델 특성인지 수집(복사) 탓인지 원문으로 확인
    cards = []
    for c, cnt in cf.items():
        for p in order:
            others = max(cnt[o] for o in order if o != p)
            if cnt[p] >= 8 and cnt[p] >= 4 * max(others, 1):
                q, why = first_reason(D, p, c)
                cards.append((cnt[p], f'<div class="rs-card" id="{card_id(p, c)}"><span class="rs-ct">{mname(p)}</span><b>{E(crit_name(D["names"], c))}</b>'
                                      f'<div class="rs-cn"><span>{cnt[p]}</span><small>문항 · 다른 모델은 많아야 {others}</small></div>'
                                      f'<p>예) Q{q} — {E(why[:80])}</p>{q_chips(crit_qs(D, p, c))}</div>'))
    py_qs = sorted(q for q in D["qtypes"] if D["lines"][("gpt", q)]["verdict"] == "FAIL"
                   and any(x.startswith("1-2.python_syntax") for x in D["lines"][("gpt", q)]["line"].get("code_fails") or [])) if "gpt" in order else []
    py = len(py_qs)
    if py:   # 10-21 확인: ChatGPT 앱의 답 아래 복사 아이콘이 파이썬 코드블록 줄바꿈을 지움(앱 화면은 여러 줄) — 사용자 결정: 복사 결과 그대로
        cards.append((0, f'<div class="rs-card rs-warn" id="k-gpt-python"><span class="rs-ct">{mname("gpt")} · 복사 기능 특성</span><b>Python 코드가 한 줄로 붙음</b>'
                         f'<div class="rs-cn"><span>{py}</span><small>문항 FAIL</small></div><p>ChatGPT 앱 화면에는 여러 줄인데, 답 아래 복사 아이콘으로 복사하면 '
                         f'줄바꿈이 사라집니다(확인 완료). 사용자가 복사해 받는 글 그대로 평가해 FAIL 로 둡니다.</p>{q_chips(py_qs)}</div>'))
    cards.sort(key=lambda x: -x[0])
    return grid + ('<h3 class="rs-sh">한 모델에만 몰린 것 — 원문으로 확인할 것</h3><div class="rs-cards">' + "".join(h for _k, h in cards) + "</div>" if cards else "")


def wobble_note() -> str:
    """심판 흔들림 — '확인할 것'에서 바로 오는 자리 (사용자 10-21). 값은 WOBBLE(10-21 오전 low/medium A/B)."""
    return (f'<div id="wobble" class="rs-anchor">' + note("심판 흔들림 — 몇 문항 차이는 오차일 수 있음",
            f"<p>같은 답을 Codex 추론 low 와 medium 으로 한 번씩 판정해 보니(표본 {WOBBLE['n']}건 = 10질문 × 4모델), "
            f"<b>{WOBBLE['changed']}건</b>의 판정이 달랐습니다. 그래서 모델 사이 몇 문항 차이는 순위를 가르는 근거로 약합니다. "
            "medium 은 1.8배 오래 걸려 최종 판정은 low 로 했습니다.</p>") + "</div>")


def scene_hold(D: Dict[str, Any]) -> str:
    order = D["order"]
    holds = [(q, p) for (p, q), L in D["lines"].items() if L["verdict"] == "HOLD"]
    hq = sorted({q for q, _p in holds})
    ty = Counter(D["qtypes"][q] for q in hq)
    state: Counter = Counter()
    cols = f"minmax(150px, 1.6fr) repeat({len(order)}, minmax(0, 1fr))"
    cells = ['<div class="rs-hh"></div>'] + [f'<div class="rs-hh">{mname(p)}</div>' for p in order]
    other = sorted((q, p) for q, p in holds if D["qtypes"][q] != EMPTY_REQ)   # 빈 출력 요구 밖 보류 — 심판이 확인 못 한 항목
    for q in hq:
        text = (D["qset"][q]["turns"][0].get("question") or "").replace("\n", " ")
        cells.append(f'<div class="rs-mq"><a href="#q-{q}"><b>Q{q}</b></a><span title="{E(text)}">{E(text)}</span></div>')
        for p in order:
            L = D["lines"][(p, q)]
            if L["verdict"] != "HOLD":
                cells.append(f'<div class="rs-mc">{badge(L["verdict"])}</div>')
                continue
            if D["qtypes"][q] != EMPTY_REQ:
                why = hold_why(D, p, q)
                state["fact"] += 1
                cells.append(f'<div class="rs-mc rs-mc-fact" title="{E(" / ".join(w["reason"] for w in why))}">{E(hold_label(why))}</div>')
                continue
            t = D["turns"].get((p, q, 1)) or {}
            a = (t.get("answer") or "").strip()
            if t.get("status") == "COPY_FAILED":
                k, lab = "copy", "복사 실패"
            elif not a:
                k, lab = "empty", "빈 답"
            else:
                k, lab = "text", f"“{a[:10]}{'…' if len(a) > 10 else ''}”"
            state[k] += 1
            cells.append(f'<div class="rs-mc rs-mc-{k}" title="{E(a) if a else E(lab)}">{E(lab)}</div>')
    kinds = Counter()
    disagree = 0
    for (p, q), L in D["lines"].items():
        for it in L["items"]:
            if it["decision"] == "UNVERIFIED" and L["verdict"] == "FAIL":
                kinds[it["kind"] or "기타"] += 1
            if "갈림, Codex 따름" in (it["how"] or ""):
                disagree += 1
    n_empty_q = len({q for q, _p in holds if D["qtypes"][q] == EMPTY_REQ})
    parts_ = ([f"빈 출력 요구 {n_empty_q}문항 × {len(order)}모델"] if n_empty_q else []) \
        + ([f"{hold_label([w for q, p in other for w in hold_why(D, p, q)])} {len(other)}칸"] if other else [])
    what = " + ".join(parts_)
    top = (f'<div class="rs-hold-top"><div class="rs-hbig"><b>{len(holds)}<small>칸</small></b><span>= {E(what)}</span>'
           '<span class="muted">사람이 정할 것은 이것뿐입니다</span></div><div class="rs-facts">'
           f'<div class="rs-fact"><b>{sum(kinds.values())}</b>이미 FAIL 인 질문 안의 보류 항목 — 결과를 바꾸지 않음 <span class="muted">({E(" · ".join(f"{k} {v}" for k, v in kinds.most_common()))})</span></div>'
           f'<div class="rs-fact"><b>{disagree:,}</b>심판이 갈려 Codex 를 따른 항목 — 보류 아님 (부록 E)</div></div></div>')
    legend = ('<div class="rs-legend">'
              f'<span><i class="rs-sw rs-mc-copy"></i>복사 실패 {state["copy"]}</span><span><i class="rs-sw rs-mc-empty"></i>빈 답 {state["empty"]}</span>'
              f'<span><i class="rs-sw rs-mc-text"></i>짧은 글 {state["text"]}</span>'
              + (f'<span><i class="rs-sw rs-mc-fact"></i>확인 못 함 {state["fact"]} — 빈 출력 요구가 아닌 질문</span>' if state["fact"] else "") + '</div>')
    grid = f'<div class="rs-heat rs-mx" style="grid-template-columns: {cols}">' + "".join(cells) + "</div>"
    why_rows = []
    for q, p in other:
        for w in hold_why(D, p, q):
            tt = f" · 턴 {w['turn']}" if len(D["qset"][q]["turns"]) > 1 else ""
            sent = f" · Codex 로 보낸 이유: {w['flagged']}" if w["flagged"] else ""
            why_rows.append([f'<a href="#q-{q}"><b>Q{q}</b></a><div class="rs-id">{E(TYPE_NAME.get(D["qtypes"][q], ""))}</div>', mname(p),
                             f'<b>{E(crit_name(D["names"], w["item"]))}</b><div class="rs-id">{E(w["item"])}{E(tt)}</div>',
                             E(w["reason"]) + (f'<div class="rs-basis">답의 해당 문장: “{E(w["quote"])}”</div>' if w["quote"] else "")
                             + f'<div class="rs-basis">{E(w["how"] + sent)}</div>'])
    why_tab = ('<h3 class="rs-sh">빈 출력 요구가 아닌 보류 — 사유</h3>' + table("", ["질문", "모델", "확인 못 한 기준", "사유 (Codex)"], why_rows, [12, 12, 18, 58])
               if why_rows else "")
    n_empty = len(holds) - len(other)
    decide = note("정할 것", f"<p>① 빈 출력 요구 {n_empty}칸 — '아무것도 출력하지 마' 지시에 <b>빈 답</b> · <b>복사 실패</b> · <b>짧은 글</b>을 각각 통과로 볼지 FAIL 로 볼지.</p>"
                  + (f"<p>② 확인 못 함 {len(other)}칸 — 위 표의 사유와 답의 해당 문장을 보고, 맞으면 통과 · 틀리면 FAIL.</p>" if other else "")
                  + "<p>질문 번호를 누르면 질문·답변과 사유가 함께 펼쳐집니다.</p>", warn=True)
    return top + legend + grid + why_tab + decide


# 걸린 시간 표의 유형 — 사용자 10-21: 싱글턴 (S) · 멀티턴 (M) · 롱컨텍스트 (L), 이 순서로
TIME_COLS = ["단일 질문", "10턴 멀티턴 대화", "롱컨텍스트", "첨부자료 확인·요약"]
TYPE_SHORT = {"단일 질문": "싱글턴 (S)", "10턴 멀티턴 대화": "멀티턴 (M)", "롱컨텍스트": "롱컨텍스트 (L)", "첨부자료 확인·요약": "첨부 자료",
              "명시적 빈 출력": "빈 출력 요구"}


def collect_tokens(D: Dict[str, Any]) -> Optional[Dict[Tuple[str, str], Tuple[float, float, float, float]]]:
    """답변 수집 토큰 — (모델, 유형) → 질문(대화) 하나당 평균 (입력 토큰, 출력 토큰, 입력 글자 수, 출력 글자 수). 앱·웹으로 받아 실제 사용량 기록이 없어
    질문·답 글을 o200k_base 로 센 추정. 입력 = 턴마다 그때까지의 대화(앞 질문·답 · 참조 문서 · 첨부 글) + 이번 질문, 출력 = 답.
    시스템 프롬프트 · 이미지 · 모델 내부 추론은 모름(뺌). tiktoken 이 없으면 None."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("o200k_base")
    except Exception:   # 설치 안 됨 · 인코딩 파일을 못 받음 — 표를 그리지 않는다
        return None
    cache: Dict[str, int] = {}

    def n(text: str) -> int:
        if not text:
            return 0
        if text not in cache:
            cache[text] = len(enc.encode(text, disallowed_special=()))
        return cache[text]

    acc: Dict[Tuple[str, str], List[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0])
    for p in D["order"]:
        for q, ty in D["qtypes"].items():
            if ty == "명시적 빈 출력":
                continue
            hist = tin = tout = 0
            hist_c = cin = cout = 0   # 같은 셈을 글자 수로 (사용자 10-21: 몇 글자인지도)
            for tn, turn in enumerate(D["qset"][q]["turns"], 1):
                docs = [turn.get("reference") or ""] + [a.get("content") or "" for a in turn.get("attachments") or [] if isinstance(a, dict)]
                question = turn.get("question") or ""
                answer = (D["turns"].get((p, q, tn)) or {}).get("answer") or ""
                ctx = hist + sum(n(x) for x in docs) + n(question)
                ctx_c = hist_c + sum(len(x) for x in docs) + len(question)
                ans, ans_c = n(answer), len(answer)
                tin, cin = tin + ctx, cin + ctx_c
                tout, cout = tout + ans, cout + ans_c
                hist, hist_c = ctx + ans, ctx_c + ans_c   # 다음 턴은 앞 대화 전체를 다시 읽는다
            a = acc[(p, ty)]
            a[0] += tin
            a[1] += tout
            a[2] += cin
            a[3] += cout
            a[4] += 1
    return {k: (v[0] / v[4], v[1] / v[4], v[2] / v[4], v[3] / v[4]) for k, v in acc.items()}


def ktok(x: float) -> str:
    return f"{x / 1e6:.2f}M" if x >= 1e6 else f"{x / 1e3:.0f}K" if x >= 1e4 else f"{x / 1e3:.1f}K" if x >= 1e3 else f"{x:.0f}"


def collect_token_table(D: Dict[str, Any]) -> str:
    """유형별 — 질문(대화) 하나당 평균 토큰 (답변 수집 · 추정) — 걸린 시간 표 바로 아래 (사용자 10-21)."""
    tok = collect_tokens(D)
    if not tok:
        return ""
    def cell(v: Tuple[float, float, float, float]) -> str:
        return (f'입력 <b>{ktok(v[0])}</b> <span class="muted">({v[2]:,.0f}자)</span> · '
                f'출력 <b>{ktok(v[1])}</b> <span class="muted">({v[3]:,.0f}자)</span>')
    rows = [[mname(p), *[(cell(tok[(p, ty)]) if (p, ty) in tok else "—") for ty in TIME_COLS]] for p in D["order"]]
    return (table("유형별 — 질문(대화) 하나당 평균 토큰 (답변 수집 · 추정)", ["모델", *[TYPE_SHORT[ty] for ty in TIME_COLS]], rows, [12, 22, 22, 22, 22])
            + '<p class="rs-basis">앱·웹 화면으로 받아 모델이 실제로 쓴 토큰은 기록되지 않습니다. 그래서 질문·답 글을 o200k_base 토크나이저(GPT 계열)로 센 추정입니다. '
              '입력 = 턴마다 그때까지의 대화(앞 질문·답 · 참조 문서 · 첨부 글) + 이번 질문 — 멀티턴은 10턴을 더한 값. 출력 = 답. '
              '괄호는 같은 글의 글자 수(공백 포함). 시스템 프롬프트 · 이미지 · 모델 내부 추론은 빠져 있어 실제보다 적고, 모델마다 토크나이저가 달라 실제 수와 다릅니다. 빈 출력 요구 질문은 뺐습니다.</p>')


def scene_time(D: Dict[str, Any]) -> str:
    order = D["order"]
    st = {}
    for p in order:
        ts = [t for (pp, _q, _t), t in D["turns"].items() if pp == p]
        perq: Dict[int, float] = defaultdict(float)
        for t in ts:
            perq[int(t["question"])] += (t.get("latency_ms") or 0) / 1000
        got = [t for t in ts if t.get("status") == "SUCCESS" and (t.get("answer") or "").strip()]
        qs_ok = sum(1 for q in D["qtypes"] if all(D["turns"].get((p, q, n), {}).get("status") == "SUCCESS" for n in range(1, len(D["qset"][q]["turns"]) + 1)))
        by = defaultdict(list)
        for q, s in perq.items():
            by[D["qtypes"][q]].append(s)
        st[p] = {"sum": sum(perq.values()), "ok": qs_ok, "chars": statistics.mean(len(t["answer"]) for t in got) if got else 0,
                 "by": by, "max": max(perq.items(), key=lambda kv: kv[1])}
    mx = max(v["sum"] for v in st.values()) or 1
    ex = report_extras(D)
    bars = "".join(f'<div class="rs-tb"><span class="rs-bl">{mname(p)}</span><span class="rs-tbar"><i style="width:{st[p]["sum"] / mx * 100:.1f}%"></i></span>'
                   f'<span class="rs-tv"><b>{hm(st[p]["sum"])}</b><small>질문당 {st[p]["sum"] / st[p]["ok"]:.0f}초 · 턴당 {st[p]["chars"]:,.0f}자</small></span></div>'
                   for p in order if st[p]["ok"])
    trows = [[mname(p), *[f"{statistics.mean(st[p]['by'][ty]):.0f}초" if st[p]["by"][ty] else "—" for ty in TIME_COLS],
              f"Q{st[p]['max'][0]} {TYPE_SHORT.get(D['qtypes'][st[p]['max'][0]], '')} · {R._hms(st[p]['max'][1])}"] for p in order]
    t2 = table("유형별 — 질문(대화) 하나당 평균 답변 시간", ["모델", *[TYPE_SHORT[ty] for ty in TIME_COLS], "가장 오래 걸린 질문"], trows, [12, 12, 13, 15, 13, 35])
    t2 += collect_token_table(D)
    tm = judge_timing(D)
    total = sum(t.get("seconds") or 0 for t in tm) or 1
    label = ["판정"] if len(tm) == 1 else [(t.get("label") or ("첫 판정" if i == 0 else f"다시 판정 {t.get('judgments', 0)}건")) for i, t in enumerate(tm)]
    segs = "".join(f'<i class="k{i + 1}" style="width:{(t.get("seconds") or 0) / total * 100:.2f}%" title="{E(label[i] if i < len(label) else "")} {hm(t.get("seconds") or 0)}">'
                   f'{hm(t.get("seconds") or 0) if (t.get("seconds") or 0) / total >= 0.15 else ""}</i>' for i, t in enumerate(tm[:4]))
    tleg = "".join(f'<span><i class="rs-sw k{i + 1}"></i>{E(label[i])} <b>{hm(t.get("seconds") or 0)}</b> <span class="muted">{t.get("judgments", 0)}건</span></span>' for i, t in enumerate(tm[:4]))
    # 사용자 10-21: 위 '2시간 25분'과 아래 모델별 '20분대'를 더해 맞춰 보다 헷갈림 → 위는 단계를 이어 붙인 실제 시간임을 식으로 쓰고, 모델별 시간 합은 뺌
    eq = " + ".join(f"{E(label[i] if i < len(label) else '판정')} {hm(t.get('seconds') or 0)}" for i, t in enumerate(tm[:4]))
    judge = (f'<div class="rs-judge"><div class="rs-jt"><b>{hm(total)}</b><span>판정 전체에 실제로 걸린 시간<br>판정 {sum(t.get("judgments", 0) for t in tm)}건 (질문 × 모델)</span></div>'
             f'<div class="rs-jb"><div class="rs-tl">{segs}</div><div class="rs-legend">{tleg}</div>'
             + (f'<p class="rs-jeq">네 모델을 동시에 판정했습니다. 그래서 모델별로 나눈 시간은 따로 없습니다.</p></div></div>' if len(tm) == 1 else
                f'<p class="rs-jeq">= {eq} — {len(tm)}단계를 차례로 돌렸고, 단계마다 네 모델을 동시에 판정했습니다. 그래서 모델별로 나눈 시간은 따로 없습니다.</p></div></div>'))
    jt = ""
    if ex["judge"]:   # report.html 4장 판정 표 — 모델마다 시간 · 토큰 (Codex 토큰은 run.log 에서 모은 값)
        J = ex["judge"]
        waits = f" · 요청이 몰려 기다린 횟수(429) {ex['waits']}회" if ex["waits"] else ""
        jr = [[mname(p), J[p]["per_q"], f'{J[p]["dec_in"]:,}', f'{J[p]["codex_calls"]:,}', f'{J[p]["codex_in"] / 1e6:.1f}M · {J[p]["codex_out"] / 1e6:.2f}M'] for p in order if p in J]
        jr.append(['<span class="rs-strong">합계</span>', "", f'{sum(v["dec_in"] for v in J.values()):,}', f'{sum(v["codex_calls"] for v in J.values()):,}',
                   f'{sum(v["codex_in"] for v in J.values()) / 1e6:.1f}M · {sum(v["codex_out"] for v in J.values()) / 1e6:.2f}M'])
        jt = (table("판정 시간 · 토큰 — 모델마다", ["모델", "질문 하나 판정 평균", "Decisions 입력 토큰", "Codex 호출", "Codex 입력 · 출력 토큰"],
                    jr, [14, 20, 22, 14, 30])
              + '<p class="rs-basis">질문 하나 판정 평균 = 그 질문의 마지막 판정 한 번에 걸린 시간의 평균(모델 사이 빠르기 비교용 — 더해도 위 전체 시간이 되지 않음). '
              + f'Decisions API 는 OpenAI API(토큰만큼 비용) · Codex CLI 는 ChatGPT 구독으로 실행{waits}.</p>')
    ref = ""
    if TIME_1020.exists():
        g = (json.loads(TIME_1020.read_text(encoding="utf-8")).get("collect_1020") or {}).get("models", {}).get("gpt") or {}
        if g:
            ref = f'<p class="rs-basis">참고: 10-20 보고 때(다른 78개 세트) GPT 답변 시간 합은 {hm(g.get("total_seconds") or 0)} — 질문이 달라 바로 비교하지는 않습니다.</p>'
    return ('<h3 class="rs-sh">답변 수집 — 답변 시간 합 (질문을 보내고 답을 받기까지)</h3><div class="rs-tbars">' + bars + "</div>" + t2
            + '<h3 class="rs-sh">판정 — 실제로 걸린 시간</h3>' + judge + jt + ref + process_block(ex))


def process_block(ex: Dict[str, Any]) -> str:
    """판정 실행 기록 (10-21) — Codex 호출 · 다시 묻기 · 429 대기와 그 이유 · 판정 실행 오류 (report.html 표에서)."""
    pr = ex.get("process") or {}
    if not pr:
        return ""
    rows = [[k, f"<b>{E(v)}</b>", E(d)] for k, (v, d) in pr.items()]
    return '<h3 class="rs-sh">판정 실행 기록</h3>' + table("", ["항목", "값", "설명"], rows, [16, 16, 68])


# ---- Arena 형 (v4, 사용자 10-21: "LM Arena 리더보드가 제일 괜찮다") — 개요(큰 제목 · 분야 알약 탭 · 순위 카드) + 분야별 순위표(왼쪽 분야 목록 → 오른쪽 표)
def avatar(p: str) -> str:
    return f'<i class="ar-av lg lg-{p}" aria-hidden="true"></i>'


def board_categories(D: Dict[str, Any]) -> List[Dict[str, Any]]:
    """분야마다 모델 값 — 결과·유형, 많을수록 좋음(통과). 기준 묶음은 뺌(사용자 10-21 — 기준별은 FAIL 원인 장면)."""
    order = D["order"]
    cats: List[Dict[str, Any]] = []
    tot = {p: D["tally"][p] for p in order}
    cats.append({"group": "결과", "label": "전체", "chip": "완전 통과", "higher": True, "of": D["n"], "unit": "문항",
                 "vals": {p: tot[p]["PASS"] for p in order}, "extra": {p: f"FAIL {tot[p]['FAIL']} · 보류 {tot[p]['HOLD']}" for p in order}})
    for ty in ROW_ORDER:
        qs = [q for q, t in D["qtypes"].items() if t == ty]
        if not qs:
            continue
        res = {p: Counter(D["lines"][(p, q)]["verdict"] for q in qs) for p in order}
        if all(c["HOLD"] == len(qs) for c in res.values()):
            continue   # 빈 출력 요구 — 모두 보류('보류' 장면)
        if ty == "10턴 멀티턴 대화" and all(c["PASS"] == 0 for c in res.values()):
            ok, n_t = turn_ok(D, qs)
            cats.append({"group": "유형", "label": "멀티턴 (턴 단위)", "chip": f"{len(qs)}문항 · {n_t}턴", "higher": True, "of": n_t, "unit": "턴",
                         "vals": ok, "extra": {p: f"FAIL 있는 턴 {n_t - ok[p]}" for p in order}})
            continue
        cats.append({"group": "유형", "label": TYPE_NAME.get(ty, ty), "chip": f"{len(qs)}문항", "higher": True, "of": len(qs), "unit": "문항",
                     "vals": {p: res[p]["PASS"] for p in order}, "extra": {p: f"FAIL {res[p]['FAIL']} · 보류 {res[p]['HOLD']}" for p in order}})
    for c in cats:   # 순위 — 같은 값은 같은 순위
        vals = c["vals"]
        c["rank"] = {p: 1 + sum(1 for o in order if (vals[o] > vals[p] if c["higher"] else vals[o] < vals[p])) for p in order}
        c["sorted"] = sorted(order, key=lambda p: (c["rank"][p], order.index(p)))
    return cats


def rank_rows(c: Dict[str, Any], limit: int = 10) -> str:
    vals = c["vals"]
    mx = max(vals.values()) or 1
    out = []
    for p in c["sorted"][:limit]:
        v = vals[p]
        w = (v / c["of"] * 100) if c["higher"] else ((mx - v) / mx * 100 if mx else 100)   # 적을수록 좋은 분야는 덜 어긴 만큼 길게
        val = f'{v}<small> / {c["of"]}</small>' if c["higher"] else f'{v}<small> {c["unit"]}</small>'
        pct = f'{v / c["of"] * 100:.0f}%' if c["higher"] else "어김"
        out.append(f'<div class="ar-row" style="--w:{max(w, 2):.1f}%"><span class="ar-rank">{c["rank"][p]}</span>{avatar(p)}<span class="ar-name">{SHORT[p]}</span>'
                   f'<b class="ar-val">{val}</b><span class="ar-pct">{pct}</span></div>')
    return "".join(out)


def tab_css(prefix: str, n: int, label_box: str, panel_box: str, panel_cls: str, active: str) -> str:
    """라디오 탭 — 고른 탭의 라벨 강조 · 그 패널만 보임 (JS 없이)."""
    sel_l = ", ".join(f"#{prefix}{i}:checked ~ {label_box} label[for={prefix}{i}]" for i in range(n))
    sel_p = ", ".join(f"#{prefix}{i}:checked ~ {panel_box} .{panel_cls}{i}" for i in range(n))
    focus = ", ".join(f"#{prefix}{i}:focus-visible ~ {label_box} label[for={prefix}{i}]" for i in range(n))
    return f"<style>{sel_l} {{ {active} }} {sel_p} {{ display: block; }} {focus} {{ outline: 2px solid var(--accent); outline-offset: 2px; }}</style>"


def collect_seconds(D: Dict[str, Any]) -> float:
    """답변 수집 시간 — 모델마다 답변 시간 합(질문을 보내고 답을 받기까지) 중 가장 긴 것 (사용자 10-21: 벽시계 대신 이것)."""
    per = defaultdict(float)
    for (p, _q, _t), t in D["turns"].items():
        per[p] += (t.get("latency_ms") or 0) / 1000
    return max(per.values()) if per else 0


def ar_overview(D: Dict[str, Any]) -> str:
    """개요 (사용자 10-21: 줄이기) — 제목 · 질문 수 · 소요 시간만. '확인할 것'은 빼고(내용은 FAIL 원인 · 보류 장면), '전체' 순위 카드는 순위표 장면 맨 위로."""
    judge_s = sum(t.get("seconds") or 0 for t in judge_timing(D))
    # 제목 하나 · 질문 수 · 소요 시간만 (사용자 10-21: 불필요한 글 지우기). 수집 = 가장 오래 걸린 모델의 답변 시간 합
    collect_s = collect_seconds(D)
    took = (f'<b>{hm(collect_s + judge_s)}</b><span>소요 · 답변 수집 {hm(collect_s)} + 판정 {hm(judge_s)}</span>' if collect_s
            else f'<b>{hm(judge_s)}</b><span>판정 소요</span>')
    hero = (f'<div class="ar-hero" style="grid-template-columns: minmax(0, 1fr)"><div class="ar-hl">'
            '<h1 class="ar-h1"><em>AIONA</em> 채팅 품질 평가</h1>'   # 사용자 10-21: 깔끔한 제목
            f'<div class="ar-stat"><b>{D["n"]}</b><span>질문</span><b style="margin-left:18px">·</b>{took}</div></div></div>')
    return hero


def rank_card(D: Dict[str, Any], cats: List[Dict[str, Any]]) -> str:
    """'전체' 순위 카드 — 순위표 장면 맨 위 (사용자 10-21: 개요에서 옮김)."""
    total_cat = next(c for c in cats if c["group"] == "결과")   # '전체' 완전 통과
    return (f'<div class="ar-card ar-top"><div class="ar-ch"><h3>순위</h3><span class="ar-chip">{E(total_cat["label"])} · {E(total_cat["chip"])}</span></div>'
            f'<div class="ar-rows">{rank_rows(total_cat)}</div></div>')


def ar_board(D: Dict[str, Any], cats: List[Dict[str, Any]]) -> str:
    order = D["order"]
    radios = "".join(f'<input type="radio" name="bd" id="bd{i}" class="ar-r"{" checked" if i == 0 else ""}>' for i in range(len(cats)))
    groups: Dict[str, List[int]] = defaultdict(list)
    for i, c in enumerate(cats):
        groups[c["group"]].append(i)
    side = "".join(f'<div class="ar-cg"><span>{E(g)}</span>' + "".join(f'<label for="bd{i}">{E(cats[i]["label"])}</label>' for i in idx) + "</div>" for g, idx in groups.items())
    panels = []
    for i, c in enumerate(cats):
        head = ("<tr><th>순위</th><th>모델</th>"
                + (f'<th>{"완전 통과" if c["unit"] == "문항" else "FAIL 없는 턴"}</th><th>비율</th>' if c["higher"] else "<th>어긴 질문</th><th></th>")
                + "<th>참고</th></tr>")
        rows = []
        for p in c["sorted"]:
            v = c["vals"][p]
            rows.append(f'<tr><td class="ar-tr">{c["rank"][p]}</td><td><div class="ar-tm">{avatar(p)}<span><b>{SHORT[p]}</b><small>{E(D["d"]["models"].get(p, ""))}</small></span></div></td>'
                        f'<td class="ar-tv">{v}<small> / {c["of"]}</small></td>' + (f'<td class="ar-tp2">{v / c["of"] * 100:.0f}%</td>' if c["higher"] else "<td></td>")
                        + f'<td class="ar-tx">{E(c["extra"][p])}</td></tr>')
        panels.append(f'<div class="ar-pn ar-bd{i}"><div class="ar-bh"><div><h3>{E(c["label"])}</h3><span class="ar-chip">{E(c["group"])} · {E(c["chip"])}</span></div>'
                      f'<div class="ar-meta">2026-10-21 · 질문 {D["n"]}개 · 모델 {len(order)}개 · {"높을수록 좋음" if c["higher"] else "적을수록 좋음"}</div></div>'
                      f'<table class="ar-tab"><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>')
    return (f'<div class="ar-board">{radios}<aside class="ar-cats">{side}</aside><div class="ar-tables">{"".join(panels)}</div></div>'
            + tab_css("bd", len(cats), ".ar-cats", ".ar-tables", "ar-bd", "background: var(--accent-soft); color: var(--accent); font-weight: 600;"))


def helm_table(D: Dict[str, Any], cats: List[Dict[str, Any]]) -> str:
    """HELM 형 (사용자 10-21: 'C 도 섞으면 좋겠다') — 모델 × 분야 점수표 한 장, 탭 둘(통과율 · 기본 정보).
    기준 묶음 · 시간·토큰 탭은 뺐다(사용자 10-21 — 시간·토큰은 '걸린 시간' 장면에 같은 숫자가 있음).
    숫자는 아래 분야별 순위표의 그 분야 라벨이라 누르면 순위표가 그 분야로 바뀐다(JS 없이)."""
    order = D["order"]
    ex = report_extras(D)
    idx = {id(c): i for i, c in enumerate(cats)}

    def score_tab(cs: List[Dict[str, Any]], fmt) -> str:
        best = {id(c): (max if c["higher"] else min)(c["vals"].values()) for c in cs}
        head = "<tr><th>모델</th>" + "".join(f'<th><label for="bd{idx[id(c)]}">{E(c["label"])}</label></th>' for c in cs) + "</tr>"
        rows = "".join(f'<tr><th scope="row"><span class="hm-m">{avatar(p)}<b>{SHORT[p]}</b></span></th>'
                       + "".join(f'<td class="{"hm-best" if c["vals"][p] == best[id(c)] else ""}"><label for="bd{idx[id(c)]}">{fmt(c, p)}</label></td>' for c in cs) + "</tr>"
                       for p in order)
        return f'<table class="hm-tab"><thead>{head}</thead><tbody>{rows}</tbody></table>'

    res = [c for c in cats if c["group"] in ("결과", "유형")]
    t1 = score_tab(res, lambda c, p: f'{c["vals"][p] / c["of"]:.2f}')
    # 기본 정보
    irows = []
    for p in order:
        ts = [t for (pp, _q, _t), t in D["turns"].items() if pp == p]
        got = [t for t in ts if t.get("status") == "SUCCESS" and (t.get("answer") or "").strip()]
        ok = sum(1 for q in D["qtypes"] if all(D["turns"].get((p, q, n), {}).get("status") == "SUCCESS" for n in range(1, len(D["qset"][q]["turns"]) + 1)))
        irows.append(f'<tr><th scope="row"><span class="hm-m">{avatar(p)}<b>{SHORT[p]}</b></span></th><td class="hm-l">{E(D["d"]["models"].get(p, ""))}</td><td>{E(method_of(D, p))}</td>'
                     f'<td>{ok} / {D["n"]}</td><td>{sum(1 for t in ts if t.get("status") == "COPY_FAILED")}</td><td>{statistics.mean(len(t["answer"]) for t in got):,.0f}자</td></tr>')
    t4 = ('<table class="hm-tab"><thead><tr><th>모델</th><th>버전 · 설정</th><th>수집</th><th>받은 질문</th><th>복사 실패 턴</th><th>턴당 답변 길이</th></tr></thead>'
          f'<tbody>{"".join(irows)}</tbody></table>')
    tabs = [("통과율", "높을수록 좋음 · 완전 통과 ÷ 질문 수 (멀티턴은 FAIL 없는 턴 ÷ 110턴)", t1),
            ("기본 정보", "모델 버전과 수집 방법", t4)]
    radios = "".join(f'<input type="radio" name="hm" id="hm{i}" class="ar-r"{" checked" if i == 0 else ""}>' for i in range(len(tabs)))
    labels = '<div class="hm-tabs">' + "".join(f'<label for="hm{i}">{E(t)}</label>' for i, (t, _d, _x) in enumerate(tabs)) + "</div>"
    panels = '<div class="hm-ps">' + "".join(f'<div class="ar-pn hm-p{i}"><p class="hm-d">{E(d)}{" · 숫자를 누르면 아래 순위표가 그 분야로 바뀝니다" if i == 0 else ""}</p>'
                                             f'<div class="hm-scroll">{x}</div></div>' for i, (_t, d, x) in enumerate(tabs)) + "</div>"
    return (f'<div class="hm">{radios}{labels}{panels}</div>'
            + tab_css("hm", len(tabs), ".hm-tabs", ".hm-ps", "hm-p", "background: var(--accent-soft); color: var(--accent); border-color: transparent;"))


# ------------------------------------------------------------------ 부록 ------------------------------------------------------------------
def find_span(text: str, quote: str) -> Optional[Tuple[int, int]]:
    """심판이 인용한 답 문장의 위치 — 그대로 → 공백·마크다운 기호 무시 → '…'로 끊긴 인용은 앞 조각."""
    qt = (quote or "").strip().strip('“”"\'')
    if not qt:
        return None
    i = text.find(qt)
    if i >= 0:
        return i, i + len(qt)
    toks = [re.escape(x) for x in re.split(r"[\s*_`#>|]+", qt) if x]
    if toks:
        m = re.search(r"[\s*_`#>|]*".join(toks), text)
        if m:
            return m.span()
    frags = [f.strip() for f in re.split(r"…|\.\.\.", qt) if len(f.strip()) >= 8]
    if len(frags) > 1 or (frags and frags[0] != qt):
        for f in frags:
            sp = find_span(text, f)
            if sp:
                return sp
    return None


def find_spans(text: str, quote: str) -> List[Tuple[int, int]]:
    """인용 전체를 못 찾으면 줄마다 — 심판이 떨어진 문장 여러 개를 빈 줄로 이어 인용한 경우."""
    sp = find_span(text, quote)
    if sp:
        return [sp]
    out = []
    for seg in re.split(r"\n", quote or ""):
        seg = seg.strip()
        if len(seg) >= 8 and not re.fullmatch(r"[|\-:\s]+", seg):
            sp = find_span(text, seg)
            if sp:
                out.append(sp)
    return out


def mark_answer(ans: str, ents: List[Dict[str, Any]]) -> str:
    """답 원문에 걸린 부분 칠하기 — 번호는 오른쪽 사유 번호. 겹치면 한 칸에 번호를 같이."""
    spans = []
    for e in ents:
        sps = find_spans(ans, e["quote"]) if e["quote"] else []
        e["found"] = bool(sps)
        spans.extend((a, b, e["n"]) for a, b in sps)
    merged: List[Tuple[int, int, List[int]]] = []
    for a, b, n in sorted(spans):
        if merged and a < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]), merged[-1][2] + [n])
        else:
            merged.append((a, b, [n]))
    out, pos = [], 0
    for a, b, ns in merged:
        out.append(E(ans[pos:a]))
        out.append(f'<mark class="rs-mk"><sup>{"·".join(map(str, ns))}</sup>{E(ans[a:b])}</mark>')
        pos = b
    out.append(E(ans[pos:]))
    return "".join(out)


def turn_html(D: Dict[str, Any], p: str, q: int, tn: int, multi: bool, ents: Optional[List[Dict[str, Any]]] = None) -> str:
    """원문 한 턴 — 질문(첨부 · 참조 문서 표시) · 답변. ents 를 주면 답에서 걸린 부분을 칠한다(FAIL)."""
    turns_q = D["qset"][q]["turns"]
    turn = turns_q[tn - 1] if 0 < tn <= len(turns_q) else {}
    t = D["turns"].get((p, q, tn)) or {}
    att = [a.get("name") if isinstance(a, dict) else str(a) for a in turn.get("attachments") or []]
    extra = (f' <span class="muted">첨부: {E(", ".join(x for x in att if x))}</span>' if att else "") + \
            (f' <span class="muted">참조 문서 {len(turn.get("reference") or ""):,}자(생략)</span>' if turn.get("reference") else "")
    st = "" if t.get("status") == "SUCCESS" else f' <span class="rs-failtxt">수집 상태 {E(t.get("status") or "없음")}</span>'
    ans = t.get("answer") or ""
    body = (mark_answer(ans, ents) if ents is not None else E(ans)) if ans else "<i>(빈 답)</i>"
    tl = f"턴 {tn} " if multi else ""
    return (f'<div class="rs-turn"><b>{tl}질문</b>{extra}<pre class="rs-pre rs-q">{E(turn.get("question") or "")}</pre>'
            f'<b>{tl}답변</b>{st}<pre class="rs-pre rs-ans">{body}</pre></div>')


def kept_criteria(D: Dict[str, Any], p: str, q: int) -> Tuple[List[Dict[str, Any]], List[str]]:
    """지킨 기준 (사용자 10-21: 어떤 기준을 지켰는지) — 의미 평가 통과 항목 · CODE 검사 통과(턴마다), 그리고 해당 없음 기준."""
    ents: List[Dict[str, Any]] = []
    na: List[str] = []
    for it in D["lines"][(p, q)]["items"]:
        if it["decision"] == "PASS":
            ents.append({"turn": int(it["turn"]), "item": it["item"], "kind": "의미", "reason": it.get("reason") or "",
                         "how": (it.get("how") or "").replace("PASS", "통과")})
        elif it["decision"] == "NOT_APPLICABLE":
            na.append(it["item"])
    for tn in range(1, len(D["qset"][q]["turns"]) + 1):
        for c in (D["turns"].get((p, q, tn)) or {}).get("code") or []:
            if c.get("verdict") == "PASS":
                ents.append({"turn": tn, "item": c["check_id"], "kind": "CODE", "reason": c.get("reason") or "", "how": "프로그램 검사"})
            elif c.get("verdict") != "FAIL":
                na.append(c["check_id"])
    return ents, sorted(set(na))


def pass_blocks(D: Dict[str, Any]) -> List[Tuple[str, int, str]]:
    """통과한 질문마다 (모델, 질문, HTML) — 왼쪽 질문·답변 원문, 오른쪽 지킨 기준(의미 · CODE)과 근거 · 해당 없음 기준 (사용자 10-21)."""
    out = []
    for p in D["order"]:
        for q in sorted(D["qtypes"]):
            if D["lines"][(p, q)]["verdict"] != "PASS":
                continue
            ents, na = kept_criteria(D, p, q)
            turns_q = D["qset"][q]["turns"]
            multi = len(turns_q) > 1
            left = "".join(turn_html(D, p, q, tn, multi) for tn in range(1, len(turns_q) + 1))

            def li(e: Dict[str, Any]) -> str:
                tt = f" · 턴 {e['turn']}" if multi else ""
                return (f'<div class="rs-item"><div><b class="rs-okno">✓</b> <b>{E(crit_name(D["names"], e["item"]) or e["item"])}</b> '
                        f'<span class="rs-id">{E(e["item"])}{tt}</span></div>'
                        + (f'<div class="rs-fwhy">{E(e["reason"])}</div>' if e["reason"] else "") + f'<div class="rs-quote">{E(e["how"])}</div></div>')
            sem = [e for e in ents if e["kind"] == "의미"]
            code = [e for e in ents if e["kind"] == "CODE"]
            right = (f'<h4>의미 평가 — 지킨 기준 {len(sem)}개</h4>' + "".join(li(e) for e in sem)
                     + f'<h4 class="rs-h4gap">CODE — 통과한 검사 {len(code)}개</h4>' + "".join(li(e) for e in code)
                     + (f'<details class="rs-d rs-inner"><summary>해당 없음 {len(na)}개 — 이 질문에 적용되지 않는 기준</summary><p class="rs-quote">'
                        + " · ".join(E(crit_name(D["names"], c) or c) for c in na) + "</p></details>" if na else ""))
            out.append((p, q, f'<article class="rs-fd" id="p-{p}-{q}"><h3 class="rs-fdh">Q{q} · {mname(p)} · {E(TYPE_NAME.get(D["qtypes"][q], ""))} · '
                              f'<span class="rs-oktxt">통과</span> · 지킨 기준 {len(ents)}개</h3>'
                              f'<div class="rs-fx"><div class="rs-fx-l"><h4>원문</h4>{left}</div><div class="rs-fx-r">{right}</div></div></article>'))
    return out


def fail_entry_html(D: Dict[str, Any], e: Dict[str, Any], multi: bool) -> str:
    if e["kind"] == "CODE":
        where = '<div class="rs-quote">프로그램 검사(CODE) — 답 전체를 세거나 형식을 검사한 결과</div>'
    elif e["quote"] and e["found"]:
        where = f'<div class="rs-quote">답에서 걸린 부분 <b class="rs-hlno">{e["n"]}</b> “{E(e["quote"][:300])}”</div>'
    elif e["quote"]:
        where = f'<div class="rs-quote">답 인용(원문에서 위치를 못 찾음): “{E(e["quote"][:300])}”</div>'
    else:
        where = '<div class="rs-quote">답의 특정 문장을 짚지 않음 — 빠진 내용이나 답 전체에 대한 판정</div>'
    qq = f'<div class="rs-quote">질문의 요구: “{E(e["qquote"][:200])}”</div>' if e.get("qquote") else ""
    how = f'<div class="rs-quote">심판: {E(e["how"])}</div>' if e.get("how") else ""
    tt = f' · 턴 {e["turn"]}' if multi else ""
    return (f'<div class="rs-item"><div><b class="rs-hlno{"" if e.get("found") else " rs-hlno-off"}">{e["n"]}</b> <b>{E(crit_name(D["names"], e["item"]))}</b> '
            f'<span class="rs-id">{E(e["item"])}{tt} · {E(e["kind"])}</span></div>'
            f'<div class="rs-fwhy">{E(e["reason"] or "사유 없음")}</div>{where}{qq}{how}</div>')


def item_html(D: Dict[str, Any], it: Dict[str, Any]) -> str:
    q = f'<div class="rs-quote">답 인용: {E(it["answer_quote"][:220])}</div>' if it.get("answer_quote") else ""
    qq = f'<div class="rs-quote">질문 인용: {E(it["question_quote"][:160])}</div>' if it.get("question_quote") else ""
    how = f'<div class="rs-quote">심판: {E(it["how"])}</div>' if it.get("how") else ""
    lab = {"FAIL": "FAIL", "UNVERIFIED": "보류"}.get(it["decision"], it["decision"])
    return (f'<div class="rs-item"><div><b>턴 {it["turn"]} · <span class="rs-id">{E(it["item"])}</span></b> {E(crit_name(D["names"], it["item"]))} '
            f'<span class="rs-{"failtxt" if it["decision"] == "FAIL" else "holdtxt"}">{E(lab)}</span>{" · " + E(it["kind"]) if it.get("kind") else ""}</div>'
            f'<div>{E(it["reason"] or "; ".join(it.get("problems") or []))}</div>{q}{qq}{how}</div>')


def q_detail(D: Dict[str, Any], q: int, part_of: Dict[str, str]) -> str:
    """질문 하나 — 질문 글 · 모델마다 판정과 이유 · 원문 링크. 평소에는 숨어 있고 칸을 누르면(#q-n, CSS :target) 펼쳐진다 — JS 없이."""
    turns = D["qset"][q]["turns"]
    first = (turns[0].get("question") or "").strip()
    tn = " · 첫 턴 질문" if len(turns) > 1 else ""
    att = turns[0].get("attachments") or []
    extra = (f" · 첨부 {len(att)}개" if att else "") + (f" · 참조 문서 {len(turns[0].get('reference') or ''):,}자" if turns[0].get("reference") else "")
    rows = []
    for p in D["order"]:
        L = D["lines"][(p, q)]
        v = L["verdict"]
        if v == "FAIL":
            crit: Dict[str, Dict[str, Any]] = {}
            for c in code_fail_rows(D, p, q):
                crit.setdefault(c["check_id"], {"turns": set(), "why": c["reason"]})["turns"].add(c["turn"])
            for it in L["items"]:
                if it["decision"] == "FAIL":
                    crit.setdefault(it["item"], {"turns": set(), "why": it["reason"]})["turns"].add(it["turn"])
            lis = []
            for c, x in list(crit.items())[:6]:
                why = x["why"] or ""
                tt = (" · 턴 " + ",".join(map(str, sorted(x["turns"])))) if len(turns) > 1 else ""
                lis.append(f'<li><b>{E(crit_name(D["names"], c))}</b><span class="rs-id">{E(c)}{tt}</span>'
                           f'<span class="rs-why">{E(why[:100])}{"…" if len(why) > 100 else ""}</span></li>')
            more = f'<li class="muted">… {len(crit) - 6}개 더 — 원문에서</li>' if len(crit) > 6 else ""
            fid = f"f-{p}-{q}"
            body = (f'<ul class="rs-ql">{"".join(lis)}{more}</ul>'
                    + (f'<a class="rs-go" href="{E(part_of[fid])}#{fid}">원문 · 사유 · 답에서 걸린 부분 →</a>' if fid in part_of else ""))
            head = f'{badge(v)} <span class="muted">어긴 기준 {len(crit)}개</span>'
        elif v == "HOLD":
            t = D["turns"].get((p, q, 1)) or {}
            a = (t.get("answer") or "").strip()
            what = "복사 실패 — 받은 글 없음" if t.get("status") == "COPY_FAILED" else ("빈 답" if not a else f"글 {len(a)}자")
            why = hold_why(D, p, q)
            if why:   # 채점하다 확인 못 한 항목이 남은 보류 — 사유 · 답의 해당 문장 (사용자 10-21)
                lis = "".join(f'<li><b>{E(crit_name(D["names"], w["item"]))}</b><span class="rs-id">{E(w["item"])}'
                              f'{" · 턴 " + str(w["turn"]) if len(turns) > 1 else ""}</span><span class="rs-why">사유: {E(w["reason"])}</span>'
                              + (f'<span class="rs-why">답의 해당 문장: “{E(w["quote"])}”</span>' if w["quote"] else "")
                              + f'<span class="rs-why">{E(w["how"])}</span></li>' for w in why)
                body = (f'<p class="rs-qa">보류 사유 — {E(hold_label(why))}</p><ul class="rs-ql">{lis}</ul>'
                        + (f'<p class="rs-why">답 앞부분 ({E(what)})</p><pre class="rs-pre rs-pre-s">{E(a[:400])}{"…" if len(a) > 400 else ""}</pre>' if a else ""))
            else:
                body = (f'<p class="rs-qa">{E(what)}</p>' + (f'<pre class="rs-pre rs-pre-s">{E(a[:400])}</pre>' if a else "")
                        + f'<p class="rs-why">보류 사유: {E(L["line"].get("reason") or (EMPTY_WHY if D["qtypes"][q] == EMPTY_REQ else "기록 없음"))}</p>')
            head = badge(v)
        else:
            ents, _na = kept_criteria(D, p, q)
            # 사용자 10-21: 이 화면에서 바로 어떤 기준을 지켰는지 — 의미 기준 이름, CODE 는 기준(검사를 기준으로 묶음) 이름
            sem = list(dict.fromkeys(crit_name(D["names"], e["item"]) or e["item"] for e in ents if e["kind"] == "의미"))
            code = list(dict.fromkeys(crit_name(D["names"], e["item"].split(":")[0]) or e["item"] for e in ents if e["kind"] == "CODE"))
            pid = f"p-{p}-{q}"
            body = ('<p class="rs-qa">해당하는 기준을 모두 지킴</p>'
                    f'<p class="rs-kept"><b>의미 {len(sem)}</b>{E(" · ".join(sem))}</p>'
                    f'<p class="rs-kept"><b>CODE {len(code)}</b>{E(" · ".join(code))}</p>'
                    + (f'<a class="rs-go" href="{E(part_of[pid])}#{pid}">질문·답변 원문 · 기준별 근거 →</a>' if pid in part_of else ""))
            head = badge(v)
        rows.append(f'<div class="rs-qm"><div class="rs-qmh"><b>{mname(p)}</b>{head}</div>{body}</div>')
    return (f'<article class="rs-qd" id="q-{q}"><div class="rs-qdh"><span><b>Q{q}</b> {E(TYPE_NAME.get(D["qtypes"][q], ""))}{E(tn)}{E(extra)}</span>'
            f'<a class="rs-x" href="#apx">닫기 ✕</a></div><p class="rs-qt">{E(first[:420])}{"…" if len(first) > 420 else ""}</p>'
            f'<div class="rs-qms">{"".join(rows)}</div></article>')


def appendix(D: Dict[str, Any], parts: List[Tuple[str, str, str]], part_of: Dict[str, str], e_link: str, n_disagree: int, sub: str,
             pparts: Optional[List[Tuple[str, str, str]]] = None) -> str:
    """부록 — 탭 셋(라디오 + CSS, JS 없이) · 질문별 결과는 유형마다 4모델 × 문항 칸, 누르면 그 질문만 펼침."""
    order = D["order"]
    # 탭 1 — 질문별 결과: 유형마다 칸
    blocks = []
    for ty in ROW_ORDER:
        qs = sorted(q for q, t in D["qtypes"].items() if t == ty)
        if not qs:
            continue
        cells = ["<span></span>"] + [f'<a class="rs-qn" href="#q-{q}">{q}</a>' for q in qs]
        for p in order:
            cells.append(f'<span class="rs-mn">{mname(p)}</span>')
            for q in qs:
                L = D["lines"][(p, q)]
                v = L["verdict"]
                k = len({i["item"] for i in L["items"] if i["decision"] == "FAIL"}) + len(L["line"].get("code_fails") or [])
                lab = {"PASS": "완전 통과", "FAIL": f"FAIL — 기준 {k}개 어김", "HOLD": "보류"}.get(v, v)
                cells.append(f'<a class="rs-t rs-t-{v.lower()}" href="#q-{q}" title="Q{q} · {SHORT[p]} · {lab}">{k if v == "FAIL" else ""}</a>')
        pas = " · ".join(f"{SHORT[p]} {sum(1 for q in qs if D['lines'][(p, q)]['verdict'] == 'PASS')}" for p in order)
        blocks.append(f'<div class="rs-mb"><div class="rs-mbh"><b>{E(TYPE_NAME.get(ty, ty))}</b><span>{len(qs)}문항 · 완전 통과 {E(pas)}</span></div>'
                      f'<div class="rs-mscroll"><div class="rs-mat" style="grid-template-columns: 64px repeat({len(qs)}, minmax(20px, 1fr)); min-width: {64 + len(qs) * 24}px">'
                      + "".join(cells) + "</div></div></div>")
    legend = ('<div class="rs-legend"><span><i class="rs-sw rs-pass"></i>완전 통과</span><span><i class="rs-sw rs-fail"></i>FAIL (칸 숫자 = 어긴 기준 수)</span>'
              '<span><i class="rs-sw rs-hold"></i>보류</span><span class="muted">칸이나 위 질문 번호를 누르면 아래에 그 질문만 펼쳐집니다</span></div>')
    p1 = legend + "".join(blocks)
    # 탭 2 — 기준 72개 (CODE 세부 검사는 그 기준으로 합침), 묶음마다 접힘
    base_fail: Dict[str, Dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for (p, q), L in D["lines"].items():
        for cid in {i["item"] for i in L["items"] if i["decision"] == "FAIL"} | set(L["line"].get("code_fails") or []):
            base_fail[cid.split(":")[0]][p].add(q)
    ids = sorted(D["names"], key=lambda c: (0 if c[0].isdigit() else 1, c))
    groups: Dict[str, List[str]] = defaultdict(list)
    for cid in ids:
        groups[("CODE " + cid.split(".")[0]) if cid[0].isdigit() else cid.split("-")[0]].append(cid)
    gblocks = []
    for g, cids in groups.items():
        rows = []
        for c in cids:
            vals = [len(base_fail[c][p]) for p in order]
            tds = "".join(f'<td class="{"rs-worst" if v == max(vals) and v else "muted" if v == 0 else ""}">{v}</td>' for v in vals)
            rows.append(f'<tr><th scope="row"><b>{E(crit_name(D["names"], c))}</b><span class="rs-id">{E(c)}</span></th>{tds}</tr>')
        tot = sum(len(base_fail[c][p]) for c in cids for p in order)
        gname = g if g.startswith("CODE") else f'{g} {GROUP.get(g, "")}'
        heads = "".join(f"<th>{mname(p)}</th>" for p in order)
        gblocks.append(f'<details class="rs-d rs-g"><summary><b>{E(gname)}</b><span class="muted">기준 {len(cids)}개 · 어긴 질문 합 {tot}</span></summary>'
                       f'<table class="rs-ctab"><thead><tr><th></th>{heads}</tr></thead><tbody>{"".join(rows)}</tbody></table></details>')
    p2 = ('<p class="rs-basis">숫자 = 그 기준을 어긴 질문 수 · 빨강 = 가장 많이 어긴 모델. 묶음을 누르면 펼쳐집니다.</p>'
          '<div class="rs-gs">' + "".join(gblocks) + "</div>")
    # 탭 3 — FAIL 원문: 모델마다 질문 번호 → 그 질문의 원문 · 어긴 기준 · 사유 · 답에서 걸린 부분 (사용자 10-21: CODE 만 FAIL 표는 뺌)
    flist = ("".join(f'<li><a href="{E(sub)}/{E(f)}">FAIL 원문 {i}</a><span>{E(desc)}</span></li>' for i, (f, desc, _b) in enumerate(parts, 1))
             + "".join(f'<li><a href="{E(sub)}/{E(f)}">통과 원문 {i}</a><span>{E(desc)}</span></li>' for i, (f, desc, _b) in enumerate(pparts or [], 1)))
    jump = "".join(f'<div><b>{mname(p)}</b>' + "".join(f'<a href="{E(part_of[f"f-{p}-{q}"])}#f-{p}-{q}">Q{q}</a>'
                                                      for q in sorted(D["qtypes"]) if f"f-{p}-{q}" in part_of) + "</div>" for p in order)
    p3 = ('<h3 class="rs-sh">FAIL 원문 — 어긴 기준 · 사유 · 답에서 걸린 부분</h3>'
          '<p class="rs-basis">질문 번호를 누르면 그 FAIL 이 열립니다. 왼쪽은 질문·답변 원문이고, 답에서 심판이 짚은 문장은 빨간 번호로 칠해져 있습니다. '
          '오른쪽은 같은 번호의 어긴 기준과 사유입니다. 통과한 답은 질문별 결과에서 질문을 펼친 뒤 "원문 · 지킨 기준"으로, 또는 아래 통과 원문 파일에서 봅니다.</p>'
          f'<div class="rs-jump">{jump}</div>'
          f'<p class="rs-basis">파일로 보기 — 미리보기 한도(512KB) 때문에 <span class="rs-id">{E(sub)}/</span> 폴더에 나눠 두었습니다.</p>'
          f'<ul class="rs-files">{flist}<li><a href="{E(e_link)}">갈림 목록</a><span>심판이 갈려 Codex 를 따른 항목 {n_disagree:,}개 — 보류 아님, 확인용</span></li></ul>')
    tabs = [("apx1", "질문별 결과", f"{D['n']}문항 × {len(order)}모델", p1), ("apx2", "기준 72개", "묶음별 어긴 질문 수", p2),
            ("apx3", "원문", "FAIL · 통과 — 기준 · 사유 · 답", p3)]
    radios = "".join(f'<input type="radio" name="apx" id="{i}"{" checked" if k == 0 else ""}>' for k, (i, *_r) in enumerate(tabs))
    labels = "".join(f'<label for="{i}"><b>{E(t)}</b><span>{E(s)}</span></label>' for i, t, s, _p in tabs)
    panels = "".join(f'<div class="rs-panel rs-{i}">{p}</div>' for i, _t, _s, p in tabs)
    details = "".join(q_detail(D, q, part_of) for q in sorted(D["qtypes"]))
    return (f'<div class="rs-apx" id="apx">{radios}<div class="rs-tabs">{labels}</div><div class="rs-panels">{panels}</div></div>'
            f'<div class="rs-qds"><p class="rs-qhint">질문을 고르면 여기에 펼쳐집니다.</p>{details}</div>')


PREVIEW_LIMIT = 512 * 1000   # 앱 파일 창 미리보기 한도("512KB 를 넘어 파일 창에서 열었습니다", 사용자 10-21) — 1000 단위로 잡아 안전하게
PART_BUDGET = 380_000        # 원문 파일 하나에 담는 블록 바이트 — 덱 CSS(약 52KB)를 더해도 PREVIEW_LIMIT 아래


def fail_blocks(D: Dict[str, Any]) -> List[Tuple[str, int, str]]:
    """FAIL 질문마다 (모델, 질문, HTML) — 어긴 기준 · 심판 근거 · 어긴 턴의 질문·답변 원문."""
    out = []
    for p in D["order"]:
        for q in sorted(D["qtypes"]):
            L = D["lines"][(p, q)]
            if L["verdict"] != "FAIL":
                continue
            fails = [i for i in L["items"] if i["decision"] == "FAIL"]
            codes = code_fail_rows(D, p, q)
            holds_in = [i for i in L["items"] if i["decision"] == "UNVERIFIED"]
            k = len({i["item"] for i in fails}) + len({c["check_id"] for c in codes})
            # 사용자 10-21: 어떤 기준에서 · 왜 FAIL 인지 · 답의 어느 부분인지 — 원문 왼쪽, 같은 번호의 사유 오른쪽
            ents = ([{"turn": c["turn"], "item": c["check_id"], "kind": "CODE", "reason": c["reason"], "quote": "", "found": False} for c in codes]
                    + [{"turn": i["turn"], "item": i["item"], "kind": "의미", "reason": i["reason"] or "; ".join(i.get("problems") or []),
                        "quote": i.get("answer_quote") or "", "qquote": i.get("question_quote") or "", "how": i.get("how") or ""} for i in fails])
            ents.sort(key=lambda e: (e["turn"], e["kind"] != "의미"))
            for n, e in enumerate(ents, 1):
                e["n"] = n
            turns_q = D["qset"][q]["turns"]
            multi = len(turns_q) > 1
            left = [turn_html(D, p, q, tn, multi, [e for e in ents if e["turn"] == tn]) for tn in sorted({e["turn"] for e in ents})]
            right = "".join(fail_entry_html(D, e, multi) for e in ents)
            if holds_in:
                right += f'<details class="rs-d rs-inner"><summary>보류 항목 {len(holds_in)}개 — 이 질문은 FAIL 이라 결과에 영향 없음</summary>{"".join(item_html(D, i) for i in holds_in)}</details>'
            body = (f'<div class="rs-fx"><div class="rs-fx-l"><h4>원문{" (어긴 턴만)" if multi else ""} — 답에서 걸린 부분은 빨간 번호</h4>{"".join(left)}</div>'
                    f'<div class="rs-fx-r"><h4>왜 FAIL 인가 — 어긴 기준 {k}개</h4>{right}</div></div>')
            out.append((p, q, f'<article class="rs-fd" id="f-{p}-{q}"><h3 class="rs-fdh">Q{q} · {mname(p)} · {E(TYPE_NAME.get(D["qtypes"][q], ""))} · 어긴 기준 {k}개'
                              f'{" · 보류 항목 " + str(len(holds_in)) + "개" if holds_in else ""}</h3>{body}</article>'))
    return out


def split_parts(D: Dict[str, Any], blocks: List[Tuple[str, int, str]], prefix: str = "원문") -> List[Tuple[str, str, str]]:
    """블록을 순서대로 PART_BUDGET 씩 나눠 (파일 이름, 설명, 본문). 모델이 바뀌는 곳에 제목."""
    groups: List[List[Tuple[str, int, str]]] = [[]]
    size = 0
    for blk in blocks:
        b = len(blk[2].encode("utf-8"))
        if groups[-1] and size + b > PART_BUDGET:
            groups.append([])
            size = 0
        groups[-1].append(blk)
        size += b
    out = []
    for i, g in enumerate(groups, 1):
        desc, body, last = [], [], None
        for p in [x for x in D["order"] if any(b[0] == x for b in g)]:
            qs = [b[1] for b in g if b[0] == p]
            desc.append(f"{SHORT[p]} Q{qs[0]}~Q{qs[-1]} ({len(qs)}건)")
        for p, q, h in g:
            if p != last:
                last = p
            body.append(h)
        out.append((f"{prefix}{i}.html", " · ".join(desc), "".join(body)))
    return out


def fold(content: str) -> str:
    """원문 파일 — 질문 블록은 평소 숨김, 위 번호를 누르면(#f-…, CSS :target) 그것만 · '모두 펼치기' 체크로 전부 — JS 없이."""
    if 'class="rs-fd"' not in content:
        return content
    return ('<input type="checkbox" id="fall" class="rs-fall"><label for="fall" class="rs-fall-l">이 파일의 질문 모두 펼치기</label>'
            f'<div class="rs-fds"><p class="rs-qhint">위에서 질문 번호를 누르면 그 질문만 펼쳐집니다.</p>{content}</div>')


def jump_index(content: str) -> str:
    """원문 파일 맨 위 — 모델마다 질문 번호 바로가기 (누르면 그 질문이 열린 채로)."""
    by: Dict[str, List[str]] = defaultdict(list)
    for k, p, q in re.findall(r'<article class="rs-fd" id="([fp])-([a-z]+)-(\d+)"', content):
        by[p].append(f"{k}-{p}-{q}")
    if not by:
        return ""
    return ('<div class="rs-jump">' + "".join(f'<div><b>{mname(p)}</b>' + "".join(f'<a href="#{x}">Q{x.rsplit("-", 1)[1]}</a>' for x in qs) + "</div>" for p, qs in by.items())
            + "</div>")


def disagree_table(D: Dict[str, Any]) -> Tuple[int, str]:
    rows = []
    for p in D["order"]:
        for q in sorted(D["qtypes"]):
            for it in D["lines"][(p, q)]["items"]:
                if "갈림, Codex 따름" in (it["how"] or ""):
                    rows.append(f'<tr><td>Q{q}</td><td>{mname(p)}</td><td>{it["turn"]}</td><td><span class="rs-id">{E(it["item"])}</span></td><td>{E(it["reason"][:160])}</td><td class="muted">{E(it["how"])}</td></tr>')
    return len(rows), ('<div class="rs-scroll"><table class="rs-etab"><thead><tr><th>질문</th><th>모델</th><th>턴</th><th>항목</th><th>Codex 사유</th><th>심판</th></tr></thead><tbody>'
                       + "".join(rows) + "</tbody></table></div>")


# ------------------------------------------------------------------ 페이지 ------------------------------------------------------------------
EXTRA_CSS = """<style>
/* 결과 보고 (result-summary-v2) — 덱 CSS 위에 rs- 로 시작하는 것만 더함. 색은 덱 변수만(밝게·어둡게 자동) */
.slide { gap: 22px; }
.rs-sh { margin: 10px 0 0; font-size: 1.15rem; }
.rs-basis { margin: 0; font-size: 0.85rem; color: var(--muted); }
/* 표지 — 핵심 네 칸 · 말 뜻 */
.rs-tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.rs-tile { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 16px 18px; display: grid; gap: 6px; align-content: start; }
.rs-tile b { font-family: var(--font-num); font-size: 1.9rem; font-weight: 600; line-height: 1.1; }
.rs-tile b small { font-size: 1rem; color: var(--muted); font-weight: 400; }
.rs-tile span { font-size: 0.88rem; color: var(--muted); line-height: 1.45; }
.rs-tile.rs-key { border: 2px solid var(--accent); }
.rs-terms { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin: 0; }
.rs-terms div { border-top: 3px solid var(--line); padding-top: 8px; }
.rs-terms div.t-pass { border-color: var(--ok); } .rs-terms div.t-fail { border-color: var(--risk); } .rs-terms div.t-hold { border-color: var(--warn); }
.rs-terms dt { font-weight: 700; font-size: 0.95rem; } .rs-terms dd { margin: 3px 0 0; font-size: 0.86rem; color: var(--muted); line-height: 1.45; }
/* 01 흐름도 — 단계 카드 다섯 개, 사이에 화살표 */
.rs-flow { display: grid; grid-template-columns: minmax(0, 0.95fr) minmax(0, 1fr) minmax(0, 1.45fr) minmax(0, 1.15fr) minmax(0, 0.95fr); gap: 30px; align-items: stretch; }
.rs-st { position: relative; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 16px 16px 18px; display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.rs-st:not(:last-child)::after { content: ""; position: absolute; right: -20px; top: 50%; width: 11px; height: 11px; border-top: 3px solid var(--accent); border-right: 3px solid var(--accent); transform: translateY(-50%) rotate(45deg); }
.rs-st h3 { margin: 0; font-size: 1.02rem; }
.rs-sn { width: 26px; height: 26px; border-radius: 50%; background: var(--accent); color: var(--surface); display: inline-grid; place-items: center; font-family: var(--font-num); font-size: 0.85rem; font-weight: 600; }
.rs-sbig { font-family: var(--font-num); font-size: 2.4rem; font-weight: 600; line-height: 1; }
.rs-sbig small { font-family: var(--font-body); font-size: 0.95rem; color: var(--muted); font-weight: 500; margin-left: 4px; }
.rs-sbig.rs-sbig-t { font-family: var(--font-body); font-size: 1.3rem; font-weight: 700; }
.rs-chips { display: flex; flex-wrap: wrap; gap: 5px; }
.rs-chip { font-size: 0.8rem; border: 1px solid var(--line); background: var(--bg); border-radius: 999px; padding: 2px 9px; white-space: nowrap; }
.rs-chip b { font-family: var(--font-num); }
.rs-chip-note { flex-basis: 100%; font-size: 0.76rem; color: var(--muted); }
/* AIONA 자리 — 유형 칸(앞섬·공동·뒤짐) · 기준 앞선 곳/뒤진 곳 */
.rs-poss { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; }
.rs-pos { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; display: grid; gap: 2px; }
.rs-pos b { font-size: 0.92rem; } .rs-pos span { font-family: var(--font-num); color: var(--muted); } .rs-pos em { font-style: normal; font-size: 1.7rem; font-weight: 600; color: var(--ink); }
.rs-pos small { font-size: 0.8rem; font-weight: 600; }
.rs-pos-up { border-color: var(--ok); } .rs-pos-up small { color: var(--ok); }
.rs-pos-eq small { color: var(--accent); }
.rs-pos-down { border-color: var(--risk); } .rs-pos-down small { color: var(--risk); }
.rs-lr { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.rs-lrc { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 14px 18px; }
.rs-lrc h3 { margin: 0 0 8px; font-size: 1rem; }
.rs-lr-up { border-top: 4px solid var(--ok); } .rs-lr-down { border-top: 4px solid var(--risk); }
.rs-lrc ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
.rs-lrc li { display: grid; grid-template-columns: minmax(0, 1fr) auto; column-gap: 8px; font-size: 0.9rem; align-items: baseline; }
.rs-lrc li .rs-id { grid-column: 1; grid-row: 2; color: var(--muted); font-size: 0.76rem; }
.rs-lrc li > span:last-child { grid-column: 2; grid-row: 1 / span 2; font-family: var(--font-num); font-size: 0.85rem; text-align: right; white-space: nowrap; }
.rs-sl { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; font-size: 0.9rem; line-height: 1.45; }
.rs-sl b { display: block; font-size: 0.76rem; color: var(--muted); font-weight: 600; }
.rs-box { border: 1px solid var(--line); background: var(--bg); border-radius: 10px; padding: 10px 12px; display: grid; gap: 3px; font-size: 0.86rem; line-height: 1.45; }
.rs-box b { font-size: 1rem; } .rs-box em { font-style: normal; font-size: 0.78rem; color: var(--muted); }
.rs-rules { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }
.rs-rules li { display: grid; grid-template-columns: 12px minmax(0, 1fr); column-gap: 8px; align-items: center; }
.rs-rules li b { font-size: 0.95rem; } .rs-rules li span { grid-column: 2; color: var(--muted); font-size: 0.82rem; line-height: 1.4; }
.rs-dot { display: inline-block; width: 12px; height: 12px; border-radius: 50%; }
.rs-bypass { display: flex; align-items: center; gap: 10px; border: 1.5px dashed var(--warn); border-radius: 12px; padding: 10px 16px; font-size: 0.92rem; background: var(--warn-soft); }
.rs-chg { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; overflow: hidden; }
.rs-chg-h, .rs-chg-r { display: grid; grid-template-columns: 160px minmax(0, 1fr) 28px minmax(0, 1.4fr); gap: 12px; align-items: baseline; padding: 11px 18px; }
.rs-chg-h { background: var(--bg); font-size: 0.8rem; color: var(--muted); font-weight: 600; }
.rs-chg-r { border-top: 1px solid var(--line); font-size: 0.95rem; }
.rs-old { color: var(--muted); } .rs-arr { color: var(--accent); font-weight: 700; text-align: center; } .rs-new { font-weight: 600; }
.rs-new small { display: block; font-weight: 400; color: var(--muted); font-size: 0.8rem; margin-top: 2px; }
/* 02 결과 — 순위 막대 */
.rs-legend { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 0.85rem; align-items: center; }
.rs-sw { display: inline-block; width: 12px; height: 12px; border-radius: 3px; margin-right: 6px; vertical-align: -1px; border: 1px solid var(--line); }
.rs-rank { display: grid; gap: 10px; }
.rs-rk { display: grid; grid-template-columns: 26px 120px 112px minmax(0, 1fr) 48px; gap: 14px; align-items: center; background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px 16px; }
.rs-rn { font-family: var(--font-num); color: var(--muted); font-size: 1.1rem; }
.rs-rm b { display: block; font-size: 1.1rem; } .rs-rm span { font-size: 0.8rem; color: var(--muted); }
.rs-rv { font-family: var(--font-num); font-size: 2rem; font-weight: 600; line-height: 1; } .rs-rv small { font-size: 0.95rem; color: var(--muted); font-weight: 400; }
.rs-rx { font-family: var(--font-num); color: var(--muted); text-align: right; }
.rs-track { display: flex; gap: 2px; height: 30px; }
.rs-track i { display: flex; align-items: center; justify-content: center; font-style: normal; font-family: var(--font-num); font-size: 0.85rem; font-weight: 600; color: var(--surface); min-width: 2px; }
.rs-track i:first-child { border-radius: 5px 0 0 5px; } .rs-track i:last-child { border-radius: 0 5px 5px 0; }
i.rs-pass, .rs-sw.rs-pass { background: var(--ok); } i.rs-fail, .rs-sw.rs-fail { background: var(--risk); } i.rs-hold, .rs-sw.rs-hold { background: var(--warn); }
.rs-notes { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
/* 03·04·05 색 칸 */
.rs-heat { display: grid; gap: 6px; align-items: stretch; }
.rs-hh { font-size: 0.85rem; font-weight: 700; text-align: center; padding: 4px 0; align-self: end; }
.rs-hl { display: grid; align-content: center; gap: 1px; padding: 6px 4px; min-width: 0; }
.rs-hl b { font-size: 0.98rem; } .rs-hl span { font-size: 0.78rem; color: var(--muted); }
.rs-hc { border-radius: 10px; border: 1px solid var(--line); display: grid; place-items: center; padding: 10px 6px; min-height: 54px; font-family: var(--font-num); }
.rs-hc b { font-size: 1.3rem; font-weight: 600; } .rs-hc small { font-size: 0.8rem; color: var(--muted); } .rs-hc span { white-space: nowrap; }
.rs-hc-s { min-height: 44px; padding: 6px; } .rs-hc-s b { font-size: 1.1rem; }
.rs-hc.rs-best, .rs-hc.rs-worst-c { border: 2px solid var(--ink); }
.rs-hc.rs-sum { background: var(--bg); } .rs-hc.rs-sum b { color: var(--muted); font-size: 1rem; }
.rs-hc.rs-span { font-family: var(--font-body); font-size: 0.92rem; color: var(--muted); background: var(--bg); border-style: dashed; }
.rs-hnote { margin: 0; font-size: 0.86rem; color: var(--muted); }
.rs-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; }
.rs-card { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; display: grid; gap: 6px; align-content: start; }
.rs-card.rs-warn { background: var(--warn-soft); border-color: var(--warn); }
.rs-ct { font-size: 0.78rem; font-weight: 700; color: var(--accent); }
.rs-card > b { font-size: 0.98rem; }
.rs-cn span { font-family: var(--font-num); font-size: 1.8rem; font-weight: 600; color: var(--risk); } .rs-cn small { color: var(--muted); margin-left: 6px; font-size: 0.82rem; }
.rs-card p { margin: 0; font-size: 0.84rem; color: var(--muted); line-height: 1.45; }
/* 05 보류 */
.rs-hold-top { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.5fr); gap: 12px; }
.rs-hbig { background: var(--warn-soft); border: 1px solid var(--warn); border-radius: 14px; padding: 18px 20px; display: grid; gap: 6px; align-content: center; }
.rs-hbig b { font-family: var(--font-num); font-size: 2.8rem; line-height: 1; color: var(--warn); } .rs-hbig b small { font-size: 1.1rem; margin-left: 4px; }
.rs-hbig span { font-size: 0.95rem; }
.rs-facts { display: grid; gap: 8px; align-content: center; }
.rs-fact { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 10px 14px; font-size: 0.9rem; line-height: 1.45; }
.rs-fact b { font-family: var(--font-num); font-size: 1.15rem; margin-right: 8px; }
.rs-mx { gap: 5px; }
.rs-mq { display: grid; align-content: center; font-size: 0.85rem; padding: 2px; min-width: 0; }
.rs-mq b { font-family: var(--font-num); } .rs-mq span { color: var(--muted); font-size: 0.8rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rs-mc { border-radius: 8px; padding: 8px 6px; text-align: center; font-size: 0.84rem; border: 1px solid var(--line); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; align-self: stretch; display: grid; place-items: center; }
.rs-mc-empty { background: var(--surface); }
.rs-mc-copy { background: var(--todo-soft); color: var(--todo); }
.rs-mc-text { background: var(--warn-soft); color: var(--warn); border-color: var(--warn); }
.rs-mc-fact { background: var(--accent-soft); color: var(--accent); border-color: var(--accent); }
/* 06 시간 */
.rs-tbars { display: grid; gap: 10px; }
.rs-tb { display: grid; grid-template-columns: 70px minmax(0, 1fr) 300px; gap: 14px; align-items: center; }
.rs-bl { font-weight: 700; font-size: 0.95rem; }
.rs-tbar { height: 24px; background: var(--bg); border: 1px solid var(--line); border-radius: 5px; overflow: hidden; }
.rs-tbar i { display: block; height: 100%; background: var(--accent); }
.rs-tv b { font-family: var(--font-num); font-size: 1.05rem; } .rs-tv small { display: block; color: var(--muted); font-size: 0.8rem; }
.rs-judge { display: grid; grid-template-columns: 200px minmax(0, 1fr); gap: 18px; align-items: center; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 16px 18px; }
.rs-jt b { display: block; font-family: var(--font-num); font-size: 1.8rem; font-weight: 600; } .rs-jt span { font-size: 0.85rem; color: var(--muted); }
.rs-jb { display: grid; gap: 10px; min-width: 0; }
.rs-jeq { margin: 0; font-size: 0.86rem; color: var(--muted); line-height: 1.5; }
.rs-tl { display: flex; gap: 2px; height: 32px; }
.rs-tl i { display: flex; align-items: center; justify-content: center; font-style: normal; font-size: 0.82rem; font-weight: 600; overflow: hidden; white-space: nowrap; min-width: 3px; }
.rs-tl i:first-child { border-radius: 5px 0 0 5px; } .rs-tl i:last-child { border-radius: 0 5px 5px 0; }
.k1 { background: var(--k20); color: var(--surface); } .k2 { background: var(--k10); color: var(--surface); } .k3 { background: var(--k2); color: var(--surface); } .k4 { background: var(--k1); color: var(--ink); }
/* 부록 */
.rs-ah { margin: 18px 0 0; font-size: 1.1rem; }
.rs-d { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 8px 14px; margin: 6px 0; }
.rs-d > summary { cursor: pointer; font-weight: 600; }
.rs-d.rs-inner { background: var(--bg); }
.rs-d h4 { margin: 12px 0 4px; font-size: 0.95rem; }
.rs-item { border-top: 1px dashed var(--line); padding: 8px 0; font-size: 0.9rem; display: grid; gap: 3px; }
.rs-quote { color: var(--muted); font-size: 0.84rem; overflow-wrap: anywhere; }
.rs-two { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; margin: 8px 0; }
.rs-pre { white-space: pre-wrap; word-break: break-word; background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 10px; font: inherit; font-size: 0.85rem; max-height: 420px; overflow: auto; margin: 4px 0 0; }
.rs-scroll { overflow-x: auto; }
.rs-files { list-style: none; margin: 4px 0 0; padding: 0; display: grid; gap: 6px; font-size: 0.92rem; }
.rs-files li { display: grid; grid-template-columns: 90px minmax(0, 1fr); gap: 10px; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 8px 14px; }
.rs-files li a { font-weight: 700; } .rs-files li span { color: var(--muted); }
/* 부록 — 탭(라디오 + CSS, JS 없이) */
.rs-apx { display: grid; gap: 0; }
.rs-apx > input { position: absolute; opacity: 0; pointer-events: none; }
.rs-tabs { display: flex; flex-wrap: wrap; gap: 6px; border-bottom: 2px solid var(--line); }
.rs-tabs label { cursor: pointer; display: grid; gap: 1px; padding: 9px 16px; border: 1px solid var(--line); border-bottom: 0; border-radius: 10px 10px 0 0; background: var(--bg); color: var(--muted); margin-bottom: -2px; }
.rs-tabs label b { font-size: 0.95rem; } .rs-tabs label span { font-size: 0.76rem; }
#apx1:checked ~ .rs-tabs label[for=apx1], #apx2:checked ~ .rs-tabs label[for=apx2], #apx3:checked ~ .rs-tabs label[for=apx3] { background: var(--surface); color: var(--ink); border-color: var(--line); border-top: 3px solid var(--accent); border-bottom: 2px solid var(--surface); }
#apx1:focus-visible ~ .rs-tabs label[for=apx1], #apx2:focus-visible ~ .rs-tabs label[for=apx2], #apx3:focus-visible ~ .rs-tabs label[for=apx3] { outline: 2px solid var(--accent); outline-offset: 2px; }
.rs-panels { background: var(--surface); border: 1px solid var(--line); border-top: 0; border-radius: 0 0 14px 14px; padding: 16px 18px; }
.rs-panel { display: none; }
#apx1:checked ~ .rs-panels .rs-apx1, #apx2:checked ~ .rs-panels .rs-apx2, #apx3:checked ~ .rs-panels .rs-apx3 { display: grid; gap: 14px; }
/* 질문별 결과 — 유형마다 4모델 × 문항 칸 */
.rs-mb { display: grid; gap: 6px; }
.rs-mbh b { font-size: 0.95rem; } .rs-mbh span { color: var(--muted); font-size: 0.8rem; margin-left: 8px; }
.rs-mscroll { overflow-x: auto; padding-bottom: 2px; }
.rs-mat { display: grid; gap: 3px; }
.rs-qn { font-family: var(--font-num); font-size: 0.64rem; color: var(--muted); text-align: center; text-decoration: none; padding-bottom: 1px; }
.rs-qn:hover { color: var(--accent); }
.rs-mn { font-size: 0.8rem; font-weight: 600; align-self: center; }
.rs-t { height: 24px; border-radius: 4px; display: grid; place-items: center; font-family: var(--font-num); font-size: 0.72rem; font-weight: 600; text-decoration: none; color: var(--surface); }
.rs-t-pass { background: var(--ok); } .rs-t-fail { background: var(--risk); } .rs-t-hold { background: var(--warn); }
.rs-t:hover, .rs-t:focus-visible { outline: 2px solid var(--ink); outline-offset: 1px; }
/* 펼쳐지는 질문 — #q-n 일 때만 보임 */
.rs-qds { display: grid; gap: 10px; margin-top: 4px; }
.rs-qhint { margin: 0; font-size: 0.86rem; color: var(--muted); border: 1px dashed var(--line); border-radius: 10px; padding: 10px 14px; }
.rs-qds:has(.rs-qd:target) .rs-qhint { display: none; }
.rs-qd { display: none; background: var(--surface); border: 2px solid var(--accent); border-radius: 14px; padding: 16px 18px; scroll-margin-top: 76px; }
.rs-qd:target { display: grid; gap: 10px; }
.rs-qdh { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; }
.rs-qdh b { font-family: var(--font-num); font-size: 1.3rem; margin-right: 6px; } .rs-qdh span { color: var(--muted); font-size: 0.9rem; }
.rs-x { font-size: 0.85rem; text-decoration: none; border: 1px solid var(--line); border-radius: 8px; padding: 3px 10px; color: var(--ink); white-space: nowrap; }
.rs-qt { margin: 0; background: var(--bg); border-radius: 10px; padding: 10px 14px; font-size: 0.92rem; line-height: 1.55; white-space: pre-line; }
.rs-qms { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.rs-qm { border: 1px solid var(--line); border-radius: 12px; padding: 10px 14px; display: grid; gap: 6px; align-content: start; min-width: 0; }
.rs-qmh { display: flex; align-items: center; gap: 8px; } .rs-qmh > b { font-size: 1rem; min-width: 64px; }
.rs-ql { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
.rs-ql li { display: grid; gap: 1px; font-size: 0.86rem; border-top: 1px dashed var(--line); padding-top: 5px; }
.rs-ql li .rs-id { color: var(--muted); font-size: 0.74rem; }
.rs-why { color: var(--muted); font-size: 0.82rem; line-height: 1.45; margin: 0; }
.rs-qa { margin: 0; font-size: 0.88rem; font-weight: 600; }
.rs-go { font-size: 0.85rem; font-weight: 600; }
.rs-pre-s { max-height: 140px; font-size: 0.8rem; }
/* 기준 72개 — 묶음마다 접힘 */
.rs-gs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; align-items: start; }
.rs-g { margin: 0; } .rs-g > summary { display: flex; justify-content: space-between; gap: 8px; } .rs-g > summary span { font-weight: 400; font-size: 0.82rem; }
.rs-ctab { width: 100%; border-collapse: collapse; font-size: 0.84rem; margin-top: 8px; }
.rs-ctab th, .rs-ctab td { border-top: 1px solid var(--line); padding: 5px 6px; text-align: center; font-family: var(--font-num); }
.rs-ctab thead th { font-family: var(--font-body); font-size: 0.76rem; color: var(--muted); }
.rs-ctab tbody th { text-align: left; font-family: var(--font-body); font-weight: 400; display: grid; } .rs-ctab tbody th .rs-id { color: var(--muted); font-size: 0.72rem; }
/* 원문 파일 맨 위 바로가기 */
.rs-jump { display: grid; gap: 6px; background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 10px 14px; }
.rs-jump div { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; } .rs-jump b { min-width: 64px; font-size: 0.85rem; }
.rs-jump a { font-family: var(--font-num); font-size: 0.78rem; border: 1px solid var(--line); border-radius: 6px; padding: 1px 6px; text-decoration: none; }
.rs-fall { position: absolute; opacity: 0; pointer-events: none; }
.rs-fall-l { justify-self: start; cursor: pointer; font-size: 0.86rem; border: 1px solid var(--line); border-radius: 8px; padding: 5px 12px; background: var(--surface); }
.rs-fall:checked + .rs-fall-l { background: var(--accent); color: var(--surface); border-color: var(--accent); }
.rs-fall:focus-visible + .rs-fall-l { outline: 2px solid var(--accent); outline-offset: 2px; }
.rs-fds { display: grid; gap: 10px; }
.rs-fds:has(.rs-fd:target) .rs-qhint, .rs-fall:checked ~ .rs-fds .rs-qhint { display: none; }
.rs-fd { display: none; background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px 16px; scroll-margin-top: 16px; }
.rs-fd:target { display: block; border: 2px solid var(--accent); }
.rs-fall:checked ~ .rs-fds .rs-fd { display: block; }
.rs-fdh { margin: 0 0 6px; font-size: 1rem; }
.rs-id { font-family: var(--font-num); font-size: 0.86em; letter-spacing: 0; overflow-wrap: anywhere; }
.rs-worst { color: var(--risk); font-weight: 700; } .rs-strong { color: var(--ink); font-weight: 700; }
.rs-b { display: inline-block; border-radius: 999px; padding: 0 8px; font-size: 0.78rem; font-weight: 700; }
.rs-b.rs-pass { color: var(--ok); background: var(--ok-soft); } .rs-b.rs-fail { color: var(--risk); background: var(--risk-soft); } .rs-b.rs-hold { color: var(--warn); background: var(--warn-soft); }
.rs-failtxt { color: var(--risk); font-weight: 700; } .rs-holdtxt { color: var(--warn); font-weight: 700; }
tr.rs-dim th, tr.rs-dim td { color: var(--muted); }
.rs-etab { border-collapse: collapse; font-size: 0.82rem; width: 100%; }
.rs-etab th, .rs-etab td { border-top: 1px solid var(--line); padding: 4px 6px; text-align: left; vertical-align: top; }
/* 표: 넓은 화면은 칸 너비 고정, 좁은 화면은 줄마다 세로로 쌓고 칸 이름을 위에 (10-20 보고와 같은 규칙) */
.cmpt.steps { min-width: 0; max-width: 100%; }
.steps table { table-layout: fixed; }
.steps td, .steps th { vertical-align: top; overflow-wrap: anywhere; }
@media (max-width: 1100px) {
  .rs-flow { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
  .rs-st:not(:last-child)::after { content: none; }
  .rs-st:last-child { grid-column: 1 / -1; }
  .rs-tiles, .rs-terms { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 760px) {
  .rs-flow { grid-template-columns: minmax(0, 1fr); gap: 22px; }
  .rs-st:not(:last-child)::after { content: ""; right: auto; left: 50%; top: auto; bottom: -17px; transform: translateX(-50%) rotate(135deg); }
  .rs-chg-h { display: none; }
  .rs-chg-r { grid-template-columns: minmax(0, 1fr); gap: 2px; }
  .rs-arr { display: none; } .rs-new::before { content: "→ "; color: var(--accent); }
  .rs-rk { grid-template-columns: 20px minmax(0, 1fr) auto; row-gap: 8px; padding: 10px 12px; }
  .rs-rk .rs-track { grid-column: 1 / -1; } .rs-rx { display: none; } .rs-rv { font-size: 1.6rem; }
  .rs-notes, .rs-hold-top, .rs-lr { grid-template-columns: minmax(0, 1fr); }
  .rs-heat { gap: 4px; } .rs-hc b { font-size: 1rem; } .rs-hc small { font-size: 0.66rem; } .rs-hl b { font-size: 0.85rem; } .rs-hh { font-size: 0.68rem; }
  .rs-mc { font-size: 0.72rem; padding: 6px 2px; }
  .rs-tb { grid-template-columns: 56px minmax(0, 1fr); } .rs-tv { grid-column: 2; }
  .rs-judge { grid-template-columns: minmax(0, 1fr); }
  .rs-tiles, .rs-terms { grid-template-columns: minmax(0, 1fr); }
  .rs-two, .rs-qms, .rs-gs { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 640px) {
  .steps thead, .steps colgroup { display: none; }
  .steps table, .steps caption, .steps tbody, .steps tr, .steps th, .steps td { display: block; width: auto; }
  .steps tbody tr { border-top: 1px solid var(--line); padding: 10px 0; }
  .steps tbody th, .steps tbody td { border: 0; padding-block: 2px; }
  .steps td[data-label]::before { content: attr(data-label); display: block; font-size: 0.76rem; font-weight: 600; color: var(--muted); }
}
/* Arena 형 (v4) — 따뜻한 흰 바탕 · 세리프 제목 · 알약 탭 · 순위 막대 · 넓은 화면은 왼쪽 메뉴 (사용자 10-21: LM Arena 리더보드 틀) */
:root { --bg: #f8f7f4; --surface: #ffffff; --ink: #1c1b19; --muted: #6b6a65; --line: #e7e5df; --accent: #2f6fe4; --accent-soft: #eaf1fd; --ar-bar: #dce9fd; --ar-bar-top: #b4d1fb; --ar-up: #16a34a; --nav-bg: rgba(248, 247, 244, 0.94); --font-serif: "Noto Serif KR", "Nanum Myeongjo", "Times New Roman", serif; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg: #141413; --surface: #1d1d1b; --ink: #ecebe7; --muted: #a3a29c; --line: #2f2e2b; --accent: #7aa7f5; --accent-soft: #1e2a3f; --ar-bar: #213049; --ar-bar-top: #2c4572; --ar-up: #4ade80; --nav-bg: rgba(20, 20, 19, 0.94); } }
:root[data-theme="dark"] { --bg: #141413; --surface: #1d1d1b; --ink: #ecebe7; --muted: #a3a29c; --line: #2f2e2b; --accent: #7aa7f5; --accent-soft: #1e2a3f; --ar-bar: #213049; --ar-bar-top: #2c4572; --ar-up: #4ade80; --nav-bg: rgba(20, 20, 19, 0.94); }
.s-head h2, .ar-h1, .ar-ch h3, .ar-bh h3, .rs-sh, .brand { font-family: var(--font-serif); font-weight: 600; letter-spacing: -0.01em; }
.ar-h1, .s-head h2, .s-head p, .ar-hl p { word-break: keep-all; }
.s-no { color: var(--muted); }
@media (min-width: 1200px) {
  body { padding-left: 232px; }
  .topnav { position: fixed; left: 0; top: 0; bottom: 0; width: 232px; border-bottom: 0; border-right: 1px solid var(--line); background: var(--bg); backdrop-filter: none; }
  .topnav-in { height: 100%; box-sizing: border-box; flex-direction: column; align-items: stretch; max-width: none; padding: 20px 12px; gap: 16px; }
  .brand { display: block; font-size: 1.2rem; padding: 0 10px; }
  .toc-links { flex-direction: column; overflow: visible; gap: 2px; flex: 1; }
  .toc-links a { border-radius: 8px; padding: 8px 10px; font-size: 0.92rem; }
  .pager { padding: 0 10px; }
  html { scroll-padding-top: 16px; }
}
.ar-s0 { padding-top: 36px; padding-bottom: 8px; }
.ar-hero { display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(0, 1fr); gap: 36px; align-items: start; }
.ar-kick { font-size: 0.76rem; letter-spacing: 0.06em; color: var(--muted); font-weight: 600; }
.ar-h1 { font-size: 2.8rem; line-height: 1.18; margin: 8px 0 14px; }
.ar-h1 em { font-style: italic; color: var(--accent); }
.ar-hl p { color: var(--muted); font-size: 1.04rem; margin: 0 0 16px; max-width: 46ch; }
.ar-stat { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ar-stat b { font-family: var(--font-num); font-size: 1.45rem; } .ar-stat span { color: var(--muted); font-size: 0.88rem; }
.ar-live { width: 8px; height: 8px; border-radius: 50%; background: var(--ar-up); box-shadow: 0 0 0 4px color-mix(in srgb, var(--ar-up) 22%, transparent); flex: none; }
.ar-side { display: grid; }
.ar-side .ar-kick { padding: 0 6px 6px; }
.ar-sl { display: grid; grid-template-columns: 30px minmax(0, 1fr) 14px; gap: 10px; align-items: center; padding: 10px 6px; border-bottom: 1px solid var(--line); text-decoration: none; color: var(--ink); }
.ar-sl:hover { background: var(--surface); }
.ar-sl b { display: block; font-size: 1rem; } .ar-sl small { color: var(--muted); font-size: 0.82rem; } .ar-sl em { font-style: normal; color: var(--muted); font-size: 1.3rem; }
.ar-av.lg { width: 28px; height: 28px; border-radius: 8px; background-size: 72%; vertical-align: middle; }
.ar-r { position: absolute; opacity: 0; pointer-events: none; }
.ar-ov { display: grid; gap: 14px; }
.ar-pills { display: flex; flex-wrap: wrap; gap: 6px; position: sticky; top: 52px; z-index: 5; background: var(--bg); padding: 8px 0; }
.ar-pills label { cursor: pointer; padding: 8px 16px; border-radius: 12px; border: 1px solid transparent; color: var(--muted); font-size: 0.93rem; }
.ar-pills label:hover { color: var(--ink); }
.ar-grid { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); gap: 16px; align-items: start; }
.ar-card { background: var(--surface); border: 1px solid var(--line); border-radius: 16px; padding: 18px 20px; display: grid; gap: 14px; }
.ar-ch { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; } .ar-ch h3 { font-size: 1.25rem; }
.ar-chip { font-size: 0.78rem; border: 1px solid var(--line); border-radius: 999px; padding: 2px 10px; color: var(--muted); background: var(--bg); white-space: nowrap; }
.ar-pn { display: none; }
.ar-grid .ar-pn { margin-bottom: 0; }
.ar-grid .ar-pn > * + * { margin-top: 12px; }
.ar-rows { display: grid; gap: 6px; }
.ar-row { position: relative; display: grid; grid-template-columns: 24px 26px minmax(0, 1fr) auto 46px; gap: 10px; align-items: center; padding: 8px 10px; border-radius: 9px; isolation: isolate; }
.ar-row::before { content: ""; position: absolute; inset: 0 auto 0 0; width: var(--w); background: var(--ar-bar); border-radius: 9px; z-index: -1; }
.ar-row:first-child::before { background: var(--ar-bar-top); }
.ar-rank { width: 22px; height: 22px; border: 1px solid var(--line); background: var(--surface); border-radius: 5px; display: grid; place-items: center; font-family: var(--font-num); font-size: 0.78rem; }
.ar-name { font-weight: 600; } .ar-val { font-family: var(--font-num); font-size: 1.05rem; white-space: nowrap; } .ar-val small { color: var(--muted); font-weight: 400; font-size: 0.8rem; }
.ar-pct { font-family: var(--font-num); font-size: 0.84rem; color: var(--ar-up); text-align: right; }
.ar-note { margin: 0; font-size: 0.84rem; color: var(--muted); line-height: 1.5; }
.ar-btn { text-align: center; border: 1px solid var(--line); border-radius: 10px; padding: 9px; text-decoration: none; color: var(--ink); font-size: 0.92rem; background: var(--surface); }
.ar-btn:hover { border-color: var(--ink); }
.ar-top { margin-bottom: 20px; }
.ar-board { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 18px; align-items: start; }
.ar-cats { position: sticky; top: 64px; display: grid; gap: 12px; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 12px; }
.ar-cg { display: grid; gap: 2px; align-content: start; } .ar-cg > span { font-size: 0.74rem; color: var(--muted); font-weight: 600; padding: 2px 10px; }
.ar-cg label { cursor: pointer; padding: 6px 10px; border-radius: 8px; font-size: 0.9rem; color: var(--ink); }
.ar-cg label:hover { background: var(--bg); }
.ar-bh { display: grid; gap: 8px; margin-bottom: 14px; } .ar-bh > div { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; } .ar-bh h3 { font-size: 1.7rem; }
.ar-bh p { margin: 0; color: var(--muted); } .ar-meta { font-size: 0.8rem; color: var(--muted); font-family: var(--font-num); }
.ar-tab { width: 100%; border-collapse: separate; border-spacing: 0; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; overflow: hidden; }
.ar-tab th { text-align: left; font-size: 0.78rem; color: var(--muted); font-weight: 600; padding: 10px 14px; background: var(--bg); border-bottom: 1px solid var(--line); }
.ar-tab td { padding: 12px 14px; border-bottom: 1px solid var(--line); vertical-align: middle; }
.ar-tab tr:last-child td { border-bottom: 0; }
.ar-tr { font-family: var(--font-num); font-size: 1.05rem; width: 48px; text-align: center; }
.ar-tab th:first-child { width: 56px; white-space: nowrap; text-align: center; }
.ar-tm { display: flex; align-items: center; gap: 10px; } .ar-tm b { display: block; font-family: var(--font-num); font-weight: 600; } .ar-tm small { color: var(--muted); font-size: 0.78rem; }
.ar-tv { font-family: var(--font-num); font-size: 1.15rem; font-weight: 600; white-space: nowrap; } .ar-tv small { color: var(--muted); font-weight: 400; font-size: 0.8rem; }
.ar-tp2 { font-family: var(--font-num); color: var(--ar-up); }
.ar-tx { color: var(--muted); font-size: 0.84rem; }
@media (min-width: 1200px) { .ar-pills { top: 0; } .ar-cats { top: 16px; } }
@media (max-width: 900px) {
  .ar-hero, .ar-grid, .ar-board { grid-template-columns: minmax(0, 1fr); }
  .ar-cats { position: static; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
  .ar-h1 { font-size: 2.1rem; }
}
@media (max-width: 640px) {
  .ar-tab th:nth-child(5), .ar-tab td:nth-child(5) { display: none; } .ar-tm small { display: none; }
  .ar-row { grid-template-columns: 22px 24px minmax(0, 1fr) auto; } .ar-pct { display: none; }
  .ar-tab td, .ar-tab th { padding: 9px 8px; }
}
.hm { display: grid; grid-template-columns: minmax(0, 1fr); min-width: 0; }
.hm-ps { min-width: 0; }
.hm-tabs { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.hm-tabs label { cursor: pointer; padding: 5px 14px; border-radius: 999px; border: 1px solid var(--line); font-size: 0.88rem; color: var(--muted); background: var(--surface); }
.hm-ps { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 14px 16px; }
.hm-d { margin: 0 0 8px; font-size: 0.82rem; color: var(--muted); }
.hm-scroll { overflow-x: auto; }
.hm-tab { width: 100%; border-collapse: collapse; font-size: 0.95rem; }
.hm-tab th, .hm-tab td { padding: 9px 10px; text-align: left; border-bottom: 1px solid var(--line); white-space: nowrap; }
.hm-tab thead th { font-size: 0.8rem; color: var(--muted); font-weight: 600; }
.hm-tab thead th label { cursor: pointer; } .hm-tab thead th label:hover { color: var(--accent); }
.hm-m { display: inline-flex; align-items: center; gap: 8px; } .hm-tab tbody th { font-weight: 400; }
.hm-tab tbody tr:last-child th, .hm-tab tbody tr:last-child td { border-bottom: 0; }
.hm-tab td { font-family: var(--font-num); }
.hm-tab td label { cursor: pointer; display: inline-block; padding: 2px 6px; border-radius: 6px; } .hm-tab td label:hover { background: var(--accent-soft); color: var(--accent); }
.hm-tab td.hm-best { font-weight: 700; }
.hm-tab td.hm-l { font-family: var(--font-body); white-space: normal; min-width: 220px; }
/* 10-21 사용자: FAIL 원문 — 왼쪽 원문(걸린 부분 칠함) · 오른쪽 같은 번호의 사유 */
.rs-fx { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr); gap: 18px; align-items: start; margin-top: 6px; }
.rs-fx h4 { margin: 0 0 6px; font-size: 0.9rem; }
.rs-fx-r { position: sticky; top: 12px; max-height: calc(100vh - 24px); overflow: auto; }
.rs-turn { display: grid; gap: 2px; margin-bottom: 12px; }
.rs-fx .rs-pre.rs-q { max-height: 200px; }
.rs-fx .rs-pre.rs-ans { max-height: none; }
.rs-fwhy { font-size: 0.9rem; line-height: 1.5; }
mark.rs-mk { background: var(--risk-soft); color: inherit; border-bottom: 2px solid var(--risk); border-radius: 3px; padding: 0 1px; }
mark.rs-mk sup, .rs-hlno { display: inline-block; min-width: 1.4em; text-align: center; font-family: var(--font-num); font-size: 0.72rem; font-weight: 700;
  background: var(--risk); color: var(--surface); border-radius: 999px; padding: 0 5px; margin-right: 3px; line-height: 1.45; vertical-align: 0.1em; }
.rs-hlno.rs-hlno-off { background: var(--muted); }
.rs-kept { margin: 0; font-size: 0.82rem; color: var(--muted); line-height: 1.55; } .rs-kept b { color: var(--ok); margin-right: 6px; }
.rs-okno { color: var(--ok); font-weight: 700; } .rs-oktxt { color: var(--ok); font-weight: 700; } .rs-h4gap { margin-top: 14px !important; }
@media (max-width: 900px) { .rs-fx { grid-template-columns: minmax(0, 1fr); } .rs-fx-r { position: static; max-height: none; } }
/* 10-21 사용자: '확인할 것'에서 바로 간 자리 강조 · 카드의 질문 번호 · 좌우 여백 줄여 한 줄에 */
.rs-card[id], .rs-anchor { scroll-margin-top: 76px; }
.rs-card:target, .rs-anchor:target .note { outline: 2px solid var(--accent); outline-offset: 2px; }
.rs-anchor { margin-top: 18px; }
.rs-qchips { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 2px; }
.rs-qchips a { font-family: var(--font-num); font-size: 0.76rem; padding: 1px 7px; border: 1px solid var(--line); border-radius: 999px; color: var(--ink); text-decoration: none; }
.rs-qchips a:hover { border-color: var(--accent); color: var(--accent); }
.wrap { max-width: 1480px; }
@media (max-width: 1199px) { .topnav-in { max-width: 1480px; } }
.slide { word-break: keep-all; overflow-wrap: break-word; }
</style>"""


# 부록 A 의 FAIL·보류 링크 — 닫힌 묶음(details) 안에 있는 원문을 열고 그리로 간다
OPEN_JS = """<script>
(function () {
  function openTo(id) {
    var el = document.getElementById(id);
    if (!el || el.tagName !== 'DETAILS') return false;
    for (var d = el; d; d = d.parentElement && d.parentElement.closest('details')) d.open = true;
    el.scrollIntoView({ block: 'start' });
    return true;
  }
  document.addEventListener('click', function (e) {
    var a = e.target.closest('a[href^="#f-"], a[href^="#h-"]');
    if (a && openTo(a.getAttribute('href').slice(1))) { e.preventDefault(); history.replaceState(null, '', a.getAttribute('href')); }
  });
  if (/^#[fh]-/.test(location.hash)) openTo(location.hash.slice(1));
})();
</script>"""


def prune_css(css: str, body: str) -> str:
    """그 파일 본문에 없는 클래스만 쓰는 CSS 규칙을 뺀다 (본문 크기 줄이기, 10-21). 클래스 없는 규칙 · @ 규칙 머리는 그대로."""
    used = {c for v in re.findall(r'class="([^"]*)"', body) for c in v.split()}

    def keep(m: "re.Match[str]") -> str:
        cls = re.findall(r"\.([A-Za-z_][\w-]*)", m.group(1))
        return m.group(0) if not cls or m.group(1).lstrip().startswith("@") or any(c in used for c in cls) else ""
    # <style> 태그 안쪽만 — 태그 글자가 규칙 앞부분으로 잡혀 함께 지워지지 않게
    return re.sub(r"(<style[^>]*>)(.*?)(</style>)", lambda b: b.group(1) + re.sub(r"([^{}]+)\{[^{}]*\}", keep, b.group(2)) + b.group(3), css, flags=re.S)


def build(run_id: str) -> Tuple[List[Path], Dict[str, Any]]:
    D = load(run_id)
    deck = DECK.read_text(encoding="utf-8")
    css = "\n".join(f"<style>{b}</style>" for b in re.findall(r"<style>(.*?)</style>", deck, re.S))
    nav_script = deck[deck.index("<script>"):deck.index("</script>") + len("</script>")]
    # 원문은 따로 — 앱 미리보기 한도(512KB) 때문에 본문 파일에는 6장면 · 질문별 표 · 보류 원문 · 기준 72개만
    parts = split_parts(D, fail_blocks(D))
    pparts = split_parts(D, pass_blocks(D), prefix="통과")   # 통과한 답 — 원문 · 지킨 기준 (사용자 10-21)
    sub = f"ModelGrade_결과요약_{run_id}_원문"   # 원문 파일은 이 하위 폴더에 (사용자 10-21: 발표 폴더 정리)
    part_of = {m: f"{sub}/{f}" for f, _d, body in parts + pparts for m in re.findall(r'<article class="rs-fd" id="([fp]-[a-z]+-\d+)"', body)}
    e_file = "갈림.html"
    n_disagree, e_table = disagree_table(D)
    cats = board_categories(D)
    n_hold = sum(1 for L in D["lines"].values() if L["verdict"] == "HOLD")
    judge_s = sum(t.get("seconds") or 0 for t in judge_timing(D))
    scenes = [   # 사용자 10-21: 평가 방법을 개요 바로 다음에 · 걸린 시간은 순위표 다음에 — 장면 id(링크용)는 내용에 붙어 있고 번호(01…)는 이 순서로 매긴다
        ("s2", "평가 방법", "다섯 단계", "평가 흐름 — 다섯 단계",
         "어제 설명한 흐름 그대로이고, 의미 평가 심판만 Decisions API 로 바뀌었습니다.", scene_flow(D)),
        ("s1", "순위표", "분야별 순위", "분야별 순위표",
         "맨 위는 전체 순위, 그 아래 점수표에서 분야를 누르면 분야별 순위표가 바뀝니다.", rank_card(D, cats) + helm_table(D, cats) + '<h3 class="rs-sh">분야별 순위</h3>' + ar_board(D, cats) + wobble_note()),
        ("s5", "걸린 시간", "수집 · 판정", "걸린 시간 — 수집과 판정",
         f"답변 수집 {hm(collect_seconds(D))} · 판정 {hm(judge_s)}. 답변 수집은 모델마다 질문을 보내고 답을 받기까지 걸린 시간의 합(아래 막대) 중 가장 긴 것입니다.", scene_time(D)),
        ("s3", "FAIL 원인", "많이 어긴 기준", "FAIL 원인 — 많이 어긴 기준 10개",
         "숫자는 그 기준을 어긴 질문 수입니다. 색이 진할수록 많이 어겼습니다.", scene_criteria(D)),
        ("s4", "보류", f"사람이 정할 {n_hold}칸", f"보류 — 사람이 정할 {n_hold}칸",
         "보류는 FAIL 로 확정된 것은 없지만 심판이 정하지 않은 것입니다.", scene_hold(D)),
        ("s6", "질문별 결과", "78문항 × 4모델 · 원문", "질문별 결과 — 필요할 때만 펼치기",
         "탭을 고르고, 칸을 누르면 그 질문만 펼쳐집니다.", appendix(D, parts, part_of, f"{sub}/{e_file}", n_disagree, sub, pparts)),
    ]
    total = len(scenes)

    def head(no: str, title: str, lead: str) -> str:
        return f'<div class="s-head"><span class="s-no">{no} / {total:02d}</span><h2>{title}</h2>{f"<p>{lead}</p>" if lead else ""}</div>'

    s0 = f'<header class="ar-s0">{ar_overview(D)}</header>'   # 사용자 10-21: 제목 줄은 맨 위에 두고 메뉴의 개요는 뺌
    body = "".join(f'\n<section class="slide" id="{sid}">\n  {head(f"{i:02d}", E(title), E(lead))}\n{content}</section>\n'
                   for i, (sid, _s, _d, title, lead, content) in enumerate(scenes, 1))
    nav = ('<nav class="topnav" aria-label="순서"><div class="topnav-in"><span class="brand">ModelGrade</span>'
           '<div class="toc-links" id="toc-links">'
           + "".join(f'<a href="#{sid}"><span class="n">{i:02d}</span>{E(short)}</a>' for i, (sid, short, *_r) in enumerate(scenes, 1))
           + '</div><div class="pager"><button type="button" id="prev" aria-label="이전 장">←</button><button type="button" id="next" aria-label="다음 장">→</button></div></div></nav>')
    ids = ", ".join(f"'{s}'" for s in [x[0] for x in scenes])
    script = re.sub(r"var ids = \[[^\]]*\];", f"var ids = [{ids}];", nav_script, count=1)
    logos = logo_style()

    def doc(title: str, desc: str, inner: str) -> str:
        return (f'<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                f'<title>{E(title)}</title>\n<meta name="description" content="{E(desc)}">\n'
                '<link rel="preconnect" href="https://fonts.googleapis.com">\n<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
                '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+KR:wght@400;500;600;700&family=Noto+Serif+KR:wght@500;600;700&display=swap">\n'
                f'{prune_css(css + chr(10) + EXTRA_CSS + chr(10) + logos, inner)}\n</head>\n<body>\n{inner}\n{OPEN_JS}\n</body>\n</html>\n')

    main_name = f"ModelGrade_결과요약_{run_id}.html"
    out = OUT_DIR / main_name
    out.write_text(doc("AIONA 채팅 품질 평가", f"78개 실행 {run_id} — 흐름도 · 결과 · 유형별 · 많이 어긴 기준 · 보류 · 걸린 시간 · 부록",
                       f'{nav}\n<main class="wrap">\n{s0}\n{body}\n</main>\n{script}'), encoding="utf-8")
    subdir = OUT_DIR / sub
    subdir.mkdir(exist_ok=True)
    # 지난번에 만든 원문 파일 — 예전에는 발표 폴더에 바로 두었고(…_원문1.html), 나눈 수도 바뀔 수 있음
    for old in [*OUT_DIR.glob(f"ModelGrade_결과요약_{run_id}_원문*.html"), *subdir.glob("*.html")]:
        old.unlink()
    files = ([(f, f"FAIL 원문 {i}", d) for i, (f, d, _b) in enumerate(parts, 1)] + [(f, f"통과 원문 {i}", d) for i, (f, d, _b) in enumerate(pparts, 1)]
             + [(e_file, "갈림 목록", f"심판이 갈려 Codex 를 따른 항목 {n_disagree:,}개")])

    def links(cur: str) -> str:
        return (f'<a href="../{E(main_name)}#apx">← 결과 요약(부록)으로</a> · ' + " · ".join(
            f"<b>{E(lab)}</b>" if f == cur else f'<a href="{E(f)}">{E(lab)}</a>' for f, lab, _d in files))

    written = [out]
    for f, lab, d in files:
        content = next((b for pf, _d, b in parts + pparts if pf == f), None)
        if content is None:   # 갈림 목록
            content = f'<p class="rs-basis">Decisions 는 FAIL, Codex 는 PASS·해당 없음이라 Codex 를 따른 항목입니다. 보류가 아니고 숫자도 바꾸지 않습니다(확인용).</p>{e_table}'
        inner = (f'<main class="wrap"><section class="slide" id="s0"><div class="s-head"><span class="s-no">부록 · {E(lab)} / 결과 요약 {E(run_id)}</span>'
                 f'<h2>{E(lab)} — {E(d)}</h2><p>{links(f)}</p></div>{jump_index(content)}{fold(content)}</section></main>')
        (subdir / f).write_text(doc(f"결과 요약 {lab}", f"78개 실행 {run_id} 부록 — {d}", inner), encoding="utf-8")
        written.append(subdir / f)
    return written, D


def check_against_report(D: Dict[str, Any]) -> List[str]:
    """그 실행의 report.html 1장(완전 통과 · FAIL · 보류)과 맞춰 본다 — 다르면 줄마다 알림."""
    f = RESULTS / f"판정_{D['run_id']}" / "report.html"
    if not f.exists():
        return [f"{f} 없음 — 맞춰 보지 못함"]
    txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", f.read_text(encoding="utf-8"))))
    found = re.findall(r"(\d+) / \d+ 완전 통과 [\d.]+% · FAIL (\d+) · 보류 (\d+)", txt)
    want = [(D["tally"][p]["PASS"], D["tally"][p]["FAIL"], D["tally"][p]["HOLD"]) for p in R.ORDER if p in D["tally"]]
    got = [tuple(int(x) for x in row) for row in found[:len(want)]]
    return [] if got == want else [f"report.html 과 다름: 보고서 {got} · 이 페이지 {want}"]


def main(argv: List[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", default=RUN, help="실행 id (참고자료/결과/<id>_결과_판정결과.json)")
    a = ap.parse_args(argv)
    written, D = build(a.run)
    big = []
    for f in written:
        size = f.stat().st_size
        print("저장:", f.name, f"{size:,} bytes")
        if size > PREVIEW_LIMIT:
            big.append(f"{f.name} 이(가) {size:,} bytes — 앱 미리보기 한도 {PREVIEW_LIMIT:,} 를 넘음")
    print("모델별 완전 통과·FAIL·보류:", {SHORT[p]: (D['tally'][p]['PASS'], D['tally'][p]['FAIL'], D['tally'][p]['HOLD']) for p in D["order"]})
    problems = check_against_report(D) + big
    print("report.html 과 같음 · 파일마다 미리보기 한도 안" if not problems else "\n".join(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
