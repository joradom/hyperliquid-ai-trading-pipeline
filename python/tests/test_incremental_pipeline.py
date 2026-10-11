"""Incremental handoff tests: synthetic snapshots, no real API or model calls."""
import copy
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.analysis_pipeline import run_pipeline
from src.decision_snapshot import validate_decision_snapshot
from src.integrity import verify_checksum
from src.research_pipeline import run_research
from src.review_session import ReviewSession
from src.scanner import scan_symbols
from test_analysis_pipeline import output
import test_scanner as scanner_fixtures
from test_market_state import NOW
from virtual_pipeline import scenario


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.http = patch('urllib.request.urlopen',side_effect=AssertionError('real HTTP forbidden')).start()
        self.process = patch('subprocess.run',side_effect=AssertionError('real model forbidden')).start()
        self.addCleanup(patch.stopall)

    def scanner(self, builder):
        def scan(symbols, **kwargs):
            return scan_symbols(symbols,market_builder=builder,**kwargs)
        scan.supports_incremental_review = True
        return scan

    def result(self):
        return SimpleNamespace(returncode=0, stdout=output(), stderr='')

    def test_fast_row_reaches_model_before_slow_capture_finishes(self):
        fast_reviewed=threading.Event(); slow_entered=threading.Event(); finished=[]
        def builder(symbol):
            if symbol=='SLOW':
                slow_entered.set()
                if not fast_reviewed.wait(timeout=3): raise AssertionError('global scanner barrier remains')
            elif not slow_entered.wait(timeout=3): raise AssertionError('slow capture never started')
            market=scanner_fixtures.ScannerTests().market(symbol)
            finished.append(symbol)
            return market
        snapshots=[]
        def runner(command,**kwargs):
            snapshot=json.loads(kwargs['input']); snapshots.append(snapshot)
            if snapshot['symbol']=='FAST':
                self.assertNotIn('SLOW',finished)
                fast_reviewed.set()
            return self.result()
        report=run_pipeline(['SLOW','FAST'],scanner=self.scanner(builder),runner=runner,clock_ms=lambda:NOW+1000)
        self.assertEqual(report['symbols'],['SLOW','FAST'])
        self.assertEqual([r['symbol'] for r in report['results']],['SLOW','FAST'])
        self.assertCountEqual([s['symbol'] for s in snapshots],['SLOW','FAST'])
        for snapshot in snapshots:
            self.assertEqual(validate_decision_snapshot(snapshot),[])
            self.assertTrue(verify_checksum(snapshot))
        self.assertEqual(report['codex_invocations'],2)

    def test_capacity_wait_is_before_capture_without_snapshot_backlog(self):
        summary,report=scenario(max_admitted=1,model_latency=20)
        details=summary['details']
        self.assertEqual(summary['complete_captures'],10)
        self.assertEqual(summary['model_invocations'],10)
        self.assertNotIn('snapshot_expired',summary['review_reasons'])
        # Review holds its lease for 20 virtual seconds. The next capture starts
        # only afterwards; its own duration excludes that admission wait.
        for previous,current in zip(details,details[1:]):
            self.assertGreaterEqual(current['start']+0.001,previous['start']+previous['duration']+20)
        self.assertTrue(all(d['duration']<=25 for d in details))
        self.assertEqual(report['max_concurrent_reviews'] if 'max_concurrent_reviews' in report else report['analysis']['max_concurrent_reviews'],1)
        for row in report['scanner']['results']:
            snapshot=row['decision_snapshot']
            self.assertEqual(validate_decision_snapshot(snapshot),[])
            self.assertTrue(verify_checksum(snapshot))

    def test_virtual_ten_asset_comparison_same_transport_and_rest_budget(self):
        before,_=scenario(incremental=False)
        after,report=scenario(incremental=True)
        self.assertEqual((before['complete_captures'],after['complete_captures']),(10,10))
        self.assertGreater(before['review_reasons'].get('snapshot_expired',0),0)
        self.assertNotIn('snapshot_expired',after['review_reasons'])
        self.assertLess(before['model_invocations'],after['model_invocations'])
        self.assertEqual(after['model_invocations'],10)
        self.assertEqual(before['rest_requests'],after['rest_requests'])
        self.assertEqual(before['accounted_rest_weight'],after['accounted_rest_weight'])
        self.assertLessEqual(abs(before['elapsed']-after['elapsed']),0.05)
        self.assertFalse(report['execution_authorized'])
        json.dumps(report,allow_nan=False)

    def test_skip_watch_and_failed_capture_release_without_model(self):
        calls=[]
        def builder(symbol):
            calls.append(symbol)
            if symbol=='BAD': raise ValueError('synthetic failed capture')
            return scanner_fixtures.ScannerTests().market(symbol,'SKIP' if symbol=='SKIP' else 'WATCH')
        runner=Mock(side_effect=AssertionError('non-ANALYZE reached model'))
        report=run_pipeline(['SKIP','WATCH','BAD'],scanner=self.scanner(builder),runner=runner,
                            clock_ms=lambda:NOW+1000,max_admitted=1)
        self.assertCountEqual(calls,['SKIP','WATCH','BAD'])
        self.assertEqual(report['symbols'],['SKIP','WATCH','BAD'])
        self.assertEqual(report['counts'],{'SKIP':2,'WATCH':1,'ANALYZE':0})
        self.assertEqual(report['status'],'partial_failure')
        runner.assert_not_called()

    def test_duplicate_delivery_and_admission_never_repeat_review(self):
        review=Mock(side_effect=lambda row:copy.deepcopy(row))
        row={'symbol':'TEST','classification':'ANALYZE','decision_snapshot':{'integrity':{'checksum':'synthetic'}}}
        with ReviewSession(['TEST'],review,capacity=1) as session:
            with session.admit(0,'TEST'):
                session.deliver(0,row)
                session.deliver(0,row)
            self.assertEqual(session.finish([row]),[row])
            with self.assertRaises(ValueError): session.admit(0,'TEST')
            self.assertEqual(session._available,1)
        review.assert_called_once()

    def test_review_exception_releases_capacity(self):
        with ReviewSession(['FIRST','NEXT'],Mock(side_effect=RuntimeError('synthetic')),capacity=1) as session:
            first={'symbol':'FIRST','classification':'ANALYZE'}
            with session.admit(0,'FIRST'): session.deliver(0,first)
            with self.assertRaises(RuntimeError): session._futures[0].result(timeout=3)
            with session.admit(1,'NEXT'): pass
            self.assertEqual(session._available,1)

    def test_submit_failure_and_callback_failure_release_capacity(self):
        with ReviewSession(['FIRST','NEXT'],lambda row:row,capacity=1) as session:
            row={'symbol':'FIRST','classification':'ANALYZE'}
            with patch.object(session._pool,'submit',side_effect=RuntimeError('synthetic submit failure')):
                with self.assertRaises(RuntimeError),session.admit(0,'FIRST'): session.deliver(0,row)
            with session.admit(1,'NEXT'): pass
            self.assertEqual(session._available,1)
        released=[]
        class Admission:
            def __enter__(self): return self
            def __exit__(self,*args): released.append(True)
        report=scan_symbols(['HYPE'],market_builder=scanner_fixtures.ScannerTests().market,clock_ms=lambda:NOW+1000,
            capture_admission=lambda *args:Admission(),on_result=Mock(side_effect=RuntimeError('callback failed')))
        self.assertEqual(released,[True])
        self.assertEqual(report['status'],'failed')
        self.assertEqual(report['results'][0]['classification'],'ANALYZE')
        self.assertEqual(report['results'][0]['error']['stage'],'review_handoff')

    def test_legacy_callable_keeps_arguments_unless_explicitly_opted_in(self):
        scan=scanner_fixtures.ScannerTests().scan(['HYPE'])
        scanner=Mock(return_value=scan)
        report=run_pipeline(['HYPE'],scanner=scanner,runner=Mock(return_value=self.result()),clock_ms=lambda:NOW+1000)
        self.assertNotIn('capture_admission',scanner.call_args.kwargs)
        self.assertNotIn('on_result',scanner.call_args.kwargs)
        self.assertEqual(report['codex_invocations'],1)
        runner=Mock(side_effect=AssertionError('invalid protocol reached model'))
        with self.assertRaisesRegex(ValueError,'omitted'):
            run_pipeline(['HYPE'],scanner=scanner,runner=runner,clock_ms=lambda:NOW+1000,incremental=True)
        runner.assert_not_called()

    def test_model_failure_releases_for_later_capture(self):
        captures=[]
        def builder(symbol): captures.append(symbol); return scanner_fixtures.ScannerTests().market(symbol)
        def invoke(command,**kwargs):
            if json.loads(kwargs['input'])['symbol']=='FIRST': raise RuntimeError('synthetic model failure')
            return self.result()
        runner=Mock(side_effect=invoke)
        report=run_pipeline(['FIRST','NEXT'],scanner=self.scanner(builder),runner=runner,
                            clock_ms=lambda:NOW+1000,max_admitted=1)
        self.assertCountEqual(captures,['FIRST','NEXT'])
        self.assertEqual(runner.call_count,2)
        self.assertEqual(report['status'],'partial_failure')
        self.assertEqual(report['results'][1]['model_review']['status'],'completed')

    def test_original_classifications_timestamps_and_checksums_are_unchanged(self):
        markets={symbol:scanner_fixtures.ScannerTests().market(symbol,label)
                 for symbol,label in [('FIRST','ANALYZE'),('WATCH','WATCH'),('SKIP','SKIP')]}
        original=copy.deepcopy(markets)
        scanner=self.scanner(lambda symbol:markets[symbol])
        kwargs={'scanner':scanner,'clock_ms':lambda:NOW+1000}
        before=run_pipeline(list(markets),runner=Mock(return_value=self.result()),incremental=False,**kwargs)
        after=run_pipeline(list(markets),runner=Mock(return_value=self.result()),**kwargs)
        for old,new in zip(before['results'],after['results']):
            for key in ('symbol','classification','reasons','market_timestamp_utc','market_quality',
                        'pre_analysis','decision_snapshot','error'):
                self.assertEqual(old[key],new[key])
        self.assertEqual(markets,original)
        self.assertEqual(after['schema_version'],before['schema_version'])

    def test_capacity_is_held_until_fast_revalidation_finishes(self):
        from src.fast_revalidation import revalidation_failure
        from test_analysis_response import directional_response
        entered=threading.Event(); resume=threading.Event(); next_capture=threading.Event()
        current={'ms':NOW+1000}
        def builder(symbol):
            if symbol=='NEXT': next_capture.set()
            return scanner_fixtures.ScannerTests().market(symbol)
        def runner(command,**kwargs):
            snapshot=json.loads(kwargs['input'])
            current['ms']=NOW+60000
            return SimpleNamespace(returncode=0,stdout=output(directional_response(snapshot)),stderr='')
        def revalidator(row,**kwargs):
            self.assertEqual(kwargs['deadline_seconds'],3)
            entered.set()
            if not resume.wait(timeout=3): raise AssertionError('revalidation not released')
            return revalidation_failure(row,'REVALIDATION_ERROR',now_ms=current['ms'])
        revalidate=Mock(side_effect=revalidator)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(run_pipeline,['FIRST','NEXT'],scanner=self.scanner(builder),runner=runner,
                revalidator=revalidate,clock_ms=lambda:current['ms'],max_admitted=1)
            try:
                self.assertTrue(entered.wait(timeout=3))
                self.assertFalse(next_capture.is_set())
            finally: resume.set()
            report=future.result(timeout=3)
        self.assertTrue(next_capture.is_set())
        self.assertEqual(revalidate.call_count,1)
        self.assertEqual(report['results'][0]['fast_revalidation']['status'],'INDETERMINATE')

    def test_late_delivery_and_duplicate_checksum_are_rejected(self):
        review=Mock(side_effect=lambda row:row)
        with ReviewSession(['FIRST','NEXT'],review,capacity=2) as session:
            first={'symbol':'FIRST','classification':'ANALYZE',
                   'decision_snapshot':{'integrity':{'checksum':'synthetic'}}}
            with session.admit(0,'FIRST'): session.deliver(0,first)
            duplicate={'symbol':'NEXT','classification':'ANALYZE',
                       'decision_snapshot':{'integrity':{'checksum':'synthetic'}}}
            with self.assertRaisesRegex(ValueError,'checksum'),session.admit(1,'NEXT'):
                session.deliver(1,duplicate)
            session._futures[0].result(timeout=3)
            self.assertEqual(session._available,2)
        review.assert_called_once()
        review.reset_mock()
        with ReviewSession(['LATE'],review,capacity=1) as session:
            with session.admit(0,'LATE'): pass
            with self.assertRaisesRegex(ValueError,'outside'):
                session.deliver(0,{'symbol':'LATE','classification':'ANALYZE'})
        review.assert_not_called()

    def test_three_admitted_paths_bound_capture_and_review_saturation(self):
        saturated=threading.Event(); resume=threading.Event(); lock=threading.Lock()
        captures=[]; active=0; peak=0; reviewed=[]
        def builder(symbol):
            with lock: captures.append(symbol)
            return scanner_fixtures.ScannerTests().market(symbol)
        def runner(command,**kwargs):
            nonlocal active,peak
            symbol=json.loads(kwargs['input'])['symbol']
            with lock:
                active+=1; peak=max(peak,active); reviewed.append(symbol)
                if active==3: saturated.set()
            try:
                if not resume.wait(timeout=3): raise AssertionError('model workers not released')
                return self.result()
            finally:
                with lock: active-=1
        symbols=['A','B','C','D','E']
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(run_pipeline,symbols,scanner=self.scanner(builder),runner=runner,
                               clock_ms=lambda:NOW+1000)
            try:
                self.assertTrue(saturated.wait(timeout=3))
                with lock:
                    self.assertEqual(len(captures),3)
                    self.assertEqual(len(reviewed),3)
                    self.assertEqual(active,3)
            finally: resume.set()
            report=future.result(timeout=3)
        self.assertEqual(peak,3)
        self.assertCountEqual(captures,symbols)
        self.assertCountEqual(reviewed,symbols)
        self.assertEqual(report['symbols'],symbols)
        self.assertEqual(report['codex_invocations'],5)

    def test_research_adapter_preserves_explicit_scanner_capability(self):
        from src.market_screener import screen_market
        from test_market_screener import FakeScreenerClient
        slow_entered=threading.Event(); fast_reviewed=threading.Event(); captured=[]
        def builder(symbol):
            if symbol=='ETH':
                slow_entered.set()
                if not fast_reviewed.wait(timeout=3): raise AssertionError('Research lost incremental protocol')
            elif not slow_entered.wait(timeout=3): raise AssertionError('slow worker never started')
            market=scanner_fixtures.ScannerTests().market(symbol)
            captured.append(symbol)
            return market
        def runner(command,**kwargs):
            if json.loads(kwargs['input'])['symbol']=='BTC':
                self.assertNotIn('ETH',captured)
                fast_reviewed.set()
            return self.result()
        selection=screen_market(client=FakeScreenerClient(),sort_by='volume',limit=2,clock_ms=lambda:NOW)
        report=run_research(screener=Mock(return_value=selection),scanner=self.scanner(builder),
                           runner=runner,clock_ms=lambda:NOW+1000)
        self.assertEqual(report['scanner']['symbols'],['BTC','ETH'])
        self.assertEqual(report['analysis']['codex_invocations'],2)
        self.assertEqual(report['status'],'ok')

    def test_invalid_capacity_rejected_before_discovery(self):
        for capacity in (0,4,True,1.5):
            with self.assertRaises(ValueError): run_research(max_admitted=capacity)
        self.http.assert_not_called()
        self.process.assert_not_called()
