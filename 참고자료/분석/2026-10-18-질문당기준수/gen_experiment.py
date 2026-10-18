"""통제 실험용 질문 생성 — 기본 질문 30개 × k_var 1~10 변형 = 단일 질문 300개 (GPT-6 Luna).
python -X utf8 gen_experiment.py --prompt-only   # 첫 호출 프롬프트만 저장(호출 없음)
python -X utf8 gen_experiment.py                 # 생성(호출 10회, 기본 질문 3개씩) → experiment/questions.json + 실제 k_var 점검
결과는 호출마다 저장 — 다시 돌리면 끝난 묶음은 건너뜀."""
import json, sys, time
from pathlib import Path

ROOT = Path(r"C:\Users\winz\Desktop\quality-test-integrated")
sys.path.insert(0, str(ROOT))
from code_eval import criteria_registry as reg, semantic_items as si
from code_eval.llm_client import env_value
from webapp import question_generation as qg

OUT = Path(__file__).parent / "experiment"
OUT.mkdir(exist_ok=True)
GEN_VERSION = "experiment-kvar-v1"

# 변형에 쓸 기준 — 변동 기준 54개 중 단일 질문·고정 자료로 만들 수 있는 것 (J6 대화·J7 긴 문서·J8 첨부 제외)
catalog = qg.criteria_catalog()
code_all = {u[0] for u in reg.CODE_UNITS if u[2] == "all"}
always = {it.id for it in si.ITEMS if it.always}
ans_dep = {u[0] for u in reg.CODE_UNITS if u[2] == "answer"} | {"J5-3.a"}
pool = {c["id"] for c in catalog["code"] if c["id"] not in code_all | ans_dep} | \
       {i["id"] for i in catalog["items"] if i["id"] not in always | ans_dep and not i["id"].startswith(("J6", "J7", "J8"))}
TYPES = ["문구 작성", "자료 요약", "정보 추출", "계산·변환", "조건 기반 판단"]
SLOTS = [(f"B{n:02d}", TYPES[(n - 1) // 6], "보통" if (n - 1) % 6 < 3 else "어려움") for n in range(1, 31)]

PROMPT = """당신은 한국어 AI 답변 품질 평가의 통제 실험용 질문을 설계합니다. 결과는 JSON 하나로만 답합니다.

# 실험 목적
한 질문에 적용되는 평가 기준 수(k_var)가 늘어날 때 평가 결과가 어떻게 변하는지 보려 합니다. 그래서 같은 기본 질문을 두고 기준 수만 1개부터 10개까지 바꾼 변형 10개를 만듭니다.

# 이번에 만들 기본 질문 ({n}개)
{slots}

# 기본 질문 규칙
- 질문 안에 가상의 자료(공지·표·기록·목록 등, 300~800자)를 직접 넣습니다. 답은 그 자료만으로 정해져야 합니다(외부 검색·최신 정보·실존 인물 정보 불필요). 가상이라고 밝힙니다.
- 정답 행동이 분명해야 합니다. 자료가 모자라 되물어야 하는 질문, 사람마다 정답이 갈리는 질문은 만들지 않습니다.
- 실제 사용자가 할 법한 자연스러운 문장으로 씁니다. 주제는 기본 질문끼리 겹치지 않게 합니다.
- 난이도 '어려움'은 자료 속 함정(헷갈리는 비슷한 값, 조건에 따라 달라지는 계산, 예외 규정)을 넣어 틀리기 쉽게 하되, 요구 자체는 명확해야 합니다.

# 변형 규칙 (가장 중요)
- 같은 기본 질문의 변형 10개는 주제·자료 원문·핵심 과제·정답의 핵심 내용을 그대로 둡니다. 바뀌는 것은 덧붙이는 조건(제약)뿐입니다.
- 변형 k 는 아래 [점검 목록]의 서로 다른 기준 id 가 정확히 k 개 적용되게 조건을 붙입니다(k = 1, 2, …, 10).
  · CODE 검사는 검사 id 하나를 1개로 셉니다. 같은 검사 안의 조건 여러 개(예: 문장 수와 불릿 수는 둘 다 2-1.count)는 1개입니다.
  · 의미 평가 항목은 항목 id 하나를 1개로 셉니다.
  · 기본 과제만으로 이미 적용되는 기준(예: 요약 요청이면 J4-4.a)도 셉니다. k=1 은 기본 과제 하나만 남긴 가장 단순한 형태입니다.
- 조건은 서로 어긋나지 않고 자연스럽게 한 문장·한 질문으로 읽혀야 합니다. 기본 질문마다 조건을 붙이는 순서를 다르게 해, 높은 k 에 특정 어려운 기준만 몰리지 않게 합니다.
- 외부 검색·최신 사실·새 첨부파일 요구로 k 를 늘리지 않습니다. 답변 분량을 늘리라는 지시도 쓰지 않습니다.
- CODE 검사 조건은 [점검 목록]의 인식 규칙 표현을 그대로 씁니다(규칙에 맞지 않으면 적용되지 않음).

# 헷갈리는 표현 피하기 (채점 오류를 막기 위해)
- 꼭 넣을 문구와 쓰지 말 문구는 다른 문장으로 씁니다('A와 B를 넣고 C는 쓰지 마' 대신 'A와 B를 넣어줘. C라는 말은 쓰지 마.').
- '…지도 마', '형식은 쓰지 마' 대신 '표를 쓰지 마'처럼 무엇을 금지하는지 직접 씁니다.
- 자료 속 숫자(상추 30개, 조리대 3개)가 답의 개수 요구로 읽히지 않게, 개수 요구는 '목록을 정확히 5개로'처럼 따로 씁니다.
- 입력 자료를 설명할 때 'JSON으로·표로' 같은 말을 쓰지 않습니다(답 형식 요구로 읽힘). 답 형식은 '답은 JSON으로 줘'처럼 따로 씁니다.

# 출력 형식 (JSON 하나만, 설명·코드 펜스 없이)
{{"bases": [
  {{"base_id": "B01", "type": "유형", "difficulty": "보통|어려움", "topic": "주제",
    "expected_answer": "자료로 정해지는 정답의 핵심(평가용, 답변 모델에게는 보내지 않음)",
    "variants": [
      {{"k": 1, "target_items": ["기준 id"], "added": "이 변형에서 붙인 조건 요약", "question": "자료를 포함한 질문 원문 전체"}},
      ... k 2~10 ...
    ]}}
]}}
- target_items 에는 그 변형에서 적용되도록 한 기준 id 를 정확히 k 개 씁니다(CODE 는 검사 id, 예: 2-1.count).
- variants 는 k = 1~10 순서로 정확히 10개입니다.

# 점검 목록 (이 기준들만 씀)
{catalog}

# 개수 (반드시 지킴)
bases 에 기본 질문 {n}개, 각 variants 10개(k=1~10). 답하기 전에 개수를 세어 확인합니다."""


def prompt_for(slots):
    lines = "\n".join(f"- {b}: 유형 '{t}', 난이도 '{d}'" for b, t, d in slots)
    cat = "\n".join(qg._catalog_lines(qg.restrict_catalog(catalog, pool), detailed=False))
    return PROMPT.format(n=len(slots), slots=lines, catalog=cat)


if "--prompt-only" in sys.argv:
    text = prompt_for(SLOTS[:3])
    (OUT / "prompt_sample.txt").write_text(text, encoding="utf-8")
    print("기준 풀", len(pool), "| 프롬프트", len(text), "자 → experiment/prompt_sample.txt")
    sys.exit(0)

config = {"key": env_value("OPENAI_API_KEY", env_path=ROOT / ".env"), "model": "gpt-6-luna"}
usage = {}
llm = qg.gpt_llm(config, usage)
for i in range(0, 30, 3):
    slots = SLOTS[i:i + 3]
    target = OUT / f"batch_{slots[0][0]}.json"
    if target.exists():
        continue
    text = llm(prompt_for(slots), 60000)
    data = qg._json_object(text)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(time.strftime("%H:%M:%S"), slots[0][0], "~", slots[-1][0], "완료 | 누적 토큰", usage, flush=True)

# 합치기 + 실제 k_var 점검
groups, meta = [], []
for f in sorted(OUT.glob("batch_B*.json")):
    for b in json.loads(f.read_text(encoding="utf-8")).get("bases") or []:
        for v in b.get("variants") or []:
            n = len(groups) + 1
            groups.append({"id": n, "type": b.get("type"), "topic": b.get("topic"), "difficulty": b.get("difficulty"),
                           "target_items": v.get("target_items") or [], "turns": [{"turn": 1, "question": v.get("question") or ""}]})
            meta.append({"id": n, "base_id": b.get("base_id"), "k_target": v.get("k"), "added": v.get("added"),
                         "expected_answer": b.get("expected_answer")})
rep = qg.check(groups)
var = {n for n in pool}
for row in rep["cases"]:
    n = int(row["case_id"].split("_")[0][1:])
    got = {t for t in (row.get("checks") or []) + (row.get("items") or []) if t in var}
    m = meta[n - 1]
    m["k_actual"], m["actual_items"] = len(got), sorted(got)
    m["target_hit"] = sorted(set(t.split(":")[0] for t in groups[n - 1]["target_items"]) & got)
(OUT / "questions.json").write_text(json.dumps(groups, ensure_ascii=False, indent=1), encoding="utf-8")
(OUT / "meta.json").write_text(json.dumps({"version": GEN_VERSION, "usage": usage, "pool": sorted(pool), "items": meta}, ensure_ascii=False, indent=1), encoding="utf-8")
ok = sum(1 for m in meta if m.get("k_actual") == m.get("k_target"))
print("질문", len(groups), "| 목표 k 와 실제 k 일치", ok, "| 판별 불가", len(rep.get("errors") or []) + len(rep.get("undetermined") or []))
