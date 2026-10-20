# -*- coding: utf-8 -*-
"""첨부 쪽 이미지(code_eval.attachment_pages) — 이미지는 그대로, 글 파일은 없음, 저장된 쪽 이미지는 다시 그리지 않음, 끄기 스위치. Word·pdf 그리기는 부르지 않는다."""
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
    cache.mkdir()
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
    cache.mkdir()
    (cache / "안내.pdf.p1.png").write_bytes(b"png")
    turns = [{"case_id": "Q1_T1", "turn": 1, "attachments": [{"name": "사진.png", "path": "사진.png"}, {"name": "안내.pdf", "path": "안내.pdf"}, {"name": "없음.docx", "path": "없음.docx"}]}]
    monkeypatch.setenv("CODEX_ATTACH_PAGES", "1")
    assert [p.name for p in jv.attachment_images(turns, files)] == ["사진.png", "안내.pdf.p1.png"]
    monkeypatch.setenv("CODEX_ATTACH_PAGES", "0")
    assert [p.name for p in jv.attachment_images(turns, files)] == ["사진.png"]   # 끄면 이미지 파일만
    assert jv.attachment_images(turns, None) == []
