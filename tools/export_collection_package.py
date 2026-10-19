"""수집용 묶음 내보내기 — 고정 질문 세트(326)를 사람이 웹브라우저(Claude·Gemini 등)에 직접 넣어 답을 받을 수 있는 파일로 만든다 (AI 호출 없음).

    python -X utf8 tools/export_collection_package.py                      # → 참고자료/질문세트_326_생성프롬프트v2/수집용/
    python -X utf8 tools/export_collection_package.py --answers 8c61f117e661   # 추가로 실행의 답변을 참고자료/질문세트_326_생성프롬프트v2/답변/<모델>.csv 로 보관

묶음: README.md · questions.md(턴 순서대로 원문) · questions.csv · 첨부/(Qn_이름) · 참조자료/(긴 문서 TXT) · answers_<모델>.csv(답 적을 파일, 턴마다 한 줄).
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SET_DIR = ROOT / "참고자료" / "질문세트_326_생성프롬프트v2"
OUT = SET_DIR / "수집용"
MODELS = ("claude", "gemini")
MODEL_LABEL = {"claude": "Claude (claude.ai 웹)", "gemini": "Gemini (gemini.google.com 웹)", "gpt": "ChatGPT (웹)", "aiona": "AIONA (웹)"}


def kind(g):
    return "긴 문서" if g["turns"][0].get("reference") else ("대화" if len(g["turns"]) > 1 else "단일")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--answers", help="실행 id — 그 실행의 모델별 답변을 참고자료/질문세트_326_생성프롬프트v2/답변/ 에 CSV 로 보관")
    args = ap.parse_args()
    groups = json.load(open(SET_DIR / "questions.json", encoding="utf-8"))
    (OUT / "첨부").mkdir(parents=True, exist_ok=True)
    (OUT / "참조자료").mkdir(parents=True, exist_ok=True)
    md = ["# 질문 326개 — 웹브라우저에 직접 넣을 때 쓰는 파일 (세트 qs-b468b011e6c3)", "",
          "질문 원문(```text``` 안)을 한 글자도 바꾸지 않고 그대로 보냅니다. 앞뒤에 지시문을 붙이지 않습니다.",
          "**대화**는 채팅 하나에서 1턴부터 순서대로 보내고(앞 턴 답을 받은 뒤 다음 턴), **단일·긴 문서**는 질문마다 새 채팅입니다.",
          "첨부가 있으면 `첨부/` 파일을 질문과 함께 올리고, 긴 문서는 `참조자료/` 의 TXT 를 파일로 올린 뒤 질문을 보냅니다.",
          "답은 답변 맨 아래 복사 버튼으로 전체를 복사해 `answers_<모델>.csv` 의 answer 칸에 그대로 붙여 넣습니다(턴마다 한 줄).", ""]
    rows = []
    for g in groups:
        k = kind(g); qid = g["id"]
        md.append(f"## Q{qid}  ({k}{', ' + str(len(g['turns'])) + '턴' if k == '대화' else ''}) — {g.get('topic') or ''}")
        for t in g["turns"]:
            n = t.get("turn") or 1
            case_id = f"Q{qid}_T{n}"
            att_names, ref_name = [], ""
            for a in t.get("attachments") or []:
                name = f"Q{qid}_{a.get('name')}"
                src = SET_DIR / "files" / (a.get("path") or "")
                if src.exists():
                    shutil.copyfile(src, OUT / "첨부" / name)
                att_names.append(name)
            if isinstance(t.get("reference"), str) and t["reference"].strip():
                ref_name = f"Q{qid}_참조자료.txt"
                (OUT / "참조자료" / ref_name).write_text(t["reference"], encoding="utf-8")
            if k == "대화":
                md.append(f"### Q{qid} {n}턴  (같은 채팅에서 이어서)")
            if att_names:
                md.append("첨부: " + ", ".join(f"`첨부/{x}`" for x in att_names) + " 를 질문과 함께 올립니다.")
            if ref_name:
                md.append(f"참조 자료: `참조자료/{ref_name}` 를 파일로 올린 뒤 아래 질문을 보냅니다 (문서가 길어 붙여 넣기 대신 파일).")
            md += ["```text", t.get("question") or "", "```", ""]
            rows.append({"case_id": case_id, "질문": f"Q{qid}", "턴": n, "종류": k, "첨부": ";".join(att_names), "참조자료": ref_name, "question": t.get("question") or ""})
    (OUT / "questions.md").write_text("\n".join(md), encoding="utf-8")
    with (OUT / "questions.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    for m in MODELS:
        p = OUT / f"answers_{m}.csv"
        if p.exists():   # 이미 채우고 있는 파일은 덮어쓰지 않는다
            continue
        with p.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f); w.writerow(["case_id", "answer", "chat_url", "note"])
            for r in rows:
                w.writerow([r["case_id"], "", "", ""])
    n_conv = sum(1 for g in groups if kind(g) == "대화"); n_long = sum(1 for g in groups if kind(g) == "긴 문서"); n_att = sum(1 for r in rows if r["첨부"])
    readme = f"""# 질문 326개 수집용 묶음 (세트 qs-b468b011e6c3) — Claude·Gemini 웹브라우저 수집

| 파일 | 내용 |
|---|---|
| `questions.md` | 질문 원문 — 턴 순서대로, 첨부·참조 자료 표시 (읽기용) |
| `questions.csv` | 같은 내용 표 (case_id · 질문 · 턴 · 종류 · 첨부 · 참조자료 · question) |
| `첨부/` | 첨부 질문 {n_att}턴의 파일 (`Q30_주차장_공사공지.txt` 처럼 질문 번호가 앞에 붙음) |
| `참조자료/` | 긴 문서 질문 {n_long}개의 문서 TXT (약 50만 자 — 파일로 올림) |
| `answers_claude.csv` · `answers_gemini.csv` | **답을 적어 돌려줄 파일** — case_id(턴마다 한 줄, {len(rows)}줄)는 채워져 있고 answer 칸만 채움. chat_url·note 는 선택 |

## 보내는 법

- 시스템 프롬프트·지시문 없이 질문 원문을 그대로 보냅니다. 모델 설정은 지금 쓰는 기본값(Claude Sonnet 5.5, Gemini 3.6 Flash)으로 둡니다.
- **단일 질문·긴 문서**: 질문마다 새 채팅.
- **대화({n_conv}개)**: 채팅 하나에서 1턴부터 마지막 턴까지 순서대로. 앞 턴 답을 받은 뒤 다음 턴을 보냅니다. 중간 턴만 따로 새 채팅에서 받지 않습니다(실패하면 그 대화 전체를 새 채팅에서 처음부터).
- **첨부**: `첨부/` 파일을 질문과 함께 올립니다. **긴 문서**: `참조자료/Qn_참조자료.txt` 를 파일로 올린 뒤 질문을 보냅니다.
- 답은 답변 맨 아래의 복사 버튼으로 전체를 복사해 answer 칸에 그대로 붙여 넣습니다(줄바꿈 포함, 고치지 않음). 대화는 턴마다 그 턴의 답만 적습니다.
- 빈 출력 요구 질문("아무것도 출력하지 마")은 답이 비어 있으면 answer 를 비우고 note 에 `빈 답` 이라고 적습니다. 오류 화면이면 note 에 `오류 화면`.
- 가능하면 chat_url 에 채팅 주소를 적어 둡니다(다시 복사할 때 씀).

## 돌려주는 법

채운 `answers_claude.csv`·`answers_gemini.csv` 를 이 폴더에 그대로 두고 push 합니다. 받으면 평가 PC 에서 실행에 올리고(CODE + 의미 평가) 네 모델 결과를 만듭니다.
"""
    (OUT / "README.md").write_text(readme, encoding="utf-8")
    print(f"수집용 묶음: {OUT} | 질문 {len(groups)} · 턴 {len(rows)} · 대화 {n_conv} · 긴 문서 {n_long} · 첨부 턴 {n_att}")
    if args.answers:
        run = json.load(open(ROOT / "data" / "comparison_runs" / f"{args.answers}.json", encoding="utf-8"))
        out = SET_DIR / "답변"; out.mkdir(exist_ok=True)
        for p in run["providers"]:
            with (out / f"{p}.csv").open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f); w.writerow(["case_id", "answer", "status", "collected_at", "source"])
                n = 0
                for r in run["datasets"][p]:
                    if not r:
                        continue
                    col = r.get("collection") or {}
                    w.writerow([r["case_id"], r.get("answer") or "", col.get("status") or "", col.get("collected_at") or r.get("collected_at") or "", r.get("source") or ""])
                    n += 1
            print(f"답변 보관: {out / (p + '.csv')} ({n}줄, 실행 {args.answers}, 출처 {run.get('models', {}).get(p) or p})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
