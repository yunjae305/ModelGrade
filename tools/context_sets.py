"""J6 멀티턴·J7 롱컨텍스트 시험 세트 생성·사전 점검 — 같은 시드면 늘 같은 데이터, API 호출 없음 (10-14 4차).

    python tools/context_sets.py j6 --out data/context_sets/j6-stress-v1                      # 장문 멀티턴(B): 3 대화 × 11턴, 턴마다 사용자 입력 10K+
    python tools/context_sets.py j6 --kind general --out data/context_sets/j6-general-v1      # 일반 멀티턴(A): 짧은 6턴 대화 6개
    python tools/context_sets.py j7 --out data/context_sets/j7-v1 [--lengths 32000 128000 250000 272000 500000]
    python tools/context_sets.py check data/context_sets/j6-stress-v1 [--json]               # 사전 점검 (다시 만들 수 있는지 포함)

공통 원칙
- 공식 규칙(J6 multi-turn-v2, J7 long-context-v3.1-272k)은 바꾸지 않는다. 세트는 그 규칙으로 적용 여부를 그대로 센다.
- 대화 하나 = 독립 시험 단위 1개 (턴 수로 세지 않는다). J7 은 길이마다 문서 하나 = 1개.
- 모델 답변 길이와 무관하게 같은 사용자 시나리오: B 는 11턴이고 사용자 턴마다 10K 토큰(o200k) 이상이라 공식 규칙(10턴 이상 · 대화 전체 10K 토큰)이
  어떤 답변에서도 채워진다. A 는 짧은 대화라 공식 규칙에서는 J6 해당 없음, '2턴 이상' 제안 규칙에서만 적용된다 (그대로 보고).
- 자료는 업무 기록(회의록·견적·문의·장애·일정·메일)을 레코드마다 다른 값·번호로 만든다 — 같은 문장 반복으로 채우지 않는다
  (중복 문장 비율을 매니페스트에 남긴다). 정답 사실은 표시 없이 같은 형식의 레코드로 넣고, 비슷한 오답 레코드를 함께 둔다.
- 행마다 기대 행동·적용 기준·검증할 맥락·정답 조건(코드로 확인하는 must/must_not)·올바른 답 예시·의도적인 실패 답을 둔다.
  실제 모델 답변이 없는 세트는 '질문 초안' 이다 (status).
- 큰 데이터는 저장소 밖(data/, 무시 목록)에 만든다. 커밋하는 것은 이 생성기와 테스트, 보고서의 매니페스트 요약뿐이다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import random
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval import long_context, multi_turn  # noqa: E402
from tools.long_context_probes import TOKEN_FACTOR  # noqa: E402  (3차 실측 근거의 제공자 토큰 배율 — 추정)
from tools.long_context_threshold_test import _CODE_TOKEN, _DEPTS, _NAMES, _TOPICS  # noqa: E402  (팀원 도구의 어휘·코드 표기)

SET_VERSION = "context-sets-v1"
J7_LENGTHS = (32_000, 128_000, 250_000, 272_000, 500_000)
STRESS_TURN_TOKENS = 10_300      # 사용자 턴 하나 — 공식 긴 턴(10K)보다 조금 크게
GENERAL_TURN_TOKENS = 260
STATUS_DRAFT = "질문 초안 (실제 모델 답변 없음)"

# --- 자료 생성: 레코드마다 다른 값 ---------------------------------------------------------------------------------
_VENDORS = ["한빛정밀", "서해물산", "미래테크", "온누리소재", "다온전자", "푸른산업", "정우기계", "새솔화학", "하늘유통", "가람전기",
            "누리포장", "대성금속", "청솔식품", "바른소프트", "태광부품", "동해해운", "백두물류", "소망인쇄", "은하조명", "금강건재"]
_GOODS = ["배터리 팩", "포장 상자", "케이블 묶음", "냉각 팬", "기판 보호 필름", "나사 세트", "라벨 용지", "충전기", "방수 패킹",
          "디스플레이 패널", "모터 드라이버", "완충재", "전원 어댑터", "손잡이 부품", "투명 커버"]
_SYSTEMS = ["주문 관리 시스템", "재고 대시보드", "결제 게이트웨이", "사내 메신저", "물류 추적 서버", "고객 포털", "인사 시스템",
            "빌드 서버", "메일 발송 서버", "전자결재"]
_CAUSES = ["인증서 만료", "디스크 용량 부족", "배포 설정 누락", "네트워크 스위치 고장", "외부 API 응답 지연", "DB 잠금 경합",
           "캐시 무효화 오류", "권한 설정 변경", "시간대 설정 오류"]
_FIXES = ["설정을 되돌렸다", "서버를 재시작했다", "용량을 늘렸다", "인증서를 갱신했다", "장비를 교체했다", "임시 우회 경로를 열었다"]
_CHANNELS = ["전화", "이메일", "채팅", "방문", "앱 리뷰"]
_COMPLAINTS = ["배송이 늦어졌다는 문의", "결제가 두 번 됐다는 문의", "사용 설명서를 찾을 수 없다는 문의", "교환 절차를 묻는 문의",
               "포장이 파손됐다는 문의", "회원 등급 혜택을 묻는 문의", "영수증 재발급 요청", "주소 변경 요청"]
_STATUS = ["처리 완료", "담당 부서 전달", "고객 회신 대기", "현장 확인 예정", "보류"]
_PLACES = ["본사 3층 대회의실", "물류센터 교육장", "판교 사무소", "부산 지점 회의실", "온라인 화상 회의", "성수 팝업 공간"]
_EVENTS = ["협력사 품질 점검", "신규 직원 안전 교육", "분기 실적 공유회", "매장 진열 리허설", "고객 패널 인터뷰", "재고 실사"]


class Material:
    """한 문서(자료)의 레코드 생성기 — 같은 rng 면 같은 내용. ``reserved`` 이름은 채움 레코드에 쓰지 않는다(정답 사실 전용)."""

    def __init__(self, rng: random.Random, prefix: str):
        self.rng, self.prefix, self.serial = rng, prefix, 0

    def _id(self, kind: str) -> str:
        self.serial += 1
        return f"{kind}-{self.prefix}{self.serial:04d}"

    def _day(self) -> str:
        return f"{self.rng.randint(1, 12)}월 {self.rng.randint(1, 28)}일"

    def record(self) -> str:
        """레코드 하나 — 문장마다 날짜·인원·금액 같은 값을 여럿 넣어, 긴 문서에서도 같은 문장이 거의 다시 나오지 않는다."""
        r = self.rng
        kind = r.randrange(6)
        if kind == 0:
            names = r.sample(_NAMES, 3)
            return (f"[회의록 {self._id('MT')}] {self._day()} {r.choice(_DEPTS)} 회의 — 안건 {r.choice(_TOPICS)}, 참석 {', '.join(names)}. "
                    f"{names[0]} 담당이 {self._note()}고 보고했고, 결정은 '{self._action()}'였다. "
                    f"후속 조치는 {names[1]} 담당이 {self._day()}까지 정리해 {names[2]} 담당에게 넘긴다.")
        if kind == 1:
            return (f"[견적 {self._id('QT')}] 공급사 {r.choice(_VENDORS)}, 품목 {r.choice(_GOODS)} {r.randint(5, 90) * 10}개, "
                    f"단가 {r.randint(12, 980) * 100:,}원, 납기 {r.randint(3, 45)}일, 결제 조건 {r.choice(['선금 30%', '월말 정산', '납품 후 15일', '분할 3회'])}, "
                    f"유효 기간 {r.randint(7, 60)}일, 승인 번호 {self._code('AP')}.")
        if kind == 2:
            return (f"[문의 {self._id('CS')}] {self._day()} {r.choice(_CHANNELS)} 접수, 고객 등급 {r.choice(['일반', '실버', '골드', 'VIP'])}, "
                    f"주문 {r.randint(100000, 999999)}번의 {r.choice(_COMPLAINTS)}. 처리 상태 {r.choice(_STATUS)}, 응대 {r.randint(2, 95)}분, "
                    f"담당 {r.choice(_NAMES)}, 회신 예정 {self._day()}.")
        if kind == 3:
            return (f"[장애 {self._id('IN')}] {self._day()} {r.choice(_SYSTEMS)} {r.randint(0, 23):02d}시 {r.randint(0, 59):02d}분 발생, "
                    f"원인 {r.choice(_CAUSES)}. 조치: {r.choice(_FIXES)}, 영향 사용자 약 {r.randint(3, 900)}명, 복구까지 {r.randint(4, 180)}분, "
                    f"조치 번호 {self._code('FX')}.")
        if kind == 4:
            return (f"[일정 {self._id('SC')}] {self._day()} {r.randint(9, 18)}시 {r.choice(_PLACES)}에서 {r.choice(_EVENTS)}, 담당 {r.choice(_NAMES)}. "
                    f"예상 인원 {r.randint(4, 60)}명, 준비물 {r.randint(1, 9)}종, 확인 마감 {self._day()}, 예약 코드 {self._code('RS')}.")
        a, b = r.sample(_NAMES, 2)
        return (f"[메일 {self._id('ML')}] {a}({r.choice(_DEPTS)}) → {b}: '{r.choice(_TOPICS)}' 관련 자료 {r.randint(2, 9)}건을 "
                f"{self._day()}까지 보내 달라는 요청. {self._note()}다는 내용이 함께 적혀 있고, 회신 기한은 {self._day()}이다.")

    def _note(self) -> str:
        r = self.rng
        return r.choice([
            f"참석자 {r.randint(5, 20)}명 중 {r.randint(2, 5)}명이 일정이 빠듯하다는 의견을 냈다",
            f"처리 건수가 지난달보다 {r.randint(3, 140)}건 {r.choice(['늘었다', '줄었다'])}",
            f"외부 업체 견적을 {r.randint(2, 5)}곳에서 더 받기로 했다",
            f"현장 직원 {r.randint(8, 120)}명의 의견을 설문으로 모으기로 했다",
            f"비용 절감 효과가 월 {r.randint(30, 900)}만 원 정도로 추정됐다",
            f"문서 양식을 {r.randint(3, 9)}종에서 하나로 줄이자는 제안이 나왔다",
            f"지난 분기 목표의 {r.randint(55, 120)}%를 달성했다",
            f"교육 이수율이 {r.randint(40, 99)}%까지 올라왔다",
        ])

    def _action(self) -> str:
        r = self.rng
        return r.choice([
            f"{self._day()}까지 초안을 정리하기로 했다",
            f"진행 상황을 {r.choice(['월요일', '수요일', '금요일'])}마다 {r.choice(_DEPTS)}와 공유하기로 했다",
            f"추가 자료 {r.randint(2, 6)}건을 받은 뒤 {self._day()}에 다시 논의하기로 했다",
            f"기존 절차 {r.randint(5, 12)}단계 가운데 {r.randint(1, 3)}단계를 줄이기로 했다",
            f"{r.choice(_DEPTS)}와 협의해 일정을 {r.randint(1, 4)}주 미루기로 했다",
            f"{r.randint(2, 8)}주 시범 운영 결과를 보고 확대 여부를 정하기로 했다",
        ])

    def _code(self, letters: str) -> str:
        return f"{letters}-{self.rng.randint(1000, 9999)}"

    def fill(self, target_tokens: int, inserts: Sequence[Tuple[float, str]] = ()) -> Tuple[str, List[Dict[str, Any]]]:
        """(본문, 넣은 사실의 위치 [{position, char_offset, token_offset, text}]) — 채움 레코드를 토큰 수로 맞추고 사실을 비율 위치에 넣는다."""
        fixed = sum(long_context.count_tokens(text + "\n") for _, text in inserts)
        records, tokens = [], 0
        while tokens < target_tokens - fixed:
            line = self.record()
            tokens += long_context.count_tokens(line + "\n")
            records.append(line)
        body: List[str] = []
        placed: List[Dict[str, Any]] = []
        cursor = 0
        for position, text in sorted(inserts):
            cut = int(len(records) * position)
            body.extend(records[cursor:cut])
            cursor = cut
            prefix = "\n".join(body)
            placed.append({"position": position, "char_offset": len(prefix) + (1 if body else 0),
                           "token_offset": long_context.count_tokens(prefix), "text": text})
            body.append(text)
        body.extend(records[cursor:])
        return "\n".join(body), placed


_NUMBERED = re.compile(r"^\s*\**\s*(?:[Qq]\s*)?([1-9])\s*\**\s*[\)\.:번]\s*(.*)$")


def grade(answer: str, answers: Dict[int, str]) -> Dict[int, bool]:
    """번호 줄마다 그 줄의 코드가 정답 하나뿐이면 정답 — 팀원 경계 시험의 채점(exact-line-v2)과 같은 규칙, 번호는 1~9 까지 읽는다
    (그 도구의 줄 형식은 1~5 만 읽어 6번 문항을 못 읽는다)."""
    lines: Dict[int, List[str]] = {}
    for line in (answer or "").splitlines():
        match = _NUMBERED.match(line)
        if match:
            lines.setdefault(int(match.group(1)), []).append(match.group(2))
    return {q: {re.sub(r"\s+", "", token).rstrip("-") for token in _CODE_TOKEN.findall(" ".join(lines.get(q, [])).upper())}
               == {expected.upper()} for q, expected in answers.items()}


def duplicate_ratio(text: str) -> float:
    """같은 문장이 다시 나온 비율 — 채움이 단순 반복인지 보는 지표 (레코드 번호를 지운 문장 단위)."""
    sentences = [re.sub(r"[A-Z]{2}-[A-Z]?\d+", "#", s).strip() for s in re.split(r"(?<=[.])\s+|\n", text) if s.strip()]
    return round(1 - len(set(sentences)) / len(sentences), 4) if sentences else 0.0


def _korean_day(day: dt.date) -> str:
    return f"{day.month}월 {day.day}일"


def _pattern(text: str) -> str:
    """정답 조건 정규식 — 공백 차이는 허용 ('11월 13일' = '11월13일')."""
    return r"(?<!\d)" + r"\s*".join(re.escape(part) for part in text.split())


def _amount_patterns(won: int) -> List[str]:
    """금액 표현 몇 가지 — 24,150,000원 · 24150000 · 2,415만 (원 단위가 만으로 나눠질 때)."""
    out = [re.escape(f"{won:,}"), re.escape(str(won))]
    if won % 10_000 == 0:
        man = won // 10_000
        out += [re.escape(f"{man:,}") + r"\s*만", re.escape(str(man)) + r"\s*만"]
    return out


def check_answer(answer: str, checks: Dict[str, Any]) -> Dict[str, Any]:
    """정답 조건 확인 — must(모두 있어야, 항목마다 대안 정규식 목록) · must_not(하나도 없어야). {passed, missing, forbidden}."""
    text = answer or ""
    missing = [label for label, options in (checks.get("must") or {}).items()
               if not any(re.search(option, text) for option in options)]
    forbidden = [label for label, options in (checks.get("must_not") or {}).items()
                 if any(re.search(option, text) for option in options)]
    return {"passed": not missing and not forbidden, "missing": missing, "forbidden": forbidden}


# --- J6 시나리오 -----------------------------------------------------------------------------------------------------
_PRODUCTS = ["무선 공기질 측정기 '숨결 S2'", "휴대용 커피 그라인더 '하루밀'", "어린이 학습 태블릿 '새싹패드'", "캠핑용 전기 랜턴 '별무리'"]
_LEADS = ["윤서진", "문하율", "배도현", "서지안"]
_RIVALS = ["코어닉스", "벨라텍", "누비아", "프라임원"]
_COLORS = ["새벽 남색", "모래 베이지", "숲 초록", "노을 주황"]
_TARGET_VENDORS = ["은가람정밀", "솔빛전자", "다래부품", "한결소재"]   # 채움 레코드에 없는 이름 (정답 사실 전용)
_CHEAPER = ["가온테크", "우듬지산업", "늘봄기기", "해담전기"]


def _scenario(seed: int, index: int) -> Dict[str, Any]:
    rng = random.Random(f"j6:{seed}:{index}")
    k = index % len(_PRODUCTS)
    launch = dt.date(2026, 11, rng.randint(17, 24))
    moved = launch + dt.timedelta(days=rng.choice((14, 15, 16)))
    vendors = [_TARGET_VENDORS[k], _CHEAPER[k], _TARGET_VENDORS[(k + 1) % 4] + "B"]
    prices = sorted(rng.sample(range(31, 49), 3))                      # 천 원 단위
    quotes = [  # (공급사, 단가, 납기) — 납기 14일 이내 중 최저가가 정답, 더 싼 곳은 납기 초과
        (vendors[1], prices[0] * 1000, 21),
        (vendors[0], prices[1] * 1000, 10),
        (vendors[2], prices[2] * 1000, 12),
    ]
    return {"product": _PRODUCTS[k], "lead": _LEADS[k], "rival": _RIVALS[k], "color": _COLORS[k],
            "budget_code": f"BG-{rng.randint(1000, 9999)}", "cap_won": 30_000_000, "launch": launch, "moved": moved,
            "quotes": quotes, "units": 500, "rng": rng}


def _quote_line(material: Material, vendor: str, price: int, days: int) -> str:
    return (f"[견적 {material._id('QT')}] 공급사 {vendor}, 품목 센서 모듈 {material.rng.randint(50, 90) * 10}개, 단가 {price:,}원, "
            f"납기 {days}일, 결제 조건 월말 정산, 승인 번호 {material._code('AP')}.")


def _turn_plan(s: Dict[str, Any], stress: bool) -> List[Dict[str, Any]]:
    """턴마다 {ask, inserts, criteria(J6 세부 항목), expect, context, checks, good, bad} — 대화 시작부터 끝까지 같은 사실 위에서."""
    launch, moved = _korean_day(s["launch"]), _korean_day(s["moved"])
    design = _korean_day(s["moved"] - dt.timedelta(days=21))
    design_old = _korean_day(s["launch"] - dt.timedelta(days=21))
    preorder = _korean_day(s["moved"] - dt.timedelta(days=10))
    preorder_old = _korean_day(s["launch"] - dt.timedelta(days=10))
    best, second = s["quotes"][1], s["quotes"][2]
    total = best[1] * s["units"]
    fits = total <= s["cap_won"]
    rival, lead, code, color = s["rival"], s["lead"], s["budget_code"], s["color"]
    no_rival = {"경쟁사 이름": [re.escape(rival)]}
    facts_t1 = [(0.12, f"[기획 메모] {s['product']} 출시 책임자는 {lead}이고, 출시일은 {launch}로 잡았다."),
                (0.48, f"[예산 메모] 이번 출시 예산 코드는 {code}이며 장비 구매 상한은 {s['cap_won'] // 10_000:,}만 원이다."),
                (0.83, f"[디자인 메모] 매장 진열용 주력 색상은 '{color}'로 정했다.")]
    plan = [
        {"ask": (f"지금부터 {s['product']} 출시 준비를 도와주는 비서 역할을 맡아 줘. 지켜야 할 것: 경쟁사 '{rival}' 이름은 답변에 쓰지 말 것, "
                 f"장비 구매 제안은 예산 상한을 넘기지 말 것. 아래는 기획 자료야. 오늘은 자료를 읽고 '출시 책임자'와 '출시일'만 확인해 줘."),
         "inserts": facts_t1, "criteria": [], "context": "1턴 — 역할·금지 사항·예산 상한, 이후 턴의 기준 정보(책임자·출시일·예산 코드·색상)",
         "expect": "책임자와 출시일을 자료대로 확인", "checks": {"must": {"책임자": [re.escape(lead)], "출시일": [_pattern(launch)]}},
         "good": f"출시 책임자는 {lead}, 출시일은 {launch}입니다.", "bad": f"출시 책임자는 {s['quotes'][0][0]} 담당, 출시일은 미정입니다.",
         "bad_note": "자료에 없는 답 (책임자·출시일 누락)"},
        {"ask": "이번엔 공급사 견적 묶음이야. 센서 모듈 견적 가운데 납기 14일 이내인 곳 중에서 단가가 가장 낮은 공급사와 단가를 알려 줘.",
         "inserts": [(0.2, "QUOTE0"), (0.55, "QUOTE1"), (0.85, "QUOTE2")], "criteria": ["J6-1.a"],
         "context": "1턴 지시(경쟁사 이름 금지)가 계속 유효", "expect": f"{best[0]} {best[1]:,}원 (더 싼 {s['quotes'][0][0]} 는 납기 21일이라 제외)",
         "checks": {"must": {"공급사": [re.escape(best[0])], "단가": _amount_patterns(best[1])}, "must_not": no_rival},
         "good": f"납기 14일 이내 최저가는 {best[0]}, 단가 {best[1]:,}원입니다.",
         "bad": f"최저가는 {s['quotes'][0][0]} {s['quotes'][0][1]:,}원입니다. 참고로 {rival} 제품보다 저렴합니다.",
         "bad_note": "납기 조건 무시 + 금지된 경쟁사 이름"},
        {"ask": "이번 회의록 묶음을 보고, 1턴 자료에 있던 예산 코드가 이번 회의록 어디에 다시 나오는지 찾아 주고, 그 예산 코드를 다시 적어 줘.",
         "inserts": [(0.6, f"[회의록 정리] 장비 구매 건은 예산 코드 {code} 로 처리하기로 재확인했다.")], "criteria": ["J6-2.a", "J6-1.a"],
         "context": "1턴의 예산 코드 기억", "expect": f"예산 코드 {code}",
         "checks": {"must": {"예산 코드": [re.escape(code)]}, "must_not": no_rival},
         "good": f"예산 코드는 {code}이고, 장비 구매 건 회의록 정리에 다시 나옵니다.", "bad": "예산 코드는 BG-0000 입니다.",
         "bad_note": "초반 정보를 잘못 기억"},
        {"ask": f"변경 사항이 있어. 출시일이 {moved}로 미뤄졌어. 앞으로는 이 날짜를 기준으로 해 줘. 포장 디자인은 출시 3주(21일) 전에 확정해야 해. "
                f"확정일이 언제야?",
         "inserts": [], "criteria": ["J6-3.a"], "context": f"출시일 변경 {launch} → {moved}",
         "expect": f"{design} (옛 출시일 기준 {design_old} 는 오답)",
         "checks": {"must": {"확정일": [_pattern(design)]}, "must_not": {"옛 날짜 기준": [_pattern(design_old)], **no_rival}},
         "good": f"새 출시일 {moved}에서 21일 전인 {design}까지 확정해야 합니다.", "bad": f"출시일 {launch} 기준으로 {design_old}까지입니다.",
         "bad_note": "변경 전 출시일 사용"},
        {"ask": "아까 두 번째 턴에서 고른 공급사 말고, 납기 조건을 만족하는 곳 중 두 번째로 싼 공급사는 어디였지?",
         "inserts": [], "criteria": ["J6-2.c", "J6-2.a"], "context": "2턴 견적 묶음의 조건·순위", "expect": f"{second[0]} ({second[1]:,}원)",
         "checks": {"must": {"공급사": [re.escape(second[0])]}, "must_not": {"첫 번째 공급사로 착각": [re.escape(best[0]) + r"[^.]*두 번째"], **no_rival}},
         "good": f"두 번째로 싼 곳은 {second[0]}({second[1]:,}원)입니다.", "bad": f"두 번째로 싼 곳은 {s['quotes'][0][0]}입니다.",
         "bad_note": "가리키는 대상을 잘못 찾음 (납기 초과 업체)"},
        {"ask": f"이번 비교표에서만은 예외로 경쟁사 '{rival}' 이름을 써도 돼. 자료의 시장 메모를 보고 우리 제품과 {rival} 제품을 한 줄씩 비교해 줘.",
         "inserts": [(0.5, f"[시장 메모] {rival}의 유사 제품은 가격이 높고 배터리 사용 시간이 짧다는 평가가 많다.")],
         "criteria": ["J6-1.a"], "context": "1턴 금지 사항의 이번 턴 한정 예외 — 최신·구체 지시가 우선",
         "expect": f"{rival} 이름을 써서 비교 (이번 턴만 허용)", "checks": {"must": {"경쟁사 비교": [re.escape(rival)]}},
         "good": f"우리 제품은 배터리 사용 시간이 길고, {rival} 제품은 가격이 높고 배터리 시간이 짧습니다.", "bad": "경쟁사 이름은 말씀드릴 수 없습니다.",
         "bad_note": "이번 턴의 명시적 예외를 무시"},
        {"ask": "사전 예약은 출시 10일 전에 시작해. 시작일을 알려 주고, 예약 안내 문구를 한 문장 써 줘.",
         "inserts": [], "criteria": ["J6-3.a", "J6-1.a"], "context": "변경된 출시일 + 예외는 6턴에서 끝남 (경쟁사 이름 다시 금지)",
         "expect": f"{preorder} (옛 기준 {preorder_old} 오답), 경쟁사 이름 없음",
         "checks": {"must": {"시작일": [_pattern(preorder)]}, "must_not": {"옛 날짜 기준": [_pattern(preorder_old)], **no_rival}},
         "good": f"사전 예약은 {preorder}에 시작합니다. 지금 예약하고 가장 먼저 받아 보세요.",
         "bad": f"사전 예약은 {preorder_old}에 시작합니다. {rival}보다 먼저 만나 보세요.", "bad_note": "옛 날짜 + 끝난 예외를 계속 적용"},
        {"ask": f"2턴에서 고른 공급사 단가로 센서 모듈 {s['units']}개를 사면 총액이 얼마고, 1턴의 장비 구매 상한 안에 들어?",
         "inserts": [], "criteria": ["J6-2.b", "J6-2.a"], "context": "2턴 단가 + 1턴 예산 상한 연결",
         "expect": f"총 {total:,}원 — 상한 {s['cap_won']:,}원 {'안' if fits else '초과'}",
         "checks": {"must": {"총액": _amount_patterns(total), "판단": [r"(안에|이내|넘지 않|들어)" if fits else r"(초과|넘)"]}, "must_not": no_rival},
         "good": f"{best[1]:,}원 × {s['units']}개 = {total:,}원으로 상한 {s['cap_won'] // 10_000:,}만 원 {'안에 들어갑니다' if fits else '을 초과합니다'}.",
         "bad": f"총액은 {s['quotes'][0][1] * s['units']:,}원이고 상한은 확인할 수 없습니다.", "bad_note": "다른 공급사 단가 + 상한 정보 누락"},
        {"ask": "1턴 기획 자료에 있던 매장 진열용 주력 색상으로 진열 안내문을 한 줄 써 줘.",
         "inserts": [], "criteria": ["J6-2.a"], "context": "1턴 자료 후반의 색상 (먼 거리 기억)", "expect": f"'{color}' 포함",
         "checks": {"must": {"색상": [re.escape(color)]}, "must_not": no_rival},
         "good": f"{color} 색상 제품을 입구 정면 진열대에 모아 주세요.", "bad": "검은색 제품을 입구 정면에 진열해 주세요.",
         "bad_note": "초반 정보 대신 지어낸 값"},
        {"ask": "아까 8턴 총액 계산, 그냥 틀린 것 같아. 다시 말해 봐.",
         "inserts": [], "criteria": ["J6-4.b", "J6-4.a"], "context": "근거 없는 반박 — 맞는 계산은 유지",
         "expect": f"근거 없이 바꾸지 않고 {total:,}원 유지",
         "checks": {"must": {"총액 유지": _amount_patterns(total)}, "must_not": no_rival},
         "good": f"다시 확인해도 {best[1]:,}원 × {s['units']}개 = {total:,}원이 맞습니다.", "bad": f"죄송합니다. 다시 계산하면 {total + 3_000_000:,}원입니다.",
         "bad_note": "근거 없는 반박에 결론을 바꿈"},
        {"ask": "마지막으로 지금까지 정한 것을 한 표로 정리해 줘: 출시일, 공급사와 단가, 예산 코드, 출시 책임자, 사전 예약 시작일.",
         "inserts": [], "criteria": ["J6-2.b", "J6-3.a", "J6-1.a"], "context": "여러 턴에 흩어진 정보 연결 + 최신 변경 반영",
         "expect": f"{moved} · {best[0]} {best[1]:,}원 · {code} · {lead} · {preorder}",
         "checks": {"must": {"출시일": [_pattern(moved)], "공급사": [re.escape(best[0])], "예산 코드": [re.escape(code)],
                             "책임자": [re.escape(lead)], "사전 예약": [_pattern(preorder)]},
                    "must_not": {"옛 출시일": [_pattern(launch)], **no_rival}},
         "good": f"| 출시일 | {moved} |\n| 공급사 | {best[0]} {best[1]:,}원 |\n| 예산 코드 | {code} |\n| 책임자 | {lead} |\n| 사전 예약 | {preorder} |",
         "bad": f"| 출시일 | {launch} |\n| 공급사 | {best[0]} |\n| 예산 코드 | {code} |\n| 책임자 | {lead} |", "bad_note": "옛 출시일 + 사전 예약 누락"},
    ]
    if not stress:   # 일반 멀티턴(A): 짧은 6턴 — 지시 유지·정보 기억·변경·지시 대상 찾기·예외 우선·변경 뒤 지시 유지
        plan = [plan[i] for i in (0, 1, 3, 4, 5, 6)]
    return plan


def build_j6_set(kind: str = "stress", conversations: int = 3, seed: int = 20261014) -> Dict[str, Any]:
    stress = kind == "stress"
    material_tokens = STRESS_TURN_TOKENS if stress else GENERAL_TURN_TOKENS
    rows, expect = [], {}
    for index in range(conversations if stress else 6):
        s = _scenario(seed, index)
        conv = f"CTX_J6{'S' if stress else 'G'}_{index + 1:02d}"
        material = Material(s["rng"], f"{index + 1}")
        turns = {}
        plan = _turn_plan(s, stress)
        for number, step in enumerate(plan, 1):
            inserts = []
            for position, text in step["inserts"]:
                if text.startswith("QUOTE"):
                    vendor, price, days = s["quotes"][int(text[-1])]
                    text = _quote_line(material, vendor, price, days)
                inserts.append((position, text))
            budget = max(0, material_tokens - long_context.count_tokens(step["ask"]) - 20)
            body, placed = material.fill(budget, inserts)
            question = f"{step['ask']}\n\n[자료 {number}]\n{body}"
            case_id = f"{conv}_T{number}"
            rows.append({"id": case_id, "conversation_id": conv, "turn": number, "question": question, "answer": ""})
            turns[str(number)] = {"case_id": case_id, "applies": step["criteria"], "expected_behavior": step["expect"],
                                  "context": step["context"], "checks": step["checks"],
                                  "good_answer": step["good"], "bad_answer": step["bad"], "bad_note": step["bad_note"],
                                  "evidence": [{"turn": number, "position": p["position"], "token_offset": p["token_offset"], "quote": p["text"]}
                                               for p in placed],
                                  "user_tokens": long_context.count_tokens(question)}
        expect[conv] = {"product": s["product"], "turns": turns, "unit": "대화 1개 = 독립 시험 1개"}
    return {"kind": f"j6-{kind}", "rows": rows, "expect": expect, "seed": seed}


# --- J7 롱컨텍스트 -----------------------------------------------------------------------------------------------------
_J7_LABELS = {1: "문서 초반 정보 검색", 2: "문서 중간 정보 검색", 3: "문서 후반 정보 검색", 4: "떨어진 정보 연결",
              5: "상충 정보 구별", 6: "사용자 요구 조건 유지"}


def build_j7_case(target_tokens: int, seed: int = 20261014) -> Dict[str, Any]:
    rng = random.Random(f"j7:{seed}:{target_tokens}")
    material = Material(rng, "D")
    used = set()

    def code(letters: str) -> str:
        while True:
            value = f"{letters}-{rng.randint(1000, 9999)}"
            if value not in used:
                used.add(value)
                return value
    a1, a2, a3, a4, old5, a5, a6 = code("KA"), code("FX"), code("RS"), code("BG"), code("ST"), code("ST"), code("EM")
    team, other_teams = "물결팀", ["바람팀", "들꽃팀", "구름팀"]
    decoys4 = {t: code("BG") for t in other_teams}
    inserts = [
        (0.06, f"[견적 QT-D9001] 공급사 은가람정밀, 품목 유지보수 계약 1건, 단가 4,830,000원, 납기 7일, 결제 조건 분할 3회, 승인 번호 {a1}."),
        (0.22, "[인사 메모] '해솔 프로젝트'는 물결팀이 맡는다. '이랑 프로젝트'는 바람팀, '가비 프로젝트'는 들꽃팀, '라온 프로젝트'는 구름팀이 맡는다."),
        (0.33, f"[3월 재고 보고] 품목 '청록 센서'의 재고 확인 코드는 {old5}로 보고되었다."),
        (0.50, f"[장애 IN-D9002] 별빛 정산 서버 02시 14분 발생, 영향 사용자 약 340명, 원인 인증서 만료, 조치: 인증서를 갱신했다, 복구까지 37분, 조치 번호 {a2}."),
        (0.61, f"[연락망] 물류팀 야간 비상 연락 코드는 {a6}이다."),
        (0.78, f"[예산 메모] {team}의 장비 예산 코드는 {a4}이다. " + " ".join(f"{t}의 장비 예산 코드는 {c}이다." for t, c in decoys4.items())),
        (0.86, f"[9월 정정 보고] 3월 보고의 '청록 센서' 재고 확인 코드는 집계 오류였다. 정정된 코드는 {a5}이다."),
        (0.93, f"[일정 SC-D9003] 11월 6일 15시 본사 3층 대회의실에서 가을 협력사 간담회. 담당 오세린, 예상 인원 28명, 예약 코드 {a3}."),
    ]
    preamble = ("[요청] 아래 업무 기록만 보고 마지막 질문에 답해 주세요. 답할 때 지킬 것: 6번에 답할 때만 코드 앞에 'OK-'를 붙이고, "
                "다른 번호에는 붙이지 마세요.\n\n다음은 회사 업무 기록 모음입니다.\n\n")
    questions = ("\n\n질문 — 위 기록만 보고 번호마다 한 줄로 답하세요 (예: '1) XX-0000').\n"
                 "1) 공급사 은가람정밀의 유지보수 계약 견적 승인 번호는?\n"
                 "2) 별빛 정산 서버 장애의 조치 번호는?\n"
                 "3) 가을 협력사 간담회의 예약 코드는?\n"
                 "4) '해솔 프로젝트'를 맡은 팀의 장비 예산 코드는?\n"
                 "5) 품목 '청록 센서'의 현재 유효한 재고 확인 코드는?\n"
                 "6) 물류팀 야간 비상 연락 코드는?\n")
    fixed = long_context.count_tokens(preamble + questions)
    body, placed = material.fill(target_tokens - fixed, inserts)
    prompt = preamble + body + questions
    answers = {1: a1, 2: a2, 3: a3, 4: a4, 5: a5, 6: f"OK-{a6}"}
    evidence = {1: placed[0], 2: placed[3], 3: placed[7], 4: [placed[1], placed[5]], 5: [placed[2], placed[6]], 6: [placed[4]]}
    good = "\n".join(f"{n}) {value}" for n, value in answers.items())
    failures = {1: f"1) {next(iter(decoys4.values()))}", 4: f"4) {decoys4['바람팀']}", 5: f"5) {old5}", 6: f"6) {a6}"}
    return {"target_tokens": target_tokens, "seed": seed, "question": prompt, "answers": answers, "labels": _J7_LABELS,
            "evidence": {str(k): v for k, v in evidence.items()}, "good_answer": good,
            "bad_answers": {str(n): good.replace(next(line for line in good.splitlines() if line.startswith(f"{n})")), wrong)
                            for n, wrong in failures.items()},
            "bad_notes": {"1": "다른 기록의 코드", "4": "연결 단계에서 다른 팀(오답 후보)", "5": "정정 전 옛 값", "6": "요구 조건(OK-) 누락"},
            "tokens": long_context.count_tokens(prompt), "chars": len(prompt), "bytes": len(prompt.encode("utf-8")),
            "duplicate_ratio": duplicate_ratio(body)}


def build_j7_set(lengths: Sequence[int] = J7_LENGTHS, seed: int = 20261014) -> Dict[str, Any]:
    rows, expect = [], {}
    for length in lengths:
        case = build_j7_case(length, seed)
        case_id = f"CTX_J7_{length // 1000}K"
        rows.append({"id": case_id, "question": case.pop("question"), "answer": ""})
        expect[case_id] = case
    return {"kind": "j7", "rows": rows, "expect": expect, "seed": seed}


# --- 저장·사전 점검 ------------------------------------------------------------------------------------------------
def _manifest(built: Dict[str, Any]) -> Dict[str, Any]:
    rows = built["rows"]
    per_row = [{"id": r["id"], "o200k_tokens": long_context.count_tokens(r["question"]), "chars": len(r["question"]),
                "sha256": hashlib.sha256(r["question"].encode("utf-8")).hexdigest(), "duplicate_ratio": duplicate_ratio(r["question"])}
               for r in rows]
    for entry in per_row:
        entry["provider_tokens_estimate"] = {p: round(entry["o200k_tokens"] * f) for p, f in TOKEN_FACTOR.items()}
    return {"version": SET_VERSION, "kind": built["kind"], "seed": built["seed"], "status": STATUS_DRAFT,
            "tokenizer": long_context.TOKENIZER if hasattr(long_context, "TOKENIZER") else "o200k_base",
            "provider_tokens_note": "제공자 토큰은 o200k × 배율 추정 (Claude 1.75 · GPT·Gemini 1.05, 3차 실측 근거) — 실제 값은 실호출 필요",
            "rows": per_row, "total_o200k_tokens": sum(e["o200k_tokens"] for e in per_row),
            "bytes": sum(len(r["question"].encode("utf-8")) for r in rows)}


def write_set(built: Dict[str, Any], out: Path) -> Dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    manifest = _manifest(built)
    (out / "questions.json").write_text(json.dumps(built["rows"], ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "expect.json").write_text(json.dumps({"version": SET_VERSION, "kind": built["kind"], "seed": built["seed"],
                                                 "status": STATUS_DRAFT, "items": built["expect"]}, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def check_set(folder: Path) -> Dict[str, Any]:
    """사전 점검 — 다시 만들어 같은지(sha256), 공식 규칙 적용 수(대화·문서 단위), 정답 조건이 올바른 답은 통과·실패 답은 걸러내는지,
    근거 문장이 그 턴·문서에 실제로 있는지. API 호출 없음."""
    from tools.check_question_set import check_items
    rows = json.loads((folder / "questions.json").read_text(encoding="utf-8"))
    expect = json.loads((folder / "expect.json").read_text(encoding="utf-8"))
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    kind = expect["kind"]
    rebuilt = (build_j7_set([int(r["id"].rsplit("_", 1)[1][:-1]) * 1000 for r in rows], expect["seed"]) if kind == "j7"
               else build_j6_set(kind.split("-", 1)[1], len({r["conversation_id"] for r in rows}), expect["seed"]))
    same = [hashlib.sha256(a["question"].encode("utf-8")).hexdigest() == hashlib.sha256(b["question"].encode("utf-8")).hexdigest()
            for a, b in zip(rows, rebuilt["rows"])]
    report: Dict[str, Any] = {"version": SET_VERSION, "kind": kind, "status": expect.get("status"), "rows": len(rows),
                              "reproducible": len(rows) == len(rebuilt["rows"]) and all(same),
                              "precheck": {cid: entry for cid, entry in check_items(rows)["criteria"].items() if cid in ("J6", "J7", "J8")}}
    problems: List[str] = []
    by_id = {r["id"]: r for r in rows}
    if kind == "j7":
        units = []
        for case_id, item in expect["items"].items():
            text = by_id[case_id]["question"]
            measured = long_context.count_tokens(text)
            good = grade(item["good_answer"], {int(k): v for k, v in item["answers"].items()})
            bad = {n: grade(answer, {int(k): v for k, v in item["answers"].items()})[int(n)] for n, answer in item["bad_answers"].items()}
            quotes = [e for ev in item["evidence"].values() for e in (ev if isinstance(ev, list) else [ev])]
            missing = [q["text"][:30] for q in quotes if q["text"] not in text]
            units.append({"id": case_id, "o200k_tokens": measured, "j7_official": measured >= long_context.MIN_TOKENS,
                          "good_passes": all(good.values()), "bad_detected": {n: not ok for n, ok in bad.items()},
                          "evidence_found": not missing, "duplicate_ratio": item["duplicate_ratio"],
                          "provider_tokens_estimate": next(e["provider_tokens_estimate"] for e in manifest["rows"] if e["id"] == case_id)})
            if not all(good.values()) or any(bad.values()) or missing:
                problems.append(case_id)
        report["units"] = units
        report["j7_official_units"] = sum(u["j7_official"] for u in units)
    else:
        conversations = []
        for conv, item in expect["items"].items():
            turns = [r for r in rows if r["conversation_id"] == conv]
            measure = multi_turn.measure_conversation([(r["turn"], r["question"], "") for r in turns])   # 답변 없이 = 어떤 답변에도 성립하는 하한
            checks = []
            for number, turn in item["turns"].items():
                good, bad = check_answer(turn["good_answer"], turn["checks"]), check_answer(turn["bad_answer"], turn["checks"])
                text = by_id[turn["case_id"]]["question"]
                missing = [e["quote"][:30] for e in turn["evidence"] if e["quote"] not in text]
                checks.append({"turn": int(number), "good_passes": good["passed"], "bad_detected": not bad["passed"],
                               "bad_reason": bad["missing"] + bad["forbidden"], "evidence_found": not missing,
                               "user_tokens": turn["user_tokens"], "applies": turn["applies"]})
                if not good["passed"] or bad["passed"] or missing:
                    problems.append(f"{conv} T{number}")
            conversations.append({"id": conv, "turns": len(turns), "official_multi_turn": measure.get("multi_turn"),
                                  "tokens_question_only": measure.get("tokens"),
                                  "two_turn_rule": len(turns) >= 2, "min_user_tokens": min(c["user_tokens"] for c in checks),
                                  "checks": checks})
        report["units"] = conversations
        report["j6_official_units"] = sum(c["official_multi_turn"] is True for c in conversations)
        report["j6_two_turn_units"] = sum(c["two_turn_rule"] for c in conversations)
    report["problems"] = problems
    report["total_o200k_tokens"] = manifest["total_o200k_tokens"]
    report["bytes"] = manifest["bytes"]
    return report


def render(report: Dict[str, Any]) -> str:
    lines = [f"# 시험 세트 사전 점검 ({report['version']}, {report['kind']}) — {report['status']}",
             f"- 행 {report['rows']} · 다시 만들기 일치 {report['reproducible']} · 총 {report['total_o200k_tokens']:,} 토큰(o200k) · "
             f"{report['bytes'] / 1_000_000:.1f}MB · 문제 {len(report['problems'])}"]
    if report["kind"] == "j7":
        lines.append(f"- 공식 J7(272K 이상) 적용 문서 {report['j7_official_units']}개 / {len(report['units'])}")
        lines += ["", "| 문서 | o200k | 공식 J7 | 올바른 답 통과 | 실패 답 걸러냄 | 근거 위치 | 중복 문장 | Claude·GPT·Gemini 추정 |", "|---|---:|---|---|---|---|---:|---|"]
        for u in report["units"]:
            est = u["provider_tokens_estimate"]
            lines.append(f"| {u['id']} | {u['o200k_tokens']:,} | {'적용' if u['j7_official'] else '해당 없음'} | {u['good_passes']} | "
                         f"{sum(u['bad_detected'].values())}/{len(u['bad_detected'])} | {u['evidence_found']} | {u['duplicate_ratio']:.1%} | "
                         f"{est['claude']:,} · {est['gpt']:,} · {est['gemini']:,} |")
    else:
        lines.append(f"- 공식 J6({multi_turn.MULTI_TURN_POLICY_VERSION}) 적용 대화 {report['j6_official_units']}개 · '2턴 이상' 규칙 {report['j6_two_turn_units']}개 "
                     f"/ {len(report['units'])} (대화 1개 = 시험 1개, 답변 없이 잰 하한)")
        lines += ["", "| 대화 | 턴 | 공식 J6 | 대화 토큰(질문만) | 사용자 턴 최소 토큰 | 올바른 답 통과 | 실패 답 걸러냄 |", "|---|---:|---|---:|---:|---|---|"]
        for c in report["units"]:
            lines.append(f"| {c['id']} | {c['turns']} | {c['official_multi_turn']} | {c['tokens_question_only']:,} | {c['min_user_tokens']:,} | "
                         f"{sum(x['good_passes'] for x in c['checks'])}/{len(c['checks'])} | {sum(x['bad_detected'] for x in c['checks'])}/{len(c['checks'])} |")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    j6 = sub.add_parser("j6")
    j6.add_argument("--kind", choices=("stress", "general"), default="stress")
    j6.add_argument("--conversations", type=int, default=3)
    j6.add_argument("--seed", type=int, default=20261014)
    j6.add_argument("--out", type=Path, required=True)
    j7 = sub.add_parser("j7")
    j7.add_argument("--lengths", type=int, nargs="+", default=list(J7_LENGTHS))
    j7.add_argument("--seed", type=int, default=20261014)
    j7.add_argument("--out", type=Path, required=True)
    check = sub.add_parser("check")
    check.add_argument("folder", type=Path)
    check.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.command == "check":
        report = check_set(args.folder)
        (args.folder / "check.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else render(report), end="")
        return 0 if not report["problems"] and report["reproducible"] else 1
    built = build_j6_set(args.kind, args.conversations, args.seed) if args.command == "j6" else build_j7_set(args.lengths, args.seed)
    manifest = write_set(built, args.out)
    print(f"{args.out}: {len(built['rows'])}행 · 총 {manifest['total_o200k_tokens']:,} 토큰(o200k) · {manifest['bytes'] / 1_000_000:.1f}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
