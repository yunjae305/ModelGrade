"""Lossless Q/A import. Delimiters are parsed locally; no LLM rewrites text."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from code_eval.io import parse_input_text, validate_json_numbers


ALIASES = {
    'question': ('question', '질문', 'Q', 'q', 'prompt', 'user'),
    'answer': ('answer', '답변', 'A', 'a', 'response', 'assistant'),
}
SINGLE = re.compile(r'(?:단일[ \t]*턴|단건|single(?:[ _-]?turn)?)(?:[ \t]*\d+[ \t]*(?:개|문항))?', re.I)
ROUND = re.compile(r'(?:멀티[ \t]*턴[ \t]*)?(?:(\d+)[ \t]*회차|(?:회차|대화|round|conversation|session)[ _-]*(\d+))', re.I)
LABEL = re.compile(
    r'^[ \t]{0,3}(?:\d+[.)][ \t]+|[-*][ \t]+)?(?:#{1,6}[ \t]+)?'
    r'(?:\*\*|__)?(?P<role>question|answer|assistant|user|human|aiona|질문|답변|사용자|응답|Q|A)'
    r'(?:[ \t]*(?P<number>\d+))?(?:\*\*|__)?'
    r'(?:[ \t]*[:：](?:\*\*|__)?[ \t]?|[ \t]*(?=\r?\n?$))', re.I)
QUESTION_ROLES = {'question', 'user', 'human', '질문', '사용자', 'q'}


def group_id(label):
    """Recognize only explicit conversation headings, never arbitrary JSON keys."""
    label = label.strip()
    if SINGLE.fullmatch(label):
        return ''
    match = ROUND.fullmatch(label)
    if match:
        return f'AIONA_MT_{int(match[1] or match[2]):02d}'
    return None


def canonical_row(row):
    if not isinstance(row, dict):
        return row, False
    result, converted = copy.deepcopy(row), False
    for target, names in ALIASES.items():
        found = [name for name in names if name in row]
        if len(found) > 1 and any(row[name] != row[found[0]] for name in found[1:]):
            raise ValueError(f'{target}에 해당하는 필드가 여러 개이고 값이 다릅니다. 사용할 질문·답변 필드를 확인해 주세요.')
        if target not in row and found:
            result[target] = row[found[0]]
            converted = True
    return result, converted


def id_turn_rows(groups):
    """Flatten explicit id/turns groups without rewriting any question or answer."""
    rows, ids, case_ids = [], set(), set()
    for index, group in enumerate(groups, 1):
        if not isinstance(group, dict) or 'id' not in group or 'turns' not in group:
            raise ValueError(f'{index}번 묶음: id와 turns가 있는 객체여야 합니다. 평탄 Q/A와 섞을 수 없습니다.')
        raw_id = group['id']
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)) or not str(raw_id).strip():
            raise ValueError(f'{index}번 묶음: id는 비어 있지 않은 문자열 또는 정수여야 합니다.')
        gid = str(raw_id).strip()
        if gid in ids:
            raise ValueError(f'id {gid}: 묶음 id가 중복되었습니다.')
        ids.add(gid)
        turns = group['turns']
        if not isinstance(turns, list) or not turns:
            raise ValueError(f'id {gid}: turns는 비어 있지 않은 배열이어야 합니다.')
        if any(name in group for names in ALIASES.values() for name in names):
            raise ValueError(f'id {gid}: 질문·답변은 상위 묶음과 turns 양쪽에 둘 수 없습니다.')
        for position, item in enumerate(turns, 1):
            if not isinstance(item, dict):
                raise ValueError(f'id {gid} {position}번 턴: 질문·답변 객체여야 합니다.')
            turn = item.get('turn')
            if isinstance(turn, bool) or not isinstance(turn, int) or turn != position:
                raise ValueError(f'id {gid}: turn은 1부터 순서대로 이어지는 정수여야 하며 중복될 수 없습니다.')
            case_id, cid = f'Q{gid}_T{turn}', f'AIONA_ID_{gid}'
            if 'id' in item and (isinstance(item['id'], bool) or not isinstance(item['id'], (str, int)) or str(item['id']).strip() != gid):
                raise ValueError(f'id {gid} {turn}턴: 상위 id와 턴 안의 id가 일치하지 않습니다.')
            if 'case_id' in item and item['case_id'] != case_id:
                raise ValueError(f'id {gid} {turn}턴: case_id가 변환할 문항 ID {case_id}와 충돌합니다.')
            if case_id in case_ids:
                raise ValueError(f'문항 ID {case_id}가 중복되었습니다.')
            case_ids.add(case_id)
            if 'conversation_id' in item and (len(turns) == 1 or item['conversation_id'] != cid):
                raise ValueError(f'id {gid} {turn}턴: conversation_id가 묶음의 대화 구분과 충돌합니다.')
            if 'import_source' in item:
                raise ValueError(f'id {gid} {turn}턴: import_source는 변환 출처를 위한 예약 필드입니다.')
            row, _ = canonical_row(item)
            row['case_id'] = case_id
            row['import_source'] = {'group': copy.deepcopy({k: v for k, v in group.items() if k != 'turns'}), 'turn': turn}
            if len(turns) > 1:
                row['conversation_id'] = cid
            else:
                row.pop('turn')
            rows.append(row)
    return rows


def json_rows(value):
    if isinstance(value, dict) and isinstance(value.get('cases'), list):
        value = value['cases']
    if isinstance(value, dict) and value and all(group_id(k) is not None for k in value):
        rows = []
        for label, group in value.items():
            if not isinstance(group, list):
                raise ValueError(f'{label}: 회차의 질문·답변은 배열이어야 합니다.')
            cid = group_id(label)
            for index, item in enumerate(group):
                row, _ = canonical_row(item)
                if not isinstance(row, dict):
                    raise ValueError(f'{label} {index + 1}번 항목은 질문·답변 객체여야 합니다.')
                if cid:
                    if row.get('conversation_id', cid) != cid:
                        raise ValueError(f'{label}: 회차 이름과 conversation_id가 일치하지 않습니다.')
                    row['conversation_id'] = cid
                    row.setdefault('turn', index + 1)
                elif 'conversation_id' in row or 'turn' in row:
                    raise ValueError(f'{label}: 단일턴 묶음에 대화 ID 또는 턴이 있습니다.')
                row['source_group'] = label
                rows.append(row)
        return rows, True, '회차별 JSON'
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise ValueError('Q/A JSON은 객체 또는 배열이어야 합니다.')
    if any(isinstance(item, dict) and 'turns' in item and
           not any(name in item for names in ALIASES.values() for name in names) for item in value):
        return id_turn_rows(value), True, 'id/turns JSON'
    rows, converted = [], False
    for item in value:
        row, changed = canonical_row(item)
        rows.append(row)
        converted |= changed
    return rows, converted, 'JSON'


def text_rows(text):
    """Slice exact source spans. Markdown inside bodies is never rendered/rewritten."""
    rows, counts = [], {}
    current, phase, begin = None, None, 0
    cid, group = '', None
    fence = None
    outside = False
    offset = 0

    def finish(end):
        nonlocal current, phase
        if current is None:
            return
        if phase != 'answer':
            raise ValueError(f'{len(rows) + 1}번 질문의 답변 표시를 찾지 못했습니다. 답변: 또는 A:를 붙여 주세요.')
        current['answer'] = text[begin:end]
        current['import_span']['answer'] = [begin, end]
        if cid:
            counts[cid] = counts.get(cid, 0) + 1
            current.update(conversation_id=cid, turn=counts[cid], source_group=group)
        rows.append(current)
        current, phase = None, None

    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        start, offset = offset, offset + len(line)
        fence_match = re.match(r'^[ \t]{0,3}(`{3,}|~{3,})(.*?)(?:\r?\n)?$', line)
        if fence is not None:
            if fence_match and fence_match[1][0] == fence[0] and len(fence_match[1]) >= len(fence) and not fence_match[2].strip():
                fence = None
            continue
        if fence_match:
            fence = fence_match[1]
            if current is None:
                raise ValueError(f'{line_number}행: Q/A 밖의 코드 블록은 질문·답변으로 구분할 수 없습니다.')
            continue
        heading = re.sub(r'^[ \t]{0,3}#{1,6}[ \t]+', '', line.strip()).strip('*_[] \t')
        section = group_id(heading)
        if section is not None:
            finish(start)
            cid, group = section, heading
            continue
        marker = LABEL.match(line)
        if marker:
            role = 'question' if marker['role'].lower() in QUESTION_ROLES else 'answer'
            # A heading-only marker occupies the whole line; inline bodies start
            # after one separator space. All subsequent whitespace is retained.
            body_start = start + marker.end()
            if line[marker.end():] in ('', '\n', '\r\n'):
                body_start = offset
            if role == 'question':
                finish(start)
                current = {'import_span': {}}
                phase, begin = role, body_start
            else:
                if current is None or phase != 'question':
                    raise ValueError(f'{line_number}행: 답변 앞에 질문 표시가 없거나 답변 표시가 중복됐습니다.')
                current['question'] = text[begin:start]
                current['import_span']['question'] = [begin, start]
                phase, begin = role, body_start
        elif current is None and line.strip():
            # Preserve document titles/prefaces in original_text and explicitly
            # disclose their exclusion from Q/A; never send them as instructions.
            outside = True
    if fence is not None:
        raise ValueError('닫히지 않은 코드 블록이 있습니다. 코드 블록의 끝 표시를 확인해 주세요.')
    finish(len(text))
    if not rows:
        raise ValueError('질문과 답변의 경계를 찾지 못했습니다. 각 항목에 질문:/답변: 또는 Q:/A: 표시를 붙여 주세요. 멀티턴은 # 1회차처럼 회차 제목도 적어 주세요.')
    return rows, outside


def import_text(text, name='업로드 파일'):
    if not isinstance(text, str):
        raise ValueError('input_text 는 문자열이어야 합니다')
    original = text
    text = text.lstrip('\ufeff')
    if not text.strip():
        raise ValueError(f'{name}: 입력이 비어 있음')
    fenced_json = re.fullmatch(r'[ \t]*```(?:json|jsonl)[ \t]*\r?\n([\s\S]*?)\r?\n```[ \t]*(?:\r?\n)?', text, re.I)
    if fenced_json:
        rows, info = import_text(fenced_json[1], '본문.json')
        info.update(format='Markdown JSON', converted=True, source_name=name, original_text=original)
        return rows, info
    notes = []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
        is_json = Path(name).suffix.lower() in ('.json', '.jsonl') or text.lstrip().startswith(('{', '['))
        if is_json:
            rows, errors = parse_input_text(text, name)
            if errors:
                raise ValueError(rows[next(i for i, r in enumerate(rows) if isinstance(r, dict) and r.get('__parse_error__'))]['__parse_error__'])
            rows, converted, label = json_rows(rows)
            label = 'JSONL'
        else:
            rows, outside = text_rows(text)
            bom_chars = len(original) - len(text)
            for row in rows:
                row['import_span'] = {key: [a + bom_chars, b + bom_chars] for key, (a, b) in row['import_span'].items()}
            converted, label = True, 'Markdown' if Path(name).suffix.lower() in ('.md', '.markdown') else '텍스트'
            if outside:
                notes.append('첫 질문 앞의 제목·설명은 Q/A에서 제외하고 업로드 원문에 보존했습니다.')
            notes.append('Q/A 표시만 분리하고 본문의 공백·줄바꿈·코드 블록을 그대로 보존했습니다.')
            if not any('conversation_id' in r for r in rows):
                notes.append('회차 표시가 없어 각 질문을 단일턴으로 분류했습니다. 이어지는 대화라면 # 1회차 제목을 추가해 주세요.')
    else:
        rows, converted, label = json_rows(value)
    validate_json_numbers(rows)
    info = {'format': label, 'converted': converted, 'source_name': name, 'notes': notes}
    if converted:
        info['original_text'] = original
    return rows, info
