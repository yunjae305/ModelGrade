"""J1~J8 적용 판정 — 질문·답변·입력 정보에서 코드 규칙으로 적용 여부와 근거를 뽑는다 (LLM 없음).

CODE 의 질문 조건 추출(``extract.py``)과 같은 원칙을 쓴다.
- 질문 안의 인용 자료(펜스 코드, ``>`` 인용 줄, 표, 큰따옴표 문장)는 지시가 아니라 데이터이므로 가린다.
- J2~J4 는 요청 문장(물음표나 '~해줘·~마·~해' 등으로 끝나는 문장)에서만 찾는다. 상황 설명 문장은 보지 않는다.
- J1 은 모든 질문에 적용한다. J2~J4 는 근거 문구가 있을 때만 적용하고, 없으면 '해당 없음'과 이유를 남긴다.
- J5 DATA 신뢰도는 **답변**에 검증할 데이터(수치·단위, 연도·날짜, 통계·출처 표현)가 있을 때, 또는 참조 데이터(reference)가
  있을 때 적용한다. 코드 펜스·인라인 코드 안의 숫자와 목록 번호는 데이터로 보지 않는다. 명칭·고유명사는 규칙으로 잡지 못한다.
- J6 멀티턴 맥락 유지는 멀티턴 대화(``conversation["multi_turn"]`` — 10K 토큰 이상 턴이 10개 이상, 그 모델이 실제로 주고받은
  대화로 잰다)의 2번째 턴부터, 앞 턴 대화가 있을 때 적용한다. 멀티턴이 아니거나 첫 턴이면 '해당 없음', 토크나이저가 없어 재지
  못하면 ``undetermined`` 이다. 단일 질문·답변 입력에는 적지 않는다. ``multi_turn`` 을 주지 않은 호출(직접 만든 conversation)은
  길이 조건 없이 예전처럼 판정한다 — 평가·점검 경로는 ``conversation_contexts`` 가 늘 채운다.
- J7 롱컨텍스트 활용은 입력(현재 질문 + 참조 자료)이 272K 토큰 이상일 때 적용한다 (``long_context`` — 고정 공개 토크나이저로 셈,
  이전 대화 턴은 세지 않음). 토크나이저를 불러오지 못하면 해당 없음이 아니라 ``undetermined`` 에 남긴다 — 평가 기록에서
  확인 필요(UNVERIFIED)로 집계되고, 적용 여부를 짐작하지 않는다.
- J8 첨부파일 활용은 질문에 첨부파일(``attachments`` — 이미지·TXT·DOCX·PDF 등)이 있을 때 적용한다. J8-2(파일 형식 읽기)는
  답이 첨부의 구조에 기대는지 평가 모델이 정하고, 답변이 첨부를 열 수 없다고 밝혔으면 J8-1·J8-2 는 평가 모델이 해당 없음으로 둔다.
- 같은 입력은 언제나 같은 결과를 낸다. 단어 규칙이라 놓치거나 과하게 잡을 수 있으므로 근거 문구와 출처(source)를 함께 남긴다.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .analysis import TextAnalysis, mask_ranges
from .extract import _instruction_text, _quote_spans
from . import long_context, multi_turn
from .attachments import labels as attachment_labels
from .jev_criteria import CRITERIA
from .semantic_items import ITEMS, SUBS

__all__ = ["JEV_APPLICABILITY_VERSION", "APPLIES_WHEN", "DATA_RULES", "jev_applicability",
           "SUB_APPLICABILITY_VERSION", "SUB_RULES", "SUB_NOT_APPLICABLE", "sub_applicability",
           "J5_ITEMS_APPLICABILITY_VERSION", "FACT_QUESTION_RULES", "INPUT_VALUE_RULES", "VERIFICATION_CLAIM_RULES",
           "fact_question", "j5_items_applicability"]

JEV_APPLICABILITY_VERSION = "jev-applicability-v2.7-attachments"   # v2.7: J8 첨부파일 활용 · v2.6: J7 272K 이상 확정 · v2.5: J7 250K 이상(테스트 기간)·J6 멀티턴 길이 조건 · v2.4: J7 추가
#: 세부 기준(27개) 적용 규칙 버전 — items 파이프라인용. legacy RULES·jev_applicability() 결과는 바꾸지 않는다.
SUB_APPLICABILITY_VERSION = "sub-applicability-v5"   # v5: J8-1·J8-2·J8-3 · v4: J7 272K 이상 · v3: J7 250K 이상·J6 멀티턴 길이 조건 · v2: J7-1·J7-2
#: items 파이프라인의 J5 세부 기준 적용 규칙 버전 (질문 기반 — 네 모델의 분모가 같다)
J5_ITEMS_APPLICABILITY_VERSION = "j5-items-applicability-v1"

_AUDIENCE = r"(?:초등학생|중학생|고등학생|대학생|어린이|고객|팀장|상사|동료|사용자|친구|동기|초보자|부모님)"

# --- 규칙 패턴 상수 — legacy RULES 와 세부 기준 SUB_RULES 가 같은 패턴을 쓴다 ------------------------
# RULES 의 패턴 문자열은 상수를 이어 붙인 결과가 이전 리터럴과 글자 단위로 같아야 한다 (정규식 결과 불변).
_P_AUDIENCE = rf"{_AUDIENCE}(?:에게|한테|도|이|가|을|를|용)|면접"                                   # J2-1 대상
_P_LEVEL = r"쉽게\s*(?:설명|알려|풀어)|이해할\s*수\s*있게"                                         # J2-1 수준
_P_TONE = r"자연스럽|정중|격식|말투|어조|친근|유쾌"                                                # J2-2 말투·어조
_P_JARGON = r"기술\s*용어"                                                                         # J2-1 용어
_P_CONSTRAINT = (r"(?:하|쓰|넣|약속하|사용하)지\s*(?:마|말)|않고|않으면서|않도록|않게|말고|만\s*사용|만\s*넣|이\s*정보만|"
                 r"이\s*경험만|주어지지\s*않은|최소화")                                             # J2-3 제약
_P_METHOD = r"(?<!\S)(?!기준)\S+(?:으로|로)\s*설명해\s*줘|비유|예시를\s*들어|연결해서\s*설명|상황을\s*기준|상황에\s*비유"   # J2-4
_P_PURPOSE = (r"(?:안내문|메시지|답글|카드|광고\s*문구|문구|회고|일기|동화|산문|줄거리|이야기|답변|글)(?:의|을|를|으로|로|에)?\s*"
              r"(?:\S+\s*){0,2}(?:작성|써|만들)")                                                   # J2-5.b · J4-3 글 작성
_P_RECOMMEND = r"추천|제안"                                                                        # J2-5.a
_P_WHY = r"이유|왜|원리|까닭|원인(?:은|을)|어떻게\s*\S+\s*(?:수\s*있|유지)"                          # J3-3
_P_REASONING = r"추론|맞을까|단정|일반화|볼\s*수\s*있는지|믿어도\s*되는지|설명할\s*수\s*있는지|문제가\s*생길|조건을\s*만족"   # J3-2
_P_SOLVE = r"계산|구해|풀어|몇\s*(?:번|초|m|L|가지|개|시간|명|권)|얼마(?:야|인지)|평균\s*(?:속력|점수)"   # J3-4
_P_COMPARE = (r"비교|(?:어떻게|무엇이|어떤\s*점(?:이|에서))\s*달라|다른\s*(?:점|변화)|차이(?:는|가)?\s*(?:뭐|무엇|있어)|"
              r"어떤\s*\S*\s*차이|구분해|구분하고|구분할")                                          # J4-1.a
_P_EXTRA_CORE = r"\S+도\s*(?:설명|알려|짚어|구해|만들어|포함|구분|함께)|각각|장단점|포함해|포함한"
_P_EXTRA = _P_EXTRA_CORE + r"|기준으로"                                                           # J4-2 (legacy)
_P_HOWTO = r"방법|(?:어떤|작업|이동)\s*순서|순서를\s*정해|단계|절차|과정을\s*보여|과정도"             # J4-3
_P_SUMMARY = r"요약"                                                                               # J4-4

#: (규칙 이름, 패턴, 이 질문에서 볼 것) — 질문의 요청 문장에서 찾는다 (legacy 기준 단위, 값 불변)
RULES: Dict[str, Tuple[Tuple[str, str, str], ...]] = {
    "J2": (
        ("대상", _P_AUDIENCE, "질문이 정한 대상에게 맞는 내용인지"),
        ("말투·수준", f"{_P_LEVEL}|{_P_TONE}|{_P_JARGON}", "질문이 정한 말투·수준에 맞는지"),
        ("제약", _P_CONSTRAINT, "질문이 정한 제약을 지켰는지"),
        ("설명 방식", _P_METHOD, "질문이 정한 방식(관점·비유·예시)으로 설명했는지"),
        ("글의 목적", _P_PURPOSE, "글의 목적과 받는 사람에 맞는 내용인지"),
        ("추천·제안", _P_RECOMMEND, "추천·제안이 질문의 상황에 맞는지"),
    ),
    "J3": (
        ("이유·원리", _P_WHY, "이유·원리 설명의 인과가 맞는지"),
        ("추론·판단", _P_REASONING, "판단과 그 근거가 논리적으로 맞는지"),
        ("풀이", _P_SOLVE, "풀이 과정과 결론이 논리적으로 이어지는지"),
    ),
    "J4": (
        ("비교", _P_COMPARE, "비교 대상과 비교 기준을 모두 다뤘는지"),
        ("추가 요구", _P_EXTRA, "질문이 함께 요구한 내용을 빠짐없이 다뤘는지"),
        ("방법·절차", _P_HOWTO, "방법·절차의 핵심 단계가 빠지지 않았는지"),
        ("요약", _P_SUMMARY, "원문의 핵심 정보가 빠지지 않았는지"),
    ),
}

# --- 세부 기준 규칙 (items 파이프라인) — 설계 기준표의 적용 조건을 세부 기준 ID 와 1:1 로 잇는다 ---------------
#: 설계 예시의 대상 표현('70대 어머니가', '시니어 백엔드 개발자들에게')까지 잡도록 legacy 대상 목록을 넓힌 것
_P_AUDIENCE_SUB = (_P_AUDIENCE + r"|(?:어머니|아버지|할머니|할아버지|아이|자녀|아들|딸|청중|신입|입주민|회원|모임원|팀원|직원|"
                   r"개발자|엔지니어|학생|전문가|초보)(?:들)?(?:에게|한테|께|도|이|가|을|를|용)")
_P_LEVEL_SUB = _P_LEVEL + r"|이해하실\s*수\s*있게|알아듣|" + _P_JARGON
_P_TONE_SUB = _P_TONE + r"|반말|존댓말|높임말|경어"                                                 # 설계 예시 '편한 반말로'
_P_RECOMMEND_SUB = _P_RECOMMEND + r"|뭐가\s*좋을|뭐가\s*나을|어떤\s*(?:게|것이)\s*좋을|골라\s*줘|골라줘"   # 설계 예시 '뭐가 좋을까?'
_P_JUDGE = _P_RECOMMEND + r"|골라|고르|어느\s*(?:쪽|것)|뭐가\s*(?:나을|좋을)|" + _P_REASONING        # J3-2 추천·선택·판단
#: 이유·원리를 '묻는' 표현만 — '퇴사 이유는 쓰지 말고' 같은 조건 문장의 '이유'는 잡지 않는다 (J3-3 세부·J5-1 원리·이유 공용)
_P_WHY_SUB = r"왜|원리|(?:원인|이유|까닭)(?:[은는이가을를])?(?:\s*\S+){0,3}?\s*(?:뭐|무엇|알려|설명|말해|궁금|일까|인지|인가|좀)"
_P_SOLVE_SUB = _P_SOLVE + r"|풀이\s*(?:과정|방법)|얼마나\s*걸리"                                  # 설계 예시 '풀이 과정과 함께'
_P_PLAN = r"(?:계획|일정|스케줄|절차|동선)(?:을|를|표를|표)?\s*(?:짜|세워|만들|정리|작성|잡아|제안)|순서를\s*정해|짜\s*줘|짜줘"   # J3-5.a
_P_VALUE_ROLE = r"예산|상한|한도|실제(?:로)?\s*쓴|예상\s*지출|목표"                                 # J3-5.b
_P_COMPARE_BASIS = r"기준으로|장단점|측면에서|관점에서"                                            # J4-1.b
_P_EXTRA_SUB = _P_EXTRA_CORE + r"|\S+도\s*(?:들어|넣어|적어|써|추가)|(?:알려|설명해|써|만들어)\s*주고"   # 설계 예시 '보상 예시도 들어 줘'
_P_DEICTIC = r"그거|그것|그때|아까|방금|앞서\s*말한|(?:첫|두|세|네)\s*번째\s*(?:거|것|항목)"          # J6-2.c 지시어

#: (세부 기준 ID, 규칙 이름, 패턴, 이 규칙이 켜는 항목 ID) — 질문의 요청 문장에서 찾는다. J1·J3-1 은 항상, J5 는
#: ``j5_items_applicability``, J6 는 대화 판정이 맡으므로 여기 없다.
SUB_RULES: Tuple[Tuple[str, str, str, Tuple[str, ...]], ...] = (
    ("J2-1", "대상", _P_AUDIENCE_SUB, ("J2-1.a", "J2-1.b")),
    ("J2-1", "수준", _P_LEVEL_SUB, ("J2-1.a", "J2-1.b")),
    ("J2-2", "말투", _P_TONE_SUB, ("J2-2.a",)),
    ("J2-3", "제약", _P_CONSTRAINT, ("J2-3.a",)),
    ("J2-4", "설명 방식", _P_METHOD, ("J2-4.a", "J2-4.b")),
    ("J2-5", "추천·제안", _P_RECOMMEND_SUB, ("J2-5.a",)),
    ("J2-5", "글 작성", _P_PURPOSE, ("J2-5.b",)),
    ("J3-2", "추천·선택·판단", _P_JUDGE, ("J3-2.a", "J3-2.b")),
    ("J3-3", "이유·원리", _P_WHY_SUB, ("J3-3.a", "J3-3.b")),
    ("J3-4", "풀이", _P_SOLVE_SUB, ("J3-4.a",)),
    ("J3-5", "계획·일정·절차", _P_PLAN, ("J3-5.a",)),
    ("J3-5", "수치 역할", _P_VALUE_ROLE, ("J3-5.b",)),
    ("J4-1", "비교", _P_COMPARE, ("J4-1.a",)),
    ("J4-1", "비교 기준", _P_COMPARE_BASIS, ("J4-1.b",)),      # 비교 요청(J4-1.a)이 있을 때만 유효
    ("J4-2", "추가 요구", _P_EXTRA_SUB, ("J4-2.a",)),
    ("J4-3", "방법·절차", _P_HOWTO, ("J4-3.a",)),
    ("J4-3", "글 작성", _P_PURPOSE, ("J4-3.a",)),
    ("J4-4", "요약", _P_SUMMARY, ("J4-4.a",)),
)

#: 세부 기준이 적용되지 않을 때의 이유 (화면·기록용)
SUB_NOT_APPLICABLE: Dict[str, str] = {
    "J2-1": "요청 문장에 대상·받는 사람·수준 표현이 없음",
    "J2-2": "요청 문장에 말투·어조 지정이 없음",
    "J2-3": "요청 문장에 조건·제약 표현이 없음",
    "J2-4": "요청 문장에 설명 방식 지정이 없음",
    "J2-5": "요청 문장에 추천·제안·글 작성 요청이 없음",
    "J3-2": "요청 문장에 추천·선택·판단 요구가 없음",
    "J3-3": "요청 문장에 이유·원리 요구가 없음",
    "J3-4": "요청 문장에 풀이 요구가 없음",
    "J3-5": "요청 문장에 계획·일정·절차 요청이나 역할 있는 수치가 없음",
    "J4-1": "요청 문장에 비교 요구가 없음",
    "J4-2": "요청 문장에 함께 요구한 항목 표현이 없음",
    "J4-3": "요청 문장에 방법·절차·글 작성 요청이 없음",
    "J4-4": "요청 문장에 요약 요구가 없음",
    "J6": "대화 입력이 아님 — 단일 질문·답변",
    "J7": f"입력이 {long_context.MIN_TOKENS:,}토큰 미만 — 롱컨텍스트가 아님",
    "J8": "질문에 첨부파일이 없음",
}

#: 단위·기호 — 긴 표기를 먼저 둬서 'mm' 이 'm' 으로 잡히지 않게 한다. 개·건·회·번 같은 순서·개수 낱말은 데이터로 보지 않는다.
#: '도' 는 조사('403도 서버')와 헷갈려 소수점이 있는 각도(23.5도)만 잡는다. '3분의 1' 같은 분수는 단위로 보지 않는다.
_UNIT = (r"%|퍼센트|달러|유로|원|엔|km|cm|mm|m|kg|mg|g|mL|ml|L|℃|°C|배|명|년|월|일|주|시간|분|초|세|kcal|GB|MB|TB|Hz|W|V|"
         r"㎡|평|인치|px|점|위")
_NUMBER = r"\d[\d,]*(?:\.\d+)?"

#: (규칙 이름, 패턴, 볼 것) — **답변**(코드 제외)에서 찾는다
DATA_RULES: Tuple[Tuple[str, str, str], ...] = (
    ("수치·단위", rf"{_NUMBER}\s*(?:만|억|조)?\s*(?:{_UNIT})(?![A-Za-z]|의)|\d+\.\d+\s*도(?![A-Za-z가-힣])",
     "답변이 제시한 수치가 실제·주어진 데이터와 맞는지"),
    ("연도·날짜", r"(?:1[0-9]|20)\d{2}년|\d{1,2}월\s*\d{1,2}일|\d{4}[-./]\d{1,2}[-./]\d{1,2}", "답변이 제시한 연도·날짜가 실제와 맞는지"),
    ("통계·출처", r"에\s*따르면|통계(?:청|\s*자료|\s*수치|치|상)|(?:조사|연구)\s*결과[^\n\d]{0,12}\d|공식\s*(?:발표|자료|통계)|평균\s*\d|약\s*\d",
     "통계·출처를 든 주장이 실제 자료와 맞는지"),
)

# --- J5 질문 규칙 (items 파이프라인) — 답변이 아니라 질문으로 정해 네 모델의 분모가 같다 ---------------------
_SCENARIO = re.compile(r"가상|허구|가정(?:할|해|한|으로|이야|해줘)")
_EXTERNAL = re.compile(r"실제\s*(?:시장|가격|요금|통계|정책|법|규정|제도)|현실에서|공식\s*(?:통계|자료|발표)|국내|전국|전\s*세계")
_CODE_TASK = re.compile(r"(?:함수|코드|스크립트|SQL|쿼리|HTML|CSS|수식|정규식|프로그램)(?:을|를|로|으로|만)?\s*(?:\S+\s*){0,3}(?:작성|만들|써|짜|구현)")
#: 순수 풀이 요청 — '인구가 얼마인지' 같은 정보 질의와 겹치지 않도록 '얼마야·얼마인지' 는 넣지 않는다
_CALC_ONLY = re.compile(r"계산해|구해|풀어|몇\s*개야")
#: (규칙 이름, 패턴, 볼 것) — 요청 문장(_request_sentences)에서 찾는다 → J5-1 사실 정확성
FACT_QUESTION_RULES: Tuple[Tuple[str, str, str], ...] = (
    ("원리·이유", _P_WHY_SUB + r"|어떻게\s*(?:작동|동작|생기|일어나|전달|되)", "설명한 원리·개념이 사실과 맞는지"),
    ("개념·정의", r"(?:뜻|의미|정의|개념|차이|유래|역사|약점|한계)(?:[은는이가을를]|\s)|(?:이|란|이란)\s*(?:뭐|무엇)", "개념 정의·명칭이 사실과 맞는지"),
    ("정보 질의", r"언제|어디(?:서|에|야)|누구|몇\s*(?:년|월|일|명|개국|살)|(?:인구|수도|면적|가격|요금|기간|기한|날짜|연도)(?:[은는이가을를]|\s)",
     "수치·날짜·명칭이 사실과 맞는지"),
)
#: 질문 전체(인용 자료 포함)에서 찾는 '질문이 준 값' — J5-2.a 값별 판정의 적용 근거
INPUT_VALUE_RULES: Tuple[Tuple[str, str], ...] = (
    ("수치", rf"{_NUMBER}\s*(?:만|억|조)?\s*(?:{_UNIT})(?![A-Za-z]|의)"),
    ("날짜·시각", r"\d{1,2}월\s*\d{1,2}일|(?:1[0-9]|20)\d{2}년|(?:오전|오후)\s*\d{1,2}시(?:\s*\d{1,2}분)?|\d{1,2}:\d{2}"),
    ("이름·명칭", r"[가-힣]{2,4}\s+(?:과장|팀장|부장|대리|사원)(?:님)?|[가-힣]{2,4}\s+(?:님|씨)(?![가-힣])|[\"“「『][^\"”」』\n]{1,40}[\"”」』]"),
)
_SUMMARY = re.compile(r"요약|정리해|핵심만|간추려")
_MATERIAL = re.compile(r"다음|아래|원문|공지|메모|자료|문서|알림장|회의|[\"“「『]")
#: 답변에서 찾는 확인·조회·출처 언급 — J5-3.a (답변에서 생긴 항목)
VERIFICATION_CLAIM_RULES: Tuple[Tuple[str, str], ...] = (
    ("조회·실행", r"(?:조회|확인|검색|실행|처리|접수|등록)(?:해\s*보니|한\s*결과|했습니다|해\s*두었|해\s*드렸|했더니|을\s*넣어)"),
    ("출처·문헌", r"https?://\S+|출처|참고\s*문헌|문헌|논문|보고서에\s*따르면|제\s*\d+\s*조|\[\d{1,3}\]"),
)

#: 화면·보고서에 보여 줄 적용 조건
APPLIES_WHEN = {
    "J1": "모든 질문",
    "J2": "요청 문장에 대상·말투·수준·제약·설명 방식·글의 목적·추천 표현이 있을 때",
    "J3": "요청 문장에 이유·원리·추론·판단·풀이 요구가 있을 때",
    "J4": "요청 문장에 비교·추가 요구·방법·절차·요약 요구가 있을 때",
    "J5": "답변에 수치·날짜·통계 데이터가 있거나 참조 데이터가 있을 때 (코드 안 숫자 제외)",
    "J6": "멀티턴 대화(10K 토큰 이상 턴이 10개 이상)의 2번째 턴부터 — 앞 턴 대화를 함께 보냄",
    "J7":f"입력(현재 질문 + 참조 자료)이 {long_context.MIN_TOKENS:,}토큰 이상일 때 ({long_context.TOKENIZER})",
    "J8": "질문에 첨부파일(이미지·TXT·DOCX·PDF 등)이 있을 때",
}
_NOT_APPLICABLE = {
    "J2": "요청 문장에 대상·말투·제약·설명 방식·글의 목적을 정한 표현이 없음",
    "J3": "요청 문장에 이유·원리·추론·풀이를 요구하는 표현이 없음",
    "J4": "요청 문장에 비교·추가 요구·방법·요약을 요구하는 표현이 없음",
    "J5": "답변에 수치·날짜·통계 데이터가 없음 (참조 데이터도 없음)",
}
_REQUEST_END = re.compile(r"(?:\?|줘|주세요|마|말아|해|하자|봐)[.?!]*\s*$")
_LIST_NUMBER = re.compile(r"^[ \t]*\d+[.)][ \t]", re.M)


def _request_sentences(question: str) -> List[str]:
    qa = TextAnalysis(question)
    text = _instruction_text(qa)
    text = mask_ranges(text, _quote_spans(text))
    # 숫자 사이 소수점은 문장 경계가 아니다. 그 앞의 대상·제약도 같은 요청에 남긴다.
    sentences = (m.group(0).strip() for m in re.finditer(r"(?:[^.?!\n]|(?<=\d)\.(?=\d))+[.?!]*", text))
    return [s for s in sentences if s and _REQUEST_END.search(s)]


def _clip(sentence: str, start: int, end: int, margin: int = 22) -> str:
    """근거 문구 주변을 잘라 보여 준다. 낱말 중간에서 끊지 않도록 띄어쓰기 위치에 맞춘다."""
    left, right = max(0, start - margin), min(len(sentence), end + margin)
    if left:
        space = sentence.rfind(" ", 0, left)
        left = space + 1 if space >= 0 else 0
    if right < len(sentence):
        space = sentence.find(" ", right)
        right = space if space >= 0 else len(sentence)
    return ("…" if left else "") + sentence[left:right].strip() + ("…" if right < len(sentence) else "")


def _data_text(answer: str) -> str:
    """답변에서 코드(펜스·인라인)와 목록 번호를 가린 본문 (위치 보존)."""
    aa = TextAnalysis(answer)
    text = mask_ranges(answer, aa.code_exclusion_ranges)
    return mask_ranges(text, [(m.start(), m.end()) for m in _LIST_NUMBER.finditer(text)])


def _answer_data_evidence(answer: str, per_rule: int = 3) -> Optional[Dict[str, Any]]:
    """답변에서 찾은 데이터 근거. 규칙마다 서로 다른 값을 최대 ``per_rule`` 개 남기고, 발췌는 첫 값이 있는 줄이다."""
    text = _data_text(answer)
    rule_names, matches, looks, excerpt = [], [], [], None
    for name, pattern, look in DATA_RULES:
        found: List[str] = []
        for m in re.finditer(pattern, text):
            value = m.group(0).strip()
            if value not in found:
                found.append(value)
            if excerpt is None:
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.end())
                line_end = len(text) if line_end < 0 else line_end
                excerpt = _clip(answer[line_start:line_end], m.start() - line_start, m.end() - line_start)
            if len(found) >= per_rule:
                break
        if found:
            rule_names.append(name)
            matches.extend(found)
            looks.append(look)
    if not rule_names:
        return None
    return {"rules": rule_names, "matches": matches, "excerpt": excerpt, "look_for": " · ".join(looks), "source": "answer"}


def _reference_excerpt(reference: Any) -> str:
    text = reference if isinstance(reference, str) else json.dumps(reference, ensure_ascii=False)
    text = " ".join(text.split())
    return text[:60] + ("…" if len(text) > 60 else "")


def jev_applicability(question: str, answer: Optional[str] = None, *, reference: Any = None, attachments: Any = None,
                      conversation: Optional[Dict[str, Any]] = None,
                      measured: Optional[Tuple[Optional[Dict[str, Any]], Optional[str]]] = None) -> Dict[str, Any]:
    """적용할 J 기준과 근거. {"criteria": [...], "evidence": {J: {...}}, "not_applicable": {J: 이유}}

    - ``answer`` 가 있으면 J5 를 답변의 데이터로 판정한다. ``reference`` 가 있으면 J5 는 항상 적용한다.
    - ``conversation`` = {"id", "turn", "history_turns"} 이면 J6 를 2번째 턴부터(앞 턴 대화가 있을 때) 적용한다.
    evidence 의 ``source`` 는 근거를 찾은 곳(question · answer · reference · conversation)이다.
    ``measured`` 는 ``long_context_size`` 결과 — 같은 질문의 적용 판정을 여러 번 할 때 길이 측정을 공유한다.
    ``attachments`` 가 있으면 J8 을 적용한다 (``code_eval.attachments``).
    """
    sentences = _request_sentences(question)
    last = sentences[-1] if sentences else question.strip()
    evidence: Dict[str, Dict[str, Any]] = {
        "J1": {"rules": ["요청"], "matches": [], "excerpt": _clip(last, 0, min(len(last), 60), 0),
               "look_for": "질문의 요청을 실제로 수행했는지", "source": "question"}}
    for cid, rules in RULES.items():
        rule_names, matches, looks, excerpt = [], [], [], None
        for name, pattern, look in rules:
            for sentence in sentences:
                m = re.search(pattern, sentence)
                if m:
                    rule_names.append(name)
                    matches.append(m.group(0).strip())
                    looks.append(look)
                    excerpt = excerpt or _clip(sentence, m.start(), m.end())
                    break
        if rule_names:
            evidence[cid] = {"rules": rule_names, "matches": matches, "excerpt": excerpt, "look_for": " · ".join(looks),
                             "source": "question"}

    data = _answer_data_evidence(answer) if answer else None
    has_reference = reference is not None and reference != "" and reference != {} and reference != []
    if has_reference:
        evidence["J5"] = {"rules": ["참조 데이터"] + (data["rules"] if data else []),
                          "matches": ["reference"] + (data["matches"] if data else []),
                          "excerpt": _reference_excerpt(reference),
                          "look_for": "답변의 데이터·사실이 참조 데이터와 일치하는지", "source": "reference"}
    elif data:
        evidence["J5"] = data

    not_applicable = {cid: reason for cid, reason in _NOT_APPLICABLE.items() if cid not in evidence}
    undetermined: Dict[str, str] = {}
    size, reason = measured if measured is not None else long_context_size(question, reference)
    if size is None:
        undetermined["J7"] = reason   # 토크나이저 없음 — 길이를 모르므로 해당 없음으로 두지 않는다
    elif size["long"]:
        evidence["J7"] = {"rules": ["긴 입력"], "matches": [f"{size['tokens']:,}토큰"], "source": "reference" if has_reference else "question",
                          "excerpt": f"입력 {size['tokens']:,}토큰", "long_context": size,
                          "look_for": "긴 입력의 어디에 있든 질문이 묻는 정보를 찾고, 흩어진 근거를 빠짐없이 연결했는지"}
    else:
        not_applicable["J7"] = reason
    if conversation:
        turn, history_turns = int(conversation.get("turn") or 0), int(conversation.get("history_turns") or 0)
        issues = list(conversation.get("issues") or [])
        scale = conversation.get("multi_turn")
        if turn <= 1:
            not_applicable["J6"] = "대화의 첫 턴 — 앞 대화 없음"
        elif isinstance(scale, dict) and scale.get("multi_turn") is None:
            undetermined["J6"] = multi_turn.describe(scale)   # 토크나이저 없음·길이를 모르는 턴 — 해당 없음으로 두지 않는다
        elif isinstance(scale, dict) and scale.get("multi_turn") is False:
            not_applicable["J6"] = "멀티턴 대화가 아님 — " + multi_turn.describe(scale)
        elif issues:  # 앞 턴 누락·입력 오류·중복·길이 초과 — 불완전한 이력으로는 판정하지 않는다
            not_applicable["J6"] = "대화 이력이 불완전함 — " + ", ".join(issues)
        elif history_turns < 1:
            not_applicable["J6"] = "앞 턴 입력이 없거나 입력 오류여서 앞 대화를 만들 수 없음"
        else:
            evidence["J6"] = {"rules": ["대화"], "matches": [], "source": "conversation",
                              "excerpt": f"대화 {conversation.get('id')} · {turn}턴 (앞 턴 {history_turns}개)",
                              "look_for": "앞 대화의 지시·설정·문맥을 이 답변이 유지했는지"}
    names = attachment_labels(attachments)
    if names:
        evidence["J8"] = {"rules": ["첨부파일"], "matches": names, "source": "attachment", "excerpt": "첨부 " + " · ".join(names),
                          "look_for": "첨부파일의 내용을 실제로 읽고 형식에 맞게 해석해 답했는지, 읽지 못한 부분을 밝혔는지"}
    else:
        not_applicable["J8"] = SUB_NOT_APPLICABLE["J8"]
    criteria = [c.id for c in CRITERIA if c.id in evidence]
    result = {"version": JEV_APPLICABILITY_VERSION, "criteria": criteria, "evidence": evidence, "not_applicable": not_applicable}
    if undetermined:
        result["undetermined"] = undetermined
    return result


# --- items 파이프라인: J5 세부 기준 (질문 기반) ------------------------------------------------------------

def long_context_size(question: Any, reference: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(길이 측정, 해당 없음 사유). 롱컨텍스트면 사유는 None. 토크나이저가 없으면 판별하지 못했다는 사유를 남긴다."""
    try:
        size = long_context.measure(question, reference)
    except long_context.TokenizerUnavailable as exc:
        return None, f"롱컨텍스트를 판별하지 못함 — {exc}"
    if size["long"]:
        return size, None
    return size, f"입력 {size['tokens']:,}토큰 — {long_context.MIN_TOKENS:,}토큰 미만이라 롱컨텍스트가 아님"


def _history_questions(history: Any) -> List[str]:
    """대화 이력(state["history"] 모양)의 앞 턴 사용자 질문 원문 목록."""
    if not isinstance(history, list):
        return []
    return [h["question"] for h in history if isinstance(h, dict) and isinstance(h.get("question"), str)]


def fact_question(question: str, history: Any = None) -> Tuple[bool, str, List[Tuple[str, str]]]:
    """요청 문장이 사실 정보(원리·이유, 개념·정의, 수치·날짜·명칭)를 묻는지 — (적용 여부, 해당 없음 이유, [(규칙, 근거 문구)…]).

    가상·가정 설정(현재 질문이나 앞 턴 사용자 질문에 있고 실제 자료를 묻지 않는 한), 코드 결과물 요청(원리·이유 질문이
    아닌 한), 순수 풀이 요청은 제외한다. ``history`` 는 state["history"] 모양의 앞 턴 목록이다.
    """
    sentences = _request_sentences(question)
    hits: List[Tuple[str, str]] = []
    for name, pattern, _ in FACT_QUESTION_RULES:
        for sentence in sentences:
            m = re.search(pattern, sentence)
            if m:
                hits.append((name, m.group(0).strip()))
                break
    if not hits:
        return False, "요청 문장에 사실 질의 표현 없음", hits
    asks_why = any(name == "원리·이유" for name, _ in hits)
    scenario = _SCENARIO.search(question) or any(_SCENARIO.search(q) for q in _history_questions(history))
    if scenario and not _EXTERNAL.search(question):
        return False, "가상·가정 설정 — 질문이 준 값만 대조(J5-2)", hits
    if _CODE_TASK.search(question) and not asks_why:
        return False, "코드 결과물 요청 — 결정 전까지 J5-1 제외(정할 것)", hits
    if sentences and all(_CALC_ONLY.search(s) for s in sentences) and not asks_why:
        return False, "풀이 요청 — J3·코드 3-1", hits
    return True, "", hits


def j5_items_applicability(question: str, answer: Optional[str] = None, *, answer_tools: str = "unknown",
                           reference: Any = None, history: Any = None) -> Dict[str, Any]:
    """items 파이프라인의 J5 세부 기준 적용 — {"version", "subs": {"J5-1", "J5-2", "J5-3": {...}}, "criteria_applies"}.

    J5-1·J5-2 는 질문(과 앞 턴 사용자 질문의 가상 설정)만 보고 정한다(네 모델의 분모가 같다). J5-3.a 는 답변에서 생긴
    항목이라 answer 로 정하고 ``trigger="answer"`` 를 남기며, ``answer_tools="unknown"``(AIONA)이면 조회·실행 언급은
    적용 근거로 보지 않는다. J5-1 적용 결과에는 ``needs_search=True`` 를 남긴다 — 검색이 꺼진 설정의 처리는 transport 가 맡는다.
    """
    question = question or ""
    subs: Dict[str, Dict[str, Any]] = {}
    applies, why, hits = fact_question(question, history)
    if applies:
        sentences = _request_sentences(question)
        excerpt = None
        for sentence in sentences:
            m = re.search(re.escape(hits[0][1]), sentence)
            if m:
                excerpt = _clip(sentence, m.start(), m.end())
                break
        subs["J5-1"] = {"items": ["J5-1.a"], "source": "question", "rules": [h[0] for h in hits], "matches": [h[1] for h in hits],
                        "excerpt": excerpt or _clip(question.strip(), 0, min(len(question.strip()), 60), 0), "needs_search": True}
    else:
        subs["J5-1"] = {"items": [], "source": "question", "rules": [h[0] for h in hits], "matches": [h[1] for h in hits],
                        "not_applicable": why, "reason": why}
    values: List[str] = []
    value_rules: List[str] = []
    for name, pattern in INPUT_VALUE_RULES:
        found = [m.group(0).strip() for m in re.finditer(pattern, question)]
        found = [v for i, v in enumerate(found) if v not in found[:i]]
        if found:
            value_rules.append(name)
            values.extend(found)
    # '10월'·'24일' 처럼 더 긴 값('10월 24일')에 포함되는 조각은 뺀다
    values = [v for v in values if not any(v != other and v in other for other in values)][:8]
    summarize = bool(_SUMMARY.search(question) and _MATERIAL.search(question))
    has_reference = reference is not None and reference != "" and reference != {} and reference != []
    items2: List[str] = []
    rules2: List[str] = []
    if values or has_reference:
        items2.append("J5-2.a")
        rules2.extend(value_rules)
    if summarize:
        items2.append("J5-2.b")
        rules2.append("자료 정리·요약")
    if has_reference:
        rules2.insert(0, "참조 데이터")
    if items2:
        subs["J5-2"] = {"items": items2, "source": "reference" if has_reference else "question", "rules": rules2,
                        "matches": (["reference"] if has_reference else []) + values,
                        "excerpt": _reference_excerpt(reference) if has_reference else _clip(question, 0, min(len(question), 60), 0)}
    else:
        reason2 = "질문이 준 값·정리할 자료 없음"
        subs["J5-2"] = {"items": [], "source": "question", "rules": [], "matches": [], "not_applicable": reason2, "reason": reason2}
    text = _data_text(answer) if answer else ""
    marks: List[Tuple[str, str]] = []
    for name, pattern in VERIFICATION_CLAIM_RULES:
        m = re.search(pattern, text)
        if m:
            marks.append((name, m.group(0).strip()))
    if answer_tools != "none":
        marks = [m for m in marks if m[0] != "조회·실행"]   # 도구 사용 여부를 알 수 없는 답변: 조회·실행 부분은 해당 없음
    if marks:
        first = re.search(re.escape(marks[0][1]), text)
        line_start = text.rfind("\n", 0, first.start()) + 1 if first else 0
        line_end = text.find("\n", first.end()) if first else -1
        line_end = len(text) if line_end < 0 else line_end
        subs["J5-3"] = {"items": ["J5-3.a"], "source": "answer", "trigger": "answer", "answer_tools": answer_tools,
                        "rules": [m[0] for m in marks], "matches": [m[1] for m in marks],
                        "excerpt": _clip(answer[line_start:line_end], (first.start() - line_start) if first else 0,
                                         (first.end() - line_start) if first else 0)}
    else:
        reason3 = ("답변에 확인·조회·출처 언급 없음" if answer_tools == "none"
                   else "답변에 출처·문헌 언급 없음 (조회·실행 부분은 도구 사용 여부를 알 수 없어 해당 없음)")
        subs["J5-3"] = {"items": [], "source": "answer", "trigger": "answer", "answer_tools": answer_tools, "rules": [], "matches": [],
                        "not_applicable": reason3, "reason": reason3}
    return {"version": J5_ITEMS_APPLICABILITY_VERSION, "subs": subs,
            "criteria_applies": any(bool(s.get("items")) for s in subs.values())}


# --- items 파이프라인: 세부 기준 27개 적용 (질문 기반) ----------------------------------------------------

_SUB_ITEMS: Dict[str, List[str]] = {}
for _it in ITEMS:
    _SUB_ITEMS.setdefault(_it.sub, []).append(_it.id)
del _it


def _ordered(ids: List[str], sub_id: str) -> List[str]:
    return [item_id for item_id in _SUB_ITEMS[sub_id] if item_id in ids]


def sub_applicability(question: str, answer: Optional[str] = None, *, reference: Any = None, attachments: Any = None,
                      conversation: Optional[Dict[str, Any]] = None, answer_tools: str = "unknown",
                      history: Any = None,
                      measured: Optional[Tuple[Optional[Dict[str, Any]], Optional[str]]] = None) -> Dict[str, Dict[str, Any]]:
    """세부 기준 27개의 적용 여부와 근거 — {sub_id: {"items": [보낼 항목 ID…], "rules", "matches", "excerpt", "source", "reason"?}}.

    - J1-1·J1-2·J1-4·J3-1 은 항상, J1-3 은 평가 모델이 적용 여부를 정하므로 항상 보낸다(``model_applies``).
    - J2~J4 는 요청 문장의 단어 규칙(``SUB_RULES``)으로, J5 는 ``j5_items_applicability`` 로 정한다
      (``history`` 는 state["history"] 모양의 앞 턴 목록 — 앞 턴의 가상 설정을 J5-1 제외에 쓴다).
    - J6 는 legacy 와 같은 대화 판정(2번째 턴부터·앞 턴 이력 완전)을 재사용하고, J6-2.c 는 지시어 규칙이 있을 때만 켠다.
    - J7-1·J7-2 는 legacy J7 과 같은 길이 판정(입력 272K 토큰 이상, ``measured`` 를 주면 그 값)이고, 질문 유형에 따른 적용은
      평가 모델이 정한다.
    - J8-1·J8-2·J8-3 은 첨부파일(``attachments``)이 있을 때 보낸다. J8-1·J8-2 는 해당 여부를 평가 모델이 정한다.
    적용되지 않은 세부 기준은 ``items=[]`` 와 ``reason`` 을 남긴다. 답변은 J5-3.a 에만 영향을 준다 (질문이 정한다).
    """
    question = question or ""
    sentences = _request_sentences(question)
    last = sentences[-1] if sentences else question.strip()
    request_excerpt = _clip(last, 0, min(len(last), 60), 0)
    subs: Dict[str, Dict[str, Any]] = {}
    for sub in SUBS:
        sid = sub.id
        if sid in ("J1-1", "J1-2", "J1-4"):
            subs[sid] = {"items": list(_SUB_ITEMS[sid]), "rules": ["요청"], "matches": [], "excerpt": request_excerpt, "source": "question"}
        elif sid == "J1-3":
            subs[sid] = {"items": list(_SUB_ITEMS[sid]), "rules": ["평가 모델 판정"], "matches": [], "excerpt": request_excerpt,
                         "source": "question", "model_applies": True}
        elif sid == "J3-1":
            subs[sid] = {"items": list(_SUB_ITEMS[sid]), "rules": ["답변"], "matches": [], "excerpt": request_excerpt, "source": "answer"}
        elif sid.startswith(("J2", "J3", "J4")):
            rule_names: List[str] = []
            matches: List[str] = []
            enabled: List[str] = []
            excerpt = None
            for rule_sid, name, pattern, items in SUB_RULES:
                if rule_sid != sid:
                    continue
                for sentence in sentences:
                    m = re.search(pattern, sentence)
                    if m:
                        rule_names.append(name)
                        matches.append(m.group(0).strip())
                        enabled.extend(items)
                        excerpt = excerpt or _clip(sentence, m.start(), m.end())
                        break
            if sid == "J4-1" and "J4-1.a" not in enabled:
                enabled = []   # 비교 기준만 있고 비교 요청이 없으면 J4-1 은 적용하지 않는다
                rule_names, matches, excerpt = [], [], None
            if sid == "J4-2" and enabled and all(m == "장단점" for m in matches) and subs["J4-1"]["items"]:
                enabled = []   # 비교의 장단점은 J4-1.b 가 본다 ('판정하는 곳' 표)
                rule_names, matches, excerpt = [], [], None
            if enabled:
                subs[sid] = {"items": _ordered(enabled, sid), "rules": rule_names, "matches": matches, "excerpt": excerpt,
                             "source": "question"}
            else:
                subs[sid] = {"items": [], "rules": [], "matches": [], "excerpt": None, "source": "question",
                             "reason": SUB_NOT_APPLICABLE[sid]}
        # J5·J6 는 아래에서 채운다
    j5 = j5_items_applicability(question, answer, answer_tools=answer_tools, reference=reference, history=history)
    for sid in ("J5-1", "J5-2", "J5-3"):
        subs[sid] = dict(j5["subs"][sid])
    # J6: legacy 대화 판정 재사용 — 2번째 턴부터, 앞 턴 이력이 완전할 때만
    j6_reason: Optional[str] = None
    j6_excerpt = None
    j6_undetermined = False   # 토크나이저 없음 — 기록 단계에서 J6 를 확인 필요로 남긴다
    if not conversation:
        j6_reason = SUB_NOT_APPLICABLE["J6"]
    else:
        turn, history_turns = int(conversation.get("turn") or 0), int(conversation.get("history_turns") or 0)
        issues = list(conversation.get("issues") or [])
        scale = conversation.get("multi_turn")
        if turn <= 1:
            j6_reason = "대화의 첫 턴 — 앞 대화 없음"
        elif isinstance(scale, dict) and scale.get("multi_turn") is None:
            j6_reason, j6_undetermined = multi_turn.describe(scale), True
        elif isinstance(scale, dict) and scale.get("multi_turn") is False:
            j6_reason = "멀티턴 대화가 아님 — " + multi_turn.describe(scale)
        elif issues:
            j6_reason = "대화 이력이 불완전함 — " + ", ".join(issues)
        elif history_turns < 1:
            j6_reason = "앞 턴 입력이 없거나 입력 오류여서 앞 대화를 만들 수 없음"
        else:
            j6_excerpt = f"대화 {conversation.get('id')} · {turn}턴 (앞 턴 {history_turns}개)"
    size, j7_reason = measured if measured is not None else long_context_size(question, reference)
    for sid in ("J7-1", "J7-2"):
        if j7_reason is not None:
            subs[sid] = {"items": [], "rules": [], "matches": [], "excerpt": None, "source": "question", "reason": j7_reason}
            if size is None:
                subs[sid]["undetermined"] = True   # 토크나이저 없음 — 기록 단계에서 J7 을 확인 필요로 남긴다
        else:
            subs[sid] = {"items": list(_SUB_ITEMS[sid]), "rules": ["긴 입력"], "matches": [f"{size['tokens']:,}토큰"],
                         "excerpt": f"입력 {size['tokens']:,}토큰", "long_context": size,
                         "source": "reference" if reference not in (None, "", {}, []) else "question", "model_applies": True}
    names = attachment_labels(attachments)
    for sid in ("J8-1", "J8-2", "J8-3"):
        if not names:
            subs[sid] = {"items": [], "rules": [], "matches": [], "excerpt": None, "source": "attachment", "reason": SUB_NOT_APPLICABLE["J8"]}
        else:
            subs[sid] = {"items": list(_SUB_ITEMS[sid]), "rules": ["첨부파일"], "matches": names, "excerpt": "첨부 " + " · ".join(names),
                         "source": "attachment", **({"model_applies": True} if sid != "J8-3" else {})}
    for sid in ("J6-1", "J6-2", "J6-3", "J6-4"):
        if j6_reason is not None:
            subs[sid] = {"items": [], "rules": [], "matches": [], "excerpt": None, "source": "conversation", "reason": j6_reason}
            if j6_undetermined:
                subs[sid]["undetermined"] = True
            continue
        entry = {"items": [i for i in _SUB_ITEMS[sid] if i != "J6-2.c"], "rules": ["대화"], "matches": [], "excerpt": j6_excerpt,
                 "source": "conversation", "model_applies": True}
        if sid == "J6-2":
            deictic = None
            for sentence in sentences or [question]:
                deictic = re.search(_P_DEICTIC, sentence)
                if deictic:
                    break
            if deictic:
                entry["items"] = list(_SUB_ITEMS[sid])
                entry["rules"].append("지시어")
                entry["matches"].append(deictic.group(0).strip())
        subs[sid] = entry
    return {sub.id: subs[sub.id] for sub in SUBS}   # 기준표 순서
