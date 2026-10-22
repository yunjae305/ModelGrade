"""ego lite(ego-browser) 로 ChatGPT·Claude 웹에서 답을 받아 보는 시험 수집기 — 수집 방식 속도 비교용 (ego-lite-bench-v1, 2026-10-20).

    python -X utf8 tools/ego_lite_collect.py --site chatgpt --only 22,58,69 --out <결과.json>
    python -X utf8 tools/ego_lite_collect.py --site claude  --only 22,58,69 --out <결과.json>
    python -X utf8 tools/ego_lite_collect.py --site chatgpt --check          # 로그인·입력칸만 확인

- ego lite 데스크톱 앱(비공개, Citro Labs)이 설치돼 있어야 한다. 이 스크립트는 질문마다 Node 스크립트(.mjs)를 써서
  `ego-browser.exe nodejs -e "await import('file:///…')"` 로 돌린다 — ego-browser 가 PowerShell 에서 권하는 방식(따옴표 깨짐 방지).
- 조작 순서·대기 규칙은 webapp/cdp_chat_web.py 의 ChatGptChat·ClaudeChat 과 같게 맞췄다(새 채팅 → 모델 설정 → 입력 → 전송 확인 →
  정지 버튼이 사라지고 글이 1.5초×2 번 같으면 끝 → 복사 버튼 원문). 그래야 시간 비교가 공정하다.
- 로그인은 사람이 ego lite 창에서 미리. 봇 확인·한도 화면이 보이면 멈춘다 — 우회하지 않는다.
- 결과 JSON: 질문마다 단계별 시간(ms) — new_chat(새 채팅 열림) · model(모델 설정) · sent(전송 확인) · answer(답 끝) · copy(복사) · total,
  그리고 프로세스 시작~끝 벽시계(wall_ms: Node 띄우는 비용 포함), 답 글자 수, 모델 표시, 오류.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_SET = ROOT / "참고자료" / "질문세트_78"
SPACE = "modelgrade-bench"   # ego lite 작업 공간 이름 — 같은 이름이면 이어 쓴다

# 사이트별 값 — webapp/cdp_chat_web.py 의 ChatGptChat·ClaudeChat 상수와 같다
SITES: Dict[str, Dict[str, Any]] = {
    "chatgpt": {
        "label": "ChatGPT 웹 (ego lite) · GPT-6 · Medium",
        "url": "https://chatgpt.com/",
        "textbox": "ChatGPT",
        "answer": "[data-turn-key]:has(h4[data-conversation-role='assistant']), [data-message-role='assistant'], [data-message-author-role='assistant']",
        "answer_heading": "ChatGPT 답변:",
        "user": "[data-user-message-bubble], [data-message-role='user'], [data-message-author-role='user']",
        "stop": ["중지", "Stop"],
        "copy": ["복사", "Copy"],
        "model": "chatgpt",
    },
    "claude": {
        "label": "Claude 웹 (ego lite) · Sonnet 5.5 · 노력 중간",
        "url": "https://claude.ai/new",
        "textbox": "Claude에게 프롬프트",
        "answer": "[data-testid='assistant-message']",
        "answer_heading": "",
        "user": "[data-testid='user-message']",
        "stop": ["중지", "Stop"],
        "copy": ["복사", "Copy"],
        "model": "claude",
    },
}

BLOCKED = ["보안 확인 수행 중", "Verify you are human", "사용자가 봇이 아닌지 확인", "Just a moment", "너무 많은 요청", "Too many requests",
           "사용량 한도", "usage limit", "한도에 도달", "메시지 한도", "limit reached"]


def ego_exe() -> Path:
    """ego-browser 실행 파일 — PATH, 사용자 설치 폴더, 프로그램 폴더 순."""
    import shutil
    found = shutil.which("ego-browser")
    if found:
        return Path(found)
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    cands = [home / ".local" / "share" / "ego" / "active_version_dir" / "ego-browser.exe"]
    cands += sorted((Path(r"C:\Program Files\Citro Labs\ego lite\Application")).glob("*/ego-browser.exe"), reverse=True)
    for c in cands:
        if c.is_file():
            return c
    raise SystemExit("ego-browser 를 찾지 못했습니다 — ego lite 를 설치하고 온보딩을 마치세요 (https://lite.ego.app/).")


# --- Node 스크립트 ------------------------------------------------------------------------------------------------
# 페이지 안에서 돌 JS 는 문자열로 넘긴다(page.evaluate 콜백은 바깥 변수를 못 본다).
COPY_JS = (ROOT / "webapp" / "factchat_web.py").read_text(encoding="utf-8").split('COPY_CAPTURE_JS = r"""', 1)[1].split('"""', 1)[0]

ROUND_JS = r'''
const P = __PARAMS__;
const COPY_JS = __COPY_JS__;
const t0 = Date.now(); const marks = {}; const mark = (k) => { marks[k] = Date.now() - t0; };
const out = { case_id: P.case_id, site: P.site, status: "ERROR", error: null, answer: "", capture: "none", model: "", marks };
const sleep = (ms) => page.waitForTimeout(ms);
const textboxSel = `loc=role:textbox[name*="${P.textbox}"]`;

// 페이지 안 도우미 — 문자열 식으로 평가 (cdp_chat_web 과 같은 규칙)
const JS_COUNT = (sel) => `document.querySelectorAll(${JSON.stringify(sel)}).length`;
const JS_LAST_TEXT = `(() => { const a = document.querySelectorAll(${JSON.stringify(P.answer)}); if (!a.length) return "";
  let t = (a[a.length - 1].innerText || "").trim(); const h = ${JSON.stringify(P.answer_heading)};
  if (h && t.includes(h)) t = t.split(h).pop().trim(); return t; })()`;
const JS_STREAMING = `(() => { const re = new RegExp(${JSON.stringify(P.stop.join("|"))});
  const btns = [...document.querySelectorAll("button")].filter(b => re.test((b.getAttribute("aria-label") || "") + " " + (b.textContent || "")));
  if (btns.slice(0, 4).some(b => b.offsetParent !== null)) return true;
  const a = document.querySelectorAll(${JSON.stringify(P.answer)}); const last = a[a.length - 1];
  return !!(last && last.getAttribute("data-is-streaming") === "true"); })()`;
const JS_BLOCKED = `(() => { let t = document.body.innerText; const sel = ${JSON.stringify([P.answer, P.user].filter(Boolean).join(", "))};
  for (const e of document.querySelectorAll(sel)) { const x = (e.innerText || "").trim(); if (x) t = t.split(x).join(" "); }
  t = t.slice(0, 6000).toLowerCase(); const hit = ${JSON.stringify(P.blocked)}.find(p => t.includes(p.toLowerCase())); return hit || ""; })()`;
const JS_COPY = `(async () => { const names = ${JSON.stringify(P.copy)}; const before = (window.__mgCopied || []).length;
  const a = document.querySelectorAll(${JSON.stringify(P.answer)}); const scopes = [a[a.length - 1], document].filter(Boolean);
  const nameOf = (b) => ((b.getAttribute("aria-label") || b.getAttribute("title") || b.textContent || "").trim());
  for (const scope of scopes) { for (const n of names) {
    const btns = [...scope.querySelectorAll("button")].filter(b => nameOf(b) === n); if (!btns.length) continue;
    const b = btns[btns.length - 1]; try { b.scrollIntoView({ block: "center" }); } catch (e) {}
    try { if (scope !== document) scope.dispatchEvent(new MouseEvent("mouseover", { bubbles: true })); } catch (e) {}
    b.click(); await new Promise(r => setTimeout(r, 600));
    const c = window.__mgCopied || []; if (c.length > before && typeof c[c.length - 1] === "string" && c[c.length - 1].trim()) return c[c.length - 1]; } }
  return ""; })()`;
const ev = (expr) => page.evaluate(expr);

async function clickRole(role, name, { exact = true, timeout = 5000 } = {}) {
  const sel = exact ? `loc=role:${role}[name='${name.replace(/'/g, "\\'")}']` : `loc=role:${role}[name*="${name}"]`;
  await page.click(sel, { timeout });
}
async function clickMenuItem(name, exact) {
  let lastErr = null;
  for (const role of ["menuitemradio", "menuitem", "menuitemcheckbox", "option"]) {
    try { await clickRole(role, name, { exact, timeout: 2500 }); await sleep(800); return; } catch (e) { lastErr = e; }
  }
  throw new Error(`메뉴 항목 '${name}' 없음: ${lastErr && lastErr.message}`);
}
async function escape(n = 1) { for (let i = 0; i < n; i++) { await page.keyboard.press("Escape"); await sleep(300); } }

async function applyModelChatgpt() {
  try { await clickRole("button", "Chat", { timeout: 2000 }); await sleep(500); } catch (e) {}
  try { await clickRole("button", "ChatGPT 모델 선택", { timeout: 15000 }); await sleep(700);
        await clickMenuItem("모델 선택", false); await clickMenuItem("GPT-6", true); } finally { await escape(1); }
  let power = "?";
  try { await clickRole("button", "ChatGPT 모델 선택"); await sleep(700); await clickMenuItem("파워", false); await sleep(900);
        const text = await ev("document.body.innerText");
        power = ["Medium", "High", "Light", "Max", "Ultra", "Extra High", "최소한", "없음"].find(lv => text.includes(lv)) || "?"; } catch (e) {}
  finally { await escape(2); }
  if (power !== "Medium") throw new Error(`ChatGPT 추론 수준이 Medium 이 아닙니다(${power})`);
  return `GPT-6 · ${power} (ego lite 웹)`;
}
async function applyModelClaude() {
  const btnText = async () => (await ev(`(() => { const b = [...document.querySelectorAll("button")].find(x => /^모델: /.test(x.getAttribute("aria-label") || x.textContent.trim())); return b ? (b.getAttribute("aria-label") || b.innerText).replace(/\\s+/g, " ").trim() : ""; })()`));
  let current = await btnText();
  if (current.includes("Sonnet 5.5") && current.includes("중간")) return `${current} (ego lite 웹)`;
  if (!current.includes("Sonnet 5.5")) { try { await clickRole("button", "모델: ", { exact: false, timeout: 15000 }); await sleep(700); await clickMenuItem("Sonnet 5.5", false); } finally { await escape(1); } }
  try { await clickRole("button", "모델: ", { exact: false }); await sleep(700); await clickMenuItem("노력", false); await clickMenuItem("중간", false); } finally { await escape(1); }
  current = await btnText();
  return `${current || "Sonnet 5.5 중간"} (ego lite 웹)`;
}

// 작업 공간이 사람에게 넘어가 있으면(로그인 등으로 사람이 창을 만짐) 되찾는다 — 사용자가 진행을 지시한 뒤에만 돌린다
try { const mine = (await listTaskSpaces()).find(s => s.name === P.space);
      if (mine && mine.ownership === "agentDelegatedToUser") { await takeOverTaskSpace(mine.id); out.took_over = true; } } catch (e) { out.take_over_error = String(e.message || e).slice(0, 120); }
const task = await taskSpace(P.space);
const page = task.page("p1");
const JS_COPY_COUNT = (inLast) => `(() => { const names = ${JSON.stringify(P.copy)}; const a = document.querySelectorAll(${JSON.stringify(P.answer)});
  const scope = ${inLast ? "a[a.length - 1]" : "document"}; if (!scope) return 0;
  return [...scope.querySelectorAll("button")].filter(b => names.includes((b.getAttribute("aria-label") || b.getAttribute("title") || b.textContent || "").trim())).length; })()`;
try {
  await page.goto(P.url);
  await sleep(3000);
  await ev(COPY_JS);
  const blocked0 = await ev(JS_BLOCKED); if (blocked0) throw new Error(`화면이 막혀 있습니다(한도 또는 봇 확인: '${blocked0}')`);
  try { await page.waitForSelector(textboxSel, { state: "visible", timeout: 20000 }); } catch (e) {}
  await sleep(1500);
  mark("new_chat");
  if (P.check) { out.status = "SUCCESS"; out.url = await page.url(); out.title = await page.title(); out.textbox = !!(await ev(`document.querySelector("[role=textbox], textarea, [contenteditable=true]") !== null`)); }
  else {
    try { out.model = P.model === "chatgpt" ? await applyModelChatgpt() : await applyModelClaude(); }
    catch (e) { out.model = `설정 확인 못 함: ${String(e.message || e).slice(0, 80)}`; }
    mark("model");
    const before = await ev(JS_COUNT(P.answer)); const askedBefore = await ev(JS_COUNT(P.user)); const textBefore = await ev(JS_LAST_TEXT);
    const copyBefore = await ev(JS_COPY_COUNT(false));
    await escape(1);
    await page.click(textboxSel, { timeout: 15000 });
    await page.keyboard.insertText(P.question);
    await sleep(600);
    await page.keyboard.press("Enter");
    let sentOk = false; const sendDeadline = Date.now() + 120000;
    while (Date.now() < sendDeadline) { await sleep(500);
      if ((await ev(JS_COUNT(P.user))) > askedBefore || (await ev(JS_COUNT(P.answer))) > before || (await ev(JS_STREAMING))) { sentOk = true; break; } }
    if (!sentOk) { const b = await ev(JS_BLOCKED); throw new Error(b ? `화면이 막혀 있습니다('${b}')` : "전송 뒤에도 답변이 시작되지 않음"); }
    mark("sent");
    if (P.asks_empty) {   // 빈 출력 요구: 15초 안에 복사 버튼이 안 뜨면 바로 보류 (사용자 규칙 10-20 밤) — cdp_chat_web 과 같은 규칙
      const emptyDeadline = Date.now() + P.empty_wait_ms; let ready = false;
      while (Date.now() < emptyDeadline) { await sleep(500);
        if ((await ev(JS_COUNT(P.answer))) > before && ((await ev(JS_COPY_COUNT(true))) > 0 || (await ev(JS_COPY_COUNT(false))) > copyBefore)) { ready = true; break; } }
      if (!ready) { mark("answer"); mark("copy"); out.status = "COPY_FAILED"; out.capture = "none";
        out.error = `빈 출력 요구 — ${P.empty_wait_ms / 1000}초 안에 복사 버튼이 뜨지 않음 → 보류 (사용자 규칙 10-20 밤)`; throw new Error(out.error); }
    }
    let last = null, stable = 0, done = false; const deadline = Date.now() + P.answer_timeout_ms;
    while (Date.now() < deadline) { await sleep(1500);
      if (await ev(JS_STREAMING)) { stable = 0; continue; }
      const cur = await ev(JS_LAST_TEXT); stable = cur === last ? stable + 1 : 0; last = cur;
      if (stable >= 2 && ((await ev(JS_COUNT(P.answer))) > before || (cur && cur !== textBefore))) { done = true; break; }
      if (stable >= 60) { done = true; break; } }
    mark("answer");
    let answer = last || "", capture = "text";
    if (done) { const copied = await ev(JS_COPY); if (copied && copied.trim()) { answer = copied; capture = "copy-button"; } else if (!answer.trim()) { capture = "empty"; } }
    mark("copy");
    if (done) { const b = await ev(JS_BLOCKED); if (b) throw new Error(`화면이 막혀 있습니다('${b}')`); }
    out.status = done ? "SUCCESS" : "INCOMPLETE"; out.answer = done ? answer : ""; out.capture = capture;
    if (!done) out.error = "답변이 제한 시간 안에 끝나지 않았습니다.";
  }
} catch (e) { if (out.status !== "COPY_FAILED") out.error = String(e && e.message || e).slice(0, 300); }
mark("total");
console.log("@@RESULT " + JSON.stringify(out));
'''


def run_round(exe: Path, mjs_dir: Path, name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """질문 하나 = Node 프로세스 하나. .mjs 로 쓰고 -e import 로 돌린다(PowerShell 따옴표 문제 회피). 돌려주는 값에 wall_ms 를 더한다."""
    mjs = mjs_dir / f"{name}.mjs"
    code = ROUND_JS.replace("__PARAMS__", json.dumps(params, ensure_ascii=False)).replace("__COPY_JS__", json.dumps(COPY_JS))
    mjs.write_text(code, encoding="utf-8")
    url = "file:///" + str(mjs.resolve()).replace("\\", "/")
    started = time.monotonic()
    proc = subprocess.run([str(exe), "nodejs", "-e", f"await import('{url}')"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=params.get("answer_timeout_ms", 600_000) / 1000 + 300)
    wall_ms = round((time.monotonic() - started) * 1000)
    # ego-browser 는 console.log 를 stderr 로도 낸다(2026-10-20 확인) — 둘 다 본다
    line = next((l for l in (proc.stdout + "\n" + proc.stderr).splitlines() if l.startswith("@@RESULT ")), None)
    if line is None:
        return {"case_id": params.get("case_id"), "status": "ERROR", "error": f"ego-browser 가 결과를 내지 않음 (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-400:]}",
                "wall_ms": wall_ms, "marks": {}}
    res = json.loads(line[len("@@RESULT "):])
    res["wall_ms"] = wall_ms
    res["process_overhead_ms"] = wall_ms - int(res.get("marks", {}).get("total") or 0)
    if proc.stderr.strip():
        res["stderr"] = proc.stderr.strip()[-300:]
    return res


def load_questions(set_dir: Path, only: List[int]) -> List[Dict[str, Any]]:
    groups = json.loads((set_dir / "questions.json").read_text(encoding="utf-8"))
    rows = []
    for g in groups:
        if int(g["id"]) not in only:
            continue
        if len(g["turns"]) != 1:
            raise SystemExit(f"Q{g['id']} 는 {len(g['turns'])}턴 대화 — 이 시험 수집기는 단일 질문만 받습니다.")
        t = g["turns"][0]
        if t.get("attachments") or t.get("reference_file"):
            raise SystemExit(f"Q{g['id']} 는 첨부·참조 문서가 있음 — 이 시험 수집기는 글만 보냅니다.")
        rows.append({"case_id": f"Q{g['id']}_T1", "qid": int(g["id"]), "question": t["question"]})
    return sorted(rows, key=lambda r: only.index(r["qid"]))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--site", required=True, choices=sorted(SITES))
    ap.add_argument("--only", default="", help="질문 번호, 쉼표로")
    ap.add_argument("--set", default=str(DEFAULT_SET))
    ap.add_argument("--out", help="결과 JSON 경로")
    ap.add_argument("--check", action="store_true", help="로그인·입력칸만 확인")
    ap.add_argument("--answer-timeout", type=int, default=0, help="답 대기 제한(초), 0 = 제한 없음(10시간)")
    ap.add_argument("--work-dir", help=".mjs 를 쓸 폴더 (기본: 임시 폴더)")
    args = ap.parse_args(argv)
    exe = ego_exe()
    site = SITES[args.site]
    import tempfile
    work = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="mg-ego-"))
    work.mkdir(parents=True, exist_ok=True)
    base = {"site": args.site, "space": SPACE, "url": site["url"], "textbox": site["textbox"], "answer": site["answer"], "answer_heading": site["answer_heading"],
            "user": site["user"], "stop": site["stop"], "copy": site["copy"], "model": site["model"], "blocked": BLOCKED,
            "answer_timeout_ms": (args.answer_timeout or 36_000) * 1000, "check": args.check}
    print(f"ego-browser: {exe}", flush=True)
    if args.check:
        res = run_round(exe, work, f"check-{args.site}", {**base, "case_id": "check", "question": ""})
        print(json.dumps({k: v for k, v in res.items() if k != "answer"}, ensure_ascii=False, indent=1))
        return 0 if res.get("status") == "SUCCESS" else 1
    only = [int(x) for x in args.only.split(",") if x.strip()]
    if not only:
        raise SystemExit("--only 로 질문 번호를 주세요.")
    rows = load_questions(Path(args.set), only)
    results = []
    t_all = time.monotonic()
    print(f"{args.site} · {site['label']}: 질문 {len(rows)}개 {[r['qid'] for r in rows]}", flush=True)
    for r in rows:
        print(f"▶ {r['case_id']}", flush=True)
        from webapp.run_summary import asks_empty_output
        res = run_round(exe, work, f"{args.site}-{r['case_id']}", {**base, "case_id": r["case_id"], "question": r["question"],
                                                                   "asks_empty": asks_empty_output(None, r["question"]), "empty_wait_ms": 15_000})
        res["question"] = r["question"]
        res["collected_at"] = datetime.now().isoformat(timespec="seconds")
        results.append(res)
        m = res.get("marks") or {}
        print(f"  {r['case_id']}: {res.get('status')} {len(res.get('answer') or '')}자 total {m.get('total', 0) // 1000}초 (새채팅 {m.get('new_chat', 0) // 1000}·모델 {(m.get('model', 0) - m.get('new_chat', 0)) // 1000}"
              f"·전송 {(m.get('sent', 0) - m.get('model', 0)) // 1000}·답 {(m.get('answer', 0) - m.get('sent', 0)) // 1000}·복사 {(m.get('copy', 0) - m.get('answer', 0)) // 1000}) "
              f"벽시계 {res.get('wall_ms', 0) // 1000}초 · {res.get('capture')} · {res.get('model')}" + (f" — {res.get('error')}" if res.get("error") else ""), flush=True)
    summary = {"method": "ego-lite-bench-v1", "site": args.site, "label": site["label"], "ego_browser": str(exe), "set": str(args.set),
               "wall_total_ms": round((time.monotonic() - t_all) * 1000), "results": results}
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"저장: {args.out}", flush=True)
    return 0 if all(r.get("status") == "SUCCESS" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
