"""Offline TTL/context tests, preserving original receipt timestamps."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
from src.hl_client import CapturedResponse, HyperliquidAPIError
from src.market_context import MarketContextProvider
from test_rest_policy import Clock
from src.rest_policy import RESTConfig


class MarketContextTests(unittest.TestCase):
    def make(self):
        clock = Clock()
        client = Mock()
        client.meta_and_asset_contexts.side_effect = lambda: CapturedResponse(({'universe':[{'name':'TEST'}]},[{'markPx':'1'}]),int(clock.now*1000))
        provider = MarketContextProvider(client, rest_config=RESTConfig(), timer=clock,wait=clock.wait,clock_ms=lambda:int(clock.now*1000))
        return clock, client, provider

    def test_reuse_original_timestamp_and_detached_values(self):
        clock,client,provider=self.make()
        first=provider.get(); clock.now=9
        first.value[1][0]['markPx']='changed'
        response=provider.market_context('test')
        self.assertEqual(response.captured_at_ms,0)
        self.assertEqual(response.value[1]['markPx'],'1')
        client.meta_and_asset_contexts.assert_called_once()

    def test_expiration_and_no_stale_fallback(self):
        clock,client,provider=self.make()
        provider.get(); clock.now=11
        self.assertEqual(provider.get().captured_at_ms,11000)
        self.assertEqual(client.meta_and_asset_contexts.call_count,2)
        clock.now=22
        client.meta_and_asset_contexts.side_effect=HyperliquidAPIError('synthetic')
        with self.assertRaises(HyperliquidAPIError): provider.get()

    def test_wall_clock_and_monotonic_invalidation(self):
        clock,client,provider=self.make()
        provider.get(); clock.now=11
        client.meta_and_asset_contexts.side_effect=None
        client.meta_and_asset_contexts.return_value=CapturedResponse(({'universe':[]},[]),0)
        with self.assertRaises(HyperliquidAPIError) as caught: provider.get()
        self.assertEqual(caught.exception.code,'stale_context')

    def test_lock_wait_obeys_deadline_without_real_sleep(self):
        clock,client,provider=self.make()
        provider._lock.acquire()
        try:
            with self.assertRaises(HyperliquidAPIError): provider.get(deadline_seconds=0.025)
        finally: provider._lock.release()
        client.meta_and_asset_contexts.assert_not_called()
        self.assertLess(clock.now,0.025)

    def test_only_one_concurrent_refresh(self):
        clock,client,provider=self.make()
        barrier=threading.Barrier(3)
        def get():
            barrier.wait(timeout=2)
            return provider.get()
        with ThreadPoolExecutor(max_workers=3) as pool:
            responses=list(pool.map(lambda _:get(),range(3)))
        self.assertEqual(client.meta_and_asset_contexts.call_count,1)
        self.assertEqual([r.captured_at_ms for r in responses],[0,0,0])

    def test_missing_symbol_and_malformed_context(self):
        clock,client,provider=self.make()
        with self.assertRaises(HyperliquidAPIError): provider.market_context('UNKNOWN')
        clock.now=11
        client.meta_and_asset_contexts.side_effect=None
        client.meta_and_asset_contexts.return_value=CapturedResponse({},11000)
        with self.assertRaises(HyperliquidAPIError): provider.get()
