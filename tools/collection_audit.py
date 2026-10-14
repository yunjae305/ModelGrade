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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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


def _provider_input(raw: Dict[str, Any]) -> Optional[int]:
    usage = raw.get("usage") or {}
    if isinstance(raw.get("usageMetadata"), dict):
        return raw["usageMetadata"].get("promptTokenCount")
    if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
        return sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return usage.get("input_tokens")


def _cached(raw: Dict[str, Any]) -> int:
    usage, meta = raw.get("usage") or {}, raw.get("usageMetadata") or {}
    return int(usage.get("cache_read_input_tokens") or (usage.get("input_tokens_details") or {}).get("cached_tokens")
               or meta.get("cachedContentTokenCount") or 0)


def conversation_inputs(run: Dict[str, Any]) -> Dict[str, Any]:
    """대화 턴마다 실제로 보낸 요청을 저장 행으로 다시 만든다 — 수집은 '앞 턴 질문 + 그 모델 자신의 답변 + 이번 질문'을
    시스템 프롬프트·도구 없이 보낸다(request_answer). 원문 대신 메시지 수·글자 수·o200k 토큰·sha256 앞 12자만 남긴다.

    ratio = 제공자가 집계한 입력 토큰 / o200k 근사 — 턴마다 거의 같으면 토크나이저 차이, 갑자기 줄면 이력 누락·잘림 의심.
    """
    import hashlib
    from code_eval import long_context
    try:
        count = long_context.count_tokens
        count("확인")
    except long_context.TokenizerUnavailable:
        count = None
    out: Dict[str, Any] = {}
    for provider, rows in (run.get("datasets") or {}).items():
        if provider == "aiona":
            continue
        conversations: Dict[Any, List[Dict[str, Any]]] = {}
        for row in rows or []:
            if isinstance(row, dict) and row.get("conversation_id") not in (None, ""):
                conversations.setdefault(row["conversation_id"], []).append(row)
        for cid, turns in conversations.items():
            turns.sort(key=lambda r: r.get("turn") or 0)
            history: List[Dict[str, str]] = []
            series = []
            for row in turns:
                collection = row.get("collection") if isinstance(row.get("collection"), dict) else {}
                raw = collection.get("raw") if isinstance(collection.get("raw"), dict) else {}
                messages = history + [{"role": "user", "content": row.get("question") or ""}]
                text = "\n".join(m["content"] for m in messages)
                approx = count(text) if count else None
                provider_tokens = _provider_input(raw)
                series.append({"turn": row.get("turn"), "messages": len(messages),
                               "roles": {"user": sum(m["role"] == "user" for m in messages),
                                         "assistant": sum(m["role"] == "assistant" for m in messages)},
                               "chars": len(text), "own_answer_chars": sum(len(m["content"]) for m in messages if m["role"] == "assistant"),
                               "approx_tokens": approx, "provider_input_tokens": provider_tokens, "cached_tokens": _cached(raw),
                               "ratio": round(provider_tokens / approx, 3) if provider_tokens and approx else None,
                               "sha256": hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode("utf-8")).hexdigest()[:12],
                               "status": collection.get("status"), "finish": collection.get("finish_reason")})
                if collection.get("status") != "SUCCESS":
                    break   # 수집도 여기서 이 대화를 멈춘다 (앞 턴 실패 → 뒤 턴 미요청)
                history = messages + [{"role": "assistant", "content": row.get("answer") or ""}]
            ratios = [s["ratio"] for s in series if s["ratio"]]
            drops = [s["turn"] for prev, s in zip(series, series[1:])
                     if prev["provider_input_tokens"] and s["provider_input_tokens"] and s["provider_input_tokens"] < prev["provider_input_tokens"]]
            last = series[-1] if series else {}
            out.setdefault(str(cid), {})[provider] = {
                "turns": len(series), "last": last,
                "ratio_min": min(ratios) if ratios else None, "ratio_max": max(ratios) if ratios else None,
                "input_drops_at_turns": drops, "complete": len(series) == len(turns) and all(s["status"] == "SUCCESS" for s in series),
                "series": series}
    return {"version": AUDIT_VERSION, "run_id": run.get("run_id"), "tokenizer": long_context.TOKENIZER if count else None,
            "system_prompt": None, "tools": [], "conversations": out}


def render_inputs(report: Dict[str, Any]) -> str:
    lines = [f"대화 입력 재구성 ({report['version']}, 실행 {report['run_id']}, 근사 토크나이저 {report['tokenizer']}, API 호출 없음)",
             "| 대화 | 모델 | 턴 | 마지막 턴 메시지(user/assistant) | 마지막 요청 글자 | 그중 자기 답변 글자 | o200k 근사 | 제공자 집계 | 비율(최소~최대) | 감소 턴 | 캐시 |",
             "|---|---|---:|---|---:|---:|---:|---:|---|---|---:|"]
    for cid, models in report["conversations"].items():
        for model, row in models.items():
            last = row["last"]
            lines.append(f"| {cid} | {model} | {row['turns']} | {last.get('messages')} ({last.get('roles', {}).get('user')}/{last.get('roles', {}).get('assistant')}) | "
                         f"{last.get('chars'):,} | {last.get('own_answer_chars'):,} | {last.get('approx_tokens') or '-':,} | {last.get('provider_input_tokens') or '-':,} | "
                         f"{row['ratio_min']}~{row['ratio_max']} | {row['input_drops_at_turns'] or '없음'} | {last.get('cached_tokens')} |")
    return "\n".join(lines)


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
    parser.add_argument("--inputs", action="store_true", help="대화 턴마다 보낸 요청을 재구성해 모델별 입력 크기를 비교 (원문 없이 길이·해시)")
    args = parser.parse_args(argv)
    run = json.loads(args.run.read_text(encoding="utf-8-sig"))
    report = conversation_inputs(run) if args.inputs else audit(run)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else render_inputs(report) if args.inputs else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
