"""Check semantic settings; live evaluation happens only with an explicit smoke flag."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .jev_client import resolve_jev_client
from .jev_eval import evaluate_jev_case
from .semantic_setup import semantic_setup


def main(argv=None):
    parser = argparse.ArgumentParser(description="의미평가 연결 확인 (기본은 설정 확인만, 평가 호출 없음)")
    parser.add_argument("--smoke", action="store_true", help="참조 자료와 장기 대화의 모의 문항 2개를 실제 평가")
    parser.add_argument("--web-smoke", action="store_true", help="공개 Python 문서의 사실 확인 1개를 실제 검색·평가")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    if not args.smoke and not args.web_smoke:
        setup = semantic_setup(env_path=root / ".env")
        print(json.dumps({"ready": setup["can_evaluate"], "configuration": setup,
                          "reason": setup["reason"]}, ensure_ascii=False, indent=2))
        return 0 if setup["can_evaluate"] else 2
    client, config, reason = resolve_jev_client(env_path=root / ".env")
    print(json.dumps({"ready": client is not None, "configuration": config.to_manifest(), "reason": reason}, ensure_ascii=False, indent=2))
    if client is None:
        return 2
    rows = []
    if args.smoke:
        rows.append(evaluate_jev_case("reference-smoke", "시험용 알파 제품의 색상은?", "알파 제품의 색상은 파랑입니다.", client,
            criteria=("J1", "J5"), reference="시험용 기준 자료: 알파 제품의 색상은 빨강입니다."))
        history = [{"turn": 1, "question": "이 대화에서 나를 민수라고 불러줘.", "answer": "알겠어, 민수."}]
        history += [{"turn": turn, "question": "이어서 이야기해줘.", "answer": "대화를 이어갈게."} for turn in range(2, 20)]
        history.append({"turn": 20, "question": "이제부터 내 이름은 준이야. 준이라고 불러줘.", "answer": "알겠어, 준."})
        history += [{"turn": turn, "question": "이어서 이야기해줘.", "answer": "대화를 이어갈게, 준."} for turn in range(21, 30)]
        rows.append(evaluate_jev_case("context-smoke", "내 이름을 부르며 인사해줘.", "안녕, 준!", client,
                                      criteria=("J6",), history=history))
    if args.web_smoke:
        rows.append(evaluate_jev_case("web-smoke", "Python 리스트는 수정할 수 있나요?",
            "Python 리스트는 생성 후 내용을 변경할 수 있는 가변 시퀀스입니다.", client, criteria=("J5",)))
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    for row in rows:
        if row.get("review_required") or row.get("status") != "SUCCESS":
            return 2
        decisions = row.get("decisions") or {}
        if row["case_id"] == "reference-smoke" and decisions.get("J5", {}).get("decision") != "FAIL":
            return 3
        if row["case_id"] == "context-smoke" and decisions.get("J6", {}).get("decision") != "PASS":
            return 3
        if row["case_id"] == "web-smoke" and decisions.get("J5", {}).get("decision") != "PASS":
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
