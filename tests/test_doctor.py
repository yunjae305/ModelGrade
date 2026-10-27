"""실행 환경 점검(aiona-chat-eval doctor) · 설치 스크립트 — 다른 PC 에서 처음 돌릴 때 (10-25 밤)."""
from __future__ import annotations

from pathlib import Path

from tools import doctor

ROOT = Path(__file__).resolve().parents[1]


def test_doctor_fails_only_on_required_items(monkeypatch, capsys):
    ok = lambda name, req: (lambda: doctor.Check(name, True, "됨", req))
    bad = lambda name, req: (lambda: doctor.Check(name, False, "안 됨", req, "고치는 법"))
    monkeypatch.setattr(doctor, "CHECKS", ((ok("파이썬", True), "파이썬", True), (bad("글꼴", False), "글꼴", False)))
    assert doctor.main([]) == 0 and "고치는 법" in capsys.readouterr().out          # 선택 항목만 빠지면 통과
    monkeypatch.setattr(doctor, "CHECKS", ((bad("Codex", True), "Codex", True),))
    assert doctor.main([]) == 1 and "필수 항목 1개" in capsys.readouterr().out
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(doctor, "CHECKS", ((boom, "깨진 점검", True),))
    assert doctor.run_checks()[0].ok is False                                        # 점검 하나가 깨져도 나머지는 계속


def test_install_and_run_scripts_exist_for_both_shells():
    ps1, sh = (ROOT / "install.ps1").read_bytes(), (ROOT / "install.sh").read_bytes()
    assert ps1.startswith(b"\xef\xbb\xbf")             # PowerShell 5.1 이 한글을 UTF-8 로 읽게 BOM
    assert b"\r\n" not in sh and b"\r\n" not in (ROOT / "modelgrade.sh").read_bytes()   # 셸 스크립트는 LF
    for f in ("install.ps1", "install.sh", "modelgrade.cmd", "modelgrade.sh"):
        assert "tools" in (ROOT / f).read_text(encoding="utf-8-sig", errors="ignore")
    (ROOT / "modelgrade.cmd").read_bytes().decode("ascii")   # cmd.exe 는 시스템 코드 페이지로 읽음 — ASCII 만
