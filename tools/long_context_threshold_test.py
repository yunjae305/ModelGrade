"""롱컨텍스트 경계 테스트 — 몇 K 토큰부터 J7(롱컨텍스트 활용)을 적용할지 정하려고, 길이별 긴 문서에서 정보를 찾는 정답률을 잰다.

사용자 결정(2026-10-13): 후보 경계는 272K 토큰. 250K~500K 를 실제로 시험해 정한다. 이 도구가 문서를 만들고(같은 시드면 늘 같은
문서), 연결된 비교 모델(Gemini·Claude·GPT)에 보내 코드로 채점한다 — 평가 모델(Opus) 호출 없음.

설계 (RULER·Lost in the Middle 방식):
- 길이: 32K(짧은 기준) + 250K·272K·300K·350K·400K·500K 토큰 (tiktoken o200k_base, ``code_eval.long_context`` 와 같은 셈).
- 문서: 한국어 업무 기록 문장을 시드로 만들어 채우고, 그 안에 정답이 정해진 사실을 넣는다.
  1·2·3번 — 문서 앞(10%)·중간(50%)·뒤(90%)의 사실 하나 찾기, 4번 — 두 곳(25%·75%)에 흩어진 사실 잇기(헷갈리게 하는 비슷한
  사실 여러 개 포함).
- 질문 4개를 문서 뒤에 한 번에 묻고 '1) … 4) …' 형식으로 답하게 한다. 해당 번호 줄의 코드가 정답 하나뿐이면 정답.
- 판단: 32K 대비 정답률이 떨어지기 시작하는 길이 — J7 경계 후보.

    python tools/long_context_threshold_test.py --dry-run          # 문서만 만들고 길이 확인 (API 호출 없음)
    python tools/long_context_threshold_test.py --providers gemini claude gpt   # 실제 호출 (유료, 길이 7개 × 모델 수)
    python tools/long_context_threshold_test.py --level hard ...   # 어려운 버전 (아래)

어려운 버전(hard, 1차 결과 500K 까지 모두 정답 → 2026-10-13 사용자 요청): 사실에 표시를 붙이지 않고 기록 문장 속에 숨긴다.
  1·2번 — 앞 10%·중간 50%, 질문과 표현이 다른 사실(NoLiMa: '부산에 사는' ↔ '해운대 바닷가 근처에 산다', 다른 도시 명소 오답 4개),
  3번 — 뒤 90%, 다른 말로 쓴 사실과 비슷한 오답('해지' ↔ '끝내려면', '연장하려면'·'바꾸려면' 번호),
  4번 — 20%·50%·80% 에 흩어진 3단계 연결(프로젝트 → 팀 → 팀장 → 예산 코드, 단계마다 오답),
  5번 — 문서 전체에 흩어진 사건 횟수 세기(4~8회).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from code_eval import long_context  # noqa: E402
from code_eval.llm_client import env_value  # noqa: E402

LENGTHS = (32_000, 250_000, 272_000, 300_000, 350_000, 400_000, 500_000)
POSITIONS = {1: 0.10, 2: 0.50, 3: 0.90}
HOP_POSITIONS = (0.25, 0.75)
TEST_VERSION = "long-context-threshold-test-v1"
HARD_VERSION = "long-context-threshold-test-v2-hard"
GRADER_VERSION = "exact-line-v2"   # 번호 줄의 코드·횟수가 정답 하나뿐일 때만 정답 (v1: 포함 여부·답 전체에서 찾기)

_DEPTS = ["재무팀", "인사팀", "물류팀", "고객지원팀", "품질관리팀", "구매팀", "법무팀", "연구개발팀", "영업1팀", "영업2팀", "시설관리팀", "교육팀"]
_TOPICS = ["분기 예산 집행 현황", "신규 채용 일정", "창고 재고 정리", "고객 문의 처리 시간", "협력사 납품 일정", "사내 교육 과정 개편",
           "보안 점검 결과", "회의실 예약 규칙", "출장비 정산 절차", "장비 교체 계획", "업무 매뉴얼 개정", "연말 행사 준비"]
_ACTIONS = ["다음 회의까지 초안을 정리하기로 했다", "담당자를 정해 진행 상황을 매주 공유하기로 했다", "추가 자료를 받은 뒤 다시 논의하기로 했다",
            "기존 방식을 유지하되 일부 절차를 줄이기로 했다", "관련 부서와 협의해 일정을 다시 잡기로 했다", "시범 운영 결과를 보고 확대 여부를 정하기로 했다"]
_NOTES = ["참석자 대부분이 일정이 빠듯하다는 의견을 냈다", "지난달보다 처리 건수가 조금 늘었다", "외부 업체 견적을 두 곳에서 더 받기로 했다",
          "현장 직원의 의견을 설문으로 모으기로 했다", "비용 절감 효과는 아직 크지 않은 것으로 보인다", "문서 양식을 하나로 통일하자는 제안이 나왔다"]
_NAMES = ["김도윤", "이서연", "박지호", "최하은", "정민준", "강지우", "조은서", "윤태양", "장서윤", "임하준", "한지민", "오세린"]


def _filler_sentence(rng: random.Random) -> str:
    return (f"{rng.randint(1, 12)}월 {rng.randint(1, 28)}일 {rng.choice(_DEPTS)} 회의에서는 {rng.choice(_TOPICS)}을(를) 다뤘다. "
            f"{rng.choice(_NAMES)} 담당이 현황을 설명했고, {rng.choice(_NOTES)}. 결론적으로 {rng.choice(_ACTIONS)}.")


def _code(rng: random.Random, prefix: str, used: set) -> str:
    """문서 안에서 겹치지 않는 코드 — 겹칠 때만 다시 뽑으므로, 겹침이 없던 문서는 예전과 똑같다."""
    while True:
        code = f"{prefix}-{rng.randint(1000, 9999)}"
        if code not in used:
            used.add(code)
            return code


def build_case(target_tokens: int, seed: int = 20261013) -> Dict[str, Any]:
    """길이 target_tokens 근처의 문서·질문·정답. 같은 (길이, 시드)면 늘 같은 내용."""
    rng = random.Random(f"{seed}:{target_tokens}")
    used: set = set()
    code = lambda prefix: _code(rng, prefix, used)
    answers = {1: code("KX"), 2: code("CR"), 3: code("HD"), 4: code("BG")}
    hop_name, hop_branch = "윤서진", "북극성"
    distractors = [(n, b, code("BG")) for n, b in (("한도경", "남십자성"), ("서지안", "오리온"), ("문하율", "카시오페아"))]
    needles = {
        1: f"[특별 기록] 프로젝트 '은하수-7'의 최종 승인 코드는 {answers[1]}로 확정되었다.",
        2: f"[특별 기록] 물류센터 '청람'의 비상 대응 코드는 {answers[2]}이다.",
        3: f"[특별 기록] '해오름 계약'의 해지 확인 코드는 {answers[3]}로 정했다.",
    }
    hop_a = (f"[인사 기록] {hop_name} 책임자는 '{hop_branch}' 지점 소속이다. "
             + " ".join(f"{n} 책임자는 '{b}' 지점 소속이다." for n, b, _ in distractors))
    hop_b = (f"[예산 기록] {hop_name} 책임자가 관리하는 지점의 예산 코드는 {answers[4]}이다. "
             + " ".join(f"{n} 책임자가 관리하는 지점의 예산 코드는 {c}이다." for n, _, c in distractors))
    questions = ("\n\n위 기록만 보고 다음 네 질문에 답하세요. 각 답은 해당 코드만 '1) 코드' 형식으로 한 줄씩 쓰세요.\n"
                 "1) 프로젝트 '은하수-7'의 최종 승인 코드는?\n2) 물류센터 '청람'의 비상 대응 코드는?\n"
                 "3) '해오름 계약'의 해지 확인 코드는?\n4) '북극성' 지점의 예산 코드는?")
    inserts = sorted([(POSITIONS[q], needles[q]) for q in (1, 2, 3)] + [(HOP_POSITIONS[0], hop_a), (HOP_POSITIONS[1], hop_b)])
    fixed = long_context.count_tokens("\n".join(text for _, text in inserts) + questions)
    budget = target_tokens - fixed
    sentences, tokens = [], 0
    while tokens < budget:   # 문장마다 세어 목표 길이에 맞춘다 (합은 이어 붙인 뒤와 거의 같다)
        sentence = _filler_sentence(rng)
        tokens += long_context.count_tokens(sentence + "\n")
        sentences.append(sentence)
    body: List[str] = []
    cursor = 0
    for position, text in inserts:
        cut = int(len(sentences) * position)
        body.extend(sentences[cursor:cut])
        body.append(text)
        cursor = cut
    body.extend(sentences[cursor:])
    prompt = "다음은 회사 업무 기록 모음입니다.\n\n" + "\n".join(body) + questions
    return {"target_tokens": target_tokens, "seed": seed, "prompt": prompt, "answers": answers,
            "tokens": long_context.count_tokens(prompt), "chars": len(prompt),
            "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}


_LINE = re.compile(r"^\s*\**\s*(?:[Qq]\s*)?([1-5])\s*\**\s*[\)\.:번]\s*(.*)$")
#: 코드처럼 생긴 낱말 전체 — KX-11110·KX-1111A·KX-1111-2·KX-99999 도 (틀린) 답 하나로 센다
_CODE_TOKEN = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\s*-\s*[A-Z0-9][A-Z0-9\-]*")
_KOREAN_COUNTS = {"한": 1, "하나": 1, "두": 2, "둘": 2, "세": 3, "셋": 3, "네": 4, "넷": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8,
                  "아홉": 9, "열": 10}
_COUNTER = r"\s*(?:번|회|건|차례)"
#: 횟수 답 전체 — '7', '총 7번', '다섯 번', '다섯'. 한·두·세·네는 뒤에 번·회가 있어야 ('네'·'모르겠네요'는 횟수가 아니다)
_COUNT_ANSWER = re.compile(r"(?:총|모두|전부)?\s*(?:(?P<num>\d+)(?:" + _COUNTER + r")?"
                           r"|(?P<word>하나|둘|셋|넷|다섯|여섯|일곱|여덟|아홉|열)(?:" + _COUNTER + r")?"
                           r"|(?P<attr>한|두|세|네)" + _COUNTER + r")\s*(?:일어났습니다|일어났다|발생했습니다|발생했다|입니다|이다)?\s*[.!]?")
#: 어려운 버전 — (명소, 도시). 질문은 도시, 문서는 명소만 쓴다 (세계 지식으로 이어야 함)
_PLACES = [("해운대 바닷가", "부산"), ("경복궁 돌담길", "서울"), ("무등산 자락", "광주"), ("성산일출봉 아래", "제주"), ("불국사 입구", "경주"),
           ("수성못 둘레길", "대구"), ("한옥마을 골목", "전주"), ("광안대교가 보이는 언덕", "부산"), ("경포대 해변", "강릉"), ("월미도 선착장", "인천")]


def build_hard_case(target_tokens: int, seed: int = 20261013) -> Dict[str, Any]:
    """어려운 버전 — 표시 없는 사실·다른 표현·비슷한 오답·3단계 연결·횟수 세기. 같은 (길이, 시드)면 늘 같은 내용."""
    rng = random.Random(f"hard:{seed}:{target_tokens}")
    used: set = set()
    code = lambda prefix: _code(rng, prefix, used)
    places = rng.sample([p for p in _PLACES if p[1] != "부산"], 4)   # 1번 오답 도시 (부산 제외)
    people = rng.sample(_NAMES, 12)
    answers: Dict[int, str] = {}
    inserts: List[Tuple[float, str]] = []
    # 1번 — 앞 10%, '부산에 사는 연구원' ↔ '해운대 바닷가 근처에 산다'. 오답 연구원 4명은 다른 도시 명소 (문서 곳곳)
    answers[1] = code("KX")
    inserts.append((0.10, f"연구원 {people[0]} 씨는 {_PLACES[0][0]} 근처에 살아 출근길이 늘 바다 풍경이다. 그의 사원 번호는 {answers[1]}이다."))
    for depth, (place, _), person in zip((0.30, 0.45, 0.62, 0.84), places, people[1:5]):
        inserts.append((depth, f"연구원 {person} 씨는 {place} 근처에 산다. 그의 사원 번호는 {code('KX')}이다."))
    # 2번 — 중간 50%, '제주에 사는 디자이너' ↔ '성산일출봉 아래'. 오답 디자이너 4명
    answers[2] = code("CR")
    others = rng.sample([p for p in _PLACES if p[1] != "제주"], 4)
    inserts.append((0.50, f"디자이너 {people[5]} 씨는 {_PLACES[3][0]} 마을에서 지낸다. 그의 출입 코드는 {answers[2]}로 등록되어 있다."))
    for depth, (place, _), person in zip((0.15, 0.35, 0.70, 0.93), others, people[6:10]):
        inserts.append((depth, f"디자이너 {person} 씨는 {place} 근처에서 지낸다. 그의 출입 코드는 {code('CR')}로 등록되어 있다."))
    # 3번 — 뒤 90%, '해지할 때' ↔ '끝내려면', 비슷한 오답(연장·변경)은 앞쪽에
    answers[3] = code("HD")
    inserts.append((0.90, f"해오름 계약을 끝내려면 담당 부서에 {answers[3]} 번호를 함께 제출해야 한다."))
    inserts.append((0.40, f"해오름 계약을 연장하려면 담당 부서에 {code('HD')} 번호를 함께 제출해야 한다."))
    inserts.append((0.75, f"해오름 계약 내용을 바꾸려면 담당 부서에 {code('HD')} 번호를 함께 제출해야 한다."))
    # 4번 — 3단계 연결 (20% → 50% → 80%), 단계마다 오답
    teams, leaders = ["푸른솔팀", "은빛물결팀", "노을빛팀"], people[10:12] + [rng.choice(people[:10])]
    projects = ["모래시계 사업", "바람개비 사업", "등대지기 사업"]
    answers[4] = code("BG")
    inserts.append((0.20, " ".join(f"{proj}은 {team}이 맡는다." for proj, team in zip(projects, teams))))
    inserts.append((0.52, " ".join(f"{team}의 팀장은 {leader}이다." for team, leader in zip(teams[::-1], leaders[::-1]))))
    codes = [answers[4], code("BG"), code("BG")]
    inserts.append((0.80, " ".join(f"{leader} 팀장이 승인한 예산 코드는 {code}이다." for leader, code in zip(leaders[1:] + leaders[:1], codes[1:] + codes[:1]))))
    # 5번 — 문서 전체에 흩어진 사건 횟수
    count = rng.randint(4, 8)
    answers[5] = str(count)
    days: set = set()
    for depth in sorted(rng.uniform(0.03, 0.97) for _ in range(count)):
        day = (rng.randint(1, 12), rng.randint(1, 28))
        while day in days:   # 같은 날이 두 번이면 사건 수가 모호하다 — 겹칠 때만 다시 뽑는다
            day = (rng.randint(1, 12), rng.randint(1, 28))
        days.add(day)
        inserts.append((depth, f"{day[0]}월 {day[1]}일 청람 물류센터에서 정전이 일어나 작업이 잠시 멈췄다."))
    questions = ("\n\n위 기록만 보고 다음 다섯 질문에 답하세요. 각 답은 코드 또는 숫자만 '1) 답' 형식으로 한 줄씩 쓰세요.\n"
                 "1) 부산에 사는 연구원의 사원 번호는?\n2) 제주에 사는 디자이너의 출입 코드는?\n"
                 "3) 해오름 계약을 해지할 때 필요한 번호는?\n4) 모래시계 사업을 맡은 팀의 팀장이 승인한 예산 코드는?\n"
                 "5) 청람 물류센터에서 정전은 모두 몇 번 일어났나요?")
    return _assemble(rng, target_tokens, seed, sorted(inserts), questions, answers, kinds={5: "count"})


def _assemble(rng: random.Random, target_tokens: int, seed: int, inserts: List[Tuple[float, str]], questions: str,
              answers: Dict[int, str], kinds: Dict[int, str] | None = None) -> Dict[str, Any]:
    fixed = long_context.count_tokens("\n".join(text for _, text in inserts) + questions)
    sentences, tokens = [], 0
    while tokens < target_tokens - fixed:
        sentence = _filler_sentence(rng)
        tokens += long_context.count_tokens(sentence + "\n")
        sentences.append(sentence)
    body: List[str] = []
    cursor = 0
    for position, text in inserts:
        cut = int(len(sentences) * position)
        body.extend(sentences[cursor:cut])
        body.append(text)
        cursor = cut
    body.extend(sentences[cursor:])
    prompt = "다음은 회사 업무 기록 모음입니다.\n\n" + "\n".join(body) + questions
    return {"target_tokens": target_tokens, "seed": seed, "prompt": prompt, "answers": answers, "kinds": kinds or {},
            "tokens": long_context.count_tokens(prompt), "chars": len(prompt),
            "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}


def _count_in(text: str) -> int | None:
    """답 전체가 횟수 하나일 때만 그 값. 범위(4-5)·여러 값(5 또는 여섯)·음수·소수·다른 말이 섞이면 None."""
    match = _COUNT_ANSWER.fullmatch((text or "").replace("*", "").strip())
    if not match:
        return None
    return int(match["num"]) if match["num"] is not None else _KOREAN_COUNTS[match["word"] or match["attr"]]


def grade(answer: str, answers: Dict[int, str], kinds: Dict[int, str] | None = None) -> Dict[int, bool]:
    """번호 줄('1)'·'1.'·'Q1:'·'1번')마다 채점한다. 코드 문항은 그 줄의 코드가 정답 하나뿐이어야, 횟수 문항은 그 줄의 횟수가
    하나뿐이고 정답과 같아야 정답. 번호 줄이 없는 문항은 틀림 — 답 전체에서 찾으면 다른 문항의 코드로 맞힐 수 있다."""
    lines: Dict[int, List[str]] = {}
    for line in (answer or "").splitlines():
        match = _LINE.match(line)
        if match:
            lines.setdefault(int(match.group(1)), []).append(match.group(2))
    result = {}
    for q, expected in answers.items():
        text = " ".join(lines.get(q, []))
        if (kinds or {}).get(q) == "count":
            result[q] = q in lines and _count_in(text) == int(expected)
        else:
            found = {re.sub(r"\s+", "", token).rstrip("-") for token in _CODE_TOKEN.findall(text.upper())}
            result[q] = found == {expected.upper()}
    return result


def _configs() -> Dict[str, Dict[str, str]]:
    from webapp.comparison_service import PROVIDER_ENV
    configs = {}
    for provider, (key_var, model_var) in PROVIDER_ENV.items():
        key, model = env_value(key_var, None, ROOT / ".env"), env_value(model_var, None, ROOT / ".env")
        if key and model:
            configs[provider] = {"key": key, "model": model}
    return configs


def _usage(provider: str, raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    if provider == "gemini":
        meta = raw.get("usageMetadata")
        meta = meta if isinstance(meta, dict) else {}   # 모양이 다르면 사용량만 비우고 답은 살린다
        return {"input_tokens": meta.get("promptTokenCount"), "output_tokens": meta.get("candidatesTokenCount")}
    usage = raw.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return {"input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
            "output_tokens": usage.get("output_tokens", usage.get("completion_tokens"))}


def run(providers: List[str], out: Path, max_tokens: int = 1024, level: str = "basic") -> Dict[str, Any]:
    import httpx
    from webapp.comparison_service import ProviderError, request_answer
    configs = _configs()
    missing = [p for p in providers if p not in configs]
    if missing:
        raise SystemExit("연결 설정(.env 키·모델)이 없는 모델: " + ", ".join(missing))
    out.mkdir(parents=True, exist_ok=True)   # 저장할 곳이 없으면 유료 호출 전에 멈춘다
    partial = out / "partial.jsonl"           # 받은 답을 하나씩 남겨, 중간에 끊겨도 잃지 않는다
    if partial.exists() or (out / "results.json").exists():   # 끊긴 실행의 답·끝난 결과를 덮어쓰지 않는다
        raise SystemExit(f"{out} 에 이전 실행의 결과가 있습니다 — 다른 --out 을 쓰세요")
    partial.open("x", encoding="utf-8").close()
    lock = threading.Lock()
    builder = build_hard_case if level == "hard" else build_case
    cases = [builder(length) for length in LENGTHS]
    results: List[Dict[str, Any]] = []

    def ask(provider: str) -> List[Dict[str, Any]]:
        rows = []
        with httpx.Client(timeout=900, follow_redirects=False) as client:
            for case in cases:   # 모델마다 짧은 길이부터 차례로
                started = time.monotonic()
                row = {"provider": provider, "model": configs[provider]["model"], "target_tokens": case["target_tokens"],
                       "tokens": case["tokens"], "sha256": case["sha256"]}
                try:
                    outcome = request_answer(provider, configs[provider], [{"role": "user", "content": case["prompt"]}],
                                             max_tokens, client)
                    row.update(status=outcome["status"], answer=outcome["answer"], finish=outcome.get("finish_reason"),
                               usage=_usage(provider, outcome.get("raw")), error=outcome.get("error"))
                except ProviderError as exc:
                    row.update(status="ERROR", answer="", error=str(exc), http_status=exc.status, usage={})
                row["latency_s"] = round(time.monotonic() - started, 1)
                row["correct"] = {str(q): ok for q, ok in grade(row["answer"], case["answers"], case.get("kinds")).items()}
                row["score"] = sum(row["correct"].values())
                rows.append(row)
                with lock, partial.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(f"{provider} {case['target_tokens']:>7,} → {row['status']} {row['score']}/{len(case['answers'])} ({row['latency_s']}s)", flush=True)
        return rows

    with ThreadPoolExecutor(max_workers=len(providers)) as pool:   # 모델끼리는 동시에, 한 모델 안에서는 차례로
        for rows in pool.map(ask, providers):
            results.extend(rows)
    labels = ({"1": "앞 10% 다른 표현", "2": "중간 50% 다른 표현", "3": "뒤 90% 비슷한 오답", "4": "3단계 연결", "5": "횟수 세기"}
              if level == "hard" else {"1": "앞 10%", "2": "중간 50%", "3": "뒤 90%", "4": "두 곳(25%·75%) 연결"})
    report = {"version": HARD_VERSION if level == "hard" else TEST_VERSION, "level": level, "grader": GRADER_VERSION,
              "created_at": datetime.now(timezone.utc).isoformat(), "tokenizer": long_context.TOKENIZER,
              "lengths": list(LENGTHS), "positions": labels,
              "cases": [{k: v for k, v in c.items() if k != "prompt"} for c in cases], "results": results}
    _write_atomic(out / "results.json", json.dumps(report, ensure_ascii=False, indent=1))
    _write_atomic(out / "summary.md", summary(report))
    partial.unlink()
    return report


def _write_atomic(path: Path, text: str) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def summary(report: Dict[str, Any]) -> str:
    providers = sorted({r["provider"] for r in report["results"]})
    lines = [f"# 롱컨텍스트 경계 테스트 ({report['version']})", "",
             "정답 수 — " + " · ".join(f"{q} {label}" for q, label in report["positions"].items())
             + ". ✓ 정답, ✗ 오답, ERR 호출 실패, (INCOMPLETE: 이유) 답이 끝까지 오지 않음 — 찾기 실패와 구분", "",
             "| 길이(목표) | 실제 토큰 | " + " | ".join(providers) + " |", "|---|---|" + "---|" * len(providers)]
    for length in report["lengths"]:
        rows = {r["provider"]: r for r in report["results"] if r["target_tokens"] == length}
        tokens = next((r["tokens"] for r in rows.values()), None)
        cells = []
        for p in providers:
            r = rows.get(p)
            if not r:
                cells.append("—")
            elif r["status"] == "ERROR":
                cells.append(f"ERR ({r.get('http_status') or ''})")
            else:
                marks = "".join("✓" if r["correct"][q] else "✗" for q in sorted(r["correct"]))
                cell = f"{r['score']}/{len(r['correct'])} {marks}"
                if r["status"] != "SUCCESS":   # 잘리거나 막힌 답
                    cell += f" ({r['status']}" + (f": {r['finish']}" if r.get("finish") else "") + ")"
                cells.append(cell)
        lines.append(f"| {length // 1000}K | {tokens:,} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--providers", nargs="+", default=["gemini", "claude", "gpt"])
    parser.add_argument("--dry-run", action="store_true", help="문서만 만들고 길이를 확인 (API 호출 없음)")
    parser.add_argument("--level", choices=("basic", "hard"), default="basic", help="hard: 표시 없는 사실·다른 표현·3단계 연결·횟수")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.dry_run:
        for length in LENGTHS:
            case = (build_hard_case if args.level == "hard" else build_case)(length)
            print(f"{length:>7,} 목표 → {case['tokens']:>7,} 토큰 · {case['chars']:>9,} 자 · 정답 {case['answers']}")
        return 0
    out = args.out or ROOT / "results" / (f"long-context-threshold-{args.level}-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    report = run(args.providers, out, level=args.level)
    print(summary(report))
    print(f"저장: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
