"""Offline admission tests; no actual time delays or API calls."""
import unittest
from src.rest_policy import RESTConfig, RESTCoordinator, RESTAdmissionError, request_weight, shared_coordinator


class Clock:
    def __init__(self): self.now = 0.0; self.waits = []
    def __call__(self): return self.now
    def wait(self, seconds): self.waits.append(seconds); self.now += seconds


class RESTPolicyTests(unittest.TestCase):
    def test_weights(self):
        self.assertEqual(request_weight({'type': 'l2Book'}), 2)
        self.assertEqual(request_weight({'type': 'metaAndAssetCtxs'}), 20)
        self.assertEqual(request_weight({'type': 'userRole'}), 60)
        self.assertEqual(request_weight({'type': 'candleSnapshot', 'req': {'interval':'1h', 'startTime':0, 'endTime':49*3600000}}), 21)
        self.assertEqual(request_weight({'type': 'candleSnapshot'}, [None]*204), 24)
        self.assertEqual(request_weight({'type': 'fundingHistory'}), 45)
        self.assertEqual(request_weight({'type': 'fundingHistory'}, [None]*72), 24)

    def test_available_budget_then_window_expiry(self):
        clock = Clock(); c = RESTCoordinator(RESTConfig(budget=40), timer=clock, wait=clock.wait)
        a = c.acquire(20, 100); c.release(a)
        b = c.acquire(20, 100); c.release(b)
        self.assertEqual(clock.now, 0)
        d = c.acquire(20, 100); c.release(d)
        self.assertEqual(clock.now, 60)

    def test_available_burst_and_immediate_window_rejection(self):
        clock=Clock(); c=RESTCoordinator(RESTConfig(budget=40),timer=clock, wait=clock.wait)
        clock.now=1000
        a=c.acquire(20, 1010); c.release(a)
        b=c.acquire(20,1010); c.release(b)
        with self.assertRaises(RESTAdmissionError): c.acquire(20,1000.5)
        self.assertEqual(clock.waits, [])

    def test_inflight_bound_without_real_sleep(self):
        clock=Clock(); c=RESTCoordinator(RESTConfig(max_inflight=1), timer=clock, wait=clock.wait)
        ticket=c.acquire(20, 10)
        with self.assertRaises(RESTAdmissionError): c.acquire(20,0.03)
        c.release(ticket)
        clock.now=2
        ticket=c.acquire(20, 10); c.release(ticket)

    def test_shared_registry_and_conflicting_config(self):
        a=shared_coordinator('https://shared-test.invalid/info')
        self.assertIs(a,shared_coordinator('https://shared-test.invalid/info/'))
        with self.assertRaises(ValueError): shared_coordinator('https://shared-test.invalid/info',RESTConfig(budget=800))

    def test_cooldown_and_reconcile_success(self):
        clock=Clock(); c=RESTCoordinator(timer=clock, wait=clock.wait)
        ticket=c.acquire(45,20); c.release(ticket,24)
        self.assertEqual(ticket[1],24)
        c.cooldown(5)
        ticket=c.acquire(2,20); c.release(ticket)
        self.assertEqual(clock.now,5)

    def test_policy_validation_and_overweight(self):
        for kwargs in ({'budget':1201},{'window_seconds':59},{'max_inflight':True},{'max_attempts':0}):
            with self.assertRaises(ValueError): RESTConfig(**kwargs)
        clock=Clock(); c=RESTCoordinator(RESTConfig(budget=10),timer=clock,wait=clock.wait)
        with self.assertRaises(RESTAdmissionError): c.acquire(20,20)

    def test_three_inflight_reservations_block_a_fourth_thread(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        clock = Clock(); waiting, resume = Event(), Event()
        c = RESTCoordinator(timer=clock, wait=clock.wait)
        tickets = [c.acquire(2,100) for _ in range(3)]
        def wait(seconds):
            waiting.set()
            if not resume.wait(timeout=2): raise AssertionError("release not signalled")
            clock.wait(seconds)
        c.wait=wait
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(c.acquire,2,100)
            self.assertTrue(waiting.wait(timeout=2))
            self.assertEqual(c._inflight,3)
            c.release(tickets.pop()); resume.set()
            tickets.append(future.result(timeout=2))
            self.assertEqual(c._inflight,3)
        for ticket in tickets: c.release(ticket)
        self.assertEqual(c._inflight,0)

    def test_unexpected_response_surcharge_becomes_debt(self):
        clock=Clock(); c=RESTCoordinator(RESTConfig(budget=30),timer=clock,wait=clock.wait)
        ticket=c.acquire(20,100); c.release(ticket,40)
        with self.assertRaises(RESTAdmissionError): c.acquire(2,10)
        self.assertEqual(sum(x[1] for x in c._entries),40)

    def test_complete_request_release_time_rejects_without_wait_or_transport(self):
        from unittest.mock import Mock
        from src.hl_client import HyperliquidInfoClient, HyperliquidAPIError
        clock=Clock(); c=RESTCoordinator(timer=clock,wait=clock.wait)
        for index in range(44):
            clock.now=index*4/3
            ticket=c.acquire(20,100); c.release(ticket)
        clock.now=44*4/3
        ticket=c.acquire(2,100); c.release(ticket)
        clock.now=59; clock.waits=[]
        transport=Mock(side_effect=AssertionError("HTTP must not start"))
        client=HyperliquidInfoClient(coordinator=c,transport=transport)
        with client.request_scope(deadline_seconds=2), self.assertRaises(HyperliquidAPIError) as caught:
            client.funding_history("TEST",0,0)
        self.assertEqual(caught.exception.code,"budget_wait_exceeded")
        self.assertEqual(clock.now,59)
        self.assertEqual(clock.waits,[])
        transport.assert_not_called()

    def test_waits_once_until_enough_reservations_expire(self):
        clock=Clock(); c=RESTCoordinator(timer=clock,wait=clock.wait)
        for index in range(44):
            clock.now=index*4/3
            ticket=c.acquire(20,100); c.release(ticket)
        clock.now=44*4/3
        ticket=c.acquire(2,100); c.release(ticket)
        clock.now=59; clock.waits=[]
        ticket=c.acquire(45,62)
        self.assertAlmostEqual(clock.now,61+1/3)
        self.assertEqual(len(clock.waits),1)
        self.assertLessEqual(sum(entry[1] for entry in c._entries),900)
        c.release(ticket)

    def test_future_commitment_converts_to_consumption_once(self):
        clock=Clock(); c=RESTCoordinator(timer=clock,wait=clock.wait)
        with c.reservation(163,180):
            self.assertEqual(c.reserved_weight,163)
            with c.reservation(163,180):
                self.assertEqual(c.reserved_weight,163)
                ticket=c.acquire(45,10)
                self.assertEqual(c.reserved_weight,163)
                c.release(ticket,24)
                self.assertEqual(c.reserved_weight,142)
        self.assertEqual(c.reserved_weight,24)
        self.assertEqual(c._commitments,{})
        self.assertEqual(c._inflight,0)

    def test_exception_releases_unused_commitment_but_keeps_failed_attempt(self):
        clock=Clock(); c=RESTCoordinator(timer=clock,wait=clock.wait)
        with self.assertRaises(RuntimeError):
            with c.reservation(163,180):
                ticket=c.acquire(20,10); c.release(ticket)
                raise RuntimeError('synthetic')
        self.assertEqual(c.reserved_weight,20)
        self.assertEqual(c._commitments,{})

    def test_discovery_headroom_and_admission_wait_are_bounded(self):
        clock=Clock(); c=RESTCoordinator(RESTConfig(budget=200),timer=clock,wait=clock.wait)
        with c.stage_headroom(100,180):
            with c.reservation(80,180):
                ticket=c.acquire(80,10); c.release(ticket)
            self.assertEqual(c.reserved_weight,180)
            with self.assertRaises(RESTAdmissionError) as caught:
                with c.reservation(40,2): pass
            self.assertEqual(caught.exception.code,'admission_wait_exceeded')
            self.assertLessEqual(clock.now,2)
        self.assertEqual(c.reserved_weight,80)
        with c.reservation(120,180):
            self.assertEqual(c.reserved_weight,200)
        self.assertEqual(c.reserved_weight,80)

    def test_committed_capture_blocks_uncommitted_requests_without_overshoot(self):
        clock=Clock(); c=RESTCoordinator(RESTConfig(budget=100),timer=clock,wait=clock.wait)
        with c.reservation(80,180):
            # Another thread cannot use this capture's credit.
            from concurrent.futures import ThreadPoolExecutor
            def other():
                try: c.acquire(30,0.05)
                except RESTAdmissionError: return 'blocked'
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertEqual(pool.submit(other).result(timeout=2),'blocked')
            self.assertEqual(c.reserved_weight,80)
            ticket=c.acquire(80,10); c.release(ticket)
            self.assertEqual(c.reserved_weight,80)
        self.assertEqual(c.reserved_weight,80)

    def test_spent_commitment_cannot_be_reused_as_a_new_full_capture(self):
        clock=Clock(); c=RESTCoordinator(timer=clock,wait=clock.wait)
        with c.reservation(163,180):
            ticket=c.acquire(20,10); c.release(ticket)
            with self.assertRaises(RESTAdmissionError) as caught:
                with c.reservation(163,180): pass
            self.assertEqual(caught.exception.code,'commitment_weight_exceeds_reservation')
            self.assertEqual(c.reserved_weight,163)
        self.assertEqual(c.reserved_weight,20)
