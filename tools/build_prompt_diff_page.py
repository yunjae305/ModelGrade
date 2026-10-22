# -*- coding: utf-8 -*-
"""평가 기준 · 판정 프롬프트가 어떻게 바뀌었는지 원문으로 보여 주는 페이지 (사용자 10-23: "바뀌었다면 어떻게 바뀌었는지 원문으로 표시해서 html에").

    python -X utf8 tools/build_prompt_diff_page.py [--base 16c2fc4]
→ 참고자료/발표/기준·프롬프트 변경 원문.html

기준 시점(--base)은 본 판정(실행 50ca3ccccace, 10-21 23:02 시작)이 쓴 판정 프롬프트의 마지막 커밋(16c2fc4, 10-21 20:54).
① 판정 프롬프트(judge_prompt_v4.txt) git diff 를 줄 단위로(빠진 줄 빨강 · 더한 줄 초록 · 같은 줄 회색), 덩어리마다 무엇을 바꿨는지 한 줄
② 1차 심판(Jev · Decisions) 질문 파일은 항목 설명 줄을 프롬프트에서 그대로 가져가므로 ① 과 같이 바뀜(파일 자체 diff 없음)
③ CODE 검사 · 판정 안전장치 · 질문 세트는 코드 · 자료라 '무엇이 어떻게' 를 말로(변경내역에서 옮김)
"""
from __future__ import annotations

import argparse
import html
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "참고자료" / "발표" / "기준·프롬프트 변경 원문.html"
PROMPT = "참고자료/프롬프트/judge_prompt_v4.txt"
JEV = "참고자료/프롬프트/jev_questions_v4.json"

#: 더한 · 바꾼 줄의 앞머리 → 무엇을 바꿨는지(변경내역 10-22 · 10-23)
LABELS: List[Tuple[str, str]] = [
    ("시점이 있는 사실은 질문이 정한 시점", "J5-1.a 사실 정확성 — 시점 기준을 '질문이 정한 시점(없으면 답변 수집 날짜)'으로, 지어낸 자료 · 날짜는 J5-2 (10-22 아침)"),
    ("J1-2.a 가 FAIL(거절·회피)이면 J1-1.a 와 J4 항목", "상황표 2 — 거절이면 결과물 품질(J4)도 해당 없음(J1-2.a 와 J4 가 같은 거절을 두 번 세던 것, 10-22 밤)"),
    ("UNVERIFIED 는 J5-1.a(사실 확인)에서만", "판정 방법 — '확인 못 함'은 사실 확인에서만, 다른 항목은 PASS · FAIL · 해당 없음 중 하나로 (10-21 밤 보류 점검)"),
    ("[입력]에 '판정할_항목'이 있으면", "판정 방법 — 질문별 목표 기준만 판정, 같은 질문이면 어느 답이든 같은 항목 (10-21 저녁 결정)"),
    ("질문으로 적용 조건이 정해지는 항목은 답변을 보기 전에", "판정 방법 — 적용 조건은 질문으로 정하고 답이 빠뜨렸다는 이유로 없애지 않음, 누락은 누락 담당 항목에서 (10-22 심판 비교)"),
    ("한 오류는 그 오류를 맡은 항목 하나에서만", "판정 방법 — 한 오류는 한 항목에서만 FAIL(기준별 FAIL 수가 부풀지 않게) (10-22 심판 비교)"),
    ("판정할 항목에 없는 기준의 위반은 다른 항목으로 옮겨", "판정 방법 — 판정 목록에 없는 기준의 위반을 다른 항목으로 옮겨 FAIL 하지 않음 (10-22 심판 비교)"),
    ("'빠졌다 · 없다'는 FAIL 은 그 턴 답변 전체", "판정 방법 — 누락 FAIL 은 답 전체(표 · 각주 · 다른 절)를 찾아본 뒤에만 (10-22)"),
    ("사실 확인(J5-1.a)은 답변의 외부 사실 주장을 claim 으로", "사실 확인 — 질문의 자료 · 첨부는 가상이라 검색하지 않고 실제 세상의 사실만 검색 (10-21 밤)"),
    ("HOLD (보류) — 어느 턴이든 UNVERIFIED 항목이 하나라도", "결론 내기 — 보류 우선(보류가 하나라도 있으면 보류, FAIL 이 있어도) (10-21 저녁 결정)"),
    ("일부 항목: 주 요청의 결과물을 냈으면", "J1-1.a — 결과물을 낸 뒤 일부를 '확인할 수 없다'로 남긴 것은 J4-2 · J7 에서 (10-22 심판 비교)"),
    ("일부 항목: 결과물을 낸 뒤 요청 일부만", "J1-2.a — 일부만 '확인할 수 없다'고 밝힌 것은 회피가 아님 (10-22 심판 비교)"),
    ("불필요 아님: 결과물을 쓰는 데 필요한 짧은 안내", "J1-4.a 불필요한 내용 — 짧은 안내 · 자료에 없어 확인이 필요한 사항 · 답이 세운 가정은 불필요 아님 (10-22)"),
    ("단위: 단락 또는 독립된 항목", "J1-4.a — 단위는 단락 · 독립 항목, 요청과 무관한 주제 · 활용법 · 일반론이 한 단락을 이룰 때만 (10-22 심판 비교)"),
    ("지어낸 날짜: 질문에 없는 '오늘 · 현재 날짜'", "J1-4.a — 질문에 없는 오늘 날짜를 가정해 덧붙인 단락은 불필요 (10-22 심판 비교)"),
    ("다른 항목: 질문의 명시 조건을 어긴 내용은 J2-3", "J1-4.a — 명시 조건 위반은 J2-3, 원문에 없는 문장은 J5-2.b 에서 (10-22 심판 비교)"),
    ("반복 아님: 표와 그 표를 풀어 쓴 설명, 결론 · 정리 한 번", "J1-4.b 반복 없음 — 표를 풀어 쓴 설명 · 결론 한 번 · 요구된 구분에 맞춘 재서술은 반복 아님 (10-21 밤 · 10-22)"),
    ("PASS 예: 표로 승인 상한 · 실지급", "J1-4.b — 통과 예시 (10-22)"),
    ("요구된 구성: 질문이 표 · 풀이 · 등식 · 근거 연결 · 검산을 함께 요구했으면", "J1-4.b — 요구된 구성(표 + 풀이 + 검산)에 같은 값이 다시 나오는 것은 반복 아님 (10-22 심판 비교)"),
    ("두괄식: 첫머리에서 답을 한두 문장으로", "J1-4.b — 두괄식(첫 요약 한 번 + 마무리 결론 한 번)은 반복 아님 (10-22 심판 비교)"),
    ("초보자: 질문에 '초보자', '처음 배우는', '입문자'", "J2-1.a 용어 설명 — 초보자 = 처음 배우는 사람 (10-21 · 10-22)"),
    ("설명 정도: 대상이 모를 용어 · 코드 요소마다", "J2-1.a — 용어 · 코드 요소마다 하는 일을 쉬운 말로 풀면 예, 정식 정의까지 요구하지 않음 (10-22 심판 비교)"),
    ("괄호 원어: 한국어 용어 뒤 괄호에 원어를 적고", "J2-1.a — 괄호 원어 + 풀이는 설명한 것(영어라는 이유로 아니오 아님) (10-22 밤, 심판 비교 Gemini Q35)"),
    ("존댓말: 해요체", "J2-2.a 말투 — 존댓말은 높임 세 등급 모두, 섞어도 지킨 것, 반말이 섞일 때만 아니오 (10-21)"),
    ("근거 위치: 답변이 밝힌 근거 위치", "J5-2.a — 근거 위치(부 · 장 · 절 · 쪽 · 표 이름)를 잘못 적은 것은 J5-2.a 에서 (10-22 심판 비교)"),
    ("다른 항목: 긴 입력(J7 적용)에서", "J5-2.a — 긴 입력에서 엉뚱한 부분을 근거로 든 것은 J7 에서 (10-22)"),
    ("다른 항목: 풀이에 쓴 입력 값 자체가 틀린 것", "J3-4.a 풀이 — 풀이에 쓴 입력 값이 틀린 것은 J5-2 에서 (10-22)"),
    ("지어낸 날짜: 원문에 없는 '오늘 날짜", "J5-2.b 원문에 없는 문장 — '오늘 날짜 · 남은 일수' 같은 문장을 더하면 아니오 (10-22)"),
    ("다른 항목: 찾은 내용이 맞고 근거 위치", "J7-1.a 롱컨텍스트 정보 찾기 — 찾은 내용이 맞고 근거 위치만 다르면 J5-2.a 에서 (10-22)"),
    ("다른 항목: 필요한 근거를 모두 찾아 연결했고", "J7-2.a 흩어진 근거 연결 — 근거를 다 연결했고 위치만 다르면 J5-2.a 에서 (10-22)"),
]

CODE_CHANGES = [
    ("합계 · 계산값 읽기(3-3 · 3-1)", "굵은 글씨 · 표 합계 행 · 등식에서 값을 읽고, '만약 · 라면 · 제외하면' 같은 가정 문장의 값은 빼며, 명시한 최종 답 → 원본 전부 등식 → 실제 제외 계산 순으로 고름. 시각 표기(09:30)는 등식이 아님. 표 끝 합계 행은 행 개수에서 뺌.", "50ca3ccccace CODE FAIL 43 → 34(거짓 FAIL 9 제거, 새 FAIL 0) — Codex 검토 4번 승인"),
    ("출처 줄 판별(1-3 인용 ↔ 출처)", "목록형('- [2] 예산 메모') · 굵은 글씨형을 출처로 인정, 조사가 붙은 줄('[2]를 보면') · 설명 줄('[1] 설명: …') · 중복 정의는 출처가 아님.", "Python 3.10 호환 유지"),
    ("이스케이프 · HTML 문자 참조", "'\\-' · '&amp;' 같은 표기는 화면 글자로 바꿔 읽음(표시본은 다시 해석하지 않음).", ""),
    ("Gemini 복사 글의 쪼개진 표 행 잇기", "복사 글에서 표 행 뒤에 빈 줄 + ' |' 로 끊긴 행을 이어 붙임(화면에서는 멀쩡한 표).", "수집 단계(webapp/copy_cleanup)"),
]
GUARD_CHANGES = [
    ("사유와 판정이 반대", "FAIL 인데 사유가 'PASS여야 합니다', PASS 인데 '실제로는 FAIL입니다' — 요소마다 한 번 다시 묻고 남으면 '사유 모순' 보류. 부정 · 양보 · 조건 · 다른 항목 주어 · 괄호 근거는 결론으로 읽지 않음."),
    ("J5-1.a 사실 확인 보정을 주장마다", "'입력 자료의 값 · 외부 사실 아님' 해당 없음, 반박 출처 검사, '맞다' 보정을 주장(펼친 요소)마다 — 한 주장의 사유가 다른 주장의 FAIL 을 지우지 않음."),
    ("J5-3.a 는 늘 판정", "꾸민 출처 · 조회는 답에 있어야 보이는 기준이라 질문별 목표와 관계없이 판정(싱글 · 첨부 · 롱컨텍스트 67문항에서 한 번도 판정하지 않던 것)."),
    ("AIONA '데이터 제공' 줄", "답 끝의 '데이터 제공: MET Norway (…)' 같은 서비스 footer 는 도구 사용 표시라 J5-3.a 로 판정하지 않음(footer 형식과 정확히 같은 한 줄만, 모르는 제공처 · 섞인 인용은 그대로)."),
    ("CODE 가 잰 형식 조건", "LLM FAIL 사유가 CODE 가 그 답에서 실제로 잰 조건(한글만 · 필수 문구 · 불릿 개수 · 각주 연결 …)이면 잰 조건과 결과를 적어 한 번 다시 묻기(한 오류는 한 기준). 판정을 코드가 바꾸지는 않음."),
    ("연결 규칙 오적용", "'J1-1.a FAIL → J4 해당 없음' 같은 규칙을 대상이 아닌 항목(J1-4.b · J2-3.a …)에 써서 낸 해당 없음은 규칙 대상을 짚어 한 번 다시 묻기."),
    ("J1-4 FAIL 다시 묻기", "불필요 · 반복 FAIL 은 정의의 예외(표를 풀어 쓴 설명 · 결론 한 번 · 두괄식 · 요구된 구성)를 짚어 한 번 다시 묻기 — 10-23 기본값으로."),
    ("J4 는 질문으로 정해지는 항목", "목표 기준인 J4 를 1차 심판이 해당 없음이라 해도 Codex 가 다시 봄."),
    ("멀티턴 다시 묻기", "한 턴씩 그 턴까지의 대화만 보내고(뒤 턴 답으로 판정하던 것), 이전 요소 번호를 함께 주며, 한 턴에 막혀 뒤 턴을 못 묻던 것과 상한(3N+8)을 고침."),
]
QSET_CHANGES = [
    ("멀티턴 턴별 확인 목록(새로)", "11개 대화 110턴마다 상황 · 확인할 것(그 턴 시점의 최신 값, 자료에 있는 사실만) · 이어지는 조건 · 흐름상 해당 없는 J6 — 심판 입력의 턴마다 '확인_목록'으로 들어감. 자료 대조 턴의 '남은 예상 지출'은 사용자가 정한 판정('구별해 밝히면 맞음')을 '정한_판정'으로."),
    ("멀티턴 턴별 판정 기준", "턴마다 대화 전체 목표(약 34개)를 다 보던 것 → 늘 보는 8개 + 인식기가 그 턴 요청에서 잡은 항목 + 턴 종류별 설계 항목 + 흐름상 해당하는 J6(턴당 14~21개, 평균 17.6) — 싱글(질문당 8개쯤)과 잣대를 맞춤 (10-23)."),
    ("Q6 · Q10 질문 결함", "변경 통지가 접수 시작을 행사 시작 뒤(Q6 09:35 > 09:30) · 같은 시각(Q10 11:00 = 11:00)으로 옮기고, Q10 은 도착~접수 30분에 확인 8분 + 설치 30분을 끝낼 수 없음 — 다시 수집하지 않고 확인 목록에 사실로 적어 모순 질문(J1-3)으로 판정 (사용자 결정)."),
    ("싱글턴 J1-3", "설계된 모순 · 빠진 자료가 없어 지금처럼 모든 질문에 두고 심판이 해당 없음으로 둠(바꾸지 않음)."),
]

CSS = """
:root { --bg:#f2f4f7; --surface:#fff; --ink:#111b26; --muted:#566272; --line:#d5dce5; --accent:#1e5d96; --accent-soft:#e3ecf6;
  --add:#e1f1e7; --add-ink:#1d7748; --del:#fae3e0; --del-ink:#b3291f; --font-body:"IBM Plex Sans KR","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif; --font-num:"IBM Plex Mono",ui-monospace,Menlo,monospace; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#0e141b; --surface:#16202a; --ink:#e5ebf1; --muted:#98a4b3; --line:#2a3644; --accent:#7fb5e3; --accent-soft:#1a2a3a;
  --add:#15301f; --add-ink:#6fcf97; --del:#3a1c1a; --del-ink:#f08a80; color-scheme:dark; } }
:root[data-theme="dark"] { --bg:#0e141b; --surface:#16202a; --ink:#e5ebf1; --muted:#98a4b3; --line:#2a3644; --accent:#7fb5e3; --accent-soft:#1a2a3a; --add:#15301f; --add-ink:#6fcf97; --del:#3a1c1a; --del-ink:#f08a80; color-scheme:dark; }
* { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--ink); font-family:var(--font-body); font-size:15px; line-height:1.6; }
.wrap { max-width:1120px; margin:0 auto; padding:30px 16px 70px; } h1 { font-size:1.8rem; margin:0 0 6px; } h2 { font-size:1.25rem; margin:34px 0 8px; } h3 { font-size:1rem; margin:0 0 6px; }
.lead { color:var(--muted); margin:0 0 14px; } .num { font-family:var(--font-num); }
nav.toc { display:flex; flex-wrap:wrap; gap:8px; margin:14px 0 8px; } nav.toc a { text-decoration:none; color:var(--accent); background:var(--accent-soft); border-radius:999px; padding:5px 12px; font-size:.9rem; font-weight:600; }
.card { background:var(--surface); border:1px solid var(--line); border-radius:14px; padding:16px 18px; margin:0 0 14px; }
.hunk { border:1px solid var(--line); border-radius:12px; margin:0 0 14px; overflow:hidden; background:var(--surface); }
.hunk .hh { padding:8px 14px; background:var(--accent-soft); color:var(--accent); font-weight:600; font-size:.92rem; }
.hunk .hh small { color:var(--muted); font-weight:400; font-family:var(--font-num); margin-left:8px; }
.hunk pre { margin:0; padding:8px 0; font-family:var(--font-num); font-size:.84rem; line-height:1.55; white-space:pre-wrap; word-break:break-word; }
.hunk pre span { display:block; padding:1px 14px 1px 34px; position:relative; }
.hunk pre span::before { position:absolute; left:12px; color:var(--muted); font-weight:700; }
.hunk pre span.add { background:var(--add); color:var(--add-ink); } .hunk pre span.add::before { content:"+"; }
.hunk pre span.del { background:var(--del); color:var(--del-ink); text-decoration:line-through; text-decoration-thickness:1px; } .hunk pre span.del::before { content:"−"; }
.hunk pre span.ctx { color:var(--muted); } .hunk pre span.ctx::before { content:" "; }
.legend { display:flex; gap:12px; flex-wrap:wrap; font-size:.88rem; color:var(--muted); margin:0 0 14px; } .legend b { display:inline-block; width:14px; height:14px; border-radius:3px; vertical-align:-2px; margin-right:4px; }
.tbl { width:100%; border-collapse:collapse; font-size:.92rem; } .tbl th, .tbl td { text-align:left; vertical-align:top; padding:9px 10px; border-bottom:1px solid var(--line); } .tbl th { color:var(--muted); font-size:.82rem; background:var(--bg); } .tbl tr:last-child td { border-bottom:0; }
.scroll { overflow-x:auto; } .scroll .tbl { min-width:600px; }
@media (max-width:640px) { .scroll .tbl { min-width:0; } .scroll .tbl tr:first-child { display:none; } .scroll .tbl tr { display:block; padding:10px 0; border-bottom:1px solid var(--line); } .scroll .tbl td { display:block; border:0; padding:2px 0; } }
footer { color:var(--muted); font-size:.82rem; border-top:1px solid var(--line); padding-top:14px; margin-top:30px; }
"""


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout


def label_for(lines: List[Tuple[str, str]]) -> str:
    found: List[str] = []
    for kind, text in lines:
        if kind in ("add", "del"):
            for key, label in LABELS:
                if key in text and label not in found:
                    found.append(label)
    return " / ".join(found) if found else "항목 설명 · 규칙 문구 손질"


def render_diff(base: str, path: str) -> Tuple[str, int, int]:
    raw = git("diff", "-U2", f"{base}..HEAD", "--", path)
    hunks: List[List[Tuple[str, str]]] = []
    heads: List[str] = []
    for line in raw.splitlines():
        if line.startswith("@@"):
            hunks.append([])
            heads.append(line)
        elif hunks:
            if line.startswith("+") and not line.startswith("+++"):
                hunks[-1].append(("add", line[1:]))
            elif line.startswith("-") and not line.startswith("---"):
                hunks[-1].append(("del", line[1:]))
            elif line.startswith(" "):
                hunks[-1].append(("ctx", line[1:]))
    adds = sum(1 for h in hunks for k, _ in h if k == "add")
    dels = sum(1 for h in hunks for k, _ in h if k == "del")
    out = []
    for n, (head, h) in enumerate(zip(heads, hunks), 1):
        m = re.search(r"\+(\d+)", head)
        out.append(f'<div class="hunk"><div class="hh">{n}. {html.escape(label_for(h))}<small>지금 파일 {m.group(1) if m else "?"}행 근처</small></div><pre>'
                   + "".join(f'<span class="{k}">{html.escape(t) or " "}</span>' for k, t in h) + "</pre></div>")
    return "".join(out), adds, dels


def build(base: str) -> Path:
    when = git("log", "-1", "--format=%ad", "--date=format:%Y-%m-%d %H:%M", base).strip()
    head = git("rev-parse", "--short", "HEAD").strip()
    body, adds, dels = render_diff(base, PROMPT)
    jev_changed = bool(git("diff", "--stat", f"{base}..HEAD", "--", JEV).strip())
    rows = lambda items: "".join(f"<tr><td><b>{html.escape(a)}</b></td><td>{html.escape(b)}</td><td>{html.escape(c)}</td></tr>" for a, b, c in items)
    rows2 = lambda items: "".join(f"<tr><td><b>{html.escape(a)}</b></td><td>{html.escape(b)}</td></tr>" for a, b in items)
    page = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>기준 · 프롬프트 변경 원문</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+KR:wght@400;500;600;700&display=swap">
<style>{CSS}</style></head><body><div class="wrap">
<p class="lead num" style="margin-bottom:4px">기준 시점: 본 판정(10-21 23:02)이 쓴 프롬프트 {html.escape(base)} ({html.escape(when)}) → 지금 {html.escape(head)}</p>
<h1>평가 기준 · 판정 프롬프트 변경 원문</h1>
<p class="lead">기준 점검(10-21 ~ 10-23)으로 바뀐 것을 원문 그대로 보여 줍니다. 빠진 줄은 빨강, 더한 줄은 초록, 바뀌지 않은 앞뒤 줄은 회색입니다. 기준 72개의 이름 · 번호 · 분류는 바뀌지 않았고, 바뀐 것은 항목 정의 아래의 '언제 예 · 아니오인가' 설명 줄과 판정 방법 · 상황표 2 · 결론 규칙입니다.</p>
<nav class="toc"><a href="#prompt">1 판정 프롬프트</a><a href="#first">2 1차 심판</a><a href="#code">3 CODE 검사</a><a href="#guard">4 판정 안전장치</a><a href="#qset">5 질문 세트</a></nav>
<h2 id="prompt">1. 판정 프롬프트 (judge_prompt_v4.txt) — 더한 줄 {adds} · 빠진 줄 {dels}</h2>
<div class="legend"><span><b style="background:var(--add)"></b>더한 줄</span><span><b style="background:var(--del)"></b>빠진 줄(지금 파일에 없음)</span><span><b style="background:var(--bg);border:1px solid var(--line)"></b>앞뒤 같은 줄</span></div>
{body}
<h2 id="first">2. 1차 심판 (Jev · Decisions)</h2>
<div class="card"><p>1차 심판 질문 파일(jev_questions_v4.json)은 {"바뀌었습니다" if jev_changed else "파일 자체는 바뀌지 않았습니다"}. 항목마다 보내는 설명 줄은 위 판정 프롬프트의 '점검 항목 정의'에서 그대로 가져가므로(10-22: '항목 설명 줄은 1차 심판에도 그대로 들어감'), 1장의 항목 설명 변경이 1차 심판에도 같이 적용됩니다. 1차 심판은 입력 3만 2천 토큰 이하 · 첨부 없음이면 Jev, 그 밖은 Decisions 가 맡고, 걸린 항목만 Codex 가 확정합니다.</p></div>
<h2 id="code">3. CODE 검사 — 규칙 문구는 그대로, 거짓 FAIL 을 내던 판별을 고침</h2>
<div class="card"><div class="scroll"><table class="tbl"><tr><th style="width:220px">검사</th><th>어떻게 바뀌었나</th><th style="width:230px">효과 · 확인</th></tr>{rows(CODE_CHANGES)}</table></div></div>
<h2 id="guard">4. 판정 안전장치 (코드가 심판 출력을 다시 묻거나 보류로 돌리는 규칙)</h2>
<div class="card"><div class="scroll"><table class="tbl"><tr><th style="width:220px">장치</th><th>규칙</th></tr>{rows2(GUARD_CHANGES)}</table></div></div>
<h2 id="qset">5. 질문 세트 (질문 원문은 그대로, 판정에 주는 정보가 바뀜)</h2>
<div class="card"><div class="scroll"><table class="tbl"><tr><th style="width:220px">무엇</th><th>어떻게</th></tr>{rows2(QSET_CHANGES)}</table></div></div>
<footer>원본: git diff {html.escape(base)}..{html.escape(head)} — 참고자료/프롬프트/judge_prompt_v4.txt · 자세한 날짜별 내역은 참고자료/변경내역/2026-10-21 · 10-22-변경내역.md</footer>
</div></body></html>
"""
    OUT.write_text(page, encoding="utf-8")
    return OUT


def main(argv: List[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="16c2fc4", help="기준 커밋(본 판정이 쓴 프롬프트의 마지막 커밋)")
    args = ap.parse_args(argv)
    out = build(args.base)
    print(f"저장: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
