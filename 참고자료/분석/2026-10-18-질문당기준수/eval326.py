"""326 질문 세트(10-14 수집 답) 의미 평가 — Jev 또는 GPT-6 Luna(high). 한 건 끝날 때마다 jsonl 에 저장하고, 다시 돌리면 끝난 것은 건너뛴다.
python -X utf8 eval326.py <jev|luna> --providers aiona,gpt,gemini [--workers 15]
결과: eval326/<engine>-<provider>.jsonl (턴마다 한 줄) · 끝나면 eval326/<engine>-<provider>.json (서버 평가 결과와 같은 batch 형식)"""
import json, os, sys, threading, time
from collections import Counter
from pathlib import Path

ROOT = Path(r"C:\Users\winz\Desktop\quality-test-integrated")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
ENGINE = sys.argv[1]
arg = lambda k, d=None: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
WORKERS = int(arg("--workers", 15))
if ENGINE == "luna":   # GPT-6 Luna · 추론 high · 질문당 웹 검색 1회 · 출력 32K · 시간 600초 (300 세트 재평가에서 정한 값)
    os.environ.update(SEMANTIC_API_MODEL="gpt-6-luna", SEMANTIC_API_REASONING="high", SEMANTIC_API_MAX_TOOL_CALLS="1",
                      SEMANTIC_API_MAX_OUTPUT_TOKENS="32000", SEMANTIC_API_TIMEOUT="600")
from code_eval import attachment_images
from code_eval.jev_client import resolve_jev_client
from webapp.code_eval_service import CodeEvalService
from webapp.question_generation import flatten

D = ROOT / "참고자료/수집결과/2026-10-14-질문세트326-세부기준"
OUT = Path(__file__).parent / "eval326"
OUT.mkdir(exist_ok=True)
rows = flatten(json.loads((D / "questions.json").read_text(encoding="utf-8")))
answers = {it["case_id"]: it["answers"] for it in json.loads((D / "answers.json").read_text(encoding="utf-8"))["items"]}
engine = {"jev": "jev", "luna": "openai_api"}[ENGINE]
resolver = lambda **kw: resolve_jev_client(engine=engine, env_path=ROOT / ".env")
service = CodeEvalService(root=ROOT, save_dir=OUT / "raw", jev_resolver=resolver)
ctx = {"answer_tools": "unknown", "collected_at": "2026-10-14",
       "attachment_roots": [str(D / "files"), *map(str, attachment_images.DEFAULT_ROOTS)]}


def cases_for(p):
    out = []
    for r in rows:
        a = (answers.get(r["case_id"]) or {}).get(p) or {}
        c = {k: r[k] for k in ("case_id", "question", "reference", "conversation_id", "turn", "attachments") if r.get(k) not in (None, "")}
        if a.get("status") == "SUCCESS" or p == "aiona":
            c["answer"] = a.get("answer") or ""
        else:
            c = {**{k: c[k] for k in ("case_id", "conversation_id", "turn") if k in c},
                 "__parse_error__": "응답 수집 실패·중단 또는 미완료로 평가에서 제외했습니다."}
        out.append(c)
    return out


for p in arg("--providers", "aiona,gpt,gemini").split(","):
    log = OUT / f"{ENGINE}-{p}.jsonl"
    done = {}
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                done[rec["case_id"]] = rec
    good = {cid for cid, r in done.items() if r.get("status") in ("SUCCESS", "SKIPPED")}
    cases = cases_for(p)
    todo_ids = {c["case_id"] for c in cases if c["case_id"] not in good}
    convs = {c.get("conversation_id") for c in cases if c["case_id"] in todo_ids and c.get("conversation_id")}
    batch_cases = [c for c in cases if c["case_id"] in todo_ids or (c.get("conversation_id") and c.get("conversation_id") in convs)]
    print(p, "전체", len(cases), "| 끝남", len(good), "| 이번에 평가", len(batch_cases), "턴", time.strftime("%H:%M:%S"), flush=True)
    if batch_cases:
        lock, n = threading.Lock(), [0]
        def observe(ev, batch_cases=batch_cases):
            if ev.get("phase") == "JEV" and "record" in ev:
                rec = ev["record"]
                with lock:
                    if rec["case_id"] in todo_ids:   # 대화 맥락 때문에 함께 넣은 이미 끝난 턴은 기록을 바꾸지 않는다
                        with log.open("a", encoding="utf-8") as f:
                            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                        done[rec["case_id"]] = rec
                    n[0] += 1
                    if n[0] % 20 == 0:
                        print(f"  {p} {n[0]}/{len(batch_cases)} {time.strftime('%H:%M:%S')}", flush=True)
        batch = service.evaluate({"cases": batch_cases}, observer=observe, semantic_workers=WORKERS,
                                 pipeline="legacy", semantic_context=ctx)
        # 최종 batch: CODE 결과는 이번 batch 의 것(같은 엔진), 의미 평가 기록은 jsonl 누적본으로
        full = service.evaluate({"cases": cases}, jev_resolver=lambda **kw: (None, None, "CODE 만"), pipeline="legacy",
                                semantic_context=ctx) if len(batch_cases) < len(cases) else batch
        full["jev_eval"] = {**(batch.get("jev_eval") or {}), "records": [done.get(c["case_id"]) or {"case_id": c["case_id"], "status": "MISSING"} for c in cases]}
        (OUT / f"{ENGINE}-{p}.json").write_text(json.dumps(full, ensure_ascii=False, default=str), encoding="utf-8")
    st = Counter((done.get(c["case_id"]) or {}).get("status", "MISSING") for c in cases)
    print(p, "끝", dict(st), time.strftime("%H:%M:%S"), flush=True)
