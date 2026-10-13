"""질문 세트 사전 점검 보고서 — ``check_question_set`` 의 결과를 질문·대화 단위로 다시 묶어 단독 HTML 로 만든다 (API 호출 없음).

    python tools/question_set_report.py 질문세트.json                          # results/question-set-check/<이름>.html
    python tools/question_set_report.py 질문세트.json --check-json 점검.json    # check_question_set --json 출력을 그대로 사용
    python tools/question_set_report.py 질문세트.json --json                   # 분석 결과 JSON

적용 규칙·30개 충족 판정은 ``check_question_set`` 그대로 쓴다(기본 ``--pipeline items``). 이 모듈은 같은 결과를
독립 질문·대화 단위로 다시 셀 뿐 판정을 바꾸지 않는다. 무료 진단이며 실제 의미 평가 결과가 아니다.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval.engine import _conversation_of  # noqa: E402
from tools.check_question_set import _TYPE_FIELDS, CRITERIA, TARGET_RATIOS, check_items, load_items  # noqa: E402

REPORT_VERSION = "question-set-report-v1"
DEFAULT_OUT = ROOT / "results" / "question-set-check"

CHECKED = ("JSON 형식·필수 필드(question·answer 문자열)·대화 ID와 turn 형식",
           "대화별 turn 이 1부터 빠짐·중복 없이 이어지는지",
           "중복 질문 — 공백·대소문자만 다른 완전 일치 (의미가 비슷한 질문은 찾지 않음)",
           "CODE 1~3 적용 — 평가와 같은 엔진이 질문(과 이전 턴 지속 지시)에서 읽은 조건, 답변은 비워 둔 채",
           "J1~J6 적용 — 평가와 같은 코드 규칙(의미 평가 방식 표시), 모델 호출 없음",
           "답변 수집 여부 — answer 가 비어 있지 않은 턴 수")
NOT_CHECKED = ("Claude 의미 평가(J1~J6 PASS·FAIL) — 유료 호출, 이 보고서는 판정 결과가 아님",
               "비교 모델(GPT·Claude·Gemini) 답변 수집 — 유료 호출",
               "답변에서 생기는 CODE 조건(1-2·1-3 구조, 3-1 답변 등식, 3-2 선언 개수) — 답변이 있어야 더해짐",
               "J5-3.a 등 답변에서 생기는 의미 항목, J1-3 의 실제 해당 여부(평가 모델이 정함 — 여기서는 적용으로 셈)",
               "롱 컨텍스트 길이(팀원 구조도: 4K~32K 토큰) — 토큰을 세지 않고 질문 글자 수만 표시")


def _cases(items: List[Any]) -> List[Dict[str, Any]]:
    """check_items 가 평가 대상으로 받는 행만 — 같은 case_id 규칙·대화 해석."""
    out = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        question, answer = item.get("question"), item.get("answer", "")
        if not isinstance(question, str) or not question.strip() or not isinstance(answer, str):
            continue
        conversation, problem = _conversation_of(item)
        if problem:
            continue
        out.append({"case_id": str(item.get("case_id") or item.get("id") or f"Q{index + 1:03d}"),
                    "conversation_id": conversation["id"] if conversation else None,
                    "turn": conversation["turn"] if conversation else None,
                    "answered": bool(answer.strip()), "chars": len(question),
                    "type": next((str(item[f]) for f in _TYPE_FIELDS if item.get(f) not in (None, "")), None)})
    return out


def _unit(case: Dict[str, Any]) -> str:
    """질문 단위 — 단일 질문 1개 = 1, 대화 1개 = 1 (설계안 v1.2 '질문을 세는 단위')."""
    return f"대화:{case['conversation_id']}" if case["conversation_id"] is not None else f"질문:{case['case_id']}"


def analyze(report: Dict[str, Any], items: List[Any]) -> Dict[str, Any]:
    """``report`` 는 ``check_items`` 결과(= ``check_question_set --json`` 출력), ``items`` 는 같은 파일의 원본 항목."""
    if report.get("items") != len(items):
        raise ValueError(f"점검 결과의 항목 수({report.get('items')})와 질문 파일의 항목 수({len(items)})가 다름 — 같은 파일인지 확인")
    cases = _cases(items)
    by_id: Dict[str, List[Dict[str, Any]]] = {}
    for case in cases:
        by_id.setdefault(case["case_id"], []).append(case)
    ambiguous = sorted(cid for cid, rows in by_id.items() if len({_unit(r) for r in rows}) > 1)
    minimum = report["min_applicable"]

    units: Dict[str, set] = {c: set() for c in CRITERIA}
    j6_turns: Counter = Counter()
    for row in report["cases"]:
        matches = by_id.get(row["case_id"]) or []
        if not matches or row["case_id"] in ambiguous:
            continue
        unit = _unit(matches[0])
        for c in row["criteria"]:
            units[c].add(unit)
        if "J6" in row["criteria"] and matches[0]["conversation_id"] is not None:
            j6_turns[matches[0]["conversation_id"]] += 1
    criteria = {}
    for c in CRITERIA:
        turns, n_units = report["criteria"][c]["applicable"], len(units[c])
        criteria[c] = {"turns": turns, "enough": report["criteria"][c]["enough"], "turns_short": max(0, minimum - turns),
                       "units": n_units, "units_singles": sum(1 for u in units[c] if u.startswith("질문:")),
                       "units_conversations": sum(1 for u in units[c] if u.startswith("대화:")),
                       "units_enough": n_units >= minimum, "units_short": max(0, minimum - n_units)}

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for case in cases:
        if case["conversation_id"] is not None:
            groups.setdefault(case["conversation_id"], []).append(case)
    conversations = []
    for cid, rows in groups.items():
        turns = [r["turn"] for r in rows]
        counts = Counter(turns)
        conversations.append({"id": cid, "turns": len(turns), "first": min(turns), "last": max(turns),
                              "missing": sorted(set(range(1, max(turns) + 1)) - set(turns)),
                              "duplicated": sorted(t for t, n in counts.items() if n > 1),
                              "j6_turns": j6_turns.get(cid, 0), "answered": sum(r["answered"] for r in rows),
                              "question_chars": sum(r["chars"] for r in rows)})

    answered = sum(c["answered"] for c in cases)
    unit_types: Dict[str, set] = {}
    for case in cases:
        unit_types.setdefault(_unit(case), set()).add(case["type"])
    unit_type = Counter(next(iter(ts - {None})) if len(ts - {None}) == 1 else ("혼합" if ts - {None} else None)
                        for ts in unit_types.values())
    turn_type = Counter(c["type"] for c in cases if c["type"])
    typed_units = sum(n for t, n in unit_type.items() if t)
    names = list(TARGET_RATIOS) + sorted(set(turn_type) - set(TARGET_RATIOS)) + (["혼합"] if unit_type.get("혼합") else [])
    types = [{"name": name, "target": TARGET_RATIOS.get(name), "turns": turn_type.get(name, 0),
              "turn_ratio": round(turn_type.get(name, 0) / sum(turn_type.values()), 3) if turn_type else None,
              "units": unit_type.get(name, 0), "unit_ratio": round(unit_type.get(name, 0) / typed_units, 3) if typed_units else None}
             for name in names]
    multi = [c for c in conversations if c["turns"] >= 2]

    short_turns = [c for c in CRITERIA if not criteria[c]["enough"]]
    short_units_only = [c for c in CRITERIA if criteria[c]["enough"] and not criteria[c]["units_enough"]]
    total = len(cases)
    if report["errors"]:
        status = ("blocked", "업로드 불가 — 형식·대화 순서 오류부터 고쳐야 함")
    elif answered == 0:
        status = ("questions_only", f"AIONA 답변 수집 전 — 평가 실행 불가 (답변 0/{total}턴, 질문 구조·적용 수 점검만 유효)")
    elif answered < total:
        status = ("partial", f"일부 답변 없음 ({answered}/{total}턴) — 빈 답변 턴은 평가 결과로 쓰지 않음")
    elif short_turns:
        status = ("ready_with_gaps", "평가 실행 가능 — 적용 수가 부족한 기준은 통과율 해석에 주의")
    else:
        status = ("ready", "평가 실행 가능")
    readiness = [
        {"name": "형식·대화 순서", "level": "fail" if report["errors"] else "pass",
         "message": f"오류 {len(report['errors'])}건" + (" — 업로드 화면도 같은 오류로 막음" if report["errors"] else "")},
        {"name": "중복 질문", "level": "warn" if report["duplicates"] else "pass", "message": f"{len(report['duplicates'])}건"},
        {"name": "의미 평가 적용 규칙", "level": "pass" if report["pipeline"] == "items" else "warn",
         "message": f"{report['pipeline']}" + ("" if report["pipeline"] == "items" else " — v1.2 항목별 규칙(items)이 아님")},
        {"name": f"기준별 적용 {minimum}개 (현재 판정: 턴)", "level": "warn" if short_turns else "pass",
         "message": "부족: " + ", ".join(short_turns) if short_turns else "모든 기준 충족"},
        {"name": "AIONA 답변", "level": "fail" if answered == 0 else "warn" if answered < total else "pass",
         "message": f"{answered}/{total}턴 수집" + (" — 빈 답변은 평가를 마친 데이터가 아님" if answered < total else "")},
    ]

    decisions = [
        {"title": f"{minimum}개 충족 판정 단위",
         "detail": f"검사기는 적용 '턴' 수로 판정한다. 설계안 v1.2 는 질문을 대화 1개 = 1로 세지만 '기준마다 적용 질문 {minimum}개'의 단위는 정하지 않았다."
                   + (f" 이 세트에서 턴으로는 충족하지만 독립 질문·대화로는 {minimum}개 미만인 기준: {', '.join(short_units_only)}." if short_units_only else "")},
        {"title": "유형 비율 집계 단위",
         "detail": "검사기는 유형 필드를 턴마다 센다. 10턴 대화 하나가 비율에 10번 들어가므로 질문 단위 비율과 다를 수 있다 (보고서는 둘 다 표시)."},
        {"title": "유형과 문맥 속성 분리",
         "detail": "비율 초안의 '멀티턴 대화 10%'·'롱 컨텍스트 5%'는 다른 작업 유형과 겹칠 수 있다(예: 멀티턴 코딩 질문). "
                   "작업 유형(type)과 문맥 속성(단일·멀티턴·롱 컨텍스트)을 따로 기록할지 정해야 한다. 공식 비율은 바꾸지 않았다."},
        {"title": "롱 컨텍스트 판정 기준",
         "detail": "팀원 구조도는 롱 컨텍스트를 '긴 문서 요약·분석, 4K~32K 토큰'으로 적는다. 턴이 많은 대화(J6 대상)를 이 유형으로 셀지, 입력 길이로만 셀지 정해야 한다."},
        {"title": "통과율의 문항 단위",
         "detail": "설계안 v1.2 '정할 것': 판정은 턴마다 하되, 통과율 점수도 대화 1개를 1문항으로 계산할지."},
    ]
    return {"version": REPORT_VERSION, "check": report, "questions": report["questions"], "single_questions": report["single_questions"],
            "conversations": report["conversations"], "turns": total, "conversation_turns": report["conversation_turns"],
            "answered": answered, "ambiguous_case_ids": ambiguous, "criteria": criteria, "short_turns": short_turns,
            "short_units_only": short_units_only, "conversation_rows": conversations,
            "context": {"single": report["single_questions"], "multi_turn": len(multi),
                        "turn_range": [min(c["turns"] for c in multi), max(c["turns"] for c in multi)] if multi else None,
                        "question_chars_range": [min(c["question_chars"] for c in multi), max(c["question_chars"] for c in multi)] if multi else None},
            "types": types if turn_type else None, "untyped_units": unit_type.get(None, 0),
            "status": {"code": status[0], "message": status[1]}, "readiness": readiness, "decisions": decisions,
            "checked": list(CHECKED), "not_checked": list(NOT_CHECKED)}


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _pct(ratio: Optional[float]) -> str:
    return "—" if ratio is None else f"{ratio * 100:.0f}%"


_CSS = """
/* 한 줄 문서형 보고서: 요약 숫자 → 준비 상태 → 기준 표 → 유형·대화 → 결정·범위 (code_eval/report_html 팔레트) */
:root{--bg:#f4f6f8;--paper:#fff;--ink:#182638;--body:#38485b;--muted:#68788b;--line:#dce3eb;--head:#eaf0f5;
--blue:#2b56be;--blue-soft:#eef3ff;--green:#23674d;--green-soft:#eff7f2;--red:#a03c45;--red-soft:#fcf0f1;--amber:#825719;--amber-soft:#fcf6e9;
--font:"Pretendard","Noto Sans CJK KR","Malgun Gothic","Apple SD Gothic Neo",sans-serif;--mono:Consolas,"Noto Sans CJK KR",monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#11161d;--paper:#18202a;--ink:#e8eef5;--body:#c3cdd9;--muted:#8b98a8;--line:#2b3644;--head:#1f2935;
--blue:#8fb0ff;--blue-soft:#1c2840;--green:#7fd1aa;--green-soft:#15291f;--red:#f0a0a8;--red-soft:#33191d;--amber:#e8c07a;--amber-soft:#2e2414;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#11161d;--paper:#18202a;--ink:#e8eef5;--body:#c3cdd9;--muted:#8b98a8;--line:#2b3644;--head:#1f2935;
--blue:#8fb0ff;--blue-soft:#1c2840;--green:#7fd1aa;--green-soft:#15291f;--red:#f0a0a8;--red-soft:#33191d;--amber:#e8c07a;--amber-soft:#2e2414;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--body);font:14px/1.7 var(--font);word-break:keep-all;overflow-wrap:anywhere}
main{max-width:1100px;margin:auto;padding-inline:16px;padding-block:28px 48px;display:grid;gap:14px}
h1,h2{color:var(--ink);letter-spacing:-.03em;margin:0;text-wrap:balance}h1{font-size:26px}h2{font-size:18px;margin-top:14px}
.meta{color:var(--muted);font-size:12px}.note{color:var(--muted);font-size:12.5px;margin:0}
.free{background:var(--amber-soft);color:var(--amber);border-radius:8px;padding:8px 12px;font-weight:600}
.card{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.tile{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:12px 14px;min-width:0}
.tile b{display:block;color:var(--ink);font-size:26px;line-height:1.2;font-variant-numeric:tabular-nums}.tile span{font-size:12px;color:var(--muted)}
.status{border-radius:10px;padding:12px 16px;font-weight:700;color:var(--ink);border-left:5px solid var(--muted);background:var(--paper)}
.status.blocked,.status.questions_only{border-color:var(--red);background:var(--red-soft)}.status.partial,.status.ready_with_gaps{border-color:var(--amber);background:var(--amber-soft)}
.status.ready{border-color:var(--green);background:var(--green-soft)}
.wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:7px 8px;text-align:left;vertical-align:top}thead th{background:var(--head);color:var(--muted);font-size:11.5px;letter-spacing:.02em}
td.n,th.n{text-align:right;white-space:nowrap}
.pill{display:inline-block;font:700 11px var(--mono);padding:1px 7px;border-radius:999px;white-space:nowrap}
.pass{color:var(--green);background:var(--green-soft)}.warn{color:var(--amber);background:var(--amber-soft)}.fail{color:var(--red);background:var(--red-soft)}
.bar{position:relative;height:8px;background:var(--head);border-radius:4px;min-width:90px}.bar i{position:absolute;inset:0 auto 0 0;background:var(--blue);border-radius:4px}
.bar s{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--red)}
ul{margin:4px 0 0;padding-left:20px}li{margin:2px 0}code{font:12px var(--mono);background:var(--head);padding:1px 5px;border-radius:3px}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
"""


def render_html(analysis: Dict[str, Any], source: str, generated_at: Optional[str] = None) -> str:
    a, check = analysis, analysis["check"]
    minimum = check["min_applicable"]
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    level = {"pass": "통과", "warn": "주의", "fail": "막힘"}
    out = ['<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
           # 질문 원문이 들어가도 스크립트·외부 리소스는 실행·로드하지 않는다 (모든 값은 이스케이프)
           "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">",
           f"<title>질문 세트 사전 점검</title><style>{_CSS}</style></head><body><main>",
           f"<header><h1>질문 세트 사전 점검</h1><div class=\"meta\">{_e(source)} · {_e(check['version'])} · 의미 평가 적용 규칙 "
           f"<code>{_e(check['pipeline'])}</code> · {_e(generated_at)}</div></header>",
           '<p class="free">무료 진단 — 모델·평가자 API를 호출하지 않았습니다. 실제 Claude 의미 평가(PASS·FAIL) 결과가 아닙니다.</p>',
           '<section class="tiles">']
    tiles = [(a["questions"], "독립 질문·대화 단위", f"단일 {a['single_questions']} + 대화 {a['conversations']}"),
             (a["turns"], "전체 평가 대상 턴", f"대화 턴 {a['conversation_turns']}"),
             (len(check["errors"]), "형식·순서 오류", "업로드 전 수정 필요" if check["errors"] else "없음"),
             (len(check["duplicates"]), "중복 질문", "완전 일치 기준"),
             (f"{a['answered']}/{a['turns']}", "답변 있는 턴", "수집 전" if a["answered"] == 0 else "")]
    for value, label, sub in tiles:
        out.append(f'<div class="tile"><span>{_e(label)}</span><b>{_e(value)}</b><span>{_e(sub)}</span></div>')
    out.append("</section>")
    out.append(f'<div class="status {_e(a["status"]["code"])}">평가 실행 상태: {_e(a["status"]["message"])}</div>')

    out.append('<section class="card"><h2>평가 실행 준비</h2><div class="wrap"><table><thead><tr><th>항목</th><th>상태</th><th>내용</th></tr></thead><tbody>')
    for row in a["readiness"]:
        out.append(f'<tr><td>{_e(row["name"])}</td><td><span class="pill {_e(row["level"])}">{level[row["level"]]}</span></td><td>{_e(row["message"])}</td></tr>')
    out.append("</tbody></table></div></section>")

    scale = max([minimum] + [r["turns"] for r in a["criteria"].values()])
    out.append(f'<section class="card"><h2>기준별 적용 수</h2><p class="note">현재 판정은 검사기 그대로 <b>적용 턴 {minimum}개 이상</b>입니다. '
               f'독립 질문·대화 단위 수는 같은 결과를 대화 1개 = 1로 다시 센 참고값이며, 공식 판정 단위는 결정 사항입니다. 막대의 빨간 선은 {minimum}턴.</p>'
               '<div class="wrap"><table><thead><tr><th>기준</th><th class="n">적용 턴</th><th></th><th>현재 판정</th><th class="n">부족 턴</th>'
               '<th class="n">적용 질문·대화</th><th>(단일 + 대화)</th><th>단위 기준(참고)</th><th class="n">부족 단위</th></tr></thead><tbody>')
    for cid, row in a["criteria"].items():
        verdict = '<span class="pill pass">충족</span>' if row["enough"] else '<span class="pill fail">부족</span>'
        unit_verdict = '<span class="pill pass">충족</span>' if row["units_enough"] else '<span class="pill warn">미달</span>'
        out.append(f'<tr><td><b>{_e(cid)}</b></td><td class="n">{row["turns"]}</td>'
                   f'<td><div class="bar"><i style="width:{row["turns"] / scale * 100:.1f}%"></i><s style="left:{minimum / scale * 100:.1f}%"></s></div></td>'
                   f'<td>{verdict}</td><td class="n">{row["turns_short"] or "—"}</td><td class="n">{row["units"]}</td>'
                   f'<td>{row["units_singles"]} + {row["units_conversations"]}</td><td>{unit_verdict}</td><td class="n">{row["units_short"] or "—"}</td></tr>')
    out.append("</tbody></table></div>")
    short = [f"{c} {a['criteria'][c]['turns_short']}턴" for c in a["short_turns"]]
    out.append(f'<p class="note">추가 질문이 필요한 기준(현재 판정): {_e(", ".join(short) or "없음")}. '
               f'턴으로는 충족하지만 질문·대화 단위로 {minimum}개 미만: {_e(", ".join(a["short_units_only"]) or "없음")}.</p>')
    if a["ambiguous_case_ids"]:
        out.append(f'<p class="note">같은 ID가 서로 다른 질문·대화에 쓰여 단위 수에서 뺀 ID: {_e(", ".join(a["ambiguous_case_ids"]))}</p>')
    out.append("</section>")

    out.append('<section class="cols"><div class="card"><h2>질문 유형 분포</h2>')
    if a["types"]:
        out.append('<div class="wrap"><table><thead><tr><th>유형</th><th class="n">초안</th><th class="n">턴</th><th class="n">턴 비율</th>'
                   '<th class="n">질문·대화</th><th class="n">단위 비율</th></tr></thead><tbody>')
        for row in a["types"]:
            out.append(f'<tr><td>{_e(row["name"])}</td><td class="n">{_pct(row["target"])}</td><td class="n">{row["turns"]}</td>'
                       f'<td class="n">{_pct(row["turn_ratio"])}</td><td class="n">{row["units"]}</td><td class="n">{_pct(row["unit_ratio"])}</td></tr>')
        out.append("</tbody></table></div>")
        out.append(f'<p class="note">검사기의 유형 비율은 턴 비율입니다. 유형 필드가 없는 질문·대화 {a["untyped_units"]}개.</p>')
    else:
        out.append('<p>유형 필드(type·category·question_type·유형)가 없어 비율 초안과 비교할 수 없습니다.</p>'
                   '<p class="note">초안: ' + _e(" · ".join(f"{k} {v * 100:.0f}%" for k, v in TARGET_RATIOS.items())) + "</p>")
    ctx = a["context"]
    out.append('</div><div class="card"><h2>문맥 속성 (유형과 별도)</h2><ul>'
               f'<li>단일 질문 {ctx["single"]}개 · 멀티턴 대화 {ctx["multi_turn"]}개'
               + (f' (대화당 {ctx["turn_range"][0]}~{ctx["turn_range"][1]}턴)' if ctx["turn_range"] else "") + "</li>"
               + (f'<li>대화별 질문만 누적 글자 수 {ctx["question_chars_range"][0]:,}~{ctx["question_chars_range"][1]:,}자 (답변 제외)</li>' if ctx["question_chars_range"] else "")
               + "<li>롱 컨텍스트(입력 길이) 여부는 판정하지 않음 — 토큰 기준과 답변이 필요</li></ul></div></section>")

    rows = a["conversation_rows"]
    if rows:
        bad = sum(1 for r in rows if r["missing"] or r["duplicated"])
        out.append(f'<section class="card"><h2>대화별 점검</h2><p class="note">대화 {len(rows)}개 · 턴 누락·중복 있는 대화 {bad}개 · J6 적용 턴 합계 '
                   f'{sum(r["j6_turns"] for r in rows)}</p><div class="wrap"><table><thead><tr><th>대화 ID</th><th class="n">턴</th>'
                   '<th class="n">범위</th><th>누락</th><th>중복</th><th class="n">J6 적용</th><th class="n">답변</th><th class="n">질문 글자</th></tr></thead><tbody>')
        for r in rows:
            out.append(f'<tr><td><code>{_e(r["id"])}</code></td><td class="n">{r["turns"]}</td><td class="n">{r["first"]}~{r["last"]}</td>'
                       f'<td>{_e(", ".join(map(str, r["missing"])) or "—")}</td><td>{_e(", ".join(map(str, r["duplicated"])) or "—")}</td>'
                       f'<td class="n">{r["j6_turns"]}</td><td class="n">{r["answered"]}/{r["turns"]}</td><td class="n">{r["question_chars"]:,}</td></tr>')
        out.append("</tbody></table></div></section>")

    out.append(f'<section class="card"><h2>데이터 형식·중복 문제</h2><p class="note">형식·순서 오류 {len(check["errors"])}건 · 중복 질문 {len(check["duplicates"])}건</p>')
    if check["errors"] or check["duplicates"]:
        out.append("<ul>")
        for error in check["errors"]:
            out.append(f'<li>{_e(error["index"] or "-")}번: {_e(error["message"])}</li>')
        for dup in check["duplicates"]:
            out.append(f'<li><code>{_e(dup["case_id"])}</code> = <code>{_e(dup["same_as"])}</code>: {_e(dup["question"])}</li>')
        out.append("</ul>")
    out.append("</section>")

    out.append('<section class="card"><h2>결정이 필요한 사항</h2><ul>')
    for d in a["decisions"]:
        out.append(f'<li><b>{_e(d["title"])}</b> — {_e(d["detail"])}</li>')
    out.append('</ul></section><section class="cols"><div class="card"><h2>점검한 것</h2><ul>')
    out.extend(f"<li>{_e(x)}</li>" for x in a["checked"])
    out.append('</ul></div><div class="card"><h2>실행하지 않은 검증</h2><ul>')
    out.extend(f"<li>{_e(x)}</li>" for x in a["not_checked"])
    out.append(f'</ul></div></section><p class="meta">{_e(a["version"])} · 다시 만들기: '
               f'<code>python tools/question_set_report.py {_e(source)} --pipeline {_e(check["pipeline"])}</code></p></main></body></html>')
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path, help="질문 세트 파일 (JSON·JSONL·MD·TXT)")
    parser.add_argument("--pipeline", choices=("legacy", "items"), default="items", help="의미 평가 적용 규칙 (기본 items, v1.2)")
    parser.add_argument("--check-json", type=Path, help="check_question_set --json 출력 파일 — 주면 다시 점검하지 않음")
    parser.add_argument("--out", type=Path, help=f"HTML 경로 (기본 {DEFAULT_OUT.relative_to(ROOT)}/<이름>.html)")
    parser.add_argument("--json", action="store_true", help="HTML 대신 분석 JSON 을 출력")
    args = parser.parse_args(argv)
    items = load_items(args.path)
    report = (json.loads(args.check_json.read_text(encoding="utf-8-sig")) if args.check_json
              else check_items(items, args.pipeline))
    analysis = analyze(report, items)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.json:
        print(json.dumps(analysis, ensure_ascii=False, indent=1))
    else:
        out = args.out or DEFAULT_OUT / f"{args.path.stem}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_html(analysis, args.path.name), encoding="utf-8")
        print(f"{analysis['status']['message']}\n보고서: {out}")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
