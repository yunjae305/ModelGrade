"""수집 재현성 점검 — 저장된 실행의 모델별 수집 성공·실패·미요청, 응답 모델명, 종료 사유, 요청 시각, 호출 방식, 원본 응답 보존 여부 (API 호출 없음).

    python tools/collection_audit.py data/comparison_runs/<run>.json [--json]

- 호출 방식(access_mode)은 실행 기록(collection_execution.request · 외부 실행의 systems)에서만 읽는다. 기록이 없는 옛 실행은 '기록 없음'.
- AIONA 는 업로드 원문이라 수집 대상이 아니다(재현 가능한 자동 수집 경로 없음 — 업로드 파일이 원본).
- 실패한 수집도 행으로 남아 있으면 사유와 함께 센다. 판정 값은 보지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

AUDIT_VERSION = "collection-audit-v1"


def _response_model(raw: Dict[str, Any]) -> Optional[str]:
    return raw.get("model") or raw.get("modelVersion") if isinstance(raw, dict) else None


def _finish(collection: Dict[str, Any]) -> Optional[str]:
    return collection.get("finish_reason") or collection.get("status")


def audit(run: Dict[str, Any]) -> Dict[str, Any]:
    request = (run.get("collection_execution") or {}).get("request")
    systems = run.get("systems") if isinstance(run.get("systems"), dict) else {}
    out: Dict[str, Any] = {"version": AUDIT_VERSION, "run_id": run.get("run_id"), "run_kind": run.get("run_kind") or "comparison",
                           "created_at": run.get("created_at"), "collection_completed_at": run.get("collection_completed_at"),
                           "request_conditions": request, "providers": {}}
    for provider, rows in (run.get("datasets") or {}).items():
        rows = [r for r in rows or [] if isinstance(r, dict)]
        if provider == "aiona":
            out["providers"][provider] = {"source": "업로드 원문 (수집 아님)", "rows": len(rows),
                                          "import_source": sorted({str(r.get("import_source")) for r in rows if r.get("import_source")}),
                                          "release": (run.get("aiona_release") or {}).get("version")}
            continue
        collections = [r.get("collection") if isinstance(r.get("collection"), dict) else {} for r in rows]
        status = Counter("NOT_REQUESTED" if c.get("not_requested") else c.get("status") or "MISSING" for c in collections)
        errors = Counter(str(c.get("error"))[:80] for c in collections if c.get("status") not in ("SUCCESS", None))
        requested = sorted(c["requested_at"] for c in collections if c.get("requested_at"))
        system = systems.get(provider) or {}
        configured = (run.get("models") or {}).get(provider) if isinstance(run.get("models"), dict) else None
        out["providers"][provider] = {
            "rows": len(rows), "status": dict(status), "errors": dict(errors.most_common(5)),
            "access_mode": system.get("access_mode") or ((request or {}).get("access_mode")) or "기록 없음",
            "configured_model": configured if isinstance(configured, str) else (configured or {}).get("model") if isinstance(configured, dict) else system.get("model"),
            "response_models": dict(Counter(m for c in collections if (m := _response_model(c.get("raw") or {})))),
            "finish_reasons": dict(Counter(f for c in collections if (f := _finish(c)))),
            "requested_at": {"first": requested[0], "last": requested[-1], "rows": len(requested)} if requested else "기록 없음",
            "raw_saved": sum(1 for c in collections if isinstance(c.get("raw"), dict)),
            "usage_saved": sum(1 for c in collections if isinstance((c.get("raw") or {}).get("usage") or (c.get("raw") or {}).get("usageMetadata"), dict)),
        }
    return out


def render(report: Dict[str, Any]) -> str:
    lines = [f"수집 재현성 점검 ({report['version']}, 실행 {report['run_id']} · {report['run_kind']}, API 호출 없음)",
             f"- 수집 완료 {report['collection_completed_at'] or '-'} · 요청 조건 " + (
                 json.dumps({k: v for k, v in report['request_conditions'].items() if k != 'note'}, ensure_ascii=False)
                 if report["request_conditions"] else "기록 없음 (이 기록이 생기기 전 실행)")]
    for provider, row in report["providers"].items():
        if "source" in row:
            lines.append(f"- {provider}: {row['source']} {row['rows']}행 · 버전 {row['release'] or '-'}")
            continue
        lines.append(f"- {provider}: {row['rows']}행 · 방식 {row['access_mode']} · 상태 {row['status']} · 설정 모델 {row['configured_model']} · "
                     f"응답 모델 {row['response_models'] or '기록 없음'} · 종료 {row['finish_reasons']} · 원본 {row['raw_saved']}/{row['rows']} · "
                     f"사용량 {row['usage_saved']}/{row['rows']} · 요청 시각 {row['requested_at'] if isinstance(row['requested_at'], str) else row['requested_at']['first'] + ' ~ ' + row['requested_at']['last']}")
        for error, n in row["errors"].items():
            lines.append(f"    실패 {n}건: {error}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = audit(json.loads(args.run.read_text(encoding="utf-8-sig")))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
