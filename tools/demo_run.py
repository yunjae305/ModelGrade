"""3분 시연용 샘플 실행 — 비공개 파일 없이 어느 PC 에서나 같은 결과 화면을 재현한다 (10-14 4차). 유료 호출·외부 네트워크 0.

    python tools/demo_run.py --serve [--port 8095]      # 샘플 실행을 만들고 그 폴더만 쓰는 로컬 서버를 띄운다 (Ctrl+C 로 종료)
    python tools/demo_run.py --out results/demo-run      # 실행 파일만 만든다

실제 경로를 그대로 쓴다: 업로드 → 자동 수집(Mock 제공자 GPT·Claude·Gemini) → CODE → 항목별 의미 평가(items, Mock Judge) → 집계·저장.
- 모든 답변과 판정은 모의다: 실행 이름·AIONA 버전 메모에 '샘플·모의'를 적고, 평가 모델 이름은 'mock-judge (대역, Claude 아님)'.
- 질문은 tools/items_e2e_demo 의 시나리오(정상·CODE FAIL·의미 FAIL·없는 인용·멀티턴·J5 사실 오류 확정·J5 출처 인용 불일치·평가기
  시간 초과) + 이미지 첨부 질문 1개(J8 — 이 도구가 만든 PNG 를 답변 모델과 평가 모델에 같이 보냄).
- 공식 규칙은 그대로다 — 멀티턴 대화(3턴)는 길이 조건(multi-turn-v1)을 채우지 않아 J6 해당 없음으로 나온다.
- 저장 폴더는 실제 data/ 와 따로다. 서버도 그 폴더만 읽고, API 키·.env 를 불러오지 않는다 (다시 평가하면 '평가 모델 없음').
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

DEMO_VERSION = "demo-run-v1"
RUN_NAME = "[샘플·모의] 3분 시연용 비교 — 실제 모델·평가 결과 아님"
IMAGE_QUESTION = "첨부한 그림 속 자동차는 무슨 색인가요? 한 문장으로 답해 주세요."
IMAGE_NAME = "demo-car.png"
#: 제공자마다 답변 길이를 다르게 — 멀티턴에서 모델마다 자기 답변 이력이 달라지는 것(입력 구성 주의)을 보여 준다
ANSWER_STYLE = {"gpt": ("짧게 답합니다.", 1), "claude": ("자세히 설명드리면, 질문하신 내용을 단계별로 정리하겠습니다.", 6),
                "gemini": ("핵심만 정리하면 다음과 같습니다.", 3)}


def demo_png() -> bytes:
    """8×8 빨간 PNG — Pillow 없이 표준 라이브러리로 만든다 (같은 바이트가 늘 나온다)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + b"\xd0\x20\x20" * 8 for _ in range(8))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def demo_cases(image_sha256: str) -> List[Dict[str, Any]]:
    from tools.items_e2e_demo import SCENARIOS
    cases = [dict(case) for scenario in SCENARIOS for case in scenario["cases"]]
    cases.append({"case_id": "G", "question": IMAGE_QUESTION, "answer": "그림 속 자동차는 빨간색입니다.",
                  "attachments": [{"name": IMAGE_NAME, "type": "image", "content": "자동차 그림 (색은 그림에서 확인)",
                                   "path": IMAGE_NAME, "sha256": image_sha256}]})
    return cases


def provider_handler():
    """Mock 제공자 — 실제 API 형식의 응답과 사용량(입력·출력 토큰)을 돌려준다. 답변 길이는 제공자마다 다르다."""
    sent: Dict[str, int] = {}

    def handle(request):
        provider = {"api.openai.com": "gpt", "api.anthropic.com": "claude", "generativelanguage.googleapis.com": "gemini"}[request.url.host]
        body = json.loads(request.content)
        sent[provider] = sent.get(provider, 0) + 1
        lead, repeat = ANSWER_STYLE[provider]
        answer = f"[모의 {provider} 답변 {sent[provider]}] {lead} " + " ".join(f"설명 {i + 1}." for i in range(repeat))
        prompt_chars = len(json.dumps(body, ensure_ascii=False))
        usage_in, usage_out = max(1, prompt_chars // 2), max(1, len(answer) // 2)
        if provider == "gpt":
            value = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": answer}]}],
                     "usage": {"input_tokens": usage_in, "output_tokens": usage_out}}
        elif provider == "claude":
            value = {"stop_reason": "end_turn", "content": [{"type": "text", "text": answer}],
                     "usage": {"input_tokens": usage_in, "output_tokens": usage_out}}
        else:
            value = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": answer}]}}],
                     "usageMetadata": {"promptTokenCount": usage_in, "candidatesTokenCount": usage_out}}
        value["model"] = f"mock-{provider} (샘플)"
        return httpx.Response(200, json=value)
    return handle


def _judge():
    from tools.items_e2e_demo import PAGES, SCENARIOS, MockJudge

    class DemoJudge(MockJudge):
        """AIONA 답변에만 시나리오 대본, 다른 모델 답변에는 기본 응답. 첨부 그림을 받는 평가 연결로 둔다 (모의)."""
        supports_images = True

        def __init__(self):
            cases = [case for scenario in SCENARIOS for case in scenario["cases"]]
            super().__init__({c["question"]: sc for s in SCENARIOS for c, sc in zip(s["cases"], s["scripts"])}, PAGES)
            self.aiona = {c["question"]: c["answer"] for c in cases}
            self.images_seen = 0

        def script_for(self, state):
            script = self.scripts.get(state.get("question"), {})
            if self.aiona.get(state.get("question")) == state.get("answer"):
                return script
            return {"apply_model_items": script.get("apply_model_items", False)}

        def system_one(self, state, questions, *, cancel_event=None, images=None):
            self.images_seen += len(images or [])
            return super().system_one(state, questions, cancel_event=cancel_event)
    return DemoJudge()


def _wait(service, run_id: str, done: Tuple[str, ...], timeout: float = 120) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = service.status(run_id)
        if status["status"] in done:
            return status
        time.sleep(0.05)
    raise TimeoutError(f"{run_id} 가 {done} 에 이르지 못함")


def build(folder: Path) -> Dict[str, Any]:
    """folder 에 샘플 실행을 만든다 → {run_id, folder, judge_calls, images_seen}. 같은 폴더에 다시 만들면 새 실행이 하나 더 생긴다."""
    import hashlib
    from tools.items_e2e_demo import _no_network
    from webapp.code_eval_service import CodeEvalService
    from webapp.comparison_service import ComparisonService
    os.environ["SEMANTIC_PIPELINE"] = "items"   # 이 프로세스에서만 — .env 를 바꾸지 않는다
    folder.mkdir(parents=True, exist_ok=True)
    attachments = folder / "attachments"
    attachments.mkdir(exist_ok=True)
    image = demo_png()
    (attachments / IMAGE_NAME).write_bytes(image)
    registry = folder / "versions.json"
    registry.write_text(json.dumps({"versions": ["v1"], "current_release": {"version": "v1", "notes": "샘플·모의 — 시연용"}},
                                   ensure_ascii=False), encoding="utf-8")
    judge = _judge()
    evaluator = CodeEvalService(save_dir=folder / "eval", jev_resolver=lambda **_: (judge, judge.config, None))
    service = ComparisonService(evaluator, save_dir=folder / "runs", registry_path=registry,
                                client_factory=lambda: httpx.Client(transport=httpx.MockTransport(provider_handler())))
    service.attachment_roots = [attachments]
    for provider in ("gpt", "claude", "gemini"):
        service.configure(provider, {"key": "fixture-only-not-a-key", "model": f"mock-{provider}"})
    service.update_auto_policy({"allowed": {p: True for p in ("gpt", "claude", "gemini")},
                                "prices": {p: {"input": 1, "output": 2} for p in ("gpt", "claude", "gemini")},
                                "limits": {"job_calls": 200, "job_cost_usd": 5, "daily_calls": 1000, "daily_cost_usd": 20}})
    with _no_network():
        started = service.start_auto_collection({"cases": demo_cases(hashlib.sha256(image).hexdigest()), "name": RUN_NAME,
                                                 "input_name": "demo-sample.json"})
        run_id = started["run"]["run_id"]
        _wait(service, run_id, ("READY", "ERROR"))
        service.evaluate(run_id, {})
        status = _wait(service, run_id, ("COMPLETED", "ERROR", "CANCELLED"), timeout=300)
    if status["status"] != "COMPLETED":
        raise RuntimeError(f"샘플 실행이 끝나지 않음: {status['status']} {status.get('error')}")
    service.shutdown()
    return {"version": DEMO_VERSION, "run_id": run_id, "folder": str(folder), "judge_calls": len(judge.calls),
            "images_seen": judge.images_seen, "name": RUN_NAME}


def serve(folder: Path, port: int) -> None:
    """샘플 폴더만 쓰는 로컬 서버 — 평가 모델·제공자 키를 불러오지 않는다 (load_environment 를 부르지 않음)."""
    from webapp.code_eval_service import CodeEvalService
    from webapp.comparison_service import ComparisonService
    from webapp.server import Server
    evaluator = CodeEvalService(save_dir=folder / "eval",
                                jev_resolver=lambda **_: (None, None, "시연용 샘플 서버 — 평가 모델을 연결하지 않았습니다"))
    service = ComparisonService(evaluator, save_dir=folder / "runs", registry_path=folder / "versions.json")
    server = Server(("127.0.0.1", port), code_eval_service=evaluator, comparison_service=service)
    print(f"시연용 샘플 서버: http://localhost:{server.server_port}/workspace.html  (AIONA 결과 이력 → 상세 보기)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, help="샘플 폴더 (기본: 임시 폴더). 실제 data/ 는 쓰지 않는다")
    parser.add_argument("--serve", action="store_true", help="만든 뒤 그 폴더만 쓰는 로컬 서버를 띄운다")
    parser.add_argument("--port", type=int, default=8095)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    folder = args.out or Path(tempfile.mkdtemp(prefix="modelgrade-demo-"))
    if folder.resolve().is_relative_to((ROOT / "data").resolve()):
        raise SystemExit("샘플은 실제 data/ 밖에 만듭니다 — 다른 --out 을 쓰세요")
    built = build(folder)
    print(json.dumps(built, ensure_ascii=False))
    if args.serve:
        serve(folder, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
