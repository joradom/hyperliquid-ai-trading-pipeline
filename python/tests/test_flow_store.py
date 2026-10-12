"""SQLite recovery/integrity, retained identities and crash-point tests offline."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.flow_store import FlowStore, FlowStoreError, FlowIntegrityError
from src.order_flow import OrderFlow, Operation
from test_order_flow import START, event, legacy_record


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

    def prepare_legacy(self):
        journal=FlowStore(self.path);self.addCleanup(journal.close)
        flow=OrderFlow(max_seen=4)
        flow.import_asset(legacy_record(max_seen=4))
        journal.write(flow,[event(1,1,network="mainnet",size="3"),
                            event(2,2,network="mainnet",side="A",size="1"),
                            Operation("advance","mainnet","ETH",START+63000,contact_ms=START+63000)])
        self.assertEqual(journal.db.execute("PRAGMA user_version").fetchone()[0],1)
        for payload, in journal.db.execute("SELECT payload FROM journal"):
            self.assertEqual(json.loads(payload)["calculation_version"],"1.0")
        return journal,flow

    def test_legacy_sqlite_recovers_exactly_and_new_segment_has_separate_signed_cvd(self):
        journal,flow=self.prepare_legacy()
        expected=flow.export_asset("mainnet","ETH",include_seen=False)
        journal.close()
        with FlowStore(self.path) as reopened:
            recovered=reopened.recover()
            self.assertEqual(recovered.export_asset("mainnet","ETH",include_seen=False),expected)
            self.assertFalse(recovered.asset("mainnet","ETH").signed)
            self.assertIsNone(recovered.snapshot("mainnet","ETH")["cvd"]["base"])
            self.assertEqual(reopened.write(recovered,[event(1,1,network="mainnet",size="3")]),["duplicate"])
            reopened.write(recovered,[Operation("disconnect","mainnet","ETH",START+64000,reason="restart"),
                Operation("connect","mainnet","ETH",START+65000,source_epoch=2),
                event(3,66,network="mainnet",side="A",size="2",source_epoch=2),
                Operation("advance","mainnet","ETH",START+128000,contact_ms=START+128000)])
            view=recovered.snapshot("mainnet","ETH")
            self.assertEqual(view["calculation_version"],"2.0")
            self.assertEqual(view["side_semantics_version"],"1.0")
            self.assertEqual(view["cvd"]["base"],"-2")
            self.assertEqual(view["windows"]["1m"]["delta_base"],"-2")
            self.assertEqual(view["observed_total"]["volume_base"],"6")
            versions={json.loads(row[0])["calculation_version"] for row in reopened.db.execute("SELECT payload FROM journal")}
            self.assertEqual(versions,{"1.0","2.0"})
            self.assertEqual(reopened.db.execute("PRAGMA user_version").fetchone()[0],1)
            self.assertEqual(reopened.verify()["integrity"],"verified")
            self.assertEqual(recovered.export_asset("mainnet","ETH",include_seen=False),
                             reopened.recover().export_asset("mainnet","ETH",include_seen=False))

    def test_legacy_transition_crash_restores_unsigned_checkpoint_and_retry_is_atomic(self):
        journal,flow=self.prepare_legacy()
        before=flow.export_asset("mainnet","ETH")
        rows=journal.status()["journal_rows"]
        operations=[Operation("disconnect","mainnet","ETH",START+64000,reason="restart"),
                    Operation("connect","mainnet","ETH",START+65000)]
        for point in ("after_journal","after_checkpoint"):
            def crash(stage):
                if stage==point:raise RuntimeError("synthetic crash")
            with self.assertRaises(FlowStoreError):journal.write(flow,operations,fault=crash)
            self.assertEqual(flow.export_asset("mainnet","ETH"),before)
            self.assertEqual(journal.status()["journal_rows"],rows)
            self.assertEqual(journal.recover().asset("mainnet","ETH").calculation_version,"1.0")
        journal.write(flow,operations)
        self.assertEqual(journal.recover().asset("mainnet","ETH").calculation_version,"2.0")
        self.assertEqual(journal.verify()["integrity"],"verified")

    def test_checkpoint_version_and_semantics_validation_even_with_recomputed_checksum(self):
        from src.flow_store import checked,digest,encode
        journal,flow=self.prepare()
        raw=journal.db.execute("SELECT payload,checksum FROM checkpoints").fetchone()
        for field,value in (("calculation_version","99.0"),("side_semantics_version","unknown"),("signed",False)):
            document=checked(*raw);document["state"][field]=value
            journal.db.execute("UPDATE checkpoints SET payload=?,checksum=?",(encode(document),digest(document)))
            with self.assertRaises(FlowIntegrityError):journal.verify()
            with self.assertRaises(FlowIntegrityError):journal.recover()
        journal.db.execute("UPDATE checkpoints SET payload=?,checksum=?",raw)
        self.assertEqual(journal.verify()["integrity"],"verified")

    def test_legacy_journal_cannot_claim_new_side_semantics(self):
        from src.flow_store import checked,digest,encode
        journal,flow=self.prepare()
        seq,payload,checksum=journal.db.execute("SELECT seq,payload,checksum FROM journal LIMIT 1").fetchone()
        document=checked(payload,checksum)
        document["calculation_version"]="1.0"  # Retaining v2 side metadata is invalid.
        journal.db.execute("UPDATE journal SET payload=?,checksum=? WHERE seq=?",
                           (encode(document),digest(document),seq))
        with self.assertRaisesRegex(FlowIntegrityError,"unsupported legacy journal side semantics"):
            journal.verify()
