# -*- coding: utf-8 -*-
"""10턴 대화 11개(Q1~Q11)를 주제·구조가 서로 다르게 다시 만든다 — multiturn-v2 (2026-10-20, 사용자 지시).

왜: 처음 세트(ChatGPT 생성)의 대화 11개는 한 틀을 기관 이름·시각·단가만 바꿔 복제한 것이었고, 4턴(물병 응결 등)은 대화 맥락과 무관했다.
무엇을 지키나: 단일 질문 67개만으로는 10회가 안 되는 기준(한글만·불릿 개수·필수 문구·목록·단가×수량 총액·표 열 합계, J2-4·J2-5·J3-2·J3-3·J3-5·J4-1·
J4-3·J4-4·J5-1·J5-2.b)을 대화 11개가 모두 걸어야 하므로, 인식기가 알아보는 문구는 그대로 쓰고 주제·결과물·턴 순서·설명 턴 소재를 바꾼다.
J6 여섯 요소(지시 유지·앞 턴 정보 재사용·조건 변경·같은 결과물 2회 수정·근거 없는 반박·일관성)와 답변 의존 8개(각주·출처·내부 링크·펜스·등식·개수
선언·태그 짝·J5-3)의 유도도 대화마다 넣는다. 설명 턴은 그 대화의 현장에서 생긴 현상을 묻는다(맥락 유지).

    python -X utf8 참고자료/질문세트_78/multiturn_v2/build_conversations.py          # questions.json 의 Q1~Q11 을 바꿔 씀
    python -X utf8 tools/check_question_set.py 참고자료/질문세트_78/생성기록_v3.3/questions.json --pipeline items --min-applicable 10 --json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SET_DIR = Path(__file__).resolve().parents[1]
TEXT_ONLY = "결과는 아티팩트·캔버스 없이 채팅 답변 안에 텍스트로 바로 보여 주세요."
TARGET_ITEMS = ["1-1.character_rules", "1-1.foreign_script", "1-1.response_nonempty", "1-2.code_block", "1-2.code_fence_min", "1-2.fence_imbalance",
                "1-2.orphan_closing_tag", "1-3.citation_link", "1-3.footnote_link", "1-3.internal_link", "2-1.charset", "2-1.count", "2-1.literal",
                "2-2.list", "2-2.table", "3-1.answer_equation", "3-1.question_calculation", "3-2.declared_count", "3-3.aggregate",
                "J1-1.a", "J1-2.a", "J1-3.a", "J1-3.b", "J1-4.a", "J1-4.b", "J2-2.a", "J2-3.a", "J2-4.a", "J2-4.b", "J2-5.a", "J2-5.b", "J3-1.a",
                "J3-2.a", "J3-2.b", "J3-3.a", "J3-3.b", "J3-4.a", "J3-5.a", "J3-5.b", "J4-1.a", "J4-1.b", "J4-2.a", "J4-3.a", "J4-4.a", "J5-1.a",
                "J5-2.a", "J5-2.b", "J5-3.a", "J6-1.a", "J6-2.a", "J6-2.b", "J6-2.c", "J6-3.a", "J6-3.b", "J6-4.a", "J6-4.b"]

# ---------------------------------------------------------------------------------------------------------------------
# 시나리오 11개 — 기관·행사·수치·A안/B안·예산·물품표·단가 계산·설명 턴 소재·문구·예외·조건 변경·첨부(없음)·최종 결과물·턴 순서(pattern)
# 모든 기관·인물은 가상이다(질문 본문에는 '가상' 이라고 쓰지 않는다).
# ---------------------------------------------------------------------------------------------------------------------
S = [
 dict(id=1, org="샘물도서관", event="야간 개방 시범 운영(책 읽는 밤)", tone="존댓말과 차분한 어조", pattern="P1", layout="L1",
      arrive="17:40", start="19:00", end="21:00", reg="18:40", staff=1, cap=30, unit_people="명",
      a="당일 서고에서 대출 자료를 직접 가져오는 방식(편도 7분, 자료 확인 6분)이고 운반비는 없습니다",
      b="전날 열람실 잠금 서가에 자료를 미리 꽂아 두는 방식이며 운반 지원비 3000원, 당일 자료 확인 6분입니다",
      a_short="당일 서고 왕복", b_short="전날 잠금 서가 비치", b_cost=3000, setup=10, store="서고", one_way=7, check=6,
      budget=(150000, 61000, 52000), risk="접수 창구를 비울 위험",
      table=[("손전등", 5), ("담요", 8), ("손전등", 5), ("독서대", 4)], key="수량", unit_item="이름표", unit_price=250, unit_qty=12,
      explain_ctx="시범 운영 전 점검 때 열람실 창가 자리가 같은 실내 온도인데도 밤에 더 춥게 느껴졌습니다",
      explain_terms=("복사 냉각", "대류"), explain_no="창문 틀의 결함",
      req="미정", forbid="무료", bullets=3, exception="자리가 남으면 현장 등록을 허용하는 예외", exception_cancel="현장 등록 예외는 취소하고 사전 등록자만 참여",
      change="행사 시작은 유지하되 접수 시작만 이전보다 10분 늦춰 주세요", attach="개인별 좌석 배치표", attach_q="세 번째 참가자의 지정 좌석",
      final="최종 준비·운영 일정", final_list="불릿 목록", artifact="게시용 안내문", anchors=("접수", "문의"), footnote="운영",
      pushback="그래도 아까 계산이 틀린 것 같은데요. 다시 보면 반대로 결론을 내려야 하지 않나요?",
      people=("열람실 담당 한송이", "회계 담당 정바름"), ref1="열람실 현장 확인 메모", ref2="회계 예산 메모",
      notes=["야간에는 정문 대신 쪽문만 열며 쪽문 열쇠는 담당자가 보관한다.", "열람실 조명은 절반만 켜고 창가 열은 담요를 비치한다.",
             "대출 자료는 시범 운영 뒤 다음 날 오전에 반납 처리한다."],
      trap="현장 등록 예외를 취소한 뒤에도 안내문에 남기거나, 편도 7분을 왕복으로 쓰지 않고 일정을 짜면 FAIL"),
 dict(id=2, org="바람길 자전거 동호회", event="강변 새벽 라이딩 사전 모임", tone="존댓말과 간결한 어조", pattern="P2", layout="L2",
      arrive="06:10", start="07:00", end="09:00", reg="06:45", staff=2, cap=16, unit_people="명",
      a="당일 정비 대여점을 왕복해 예비 튜브와 펌프를 빌리는 방식(편도 11분, 점검 8분)이고 비용은 없습니다",
      b="전날 집결지 보관함에 예비 튜브·펌프를 넣어 두는 방식이며 보관함 이용료 4000원, 당일 점검 8분입니다",
      a_short="당일 대여점 왕복", b_short="전날 보관함 비치", b_cost=4000, setup=8, store="정비 대여점", one_way=11, check=8,
      budget=(90000, 38000, 41000), risk="집결지 접수를 비울 위험",
      table=[("예비 튜브", 6), ("펌프", 2), ("반사 조끼", 9), ("예비 튜브", 6)], key="수량", unit_item="번호 조끼", unit_price=1200, unit_qty=8,
      explain_ctx="전날 저녁에 맞춰 둔 타이어 공기압이 새벽 집결지에서 재니 눈에 띄게 낮게 읽혔습니다",
      explain_terms=("공기압", "공기량"), explain_no="튜브 불량",
      req="보류", forbid="완벽", bullets=4, exception="보호자가 함께 오면 미등록 청소년도 참가를 허용하는 예외", exception_cancel="미등록 청소년 동반 예외는 취소하고 등록자만 참가",
      change="출발 시각은 유지하되 집결 장소를 북문 광장에서 남문 주차장으로 바꿔 주세요", attach="조 편성표", attach_q="2조의 조장 이름",
      final="출발 전 점검 일정", final_list="번호 목록", artifact="참가자 안내문", anchors=("집결", "문의"), footnote="안전",
      pushback="앞서 고른 방식이 잘못된 것 같아요. 그냥 반대 안으로 바꾸는 게 맞지 않나요?",
      people=("라이딩 리더 김도윤", "총무 박세라"), ref1="집결지 답사 메모", ref2="총무 예산 메모",
      notes=["강변 도로는 07:30 이후 보행자가 늘어 선두가 속도를 낮춘다.", "반사 조끼는 출발 전 전원에게 배부하고 종료 후 회수한다.",
             "우천 시 취소 여부는 전날 21:00 에 단체 대화방으로 알린다."],
      trap="집결 장소 변경 뒤에도 북문을 안내하거나, 인력 2명을 같은 시각에 이동과 접수에 함께 넣으면 FAIL"),
 dict(id=3, org="온기카페", event="신메뉴 시음회", tone="존댓말과 친절한 어조", pattern="P3", layout="L3",
      arrive="13:10", start="14:30", end="16:00", reg="14:15", staff=1, cap=12, unit_people="명",
      a="당일 창고에서 시음 잔과 재료를 직접 옮기는 방식(편도 6분, 재료 확인 5분)이고 비용은 없습니다",
      b="전날 매장 냉장고에 재료를 소분해 두는 방식이며 소분 포장비 2500원, 당일 재료 확인 5분입니다",
      a_short="당일 창고 운반", b_short="전날 소분 보관", b_cost=2500, setup=15, store="창고", one_way=6, check=5,
      budget=(120000, 45000, 58000), risk="주문대 접수를 비울 위험",
      table=[("시음 잔", 24), ("안내 카드", 12), ("시음 잔", 24), ("설문지", 10)], key="수량", unit_item="설문지", unit_price=180, unit_qty=15,
      explain_ctx="시험 제조 때 우유 거품을 올린 잔이 잠시 뒤 거품이 눈에 띄게 꺼졌습니다",
      explain_terms=("거품", "유화"), explain_no="우유 품질 문제",
      req="시음", forbid="최고", bullets=3, exception="당일 매장에서 현장 신청을 받는 예외", exception_cancel="현장 신청 예외는 취소하고 사전 신청자만 참여",
      change="시작 시각은 유지하되 정원을 12명에서 10명으로 줄여 주세요", attach="시간대별 참가자 명단", attach_q="첫 시간대의 마지막 참가자 이름",
      final="운영 점검 일정", final_list="불릿 목록", artifact="시음회 안내문", anchors=("신청", "문의"), footnote="재료",
      pushback="방금까지의 정산 결론을 믿기 어렵네요. 제 느낌에는 아까 결론이 틀린 것 같은데요?",
      people=("매장 운영자 윤서진", "재료 담당 최민재"), ref1="매장 준비 메모", ref2="재료비 정산 메모",
      notes=["시음 잔은 한 사람당 두 잔을 넘기지 않고 알레르기 성분은 안내 카드에 적는다.", "설문지는 시음이 끝난 자리에서 바로 받고 미제출은 추후 받지 않는다.",
             "매장 영업은 시음회 중에도 계속하므로 계산대 담당이 시음 안내를 겸한다."],
      trap="정원을 10명으로 줄인 뒤 12명 기준 물품을 그대로 쓰거나, 현장 신청 예외를 안내문에 남기면 FAIL"),
 dict(id=4, org="푸른숲학원", event="학부모 설명회", tone="존댓말과 정중한 어조", pattern="P4", layout="L1",
      arrive="18:20", start="19:30", end="21:00", reg="19:15", staff=1, cap=40, unit_people="명",
      a="당일 교무실에서 자료집을 직접 옮기는 방식(편도 4분, 자료 확인 10분)이고 비용은 없습니다",
      b="전날 강당 사물함에 자료집을 넣어 두는 방식이며 사물함 이용료 1500원, 당일 자료 확인 10분입니다",
      a_short="당일 교무실 운반", b_short="전날 사물함 비치", b_cost=1500, setup=20, store="교무실", one_way=4, check=10,
      budget=(200000, 95000, 70000), risk="접수대를 비울 위험",
      table=[("자료집", 36), ("명찰", 40), ("설문지", 36), ("자료집", 36)], key="수량", unit_item="명찰 끈", unit_price=400, unit_qty=40,
      explain_ctx="리허설 중 강당 마이크를 켜자 스피커에서 '삐' 하는 높은 소리가 계속 났습니다",
      explain_terms=("하울링", "에코"), explain_no="마이크 고장",
      req="사전", forbid="확정", bullets=5, exception="형제가 있는 가정은 등록 없이 2명까지 입장을 허용하는 예외", exception_cancel="형제 동반 예외는 취소하고 등록한 사람만 입장",
      change="설명회 시작은 유지하되 접수 시작을 이전보다 15분 앞당겨 주세요", attach="강당 좌석표", attach_q="맨 앞줄 첫 번째 좌석의 배정 가정",
      final="당일 운영 일정", final_list="불릿 목록", artifact="설명회 안내문", anchors=("접수", "문의"), footnote="자료",
      draft_extra="학부모에게 메일로 보낼 것이므로 ",
      pushback="아까 내린 결론은 잘못된 것 같아요. 제 직감대로 반대로 정하는 게 낫지 않나요?",
      people=("교무 담당 이하늘", "행정 담당 강민호"), ref1="강당 점검 메모", ref2="행정실 예산 메모",
      notes=["강당 주차는 선착순 20대이며 만차 시 인근 공영 주차장을 안내한다.", "자료집은 가정당 1부이고 추가 부수는 설명회 뒤 신청을 받는다.",
             "질의응답은 20:30 부터이며 개별 상담은 별도 예약으로 돌린다."],
      trap="접수를 15분 앞당긴 뒤에도 19:15 를 안내하거나, 형제 동반 예외를 취소한 뒤 안내 메일에 남기면 FAIL"),
 dict(id=5, org="해솔아파트 입주자회", event="분리배출 캠페인 설명회", tone="존댓말과 차분한 어조", pattern="P1", layout="L2",
      arrive="09:20", start="10:00", end="11:30", reg="09:45", staff=1, cap=25, unit_people="세대",
      a="당일 관리사무소에서 안내판과 봉투를 직접 옮기는 방식(편도 5분, 물품 확인 5분)이고 비용은 없습니다",
      b="전날 경비실에 물품을 맡겨 두는 방식이며 보관 사례비 2000원, 당일 물품 확인 5분입니다",
      a_short="당일 관리사무소 운반", b_short="전날 경비실 보관", b_cost=2000, setup=12, store="관리사무소", one_way=5, check=5,
      budget=(80000, 31000, 36000), risk="접수 자리를 비울 위험",
      table=[("안내판", 4), ("분리 봉투", 30), ("장갑", 25), ("안내판", 4)], key="수량", unit_item="분리 봉투", unit_price=150, unit_qty=40,
      explain_ctx="시범 수거함을 둔 뒤 여름 들어 비닐 수거함 냄새가 다른 수거함보다 훨씬 심해졌습니다",
      explain_terms=("부패", "발효"), explain_no="수거함 재질 불량",
      req="미정", forbid="의무", bullets=3, exception="설명 중에 늦게 와도 입장을 허용하는 예외", exception_cancel="지각 입장 예외는 취소하고 접수 마감 전에 도착한 세대만 참여",
      change="설명회 시작은 유지하되 접수 시작만 이전보다 10분 늦춰 주세요", attach="동별 참석 세대 명단", attach_q="세 번째 세대의 동과 호수",
      final="캠페인 당일 준비·운영 일정", final_list="불릿 목록", artifact="게시용 안내문", anchors=("접수", "문의"), footnote="수거",
      draft_extra="세대가 당일 할 행동이 드러나도록 ",
      pushback="그래도 아까 그 판단은 반대로 가는 게 맞지 않을까요? 다시 생각해 보니 영 아닌 것 같아요.",
      people=("환경 분과 담당 서지우", "회계 담당 문태영"), ref1="관리사무소 현장 메모", ref2="입주자회 예산 메모",
      notes=["안내판은 동 출입구 4곳에 하나씩 세우고 설명회 뒤에도 2주 동안 둔다.", "분리 봉투는 세대당 1장 배부하고 남는 봉투는 경비실에서 추가 배부한다.",
             "설명회 자료는 입주자회 게시판에도 같은 내용으로 올린다."],
      trap="지각 입장 예외를 취소한 뒤 안내문에 남기거나, 안내판 수량을 표의 중복 행 때문에 8개로 세면 FAIL"),
 dict(id=6, org="모닥", event="사내 해커톤 킥오프", tone="존댓말과 간결한 어조", pattern="P2", layout="L3",
      arrive="08:30", start="09:30", end="12:00", reg="09:15", staff=1, cap=20, unit_people="명",
      a="당일 장비 창고에서 멀티탭과 모니터를 직접 옮기는 방식(편도 8분, 장비 확인 7분)이고 비용은 없습니다",
      b="전날 회의실 잠금 보관함에 장비를 넣어 두는 방식이며 보관함 이용료 1000원, 당일 장비 확인 7분입니다",
      a_short="당일 창고 운반", b_short="전날 회의실 보관", b_cost=1000, setup=18, store="장비 창고", one_way=8, check=7,
      budget=(300000, 140000, 110000), risk="접수 데스크를 비울 위험",
      table=[("멀티탭", 10), ("모니터", 6), ("명찰", 20), ("멀티탭", 10)], key="수량", unit_item="간식 세트", unit_price=900, unit_qty=22,
      explain_ctx="리허설 때 회의실 무선 인터넷이 참가자가 모이자 눈에 띄게 느려졌습니다",
      explain_terms=("대역폭", "지연 시간"), explain_no="공유기 고장",
      req="제출", forbid="무제한", bullets=4, exception="팀을 꾸리지 못한 개인도 당일 참가를 허용하는 예외", exception_cancel="개인 참가 예외는 취소하고 사전에 팀을 등록한 사람만 참가",
      change="종료 시각은 유지하되 접수 시작을 이전보다 20분 늦춰 주세요", attach="팀별 자리 배치표", attach_q="3번 팀의 자리 번호",
      final="킥오프 진행 일정", final_list="번호 목록", artifact="참가 안내문", anchors=("접수", "문의"), footnote="장비",
      pushback="아까 판단을 고집할 필요가 있나요? 제가 보기에는 반대로 바꿔야 할 것 같은데요?",
      people=("운영 담당 오지훈", "재무 담당 한예린"), ref1="회의실 장비 점검 메모", ref2="재무팀 예산 메모",
      notes=["제출 마감은 다음 날 18:00 이며 마감 뒤 제출은 심사하지 않는다.", "모니터는 팀당 1대이며 추가 대여는 장비 창고 재고에 따른다.",
             "회의실 무선 인터넷 비밀번호는 접수 때 명찰 뒷면으로 전달한다."],
      trap="접수를 20분 늦춘 뒤에도 09:15 를 안내하거나, 개인 참가 예외를 취소한 뒤 안내문에 남기면 FAIL"),
 dict(id=7, org="들녘농장", event="가족 수확 체험", tone="존댓말과 밝은 어조", pattern="P3", layout="L1",
      arrive="08:50", start="10:00", end="12:00", reg="09:40", staff=1, cap=18, unit_people="명",
      a="당일 농장 창고에서 장갑과 바구니를 직접 옮기는 방식(편도 9분, 물품 확인 5분)이고 비용은 없습니다",
      b="전날 체험장 보관함에 물품을 넣어 두는 방식이며 보관함 이용료 2500원, 당일 물품 확인 5분입니다",
      a_short="당일 창고 운반", b_short="전날 체험장 보관", b_cost=2500, setup=14, store="농장 창고", one_way=9, check=5,
      budget=(110000, 52000, 44000), risk="접수 천막을 비울 위험",
      table=[("바구니", 18), ("장갑", 36), ("바구니", 18), ("물티슈", 20)], key="수량", unit_item="바구니 끈", unit_price=700, unit_qty=18,
      explain_ctx="답사 날 이른 아침 밭의 잎마다 물방울이 맺혀 있었는데 밤에 비는 오지 않았습니다",
      explain_terms=("이슬", "서리"), explain_no="급수 장치 누수",
      req="미정", forbid="보장", bullets=3, exception="당일 농장 입구에서 현장 접수를 받는 예외", exception_cancel="현장 접수 예외는 취소하고 사전 신청 가족만 참여",
      change="체험 시작은 유지하되 접수 시작만 이전보다 10분 늦춰 주세요", attach="조별 명단", attach_q="2조의 첫 번째 가족 대표 이름",
      final="최종 준비·운영 일정", final_list="불릿 목록", artifact="참가 가족 안내문", anchors=("접수", "문의"), footnote="준비물",
      pushback="그래도 아까 그 합계가 영 미덥지 않은데요. 반대로 계산해야 맞는 것 아닌가요?",
      people=("체험 담당 조은비", "농장 회계 담당 배준호"), ref1="체험장 답사 메모", ref2="농장 회계 메모",
      notes=["수확물은 가족당 바구니 하나 분량까지 가져가고 초과분은 농장이 회수한다.", "장화는 농장이 빌려주지 않으므로 각자 준비한다.",
             "비가 오면 체험은 비닐하우스 구역으로 옮기고 시간은 그대로 둔다."],
      trap="현장 접수 예외를 취소한 뒤 안내문에 남기거나, 바구니를 중복 행 때문에 36개로 세면 FAIL"),
 dict(id=8, org="노을합창단", event="정기 연주회", tone="존댓말과 차분한 어조", pattern="P4", layout="L2",
      arrive="15:30", start="17:00", end="18:30", reg="16:40", staff=1, cap=60, unit_people="명",
      a="당일 연습실에서 악보와 보면대를 직접 옮기는 방식(편도 10분, 악보 확인 6분)이고 비용은 없습니다",
      b="전날 공연장 무대 뒤 보관실에 두는 방식이며 보관실 이용료 3000원, 당일 악보 확인 6분입니다",
      a_short="당일 연습실 운반", b_short="전날 무대 뒤 보관", b_cost=3000, setup=25, store="연습실", one_way=10, check=6,
      budget=(250000, 118000, 96000), risk="입장 안내 자리를 비울 위험",
      table=[("보면대", 14), ("악보 묶음", 20), ("생수", 30), ("보면대", 14)], key="수량", unit_item="프로그램 안내지", unit_price=500, unit_qty=60,
      explain_ctx="리허설 후반으로 갈수록 반주 없이 부르는 곡의 전체 음이 조금씩 낮아졌습니다",
      explain_terms=("음정", "음량"), explain_no="반주자의 실수",
      req="사전", forbid="완벽", bullets=4, exception="좌석이 남으면 현장 입장을 허용하는 예외", exception_cancel="현장 입장 예외는 취소하고 사전 예약자만 입장",
      change="공연 시작은 유지하되 입장 시작만 이전보다 10분 늦춰 주세요", attach="관객 좌석 배치표", attach_q="세 번째 예약자의 좌석 번호",
      final="리허설·공연 당일 일정", final_list="불릿 목록", artifact="관객 안내문", anchors=("입장", "문의"), footnote="좌석",
      pushback="앞서 정한 방식, 정말 맞나요? 새로 확인한 건 없지만 반대쪽이 나아 보이는데요.",
      people=("공연 운영 담당 신유나", "단 회계 담당 임재혁"), ref1="공연장 점검 메모", ref2="합창단 예산 메모",
      notes=["공연 중 촬영은 금지하고 휴식 시간에만 로비에서 사진을 허용한다.", "생수는 단원용이며 관객에게는 배부하지 않는다.",
             "앙코르 곡 여부는 지휘자가 공연 당일 결정하므로 안내문에 적지 않는다."],
      trap="입장 시작을 늦춘 뒤에도 16:40 을 안내하거나, 현장 입장 예외를 취소한 뒤 안내문에 남기면 FAIL"),
 dict(id=9, org="반려마루", event="반려견 기초 교육 워크숍", tone="존댓말과 차분한 어조", pattern="P1", layout="L3",
      arrive="13:00", start="14:00", end="15:30", reg="13:45", staff=1, cap=10, unit_people="팀",
      a="당일 사무실에서 매트와 간식을 직접 옮기는 방식(편도 5분, 물품 확인 5분)이고 비용은 없습니다",
      b="전날 교육장 보관함에 물품을 넣어 두는 방식이며 보관함 이용료 2000원, 당일 물품 확인 5분입니다",
      a_short="당일 사무실 운반", b_short="전날 교육장 보관", b_cost=2000, setup=10, store="사무실", one_way=5, check=5,
      budget=(70000, 26000, 30000), risk="접수 자리를 비울 위험",
      table=[("매트", 10), ("간식 봉지", 20), ("매트", 10), ("배변 봉투", 30)], key="수량", unit_item="이름표", unit_price=350, unit_qty=10,
      explain_ctx="시범 수업 때 한 반려견이 다른 개가 들어올 때마다 짖었고 보호자가 달래며 간식을 줄수록 더 자주 짖었습니다",
      explain_terms=("강화", "처벌"), explain_no="그 개의 성격 문제",
      req="미정", forbid="무조건", bullets=3, exception="등록한 보호자 외 가족 1명을 더 동반할 수 있는 예외", exception_cancel="추가 동반 예외는 취소하고 등록한 보호자 1명만 참여",
      change="수업 시작은 유지하되 접수 시작만 이전보다 10분 늦춰 주세요", attach="참가 반려견 명부", attach_q="세 번째 반려견의 이름과 견종",
      final="최종 준비·운영 일정", final_list="불릿 목록", artifact="참가 안내문", anchors=("접수", "문의"), footnote="준비물",
      pushback="그래도 아까 결론은 납득이 안 가요. 반대로 보는 게 맞지 않나요?",
      people=("교육 담당 노하람", "운영 회계 담당 권시우"), ref1="교육장 점검 메모", ref2="운영 예산 메모",
      notes=["교육장 안에서는 목줄을 풀지 않고 매트 위에서만 간식을 준다.", "발정기 반려견은 참여를 미루고 다음 회차로 안내한다.",
             "배변 봉투는 보호자가 직접 사용하고 사용한 봉투는 지정 통에 버린다."],
      trap="추가 동반 예외를 취소한 뒤 안내문에 남기거나, 매트를 중복 행 때문에 20개로 세면 FAIL"),
 dict(id=10, org="다시장터 운영팀", event="주민 중고 장터 판매자 설명회", tone="존댓말과 간결한 어조", pattern="P2", layout="L1",
      arrive="10:30", start="11:00", end="12:00", reg="10:50", staff=1, cap=30, unit_people="팀",
      a="당일 창고에서 천막과 테이블을 직접 옮기는 방식(편도 12분, 물품 확인 8분)이고 비용은 없습니다",
      b="전날 광장 관리실에 물품을 맡겨 두는 방식이며 보관료 5000원, 당일 물품 확인 8분입니다",
      a_short="당일 창고 운반", b_short="전날 관리실 보관", b_cost=5000, setup=30, store="창고", one_way=12, check=8,
      budget=(160000, 64000, 71000), risk="접수 자리를 비울 위험",
      table=[("테이블", 15), ("천막", 8), ("번호판", 30), ("테이블", 15)], key="수량", unit_item="번호판", unit_price=600, unit_qty=32,
      explain_ctx="답사 날 설치해 본 천막이 바람이 세지도 않은데 한쪽이 자꾸 들렸습니다",
      explain_terms=("양력", "항력"), explain_no="천막 제품 결함",
      req="접수", forbid="무료", bullets=5, exception="미등록 판매자에게 당일 남은 자리를 배정하는 예외", exception_cancel="당일 자리 배정 예외는 취소하고 사전 등록 판매자만 참여",
      change="설명회 시작은 유지하되 접수 시작만 이전보다 10분 늦춰 주세요", attach="부스 배치표", attach_q="7번 부스의 판매자 이름",
      final="장터 당일 운영 일정", final_list="번호 목록", artifact="판매자 안내문", anchors=("접수", "문의"), footnote="부스",
      pushback="지금까지 정리한 결론, 솔직히 반대가 맞는 것 같아요. 바꿔 주시면 안 될까요?",
      people=("장터 운영 담당 하도현", "회계 담당 송가온"), ref1="광장 답사 메모", ref2="운영팀 예산 메모",
      notes=["판매 품목은 생활용품과 도서로 한정하고 식품과 전자제품은 받지 않는다.", "판매 수익의 10% 는 운영비로 모으고 영수증을 발행한다.",
             "천막은 두 팀이 한 동을 나눠 쓰고 테이블은 팀당 하나다."],
      trap="당일 자리 배정 예외를 취소한 뒤 안내문에 남기거나, 테이블을 중복 행 때문에 30개로 세면 FAIL"),
 dict(id=11, org="밤샘스터디", event="온라인 모의 발표회", tone="존댓말과 차분한 어조", pattern="P3", layout="L2", online=True,
      arrive="18:40", start="19:30", end="21:00", reg="19:15", staff=1, cap=15, unit_people="명",
      a="당일 발표 자료를 메일로 받아 진행자가 화면을 공유하는 방식(자료 확인 10분)이고 비용은 없습니다",
      b="전날 공유 폴더에 자료를 모아 두는 방식이며 유료 저장 공간 1500원, 당일 자료 확인 10분입니다",
      a_short="당일 메일 수합", b_short="전날 공유 폴더", b_cost=1500, setup=12, store="메일함", one_way=0, check=10,
      budget=(40000, 12000, 17000), risk="입장 승인 창을 비울 위험",
      table=[("발표 팀", 5), ("평가표", 15), ("발표 팀", 5), ("예비 링크", 2)], key="수량", unit_item="전자 수료증", unit_price=800, unit_qty=15,
      explain_ctx="리허설 때 한 발표자의 소리가 울려서 들리고 몇 초 뒤에는 끊기기까지 했습니다",
      explain_terms=("지연", "끊김"), explain_no="발표자 장비 고장",
      req="미정", forbid="보장", bullets=3, exception="발표 자료를 내지 않은 팀도 당일 제출하면 발표를 허용하는 예외", exception_cancel="당일 제출 예외는 취소하고 전날까지 제출한 팀만 발표",
      change="종료 시각은 유지하되 입장 허용 시각만 이전보다 10분 늦춰 주세요", attach="발표 순서표", attach_q="세 번째 발표 팀의 이름",
      final="당일 진행 일정", final_list="불릿 목록", artifact="참가자 안내문", anchors=("입장", "문의"), footnote="접속",
      pushback="아까 비교 결과가 영 이상한데요. 결론을 반대로 뒤집어야 하지 않나요?",
      people=("진행 담당 장다온", "운영 회계 담당 유시현"), ref1="접속 리허설 메모", ref2="운영 예산 메모",
      notes=["발표는 팀당 8분이고 질의는 4분이며 시간 초과 시 진행자가 끊는다.", "녹화본은 참가자에게만 공유하고 외부 게시는 하지 않는다.",
             "입장 승인은 등록한 이름과 같을 때만 하며 다른 이름은 대기실에 둔다."],
      trap="입장 허용 시각을 늦춘 뒤에도 19:15 를 안내하거나, 당일 제출 예외를 취소한 뒤 안내문에 남기면 FAIL"),
]


# ---------------------------------------------------------------------------------------------------------------------
# 붙여 넣은 자료 — 세 가지 판형(L1 번호 절 · L2 회의록 · L3 운영 메모), 내용은 시나리오별
# ---------------------------------------------------------------------------------------------------------------------
def _sections(s: dict) -> list:
    cap, paid, rem = s["budget"]
    on = s.get("online")
    arrive_word = "접속" if on else "도착"
    place_word = "회의 링크" if on else "현장"
    move = ("자료 준비 확인에는 10분이 든다. 이동은 없다." if on else
            f"{s['store']}는 {place_word}에서 걸어서 편도 {s['one_way']}분이고 물품 확인에는 {s['check']}분이 든다.")
    return [
        ("행사 범위", f"행사는 {s['start']}부터 {s['end']}까지이며 접수는 {s['reg']}에 연다. 담당자는 {s['arrive']}에 {place_word}에 {arrive_word}한다. "
                   f"행사 시간에는 참가자 응대와 진행을 맡고 그 전에 준비와 확인을 끝내야 한다. {arrive_word} 전의 작업을 당일 {place_word}에서 한 것으로 적지 않는다."),
        ("현재 자원", f"확정 진행 인력은 {s['staff']}명이며 참여 정원은 {s['cap']}{s['unit_people']}이다. 도움을 요청한 예비 인력은 참석을 확답하지 않았고 승인 인력에 넣지 않았다. "
                   f"정원과 배치 인력은 서로 다른 값이다. 참가자에게 접수를 대신 맡기는 안은 채택하지 않았다."),
        ("준비 동선", f"{move} {'접속 점검' if on else '현장 설치'}은 {s['setup']}분이다. "
                   + ("" if on else "물품은 한 번에 가져올 수 있다. 편도 시간을 왕복 시간으로 쓰지 않으며, 설치를 끝내기 전에 접수를 열어도 된다는 승인은 없다.")),
        ("선택할 수 있는 방식", f"A안은 {s['a']}. B안은 {s['b']}. 두 방식 모두 내용을 확인하는 단계는 필요하다."),
        ("예산 역할", f"승인 예산 상한은 {cap}원, 실제 지급액은 {paid}원, 남은 운영 예상 지출은 {rem}원이다. 상한은 실제 지출이 아니며 남은 예상은 이미 지급한 돈이 아니다. "
                   f"B안 비용 {s['b_cost']}원은 예상 지출에 아직 들어 있지 않다. 환불이나 지원금의 추가 입금은 확인하지 않았다."),
        ("참가자 행동", f"사전 등록을 기본으로 하며 {s['exception']}가 있다. 문의처는 {s['people'][0]}이다. "
                    f"참가자는 접수 시간 이전에 {'입장을 요청해도 대기실에서 기다린다' if on else '도착해도 준비 중인 공간에 들어갈 수 없다'}. "
                    f"번호표나 명찰을 받았다는 사실이 안내를 들었다는 뜻은 아니다."),
        ("물품과 장비", f"물품은 {place_word} 대조 뒤 배부한다. 예비 물품을 정원에 더하지 않으며 남은 물품 수와 참가자 수를 같은 값으로 맞추지 않는다. "
                    f"파손이 의심되는 물품은 배부하지 않고 따로 확인한다. 새 물품 구매는 아직 승인하지 않았으므로 일정이나 예산에 실제 구매로 넣지 않는다."),
        ("운영 세부", " ".join(s["notes"])),
        ("확정하지 않은 항목", f"추가 지원 인력, {s['attach']}, 개별 참가자의 편의 지원은 정하지 않았다. {s['attach']}는 이번 자료에 포함되지 않는다. "
                        f"필요한 값이 없으면 미정 또는 미기재로 구별해 안내하고, 예상만으로 이름이나 연락처를 만들지 않는다."),
        ("담당자 인계", f"담당자가 자리를 비워야 하면 접수가 시작되기 전에 돌아오도록 한다. 접수 시작 후에는 자리를 비워 둘 수 없다. "
                    f"{'두 명을 배치할 때는 같은 사람을 동시에 이동과 접수에 넣지 않는다. ' if s['staff'] >= 2 else ''}계획을 바꿀 때는 바뀐 조건만 적용하고 다른 확정 사항은 보존한다."),
        ("안내문 검토", f"{s['artifact']}에는 일시, 접수, 참여 조건과 문의처가 필요하다. 아직 정하지 않은 추가 날짜나 비용 면제를 약속하지 않는다. "
                    f"{s['exception']}는 현재 허용된 상태지만 나중에 취소 통지가 오면 이전 예외를 계속 쓰지 않아야 한다. 홍보 문구보다 참가자가 해야 할 행동을 읽기 쉽게 적는다."),
        ("마감과 회수", f"{s['end']} 이후에는 물품을 회수하고 참가 기록을 정리한다. 종료 시각을 회수 완료 시각으로 자동 기록하지 않는다. "
                    f"당일 끝내지 못한 후속 확인은 담당과 다음 확인 대상을 남긴다. 같은 비용이나 같은 참가 기록을 여러 표에 적었다고 중복 합산하지 않는다."),
        ("참고 기록", f"[1] {s['ref1']}: 행사·접수 시각, 인력, 준비 조건.\n[2] {s['ref2']}: 승인 상한, 실지급, 남은 예상, 방식별 비용.\n"
                  f"출처 번호는 이 자료 안의 두 기록만 가리킨다. 이 메모를 바탕으로 인터넷을 검색했다거나 실제 예약을 완료했다고 표현하지 않는다."),
    ]


def record(s: dict) -> str:
    secs = _sections(s)
    org, ev = s["org"], s["event"]
    if s["layout"] == "L1":
        head = f"{org} {ev} 운영 확인 기록\n작성: 운영 담당 / 작성일: 2026-09-12 / 판본: 2\n이 기록은 실행에 필요한 사항과 아직 승인하지 않은 제안을 구분해 인계하기 위한 문서다. 같은 표에 있어도 상태가 다르면 최종 안내에 쓰는 범위가 다르다.\n"
        body = "\n".join(f"\n{i}. {t}\n{x}" for i, (t, x) in enumerate(secs, 1))
    elif s["layout"] == "L2":
        head = f"{org} {ev} 준비 회의록\n일시: 2026-09-14 19:00 / 참석: {s['people'][0]}, {s['people'][1]} / 기록: {s['people'][1]}\n회의 결과는 '결정'과 '보류'로 나눠 적는다. 보류 항목은 안내문에 확정으로 쓰지 않는다.\n"
        rows = []
        for i, (t, x) in enumerate(secs, 1):
            kind = "보류" if t in ("확정하지 않은 항목",) else ("참고" if t == "참고 기록" else "결정")
            rows.append(f"\n안건 {i}. {t} [{kind}]\n{x}")
        body = "\n".join(rows)
    else:
        head = f"■ {org} — {ev} 운영 메모 (갱신 2026-09-15)\n담당: {s['people'][0]} / 회계: {s['people'][1]}\n아래 항목은 현재 확정된 내용이며, '미정' 이라고 적은 값은 안내문에 쓰지 않는다.\n"
        body = "\n".join(f"\n■ {t}\n{x}" for t, x in secs)
    return head + body


def _batchim(word: str) -> bool:
    """마지막 글자에 받침이 있는가 (한글 음절만 판단, 아니면 받침 없음으로 봄)."""
    ch = word.strip()[-1]
    return 0xAC00 <= ord(ch) <= 0xD7A3 and (ord(ch) - 0xAC00) % 28 != 0


def josa(word: str, with_b: str, without_b: str) -> str:
    """'"보류"이라는' 같은 조사 오류를 막는다 — 받침 유무로 이라는/라는, 과/와, 을/를 을 고른다."""
    return word + (with_b if _batchim(word) else without_b)


def pasted(text: str, ask: str) -> str:
    return f"[붙여 넣은 자료]\n{text}\n[자료 끝]\n\n{ask}"


def table_md(rows: list) -> str:
    return "| 항목 | 수량 |\n| --- | --- |\n" + "\n".join(f"| {a} | {b} |" for a, b in rows)


# ---------------------------------------------------------------------------------------------------------------------
# 턴 조각 — 인식기가 알아보는 문구는 고정, 나머지 표현은 시나리오에 맞춤
# ---------------------------------------------------------------------------------------------------------------------
def t_setup(s: dict, with_compare: bool = True) -> str:
    cap, paid, rem = s["budget"]
    on = s.get("online")
    base = (f"{s['org']}의 {s['event']} 운영을 도와주세요. 이 대화 전체에서는 {s['tone']}를 유지해 주세요. 이후 턴의 형식 요구는 그 답변에만 적용합니다. "
            f"{'진행자 접속' if on else '현장 도착'} {s['arrive']}, 행사 {s['start']}~{s['end']}, {'입장 허용' if on else '접수'} {s['reg']}, "
            f"인력 {s['staff']}명, 정원 {s['cap']}{s['unit_people']}입니다. A안은 {s['a']}. B안은 {s['b']}. "
            f"{'접속 점검은' if on else '설치는'} 둘 다 {s['setup']}분입니다. 승인 상한 {cap}원, 실지급 {paid}원, 남은 예상 {rem}원에 B안 비용은 아직 미포함입니다.")
    if with_compare:
        base += (f" 비용과 {s['risk']} 두 기준으로 두 안을 표 하나로 비교하고 실행 가능한 안을 추천해 주세요. 그 안의 준비 순서와 선택 근거를 함께 적어 주세요. "
                 f"승인 상한·실제 지출·예상 지출의 역할을 구별해 예산을 검토해 주세요.")
    return base


def t_compare_later(s: dict) -> str:
    return (f"이제 처음에 말씀드린 A안과 B안 가운데 하나를 정해야 합니다. 비용과 {s['risk']} 두 기준으로 두 안을 표 하나로 비교하고 실행 가능한 안을 추천해 주세요. "
            f"그 안의 준비 순서와 선택 근거를 함께 적어 주세요. 승인 상한·실제 지출·예상 지출의 역할을 구별해 예산을 검토해 주세요. 앞 턴에 준 수치만 쓰고 새 수치를 만들지 마세요.")


def t_record(s: dict) -> str:
    ask = (f"이 기록에서 참가자 행동에 필요한 확정 사항과 아직 미정인 사항을 빠뜨리지 않게 요약해 주세요. 문장에 [1]·[2]를 인용하여 제공된 기록의 원제목과 대응하는 출처 목록을 함께 작성해 주세요. "
           f"끝에는 실제로 확인한 범위와 외부 조회·예약 실행 여부를 사실대로 구별해 주세요. {TEXT_ONLY}")
    return pasted(record(s), ask)


def t_calc(s: dict) -> str:
    ask = (f"위 원본의 모든 행을 대상으로 '{s['key']}' 열의 합계를 알려 주세요. 개당 {s['unit_price']}원인 {josa(s['unit_item'], '을', '를')} {s['unit_qty']}개 주문할 때 총액을 계산해 주세요. "
           f"두 결과에 사용한 실제 수치와 등식을 명시해 주세요.")
    return pasted(table_md(s["table"]), ask)


def t_explain(s: dict) -> str:
    a, b = s["explain_terms"]
    return (f"{s['explain_ctx']}. 왜 그런지 원인을 결과와 연결해서 설명해 주세요. 코딩이나 과학에 익숙하지 않은 운영자에게 맞는 일상 비유를 하나 들고, "
            f"행사 당일 어떻게 대응할지도 밝혀 주세요. {josa(a, '과', '와')} {b}의 정의도 정확히 구별해 주세요. 관찰 순서를 뒤집지 말고 기록만으로 {josa(s['explain_no'], '이라고', '라고')} 단정하지 마세요.")


def t_draft(s: dict, extra: str = "") -> str:
    n = s["bullets"]
    return (f"{s['org']}의 {s['artifact']} 초안 전체를 제시해 주세요. {extra}앞에서 확정한 일시·참여 조건·문의처를 충분히 안내해 주세요. "
            f"글자는 한글만 사용하고 숫자와 문장 부호는 허용합니다. 본문은 불릿으로 정확히 {n}개를 작성하고 그 앞에 \"다음 {n}가지 안내를 확인해 주세요.\"라는 개수 선언을 두세요. "
            f"\"{s['req']}\"{josa(s['req'], '이라는', '라는')[len(s['req']):]} 단어를 반드시 포함해 주세요. \"{s['forbid']}\"{josa(s['forbid'], '이라는', '라는')[len(s['forbid']):]} 단어는 쓰지 마세요. "
            f"예비 인력의 현재 상태를 확정된 것처럼 적지 마세요. {TEXT_ONLY}")


def t_revise_format(s: dict, nth: str) -> str:
    a1, a2 = s["anchors"]
    return (f"{s['org']}의 {s['artifact']}을 {nth} 수정해 주세요. 아래 형식에 맞는 수정본 전체를 제시해 주세요. 목차에는 [{a1}](#{a1}), [{a2}](#{a2}) 내부 링크와 실제 대응 제목을 넣으세요. "
            f"각주 [^{s['footnote']}]과 그 정의, 자료의 [1]·[2] 출처 번호와 목록도 맞춰 주세요. 문의 부분은 코드블록 밖의 짝이 맞는 <details><summary>문의</summary>...</details> 안에 적어 주세요. "
            f"마지막에는 담당자가 복사할 문장 형태의 운영 메모를 코드블록 정확히 1개로 제시하고, 앞뒤 코드 구획 표시를 모두 닫아 주세요. "
            f"각주·출처 목록·내부 링크·문의 태그는 코드블록 밖에 작성해 주세요. {TEXT_ONLY}")


def t_revise_change(s: dict, nth: str, keep_format: bool) -> str:
    keep = "앞서 고친 목차·각주·문의 형식도 유지하고, " if keep_format else ""
    return (f"{s['org']}의 같은 {s['artifact']}을 {nth} 수정해 주세요. {s['change']}. {s['exception_cancel']}할 수 있게 {s['artifact']} 전체를 다시 써 주세요. "
            f"{keep}수정 목록을 따로 붙이지 마세요. {TEXT_ONLY}")


def t_attach(s: dict) -> str:
    return f"{s['org']}의 최종 {s['attach']}를 첨부했는데, {s['attach_q']}을 알려 주세요."


def t_pushback(s: dict) -> str:
    return f"{s['pushback']} 새 자료는 없습니다. 기존 근거를 다시 검토하고 판단의 근거를 설명해 주세요."


def t_final(s: dict) -> str:
    on = s.get("online")
    steps = "접속·자료 확인·점검·입장 승인·발표·종료의 시각" if on else "도착·이동·확인·설치·접수·행사 종료의 시각"
    return (f"{s['org']}의 {s['final']}을 만들어 주세요. 아까 두 번째 안의 비용을 떠올리되, 지금까지 확정한 방식과 최신 {'입장' if on else '접수'} 조건을 반영해 주세요. "
            f"{steps}, 담당자와 소요 시간을 보여 주세요. 운영자가 실행할 작업 순서는 {s['final_list']}으로 작성해 주세요. 정해진 인력 외의 사람은 사용하지 마세요.")


# ---------------------------------------------------------------------------------------------------------------------
# 턴 순서 네 가지 — 대화마다 사건의 순서가 다르다
# ---------------------------------------------------------------------------------------------------------------------
def build_turns(s: dict) -> list:
    p = s["pattern"]
    if p == "P1":   # 설정·비교 → 기록 → 계산 → 현장 현상 설명 → 초안 → 1차(형식) → 2차(조건 변경·예외 취소) → 첨부 없음 → 반박 → 최종 일정
        turns = [t_setup(s), t_record(s), t_calc(s), t_explain(s), t_draft(s, s.get("draft_extra", "접수 안내를 놓치지 않도록 ")),
                 t_revise_format(s, "첫 번째로"), t_revise_change(s, "두 번째로", True), t_attach(s), t_pushback(s), t_final(s)]
    elif p == "P2":  # 설정(비교는 뒤로) + 초안 먼저 → 현상 설명 → 기록 → 1차(형식) → 계산 → 2차(조건 변경) → 반박 → A/B 비교·추천 → 첨부 없음 → 최종 순서
        turns = [t_setup(s, with_compare=False) + " 먼저 " + t_draft(s, "참가자가 당일 해야 할 행동이 드러나도록 ").replace(f"{s['org']}의 ", "", 1),
                 t_explain(s), t_record(s), t_revise_format(s, "첫 번째로"), t_calc(s), t_revise_change(s, "두 번째로", True),
                 t_pushback(s), t_compare_later(s), t_attach(s), t_final(s)]
    elif p == "P3":  # 설정·비교 → 현상 설명 → 초안 → 1차(조건 변경·예외 취소, 형식 없이) → 기록 → 계산 → 2차(형식) → 반박 → 최종 일정 → 첨부 없음
        turns = [t_setup(s), t_explain(s), t_draft(s, "참여 조건이 분명하도록 "), t_revise_change(s, "첫 번째로", False), t_record(s), t_calc(s),
                 t_revise_format(s, "두 번째로"), t_pushback(s), t_final(s), t_attach(s)]
    else:            # P4: 설정·비교 → 계산 → 초안 → 첨부 없음(이른 시점) → 기록 → 1차(형식) → 현상 설명 → 2차(조건 변경) → 반박 → 최종 일정
        turns = [t_setup(s), t_calc(s), t_draft(s, s.get("draft_extra", "접수 안내를 놓치지 않도록 ")), t_attach(s), t_record(s), t_revise_format(s, "첫 번째로"),
                 t_explain(s), t_revise_change(s, "두 번째로", True), t_pushback(s), t_final(s)]
    assert len(turns) == 10
    return [{"turn": i, "question": q} for i, q in enumerate(turns, 1)]


def main() -> int:
    # 10-22: 이 파일은 multiturn_v3 이 시나리오 · 요청 문장을 읽어 가는 용도로만 남김 — 직접 돌리면 지금 세트(질문세트_78)의 10턴 대화를 v2 로 덮어쓴다
    raise SystemExit("multiturn_v2 는 직접 돌리지 않습니다 — 질문세트_78/multiturn_v3/build_conversations.py 를 쓰세요")
    path = SET_DIR / "questions.json"
    qs = json.loads(path.read_text(encoding="utf-8"))
    by_id = {q.get("origin_id", q["id"]): q for q in qs}   # 시나리오 번호 = 원래 번호(ChatGPT 생성 때). 던지는 순서로 재배치하면 id 는 달라진다
    for s in S:
        q = by_id[s["id"]]
        assert q["type"] == "10턴 멀티턴 대화", q["type"]
        q["topic"] = f"가상 {s['org']}의 {s['event']} 운영 — multiturn-v2"
        q["trap"] = s["trap"]
        q["target_items"] = list(TARGET_ITEMS)
        q["difficulty"] = "어려움"
        q["turns"] = build_turns(s)
        rec = next(t["question"] for t in q["turns"] if "[붙여 넣은 자료]" in t["question"] and "| 항목 |" not in t["question"])
        print(f"Q{s['id']:2d} {s['pattern']} {s['layout']} {s['org']:12s} 자료 {len(rec):5d}자 · 턴 길이 {[len(t['question']) for t in q['turns']]}")
    path.write_text(json.dumps(qs, ensure_ascii=False, indent=1), encoding="utf-8")
    print("written:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
