"""판정 규칙 시험 — 시험 묶음(갈린 건 + 대조군)만 Jev 또는 GPT-6 Luna 로 다시 판정한다. 끝난 건은 건너뜀.
python -X utf8 rules_test.py <run_id> <jev|luna> <tag>   결과: exp_eval/rules_<tag>/<engine>-<provider>.jsonl"""
import json, os, sys, threading, time
from collections import Counter
from pathlib import Path
ROOT = Path(r"C:\Users\winz\Desktop\quality-test-integrated")
sys.path.insert(0, str(ROOT)); os.chdir(ROOT)
RUN, ENGINE, TAG = sys.argv[1:4]
if ENGINE == "luna":
    os.environ.update(SEMANTIC_API_MODEL="gpt-6-luna", SEMANTIC_API_REASONING="high", SEMANTIC_API_MAX_TOOL_CALLS="1",
                      SEMANTIC_API_MAX_OUTPUT_TOKENS="32000", SEMANTIC_API_TIMEOUT="600")
from code_eval import attachment_images
from code_eval.jev_client import resolve_jev_client
from code_eval.jev_criteria import JEV_CRITERIA_VERSION
from webapp.code_eval_service import CodeEvalService
HERE = Path(__file__).parent
TEST = json.loads((HERE / "rules_testset.json").read_text(encoding="utf-8"))
run = json.loads((ROOT / "data/comparison_runs" / f"{RUN}.json").read_text(encoding="utf-8"))
set_id = (run.get("question_set") or {}).get("set_id")
OUT = HERE / "exp_eval" / f"rules_{TAG}"; OUT.mkdir(parents=True, exist_ok=True)
engine = {"jev": "jev", "luna": "openai_api"}[ENGINE]
service = CodeEvalService(root=ROOT, save_dir=OUT / "raw", jev_resolver=lambda **kw: resolve_jev_client(engine=engine, env_path=ROOT / ".env"))
ctx = {"answer_tools": "unknown", "collected_at": (run.get("created_at") or "")[:10] or None,
       "attachment_roots": [str(ROOT / "data/question_sets" / str(set_id) / "files"), *map(str, attachment_images.DEFAULT_ROOTS)]}
print("규칙", JEV_CRITERIA_VERSION, flush=True)
for p, t in TEST.items():
    want = set(t["conflict"]) | set(t["control"])
    log = OUT / f"{ENGINE}-{p}.jsonl"
    done = {}
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line); done[r["case_id"]] = r
    batch = [r for r in run["datasets"][p] if r and r["case_id"] in want and (done.get(r["case_id"]) or {}).get("status") != "SUCCESS"]
    print(p, "대상", len(want), "| 이번에", len(batch), time.strftime("%H:%M:%S"), flush=True)
    if not batch:
        continue
    lock = threading.Lock()
    def observe(ev):
        if ev.get("phase") == "JEV" and "record" in ev:
            rec = ev["record"]
            with lock:
                if rec["case_id"] in want:
                    with log.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    service.evaluate({"cases": batch}, observer=observe, semantic_workers=12, pipeline="legacy", semantic_context=ctx)
    print(p, "끝", time.strftime("%H:%M:%S"), flush=True)
