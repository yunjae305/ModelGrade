"""첨부 파일의 쪽 이미지 — pdf·docx 를 쪽마다 PNG 로 그려 두 심판(Decisions input_image · Codex -i)이 원본 모양 그대로 보게 한다 (사용자 요청 2026-10-21).

- pdf → pypdfium2 로 쪽마다 PNG (Decisions 가 쓰던 것과 같은 배율 1.5, 최대 20쪽)
- docx → Word(COM, 창 없이)로 pdf 로 바꾼 뒤 위와 같이. Word 가 없으면 빈 목록(글로만 간다)
- png·jpg 등 이미지 → 그 파일 하나
결과는 <files_dir>/_pages/<파일이름>.p<n>.png 에 두고 원본보다 새로우면 다시 그리지 않는다. 변환은 한 번에 한 스레드만(Word COM).
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
PAGE_EXT = (".pdf", ".docx")
MAX_PAGES = 20
RENDER_SCALE = 1.5
_LOCK = threading.Lock()
_NOTES: Dict[str, str] = {}


def enabled() -> bool:
    """CODEX_ATTACH_PAGES=0 이면 쪽 이미지를 붙이지 않는다 (A/B 처럼 조건을 고정할 때)."""
    return os.environ.get("CODEX_ATTACH_PAGES", "1").strip() not in ("0", "false", "no")


def cache_dir(files_dir: Path) -> Path:
    return Path(files_dir) / "_pages"


def _fresh(targets: List[Path], source: Path) -> bool:
    return bool(targets) and all(t.is_file() and t.stat().st_mtime >= source.stat().st_mtime for t in targets)


_PAGE_NO = re.compile(r"\.p(\d+)\.png$")


def _page_no(p: Path) -> int:
    m = _PAGE_NO.search(p.name)
    return int(m.group(1)) if m else 0


def _existing_pages(source: Path, out_dir: Path) -> List[Path]:
    pages = sorted((p for p in out_dir.glob(f"{source.name}.p*.png") if _PAGE_NO.search(p.name)), key=_page_no)
    return pages if _fresh(pages, source) else []


def _render_pdf(pdf: Path, source: Path, out_dir: Path) -> List[Path]:
    import pypdfium2
    doc = pypdfium2.PdfDocument(str(pdf))
    out: List[Path] = []
    for i in range(min(len(doc), MAX_PAGES)):
        target = out_dir / f"{source.name}.p{i + 1}.png"
        doc[i].render(scale=RENDER_SCALE).to_pil().save(str(target), format="PNG")
        out.append(target)
    return out


def _docx_to_pdf(docx: Path, out_dir: Path) -> Optional[Path]:
    """Word 로 docx → pdf (창 없이). 한 번에 하나만."""
    target = out_dir / f"{docx.name}.pdf"
    if target.is_file() and target.stat().st_mtime >= docx.stat().st_mtime:
        return target
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return None
    pythoncom.CoInitialize()
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        try:
            doc = word.Documents.Open(str(docx.resolve()), ReadOnly=True, AddToRecentFiles=False)
            try:
                doc.SaveAs2(str(target.resolve()), FileFormat=17)   # 17 = wdFormatPDF
            finally:
                doc.Close(False)
        finally:
            word.Quit()
    finally:
        pythoncom.CoUninitialize()
    return target if target.is_file() else None


def page_images(path: Path, files_dir: Path) -> Tuple[List[Path], str]:
    """첨부 하나 → (쪽 이미지 경로들, 어떻게 됐는지 한 줄). 이미지 파일은 그대로, pdf·docx 는 그려서, 그 밖은 빈 목록."""
    path = Path(path)
    if not path.is_file():
        return [], f"{path.name}: 파일 없음"
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return [path], f"{path.name}: 이미지 원본 1장"
    if ext not in PAGE_EXT:
        return [], f"{path.name}: 글 파일 (쪽 이미지 없음)"
    out_dir = cache_dir(files_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        cached = _existing_pages(path, out_dir)
        if cached:
            return cached, f"{path.name}: 쪽 이미지 {len(cached)}장(저장된 것)"
        try:
            pdf = path if ext == ".pdf" else _docx_to_pdf(path, out_dir)
            if pdf is None:
                return [], f"{path.name}: docx → pdf 변환 불가(Word 없음) — 글로만"
            pages = _render_pdf(pdf, path, out_dir)
            note = f"{path.name}: {'PDF' if ext == '.pdf' else 'DOCX(Word 로 pdf)'} {len(pages)}쪽을 이미지로"
        except Exception as exc:   # 못 그리면 글로만 — 어떻게 됐는지 남긴다
            return [], f"{path.name}: 쪽 이미지 못 그림 ({type(exc).__name__}: {str(exc)[:80]})"
    _NOTES[path.name] = note
    return pages, note


def prerender(files_dir: Path) -> Dict[str, str]:
    """세트의 pdf·docx 를 미리 다 그려 둔다 — 네 모델이 동시에 돌 때 Word 를 여러 번 띄우지 않게 (judge_v4.main 이 스레드 전에 부른다)."""
    notes: Dict[str, str] = {}
    files_dir = Path(files_dir)
    if not files_dir.is_dir() or not enabled():
        return notes
    for p in sorted(files_dir.iterdir()):
        if p.suffix.lower() in PAGE_EXT:
            _, notes[p.name] = page_images(p, files_dir)
    return notes
