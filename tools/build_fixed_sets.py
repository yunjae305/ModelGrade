"""고정 질문 세트 만들기 — 간단 평가 50문항 · 정밀 평가 300문항 (사용자 결정 2026-10-14: 최소 기준 50, 최대 기준 300).

후보 — 공개 세트를 먼저 쓰고, 공개 세트에 없는 기준만 우리 질문으로 채운다 (사용자 결정 2026-10-14):
- 공개: IFEval-Ko(allganize, Apache-2.0) · KoMT-Bench(LGAI-EXAONE, LGPL-3.0) · LogicKor(maywell, CC BY-SA 4.0) ·
  Ko-Arena-Hard-Auto(qwopqwop, Apache-2.0) · KOFFVQA(maum-ai, Apache-2.0, 이미지) · HAERAE-Vision(HAERAE-HUB, CC BY 4.0, 이미지) ·
  RAG-Evaluation-Dataset-KO(allganize, MIT — 질문, PDF 는 각 기관 사이트 원본, 20쪽 이하만)
- 우리: 질문 세트 300 (형식 조건·10턴 대화 J6·긴 문서 J7 등) + 보충 질문(어느 후보에도 없던 '표 열 수' 조건)
적용 여부는 평가 때와 같은 코드 규칙(tools/check_question_set.check_items)으로 정하고 모델은 부르지 않는다. 답변 모양으로만 적용이 정해지는
검사(코드 펜스 짝·각주 등)는 모든 답변에서 늘 검사되므로 고르는 기준에서 뺀다.

고르기 (모든 세부 기준을 반드시 검사 — 사용자 요청 2026-10-14):
1. 덮기 — 기준마다 1번 이상이 될 때까지 공개 후보에서 먼저, 남은 기준만 우리 질문에서 고른다.
2. 고르게 채우기 — 정한 문항 수까지, 적게 적용된 기준을 더 거치는 질문(가중치 1/(지금 적용 수+1)²)을 고른다. 우리 질문은 점수를 절반으로 쳐서
   공개 질문이 먼저 들어가게 한다.
고른 질문의 첨부만 받는다 — 이미지는 HF, PDF 는 기관 페이지의 첨부 링크(문서 목록의 쪽수와 같아야 씀). 받지 못한 질문은 빼고 다시 고른다.
결과: examples/fixed_sets/{simple,precise}/questions.json(질문 생성 세트와 같은 id/turns 모양) + files/ + selection.json.

    python tools/build_fixed_sets.py
"""
from __future__ import annotations

import ast
import base64
import hashlib
import html
import io
import json
import re
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.attachment_text import extract  # noqa: E402
from tools.check_question_set import check_items, unit_of  # noqa: E402
from tools.min_question_count import all_targets, question_units  # noqa: E402
from webapp.question_generation import _summary, check, flatten  # noqa: E402

FIXED_SET_VERSION = "fixed-set-v2"
OUT = ROOT / "examples" / "fixed_sets"
SOURCES = OUT / "sources"
SET300 = ROOT / "참고자료" / "수집결과" / "2026-10-14-질문세트300"
KINDS = {"simple": {"size": 50, "label": "간단 평가"}, "precise": {"size": 300, "label": "정밀 평가"}}
MAX_PDF_PAGES = 20
OURS = "질문 세트 300"
SUPPLEMENT = "보충 질문"
OURS_WEIGHT = 0.5   # 고르게 채우기에서 우리 질문 점수 — 공개 질문이 먼저 들어가게
# 어느 후보에도 없던 조건(2-2.table:table_column_count)을 거치는 보충 질문 — 우리가 쓴 질문 (사용자 결정: 부족한 부분은 우리 질문으로)
SUPPLEMENT_QUESTIONS = [
    "서울·부산·제주의 대표 관광지를 3열 표로 정리해줘. 열은 도시, 관광지, 추천 이유로 해줘.",
    "커피·녹차·홍차의 카페인 특징을 비교해서 표로 정리하고 열은 4개로 해줘.",
    "아침 운동 세 가지(걷기·스트레칭·줄넘기)를 2열 표로 정리해줘. 왼쪽 열은 운동, 오른쪽 열은 효과로 해줘.",
]
LICENSES = {"IFEval-Ko": "allganize/IFEval-Ko · Apache-2.0", "KoMT-Bench": "LGAI-EXAONE/KoMT-Bench · LGPL-3.0",
            "LogicKor": "maywell/LogicKor · CC BY-SA 4.0", "Ko-Arena-Hard": "qwopqwop/ko-arena-hard-auto-v0.1 · Apache-2.0",
            "KOFFVQA": "maum-ai/KOFFVQA_Data · Apache-2.0", "HAERAE-Vision": "HAERAE-HUB/HAERAE-VISION · CC BY 4.0",
            "RAG-Evaluation-KO": "allganize/RAG-Evaluation-Dataset-KO · MIT (질문) — PDF 는 각 기관 원본"}
UA = {"User-Agent": "Mozilla/5.0 (ModelGrade fixed-set builder)"}


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def candidates():
    """후보 질문 묶음 — source(출처)·pool(public|ours)·fetch(첨부를 받는 방법)."""
    groups = []

    def add(turns, source, kind="", fetch=None, pool="public"):
        groups.append({"type": kind, "topic": kind, "difficulty": "", "trap": "", "target_items": [],
                       "turns": [{"turn": i, **t} for i, t in enumerate(turns, 1)], "source": source, "pool": pool,
                       **({"fetch": fetch} if fetch else {})})

    for r in json.loads((SOURCES / "ifeval_ko.json").read_text(encoding="utf-8")):
        add([{"question": r["prompt"]}], f"IFEval-Ko key {r['key']}", "지시 따르기")
    for r in _lines(SOURCES / "komt_question_ko.jsonl"):
        add([{"question": q} for q in r["turns"]], f"KoMT-Bench {r['question_id']}", r.get("category") or "")
    for r in _lines(SOURCES / "logickor_questions.jsonl"):
        add([{"question": q} for q in r["questions"]], f"LogicKor {r['id']}", r.get("category") or "")
    for r in _lines(SOURCES / "ko_arena_hard_questions.jsonl"):
        add([{"question": t["ko_content"]} for t in r["turns"]], f"Ko-Arena-Hard {r['question_id']}", r.get("ko_cluster") or "")
    for r in json.loads((SOURCES / "koffvqa_questions.json").read_text(encoding="utf-8")):   # 이미지는 고른 문항만 받는다
        add([{"question": r["question"], "attachments": [{"name": f"KOFFVQA_{r['index']}.jpg", "type": "image"}]}],
            f"KOFFVQA {r['index']}", r.get("category") or "", {"koffvqa": r["_position"]})
    for position, r in enumerate(json.loads((SOURCES / "haerae-vision.json").read_text(encoding="utf-8"))):
        images = r["images"] if isinstance(r["images"], list) else ast.literal_eval(r["images"] or "[]")
        add([{"question": r.get("question_explicit") or r["question_original"],
              "attachments": [{"name": f"HAERAE_{r['question_idx']}_{i + 1}.jpg", "type": "image"} for i in range(max(1, len(images)))]}],
            f"HAERAE-Vision {r['question_idx']}", r.get("category") or "", {"haerae": position})
    rag = json.loads((SOURCES / "rag_evaluation_ko.json").read_text(encoding="utf-8"))
    docs = {d["file_name"]: d for d in rag["documents"]}
    for r in rag["questions"]:
        doc = docs.get(r["target_file_name"])
        if doc and int(doc.get("pages") or 0) <= MAX_PDF_PAGES:
            add([{"question": r["question"], "attachments": [{"name": r["target_file_name"], "type": "pdf"}]}],
                f"RAG-Evaluation-KO {r['domain']}", r["domain"], {"rag": r["target_file_name"]})
    for g in json.loads((SET300 / "questions.json").read_text(encoding="utf-8")):
        groups.append({**g, "source": f"{OURS} #{g['id']}", "pool": "ours"})
    for i, q in enumerate(SUPPLEMENT_QUESTIONS, 1):
        add([{"question": q}], f"{SUPPLEMENT} {i}", "형식", pool="ours")
    for i, g in enumerate(groups, 1):
        g["id"] = i
    return groups


def select(order, targets, pools, names, size, excluded):
    """1) 모든 기준 1번 이상(공개 먼저) 2) 정한 수까지 적게 적용된 기준을 더 거치게. 결정적(같은 입력이면 같은 결과)."""
    count = Counter()
    chosen, left = [], [u for u in order if u not in excluded]
    for pool in ("public", "ours"):   # 1) 덮기
        while len(chosen) < size:
            gain = {u: sum(1 for t in targets[u] if t in names and count[t] == 0) for u in left if pools[u] == pool}
            best = max(gain.values(), default=0)
            if best == 0:
                break
            pick = min(u for u in gain if gain[u] == best)
            chosen.append(pick)
            left.remove(pick)
            count.update(t for t in targets[pick] if t in names)
    while len(chosen) < size and left:   # 2) 고르게 채우기
        def score(u):
            s = sum(1 / (count[t] + 1) ** 2 for t in targets[u] if t in names)
            return s * (OURS_WEIGHT if pools[u] == "ours" else 1.0)
        pick = max(left, key=lambda u: (score(u), -order.index(u)))
        chosen.append(pick)
        left.remove(pick)
        count.update(t for t in targets[pick] if t in names)
    return chosen, count


# --- 첨부 받기 ---------------------------------------------------------------------------------
def _get(url, timeout=60):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def _store(data: bytes, name: str, files: Path):
    """받은 파일을 sha256 이름으로 두고 J8 첨부 항목({name, type, content, sha256, size, path})을 만든다."""
    with tempfile.TemporaryDirectory() as temp:
        temp_path = Path(temp) / name
        temp_path.write_bytes(data)
        item = extract(temp_path, Path(temp))
    sha = hashlib.sha256(data).hexdigest()
    stored = f"{sha}{Path(name).suffix.lower()}"
    (files / stored).write_bytes(data)
    return {**item, "name": name, "path": stored, "sha256": sha, "size": len(data)}


_PDF_CACHE = {}


def _rag_pdf(file_name: str, docs) -> bytes:
    """기관 게시판 페이지에서 이름이 가장 비슷한 PDF 첨부 링크를 찾아 받는다 — 문서 목록의 쪽수와 같아야 쓴다."""
    if file_name in _PDF_CACHE:
        return _PDF_CACHE[file_name]
    from pypdf import PdfReader
    doc = docs[file_name]
    page = _get(doc["url"]).decode("utf-8", "replace")
    want = set(re.sub(r"\s+", "", re.sub(r"^\d{4}년\s*\d+월_", "", file_name)).lower())
    links = []
    for href, text in re.findall(r'href="([^"]+)"[^>]*>([^<]{1,200})<', page):
        text = html.unescape(text).strip()
        if ".pdf" in text.lower() or ".pdf" in href.lower() or "down" in href.lower():
            score = len(set(re.sub(r"\s+", "", text).lower()) & want) / max(1, len(want))
            links.append((score, urllib.parse.urljoin(doc["url"], html.unescape(href))))
    for _, url in sorted(links, reverse=True)[:6]:
        try:
            data = _get(url)
            if data[:4] == b"%PDF" and len(PdfReader(io.BytesIO(data)).pages) == int(doc["pages"]):
                _PDF_CACHE[file_name] = data
                return data
        except Exception:
            continue
    raise RuntimeError(f"PDF 를 찾지 못함: {file_name}")


def materialize(group, files: Path, rag_docs, koffvqa, haerae_url):
    """고른 질문의 첨부를 실제 파일로 — 첨부 항목을 채운 질문 묶음."""
    g = json.loads(json.dumps({k: v for k, v in group.items() if k not in ("fetch", "pool")}, ensure_ascii=False))
    fetch = group.get("fetch") or {}
    for turn in g["turns"]:
        if not turn.get("attachments"):
            continue
        if group["pool"] == "ours":   # 우리 세트 파일은 그대로 복사
            for a in turn["attachments"]:
                shutil.copyfile(SET300 / "files" / a["path"], files / a["path"])
        elif "koffvqa" in fetch:
            turn["attachments"] = [_store(base64.b64decode(koffvqa(fetch["koffvqa"])), turn["attachments"][0]["name"], files)]
        elif "haerae" in fetch:
            urls = haerae_url(fetch["haerae"])
            turn["attachments"] = [_store(_get(u), a["name"], files) for a, u in zip(turn["attachments"], urls)]
        elif "rag" in fetch:
            turn["attachments"] = [_store(_rag_pdf(fetch["rag"], rag_docs), fetch["rag"], files)]
    return g


def main() -> int:
    groups = candidates()
    report = check_items(flatten(groups), pipeline="items")
    order, targets, _ = question_units(report)
    names = set(all_targets(report))
    by_unit = {}
    for row in report["cases"]:
        by_unit[unit_of(row)] = int(str(row.get("case_id", "")).split("_T")[0].lstrip("Q"))
    pools = {u: groups[by_unit[u] - 1]["pool"] for u in order}
    available = Counter(t for u in order for t in targets[u])
    public = Counter(t for u in order if pools[u] == "public" for t in targets[u])
    print(f"후보 {len(groups)}개 · 세부 기준 {len(names)}개 · 공개로 덮는 기준 {sum(1 for t in names if public[t])} · "
          f"어디에도 없는 기준 {sorted(t for t in names if not available[t])}")
    rag = json.loads((SOURCES / "rag_evaluation_ko.json").read_text(encoding="utf-8"))
    rag_docs = {d["file_name"]: d for d in rag["documents"]}
    def rows_api(dataset, position):   # 데이터셋 서버에서 그 행만 — 이미지 원본·주소는 고른 문항만 받는다
        q = urllib.parse.quote(dataset, safe="")
        return json.loads(_get(f"https://datasets-server.huggingface.co/rows?dataset={q}&config=default&split=train&offset={position}&length=1"))["rows"][0]["row"]

    def koffvqa(position):   # KOFFVQA 는 이미지가 base64 글자로 들어 있다
        return rows_api("maum-ai/KOFFVQA_Data", position)["image"]

    def haerae_url(position):   # 데이터셋 서버의 이미지 주소는 시간이 지나면 바뀐다 — 고른 행만 새로 묻는다
        return [image["src"] for image in rows_api("HAERAE-HUB/HAERAE-VISION", position)["images"]]

    excluded = set()
    for kind, spec in KINDS.items():
        for _ in range(10):   # 받지 못한 첨부 질문은 빼고 다시 고른다
            best, count = select(order, targets, pools, names, spec["size"], excluded)
            out = OUT / kind
            if out.exists():
                shutil.rmtree(out)
            (out / "files").mkdir(parents=True)
            chosen_groups, failed = [], []
            for gid in sorted({by_unit[u] for u in best}):
                try:
                    chosen_groups.append(materialize(groups[gid - 1], out / "files", rag_docs, koffvqa, haerae_url))
                except Exception as exc:
                    failed.append(gid)
                    print(f"  첨부를 받지 못해 다시 고름: {groups[gid - 1]['source']} — {type(exc).__name__}: {str(exc)[:80]}")
            if not failed:
                break
            excluded |= {u for u in order if by_unit[u] in failed}
        for new_id, g in enumerate(chosen_groups, 1):
            g["id"] = new_id
        (out / "questions.json").write_text(json.dumps(chosen_groups, ensure_ascii=False, indent=1), encoding="utf-8")
        missing = sorted(t for t in names if count[t] == 0)
        sources = Counter(g["source"].split(" ")[0] if not g["source"].startswith((OURS, SUPPLEMENT)) else
                          (OURS if g["source"].startswith(OURS) else SUPPLEMENT) for g in chosen_groups)
        set_report = check(chosen_groups)   # 버튼을 누를 때 다시 점검하지 않게 요약·적용 현황을 미리 둔다
        spread = sorted(count[t] for t in names)
        selection = {"version": FIXED_SET_VERSION, "kind": kind, "label": spec["label"], "size": spec["size"], "questions": len(chosen_groups),
                     "summary": _summary(chosen_groups, set_report), "coverage": set_report["coverage"],
                     "turns": sum(len(g["turns"]) for g in chosen_groups), "targets": len(names), "sources": dict(sources),
                     "covered": len(names) - len(missing), "missing": missing,
                     "applications": {"min": spread[0], "median": spread[len(spread) // 2], "max": spread[-1]},
                     "per_target": {t: count[t] for t in sorted(names)}, "candidates": len(groups),
                     "licenses": {s: LICENSES[s] for s in sources if s in LICENSES},
                     "short": {t: count[t] for t in sorted(names) if count[t] < 3}}
        (out / "selection.json").write_text(json.dumps(selection, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{spec['label']}: 질문 {selection['questions']}개(턴 {selection['turns']}) · 출처 {dict(sources)} · 기준 {selection['covered']}/{len(names)} · "
              f"기준별 적용 수 최소 {spread[0]} · 중앙 {spread[len(spread) // 2]} · 3번 미만 {len(selection['short'])}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
