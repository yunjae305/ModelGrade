"""Per-execution USD estimates, held in memory and printed only to server stdout.

Public standard text rates verified 2026-10-11 (USD per million tokens):
https://developers.openai.com/api/docs/pricing
https://developers.openai.com/api/docs/guides/prompt-caching
https://platform.claude.com/docs/en/models/sonnet-5-5/overview
https://platform.claude.com/docs/en/about-claude/pricing (Claude Opus 5.5 rates added 2026-10-12)
https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool
https://ai.google.dev/gemini-api/docs/pricing#gemini-3.8-flash
No billing requests, prompts, credentials, or monetary result fields are emitted.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation


MILLION = Decimal(1_000_000)
PRINT_LOCK = threading.Lock()
PRICING_CHECKED = '2026-10-11'


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('invalid token count')
    return value


def _money(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError('invalid cost')
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError('invalid cost')
    return amount


def _label(value):
    # Model IDs are metadata, never prompts or arbitrary provider error text.
    return re.sub(r'[^a-zA-Z0-9_./:-]', '_', str(value or 'unknown'))[:120]


def _usd(amount):
    return '<$0.000001' if 0 < amount < Decimal('.000001') else f'${amount:.6f}'


def estimate(provider, model, usage, *, web_search_calls=0, service_tier=None, inference_geo=None, today=None):
    """Return (USD, total input, output); reject unknown rates/invalid usage."""
    if not isinstance(usage, dict) or not usage:
        raise ValueError('사용량 미확인')
    if provider in ('claude', 'anthropic_api') and service_tier is None:
        service_tier = usage.get('service_tier')
    model = str(model or '').removeprefix('models/')
    if service_tier not in (None, 'default', 'standard'):
        raise ValueError('서비스 등급 단가 미확인')
    try:
        if provider in ('gpt', 'openai_api'):
            rates = {'gpt-6-luna': ('0.10', '0.01', '0.125', '0.50'),
                     'gpt-6.1-sol': ('2', '0.10', '2.50', '10')}
            if model not in rates:
                raise ValueError('모델 단가 미확인')
            inp, out = _count(usage.get('input_tokens')), _count(usage.get('output_tokens'))
            details = usage.get('input_tokens_details')
            if not isinstance(details, dict):
                raise ValueError('캐시 사용량 미확인')
            cached = _count(details.get('cached_tokens'))
            written = _count(details.get('cache_write_tokens'))
            if cached + written > inp:
                raise ValueError('사용량 불일치')
            base, read, write, output = map(Decimal, rates[model])
            if inp > 272_000:
                base, read, write, output = base * 2, read * 2, write * 2, output * Decimal('1.5')
            # Reasoning tokens are already in output_tokens; do not add twice.
            amount = ((inp - cached - written) * base + cached * read + written * write + out * output) / MILLION
            if web_search_calls is None:
                raise ValueError('검색 사용량 미확인')
            searches = _count(web_search_calls)
            return amount + searches * Decimal('0.01'), inp, out
        if provider in ('claude', 'anthropic_api'):
            # input, cache read, 5m cache write, 1h cache write, output
            rates = {'claude-sonnet-5-5': ('2', '.10', '2.50', '4', '10'),
                     'claude-opus-5-5': ('4', '.20', '5', '8', '20')}
            if model not in rates:
                raise ValueError('모델 단가 미확인')
            ordinary, out = _count(usage.get('input_tokens')), _count(usage.get('output_tokens'))
            if provider == 'anthropic_api':
                # Server tools may create caches without explicit cache_control.
                # Unknown counts/TTLs must not silently acquire the lowest rate.
                if any(usage.get(key) is None for key in ('cache_read_input_tokens', 'cache_creation_input_tokens')):
                    raise ValueError('캐시 사용량 미확인')
                cached = _count(usage['cache_read_input_tokens'])
                written = _count(usage['cache_creation_input_tokens'])
                ttl = usage.get('cache_creation')
                if written and (not isinstance(ttl, dict) or any(ttl.get(key) is None for key in
                        ('ephemeral_1h_input_tokens', 'ephemeral_5m_input_tokens'))):
                    raise ValueError('캐시 저장 기간별 사용량 미확인')
                ttl = {} if ttl is None else ttl
                hour = _count(ttl.get('ephemeral_1h_input_tokens', 0))
                short = _count(ttl.get('ephemeral_5m_input_tokens', 0))
            else:
                # Keep the existing collection provider's compatibility behavior.
                cached = _count(usage.get('cache_read_input_tokens', 0))
                written = _count(usage.get('cache_creation_input_tokens', 0))
                ttl = usage.get('cache_creation') or {}
                hour = _count(ttl.get('ephemeral_1h_input_tokens', 0))
                short = _count(ttl.get('ephemeral_5m_input_tokens', written - hour))
            if short + hour != written:
                raise ValueError('사용량 불일치')
            inp = ordinary + cached + written
            # Both models use standard rates throughout their 1M context window.
            base, read, short_write, hour_write, output = map(Decimal, rates[model])
            amount = (ordinary * base + cached * read + short * short_write
                      + hour * hour_write + out * output) / MILLION
            geography = inference_geo if inference_geo is not None else usage.get('inference_geo')
            if provider == 'anthropic_api' or inference_geo is not None or 'inference_geo' in usage:
                if geography not in ('global', 'us'):
                    raise ValueError('추론 지역별 단가 미확인')
                if geography == 'us':
                    amount *= Decimal('1.1')
            if provider == 'anthropic_api':
                # Thinking and search-generated content are already in token
                # totals. Only the provider's billable search count is added.
                if web_search_calls is None:
                    raise ValueError('검색 사용량 미확인')
                searches = _count(web_search_calls)
                # The US premium applies to tokens, not the per-search fee.
                amount += searches * Decimal('.01')
            return amount, inp, out
        if provider == 'gemini':
            if model != 'gemini-3.8-flash':
                raise ValueError('모델 단가 미확인')
            inp = _count(usage.get('promptTokenCount'))
            cached = _count(usage.get('cachedContentTokenCount', 0))
            out = _count(usage.get('candidatesTokenCount', 0)) + _count(usage.get('thoughtsTokenCount', 0))
            if 'candidatesTokenCount' not in usage and 'thoughtsTokenCount' not in usage:
                raise ValueError('사용량 미확인')
            if cached > inp:
                raise ValueError('사용량 불일치')
            # This application never creates explicit caches or enables Gemini tools.
            factor = 1 if (today or date.today()) <= date(2026, 12, 31) else 2
            amount = ((inp - cached) * Decimal('.75') + cached * Decimal('.075') + out * Decimal('3.75')) / MILLION
            return amount * factor, inp, out
        raise ValueError('모델 단가 미확인')
    except (TypeError, AttributeError, InvalidOperation):
        raise ValueError('사용량 형식 미확인') from None


@dataclass
class _Total:
    calls: int = 0
    known: int = 0
    usd: Decimal = Decimal(0)
    input_tokens: int = 0
    output_tokens: int = 0
    reasons: set = field(default_factory=set)
    reported: bool = False


class CostLedger:
    """One execution window. Never attach this object to a serialized run/batch."""
    def __init__(self):
        self._rows = {}
        self._lock = threading.Lock()
        self._closed = False

    def _record(self, phase, provider, model, usage=None, *, reported=None, reason=None,
                web_search_calls=0, service_tier=None, inference_geo=None):
        amount, inp, out = None, 0, 0
        try:
            if reason:
                raise ValueError(reason)
            if reported is not None:
                amount = _money(reported)
            elif provider.endswith('_cli'):
                raise ValueError('CLI 달러 청구액 미제공')
            else:
                amount, inp, out = estimate(provider, model, usage, web_search_calls=web_search_calls,
                                            service_tier=service_tier, inference_geo=inference_geo)
        except ValueError as exc:
            reason = str(exc)
        with self._lock:
            if self._closed:
                return
            row = self._rows.setdefault((phase, _label(provider), _label(model)), _Total())
            row.calls += 1
            if amount is None:
                row.reasons.add(reason or '사용량 미확인')
            else:
                row.known += 1
                row.usd += amount
                row.input_tokens += inp
                row.output_tokens += out
                row.reported |= reported is not None

    def record_collection(self, provider, configured_model, raw=None):
        raw = raw if isinstance(raw, dict) else {}
        self._record('수집', provider, raw.get('model') or raw.get('modelVersion') or configured_model,
                     raw.get('usageMetadata' if provider == 'gemini' else 'usage'),
                     service_tier=raw.get('service_tier'))

    def _semantic(self, config, call):
        if getattr(call, 'status', None) == 'SKIPPED' and getattr(call, 'attempts', None) == 0:
            return  # A cancelled, never-started request has no provider usage.
        provider = getattr(config, 'engine', 'unknown')
        accounting = getattr(call, 'accounting', {}) or {}
        model = accounting.get('model') or getattr(config, 'model', 'unknown')
        model_usage = accounting.get('model_usage')
        usage = accounting.get('usage', getattr(call, 'usage', {}))
        unreported = _count(accounting.get('unreported_attempts', 0))
        if provider in ('anthropic_api', 'openai_api', 'dual') and 'cost_parts' in accounting:
            # Each API request (including pause_turn continuation) has its
            # own cache details, tier, geography, and search counter. Aggregate usage is
            # for display only and must never be priced again here.
            parts = accounting['cost_parts']
            if not isinstance(parts, list):
                raise ValueError('요청별 사용량 형식 미확인')
            for part in parts:
                if not isinstance(part, dict):
                    self._record('평가', provider, model, reason='요청별 사용량 형식 미확인')
                    continue
                part_model = part.get('model') or model
                # 병행 판정(dual)의 요청별 사용량은 심판마다 단가가 다르다 — Jev 요청은 jev, 나머지는 openai_api 로 센다
                part_provider = ('jev' if str(part_model).startswith('jev') else 'openai_api') if provider == 'dual' else provider
                if part_provider == 'openai_api' and part.get('service_tier') is None:
                    self._record('평가', part_provider, part_model, reason='서비스 등급 미기록')
                    continue
                self._record('평가', part_provider, part_model, part.get('usage'),
                             web_search_calls=part.get('web_search_calls'),
                             service_tier=part.get('service_tier'), inference_geo=part.get('inference_geo'))
            attempts = getattr(call, 'attempts', None)
            if attempts is not None:
                unreported = max(unreported, max(0, _count(attempts) - len(parts)))
            by_model = accounting.get('unreported_by_model') if provider == 'dual' else None
            if isinstance(by_model, dict) and sum(_count(n) for n in by_model.values()) >= unreported:
                # 병행 판정 — 미확인 요청도 심판(모델)별 행에 적는다 (Jev 시간 초과를 Luna 행으로 세지 않게)
                for part_model, n in by_model.items():
                    part_provider = 'jev' if str(part_model).startswith('jev') else 'openai_api'
                    for _ in range(min(_count(n), 100)):
                        self._record('평가', part_provider, part_model, reason='요청 사용량 미확인')
                return
            for _ in range(min(unreported, 100)):
                self._record('평가', provider, model, reason='요청 사용량 미확인')
            return
        if isinstance(model_usage, dict) and model_usage:
            for resolved, item in model_usage.items():
                self._record('평가', provider, resolved, item.get('usage'), reported=item.get('reported_cost_usd'))
        elif usage or accounting.get('reported_cost_usd') is not None or not unreported:
            self._record('평가', provider, model, usage,
                         reported=accounting.get('reported_cost_usd'),
                         web_search_calls=accounting.get('web_search_calls'),
                         service_tier=accounting.get('service_tier'),
                         inference_geo=accounting.get('inference_geo'))
        for _ in range(min(unreported, 100)):
            self._record('평가', provider, model, reason='요청 사용량 미확인')

    def wrap_resolver(self, resolver):
        ledger = self
        def resolve(*args, **kwargs):
            client, config, reason = resolver(*args, **kwargs)
            if client is None:
                return client, config, reason
            class MeteredClient:
                def __getattr__(self, name):
                    return getattr(client, name)

                def system_one(self, *args, **kwargs):
                    try:
                        call = client.system_one(*args, **kwargs)
                    except Exception:
                        ledger._record('평가', getattr(config, 'engine', 'unknown'), getattr(config, 'model', 'unknown'),
                                       reason='요청 종료 후 사용량 미확인')
                        raise
                    try:
                        ledger._semantic(config, call)
                    except Exception:
                        # Accounting is ancillary and must never change a verdict.
                        ledger._record('평가', getattr(config, 'engine', 'unknown'), getattr(config, 'model', 'unknown'),
                                       reason='사용량 형식 미확인')
                    return call
            return MeteredClient(), config, reason
        return resolve

    def finish(self, run_id, status):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            lines = [f'\n[LLM 비용] 실행 {_label(run_id)} | {_label(status)} | USD | 공개 단가 기준 {PRICING_CHECKED}']
            total, unknown = Decimal(0), 0
            for (phase, provider, model), row in sorted(self._rows.items()):
                missing = row.calls - row.known
                total += row.usd
                unknown += missing
                money = _usd(row.usd) if row.known else '확인 불가'
                basis = ('CLI 제공 추정액' if provider.endswith('_cli') else '제공자 제공 추정액') if row.reported else '추정액'
                suffix = f' | 미확인 {missing}건 ({", ".join(sorted(row.reasons))})' if missing else ''
                lines.append(f'  {phase} | {provider} | {model} | {basis} {money} | 호출 {row.calls}건{suffix}')
                if row.known and not row.reported:
                    lines.append(f'    비용 산정 {row.known}건 기준 · 입력 {row.input_tokens:,} / 출력 {row.output_tokens:,} 토큰')
            if not self._rows:
                lines.append('  이번 작업의 LLM 호출 없음')
            title = '확인 가능한 부분 합계' if unknown else '추정 합계'
            lines.append(f'  {title}: {_usd(total)}' + (f' | 미확인 {unknown}건 제외' if unknown else ''))
            lines.append('  세금·계정 할인·무료 할당 제외. 확정 청구액은 제공자 청구서 기준.\n')
            self._rows.clear()
        # One write keeps simultaneous comparison summaries together. Closed stdout
        # (or a terminal encoding issue) must not fail the completed evaluation.
        try:
            with PRINT_LOCK:
                print('\n'.join(lines), flush=True)
        except (OSError, UnicodeError, ValueError):
            pass
