"""SQLite recovery/integrity, retained identities and crash-point tests offline."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.flow_store import FlowStore, FlowStoreError, FlowIntegrityError
from src.order_flow import OrderFlow, Operation
from test_order_flow import START, event


class FlowStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/"flow.sqlite3"
    def tearDown(self):self.temp.cleanup()

    def prepare(self,**limits):
        journal=FlowStore(self.path,**limits);self.addCleanup(journal.close)
        flow=OrderFlow(max_seen=4)
        journal.write(flow,[Operation("connect","synthetic","ETH",START)])
        return journal,flow

    def test_restart_checkpoint_and_pending_recovery_without_double_count(self):
        journal,flow=self.prepare()
        journal.write(flow,[event(1,1),event(2,2,side="A")])
        self.assertEqual(journal.verify()["integrity"],"verified")
        state=flow.export_asset("synthetic","ETH",include_seen=False)
        journal.close()
        with FlowStore(self.path) as reopened:
            recovered=reopened.recover()
            self.assertEqual(state,recovered.export_asset("synthetic","ETH",include_seen=False))
            self.assertEqual(reopened.write(recovered,[event(1,1)])[0],"duplicate")
            reopened.write(recovered,[Operation("advance","synthetic","ETH",START+63_000,contact_ms=START+63_000)])
            view=recovered.snapshot("synthetic","ETH")
            self.assertEqual(view["observed_total"]["unique_trades"],2)
            self.assertEqual(view["cvd"]["base"],"0")

    def test_both_crash_points_roll_back_journal_checkpoint_and_engine(self):
        for point in ("after_journal","after_checkpoint"):
            self.path=Path(self.temp.name)/(point+".sqlite3")
            journal,flow=self.prepare()
            before=flow.export_asset("synthetic","ETH")
            rows=journal.status()["journal_rows"]
            def fault(stage):
                if stage==point:raise RuntimeError("private crash payload")
            with self.assertRaises(FlowStoreError) as caught:journal.write(flow,[event(1,1)],fault=fault)
            self.assertNotIn("private",str(caught.exception))
            self.assertEqual(before,flow.export_asset("synthetic","ETH"))
            self.assertEqual(journal.status()["journal_rows"],rows)
            self.assertEqual(journal.recover().snapshot("synthetic","ETH"),flow.snapshot("synthetic","ETH"))
            journal.close()

    def test_uncommitted_changes_invisible_to_other_connection(self):
        journal,flow=self.prepare()
        initial_rows=journal.status()["journal_rows"]
        def fault(stage):
            if stage=="after_checkpoint":
                with sqlite3.connect(str(self.path)) as reader:
                    self.assertEqual(reader.execute("SELECT count(*) FROM journal").fetchone()[0],initial_rows)
                raise RuntimeError("interruption")
        with self.assertRaises(FlowStoreError):journal.write(flow,[event(1,1)],fault=fault)
        self.assertEqual(journal.recover().asset("synthetic","ETH").observed.count,0)

    def test_durable_duplicates_beyond_memory_and_conflict_cannot_change_original(self):
        journal,flow=self.prepare()
        journal.write(flow,[event(index,index) for index in range(12)])
        self.assertLessEqual(len(flow.asset("synthetic","ETH").seen),4)
        self.assertEqual(journal.write(flow,[event(0,0)])[0],"duplicate")
        self.assertEqual(journal.write(flow,[event(0,0,size="2")])[0],"conflict")
        self.assertEqual(flow.asset("synthetic","ETH").observed.count,12)
        self.assertEqual(journal.db.execute("SELECT count(*) FROM journal WHERE kind='trade'").fetchone()[0],12)
        self.assertEqual(journal.verify()["integrity"],"verified")

    def test_capacity_retention_floor_and_recovery_integrity(self):
        journal,flow=self.prepare(max_journal_rows=5,retention_ms=5000)
        for index in range(20):
            journal.write(flow,[event(index,index),Operation("advance","synthetic","ETH",START+index*1000+2000,contact_ms=START+index*1000+2000)])
        self.assertLessEqual(journal.status()["journal_rows"],5)
        self.assertGreater(flow.asset("synthetic","ETH").retention_floor_ms,START)
        self.assertEqual(journal.write(flow,[event(0,0)])[0],"outside_retention")
        self.assertEqual(flow.asset("synthetic","ETH").observed.count,20)
        self.assertEqual(journal.verify()["integrity"],"verified")
        recovered=journal.recover()
        self.assertEqual(flow.export_asset("synthetic","ETH",include_seen=False),recovered.export_asset("synthetic","ETH",include_seen=False))

    def test_checkpoint_and_journal_tampering_detected(self):
        for target in ("checkpoint","journal","delete"):
            path=Path(self.temp.name)/(target+".sqlite3")
            with FlowStore(path) as journal:
                flow=OrderFlow();journal.write(flow,[Operation("connect","synthetic","ETH",START),event(1,1),event(2,2)])
                if target=="checkpoint":journal.db.execute("UPDATE checkpoints SET payload='{}'")
                elif target=="journal":journal.db.execute("UPDATE journal SET payload='{}' WHERE seq=2")
                else:journal.db.execute("DELETE FROM journal WHERE seq=2")
                with self.assertRaises(FlowIntegrityError):journal.recover()

    def test_versions_and_unrelated_database_refused(self):
        with sqlite3.connect(str(self.path)) as db:db.execute("CREATE TABLE unrelated(value TEXT)")
        with self.assertRaises(FlowStoreError):FlowStore(self.path)
        alternate=Path(self.temp.name)/"version.sqlite3"
        with sqlite3.connect(str(alternate)) as db:db.execute("PRAGMA user_version=99")
        with self.assertRaises(FlowStoreError):FlowStore(alternate)

    def test_storage_limits_survive_restart(self):
        journal,flow=self.prepare(max_journal_rows=12,retention_ms=12345,max_db_bytes=1_048_576)
        journal.close()
        with FlowStore(self.path) as restored:
            self.assertEqual(restored.max_rows,12)
            self.assertEqual(restored.retention_ms,12345)
            self.assertEqual(restored.max_db_bytes,1_048_576)

    def test_database_size_limit_failure_is_atomic(self):
        journal,flow=self.prepare(max_db_bytes=65536)
        before=flow.export_asset("synthetic","ETH")
        rows=journal.status()["journal_rows"]
        with self.assertRaises(FlowStoreError):
            journal.write(flow,[event(index,index) for index in range(512)])
        self.assertEqual(before,flow.export_asset("synthetic","ETH"))
        self.assertEqual(journal.status()["journal_rows"],rows)
        self.assertEqual(journal.verify()["integrity"],"verified")

    def test_stale_engine_cannot_overwrite_authoritative_checkpoint(self):
        journal,flow=self.prepare()
        journal.write(flow,[event(1,1)])
        stale=OrderFlow(max_seen=4)
        with self.assertRaises(FlowStoreError):journal.write(stale,[event(2,2)])
        self.assertEqual(journal.recover().asset("synthetic","ETH").observed.count,1)

    def test_no_personal_metadata_or_financial_operations_are_persisted(self):
        journal,flow=self.prepare()
        with patch("subprocess.run",side_effect=AssertionError("execution forbidden")),patch("urllib.request.urlopen",side_effect=AssertionError("network forbidden")):
            journal.write(flow,[event(1,1)])
            journal.recover()
        for row in journal.db.execute("SELECT payload FROM journal"):
            self.assertNotIn("users",row[0]);self.assertNotIn("wallet",row[0]);self.assertNotIn("hash",row[0])
        self.assertFalse(flow.snapshot("synthetic","ETH")["execution_authorized"])
