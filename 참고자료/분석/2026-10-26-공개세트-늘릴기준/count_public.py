"""공개 세트(IFEval-Ko 342 · KoMT-Bench 80 1턴)에 지금 기준이 얼마나 붙는지 센다 — 모델 · API 호출 없음.

10-25 검토(참고자료/문서/공개세트_검토_2026-10-25.md)와 같은 방법: 질문만 넣고(답 빈칸) tools/check_question_set.check_items(pipeline=items)
로 적용 CODE 검사 · LLM 항목을 세고, webapp.question_generation.k_var_units()(54개)와의 교집합 크기를 k 로.
더해서 질문 조건 추출(code_eval.engine.evaluate_batch 의 meta.question_conditions)의 조건 종류를 지시 종류별로 모은다.

어림(stages): 코드는 고치지 않고 이 스크립트 안에서만 가정을 더해 k 를 다시 센다.
- S1 요청 문장 끝 넓힘: jev_applicability._REQUEST_END 를 이 실행 동안만 BROAD_END 로 바꿔 check_items 를 다시 돌림(J2~J5 항목 규칙의 전제).
- S2 기존 기준 인식 넓힘: IFEval 원본의 지시 종류(instruction_id_list)를 '완벽히 알아본다'고 보고 기존 검사 id 를 더함(EXISTING_BY_INST),
  강조 섹션을 제목 개수로 잘못 읽은 2-2.heading 은 뺌, 문체 지정(STYLE)은 J2-2.a 로.
- S3 새 CODE 검사: 같은 가정으로 새 검사 id 를 더함(NEW_CODE_BY_INST). 새 id 는 모두 변동 기준(k_var)에 넣음.
- S4 새 LLM · 정답 기준: KoMT 범주(NEW_BY_CATEGORY)와 낱말 규칙(GENRE · PERSONA)으로 더함.

    python -X utf8 참고자료/분석/2026-10-26-공개세트-늘릴기준/count_public.py

출력: 같은 폴더 counts.json (질문 원문은 넣지 않음 — 공개 원본은 data/question_sets/public_sources/ 에만, git 제외).
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from code_eval import jev_applicability as ja  # noqa: E402
from code_eval.engine import evaluate_batch  # noqa: E402
from tools.check_question_set import check_items  # noqa: E402
from webapp.question_generation import check, k_var_summary, k_var_units  # noqa: E402

SRC = ROOT / "data" / "question_sets" / "public_sources"
OUT = Path(__file__).with_name("counts.json")

#: S1 — 번역투 지시문 끝(하세요 · 하십시오 · 하라 · 해야 합니다 · 합니다)도 요청 문장으로 봄. 서술문(…합니다)도 일부 섞여 위쪽 어림이다
BROAD_END = re.compile(r"(?:\?|줘|주세요|마|말아|해|하자|봐|세요|십시오|시오|라|야\s*(?:합니다|한다|해요)|됩니다|바랍니다|합니다|봅시다)[.?!:]*\s*$")
#: S1 민감도 — 서술문이 섞이는 '합니다 · 됩니다'를 뺀 좁은 판 (아래쪽 어림)
NARROW_END = re.compile(r"(?:\?|줘|주세요|마|말아|해|하자|봐|세요|십시오|시오|라|야\s*(?:합니다|한다|해요)|바랍니다)[.?!:]*\s*$")
#: S2 — 이미 있는 검사가 맡을 IFEval 지시(인식만 넓히면 됨)
EXISTING_BY_INST = {
    "length_constraints:number_words": ["2-1.count"], "length_constraints:number_sentences": ["2-1.count"],
    "length_constraints:number_paragraphs": ["2-1.count"], "detectable_format:number_bullet_lists": ["2-1.count"],
    "keywords:forbidden_words": ["2-1.literal"], "keywords:existence": ["2-1.literal"], "startend:end_checker": ["2-1.literal"],
    "startend:quotation": ["2-1.literal"], "punctuation:no_comma": ["2-1.literal"], "combination:repeat_prompt": ["2-1.literal"],
    "detectable_content:postscript": ["2-1.literal"],
    "detectable_format:json_format": ["1-2.json_syntax", "2-2.json_structure"], "detectable_format:multiple_sections": ["2-2.heading"],
}
#: S3 — 새 CODE 검사가 필요한 IFEval 지시
NEW_CODE_BY_INST = {
    "detectable_format:number_highlighted_sections": ["새.강조 구간 개수"], "detectable_format:title": ["새.<<제목>> 표기"],
    "keywords:frequency": ["새.낱말 빈도"], "detectable_content:number_placeholders": ["새.자리표시자 개수"],
    "combination:two_responses": ["새.구분자로 나눈 답"], "detectable_format:constrained_response": ["새.정해진 보기로 답"],
    "language:response_language": ["새.응답 언어"], "length_constraints:nth_paragraph_first_word": ["새.항목별 조건"],
}
#: S4 — KoMT 범주로 켜지는 새 기준
NEW_BY_CATEGORY = {"math": ["새.최종 답 정확"], "reasoning": ["새.최종 답 정확"], "coding": ["새.코드 동작"],
                   "roleplay": ["새.역할 유지"], "extraction": ["새.추출 완결"]}
GENRE = re.compile(r"(?<![가-힣A-Za-z])(?:시(?:를|는|의|로|가)?|랩(?:을|으로)?)(?![가-힣,])|노래|리머릭|하이쿠|소네트|수수께끼|농담|우화|동화|소설|대본|연설")
WRITE = re.compile(r"써|쓰|작성|만들|지어|형식|형태|스타일|표현")   # 장르 낱말과 같은 문장에 쓰기 동사가 있을 때만 장르 지정으로 봄
PERSONA = re.compile(r"(?:인|이|하는|된)\s*것처럼|되어\s*(?:보|대화|답)|역(?:을|할을)\s*맡|(?:이|라)고\s*(?:상상|가정)|당신은\s*\S+\s*(?:입니다|이다)|이제\s*당신은")
STYLE = re.compile(r"스타일|사투리|말투|처럼\s*(?:말|대화|답|써|작성)")


def genre_hit(text):
    return any(GENRE.search(sent) and WRITE.search(sent) for sent in re.split(r"[.?!\n]", text))


def load():
    ifeval = json.loads((SRC / "ifeval_ko.json").read_text(encoding="utf-8"))
    komt = [json.loads(line) for line in (SRC / "komt_question_ko.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    return ifeval, komt


def run(rows):
    """rows: [{case_id, question}] → {case_id: {checks, items, var, k, kinds, kinds_unsupported}}"""
    payload = [{"case_id": r["case_id"], "question": r["question"], "answer": ""} for r in rows]
    report = check_items(payload, "items", 10)
    evaluated = evaluate_batch(payload)["cases"]
    units = k_var_units()
    out = {}
    for row, ev in zip(report["cases"], evaluated):
        conds = [c for c in (ev.get("meta") or {}).get("question_conditions") or [] if isinstance(c, dict)]
        var = sorted({t for t in row["checks"] + row["items"] if t in units})
        out[row["case_id"]] = {"checks": row["checks"], "items": row["items"], "var": var, "k": len(var),
                               "kinds": sorted({c.get("kind") for c in conds if c.get("status", "ok") == "ok"}),
                               "kinds_unsupported": sorted({c.get("kind") for c in conds if c.get("status", "ok") != "ok"})}
    return out, report["errors"]


def run_broad(rows, pattern=BROAD_END):
    old = ja._REQUEST_END
    ja._REQUEST_END = pattern
    try:
        return run(rows)
    finally:
        ja._REQUEST_END = old


def summary(sets):
    ks = [len(v) for v in sets.values()]
    return {"k_ge1": sum(k >= 1 for k in ks), "k_6_10": sum(6 <= k <= 10 for k in ks),
            "k_ge3": sum(k >= 3 for k in ks), "k_ge5": sum(k >= 5 for k in ks),
            "k_dist": dict(sorted(Counter(ks).items())), "k_mean": round(sum(ks) / len(ks), 2)}


def stages(s0, s1, texts):
    """S0~S4 단계별 변동 기준 집합 요약과 새 기준이 덮는 문항 수."""
    out = {"S0 지금": summary({cid: set(r["var"]) for cid, r in s0.items()})}
    cur = {cid: set(r["var"]) for cid, r in s1.items()}
    out["S1 +요청 문장 끝 넓힘"] = summary(cur)
    gain = Counter()   # S2 에서 그 문항에 새로 붙은 기존 기준 (지시 종류별 · 문항 수)
    for cid, r in s1.items():
        insts = r.get("instructions") or []
        for inst in dict.fromkeys(insts):
            if set(EXISTING_BY_INST.get(inst, [])) - set(r["var"]):
                gain[inst] += 1
            cur[cid].update(EXISTING_BY_INST.get(inst, []))
        if "detectable_format:number_highlighted_sections" in insts and not {"detectable_format:multiple_sections"} & set(insts):
            cur[cid].discard("2-2.heading")   # 강조 섹션을 제목 개수로 잘못 읽은 것을 고친다고 가정
        if STYLE.search(texts[cid]):
            if "J2-2.a" not in r["var"]:
                gain["문체 지정 → J2-2.a"] += 1
            cur[cid].add("J2-2.a")
    out["S2 +기존 기준 인식 넓힘"] = summary(cur)
    out["S2 새로 붙은 문항"] = dict(gain.most_common())
    for cid, r in s1.items():
        for inst in r.get("instructions") or []:
            cur[cid].update(NEW_CODE_BY_INST.get(inst, []))
    out["S3 +새 CODE 검사"] = summary(cur)
    for cid, r in s1.items():
        cur[cid].update(NEW_BY_CATEGORY.get(r.get("category"), []))
        if genre_hit(texts[cid]):
            cur[cid].add("새.장르 · 형식")
        if PERSONA.search(texts[cid]):
            cur[cid].add("새.역할 유지")
    out["S4 +새 LLM · 정답 기준"] = summary(cur)
    return out, dict(Counter(u for v in cur.values() for u in v if u.startswith("새.")).most_common())


def ours_78():
    """잣대 확인 — 우리 78 세트 단일 질문의 k 분포가 S1 에서 얼마나 바뀌는지."""
    groups = json.loads((ROOT / "참고자료" / "질문세트_78" / "questions.json").read_text(encoding="utf-8"))
    keys = ("singles", "in_range", "out_of_range", "exempt", "by_k")
    before = k_var_summary(groups, check(groups))
    old = ja._REQUEST_END
    ja._REQUEST_END = BROAD_END
    try:
        after = k_var_summary(groups, check(groups))
    finally:
        ja._REQUEST_END = old
    return {"S0": {k: before[k] for k in keys}, "S1": {k: after[k] for k in keys}}


def main():
    ifeval, komt = load()
    units = k_var_units()
    ife_rows = [{"case_id": f"IF{x['key']}", "question": x["prompt"]} for x in ifeval]
    mt1_rows = [{"case_id": f"MT{x['question_id']}", "question": x["turns"][0]} for x in komt]
    mt2_rows = [{"case_id": f"MT{x['question_id']}_2", "question": x["turns"][1]} for x in komt if len(x["turns"]) > 1]
    ife, e1 = run(ife_rows)
    mt1, e2 = run(mt1_rows)
    mt2, e3 = run(mt2_rows)   # 참고: 2턴 질문만 따로(앞 턴 없이) — 공식 k 아님
    ife1, _ = run_broad(ife_rows)
    mt11, _ = run_broad(mt1_rows)
    for x in ifeval:
        for d in (ife, ife1):
            d[f"IF{x['key']}"].update(instructions=x["instruction_id_list"])
    for x in komt:
        for d in (mt1, mt11):
            d[f"MT{x['question_id']}"].update(category=x["category"])
        if f"MT{x['question_id']}_2" in mt2:
            mt2[f"MT{x['question_id']}_2"].update(category=x["category"])

    # 지시 종류별(문항 단위 — 한 문항에 같은 지시가 두 번 있어도 1): 문항 수 · 질문당 CODE 변동 검사 평균 · 조건 종류 · 적용 항목
    by_inst = defaultdict(list)
    for r in ife.values():
        for inst in dict.fromkeys(r["instructions"]):
            by_inst[inst].append(r)
    code_var = {u for u in units if u[:1].isdigit()}
    inst_summary = {}
    for inst, rs in sorted(by_inst.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        inst_summary[inst] = {"n": n, "code_var_per_q": round(sum(len([c for c in r["var"] if c in code_var]) for r in rs) / n, 2),
                              "k_mean": round(sum(r["k"] for r in rs) / n, 2),
                              "kinds": dict(Counter(k for r in rs for k in r["kinds"]).most_common()),
                              "kinds_unsupported": dict(Counter(k for r in rs for k in r["kinds_unsupported"]).most_common()),
                              "code_checks": dict(Counter(c for r in rs for c in r["var"] if c in code_var).most_common()),
                              "llm_items": dict(Counter(i for r in rs for i in r["var"] if i not in code_var).most_common()),
                              "J2-3.a_rate": round(sum("J2-3.a" in r["items"] for r in rs) / n, 2)}
    by_cat = defaultdict(list)
    for r in mt1.values():
        by_cat[r["category"]].append(r)
    cat_summary = {cat: {"n": len(rs), "k_dist": dict(sorted(Counter(r["k"] for r in rs).items())),
                         "k_mean": round(sum(r["k"] for r in rs) / len(rs), 2),
                         "var_units": dict(Counter(u for r in rs for u in r["var"]).most_common())} for cat, rs in by_cat.items()}
    by_cat1 = defaultdict(list)
    for r in mt11.values():
        by_cat1[r["category"]].append(r)
    cat_summary_s1 = {cat: {"k_mean": round(sum(r["k"] for r in rs) / len(rs), 2),
                            "var_units": dict(Counter(u for r in rs for u in r["var"]).most_common())} for cat, rs in by_cat1.items()}
    by_cat2 = defaultdict(list)
    for r in mt2.values():
        by_cat2[r["category"]].append(r)
    cat2_summary = {cat: {"n": len(rs), "var_units": dict(Counter(u for r in rs for u in r["var"]).most_common()),
                          "kinds": dict(Counter(k for r in rs for k in r["kinds"]).most_common())} for cat, rs in by_cat2.items()}

    narrow = {"ifeval": summary({cid: set(r["var"]) for cid, r in run_broad(ife_rows, NARROW_END)[0].items()}),
              "komt_turn1": summary({cid: set(r["var"]) for cid, r in run_broad(mt1_rows, NARROW_END)[0].items()})}
    sim_ife, new_ife = stages(ife, ife1, {r["case_id"]: r["question"] for r in ife_rows})
    sim_mt, new_mt = stages(mt1, mt11, {r["case_id"]: r["question"] for r in mt1_rows})
    s1_gain = {name: dict(Counter(u for cid in d0 for u in set(d1[cid]["var"]) - set(d0[cid]["var"])).most_common())
               for name, d0, d1 in (("ifeval", ife, ife1), ("komt_turn1", mt1, mt11))}
    no_request = {"ifeval": sum(1 for r in ife_rows if not ja._request_sentences(r["question"])),
                  "komt_turn1": sum(1 for r in mt1_rows if not ja._request_sentences(r["question"]))}

    def dist(d):
        return dict(sorted(Counter(r["k"] for r in d.values()).items()))
    out = {"method": "check_question_set.check_items(pipeline=items, min=10) + evaluate_batch 조건 종류, 답 빈칸, k = |적용 ∩ k_var_units(54)|",
           "k_var_units": len(units), "errors": {"ifeval": e1, "komt1": e2, "komt2": e3},
           "ifeval": {"n": len(ife), "k_dist": dist(ife), "k_ge1": sum(r["k"] >= 1 for r in ife.values()),
                      "k_6_10": sum(6 <= r["k"] <= 10 for r in ife.values()), "by_instruction": inst_summary},
           "komt_turn1": {"n": len(mt1), "k_dist": dist(mt1), "k_ge1": sum(r["k"] >= 1 for r in mt1.values()),
                          "k_6_10": sum(6 <= r["k"] <= 10 for r in mt1.values()), "by_category": cat_summary,
                          "by_category_S1": cat_summary_s1},
           "komt_turn2_standalone": {"n": len(mt2), "by_category": cat2_summary},
           "no_request_sentence": no_request, "s1_gain_units": s1_gain,
           "simulation": {"ifeval": sim_ife, "komt_turn1": sim_mt, "new_units_cover": {"ifeval": new_ife, "komt_turn1": new_mt},
                          "ours_78_singles": ours_78(), "broad_end": BROAD_END.pattern,
                          "S1_narrow": narrow, "narrow_end": NARROW_END.pattern},
           "cases": {"ifeval": ife, "komt_turn1": mt1, "komt_turn2_standalone": mt2,
                     "ifeval_S1": {cid: r["var"] for cid, r in ife1.items()}, "komt_turn1_S1": {cid: r["var"] for cid, r in mt11.items()}}}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("ifeval", out["ifeval"]["k_dist"], "k>=1", out["ifeval"]["k_ge1"], "k6-10", out["ifeval"]["k_6_10"])
    print("komt1", out["komt_turn1"]["k_dist"], "k>=1", out["komt_turn1"]["k_ge1"], "k6-10", out["komt_turn1"]["k_6_10"])
    print("errors", {k: len(v) for k, v in out["errors"].items()}, "no request sentence", no_request)
    for name in ("ifeval", "komt_turn1"):
        for stage, row in out["simulation"][name].items():
            print(name, stage, row)
    print("S1 narrow", narrow)
    print("new cover", out["simulation"]["new_units_cover"])
    print("ours78", out["simulation"]["ours_78_singles"])
    print("s1 gain", s1_gain)


if __name__ == "__main__":
    main()
