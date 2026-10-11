"""Offline stage allocation and complete-capture admission regression checks."""
import unittest
from unittest.mock import patch
from src.rest_policy import RESTCoordinator
from virtual_pipeline import scenario


class JointAdmissionTests(unittest.TestCase):
    def test_complete_discovery_and_captures_never_exceed_reserved_window(self):
        coordinators=[]
        class Tracked(RESTCoordinator):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs); self.observations=[]; coordinators.append(self)
            def record(self):
                used=self.reserved_weight
                self.observations.append(used)
                if used>900: raise AssertionError('reserved rolling window exceeded')
            def _reserve(self,*args,**kwargs):
                token=super()._reserve(*args,**kwargs); self.record(); return token
            def acquire(self,*args,**kwargs):
                ticket=super().acquire(*args,**kwargs); self.record(); return ticket
            def release(self,*args,**kwargs):
                super().release(*args,**kwargs); self.record()
        with patch('virtual_pipeline.RESTCoordinator',Tracked):
            summary,report=scenario('vol_delta',50,.02,model_latency=20)
        self.assertEqual(summary['complete_captures'],10)
        self.assertEqual(summary['classified'],{'SKIP':0,'WATCH':0,'ANALYZE':10})
        self.assertEqual(report['screener']['screened_count'],50)
        self.assertEqual(report['screener']['errors'],[])
        self.assertEqual(report['status'],'ok')
        self.assertEqual(summary['model_invocations'],10)
        self.assertNotIn('snapshot_expired',summary['review_reasons'])
        self.assertLessEqual(summary['max_capture_duration'],25)
        coordinator=coordinators[0]
        self.assertLessEqual(max(coordinator.observations),900)
        self.assertEqual(coordinator._commitments,{})
        self.assertEqual(coordinator._headrooms,{})
        self.assertEqual(coordinator._inflight,0)
        self.assertEqual(summary['calls_by_endpoint']['fundingHistory'],10)
        self.assertEqual(summary['calls_by_endpoint']['candleSnapshot'],90)

    def test_admission_error_is_delivered_and_leases_are_released(self):
        from unittest.mock import Mock
        from src.rest_policy import RESTConfig
        from src.hl_client import HyperliquidInfoClient
        from src.market_context import MarketContextProvider
        from src.analysis_pipeline import run_pipeline
        from src.scanner import scan_symbols
        from test_rest_policy import Clock
        from test_market_state import NOW
        clock=Clock(); transport=Mock(side_effect=AssertionError('HTTP must not start'))
        coordinator=RESTCoordinator(RESTConfig(budget=200,admission_wait_seconds=1),timer=clock,wait=clock.wait)
        ticket=coordinator.acquire(100,10); coordinator.release(ticket)
        client=HyperliquidInfoClient(coordinator=coordinator,transport=transport)
        provider=MarketContextProvider(client,clock_ms=lambda:NOW)
        def scanner(symbols,**kwargs): return scan_symbols(symbols,context_provider=provider,**kwargs)
        scanner.supports_incremental_review=True
        runner=Mock(side_effect=AssertionError('rejected capture cannot reach IA'))
        report=run_pipeline(['TEST'],scanner=scanner,runner=runner,clock_ms=lambda:NOW,timer=clock)
        self.assertEqual(report['status'],'failed')
        self.assertEqual(len(report['results']),1)
        self.assertEqual(report['results'][0]['classification'],'SKIP')
        self.assertIn('admission_wait_exceeded',report['results'][0]['error']['message'])
        self.assertEqual(coordinator._commitments,{})
        runner.assert_not_called(); transport.assert_not_called()
