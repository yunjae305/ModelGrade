"""통제 실험 질문 326개 — 기본 질문 18개 × 실제 k_var 1~16 (288) + 첨부·커버리지 보충 12 + 기존 326 세트의 긴 입력 10·대화 16.
1) 생성: 호출마다 기본 질문 2개(변형 32개)  2) 코드 규칙으로 실제 k 를 세어 목표와 다르면 조건만 고침(최대 3회)
3) 기준별 적용 횟수를 세어 첨부(txt·docx·pdf·image)·부족 기준 보충 12개  4) 기존 26개와 합쳐 experiment2/questions.json
단계마다 저장 — 다시 돌리면 끝난 단계는 건너뜀.   python -X utf8 gen_experiment_v2.py"""
import json, sys, time
from collections import Counter
from pathlib import Path

ROOT = Path(r"C:\Users\winz\Desktop\quality-test-integrated")
sys.path.insert(0, str(ROOT))
from code_eval import criteria_registry as reg, semantic_items as si
from code_eval.llm_client import env_value
from webapp import question_generation as qg
from webapp.attachment_files import render_attachment

OUT = Path(__file__).parent / "experiment2"
(OUT / "files").mkdir(parents=True, exist_ok=True)
GEN_VERSION = "experiment-kvar-v2 (실제 k 1~16 × 18)"
LEVELS = list(range(1, 17))
catalog = qg.criteria_catalog()
code_all = {u[0] for u in reg.CODE_UNITS if u[2] == "all"}
always = {it.id for it in si.ITEMS if it.always}
ans_dep = {u[0] for u in reg.CODE_UNITS if u[2] == "answer"} | {"J5-3.a"}
VAR = {c["id"] for c in catalog["code"] if c["id"] not in code_all | ans_dep} | {i["id"] for i in catalog["items"] if i["id"] not in always | ans_dep}
POOL = {x for x in VAR if not x.startswith(("J6", "J7", "J8"))}
TYPES = ["문구 작성"] * 4 + ["자료 요약"] * 4 + ["정보 추출"] * 4 + ["계산·변환"] * 3 + ["조건 기반 판단"] * 3
SLOTS = [(f"B{n:02d}", TYPES[n - 1], "보통" if n % 2 else "어려움") for n in range(1, 19)]
CAT = "\n".join(qg._catalog_lines(qg.restrict_catalog(catalog, POOL), detailed=False))

GEN = """당신은 한국어 AI 답변 품질 평가의 통제 실험용 질문을 설계합니다. 결과는 JSON 하나로만 답합니다.

# 실험 목적
한 질문에 적용되는 평가 기준 수(k)가 늘 때 평가 결과가 어떻게 변하는지 봅니다. 같은 기본 질문을 두고 k 만 1부터 16까지 바꾼 변형 16개를 만듭니다.

# 이번에 만들 기본 질문 ({n}개)
{slots}

# 기본 질문 규칙
- 질문 안에 가상의 자료(공지·표·기록·목록 등, 300~800자)를 직접 넣습니다. 답은 그 자료만으로 정해져야 합니다(외부 검색·최신 정보 불필요). 가상이라고 밝힙니다.
- 정답 행동이 분명해야 합니다. 되물어야 하는 질문, 사람마다 정답이 갈리는 질문은 만들지 않습니다.
- 실제 사용자가 할 법한 자연스러운 문장으로 씁니다. 기본 질문끼리 주제가 겹치지 않게 합니다.
- '어려움'은 자료 속 함정(헷갈리는 비슷한 값, 조건에 따라 달라지는 계산, 예외 규정)으로 틀리기 쉽게 하되 요구는 명확하게 합니다.

# k 세는 법 (가장 중요)
- k = 질문에 실제로 적용되는 [점검 목록]의 서로 다른 기준 id 수입니다. CODE 검사는 검사 id 하나가 1개(같은 검사 안 조건 여러 개는 1개), 의미 항목은 항목 id 하나가 1개입니다.
- 기본 과제만으로 자동으로 적용되는 기준도 셉니다. 자료를 주고 일을 시키면 보통 2~4개가 자동으로 붙습니다(예: 자료 값을 옮기면 J5-2.a, 요약이면 J4-4.a·J5-2.b, 글 작성이면 J2-5.b·J4-3.a, 조건을 주면 J2-3.a). 그래서 k=1 은 자료를 아주 짧게 하고 조건 없이 한 가지만 묻는 가장 단순한 형태여야 합니다.
- 변형마다 target_items 에 실제로 적용될 기준 id 를 자동으로 붙는 것까지 포함해 정확히 k 개 씁니다.
- 유형 때문에 작은 k(예: 1·2)를 만들 수 없으면 오류를 내지 말고, 그 변형은 같은 자료로 가능한 가장 단순한 요청(예: 자료에서 값 하나를 그대로 묻기)으로 만들고 k 에 실제로 예상되는 값을 적습니다. 변형은 항상 16개를 만들고, 가능한 한 서로 다른 k(1~16)가 되게 합니다.

# 변형 규칙
- 같은 기본 질문의 변형 16개는 주제·자료·핵심 과제·정답의 핵심을 그대로 둡니다(k=1 은 자료를 줄일 수 있음). 바뀌는 것은 덧붙이는 조건뿐입니다.
- 조건은 서로 어긋나지 않고 한 질문으로 자연스럽게 읽혀야 합니다. 기본 질문마다 조건을 붙이는 순서를 다르게 합니다.
- 외부 검색·최신 사실·첨부파일 요구로 k 를 늘리지 않습니다. 답변 분량을 늘리라는 지시도 쓰지 않습니다.
- CODE 검사 조건은 [점검 목록]의 인식 규칙 표현을 그대로 씁니다.

# 헷갈리는 표현 피하기
- 꼭 넣을 문구와 쓰지 말 문구는 다른 문장으로 씁니다('A를 넣어줘. C라는 말은 쓰지 마.').
- '…지도 마', '형식은 쓰지 마' 대신 '표를 쓰지 마'처럼 무엇을 금지하는지 직접 씁니다.
- 자료 속 숫자가 답의 개수 요구로 읽히지 않게, 개수 요구는 '목록을 정확히 5개로'처럼 따로 씁니다.
- 입력 자료를 설명할 때 'JSON으로·표로' 같은 말을 쓰지 않습니다. 답 형식은 '답은 JSON으로 줘'처럼 따로 씁니다.

# 출력 형식 (JSON 하나만, 설명·코드 펜스 없이)
{{"bases": [{{"base_id": "B01", "type": "유형", "difficulty": "보통|어려움", "topic": "주제", "expected_answer": "자료로 정해지는 정답의 핵심",
  "variants": [{{"k": 1, "target_items": ["기준 id"], "added": "덧붙인 조건 요약", "question": "자료를 포함한 질문 원문 전체"}}, ... k 2~16 ...]}}]}}
- variants 는 k = 1~16 순서로 정확히 16개.

# 점검 목록 (이 기준들만 씀)
{catalog}

# 개수 (반드시 지킴)
bases {n}개, 각 variants 16개. 답하기 전에 세어 확인합니다."""

FIX = """다음은 통제 실험용 질문 변형들입니다. 각 변형의 실제 적용 기준 수(k)가 목표와 다릅니다. 조건(제약)만 더하거나 빼서 목표 k 에 맞게 고칩니다.
- 자료·주제·핵심 과제·정답의 핵심은 그대로 둡니다. 자연스러운 한 질문으로 읽혀야 합니다.
- '지금 적용된 기준'은 코드가 질문을 읽고 실제로 찾은 기준입니다. 늘릴 때는 아직 없는 기준의 조건을 [점검 목록]의 인식 규칙 표현대로 더하고, 줄일 때는 조건을 빼거나 자료를 줄입니다.
- 헷갈리는 표현 피하기: 필수 문구와 금지 문구는 다른 문장으로, 금지 대상은 직접, 자료 속 숫자와 개수 요구 구분, 입력 자료 설명에 'JSON으로·표로' 쓰지 않기.
결과는 JSON 하나: {{"fixes": [{{"id": 번호, "question": "고친 질문 원문 전체"}}, ...]}}

# 고칠 변형
{items}

# 점검 목록
{catalog}"""

FILL = """한국어 AI 답변 품질 평가용 질문 12개를 만듭니다. 결과는 JSON 하나로만 답합니다.
- 12개 모두 첨부파일 질문입니다. 종류별 3개씩: txt 3, docx 3, pdf 3, image 3. 답이 첨부 내용(표·도표·쪽·목록 구조)에 기대게 하고, 표나 도표를 읽어야 답이 나오게 합니다(J8-1.a·J8-2.a·J8-3.a 적용).
- 아래 '부족한 기준'이 각각 최소 1번 이상, 가능한 한 여러 번 적용되게 조건을 붙입니다. CODE 조건은 [점검 목록]의 인식 규칙 표현 그대로.
- 정답 행동이 분명하고 자연스러운 질문으로. 헷갈리는 표현(필수·금지 문구를 한 문장에, '…지도 마', 자료 숫자를 개수처럼) 피하기.
- attachments: content 에 파일 내용 전체(4,000자 이하). 줄 표기: '# 제목', '## 소제목', '- 항목', '| a | b |' 표 행, '[다음 쪽]' 쪽 나눔. image 가 막대그래프면 "chart": {{"title": "...", "labels": [...], "values": [숫자], "unit": "단위"}}.
출력: {{"questions": [{{"id": "F01", "type": "유형", "topic": "주제", "target_items": ["id"], "difficulty": "보통|어려움", "trap": "FAIL 이 되는 답",
  "turns": [{{"question": "질문 원문", "attachments": [{{"name": "파일이름.pdf", "type": "pdf", "content": "..."}}]}}]}}]}}

# 부족한 기준 (326개 전체에서 10회 미만)
{short}

# 점검 목록
{catalog}"""

config = {"key": env_value("OPENAI_API_KEY", env_path=ROOT / ".env"), "model": "gpt-6-luna"}
usage = {}
llm = qg.gpt_llm(config, usage)
def ask(prompt, max_tokens=60000):
    return qg._json_object(llm(prompt, max_tokens))
def actual_k(questions):
    rep = qg.check([{"id": i + 1, "turns": [{"turn": 1, "question": q}]} for i, q in enumerate(questions)])
    if rep.get("errors") or rep.get("undetermined"):
        raise SystemExit("판별 불가 — 토크나이저 캐시 확인")
    out = []
    for row in rep["cases"]:
        got = {t for t in (row.get("checks") or []) + (row.get("items") or []) if t in POOL}
        out.append(sorted(got))
    return out

# 1) 생성
for i in range(0, 18, 2):
    slots = SLOTS[i:i + 2]
    target = OUT / f"batch_{slots[0][0]}.json"
    if target.exists():
        continue
    data = ask(GEN.format(n=len(slots), slots="\n".join(f"- {b}: 유형 '{t}', 난이도 '{d}'" for b, t, d in slots), catalog=CAT))
    if not data.get("bases"):   # 거부·오류 응답은 저장하지 않고 다음 실행에 다시
        print(time.strftime("%H:%M:%S"), "생성 거부", slots[0][0], str(data)[:200], flush=True); continue
    target.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(time.strftime("%H:%M:%S"), "생성", slots[0][0], "~", slots[-1][0], "| 토큰", usage, flush=True)

if len(list(OUT.glob("batch_B*.json"))) < 9:
    raise SystemExit("생성 안 된 묶음이 있음 — 다시 실행하면 그 묶음만 다시 만든다")

# 2) 실제 k 맞추기
state_f = OUT / "variants.json"
if state_f.exists():
    V = json.loads(state_f.read_text(encoding="utf-8"))
else:
    V = []
    for f in sorted(OUT.glob("batch_B*.json")):
        for b in json.loads(f.read_text(encoding="utf-8")).get("bases") or []:
            for v in b.get("variants") or []:
                V.append({"id": len(V) + 1, "base_id": b.get("base_id"), "type": b.get("type"), "difficulty": b.get("difficulty"),
                          "topic": b.get("topic"), "expected_answer": b.get("expected_answer"), "k_target": v.get("k"),
                          "target_items": v.get("target_items") or [], "question": v.get("question") or "", "rounds": 0})
for rnd in range(1, 4):
    ks = actual_k([v["question"] for v in V])
    for v, got in zip(V, ks):
        v["actual_items"], v["k_actual"] = got, len(got)
    bad = [v for v in V if v["k_actual"] != v["k_target"]]
    state_f.write_text(json.dumps(V, ensure_ascii=False, indent=1), encoding="utf-8")
    print(time.strftime("%H:%M:%S"), f"{rnd}회차 점검: 목표와 다른 변형 {len(bad)}/{len(V)}", flush=True)
    if not bad:
        break
    for j in range(0, len(bad), 16):
        chunk = bad[j:j + 16]
        items = "\n\n".join(f"[id {v['id']}] 목표 k={v['k_target']} · 지금 k={v['k_actual']} · 지금 적용된 기준: {', '.join(v['actual_items'])}\n질문: {v['question']}" for v in chunk)
        try:
            fixes = ask(FIX.format(items=items, catalog=CAT), 40000).get("fixes") or []
        except Exception as exc:
            print("  고치기 호출 실패:", type(exc).__name__, flush=True); continue
        byid = {v["id"]: v for v in chunk}
        for fx in fixes:
            v = byid.get(fx.get("id"))
            if v and isinstance(fx.get("question"), str) and fx["question"].strip():
                v["question"], v["rounds"] = fx["question"], v["rounds"] + 1
    state_f.write_text(json.dumps(V, ensure_ascii=False, indent=1), encoding="utf-8")
ks = actual_k([v["question"] for v in V])
for v, got in zip(V, ks):
    v["actual_items"], v["k_actual"] = got, len(got)
state_f.write_text(json.dumps(V, ensure_ascii=False, indent=1), encoding="utf-8")

# 3) 기준별 적용 횟수 + 보충 12
old = json.loads((ROOT / "참고자료/수집결과/2026-10-14-질문세트326-세부기준/questions.json").read_text(encoding="utf-8"))
keep = [g for g in old if len(g.get("turns") or []) > 1 or any(t.get("reference") or t.get("long_reference") for t in g.get("turns") or [])]
groups = [{"id": v["id"], "type": v["type"], "topic": v["topic"], "difficulty": v["difficulty"], "target_items": v["target_items"],
           "turns": [{"turn": 1, "question": v["question"]}]} for v in V]
fill_f = OUT / "fill.json"
if not fill_f.exists():
    rep = qg.check([dict(g, id=n) for n, g in enumerate(groups + keep, 1)])   # 기존 26개의 원래 번호가 변형 번호와 겹치지 않게
    cnt = Counter()
    for row in rep["cases"]:
        cnt.update({t for t in (row.get("checks") or []) + (row.get("items") or []) if t in VAR})
    short = sorted((n for n in VAR if cnt[n] < 10), key=lambda n: cnt[n])
    data = ask(FILL.format(short="\n".join(f"- {n} (지금 {cnt[n]}회)" for n in short), catalog="\n".join(qg._catalog_lines(qg.restrict_catalog(catalog, VAR), detailed=False))))
    fill_f.write_text(json.dumps({"short": short, "data": data}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(time.strftime("%H:%M:%S"), "보충 생성 | 부족 기준", len(short), "| 토큰", usage, flush=True)
fill = json.loads(fill_f.read_text(encoding="utf-8"))
fills = []
for q in (fill["data"].get("questions") or [])[:12]:
    turns = []
    for t_i, t in enumerate(q.get("turns") or [], 1):
        files = [render_attachment(spec, OUT / "files") for spec in t.get("attachments") or []]
        turns.append({"turn": t_i, "question": t.get("question") or "", **({"attachments": files} if files else {})})
    fills.append({"id": 0, "type": q.get("type"), "topic": q.get("topic"), "difficulty": q.get("difficulty"), "trap": q.get("trap"),
                  "target_items": q.get("target_items") or [], "turns": turns, "role": "보충"})

# 4) 합치기 — 번호: 변형 1~288 · 보충 289~300 · 기존 26 = 301~326
final = groups + fills + [dict(g) for g in keep]
for n, g in enumerate(final, 1):
    g["id"] = n
(OUT / "questions.json").write_text(json.dumps(final, ensure_ascii=False, indent=1), encoding="utf-8")
rep = qg.check(final)
cnt = Counter()
for row in rep["cases"]:
    cnt.update({t for t in (row.get("checks") or []) + (row.get("items") or []) if t in VAR})
summary = {"version": GEN_VERSION, "questions": len(final), "variants": len(V), "fills": len(fills), "kept": len(keep), "usage": usage,
           "k_match": sum(v["k_actual"] == v["k_target"] for v in V), "k_actual_by_target": {k: sorted(v["k_actual"] for v in V if v["k_target"] == k) for k in LEVELS},
           "coverage_min": min(cnt[n] for n in VAR), "coverage_under10": sorted((n, cnt[n]) for n in VAR if cnt[n] < 10)}
(OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps({k: v for k, v in summary.items() if k != "k_actual_by_target"}, ensure_ascii=False))
for k in LEVELS:
    print(f"  목표 k={k:>2} → 실제", summary["k_actual_by_target"][k])
