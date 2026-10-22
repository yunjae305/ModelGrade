from webapp.copy_cleanup import join_split_table_rows


def test_joins_rows_split_by_gemini_copy():
    text = "| 항목 | 내용 |\n| --- | --- |\n| 일시 | 10월 12일\n\n |\n| 장소 | 서문\n\n | 비고\n\n |\n\n끝"
    new, n = join_split_table_rows(text)
    assert n == 2
    assert new == "| 항목 | 내용 |\n| --- | --- |\n| 일시 | 10월 12일 |\n| 장소 | 서문 | 비고 |\n\n끝"


def test_joins_split_header_row():
    text = "| 항목 | 이전\n\n | 최종\n\n |\n| --- | --- | --- |\n| 마감 | 25일 | 27일 |"
    new, n = join_split_table_rows(text)
    assert n == 1
    assert new.splitlines()[0] == "| 항목 | 이전 | 최종 |"


def test_leaves_normal_tables_and_text_alone():
    text = "| a | b |\n| --- | --- |\n| 1 | 2\n| 3 | 4 |\n\n | 표 밖 글"
    assert join_split_table_rows(text) == (text, 0)
