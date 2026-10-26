"""새 질문 리스트(tools/new_question_set.py) — 검사기 · 프롬프트 [요청] 바꾸기 · 회차 이어 고치기 · 완성 처리. Claude 는 부르지 않는다."""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SET78 = ROOT / "참고자료" / "질문세트_78"


def _small_set(dest: Path) -> Path:
    """78 세트에서 작은 세트(일반 2 · docx 1 · pdf 1 · 빈 출력 1)를 떼어 온다 — 검사를 통과하는 진짜 질문들."""
    groups = {g["id"]: g for g in json.loads((SET78 / "questions.json").read_text(encoding="utf-8"))}
    picked = [dict(groups[i]) for i in (11, 12, 31, 17, 59)]
    (dest / "files").mkdir(parents=True, exist_ok=True)
    for n, g in enumerate(picked, 1):
        g["id"] = n
        for a in g["turns"][0].get("attachments") or []:
            shutil.copy(SET78 / "files" / a["path"], dest / "files" / a["path"])
    (dest / "questions.json").write_text(json.dumps(picked, ensure_ascii=False), encoding="utf-8")
    return dest


def test_check_set_passes_real_questions_and_names_each_problem(tmp_path):
    from tools import new_question_set as nq
    d = _small_set(tmp_path / "s")
    res = nq.check_set(d, "small")
    assert res["ok"], res["problems"]
    assert res["counts"]["attachment_types"] == {"docx": 1, "pdf": 1} and res["counts"]["empty"] == 1
    assert nq.render(res).startswith("검사: 통과")

    groups = json.loads((d / "questions.json").read_text(encoding="utf-8"))
    groups[0]["turns"][0]["question"] = "회의 시간을 알려줘."                     # k 가 범위 밖
    groups[3]["turns"][0]["attachments"][0]["content"] = "[1쪽]\n전혀 다른 글"    # 파일 글과 content 가 다름
    (d / "questions.json").write_text(json.dumps(groups, ensure_ascii=False), encoding="utf-8")
    (d / "files" / groups[2]["turns"][0]["attachments"][0]["path"]).unlink()       # 파일 없음
    res = nq.check_set(d, "small")
    text = "\n".join(res["problems"])
    assert not res["ok"] and "k 범위(6~10) 밖 단일 질문 1개: Q1" in text and "files/ 에 파일이 없음" in text and "content 에 없음" in text

    (d / "questions.json").write_text("[{", encoding="utf-8")
    assert "JSON 으로 읽지 못함" in nq.check_set(d, "small")["problems"][0]


def test_request_block_keeps_the_prompt_and_changes_only_the_request():
    from tools import new_question_set as nq
    prompt = nq.PROMPT_PATH.read_text(encoding="utf-8")
    full = nq.request_block(prompt, "qs-test", "full")
    assert "세트 이름: `qs-test`" in full and len(full) == len(prompt) - len("qs-2026-10-25-v36") + len("qs-test")
    small = nq.request_block(prompt, "qs-test-small", "small")
    assert "**5개**" in small and "`qs-test-small`" in small and "# 0. 확정한 운영 규모" in small and "**78개**" not in small.split("# 0.")[0]


def test_make_fixes_over_rounds_then_finalizes_and_lists_the_set(tmp_path, monkeypatch):
    """1회차는 아무것도 못 만들고(검사 실패) 2회차에 다 만든다 — 2회차 지시에 지난 검사 결과가 들어가고, 통과하면 set.json · 기준 목록을 맞춘다."""
    from tools import new_question_set as nq
    import code_eval.claude_cli_client as cc
    monkeypatch.setattr(cc, "find_claude_executable", lambda: "claude")
    monkeypatch.setattr(nq, "claude_version", lambda exe: "2.1.310")
    import tools.setup_wizard as sw
    monkeypatch.setattr(sw, "claude_logged_in", lambda exe: True)
    monkeypatch.setattr(nq, "SETS_DIR", tmp_path)
    src = _small_set(tmp_path / "src")
    tasks = []

    def fake_round(exe, set_dir, task, meta, round_no, on_note):
        tasks.append(task)
        on_note("쓰는 중 questions.json")
        if round_no == 2:
            shutil.copytree(src / "files", set_dir / "files", dirs_exist_ok=True)
            shutil.copy(src / "questions.json", set_dir / "questions.json")
        return {"round": round_no, "exit": 0, "result": {"num_turns": 3, "text": "검사: 통과"}}
    monkeypatch.setattr(nq, "run_round", fake_round)
    d = nq.make("small", resume=True, set_dir=tmp_path / "질문세트_시험", say=lambda s: None)
    assert len(tasks) == 2 and "[지난 검사 결과]" in tasks[1] and "questions.json 이 아직 없음" in tasks[1] and "질문생성_프롬프트.md" in tasks[0]
    s = json.loads((d / "set.json").read_text(encoding="utf-8"))
    assert s["status"] == "COMPLETED" and s["generator"].startswith(nq.GENERATOR) and s["counts"]["total"] == 5 and s["rounds"] == 2
    cat = json.loads((d / "criteria_catalog.json").read_text(encoding="utf-8"))
    assert len(cat["variable_ids"]) == 54 and "J5-3.a" in cat["answer_dependent_ids"] and "J3-1.a" in cat["always_ids"]
    assert not (d / nq.MARKER).exists() and (d / nq.LOG_DIR / "생성.json").exists() and (d / "질문생성_프롬프트.md").exists()
    assert [m["dir"] for m in nq.made_sets()] == [d] and nq.unfinished_set() is None


def test_live_board_redraws_in_place_and_fits_the_terminal(monkeypatch):
    """진행 판 — 터미널이면 커서를 올려 같은 자리에 다시 그리고 폭에 맞게 자르며, 아니면 바뀐 줄만 찍는다."""
    import io
    import os
    import shutil
    from tools.live_board import LiveBoard
    plain = io.StringIO()
    b = LiveBoard({"aiona": "AIONA", "gpt": "ChatGPT"}, {"aiona": 78, "gpt": 78}, stream=plain)
    b.start()
    b.update("aiona", add=1, note="Q2 받는 중")
    b.update("aiona", note="Q2 받는 중")   # 같은 글 — 다시 찍지 않음
    assert plain.getvalue().splitlines() == ["  AIONA   :  0 / 78  (남은 78) · 대기", "  ChatGPT :  0 / 78  (남은 78) · 대기",
                                             "  AIONA   :  1 / 78  (남은 77) · Q2 받는 중"]
    tty = io.StringIO()
    b = LiveBoard({"aiona": "AIONA"}, {"aiona": 5}, stream=tty)
    b.ansi = True
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((40, 20)))
    b.start()
    b.update("aiona", done=2, note="아주 긴 메모 " * 10)
    b.log("⚠ 경고")
    out = tty.getvalue()
    assert "\x1b[1F" in out and "⚠ 경고\n" in out and "…" in out
    assert all(b.line("aiona") != x.split("\n")[0] for x in out.split("\x1b[2K")[1:])   # 긴 줄은 그대로 안 나감
