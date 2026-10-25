"""첨부 파일의 쪽 이미지 — pdf·docx 를 쪽마다 PNG 로 그려 두 심판(Decisions input_image · Codex -i)이 원본 모양 그대로 보게 한다 (사용자 요청 2026-10-21).

- pdf → pypdfium2 로 쪽마다 PNG (Decisions 가 쓰던 것과 같은 배율 1.5, 최대 20쪽)
- docx → Word(COM, 창 없이)로 pdf 로 바꾼 뒤 위와 같이. Word 가 없으면 빈 목록(글로만 간다)
- png·jpg 등 이미지 → 그 파일 하나
그린 이미지는 저장하지 않는다 — 프로세스마다 임시 폴더에 두고 끝나면 지운다(사용자 10-21: 변환은 평가할 때만, 변환물은 남기지 않기).
Codex CLI 는 -i 에 파일 경로가 필요해서 호출 동안만 임시 파일로 있다. 같은 프로세스 안에서는 한 번 그린 것을 다시 쓴다. 변환은 한 번에 한 스레드만(Word COM).
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import tempfile
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


_TMP: Dict[str, Path] = {}


def cache_dir(files_dir: Path) -> Path:
    """이 프로세스의 임시 폴더(세트 폴더마다 하나) — 끝나면 통째로 지운다."""
    key = str(Path(files_dir).resolve())
    if key not in _TMP:
        _TMP[key] = Path(tempfile.mkdtemp(prefix="mg-pages-"))
    return _TMP[key]


def _cleanup() -> None:
    for d in _TMP.values():
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup)


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


def _soffice() -> Optional[str]:
    """LibreOffice 실행 파일 — PATH 의 soffice · libreoffice, Mac · Linux 기본 설치 위치. Word(COM)가 없는 Mac · Linux 에서 docx → pdf 에 쓴다 (10-25 밤, 할일 17번)."""
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for p in (Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"), Path("/usr/bin/soffice"), Path("/usr/lib/libreoffice/program/soffice")):
        if p.is_file():
            return str(p)
    return None


def _docx_to_pdf_soffice(docx: Path, out_dir: Path, target: Path) -> Optional[Path]:
    """LibreOffice 로 docx → pdf (창 없이). 이번 호출만의 임시 폴더에 만들고 거기서 나온 PDF 만 캐시로 옮긴다 — 변환이 실패하면 옛 PDF 를 쓰지 않는다
    (Codex 리뷰 10-25 밤: 바뀐 docx 의 변환이 실패해도 옛 PDF 를 현재 첨부로 보내던 것)."""
    exe = _soffice()
    if not exe:
        return None
    import subprocess
    with tempfile.TemporaryDirectory(prefix="soffice-", dir=str(out_dir)) as tmp:
        try:
            subprocess.run([exe, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(docx)], check=False, capture_output=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired):
            return None
        produced = Path(tmp) / f"{docx.stem}.pdf"
        if not produced.is_file():
            return None
        produced.replace(target)
    return target


def _docx_to_pdf(docx: Path, out_dir: Path) -> Optional[Path]:
    """docx → pdf (창 없이) — Windows 는 Word(COM), 없으면 LibreOffice. 한 번에 하나만."""
    target = out_dir / f"{docx.name}.pdf"
    if target.is_file() and target.stat().st_mtime >= docx.stat().st_mtime:
        return target
    return _docx_to_pdf_word(docx, target) or _docx_to_pdf_soffice(docx, out_dir, target)


def _docx_to_pdf_word(docx: Path, target: Path) -> Optional[Path]:
    """Word(COM) 로 docx → pdf — Windows + Word 가 있을 때만, 없으면 None."""
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return None
    pythoncom.CoInitialize()
    try:
        try:
            word = win32com.client.DispatchEx("Word.Application")
        except Exception:   # pywin32 는 있는데 Word 가 없음 — LibreOffice 로 넘긴다 (Codex 리뷰 10-25 밤)
            return None
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
                return [], f"{path.name}: docx → pdf 변환 불가(Word · LibreOffice 없음) — 글로만"
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
