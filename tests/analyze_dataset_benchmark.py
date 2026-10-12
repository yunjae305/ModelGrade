"""Read-only evaluation diagnosis; writes a separate report, never rewrites a run."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from code_eval.common_adjudication import resolved_batch
from code_eval.engine import _UNJUDGED


def distribution(values):
    values = sorted(v for v in values if isinstance(v, (float, int)))
    if not values:
        return None
    return {'count': len(values), 'min': values[0], 'median': statistics.median(values),
            'p95': values[min(len(values) - 1, int((len(values) - 1) * .95))], 'max': values[-1]}


def diagnose(run):
    report = {'run_id': run['run_id'], 'status': run['status'], 'models': run.get('models'),
              'collection': {}, 'evaluation': {}, 'issues': [],
              'cost_location': 'server_terminal_only'}
    for provider, progress in run.get('progress', {}).items():
        rows = [row for row in run.get('datasets', {}).get(provider, []) if isinstance(row, dict)]
        report['collection'][provider] = {key: progress.get(key) for key in ('done', 'success', 'failed', 'incomplete', 'duration_ms')}
        report['collection'][provider]['request_latency_ms'] = distribution((row.get('collection') or {}).get('latency_ms') for row in rows)
        for row in rows:
            value = row.get('collection') or {}
            if value.get('status') != 'SUCCESS':
                report['issues'].append({'provider': provider, 'case_id': row.get('case_id'), 'type': 'collection',
                                         'status': value.get('status'), 'reason': value.get('error')})
    if run.get('created_at') and run.get('collection_completed_at'):
        report['collection_wall_s'] = (datetime.fromisoformat(run['collection_completed_at']) - datetime.fromisoformat(run['created_at'])).total_seconds()
    for provider, original in run.get('evaluations', {}).items():
        batch = resolved_batch(original)
        semantic = batch.get('jev_eval') or {}
        records = [r for r in semantic.get('records', []) if isinstance(r, dict)]
        criterion_counts = {}
        sources = Counter()
        quote_reasons = Counter()
        specialized = Counter()
        for record in records:
            identity = {'provider': provider, 'case_id': record.get('case_id')}
            if record.get('status') not in ('SUCCESS', 'RUNNING'):
                report['issues'].append({**identity, 'type': 'semantic_call', 'status': record.get('status'), 'reason': record.get('error')})
            for cid, decision in (record.get('decisions') or {}).items():
                status = decision.get('decision') or decision.get('status') or 'MISSING'
                criterion_counts.setdefault(cid, Counter())[status] += 1
                if status == 'UNVERIFIED':
                    report['issues'].append({**identity, 'type': 'semantic_unverified', 'criterion': cid,
                                             'reason': decision.get('reason'),
                                             'model_original_assessment': decision.get('model_original_assessment')})
                for check in decision.get('context_checks', []):
                    if check.get('decision') == 'UNVERIFIED':
                        report['issues'].append({**identity, 'type': 'context_unverified', 'criterion': cid,
                                                **{key: check.get(key) for key in ('turn', 'instruction_quote', 'answer_quote', 'reason')}})
                for claim in (decision.get('fact_check') or {}).get('claims', []):
                    if claim.get('verdict') == 'UNVERIFIED':
                        report['issues'].append({**identity, 'type': 'claim_unverified', 'criterion': cid,
                                                'claim': claim.get('claim'), 'reason': claim.get('reason'),
                                                'sources': [{key: s.get(key) for key in ('url', 'origin', 'quote_verified', 'verification_reason', 'assessment_warning')}
                                                            for s in claim.get('sources', []) if isinstance(s, dict)]})
                    for source in claim.get('sources', []):
                        if source.get('quote_verified') is False:
                            quote_reasons[source.get('verification_reason') or 'unspecified'] += 1
            for check in (record.get('verification') or {}).get('source_checks', []):
                sources[check.get('status') or 'unknown'] += 1
        for case in batch.get('cases', []):
            if case.get('case_status') != 'EVALUATED':
                report['issues'].append({'provider': provider, 'case_id': case.get('case_id'), 'type': 'code_input',
                                         'status': case.get('case_status'), 'reason': case.get('input_error') or case.get('error')})
            for row in case.get('results', []):
                if row.get('area') == 'aiona_specialized':
                    specialized[(row.get('check_id'), row.get('status'))] += 1
                if row.get('area') == 'common' and row.get('verdict') is None and row.get('status') in _UNJUDGED:
                    report['issues'].append({'provider': provider, 'case_id': case.get('case_id'), 'type': 'code_unjudged',
                                             'check_id': row.get('check_id'), 'status': row.get('status'), 'reason': row.get('reason')})
        report['evaluation'][provider] = {
            'status': semantic.get('status'), 'configuration': semantic.get('client'),
            'records': len(records), 'calls': dict(Counter(r.get('status') for r in records)),
            'review_records': sum(bool(r.get('review_required')) for r in records),
            'criteria': {cid: dict(counts) for cid, counts in criterion_counts.items()},
            'duration_ms': semantic.get('duration_ms'), 'latency_ms': distribution(r.get('latency_ms') for r in records),
            'api_ms': distribution((r.get('verification') or {}).get('timings_ms', {}).get('api') for r in records),
            'source_verification_ms': distribution((r.get('verification') or {}).get('timings_ms', {}).get('source_verification') for r in records),
            'slowest': [{'case_id': r.get('case_id'), 'latency_ms': r.get('latency_ms'), 'status': r.get('status')}
                        for r in sorted(records, key=lambda r: r.get('latency_ms') or 0, reverse=True)[:8]],
            'source_status_counts': dict(sources), 'quote_failure_reasons': dict(quote_reasons),
            'aiona_specialized_checks': [{'check_id': cid, 'status': status, 'count': count}
                                         for (cid, status), count in sorted(specialized.items())],
            'aiona_specialized_configuration': batch.get('aiona_config') if provider == 'aiona' else None,
            'resolved_code_summary': batch.get('summary'),
        }
    report['issue_counts'] = dict(Counter(issue['type'] for issue in report['issues']))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.run.resolve() == args.output.resolve():
        parser.error('원본 실행 파일을 덮어쓸 수 없습니다.')
    report = diagnose(json.loads(args.run.read_text(encoding='utf-8')))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'run_id': report['run_id'], 'status': report['status'],
                      'issue_counts': report['issue_counts'], 'report': str(args.output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
