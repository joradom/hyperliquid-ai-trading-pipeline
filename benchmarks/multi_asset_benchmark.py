"""Public R102 multi-asset experiment; production modules/config are unchanged.

Run select once, then capture 5/10/20 sequentially in separate processes.
Only public metaAndAssetCtxs, trades and l2Book are used. No raw WS packets
are retained. Instance wrappers observe original calls without changing them.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import resource
import sqlite3
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from src.flow_store import FlowStore
from src.live_store import LiveStore, process_memory
from src.ws_client import PublicWSClient

PRODUCTION = tuple(sorted((ROOT / "python/src").glob("*.py")))
STOP_LIMITS = {"queue_fraction": 0.8, "pending_fraction": 0.8,
               "database_fraction": 0.8, "journal_fraction": 0.8,
               "rss_kib": 512 * 1024, "slow_sync_seconds": 2.0, "slow_sync_consecutive": 3}


def utc():
    return datetime.now(timezone.utc).isoformat()


def ms():
    return time.time_ns() // 1_000_000


def hashes():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in PRODUCTION}


def environment():
    import websockets
    cpu = next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                if line.startswith("model name")), None)
    return {"python": sys.version, "executable": sys.executable, "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version, "websockets": websockets.__version__, "cpu": cpu,
            "logical_cpus": os.cpu_count(), "loadavg": Path("/proc/loadavg").read_text().strip(),
            "memory": process_memory(), "production_sha256": hashes()}


def save_new(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


class Durations:
    def __init__(self):
        self.count = 0
        self.total = 0.0
        self.maximum = None
        self.minimum = None
        self.values = []

    def add(self, value):
        self.count += 1
        self.total += value
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        if len(self.values) < 100_000:
            self.values.append(value)

    def summary(self):
        values = sorted(self.values)
        def percentile(fraction):
            return values[max(0, math.ceil(len(values) * fraction)-1)] if values else None
        return {"count": self.count, "sum": self.total, "min": self.minimum, "max": self.maximum,
                "p50": percentile(0.5), "p95": percentile(0.95),
                "percentile_samples": len(values), "percentile_sample_truncated": self.count > len(values)}


def select():
    requested = utc()
    req = urllib.request.Request("https://api.hyperliquid.xyz/info",
        data=b'{"type":"metaAndAssetCtxs"}', headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as response:
        meta, contexts = json.load(response)
    received = utc()
    if len(meta["universe"]) != len(contexts):
        raise ValueError("public universe/context length mismatch")
    selected = []
    for asset, context in zip(meta["universe"], contexts):
        name = asset["name"]
        if asset.get("isDelisted") or not name.isascii() or not name.isalnum():
            continue
        volume, mark = Decimal(context["dayNtlVlm"]), Decimal(context["markPx"])
        if volume.is_finite() and volume > 0 and mark.is_finite() and mark > 0:
            selected.append({"symbol": name, "day_notional_volume": str(volume), "mark": str(mark)})
    selected.sort(key=lambda row: (-Decimal(row["day_notional_volume"]), row["symbol"]))
    if len(selected) < 20:
        raise ValueError("fewer than 20 valid public native perpetuals")
    parent = ROOT / "data/order_flow"
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="multi_asset_benchmark_", dir=parent))
    selection = {"criterion": "top 20 native non-delisted ASCII-alphanumeric perpetuals by public dayNtlVlm; positive finite mark/volume",
                 "request": {"endpoint": "https://api.hyperliquid.xyz/info", "type": "metaAndAssetCtxs",
                             "requested_at_utc": requested, "received_at_utc": received},
                 "assets": selected[:20], "environment": environment(), "stop_limits": STOP_LIMITS,
                 "artifact_directory": str(directory)}
    save_new(directory / "selection.json", selection)
    print(json.dumps({"directory": str(directory), "selection": str(directory / "selection.json"),
                      "assets": [a["symbol"] for a in selection["assets"]]}), flush=True)


async def capture(selection_path, size, seconds):
    selection = json.loads(Path(selection_path).read_text())
    directory = Path(selection["artifact_directory"])
    if not directory.resolve().is_relative_to((ROOT / "data/order_flow").resolve()):
        raise ValueError("experiment directory must be within ignored project data/order_flow")
    db_path = directory / f"scenario_{size:02}.sqlite3"
    out_path = directory / f"scenario_{size:02}.json"
    if any(p.exists() for p in (db_path, Path(str(db_path)+"-wal"), Path(str(db_path)+"-shm"), out_path)):
        raise ValueError("refusing to reuse any existing experiment artifact")
    symbols = [a["symbol"] for a in selection["assets"][:size]]
    started_wall, started_cpu, started_utc = time.perf_counter(), time.process_time(), utc()
    initial_environment = environment()
    timing = {name: Durations() for name in ("handle_message_seconds", "sync_ws_seconds", "journal_write_seconds",
                                           "publication_read_seconds", "loop_interval_seconds", "receipt_to_commit_ms")}
    per_asset = {coin: {"book_samples": 0, "max_exchange_age_ms_sampled": None,
                        "max_receipt_age_ms_sampled": None, "l2_quality_samples": Counter(),
                        "l2_quality_reasons": Counter(), "band_coverage_samples": {str(b):Counter() for b in ("0.001","0.0025","0.005","0.01")},
                        "max_pending": 0, "max_seen": 0, "max_buckets": 0, "journal_outcomes": Counter()} for coin in symbols}
    report = {"kind":"multi_asset_benchmark", "read_only":True, "execution_authorized":False,
              "size":size, "symbols":symbols, "requested_capture_seconds":seconds, "started_at_utc":started_utc,
              "database":str(db_path), "environment":initial_environment, "stop_limits":STOP_LIMITS,
              "errors":[], "stop_reasons":[], "operations_failed":0, "transactions_committed":0}
    journal = FlowStore(db_path)  # Defaults only: WAL/FULL/autocheckpoint 64/quota/retention unchanged.
    live = LiveStore(journal)
    client = PublicWSClient(symbols)
    report["configuration"] = {"sqlite_pragmas": {key:journal.db.execute("PRAGMA "+key).fetchone()[0]
        for key in ("journal_mode","synchronous","wal_autocheckpoint","page_size","max_page_count")},
        "storage":journal.status(), "flow":live.engine.config,
        "ws":{"queue_capacity":client.queue_capacity,"queue_bytes":client.queue_limit_bytes,
              "dedup_capacity":client.dedup_capacity,"dedup_ttl_seconds":client.dedup_ttl,
              "book_history_per_asset":128,"heartbeat_seconds":client.heartbeat_seconds,
              "pong_timeout":client.pong_timeout,"ack_timeout":client.ack_timeout,
              "open_timeout":client.open_timeout,"close_timeout":client.close_timeout,
              "desired_subscriptions":len(client.desired)},
        "sync_sleep_seconds":0.1,"publication_period_seconds":1.0}
    first_all_ack = None
    max_confirmed = max_queue_bytes = 0
    actual_close_seconds = 0.0
    queue_fractions = Durations()
    committed_trade_ops = 0
    live_rss_max = 0
    storage_peaks = {"database_bytes":0,"wal_bytes":0,"journal_rows":0,"checkpoint_payload_bytes":0}
    prior_tick = None
    slow_sync = 0
    failed = False
    task = None
    original_handle = client.handle_message
    original_close = client._close
    original_write = journal.write

    def handle(raw):
        nonlocal first_all_ack, max_confirmed, max_queue_bytes
        begin = time.perf_counter()
        try:
            return original_handle(raw)
        finally:
            timing["handle_message_seconds"].add(time.perf_counter()-begin)
            max_confirmed = max(max_confirmed,len(client.confirmed))
            max_queue_bytes = max(max_queue_bytes,client.queue_size_bytes)
            if client.confirmed == client.desired and first_all_ack is None:
                first_all_ack = time.perf_counter()

    async def close():
        nonlocal actual_close_seconds
        actual = client._transport is not None
        begin = time.perf_counter()
        try:
            return await original_close()
        finally:
            if actual:
                actual_close_seconds += time.perf_counter()-begin

    def write(engine, operations, **kwargs):
        nonlocal committed_trade_ops
        operations = list(operations)
        begin = time.perf_counter()
        try:
            results = original_write(engine,operations,**kwargs)
        except Exception:
            report["operations_failed"] += len(operations)
            raise
        else:
            report["transactions_committed"] += 1
            committed_at = ms()
            for operation,result in zip(operations,results):
                per_asset[operation.coin]["journal_outcomes"][operation.kind+":"+result] += 1
                if operation.kind == "trade":
                    committed_trade_ops += 1
                    timing["receipt_to_commit_ms"].add(committed_at-operation.at_ms)
            return results
        finally:
            timing["journal_write_seconds"].add(time.perf_counter()-begin)

    client.handle_message, client._close, journal.write = handle, close, write

    def storage_sample():
        status = journal.status()
        payload = journal.db.execute("SELECT coalesce(sum(length(CAST(payload AS BLOB))),0) FROM checkpoints").fetchone()[0]
        for key in ("database_bytes","wal_bytes","journal_rows"):
            storage_peaks[key] = max(storage_peaks[key],status[key])
        storage_peaks["checkpoint_payload_bytes"] = max(storage_peaks["checkpoint_payload_bytes"],payload)
        return status

    def sample_books():
        begin = time.perf_counter()
        now = ms()
        for coin in symbols:
            if ("mainnet",coin) not in live.engine.assets:
                continue
            row = per_asset[coin]
            result = live.read_l2("mainnet",coin,as_of_ms=now).to_dict()
            # Also exercise the unchanged immutable Order Flow/legacy Book view.
            live.read("mainnet",coin,as_of_ms=max(now,live.engine.asset("mainnet",coin).evaluated_at_ms))
            row["book_samples"] += 1
            row["l2_quality_samples"][result["quality"]["status"]] += 1
            row["l2_quality_reasons"].update(result["quality"]["reasons"])
            for source,key in (("exchange","max_exchange_age_ms_sampled"),("receipt","max_receipt_age_ms_sampled")):
                value = result["freshness"][source]["age_ms"]
                if value is not None:
                    row[key] = value if row[key] is None else max(row[key],value)
            for band,metadata in result["metrics"]["bands"].items():
                row["band_coverage_samples"][band][metadata["coverage"]] += 1
            row["last_l2_view"] = result
        timing["publication_read_seconds"].add(time.perf_counter()-begin)

    def safety(sync_seconds, status=None):
        nonlocal slow_sync, live_rss_max
        reasons=[]
        memory=process_memory()
        live_rss_max=max(live_rss_max,memory["rss_current_kib"] or 0)
        if live_rss_max>STOP_LIMITS["rss_kib"]:reasons.append("rss_limit")
        queue_fraction=max(len(client.trade_queue)/client.queue_capacity,client.queue_size_bytes/client.queue_limit_bytes)
        queue_fractions.add(queue_fraction)
        if queue_fraction>=STOP_LIMITS["queue_fraction"]:reasons.append("queue_saturation")
        if client.counts["queue_dropped"]:reasons.append("ws_queue_overflow")
        if client.counts["conflicts"] or client.book_conflicts:reasons.append("ws_identity_or_book_conflict")
        if client.counts["dedup_evictions"]:reasons.append("ws_dedup_eviction")
        if client.counts["reconnections"]:reasons.append("reconnection")
        for coin in symbols:
            if ("mainnet",coin) not in live.engine.assets:continue
            asset=live.engine.asset("mainnet",coin)
            row=per_asset[coin]
            for name,values in (("max_pending",asset.pending),("max_seen",asset.seen),("max_buckets",asset.buckets)):
                row[name]=max(row[name],len(values))
            if asset.counts["pending_overflow"]:reasons.append("flow_pending_overflow")
            if asset.counts["conflicts"]:reasons.append("flow_identity_conflict")
            if len(asset.pending)>=live.engine.config["max_pending"]*STOP_LIMITS["pending_fraction"]:
                reasons.append("flow_pending_saturation")
        slow_sync=slow_sync+1 if sync_seconds>STOP_LIMITS["slow_sync_seconds"] else 0
        if slow_sync>=STOP_LIMITS["slow_sync_consecutive"]:reasons.append("sustained_sync_saturation")
        if status:
            if status["database_bytes"]>=status["database_byte_limit"]*STOP_LIMITS["database_fraction"]:reasons.append("sqlite_quota_saturation")
            if status["journal_rows"]>=status["journal_row_limit"]*STOP_LIMITS["journal_fraction"]:reasons.append("journal_retention_saturation")
        return reasons

    async def monitor():
        nonlocal prior_tick
        next_publication=time.perf_counter()
        while not task.done():
            begin=time.perf_counter()
            if prior_tick is not None:timing["loop_interval_seconds"].add(begin-prior_tick)
            prior_tick=begin
            live.sync_ws(client,now_ms=ms())
            sync_seconds=time.perf_counter()-begin
            timing["sync_ws_seconds"].add(sync_seconds)
            status=None
            if time.perf_counter()>=next_publication:
                sample_books();status=storage_sample()
                next_publication=time.perf_counter()+1.0
            reasons=safety(sync_seconds,status)
            if reasons:
                report["stop_reasons"].extend(reasons)
                client.stop()
                break
            await asyncio.sleep(0.1)

    capture_begin=time.perf_counter()
    try:
        task=asyncio.create_task(client.run(seconds))
        await monitor()
        await task
    except Exception as exc:
        failed=True
        report["errors"].append({"stage":"capture","type":type(exc).__name__,"message":str(exc)[:180]})
        report["stop_reasons"].append("capture_or_transaction_failed")
    finally:
        client.stop()
        if task and not task.done():
            await task
    capture_end=time.perf_counter()
    report["capture_task_seconds"]=capture_end-capture_begin
    report["first_all_ack_elapsed_seconds"]=first_all_ack-capture_begin if first_all_ack else None
    report["confirmed_monitoring_seconds"]=capture_end-first_all_ack if first_all_ack else None
    report["ws_socket_close_seconds"]=actual_close_seconds
    report["memory_capture_end"]=process_memory()
    report["rss_current_max_sampled_during_capture_kib"]=live_rss_max
    report["capture_cpu_seconds"]=time.process_time()-started_cpu
    report["pending_before_terminal_drain"]={coin:len(live.engine.asset("mainnet",coin).pending) for coin in symbols
                                          if ("mainnet",coin) in live.engine.assets}
    drain_begin=time.perf_counter()
    if not failed:
        try:
            # Identical terminal-drain convention to live_store.capture.
            while client.trade_queue:
                live.sync_ws(client,now_ms=ms())
            live.sync_ws(client,now_ms=ms())
        except Exception as exc:
            failed=True
            report["errors"].append({"stage":"terminal_drain","type":type(exc).__name__,"message":str(exc)[:180]})
            report["stop_reasons"].append("terminal_drain_failed")
    report["terminal_drain_seconds"]=time.perf_counter()-drain_begin
    report["transport"]=client.summary()
    report["maximum_confirmed_subscriptions"]=max_confirmed
    report["queue_peak_payload_bytes"]=max_queue_bytes
    report["queue_fraction_samples"]=queue_fractions.summary()
    report["committed_trade_operations"]=committed_trade_ops
    report["storage_at_capture_end"]=storage_sample()
    report["storage_sampled_peaks"]=storage_peaks
    report["checkpoint_payload_bytes_at_end"]=journal.db.execute("SELECT coalesce(sum(length(CAST(payload AS BLOB))),0) FROM checkpoints").fetchone()[0]
    report["retired_through_seq"]=int(journal.db.execute("SELECT value FROM meta WHERE key='retired_through_seq'").fetchone()[0]) if report["transactions_committed"] else 0
    observed_total=0
    for coin in symbols:
        if ("mainnet",coin) in live.engine.assets:
            row=per_asset[coin]
            flow=live.read("mainnet",coin).to_dict()
            row["flow_snapshot_at_end"]=flow
            row["ws"]=report["transport"]["assets"][coin]
            row["journal_trade_rows"]=journal.db.execute("SELECT count(*) FROM journal WHERE coin=? AND kind='trade'",(coin,)).fetchone()[0]
            observed_total+=flow["observed_total"]["unique_trades"] or 0
    report["per_asset"]=per_asset
    report["validation"]={}
    verify_begin=time.perf_counter()
    try:
        report["validation"]["journal_and_checkpoints"]=journal.verify()
    except Exception as exc:
        report["errors"].append({"stage":"verify","type":type(exc).__name__,"message":str(exc)[:180]})
        report["stop_reasons"].append("integrity_failure")
    report["verify_seconds"]=time.perf_counter()-verify_begin
    recovery_begin=time.perf_counter()
    try:
        restored=journal.recover()
        matches=set(restored.assets)==set(live.engine.assets) and all(
            restored.export_asset(*key,include_seen=False)==live.engine.export_asset(*key,include_seen=False)
            for key in live.engine.assets)
        report["validation"]["recovery_state_matches"]=matches
        if not matches:report["stop_reasons"].append("recovery_mismatch")
        del restored
    except Exception as exc:
        report["errors"].append({"stage":"recover","type":type(exc).__name__,"message":str(exc)[:180]})
        report["stop_reasons"].append("recovery_failed")
    report["recover_seconds_including_verify"]=time.perf_counter()-recovery_begin
    close_begin=time.perf_counter()
    try:journal.close()
    except Exception as exc:
        report["errors"].append({"stage":"sqlite_close","type":type(exc).__name__,"message":str(exc)[:180]})
        report["stop_reasons"].append("sqlite_close_failed")
    report["sqlite_close_seconds"]=time.perf_counter()-close_begin
    reopen_begin=time.perf_counter()
    try:
        with FlowStore(db_path) as reopened:
            restored=reopened.recover()
            matches=set(restored.assets)==set(live.engine.assets) and all(
                restored.export_asset(*key,include_seen=False)==live.engine.export_asset(*key,include_seen=False)
                for key in live.engine.assets)
            report["validation"]["reopened_recovery_state_matches"]=matches
            if not matches:report["stop_reasons"].append("reopened_recovery_mismatch")
    except Exception as exc:
        report["errors"].append({"stage":"reopen_recover","type":type(exc).__name__,"message":str(exc)[:180]})
        report["stop_reasons"].append("reopened_recovery_failed")
    report["reopen_recover_and_close_seconds"]=time.perf_counter()-reopen_begin
    counters=report["transport"]["counts"]
    accounting={"ws_unique":counters["unique_trades"],"ws_consumed":counters["trades_consumed"],
                "ws_dropped":counters["queue_dropped"],"remaining_trade_queue":len(client.trade_queue),
                "committed_trade_operations":committed_trade_ops,"flow_observed_unique":observed_total,
                "journal_retained_trade_rows":sum(row.get("journal_trade_rows",0) for row in per_asset.values())}
    accounting["transport_accounted"]=accounting["ws_unique"]==accounting["ws_consumed"]+accounting["ws_dropped"]+accounting["remaining_trade_queue"]
    accounting["consumed_committed"]=accounting["ws_consumed"]==committed_trade_ops
    accounting["observed_matches_consumed"]=observed_total==committed_trade_ops
    accounting["journal_retains_consumed"]=accounting["journal_retained_trade_rows"]==committed_trade_ops
    report["validation"]["accounting"]=accounting
    if not all(accounting[key] for key in ("transport_accounted","consumed_committed","observed_matches_consumed","journal_retains_consumed")):
        report["stop_reasons"].append("accounting_mismatch")
    if counters["queue_dropped"]:report["stop_reasons"].append("ws_queue_overflow")
    if client.errors:report["stop_reasons"].append("ws_errors")
    if max_confirmed!=2*size:report["stop_reasons"].append("incomplete_subscriptions")
    report["throughput"]={"ws_unique_per_observation_second":counters["unique_trades"]/client.observation_seconds if client.observation_seconds else None,
                         "committed_trade_operations_per_capture_second":committed_trade_ops/report["capture_task_seconds"],
                         "committed_trades_per_journal_write_wall_second":committed_trade_ops/timing["journal_write_seconds"].total if timing["journal_write_seconds"].total else None}
    report["timing_observations"]={name:value.summary() for name,value in timing.items()}
    report["unavailable_latency_metrics"]={"exchange_to_socket_receipt":"NO DISPONIBLE: clocks/network not instrumented",
                                           "socket_to_handler":"NO DISPONIBLE: native websocket buffers not instrumented",
                                           "per_trade_commit_to_publication":"NO DISPONIBLE: no per-event publication marker"}
    report["files_after_close"]={name:Path(str(db_path)+suffix).stat().st_size if Path(str(db_path)+suffix).exists() else 0
                                 for name,suffix in (("sqlite_bytes",""),("wal_bytes","-wal"),("shm_bytes","-shm"))}
    report["production_hashes_unchanged"]=hashes()==initial_environment["production_sha256"]==selection["environment"]["production_sha256"]
    if not report["production_hashes_unchanged"]:report["stop_reasons"].append("production_changed")
    report["stop_reasons"]=sorted(set(report["stop_reasons"]))
    report["completed_at_utc"]=utc()
    report["total_seconds"]=time.perf_counter()-started_wall
    report["cpu_total_seconds"]=time.process_time()-started_cpu
    usage=resource.getrusage(resource.RUSAGE_SELF)
    report["cpu_user_seconds_process_lifetime"],report["cpu_system_seconds_process_lifetime"]=usage.ru_utime,usage.ru_stime
    report["memory_final"]=process_memory()
    report["status"]="stop_escalation" if report["stop_reasons"] else "ok"
    save_new(out_path,report)
    print(json.dumps({"size":size,"status":report["status"],"artifact":str(out_path),
                      "capture_seconds":report["capture_task_seconds"],"ws_observation_seconds":client.observation_seconds,
                      "trades":counters["unique_trades"],"messages":counters["messages"],
                      "cpu_seconds":report["cpu_total_seconds"],"rss_peak_kib":report["memory_final"]["rss_peak_kib"],
                      "stop_reasons":report["stop_reasons"]}),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="mode",required=True)
    sub.add_parser("select")
    cap=sub.add_parser("capture")
    cap.add_argument("--selection",type=Path,required=True)
    cap.add_argument("--size",type=int,choices=(5,10,20),required=True)
    cap.add_argument("--seconds",type=float,default=60)
    args=parser.parse_args()
    if args.mode=="select":select()
    else:asyncio.run(capture(args.selection,args.size,args.seconds))


if __name__=="__main__":main()
