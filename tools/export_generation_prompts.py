"""질문생성 프롬프트 원문을 문서 하나로 낸다 — 코드가 실제로 보내는 글을 그대로 (모델 호출 없음).

    python tools/export_generation_prompts.py            # 참고자료/프롬프트/질문생성_프롬프트_v2.0.md

구조: 공통 규칙은 모든 단계가 같고, 단계마다 "이번 요청"(단일 질문 계획·질문 추가·빠진 기준 보충·10턴 대화·긴 문서·질문당 기준 수 고치기)만 다르다.
한 번에 326개를 받을 수 없어(응답 출력 한도 60K 토큰, 326개는 약 550K) 단계·묶음으로 나눠 부르되 규칙은 한 벌이다.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp import question_generation as qg  # noqa: E402

OUT = ROOT / "참고자료" / "프롬프트" / "질문생성_프롬프트_v2.0.md"


def fence(text: str) -> str:
    return "```text\n" + text.strip() + "\n```\n"


def main() -> int:
    catalog = qg.criteria_catalog()
    plan = qg.plan_prompt(target_questions=40, dedicated=True)
    catalog_at = plan.index("\n# 점검 목록\n")
    rules, catalog_text = plan[:catalog_at], plan[catalog_at + 1:]
    sample = [{"id": "Q36", "k": 3, "units": {"2-2.heading", "J2-3.a", "J4-2.a"},
               "target_items": ["1-3.citation_link", "1-3.footnote_link", "2-2.heading", "2-1.count", "J5-1.a", "J5-3.a", "J2-3.a"], "target": 7,
               "question": "해수면 기준 순수한 물의 끓는점이 몇 도인지 사실을 확인해줘. 정확히 2개의 소제목으로 나누고, 사실 주장에는 [1] 형식의 출처 번호를 달아 끝에 [1] 출처 줄을 적어줘."}]
    repair = qg.k_repair_prompt(sample, catalog, ["J3-5.a"])
    repair_rules = repair[:repair.index("# 점검 목록")]
    doc = f"""# 질문생성 프롬프트 v2.2 ({qg.QUESTION_GEN_VERSION})

코드(`webapp/question_generation.py`)가 실제로 보내는 글을 그대로 모았다. 한 번에 326개를 받을 수 없어(응답 출력 한도 60K 토큰, 326개는 약 550K) **규칙은 한 벌**이고 단계·묶음마다 "이번 요청"만 다르다.
순서: ③ 긴 문서 질문 10개 → ② 10턴 대화 16개 → ① 단일 질문 40개씩 → 빠진 기준 보충 → ④ 질문당 기준 수(k_var) 고치기. 질문당 기준 수 규칙(6~10개, 세지 않는 기준 목록)은 ①과 ④가 같은 글을 쓴다.

## 공통 규칙 + ① 단일 질문 계획 (한 묶음 40개)

{fence(rules)}
## ② 10턴 대화 (J6) — 한 호출에 4개

{fence(qg.multi_turn_prompt(4, []).split("# 점검 목록")[0])}
## ③ 긴 문서 질문 (J7) — 문서 1개 + 질문 10개

{fence(qg.long_context_prompt(10).split("# 점검 목록")[0])}
## ④ 질문당 기준 수(k_var) 고치기 — 만든 뒤 범위(6~10) 밖 단일 질문을 모두, 질문 하나씩 (빈 출력 요구 질문만 예외)

{fence(repair_rules)}
## 점검 목록 (①·④ 뒤에 붙는 기준 72개 — ④는 k 에 세는 54개만)

{fence(catalog_text)}"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(doc, encoding="utf-8")
    print("저장:", OUT, len(doc), "자")
    return 0


if __name__ == "__main__":
    sys.exit(main())
