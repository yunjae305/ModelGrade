"""보류 사유 분류 — 실행의 의미 평가 보류 항목(판정 없음)을 사유별로 묶어 모델마다 센다 (2026-10-19, 평가 API 호출 없음).

    python -X utf8 tools/hold_reasons.py <run_id>
"""
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.run_summary import question_outcomes  # noqa: E402


def bucket(k, v):
    rs = str(v.get("reason") or "")
    if k != "J5":
        if "근거가 부족" in rs:
            return f"{k} 근거 부족(확인 못 함)"
        return f"{k} 심판 갈림(Jev↔Luna, 재판정도 다름)"
    tail = rs.split("확인 필요:")[-1]
    if "웹검색을 사용할 수 없" in tail or "웹 검색을 사용할 수 없" in tail:
        return "J5 참조 자료 질문 — 자료 밖 일반 지식(웹 검색 안 함)"
    if "답변 원문에 없" in tail:
        return "J5 Luna 가 옮긴 주장이 답변 원문과 안 맞음"
    if "출처 원문을 확인하지 못함" in tail or "출처를 확인하지 못했" in tail or "본문에서 인용문을 확인하지 못" in tail:
        return "J5 웹 출처 원문 확인 못 함"
    if "참조 자료에서 인용문을 확인하지 못" in tail:
        return "J5 참조 자료 인용 대조 실패"
    if "참조 자료" in tail or "reference" in tail:
        return "J5 참조 자료에 근거 없음(자료 밖 주장)"
    return "J5 기타: " + re.sub(r"\d+", "N", tail.strip())[:40]


def main(run_id: str) -> None:
    s = json.loads((ROOT / "data" / "comparison_runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    for p in s["providers"]:
        ev = s["evaluations"][p]
        turn = collections.Counter()
        for c, r in zip(ev["cases"], ev["jev_eval"]["records"]):
            if not isinstance(r, dict) or r.get("status") != "SUCCESS":
                continue
            for k, v in (r.get("decisions") or {}).items():
                if isinstance(v, dict) and v.get("decision") is None:
                    turn[bucket(k, v)] += 1
        rows = s["datasets"][p]
        empty = sum(1 for o in question_outcomes(ev, len(rows), rows) if o.get("hold") == "empty_output")
        print(f"\n[{p}] 보류 항목(턴) 사유")
        if empty:   # 의미 판정과 별개로 질문(턴) 전체가 보류 — empty-output-hold-v1
            print(f"  {empty:4d}  빈 출력 요구 질문 — 질문 전체 보류(사용자 결정 10-19, 판정·수집 결과와 무관)")
        for b, n in turn.most_common():
            print(f"  {n:4d}  {b}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "8c61f117e661")
