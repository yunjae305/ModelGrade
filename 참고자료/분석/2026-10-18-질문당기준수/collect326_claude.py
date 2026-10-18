"""326 질문 세트의 Claude 답을 수집용 Chrome(Claude 탭, Sonnet 5.5 · 노력 중간)으로 받는다.
- 대화는 1턴부터 같은 채팅에서, 한 턴이라도 실패하면 그 대화는 다음 실행 때 새 채팅에서 처음부터 다시 받는다.
- 턴마다 collect326/claude.jsonl 에 저장(나중 줄이 앞 줄을 대신함). 다시 돌리면 모든 턴이 성공한 질문은 건너뛴다.
- 멈추려면 collect326/STOP 파일을 만든다(지금 턴을 끝내고 멈춤).
python -X utf8 collect326_claude.py"""
import json, sys, tempfile, time
from pathlib import Path

ROOT = Path(r"C:\Users\winz\Desktop\quality-test-integrated")
sys.path.insert(0, str(ROOT))
from webapp.cdp_chat_web import ClaudeChat
from webapp.question_generation import flatten

D = ROOT / "참고자료/수집결과/2026-10-14-질문세트326-세부기준"
OUT = Path(__file__).parent / "collect326"
OUT.mkdir(exist_ok=True)
LOG, STOP = OUT / "claude.jsonl", OUT / "STOP"
rows = flatten(json.loads((D / "questions.json").read_text(encoding="utf-8")))
qnum = lambda cid: int(cid.split("_T")[0][1:])
chains, seen = [], {}
for r in sorted(rows, key=lambda r: (qnum(r["case_id"]), r.get("turn") or 1)):
    q = r["case_id"].split("_T")[0]
    if q not in seen:
        seen[q] = []
        chains.append((q, seen[q]))
    seen[q].append(r)
done = {}
if LOG.exists():
    for line in LOG.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            done[rec["case_id"]] = rec
def ok(q, turns):
    return all((done.get(t["case_id"]) or {}).get("collection", {}).get("status") == "SUCCESS" for t in turns)
def save(row, outcome):
    rec = {"case_id": row["case_id"], "conversation_id": row.get("conversation_id"), "turn": row.get("turn"),
           "question": row["question"], "answer": outcome.get("answer") or "", "collection": outcome}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    done[row["case_id"]] = rec

todo = [(q, t) for q, t in chains if not ok(q, t)]
print(f"질문 {len(chains)} | 끝남 {len(chains) - len(todo)} | 이번에 받을 질문 {len(todo)}", time.strftime("%H:%M:%S"), flush=True)
with tempfile.TemporaryDirectory(prefix="claude326-") as temp, \
        ClaudeChat(headless=False, debug_dir=OUT / "debug", cancelled=STOP.exists) as chat:
    for pos, (q, turns) in enumerate(chains, 1):
        if ok(q, turns):
            continue
        if STOP.exists():
            print("STOP 파일로 멈춤", flush=True)
            break
        chat.new_chat()
        broken = False
        for t in turns:
            if broken:
                save(t, {"status": "ERROR", "error": "앞선 대화 응답이 없어 이 턴의 수집을 건너뛰었습니다.", "sent": []})
                continue
            files = []
            for a in t.get("attachments") or []:   # 원래 파일 이름으로 올린다
                target = Path(temp) / t["case_id"] / str(a.get("name"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((D / "files" / str(a.get("path"))).read_bytes())
                files.append(target)
            try:
                outcome = chat.ask(t["question"], files, t.get("reference"))
            except Exception as exc:
                outcome = {"status": "ERROR", "error": f"Claude 화면 처리 실패: {type(exc).__name__}", "sent": []}
                chat.new_chat()
            outcome["collected_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            save(t, outcome)
            if outcome.get("status") != "SUCCESS":
                broken = True
        turn_note = f", {len(turns)}턴" if len(turns) > 1 else ""
        status = "실패" if broken else "성공"
        print(f"{time.strftime('%H:%M:%S')} {pos}/{len(chains)} ({q}{turn_note}) {status}", flush=True)
fails = [q for q, t in chains if not ok(q, t)]
print("끝", time.strftime("%H:%M:%S"), "| 성공", len(chains) - len(fails), "| 실패", len(fails), fails[:20], flush=True)
