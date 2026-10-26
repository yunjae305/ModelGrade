"""결과 요약 발표 페이지 (result-summary-v1) — 78개 실행 하나를 발표 6장면 + 부록으로 (사용자 10-21: report.html 은 정보가 너무 많아
발표하기 어려움 → 10.20 미팅 기준으로 흐름도 · 결과 · 유형별 · 많이 어긴 기준 · 보류 · 걸린 시간, 원문은 부록).
AI 호출 없음. 내보낸 결과(참고자료/결과/<실행>_결과_판정결과.json)와 질문 세트만 읽고, 판정 셈은 tools/report_html 과 같은 함수를 쓴다
(숫자가 그 실행의 판정 보고서와 같아야 한다 — 끝에서 맞춰 본다). 디자인은 10-19 발표 덱 CSS 를 그대로 읽어 쓴다(10-20 보고와 같은 모양).

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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import report_html as R  # noqa: E402
from tools.result_flow import FLOW_CSS, WARN as FLOW_WARN, flow_scene  # noqa: E402
from code_eval.model import code_group  # noqa: E402   # CODE 검사 ID → 소분류 (10-26 에러 케이스 대분류)

RUN = "a1642702fccc"
RESULTS = ROOT / "참고자료" / "결과"
OUT_DIR = ROOT / "참고자료" / "발표"
MAIN_NAME = "ModelGrade_결과요약_10-20 발표자료.html"   # 본문 파일 이름 (사용자 10-21: 고치면 이 파일에 바로, 따로 파일 만들지 않음)
DECK = ROOT / "tools" / "assets" / "result_summary_base.html"   # 디자인 바탕(10-19 덱의 CSS · 목차 스크립트, 원본 덱은 10-22 정리 때 지움)
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


def result_file(run_id: str, name: str, flat: bool = False) -> Path:
    """실행의 결과 파일 — 결과 폴더(참고자료/결과/<시작 시각>/, tools/result_dirs, 사용자 10-22)에 있으면 그것,
    없으면 옛 자리(결과 JSON 은 참고자료/결과/ 바로 아래, 판정 기록은 참고자료/결과/판정_<실행>/)."""
    from tools import result_dirs
    rd = result_dirs.find(run_id, RESULTS)
    if rd is not None and (rd / name).exists():
        return rd / name
    return RESULTS / name if flat else RESULTS / f"판정_{run_id}" / name


def results_json(run_id: str) -> Path:
    """결과 JSON — 이름은 평가 종류에 따라 <실행>_결과_판정결과 · _간단78_판정결과 · _정밀326_판정결과 (tools/modelgrade report).
    결과 폴더에 있으면 그것, 없으면 옛 자리(참고자료/결과/ 바로 아래)."""
    from tools import result_dirs
    rd = result_dirs.find(run_id, RESULTS)
    for base in ([rd] if rd is not None else []) + [RESULTS]:
        hits = sorted(base.glob(f"{run_id}_*판정결과.json"))
        if hits:
            return hits[0]
    return RESULTS / f"{run_id}_결과_판정결과.json"


def judge_report_file(run_id: str) -> Path:
    """판정 보고서(tools/report_html) — 결과 폴더의 판정보고서.html, 없으면 옛 자리 참고자료/결과/판정_<실행>/report.html.
    결과 폴더의 report.html 은 이 빌더가 만드는 결과 보고서라 판정 보고서가 아니다 (사용자 10-22)."""
    from tools import result_dirs
    rd = result_dirs.find(run_id, RESULTS)
    if rd is not None and (rd / result_dirs.JUDGE_REPORT).exists():
        return rd / result_dirs.JUDGE_REPORT
    return RESULTS / f"판정_{run_id}" / "report.html"


def logo_style() -> str:
    """모델 로고 CSS — 그림은 48px PNG(svg 는 그대로)로 줄여 data URI 로 한 번만 넣는다. 흰 칸 위에 두어 어두운 화면에서도 보이게."""
    import base64
    import io
    rules = []
    for p, name in LOGO_FILES.items():
        f = ROOT / "tools" / "assets" / name   # 10-23 코드 정리: frontend/ 를 지우며 로고만 tools/assets 로
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
    d = json.loads(results_json(run_id).read_text(encoding="utf-8"))
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
    if it.get("kind") in R.GUARD_KINDS:   # 판정이 붙인 종류(내보낸 JSON 의 kind) 그대로 (10-22)
        return it["kind"]
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
        '<li>모델을 고르지 않으면 AIONA 자동 추천과 네 모델. AIONA 에서 모델을 고르면(예: Sonnet 5.5) 같은 모델의 앱(Claude 앱 Sonnet 5.5)과 둘만 — GPT · Gemini 는 빠짐</li>'
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
        """'…했는가 (보충)' → ('…했는가', '보충') — 문장 끝에 붙은 괄호 하나만 보충 설명으로 흐리게.
        문장 중간 괄호('결과물(비교 결과 …)을')는 본문에 둔다 (사용자 10-22: 첫 괄호에서 잘라 문장이 끊기던 것)."""
        s = s.strip()
        if not s.endswith(")"):
            return s, ""
        depth = 0
        for i in range(len(s) - 1, -1, -1):
            depth += {")": 1, "(": -1}.get(s[i], 0)
            if depth == 0:
                break
        main = s[:i].rstrip()
        return (main, s[i + 1:-1]) if depth == 0 and main else (s, "")

    # CODE — 세 갈래 · 여덟 묶음
    cgroups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in sorted(codes, key=lambda c: c["id"]):
        cgroups[code_group(c["id"])].append(c)
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
    ("질문당 기준 수(k)", "질문이 요구하는 조건(기준)의 수 — 6~10개로 맞췄습니다(근거는 '질문 설계'). 모든 답에 늘 보는 기준 · 답에 따라 보는 기준은 따로 셉니다.", ""),
]


def scene_intro(D: Dict[str, Any]) -> str:
    """왜 만들었나 — 흐름도 앞 (사용자 10-21 저녁 · 10-22: 프로젝트 이름은 미정이라 이름 · '무엇을 만들었나' 없이 필요성만). 숫자는 이번 실행에서 다시 셈.
    근거: 10-19 · 10-20 발표 표지(같은 질문 → 자동 채점 → 비교), 참고자료/기준/채팅품질 평가기준.md(AIONA ↔ 외부 시스템, 같은 질문 집합 ·
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
            + scene_intro_diff(D)
            + '<h3 class="rs-sh in-gl">처음 보시는 분을 위한 용어</h3><dl class="rs-terms in-terms">'
            + "".join(f'<div{f" class={chr(34)}{c}{chr(34)}" if c else ""}><dt>{E(t)}</dt><dd>{E(d)}</dd></div>' for t, d, c in TERMS)
            + "</dl>")


#: 이번 실행에서 찾은 AIONA 제품 쪽 문제 — 답을 원문과 대조한 점검(참고자료/분석/2026-10-23-멀티턴싱글턴점검 가설 25 · 26 · 34, 팀원 질문 점검)
AIONA_FOUND = "일정 질문에 위기 상담 안내만 낸 답 3턴 · 답 끝에 붙는 데이터 출처 줄 · 코드 들여쓰기가 한 칸으로 뭉개진 답 2건"


def scene_intro_diff(D: Dict[str, Any]) -> str:
    """왜 필요한가 — 다른 평가와 무엇이 다른가 · 차별점 · 꼭 이 평가기를 써야 하는 이유 (10-22 미팅 12번, 사용자 10-23: "차별화 요소가 꼭 한 개가
    아니어도 … 꼭 우리 평가기를 써야만 하는 이유 … 다른 평가기 모델과 비교해서"). 다른 평가는 방식의 일반적인 특징만 적는다(숫자 비교 없음)."""
    n_crit = len(D["names"])
    n_code = sum(1 for c in D["names"] if c[0].isdigit())
    cmp = table("", ["", "우리 평가기", "공개 순위표 <br><small>(LMArena 등)</small>", "공개 벤치마크 <br><small>(HELM · MT-Bench · 한국어 리더보드)</small>",
                     "개발용 평가 도구 <br><small>(promptfoo · DeepEval · LangSmith 등)</small>"], [
        ["평가하는 답", "<b>앱 · 웹 화면에서 사용자가 받는 답 그대로</b> (AIONA 포함)", "API 로 연결된 모델의 답", "모델의 답(API · 공개 모델)", "개발자가 연결한 API · 엔드포인트의 답"],
        ["비교 단위", "<b>서비스</b> — AIONA ↔ 같은 모델의 원래 앱", "모델", "모델", "직접 정함"],
        ["판정 기준", f"<b>질문마다 미리 정한 기준 6~10개</b> (전체 {n_crit}개 = 프로그램 {n_code} + AI 심판 {n_crit - n_code})", "정하지 않음 — 투표자의 선호",
         "정답 맞히기 · 1~10점", "직접 만들어야 함"],
        ["결과", "<b>질문마다 완전 통과 · FAIL · 보류 + 기준별 사유 · 답의 해당 문장</b>", "순위 점수", "과제별 점수", "직접 만든 검사의 점수 · 통과율"],
        ["확신이 없을 때", "<b>보류로 사람에게</b> — 원문 · 사유와 함께", "—", "그래도 점수를 냄", "그래도 점수를 냄"],
        ["질문", "<b>우리 업무형 한국어</b> — 10턴 대화 · 긴 문서 · 첨부 · 함정", "투표자가 낸 질문", "정해진 공개 문항(짧은 질문 · 2턴 대화가 많음)", "직접 만들어야 함"],
    ], [13, 27, 18, 21, 21])
    cards = [
        ("1 · 실제 답", "사용자가 받는 답 그대로", "API 가 아니라 앱 · 웹 화면의 답을 받아 평가합니다. AIONA 의 지시문 · 도구 · 화면 처리까지 들어간 품질이라, "
         f"이번 실행에서 {AIONA_FOUND}처럼 모델 점수로는 보이지 않는 제품 문제를 찾았습니다."),
        ("2 · 같은 모델끼리", "AIONA 가 더한 값만 따로", "AIONA 에서 고른 모델과 같은 모델의 원래 앱(예: Sonnet 5.5 ↔ Claude 앱 Sonnet 5.5)을 같은 질문으로 비교합니다. "
         "모델이 같으니 차이는 AIONA 가 더한 값입니다 — 모델 짝 18개 준비."),
        ("3 · 고칠 곳까지", "점수 하나가 아니라 이유", f"질문마다 볼 기준을 미리 정해(기준 {n_crit}개 모두 10문항 이상) 기준별 통과율 · 사유 · 답의 해당 문장이 나옵니다. "
         "다음 업데이트에서 무엇을 고칠지가 바로 보입니다."),
        ("4 · 우리 업무 문제", "한국어 · 실제 업무 난이도", "10턴 · 약 10만 토큰 대화(조건 변경 · 근거 없는 반박 · 빠진 자료), 52만 자 문서, 첨부, 질문마다 설계한 함정 — "
         "공개 문항은 짧은 질문이나 2턴 대화가 많습니다."),
        ("5 · 틀린 채 굳지 않음", "확신 없으면 사람에게", "프로그램 검사(같은 답이면 같은 결과) + 두 단계 AI 심판, 확신 없는 칸은 보류로 원문과 함께 넘깁니다. "
         "판정이 쌓일수록 기준을 다듬습니다 — 이번 주 708칸을 원문과 대조해 기준 · 확인 목록을 고쳤습니다."),
        ("6 · 같은 잣대로 계속", "업데이트마다 한 바퀴", "명령 하나로 수집 → 판정 → 보고서, 지난 회차와 같은 기준으로 비교하고 질문 · 모델 · 심판별 시간 · 토큰까지 남깁니다."),
    ]
    card_html = ('<div class="rs-cards rs-next">' + "".join(
        f'<div class="rs-card"><span class="rs-ct">{E(st)}</span><b>{E(t)}</b><p>{E(body)}</p></div>' for st, t, body in cards) + "</div>")
    must = note("다른 평가가 답하지 못하는 질문",
                "<p><b>다른 평가는 '어느 모델이 대체로 나은가'에 답합니다. 우리 평가기는 'AIONA 가 우리 사용자의 일에서 같은 모델의 원래 앱보다 나은가, "
                "아니라면 무엇을 고쳐야 하나'에 답합니다 — 이 질문에는 다른 평가가 답하지 못합니다.</b></p>"
                "<p>① AIONA 는 공개 순위표 · 벤치마크에 없습니다 — API 가 아닌 서비스라 직접 받아 평가해야 합니다.</p>"
                "<p>② 모델 점수로는 AIONA 를 알 수 없습니다 — 같은 모델이라도 AIONA 안과 원래 앱의 답이 다르고, 품질 문제는 서비스 쪽에서도 생깁니다"
                f"(이번 실행: {E(AIONA_FOUND)}).</p>"
                "<p>③ 업데이트마다 나빠진 곳을 기준별로 잡아야 합니다 — 순위 하나로는 무엇이 나빠졌는지 모릅니다.</p>"
                "<p>④ 내부 품질 관리와 외부 제안에 같은 근거를 씁니다 — 판정마다 답의 원문 · 사유가 남아 그대로 보여 줄 수 있습니다.</p>")
    return ('<h3 class="rs-sh">다른 평가와 무엇이 다른가</h3>' + cmp
            + '<h3 class="rs-sh">차별점</h3>' + card_html
            + '<h3 class="rs-sh">꼭 이 평가기를 써야 하는 이유</h3>' + must)


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
    gbars = "".join(f'<div class="ds-hb{" ds-win" if r["n"] == gen["questions"] else ""}"><span>{r["n"]}개</span><div><i style="width:{r["k10"] / gc * 80:.1f}%"></i>'   # 숫자 칸이 꺾이지 않게 80% 까지
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
    cards = "".join(f'<div class="cb-card"><span class="cb-m">{mname(p)}</span><b>{rate[p] * 100:.1f}%</b>'
                    f'<small>기준 판정 {sum(tot[p].values()):,}건 · FAIL {tot[p]["FAIL"]} · 보류 {tot[p]["HOLD"]}</small>'
                    f'<small class="cb-q">질문 단위 완전 통과 {D["tally"][p]["PASS"]} / {D["n"]} ({D["tally"][p]["PASS"] / D["n"] * 100:.0f}%)</small></div>' for p in by)

    def cell(c: Counter) -> str:
        n = sum(c.values())
        if not n:
            return '<div class="cb-c cb-na">—</div>'
        return (f'<div class="cb-c" style="{tint(min(1.0, c["FAIL"] / n / 0.4), "--risk")}" title="통과 {c["PASS"]} · FAIL {c["FAIL"]} · 보류 {c["HOLD"]} · 평가 {n}">'
                f'<b>{c["PASS"] / n * 100:.0f}%</b><small>FAIL {c["FAIL"]} · 보류 {c["HOLD"]} / {n}</small></div>')   # 받은 의견 B: 보류 수도

    ids = sorted(D["names"], key=lambda c: (0 if c[0].isdigit() else 1, c))
    groups: Dict[str, List[str]] = defaultdict(list)
    for cid in ids:
        groups[code_group(cid) if cid[0].isdigit() else cid.split("-")[0]].append(cid)
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
    key = ('<p class="cb-key"><b>칸 색 = 그 묶음을 어긴 정도(빨갈수록 FAIL 많음)</b> · 큰 숫자 = 통과율 · 작은 숫자 = FAIL · 보류 / 평가된 질문</p>'   # 이사님 10-23: 빨간 표의 뜻을 먼저
           f'<p class="cb-key">묶음을 누르면 기준별로 펼쳐짐 · 빈 출력 요구 {n_empty}문항은 보류라 뺌 · 멀티턴은 한 턴이라도 FAIL 이면 그 질문은 FAIL</p>')
    return (f'<div class="cb-cards">{cards}</div>'
            '<p class="cb-key">기준 통과율 = 기준 판정 중 통과한 비율(해당 없음은 통과로, 보류는 통과로 세지 않음). 같은 질문에는 네 모델 모두 같은 기준을 매겨 판정 건수가 같습니다. '
            '모델마다 90%대로 높으니 차이는 FAIL 수로 보세요 — 질문 하나의 기준을 모두 통과해야 하는 완전 통과보다 훨씬 높게 나옵니다.</p>'
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
    files = [   # 사용자 10-22: 평가를 시작할 때마다 시각 이름의 결과 폴더 하나에 — report.html 은 이 템플릿의 상세 분석 장면만
        ("결과 보고서", "report.html",
         "이 자료의 상세 분석 장면만 모은 것 — 이번 실행 결과 · 분야별 순위표 · 기준별 결과 · CODE 검사 · 걸린 시간 · FAIL 원인 · 보류 목록 · 질문별 원문"),
        ("결과 JSON", "…_판정결과.json",
         "질문 × 모델마다 판정, 기준마다 판정 · 사유 · 답의 해당 문장, 답 원문과 수집 정보(복사 방식 · 걸린 시간). 다른 프로그램이 읽는 원본"),
        ("판정 보고서", "판정보고서.html",
         "모든 판정 기록 — 기준별로 어긴 질문, FAIL 상세(심판이 든 원문 인용), 질문별 판정표, 보류를 사람이 정하는 화면"),
    ]
    units = [
        ("질문 단위 — 순위", f"모델마다 완전 통과 · FAIL · 보류가 몇 개인지 (/ 질문 {n}개). 순위는 완전 통과 수로 매깁니다."),
        ("유형 · 분야별", "싱글턴 · 멀티턴 · 롱컨텍스트 · 첨부 자료마다, 기준 묶음(분야)마다 통과율과 순위표."),
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
    화면 · 반영은 판정 보고서(판정보고서.html) '보류' 장 · held-apply(human_decisions.json, 원본 results.jsonl 은 그대로), 사례 축적은 계획."""
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


def scene_prompt(D: Dict[str, Any], open_first: bool = True) -> str:
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
            + block("1차 심판(Jev · Decisions) — 판정 프롬프트",   # 사용자 10-22: Jev · Decisions 것은 하나로 — 실제로 안내문과 기준별 질문을 한 번에 보냄
                    f"안내문 + 기준별 질문 {len(allq)}개 · 질문마다 [판정할 항목]에 든 기준의 질문만 골라 함께 보냄",
                    guide + f"\n\n[기준별 질문 {len(allq)}개 — 질문마다 [판정할 항목]에 든 것만 보냄 · 예(PASS) · 아니오(FAIL) · 해당 없음(NOT_APPLICABLE) 중 하나]\n\n" + items,
                    open_first)
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
         "표시용 표(FAIL 60 · 통과 40칸)는 만들어 두었습니다.", "다음 할 일"),
        ("모델 확장", "다른 모델도 같은 질문으로", "답변 수집은 앱 · 웹브라우저 자동화라, 연결만 하면 다른 모델도 같은 78문항으로 받아 같은 기준으로 비교합니다.", "구조 준비됨"),
        ("같은 모델 비교", "AIONA 에서 고른 모델 ↔ 같은 모델의 앱", "모델을 고르지 않으면 AIONA 자동 추천과 네 모델을 평가합니다. AIONA 에서 모델을 고르면(예: Sonnet 5.5) "
         "같은 모델의 앱(Claude 앱 Sonnet 5.5)과 둘만 평가하고 GPT · Gemini 는 빠집니다 — 같은 모델 · 같은 질문이라도 앱과 AIONA 안에서 답이 달라, "
         "AIONA 쪽이 더 낫다면 그것이 강점입니다. 모델 짝 18개 · 실제 전환 확인.", "기능 준비됨 · 결과는 다음 실행"),   # 10-22 미팅 1번 · 사용자 10-23
        ("보류 사례집", "사람의 판단을 데이터로", "보류를 사람이 통과 · FAIL 로 정하면 사례로 쌓고, 다음 판정에서 비슷한 경우의 근거로 씁니다. 반복되는 판단은 채점 기준 · 프롬프트로 올립니다.", "만드는 중"),
        (("한 번 요청 판정", "질문 · 답을 한 파일로", "이번 실행은 이미 심판 셋(Jev · Decisions · Codex)으로 판정했습니다. 다음은 수집한 질문 · 답을 한 파일로 모아 "
          "심판이 그 파일을 읽어 한 번 요청으로 평가합니다 — 호출과 재시도를 줄입니다.", "만드는 중") if three_judges(D) else   # 사용자 10-22: 이미 쓰는 심판 셋을 '만드는 중'으로 적던 것
         ("심판 셋 + 한 번 요청", "Jev · Decisions · Codex", "세 심판을 함께 쓰고, 수집한 질문 · 답을 한 파일로 모아 심판이 그 파일을 읽어 한 번 요청으로 평가합니다 — 호출과 재시도를 줄입니다.", "만드는 중")),
        ("수집 방식", "복사의 한계", "ChatGPT 앱은 복사할 때 코드 줄바꿈을 지웁니다. 화면 캡처로 받아 보는 방식을 검토합니다.", "검토"),
    ]
    return ('<div class="rs-cards rs-next">' + "".join(
        f'<div class="rs-card"><span class="rs-ct">{E(st)}</span><b>{E(t)}</b><div class="rs-sub">{E(sub)}</div><p>{E(body)}</p></div>'
        for t, sub, body, st in cards) + "</div>")


#: 10-23 v4-r3 판정(저장 출력 재사용 없이 처음부터, 결과 폴더 2026-10-23_12-41-05-662 · 토큰기록.md) — 한 바퀴 판정에 드는 것
FULL_JUDGE = {"minutes": 71, "input": "1억 2,126만", "output": "157만", "calls": "1,425"}


def scene_close(D: Dict[str, Any]) -> str:
    """마감 · 상시 운영 (10-22 미팅 11번: 다음 주까지 닫을 범위와 일정, 그 뒤 일부 리소스로 상시 운영하며 패치를 어떻게 관리할지 — 일정이 바뀌면 미리 알림).
    팀원 초안(개선 예정 작업 6장) + 10-23 판정 종합 1차에서 남긴 일. 일정 · 범위는 사용자 확정(10-23)."""
    def ul(xs: List[str]) -> str:
        return "<ul>" + "".join(f"<li>{E(x)}</li>" for x in xs) + "</ul>"
    inside = ["범위 고정 — 78문항 · 4모델 · 기준 72개, 수집 → 판정 → 보고서 명령 하나",
              "새 규칙으로 다시 판정(멀티턴 11 × 4) → 결과 보고서 · 발표 숫자 갱신",
              "보류 사람 결정 — 빈 출력 10문항 기준 · 설명 턴 사실 확인 10칸 → 최종 숫자",
              "심판 표시용 표 100칸 — 기준마다 거짓 FAIL · 거짓 통과 비율",
              "기준 다듬기 한 바퀴(J1-4.b) — 같은 칸 고치기 전 · 후 비교",
              "차별점 하나 (미팅 12번)"]
    after = ["싱글 함정 다시 설계 — 한 줄 함정 → 자료 속에 섞인 함정",
             "Codex 입력 — 대화 10턴을 한 번에 → 판정할 턴까지만",
             "CODE 빈틈 넷 — 가정 문장의 합계 · 시각 덧셈 · 구조가 아예 없으면 통과 · 겹침 다시 묻기 낱말",
             "수집 흔적 — AIONA 출처 번호 지움 · ChatGPT 이스케이프 · 앱 오류 문구는 다시 받기",
             "싱글 1차 확신 문턱 · 롱컨텍스트 근거 위치 기준",
             "같은 모델 비교 첫 실행 · 원 단위 비용(단가가 정해지면) · 기준 다듬기 도구"]
    scope = ('<div class="rs-cards rs-close">'
             f'<div class="rs-card"><span class="rs-ct">마감 안에</span><b>다음 주에 끝내는 것</b>{ul(inside)}</div>'
             f'<div class="rs-card"><span class="rs-ct">마감 뒤로</span><b>상시 운영 때 패치로 넘기는 것</b>{ul(after)}</div></div>')
    sched = table("", ["날짜", "할 일", "끝나면 나오는 것"], [
        ["10-26 (월)", "새 규칙으로 다시 판정 — 멀티턴 11 × 4", "결과 보고서 · 고치기 전 · 후 비교"],
        ["10-27 (화)", "보류 사람 결정 · 심판 표시용 표 100칸", "최종 숫자 · 기준별 거짓 FAIL · 거짓 통과 비율"],
        ["10-28 (수)", "기준 다듬기 한 바퀴 — J1-4.b", "흔들리는 기준을 숫자로 · 받을지 결정"],
        ["10-29 (목)", "발표 자료 숫자 · 장면 최종 반영 · 차별점 하나", "보고 자료 확정"],
        ["10-30 (금)", "마감 보고 — 범위 · 결과 · 상시 운영 계획", "마감"],
    ], [16, 46, 38])
    run = (f"수집 {hm(collect_seconds(D))} · 판정 {FULL_JUDGE['minutes']}분, 판정 입력 {FULL_JUDGE['input']} · 출력 {FULL_JUDGE['output']} 토큰 · "
           f"호출 {FULL_JUDGE['calls']}번(10-23 처음부터 판정한 기준). 사람은 보류 칸 결정 · 기준 정의 승인만.")
    ops = [("언제", "AIONA 업데이트마다 한 바퀴", "같은 78문항으로 수집 → 판정 → 보고서를 돌리고, 지난 회차와 같은 잣대로 비교합니다."),
           ("한 바퀴에 드는 것", "일부 리소스로 충분", run),
           ("질문 세트", "분기마다 손봄", "기준마다 10문항 이상 · 질문당 기준 6~10개를 지키며 고칩니다 — 싱글 함정 다시 설계부터.")]
    cards = ('<div class="rs-cards rs-next">' + "".join(
        f'<div class="rs-card"><span class="rs-ct">{E(st)}</span><b>{E(t)}</b><p>{E(body)}</p></div>' for st, t, body in ops) + "</div>")
    steps = [("모으기", "판정이 끝날 때마다 FAIL · 보류 칸과 통과 표본(두 심판이 갈린 칸 · 확신 낮은 칸 · 무작위)을 모읍니다."),
             ("원인 나누기", "답이 틀림 · 프롬프트 · 기준 · 질문 · 수집 가운데 하나로 나눕니다."),
             ("고치기", "기준 v2 문서를 먼저 고치고(기준 정의는 사람이 승인) 프롬프트 · 확인 목록 · 코드가 따라갑니다."),
             ("시험", "같은 칸을 고치기 전 · 후로 다시 판정해, 정답률이 오르고 뒤집힘이 줄고 다른 기준이 나빠지지 않을 때만 받습니다."),
             ("기록 · 알림", "변경내역 · 기준 v2 '바뀐 규칙' 표 · 변경 원문 페이지에 남기고 다음 보고서에 함께 냅니다. 일정이나 규칙이 바뀌면 미리 알립니다.")]
    step_html = '<ol class="jc-steps">' + "".join(f"<li><b>{E(t)}</b><span>{E(b)}</span></li>" for t, b in steps) + "</ol>"
    example = note("이번 주 한 바퀴 (10-23)", "<p>멀티턴 · 싱글 표본 177개(708칸)를 원문과 대조해 원인을 나눴습니다 → 기준 v2 를 먼저 고치고 확인 목록(왕복 = 편도 두 번 · 최종 턴 · 모순 턴 판정) · "
                   "판정 프롬프트(J1-4.b 경계 · 거절 답 말투) · 거절 턴 규칙을 고쳤습니다 → 효과는 다시 판정(10-26)으로 확인합니다.</p>")
    return ('<h3 class="rs-sh">마감 범위 — 다음 주 (10-26 ~ 10-30)</h3>' + scope
            + '<h3 class="rs-sh">일정 (10-23 확정 — 바뀌면 그날 바로 알림)</h3>' + sched
            + '<h3 class="rs-sh">이후 상시 운영 — 일부 리소스로</h3>' + cards
            + '<h3 class="rs-sh">패치는 이렇게 관리합니다</h3>' + step_html + example
            + '<p class="rs-close-end">만든 것을 AIONA 품질 관리에 상시로 쓰고, 외부에 제안할 때도 씁니다.</p>')


def judged_lines(D: Dict[str, Any]) -> List[Dict[str, Any]]:
    """판정 기록(results.jsonl)의 실제 판정 줄 — 다시 판정 · 복구 포함. 이름 되살림 · 다시 계산(recode) 줄은 앞 줄을 옮겨 적은 것이라 뺀다."""
    f = result_file(D["run_id"], "results.jsonl")
    if not f.exists():
        f = ROOT / "data" / "judge_runs" / D["run_id"] / "v4" / "results.jsonl"
    if not f.exists():
        return []
    out = []
    for x in f.read_text(encoding="utf-8").splitlines():
        if not x.strip():
            continue
        l = json.loads(x)
        if l.get("provider") in D["order"] and l.get("status") == "judged" and not l.get("first_judge_restored") and not l.get("recoded"):
            out.append(l)
    return out


def judge_work(D: Dict[str, Any]) -> Dict[str, Counter]:
    """모델마다 판정 작업량 — 판정 기록의 실제 판정 줄을 모두 더한다(다시 판정 · 복구 포함, 사용자 10-22 '총 걸린 시간으로 합쳐').
    seconds = 그 질문을 판정하는 데 실제로 걸린 시간(벽시계 아님)."""
    per: Dict[str, Counter] = {p: Counter() for p in D["order"]}
    for l in judged_lines(D):
        p = l.get("provider")
        c = l.get("calls") or {}
        dc, cx = c.get("decisions") or {}, c.get("codex") or {}
        w = per[p]
        w["seconds"] += float(l.get("seconds") or 0)
        if not dc.get("reused") or dc.get("fresh_judge"):   # --repair 는 이번에 새로 부른 1차 심판만 (10-22)
            ab = dc.get("attempts_by") if isinstance(dc.get("attempts_by"), dict) else None   # 10-25 투표: 심판마다 센 호출 수
            fj = dc.get("fresh_judge") or dc.get("first_judge") or {}
            if ab is not None:
                w["jev"] += int(ab.get("jev") or 0)
                w["dec"] += int(ab.get("decisions") or 0)
            elif fj:
                w["jev"] += sum(1 for v in fj.values() if v == "jev")
                w["dec"] += sum(1 for v in fj.values() if v == "decisions")
            else:
                w["dec"] += int(dc.get("calls") or 0)
            w["dec_in"] += int(dc.get("input_tokens") or 0)
        w["codex_calls"] += int(cx.get("calls") if cx.get("calls") is not None else (cx.get("rounds") or 0))   # 묶음 호출은 대표 질문에 1 (10-25)
        w["codex_in"] += int(cx.get("input_tokens") or 0)
        w["codex_out"] += int(cx.get("output_tokens") or 0)
        w["errors"] += 1 if l.get("judge_error") else 0
    return per


#: 심판이 돌려준 사용량 칸 이름 → 표의 칸 (OpenAI · Codex 이름 둘 다; 안쪽 칸은 tools/judge_v4.add_usage 가 '바깥.안쪽'으로 펼침)
USAGE_KEYS = {"in": ("input_tokens", "prompt_tokens"),
              "cached": ("cached_input_tokens", "input_tokens_details.cached_tokens", "prompt_tokens_details.cached_tokens"),
              "out": ("output_tokens", "completion_tokens"),
              "reason": ("reasoning_output_tokens", "output_tokens_details.reasoning_tokens", "completion_tokens_details.reasoning_tokens")}
FIRST_SUM = "1차 (Jev · Decisions 합)"
JUDGE_ORDER = ("Jev", "Decisions", FIRST_SUM, "Codex")


def judge_tokens(D: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """심판마다 판정 토큰 (10-22 미팅 7번) — 심판이 호출마다 돌려준 사용량을 그대로 더한다. 칸에 기록이 한 번도 없으면 seen 에 없음('기록 안 됨').
    1차 사용량을 심판별로 남기기 전 실행은 1차(Jev · Decisions 합) 입력만 있다."""
    T: Dict[str, Dict[str, Any]] = {}

    def add(judge: str, usage: Dict[str, Any], calls: int) -> None:
        t = T.setdefault(judge, {"in": 0, "cached": 0, "out": 0, "reason": 0, "calls": 0, "seen": set()})
        t["calls"] += calls
        for col, names in USAGE_KEYS.items():
            hit = [k for k in names if isinstance(usage.get(k), (int, float))]
            if hit:
                t[col] += usage[hit[0]]
                t["seen"].add(col)

    for l in judged_lines(D):
        c = l.get("calls") or {}
        dc, cx = c.get("decisions") or {}, c.get("codex") or {}
        if not dc.get("reused") or dc.get("fresh_judge"):   # --repair 는 이번에 새로 부른 1차 심판만 (10-22)
            fj = list((dc.get("fresh_judge") or dc.get("first_judge") or {}).values())
            if isinstance(dc.get("usage"), dict):
                ab = dc.get("attempts_by") if isinstance(dc.get("attempts_by"), dict) else None
                for name, u in dc["usage"].items():
                    add({"jev": "Jev", "decisions": "Decisions"}.get(name, name), u if isinstance(u, dict) else {},
                        int(ab.get(name) or 0) if ab is not None else fj.count(name))
            else:
                add(FIRST_SUM, {"input_tokens": int(dc.get("input_tokens") or 0)}, len(fj) or int(dc.get("calls") or 0))
        if cx.get("rounds") or cx.get("calls"):
            add("Codex", cx, int(cx.get("calls") if cx.get("calls") is not None else (cx.get("rounds") or 0)))
    return T


def judge_seconds(D: Dict[str, Any]) -> float:
    """판정에 걸린 시간 — 모델마다 판정 시간 합 중 가장 긴 것(네 모델을 동시에 판정, 수집 시간과 같은 셈)."""
    w = judge_work(D)
    return max((v["seconds"] for v in w.values()), default=0)


def fmt_s(sec: float) -> str:
    """초 → '15초' · '4분 05초' (질문 하나의 답변 시간)."""
    sec = round(sec)
    return f"{sec}초" if sec < 60 else f"{sec // 60}분 {sec % 60:02d}초"


def answer_times(D: Dict[str, Any]) -> Tuple[Dict[int, Dict[str, float]], Dict[int, set]]:
    """질문 → 모델 → 답변 시간 합(초 — 질문을 보내고 답을 받기까지, 멀티턴은 10턴 합) ·
    질문 → 다시 수집할 턴이 있는 모델(그 칸의 시간은 실패까지 걸린 시간이라 편차에서 뺀다)."""
    secs: Dict[int, Dict[str, float]] = defaultdict(dict)
    redo: Dict[int, set] = defaultdict(set)
    for (p, q, _t), t in D["turns"].items():
        secs[q][p] = secs[q].get(p, 0.0) + (t.get("latency_ms") or 0) / 1000
        if t.get("status") != "SUCCESS":
            redo[q].add(p)
    return secs, redo


def time_spread(D: Dict[str, Any]) -> str:
    """답변 시간 — 질문 · 유형별 편차 (사용자 10-22 미팅 6번: 모델별 합만으로는 의미가 적다 —
    78개 질문을 펼쳐 유형마다 어느 모델이 빠르고 느린지, 무엇을 개선할지 보이게).
    위: 질문 78개 하나하나(번호 순 · 펼친 채), 가운데: 유형 × 모델 보통 시간 표, 아래: 개선할 곳 (사용자 10-22: 몇 번 질문에서 얼마나 걸렸는지 바로)."""
    order = D["order"]
    secs, redo = answer_times(D)
    ok = lambda q, p: p in secs[q] and p not in redo[q]
    me = "aiona" if "aiona" in order else ""

    def speed_order(qs: List[int]) -> List[str]:
        """AIONA 맨 앞, 나머지는 그 질문들에서 실제로 빠른 순(중앙값) — 걸린 시간 장면만 (사용자 10-22)."""
        md = {p: statistics.median(v) for p in order if (v := [secs[q][p] for q in qs if ok(q, p)])}
        return ([me] if me else []) + sorted((p for p in order if p != me), key=lambda p: (p not in md, md.get(p, 0)))

    common = [q for q in secs if all(ok(q, p) for p in order)]
    tot = {p: sum(secs[q][p] for q in common) for p in order}
    cols_all = ([me] if me else []) + sorted((p for p in order if p != me), key=tot.get)   # 요약 표 열 — 네 모델이 모두 답한 질문의 합이 짧은 순
    panels, finds, redo_note, fasts = [], [], [], []
    for ty in TIME_COLS:
        qs = sorted(q for q, t in D["dtypes"].items() if t == ty)
        vals = {p: [secs[q][p] for q in qs if ok(q, p)] for p in order}
        if not qs or not any(vals.values()):
            continue
        med = {p: statistics.median(vals[p]) for p in order if vals[p]}
        fast = min(med, key=med.get)
        n_redo = sum(1 for q in qs for p in order if p in redo[q])
        if n_redo:
            redo_note.append(f"{TYPE_SHORT[ty]} {n_redo}칸")
        fasts.append(fast)
        cells = ""
        for p in cols_all:
            if p not in med:
                cells += '<div class="lt-sc">—</div>'
                continue
            best = "<!--best-->" if p == fast else ""   # 모든 유형에서 같은 모델이 가장 빠르면 표 머리에, 아니면 이 칸 위에 (아래에서 바꿈)
            if p == me or me not in med:
                # AIONA 칸 — 그 유형에서 가장 빠른 모델 대비 1.5 · 2 · 2.5 · 3배 이상 → 1~4단계
                r = med[p] / med[fast]
                cls, note = f"lt-t{min(4, int((r - 1) / 0.5))}", ("<em>가장 빠름</em>" if p == fast else f"<em>{SHORT[fast]}보다 {r:.1f}배 느림</em>")
            else:
                # 다른 모델 — AIONA 기준 (사용자 10-22: AIONA 채팅 품질 평가라 AIONA 를 부각) · 1 · 1.5 · 2 · 2.5배 이상 → 1~4단계
                v, a = med[p], med[me]
                r = max(v, a) / min(v, a)
                lv = min(4, 1 + int((r - 1) / 0.5))
                cls, note = ((f"lt-s{lv}", f'<em class="lt-vs-s">AIONA보다 {r:.1f}배 느림</em>') if v > a else
                             (f"lt-f{lv}", f'<em class="lt-vs-f">AIONA보다 {r:.1f}배 빠름</em>') if v < a else ("", "<em>AIONA와 같음</em>"))
            cells += (f'<div class="lt-sc {cls}{" lt-mec" if p == me else ""}">{best}<b>{fmt_s(med[p])}</b>{note}'
                      f'<small>질문마다 {fmt_s(min(vals[p]))} ~ {fmt_s(max(vals[p]))}</small></div>')
        what = TYPE_SHORT[ty]
        sub_ = f"{len(qs)}문항" + (" · 10턴 합" if ty == "10턴 멀티턴 대화" else "")
        panels.append(f'<div class="lt-sr"><span class="lt-st"><b>{E(what)}</b><small>{sub_}</small></span>{cells}</div>')
        slow = max(med, key=med.get)
        wide = max(med, key=lambda p: max(vals[p]) / max(1e-9, min(vals[p])))
        line = (f"<b>{E(TYPE_SHORT[ty])}</b> — 가장 빠름 {SHORT[fast]} {fmt_s(med[fast])} · 가장 느림 {SHORT[slow]} {fmt_s(med[slow])}"
                f"({med[slow] / med[fast]:.1f}배)")
        if me in med and me not in (fast, slow):
            line += f" · {SHORT[me]} {fmt_s(med[me])}"
        line += f" · 편차 가장 큼 {SHORT[wide]}({fmt_s(min(vals[wide]))} ~ {fmt_s(max(vals[wide]))})"
        finds.append(f"<li>{line}</li>")
    # AIONA 가 다른 모델보다 눈에 띄게 느린 질문 — 다음 업데이트에서 볼 곳
    slow_q = []
    if me:
        for q in sorted(secs):
            others = [secs[q][p] for p in order if p != me and ok(q, p)]
            if ok(q, me) and others and secs[q][me] >= 1.5 * statistics.median(others):
                slow_q.append((secs[q][me] / statistics.median(others), q, others))
        slow_q.sort(reverse=True)
    if slow_q:
        items = " · ".join(f"Q{q} {TYPE_SHORT.get(D['dtypes'][q], '')} {fmt_s(secs[q][me])}(다른 모델 {fmt_s(min(o))} ~ {fmt_s(max(o))})" for _r, q, o in slow_q[:5])
        finds.append(f"<li><b>{SHORT[me]} 가 다른 세 모델 중앙값보다 1.5배 이상 느린 질문 {len(slow_q)}개</b> — {E(items)}"
                     + (" 외" if len(slow_q) > 5 else "") + "</li>")
    # 질문별 — 유형 탭(고른 유형만), 열은 AIONA 다음 그 유형에서 빠른 순 · 칸 안 막대는 그 질문에서 가장 오래 걸린 모델을 끝(100%)으로,
    # 초록 = 그 질문에서 가장 빠름 · 빨강 = 가장 느림 (사용자 10-22: 78문항을 한 표에 두니 난잡 — 유형끼리 묶어 보고 싶은 것만)
    allq = sorted(secs)
    groups = [("싱글턴", [q for q in allq if D["dtypes"][q] == "단일 질문" and D["qtypes"][q] != "명시적 빈 출력"]),
              ("빈 출력 요구", [q for q in allq if D["qtypes"][q] == "명시적 빈 출력"])] + \
        [(TYPE_SHORT[ty], [q for q in allq if D["dtypes"][q] == ty]) for ty in TIME_COLS if ty != "단일 질문"]
    qtabs = []
    for g, qs in [(g, qs) for g, qs in groups if qs]:
        cols = speed_order(qs)
        rows = ""
        for q in qs:
            okv = {p: secs[q][p] for p in cols if ok(q, p)}
            lo, hi = (min(okv.values()), max(okv.values())) if okv else (0, 0)
            cells = ""
            for p in cols:
                if p not in secs[q]:
                    cell = '<span class="lq-c">—</span>'
                elif p not in okv:
                    cell = '<span class="lq-c lq-redo" title="복사 버튼이 뜨지 않아 다시 수집할 턴이 있음">다시 수집</span>'
                else:
                    v = okv[p]
                    k = (" lq-fast" if v == lo else " lq-slow" if v == hi else "") if len(okv) > 1 else ""
                    cell = (f'<span class="lq-c{k}" style="--w:{v / hi * 100:.0f}%" title="Q{q} · {SHORT[p]} {fmt_s(v)}'
                            + (f" · 가장 빠른 모델의 {v / lo:.1f}배" if lo and v > lo else "") + f'">{fmt_s(v)}</span>')
                cells += f'<span class="lq-col{" lq-me" if p == me else ""}">{cell}</span>'   # 모델마다 세로 상자 한 조각
            lines_ = [x.strip() for x in (D["qset"][q]["turns"][0].get("question") or "").split("\n") if x.strip()] or [""]
            qt = lines_[-1] if re.fullmatch(r"\[[^\]]{1,20}\]", lines_[0]) else lines_[0]   # '[붙여 넣은 자료]' 로 시작하면 요청이 있는 끝 줄
            qt = qt[:120] + ("…" if len(qt) > 120 else "")   # 원문은 마우스를 올리면(title) — 칸에는 분류만 (이사님 10-23, 할일 11번)
            rows += (f'<div class="lq-r"><span class="lq-q"><span class="lq-n">Q{q}</span><span class="lq-t" title="{E(qt)}">{q_class_label(D, q)}</span></span>'
                     f'{cells}</div>')
        qhead = ('<div class="lq-r lq-h"><span class="lq-q">질문</span>'
                 + "".join(f'<span class="lq-col{" lq-me" if p == me else ""}"><span class="{"lt-meh" if p == me else ""}">{mname(p)}</span></span>' for p in cols)
                 + "</div>")
        qtabs.append((g, len(qs), f'<div class="lq-list">{qhead}{rows}</div>'))
    radios = "".join(f'<input type="radio" name="lq" id="lq{i}" class="ar-r"{" checked" if i == 0 else ""}>' for i in range(len(qtabs)))
    labels = ('<div class="hm-tabs lq-tabs">'
              + "".join(f'<label for="lq{i}">{E(g)} <small>{n}</small></label>' for i, (g, n, _x) in enumerate(qtabs)) + "</div>")
    panes = '<div class="lq-ps">' + "".join(f'<div class="ar-pn lq-p{i}">{x}</div>' for i, (_g, _n, x) in enumerate(qtabs)) + "</div>"
    per_q = (f'<div class="lq">{radios}{labels}{panes}</div>'
             + tab_css("lq", len(qtabs), ".lq-tabs", ".lq-ps", "lq-p", "background: var(--accent-soft); color: var(--accent); border-color: transparent;"))
    top_fast = fasts[0] if fasts and all(f == fasts[0] for f in fasts) else ""   # 모든 유형에서 가장 빠른 모델 — 표 머리 이름 위에 '가장 빠름'
    panels = [x.replace("<!--best-->", "" if top_fast else '<i class="lt-bestc">이 유형 가장 빠름</i>') for x in panels]
    head = ('<div class="lt-sr lt-sh"><span>유형</span>'
            + "".join(f'<span class="{"lt-meh" if p == me else ""}">' + ('<i class="lt-bestc">가장 빠름</i>' if p == top_fast else "") + f"{mname(p)}</span>"
                      for p in cols_all) + "</div>")
    return (f'<h3 class="rs-sh">질문별 답변 시간 — {len(allq)}문항</h3>'
            '<p class="rs-basis">유형을 고르면 그 유형의 질문만 보입니다. 질문마다 네 모델이 답하는 데 걸린 시간(질문을 보내고 답을 받기까지, 멀티턴은 10턴 합) · '
            '칸 안 막대는 그 질문에서 가장 오래 걸린 모델을 끝으로 한 길이 · 열은 AIONA 다음 그 유형에서 빠른 모델 순입니다.</p>'
            '<div class="lq-key"><span><i class="lq-sw lq-sw-f"></i>그 질문에서 가장 빠름</span><span><i class="lq-sw lq-sw-s"></i>가장 느림</span>'
            '<span><i class="lq-sw lq-sw-r"></i>다시 수집 — 복사 버튼이 뜨지 않아 다시 받을 턴이 있는 칸(시간에서 뺌)</span></div>'
            + per_q
            + '<h3 class="rs-sh">답변 시간 — 질문 하나에 보통 걸린 시간</h3>'
            '<p class="rs-basis">큰 숫자 = 그 유형의 질문 하나에 보통 걸린 시간(중앙값, 질문을 보내고 답을 받기까지) · '
            + (f"가운데 = AIONA 칸은 그 유형에서 가장 빠른 모델보다 몇 배인지, 다른 모델 칸은 AIONA보다 몇 배 빠르고(초록) 느린지(빨강) · " if me else
               "가운데 = 그 유형에서 가장 빠른 모델보다 몇 배 오래 걸렸는지 · ")
            + '작은 숫자 = 질문마다 가장 짧은 ~ 가장 긴 시간 · 색이 진할수록 차이가 큼'
            + f' · 열은 AIONA 다음, 네 모델이 모두 답한 {len(common)}문항의 답변 시간 합이 짧은 순'
            + (f" · 다시 수집할 칸({', '.join(redo_note)})은 뺐습니다" if redo_note else "") + '.</p>'
            f'<div class="lt-sum">{head}{"".join(panels)}</div>'
            f'<h3 class="rs-sh">개선할 곳 — 답변 시간</h3><ul class="lt-finds">{"".join(finds)}</ul>')


#: 심판 비교 — 이 실행에서 갈린 항목의 최종 결론(정의로 다시 묻기 · 수집 오류 고침 · 답 원문으로 확인, 10-22 조사). 숫자는 판정 기록에서 다시 센다.
#: 값 = (최종 결론과 같은 쪽, 이유) — '수집 오류'는 답을 고친 뒤의 결론이 Jev 1차 판정과 같은 것. 이유 칸(reason)은 Codex 가 다시 봤으면 Codex 의 글이다.
JD_NOTES: Dict[str, Dict[Tuple[str, int, str], Tuple[str, str]]] = {
    "50ca3ccccace": {
        ("aiona", 22, "J2-2.a"): ("Jev", "끝까지 해요체로 존댓말. Decisions 는 반말로 봤고, Codex 도 FAIL 이지만 이유 문장은 '해요체라 존댓말이므로 요청을 지켰습니다' — 최종 통과."),
        ("aiona", 28, "J2-2.a"): ("Jev", "Codex 도 통과 — 끝까지 해요체로 존댓말."),
        ("gpt", 37, "J2-2.a"): ("Jev", "Codex 도 통과 — 끝까지 존댓말."),
        ("claude", 38, "J1-4.a"): ("Jev", "덧붙은 날짜 안내를 Codex 가 처음엔 FAIL, 정의(사과문을 보완하는 안내는 불필요한 내용 아님)를 짚어 다시 묻자 통과 — 최종 통과. Jev 는 확신이 낮아(0.52) 규칙대로 Codex 로 넘김."),
        ("gpt", 38, "J1-4.a"): ("수집 오류", "덧붙은 끝 줄은 ChatGPT 앱의 다음 요청 제안 버튼 글이 수집 때 섞인 것 — 빼고 다시 판정해 통과. Jev 는 확신이 낮아(0.06) Codex 로 넘김."),
        ("gpt", 38, "J1-4.b"): ("Jev", "Codex 도 통과 — 같은 내용을 반복하지 않음."),
        ("gemini", 35, "J2-1.a"): ("Jev", "답이 '사전식 정렬(Lexicographical Order): 사전에서 단어를 찾듯이 첫 글자부터…'로 풀어 썼는데 Codex 가 '영어 용어를 설명 없이 씀'으로 FAIL — 최종 통과."),
    },
}
#: 기준마다 카드 — (기준 id들, 맞은 쪽, 왜, 대응 후보). 갈린 칸 · 수는 판정 기록에서 다시 센다.
JD_CARDS: Dict[str, List[Tuple[Tuple[str, ...], str, str, str]]] = {
    "50ca3ccccace": [
        (("J2-2.a",), "Jev", "Decisions 가 해요체를 반말로 봤습니다. 판정문에는 10-21 부터 '해요체도 존댓말'이 있는데도 그랬고, Codex 1건은 이유에 '요청을 지켰다'고 쓰고 FAIL.",
         "적용(10-22) — 이유가 '요청을 지켰다'인데 FAIL 이면 정의를 짚어 한 번 더 묻기(사유 · 판정 반대 검사)"),
        (("J1-4.a", "J1-4.b"), "Jev (1건은 수집 오류)", "Jev 는 확신이 낮아 규칙대로 Codex 로 넘겼습니다. Codex 의 FAIL 은 정의를 짚어 다시 묻자 통과, GPT Q38 은 앱 버튼 글이 섞인 수집 오류.",
         "이미 적용 — J1-4 FAIL 은 정의를 짚어 한 번 더 묻기(10-22), 수집 때 버튼 글 빼기"),
        (("J2-1.a",), "Jev", "한국어 용어 뒤 괄호에 원어를 적고 바로 풀어 썼는데, Codex 가 '영어 용어를 설명 없이 씀'으로 FAIL.",
         "J2-1.a 정의에 '한국어 풀이 + 괄호 원어는 설명한 것' 경계를 더함"),
    ],
}
#: 예전 실험의 Jev 비교 — (실험, 심판 · 기준, 범위, 비교한 수, 일치, 상대가 FAIL 로 본 것 중 Jev 도 FAIL, 메모).
#: 10-15 는 참고자료/변경내역/2026-10-15, 10-18 은 참고자료/분석/2026-10-18-질문당기준수/eval326 의 Jev · Luna 판정을 J1~J4 로 다시 셈 (10-22).
JD_PRIOR: List[Tuple[str, str, str, str, str, str, str]] = [
    ("10-15 관찰 시험", "Jev ↔ Opus 5.5 · 옛 J1~J4", "단일 10문항 × 4모델", "104", "92%", "—", "불일치 8건 모두 '계약서 없음' 한 질문 → 규칙 고침"),
    ("10-18 326문항", "Jev ↔ Luna · 옛 J1~J4", "단일 질문", "1,505", "98.5%", "1/17", "갈림이 기준에 흩어짐"),
    ("10-18 326문항", "Jev ↔ Luna · 옛 J1~J4", "대화 16개", "785", "92.2%", "5/62", "내용 적절성 갈림 18.9% — 지금 대화는 Decisions 가 맡음"),
]
#: 10-18 단일 질문에서 Jev 가 0.9 이상 확신으로 통과시켰지만 Luna 의 FAIL 이 맞아 보인 것 (답 원문을 읽고 셈, 10-22)
JD_PRIOR_MISS = "10-18 실험의 단일 질문에서 Jev 가 0.9 이상 확신으로 통과시킨 것 중 약 6건은 Luna 의 FAIL 이 맞아 보였습니다 — 대부분 질문에 없는 기록 · 위치 · 원인을 지어낸 답."


def judge_pairs(D: Dict[str, Any]) -> Tuple[Dict[Tuple[str, int, str, str], Tuple[Dict[str, Any], Dict[str, Any]]],
                                            Dict[Tuple[str, int, str, str], Dict[str, Any]], int, Dict[Tuple[str, int], float]]:
    """(모델, 질문, 턴, 기준) → (Decisions 항목, Jev 항목) — 두 심판이 같은 항목을 각각 '직접' 1차 판정한 기록(질문 세트의 단일 질문은 처음에 Decisions,
    뒤에 Jev 로 다시 판정). 다시 쓴(reused) 줄은 뺀다 — 1차 심판 표시가 'decisions' 여도 앞 Jev 판정을 옮겨 적은 것이라 그대로 세면 100% 일치로 잘못 나온다.
    함께 돌려줌: Jev 가 직접 판정한 항목 전체((모델, 질문, 턴, 기준) → 항목) · 다시 쓴 줄 수 · Jev 칸마다 처음 직접 판정 때 Codex 시간(초, Codex 를 안 불렀으면 0)."""
    f = result_file(D["run_id"], "results.jsonl")
    if not f.exists():
        return {}, {}, 0, {}
    rows = sorted((json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()), key=lambda r: r.get("at") or "")
    first: Dict[Tuple[str, int, str, str, str], Dict[str, Any]] = {}
    n_reused = 0
    codex_s: Dict[Tuple[str, int], float] = {}
    for r in rows:
        calls = r.get("calls") or {}
        if (calls.get("decisions") or {}).get("reused"):
            n_reused += 1
            continue
        fj = (calls.get("decisions") or {}).get("first_judge") or {}
        if fj and all(v == "jev" for v in fj.values()):
            cx = calls.get("codex") or {}
            codex_s.setdefault((r["provider"], int(r["question"])), float(cx.get("seconds") or 0) if cx.get("items") else 0.0)
        for tn, t in (r.get("turn_results") or {}).items():
            for cid, it in (t.get("items") or {}).items():
                j = it.get("second_judge")
                if j in ("jev", "decisions") and it.get("jev"):
                    first.setdefault((r["provider"], int(r["question"]), tn, cid, j), it)
    pairs = {k[:4]: (first[k[:4] + ("decisions",)], it) for k, it in first.items() if k[4] == "jev" and k[:4] + ("decisions",) in first}
    return pairs, {k[:4]: it for k, it in first.items() if k[4] == "jev"}, n_reused, codex_s


def scene_judge_compare(D: Dict[str, Any]) -> Tuple[str, str]:
    """심판 비교 — Jev · Decisions 가 갈린 곳 (사용자 10-22 미팅 3번: '약 90%'와 '나머지 2%'가 섞여 수치가 불확실 — 수치의 함정에 빠지지 말고,
    갈림이 특정 기준에 몰려 있는지 확인. 당장 고치지 않고 확인만). 돌려줌: (장면 HTML, 한 줄 메시지) — 비교할 쌍이 없으면 ("", "")."""
    pairs, jev_by, n_reused, codex_s = judge_pairs(D)
    if not pairs:
        return "", ""
    jev_all = list(jev_by.values())
    n = len(pairs)
    cells = {(k[0], k[1]) for k in pairs}
    same = sum(1 for d, j in pairs.values() if d["jev"] == j["jev"])
    d_fail = [k for k, (d, j) in pairs.items() if d["jev"] == "FAIL"]
    both_fail = sum(1 for k in d_fail if pairs[k][1]["jev"] == "FAIL")
    sent = [it for it in jev_all if it.get("codex")]
    jev_fail_all = sum(1 for it in jev_all if it["jev"] == "FAIL")
    pct = lambda a, b: f"{a / b * 100:.1f}%" if b else "—"
    n_dis = n - same
    q_n = len({q for _p, q in cells})
    notes = JD_NOTES.get(D["run_id"], {})
    # 갈린 사례 — FAIL 이 끼어 1차 판정이나 그 회차 결과가 달라진 항목, 최종 결론은 사람 메모(JD_NOTES)
    case_keys = [k for k, (d, j) in sorted(pairs.items())
                 if "FAIL" in (d["jev"], j["jev"], d.get("decision"), j.get("decision")) and not (d["jev"] == j["jev"] and d.get("decision") == j.get("decision"))]
    judged = [k for k in case_keys if (k[0], k[1], k[3]) in notes]
    jev_ok = [k for k in judged if notes[(k[0], k[1], k[3])][0] in ("Jev", "수집 오류")]
    n_coll = sum(1 for k in judged if notes[(k[0], k[1], k[3])][0] == "수집 오류")
    d_fail_ok = [k for k in d_fail if (k[0], k[1], k[3]) in notes and notes[(k[0], k[1], k[3])][0] in ("Jev", "수집 오류")]
    jev_skip = [k for k, (d, j) in pairs.items() if not j.get("codex")]
    audited = [k for k in jev_skip if pairs[k][0].get("codex")]   # Jev 가 넘기지 않았는데 Decisions 경로에서 Codex 가 본 것 — 둘 다 놓친 FAIL 을 볼 수 있는 범위
    # ① 숫자 3장 — 겉보기 → FAIL 만 → 다시 확인한 결론
    nums = [
        ("겉보기", pct(same, n), "같은 항목의 1차 판정 일치", f"{same}/{n} · 통과 · 해당 없음이 대부분이라 높게 나옴", ""),
        ("FAIL 만 보면", f"{both_fail}/{len(d_fail)}", "Decisions 가 FAIL 로 본 것 중 Jev 도 FAIL",
         (f"다시 보니 {len(d_fail_ok)}개는 FAIL 이 아니었음" + (f"(수집 오류 {n_coll} 포함)" if n_coll else "")) if notes else "누가 맞았는지는 아래", " jd-warn"),   # 사용자 10-22: 빨강이면 Jev 가 잘못한 것처럼 보임
    ] + ([("다시 확인한 결론", f"{len(jev_ok)}/{len(case_keys)}", "갈린 항목의 최종 결론이 Jev 1차 판정과 같음",
           f"사람 확정 아님 · Jev 가 넘기지 않은 {len(jev_skip)}항목 중 Codex 가 본 것 {len(audited)}개({pct(len(audited), len(jev_skip))})", " jd-good")] if len(judged) == len(case_keys) else [])
    num_html = "".join(f'<div class="jd-n{c}"><i>{E(st)}</i><b>{E(v)}</b><span>{E(t)}</span><small>{E(sub)}</small></div>' for st, v, t, sub, c in nums)
    # 예전 실험과 비교 — '약 90%' 는 어디서
    prior_rows = [[E(a), E(b), E(c), d, e, f, E(g)] for a, b, c, d, e, f, g in JD_PRIOR]
    prior_rows.append(['<b>이번 실행</b>', "Jev ↔ Decisions · 44개 항목", f"단일 {q_n}문항 × {len(D['order'])}모델", f"{n:,}", pct(same, n),
                       f"{both_fail}/{len(d_fail)}", "말투에 몰림 — 틀린 쪽은 Decisions" if notes else ""])
    prior_tab = table("", ["실험", "심판 · 기준", "범위", "비교한 수", "일치", "상대 FAIL 중 Jev 도 FAIL", "메모"], prior_rows, [13, 19, 14, 9, 7, 13, 25], "jd-t")
    # ② 갈림 칸 — 기준마다 네모 하나 = 갈린 항목 하나 (빨강 = FAIL ↔ 통과, 회색 = 해당 없음 ↔ 적용)
    by: Dict[str, Counter] = defaultdict(Counter)
    for (_p, _q, _t, c), (d, j) in pairs.items():
        by[c]["n"] += 1
        if d["jev"] != j["jev"]:
            by[c]["fail" if "FAIL" in (d["jev"], j["jev"]) else "na"] += 1
    dis = sorted((c for c in by if by[c]["fail"] + by[c]["na"]), key=lambda c: (-by[c]["fail"], -by[c]["na"], c))
    sq = lambda c: '<i class="jd-f"></i>' * by[c]["fail"] + '<i class="jd-a"></i>' * by[c]["na"]
    bars = "".join(f'<div class="jd-br"><span class="jd-bl">{E(crit_name(D["names"], c))} <span class="rs-id">{E(c)}</span></span>'
                   f'<span class="jd-bt">{sq(c)}</span>'
                   f'<span class="jd-bv"><b>{by[c]["fail"] + by[c]["na"]}</b><small> / {by[c]["n"]}항목 · {pct(by[c]["fail"] + by[c]["na"], by[c]["n"])}</small></span></div>' for c in dis)
    bars += f'<div class="jd-br jd-rest"><span class="jd-bl">나머지 {len(by) - len(dis)}개 기준</span><span class="jd-bt"></span><span class="jd-bv">갈림 0</span></div>'
    # ③ 기준 카드 — 갈린 칸은 판정 기록에서, 맞은 쪽 · 왜 · 대응은 이 실행의 사람 메모
    cards = ""
    for ids, who, why, act in JD_CARDS.get(D["run_id"], []):
        ks = [k for k in case_keys if k[3] in ids]
        chips = "".join(f'<span class="jd-chip">{SHORT[k[0]]} · Q{k[1]}</span>' for k in ks)
        cards += (f'<div class="jd-c"><div class="jd-ch">{E(crit_name(D["names"], ids[0]))} <span class="rs-id">{E(" · ".join(ids))}</span></div>'
                  f'<div class="jd-cn"><b>{len(ks)}칸</b> 갈림 {chips}</div>'
                  f'<dl class="jd-dl"><dt>맞은 쪽</dt><dd class="jd-who">{E(who)}</dd><dt>왜</dt><dd>{E(why)}</dd>'
                  f'<dt>대응 후보</dt><dd><b>{E(act)}</b></dd></dl></div>')
    # ④ 믿을 만한가 — 두 상자 (비교 표본과 Jev 만 본 단일 질문의 최종 FAIL 칸 — 표본이 쉬운 쪽인지)
    only_q = sorted({k[1] for k in jev_by} - {q for _p, q in cells})
    fin_p = [L["verdict"] for (p_, q_), L in D["lines"].items() if q_ in {q for _p, q in cells}]
    fin_o = [L["verdict"] for (p_, q_), L in D["lines"].items() if q_ in set(only_q)]
    fp = lambda it: (it.get("decisions_prob") or {}).get("FAIL", 0)
    sure_miss = sum(1 for it in sent if it.get("codex") == "FAIL" and (it.get("decisions_confidence") or 0) >= 0.9)
    good_b = (f"갈린 곳에서 Jev 가 놓친 FAIL 은 없었습니다", f"갈린 {len(case_keys)}건 모두 최종 결론이 Jev 판정과 같고, 갈림이 많은 말투는 Decisions 쪽 오판입니다. "
              "어느 기준도 Jev 에서 뺄 근거는 이번에 없습니다.") if notes and len(jev_ok) == len(case_keys) else \
             ("최종 결과는 거의 같습니다", "갈린 FAIL 은 Codex 가 다시 봅니다.")
    verdict = ('<div class="jd-v">'
               f'<div class="jd-vok"><i>믿을 만한 점</i><b>{E(good_b[0])}</b><p>{E(good_b[1])}</p></div>'
               f'<div class="jd-vwarn"><i>주의할 점</i><b>Jev 는 혼자 FAIL 을 내지 않고, 확신이 높으면 다시 보지 않습니다</b>'
               f'<p>직접 판정 {len(jev_all):,}항목 중 FAIL {jev_fail_all}개 — FAIL 은 모두 Codex 로 넘어간 {pct(len(sent), len(jev_all))}에서 나왔고, '
               + (f"그중 {sure_miss}개(사실 확인 — 늘 Codex)는 Jev 가 0.9 이상 확신한 통과였습니다. " if sure_miss else "")
               + f'{E(JD_PRIOR_MISS)}</p>'
               + (f'<p>비교한 {q_n}문항은 최종 FAIL 이 {fin_p.count("FAIL")}/{len(fin_p)}칸으로 Jev 만 본 나머지 {len(only_q)}문항({fin_o.count("FAIL")}/{len(fin_o)}칸)보다 적어, '
                  "진짜 FAIL 답에서 두 심판을 견준 경우가 거의 없고 둘 다 놓친 FAIL 은 이 자료로 보이지 않습니다.</p>" if fin_o else "")
               + '</div></div>'
               f'<p class="rs-basis">참고 — 이번 비교는 단일 질문 {q_n}개이고, 최종 결론은 Codex · 정의로 다시 묻기 · 답 원문으로 본 것입니다(사람이 정한 정답은 아직 없음).</p>')
    # ⑤ 대응 후보 — 더 드는 Codex 시간은 이번 실행 기준(Codex 는 구독이라 돈은 들지 않음)
    cx_on = [s_ for s_ in codex_s.values() if s_]
    cx_med = statistics.median(cx_on) if cx_on else 0
    no_cx = [c for c, s_ in codex_s.items() if not s_]
    near = {k: it for k, it in jev_by.items() if not it.get("codex") and 0.15 <= fp(it) < 0.3}   # 넘기는 문턱 0.3 = tools/judge_v4.SCREEN_FAIL_PROB
    near_new = {(k[0], k[1]) for k in near} & set(no_cx)
    audit = max(1, round(len(no_cx) * 0.1))
    # 확신도 = (선택지 수 × 1위 확률 − 1) / (선택지 수 − 1) (심판 API 값, 10-22 판정 기록으로 확인) — 확신 0.7 미만은 2지선다면 1위 확률 0.85 미만,
    # 해당 없음까지 3지선다면 0.80 미만이라, 같은 통과 확률이라도 항목에 따라 Codex 로 가거나 안 감
    gap = {k: it for k, it in jev_by.items() if not it.get("codex") and len(it.get("decisions_prob") or {}) == 3 and it["jev"] != "NOT_APPLICABLE"
           and max((it.get("decisions_prob") or {}).values()) < 0.85}
    gap_new = {(k[0], k[1]) for k in gap} & set(no_cx)
    act_rows = [
        ["확신 문턱을 선택지 수와 상관없이 같게 — 1위 확률 0.85 미만이면 Codex 로", "같은 통과 0.81 이 2지선다 항목은 넘어가고 3지선다 항목은 안 넘어감(Gemini Q35)",
         f"+{len(gap)}항목 · Codex 새로 {len(gap_new)}칸(약 {fmt_s(len(gap_new) * cx_med)})"],
        ["Codex 로 넘기는 FAIL 확률 문턱 0.3 → 0.15", "문턱 바로 아래 통과(Gemini Q35 는 0.17)", f"+{len(near)}항목 · Codex 새로 {len(near_new)}칸(약 {fmt_s(len(near_new) * cx_med)})"],
        [f"Codex 를 안 부른 Jev 칸 {len(no_cx)}개 중 10%를 골라 Codex 로도", "확신 높은 통과의 놓침 비율을 실행마다 잼", f"+약 {audit}칸(약 {fmt_s(audit * cx_med)})"],
        ["판정문 · 검사 보완 — 위 카드의 말투 · 용어 두 가지", "Decisions · Codex 의 말투 · 용어 오판", "시간 그대로"],
        [f"사람 정답 — 갈린 {len(case_keys)}건 + 문턱 근처 {len(near)}항목", "Codex 를 정답처럼 쓰는 것", f"사람 검토 {len(case_keys) + len(near)}항목"],
        ["(참고) Jev 칸 전부 Codex 로도 — 'J1-4 늘 Codex' 와 같은 효과", "이번엔 근거 없음", f"+{len(no_cx)}칸(Codex 시간 합 약 {fmt_s(len(no_cx) * cx_med)})"],
    ]
    act_tab = table("", ["후보", "무엇을 막나", "더 드는 것 (이번 실행 기준)"], act_rows, [44, 28, 28], "jd-t")
    # 자세히 (접힘) — 기준 표 · 갈린 사례 표 · 센 방법
    lab = {"PASS": "통과", "FAIL": "FAIL", "NOT_APPLICABLE": "해당 없음", "UNVERIFIED": "보류"}

    def side(it: Dict[str, Any]) -> str:
        conf = (it.get("decisions_prob") or {}).get(it["jev"])
        return (f'{lab.get(it["jev"], it["jev"])}' + (f' <small>{conf:.2f}</small>' if conf is not None else "")
                + f' → <b>{lab.get(it.get("decision"), it.get("decision") or "—")}</b>' + (' <small>(Codex)</small>' if it.get("codex") else ""))
    crit_rows = [[f'{E(crit_name(D["names"], c))} <span class="rs-id">{E(c)}</span>', f'{by[c]["n"]}',
                  (f'<b class="jd-hot">{by[c]["fail"]}</b>' if by[c]["fail"] else "0"), f'{by[c]["na"]}',
                  pct(by[c]["fail"] + by[c]["na"], by[c]["n"]) if by[c]["fail"] + by[c]["na"] else "0%"]
                 for c in sorted(by, key=lambda c: (-(by[c]["fail"] + by[c]["na"]), -by[c]["fail"], c))]
    crit_tab = table("", ["기준", "비교한 항목", "FAIL ↔ 통과", "해당 없음 ↔ 적용", "갈림 비율"], crit_rows, [44, 14, 14, 14, 14], "jd-t")
    case_rows = [[f'{mname(k[0])} · Q{k[1]}', f'{E(crit_name(D["names"], k[3]))} <span class="rs-id">{E(k[3])}</span>', side(pairs[k][0]), side(pairs[k][1]),
                  (lambda w: f'<b>{E(w[0])}{" 쪽" if w[0] in ("Jev", "Decisions") else ""}</b> — {E(w[1])}' if w[0] else '<span class="muted">사람 판단 없음</span>')(notes.get((k[0], k[1], k[3]), ("", "")))]
                 for k in case_keys]
    case_tab = table("", ["칸", "기준", "Decisions 1차 → 그 회차 결과", "Jev 1차 → 그 회차 결과", "최종 결론과 같은 쪽"], case_rows, [11, 17, 17, 17, 38], "jd-t") if case_rows else ""
    more = (f'<details class="rs-d jd-more"><summary>자세히 — 기준 {len(by)}개 표 · 갈린 사례 {len(case_rows)}칸 · 센 방법</summary>'
            f'<h4>기준별 갈림</h4>{crit_tab}' + (f'<h4>갈린 사례 — FAIL 이 낀 것</h4>{case_tab}' if case_tab else "")
            + f'<h4>센 방법</h4><p class="rs-basis">단일 질문 {q_n}개 × {len(D["order"])}모델 = {len(cells)}칸은 처음에 Decisions 가 1차 판정했다가 Jev 로 다시 판정해, '
            f'같은 답 · 같은 기준을 두 심판이 따로 본 기록이 {n}항목 남아 있습니다. 판정 기록의 다시 쓴 줄 {n_reused}개는 1차 심판이 \'decisions\' 로 표시돼도 '
            "앞 Jev 판정을 옮겨 적은 것이라 뺐습니다(그대로 세면 100% 일치로 잘못 나옴). 숫자 옆 작은 수는 심판의 확신도, '그 회차 결과'는 그 판정 때 Codex 까지 본 결과입니다 — "
            "뒤에 정의로 다시 묻기 · 수집 오류 고침으로 바뀐 것은 마지막 칸에 적었습니다. Codex 로 넘어간 항목의 이유 칸은 Codex 가 쓴 글이라, 1차 심판의 이유는 남지 않습니다.</p></details>")
    html_ = (f'<h3 class="rs-sh">일치율 — 무엇을 세느냐에 따라 다릅니다</h3><div class="jd-nums">{num_html}</div>'
             f'<h3 class="rs-sh">"약 90%"는 어디서 — 예전 실험과 비교</h3>{prior_tab}'
             f'<p class="rs-basis">"약 90%"는 10-15(92%) · 10-18 턴 단위(93.7%) 같은 예전 실험 숫자로 보이고, "2%"는 이번 실행의 항목 갈림({n_dis}개, {pct(n_dis, n)})입니다 — '
             '심판 · 기준판 · 세는 단위가 다른 숫자가 섞였습니다. 어느 실험이든 겉보기 일치는 높고, 상대가 FAIL 로 본 것을 Jev 가 같이 잡은 경우는 드뭅니다.</p>'
             f'<h3 class="rs-sh">갈림이 몰린 기준</h3>'
             '<div class="jd-key"><span><i class="jd-f"></i>FAIL ↔ 통과 — 결과에 영향</span><span><i class="jd-a"></i>해당 없음 ↔ 적용 — 영향 없음</span>'
             '<span>네모 하나 = 갈린 항목 하나</span></div>'
             f'<div class="jd-bars" style="--sq:{max((by[c]["fail"] + by[c]["na"] for c in dis), default=1)}">{bars}</div>'
             + (f'<h3 class="rs-sh">기준마다 — 누가 맞았나 <small class="muted">최종 결론으로 다시 확인</small></h3><div class="jd-cards">{cards}</div>' if cards else "")
             + f'<h3 class="rs-sh">믿을 만한가</h3>{verdict}'
             + f'<h3 class="rs-sh">대응 후보 <small class="muted">지금은 바꾸지 않음</small></h3>{act_tab}{more}')
    key = (f"1차 판정은 {pct(same, n)} 같지만 FAIL 은 같이 잡지 못했습니다({both_fail}/{len(d_fail)}) — 다시 확인하니 갈린 {len(case_keys)}건 모두 "
           "최종 결론이 Jev 판정과 같았습니다" + (f"(수집 오류 {n_coll}건 포함 · 사람이 정한 정답은 아직 없음)." if n_coll else "(사람이 정한 정답은 아직 없음).") if notes and len(jev_ok) == len(case_keys) else
           f"1차 판정은 {pct(same, n)} 같지만 FAIL 은 같이 잡지 못했습니다({both_fail}/{len(d_fail)}).")
    return html_, key


#: 심판 선택 — 갈린 36칸 검토(비교 폴더의 검토_36칸.md, 10-22). 판단한 쪽은 Claude 세션이 판정 프롬프트 v4 의 정의로 본 것 — 사람 정답이 아님.
JC_REVIEW: Dict[str, Dict[str, Any]] = {
    "50ca3ccccace": {"codex": 14, "claude": 12, "unclear": 10, "lc_missed": 6, "j14_strict": 11},   # 검토_36칸.md — j14_strict: Codex 가 맞은 14 중 J1-4 예외를 무시한 Claude FAIL
}


def compare_dirs(run_id: str) -> List[Path]:
    """실행의 비교 폴더들 — run.json 의 compare_of 가 그 실행인 것 (최근 것부터)."""
    out = []
    if RESULTS.exists():
        for d in sorted(RESULTS.iterdir(), reverse=True):
            try:
                if d.is_dir() and json.loads((d / "run.json").read_text(encoding="utf-8")).get("compare_of") == run_id:
                    out.append(d)
            except (OSError, ValueError):
                continue
    return out


def scene_judge_choice(D: Dict[str, Any]) -> Tuple[str, str]:
    """심판 선택 — 2차 심판을 Codex 로 고른 근거 (사용자 10-22 미팅 4번: '비용 · 시간 때문'으로 들림 — 시간이 1순위면 안 됨,
    여러 심판 비교 → 결과가 비슷 → 그중 싸고 빠른 것 순서로). 10-26 사용자: 처음 보는 사람도 바로 알게 — 낱말('칸' = 답 하나) ·
    비교 방법 · 두 심판 한눈 표(호출 · 시간은 답한 모델별로 펼쳐 합계) · 그래서 · 왜 오래 걸렸나 · 흔들렸나. Claude CLI 비교 폴더가 없으면 ("", "")."""
    dirs = compare_dirs(D["run_id"])
    cmp_dir = next((d for d in dirs if (d / "비교.json").exists() and (d / "results.jsonl").exists()), None)
    base_f = result_file(D["run_id"], "results.jsonl")
    if cmp_dir is None or not base_f.exists():
        return "", ""
    cj = json.loads((cmp_dir / "비교.json").read_text(encoding="utf-8"))

    def last_rows(f: Path) -> Dict[Tuple[str, int], Dict[str, Any]]:
        out: Dict[Tuple[str, int], Dict[str, Any]] = {}
        for r in sorted((json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()), key=lambda r: r.get("at") or ""):
            out[(r["provider"], int(r["question"]))] = r
        return out

    def fails(r: Dict[str, Any]) -> set:
        return {(tn, c) for tn, t in (r.get("turn_results") or {}).items() for c, it in (t.get("items") or {}).items() if it.get("decision") == "FAIL"}

    A, B = last_rows(base_f), last_rows(cmp_dir / "results.jsonl")
    common = sorted(set(A) & set(B))
    n_ans = len(common)
    diff = [k for k in common if A[k].get("verdict") != B[k].get("verdict")]
    flip = [k for k in diff if "PASS" in (A[k].get("verdict"), B[k].get("verdict"))]
    to_fail = sum(1 for k in flip if A[k].get("verdict") == "PASS" and B[k].get("verdict") == "FAIL")
    rev = len(flip) - to_fail
    only_j14 = [k for k in flip if (fails(A[k]) ^ fails(B[k])) and all(c.startswith("J1-4") for _t, c in fails(A[k]) ^ fails(B[k]))]
    flip_q = len({q for _p, q in flip})
    same = n_ans - len(flip)
    pa, pb = (sum(1 for k in common if X[k].get("verdict") == "PASS") for X in (A, B))
    rv = JC_REVIEW.get(D["run_id"])
    order = [p for p in D["order"] if p in (cj.get("codex_first_calls") or {})]
    # 호출 = 첫 호출 + 다시 묻기, 두 심판 같은 단위 (10-26: 전에는 Codex 를 답마다 1로 세어 206 으로 적었음). 답한 모델별로 펼쳐 합계
    cx, cl = cj.get("codex_first_calls") or {}, cj.get("claude_tokens") or {}
    cx_sec = {p: float((cx.get(p) or {}).get("seconds") or 0) for p in order}
    cl_sec = {p: float((cj.get("claude_seconds_per_model") or {}).get(p) or 0) for p in order}
    cx_n = {p: int((cx.get(p) or {}).get("calls") or 0) for p in order}
    cl_n = {p: int((cl.get(p) or {}).get("calls") or 0) for p in order}
    cx_calls, cl_calls = sum(cx_n.values()), sum(cl_n.values())
    cx_max, cl_max = max(cx_sec.values(), default=0), max(cl_sec.values(), default=0)
    t_ratio = cl_max / cx_max if cx_max else 0
    # 같은 답끼리 — Codex 는 비교 도구와 같은 줄(답마다 저장 출력을 다시 쓰지 않은 처음 판정 줄), Claude 는 마지막 줄
    first: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for x in base_f.read_text(encoding="utf-8").splitlines():
        if x.strip():
            r = json.loads(x)
            c, k = (r.get("calls") or {}).get("codex") or {}, (r["provider"], int(r["question"]))
            if c.get("status") == "OK" and not c.get("reused") and k not in first:
                first[k] = c
    cb = {k: (B[k].get("calls") or {}).get("codex") or {} for k in common}
    both = [k for k in common if k in first and cb[k].get("status") == "OK" and "rounds" in cb[k]]

    def rounds(c: Dict[str, Any]) -> int:
        return int(c.get("calls") if c.get("calls") is not None else (c.get("rounds") or 1))

    def multi(q: int) -> bool:
        return str(D["qtypes"].get(q, "")).startswith("10턴")

    def part(ks: List[Tuple[str, int]]) -> Dict[str, float]:
        ra, rb = sum(rounds(first[k]) for k in ks), sum(rounds(cb[k]) for k in ks)
        ia, ib = sum(first[k].get("input_tokens") or 0 for k in ks), sum(cb[k].get("input_tokens") or 0 for k in ks)
        sa, sb = sum(first[k].get("seconds") or 0 for k in ks), sum(cb[k].get("seconds") or 0 for k in ks)
        ok = ra and rb and ia and sa
        return {"n": len(ks), "ra": ra, "rb": rb, "inp": (ib / rb) / (ia / ra) if ok else 0, "sec": (sb / rb) / (sa / ra) if ok else 0}

    mt, st1 = part([k for k in both if multi(k[1])]), part([k for k in both if not multi(k[1])])
    worst = max((k for k in both if multi(k[1])), key=lambda k: rounds(cb[k]) - rounds(first[k]), default=None)
    # '반복'(J1-4.b) 같은 칸을 두 심판에게 세 번씩 — 다수결 결과 폴더(run.json kind item-vote)
    vote = None
    for d in (sorted(RESULTS.iterdir(), reverse=True) if RESULTS.exists() else []):
        try:
            meta = json.loads((d / "run.json").read_text(encoding="utf-8"))
            if meta.get("kind") == "item-vote" and meta.get("compare_of") == D["run_id"] and (d / "다수결.json").exists():
                vote = {**(json.loads((d / "다수결.json").read_text(encoding="utf-8")).get("total") or {}), "item": meta.get("item")}
                break
        except (OSError, ValueError):
            continue
    j14 = None   # 판정 문구만 고쳐도 — J1-4.b 다시 판정 폴더의 합계 줄(옛 → 새 프롬프트)
    for d in dirs:
        md = d / "비교_옛프롬프트→새프롬프트.md"
        if md.exists():
            m = re.search(r"\|\s*합계\s*\|\s*(\d+)\s*\|\s*(\d+)\s*→\s*(\d+)\s*\|\s*(\d+)\s*→\s*(\d+)\s*\|", md.read_text(encoding="utf-8"))
            if m:
                j14 = tuple(int(x) for x in m.groups())
            break
    name_ = str(cj.get("model") or "").replace("claude-", "").replace("-", " ", 1).replace("-", ".").title()   # claude-sonnet-5-5 → Sonnet 5.5
    model = f"Claude CLI({name_})"
    nq, nm = cj.get("questions"), len(cj.get("providers") or [])

    terms = [("답 하나 (이 장면의 '칸')", f"한 모델이 한 질문에 낸 답. 질문 {nq}개 × 답한 모델 {nm}개 = {n_ans}개. 10턴 대화 질문은 10턴 전체가 답 하나이고, "
                                      "답마다 결론이 하나씩 나옵니다 — 완전 통과 · FAIL · 보류.", ""),
             ("2차 심판", f"1차 심판(Jev · Decisions)이 'FAIL 같다 · 애매하다'고 표시한 기준만 다시 읽고 확정하는 AI. 지금은 Codex(ChatGPT 구독), "
                         f"비교 상대는 {model}(Claude 구독).", ""),
             ("호출", "2차 심판 프로그램을 한 번 실행해 판정을 받아 오는 것. 처음 한 번 묻고, 빠진 판정이나 답에 없는 근거 문장이 있으면 다시 묻습니다 — "
                      "10턴 대화는 턴마다 다시 물어 답 하나에 여러 번입니다.", ""),
             ("완전 통과", f"그 답에 적용된 기준을 하나도 어기지 않은 답 — 모델 점수 '통과 질문 수(/{nq})'가 이것을 셉니다.", "t-pass")]
    term_html = ('<dl class="rs-terms">'
                 + "".join(f'<div{f" class={chr(34)}{c}{chr(34)}" if c else ""}><dt>{E(t)}</dt><dd>{E(d)}</dd></div>' for t, d, c in terms) + "</dl>")
    steps = [("같은 답을 두 심판에게", f"본 판정(답 {n_ans}개)의 1차 심판 결과 · 2차로 넘긴 기준 · 판정 프롬프트는 그대로 두고, 2차 심판만 Codex → {model} 로 바꿔 "
                                  "다시 판정했습니다. 같은 문제를 두 심판에게 따로 풀게 한 것입니다."),
             ("결론 맞대기", "답마다 두 심판의 결론 — 통과인가 아닌가 — 를 맞대 봤습니다.")]
    if rv:
        steps.append(("갈린 답은 다시 읽기", "결론이 갈린 답은 판정 기준 정의를 잣대로 다시 읽어 누가 맞는지 따졌습니다(Claude 세션이 판단 — 사람이 정한 정답은 아님)."))
    step_html = '<ol class="jc-steps">' + "".join(f"<li><b>{E(t)}</b><span>{E(b)}</span></li>" for t, b in steps) + "</ol>"
    nums = [("통과 여부가 같았던 답", f"{same}/{n_ans}", f"{same / n_ans * 100:.1f}% — {len(flip)}개(질문 {flip_q}개)만 갈림" if n_ans else "", ""),
            ("갈린 답 중 '불필요 · 반복' 하나 때문", f"{len(only_j14)}/{len(flip)}", f"Codex 통과 · Claude FAIL {to_fail} · 반대 {rev}", " jd-warn")]
    if rv:
        nums.append(("다시 보니 맞은 쪽", f"{rv['codex']} : {rv['claude']}", f"Codex : Claude · 둘 다 애매 {rv['unclear']}", ""))
    num_html = "".join(f'<div class="jd-n{c}"><i>{E(t)}</i><b>{E(v)}</b><small>{E(s)}</small></div>' for t, v, s, c in nums)

    def by(vals: Dict[str, Any], unit: str, sep: str) -> str:
        return sep.join(f"{SHORT[p]} 답 {vals[p]}{unit}" for p in order)

    rows = [["완전 통과로 본 답", f"{pa} / {n_ans}", f"{pb} / {n_ans}" + (" — 더 엄격" if pb < pa else " — 더 너그러움" if pb > pa else "")]]
    if rv:
        rows += [["잘한 것", "'불필요 · 반복' 기준의 예외(표를 풀어 쓴 설명 · 결론 한 번)를 지켜 괜한 FAIL 을 안 냄",
                  f"긴 문서 질문에서 근거를 잘못 찾은 답 {rv['lc_missed']}개를 잡음"],
                 ["놓친 것", f"긴 문서 근거 오류 {rv['lc_missed']}개를 통과시킴", f"'불필요 · 반복' 예외를 무시한 FAIL {rv['j14_strict']}개"]]
    rows += [["판정 시간<br><small>답한 모델별 · 네 모델 동시</small>",
              E(by({p: round(v / 60) for p, v in cx_sec.items()}, "분", " · ")) + f" → 다 끝나기까지 <b>약 {hm(cx_max)}</b>",
              E(by({p: round(v / 60) for p, v in cl_sec.items()}, "분", " · ")) + f" → <b>약 {hm(cl_max)}</b> ({t_ratio:.1f}배)"],
             ["호출 수<br><small>답한 모델별 합 · 첫 호출 + 다시 묻기</small>", E(by(cx_n, "", " + ")) + f" = <b>{cx_calls}번</b>",
              E(by(cl_n, "", " + ")) + f" = <b>{cl_calls}번</b>" + (f" ({cl_calls / cx_calls:.1f}배)" if cx_calls else "")],
             ["비용", "구독(정액)", "구독(정액)"]]
    tab_cmp = table("", ["", "Codex (지금)", E(model)], rows, [22, 39, 39], "jd-t")
    dont = ('<div class="jd-v">'
            '<div class="jd-vok"><i>이렇게 말합니다</i><b>정확도는 비슷하고 약점만 다르다 → 같은 일을 더 빨리 끝내는 Codex</b>'
            f"<p>같은 답 {n_ans}개를 두 심판에게 판정시켜 보니 {same}개는 결론이 같았고, 갈린 답 대부분은 '불필요 · 반복' 한 기준의 해석 차이였으며, "
            + (f"다시 보니 어느 쪽도 더 정확하지 않았습니다({rv['codex']} : {rv['claude']}). " if rv else "")
            + f"그래서 정확도가 같을 때의 기준인 시간으로 골랐습니다(약 {hm(cx_max)} ↔ {hm(cl_max)}, 둘 다 구독이라 비용은 같음).</p></div>"
            '<div class="jd-vwarn"><i>말하지 않습니다</i><b>"78개 중 3개만 달랐다" · "시간 때문에"</b>'
            f"<p>같은 조건으로 다시 비교하니 질문 {flip_q}개에서 통과 여부가 갈렸습니다. 시간은 첫 이유가 아니라 정확도가 같을 때의 이유입니다.</p></div></div>")
    nxt = ("다음 할 일은 심판 교체가 아니라 기준 다듬기 — "
           + (f"'반복'(J1-4.b) 판정 문구만 고쳐도 같은 {j14[0]}건에서 FAIL 이 Codex {j14[1]} → {j14[2]} · Claude {j14[3]} → {j14[4]} 로 바뀌었습니다. " if j14 else "")
           + ("긴 문서 질문의 근거 확인은 따로 보강합니다." if rv else ""))
    slow = []
    if mt["n"]:
        ex = (f"(예: Q{worst[1]} {SHORT[worst[0]]} 답 — Codex {rounds(first[worst])}번 · Claude {rounds(cb[worst])}번, 그 뒤 '두 번 물어도 그대로면 다음 턴'으로 고침)"
              if worst else "")
        slow.append(("10턴 대화에서 다시 묻기가 많음", f"대화 질문 답 {mt['n']}개에서 호출 {mt['ra']} → {mt['rb']}번. 문제가 있는 턴을 한 턴씩 다시 묻는데 "
                                                    f"Claude 첫 답에서 다시 물을 턴이 더 많았고, 같은 턴을 끝없이 되묻는 일도 있었습니다{ex}."))
    if mt["inp"] and st1["inp"]:
        lo, hi = sorted((mt["sec"], st1["sec"]))
        slow.append(("한 번 부를 때도 무거움", f"같은 질문인데 읽은 글(입력 토큰)이 대화 {mt['inp']:.1f}배 · 한 턴 질문 {st1['inp']:.1f}배, 호출 한 번 시간도 {lo:.1f}~{hi:.1f}배."))
    if st1["n"]:
        slow.append(("한 턴짜리 질문은 호출이 비슷", f"단일 · 첨부 · 긴 문서 답 {st1['n']}개는 호출 {st1['ra']} ↔ {st1['rb']}번 — 시간 차이는 대부분 대화 질문에서 납니다."))
    slow_html = '<div class="rs-cards">' + "".join(f'<div class="rs-card"><b>{E(t)}</b><p>{E(b)}</p></div>' for t, b in slow) + "</div>"
    wob = ""
    if vote and vote.get("칸"):
        nv = int(vote["칸"])
        wn = [("세 번 모두 같은 답 — Codex", f"{vote.get('codex_만장일치', 0)}/{nv}", f"{vote.get('codex_만장일치', 0) / nv * 100:.0f}%"),
              ("세 번 모두 같은 답 — Claude CLI", f"{vote.get('claude_만장일치', 0)}/{nv}", f"{vote.get('claude_만장일치', 0) / nv * 100:.0f}%")]
        wob = ('<h3 class="rs-sh">같은 심판이 흔들린 걸까?</h3><div class="jd-nums">'
               + "".join(f'<div class="jd-n"><i>{E(t)}</i><b>{E(v)}</b><small>{E(s)}</small></div>' for t, v, s in wn) + "</div>"
               + f"<p class=\"rs-basis\">'반복'({E(str(vote.get('item') or 'J1-4.b'))}) 기준으로 2차 심판까지 간 {nv}건(답의 턴마다 하나)을 두 심판에게 각각 세 번씩 물었습니다. "
               "둘 다 대체로 자기 답을 지키므로, 갈림은 우연한 흔들림이 아니라 두 심판이 '반복'을 다르게 읽은 것입니다.</p>")
    tally = cj.get("tally") or {}
    trows = []
    for p_ in D["order"]:
        c_, l_ = (tally.get("codex") or {}).get(p_, {}), (tally.get("claude") or {}).get(p_, {})
        trows.append([mname(p_), f'{(cj.get("diff_per_model") or {}).get(p_, 0)} / {cj.get("questions")}',
                      f'{c_.get("PASS", 0)} · {c_.get("FAIL", 0)} · {c_.get("HOLD", 0)}', f'{l_.get("PASS", 0)} · {l_.get("FAIL", 0)} · {l_.get("HOLD", 0)}'])
    tab = table("", ["답한 모델", "결론이 다른 답", "Codex (통과 · FAIL · 보류)", "Claude CLI (통과 · FAIL · 보류)"], trows, [28, 20, 26, 26], "jd-t")
    more = (f'<details class="rs-d jd-more"><summary>자세히 — 답한 모델별 결론 · 자료</summary>{tab}'
            f'<p class="rs-basis">자료: 결과 폴더 {E(cmp_dir.name)}(비교.md · 비교.json · 검토_36칸.md)'
            + (" · 다수결 결과 폴더(다수결.json)" if vote else "")
            + " — 갈린 답 검토는 Claude 세션이 판정 프롬프트의 정의로 본 판단이고 사람 정답이 아닙니다. "
            f'기준 단위 최종 판정 일치 {cj.get("items_same", 0):,}/{cj.get("items_total", 0):,}, 2차 심판에 넘긴 기준만 {cj.get("flagged_same", 0):,}/{cj.get("flagged_total", 0):,}.</p></details>')
    html_ = (f'<h3 class="rs-sh">먼저 낱말</h3>{term_html}'
             f'<h3 class="rs-sh">어떻게 비교했나</h3>{step_html}'
             f'<h3 class="rs-sh">결과 — 두 심판 한눈에</h3><div class="jd-nums">{num_html}</div>{tab_cmp}'
             f'<h3 class="rs-sh">그래서 Codex</h3>{dont}<p class="rs-basis">{E(nxt)}</p>'
             f'<h3 class="rs-sh">왜 Claude CLI 가 오래 걸렸나</h3>{slow_html}'
             f'{wob}{more}')
    key = (f"같은 답 {n_ans}개를 Claude CLI 로도 판정해 보니 {same}개는 결론이 같았고, 갈린 {len(flip)}개 대부분은 '불필요 · 반복' 한 기준의 해석 차이였습니다. "
           + (f"다시 보니 어느 쪽도 더 정확하지 않아({rv['codex']} : {rv['claude']}), " if rv else "")
           + (f"같은 일을 {t_ratio:.1f}배 빨리 끝내는 Codex 를 골랐습니다." if t_ratio else "Codex 를 골랐습니다."))
    return html_, key


def scene_time(D: Dict[str, Any]) -> str:
    order = D["order"]
    W = judge_work(D)
    jmx = max((W[p]["seconds"] for p in order), default=0) or 1
    jbars = "".join(f'<div class="rs-tb"><span class="rs-bl">{mname(p)}</span><span class="rs-tbar"><i style="width:{W[p]["seconds"] / jmx * 100:.1f}%"></i></span>'
                    f'<span class="rs-tv"><b>{hm(W[p]["seconds"])}</b><small>질문당 {W[p]["seconds"] / max(1, D["n"]):.0f}초</small></span></div>' for p in order)
    judge = (f'<div class="rs-judge"><div class="rs-jt"><b>{hm(jmx)}</b><span>판정에 실제로 걸린 시간<br>모델마다 질문 판정 시간의 합 중 가장 긴 것(네 모델 동시)</span></div>'
             f'<div class="rs-jb"><div class="rs-tbars">{jbars}</div></div></div>')
    jr = [[mname(p), f'{W[p]["jev"]:,}', f'{W[p]["dec"]:,}', f'{W[p]["codex_calls"]:,}'] for p in order]
    jr.append(['<span class="rs-strong">합계</span>', f'{sum(W[p]["jev"] for p in order):,}', f'{sum(W[p]["dec"] for p in order):,}',
               f'{sum(W[p]["codex_calls"] for p in order):,}'])
    jt = (table("심판별 판정량 — 모델마다", ["모델", "Jev 판정(턴)", "Decisions 판정(턴)", "Codex 호출"], jr, [28, 24, 24, 24])
          + '<p class="rs-basis">1차 심판 — 질문 전체가 3만 2천 토큰 이하이고 첨부가 없으면 Jev, 그 밖(롱컨텍스트 · 첨부 · 멀티턴)은 Decisions. '
            '1차가 FAIL · 애매로 본 항목만 Codex 가 다시 판정. 다시 판정 · 복구까지 모두 더한 값입니다. '
          + 'Jev · Decisions 는 외부 API · Codex CLI 는 ChatGPT 구독으로 실행.</p>')
    # 10-20 보고 때(다른 78개 세트) 수집 시간 참고 줄은 뺌 — 이 실행의 숫자만 (받은 의견 E: 질문 세트 · 실행 고정)
    # 사용자 10-22 미팅 6번: 모델별 답변 시간 합 막대 · 유형별 평균 표 대신 질문 · 유형별 편차 (time_spread)
    return time_spread(D) + '<h3 class="rs-sh">판정 — 실제로 걸린 시간</h3>' + judge + jt + token_table(D)


def token_table(D: Dict[str, Any]) -> str:
    """판정 토큰 — 심판별 (10-22 미팅 7번: 답을 받은 뒤 판정에 드는 토큰은 우리 내부 데이터라 알아야 함, 사용자 "판정쪽만 하자").
    답변 수집 쪽은 앱 · 웹 화면이라 셀 수 없어 넣지 않음. 비용은 단가가 정해지면."""
    T = judge_tokens(D)
    if not T:
        return ""
    n_ans = max(1, D["n"] * len(D["order"]))

    def tk(v: float) -> str:
        return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.0f}K" if v >= 1e3 else f"{v:,.0f}"

    def cell(t: Dict[str, Any], col: str) -> str:
        return tk(t[col]) if col in t["seen"] else '<span class="rs-na">기록 안 됨</span>'

    rows = []
    for name in sorted(T, key=lambda k: JUDGE_ORDER.index(k) if k in JUDGE_ORDER else len(JUDGE_ORDER)):
        t = T[name]
        per = (t["in"] + t["out"]) / n_ans
        rows.append([E(name), f'{t["calls"]:,}' + ("번 호출" if name == "Codex" else "턴"), cell(t, "in"), cell(t, "cached"), cell(t, "out"),
                     cell(t, "reason"), tk(per) + ("" if "out" in t["seen"] else " <small>(입력만)</small>")])
    tot_in, tot_out = sum(t["in"] for t in T.values()), sum(t["out"] for t in T.values())
    rows.append(['<span class="rs-strong">합계</span>', "", tk(tot_in), "", tk(tot_out), "", tk((tot_in + tot_out) / n_ans)])
    missing = any(col not in t["seen"] for t in T.values() for col in USAGE_KEYS)
    return (table("판정 토큰 — 심판별", ["심판", "판정", "입력", "그중 캐시", "출력", "그중 추론", "답 하나 판정당"], rows, [19, 12, 13, 13, 13, 13, 17])
            + '<p class="rs-basis">심판이 호출마다 돌려준 사용량을 그대로 더한 값입니다 — 다시 묻기 · 복구 호출도 들어갑니다. '
              'Codex 는 CLI 가 덧붙이는 시스템 프롬프트 · 도구 설명과 추론까지 셉니다. 캐시 = 앞 호출과 같은 앞부분이라 다시 읽은 입력(단가가 낮음). '
              f'답 하나 판정당 = (입력 + 출력) ÷ ({D["n"]}문항 × {len(D["order"])}모델). 답변 수집(앱 · 웹 화면)은 토큰이 보이지 않아 판정만 셉니다.'
            + (' 기록 안 됨 = 이 실행 때는 그 칸을 남기지 않았음(10-22 부터 남김).' if missing else "") + "</p>")


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
    leads = [p for p in order if rank[p] == 1]   # 공동 1위 (사용자 10-22: GPT · Claude 55 동률인데 한 모델만 적던 것)
    rivals = [p for p in order if rank[p] > 1 and T[p]["PASS"] + T[p]["HOLD"] >= T[lead]["PASS"]]   # 보류가 모두 통과면 1위와 같거나 넘음
    return {"rank": rank, "lead": lead, "leads": leads, "rivals": rivals, "hold": sum(T[p]["HOLD"] for p in order)}


def dist_card(D: Dict[str, Any]) -> str:
    """결과 분포 — 모델마다 완전 통과 · 보류 · FAIL 을 쌓은 막대 (받은 의견 A: 순위만 보이면 확정처럼 읽힘 → 분포와 '잠정').
    초록 끝 = 지금 완전 통과, 노랑 빗금 끝 = 보류가 모두 통과일 때 최대, 점선 = 1위의 지금 완전 통과."""
    order, n, T = D["order"], D["n"], D["tally"]
    st = dist_stats(D)
    lp = T[st["lead"]]["PASS"]
    top = " · ".join(SHORT[p] for p in st["leads"])
    first = f"1위({lp})" if len(st["leads"]) > 1 else f"{top}({lp})"
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
                  + f" — 보류를 어떻게 정하느냐에 따라 {first}와 순위가 바뀔 수 있습니다.")
    else:
        change = f"보류를 모두 통과로 정해도 {first}를 넘는 모델은 없지만, 숫자와 아래 순위는 바뀔 수 있습니다."
    legend = ('<div class="rs-legend ds-leg"><span><i class="rs-sw rs-pass"></i>완전 통과</span><span><i class="rs-sw rs-hold"></i>보류 (사람이 정할 칸)</span>'
              f'<span><i class="rs-sw rs-fail"></i>FAIL</span><span class="muted">점선 = 1위의 지금 완전 통과 {lp}</span></div>')
    return (f'<div class="ar-card ds-card"><div class="ar-ch"><h3>결과 분포</h3><span class="ds-tag">잠정</span><span class="ar-chip">질문 단위 · 완전 통과 순</span></div>'
            f'<p class="ds-note">이번 {n}개 질문과 이 실행 조건에서 완전 통과가 가장 많은 모델은 <b>{top}</b>{f"(공동 {lp})" if len(st["leads"]) > 1 else ""}입니다. '
            f'보류 {st["hold"]}칸은 아직 사람이 정하지 않았습니다 — {E(change)}</p>'
            f'{legend}<div class="ds-rows">{"".join(rows)}</div>{unit_note(D)}</div>')


def q_class_label(D: Dict[str, Any], q: int) -> str:
    """질문 하나의 분류 — 유형 · 난이도 · 그 질문이 요구하는 기준 묶음(늘 보는 기준 · 답에 따라 보는 기준은 뺌). 질문 원문 대신 (이사님 10-23, 할일 11번)."""
    qd = D["qset"].get(q) or {}
    if "_when" not in D:
        f = Path(D["set_dir"]) / "criteria_catalog.json"
        cat = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        D["_when"] = (set(cat.get("always_ids") or []), set(cat.get("answer_dependent_ids") or []))
    always, by_answer = D["_when"]
    short_code = {"1-1": "응답", "1-2": "코드", "1-3": "인용", "2-1": "글자 수", "2-2": "구조", "3-1": "계산", "3-2": "개수", "3-3": "집계"}
    llm: List[str] = []
    code: List[str] = []
    for cid in qd.get("target_items") or []:
        if cid in always or cid in by_answer:
            continue
        if cid.startswith("J"):
            g = cid.split("-")[0]
            lab = f"{g} {GROUP.get(g, '')}".strip()
            if lab not in llm:
                llm.append(lab)
        else:
            g = code_group(cid)
            lab = short_code.get(g, g)
            if lab not in code:
                code.append(lab)
    groups = llm + code   # 의미 기준 묶음을 앞에, CODE 묶음은 짧은 이름으로 — 칸이 좁다
    more = f" +{len(groups) - 3}" if len(groups) > 3 else ""
    parts = [f"<b>{E(TYPE_NAME.get(qd.get('type') or '', qd.get('type') or ''))}</b>"] + ([E(qd["difficulty"])] if qd.get("difficulty") else []) + [E(" · ".join(groups[:3]) + more)]
    return " · ".join(p for p in parts if p)


def numbers_key(D: Dict[str, Any]) -> str:
    """숫자 읽는 법 — 보고서에 나오는 숫자마다 뜻 한 줄 (이사님 10-23: 78 질문 · 72 기준 · 질문당 6~10 · 판정 건수 같은 숫자와 용어가 섞여 혼동, 할일 11번)."""
    order, n = D["order"], D["n"]
    n_crit = len(D["names"])
    n_code = sum(1 for c in D["names"] if not str(c).startswith("J"))
    n_empty = sum(1 for t in D["qtypes"].values() if t == "명시적 빈 출력")
    per = [len(crit_verdicts(D, p, q)) for p in order for q in D["qtypes"] if D["qtypes"][q] != "명시적 빈 출력"]
    cr = crit_results(D)
    n_dec = sum(sum(cr[c][order[0]].values()) for c in cr) if order else 0
    rows = [
        (f"{n}", "질문", f"AI 에게 보낸 요청 수. 10턴 대화도 질문 1개로 셈. 네 모델에 같은 {n}개."),
        (f"{n_crit}", "기준", f"답을 점검하는 항목 수 — CODE {n_code}개(프로그램) + LLM Judge {n_crit - n_code}개(AI 심판). 질문마다 해당하는 기준만 봄."),
        ("6~10", "질문당 요구 기준", "질문 하나가 요구하는 기준 수." + (f" 늘 보는 기준 · 답에 따라 보는 기준을 더하면 한 질문에 평균 {statistics.mean(per):.0f}개를 판정." if per else "")),
        (f"{n_dec:,}", "기준 판정 건수", f"질문 {n - n_empty}개 × 그 질문에 적용된 기준 — 모델마다 같은 수. 빈 출력 요구 {n_empty}문항은 보류라 뺌."),
        ("통과 · FAIL · 보류", "질문 단위", "적용 기준을 모두 통과 / 하나라도 어김 / 심판이 확인 못 한 기준이 남음(사람이 정함)."),
        ("%", "통과율", "완전 통과 ÷ 질문 수. '기준 통과율'(기준별 결과)은 기준 판정 중 통과한 비율이라 90%대로 더 높게 나옴."),
    ]
    return ('<div class="nk"><b class="nk-h">숫자 읽는 법</b>'
            + "".join(f'<div class="nk-r"><b>{E(v)}</b><span>{E(k)}</span><p>{E(t)}</p></div>' for v, k, t in rows) + "</div>")


def unit_note(D: Dict[str, Any]) -> str:
    """질문 단위 · 기준 단위 — 두 숫자가 왜 다른지 (받은 의견 B). 보류는 어느 쪽에서도 통과로 세지 않는다."""
    order, n = D["order"], D["n"]
    per = [len(crit_verdicts(D, p, q)) for p in order for q in D["qtypes"] if D["qtypes"][q] != "명시적 빈 출력"]
    cr = crit_results(D)
    tot = {p: sum((cr[c][p] for c in cr), Counter()) for p in order}
    lead = dist_stats(D)["lead"]
    rate = tot[lead]["PASS"] / max(1, sum(tot[lead].values()))
    return (f'<div class="ds-unit"><div><b>질문 단위 — 이 장면</b><p>완전 통과 = 질문 하나에 적용된 기준(질문이 요구한 6~10개에 늘 보는 기준 · 답에 따라 보는 기준을 더해 평균 {statistics.mean(per):.0f}개)을 <b>모두</b> 통과한 질문 수. '
            f'하나만 어겨도 FAIL, 하나라도 확인 못 하면 보류.</p></div>'
            f'<div><b>기준 단위 — 상세 분석 "기준별 결과"</b><p>기준 판정 하나하나 중 통과한 비율(해당 없음도 통과로 셈). 그래서 훨씬 높게 나옵니다 '
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
        groups[("CODE 검사 " + code_group(cid)) if cid[0].isdigit() else cid.split("-")[0]].append(cid)
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
    tabs = [("apx1", "질문별 결과", f"{D['n']}문항 × {len(order)}모델", p1), ("apx2", "기준별 어긴 질문", "72개 · 어긴 질문 수 · 질문 번호", p2),
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
.rs-na { color: var(--muted); font-size: .92em; }
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
.nk { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px 14px; margin: 0 0 14px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; }
.nk-h { grid-column: 1 / -1; font-size: 0.8rem; color: var(--muted); } .nk-r b { font-family: var(--font-num); font-size: 1.02rem; } .nk-r span { margin-left: 6px; font-size: 0.82rem; font-weight: 600; }
.nk-r p { margin: 2px 0 0; font-size: 0.78rem; color: var(--muted); line-height: 1.45; word-break: keep-all; }
.cb-card small.cb-q { color: var(--ink); border-top: 1px dashed var(--line); padding-top: 4px; }
.hm-ref { margin: 10px 0 0; font-size: 0.8rem; color: var(--muted); line-height: 1.55; word-break: keep-all; } .hm-ref b { color: var(--ink); }
@media (max-width: 760px) { .ds-row { grid-template-columns: 18px 24px minmax(0, 1fr) 64px; } .ds-bar { grid-column: 1 / -1; order: 5; } .ds-x { grid-column: 1 / -1; order: 6; }
  .ds-unit { grid-template-columns: minmax(0, 1fr); } .nk { grid-template-columns: minmax(0, 1fr); } }
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
.pr-h { margin-top: 28px; scroll-margin-top: 16px; }
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
@media (max-width: 560px) { .rs-next { grid-template-columns: minmax(0, 1fr); } .rs-close.rs-cards { grid-template-columns: minmax(0, 1fr); } }
.rs-close { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.rs-close ul { margin: 0; padding-left: 1.1em; font-size: 0.86rem; line-height: 1.55; }
.rs-close-end { margin: 14px 0 0; font-weight: 700; }
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
/* 심판 비교 (사용자 10-22 미팅 3번) */
.jd-nums { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }   /* 사용자 10-22: 겉보기 → FAIL 만 → 최종 */
.jd-n { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; display: grid; gap: 4px; align-content: start; }
.jd-n i { font-style: normal; font-size: 0.74rem; font-weight: 700; color: var(--muted); }
.jd-n b { font-family: var(--font-num); font-size: 1.9rem; line-height: 1.15; } .jd-n span { font-size: 0.9rem; font-weight: 600; } .jd-n small { font-size: 0.76rem; color: var(--muted); }
.jd-n.jd-warn { border-color: var(--warn); } .jd-n.jd-warn b { color: var(--warn); } .jd-n.jd-good b { color: var(--ok); }
.jd-key { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 0.8rem; color: var(--muted); margin: 0 0 8px; } .jd-key span { display: inline-flex; align-items: center; gap: 6px; }
.jd-f, .jd-a { display: inline-block; width: 16px; height: 16px; border-radius: 4px; } .jd-f { background: var(--risk); } .jd-a { background: color-mix(in srgb, var(--muted) 45%, var(--surface)); }
.jd-bars { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 8px 16px; display: grid; }
.jd-br { display: grid; grid-template-columns: minmax(0, 260px) calc(var(--sq, 3) * 20px) minmax(0, 1fr); gap: 14px; align-items: center; padding: 7px 0; border-bottom: 1px solid color-mix(in srgb, var(--line) 60%, transparent); }
.jd-br:last-child { border-bottom: 0; } .jd-bl { font-size: 0.88rem; font-weight: 600; } .jd-bt { display: flex; gap: 4px; }
.jd-bv { font-size: 0.84rem; } .jd-bv b { font-family: var(--font-num); } .jd-bv small { color: var(--muted); }
.jd-rest .jd-bl, .jd-rest .jd-bv { color: var(--muted); font-weight: 400; }
.jd-cards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.jd-c { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; display: grid; gap: 10px; align-content: start; }
.jd-ch { font-weight: 700; font-size: 0.98rem; } .jd-ch > span { color: var(--muted); font-weight: 400; }
.jd-cn { font-size: 0.82rem; color: var(--muted); display: flex; flex-wrap: wrap; align-items: center; gap: 4px; } .jd-cn b { color: var(--ink); margin-right: 2px; }
.jd-chip { padding: 1px 8px; border: 1px solid var(--line); border-radius: 999px; background: var(--bg); font-size: 0.76rem; color: var(--ink); white-space: nowrap; }
.jd-dl { display: grid; grid-template-columns: 64px minmax(0, 1fr); gap: 8px 10px; margin: 0; font-size: 0.86rem; line-height: 1.5; }
.jd-dl dt { color: var(--muted); font-size: 0.78rem; padding-top: 2px; } .jd-dl dd { margin: 0; } .jd-dl .jd-who { font-weight: 700; color: var(--accent); }
.jd-v { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.jd-v > div { border-radius: 12px; padding: 14px 16px; display: grid; gap: 4px; align-content: start; } .jd-v i { font-style: normal; font-size: 0.74rem; font-weight: 700; }
.jd-v b { font-size: 1rem; } .jd-v p { margin: 0; font-size: 0.86rem; line-height: 1.55; }
.jd-vok { background: var(--ok-soft); border: 1px solid color-mix(in srgb, var(--ok) 35%, transparent); } .jd-vok i { color: var(--ok); }
.jd-vwarn { background: var(--warn-soft); border: 1px solid color-mix(in srgb, var(--warn) 40%, transparent); } .jd-vwarn i { color: var(--warn); }
.jd-more { margin-top: 14px; } .jd-more h4 { margin: 14px 0 6px; }
.jc-steps { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; counter-reset: jc; }   /* 사용자 10-22 미팅 4번: 말할 순서 */
.jc-steps li { counter-increment: jc; display: grid; grid-template-columns: 30px minmax(0, 1fr); gap: 2px 12px; background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px 16px; }
.jc-steps li::before { content: counter(jc); grid-row: 1 / span 2; width: 28px; height: 28px; border-radius: 50%; background: var(--accent); color: var(--surface); display: grid; place-items: center; font-weight: 700; font-family: var(--font-num); }
.jc-steps b { font-size: 0.98rem; } .jc-steps span { font-size: 0.86rem; color: var(--muted); line-height: 1.55; }
.cmpt.jd-t td b { color: var(--ink); } .cmpt.jd-t td b.jd-hot { color: var(--risk); }
@media (max-width: 1000px) { .jd-nums, .jd-cards, .jd-v { grid-template-columns: minmax(0, 1fr); } .jd-br { grid-template-columns: minmax(0, 1fr) auto; } .jd-bt { grid-column: 1 / -1; grid-row: 2; } .jd-bv { text-align: right; } }
/* 걸린 시간 — 질문 · 유형별 편차 (사용자 10-22 미팅 6번) */
.lt-sum { display: grid; gap: 6px; overflow-x: auto; }
.lt-sr { display: grid; grid-template-columns: minmax(104px, 1fr) repeat(4, minmax(92px, 1fr)); gap: 6px; align-items: stretch; }
.lt-sh span { font-size: 0.8rem; color: var(--muted); text-align: center; align-self: end; } .lt-sh span:first-child { text-align: left; }
.lt-sh .lt-meh { color: var(--accent); font-weight: 700; }
.lt-st { display: grid; align-content: center; gap: 1px; } .lt-st small { font-size: 0.74rem; color: var(--muted); }
.lt-sc { display: grid; justify-items: center; align-content: center; gap: 1px; padding: 10px 6px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface); text-align: center; }
.lt-sc b { font-family: var(--font-num); font-size: 1.12rem; font-variant-numeric: tabular-nums; white-space: nowrap; }
.lt-sc em { font-style: normal; font-size: 0.78rem; color: var(--ink); } .lt-sc em.lt-best { color: var(--ok); font-weight: 700; }
.lt-sc small { font-size: 0.7rem; color: var(--muted); white-space: nowrap; }
.lt-sc.lt-mec { border: 2px solid var(--accent); }
.lq-key { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 0.78rem; color: var(--muted); margin-bottom: 8px; }
.lq-key span { display: inline-flex; align-items: center; gap: 6px; } .lq-sw { width: 14px; height: 10px; border-radius: 3px; display: inline-block; }
.lq-sw-f { background: color-mix(in srgb, var(--ok) 40%, transparent); } .lq-sw-s { background: color-mix(in srgb, var(--risk) 40%, transparent); } .lq-sw-r { background: var(--hold-soft); }
.lq-tabs label small { color: var(--muted); margin-left: 2px; } .lq-ps .lq-list { margin-bottom: 8px; }
.lq-list { display: grid; min-width: 0; overflow-x: auto; }
.lq-r { display: grid; grid-template-columns: minmax(0, 1.9fr) repeat(4, minmax(96px, 1fr)); column-gap: 10px; }   /* 사용자 10-22: 모델마다 세로 상자 · 질문도 따로. 질문 칸은 분류 라벨이 두 줄까지 (10-25) */
.lq-q, .lq-col { background: var(--surface); border: 1px solid var(--line); border-top: 0; border-bottom: 1px solid color-mix(in srgb, var(--line) 55%, transparent); padding: 4px 8px; min-width: 0; }
.lq-q { display: grid; grid-template-columns: 40px minmax(0, 1fr); gap: 6px; align-items: center; }
.lq-col { display: grid; align-items: center; }   /* 안의 막대 칸이 상자 폭을 꽉 채우게 */
.lq-h .lq-q, .lq-h .lq-col { border-top: 1px solid var(--line); border-radius: 10px 10px 0 0; border-bottom: 2px solid var(--line); padding: 8px; font-size: 0.76rem; color: var(--muted); white-space: nowrap; }
.lq-h .lq-q { display: block; } .lq-h .lq-col { text-align: center; } .lq-h .lt-meh { color: var(--accent); font-weight: 700; }   /* 모델 이름은 막대 칸 가운데 */
.lq-r:last-child .lq-q, .lq-r:last-child .lq-col { border-bottom: 1px solid var(--line); border-radius: 0 0 10px 10px; padding-bottom: 6px; }
.lq-col.lq-me { border-left-color: var(--accent); border-right-color: var(--accent); }
.lq-h .lq-col.lq-me { border-top-color: var(--accent); } .lq-r:last-child .lq-col.lq-me { border-bottom-color: var(--accent); }
.lq-n { font-family: var(--font-num); font-size: 0.82rem; font-weight: 600; } .lq-t { font-size: 0.7rem; color: var(--muted); line-height: 1.3; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; word-break: keep-all; } .lq-t b { color: var(--ink); }
.lq-c { --bar: color-mix(in srgb, var(--muted) 20%, transparent); padding: 3px 8px; border-radius: 6px; border: 1px solid var(--line); text-align: right; white-space: nowrap;
  font-family: var(--font-num); font-size: 0.8rem; font-variant-numeric: tabular-nums; background: linear-gradient(to right, var(--bar) var(--w, 0%), transparent var(--w, 0%)), var(--bg); }
.lq-c.lq-fast { --bar: color-mix(in srgb, var(--ok) 34%, transparent); } .lq-c.lq-slow { --bar: color-mix(in srgb, var(--risk) 34%, transparent); font-weight: 700; }
.lq-c.lq-redo { background: var(--hold-soft); border-color: var(--hold); color: var(--hold-text); font-family: inherit; font-size: 0.7rem; text-align: center; }
.lt-finds { margin: 0; padding-left: 20px; display: grid; gap: 6px; font-size: 0.9rem; }
.lt-sh .lt-bestc, .lt-sc .lt-bestc { display: block; font-style: normal; font-size: 0.72rem; font-weight: 700; color: var(--ok); }
.lt-s1 { background: color-mix(in srgb, var(--risk) 9%, var(--surface)); } .lt-s2 { background: color-mix(in srgb, var(--risk) 16%, var(--surface)); }
.lt-s3 { background: color-mix(in srgb, var(--risk) 24%, var(--surface)); } .lt-s4 { background: color-mix(in srgb, var(--risk) 34%, var(--surface)); }
.lt-f1 { background: color-mix(in srgb, var(--ok) 10%, var(--surface)); } .lt-f2 { background: color-mix(in srgb, var(--ok) 17%, var(--surface)); }
.lt-f3 { background: color-mix(in srgb, var(--ok) 25%, var(--surface)); } .lt-f4 { background: color-mix(in srgb, var(--ok) 34%, var(--surface)); }
.lt-sc em.lt-vs-s { color: var(--risk); font-weight: 600; } .lt-sc em.lt-vs-f { color: var(--ok); font-weight: 600; }
.lt-t1 { background: color-mix(in srgb, var(--risk) 10%, var(--surface)); } .lt-t2 { background: color-mix(in srgb, var(--risk) 18%, var(--surface)); }
.lt-t3 { background: color-mix(in srgb, var(--risk) 28%, var(--surface)); } .lt-t4 { background: color-mix(in srgb, var(--risk) 40%, var(--surface)); }
@media (max-width: 560px) { .lt-sr { grid-template-columns: 64px repeat(4, minmax(84px, 1fr)); } .lt-sc b { font-size: 0.9rem; white-space: normal; } .lt-sc small { white-space: normal; }
  .lq-r { grid-template-columns: 48px repeat(4, minmax(66px, 1fr)); column-gap: 4px; } .lq-q { grid-template-columns: minmax(0, 1fr); } .lq-t { display: none; }
  .lq-q, .lq-col { padding: 4px 4px; } }
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


def build(run_id: str, report_dir: Optional[Path] = None) -> Tuple[List[Path], Dict[str, Any]]:
    """report_dir 없음 — 발표 템플릿(참고자료/발표/MAIN_NAME: 핵심 장면 + 상세 분석).
    report_dir 있음 — 결과 보고서(<report_dir>/report.html: 상세 분석 장면만, 원문은 <report_dir>/원문/) (사용자 10-22)."""
    D = load(run_id)
    deck = DECK.read_text(encoding="utf-8")
    css = "\n".join(f"<style>{b}</style>" for b in re.findall(r"<style>(.*?)</style>", deck, re.S))
    nav_script = deck[deck.index("<script>"):deck.index("</script>") + len("</script>")]
    # 원문은 따로 — 앱 미리보기 한도(512KB) 때문에 본문 파일에는 6장면 · 질문별 표 · 보류 원문 · 기준 72개만
    parts = split_parts(D, fail_blocks(D))
    pparts = split_parts(D, pass_blocks(D), prefix="통과")   # 통과한 답 — 원문 · 지킨 기준 (사용자 10-21)
    hparts = split_parts(D, hold_blocks(D), prefix="보류")   # 보류 칸 — 원문 · 보류 사유 (사용자 10-21)
    sub = "원문" if report_dir is not None else f"ModelGrade_결과요약_{run_id}_원문"   # 원문 파일은 이 하위 폴더에 (사용자 10-21: 발표 폴더 정리)
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
    core = [] if report_dir is not None else [   # 결과 보고서는 상세 분석 장면만 (사용자 10-22)
        ("s_intro", "왜 필요한가", "만든 이유", "왜 만들었나", "", scene_intro(D),
         "AIONA 가 업데이트될 때마다 같은 조건으로 다시 테스트해, 채팅 품질을 꾸준히 높이기 위해 만들었습니다."),
        # 사용자 10-22: 순서 — 왜 만들었나 → 전체 흐름도 → 질문 설계 → 평가 방법 (큰 그림을 먼저)
        ("s_map", "전체 흐름도", "A~H 단계", "전체 흐름도",
         f"질문 {D['n']}개를 네 모델에 던져 답을 받고, CODE 검사와 LLM Judge 가 채점해 질문마다 완전 통과·FAIL·보류를 냅니다.", flow_scene(D),
         "질문 세트부터 보고서까지 명령 하나로 돌고, AI 가 끼는 단계는 답변 수집(B)과 LLM Judge(E) 두 곳입니다."),
        ("s_design", "질문 설계", "왜 78개 · 왜 기준 6~10개", "질문 설계 — 왜 78개, 왜 질문마다 기준 6~10개", "", scene_design(D),
         "기준 6~10개는 모델 차이가 가장 잘 드러나는 난이도, 78개는 모든 기준을 질문 10개 이상에서 평가하는 가장 적은 수입니다."),
        ("s2", "평가 방법", "다섯 단계", "평가 흐름", "", scene_flow(D)
         + '<h3 class="rs-sh pr-h" id="s_prompt">판정 프롬프트 — 심판이 받는 글 원문</h3>' + scene_prompt(D, open_first=False),   # 사용자 10-22: 평가 방법 쪽으로
         f"질문 {D['n']}개 → {len(D['order'])}모델 답 → 기준 {len(D['names'])}개(CODE 검사 {n_code} + LLM Judge {len(D['names']) - n_code})로 채점 → 질문마다 완전 통과 · FAIL · 보류"),
        # 05 검증 장면은 뺌 (사용자 10-22: "이 검증파트는 빼도 될 것 같네") — scene_verify 는 남겨 둠
        ("s_out", "결과", "어떻게 제공하나", "결과 — 이렇게 제공합니다",
         "실행이 끝나면 명령 하나로 결과 폴더(평가를 시작한 시각 이름)에 결과 보고서 · 결과 JSON · 판정 보고서가 나옵니다. 이번 실행의 숫자는 보류를 정하기 전이라 상세 분석에 따로 둡니다.",
         scene_output(D), "질문마다 완전 통과 · FAIL · 보류와 기준별 사유 · 원문을 함께 제공합니다 — 이번 실행 숫자는 상세 분석 '이번 실행 결과'에."),
        ("s_hold", "보류", "어떻게 사람에게 넘기나", "보류 — 이렇게 사람에게 넘깁니다",
         "보류는 심판이 확정하지 못한 칸입니다. 원문과 사유를 붙여 사람에게 넘기고, 사람이 정한 값으로 다시 셉니다.",
         scene_hold_flow(D), "확인하지 못한 판정은 억지로 정하지 않고 원문 · 사유와 함께 사람에게 넘깁니다 — 이번 실행의 목록은 상세 분석 '보류 목록'에."),
        ("s7", "발전 방향", "확장 · 개선", "발전 방향", "", scene_next(D),
         "가장 먼저 사람 판정과 맞춰 보고, 보류 사례집 · " + ("한 번 요청 판정" if three_judges(D) else "심판 셋 구조로 다시 판정") + " · 다른 AI 서비스 연결로 넓힙니다."),
        # 10-22 미팅 11번 (사용자 10-23: "11번 진행") — 발전 방향 다음, 보고 이야기의 끝
        ("s_close", "마감 · 운영", "다음 주 마감 · 상시 운영", "마감 · 상시 운영 — 다음 주에 닫고, 그 뒤엔 업데이트마다 한 바퀴",
         "다음 주(10-26 ~ 10-30) 안에 범위를 닫고, 그 뒤에는 일부 리소스로 상시 운영하며 패치합니다. 일정 · 범위는 10-23 에 정했고, 바뀌면 미리 알립니다.",
         scene_close(D), "다음 주 금요일(10-30)에 범위를 닫고, 그 뒤엔 AIONA 업데이트마다 한 바퀴 — 패치는 기준 문서 → 프롬프트 · 코드 순서로, 같은 칸 전후 비교를 넘을 때만 받습니다."),
    ]
    detail = [   # 상세 분석 — 발표 뒤 필요할 때 보는 화면
        ("s1", "이번 실행 결과", "분포 · 유형별 통과율 (잠정)", "이번 실행 결과 — 분포와 유형별 통과율 (잠정)",
         "맨 위는 모델마다 완전 통과 · 보류 · FAIL 분포, 그 아래는 유형별 통과율입니다. 모두 질문 단위이고, 보류를 사람이 정하기 전의 잠정 결과입니다.",
         numbers_key(D) + dist_card(D) + helm_table(D, cats), res_key),
        ("s1b", "분야별 순위표", "분야를 고르면 바뀜", "분야별 순위표",
         "분야를 고르면 순위표가 바뀝니다. '이번 실행 결과'의 통과율 숫자를 눌러도 이 표가 그 분야로 바뀝니다.", ar_board(D, cats), ""),
        ("s_crit", "기준별 결과", "기준마다 통과율", "기준별 결과 — 기준마다 통과율",
         "질문 하나를 통째로 보는 '완전 통과'와 달리, 기준 하나하나가 적용된 질문에서 얼마나 통과했는지 봅니다.", scene_by_crit(D), ""),
        ("s_code", "CODE 검사", "프로그램이 센 결과", "CODE 검사 결과 — 프로그램이 센 것만",
         "형식 · 개수 · 글자 수 · 계산처럼 프로그램이 정해진 규칙으로 센 결과만 따로 봤습니다. 막대는 CODE 검사를 모두 통과한 질문과 하나라도 걸린 질문입니다.", scene_code(D), ""),
        ("s5", "걸린 시간", "수집 · 판정", "걸린 시간 — 답변 수집과 LLM Judge 판정",
         f"답변 수집 {hm(collect_seconds(D))} · 판정 {hm(judge_s)}. 모델별 합보다 질문 하나하나의 시간이 유형에 따라 어떻게 다른지가 개선할 곳을 알려 줍니다.", scene_time(D), ""),
        ("s3", "FAIL 원인", "많이 어긴 기준", "FAIL 원인 — LLM Judge 가 많이 어긴 기준 10개",
         "숫자는 그 기준을 어긴 질문 수입니다. 색이 진할수록 많이 어겼습니다.", scene_criteria(D), ""),
        ("s4", "보류 목록", f"사람이 정할 {n_hold}칸", f"보류 목록 — 사람이 정할 {n_hold}칸 (빈 출력 규칙 {n_hold_empty} · LLM Judge 확인 못 함 {n_hold - n_hold_empty})",
         "보류는 심판이 확정하지 못한 기준이 남은 칸입니다. 지금은 사람이 정하고, 그 판단을 사례로 모아 다음 판정에 쓰는 것은 만드는 중입니다.", scene_hold(D, part_of),
         f"사람이 정할 칸 {n_hold} = 빈 출력 요구 {n_hold_empty} + 확인 못 함 {n_hold - n_hold_empty} — 아직 정하지 않아, 정하면 순위 · 통과율이 바뀔 수 있습니다."),
        ("s6", "질문별 결과", f"{D['n']}문항 × {len(D['order'])}모델 · 원문", "질문별 결과",   # 사용자 10-22: 제목 뒤 '— 필요할 때만 펼치기' · 안내 문장 지움
         "", appendix(D, parts, part_of, f"{sub}/{e_file}", n_disagree, sub, pparts, hparts), ""),
    ]
    jd_html, jd_key = scene_judge_compare(D)   # 사용자 10-22 미팅 3번 — 기존 장면 번호가 바뀌지 않게 맨 끝에
    if jd_html:
        detail.append(("s_jd", "심판 비교", "Jev · Decisions 갈림", "심판 비교 — Jev 와 Decisions 가 갈린 곳",
                       "같은 답을 두 심판이 따로 판정한 기록과 예전 실험으로, 갈림이 어느 기준에 몰렸고 최종 결론에서 누가 맞았는지 봤습니다. 확인만 하고 판정 방식은 바꾸지 않았습니다.",
                       jd_html, jd_key))
    jc_html, jc_key = scene_judge_choice(D)   # 사용자 10-22 미팅 4번 — 심판 비교 다음에
    if jc_html:
        detail.append(("s_jc", "심판 선택", "Codex 를 고른 근거", "심판 선택 — 2차 심판을 Codex 로 고른 근거",
                       f"2차 심판만 Codex 와 Claude CLI 로 바꿔 같은 답 {D['n'] * len(D['order'])}개(질문 {D['n']} × 모델 {len(D['order'])})를 판정해 비교했습니다. 시간은 정확도가 같을 때의 이유입니다.",
                       jc_html, jc_key))
    scenes = core + detail
    total, n_core = len(scenes), len(core)

    def head(i: int, title: str, lead: str, key: str) -> str:
        tag = f"{i:02d} / {total:02d}" + (" · 상세 분석" if n_core and i > n_core else "")
        return (f'<div class="s-head"><span class="s-no">{tag}</span><h2>{title}</h2>'
                + (f'<p class="s-key">{E(key)}</p>' if key else "") + (f"<p>{lead}</p>" if lead else "") + "</div>")

    s0 = f'<header class="ar-s0">{ar_overview(D)}</header>'   # 사용자 10-21: 제목 줄은 맨 위에 두고 메뉴의 개요는 뺌
    # 사용자 10-22: 본문의 '상세 분석' 구분 상자는 뺌 (목차 구분 · 장면 번호 옆 '· 상세 분석'은 그대로)
    nav_sep: Dict[str, str] = {}   # 사용자 10-22: 목차의 'LLM Judge' 묶음 구분(글자 · 선) 뺌
    body = "".join(f'\n<section class="slide" id="{sid}">\n  {head(i, E(title), E(lead), key)}\n{content}</section>\n'
                   for i, (sid, _s, _d, title, lead, content, key) in enumerate(scenes, 1))
    if report_dir is not None:   # 결과 보고서는 통째로 상세 분석 — "상세 분석 '분야별 순위표'" 같은 안내는 장면 이름만
        body = re.sub(r"상세 분석 (?=['‘\"]|&quot;|&#x27;)", "", body)
    nav = ('<nav class="topnav" aria-label="순서"><div class="topnav-in"><span class="brand">AIONA 채팅 품질 평가</span>'
           '<div class="toc-links" id="toc-links">'
           + "".join(('<span class="toc-sep">상세 분석</span>' if n_core and i == n_core + 1 else "") + nav_sep.get(sid, "") + f'<a href="#{sid}"><span class="n">{i:02d}</span>{E(short)}</a>'
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

    base = report_dir if report_dir is not None else OUT_DIR
    main_name = "report.html" if report_dir is not None else MAIN_NAME
    out = base / main_name
    title = "AIONA 채팅 품질 평가 — 결과 보고서" if report_dir is not None else "AIONA 채팅 품질 평가"
    doc_name = "결과 보고서" if report_dir is not None else "결과 요약"   # 원문 파일 머리 · 돌아가기 링크
    out.write_text(bind_words(doc(title, f"{D['n']}개 실행 {run_id} — 흐름도 · 결과 · 유형별 · 많이 어긴 기준 · 보류 · 걸린 시간 · 부록",
                                  f'{nav}\n<main class="wrap">\n{s0}\n{body}\n</main>\n{script}')), encoding="utf-8")
    subdir = base / sub
    subdir.mkdir(exist_ok=True)
    # 지난번에 만든 원문 파일 — 예전에는 발표 폴더에 바로 두었고(…_원문1.html), 나눈 수도 바뀔 수 있음
    for old in [*(OUT_DIR.glob(f"ModelGrade_결과요약_{run_id}_원문*.html") if report_dir is None else []), *subdir.glob("*.html")]:
        old.unlink()
    files = ([(f, f"FAIL 원문 {i}", d) for i, (f, d, _b) in enumerate(parts, 1)] + [(f, f"통과 원문 {i}", d) for i, (f, d, _b) in enumerate(pparts, 1)]
             + [(f, f"보류 원문 {i}" if len(hparts) > 1 else "보류 원문", d) for i, (f, d, _b) in enumerate(hparts, 1)]
             + [(e_file, "갈림 목록", f"심판이 갈려 Codex 를 따른 항목 {n_disagree:,}개")])

    def links(cur: str) -> str:
        return (f'<a href="../{E(main_name.replace(" ", "%20"))}#apx">← {E(doc_name)}(부록)으로</a> · ' + " · ".join(
            f"<b>{E(lab)}</b>" if f == cur else f'<a href="{E(f)}">{E(lab)}</a>' for f, lab, _d in files))

    written = [out]
    for f, lab, d in files:
        content = next((b for pf, _d, b in parts + pparts + hparts if pf == f), None)
        if content is None:   # 갈림 목록
            content = f'<p class="rs-basis">Decisions 는 FAIL, Codex 는 PASS·해당 없음이라 Codex 를 따른 항목입니다. 보류가 아니고 숫자도 바꾸지 않습니다(확인용).</p>{e_table}'
        inner = (f'<main class="wrap"><section class="slide" id="s0"><div class="s-head"><span class="s-no">부록 · {E(lab)} / {E(doc_name)} {E(run_id)}</span>'
                 f'<h2>{E(lab)} — {E(d)}</h2><p>{links(f)}</p></div>{jump_index(content)}{fold(content)}</section></main>')
        (subdir / f).write_text(doc(f"{doc_name} {lab}", f"{D['n']}개 실행 {run_id} 부록 — {d}", inner), encoding="utf-8")
        written.append(subdir / f)
    return written, D


def check_against_report(D: Dict[str, Any]) -> List[str]:
    """그 실행의 판정 보고서 1장(완전 통과 · FAIL · 보류)과 맞춰 본다 — 다르면 줄마다 알림."""
    f = judge_report_file(D["run_id"])
    if not f.exists():
        return [f"{f} 없음 — 맞춰 보지 못함"]
    txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", f.read_text(encoding="utf-8"))))
    found = re.findall(r"(\d+) / \d+ 완전 통과 [\d.]+% · FAIL (\d+) · 보류 (\d+)", txt)
    want = [(D["tally"][p]["PASS"], D["tally"][p]["FAIL"], D["tally"][p]["HOLD"]) for p in R.ORDER if p in D["tally"]]
    got = [tuple(int(x) for x in row) for row in found[:len(want)]]
    return [] if got == want else [f"판정 보고서와 다름: 보고서 {got} · 이 페이지 {want}"]


def main(argv: List[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", default=RUN, help="실행 id (참고자료/결과/<시작 시각>/<id>_결과_판정결과.json)")
    ap.add_argument("--report", action="store_true", help="발표 템플릿 대신 그 실행의 결과 폴더에 결과 보고서(report.html, 상세 분석만)를 만듦")
    a = ap.parse_args(argv)
    report_dir = None
    if a.report:
        from tools import result_dirs
        report_dir = result_dirs.find(a.run, RESULTS)
        if report_dir is None:
            raise SystemExit(f"실행 {a.run} 의 결과 폴더(참고자료/결과/<시작 시각>/run.json)가 없습니다")
    written, D = build(a.run, report_dir)
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
    print("판정 보고서와 같음 · 파일마다 미리보기 한도 안" if not problems else "\n".join(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
