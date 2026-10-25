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
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
PAGE_EXT = (".pdf", ".docx")
MAX_PAGES = 20
RENDER_SCALE = 1.5
_LOCK = threading.Lock()
CONVERT_WAIT_S = 300    # 다른 실행이 변환 중이면 이만큼까지 기다린다 — 넘으면 그 첨부는 글로만
CONVERT_STALE_S = 900   # 이보다 오래된 잠금은 죽은 실행의 것으로 보고 지운다


@contextmanager
def _convert_lock(out_dir: Path, wait_s: float = CONVERT_WAIT_S, stale_s: float = CONVERT_STALE_S):
    """실행(프로세스)끼리 docx → pdf 변환을 한 번에 하나만 — 폴더 만들기(mkdir)는 원자적이라 잠금으로 쓴다. Word 자동화를 두 프로세스가 동시에 돌리면
    서로 막혀 끝나지 않는다(10-26 새벽: 판정 둘을 동시에 띄우자 한쪽이 4시간 멈춤). 기다림이 wait_s 를 넘으면 TimeoutError."""
    lock = out_dir / ".convert.lock"
    started = time.monotonic()
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale_s:
                    shutil.rmtree(lock, ignore_errors=True)
                    continue
            except OSError:
                pass
            if time.monotonic() - started > wait_s:
                raise TimeoutError("다른 실행의 첨부 변환이 끝나지 않음")
            time.sleep(0.5)
    try:
        yield
    finally:
        shutil.rmtree(lock, ignore_errors=True)
_NOTES: Dict[str, str] = {}


def enabled() -> bool:
    """CODEX_ATTACH_PAGES=0 이면 쪽 이미지를 붙이지 않는다 (A/B 처럼 조건을 고정할 때)."""
    return os.environ.get("CODEX_ATTACH_PAGES", "1").strip() not in ("0", "false", "no")


_TMP: Dict[str, Path] = {}
#: 프로세스 사이에 남는 쪽 이미지 캐시 — 한 번 그린 첨부는 다시 변환하지 않는다(Word 자동화를 실행마다 다시 띄우지 않게, 10-26 새벽).
#: 원본보다 오래된 이미지는 _fresh 가 버린다. .env · 환경 변수 MG_PAGES_CACHE 로 옮길 수 있고, 만들 수 없으면 예전처럼 프로세스 임시 폴더.
PAGES_CACHE = Path(os.environ.get("MG_PAGES_CACHE") or (Path(__file__).resolve().parents[1] / "data" / "cache" / "attachment_pages"))
WORD_TIMEOUT_S = float(os.environ.get("MG_WORD_TIMEOUT_S") or 120)   # Word 변환 한 번의 시간 제한 — 넘으면 그 Word 만 끝내고 글로만


def cache_dir(files_dir: Path) -> Path:
    """세트 폴더마다의 쪽 이미지 캐시 — PAGES_CACHE/<세트 폴더 경로의 해시>. 만들 수 없으면 이 프로세스의 임시 폴더(끝나면 지움)."""
    import hashlib
    key = str(Path(files_dir).resolve())
    if key not in _TMP:
        target = PAGES_CACHE / hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        try:
            target.mkdir(parents=True, exist_ok=True)
            _TMP[key] = target
        except OSError:
            _TMP[key] = Path(tempfile.mkdtemp(prefix="mg-pages-"))
    return _TMP[key]


def _cleanup() -> None:
    for d in [d for d in _TMP.values() if PAGES_CACHE not in d.parents]:   # 남기는 캐시는 지우지 않는다
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


_WORD_SCRIPT = r"""
import sys
import pythoncom
import win32com.client
src, dst = sys.argv[1], sys.argv[2]
pythoncom.CoInitialize()
word = win32com.client.DispatchEx("Word.Application")
word.Visible = False
word.DisplayAlerts = 0
try:
    doc = word.Documents.Open(src, ReadOnly=True, AddToRecentFiles=False, ConfirmConversions=False)
    try:
        doc.SaveAs2(dst, FileFormat=17)   # 17 = wdFormatPDF
    finally:
        doc.Close(False)
finally:
    word.Quit()
"""


def _word_pids() -> set:
    """지금 떠 있는 WINWORD.EXE 의 PID — 변환이 띄운 Word 만 골라 끝내려고."""
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WINWORD.EXE", "/FO", "CSV", "/NH"], capture_output=True, timeout=20).stdout.decode("ascii", "ignore")   # 콘솔 코드 페이지(cp949)와 무관하게
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {int(row.split('","')[1]) for row in out.splitlines() if row.startswith('"WINWORD') and row.count('","') >= 1 and row.split('","')[1].isdigit()}


def _docx_to_pdf_word(docx: Path, target: Path) -> Optional[Path]:
    """Word(COM) 로 docx → pdf — Windows + pywin32 + Word 가 있을 때만. 하위 프로세스에서 WORD_TIMEOUT_S 안에 끝나지 않으면 그 프로세스와 그때 새로 뜬
    Word 만 끝내고 None(글로만). 보이지 않는 Word 가 대화 상자(안전 모드 · 복구)에서 멈춰 판정이 끝나지 않던 것 (10-26 새벽 4시간 멈춤)."""
    import subprocess
    import sys
    if os.name != "nt":
        return None
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        return None
    import uuid
    before = _word_pids()
    # 원본을 임시 폴더에 새 이름으로 복사해 연다 — Word 는 열다 멈춘(강제로 끝낸) 문서 경로를 기억해 다음에 보이지 않는 확인 창을 띄운다
    # (10-26 새벽: 같은 docx 에서만 매번 멈춤). 결과 PDF 도 임시 폴더에 만들고 끝난 것만 캐시로 옮긴다.
    with tempfile.TemporaryDirectory(prefix="mg-word-") as tmp:
        src = Path(tmp) / f"{uuid.uuid4().hex}.docx"
        out = Path(tmp) / f"{src.stem}.pdf"
        try:
            shutil.copy2(docx, src)
            proc = subprocess.run([sys.executable, "-c", _WORD_SCRIPT, str(src), str(out)],
                                  capture_output=True, timeout=WORD_TIMEOUT_S)   # 출력은 바이트로(콘솔 코드 페이지 문제 없게)
        except subprocess.TimeoutExpired:
            for pid in _word_pids() - before:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            return None
        except OSError:
            return None
        if proc.returncode != 0 or not out.is_file():
            return None
        shutil.move(str(out), str(target))
    return target


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
            with _convert_lock(PAGES_CACHE if PAGES_CACHE in out_dir.parents else out_dir):   # 실행 공통 잠금 — 캐시 뿌리 하나
                cached = _existing_pages(path, out_dir)   # 기다리는 동안 다른 실행이 만들었으면 그것을 쓴다
                if cached:
                    return cached, f"{path.name}: 쪽 이미지 {len(cached)}장(저장된 것)"
                pdf = path if ext == ".pdf" else _docx_to_pdf(path, out_dir)
                pages = _render_pdf(pdf, path, out_dir) if pdf is not None else []
            if pdf is None:
                return [], f"{path.name}: docx → pdf 변환 불가(Word · LibreOffice 없음) — 글로만"
            note = f"{path.name}: {'PDF' if ext == '.pdf' else 'DOCX(Word 로 pdf)'} {len(pages)}쪽을 이미지로"
        except TimeoutError:
            return [], f"{path.name}: 다른 실행의 첨부 변환을 기다리다 시간 초과 — 글로만"
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
