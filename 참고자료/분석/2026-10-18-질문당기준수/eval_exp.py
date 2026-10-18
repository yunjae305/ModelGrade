"""통제 실험 수집 실행(서버 run)의 답을 Jev 또는 GPT-6 Luna(high)로 평가 — 턴마다 jsonl 저장, 다시 돌리면 끝난 턴은 건너뜀.
수집이 끝난 질문(대화는 모든 턴)만 평가하므로 수집 중에도 여러 번 돌릴 수 있다.
python -X utf8 eval_exp.py <run_id> <jev|luna> [--providers aiona,chatgpt_web,gemini_web,claude_web] [--workers 12]
결과: exp_eval/<run_id>/<engine>-<provider>.jsonl"""
import json, os, sys, threading, time
from collections import Counter
from pathlib import Path

ROOT = Path(r"C:\Users\winz\Desktop\quality-test-integrated")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
RUN, ENGINE = sys.argv[1], sys.argv[2]
arg = lambda k, d=None: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
WORKERS = int(arg("--workers", 12))
if ENGINE == "luna":
    os.environ.update(SEMANTIC_API_MODEL="gpt-6-luna", SEMANTIC_API_REASONING="high", SEMANTIC_API_MAX_TOOL_CALLS="1",
                      SEMANTIC_API_MAX_OUTPUT_TOKENS="32000", SEMANTIC_API_TIMEOUT="600")
from code_eval import attachment_images
from code_eval.jev_client import resolve_jev_client
from webapp.code_eval_service import CodeEvalService

run = json.loads((ROOT / "data/comparison_runs" / f"{RUN}.json").read_text(encoding="utf-8"))
set_id = (run.get("question_set") or {}).get("set_id")
OUT = Path(__file__).parent / "exp_eval" / RUN
OUT.mkdir(parents=True, exist_ok=True)
engine = {"jev": "jev", "luna": "openai_api"}[ENGINE]
service = CodeEvalService(root=ROOT, save_dir=OUT / "raw", jev_resolver=lambda **kw: resolve_jev_client(engine=engine, env_path=ROOT / ".env"))
ctx = {"answer_tools": "unknown", "collected_at": (run.get("created_at") or "")[:10] or None,
       "attachment_roots": [str(ROOT / "data/question_sets" / str(set_id) / "files"), *map(str, attachment_images.DEFAULT_ROOTS)]}
qid = lambda cid: cid.split("_T")[0]
from webapp.question_generation import flatten
EXPECTED = Counter(qid(r["case_id"]) for r in flatten(json.loads((ROOT / "data/question_sets" / str(set_id) / "questions.json").read_text(encoding="utf-8"))))

for p in arg("--providers", ",".join(run["providers"])).split(","):
    rows = run["datasets"].get(p) or []
    log = OUT / f"{ENGINE}-{p}.jsonl"
    done = {}
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line); done[r["case_id"]] = r
    # 수집이 끝난 질문만 — 질문 세트의 턴 목록과 맞춰 대화는 모든 턴이 들어와 있어야 한다
    byq = {}
    for r in rows:
        if r is not None:
            byq.setdefault(qid(r["case_id"]), []).append(r)
    cases = []
    for q, rs in byq.items():
        if len(rs) < EXPECTED.get(q, 1):
            continue
        for r in rs:
            ok = (r.get("collection") or {}).get("status") == "SUCCESS"
            if ok or p == "aiona":   # 서버와 같음: AIONA 는 상태와 관계없이(빈 답 포함) 평가, 다른 모델은 수집 실패 제외
                cases.append(r)
            else:
                cases.append({**{k: r[k] for k in ("case_id", "conversation_id", "turn") if k in r},
                              "__parse_error__": "응답 수집 실패·중단 또는 미완료로 평가에서 제외했습니다."})
    good = {c for c, r in done.items() if r.get("status") in ("SUCCESS", "SKIPPED")}
    todo = {c["case_id"] for c in cases if c["case_id"] not in good}
    convs = {c.get("conversation_id") for c in cases if c["case_id"] in todo and c.get("conversation_id")}
    batch = [c for c in cases if c["case_id"] in todo or (c.get("conversation_id") and c.get("conversation_id") in convs)]
    print(p, "수집된 턴", len(cases), "| 평가 끝", len(good), "| 이번에", len(batch), time.strftime("%H:%M:%S"), flush=True)
    if not batch:
        continue
    lock = threading.Lock()
    def observe(ev):
        if ev.get("phase") == "JEV" and "record" in ev:
            rec = ev["record"]
            with lock:
                if rec["case_id"] in todo:
                    with log.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                    done[rec["case_id"]] = rec
    service.evaluate({"cases": batch}, observer=observe, semantic_workers=WORKERS, pipeline="legacy", semantic_context=ctx)
    print(p, "끝", dict(Counter(done[c["case_id"]].get("status") for c in cases if c["case_id"] in done)), time.strftime("%H:%M:%S"), flush=True)
