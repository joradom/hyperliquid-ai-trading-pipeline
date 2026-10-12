"""Fast offline checks of benchmark instrumentation, separate from timed runs."""
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[2] / 'benchmarks/offline_rest_benchmark.py'
SPEC = importlib.util.spec_from_file_location('offline_rest_benchmark', PATH)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)
REPORT_SPEC = importlib.util.spec_from_file_location('report_offline_rest', PATH.with_name('report_offline_rest.py'))
reporter = importlib.util.module_from_spec(REPORT_SPEC)
REPORT_SPEC.loader.exec_module(reporter)


class HarnessTests(unittest.TestCase):
    def recorder(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        recorder = benchmark.Recorder(Path(temporary.name) / 'run')
        self.addCleanup(recorder.stream.close)
        return recorder

    def test_guard_blocks_network_and_reports_harness_error(self):
        recorder = self.recorder()
        with benchmark.guarded(recorder):
            with self.assertRaises(AssertionError):
                socket.create_connection(('synthetic.invalid', 443))
        self.assertEqual(recorder.violations[0]['category'], 'harness')
        self.assertEqual(recorder.violations[0]['code'], 'external_io_attempt')

    def test_debt_is_distinguished_from_illegal_admission(self):
        recorder = self.recorder()
        coordinator = benchmark.TrackedCoordinator(recorder, 'test')
        ticket = coordinator.acquire(45, benchmark.time.monotonic()+1)
        coordinator.release(ticket, 920)
        self.assertEqual(coordinator.reserved_weight, 920)
        with self.assertRaises(benchmark.HyperliquidAPIError) as caught:
            client = benchmark.HyperliquidInfoClient(coordinator=coordinator,
                transport=lambda *args, **kwargs: self.fail('transport cannot start'))
            with client.request_scope(deadline_seconds=.1):
                client.l2_book('T00')
        self.assertEqual(caught.exception.code, 'budget_wait_exceeded')
        self.assertFalse(recorder.violations)
        debt = [row for row in recorder.events if row['kind'] == 'weight_entry' and row['debt']]
        self.assertEqual(len(debt), 1)
        coordinator.clean_check()

    def test_real_threads_reuse_snapshot_and_context(self):
        recorder = self.recorder()
        runner = benchmark.Benchmark(recorder)
        with benchmark.guarded(recorder):
            result = runner.research(runner.coordinator('test'), pool=3)
        self.assertEqual(result['captures'], 3)
        self.assertEqual(result['model_calls'], 3)
        self.assertEqual(result['endpoints']['metaAndAssetCtxs'], 1)
        self.assertFalse(recorder.violations)
        report = json.loads((recorder.directory / result['report']).read_text())
        self.assertFalse(report['execution_authorized'])
        self.assertTrue(report['read_only'])

    def test_percentiles_include_all_observations(self):
        self.assertEqual(benchmark.stats([])['p95'], None)
        self.assertEqual(benchmark.stats(range(1, 101))['p95'], 95)

    def test_mixed_invoked_and_expired_reasons_are_json_safe(self):
        recorder = self.recorder()
        runner = benchmark.Benchmark(recorder)
        with benchmark.guarded(recorder):
            runner.research(runner.coordinator('test'), pool=3)
        runner.reports[0]['review_reasons']['snapshot_expired'] = 1
        recorder.save('summary.json', runner.summary())
        self.assertEqual(runner.reports[0]['review_reasons']['invoked'], 3)

    def test_isolated_saturation_worker_writes_reusable_evidence(self):
        recorder = self.recorder()
        original = benchmark.Benchmark.research
        def small_workload(runner, coordinator, **kwargs):
            return original(runner, coordinator, pool=3, max_admitted=kwargs['max_admitted'])
        destination = recorder.directory / 'child'
        with patch.object(benchmark.Benchmark, 'research', small_workload):
            worker = benchmark.multiprocessing.get_context('fork').Process(
                target=benchmark.saturation_child, args=(destination, 1, recorder.started))
            worker.start()
            worker.join(5)
            if worker.is_alive():
                worker.terminate()
                worker.join(2)
                self.fail('synthetic child did not finish')
        self.assertEqual(worker.exitcode, 0)
        summary = json.loads((destination / 'summary.json').read_text())
        self.assertTrue(summary['passed'])
        self.assertEqual(summary['research_rounds'][0]['captures'], 3)
        self.assertEqual(summary['coordinators'][0]['scope'], 'vol_delta-capacity-1')

    def test_report_keeps_independent_quota_windows_separate(self):
        recorder = self.recorder()
        runner = benchmark.Benchmark(recorder)
        for scope in ('a', 'b'):
            recorder.event('http', scope=scope, endpoint='metaAndAssetCtxs',
                           reserved=700, actual=700, seconds=.01, status='ok')
        recorder.save('manifest.json', {'mode': 'synthetic-report-test'})
        recorder.save('summary.json', runner.summary())
        report = reporter.analyze(recorder.directory)
        self.assertEqual(report['rolling_windows']['a']['peak_60s_final_response_weights'], 700)
        self.assertEqual(report['rolling_windows']['b']['peak_60s_final_response_weights'], 700)

    def test_report_measures_reuse_and_observed_runner_duration(self):
        recorder = self.recorder()
        runner = benchmark.Benchmark(recorder)
        with benchmark.guarded(recorder):
            runner.research(runner.coordinator('smoke'), pool=3)
        recorder.save('manifest.json', {'mode': 'smoke'})
        recorder.save('summary.json', runner.summary())
        report = reporter.analyze(recorder.directory)
        self.assertEqual(report['context_units_avoided'], 60)
        self.assertEqual(report['observed_synthetic_model_duration']['n'], 3)
        self.assertGreaterEqual(report['observed_synthetic_model_duration']['p50'], .05)

    def test_report_separates_repeated_revalidation_scope_instances(self):
        recorder = self.recorder()
        runner = benchmark.Benchmark(recorder)
        for _ in range(2):
            recorder.event('http', scope='revalidation-normal', endpoint='metaAndAssetCtxs',
                           reserved=700, actual=700, seconds=.01, status='ok')
            recorder.event('coordinator', scope='revalidation-exhausted')
        recorder.save('manifest.json', {'mode': 'synthetic-report-test'})
        recorder.save('summary.json', runner.summary())
        report = reporter.analyze(recorder.directory)
        self.assertEqual(len(report['rolling_windows']), 2)
        self.assertTrue(all(row['peak_60s_final_response_weights'] == 700
                            for row in report['rolling_windows'].values()))

    def test_context_timestamp_collision_between_rounds_is_not_a_cache_hit(self):
        recorder = self.recorder()
        runner = benchmark.Benchmark(recorder)
        for number in (1, 2):
            recorder.round = number
            recorder.event('context_use', age_seconds=0, timestamp_ms=123456)
            recorder.event('context_use', age_seconds=0, timestamp_ms=123456)
        summary = runner.summary()
        self.assertEqual(summary['context_unique_responses'], 2)
        self.assertEqual(summary['context_reused_uses'], 2)


if __name__ == '__main__':
    unittest.main()
