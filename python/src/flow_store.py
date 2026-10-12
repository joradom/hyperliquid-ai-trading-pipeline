"""Local normalized SQLite journal + atomic, checksummed per-asset checkpoints."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Callable

from .integrity import canonical_checksum_payload
from .order_flow import CALCULATION_VERSION, SUPPORTED_CALCULATIONS, Operation, OrderFlow
from .stream_types import SIDE_SEMANTICS_VERSION, restore_trade

DB_VERSION = 1


class FlowStoreError(RuntimeError):
    pass


class FlowIntegrityError(FlowStoreError):
    pass


def digest(document):
    return hashlib.sha256(canonical_checksum_payload(document)).hexdigest()


def encode(document):
    return json.dumps(document,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False)


def checked(payload, checksum):
    def unique(pairs):
        result = {}
        for key,value in pairs:
            if key in result: raise FlowIntegrityError("duplicate stored JSON key")
            result[key] = value
        return result
    try:
        result = json.loads(payload,object_pairs_hook=unique)
        if digest(result) != checksum: raise FlowIntegrityError("stored checksum mismatch")
        return result
    except (ValueError,TypeError,OverflowError) as exc:
        raise FlowIntegrityError("invalid stored document") from None


class FlowStore:
    """Single local writer; no daemon, wallet, HTTP or raw messages.

    SQLite commit is the authority. On error the engine is restored to the
    transaction's in-memory backup. A kill before commit rolls both writes back.
    """

    def __init__(self, path: Path | str, *, max_journal_rows=None,
                 retention_ms=None, max_db_bytes=None):
        for value in (max_journal_rows,retention_ms,max_db_bytes):
            if value is not None and (type(value) is not int or value <= 0): raise ValueError("invalid persistence limit")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db = sqlite3.connect(str(self.path),isolation_level=None,timeout=5)
        try:
            tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {"journal","checkpoints","meta","sqlite_sequence"}:
                raise FlowStoreError("refusing unrelated database")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0,DB_VERSION): raise FlowStoreError("unsupported database schema")
            previous={}
            if "meta" in tables:
                meta=dict(self.db.execute("SELECT key,value FROM meta"))
                if "manifest" in meta: previous=checked(meta["manifest"],meta["manifest_checksum"])["storage_config"]
            self.max_rows=max_journal_rows if max_journal_rows is not None else previous.get("max_journal_rows",100_000)
            self.retention_ms=retention_ms if retention_ms is not None else previous.get("retention_ms",86_400_000)
            self.max_db_bytes=max_db_bytes if max_db_bytes is not None else previous.get("max_db_bytes",64*1024*1024)
            if self.max_db_bytes<65536: raise ValueError("database limit must be at least 64 KiB")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA wal_autocheckpoint=64")
            page = self.db.execute("PRAGMA page_size").fetchone()[0]
            if self.db.execute("PRAGMA page_count").fetchone()[0]*page>self.max_db_bytes:
                raise FlowStoreError("existing database exceeds configured size limit")
            self.db.execute(f"PRAGMA max_page_count={self.max_db_bytes//page}")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS journal(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    network TEXT NOT NULL, coin TEXT NOT NULL, kind TEXT NOT NULL,
                    at_ms INTEGER NOT NULL, trade_ms INTEGER, tid INTEGER,
                    payload TEXT NOT NULL, checksum TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS trade_identity
                    ON journal(network,coin,trade_ms,tid) WHERE kind='trade';
                CREATE INDEX IF NOT EXISTS journal_asset ON journal(network,coin,seq);
                CREATE TABLE IF NOT EXISTS checkpoints(
                    network TEXT NOT NULL, coin TEXT NOT NULL, last_seq INTEGER NOT NULL,
                    payload TEXT NOT NULL, checksum TEXT NOT NULL,
                    PRIMARY KEY(network,coin));
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)
            self.db.execute(f"PRAGMA user_version={DB_VERSION}")
        except Exception:
            self.db.close(); raise
        self.closed = False

    def __enter__(self): return self
    def __exit__(self, *args): self.close()

    def close(self):
        if not self.closed:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.close(); self.closed = True

    def _journal(self, op, kind, result, asset):
        document = {"db_version":DB_VERSION,"calculation_version":asset.calculation_version,
                    "operation":op.record(),"outcome":result}
        if asset.calculation_version == CALCULATION_VERSION:
            document["side_semantics_version"] = asset.side_semantics_version
        row = self.db.execute("INSERT INTO journal(network,coin,kind,at_ms,trade_ms,tid,payload,checksum) VALUES(?,?,?,?,?,?,?,?)",
                   (op.network,op.coin,kind,op.at_ms,op.trade.time_ms if op.trade else None,
                    op.trade.tid if op.trade else None,encode(document),digest(document)))
        return row.lastrowid

    def _prune(self, engine, now_ms):
        excess = max(0,self.db.execute("SELECT count(*) FROM journal").fetchone()[0]-self.max_rows)
        cutoff = self.db.execute("SELECT seq FROM journal ORDER BY seq LIMIT 1 OFFSET ?",(excess-1,)).fetchone()[0] if excess else -1
        age_cutoff = self.db.execute("SELECT max(seq) FROM journal WHERE at_ms<?",(now_ms-self.retention_ms,)).fetchone()[0]
        cutoff = max(cutoff,age_cutoff or -1)
        removed = self.db.execute("SELECT network,coin,max(trade_ms) FROM journal WHERE kind='trade' AND seq<=? GROUP BY network,coin",(cutoff,)).fetchall()
        for network,coin,floor in removed:
            asset = engine.asset(network,coin)
            asset.retention_floor_ms = max(asset.retention_floor_ms,floor)
        self.db.execute("DELETE FROM journal WHERE seq<=?",(cutoff,))
        old = self.db.execute("SELECT value FROM meta WHERE key='retired_through_seq'").fetchone()
        retired = max(int(old[0]) if old else 0,cutoff)
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('retired_through_seq',?)",(str(retired),))

    def write(self, engine: OrderFlow, operations, *, fault: Callable[[str],None] | None = None):
        operations = list(operations)
        if not operations: return []
        if len(operations)>1024 or not all(isinstance(op,Operation) for op in operations):
            raise ValueError("transaction requires at most 1024 normalized operations")
        # Pruning can update retention floors of other assets, so back up all.
        backup = {key:engine.export_asset(*key) for key in engine.assets}
        old_durable = engine.durable_dedup
        engine.durable_dedup = True
        results=[]
        try:
            self.db.execute("BEGIN IMMEDIATE")
            for key in set(backup)|{(op.network,op.coin) for op in operations}:
                checkpoint=self.db.execute("SELECT payload,checksum FROM checkpoints WHERE network=? AND coin=?",key).fetchone()
                if checkpoint:
                    previous=checked(*checkpoint)["state"]
                    supplied=dict(backup[key]) if key in backup else None
                    if supplied is not None:supplied.pop("seen",None)
                    if supplied!=previous:raise FlowStoreError("recover authoritative engine before writing")
            touched={}
            for op in operations:
                result=None
                if op.kind == "trade":
                    original = self.db.execute("SELECT payload,checksum FROM journal WHERE kind='trade' AND network=? AND coin=? AND trade_ms=? AND tid=?",
                                              (op.network,op.coin,op.trade.time_ms,op.trade.tid)).fetchone()
                    if original:
                        prior = Operation.restore(checked(*original)["operation"]).trade
                        if prior == op.trade:
                            engine.note_duplicate(op.network,op.coin); result="duplicate"
                        else:
                            engine.note_conflict(op.network,op.coin,op.at_ms); result="conflict"
                if result is None: result = engine.apply(op)
                kind = ("trade" if result in ("accepted","bootstrap","prior_epoch","late","overflow") else result) if op.kind=="trade" else op.kind
                seq = self._journal(op,kind,result,engine.asset(op.network,op.coin))
                touched[(op.network,op.coin)]=seq; results.append(result)
            if fault: fault("after_journal")
            anchors={key:self.db.execute("SELECT checksum FROM journal WHERE seq=?",(seq,)).fetchone()[0]
                     for key,seq in touched.items()}
            self._prune(engine,max(op.at_ms for op in operations))
            for key in engine.assets:
                seq = touched.get(key)
                if seq is None:
                    row = self.db.execute("SELECT last_seq,payload,checksum FROM checkpoints WHERE network=? AND coin=?",key).fetchone()
                    if row is None: continue
                    seq = row[0]
                    anchors[key]=checked(row[1],row[2])["last_journal_checksum"]
                document={"db_version":DB_VERSION,"last_seq":seq,"last_journal_checksum":anchors[key],
                          "state":engine.export_asset(*key,include_seen=False)}
                self.db.execute("INSERT OR REPLACE INTO checkpoints(network,coin,last_seq,payload,checksum) VALUES(?,?,?,?,?)",
                                (*key,seq,encode(document),digest(document)))
            if fault: fault("after_checkpoint")
            tail=self.db.execute("SELECT seq,checksum FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
            retired=int(self.db.execute("SELECT value FROM meta WHERE key='retired_through_seq'").fetchone()[0])
            manifest={"db_version":DB_VERSION,"retired_through_seq":retired,
                      "tail_seq":tail[0] if tail else max(touched.values()),"tail_checksum":tail[1] if tail else None,
                      "storage_config":{"max_journal_rows":self.max_rows,"retention_ms":self.retention_ms,"max_db_bytes":self.max_db_bytes}}
            self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('manifest',?)",(encode(manifest),))
            self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('manifest_checksum',?)",(digest(manifest),))
            self.db.execute("COMMIT")
        except Exception as exc:
            if self.db.in_transaction: self.db.execute("ROLLBACK")
            engine.assets.clear()
            for state in backup.values(): engine.import_asset(state)
            engine.durable_dedup = old_durable
            if isinstance(exc,(FlowIntegrityError,ValueError)): raise
            raise FlowStoreError("flow transaction rolled back") from None
        return results

    def _verify(self):
        if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok": raise FlowIntegrityError("SQLite integrity failed")
        meta=dict(self.db.execute("SELECT key,value FROM meta"))
        manifest=checked(meta["manifest"],meta["manifest_checksum"]) if "manifest" in meta else {"retired_through_seq":0,"tail_seq":0,"tail_checksum":None}
        expected=manifest["retired_through_seq"]+1
        if "manifest" in meta and int(meta["retired_through_seq"])!=manifest["retired_through_seq"]:
            raise FlowIntegrityError("retention manifest mismatch")
        rows = 0
        for seq,network,coin,kind,at_ms,trade_ms,tid,payload,checksum in self.db.execute("SELECT * FROM journal ORDER BY seq"):
            document = checked(payload,checksum)
            if seq!=expected: raise FlowIntegrityError("journal sequence missing")
            expected+=1
            if document["db_version"]!=DB_VERSION or document["calculation_version"] not in SUPPORTED_CALCULATIONS:
                raise FlowIntegrityError("unsupported journal version")
            if document["calculation_version"] == CALCULATION_VERSION and document.get("side_semantics_version") != SIDE_SEMANTICS_VERSION:
                raise FlowIntegrityError("unsupported journal side semantics")
            if document["calculation_version"] != CALCULATION_VERSION and document.get("side_semantics_version") is not None:
                raise FlowIntegrityError("unsupported legacy journal side semantics")
            op = Operation.restore(document["operation"])
            if (network,coin,at_ms,trade_ms,tid)!=(op.network,op.coin,op.at_ms,op.trade.time_ms if op.trade else None,op.trade.tid if op.trade else None):
                raise FlowIntegrityError("journal index/document mismatch")
            expected_kind = ("trade" if document["outcome"] in ("accepted","bootstrap","prior_epoch","late","overflow") else document["outcome"]) if op.kind=="trade" else op.kind
            if kind != expected_kind: raise FlowIntegrityError("journal kind mismatch")
            rows += 1
        if expected!=manifest["tail_seq"]+1: raise FlowIntegrityError("journal tail missing")
        tail=self.db.execute("SELECT checksum FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
        if (tail[0] if tail else None)!=manifest["tail_checksum"]: raise FlowIntegrityError("journal tail checksum mismatch")
        checkpoints = 0
        for network,coin,last_seq,payload,checksum in self.db.execute("SELECT * FROM checkpoints"):
            document = checked(payload,checksum)
            state = document["state"]
            if document["db_version"]!=DB_VERSION or document["last_seq"]!=last_seq or (state["network"],state["coin"])!=(network,coin):
                raise FlowIntegrityError("checkpoint metadata mismatch")
            # Validate calculation/semantics and aggregate signatures even when
            # callers request verify() without subsequently recovering.
            OrderFlow(**state["config"]).import_asset(state)
            if self.db.execute("SELECT 1 FROM journal WHERE network=? AND coin=? AND seq>? LIMIT 1",(network,coin,last_seq)).fetchone():
                raise FlowIntegrityError("journal beyond atomic checkpoint")
            last = self.db.execute("SELECT checksum FROM journal WHERE network=? AND coin=? AND seq=?",(network,coin,last_seq)).fetchone()
            if last is None and last_seq>manifest["retired_through_seq"]:
                raise FlowIntegrityError("checkpoint cursor missing")
            if last and last[0]!=document["last_journal_checksum"]: raise FlowIntegrityError("checkpoint anchor mismatch")
            checkpoints += 1
        return {"integrity":"verified","journal_rows":rows,"checkpoints":checkpoints}

    def verify(self):
        try: return self._verify()
        except FlowIntegrityError: raise
        except (KeyError,ValueError,TypeError,ArithmeticError,sqlite3.Error):
            raise FlowIntegrityError("invalid journal/checkpoint integrity") from None

    def recover(self) -> OrderFlow:
        self.verify()
        records=[checked(row[0],row[1]) for row in self.db.execute("SELECT payload,checksum FROM checkpoints ORDER BY network,coin")]
        engine=OrderFlow(**records[0]["state"]["config"]) if records else OrderFlow()
        engine.durable_dedup=True
        try:
            for record in records:
                engine.import_asset(record["state"])
                asset=engine.asset(record["state"]["network"],record["state"]["coin"])
                # Rebuild the bounded recent-ID cache from retained, verified unique rows.
                rows=self.db.execute("SELECT payload,checksum FROM journal WHERE network=? AND coin=? AND kind='trade' ORDER BY seq DESC LIMIT ?",
                                     (asset.network,asset.coin,engine.config["max_seen"])).fetchall()
                for payload,checksum in reversed(rows):
                    trade=Operation.restore(checked(payload,checksum)["operation"]).trade
                    asset.seen[(trade.time_ms,trade.tid)]=trade
        except (ValueError,TypeError,KeyError,ArithmeticError):
            raise FlowIntegrityError("invalid flow checkpoint state") from None
        return engine

    def status(self):
        rows=self.db.execute("SELECT count(*) FROM journal").fetchone()[0]
        checkpoints=self.db.execute("SELECT count(*) FROM checkpoints").fetchone()[0]
        pages=self.db.execute("PRAGMA page_count").fetchone()[0]
        page_size=self.db.execute("PRAGMA page_size").fetchone()[0]
        return {"schema_version":DB_VERSION,"calculation_version":CALCULATION_VERSION,"journal_rows":rows,
                "checkpoints":checkpoints,"database_bytes":pages*page_size,
                "journal_row_limit":self.max_rows,"retention_ms":self.retention_ms,"database_byte_limit":self.max_db_bytes,
                "wal_bytes":Path(str(self.path)+"-wal").stat().st_size if Path(str(self.path)+"-wal").exists() else 0}
