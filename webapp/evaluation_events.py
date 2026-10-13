"""Bounded SSE replay plus a complete per-question snapshot. No model identities or source answers."""
from collections import deque
import copy
import json
import threading
import uuid

from code_eval.jev_criteria import CRITERIA

NAMES = {c.id: c.name for c in CRITERIA}


def code_event(row, index, qid):
    # "UNJUDGED"·"LLM 보완" 은 옛 run 의 저장 로그 재생 호환용 — 새 엔진은 counted(PASS/FAIL) 행만 보내고 code_completion 을 만들지 않는다.
    return {"type": "criterion_result", "index": index, "qid": qid,
            "key": "CODE:" + row["check_id"] + ":" + row["condition_id"],
            "code": row["subcategory"], "name": row["title"],
            "result": row.get("verdict") or "UNJUDGED", "reason": row.get("reason") or "",
            "stage": "LLM 보완" if row.get("evaluation_method") == "LLM_COMPLETION" else "CODE"}


def record_events(case, record, index):
    base = {"index": index, "qid": case["case_id"]}
    if record["status"] == "SKIPPED":
        yield {**base, "type": "q_skip", "reason": record.get("error") or "평가할 질문·답변 없음"}
        return
    completion = record.get("code_completion") or {}
    for item in completion.get("resolutions", []):
        yield code_event(item["result"], index, case["case_id"])
    items_mode = record.get("pipeline") == "items"
    unverifiable_total = 0
    for cid in record.get("criteria") or []:
        d = (record.get("decisions") or {}).get(cid) or {}
        # items 파이프라인: 재확인 3회 뒤에도 인용을 확인하지 못해 '확인 불가' 로 FAIL 한 항목 수 (결함 확인 FAIL 과 구분)
        unverifiable = sum(1 for sub in (d.get("subs") or {}).values() for it in (sub.get("items") or []) if it.get("unverifiable")) if items_mode else 0
        unverifiable_total += unverifiable
        yield {**base, "type": "criterion_result", "key": cid, "code": cid, "name": NAMES.get(cid, cid),
               "result": d.get("decision") or "UNJUDGED", "reason": d.get("reason") or record.get("error") or "판정 근거 없음",
               **({'verification_status': d['status']} if d.get('status') in ('UNVERIFIED', 'INCOMPLETE') else {}),
               **({"unverifiable": unverifiable} if unverifiable else {}),
               "stage": "LLM"}
    yield {**base, "type": "q_done", "error": record.get("error") or completion.get("error"),
           "review_required": bool(record.get('review_required')),
           # items 파이프라인의 재확인 회차·확인 불가 항목 수 — legacy 레코드에는 없는 키라 있을 때만 싣는다
           **({"rechecks": int(((record.get("recheck") or {}).get("rounds")) or 0), "unverifiable": unverifiable_total} if items_mode else {})}


class EvaluationEvents:
    def __init__(self, saved=None, capacity=512):
        self.condition = threading.Condition()
        self.events = deque(maxlen=capacity)
        self.sequence = 0
        self.state = {"generation": None, "total": 0, "done": 0, "pass": 0, "fail": 0,
                      "unjudged": 0, "skipped": 0, "status": "WAITING", "rows": {}}
        if saved:
            self.state.update(copy.deepcopy(saved))
            if self.state["status"] == "ERROR":
                for row in self.state["rows"].values():
                    if row["status"] == "RUNNING":
                        row.update(status="INTERRUPTED", active=None, reason="서버 재시작으로 평가 중단")

    def publish(self, event):
        with self.condition:
            event = copy.deepcopy(event)
            kind = event["type"]
            if kind == "run_start":
                self.state = {"generation": uuid.uuid4().hex, "total": event["total"], "done": 0,
                              "pass": 0, "fail": 0, "unjudged": 0, "skipped": 0, "status": "RUNNING", "rows": {}}
                event["generation"] = self.state["generation"]
            elif kind.startswith("q_") or kind.startswith("criterion_"):
                key = f'{event["model"]}:{event["index"]}'  # duplicate qids are deliberately allowed
                row = self.state["rows"].setdefault(key, {"model": event["model"], "index": event["index"],
                    "qid": event["qid"], "criteria": {}, "status": "RUNNING", "pass": 0, "fail": 0, "unjudged": 0})
                row["qid"] = event["qid"]
                if kind == "criterion_start":
                    row["active"] = {k: event[k] for k in ("code", "name", "stage")}
                if kind == "criterion_end" and (row.get("active") or {}).get("code") == event["code"]:
                    row["active"] = None
                if kind == "criterion_result":
                    old = row["criteria"].get(event["key"])
                    if old:
                        field = {"PASS": "pass", "FAIL": "fail", "UNJUDGED": "unjudged"}[old["result"]]
                        row[field] -= 1
                        self.state[field] -= 1
                    row["criteria"][event["key"]] = {k: event[k] for k in ("code", "name", "result", "reason", "stage")}
                    if event.get('verification_status'):
                        row['criteria'][event['key']]['verification_status'] = event['verification_status']
                    if event.get('unverifiable'):
                        row['criteria'][event['key']]['unverifiable'] = event['unverifiable']
                    field = {"PASS": "pass", "FAIL": "fail", "UNJUDGED": "unjudged"}[event["result"]]
                    row[field] += 1
                    self.state[field] += 1
                if kind in ("q_done", "q_skip"):
                    if row["status"] == "RUNNING":
                        self.state["done"] += 1
                        self.state["skipped"] += kind == "q_skip"
                    row.update(status="SKIPPED" if kind == "q_skip" else "DONE", active=None,
                               reason=event.get("reason") or event.get("error"), finished_id=self.sequence + 1,
                               review_required=bool(event.get('review_required')))
                    for key in ("rechecks", "unverifiable"):   # items 파이프라인만 보내는 값
                        if key in event:
                            row[key] = event[key]
                    event.update({k: row[k] for k in ("pass", "fail", "unjudged")})
            elif kind in ("run_done", "run_error"):
                self.state["status"] = "COMPLETED" if kind == "run_done" else "ERROR"
                if kind == "run_error":
                    for row in self.state["rows"].values():
                        if row["status"] == "RUNNING":
                            row.update(status="INTERRUPTED", active=None, reason=event.get("reason"))
                event.update({k: self.state[k] for k in ("total", "done", "pass", "fail", "unjudged", "skipped")})
            self.sequence += 1
            event.update(id=self.sequence, generation=self.state["generation"])
            self.events.append(event)
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return copy.deepcopy(self.state)

    def read(self, after=None, timeout=10):
        with self.condition:
            if after is None or after > self.sequence or (self.events and after < self.events[0]["id"] - 1):
                return [{"type": "snapshot", "id": self.sequence, **self.snapshot()}]
            if after == self.sequence and self.state["status"] not in ("COMPLETED", "ERROR"):
                self.condition.wait(timeout)
            return [copy.deepcopy(e) for e in self.events if e["id"] > after]

    def frames(self, after=None):
        """SSE generator; heartbeats release disconnects even during long CLI calls."""
        while True:
            events = self.read(after)
            if not events:
                yield b": heartbeat\n\n"
            for event in events:
                after = event["id"]
                yield (f"id: {after}\ndata: " + json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n\n").encode("utf-8")
            with self.condition:
                terminal = self.state["status"] in ("COMPLETED", "ERROR") and after == self.sequence
            if terminal:
                return
