"""결과 요약 발표 페이지 (result-summary-v1) — 78개 실행 하나를 발표 6장면 + 부록으로 (사용자 10-21: report.html 은 정보가 너무 많아
발표하기 어려움 → 10.20 미팅 기준으로 흐름도 · 결과 · 유형별 · 많이 어긴 기준 · 보류 · 걸린 시간, 원문은 부록).
AI 호출 없음. 내보낸 결과(참고자료/결과/<실행>_결과_판정결과.json)와 질문 세트만 읽고, 판정 셈은 tools/report_html 과 같은 함수를 쓴다
(숫자가 그 실행의 report.html 과 같아야 한다 — 끝에서 맞춰 본다). 디자인은 10-19 발표 덱 CSS 를 그대로 읽어 쓴다(10-20 보고와 같은 모양).

    python -X utf8 tools/build_result_summary.py [--run a1642702fccc]
    → 참고자료/발표/ModelGrade_결과요약_10-20 발표자료.html (원문은 ModelGrade_결과요약_<실행>_원문/)
"""
from __future__ import annotations

import argparse
import html
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import report_html as R  # noqa: E402
from tools.result_flow import FLOW_CSS, WARN as FLOW_WARN, flow_scene  # noqa: E402

VERSION = "result-summary-v4"   # v2 (10-21 오후): 흐름도·장면을 그림 위주로 — 멘토·소장님 발표용
RUN = "a1642702fccc"
RESULTS = ROOT / "참고자료" / "결과"
OUT_DIR = ROOT / "참고자료" / "발표"
MAIN_NAME = "ModelGrade_결과요약_10-20 발표자료.html"   # 본문 파일 이름 (사용자 10-21: 고치면 이 파일에 바로, 따로 파일 만들지 않음)
DECK = OUT_DIR / "ModelGrade_진행보고_2026-10-19.html"
E = html.escape
SHORT = {"aiona": "AIONA", "gpt": "GPT", "claude": "Claude", "gemini": "Gemini"}
# 미팅(10.20)에서 정한 이름 — 싱글턴 · 멀티턴 · 롱컨텍스트
TYPE_NAME = {"단일 질문": "싱글턴", "10턴 멀티턴 대화": "멀티턴", "첨부자료 확인·요약": "첨부 자료",
             "롱컨텍스트": "롱컨텍스트", "명시적 빈 출력": "싱글턴 · 빈 출력 요구"}
KIND_LABEL = {"CODE": "CODE 검사", "의미": "LLM Judge"}   # 10-21 사용자: 페이지 전체 'CODE 검사 · LLM Judge' (오후 미팅 때 '규칙 검사 · AI 판정' 대신)
GROUP = {"J1": "요청 충실", "J2": "지시 준수", "J3": "일관성·논리", "J4": "완결성", "J5": "DATA 신뢰도",
         "J6": "대화 맥락 유지", "J7": "긴 문서 활용", "J8": "첨부 활용"}
SUFFIX = {"bullet_count": "불릿 수", "required": "필수", "count": "개수", "block1": "첫 코드블록", "required_literal": "필수 문구",
          "present": "있어야 함", "1": "첫 등식"}


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
    # 표시 유형 — 빈 출력 요구는 싱글턴에 합쳐 보여 준다 (10-21 오후 미팅: 카테고리를 따로 빼지 않음). 보류 규칙은 원래 유형(qtypes)으로
    dtypes = {q: ("단일 질문" if t == "명시적 빈 출력" else t) for q, t in qtypes.items()}
    return {"d": d, "run_id": run_id, "set_dir": set_dir, "qtypes": qtypes, "dtypes": dtypes, "names": names, "qset": qset, "lines": lines,
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
EMPTY_WHY = "빈 출력 요구 질문 — 채점하지 않고 바로 보류, 사람이 정함"


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
ROW_ORDER = ["단일 질문", "10턴 멀티턴 대화", "롱컨텍스트", "첨부자료 확인·요약"]   # 10-21 오후 미팅: 싱글턴 · 멀티턴 · 롱컨텍스트 · 첨부, 빈 출력 요구는 싱글턴에


def hm(seconds: float) -> str:
    m = int(round((seconds or 0) / 60))
    return f"{m // 60}시간 {m % 60}분" if m >= 60 else f"{m}분"


def tint(r: float, var: str, cap: int = 42) -> str:
    """칸 배경 — 값이 클수록 진하게(한 색을 표면색과 섞음). 글자는 늘 본문 색."""
    return f"background: color-mix(in srgb, var({var}) {max(0.0, min(1.0, r)) * cap:.0f}%, var(--surface));"


#: 페이지에 보이는 모델 이름 — 사용자 10-21: 모순을 고친 뒤 다시 돌릴 평가는 Gemini 3.8 Flash 라 표기를 미리 3.8 로 (10-21 밤 다시 확인: 3.8 로 둠).
#: 결과 JSON 의 수집 기록(a1642702fccc 는 3.6 Flash)과 report.html 짝 맞추기(by_label)는 그대로 둔다.
#: 10-21 밤부터 Gemini 는 데스크톱 앱으로 받으므로, 덮어쓰기는 웹브라우저로 받은 실행에만 쓴다.
MODEL_SHOWN = {"gemini": "Gemini 웹브라우저 (3.8 Flash · 사고 보통)"}


def model_label(D: Dict[str, Any], p: str) -> str:
    raw = D["d"]["models"].get(p, "")
    return MODEL_SHOWN[p] if p in MODEL_SHOWN and "웹" in raw else raw


def method_of(D: Dict[str, Any], p: str) -> str:
    lab = D["d"]["models"].get(p, "")
    return "데스크톱 앱" if "앱" in lab else "웹브라우저" if "웹" in lab else ""


def turn_ok(D: Dict[str, Any], qs: List[int]) -> Tuple[Dict[str, int], int, Dict[str, int]]:
    """대화 질문들의 턴 가운데 FAIL(의미 항목 · CODE)이 없는 턴 수 · 전체 턴 수 · 그중 보류 항목이 남은 턴 수(참고 지표라 보류 턴도 셈 — 받은 의견 C)."""
    ok, tot, held = {}, 0, {}
    for p in D["order"]:
        o = t = h = 0
        for q in qs:
            L = D["lines"][(p, q)]
            turns = set(range(1, len(D["qset"][q]["turns"]) + 1))
            bad = {i["turn"] for i in L["items"] if i["decision"] == "FAIL"} | {c["turn"] for c in code_fail_rows(D, p, q)}
            hold = {i["turn"] for i in L["items"] if i["decision"] == "UNVERIFIED"}
            o += len(turns - bad)
            h += len((turns - bad) & hold)
            t += len(turns)
        ok[p], tot, held[p] = o, t, h
    return ok, tot, held


def scene_flow(D: Dict[str, Any]) -> str:
    types = Counter(D["dtypes"].values())
    chip_name = {"첨부자료 확인·요약": "첨부"}
    chips = "".join(f'<span class="rs-chip">{E(chip_name.get(t, TYPE_NAME.get(t, t)))} <b>{types[t]}</b></span>' for t in ROW_ORDER if types[t])
    n_empty = sum(1 for t in D["qtypes"].values() if t == "명시적 빈 출력")
    chips += (f'<span class="rs-chip-note">싱글턴에 빈 출력 요구 {n_empty}문항 포함 · 롱컨텍스트 = 27만 토큰 이상 문서</span>' if n_empty
              else '<span class="rs-chip-note">롱컨텍스트 = 27만 토큰 이상 문서</span>')
    app = " · ".join(mname(p) for p in D["order"] if method_of(D, p) == "데스크톱 앱")
    web = " · ".join(mname(p) for p in D["order"] if method_of(D, p) == "웹브라우저")
    flow = (
        '<div class="rs-flow">'
        f'<div class="rs-st"><span class="rs-sn">1</span><h3>질문</h3><div class="rs-sbig">{D["n"]}<small>개</small></div><div class="rs-chips">{chips}</div></div>'
        f'<div class="rs-st"><span class="rs-sn">2</span><h3>답변 수집</h3><div class="rs-sbig">{len(D["order"])}<small>모델 · 늘릴 수 있음</small></div>'
        f'<ul class="rs-sl"><li><b>데스크톱 앱</b>{app}</li><li><b>웹브라우저</b>{web}</li><li class="muted">복사 버튼으로 받아 JSON 으로 저장</li>'
        '<li class="rs-ext">+ 우수AI · 팩트챗 등 다른 AI 서비스도 앱·웹브라우저 자동화로 연결해 같은 질문으로 비교 가능</li></ul></div>'
        '<div class="rs-st"><span class="rs-sn">3</span><h3>채점 — 기준 72개</h3>'
        '<div class="rs-box"><b>CODE 검사 28</b><span>형식 · 개수 · 글자 수 · 계산</span><em>정해진 규칙으로 정확히 셈</em></div>'
        '<div class="rs-box"><b>LLM Judge 44</b><span>요청 충실 · 지시 준수 · 논리 · 사실</span>'
        + ('<em>질문에 정한 기준만 · 입력 3만 2천 토큰 이하(첨부 없음) Jev · 그 밖 Decisions → FAIL · 애매한 항목만 Codex 가 확정 · 블라인드</em>' if three_judges(D) else
           '<em>질문에 정한 기준만 · 블라인드</em><em class="rs-jnow"><b>이번 결과</b> Decisions 가 모두 판정 → FAIL · 애매한 항목만 Codex 가 확정</em>'
           '<em class="rs-jnext"><b>다음 평가</b> 입력 3만 2천 토큰 이하 Jev · 넘으면 Decisions → FAIL 로 본 항목만 Codex</em>')   # 받은 의견 E: 이번 방식과 다음 구조를 나눔
        + '</div></div>'
        '<div class="rs-st"><span class="rs-sn">4</span><h3>질문 판정</h3><ul class="rs-rules">'
        '<li><i class="rs-dot rs-hold"></i><b>보류</b><span>확인 못 한 기준이 하나라도 남음</span></li>'
        '<li><i class="rs-dot rs-fail"></i><b>FAIL</b><span>보류 없이 기준을 하나라도 어김</span></li>'
        '<li><i class="rs-dot rs-pass"></i><b>완전 통과</b><span>적용된 기준을 모두 통과 (FAIL · 보류 없음)</span></li></ul></div>'
        f'<div class="rs-st"><span class="rs-sn">5</span><h3>결과</h3><div class="rs-sbig rs-sbig-t">완전 통과<small>/ {D["n"]}</small></div>'
        '<ul class="rs-sl"><li>모델마다 질문 수로 비교</li><li>보류는 사람이 정함</li></ul></div>'
        '</div>')   # 사용자 10-21: 빈 출력 안내 줄 · '어제 설명에서 바뀐 점' 표 지움
    # 동작 구조 줄(CLI → … → 보고서)은 뺌 — 바로 뒤 전체 흐름도와 겹침, CLI 한 번 실행 · 걸린 시간은 01 '무엇 · 왜'와 걸린 시간 장면에 (사용자 10-21 밤)
    return flow + criteria_list(D)


#: CODE 묶음 이름 — 10-19 발표 '평가 기준 72개' 장의 세 갈래(1 출력 무결성 · 2 형식·지시 준수 · 3 계산·자료 처리)를 묶음마다 풀어 씀
CODE_TOP = {"1": "출력 무결성", "2": "형식 · 지시 준수", "3": "계산 · 자료 처리"}
CODE_GROUP = {"1-1": "응답 · 문자", "1-2": "코드 · 구문", "1-3": "인용 · 링크", "2-1": "글자 수 · 개수 · 문자열",
              "2-2": "제목 · 목록 · 표 · JSON 구조", "3-1": "계산", "3-2": "선언한 개수", "3-3": "집계 · 변환"}
#: 언제 보나 — criteria_catalog.json 의 always_ids · answer_dependent_ids (나머지는 질문이 요구할 때)
WHEN = {"always": ("항상", "cr-al"), "answer": ("답에 있으면", "cr-an"), "asked": ("요구 시", "cr-as")}


def criteria_list(D: Dict[str, Any]) -> str:
    """채점 기준 72개 — 위에 늘 보이는 기준 지도(묶음 · 개수 · 언제 보나), 아래 접힌 전체 목록
    (10-21 오후 미팅 46:57: 처음 보는 사람이 기준을 궁금해함 · 사용자 10-21 저녁: 더 읽기 쉽고 직관적으로)."""
    f = Path(D["set_dir"]) / "criteria_catalog.json"
    cat = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    codes = (cat.get("catalog") or {}).get("code") or []
    items = (cat.get("catalog") or {}).get("items") or []
    always, by_answer = set(cat.get("always_ids") or []), set(cat.get("answer_dependent_ids") or [])
    when = lambda cid: "always" if cid in always else "answer" if cid in by_answer else "asked"
    badge = lambda k: f'<i class="cr-w {WHEN[k][1]}">{WHEN[k][0]}</i>'
    n_when = Counter(when(c["id"]) for c in codes + items)

    def first_line(s: str, n: int = 70) -> str:
        """예시 질문의 첫 문장만 — 붙어 있는 코드 구획(```) · 표(|) · 다음 줄은 뺀다."""
        s = re.split(r"\n|```|\|", s)[0].strip()
        return s[:n] + ("…" if len(s) > n else "")

    def split_note(s: str) -> Tuple[str, str]:
        """'…했는가 (보충)' → ('…했는가', '보충') — 괄호 속 보충 설명은 흐리게."""
        m = re.match(r"^(.*?)\s*\((.*)\)\s*$", s.strip())
        return (m.group(1), m.group(2)) if m and m.group(1) else (s.strip(), "")

    # CODE — 세 갈래 · 여덟 묶음
    cgroups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in sorted(codes, key=lambda c: c["id"]):
        cgroups[c["id"].split(".")[0]].append(c)
    # LLM Judge — J1~J8, 같은 이름(J1-4.a · b)은 하나로 묶어 a · b 를 그 아래에
    jgroups: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for it in sorted(items, key=lambda i: i["id"]):
        jgroups[it["id"].split("-")[0]][it.get("sub_id") or it["id"]].append(it)

    # 기준 지도 — 늘 보임
    top_rows = "".join(f'<div class="cr-top"><span class="cr-tn">{k} {E(v)}</span><span class="cr-chips">'
                       + "".join(f'<span class="cr-chip">{g} {E(CODE_GROUP.get(g, ""))} <b>{len(cgroups[g])}</b></span>' for g in cgroups if g.startswith(k))
                       + "</span></div>" for k, v in CODE_TOP.items())
    j_chips = "".join(f'<span class="cr-chip">{g} {E(GROUP.get(g, ""))} <b>{sum(len(v) for v in subs.values())}</b></span>' for g, subs in jgroups.items())
    overview = (f'<div class="cr-map">'
                f'<div class="cr-half"><h4>CODE 검사 <b>{len(codes)}</b><span>프로그램이 정해진 규칙으로 셈</span></h4>{top_rows}</div>'
                f'<div class="cr-half"><h4>LLM Judge <b>{len(items)}</b><span>AI 심판이 답을 읽고 판단</span></h4><div class="cr-chips">{j_chips}</div></div>'
                f'<div class="cr-when"><span class="cr-wl">언제 보나</span>{badge("always")}<span>모든 질문 <b>{n_when["always"]}</b></span>'
                f'{badge("asked")}<span>표·글자 수·말투처럼 질문에 그 요구가 있을 때 <b>{n_when["asked"]}</b></span>'
                f'{badge("answer")}<span>답이 인용·링크·등식 등을 썼을 때 <b>{n_when["answer"]}</b></span></div></div>')

    # 전체 목록 — 접힘, 두 단으로 흘림
    cblocks, jblocks = [], []
    for g, cs in cgroups.items():
        lis = "".join(f'<li><div class="cr-h">{badge(when(c["id"]))}<b>{E(c.get("name") or c["id"])}</b><span class="cr-id">{E(c["id"])}</span></div>'
                      + (f'<p class="cr-ex">예: {E(first_line(c["example"]))}</p>' if c.get("example") else "") + "</li>"
                      for c in cs)
        cblocks.append(f'<div class="cr-g cr-g-code"><h5><span class="cr-gk">{g}</span>{E(CODE_GROUP.get(g, ""))}<span class="cr-n">{len(cs)}개 · {E(CODE_TOP.get(g[0], ""))}</span></h5><ul>{lis}</ul></div>')
    for g, subs in jgroups.items():
        lis = []
        for sub_id, its in subs.items():
            checks = ""
            for it in its:
                main, note = split_note(it.get("question") or "")
                ab = f'<span class="cr-ab">{E(it["id"].split(".")[-1])}</span>' if len(its) > 1 else ""
                checks += (f'<div class="cr-chk">{ab}<span class="cr-q">{E(main)}{f"<small>{E(note)}</small>" if note else ""}</span>'
                           f'<span class="cr-id">{E(it["id"])}</span></div>')
            lis.append(f'<li><div class="cr-h">{badge(when(its[0]["id"]))}<b>{E(its[0].get("sub") or sub_id)}</b></div>{checks}</li>')
        n = sum(len(v) for v in subs.values())
        jblocks.append(f'<div class="cr-g cr-g-llm"><h5><span class="cr-gk">{g}</span>{E(GROUP.get(g, ""))}<span class="cr-n">{n}개</span></h5><ul>{"".join(lis)}</ul></div>')
    # 사용자 10-22: CODE 검사 · LLM Judge 를 두 구역 · 두 색으로 — 섞여 흐르지 않게
    sec = lambda k, name, n, what, blocks: (f'<section class="cr-sec cr-sec-{k}"><h4 class="cr-sh"><span class="cr-kind">{name}</span><b>{n}개</b>'
                                            f'<span>{E(what)}</span></h4><div class="cr-list">{"".join(blocks)}</div></section>')
    return (overview
            + f'<details class="rs-d rs-crit"><summary><b>채점 기준 {len(codes) + len(items)}개 전체</b><span class="muted">CODE 검사 {len(codes)} · LLM Judge {len(items)} — 묶음마다 무엇을 보는지 · 언제 보는지 — 누르면 펼침</span></summary>'
            + sec("code", "CODE 검사", len(codes), "프로그램이 정해진 규칙으로 셉니다 — 글자 수 · 개수 · 형식 · 코드 문법 · 계산처럼 맞고 틀림이 분명한 것", cblocks)
            + sec("llm", "LLM Judge", len(items), "AI 심판이 답을 읽고 판단합니다 — 요청 수행 · 지시 준수 · 논리 · 사실 · 대화 기억처럼 읽어야 아는 것", jblocks)
            + "</details>")


def scene_criteria(D: Dict[str, Any]) -> str:
    order = D["order"]
    cf = {c: v for c, v in crit_fail(D).items() if not c[0].isdigit()}   # 사용자 10-22: 'LLM Judge 가 많이 어긴 기준' — CODE 검사는 CODE 검사 장면
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


#: 01장 용어 풀이 — 프로젝트를 모르는 사람에게 (사용자 10-21 밤). (용어, 풀이, 색 표시 class)
TERMS = [
    ("질문 · 턴 · 칸", "질문은 AI 에게 보내는 요청 하나. 멀티턴은 한 대화에서 10번(10턴) 주고받고, 대화 하나를 질문 1개로 셉니다. 칸은 모델 하나 × 질문 하나의 결과(78 × 4 = 312칸).", ""),
    ("싱글턴 · 멀티턴 · 롱컨텍스트 · 첨부", "한 번 묻고 답 · 10턴 대화 · 아주 긴 문서(27만 토큰 이상)를 읽고 답 · 파일(PDF · 워드 · 그림 · 글)을 읽고 답.", ""),
    ("기준", "답이 지켜야 할 점검 항목. 모두 72개 — 질문마다 그 질문에 해당하는 기준만 봅니다.", ""),
    ("CODE 검사", "프로그램이 정해진 규칙으로 세는 기준 28개 — 글자 수 · 개수 · 표 · 코드 문법 · 계산처럼 맞고 틀림이 분명한 것.", ""),
    ("LLM Judge (AI 심판)", "AI 가 답을 읽고 판단하는 기준 44개 — 요청을 했는지 · 지시를 지켰는지 · 논리 · 사실 · 대화 기억 등. 어느 AI 의 답인지 모르게(블라인드) 채점합니다.", ""),
    ("심판 셋", "Jev · Decisions(외부 AI 판정 API)가 먼저 보고, FAIL 이거나 애매한 항목만 Codex(GPT-6 Luna)가 원문을 다시 읽고 근거 문장을 인용해 확정합니다.", ""),
    ("완전 통과", "그 질문에 적용된 기준을 모두 통과한 질문.", "t-pass"),
    ("FAIL", "기준을 하나라도 어긴 질문(보류가 없을 때).", "t-fail"),
    ("보류", "심판이 확인하지 못한 기준이 남은 질문 — 억지로 정하지 않고 사람이 정합니다.", "t-hold"),
    ("질문당 기준 수(k)", "질문 하나에 걸리는 기준의 수. 6~10개로 맞췄습니다(근거는 02장).", ""),
]


def scene_intro(D: Dict[str, Any]) -> str:
    """왜 만들었나 — 흐름도 앞 (사용자 10-21 저녁 · 10-22: 프로젝트 이름은 미정이라 이름 · '무엇을 만들었나' 없이 필요성만). 숫자는 이번 실행에서 다시 셈.
    근거: 10-19 · 10-20 발표 표지(같은 질문 → 자동 채점 → 비교), 참고자료/채팅품질 평가기준.md(AIONA ↔ 외부 시스템, 같은 질문 집합 ·
    완결된 답끼리), 10-19 · 10-20 미팅(앱 · 웹 답 · 판정 프롬프트 고정), 10-21 체크리스트 A3 · A4(확장 · 보류 사례)."""
    n, m = D["n"], len(D["order"])
    cr = crit_results(D)
    n_dec = sum(sum(cr[c][p].values()) for c in cr for p in D["order"])
    why = [   # 사용자 10-22: 이름은 미정 — 필요성만. "지속적으로 업데이트를 거치면서 테스트를 진행하여 채팅의 품질을 높이기 위해 필요"
        ("업데이트마다 다시 재야 한다", "AIONA 는 계속 업데이트됩니다. 바뀔 때마다 같은 질문 · 같은 기준 · 고정된 판정 프롬프트로 다시 테스트해야 "
         "나아졌는지 나빠졌는지 알 수 있습니다."),
        ("무엇을 고칠지 보여야 한다", "질문마다 어긴 기준과 근거(답의 해당 문장)가 나오고, 같은 질문에 다른 AI 가 어떻게 답했는지 견줘 봐야 "
         "다음 업데이트에서 고칠 곳이 보입니다."),
        ("사람이 매번 손으로 확인할 수 없다", f"한 번 테스트에 질문 {n}개 × {m}모델, 기준 판정 {n_dec:,}건입니다. 업데이트마다 사람이 수작업으로 "
         "확인할 수 없어, 프로그램과 AI 심판이 채점하고 사람은 심판이 확인하지 못한 보류만 정하도록 만들었습니다."),
    ]
    return ('<div class="in-why in-why2">'   # 한 줄 메시지와 겹치는 머리 상자 · 소제목은 뺌
            + "".join(f'<div><b>{E(t)}</b><p>{E(d)}</p></div>' for t, d in why)
            + "</div>"
            + '<h3 class="rs-sh in-gl">처음 보시는 분을 위한 용어</h3><dl class="rs-terms in-terms">'
            + "".join(f'<div{f" class={chr(34)}{c}{chr(34)}" if c else ""}><dt>{E(t)}</dt><dd>{E(d)}</dd></div>' for t, d, c in TERMS)
            + "</dl>")


#: 질문 설계 근거 자료 — 숫자는 모두 이 파일들에서 읽는다(손으로 옮기지 않음)
DESIGN_K_EXP = ROOT / "참고자료" / "분석" / "2026-10-18-질문당기준수" / "boundary_exp_3models.json"   # 통제 실험: 같은 질문에 조건만 늘려 k 1~16
DESIGN_MIN = ROOT / "참고자료" / "최소평가"   # min_eval*.json(목표별 질문 수 · 78 vs 326 비교) · 평가횟수k_곡선_326.json(고르는 순서별)
DESIGN_GEN326 = DESIGN_MIN / "생성_326_v1.4_기준채움_2026-10-14.json"   # 10-14 생성기(per_target=10) 결과 — 326 이 나온 과정 (사용자 10-22)


def scene_design(D: Dict[str, Any]) -> str:
    """질문 설계 — 왜 질문마다 기준 6~10개, 왜 78개 (사용자 10-21 밤: 프로젝트를 모르는 사람에게 두 숫자의 근거).
    근거: 참고자료/발표/ModelGrade_진행보고_2026-10-19.html 03장(k 통제 실험) · ModelGrade_보고_2026-10-20.html 01장(왜 78개) · 참고자료/최소평가/README.md."""
    kx = json.loads(DESIGN_K_EXP.read_text(encoding="utf-8"))["results"]["strict"]["by_k"]
    m10 = json.loads((DESIGN_MIN / "min_eval_m10.json").read_text(encoding="utf-8"))   # 326 평가 실행 — 질문 수(full_n)
    gen = json.loads(DESIGN_GEN326.read_text(encoding="utf-8"))   # 10-14 생성 결과 — 앞에서부터 N개일 때 10번 이상 들어간 기준 수
    reach = json.loads((DESIGN_MIN / "평가횟수k_곡선_326.json").read_text(encoding="utf-8"))["reach"]
    setj = Path(D["set_dir"]) / "set.json"
    kvar = json.loads(setj.read_text(encoding="utf-8")).get("k_var", {}) if setj.exists() else {}
    mt_k = json.loads(setj.read_text(encoding="utf-8")).get("multiturn_k_exact", {}) if setj.exists() else {}

    # ① 질문마다 기준 6~10개 — k 별 '모두 통과 / 모델마다 다름 / 모두 실패' 막대
    cols = []
    for r in kx:
        k, n = r["k"], r["n"]
        ap, af = r["all_pass"], r["all_fail"]
        sp = max(0.0, 1 - ap - af)
        band = 6 <= k <= 10
        cols.append(f'<div class="ds-col{" ds-band" if band else ""}" title="k={k} · 질문 {n}개 · 모두 통과 {ap * 100:.0f}% · 모델마다 다름 {sp * 100:.0f}% · 모두 실패 {af * 100:.0f}%">'
                    f'<em>{sp * 100:.0f}%</em><div class="ds-bar"><i class="ds-f" style="height:{af * 100:.1f}%"></i><i class="ds-s" style="height:{sp * 100:.1f}%"></i>'
                    f'<i class="ds-p" style="height:{ap * 100:.1f}%"></i></div><b>{k}</b></div>')
    lo = [r for r in kx if r["k"] <= 5]
    mid = [r for r in kx if 6 <= r["k"] <= 10]
    hi = [r for r in kx if r["k"] >= 12]
    pct = lambda v: f"{v * 100:.0f}%"
    split = lambda r: max(0.0, 1 - r["all_pass"] - r["all_fail"])
    k_find = [
        ("기준 1~5개", f"모두 통과 {pct(min(r['all_pass'] for r in lo))}~{pct(max(r['all_pass'] for r in lo))}", "너무 쉬워 모든 모델이 맞힘 — 누가 나은지 못 가름"),
        ("기준 6~10개", f"모델마다 다름 {pct(min(split(r) for r in mid))}~{pct(max(split(r) for r in mid))}", "모델 차이가 가장 잘 드러남 → 채택"),
        ("기준 12개 이상", f"모두 실패 {pct(min(r['all_fail'] for r in hi))}~{pct(max(r['all_fail'] for r in hi))}", "너무 어려워 모두 틀림 — 역시 못 가름"),
    ]
    by_k = kvar.get("by_k") or {}
    singles_in = sum(v for k, v in by_k.items() if 6 <= int(k) <= 10)
    exempt = kvar.get("exempt", 0)
    now = (f"이번 세트: 단일 질문 {singles_in}개 모두 기준 6~10개" + (f"(6개 {by_k.get('6', 0)} · 7개 {by_k.get('7', 0)} · 8개 {by_k.get('8', 0)} · 9개 {by_k.get('9', 0)} · 10개 {by_k.get('10', 0)})" if by_k else "")
           + (f", '아무것도 출력하지 마' 같은 빈 출력 요구 {exempt}개만 예외(조건을 붙이면 모순)" if exempt else "")
           + (f", 10턴 대화 {len(mt_k)}개는 턴 평균 {min(mt_k.values()):.1f}~{max(mt_k.values()):.1f}개" if mt_k else "") + ".") if kvar else ""
    part_k = (
        '<div class="ds-card"><h3 class="rs-sh">① 왜 질문마다 기준 6~10개인가</h3>'
        '<p class="ds-q">질문 하나에 기준(지켜야 할 조건)이 몇 개 걸리느냐에 따라 난이도가 달라집니다. 너무 쉬우면 모든 AI 가 통과하고, '
        '너무 어려우면 모두 실패해서 <b>어느 AI 가 더 나은지</b> 가르지 못합니다.</p>'
        f'<p class="ds-how"><b>어떻게 정했나</b> — 같은 질문 18개에 조건 문장만 하나씩 더해 기준 1~16개짜리 변형 {sum(r["n"] for r in kx)}개를 만들어 '
        'GPT · Claude · Gemini 에 던지고, 기준 수마다 <span class="ds-lg ds-p">모두 통과</span> <span class="ds-lg ds-s">모델마다 다름</span> '
        '<span class="ds-lg ds-f">모두 실패</span> 비율을 셌습니다(통제 실험).</p>'
        f'<div class="ds-chart" role="img" aria-label="기준 수별 판정 분포">{"".join(cols)}</div><p class="ds-axis">막대 위 숫자 = 모델마다 판정이 다른(=모델 차이가 드러난) 질문 비율 · 아래 숫자 = 질문당 기준 수</p>'
        '<ul class="ds-find">' + "".join(f'<li><b>{E(a)}</b><span>{E(b)}</span><em>{E(c)}</em></li>' for a, b, c in k_find) + '</ul>'
        + (f'<p class="ds-now">{E(now)}</p>' if now else "") + '</div>')

    # ② 왜 78개 — 기준마다 서로 다른 질문 10개 이상에서 평가되는 가장 적은 수
    order_rows = [("질문 번호 순서대로", reach["number"]["raw"]["10"]), ("기준 많은 질문부터", reach["kdesc"]["raw"]["10"]),
                  ("아직 덜 채운 기준을 가장 많이 채우는 질문부터", reach["greedy10"]["raw"]["10"])]
    top = max(v for _t, v in order_rows)
    bars = "".join(f'<div class="ds-hb{" ds-win" if v == min(x for _t, x in order_rows) else ""}"><span>{E(t)}</span><div><i style="width:{v / top * 100:.1f}%"></i><b>{v}개</b></div></div>'
                   for t, v in order_rows)
    cmp_ = m10["compare"]
    full_n = cmp_["정밀"][cmp_["정밀"]["_ranking"][0]]["n"]
    gp, gc = gen["prefix"], gen["criteria"]
    gbars = "".join(f'<div class="ds-hb{" ds-win" if r["n"] == gen["questions"] else ""}"><span>앞 {r["n"]}개</span><div><i style="width:{r["k10"] / gc * 80:.1f}%"></i>'   # 숫자 칸이 꺾이지 않게 80% 까지
                    f'<b>{r["k10"]}/{gc}</b></div></div>' for r in gp)
    part_n = (
        '<div class="ds-card"><h3 class="rs-sh">② 왜 78개인가</h3>'
        '<p class="ds-q">기준 하나가 믿을 만하게 평가되려면 한두 질문이 아니라 <b>서로 다른 질문 여러 개</b>에서 평가돼야 합니다. '
        '그래서 "모든 기준이 서로 다른 질문 10개 이상에서 평가된다"를 목표로 정하고, 이 목표를 채우는 가장 적은 질문 수를 찾았습니다.</p>'
        f'<p class="ds-how"><b>{full_n}개는 어디서</b> — 처음(10-14)에는 질문 수를 정하지 않고, 생성기에 "모든 기준이 질문 10개 이상에 들어갈 때까지" '
        f'모자란 기준을 노린 질문을 계속 더하게 했습니다. 그렇게 나온 것이 {gen["questions"]}개입니다.</p>'
        f'<div class="ds-hbars">{gbars}</div>'
        '<p class="ds-how"><b>어떻게 줄였나</b> — 326개 평가 결과로 고르는 순서를 바꿔 가며 목표에 닿는 질문 수를 셌습니다. '
        '아직 덜 채운(드문) 기준을 가장 많이 채우는 질문부터 고르면, 겹치는 질문을 건너뛰어 훨씬 적은 수로 목표에 닿습니다.</p>'
        f'<div class="ds-hbars">{bars}</div>'
        '<p class="ds-axis">모든 기준이 10번 이상 평가될 때까지 필요한 질문 수 — 순서만 바꿔도 4분의 1로 줄어듦</p>'
        '</div>')
    return ('<p class="ds-lead">평가에 쓰는 질문은 사람이 감으로 고른 것이 아니라 <b>규칙과 실험</b>으로 정했습니다. 두 숫자의 근거입니다.</p>'
            f'<div class="ds-grid">{part_k}{part_n}</div>')   # 사용자 10-22: 출처 줄 뺌 (자료 경로는 이 함수 docstring · DESIGN_* 상수)


#: 심판 흔들림 A/B — 같은 답 40건(10질문 × 4모델)을 Codex low · medium 으로 (참고자료/변경내역/2026-10-20-변경내역.md '10-21 오전' A/B)
WOBBLE_AB = {"run": "a1642702fccc", "n": 40, "same": 34, "changed": 6, "how": "통과→FAIL 2 · FAIL→통과 3 · 보류→FAIL 1"}   # 받은 의견 E: 어느 실행에서 잰 것인지
#: 실제 사례 — (모델, 질문, 기준, 꼬리표, 꼬리표 색, 덧붙일 말, 기대(질문 결과, 항목 판정, 판정 경로에 있어야 할 말)).
#: 판정 내용은 결과 JSON 에서 읽고, 기대와 다르면(새 결과로 다시 만들 때) 그 사례는 빼고 경고 — 글이 판정과 어긋나지 않게 (받은 의견 E)
VERIFY_CASES = [   # 실행 50ca3ccccace 에서 고름 (10-22) — 이 결과의 판정과 다르면 빠진다
    ("gemini", 29, "J7-1.a", "두 심판 모두 FAIL", "ok", "긴 문서에 있는 기록을 '본문에 없다'고 답했습니다 — 1차 심판과 Codex 가 같은 판단을 냈습니다.",
     ("FAIL", "FAIL", "Decisions 도 FAIL")),
    ("gemini", 75, "J5-1.a", "확인 못 해 보류 — 사람이 정함", "hold", "일반 상식이 틀렸다는 주장이지만 반박 출처가 없어 억지로 정하지 않고 사람에게 넘깁니다.",
     ("HOLD", "UNVERIFIED", "반박 출처 없음")),
    ("gpt", 43, "J1-4.b", "1차 심판이 틀리고 Codex 가 바로잡은 예", "bad", "Decisions 는 '반복'으로 강하게 FAIL 을 냈지만, Codex 가 기준 정의(표를 풀어 쓴 정리 · 결론 한 번은 "
     "반복 아님)를 들어 통과로 바로잡았습니다. 두 단계 심판이 필요한 이유입니다.", ("PASS", "PASS", "Decisions 강한 FAIL")),
]


def scene_verify(D: Dict[str, Any], part_of: Optional[Dict[str, str]] = None) -> str:
    """검증 — 무엇을 확인했고, 무엇이 남았나 (사용자 10-21 저녁 · 받은 의견 ④ 'AI 가 AI 를 평가한 판정을 어떻게 믿나'):
    확인한 것(근거 있는 것만) · 아직 남은 것 · 실제 사례 3개(질문 · 답 · 판정 근거). 숫자는 이번 실행에서 다시 셈."""
    order = D["order"]
    # 질문 세트 사전검사 — 질문만으로 셀 수 있는 기준이 각각 몇 문항에 걸리나 (78개를 고른 근거)
    planned: Dict[str, int] = {}
    try:
        from webapp import question_generation as qg
        groups = json.loads((Path(D["set_dir"]) / "questions.json").read_text(encoding="utf-8"))
        planned = {k: v for k, v in qg.target_counts(qg.check(groups)).items() if v is not None}
    except Exception:   # 사전검사를 못 돌리면 그 카드의 숫자만 뺀다
        planned = {}
    cr = crit_results(D)
    mins = {c: min(sum(cr[c][p].values()) for p in order) for c in cr}
    low = sorted((c for c in mins if mins[c] < 10), key=lambda c: (mins[c], c))
    pair: Counter = Counter()
    for r in D["d"]["judgment"]["results"]:
        for t in (r.get("turn_results") or {}).values():
            for v in t["items"].values():
                if "codex" in v:
                    pair[(v.get("decisions"), v.get("codex"))] += 1
    scr = [r.get("screen") or {} for r in D["d"]["judgment"]["results"]]
    n_items, n_flag = sum(s.get("items", 0) for s in scr), sum(s.get("flagged", 0) for s in scr)
    dec_fail = sum(v for (d, _c), v in pair.items() if d == "FAIL")
    both_fail = pair[("FAIL", "FAIL")]
    caught = pair[("PASS", "FAIL")]
    kinds = Counter(it.get("kind") or "" for L in D["lines"].values() for it in L["items"] if it["decision"] == "UNVERIFIED")
    n_hold_q = sum(1 for L in D["lines"].values() if L["verdict"] == "HOLD")

    done = [
        ("질문 78개를 고른 근거", (f"질문만으로 셀 수 있는 기준 {len(planned)}개가 모두 서로 다른 질문 {min(planned.values())}개 이상에 걸리도록 326개에서 골랐습니다."
                               if planned else "기준마다 서로 다른 질문 10개 이상에 걸리도록 326개에서 골랐습니다."), "질문 세트 사전검사 · 02 질문 설계"),
        ("기준이 실제로 평가된 횟수", (f"기준 72개 중 이번 답에 실제로 쓰인 {len(cr)}개 가운데 " if len(cr) != 72 else "기준 72개 중 ")
         + f"{len(cr) - len(low)}개가 네 모델 모두에서 10문항 이상 평가됐습니다.", "상세 분석 · 기준별 결과"),
        ("두 단계 심판", f"Decisions 가 항목 {n_items:,}개를 모두 보고, 그중 {n_flag:,}개({n_flag / max(1, n_items):.0%})는 Codex 가 원문을 다시 읽고 판정했습니다. "
                       f"Decisions FAIL {dec_fail:,}개 중 Codex 도 FAIL 은 {both_fail}개 — 나머지는 통과 · 해당 없음으로 바로잡았고, "
                       f"Decisions 통과였던 {caught}개는 Codex 가 FAIL 로 잡았습니다.", "결과 JSON"),
        ("근거를 원문과 대조", f"FAIL 근거로 든 답 문장이 원문에 실제로 있는지 프로그램이 대조합니다. 없으면 다시 묻고, 끝내 맞지 않으면 보류 "
                            f"(이번 인용 불일치 {kinds.get('인용 불일치', 0)}개). 사실을 확인하지 못한 {kinds.get('확인 못함', 0)}개도 억지로 정하지 않고 보류했습니다.", "판정 규칙"),
        ("심판 흔들림을 숫자로", (f"같은 답 {WOBBLE_AB['n']}건을 Codex 의 생각 깊이를 낮음 · 보통으로 바꿔 두 번 판정 — {WOBBLE_AB['same']}건 같음, {WOBBLE_AB['changed']}건 바뀜"
                              f"({WOBBLE_AB['how']}, 한쪽으로 쏠리지 않음). 그래서 몇 문항 차이는 단정하지 않습니다."
                              + ("" if D["run_id"] == WOBBLE_AB["run"] else f" (이전 실행 {WOBBLE_AB['run']} 에서 잰 값 — 이 결과의 심판 구조로는 다시 재야 합니다)")),
         f"실행 {WOBBLE_AB['run']} 표본 A/B"),
        ("사람이 찾아 고친 오판", "해요체를 반말로 본 말투 판정(AIONA Q22) · CODE '질문이 요구한 계산값' 오판 4건을 고쳤고, ChatGPT 앱 복사가 Python 줄바꿈을 지우는 것을 화면과 대조해 확인했습니다.",
         "10-21 점검 기록"),
        ("같은 조건 · 블라인드", "CODE 검사는 같은 답이면 몇 번을 돌려도 결과가 같고, LLM Judge 는 어느 모델의 답인지 모른 채 고정된 판정 프롬프트로 채점합니다.", "평가 방법"),
    ]
    left = [
        ("사람 판정과의 일치율", "사람이 직접 정한 정답 표본과 자동 판정을 대조해 일치율을 재야 합니다. 대조 도구(tools/review_kit.py)는 있지만 정답 표본이 아직 없어 재지 못했습니다.", "가장 먼저"),
        ("기준별 판정 정확도", "기준 정의 · 겹침 감사(10-12)는 했지만, 기준마다 판정이 몇 % 맞는지는 아직입니다.", "다음"),
        ("적게 평가된 기준", f"{len(low)}개 기준은 한 모델 이상에서 10문항 미만 — 예: " + " · ".join(E(crit_name(D["names"], c)) for c in low[:4]) + ". 답에 따라 적용되는 기준이 많습니다.", "다음"),
        ("보류를 사람이 정하기", f"보류 {n_hold_q}칸은 아직 사람이 정하지 않았습니다. 정한 판단을 사례로 모아 다음 판정의 근거로 쓰는 구조는 만드는 중입니다.", "계획"),
    ]
    done_html = "".join(f'<div class="vf-i"><b>{E(t)}</b><p>{E(d)}</p><small>근거 · {E(s)}</small></div>' for t, d, s in done)
    left_html = "".join(f'<div class="vf-i"><b>{E(t)}<span class="vf-st">{E(st)}</span></b><p>{d if "<" in d else E(d)}</p></div>' for t, d, st in left)

    def verdict_ko(v: Optional[str]) -> str:
        return {"PASS": "통과", "FAIL": "FAIL", "UNVERIFIED": "보류", "NOT_APPLICABLE": "해당 없음"}.get(v or "", v or "—")
    cases = []
    for p, q, c, tag, tone, note, (want_q, want_it, want_how) in VERIFY_CASES:
        L = D["lines"].get((p, q))
        it = next((i for i in (L or {}).get("items", []) if i["item"] == c), None)
        if not L or not it or L["verdict"] != want_q or it["decision"] != want_it or want_how not in (it.get("how") or ""):
            print(f"경고: 검증 사례 {p} Q{q} {c} — 이 결과의 판정이 사례 글과 달라 뺌", file=sys.stderr)
            continue
        qtext = it.get("question_quote") or (D["qset"][q]["turns"][0].get("question") or "")
        aid = f'{"f" if L["verdict"] == "FAIL" else "b" if L["verdict"] == "HOLD" else "p"}-{p}-{q}'
        link = f'<a class="vf-link" href="{E(part_of[aid])}#{aid}">질문 · 답 원문 보기 →</a>' if part_of and aid in part_of else ""
        cases.append(f'<div class="vf-case vf-{tone}"><span class="vf-tag">{E(tag)}</span>'
                     f'<b class="vf-ct">{mname(p)} · Q{q} · {E(crit_name(D["names"], c))} <span class="rs-id">{E(c)}</span></b>'
                     f'<dl><dt>질문</dt><dd>{E(qtext[:110])}{"…" if len(qtext) > 110 else ""}</dd>'
                     f'<dt>답의 해당 문장</dt><dd class="vf-q">“{E((it.get("answer_quote") or "")[:140])}”</dd>'
                     f'<dt>판정</dt><dd><b>{verdict_ko(it["decision"])}</b> · {E(it.get("how") or "")}</dd>'
                     f'<dt>심판이 든 근거</dt><dd>{E((it.get("reason") or "")[:200])}</dd></dl>'
                     f'<p class="vf-note">{E(note)} (질문 결과: {verdict_ko("UNVERIFIED" if L["verdict"] == "HOLD" else L["verdict"])})</p>{link}</div>')
    return (f'<div class="vf-cols"><div class="vf-col vf-done"><h3 class="rs-sh">확인한 것 <small>{len(done)}</small></h3>{done_html}</div>'
            f'<div class="vf-col vf-left"><h3 class="rs-sh">아직 남은 것 <small>{len(left)}</small></h3>{left_html}</div></div>'
            f'<h3 class="rs-sh">실제 사례 — 질문 · 답 · 판정 근거</h3><div class="vf-cases">{"".join(cases)}</div>')


def crit_verdicts(D: Dict[str, Any], p: str, q: int) -> Dict[str, str]:
    """한 질문에 적용된 기준 → PASS · FAIL · HOLD (멀티턴은 한 턴이라도 FAIL 이면 FAIL, 아니면 보류가 있으면 보류).
    LLM Judge 는 질문의 목표 기준 전부(해당 없음 = 어긴 것 없음 → 통과), CODE 는 실제로 돈 검사."""
    seen: Dict[str, set] = defaultdict(set)
    for it in D["lines"][(p, q)]["items"]:
        seen[it["item"]].add(it["decision"])
    for t in range(1, len(D["qset"][q]["turns"]) + 1):
        for c in (D["turns"].get((p, q, t)) or {}).get("code") or []:
            if c.get("status") == "EXECUTED":
                seen[c["check_id"].split(":")[0]].add(c.get("verdict"))
    # 목표 기준 안의 '해당 없음'(답에 그 내용이 없어 어긴 것도 없음)은 통과로 센다 — 같은 질문 · 같은 기준이면 네 모델의 기준 판정 건수가 같아야 한다 (사용자 10-22)
    out = {cid: "FAIL" if "FAIL" in ds else "HOLD" if "UNVERIFIED" in ds else "PASS" if ("PASS" in ds or "NOT_APPLICABLE" in ds) else "" for cid, ds in seen.items()}
    return {c: v for c, v in out.items() if v}


def crit_results(D: Dict[str, Any]) -> Dict[str, Dict[str, Counter]]:
    """기준 → 모델 → Counter(PASS · FAIL · HOLD) — 질문 하나에 한 번. 빈 출력 요구 문항은 보류라 뺀다."""
    out: Dict[str, Dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for q in D["qtypes"]:
        if D["qtypes"][q] == "명시적 빈 출력":
            continue
        per = {p: crit_verdicts(D, p, q) for p in D["order"]}
        cells = set().union(*per.values())   # 같은 질문은 네 모델이 같은 칸 — 어느 모델에서든 매겨진 기준(사용자 10-22: 기준 판정 건수가 같아야 함)
        for p in D["order"]:
            for cid in cells:
                out[cid][p][per[p].get(cid, "PASS")] += 1   # 그 모델 답에서 돌지 않은 검사(표 · 코드 · 계산이 없어 세부 검사 대상 없음)는 어긴 것 없음
    return out


def scene_by_crit(D: Dict[str, Any]) -> str:
    """기준별 결과 — 질문 단위 '완전 통과'와 달리 기준 하나하나의 통과율 (사용자 10-21 저녁: 기준별로 나온 결과도 바로 보이게).
    위: 모델마다 기준 통과율, 아래: 묶음 16개(CODE 8 · LLM Judge 8) — 묶음을 누르면 기준별로 펼침."""
    order = D["order"]
    cr = crit_results(D)
    tot = {p: Counter() for p in order}
    for cid in cr:
        for p in order:
            tot[p].update(cr[cid][p])
    rate = {p: tot[p]["PASS"] / max(1, sum(tot[p].values())) for p in order}
    by = sorted(order, key=lambda p: -rate[p])
    cards = "".join(f'<div class="cb-card"><span class="cb-r">{i}위</span><span class="cb-m">{mname(p)}</span><b>{rate[p] * 100:.1f}%</b>'
                    f'<small>기준 판정 {sum(tot[p].values()):,}건 · FAIL {tot[p]["FAIL"]} · 보류 {tot[p]["HOLD"]}</small>'
                    f'<small class="cb-q">질문 단위 완전 통과 {D["tally"][p]["PASS"]} / {D["n"]} ({D["tally"][p]["PASS"] / D["n"] * 100:.0f}%)</small></div>' for i, p in enumerate(by, 1))

    def cell(c: Counter) -> str:
        n = sum(c.values())
        if not n:
            return '<div class="cb-c cb-na">—</div>'
        return (f'<div class="cb-c" style="{tint(min(1.0, c["FAIL"] / n / 0.4), "--risk")}" title="통과 {c["PASS"]} · FAIL {c["FAIL"]} · 보류 {c["HOLD"]} · 평가 {n}">'
                f'<b>{c["PASS"] / n * 100:.0f}%</b><small>FAIL {c["FAIL"]} · 보류 {c["HOLD"]} / {n}</small></div>')   # 받은 의견 B: 보류 수도

    ids = sorted(D["names"], key=lambda c: (0 if c[0].isdigit() else 1, c))
    groups: Dict[str, List[str]] = defaultdict(list)
    for cid in ids:
        groups[cid.split(".")[0] if cid[0].isdigit() else cid.split("-")[0]].append(cid)
    head = '<div class="cb-row cb-head"><span>묶음 · 기준</span>' + "".join(f"<span>{mname(p)}</span>" for p in order) + "</div>"
    rows = []
    for g, cids in groups.items():
        gsum = {p: sum((cr[c][p] for c in cids), Counter()) for p in order}
        label = f'{g} {CODE_GROUP.get(g, "")}' if g[0].isdigit() else f'{g} {GROUP.get(g, "")}'
        kind = "CODE 검사" if g[0].isdigit() else "LLM Judge"
        sub = "".join(f'<div class="cb-row"><span class="cb-l"><b>{E(crit_name(D["names"], c))}</b><small>{E(c)}</small></span>'
                      + "".join(cell(cr[c][p]) if c in cr else '<div class="cb-c cb-na">—</div>' for p in order) + "</div>" for c in cids)
        rows.append(f'<details class="cb-g"><summary class="cb-row"><span class="cb-l"><b>{E(label)}</b><small>{kind} · 기준 {len(cids)}개</small></span>'
                    + "".join(cell(gsum[p]) for p in order) + f'</summary><div class="cb-sub">{sub}</div></details>')
    n_empty = sum(1 for t in D["qtypes"].values() if t == "명시적 빈 출력")
    key = ('<p class="cb-key">칸 = 통과율(그 기준이 적용된 질문 중 통과한 비율) · 색이 진할수록 FAIL 이 많음 · 아래 숫자 = FAIL · 보류 / 평가된 질문 · '
           f'묶음을 누르면 기준별로 펼쳐집니다 · 빈 출력 요구 {n_empty}문항은 보류라 빼고, 멀티턴은 한 턴이라도 FAIL 이면 그 질문은 FAIL</p>')
    return (f'<div class="cb-cards">{cards}</div>'
            '<p class="cb-key">기준 통과율 = 기준이 적용된 (질문 × 기준) 판정 중 통과한 비율. 같은 질문에는 네 모델 모두 같은 기준을 매겨 판정 건수가 같습니다 — '
            f'질문 {sum(1 for t in D["qtypes"].values() if t != "명시적 빈 출력")}개 × 그 질문에 적용된 기준(질문마다 늘 보는 기준 + 그 질문이 요구한 기준 6~10개, 멀티턴은 10턴 합). '
            '답에 그 내용이 없어 해당 없음인 칸은 어긴 것이 없으니 통과로, 보류는 통과로 세지 않음. '
            '질문 단위 완전 통과(상세 분석 ‘이번 실행 결과’)는 질문 하나의 기준을 모두 통과해야 해서 이보다 훨씬 낮습니다.</p>'
            f'<h3 class="rs-sh">묶음 · 기준마다</h3>{key}<div class="cb-table">{head}{"".join(rows)}</div>')


def scene_code(D: Dict[str, Any]) -> str:
    """CODE 검사 결과 — 프로그램이 센 것만 따로 (사용자 10-21 저녁: LLM Judge 결과와 따로, 78문항에서).
    빈 출력 요구 문항은 CODE 대신 보류(사람이 정함)라 빼고, 질문 판정에 들어간 code_fails 로 센다."""
    order = D["order"]
    qs = sorted(q for q in D["qtypes"] if D["qtypes"][q] != "명시적 빈 출력")
    n = len(qs)
    st: Dict[str, Dict[str, Any]] = {}
    per: Dict[str, Dict[str, set]] = defaultdict(lambda: defaultdict(set))   # 검사 → 모델 → 걸린 질문
    for p in order:
        fail, sem, ran, bad = set(), set(), 0, 0
        for q in qs:
            L = D["lines"][(p, q)]
            for c in {x.split(":")[0] for x in L["line"].get("code_fails") or []}:
                fail.add(q)
                per[c][p].add(q)
            if any(i["decision"] == "FAIL" for i in L["items"]):
                sem.add(q)
            for t in range(1, len(D["qset"][q]["turns"]) + 1):
                for c in (D["turns"].get((p, q, t)) or {}).get("code") or []:
                    if c.get("status") == "EXECUTED":
                        ran += 1
                        bad += c.get("verdict") == "FAIL"
        st[p] = {"fail": fail, "sem": sem, "ran": ran, "bad": bad}
    rank = sorted(order, key=lambda p: (len(st[p]["fail"]), order.index(p)))
    rows = "".join(
        f'<div class="cd-row"><span class="cd-m">{mname(p)}</span>'
        f'<div class="cd-bar"><span class="cd-ok" style="width:{(n - len(st[p]["fail"])) / n * 100:.1f}%">{n - len(st[p]["fail"])}</span>'
        + (f'<span class="cd-bad" style="width:{len(st[p]["fail"]) / n * 100:.1f}%">{len(st[p]["fail"])}</span>' if st[p]["fail"] else "")
        + f'</div><span class="cd-v"><b>{n - len(st[p]["fail"])}</b> / {n} 통과'
        f'<small>검사 {st[p]["ran"]:,}번 중 FAIL {st[p]["bad"]}번</small>'
        f'<small>걸린 {len(st[p]["fail"])}문항 중 LLM Judge 도 FAIL {len(st[p]["fail"] & st[p]["sem"])}</small></span></div>'
        for p in rank)
    bars = (f'<div class="cd-bars">{rows}</div>'
            f'<div class="cd-key"><span><i class="cd-ok"></i>CODE 검사 모두 통과</span><span><i class="cd-bad"></i>하나라도 걸림</span>'
            f'<span class="muted">질문 수 · 빈 출력 요구 {len(D["qtypes"]) - n}문항은 보류라 뺌 · 같은 답이면 다시 돌려도 결과가 같음(프로그램)</span></div>')
    checks = sorted(per, key=lambda c: (-sum(len(v) for v in per[c].values()), c))
    n_code = sum(1 for c in D["names"] if c[0].isdigit())
    mx = max([len(per[c][p]) for c in checks for p in order] or [1]) or 1
    cols = f"minmax(150px, 1.6fr) repeat({len(order)}, minmax(0, 1fr)) minmax(0, 0.8fr)"
    cells = ['<div class="rs-hh"></div>'] + [f'<div class="rs-hh">{mname(p)}</div>' for p in order] + ['<div class="rs-hh muted">합</div>']
    for c in checks:
        worst = max(len(per[c][p]) for p in order)
        cells.append(f'<div class="rs-hl"><b>{E(crit_name(D["names"], c))}</b><span class="rs-id">{E(c)}</span></div>')
        cells.extend(f'<div class="rs-hc rs-hc-s{" rs-worst-c" if len(per[c][p]) == worst and worst else ""}" style="{tint(len(per[c][p]) / mx, "--risk")}" '
                     f'title="{E(" · ".join(f"Q{q}" for q in sorted(per[c][p])))}"><b>{len(per[c][p])}</b></div>' for p in order)
        cells.append(f'<div class="rs-hc rs-hc-s rs-sum"><b>{sum(len(v) for v in per[c].values())}</b></div>')
    grid = (f'<h3 class="rs-sh">어떤 검사에 걸렸나 — 걸린 검사 {len(checks)}개 ({n_code}개 중)</h3>'
            f'<p class="rs-basis">숫자는 그 검사에 걸린 질문 수입니다. 칸에 마우스를 올리면 질문 번호가 보입니다. 나머지 {n_code - len(checks)}개 검사는 네 모델 모두 통과.</p>'
            f'<div class="rs-heat" style="grid-template-columns: {cols}">' + "".join(cells) + "</div>")
    return bars + grid


def scene_hold(D: Dict[str, Any], part_of: Optional[Dict[str, str]] = None) -> str:
    order = D["order"]
    holds = [(q, p) for (p, q), L in D["lines"].items() if L["verdict"] == "HOLD"]
    hq = sorted({q for q, _p in holds})
    ty = Counter(D["qtypes"][q] for q in hq)
    state: Counter = Counter()
    cols = f"minmax(150px, 1.6fr) repeat({len(order)}, minmax(0, 1fr))"
    cells = ['<div class="rs-hh"></div>'] + [f'<div class="rs-hh">{mname(p)}</div>' for p in order]
    other = sorted((q, p) for q, p in holds if D["qtypes"][q] != EMPTY_REQ)   # 빈 출력 요구 밖 보류 — 심판이 확인 못 한 항목
    with_fail = 0   # 보류 우선(10-21) — FAIL 항목도 함께 있는 보류 칸

    def go(p: str, q: int, inner: str) -> str:   # 보류 원문으로 (사용자 10-21)
        bid = f"b-{p}-{q}"
        return f'<a class="rs-mca" href="{E(part_of[bid])}#{bid}">{inner}</a>' if part_of and bid in part_of else inner
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
                nf = len(set(L["line"].get("code_fails") or []) | {i["item"] for i in L["items"] if i["decision"] == "FAIL"})
                with_fail += 1 if nf else 0
                cells.append(f'<div class="rs-mc rs-mc-fact" title="{E(" / ".join(w["reason"] for w in why))}">'
                             f'{go(p, q, E(hold_label(why)) + (f" · FAIL {nf}" if nf else ""))}</div>')
                continue
            t = D["turns"].get((p, q, 1)) or {}
            a = (t.get("answer") or "").strip()
            if t.get("status") == "COPY_FAILED":
                k, lab = "copy", "재시도 대상"
            elif not a:
                k, lab = "empty", "빈 답"
            else:
                k, lab = "text", f"“{a[:10]}{'…' if len(a) > 10 else ''}”"
            state[k] += 1
            cells.append(f'<div class="rs-mc rs-mc-{k}" title="{E(a) if a else E(lab)}">{go(p, q, E(lab))}</div>')
    n_empty_q = len({q for q, _p in holds if D["qtypes"][q] == EMPTY_REQ})
    parts_ = ([f"빈 출력 요구 {n_empty_q}문항 × {len(order)}모델"] if n_empty_q else []) \
        + ([f"{hold_label([w for q, p in other for w in hold_why(D, p, q)])} {len(other)}칸"] if other else [])
    what = " + ".join(parts_)
    top = (f'<div class="rs-hold-top"><div class="rs-hbig"><b>{len(holds)}<small>칸</small></b><span>= {E(what)}</span>'
           '<span class="muted">사람이 정할 것은 이것뿐입니다</span></div><div class="rs-facts">'
           f'<div class="rs-fact"><b>{with_fail}</b>FAIL 항목도 함께 있는 보류 칸 — 보류를 어떻게 정해도 결과는 FAIL <span class="muted">(보류가 하나라도 있으면 보류로 둠)</span></div></div></div>')
    legend = ('<div class="rs-legend">'
              f'<span><i class="rs-sw rs-mc-copy"></i>재시도 대상 {state["copy"]}</span><span><i class="rs-sw rs-mc-empty"></i>빈 답 {state["empty"]}</span>'
              f'<span><i class="rs-sw rs-mc-text"></i>짧은 글 {state["text"]}</span>'
              + (f'<span><i class="rs-sw rs-mc-fact"></i>확인 못 함 {state["fact"]} — 빈 출력 요구가 아닌 질문</span>' if state["fact"] else "") + '</div>')
    grid = f'<div class="rs-heat rs-mx" style="grid-template-columns: {cols}">' + "".join(cells) + "</div>"
    why_rows = []
    for q, p in other:
        for w in hold_why(D, p, q):
            tt = f" · 턴 {w['turn']}" if len(D["qset"][q]["turns"]) > 1 else ""
            sent = f" · Codex 로 보낸 이유: {w['flagged']}" if w["flagged"] else ""
            why_rows.append([f'<a href="#q-{q}"><b>Q{q}</b></a><div class="rs-id">{E(TYPE_NAME.get(D["qtypes"][q], ""))}</div>'
                             + f'<div>{go(p, q, "원문 →")}</div>', mname(p),
                             f'<b>{E(crit_name(D["names"], w["item"]))}</b><div class="rs-id">{E(w["item"])}{E(tt)}</div>',
                             E(w["reason"]) + (f'<div class="rs-basis">답의 해당 문장: “{E(w["quote"])}”</div>' if w["quote"] else "")
                             + f'<div class="rs-basis">{E(w["how"] + sent)}</div>'])
    why_tab = ('<h3 class="rs-sh">빈 출력 요구가 아닌 보류 — 사유</h3>' + table("", ["질문", "모델", "확인 못 한 기준", "사유 (Codex)"], why_rows, [12, 12, 18, 58])
               if why_rows else "")
    n_empty = len(holds) - len(other)
    decide = note("정할 것", f"<p>① 빈 출력 요구 {n_empty}칸 — '아무것도 출력하지 마' 지시에 <b>빈 답</b> · <b>재시도 대상</b> · <b>짧은 글</b>을 각각 통과로 볼지 FAIL 로 볼지.</p>"
                  + (f"<p>② 확인 못 함 {len(other)}칸 — 아래 표의 사유와 답의 해당 문장을 보고, 맞으면 통과 · 틀리면 FAIL. "
                     f"그중 {with_fail}칸은 FAIL 항목도 있어 결과는 FAIL 이 됩니다.</p>" if other else "")
                  + "<p>칸을 누르면 그 질문·답변 원문과 보류 사유가 열립니다.</p>", warn=True)
    return top + legend + grid + why_tab + decide   # 사용자 10-22: 위 '계획' 상자는 뺌 — 계획은 보류 · 발전 방향 장면에


def verdict_rule() -> str:
    """질문 판정 규칙 (10-21 저녁: 보류 우선) — 결과 · 보류 장면에 같은 그림으로."""
    rows = [("보류가 하나라도 있으면", "HOLD", "FAIL 항목이 함께 있어도 — 사람이 정함"),
            ("보류 없이 FAIL 이 하나라도 있으면", "FAIL", "CODE 검사 · LLM Judge 어느 쪽이든"),
            ("적용된 기준을 모두 통과하면", "PASS", "")]
    return ('<div class="ot-rule"><b>질문 판정</b>' + "".join(
        f'<div><span>{E(a)}</span><i>→</i>{badge(v).replace("통과", "완전 통과") if v == "PASS" else badge(v)}'
        + (f'<small>{E(c)}</small>' if c else "") + "</div>" for a, v, c in rows) + "</div>")


def scene_output(D: Dict[str, Any]) -> str:
    """결과 — 어떻게 제공하나 (사용자 10-22: 핵심 장면에는 실제 숫자 대신 제공 방식만, 이번 실행 숫자는 상세 분석 '이번 실행 결과'로)."""
    n, n_crit = D["n"], len(D["names"])
    files = [
        ("결과 JSON", "",
         "질문 × 모델마다 판정, 기준마다 판정 · 사유 · 답의 해당 문장, 답 원문과 수집 정보(복사 방식 · 걸린 시간). 다른 프로그램이 읽는 원본"),
        ("판정 보고서", "",
         "모든 판정 기록 — 기준별로 어긴 질문, FAIL 상세(심판이 든 원문 인용), 질문별 판정표, 보류를 사람이 정하는 화면"),
        ("결과 요약", "이 파일",
         "발표용 핵심 장면 + 상세 분석(분포 · 분야별 순위표 · 기준별 결과 · FAIL 원인 · 보류 목록 · 질문별 원문)"),
    ]
    units = [
        ("질문 단위 — 순위", f"모델마다 완전 통과 · FAIL · 보류가 몇 개인지 (/ 질문 {n}개). 순위는 완전 통과 수로 매깁니다."),
        ("유형 · 분야별", "싱글턴 · 멀티턴 · 롱컨텍스트 · 첨부 자료 · 빈 출력 요구마다, 기준 묶음(분야)마다 통과율과 순위표."),
        ("기준 단위", f"기준 {n_crit}개 하나하나가 적용된 질문에서 통과한 비율 — 모델이 무엇에 약한지 봅니다."),
        ("근거", "FAIL · 보류마다 질문 · 답 원문과 심판 사유, 답의 해당 문장을 함께 둡니다."),
    ]
    mock = ('<div class="ot-card"><div class="ot-cap">형식 예시 — 실제 결과가 아닙니다</div><div class="ot-grid">'
            '<div class="ot-src"><b>질문 원문</b><p>질문 세트의 글 그대로 (멀티턴은 턴마다)</p>'
            '<b>답 원문</b><p>답변 아래 복사 버튼으로 받은 글 그대로</p></div><div class="ot-items">'
            '<div class="ot-it">' + badge("PASS") + '<div><b>형식 지시</b> <small>CODE 검사</small></div></div>'
            '<div class="ot-it">' + badge("FAIL") + '<div><b>글자 수 제한</b> <small>CODE 검사</small>'
            '<p>사유: 500자 이내를 요구했는데 답은 612자</p></div></div>'
            '<div class="ot-it">' + badge("HOLD") + '<div><b>사실 정확성</b> <small>LLM Judge</small>'
            '<p>사유: 답의 사실을 질문 자료 · 원문으로 확인하지 못함 · 답의 해당 문장 “…”</p></div></div>'
            '<div class="ot-v">이 질문 → ' + badge("HOLD") + ' <small>보류가 있어 사람이 정함 — FAIL 항목도 있어 어떻게 정해도 FAIL</small></div>'
            '</div></div></div>')
    return ('<div class="in-cols"><div class="in-col"><h3 class="rs-sh">무엇이 나오나 — 명령 하나로 세 가지</h3><ol class="in-built">'
            + "".join(f'<li><span class="in-n">{i}</span><div><b>{t}</b>' + (f'<em>{k}</em>' if k else '') + f'<p>{E(d)}</p></div></li>' for i, (t, k, d) in enumerate(files, 1))
            + '</ol></div><div class="in-col"><h3 class="rs-sh">무엇을 세나</h3><div class="in-why">'
            + "".join(f'<div><b>{E(t)}</b><p>{E(d)}</p></div>' for t, d in units)
            + '</div></div></div><h3 class="rs-sh">질문 하나의 결과는 이렇게 나옵니다</h3>' + mock + verdict_rule())


def scene_hold_flow(D: Dict[str, Any]) -> str:
    """보류 — 어떻게 사람에게 넘기나 (사용자 10-22: 실제 보류 목록 대신 처리 방식, 목록은 상세 분석 '보류 목록'으로).
    화면 · 반영은 report.html '보류' 장 · held-apply(human_decisions.json, 원본 results.jsonl 은 그대로), 사례 축적은 계획."""
    steps = [
        ("언제 보류가 되나", "",
         "빈 출력 요구 질문(빈 답 · 복사 버튼이 안 뜸 · 짧은 글을 통과로 볼지), 두 번째 심판(Codex)도 근거를 확정하지 못한 기준, "
         "답을 복사해 오지 못한 질문. 억지로 정하지 않고 보류로 남깁니다."),
        ("사람이 보는 화면", "판정 보고서 '보류' 장",
         "질문 · 답 원문과 보류 사유(어느 기준인지 · 왜 못 정했는지 · 답의 해당 문장)를 보고 항목마다 PASS / FAIL 을 누릅니다. "
         "결과 요약의 보류 칸을 눌러도 같은 원문 · 사유가 열립니다."),
        ("반영", "명령 하나",
         "사람이 정한 판정을 명령 하나로 넣으면, 그 숫자로 보고서를 다시 만들고 전후를 함께 적습니다. "
         "심판의 원래 판정 기록은 그대로 둡니다."),
        ("사례로 쌓기", "계획 · 만드는 중",
         "사람이 정한 통과 · FAIL 을 모아 다음 판정 때 비슷한 경우의 근거로 쓰고, 같은 판단이 반복되면 채점 기준과 평가 프롬프트로 올립니다."),
    ]
    return ('<ol class="in-built ot-steps">'
            + "".join(f'<li><span class="in-n">{i}</span><div><b>{E(t)}</b>' + (f'<em>{E(k)}</em>' if k else "") + f'<p>{E(d)}</p></div></li>'
                      for i, (t, k, d) in enumerate(steps, 1))
            + "</ol>" + verdict_rule()
            + note("보류를 두는 이유", "<p>AI 심판이 확신하지 못한 판정을 통과나 FAIL 로 억지로 정하면, 순위가 틀려도 알 수 없습니다. "
                   "확인하지 못한 것은 보류로 드러내고, 사람이 정한 만큼만 숫자에 넣습니다.</p>"))


def scene_prompt(D: Dict[str, Any]) -> str:
    """판정 프롬프트 원문 — 심판마다 실제로 보내는 글 그대로 (사용자 10-22). 글은 판정 코드(tools/judge_v4.py)가 읽는 파일 · 상수에서 읽는다:
    Codex = judge_prompt_v4.txt + SCREEN_SUFFIX, 1차 심판(Jev · Decisions) = jev_questions_v4.json 의 guide + TARGET_GUIDE + 기준별 질문(all_item_questions)."""
    from tools import judge_v4 as jv
    codex = jv.PROMPT_V4.read_text(encoding="utf-8") + jv.SCREEN_SUFFIX
    fixed = json.loads(jv.JEV_V4.read_text(encoding="utf-8"))
    guide = fixed["guide"] + jv.TARGET_GUIDE
    allq = jv.all_item_questions(fixed)

    def item_text(i: str, q: Dict[str, Any]) -> str:
        crit = q.get("criteria") or {}
        return (q.get("instructions") or i) + "\n" + "\n".join(f"  · {k}: {v}" for k, v in crit.items())
    items = "\n\n".join(item_text(i, q) for i, q in allq.items())

    def block(title: str, what: str, text: str, open_: bool = False) -> str:
        return (f'<details class="rs-d pr-d"{" open" if open_ else ""}><summary><b>{E(title)}</b><span class="muted">{E(what)} · {len(text):,}자</span></summary>'
                f'<pre class="rs-pre pr-pre">{E(text)}</pre></details>')
    return ('<p class="ds-lead">심판에게 실제로 보내는 글 그대로입니다. 이 글은 모든 질문에 같고, 질문마다 바뀌는 것은 [입력] JSON(질문 · 첨부 · 참조 자료 · 답변)과 '
            '[판정할 항목] 목록(그 질문을 만들 때 정한 기준)뿐입니다. 어느 모델의 답인지는 넣지 않습니다.</p>'
            + block("1차 심판(Jev · Decisions) — 안내문", "질문마다 [판정할 항목] 목록과 함께 보냄", guide, True)
            + block(f"1차 심판(Jev · Decisions) — 기준별 질문 {len(allq)}개", "목록에 든 기준만 골라 보냄 · 예(PASS) · 아니오(FAIL) · 해당 없음 중 하나", items)
            + block("Codex — 판정 프롬프트", "1차 심판이 FAIL · 애매로 본 항목만 이 글로 다시 판정 (판정 프롬프트 v4 + 선별 판정 덧붙임)", codex)
            + '<p class="rs-basis">J5-1.a(사실 정확성)의 시점 문구는 이번 실행 판정 뒤(10-22) "답변 수집 날짜 기준" → "질문이 정한 시점(없으면 답변 수집 날짜) 기준"으로 고친 글입니다. '
              '이번 실행에서 이 문구 때문에 달라지는 판정은 없습니다.</p>')


# 걸린 시간 표의 유형 — 사용자 10-21: 싱글턴 (S) · 멀티턴 (M) · 롱컨텍스트 (L), 이 순서로
TIME_COLS = ["단일 질문", "10턴 멀티턴 대화", "롱컨텍스트", "첨부자료 확인·요약"]
TYPE_SHORT = {"단일 질문": "싱글턴", "10턴 멀티턴 대화": "멀티턴", "롱컨텍스트": "롱컨텍스트", "첨부자료 확인·요약": "첨부 자료",
              "명시적 빈 출력": "빈 출력 요구"}


def scene_next(D: Dict[str, Any]) -> str:
    """발전 방향 — 확장성 중심 (10-21 오후 미팅). 카드: 무엇을 · 왜 · 지금 상태."""
    cards = [
        ("사람 판정과 맞춰 보기", "정답 표본으로 일치율 재기", "사람이 직접 판정한 정답 표본을 만들어 자동 판정과 대조하고, 기준마다 일치율을 냅니다. "
         "대조 도구(tools/review_kit.py)는 있습니다.", "다음 할 일"),
        ("모델 확장", "다른 모델도 같은 질문으로", "답변 수집은 앱 · 웹브라우저 자동화라, 연결만 하면 다른 모델도 같은 78문항으로 받아 같은 기준으로 비교합니다.", "구조 준비됨"),
        ("보류 사례집", "사람의 판단을 데이터로", "보류를 사람이 통과 · FAIL 로 정하면 사례로 쌓고, 다음 판정에서 비슷한 경우의 근거로 씁니다. 반복되는 판단은 채점 기준 · 프롬프트로 올립니다.", "만드는 중"),
        ("심판 셋 + 한 번 요청", "Jev · Decisions · Codex", "세 심판을 함께 쓰고, 수집한 질문 · 답을 한 파일로 모아 심판이 그 파일을 읽어 한 번 요청으로 평가합니다 — 호출과 재시도를 줄입니다.", "만드는 중"),
        ("수집 방식", "복사의 한계", "ChatGPT 앱은 복사할 때 코드 줄바꿈을 지웁니다. 화면 캡처로 받아 보는 방식을 검토합니다.", "검토"),
    ]
    return ('<div class="rs-cards rs-next">' + "".join(
        f'<div class="rs-card"><span class="rs-ct">{E(st)}</span><b>{E(t)}</b><div class="rs-sub">{E(sub)}</div><p>{E(body)}</p></div>'
        for t, sub, body, st in cards) + "</div>")


def judge_work(D: Dict[str, Any]) -> Dict[str, Counter]:
    """모델마다 판정 작업량 — 판정 기록(results.jsonl)의 실제 판정 줄을 모두 더한다(다시 판정 · 복구 포함, 사용자 10-22 '총 걸린 시간으로 합쳐').
    이름 되살림 · 다시 계산(recode) 줄은 앞 줄을 옮겨 적은 것이라 뺀다. seconds = 그 질문을 판정하는 데 실제로 걸린 시간(벽시계 아님)."""
    f = RESULTS / f"판정_{D['run_id']}" / "results.jsonl"
    if not f.exists():
        f = ROOT / "data" / "judge_runs" / D["run_id"] / "v4" / "results.jsonl"
    per: Dict[str, Counter] = {p: Counter() for p in D["order"]}
    if not f.exists():
        return per
    for x in f.read_text(encoding="utf-8").splitlines():
        if not x.strip():
            continue
        l = json.loads(x)
        p = l.get("provider")
        if p not in per or l.get("status") != "judged" or l.get("first_judge_restored") or l.get("recoded"):
            continue
        c = l.get("calls") or {}
        dc, cx = c.get("decisions") or {}, c.get("codex") or {}
        w = per[p]
        w["seconds"] += float(l.get("seconds") or 0)
        if not dc.get("reused"):
            fj = dc.get("first_judge") or {}
            if fj:
                w["jev"] += sum(1 for v in fj.values() if v == "jev")
                w["dec"] += sum(1 for v in fj.values() if v == "decisions")
            else:
                w["dec"] += int(dc.get("calls") or 0)
            w["dec_in"] += int(dc.get("input_tokens") or 0)
        w["codex_calls"] += int(cx.get("rounds") or 0)
        w["codex_in"] += int(cx.get("input_tokens") or 0)
        w["codex_out"] += int(cx.get("output_tokens") or 0)
        w["errors"] += 1 if l.get("judge_error") else 0
    return per


def judge_seconds(D: Dict[str, Any]) -> float:
    """판정에 걸린 시간 — 모델마다 판정 시간 합 중 가장 긴 것(네 모델을 동시에 판정, 수집 시간과 같은 셈)."""
    w = judge_work(D)
    return max((v["seconds"] for v in w.values()), default=0)


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
            by[D["dtypes"][q]].append(s)
        st[p] = {"sum": sum(perq.values()), "ok": qs_ok, "chars": statistics.mean(len(t["answer"]) for t in got) if got else 0,
                 "by": by, "max": max(perq.items(), key=lambda kv: kv[1])}
    mx = max(v["sum"] for v in st.values()) or 1
    bars = "".join(f'<div class="rs-tb"><span class="rs-bl">{mname(p)}</span><span class="rs-tbar"><i style="width:{st[p]["sum"] / mx * 100:.1f}%"></i></span>'
                   f'<span class="rs-tv"><b>{hm(st[p]["sum"])}</b><small>질문당 {st[p]["sum"] / st[p]["ok"]:.0f}초 · 턴당 {st[p]["chars"]:,.0f}자</small></span></div>'
                   for p in order if st[p]["ok"])
    trows = [[mname(p), *[f"{statistics.mean(st[p]['by'][ty]):.0f}초" if st[p]["by"][ty] else "—" for ty in TIME_COLS],
              f"Q{st[p]['max'][0]} {TYPE_SHORT.get(D['dtypes'][st[p]['max'][0]], '')} · {R._hms(st[p]['max'][1])}"] for p in order]
    t2 = table("유형별 — 질문(대화) 하나당 평균 답변 시간", ["모델", *[TYPE_SHORT[ty] for ty in TIME_COLS], "가장 오래 걸린 질문"], trows, [12, 12, 13, 15, 13, 35])
    W = judge_work(D)
    jmx = max((W[p]["seconds"] for p in order), default=0) or 1
    jbars = "".join(f'<div class="rs-tb"><span class="rs-bl">{mname(p)}</span><span class="rs-tbar"><i style="width:{W[p]["seconds"] / jmx * 100:.1f}%"></i></span>'
                    f'<span class="rs-tv"><b>{hm(W[p]["seconds"])}</b><small>질문당 {W[p]["seconds"] / max(1, D["n"]):.0f}초</small></span></div>' for p in order)
    judge = (f'<div class="rs-judge"><div class="rs-jt"><b>{hm(jmx)}</b><span>판정에 실제로 걸린 시간<br>모델마다 질문 판정 시간의 합 중 가장 긴 것(네 모델 동시)</span></div>'
             f'<div class="rs-jb"><div class="rs-tbars">{jbars}</div></div></div>')
    jr = [[mname(p), f'{W[p]["jev"]:,}', f'{W[p]["dec"]:,}', f'{W[p]["dec_in"] / 1e6:.1f}M', f'{W[p]["codex_calls"]:,}'] for p in order]
    jr.append(['<span class="rs-strong">합계</span>', f'{sum(W[p]["jev"] for p in order):,}', f'{sum(W[p]["dec"] for p in order):,}',
               f'{sum(W[p]["dec_in"] for p in order) / 1e6:.1f}M', f'{sum(W[p]["codex_calls"] for p in order):,}'])
    jt = (table("심판별 판정량 · 토큰 — 모델마다", ["모델", "Jev 판정(턴)", "Decisions 판정(턴)", "Decisions 입력 토큰", "Codex 호출"],
                jr, [20, 20, 20, 20, 20])
          + '<p class="rs-basis">1차 심판 — 질문 전체가 3만 2천 토큰 이하이고 첨부가 없으면 Jev, 그 밖(롱컨텍스트 · 첨부 · 멀티턴)은 Decisions. '
            '1차가 FAIL · 애매로 본 항목만 Codex 가 다시 판정. 다시 판정 · 복구까지 모두 더한 값입니다. '
          + 'Jev · Decisions 는 외부 API · Codex CLI 는 ChatGPT 구독으로 실행.</p>')
    # 10-20 보고 때(다른 78개 세트) 수집 시간 참고 줄은 뺌 — 이 실행의 숫자만 (받은 의견 E: 질문 세트 · 실행 고정)
    return ('<h3 class="rs-sh">답변 수집 — 답변 시간 합 (질문을 보내고 답을 받기까지)</h3><div class="rs-tbars">' + bars + "</div>" + t2
            + '<h3 class="rs-sh">판정 — 실제로 걸린 시간</h3>' + judge + jt)


def process_block(ex: Dict[str, Any]) -> str:
    """판정 실행 기록 (10-21) — Codex 호출 · 다시 묻기 · 429 대기와 그 이유 · 판정 실행 오류 (report.html 표에서).
    Codex 호출은 첫 판정만 센 값 — 위 모델별 표(다시 판정 포함)의 합과 다르면 그 차이를 적는다 (받은 의견 D)."""
    pr = ex.get("process") or {}
    if not pr:
        return ""
    rows = [[k, f"<b>{E(v)}</b>", E(d)] for k, (v, d) in pr.items()]
    calls = sum(v["codex_calls"] for v in (ex.get("judge") or {}).values())
    rec = int(re.sub(r"[^\d]", "", (pr.get("Codex 호출") or ("",))[0]) or 0)
    if calls and rec and calls > rec:
        i = list(pr).index("Codex 호출")
        rows[i][2] = E(pr["Codex 호출"][1]) + f" — 첫 판정만 센 값. 위 모델별 표의 합 {calls:,} 와의 차이 {calls - rec:,}회는 다시 판정 때 호출"
    return '<h3 class="rs-sh">판정 실행 기록</h3>' + table("", ["항목", "값", "설명"], rows, [16, 16, 68])


# ---- Arena 형 (v4, 사용자 10-21: "LM Arena 리더보드가 제일 괜찮다") — 개요(큰 제목 · 분야 알약 탭 · 순위 카드) + 분야별 순위표(왼쪽 분야 목록 → 오른쪽 표)
def avatar(p: str) -> str:
    return f'<i class="ar-av lg lg-{p}" aria-hidden="true"></i>'


def board_categories(D: Dict[str, Any]) -> List[Dict[str, Any]]:
    """분야마다 모델 값 — 결과·유형, 많을수록 좋음(통과). 기준 묶음은 뺌(사용자 10-21 — 기준별은 FAIL 원인 장면)."""
    order = D["order"]
    cats: List[Dict[str, Any]] = []
    ref: Dict[str, Any] = {}
    tot = {p: D["tally"][p] for p in order}
    cats.append({"group": "결과", "label": "전체", "chip": "완전 통과", "higher": True, "of": D["n"], "unit": "문항",
                 "vals": {p: tot[p]["PASS"] for p in order}, "extra": {p: f"FAIL {tot[p]['FAIL']} · 보류 {tot[p]['HOLD']}" for p in order}})
    for ty in ROW_ORDER:
        qs = [q for q, t in D["dtypes"].items() if t == ty]
        if not qs:
            continue
        res = {p: Counter(D["lines"][(p, q)]["verdict"] for q in qs) for p in order}
        if all(c["HOLD"] == len(qs) for c in res.values()):
            continue   # 빈 출력 요구 — 모두 보류('보류' 장면)
        if ty == "10턴 멀티턴 대화":   # 받은 의견 C: 멀티턴도 대화(질문) 단위로 — 턴 단위는 아래 '참고'로 따로
            ok, n_t, held = turn_ok(D, qs)
            ref = {"group": "참고", "label": "멀티턴 턴 단위", "chip": f"{len(qs)}대화 · {n_t}턴 · 보류 턴 포함", "higher": True, "of": n_t, "unit": "턴",
                   "vals": ok, "extra": {p: f"FAIL 있는 턴 {n_t - ok[p]} · 셈에 든 보류 턴 {held[p]}" for p in order}}
        cats.append({"group": "유형", "label": TYPE_NAME.get(ty, ty), "chip": f"{len(qs)}문항", "higher": True, "of": len(qs), "unit": "문항",
                     "vals": {p: res[p]["PASS"] for p in order}, "extra": {p: f"FAIL {res[p]['FAIL']} · 보류 {res[p]['HOLD']}" for p in order}})
    if ref:
        cats.append(ref)
    for c in cats:   # 순위 — 같은 값은 같은 순위
        vals = c["vals"]
        c["rank"] = {p: 1 + sum(1 for o in order if (vals[o] > vals[p] if c["higher"] else vals[o] < vals[p])) for p in order}
        c["sorted"] = sorted(order, key=lambda p: (c["rank"][p], order.index(p)))
    return cats


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


def kst(iso: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """결과 JSON 의 시각(UTC) → 한국 시간."""
    return datetime.fromisoformat(iso).astimezone(timezone(timedelta(hours=9))).strftime(fmt) if iso else ""


def run_info(D: Dict[str, Any]) -> Dict[str, str]:
    """실행 정보 — 결과 ID · 질문 세트 · 실행 시작 · 판정 끝 · 판정 방식 (받은 의견 E: 발표본에서 질문 세트 버전 · 실행 일시 · 결과 식별자를 고정).
    모두 결과 JSON 에서 읽어, 새 결과로 다시 만들면 따라 바뀐다."""
    d, j = D["d"], D["d"]["judgment"]
    s = j.get("summary") or {}
    cx = s.get("codex") or {}
    return {"run": D["run_id"], "set": (d.get("question_set") or {}).get("set_id") or "", "rev": d.get("question_revision") or "",
            "start": kst(d.get("created_at")), "judged": kst(s.get("finished_at") or d.get("exported_at")),
            "pipe": j.get("pipeline") or s.get("version") or "", "codex": " ".join(x for x in (cx.get("model"), cx.get("reasoning")) if x)}


def three_judges(D: Dict[str, Any]) -> bool:
    """이 결과가 심판 셋(Jev · Decisions · Codex) 구조로 판정됐나 — 아니면 흐름도 · 평가 방법의 심판 셋은 '다음 평가 구조'로 표시."""
    return "Jev" in (D["d"]["judgment"].get("pipeline") or "")


def ar_overview(D: Dict[str, Any]) -> str:
    """개요 (사용자 10-21: 줄이기) — 제목 · 질문 수 · 소요 시간만. '확인할 것'은 빼고(내용은 FAIL 원인 · 보류 장면), '전체' 순위 카드는 순위표 장면 맨 위로."""
    judge_s = judge_seconds(D)
    # 제목 하나 · 질문 수 · 소요 시간만 (사용자 10-21: 불필요한 글 지우기). 수집 = 가장 오래 걸린 모델의 답변 시간 합
    collect_s = collect_seconds(D)
    took = (f'<b>{hm(collect_s + judge_s)}</b><span>소요 · 답변 수집 {hm(collect_s)} + 판정 {hm(judge_s)}</span>' if collect_s
            else f'<b>{hm(judge_s)}</b><span>판정 소요</span>')
    hero = (f'<div class="ar-hero" style="grid-template-columns: minmax(0, 1fr)"><div class="ar-hl">'
            '<h1 class="ar-h1"><em>AIONA</em> 채팅 품질 평가</h1>'   # 사용자 10-21: 깔끔한 제목
            f'<div class="ar-stat"><b>{D["n"]}</b><span>질문</span><b style="margin-left:18px">·</b>{took}</div></div></div>')   # 실행 정보 줄은 뺌 (사용자 10-22)
    return hero


def dist_stats(D: Dict[str, Any]) -> Dict[str, Any]:
    """결과 분포 숫자 — 순위(같은 값은 같은 순위) · 보류를 모두 통과로 볼 때 최대 · 1위를 넘볼 수 있는 모델 (받은 의견 A)."""
    order, T = D["order"], D["tally"]
    rank = {p: 1 + sum(1 for o in order if T[o]["PASS"] > T[p]["PASS"]) for p in order}
    lead = min(order, key=lambda p: (rank[p], order.index(p)))
    rivals = [p for p in order if rank[p] > 1 and T[p]["PASS"] + T[p]["HOLD"] >= T[lead]["PASS"]]   # 보류가 모두 통과면 1위와 같거나 넘음
    return {"rank": rank, "lead": lead, "rivals": rivals, "hold": sum(T[p]["HOLD"] for p in order)}


def dist_card(D: Dict[str, Any]) -> str:
    """결과 분포 — 모델마다 완전 통과 · 보류 · FAIL 을 쌓은 막대 (받은 의견 A: 순위만 보이면 확정처럼 읽힘 → 분포와 '잠정').
    초록 끝 = 지금 완전 통과, 노랑 빗금 끝 = 보류가 모두 통과일 때 최대, 점선 = 1위의 지금 완전 통과."""
    order, n, T = D["order"], D["n"], D["tally"]
    st = dist_stats(D)
    lead, lp = st["lead"], T[st["lead"]]["PASS"]
    rows = []
    for p in order:
        P, H, F = T[p]["PASS"], T[p]["HOLD"], T[p]["FAIL"]
        seg = "".join(f'<i class="ds-{k}" style="width:{v / n * 100:.2f}%" title="{lab} {v}"></i>' for k, v, lab in (("p", P, "완전 통과"), ("h", H, "보류"), ("f", F, "FAIL")) if v)
        rows.append(f'<div class="ds-row"><span class="ds-rk">{st["rank"][p]}</span>{avatar(p)}<span class="ds-nm">{SHORT[p]}</span>'
                    f'<div class="ds-bar" role="img" aria-label="{SHORT[p]} 완전 통과 {P} · 보류 {H} · FAIL {F} / {n}">{seg}'
                    f'<b class="ds-ln" style="left:{lp / n * 100:.2f}%" aria-hidden="true"></b></div>'
                    f'<span class="ds-v"><b>{P}</b><small> / {n}</small></span>'
                    f'<span class="ds-x">보류 {H} · FAIL {F} — 보류가 모두 통과면 최대 <b>{P + H}</b></span></div>')
    if st["rivals"]:
        change = (" · ".join(f"{SHORT[r]} 최대 {T[r]['PASS'] + T[r]['HOLD']}" for r in st["rivals"])
                  + f" — 보류를 어떻게 정하느냐에 따라 {SHORT[lead]}({lp})와 순위가 바뀔 수 있습니다.")
    else:
        change = f"보류를 모두 통과로 정해도 {SHORT[lead]}({lp})를 넘는 모델은 없지만, 숫자와 아래 순위는 바뀔 수 있습니다."
    legend = ('<div class="rs-legend ds-leg"><span><i class="rs-sw rs-pass"></i>완전 통과</span><span><i class="rs-sw rs-hold"></i>보류 (사람이 정할 칸)</span>'
              f'<span><i class="rs-sw rs-fail"></i>FAIL</span><span class="muted">점선 = {SHORT[lead]}의 지금 완전 통과 {lp}</span></div>')
    return (f'<div class="ar-card ds-card"><div class="ar-ch"><h3>결과 분포</h3><span class="ds-tag">잠정</span><span class="ar-chip">질문 단위 · 완전 통과 순</span></div>'
            f'<p class="ds-note">이번 {n}개 질문과 이 실행 조건에서 완전 통과가 가장 많은 모델은 <b>{SHORT[lead]}</b>입니다. '
            f'보류 {st["hold"]}칸은 아직 사람이 정하지 않았습니다 — {E(change)}</p>'
            f'{legend}<div class="ds-rows">{"".join(rows)}</div>{unit_note(D)}</div>')


def unit_note(D: Dict[str, Any]) -> str:
    """질문 단위 · 기준 단위 — 두 숫자가 왜 다른지 (받은 의견 B). 보류는 어느 쪽에서도 통과로 세지 않는다."""
    order, n = D["order"], D["n"]
    per = [len(crit_verdicts(D, p, q)) for p in order for q in D["qtypes"] if D["qtypes"][q] != "명시적 빈 출력"]
    cr = crit_results(D)
    tot = {p: sum((cr[c][p] for c in cr), Counter()) for p in order}
    lead = dist_stats(D)["lead"]
    rate = tot[lead]["PASS"] / max(1, sum(tot[lead].values()))
    return (f'<div class="ds-unit"><div><b>질문 단위 — 이 장면</b><p>완전 통과 = 질문 하나에 적용된 기준(평균 {statistics.mean(per):.0f}개 — CODE 검사 · LLM Judge 합)을 <b>모두</b> 통과한 질문 수. '
            f'하나만 어겨도 FAIL, 하나라도 확인 못 하면 보류.</p></div>'
            f'<div><b>기준 단위 — 상세 분석 "기준별 결과"</b><p>기준 판정 하나하나 중 통과한 비율. 그래서 훨씬 높게 나옵니다 '
            f'(예: {SHORT[lead]} 기준 통과율 {rate * 100:.1f}% · 완전 통과 {D["tally"][lead]["PASS"] / n * 100:.0f}%).</p></div>'
            '<div><b>보류</b><p>두 숫자 모두 보류를 통과로 세지 않습니다 — 사람이 정하면 둘 다 바뀝니다.</p></div></div>')


def ar_board(D: Dict[str, Any], cats: List[Dict[str, Any]]) -> str:
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
            rows.append(f'<tr><td class="ar-tr">{c["rank"][p]}</td><td><div class="ar-tm">{avatar(p)}<span><b>{SHORT[p]}</b><small>{E(model_label(D, p))}</small></span></div></td>'
                        f'<td class="ar-tv">{v}<small> / {c["of"]}</small></td>' + (f'<td class="ar-tp2">{v / c["of"] * 100:.0f}%</td>' if c["higher"] else "<td></td>")
                        + f'<td class="ar-tx">{E(c["extra"][p])}</td></tr>')
        panels.append(f'<div class="ar-pn ar-bd{i}"><div class="ar-bh"><div><h3>{E(c["label"])}</h3><span class="ar-chip">{E(c["group"])} · {E(c["chip"])}</span></div></div>'
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
        cols = '<colgroup><col class="hm-mc">' + "<col>" * len(cs) + "</colgroup>"   # 숫자 칸은 같은 너비 (사용자 10-21 저녁: 멀티턴 · 롱컨텍스트 사이가 너무 벌어짐)
        return f'<table class="hm-tab hm-score">{cols}<thead>{head}</thead><tbody>{rows}</tbody></table>'

    res = [c for c in cats if c["group"] in ("결과", "유형")]
    t1 = score_tab(res, lambda c, p: f'{c["vals"][p] / c["of"]:.2f}')
    # 기본 정보
    irows = []
    for p in order:
        ts = [t for (pp, _q, _t), t in D["turns"].items() if pp == p]
        got = [t for t in ts if t.get("status") == "SUCCESS" and (t.get("answer") or "").strip()]
        ok = sum(1 for q in D["qtypes"] if all(D["turns"].get((p, q, n), {}).get("status") == "SUCCESS" for n in range(1, len(D["qset"][q]["turns"]) + 1)))
        irows.append(f'<tr><th scope="row"><span class="hm-m">{avatar(p)}<b>{SHORT[p]}</b></span></th><td class="hm-l">{E(model_label(D, p))}</td><td>{E(method_of(D, p))}</td>'
                     f'<td>{ok} / {D["n"]}</td><td>{sum(1 for t in ts if t.get("status") == "COPY_FAILED")}</td><td>{statistics.mean(len(t["answer"]) for t in got):,.0f}자</td></tr>')
    t4 = ('<table class="hm-tab"><thead><tr><th>모델</th><th>버전 · 설정</th><th>수집</th><th>받은 질문</th><th>재시도할 턴</th><th>턴당 답변 길이</th></tr></thead>'
          f'<tbody>{"".join(irows)}</tbody></table>')
    tabs = [("통과율", "높을수록 좋음 · 완전 통과 ÷ 질문 수 — 멀티턴도 대화(질문) 단위 · 보류는 통과로 세지 않음", t1),
            ("기본 정보", "모델 버전과 수집 방법 · 재시도할 턴 = 복사 버튼이 뜨지 않아 다시 수집할 턴", t4)]
    radios = "".join(f'<input type="radio" name="hm" id="hm{i}" class="ar-r"{" checked" if i == 0 else ""}>' for i in range(len(tabs)))
    labels = '<div class="hm-tabs">' + "".join(f'<label for="hm{i}">{E(t)}</label>' for i, (t, _d, _x) in enumerate(tabs)) + "</div>"
    panels = '<div class="hm-ps">' + "".join(f'<div class="ar-pn hm-p{i}"><p class="hm-d">{E(d)}{" · 분야별 자세한 순위는 상세 분석 '분야별 순위표'" if i == 0 else ""}</p>'
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
    hold_n = {e["n"] for e in ents if e.get("hold")}   # 보류 항목 번호 — 노랑 (10-21)
    merged: List[Tuple[int, int, List[int]]] = []
    for a, b, n in sorted(spans):
        if merged and a < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]), merged[-1][2] + [n])
        else:
            merged.append((a, b, [n]))
    out, pos = [], 0
    for a, b, ns in merged:
        out.append(E(ans[pos:a]))
        cls = "rs-mk rs-mk-hold" if all(n in hold_n for n in ns) else "rs-mk"
        out.append(f'<mark class="{cls}"><sup>{"·".join(map(str, ns))}</sup>{E(ans[a:b])}</mark>')
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
    """지킨 기준 (사용자 10-21: 어떤 기준을 지켰는지) — 의미 평가 통과 항목 · CODE 검사 통과(턴마다), 그리고 해당 없음 기준.
    의미 기준을 심판이 이 답에서 '해당 없음'으로 본 것은 어긴 것이 없으니 통과로 센다(na 표시) — 기준별 결과 표와 같은 규칙,
    같은 질문이면 네 모델 기준 수가 같게 (사용자 10-22: Q75 AIONA 만 LLM Judge 9)."""
    ents: List[Dict[str, Any]] = []
    na: List[str] = []
    for it in D["lines"][(p, q)]["items"]:
        if it["decision"] in ("PASS", "NOT_APPLICABLE"):
            is_na = it["decision"] == "NOT_APPLICABLE"
            ents.append({"turn": int(it["turn"]), "item": it["item"], "kind": "의미", "reason": it.get("reason") or "", "na": is_na,
                         "how": ("해당 없음 — 어긴 것이 없어 통과로 셈 · " if is_na else "") + (it.get("how") or "").replace("PASS", "통과")})
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
                        f'<span class="rs-id">{E(e["item"])}{tt}</span>' + (' <span class="muted">해당 없음</span>' if e.get("na") else "") + '</div>'
                        + (f'<div class="rs-fwhy">{E(e["reason"])}</div>' if e["reason"] else "") + f'<div class="rs-quote">{E(e["how"])}</div></div>')
            sem = [e for e in ents if e["kind"] == "의미"]
            code = [e for e in ents if e["kind"] == "CODE"]
            right = (f'<h4>LLM Judge — 지킨 기준 {len(sem)}개</h4>' + "".join(li(e) for e in sem)
                     + f'<h4 class="rs-h4gap">CODE 검사 — 통과한 검사 {len(code)}개</h4>' + "".join(li(e) for e in code)
                     + (f'<details class="rs-d rs-inner"><summary>해당 없음 {len(na)}개 — 이 질문에 적용되지 않는 기준</summary><p class="rs-quote">'
                        + " · ".join(E(crit_name(D["names"], c) or c) for c in na) + "</p></details>" if na else ""))
            out.append((p, q, f'<article class="rs-fd" id="p-{p}-{q}"><h3 class="rs-fdh">Q{q} · {mname(p)} · {E(TYPE_NAME.get(D["qtypes"][q], ""))} · '
                              f'<span class="rs-oktxt">통과</span> · 지킨 기준 {len(ents)}개</h3>'
                              f'<div class="rs-fx"><div class="rs-fx-l"><h4>원문</h4>{left}</div><div class="rs-fx-r">{right}</div></div></article>'))
    return out


def hold_blocks(D: Dict[str, Any]) -> List[Tuple[str, int, str]]:
    """보류 칸마다 (모델, 질문, HTML) — 왼쪽 질문·답변 원문(사유가 짚은 답 문장은 번호로 칠함), 오른쪽 보류 사유 (사용자 10-21)."""
    out = []
    for p in D["order"]:
        for q in sorted(D["qtypes"]):
            L = D["lines"][(p, q)]
            if L["verdict"] != "HOLD":
                continue
            turns_q = D["qset"][q]["turns"]
            multi = len(turns_q) > 1
            why = hold_why(D, p, q)
            for k, w in enumerate(why, 1):
                w["n"], w["turn"], w["hold"] = k, int(w["turn"]), True
            fents = ([{"turn": int(c["turn"]), "item": c["check_id"], "kind": "CODE", "reason": c["reason"], "quote": "", "found": False} for c in code_fail_rows(D, p, q)]
                     + [{"turn": int(i["turn"]), "item": i["item"], "kind": "의미", "reason": i["reason"] or "; ".join(i.get("problems") or []),
                         "quote": i.get("answer_quote") or "", "qquote": i.get("question_quote") or "", "how": i.get("how") or ""}
                        for i in L["items"] if i["decision"] == "FAIL"])
            for k, e in enumerate(fents, len(why) + 1):
                e["n"] = k
            n_fail = len({e["item"] for e in fents})
            left = "".join(turn_html(D, p, q, tn, multi, [w for w in why + fents if w["turn"] == tn]) for tn in range(1, len(turns_q) + 1))
            if why:
                right = f'<h4>보류 — 확인 못 한 기준 {len(why)}개 ({E(hold_label(why))})</h4>' + "".join(
                    f'<div class="rs-item"><div><b class="rs-hlno rs-hlno-hold{"" if w.get("found") else " rs-hlno-off"}">{w["n"]}</b> '
                    f'<b>{E(crit_name(D["names"], w["item"]) or w["item"])}</b> <span class="rs-id">{E(w["item"])}{" · 턴 " + str(w["turn"]) if multi else ""}</span></div>'
                    f'<div class="rs-fwhy">{E(w["reason"])}</div>'
                    + (f'<div class="rs-quote">답의 해당 문장 “{E(w["quote"][:300])}”</div>' if w["quote"] else "")
                    + f'<div class="rs-quote">{E(w["how"] + (" · Codex 로 보낸 이유: " + w["flagged"] if w["flagged"] else ""))}</div></div>' for w in why)
                if fents:   # 보류 우선(10-21) — FAIL 항목도 같이 보인다
                    right += (f'<h4 class="rs-h4gap">FAIL — 어긴 기준 {n_fail}개</h4>' + "".join(fail_entry_html(D, e, multi) for e in fents)
                              + '<p class="rs-quote">FAIL 항목이 있어 보류를 어떻게 정해도 이 질문은 FAIL 이 됩니다.</p>')
                else:
                    right += '<p class="rs-quote">사유와 답 문장을 보고 맞으면 통과 · 틀리면 FAIL — 사람이 정함</p>'
            else:
                t = D["turns"].get((p, q, 1)) or {}
                a = (t.get("answer") or "").strip()
                got = "재시도 대상 — 복사 버튼이 뜨지 않아 받은 글 없음" if t.get("status") == "COPY_FAILED" else ("빈 답" if not a else f"짧은 글 {len(a)}자")
                is_empty = D["qtypes"][q] == EMPTY_REQ
                right = (f'<h4>보류 사유 — {"빈 출력 요구" if is_empty else "기록"}</h4>'
                         f'<div class="rs-item"><div class="rs-fwhy">{E(L["line"].get("reason") or (EMPTY_WHY if is_empty else "기록 없음"))}</div>'
                         f'<div class="rs-quote">받은 답: {E(got)}</div></div>'
                         + ('<p class="rs-quote">빈 답 · 재시도 대상 · 짧은 글을 각각 통과로 볼지 FAIL 로 볼지 사람이 정함</p>' if is_empty else ""))
            out.append((p, q, f'<article class="rs-fd" id="b-{p}-{q}"><h3 class="rs-fdh">Q{q} · {mname(p)} · {E(TYPE_NAME.get(D["qtypes"][q], ""))} · '
                              f'<span class="rs-holdtxt">보류</span>{" · 확인 못 함 " + str(len(why)) + "개" if why else ""}'
                              f'{" · 어긴 기준 " + str(n_fail) + "개" if n_fail else ""}</h3>'
                              f'<div class="rs-fx rs-holdart"><div class="rs-fx-l"><h4>원문</h4>{left}</div><div class="rs-fx-r">{right}</div></div></article>'))
    return out


def fail_entry_html(D: Dict[str, Any], e: Dict[str, Any], multi: bool) -> str:
    if e["kind"] == "CODE":
        where = '<div class="rs-quote">CODE 검사 — 프로그램이 답 전체를 세거나 형식을 검사한 결과</div>'
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
            f'<span class="rs-id">{E(e["item"])}{tt} · {E(KIND_LABEL.get(e["kind"], e["kind"]))}</span></div>'
            f'<div class="rs-fwhy">{E(e["reason"] or "사유 없음")}</div>{where}{qq}{how}</div>')


def item_html(D: Dict[str, Any], it: Dict[str, Any]) -> str:
    q = f'<div class="rs-quote">답 인용: {E(it["answer_quote"][:220])}</div>' if it.get("answer_quote") else ""
    qq = f'<div class="rs-quote">질문 인용: {E(it["question_quote"][:160])}</div>' if it.get("question_quote") else ""
    how = f'<div class="rs-quote">심판: {E(it["how"])}</div>' if it.get("how") else ""
    lab = {"FAIL": "FAIL", "UNVERIFIED": "보류"}.get(it["decision"], it["decision"])
    return (f'<div class="rs-item"><div><b>턴 {it["turn"]} · <span class="rs-id">{E(it["item"])}</span></b> {E(crit_name(D["names"], it["item"]))} '
            f'<span class="rs-{"failtxt" if it["decision"] == "FAIL" else "holdtxt"}">{E(lab)}</span>{" · " + E(it["kind"]) if it.get("kind") else ""}</div>'
            f'<div>{E(it["reason"] or "; ".join(it.get("problems") or []))}</div>{q}{qq}{how}</div>')


def fail_crit_list(D: Dict[str, Any], p: str, q: int, multi: bool) -> Tuple[str, int]:
    """질문 상세의 어긴 기준 목록 — (HTML, 어긴 기준 수). CODE · 의미 FAIL, 6개까지."""
    crit: Dict[str, Dict[str, Any]] = {}
    for c in code_fail_rows(D, p, q):
        crit.setdefault(c["check_id"], {"turns": set(), "why": c["reason"]})["turns"].add(c["turn"])
    for it in D["lines"][(p, q)]["items"]:
        if it["decision"] == "FAIL":
            crit.setdefault(it["item"], {"turns": set(), "why": it["reason"]})["turns"].add(it["turn"])
    lis = []
    for c, x in list(crit.items())[:6]:
        why = x["why"] or ""
        tt = (" · 턴 " + ",".join(map(str, sorted(x["turns"])))) if multi else ""
        lis.append(f'<li><b>{E(crit_name(D["names"], c))}</b><span class="rs-id">{E(c)}{tt}</span>'
                   f'<span class="rs-why">{E(why[:100])}{"…" if len(why) > 100 else ""}</span></li>')
    more = f'<li class="muted">… {len(crit) - 6}개 더 — 원문에서</li>' if len(crit) > 6 else ""
    return (f'<ul class="rs-ql">{"".join(lis)}{more}</ul>' if crit else ""), len(crit)


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
            ul, n_crit = fail_crit_list(D, p, q, len(turns) > 1)
            fid = f"f-{p}-{q}"
            body = ul + (f'<a class="rs-go" href="{E(part_of[fid])}#{fid}">원문 · 사유 · 답에서 걸린 부분 →</a>' if fid in part_of else "")
            head = f'{badge(v)} <span class="muted">어긴 기준 {n_crit}개</span>'
        elif v == "HOLD":
            t = D["turns"].get((p, q, 1)) or {}
            a = (t.get("answer") or "").strip()
            what = "재시도 대상 — 복사 버튼이 뜨지 않아 받은 글 없음" if t.get("status") == "COPY_FAILED" else ("빈 답" if not a else f"글 {len(a)}자")
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
            ul, n_crit = fail_crit_list(D, p, q, len(turns) > 1)   # 보류 우선(10-21) — 보류 칸에도 FAIL 항목이 있을 수 있다
            if n_crit:
                body += f'<p class="rs-qa">FAIL — 어긴 기준 {n_crit}개 <span class="muted">(보류를 정해도 FAIL)</span></p>{ul}'
            bid = f"b-{p}-{q}"
            body += f'<a class="rs-go" href="{E(part_of[bid])}#{bid}">질문·답변 원문 · 보류 · FAIL 사유 →</a>' if bid in part_of else ""
            head = badge(v) + (f' <span class="muted">확인 못 함 {len(why)} · 어긴 기준 {n_crit}</span>' if n_crit else "")
        else:
            ents, _na = kept_criteria(D, p, q)
            # 사용자 10-21: 이 화면에서 바로 어떤 기준을 지켰는지 — 의미 기준 이름, CODE 는 기준(검사를 기준으로 묶음) 이름
            sem_na: Dict[str, bool] = {}   # 이름마다 — a · b 가 같은 이름이면 하나로, 모두 해당 없음일 때만 '(해당 없음)'
            for e in ents:
                if e["kind"] == "의미":
                    nm = crit_name(D["names"], e["item"]) or e["item"]
                    sem_na[nm] = sem_na.get(nm, True) and bool(e.get("na"))
            sem = [nm + (" (해당 없음)" if na_all else "") for nm, na_all in sem_na.items()]
            code = list(dict.fromkeys(crit_name(D["names"], e["item"].split(":")[0]) or e["item"] for e in ents if e["kind"] == "CODE"))
            pid = f"p-{p}-{q}"
            body = ('<p class="rs-qa">해당하는 기준을 모두 지킴</p>'
                    f'<p class="rs-kept"><b>LLM Judge {len(sem)}</b>{E(" · ".join(sem))}</p>'
                    f'<p class="rs-kept"><b>CODE 검사 {len(code)}</b>{E(" · ".join(code))}</p>'
                    + (f'<a class="rs-go" href="{E(part_of[pid])}#{pid}">질문·답변 원문 · 기준별 근거 →</a>' if pid in part_of else ""))
            head = badge(v)
        rows.append(f'<div class="rs-qm"><div class="rs-qmh"><b>{mname(p)}</b>{head}</div>{body}</div>')
    return (f'<article class="rs-qd" id="q-{q}"><div class="rs-qdh"><span><b>Q{q}</b> {E(TYPE_NAME.get(D["qtypes"][q], ""))}{E(tn)}{E(extra)}</span>'
            f'<a class="rs-x" href="#apx">닫기 ✕</a></div><p class="rs-qt">{E(first[:420])}{"…" if len(first) > 420 else ""}</p>'
            f'<div class="rs-qms">{"".join(rows)}</div></article>')


def appendix(D: Dict[str, Any], parts: List[Tuple[str, str, str]], part_of: Dict[str, str], e_link: str, n_disagree: int, sub: str,
             pparts: Optional[List[Tuple[str, str, str]]] = None, hparts: Optional[List[Tuple[str, str, str]]] = None) -> str:
    """부록 — 탭 셋(라디오 + CSS, JS 없이) · 질문별 결과는 유형마다 4모델 × 문항 칸, 누르면 그 질문만 펼침."""
    order = D["order"]
    # 탭 1 — 질문별 결과: 유형마다 칸
    blocks = []
    for ty in ROW_ORDER:
        qs = sorted(q for q, t in D["dtypes"].items() if t == ty)
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
        groups[("CODE 검사 " + cid.split(".")[0]) if cid[0].isdigit() else cid.split("-")[0]].append(cid)
    gblocks = []
    for g, cids in groups.items():
        rows = []
        for c in cids:
            vals = [len(base_fail[c][p]) for p in order]
            tds = "".join((f'<td class="{"rs-worst" if v == max(vals) else ""}"><details class="rs-cell"><summary>{v}</summary><div class="rs-qchips">'
                           + "".join(f'<a href="#q-{q}">Q{q}</a>' for q in sorted(base_fail[c][p])) + '</div></details></td>') if v else '<td class="muted">0</td>'
                          for v, p in zip(vals, order))
            rows.append(f'<tr><th scope="row"><b>{E(crit_name(D["names"], c))}</b><span class="rs-id">{E(c)}</span></th>{tds}</tr>')
        tot = sum(len(base_fail[c][p]) for c in cids for p in order)
        gname = g if g.startswith("CODE") else f'{g} {GROUP.get(g, "")}'
        heads = "".join(f"<th>{mname(p)}</th>" for p in order)
        gblocks.append(f'<details class="rs-d rs-g"><summary><b>{E(gname)}</b><span class="muted">기준 {len(cids)}개 · 어긴 질문 합 {tot}</span></summary>'
                       f'<table class="rs-ctab"><thead><tr><th></th>{heads}</tr></thead><tbody>{"".join(rows)}</tbody></table></details>')
    p2 = ('<p class="rs-basis">숫자 = 그 기준을 어긴 질문 수 · 빨강 = 가장 많이 어긴 모델. 묶음을 누르면 펼쳐지고, 숫자를 누르면 그 질문 번호가 나옵니다.</p>'
          '<div class="rs-gs">' + "".join(gblocks) + "</div>")
    # 탭 3 — FAIL 원문: 모델마다 질문 번호 → 그 질문의 원문 · 어긴 기준 · 사유 · 답에서 걸린 부분 (사용자 10-21: CODE 만 FAIL 표는 뺌)
    flist = ("".join(f'<li><a href="{E(sub)}/{E(f)}">FAIL 원문 {i}</a><span>{E(desc)}</span></li>' for i, (f, desc, _b) in enumerate(parts, 1))
             + "".join(f'<li><a href="{E(sub)}/{E(f)}">통과 원문 {i}</a><span>{E(desc)}</span></li>' for i, (f, desc, _b) in enumerate(pparts or [], 1))
             + "".join(f'<li><a href="{E(sub)}/{E(f)}">보류 원문{" " + str(i) if len(hparts or []) > 1 else ""}</a><span>{E(desc)}</span></li>' for i, (f, desc, _b) in enumerate(hparts or [], 1)))
    jump = "".join(f'<div><b>{mname(p)}</b>' + "".join(f'<a href="{E(part_of[f"f-{p}-{q}"])}#f-{p}-{q}">Q{q}</a>'
                                                      for q in sorted(D["qtypes"]) if f"f-{p}-{q}" in part_of) + "</div>" for p in order)
    p3 = ('<h3 class="rs-sh">FAIL 원문 — 어긴 기준 · 사유 · 답에서 걸린 부분</h3>'
          '<p class="rs-basis">질문 번호를 누르면 그 FAIL 이 열립니다. 왼쪽은 질문·답변 원문이고, 답에서 심판이 짚은 문장은 빨간 번호로 칠해져 있습니다. '
          '오른쪽은 같은 번호의 어긴 기준과 사유입니다. 통과한 답은 질문별 결과에서 질문을 펼친 뒤 "원문 · 지킨 기준"으로, 또는 아래 통과 원문 파일에서 봅니다.</p>'
          f'<div class="rs-jump">{jump}</div>'
          f'<p class="rs-basis">파일로 보기 — 미리보기 한도(512KB) 때문에 <span class="rs-id">{E(sub)}/</span> 폴더에 나눠 두었습니다.</p>'
          f'<ul class="rs-files">{flist}<li><a href="{E(e_link)}">갈림 목록</a><span>심판이 갈려 Codex 를 따른 항목 {n_disagree:,}개 — 보류 아님, 확인용</span></li></ul>')
    tabs = [("apx1", "질문별 결과", f"{D['n']}문항 × {len(order)}모델", p1), ("apx2", "기준별 결과", "72개 · 어긴 질문 수 · 질문 번호", p2),
            ("apx3", "원문", "FAIL · 통과 · 보류 — 기준 · 사유 · 답", p3)]
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
    for k, p, q in re.findall(r'<article class="rs-fd" id="([fpb])-([a-z]+)-(\d+)"', content):
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
.ds-lead { margin: 0 0 14px; font-size: 1.02rem; }
.ds-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 18px; }
.ds-card { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 16px 18px; display: grid; gap: 10px; align-content: start; min-width: 0; }
.ds-q { margin: 0; line-height: 1.6; } .ds-how { margin: 0; font-size: 0.9rem; color: var(--muted); line-height: 1.55; }
.ds-chart { display: flex; align-items: flex-end; gap: 4px; height: 190px; padding: 8px 4px 0; border-bottom: 1px solid var(--line); }
.ds-col { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 3px; height: 100%; min-width: 0; }
.ds-col em { font-style: normal; font-family: var(--font-num); font-size: 0.68rem; color: var(--muted); }
.ds-col b { font-family: var(--font-num); font-size: 0.72rem; font-weight: 500; color: var(--muted); }
.ds-band em, .ds-band b { color: var(--accent); font-weight: 700; }
.ds-bar { flex: 1; width: 100%; display: flex; flex-direction: column-reverse; border-radius: 4px 4px 0 0; overflow: hidden; background: var(--bg); }
.ds-band .ds-bar { outline: 2px solid var(--accent); outline-offset: 1px; }
.ds-bar i { display: block; width: 100%; } .ds-p { background: var(--ok); } .ds-s { background: var(--accent); } .ds-f { background: var(--risk); }
.ds-lg { display: inline-block; padding: 0 6px; border-radius: 4px; color: var(--surface); font-size: 0.8rem; } .ds-lg.ds-p { background: var(--ok); } .ds-lg.ds-s { background: var(--accent); } .ds-lg.ds-f { background: var(--risk); }
.ds-axis { margin: 0; font-size: 0.78rem; color: var(--muted); }
.ds-find { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
.ds-find li { display: grid; grid-template-columns: 7.5em 9.5em 1fr; gap: 8px; align-items: baseline; font-size: 0.88rem; }
.ds-find li span { font-family: var(--font-num); } .ds-find li em { font-style: normal; color: var(--muted); }
.ds-now { margin: 0; font-size: 0.86rem; border-left: 3px solid var(--accent); padding-left: 10px; color: var(--ink); }
.ds-hbars { display: grid; gap: 8px; }
.ds-hb { display: grid; grid-template-columns: 13em 1fr; gap: 8px; align-items: center; font-size: 0.86rem; }
.ds-hb > div { display: flex; align-items: center; gap: 6px; } .ds-hb i { display: block; height: 14px; border-radius: 3px; background: var(--line); }
.ds-hb b { font-family: var(--font-num); font-weight: 600; } .ds-win i { background: var(--accent); } .ds-win span, .ds-win b { color: var(--accent); font-weight: 700; }
.ds-steps { list-style: none; margin: 0; padding: 0; display: flex; gap: 8px; flex-wrap: wrap; }
.ds-steps li { border: 1px solid var(--line); border-radius: 10px; padding: 6px 10px; display: grid; gap: 1px; min-width: 6.5em; }
.ds-steps li b { font-family: var(--font-num); font-size: 1.1rem; } .ds-steps li span { font-size: 0.76rem; color: var(--muted); }
.ds-steps li:last-child { border-color: var(--accent); } .ds-steps li:last-child b { color: var(--accent); }
@media (max-width: 640px) { .ds-find li { grid-template-columns: 1fr; gap: 0; } .ds-hb { grid-template-columns: 1fr; } }
/* 표지 — 핵심 네 칸 · 말 뜻 */
.rs-tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.rs-tile { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 16px 18px; display: grid; gap: 6px; align-content: start; }
.rs-tile b { font-family: var(--font-num); font-size: 1.9rem; font-weight: 600; line-height: 1.1; }
.rs-tile b small { font-size: 1rem; color: var(--muted); font-weight: 400; }
.rs-tile span { font-size: 0.88rem; color: var(--muted); line-height: 1.45; }
.rs-tile.rs-key { border: 2px solid var(--accent); }
.rs-terms { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin: 0; }
.in-gl { margin-top: 22px; } .in-terms { grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
.rs-terms div { border-top: 3px solid var(--line); padding-top: 8px; }
.rs-terms div.t-pass { border-color: var(--ok); } .rs-terms div.t-fail { border-color: var(--risk); } .rs-terms div.t-hold { border-color: var(--hold); }
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
i.rs-pass, .rs-sw.rs-pass { background: var(--ok); } i.rs-fail, .rs-sw.rs-fail { background: var(--risk); } i.rs-hold, .rs-sw.rs-hold { background: var(--hold-fill); border-color: var(--hold-2); }
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
.rs-hbig { background: var(--hold-soft); border: 1px solid var(--hold); border-radius: 14px; padding: 18px 20px; display: grid; gap: 6px; align-content: center; }
.rs-hbig b { font-family: var(--font-num); font-size: 2.8rem; line-height: 1; color: var(--hold-text); } .rs-hbig b small { font-size: 1.1rem; margin-left: 4px; }
.rs-hbig span { font-size: 0.95rem; }
.rs-facts { display: grid; gap: 8px; align-content: center; }
.rs-fact { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 10px 14px; font-size: 0.9rem; line-height: 1.45; }
.rs-fact b { font-family: var(--font-num); font-size: 1.15rem; margin-right: 8px; }
.rs-mx { gap: 5px; }
.rs-mq { display: grid; align-content: center; font-size: 0.85rem; padding: 2px; min-width: 0; }
.rs-mq b { font-family: var(--font-num); } .rs-mq span { color: var(--muted); font-size: 0.8rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rs-mc { border-radius: 8px; padding: 8px 6px; text-align: center; font-size: 0.84rem; border: 1px solid var(--line); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; align-self: stretch; display: grid; place-items: center; }
.rs-mc-empty, .rs-mc-copy, .rs-mc-text, .rs-mc-fact { background: var(--hold-fill-soft); color: var(--hold-text); border-color: var(--hold); }
.rs-sw.rs-mc-empty, .rs-sw.rs-mc-copy, .rs-sw.rs-mc-text, .rs-sw.rs-mc-fact { background: var(--hold-fill); border-color: var(--hold-2); }   /* 보류 칸은 사유와 상관없이 같은 노랑 빗금 — 사유는 칸 글자로 (사용자 10-21 저녁) */
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
.rs-t-pass { background: var(--ok); } .rs-t-fail { background: var(--risk); } .rs-t-hold { background: var(--hold-fill); color: var(--hold-ink); }
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
.rs-b.rs-pass { color: var(--ok); background: var(--ok-soft); } .rs-b.rs-fail { color: var(--risk); background: var(--risk-soft); } .rs-b.rs-hold { color: var(--hold-text); background: var(--hold-soft); }
.rs-failtxt { color: var(--risk); font-weight: 700; } .rs-holdtxt { color: var(--hold-text); font-weight: 700; }
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
:root { --bg: #f8f7f4; --surface: #ffffff; --ink: #1c1b19; --muted: #6b6a65; --line: #e7e5df; --accent: #2f6fe4; --accent-soft: #eaf1fd; --ar-bar: #dce9fd; --ar-bar-top: #b4d1fb; --ar-up: #16a34a; --nav-bg: rgba(248, 247, 244, 0.94); --font-serif: "Noto Sans KR", "Malgun Gothic", "Apple SD Gothic Neo", sans-serif; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg: #141413; --surface: #1d1d1b; --ink: #ecebe7; --muted: #a3a29c; --line: #2f2e2b; --accent: #7aa7f5; --accent-soft: #1e2a3f; --ar-bar: #213049; --ar-bar-top: #2c4572; --ar-up: #4ade80; --nav-bg: rgba(20, 20, 19, 0.94); } }
:root[data-theme="dark"] { --bg: #141413; --surface: #1d1d1b; --ink: #ecebe7; --muted: #a3a29c; --line: #2f2e2b; --accent: #7aa7f5; --accent-soft: #1e2a3f; --ar-bar: #213049; --ar-bar-top: #2c4572; --ar-up: #4ade80; --nav-bg: rgba(20, 20, 19, 0.94); }
/* 보류 색 (사용자 10-21 저녁: 갈색 → 밝은 노랑 + 빗금, 페이지의 보류 표시 전부) — 신호등처럼 초록 통과 · 빨강 FAIL · 노랑 대기, 밝기로도 구분 */
:root { --hold: #f5c542; --hold-2: #e3ad25; --hold-soft: #fdf2cc; --hold-soft-2: #f7e3a3; --hold-ink: #4a3200; --hold-text: #8a5a00;
  --hold-fill: repeating-linear-gradient(135deg, var(--hold) 0 6px, var(--hold-2) 6px 9px);
  --hold-fill-soft: repeating-linear-gradient(135deg, var(--hold-soft) 0 6px, var(--hold-soft-2) 6px 9px); }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --hold-soft: #3a3014; --hold-soft-2: #4a3d17; --hold-text: #f5c542; } }
:root[data-theme="dark"] { --hold-soft: #3a3014; --hold-soft-2: #4a3d17; --hold-text: #f5c542; }
.s-head h2, .ar-h1, .ar-ch h3, .ar-bh h3, .rs-sh, .brand { font-family: var(--font-serif); font-weight: 600; letter-spacing: -0.01em; }
.ar-h1, .s-head h2, .s-head p, .ar-hl p { word-break: keep-all; }
/* 핵심 장면 한 줄 메시지 · 상세 분석 구분 (사용자 10-21 밤 · 받은 의견 ③) */
.s-head p.s-key { margin: 8px 0 2px; font-size: 1.06rem; font-weight: 600; color: var(--ink); background: var(--accent-soft); border-left: 4px solid var(--accent); border-radius: 0 8px 8px 0; padding: 8px 14px; line-height: 1.55; max-width: 980px; }
.s-div { margin: 56px 0 8px; padding: 16px 20px; border: 1.5px dashed var(--line); border-radius: 14px; display: grid; gap: 4px; }
.s-div b { font-family: var(--font-serif); font-size: 1.35rem; } .s-div span { color: var(--muted); font-size: 0.9rem; word-break: keep-all; }
.toc-sep { flex: none; align-self: center; font-size: 0.72rem; font-weight: 600; color: var(--muted); letter-spacing: 0.04em; padding: 2px 10px; white-space: nowrap; }
@media (min-width: 1200px) { .toc-sep { padding: 14px 10px 4px; border-top: 1px solid var(--line); margin-top: 8px; } }
.s-no { color: var(--muted); }
/* 받은 의견 A~E (10-21 밤) — 실행 정보 줄 · 결과 분포(잠정) · 질문/기준 단위 · 이번/다음 심판 · 멀티턴 턴 참고 */
.rs-box em.rs-jnow, .rs-box em.rs-jnext { border-top: 1px dashed var(--line); padding-top: 3px; } .rs-box em b { font-size: 0.78rem; color: var(--ink); margin-right: 4px; }
.poster-sub b { color: var(--ink); }
.ds-tag { font-size: 0.78rem; font-weight: 700; color: var(--hold-text); background: var(--hold-soft); border: 1px solid var(--hold); border-radius: 999px; padding: 1px 10px; }
.ds-note { margin: 0; font-size: 0.9rem; line-height: 1.6; word-break: keep-all; }
.ds-rows { display: grid; gap: 12px; }
.ds-row { display: grid; grid-template-columns: 20px 26px 64px minmax(0, 1fr) 70px; column-gap: 10px; row-gap: 3px; align-items: center; }
.ds-rk { font-family: var(--font-num); font-weight: 700; color: var(--muted); text-align: center; } .ds-nm { font-weight: 600; }
.ds-bar { position: relative; display: flex; gap: 2px; height: 24px; }
.ds-bar i { display: block; height: 100%; min-width: 2px; } .ds-bar i:first-child { border-radius: 4px 0 0 4px; } .ds-bar i:last-of-type { border-radius: 0 4px 4px 0; }
.ds-p { background: var(--ok); } .ds-h { background: var(--hold-fill); } .ds-f { background: var(--risk); }
.ds-ln { position: absolute; top: -4px; bottom: -4px; width: 0; border-left: 2px dashed var(--ink); opacity: 0.55; }
.ds-v { text-align: right; } .ds-v b { font-family: var(--font-num); font-size: 1.25rem; } .ds-v small { color: var(--muted); font-size: 0.8rem; }
.ds-x { grid-column: 4 / -1; font-size: 0.78rem; color: var(--muted); } .ds-x b { color: var(--hold-text); font-family: var(--font-num); }
.ds-unit { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; border-top: 1px solid var(--line); padding-top: 12px; }
.ds-unit b { font-size: 0.86rem; } .ds-unit p { margin: 2px 0 0; font-size: 0.8rem; color: var(--muted); line-height: 1.5; word-break: keep-all; }
.cb-card small.cb-q { color: var(--ink); border-top: 1px dashed var(--line); padding-top: 4px; }
.hm-ref { margin: 10px 0 0; font-size: 0.8rem; color: var(--muted); line-height: 1.55; word-break: keep-all; } .hm-ref b { color: var(--ink); }
@media (max-width: 760px) { .ds-row { grid-template-columns: 18px 24px minmax(0, 1fr) 64px; } .ds-bar { grid-column: 1 / -1; order: 5; } .ds-x { grid-column: 1 / -1; order: 6; }
  .ds-unit { grid-template-columns: minmax(0, 1fr); } }
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
.ar-h1 em { font-style: normal; color: var(--accent); }
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
.hm-score { table-layout: fixed; min-width: 600px; } .hm-score col.hm-mc { width: 20%; }
.hm-score thead th:not(:first-child), .hm-score td { text-align: center; } .hm-score thead th { white-space: normal; }
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
mark.rs-mk-hold { background: var(--hold-soft); border-bottom-color: var(--hold); }
mark.rs-mk-hold sup, .rs-hlno.rs-hlno-hold { background: var(--hold); color: var(--hold-ink); }
.rs-hlno.rs-hlno-hold.rs-hlno-off { background: var(--muted); }
a.rs-mca { color: inherit; text-decoration: none; display: block; } a.rs-mca:hover { text-decoration: underline; }
.rs-arch { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; margin: 0 0 6px; }
.rs-arch span { display: grid; gap: 1px; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 7px 12px; font-size: 0.8rem; color: var(--muted); }
.rs-arch span b { color: var(--ink); font-size: 0.9rem; } .rs-arch i { color: var(--accent); font-style: normal; font-weight: 700; }
.rs-ext { color: var(--accent); }
.rs-crit { margin-top: 16px; } .rs-crit > summary .muted { margin-left: 10px; } .rs-cls { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 14px; margin-top: 10px; }
.rs-cl h4 { margin: 0 0 6px; font-size: 0.92rem; } .rs-cl ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 5px; }
.rs-cl li { display: grid; gap: 1px; font-size: 0.84rem; border-top: 1px dashed var(--line); padding-top: 4px; } .rs-cl li span { color: var(--muted); font-size: 0.78rem; }
/* 채점 기준 72개 (사용자 10-21 저녁: 더 읽기 쉽고 직관적으로) — 위 기준 지도(늘 보임) · 아래 접힌 전체 목록은 두 단으로 흘림 */
.cr-map { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; margin-top: 18px; }
.cr-half { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; display: grid; gap: 8px; align-content: start; }
.cr-half h4 { margin: 0; font-size: 0.98rem; display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.cr-half h4 b { font-family: var(--font-num); font-size: 1.15rem; } .cr-half h4 span { font-size: 0.8rem; color: var(--muted); font-weight: 400; }
.cr-top { display: grid; grid-template-columns: 7.5em minmax(0, 1fr); gap: 8px; align-items: start; }
.cr-tn { font-size: 0.82rem; font-weight: 600; color: var(--accent); padding-top: 3px; }
.cr-chips { display: flex; flex-wrap: wrap; gap: 5px; }
.cr-chip { font-size: 0.8rem; border: 1px solid var(--line); border-radius: 999px; padding: 2px 9px; background: var(--bg); white-space: nowrap; } .cr-chip b { font-family: var(--font-num); margin-left: 2px; }
.cr-when { grid-column: 1 / -1; display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; font-size: 0.82rem; color: var(--muted); }
.cr-when b { color: var(--ink); font-family: var(--font-num); } .cr-wl { font-weight: 600; color: var(--ink); }
.cr-w { font-style: normal; font-size: 0.72rem; font-weight: 600; border-radius: 4px; padding: 1px 6px; white-space: nowrap; flex: none; }
.cr-al { background: var(--accent-soft); color: var(--accent); } .cr-as { background: var(--bg); color: var(--muted); border: 1px solid var(--line); } .cr-an { background: var(--warn-soft); color: var(--warn); }
.cr-list { columns: 2 380px; column-gap: 16px; margin-top: 12px; }
.cr-g { break-inside: avoid; background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 10px 14px; margin: 0 0 14px; }
.cr-g h5 { margin: 0 0 6px; font-size: 0.95rem; display: flex; align-items: baseline; gap: 8px; }
.cr-gk { font-family: var(--font-num); color: var(--accent); font-size: 0.85rem; } .cr-n { margin-left: auto; font-size: 0.76rem; color: var(--muted); font-weight: 400; }
.cr-g ul { list-style: none; margin: 0; padding: 0; } .cr-g li { border-top: 1px solid var(--line); padding: 7px 0; display: grid; gap: 3px; }
.cr-h { display: flex; align-items: baseline; gap: 6px; } .cr-h b { font-size: 0.88rem; }
.cr-id { margin-left: auto; font-family: var(--font-num); font-size: 0.7rem; color: var(--muted); white-space: nowrap; }
.cr-ex { margin: 0; font-size: 0.78rem; color: var(--muted); }
.cr-chk { display: flex; gap: 6px; align-items: baseline; font-size: 0.82rem; line-height: 1.5; }
.cr-ab { font-family: var(--font-num); font-size: 0.72rem; color: var(--accent); border: 1px solid var(--accent-soft); border-radius: 4px; padding: 0 4px; flex: none; }
.cr-q { min-width: 0; word-break: keep-all; } .cr-q small { display: block; color: var(--muted); font-size: 0.76rem; }
@media (max-width: 760px) { .cr-map, .cr-top { grid-template-columns: minmax(0, 1fr); } }
/* 판정 프롬프트 원문 (사용자 10-22) */
.pr-d { margin-top: 12px; } .pr-d > summary .muted { margin-left: 10px; } .pr-pre { max-height: 70vh; font-size: 0.8rem; line-height: 1.55; }
.pr-link a { font-weight: 600; }
/* CODE 검사 · LLM Judge 두 색 (사용자 10-22) */
:root { --code-c: #0f766e; --code-soft: #e6f4f1; --llm-c: #6d3fc0; --llm-soft: #f1ebfb; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --code-c: #5fd4c2; --code-soft: #14302c; --llm-c: #b9a0f0; --llm-soft: #2a2140; } }
:root[data-theme="dark"] { --code-c: #5fd4c2; --code-soft: #14302c; --llm-c: #b9a0f0; --llm-soft: #2a2140; }
.cr-kind, .cr-sh .cr-kind { display: inline-block; border-radius: 6px; padding: 2px 9px; font-size: 0.82rem; font-weight: 700; color: var(--surface); }
.cr-sec { margin-top: 14px; border-radius: 14px; padding: 12px 14px 2px; }
.cr-sec-code { background: var(--code-soft); } .cr-sec-llm { background: var(--llm-soft); }
.cr-sh { margin: 0; display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; font-size: 1rem; }
.cr-sh b { font-family: var(--font-num); font-size: 1.2rem; } .cr-sh > span:last-child { font-size: 0.84rem; color: var(--muted); font-weight: 400; }
.cr-sec-code .cr-kind { background: var(--code-c); } .cr-sec-code .cr-sh b { color: var(--code-c); }
.cr-sec-llm .cr-kind { background: var(--llm-c); } .cr-sec-llm .cr-sh b { color: var(--llm-c); }
.cr-g-code { border-left: 4px solid var(--code-c); } .cr-g-code .cr-gk { color: var(--code-c); }
.cr-g-llm { border-left: 4px solid var(--llm-c); } .cr-g-llm .cr-gk { color: var(--llm-c); }
/* CODE 검사 결과 (사용자 10-21 저녁) — 모델마다 통과 · 걸림 막대, 아래 걸린 검사 색 격자(FAIL 원인과 같은 모양) */
.cd-bars { display: grid; gap: 10px; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 16px 18px; }
.cd-row { display: grid; grid-template-columns: 120px minmax(0, 1fr) 260px; gap: 14px; align-items: center; }
.cd-m { font-weight: 600; } .cd-bar { display: flex; height: 26px; border-radius: 6px; overflow: hidden; gap: 2px; background: var(--line); }
.cd-bar span { display: flex; align-items: center; justify-content: center; font-family: var(--font-num); font-size: 0.82rem; font-weight: 600; min-width: 1.6em; }
.cd-ok { background: var(--ok-soft); color: var(--ok); } .cd-bad { background: var(--risk); color: var(--surface); }
.cd-v { font-size: 0.92rem; } .cd-v b { font-family: var(--font-num); font-size: 1.1rem; } .cd-v small { display: block; color: var(--muted); font-size: 0.76rem; line-height: 1.4; }
.cd-key { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 0.82rem; margin: 8px 0 0; align-items: center; }
.cd-key i { display: inline-block; width: 12px; height: 12px; border-radius: 3px; vertical-align: -1px; margin-right: 5px; }
@media (max-width: 760px) { .cd-row { grid-template-columns: minmax(0, 1fr); gap: 6px; } }
/* 검증 — 확인한 것 · 남은 것 · 실제 사례 (사용자 10-21 저녁) */
.vf-cols { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); gap: 18px; align-items: start; }
.vf-col { display: grid; gap: 8px; } .vf-col h3 small { font-family: var(--font-num); font-size: 0.85rem; color: var(--muted); margin-left: 4px; }
.vf-i { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 10px 14px 10px 16px; position: relative; }
.vf-done .vf-i { border-left: 4px solid var(--ok); border-radius: 0 12px 12px 0; } .vf-left .vf-i { border-left: 4px solid var(--hold); border-radius: 0 12px 12px 0; }
.vf-i b { font-size: 0.95rem; display: flex; align-items: baseline; gap: 8px; } .vf-i p { margin: 3px 0 0; font-size: 0.86rem; line-height: 1.55; word-break: keep-all; }
.vf-i small { display: block; margin-top: 4px; font-size: 0.74rem; color: var(--muted); }
.vf-st { margin-left: auto; font-size: 0.72rem; font-weight: 600; color: var(--hold-text); background: var(--hold-soft); border-radius: 4px; padding: 1px 7px; white-space: nowrap; }
.vf-cases { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.vf-case { background: var(--surface); border: 1px solid var(--line); border-top: 4px solid var(--line); border-radius: 0 0 12px 12px; padding: 12px 14px; display: grid; gap: 6px; align-content: start; }
.vf-ok { border-top-color: var(--ok); } .vf-hold { border-top-color: var(--hold); } .vf-bad { border-top-color: var(--risk); }
.vf-tag { font-size: 0.74rem; font-weight: 700; } .vf-ok .vf-tag { color: var(--ok); } .vf-hold .vf-tag { color: var(--hold-text); } .vf-bad .vf-tag { color: var(--risk); }
.vf-ct { font-size: 0.92rem; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; } .vf-ct .rs-id { font-weight: 400; }
.vf-case dl { margin: 0; display: grid; gap: 2px; font-size: 0.84rem; } .vf-case dt { color: var(--muted); font-size: 0.74rem; margin-top: 4px; } .vf-case dd { margin: 0; line-height: 1.5; word-break: keep-all; }
.vf-q { background: var(--bg); border-radius: 6px; padding: 4px 8px; }
.vf-note { margin: 4px 0 0; font-size: 0.8rem; color: var(--muted); line-height: 1.5; word-break: keep-all; } .vf-link { font-size: 0.82rem; }
@media (max-width: 1000px) { .vf-cols, .vf-cases { grid-template-columns: minmax(0, 1fr); } }
/* 무엇을 만들었고, 왜 필요한가 (사용자 10-21 저녁) — 위 한 줄 정의 + 흐름, 아래 두 단(만든 것 5 · 이유 4) */
.in-hero { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 18px 22px; display: grid; gap: 12px; }
.in-def { margin: 0; font-size: 1.12rem; line-height: 1.6; word-break: keep-all; } .in-def b { font-family: var(--font-serif); font-size: 1.2rem; color: var(--accent); }
.in-flow { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; }
.in-flow span { background: var(--accent-soft); color: var(--accent); border-radius: 999px; padding: 4px 12px; font-size: 0.88rem; font-weight: 600; } .in-flow i { color: var(--muted); font-style: normal; }
.in-cols { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 18px; margin-top: 6px; }
.in-built { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }
.in-built li { display: grid; grid-template-columns: 30px minmax(0, 1fr); gap: 10px; background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; }
.in-n { width: 26px; height: 26px; border-radius: 50%; background: var(--accent); color: var(--surface); display: grid; place-items: center; font-family: var(--font-num); font-size: 0.85rem; font-weight: 700; }
.in-built b { font-size: 0.98rem; } .in-built em { font-style: normal; margin-left: 8px; color: var(--accent); font-weight: 600; font-size: 0.9rem; }
.in-built p, .in-why p { margin: 3px 0 0; font-size: 0.88rem; color: var(--muted); line-height: 1.55; word-break: keep-all; }
.in-why { display: grid; gap: 10px; } .in-why > div { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px 16px; }
.in-why b { font-size: 0.98rem; }
@media (max-width: 860px) { .in-cols { grid-template-columns: minmax(0, 1fr); } }
.in-why2 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
/* 결과 · 보류 — 제공 방식 (사용자 10-22) */
.ot-card { background: var(--surface); border: 1px dashed var(--line); border-radius: 14px; padding: 12px 16px; }
.ot-cap { font-size: 0.78rem; color: var(--muted); margin-bottom: 8px; }
.ot-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr); gap: 14px; }
.ot-src { background: var(--bg); border-radius: 10px; padding: 10px 12px; } .ot-src b { font-size: 0.88rem; } .ot-src p { margin: 2px 0 10px; font-size: 0.84rem; color: var(--muted); }
.ot-items { display: grid; gap: 8px; align-content: start; }
.ot-it { display: grid; grid-template-columns: 64px minmax(0, 1fr); gap: 8px; align-items: start; } .ot-it .rs-b { justify-self: start; }
.ot-it b { font-size: 0.9rem; } .ot-it small, .ot-v small { color: var(--muted); font-size: 0.78rem; } .ot-it p { margin: 2px 0 0; font-size: 0.82rem; color: var(--muted); line-height: 1.5; word-break: keep-all; }
.ot-v { border-top: 1px solid var(--line); padding-top: 8px; font-size: 0.9rem; font-weight: 600; }
.ot-rule { margin-top: 14px; background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 10px 16px; display: grid; gap: 6px; }
.ot-rule > b { font-size: 0.9rem; } .ot-rule > div { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; font-size: 0.88rem; }
.ot-rule i { font-style: normal; color: var(--muted); } .ot-rule small { color: var(--muted); font-size: 0.78rem; }
.ot-steps { grid-template-columns: repeat(2, minmax(0, 1fr)); }
@media (max-width: 860px) { .ot-grid, .ot-steps, .in-why2 { grid-template-columns: minmax(0, 1fr); } }
/* 기준별 결과 (사용자 10-21 저녁) — 위 모델별 기준 통과율 카드, 아래 묶음 16개 행(누르면 기준별로 펼침) */
.cb-cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.cb-card { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 14px 16px; display: grid; gap: 4px; }
.cb-r { font-size: 0.78rem; color: var(--muted); } .cb-m { font-weight: 600; }
.cb-card b { font-family: var(--font-num); font-size: 1.7rem; line-height: 1.2; } .cb-card small { color: var(--muted); font-size: 0.78rem; }
.cb-key { font-size: 0.82rem; color: var(--muted); margin: 10px 0 6px; }
.cb-table { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 6px 12px; }
.cb-row { display: grid; grid-template-columns: minmax(200px, 1.7fr) repeat(4, minmax(0, 1fr)); gap: 6px; align-items: center; padding: 5px 0; }
.cb-head { font-size: 0.8rem; color: var(--muted); border-bottom: 1px solid var(--line); text-align: center; } .cb-head span:first-child { text-align: left; }
.cb-row > * { min-width: 0; } .cb-head span { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.cb-g { border-top: 1px solid var(--line); } .cb-head + .cb-g { border-top: 0; }
.cb-g > summary { list-style: none; cursor: pointer; } .cb-g > summary::-webkit-details-marker { display: none; }
.cb-g > summary:hover { background: var(--bg); } .cb-g > summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.cb-l { display: flex; align-items: baseline; gap: 8px; min-width: 0; flex-wrap: wrap; } .cb-l b { font-size: 0.9rem; } .cb-l small { color: var(--muted); font-size: 0.74rem; }
.cb-g > summary .cb-l::before { content: "▸"; color: var(--muted); font-size: 0.8rem; } .cb-g[open] > summary .cb-l::before { content: "▾"; }
.cb-c { border-radius: 8px; padding: 5px 6px; text-align: center; display: grid; } .cb-c b { font-family: var(--font-num); font-size: 0.95rem; } .cb-c small { font-size: 0.7rem; color: var(--muted); }
.cb-sub { padding: 0 0 6px; } .cb-sub .cb-row { padding: 3px 0; border-top: 1px dashed var(--line); } .cb-sub .cb-l { padding-left: 18px; } .cb-sub .cb-l b { font-weight: 500; font-size: 0.84rem; } .cb-sub .cb-c b { font-size: 0.86rem; }
.cb-na { color: var(--muted); }
@media (max-width: 760px) { .cb-cards { grid-template-columns: repeat(2, minmax(0, 1fr)); } .cb-row { grid-template-columns: minmax(96px, 1.2fr) repeat(4, minmax(0, 1fr)); gap: 3px; } .cb-c small { display: none; } }
.rs-next .rs-sub { color: var(--muted); font-size: 0.84rem; }
.rs-next { grid-template-columns: repeat(3, minmax(0, 1fr)); }   /* 사용자 10-22: 카드 6개를 3 · 3 으로 — 하네스 증류 · 심판 셋 · 수집 방식이 한 줄 */
@media (max-width: 900px) { .rs-next { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 560px) { .rs-next { grid-template-columns: minmax(0, 1fr); } }
details.rs-cell > summary { cursor: pointer; list-style: none; } details.rs-cell > summary::-webkit-details-marker { display: none; }
details.rs-cell[open] > summary { text-decoration: underline; } details.rs-cell .rs-qchips { margin-top: 4px; }
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
.toc-sep.toc-end { display: none; }   /* 사용자 10-22: LLM Judge 묶음 끝 — 넓은 화면에서만 선 */
@media (min-width: 1200px) { .toc-sep.toc-end { display: block; padding: 0; } }
/* 사용자 10-22: 줄바꿈 정리 — 본문은 마지막 줄에 한두 단어만 남지 않게(pretty), 문단 · 장면 위 강조 · 안내 문장은 줄 길이를 고르게(balance) */
.slide, .ar-s0 { text-wrap: pretty; }
.s-head p, .slide p { text-wrap: balance; }
@media (max-width: 560px) { .slide p { text-wrap: pretty; } }   /* 좁은 화면은 긴 문단이 많아 고아 줄 방지만 */
/* 사용자 10-22: 나란히 놓인 상자의 위아래 끝 맞추기 */
@media (min-width: 861px) {
  .in-cols > .in-col { display: flex; flex-direction: column; }   /* 결과 — 왼쪽 3 · 오른쪽 4 카드가 열 높이를 나눠 채움 */
  .in-cols .in-built, .in-cols .in-why { flex: 1; grid-auto-rows: 1fr; }
}
.ar-cats { align-self: stretch; align-content: start; }   /* 분야별 순위표 — 왼쪽 메뉴 상자를 표 아래 끝까지 */
.rs-facts { align-content: stretch; grid-auto-rows: 1fr; }   /* 보류 목록 — 오른쪽 상자가 왼쪽 큰 상자 높이를 채움 */
.rs-fact { display: grid; align-content: center; gap: 4px; }   /* 늘어난 상자 안 글은 세로 가운데로 */
.vf-cols { grid-template-columns: minmax(0, 1fr); gap: 22px; }   /* 검증 — 확인한 것 · 남은 것을 위아래로, 각각 두 칸씩 줄 맞춤 */
.vf-col { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; } .vf-col > h3 { grid-column: 1 / -1; }
@media (max-width: 760px) { .vf-col { grid-template-columns: minmax(0, 1fr); } }
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


#: 줄 안 바뀌는 공백으로 붙일 묶음 말 (사용자 10-22: 줄바꿈 정리 — 'LLM / Judge' 처럼 갈라지지 않게)
KEEP_TOGETHER = ["LLM Judge", "CODE 검사", "평가 프롬프트", "판정 프롬프트", "완전 통과", "빈 출력", "질문 세트", "상세 분석", "분야별 순위표",
                 "보류 목록", "확인 못 함", "Decisions API", "Codex CLI"]
NBSP, WJ = chr(0xA0), chr(0x2060)
BIND_RULES = [
    (re.compile(r"""(?<=[A-Za-z0-9%)\]])[ ](?=(?:가|는|은|이|를|을|의|로|으로|와|과|도|에|에서|만|까지|부터|처럼|보다|나|이나|라는|이라는|로는|으로는|에는|에도|와의|과의|이며|이고|이면|이라|라)(?=[\s.,·:;)\]?!’”'"]|$))"""),
     NBSP),                                                                   # 영문 · 숫자 뒤에 띄어 쓴 조사 ('LLM Judge 는')
    (re.compile(r"[ ](?=[·—→=+×/](?: |$))"), NBSP),                           # 가운뎃점 · 줄표 · 화살표 · 셈 기호가 줄 앞에 오지 않게
    (re.compile(NBSP + r"([/=+×]) "), lambda m: NBSP + m.group(1) + NBSP),   # 셈 기호는 뒤도 붙임 ('55 / 78' 한 덩어리)
    (re.compile(r"(FAIL|보류|통과|PASS|질문|기준|모델|턴)[ ](?=[0-9])"), lambda m: m.group(1) + NBSP),   # 이름 + 숫자 ('FAIL 91')
    (re.compile(r"""([)\]’”'])(?=[가-힣])"""), lambda m: m.group(1) + WJ),   # 괄호 · 따옴표 뒤 조사 ('(Codex)도')
]


def bind_words(page: str) -> str:
    """본문 글자(태그 · 스크립트 · 스타일 · 원문 블록 pre/code 밖)만 — 묶음 말 · 조사 · 기호 앞뒤 공백을 줄 안 바뀌는 공백으로 (사용자 10-22). 글자 자체는 그대로."""
    parts = re.split(r"(<script\b.*?</script>|<style\b.*?</style>|<pre\b.*?</pre>|<code\b.*?</code>|<textarea\b.*?</textarea>|<[^>]+>)", page, flags=re.S)
    for i in range(0, len(parts), 2):
        t = parts[i]
        for w in KEEP_TOGETHER:
            t = t.replace(w, w.replace(" ", NBSP))
        for pat, sub in BIND_RULES:
            t = pat.sub(sub, t)
        parts[i] = t
    return "".join(parts)


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
    hparts = split_parts(D, hold_blocks(D), prefix="보류")   # 보류 칸 — 원문 · 보류 사유 (사용자 10-21)
    sub = f"ModelGrade_결과요약_{run_id}_원문"   # 원문 파일은 이 하위 폴더에 (사용자 10-21: 발표 폴더 정리)
    part_of = {m: f"{sub}/{f}" for f, _d, body in parts + pparts + hparts for m in re.findall(r'<article class="rs-fd" id="([fpb]-[a-z]+-\d+)"', body)}
    e_file = "갈림.html"
    n_disagree, e_table = disagree_table(D)
    cats = board_categories(D)
    n_hold = sum(1 for L in D["lines"].values() if L["verdict"] == "HOLD")
    judge_s = judge_seconds(D)
    # 사용자 10-21 밤(받은 의견 ③): 발표용 핵심 장면과 상세 분석을 한 파일 안에서 나눔 — 핵심은 평가 흐름 → 전체 흐름도 순서, 장면마다 한 줄 메시지.
    # 장면 id(링크용)는 내용에 붙어 있고 번호(01…)는 이 순서로 매긴다. (id, 메뉴 이름, 부제, 제목, 설명, 내용, 한 줄 메시지)
    n_code = sum(1 for c in D["names"] if c[0].isdigit())
    ds = dist_stats(D)   # 받은 의견 A: 순위는 잠정 — 보류를 정하면 바뀔 수 있음을 한 줄 메시지에
    res_key = (f"잠정 — 이번 {D['n']}개 질문 · 이 실행 조건에서 완전 통과 " + " · ".join(f"{SHORT[p]} {D['tally'][p]['PASS']}" for p in D["order"])
               + f" (/ {D['n']}). 보류 {ds['hold']}칸을 아직 정하지 않아 " + ("순위가 바뀔 수 있습니다." if ds["rivals"] else "숫자가 바뀔 수 있습니다."))
    n_hold_empty = sum(1 for (_p, q), L in D["lines"].items() if L["verdict"] == "HOLD" and D["qtypes"][q] == "명시적 빈 출력")
    core = [
        ("s_intro", "왜 필요한가", "만든 이유", "왜 만들었나", "", scene_intro(D),
         "AIONA 가 업데이트될 때마다 같은 조건으로 다시 테스트해, 채팅 품질을 꾸준히 높이기 위해 만들었습니다."),
        ("s_design", "질문 설계", "왜 78개 · 왜 기준 6~10개", "질문 설계 — 왜 78개, 왜 질문마다 기준 6~10개", "", scene_design(D),
         "기준 6~10개는 모델 차이가 가장 잘 드러나는 난이도, 78개는 모든 기준을 질문 10개 이상에서 평가하는 가장 적은 수입니다."),
        ("s2", "평가 방법", "다섯 단계", "평가 흐름", "", scene_flow(D)
         + '<p class="rs-basis pr-link"><a href="#s_prompt">판정 프롬프트 원문 보기 → 상세 분석 \'판정 프롬프트\'</a></p>',   # 사용자 10-22
         f"질문 {D['n']}개 → {len(D['order'])}모델 답 → 기준 {len(D['names'])}개(CODE 검사 {n_code} + LLM Judge {len(D['names']) - n_code})로 채점 → 질문마다 완전 통과 · FAIL · 보류"),
        ("s_map", "전체 흐름도", "A~H 단계", "전체 흐름도",
         f"질문 {D['n']}개를 네 모델에 던져 답을 받고, CODE 검사와 LLM Judge 가 채점해 질문마다 완전 통과·FAIL·보류를 냅니다.", flow_scene(D),
         "질문 세트부터 보고서까지 명령 하나로 돌고, AI 가 끼는 단계는 답변 수집(B)과 LLM Judge(E) 두 곳입니다."),
        # 05 검증 장면은 뺌 (사용자 10-22: "이 검증파트는 빼도 될 것 같네") — scene_verify 는 남겨 둠
        ("s_out", "결과", "어떻게 제공하나", "결과 — 이렇게 제공합니다",
         "실행이 끝나면 명령 하나로 결과 JSON · 판정 보고서 · 결과 요약이 나옵니다. 이번 실행의 숫자는 보류를 정하기 전이라 상세 분석에 따로 둡니다.",
         scene_output(D), "질문마다 완전 통과 · FAIL · 보류와 기준별 사유 · 원문을 함께 제공합니다 — 이번 실행 숫자는 상세 분석 '이번 실행 결과'에."),
        ("s_hold", "보류", "어떻게 사람에게 넘기나", "보류 — 이렇게 사람에게 넘깁니다",
         "보류는 심판이 확정하지 못한 칸입니다. 원문과 사유를 붙여 사람에게 넘기고, 사람이 정한 값으로 다시 셉니다.",
         scene_hold_flow(D), "확인하지 못한 판정은 억지로 정하지 않고 원문 · 사유와 함께 사람에게 넘깁니다 — 이번 실행의 목록은 상세 분석 '보류 목록'에."),
        ("s7", "발전 방향", "확장 · 개선", "발전 방향", "", scene_next(D),
         "가장 먼저 사람 판정과 맞춰 보고, 보류 사례집 · 심판 셋 구조로 다시 판정 · 다른 AI 서비스 연결로 넓힙니다."),
    ]
    detail = [   # 상세 분석 — 발표 뒤 필요할 때 보는 화면
        ("s1", "이번 실행 결과", "분포 · 유형별 통과율 (잠정)", "이번 실행 결과 — 분포와 유형별 통과율 (잠정)",
         "맨 위는 모델마다 완전 통과 · 보류 · FAIL 분포, 그 아래는 유형별 통과율입니다. 모두 질문 단위이고, 보류를 사람이 정하기 전의 잠정 결과입니다.",
         dist_card(D) + helm_table(D, cats), res_key),
        ("s1b", "분야별 순위표", "분야를 고르면 바뀜", "분야별 순위표",
         "분야를 고르면 순위표가 바뀝니다. '이번 실행 결과'의 통과율 숫자를 눌러도 이 표가 그 분야로 바뀝니다.", ar_board(D, cats), ""),
        ("s_prompt", "판정 프롬프트", "심판이 받는 글 원문", "판정 프롬프트 — 심판이 받는 글 원문",
         "", scene_prompt(D), ""),
        ("s_crit", "기준별 결과", "기준마다 통과율", "기준별 결과 — 기준마다 통과율",
         "질문 하나를 통째로 보는 '완전 통과'와 달리, 기준 하나하나가 적용된 질문에서 얼마나 통과했는지 봅니다.", scene_by_crit(D), ""),
        ("s_code", "CODE 검사", "프로그램이 센 결과", "CODE 검사 결과 — 프로그램이 센 것만",
         "형식 · 개수 · 글자 수 · 계산처럼 프로그램이 정해진 규칙으로 센 결과만 따로 봤습니다. 막대는 CODE 검사를 모두 통과한 질문과 하나라도 걸린 질문입니다.", scene_code(D), ""),
        ("s5", "걸린 시간", "수집 · 판정", "걸린 시간 — 답변 수집과 LLM Judge 판정",
         f"답변 수집 {hm(collect_seconds(D))} · 판정 {hm(judge_s)}. 답변 수집은 모델마다 질문을 보내고 답을 받기까지 걸린 시간의 합(아래 막대) 중 가장 긴 것입니다.", scene_time(D), ""),
        ("s3", "FAIL 원인", "많이 어긴 기준", "FAIL 원인 — LLM Judge 가 많이 어긴 기준 10개",
         "숫자는 그 기준을 어긴 질문 수입니다. 색이 진할수록 많이 어겼습니다.", scene_criteria(D), ""),
        ("s4", "보류 목록", f"사람이 정할 {n_hold}칸", f"보류 목록 — 사람이 정할 {n_hold}칸 (빈 출력 규칙 {n_hold_empty} · LLM Judge 확인 못 함 {n_hold - n_hold_empty})",
         "보류는 심판이 확정하지 못한 기준이 남은 칸입니다. 지금은 사람이 정하고, 그 판단을 사례로 모아 다음 판정에 쓰는 것은 만드는 중입니다.", scene_hold(D, part_of),
         f"사람이 정할 칸 {n_hold} = 빈 출력 요구 {n_hold_empty} + 확인 못 함 {n_hold - n_hold_empty} — 아직 정하지 않아, 정하면 순위 · 통과율이 바뀔 수 있습니다."),
        ("s6", "질문별 결과", "78문항 × 4모델 · 원문", "질문별 결과",   # 사용자 10-22: 제목 뒤 '— 필요할 때만 펼치기' · 안내 문장 지움
         "", appendix(D, parts, part_of, f"{sub}/{e_file}", n_disagree, sub, pparts, hparts), ""),
    ]
    scenes = core + detail
    total, n_core = len(scenes), len(core)

    def head(i: int, title: str, lead: str, key: str) -> str:
        tag = f"{i:02d} / {total:02d}" + (" · 상세 분석" if i > n_core else "")
        return (f'<div class="s-head"><span class="s-no">{tag}</span><h2>{title}</h2>'
                + (f'<p class="s-key">{E(key)}</p>' if key else "") + (f"<p>{lead}</p>" if lead else "") + "</div>")

    s0 = f'<header class="ar-s0">{ar_overview(D)}</header>'   # 사용자 10-21: 제목 줄은 맨 위에 두고 메뉴의 개요는 뺌
    # 사용자 10-22: 본문의 '상세 분석' 구분 상자는 뺌 (목차 구분 · 장면 번호 옆 '· 상세 분석'은 그대로)
    nav_sep = {"s5": '<span class="toc-sep">LLM Judge</span>', "s6": '<span class="toc-sep toc-end" aria-hidden="true"></span>'}   # 사용자 10-22: LLM Judge 묶음
    body = "".join(f'\n<section class="slide" id="{sid}">\n  {head(i, E(title), E(lead), key)}\n{content}</section>\n'
                   for i, (sid, _s, _d, title, lead, content, key) in enumerate(scenes, 1))
    nav = ('<nav class="topnav" aria-label="순서"><div class="topnav-in"><span class="brand">AIONA 채팅 품질 평가</span>'
           '<div class="toc-links" id="toc-links">'
           + "".join(('<span class="toc-sep">상세 분석</span>' if i == n_core + 1 else "") + nav_sep.get(sid, "") + f'<a href="#{sid}"><span class="n">{i:02d}</span>{E(short)}</a>'
                     for i, (sid, short, *_r) in enumerate(scenes, 1))
           + '</div><div class="pager"><button type="button" id="prev" aria-label="이전 장">←</button><button type="button" id="next" aria-label="다음 장">→</button></div></div></nav>')
    ids = ", ".join(f"'{s}'" for s in [x[0] for x in scenes])
    script = re.sub(r"var ids = \[[^\]]*\];", f"var ids = [{ids}];", nav_script, count=1)
    logos = logo_style()

    def doc(title: str, desc: str, inner: str) -> str:
        return (f'<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                f'<title>{E(title)}</title>\n<meta name="description" content="{E(desc)}">\n'
                '<link rel="preconnect" href="https://fonts.googleapis.com">\n<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
                '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+KR:wght@400;500;600;700&family=Noto+Sans+KR:wght@500;600;700&display=swap">\n'
                f'{prune_css(css + chr(10) + EXTRA_CSS + chr(10) + FLOW_CSS + chr(10) + logos, inner)}\n</head>\n<body>\n{inner}\n{OPEN_JS}\n</body>\n</html>\n')

    main_name = MAIN_NAME
    out = OUT_DIR / main_name
    out.write_text(bind_words(doc("AIONA 채팅 품질 평가", f"78개 실행 {run_id} — 흐름도 · 결과 · 유형별 · 많이 어긴 기준 · 보류 · 걸린 시간 · 부록",
                                  f'{nav}\n<main class="wrap">\n{s0}\n{body}\n</main>\n{script}')), encoding="utf-8")
    subdir = OUT_DIR / sub
    subdir.mkdir(exist_ok=True)
    # 지난번에 만든 원문 파일 — 예전에는 발표 폴더에 바로 두었고(…_원문1.html), 나눈 수도 바뀔 수 있음
    for old in [*OUT_DIR.glob(f"ModelGrade_결과요약_{run_id}_원문*.html"), *subdir.glob("*.html")]:
        old.unlink()
    files = ([(f, f"FAIL 원문 {i}", d) for i, (f, d, _b) in enumerate(parts, 1)] + [(f, f"통과 원문 {i}", d) for i, (f, d, _b) in enumerate(pparts, 1)]
             + [(f, f"보류 원문 {i}" if len(hparts) > 1 else "보류 원문", d) for i, (f, d, _b) in enumerate(hparts, 1)]
             + [(e_file, "갈림 목록", f"심판이 갈려 Codex 를 따른 항목 {n_disagree:,}개")])

    def links(cur: str) -> str:
        return (f'<a href="../{E(main_name.replace(" ", "%20"))}#apx">← 결과 요약(부록)으로</a> · ' + " · ".join(
            f"<b>{E(lab)}</b>" if f == cur else f'<a href="{E(f)}">{E(lab)}</a>' for f, lab, _d in files))

    written = [out]
    for f, lab, d in files:
        content = next((b for pf, _d, b in parts + pparts + hparts if pf == f), None)
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
    if FLOW_WARN:
        print("흐름도 — 칸보다 긴 글:", *FLOW_WARN, sep="\n  ")
    problems = check_against_report(D) + big
    print("report.html 과 같음 · 파일마다 미리보기 한도 안" if not problems else "\n".join(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
