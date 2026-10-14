"""첨부 글자 추출 — DOCX(표준 라이브러리)·PDF(pypdf 있으면)·TXT·이미지, 그리고 뽑은 글자가 기존 J8 적용·답변 수집 경로로 그대로 쓰이는지."""
import zipfile

import pytest

from tools.attachment_text import extract

DOCX_XML = ('<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>견적서</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>합계</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>1,250,000원</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '</w:body></w:document>')


def _docx(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", DOCX_XML)
    return path


def _pdf(path, text="TOTAL 1250000"):
    """글자 층이 있는 가장 작은 PDF (Helvetica) — 바이트 위치표를 맞게 만든다."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def test_docx_txt_and_image_entries(tmp_path):
    docx = extract(_docx(tmp_path / "견적서.docx"), tmp_path)
    assert docx["content"] == "견적서\n합계 | 1,250,000원" and docx["extractor"] == "docx-xml-stdlib" and docx["path"] == "견적서.docx"
    (tmp_path / "memo.txt").write_text("메모 내용", encoding="utf-8")
    assert extract(tmp_path / "memo.txt")["content"] == "메모 내용"
    (tmp_path / "chart.png").write_bytes(b"PNG")
    image = extract(tmp_path / "chart.png")
    assert image["type"] == "image" and image["content"] is None and "실제 파일" in image["extractor"]


def test_pdf_text_layer_when_pypdf_is_installed(tmp_path):
    pytest.importorskip("pypdf")
    entry = extract(_pdf(tmp_path / "quote.pdf"))
    assert "TOTAL 1250000" in entry["content"] and entry["content"].startswith("[1쪽]") and entry["pages"] == 1
    assert entry["extractor"].startswith("pypdf ")


def test_extracted_text_flows_through_existing_j8_applicability_and_collection_delivery(tmp_path):
    """새 엔진 없이 — 뽑은 글자를 attachments[].content 에 넣으면 기존 J8 적용 판정과 답변 수집 전달이 그대로 쓴다."""
    from tools.check_question_set import check_items
    from webapp.attachment_delivery import prepare
    entry = extract(_docx(tmp_path / "견적서.docx"), tmp_path)
    report = check_items([{"question": "첨부한 견적서의 합계를 알려줘.", "attachments": [entry]}], pipeline="items")
    assert report["criteria"]["J8"]["applicable_units"] == 1
    question, images, delivery = prepare("첨부한 견적서의 합계를 알려줘.", [entry], [tmp_path])
    assert "합계 | 1,250,000원" in question and images == [] and delivery[0]["mode"] == "text_extracted"


def test_generated_attachment_with_only_a_file_name_is_found_by_sha256(tmp_path):
    """질문 세트 생성 파일(path = 파일 이름만)은 허용 폴더 안에서 이름으로 찾고 sha256 이 맞을 때만 보낸다."""
    import hashlib
    from webapp.attachment_delivery import prepare
    (tmp_path / "set1" / "files").mkdir(parents=True)
    (tmp_path / "set2" / "files").mkdir(parents=True)
    png = b"\x89PNG\r\n\x1a\n"   # 보내기 전에 파일 서명으로 형식을 확인한다 (4차)
    (tmp_path / "set1" / "files" / "chart.png").write_bytes(png + b"other-image")
    (tmp_path / "set2" / "files" / "chart.png").write_bytes(png + b"right-image")
    digest = hashlib.sha256(png + b"right-image").hexdigest()
    entry = {"name": "chart.png", "type": "image", "content": "막대 3개", "path": "chart.png", "sha256": digest}
    _, images, delivery = prepare("그래프를 보고 답해줘.", [entry], [tmp_path])
    assert delivery[0]["mode"] == "image" and len(images) == 1
    wrong = dict(entry, sha256=hashlib.sha256(b"nothing").hexdigest())
    _, images, delivery = prepare("그래프를 보고 답해줘.", [wrong], [tmp_path])
    assert images == [] and delivery[0]["mode"] == "not_sent"

