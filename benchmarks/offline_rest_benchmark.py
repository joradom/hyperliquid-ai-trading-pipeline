"""Real-thread, synthetic-only REST/research benchmark. Standard library only."""
from __future__ import annotations

import argparse
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import multiprocessing
from pathlib import Path
import platform
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'python'), str(ROOT / 'python/tests')]
from virtual_pipeline import BASE, synthetic_value
from test_hl_client import Response
from test_analysis_pipeline import output
from src.rest_policy import RESTConfig, RESTCoordinator, request_weight
from src.hl_client import HyperliquidInfoClient, HyperliquidAPIError
from src.market_context import MarketContextProvider
from src.market_state import build_market_state
from src.research_config import ResearchConfig
from src.research_pipeline import run_research
from src.fast_revalidation import capture_pair
from src.review_session import ReviewSession, _Lease


def stats(values):
    values = sorted(values)
    return {'count': len(values), 'sum': round(sum(values), 6),
            **{key: round(values[max(0, math.ceil(len(values)*fraction)-1)], 6) if values else None
               for key, fraction in [('p50', .5), ('p95', .95), ('max', 1)]}}


class Recorder:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=False)
        self.stream = (directory / 'events.jsonl').open('x', encoding='utf-8')
        self.lock = threading.RLock()
        self.started = time.monotonic()
        self.events = []
        self.violations = []
        self.phase = 'setup'
        self.round = 0

    def event(self, kind, **data):
        with self.lock:
            row = {'t': round(time.monotonic()-self.started, 6), 'phase': self.phase,
                   'round': self.round, 'kind': kind, **data}
            self.events.append(row)
            self.stream.write(json.dumps(row, allow_nan=False, sort_keys=True)+'\n')
            self.stream.flush()

    def check(self, condition, code, category='pipeline', **detail):
        if not condition:
            with self.lock:
                self.violations.append({'phase': self.phase, 'round': self.round,
                                        'code': code, 'category': category, **detail})
            self.event('violation', code=code, category=category, **detail)

    def save(self, name, value):
        (self.directory / name).write_text(json.dumps(value, indent=2, sort_keys=True,
                                                     allow_nan=False)+'\n', encoding='utf-8')


class TrackedCoordinator(RESTCoordinator):
    def __init__(self, recorder, scope):
        super().__init__()  # Production defaults, including real monotonic time.
        self.recorder, self.scope = recorder, scope
        self.operation = threading.local()
        self.peak_reserved = 0
        self.peak_inflight = 0
        self.admission_ledger = []
        owner = self

        class Ledger(deque):
            def append(self, entry):
                super().append(entry)
                # Called under the production lock, after commitment credit is spent.
                total = owner._used()
                debt = getattr(owner.operation, 'release', False)
                owner.recorder.check(debt or total <= owner.config.budget,
                                     'admission_budget_exceeded', scope=scope, reserved=total)
                owner.recorder.event('weight_entry', scope=scope, weight=entry[1],
                                     debt=debt, reserved=total)
                owner.admission_ledger.append((entry[0], entry[1], debt))
        self._entries = Ledger()

    def _used(self):
        value = super()._used()
        self.peak_reserved = max(self.peak_reserved, value)
        return value

    def acquire(self, weight, deadline):
        begin = time.monotonic()
        try:
            entry = super().acquire(weight, deadline)
            with self._lock:
                self.peak_inflight = max(self.peak_inflight, self._inflight)
                self.recorder.check(self._inflight <= self.config.max_inflight,
                                     'http_slot_limit', scope=self.scope)
            return entry
        except Exception as exc:
            self.recorder.event('admission_error', scope=self.scope,
                                code=getattr(exc, 'code', type(exc).__name__))
            raise
        finally:
            self.recorder.event('request_admission', scope=self.scope,
                                seconds=time.monotonic()-begin)

    def _reserve(self, *args, **kwargs):
        begin = time.monotonic()
        try:
            return super()._reserve(*args, **kwargs)
        finally:
            elapsed = time.monotonic()-begin
            self.recorder.event('capture_admission', scope=self.scope, seconds=elapsed)
            self.recorder.check(elapsed <= self.config.admission_wait_seconds+.2,
                                 'capture_admission_deadline', scope=self.scope)

    def release(self, entry, actual_weight=None):
        self.operation.release = True
        try:
            super().release(entry, actual_weight)
            self.recorder.event('reconciled', scope=self.scope, weight=entry[1],
                                actual=actual_weight)
        finally:
            self.operation.release = False

    def clean_check(self):
        with self._lock:
            self.recorder.check(not self._commitments and not self._headrooms and self._inflight == 0,
                                 'resources_not_released', scope=self.scope)
            self.recorder.event('coordinator', scope=self.scope,
                                peak_reserved=self.peak_reserved, peak_inflight=self.peak_inflight,
                                outstanding_inflight=self._inflight,
                                outstanding_commitments=len(self._commitments),
                                outstanding_headrooms=len(self._headrooms))


class SyntheticTransport:
    def __init__(self, recorder, wall, *, fault=None, latency=.02, scope='research'):
        self.recorder, self.wall = recorder, wall
        self.fault, self.latency, self.scope = fault, latency, scope
        self.symbols = ['T'+str(i).zfill(2) for i in range(50)]
        self.lock = threading.Lock()
        self.counts = Counter()
        self.active = self.context_active = 0
        self.blocked = threading.Event()
        self.release_block = threading.Event()

    def __call__(self, request, timeout):
        payload = json.loads(request.data)
        kind = payload['type']
        with self.lock:
            self.counts[kind] += 1
            number = self.counts[kind]
            self.active += 1
            self.context_active += kind == 'metaAndAssetCtxs'
            active = self.active
        started = time.monotonic()
        actual = None
        status = 'ok'
        try:
            self.recorder.check(active <= 3, 'transport_concurrency', scope=self.scope)
            time.sleep(min(self.latency, timeout))
            if self.latency >= timeout:
                raise TimeoutError('synthetic timeout')
            if self.fault in ('429', 'retry_date', 'retry_long') and kind == 'metaAndAssetCtxs':
                retry = '15' if self.fault == 'retry_long' else '1'
                if self.fault == 'retry_date':
                    from email.utils import format_datetime
                    retry = format_datetime(datetime.fromtimestamp(self.wall()/1000+1, timezone.utc), usegmt=True)
                raise urllib.error.HTTPError('https://synthetic.invalid/info', 429, 'synthetic',
                                             {'Retry-After': retry}, io.BytesIO(b'synthetic rate limit'))
            if self.fault == 'refresh' and kind == 'metaAndAssetCtxs' and number > 1:
                raise urllib.error.URLError('synthetic refresh failure')
            if self.fault == 'timeout' and kind == 'l2Book':
                time.sleep(timeout)
                raise TimeoutError('synthetic timeout')
            if self.fault == 'blocked':
                self.blocked.set()
                self.release_block.wait()  # Deliberately ignore timeout; controlled release by harness.
            if self.fault == 'json' and kind == 'fundingHistory':
                return Response('{invalid synthetic json')
            value = synthetic_value(payload, self.symbols, self.wall)
            if self.fault == 'oversize' and kind == 'fundingHistory':
                value = value*250  # 18000 records: explicit debt test, not a nominal size.
            actual = request_weight(payload, value)
            return Response(json.dumps(value))
        except Exception as exc:
            status = getattr(exc, 'code', type(exc).__name__)
            raise
        finally:
            with self.lock:
                self.active -= 1
                self.context_active -= kind == 'metaAndAssetCtxs'
            self.recorder.event('http', scope=self.scope, endpoint=kind, attempt=number,
                                seconds=time.monotonic()-started, status=status,
                                reserved=request_weight(payload), actual=actual)


class TrackedProvider(MarketContextProvider):
    def __init__(self, recorder, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.recorder = recorder

    def get(self, **kwargs):
        try:
            response = super().get(**kwargs)
            age = (self.clock_ms()-response.captured_at_ms)/1000
            self.recorder.check(0 <= age <= self.ttl_seconds+.01, 'stale_context_served')
            self.recorder.event('context_use', age_seconds=age, timestamp_ms=response.captured_at_ms)
            return response
        except Exception as exc:
            self.recorder.event('context_error', code=getattr(exc, 'code', type(exc).__name__))
            raise


def guarded(recorder):
    stack = ExitStack()
    def blocked(*args, **kwargs):
        recorder.check(False, 'external_io_attempt', 'harness')
        raise AssertionError('external I/O forbidden in offline benchmark')
    for target in ('urllib.request.urlopen', 'socket.create_connection', 'socket.socket.connect',
                   'socket.socket.connect_ex', 'subprocess.run', 'subprocess.Popen', 'os.system'):
        stack.enter_context(patch(target, side_effect=blocked))
    return stack


class Benchmark:
    def __init__(self, recorder):
        self.r = recorder
        self.baseline_threads = threading.active_count()
        self.coordinators = []
        self.wall = lambda: BASE+int((time.monotonic()-recorder.started)*1000)
        self.reports = []
        self.child_coordinators = []

    def coordinator(self, scope):
        c = TrackedCoordinator(self.r, scope)
        self.coordinators.append(c)
        return c

    def client(self, coordinator, transport):
        return HyperliquidInfoClient(api_url='https://synthetic.invalid/info', coordinator=coordinator,
                                     transport=transport, clock_ms=self.wall, rng=lambda: .5)

    def research(self, coordinator, *, mode='volume', pool=10, incremental=True,
                 max_admitted=3, model_latency=.05, fault=None):
        self.r.round += 1
        transport = SyntheticTransport(self.r, self.wall, fault=fault, scope=coordinator.scope)
        client = self.client(coordinator, transport)
        provider = TrackedProvider(self.r, client, clock_ms=self.wall)
        captures, model_calls = {}, []
        lock = threading.Lock()
        recorder = self.r
        original_admit, original_release = ReviewSession.admit, _Lease.release

        def admit(session, *args):
            lease = original_admit(session, *args)
            with session._condition:
                recorder.check(0 <= session._available <= session.capacity, 'review_capacity')
                recorder.event('review_capacity', active=session.capacity-session._available,
                               capacity=session.capacity)
            return lease

        def release(lease):
            original_release(lease)
            with lease.session._condition:
                recorder.check(0 <= lease.session._available <= lease.session.capacity, 'review_capacity')

        def builder(symbol, **kwargs):
            begin = time.monotonic()
            try:
                market = build_market_state(symbol, **kwargs)
                issues = [i['code'] for i in market['data_quality']['issues'] if i['code'] != 'unknown_freshness']
                with lock:
                    recorder.check(symbol not in captures, 'duplicate_capture')
                    captures[symbol] = time.monotonic()
                recorder.event('capture', symbol=symbol, seconds=time.monotonic()-begin, issues=issues)
                if fault is None:
                    recorder.check(not issues, 'nominal_capture_degraded', issues=issues)
                recorder.check(time.monotonic()-begin <= 25.2, 'capture_deadline')
                return market
            except Exception as exc:
                recorder.event('capture_error', symbol=symbol, code=getattr(exc, 'code', type(exc).__name__))
                raise

        def runner(command, **kwargs):
            snapshot = json.loads(kwargs['input'])
            expires = datetime.fromisoformat(snapshot['valid_until_utc'].replace('Z', '+00:00')).timestamp()*1000
            recorder.check(self.wall() <= expires, 'expired_snapshot_invoked')
            symbol = snapshot['symbol']
            with lock:
                model_calls.append(symbol)
                capture_end = captures.get(symbol)
            recorder.check(capture_end is not None, 'model_without_capture', 'harness')
            recorder.event('capture_to_model', symbol=symbol,
                           seconds=time.monotonic()-capture_end if capture_end else 0)
            time.sleep(model_latency)
            recorder.event('model', symbol=symbol, seconds=model_latency)
            return SimpleNamespace(returncode=0, stdout=output(), stderr='')

        begin = time.monotonic()
        with patch('src.scanner.build_market_state', builder), \
             patch.object(ReviewSession, 'admit', admit), patch.object(_Lease, 'release', release):
            report = run_research(configuration=ResearchConfig(), context_provider=provider,
                                  top_n=pool, limit=min(pool, 10), sort_by=mode,
                                  clock_ms=self.wall, runner=runner, incremental=incremental,
                                  max_admitted=max_admitted)
        scanner, analysis = report['scanner'], report['analysis']
        if scanner and analysis:
            scanner_rows = {r['symbol']: r for r in scanner['results']}
            for row in analysis['results']:
                source = scanner_rows[row['symbol']]
                recorder.check(source['decision_snapshot'] is row['decision_snapshot'], 'snapshot_not_reused')
                recorder.check(row['classification'] == 'ANALYZE' or not row['model_review']['invoked'],
                               'ineligible_model_invocation')
            recorder.check(len(model_calls) == len(set(model_calls)), 'duplicate_model_invocation')
            if fault is None:
                recorder.check(len(captures) == min(pool, 10), 'missing_nominal_capture')
        else:
            recorder.check(fault is not None, 'missing_nominal_stage')
        if fault is None and incremental:
            recorder.check(report['status'] == 'ok', 'nominal_research_failure', status=report['status'])
        # Expiry in the legacy batch comparator is an observed outcome, not an invariant violation.
        filename = f'round-{self.r.round:04d}.json'
        recorder.save(filename, report)
        metrics = {'report': filename, 'mode': mode, 'pool': pool, 'incremental': incremental,
                   'max_admitted': max_admitted, 'model_latency': model_latency, 'fault': fault,
                   'seconds': time.monotonic()-begin, 'status': report['status'],
                   'captures': len(captures), 'model_calls': len(model_calls),
                   'classification': scanner['counts'] if scanner else {},
                   'review_reasons': dict(Counter(row['model_review']['reason'] or 'invoked'
                       for row in analysis['results'])) if analysis else {},
                   'endpoints': dict(transport.counts),
                   'errors': report['errors'], 'selection_issues': report['selection_quality']['issues']}
        self.reports.append(metrics)
        recorder.event('research', **metrics)
        coordinator.clean_check()
        return metrics

    def fault_probe(self, fault):
        """Isolated scope per fault; never reset a running research coordinator."""
        coordinator = self.coordinator('probe-'+fault)
        transport = SyntheticTransport(self.r, self.wall, fault=fault, scope=coordinator.scope)
        client = self.client(coordinator, transport)
        code = None
        begin = time.monotonic()
        try:
            if fault == 'refresh':
                provider = TrackedProvider(self.r, client, clock_ms=self.wall)
                with ThreadPoolExecutor(max_workers=3) as pool:
                    responses = list(pool.map(lambda _: provider.get(), range(3)))
                self.r.check(transport.counts['metaAndAssetCtxs'] == 1, 'duplicate_concurrent_refresh')
                self.r.check(len({r.captured_at_ms for r in responses}) == 1, 'context_timestamp_changed')
                time.sleep(10.05)
                provider.get()
            elif fault == 'oversize':
                client.funding_history('T00', self.wall()-72*3600000, self.wall())
                before = sum(transport.counts.values())
                try:
                    with client.request_scope(deadline_seconds=.1):
                        client.l2_book('T00')
                except HyperliquidAPIError as exc:
                    code = exc.code
                self.r.check(sum(transport.counts.values()) == before, 'debt_did_not_block_http')
                self.r.check(code == 'budget_wait_exceeded', 'debt_error_missing')
            else:
                if fault in ('json', 'oversize'):
                    client.funding_history('T00', self.wall()-72*3600000, self.wall())
                elif fault == 'timeout':
                    with client.request_scope(deadline_seconds=1):
                        client.l2_book('T00')
                else:
                    client.meta_and_asset_contexts()
        except HyperliquidAPIError as exc:
            code = exc.code
            self.r.check(exc.attempts <= 3, 'retry_limit')
            self.r.event('expected_error', fault=fault, code=code, attempts=exc.attempts,
                         status_code=exc.status_code)
        expected = {'refresh': 'network_error', 'json': 'invalid_json', 'timeout': 'network_error',
                    '429': 'api_error', 'retry_date': 'api_error', 'retry_long': 'retry_wait_exceeded',
                    'oversize': 'budget_wait_exceeded'}
        self.r.check(code == expected[fault], 'fault_not_detected', fault=fault, observed=code)
        self.r.event('fault_probe', fault=fault, code=code, seconds=time.monotonic()-begin,
                     calls=dict(transport.counts))
        coordinator.clean_check()

    def revalidation_probe(self, blocked=False, exhausted=False, rate_limited=False):
        label = 'blocked' if blocked else 'exhausted' if exhausted else '429' if rate_limited else 'normal'
        coordinator = self.coordinator('revalidation-'+label)
        transport = SyntheticTransport(self.r, self.wall,
                                       fault='blocked' if blocked else '429' if rate_limited else None,
                                       scope=coordinator.scope)
        client = self.client(coordinator, transport)
        if exhausted:
            ticket = coordinator.acquire(900, time.monotonic()+1)
            coordinator.release(ticket)
        begin = time.monotonic()
        captured = capture_pair('T00', client=client, clock_ms=self.wall)
        elapsed = time.monotonic()-begin
        frozen = json.dumps(captured, default=lambda r: asdict(r), sort_keys=True)
        self.r.check(elapsed <= 3.2, 'revalidation_return_deadline', seconds=elapsed)
        self.r.check(all(n <= 1 for n in transport.counts.values()), 'revalidation_retry')
        if blocked:
            self.r.check(transport.blocked.is_set(), 'blocked_transport_not_started', 'harness')
            self.r.check(all(s['response'] is None for s in captured['sources'].values()),
                         'late_response_accepted')
        transport.release_block.set()
        deadline = time.monotonic()+2
        while coordinator._inflight and time.monotonic() < deadline:
            time.sleep(.01)
        self.r.check(frozen == json.dumps(captured, default=lambda r: asdict(r), sort_keys=True),
                     'late_result_mutated_report')
        if exhausted:
            self.r.check(not transport.counts, 'exhausted_revalidation_sent_http')
        self.r.event('revalidation', scenario=label, seconds=elapsed,
                     errors={k: s['error'] for k, s in captured['sources'].items()},
                     calls=dict(transport.counts))
        coordinator.clean_check()
        if blocked:
            transport.fault = None
            recovered = capture_pair('T00', client=client, clock_ms=self.wall)
            self.r.check(all(s['response'] is not None for s in recovered['sources'].values()),
                         'revalidation_did_not_recover')
            coordinator.clean_check()

    def phase(self, name, seconds, work):
        self.r.phase = name
        begin = time.monotonic()
        deadline = begin+seconds
        self.r.event('phase_start', target_seconds=seconds)
        work(deadline)
        remaining = deadline-time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        self.r.event('phase_end', seconds=time.monotonic()-begin,
                     drain_overrun_seconds=max(0, time.monotonic()-deadline))
        print(json.dumps({'phase': name, 'seconds': round(time.monotonic()-begin, 2),
                          'violations': len(self.r.violations)}), flush=True)

    def run(self, mode):
        if mode == 'smoke':
            c = self.coordinator('smoke')
            def smoke(deadline):
                while deadline-time.monotonic() > 65:
                    self.research(c, pool=3)
                self.revalidation_probe(blocked=True)
            self.phase('smoke', 120, smoke)
        else:
            self.phase('warmup', 120, lambda _: self.research(self.coordinator('warmup'), pool=3))
            c = self.coordinator('volume')
            def volume(deadline):
                # Pairing shares fixture, transport latency and continuous REST budget.
                for incremental in (False, True, False, True):
                    if deadline-time.monotonic() > 70:
                        self.research(c, incremental=incremental)
            self.phase('volume', 360, volume)
            def saturation(deadline):
                # Each variant has a process-local quota. Run isolated variants
                # together to fit both 50/10 workloads in the six-minute phase.
                # Fork only after all parent worker pools have drained.
                self.r.check(threading.active_count() == self.baseline_threads,
                             'fork_with_live_workers', 'harness')
                context = multiprocessing.get_context('fork')
                children = []
                for capacity in (1, 3):
                    path = self.r.directory/f'saturation-capacity-{capacity}'
                    child = context.Process(target=saturation_child,
                                            args=(path, capacity, self.r.started),
                                            name=f'offline-saturation-{capacity}')
                    child.start()
                    children.append((child, path))
                for child, path in children:
                    child.join(max(0, deadline-time.monotonic()))
                    if child.is_alive():
                        child.terminate()
                        child.join(2)
                        self.r.check(False, 'saturation_worker_deadline', 'unclassified')
                    self.r.check(child.exitcode == 0, 'saturation_worker_failed', 'unclassified',
                                 exitcode=child.exitcode)
                    if not (path/'summary.json').is_file():
                        self.r.check(False, 'missing_saturation_evidence', 'harness')
                        continue
                    summary = json.loads((path/'summary.json').read_text())
                    self.r.round += 1
                    with self.r.lock:
                        for line in (path/'events.jsonl').read_text().splitlines():
                            event = json.loads(line)
                            event['t'] += summary['parent_clock_offset_seconds']
                            event['phase'] = 'vol_delta'
                            event['round'] = self.r.round
                            self.r.events.append(event)
                            self.r.stream.write(json.dumps(event, sort_keys=True)+'\n')
                        self.r.stream.flush()
                    for row in summary['research_rounds']:
                        row['report'] = path.name+'/'+row['report']
                        self.reports.append(row)
                    self.r.violations.extend(summary['violations'])
                    self.child_coordinators.extend(summary['coordinators'])
            self.phase('vol_delta', 360, saturation)
            c = self.coordinator('fault-research')
            def faults(deadline):
                for fault in ('429', 'retry_date', 'retry_long', 'timeout', 'json', 'refresh', 'oversize'):
                    self.fault_probe(fault)
                for fault in ('json', '429', 'timeout'):
                    if deadline-time.monotonic() > 75:
                        self.research(c, pool=3, fault=fault)
            self.phase('faults', 360, faults)
            def revalidation(deadline):
                while deadline-time.monotonic() > 10:
                    self.revalidation_probe(blocked=True)
                    self.revalidation_probe(exhausted=True)
                    self.revalidation_probe(rate_limited=True)
                    self.revalidation_probe()
                    time.sleep(5)
            self.phase('revalidation', 240, revalidation)
        self.r.phase = 'final'
        for coordinator in self.coordinators:
            coordinator.clean_check()
        self.r.check(threading.active_count() <= self.baseline_threads, 'threads_not_drained',
                     threads=threading.active_count(), baseline=self.baseline_threads)

    def summary(self):
        events = self.r.events
        durations = {kind: stats([e['seconds'] for e in events if e['kind'] == kind])
                     for kind in ('http', 'request_admission', 'capture_admission', 'capture',
                                  'capture_to_model', 'model', 'research', 'revalidation')}
        http = [e for e in events if e['kind'] == 'http']
        context = [e for e in events if e['kind'] == 'context_use']
        unique_contexts = {(e['phase'], e['round'], e['timestamp_ms']) for e in context}
        return {'schema_version': '1.0', 'passed': not self.r.violations,
                'elapsed_seconds': time.monotonic()-self.r.started,
                'violations': self.r.violations, 'durations': durations,
                'http_requests': len(http), 'endpoints': dict(Counter(e['endpoint'] for e in http)),
                'reserved_attempt_weight': sum(e['reserved'] for e in http),
                'reconciled_attempt_weight': sum(e['actual'] if e['actual'] is not None else e['reserved'] for e in http),
                'context_uses': len(context), 'context_age': stats([e['age_seconds'] for e in context]),
                'context_unique_timestamps': len({e['timestamp_ms'] for e in context}),
                'context_unique_responses': len(unique_contexts),
                'context_reused_uses': len(context)-len(unique_contexts),
                'errors': dict(Counter(str(e.get('code')) for e in events if e['kind'] in
                                      ('admission_error', 'context_error', 'capture_error', 'expected_error'))),
                'research_rounds': self.reports,
                'coordinators': [{'scope': c.scope, 'peak_reserved': c.peak_reserved,
                                  'peak_inflight': c.peak_inflight} for c in self.coordinators]+self.child_coordinators,
                'limitations': ['Real scheduling is nondeterministic; fixtures and fault order are fixed.',
                                'Legacy batch expiry is reported, not a smoke-gating violation.',
                                'Phases include idle time; active operation counts are reported.',
                                'Probe scopes are isolated; research scopes persist through each phase.',
                                'Capacity 1 and 3 saturation variants run in isolated processes concurrently; quotas are separate.',
                                'No external per-IP quota claim; no hard cancellation claim for urllib.']}


def saturation_child(directory, capacity, parent_started):
    recorder = Recorder(directory)
    recorder.phase = 'vol_delta'
    benchmark = Benchmark(recorder)
    try:
        with guarded(recorder):
            benchmark.research(benchmark.coordinator(f'vol_delta-capacity-{capacity}'),
                               mode='vol_delta', pool=50, max_admitted=capacity, model_latency=20)
    except Exception as exc:
        recorder.check(False, 'uncaught_saturation_exception', 'unclassified',
                       exception_type=type(exc).__name__)
    finally:
        summary = benchmark.summary()
        summary['parent_clock_offset_seconds'] = recorder.started-parent_started
        recorder.save('summary.json', summary)
        recorder.stream.close()
    if not summary['passed']:
        raise SystemExit(1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('smoke', 'full', 'all'), default='all')
    parser.add_argument('--output', type=Path, default=ROOT/'benchmarks/results/offline-rest')
    args = parser.parse_args(argv)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    sources = [Path(__file__), ROOT/'python/tests/virtual_pipeline.py', *sorted((ROOT/'python/src').glob('*.py'))]
    metadata = {'commit': commit, 'python': platform.python_version(), 'system': platform.system(),
                'rest_config': asdict(RESTConfig()), 'synthetic_utc_base_ms': BASE, 'rng_value': .5,
                'transport_latency_seconds': .02, 'threads': 'real ThreadPoolExecutor and daemon revalidation workers',
                'sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}}
    modes = ('smoke', 'full') if args.mode == 'all' else (args.mode,)
    results = []
    for mode in modes:
        recorder = Recorder(args.output/mode)
        recorder.save('manifest.json', {**metadata, 'mode': mode, 'target_seconds': 120 if mode == 'smoke' else 1440})
        benchmark = Benchmark(recorder)
        try:
            with guarded(recorder):
                benchmark.run(mode)
        except Exception as exc:
            # Raw exception payloads/tracebacks may contain private paths; persist only type.
            recorder.check(False, 'uncaught_harness_exception', 'unclassified', exception_type=type(exc).__name__)
        finally:
            summary = benchmark.summary()
            recorder.save('summary.json', summary)
            recorder.stream.close()
        results.append({'mode': mode, 'passed': summary['passed'], 'elapsed_seconds': summary['elapsed_seconds']})
        print(json.dumps(results[-1]), flush=True)
        if not summary['passed']:
            break
    return 0 if all(r['passed'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
