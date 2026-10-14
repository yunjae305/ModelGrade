"""첨부파일 글자 추출 — 파일에서 J8 첨부 항목({name, type, content, sha256, size, path, extractor})을 만든다 (API 호출 없음).

    python tools/attachment_text.py 견적서.pdf 회의록.docx 메모.txt [--root 폴더] [--json]

- txt·md·csv·json: 파일 글자 그대로 (UTF-8).
- docx: 표준 라이브러리(zip + XML)로 문단(w:p)마다 한 줄, 표 칸(w:tc)은 ' | ' 로 잇는다.
- pdf: pypdf 가 설치돼 있을 때만 쪽마다 글자를 뽑는다 (requirements 에 없는 선택 기능). 없으면 content 없음 + extractor 에 사유.
  스캔 PDF 처럼 글자 층이 없으면 빈 글자 — 내용이 없다고 남기고 OCR 로 대신하지 않는다.
- 이미지: 글자를 뽑지 않는다 (content 없음). 이미지 이해는 실제 파일을 보내야 한다 — webapp/attachment_delivery.
뽑은 글자는 질문 세트의 attachments[].content 에 넣어 쓴다: 평가 모델은 J8 judge_view 로, 답변 수집은 attachment_delivery 로 받는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional

TEXT_TYPES = {"txt", "md", "csv", "json", "tsv"}
IMAGE_TYPES = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    words = lambda part: "".join(unescape(t) for t in re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", part, flags=re.S))
    lines = []
    # 본문의 표(w:tbl)는 행(w:tr)마다 칸(w:tc)을 ' | ' 로, 그 밖의 문단(w:p)은 한 줄씩 — 표 안 문단을 따로 세지 않게 표를 먼저 찾는다
    for block in re.finditer(r"<w:tbl>.*?</w:tbl>|<w:p[ >].*?</w:p>", xml, flags=re.S):
        part = block.group(0)
        if part.startswith("<w:tbl>"):
            for row in re.findall(r"<w:tr[ >].*?</w:tr>", part, flags=re.S):
                lines.append(" | ".join(words(cell) for cell in re.findall(r"<w:tc[ >].*?</w:tc>", row, flags=re.S)))
        else:
            lines.append(words(part))
    return "\n".join(line for line in lines if line.strip())


def extract(path: Path, root: Optional[Path] = None) -> Dict[str, Any]:
    data = path.read_bytes()
    suffix = path.suffix.lower().lstrip(".")
    entry: Dict[str, Any] = {"name": path.name, "type": "image" if suffix in IMAGE_TYPES else suffix or "file",
                             "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                             "path": path.relative_to(root).as_posix() if root and path.is_relative_to(root) else path.name}
    if suffix in TEXT_TYPES:
        entry.update(content=data.decode("utf-8-sig"), extractor="text-utf8")
    elif suffix == "docx":
        entry.update(content=_docx_text(path), extractor="docx-xml-stdlib")
    elif suffix == "pdf":
        try:
            import pypdf
        except ImportError:
            entry.update(content=None, extractor="unavailable: pypdf 가 설치돼 있지 않음 (선택 기능)")
            return entry
        reader = pypdf.PdfReader(str(path))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        entry.update(content="\n\n".join(f"[{i}쪽]\n{text}" for i, text in enumerate(pages, 1) if text) or None,
                     extractor=f"pypdf {pypdf.__version__}", pages=len(pages))
        if entry["content"] is None:
            entry["extractor"] += " — 글자 층 없음 (스캔 PDF 일 수 있음, OCR 로 대신하지 않음)"
    elif entry["type"] == "image":
        entry.update(content=None, extractor="image — 글자 추출 안 함 (실제 파일을 보내야 이미지 이해를 평가할 수 있음)")
    else:
        entry.update(content=None, extractor=f"unsupported: {suffix}")
    return entry


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--root", type=Path, help="path 를 이 폴더 기준 상대 경로로 남김 (예: data/attachments)")
    args = parser.parse_args(argv)
    root = args.root.resolve() if args.root else None
    entries = [extract(f.resolve(), root) for f in args.files]
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(entries, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
