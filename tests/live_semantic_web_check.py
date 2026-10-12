"""Opt-in public, synthetic factual checks through the configured real judge.

No existing evaluations are modified. Artifacts contain public test text only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from code_eval.codex_cli_client import CodexCliClient
from code_eval.jev_client import resolve_jev_client
from code_eval.jev_eval import evaluate_jev_case

CASES = {
    "supported": ("Python 리스트는 수정할 수 있나요?", "Python 리스트는 생성 후 내용을 변경할 수 있는 가변 시퀀스입니다.", "PASS"),
    "contradicted": ("Python 리스트는 수정할 수 있나요?", "Python 리스트는 생성 후 내용을 변경할 수 없는 불변 시퀀스입니다.", "FAIL"),
    "unverifiable": ("공개되지 않은 가상의 모아컵 실험실 내부 창고 재고는 몇 개인가요?",
                     "공개되지 않은 가상의 모아컵 실험실 내부 창고 재고는 현재 정확히 1234개입니다.", "UNVERIFIED"),
    "outdated": (f"{datetime.now(timezone.utc).date().isoformat()} 현재 Python의 최신 안정 버전은 무엇인가요?",
                 f"{datetime.now(timezone.utc).date().isoformat()} 현재 Python의 최신 안정 버전은 3.10입니다.", "FAIL"),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="실제 CLI 평가·공개 웹검색·원문 요청 허용")
    parser.add_argument("--case", choices=["all", *CASES], default="all")
    args = parser.parse_args()
    if not args.live:
        parser.error("실제 호출은 --live 인자가 있어야 실행합니다.")
    folder = ROOT / "results" / "semantic-web-review" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder.mkdir(parents=True)
    client, config, error = resolve_jev_client(env_path=ROOT / ".env")
    if not isinstance(client, CodexCliClient) or error:
        raise RuntimeError("이 검사는 설정된 Codex CLI 연결이 필요합니다.")
    results = []
    for name, (question, answer, expected) in CASES.items():
        if args.case not in ("all", name):
            continue
        def run(command, **kwargs):
            proc = subprocess.run(command, **kwargs)
            allowed = []
            for line in (proc.stdout or "").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "turn.completed" or (
                    event.get("type") == "item.completed" and (event.get("item") or {}).get("type") == "web_search"
                ):
                    allowed.append(event)
            (folder / f"{name}-search-events.json").write_text(json.dumps(allowed, ensure_ascii=False, indent=2), encoding="utf-8")
            final_path = Path(command[command.index("--output-last-message") + 1])
            if final_path.exists():
                (folder / f"{name}-model-output.json").write_text(final_path.read_text(encoding="utf-8-sig"), encoding="utf-8")
            return proc
        judge = CodexCliClient(config, runner=run)
        record = evaluate_jev_case(f"synthetic-web-{name}", question, answer, judge, criteria=("J5",))
        decision = (record.get("decisions") or {}).get("J5") or {}
        actual = decision.get("decision") or decision.get("status")
        verification = record.get("verification") or {}
        sources = [s for c in (decision.get("fact_check") or {}).get("claims", []) for s in c.get("sources", [])]
        okay = record.get("status") == "SUCCESS" and actual == expected
        if expected in ("PASS", "FAIL"):
            okay = okay and verification.get("search_performed") is True and any(s.get("quote_verified") is True for s in sources)
        item = {"case": name, "expected": expected, "actual": actual, "okay": okay, "record": record}
        results.append(item)
        (folder / f"{name}-result.json").write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"case": name, "expected": expected, "actual": actual, "okay": okay,
                          "search_performed": verification.get("search_performed"),
                          "verified_quotes": sum(s.get("quote_verified") is True for s in sources)}, ensure_ascii=False), flush=True)
    summary = {"synthetic_only": True, "okay": bool(results) and all(r["okay"] for r in results), "results": results}
    (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(folder), flush=True)
    return 0 if summary["okay"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
