# -*- coding: utf-8 -*-
"""첨부 쪽 이미지(code_eval.attachment_pages) — 이미지는 그대로, 글 파일은 없음, 같은 프로세스에서 한 번 그린 쪽은 다시 그리지 않음(임시 폴더, 세트 폴더에 남기지 않음), 끄기 스위치. Word·pdf 그리기는 부르지 않는다."""
from __future__ import annotations

from pathlib import Path

from code_eval import attachment_pages as ap
from tools import judge_v4 as jv


def test_image_passthrough_and_text_files_have_no_pages(tmp_path: Path):
    img = tmp_path / "사진.png"
    img.write_bytes(b"x")
    assert ap.page_images(img, tmp_path) == ([img], "사진.png: 이미지 원본 1장")
    txt = tmp_path / "메모.txt"
    txt.write_text("a", encoding="utf-8")
    assert ap.page_images(txt, tmp_path)[0] == []
    assert ap.page_images(tmp_path / "없음.pdf", tmp_path)[0] == []


def test_cached_pages_are_reused_without_rendering(tmp_path: Path, monkeypatch):
    pdf = tmp_path / "안내.pdf"
    pdf.write_bytes(b"%PDF")
    cache = ap.cache_dir(tmp_path)
    assert not str(cache).startswith(str(tmp_path))   # 세트 폴더가 아니라 임시 폴더
    for n in (2, 1):
        (cache / f"안내.pdf.p{n}.png").write_bytes(b"png")
    monkeypatch.setattr(ap, "_render_pdf", lambda *a, **k: (_ for _ in ()).throw(AssertionError("다시 그리면 안 됨")))
    pages, note = ap.page_images(pdf, tmp_path)
    assert [p.name for p in pages] == ["안내.pdf.p1.png", "안내.pdf.p2.png"] and "저장된 것" in note


def test_attachment_images_collects_pages_and_respects_switch(tmp_path: Path, monkeypatch):
    files = tmp_path / "files"
    files.mkdir()
    (files / "사진.png").write_bytes(b"x")
    (files / "안내.pdf").write_bytes(b"%PDF")
    cache = ap.cache_dir(files)
    (cache / "안내.pdf.p1.png").write_bytes(b"png")
    assert not (files / "_pages").exists()
    turns = [{"case_id": "Q1_T1", "turn": 1, "attachments": [{"name": "사진.png", "path": "사진.png"}, {"name": "안내.pdf", "path": "안내.pdf"}, {"name": "없음.docx", "path": "없음.docx"}]}]
    monkeypatch.setenv("CODEX_ATTACH_PAGES", "1")
    assert [p.name for p in jv.attachment_images(turns, files)] == ["사진.png", "안내.pdf.p1.png"]
    monkeypatch.setenv("CODEX_ATTACH_PAGES", "0")
    assert [p.name for p in jv.attachment_images(turns, files)] == ["사진.png"]   # 끄면 이미지 파일만
    assert jv.attachment_images(turns, None) == []


def test_docx_falls_back_to_libreoffice_when_word_is_missing(tmp_path: Path, monkeypatch):
    """Word(COM)가 없는 Mac · Linux 에서는 LibreOffice(soffice --headless)로 docx → pdf, 둘 다 없으면 None (10-25 밤, 할일 17번)."""
    from code_eval import attachment_pages as ap
    docx = tmp_path / "안내.docx"
    docx.write_bytes(b"PK fake")
    monkeypatch.setattr(ap, "_docx_to_pdf_word", lambda d, t: None)
    monkeypatch.setattr(ap, "_soffice", lambda: None)
    assert ap._docx_to_pdf(docx, tmp_path) is None
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        (Path(cmd[cmd.index("--outdir") + 1]) / "안내.pdf").write_bytes(b"%PDF-fake")   # soffice 는 <이름>.pdf 로 쓴다(이번 호출의 임시 폴더)
        return None
    monkeypatch.setattr(ap, "_soffice", lambda: "/usr/bin/soffice")
    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    out = ap._docx_to_pdf(docx, tmp_path)
    assert out == tmp_path / "안내.docx.pdf" and out.is_file() and calls and calls[0][:3] == ["/usr/bin/soffice", "--headless", "--convert-to"]


def test_failed_libreoffice_conversion_does_not_reuse_stale_pdf(tmp_path: Path, monkeypatch):
    """LibreOffice 변환이 실패하면 옛 PDF 를 현재 첨부로 쓰지 않는다 (Codex 리뷰 10-25 밤)."""
    import os
    import subprocess
    import time
    from code_eval import attachment_pages as ap
    docx = tmp_path / "안내.docx"
    docx.write_bytes(b"PK new")
    stale = tmp_path / "안내.docx.pdf"
    stale.write_bytes(b"%PDF-old")
    old = time.time() - 3600
    os.utime(stale, (old, old))                       # docx 보다 오래된 PDF — 다시 변환해야 함
    monkeypatch.setattr(ap, "_docx_to_pdf_word", lambda d, t: None)
    monkeypatch.setattr(ap, "_soffice", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: None)   # 아무것도 만들지 못함
    assert ap._docx_to_pdf(docx, tmp_path) is None

