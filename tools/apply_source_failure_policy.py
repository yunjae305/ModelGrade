"""Apply a final decision policy to one saved completed comparison.

Default operation is a read-only dry run. ``--apply`` retains exact-byte backups,
updates linked evaluation files and records the policy update in the run (evaluation_policy_updates).
This never calls a judge, collects answers, or refreshes a running server cache.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_eval.combined import combined_summary
from code_eval.jev_eval import _overall, build_history, conversation_contexts, conversations_summary
from code_eval.semantic_policy import (
    DECISION_POLICY,
    SOURCE_ORIGINAL_FAILURE_POLICY_VERSION,
    apply_source_original_failure_policy,
)
from webapp.registry_store import RegistryStore
from webapp.version_registry import evaluation_profile


def _hash(body):
    return hashlib.sha256(body).hexdigest()


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8')


def _counts(batch):
    combined = batch.get('combined') or combined_summary(batch)
    return {key: combined[key] for key in ('questions', 'pass', 'fail', 'pending')}


def _refresh_summary(batch, *, decision_policy=None, refresh_conversations=False):
    """Recount semantic aggregates without altering CODE or J6 results."""
    semantic = batch['jev_eval']
    records = semantic['records']
    for part in semantic['summary'].values():
        for field in ('targets', 'pass', 'fail', 'pending', 'unverified'):
            part[field] = 0
    for record in records:
        for cid in record['criteria']:
            semantic['summary'][cid]['targets'] += 1
        for cid, decision in (record.get('decisions') or {}).items():
            if decision.get('decision') in ('PASS', 'FAIL'):
                semantic['summary'][cid][decision['decision'].lower()] += 1
            elif decision.get('status') == 'UNVERIFIED':
                semantic['summary'][cid]['unverified'] += 1
    for part in semantic['summary'].values():
        part['pending'] = part['targets'] - part['pass'] - part['fail']
    semantic['completed'] = sum(record['status'] == 'SUCCESS' and not record.get('review_required') for record in records)
    semantic['review_required'] = sum(bool(record.get('review_required')) for record in records)
    semantic['unverified'] = sum(part['unverified'] for part in semantic['summary'].values())
    semantic['status'] = _overall(records)
    semantic['decision_policy'] = copy.deepcopy(DECISION_POLICY if decision_policy is None else decision_policy)
    if refresh_conversations:
        semantic['conversations'] = conversations_summary(batch['cases'], records, conversation_contexts(batch['cases']))
    batch['combined'] = combined_summary(batch)


def _evaluation_state(batch, index, contexts=None):
    """Rebuild the exact original request, including every prior original turn."""
    case = batch['cases'][index]
    contexts = conversation_contexts(batch['cases']) if contexts is None else contexts
    state = {'question': case['question'], 'answer': case['answer']}
    if case.get('reference') is not None:
        state['reference'] = copy.deepcopy(case['reference'])
    context = contexts.get(index)
    if context:
        history, _ = build_history(context['prior'])
        if history:
            state['history'] = history
    return state


def _refresh_progress(run, timestamp):
    for provider, detail in run.get('evaluation_progress', {}).items():
        batch = run.get('evaluations', {}).get(provider) or {}
        records = [record for record in batch.get('jev_eval', {}).get('records', [])
                   if record and record.get('status') not in ('RUNNING', 'WAITING', 'PENDING', 'INTERRUPTED')]
        counts = dict(semantic_done=len(records), semantic_success=0, semantic_unverified=0,
                      semantic_errors=0, skipped=0)
        for record in records:
            key = ('semantic_unverified' if record.get('review_required') else 'semantic_success'
                   if record['status'] == 'SUCCESS' else 'skipped' if record['status'] == 'SKIPPED'
                   else 'semantic_errors')
            counts[key] += 1
        if any(detail.get(key) != value for key, value in counts.items()):
            detail.update(counts, policy_updated_at=timestamp)


def _policy_settings(policy):
    if policy == 'source_original':
        return (SOURCE_ORIGINAL_FAILURE_POLICY_VERSION,
                {'j5_source_original_failure': SOURCE_ORIGINAL_FAILURE_POLICY_VERSION},
                'source-failure-policy',
                '사용자 요청: 출처 원문 확인 실패는 보류 대신 신뢰도 검증 기준 미충족 FAIL로 반영. 답변 재수집·재평가 호출 없음.')
    if policy == 'final':
        from code_eval.semantic_policy import FINAL_DECISION_POLICY_VERSION
        return (FINAL_DECISION_POLICY_VERSION, DECISION_POLICY, 'binary-decision-policy',
                '사용자 요청: 보류 판정을 원문 근거 재검증 후 PASS 또는 검증 기준 미충족 FAIL로 확정. 답변 재수집·재평가 호출 없음.')
    raise ValueError('지원하지 않는 최종 판정 정책입니다.')


def _linked_path(root, batch):
    saved = batch.get('run', {}).get('saved_to')
    if not isinstance(saved, str) or not saved:
        raise ValueError('연결된 평가 파일 경로가 없습니다.')
    path = (root / saved).resolve()
    allowed = (root / 'data' / 'code_eval_runs').resolve()
    run_id = batch.get('run', {}).get('run_id')
    if (not path.is_relative_to(allowed) or path.parent != allowed
            or not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{12}', run_id)
            or path.name != f'code-eval-{run_id}.json' or not path.is_file()):
        raise ValueError('연결된 평가 파일이 허용된 저장 경로와 일치하지 않습니다.')
    return path


def _without_saved_to(batch):
    value = copy.deepcopy(batch)
    value.get('run', {}).pop('saved_to', None)
    return value


def _replace_file(source, target):
    source.replace(target)


def _write_atomic(path, body):
    temporary = path.with_name(path.name + '.policy-' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(body)
        _replace_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def migrate(root, run_id, *, apply=False, policy='source_original'):
    """Return an audit report. No files or directories are written in dry run."""
    root = Path(root).resolve()
    policy_version, policy_manifest, backup_kind, policy_reason = _policy_settings(policy)
    if not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{12}', run_id):
        raise ValueError('실행 ID는 12자리 소문자 16진수여야 합니다.')
    comparison_path = root / 'data' / 'comparison_runs' / f'{run_id}.json'
    original_body = comparison_path.read_bytes()
    original = json.loads(original_body)
    if original.get('run_id') != run_id or original.get('status') != 'COMPLETED':
        raise ValueError('완료된 동일 실행 기록에만 정책을 적용할 수 있습니다.')
    changed = copy.deepcopy(original)
    timestamp = datetime.now(timezone.utc).isoformat()
    originals, replacements = {comparison_path: original_body}, {}
    modifications = []
    before, after = {}, {}
    for provider, batch in changed.get('evaluations', {}).items():
        if batch.get('live'):
            raise ValueError('진행 중인 평가에는 정책을 적용할 수 없습니다.')
        original_batch = original['evaluations'][provider]
        before[provider] = _counts(original_batch)
        provider_modifications = []
        contexts = conversation_contexts(batch['cases']) if policy == 'final' else {}
        for index, record in enumerate(batch.get('jev_eval', {}).get('records', [])):
            if policy == 'source_original':
                update = apply_source_original_failure_policy(record)
                if update['changed']:
                    provider_modifications.append({'provider': provider, 'case_index': index,
                        'case_id': record['case_id'], 'criterion': 'J5', 'before': 'UNVERIFIED',
                        'after': 'FAIL', 'source_failures': copy.deepcopy(update['failures'])})
            elif isinstance(record, dict):
                from code_eval.semantic_policy import apply_final_decision_policy
                previous = copy.deepcopy(record.get('decisions') or {})
                update = apply_final_decision_policy(record, state=_evaluation_state(batch, index, contexts))
                if update['changed']:
                    for cid, decision in (record.get('decisions') or {}).items():
                        old = previous.get(cid) or {}
                        if old.get('status') == 'UNVERIFIED' and decision.get('decision') in ('PASS', 'FAIL'):
                            provider_modifications.append({'provider': provider, 'case_index': index,
                                'case_id': record['case_id'], 'criterion': cid, 'before': 'UNVERIFIED',
                                'after': decision['decision'], 'reason_code': decision.get('reason_code'),
                                'reason': decision.get('reason')})
        if provider_modifications:
            linked = _linked_path(root, original_batch)
            standalone_body = linked.read_bytes()
            standalone = json.loads(standalone_body)
            if _without_saved_to(standalone) != _without_saved_to(original_batch):
                raise ValueError(f'{provider}: 연결된 평가 파일 내용이 비교 결과와 일치하지 않습니다.')
            _refresh_summary(batch, decision_policy=policy_manifest,
                             refresh_conversations=any(item['criterion'] == 'J6' for item in provider_modifications))
            # Retain the standalone file's own run metadata (usually no saved_to).
            linked_updated = copy.deepcopy(batch)
            linked_updated['run'] = copy.deepcopy(standalone['run'])
            originals[linked] = standalone_body
            replacements[linked] = _encode(linked_updated)
            modifications.extend(provider_modifications)
        after[provider] = _counts(batch)

    report = {'schema': 'aiona-source-failure-policy-migration/v1', 'run_id': run_id,
        'policy_version': policy_version, 'policy': policy,
        'status': 'DRY_RUN' if modifications else 'NO_CHANGES',
        'mode': 'apply' if apply else 'dry_run', 'timestamp': timestamp,
        'changed_records': modifications, 'before': before, 'after': after,
        'external_calls': 0, 'original_inputs_preserved': True,
        'original_judge_configuration_preserved': True,
        'running_server_refresh_required': bool(modifications), 'files': []}
    if not modifications:
        report['files'] = [{'path': comparison_path.relative_to(root).as_posix(),
                            'before_sha256': _hash(original_body), 'after_sha256': _hash(original_body)}]
        return report

    update_id = hashlib.sha256(f'aiona-policy:{run_id}:{policy_version}'.encode('utf-8')).hexdigest()[:32]
    if any(item.get('policy_version') == policy_version for item in changed.get('evaluation_policy_updates') or []):
        raise ValueError('같은 정책 적용 기록이 있지만 보류 판정이 남아 있습니다. 기록을 확인해 주세요.')
    changed.setdefault('evaluation_policy_updates', []).append({
        'policy_version': policy_version, 'applied_at': timestamp, 'update_id': update_id, 'reason': policy_reason,
        'actor': 'local', 'changed_records': len(modifications),
        'before': before, 'after': after,
        'original_evaluation_profile': copy.deepcopy(original.get('evaluation_profile')),
        'original_evaluation_progress': copy.deepcopy(original.get('evaluation_progress'))})
    _refresh_progress(changed, timestamp)
    if 'evaluation_profile' in changed:
        changed['evaluation_profile'] = evaluation_profile(changed)
    changed['revision'] = changed.get('revision', 0) + 1
    replacements[comparison_path] = _encode(changed)
    report['update_id'] = update_id
    report['files'] = [{'path': path.relative_to(root).as_posix(),
                       'before_sha256': _hash(originals[path]), 'after_sha256': _hash(body)}
                      for path, body in replacements.items()]
    if not apply:
        return report

    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup_root = root / 'backups' / backup_kind / f'{stamp}-{run_id}'
    backup_root.mkdir(parents=True, exist_ok=False)
    for path, body in originals.items():
        backup = backup_root / path.relative_to(root)
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(body)
    report['backup_directory'] = backup_root.relative_to(root).as_posix()
    audit_path = backup_root / 'audit.json'
    report['status'] = 'PREPARED'
    _write_atomic(audit_path, _encode(report))
    # 버전 목록 DB 의 쓰기 잠금으로 서버의 저장과 겹치지 않게 파일을 바꾼다 (기록은 실행 JSON 의 evaluation_policy_updates)
    store = RegistryStore(root / 'data' / 'aiona_versions.sqlite3')
    replaced = []
    try:
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for path, body in originals.items():
                if path.read_bytes() != body:
                    raise ValueError('검토 이후 저장 파일이 변경되어 적용을 중단했습니다.')
            for path, body in replacements.items():
                _write_atomic(path, body)
                replaced.append(path)
    except Exception:
        restore_errors = []
        for path in reversed(replaced):
            try:
                _write_atomic(path, originals[path])
            except Exception as error:
                restore_errors.append({'path': path.relative_to(root).as_posix(), 'error': type(error).__name__})
        report['status'] = 'ROLLBACK_FAILED' if restore_errors else 'ROLLED_BACK'
        report['restore_errors'] = restore_errors
        _write_atomic(audit_path, _encode(report))
        raise
    report['status'] = 'APPLIED'
    try:
        _write_atomic(audit_path, _encode(report))
    except OSError:
        # The transaction already committed. Do not undo successfully committed
        # files while leaving a committed policy event behind.
        report['audit_warning'] = '정책 적용은 완료됐으나 감사 파일의 완료 상태를 저장하지 못했습니다.'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--policy', choices=('source_original', 'final'), default='source_original',
                        help='final은 원문 재검증 후 모든 의미 보류를 PASS/FAIL로 확정합니다.')
    parser.add_argument('--apply', action='store_true', help='정확한 원본 백업 후 변경을 저장합니다. 기본값은 읽기 전용 검토입니다.')
    args = parser.parse_args()
    print(json.dumps(migrate(ROOT, args.run_id, apply=args.apply, policy=args.policy), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
