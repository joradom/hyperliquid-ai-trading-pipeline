"""Immutable flow/L2 views and foreground public-WS adapter; no DOM/scanner/IPC."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import tracemalloc
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

from .flow_store import FlowStore, FlowStoreError
from .order_flow import Operation, OrderFlow, decimal_text
from .orderbook import l2_view
from .stream_types import SIDE_SEMANTICS, Trade
from .ws_client import PublicWSClient


def freeze(value):
    if isinstance(value,dict): return MappingProxyType({key:freeze(item) for key,item in value.items()})
    if isinstance(value,(list,tuple)): return tuple(freeze(item) for item in value)
    return value


def thaw(value):
    if isinstance(value,Mapping): return {key:thaw(item) for key,item in value.items()}
    if isinstance(value,tuple): return [thaw(item) for item in value]
    return value


def process_memory():
    # Linux VmHWM belongs to the current executable memory image. ru_maxrss can
    # include the launcher's pre-exec high-water mark in this environment.
    result={"rss_current_kib":None,"rss_peak_kib":None}
    try:
        fields={row.split(":",1)[0]:row.split(":",1)[1].strip() for row in Path("/proc/self/status").read_text().splitlines() if ":" in row}
        result["rss_current_kib"]=int(fields["VmRSS"].split()[0])
        result["rss_peak_kib"]=int(fields["VmHWM"].split()[0])
    except (OSError,KeyError,ValueError):pass
    return result


@dataclass(frozen=True)
class LiveSnapshot:
    data: Mapping

    def __getitem__(self,key): return self.data[key]
    def to_dict(self): return thaw(self.data)


class LiveStore:
    """Owned by one event loop. Reads return detached, deeply immutable values."""
    def __init__(self,journal: FlowStore,engine: OrderFlow | None = None):
        self.journal=journal
        self.engine=engine if engine is not None else journal.recover()
        self.books={}
        self._book_receipts={}
        self._book_epochs={}
        self._book_conflicts={}
        self._connection_epoch=None
        self._source={}
        self._recovery_pending=set(self.engine.assets)
        self.transport_state="recovered_not_live" if self.engine.assets else "not_connected"

    def write(self,operations,**kwargs): return self.journal.write(self.engine,operations,**kwargs)

    def sync_ws(self,client: PublicWSClient,*,now_ms:int):
        """Sample epoch/state metadata; consume only Phase-1 normalized events.

        Coverage begins at this observer's confirmation, conservatively later
        than the socket ACK. Epoch changes never bridge an unobserved interval.
        """
        # SQLite is the identity authority, including after WS cache expiry.
        # Real late/conflicting trades still cut segments inside OrderFlow.
        client.durable_dedup = not self.journal.closed
        network = client.network
        operations=[]
        next_source={}
        active=(client.state=="live" and client.last_message_ms is not None
                and -5000<=now_ms-client.last_message_ms<=self.engine.config["freshness_ms"])
        for coin in client.symbols:
            key=(network,coin)
            asset=self.engine.asset(*key)
            current=(client.connection_epoch,client.continuity_epoch[coin])
            prior=self._source.get(key)
            if key in self._recovery_pending and asset.connected:
                operations.append(Operation("disconnect",*key,now_ms,reason="restart"))
            changed=prior is not None and prior!=current
            if asset.connected and (not active or changed):
                operations.append(Operation("disconnect",*key,now_ms,
                                  reason="ws_continuity_change" if changed else "capture_end" if client.state=="closed" else "disconnect"))
            if active and (not asset.connected or changed or key in self._recovery_pending):
                operations.append(Operation("connect",*key,now_ms,source_epoch=current[0],source_continuity=current[1]))
            next_source[key]=current
        # A transaction stays bounded. Leave remaining data in the existing queue.
        consumed=[]
        for _ in range(512):
            event=client.pop_trade()
            if event is None: break
            consumed.append(event)
            operations.append(Operation.from_received(event, network=network))
        for coin in client.symbols:
            asset=self.engine.asset(network,coin)
            if active and now_ms//1000>asset.evaluated_at_ms//1000:
                operations.append(Operation("advance",network,coin,now_ms,contact_ms=client.last_message_ms))
        if operations:
            try:self.write(operations)
            except Exception:
                # No await occurred: single owner can restore exactly this prefix.
                for event in reversed(consumed):
                    client.trade_queue.appendleft(event)
                    client.queue_size_bytes+=event.trade.buffer_bytes
                client.counts["trades_consumed"]-=len(consumed)
                raise
        self._source=next_source
        self._recovery_pending.difference_update(next_source)
        self.books={ (network,coin):book for coin,book in client.books.items() }
        self._book_receipts={ (network,coin):stamp for coin,stamp in client.last_book_received_ms.items() }
        self._book_epochs={ (network,coin):epoch for coin,epoch in client.book_epochs.items() }
        self._book_conflicts={ (network,coin):stamp for coin,stamp in client.book_conflicts.items() }
        self._connection_epoch=client.connection_epoch
        self.transport_state=client.state

    def read(self,network,coin,*,as_of_ms=None):
        if (network,coin) not in self.engine.assets:raise KeyError("unknown flow asset")
        data=self.engine.snapshot(network,coin,as_of_ms=as_of_ms)
        data["recovered_not_live"]=(network,coin) in self._recovery_pending
        data["transport_state"]="recovered_not_live" if data["recovered_not_live"] else self.transport_state
        if data["recovered_not_live"]:
            data["quality"]["status"]="degraded"
            data["quality"]["reasons"].append("recovered_not_live")
            data["connection"]["connected"]=False
            data["connection"]["fresh"]=False
            data["cvd"]["available"]=False
            data["cvd"]["base"]=data["cvd"]["quote"]=None
            for window in data["windows"].values():
                window["quality_reasons"].append("recovered_not_live")
                window["ready"]=window["signed_available"]=False
                for field in ("trades","trades_per_second","volume_base","volume_quote","buy_volume_base","sell_volume_base",
                              "buy_volume_quote","sell_volume_quote","delta_base","delta_quote","cvd_window_base","cvd_window_quote",
                              "cvd_slope_base_per_second","cvd_slope_quote_per_second","buy_sell_ratio_base"):
                    window[field]=None
        book=self.books.get((network,coin))
        data["l2_book"]={"time_ms":book.time_ms,
                         "bids":[{"price":decimal_text(level.price),"size":decimal_text(level.size),"n":level.order_count} for level in book.bids],
                         "asks":[{"price":decimal_text(level.price),"size":decimal_text(level.size),"n":level.order_count} for level in book.asks]} if book else None
        return LiveSnapshot(freeze(data))

    def read_l2(self,network,coin,*,as_of_ms=None):
        """Additional immutable metrics view; read() keeps its Phase-2 contract.

        Defaults to the current local clock, independently of flow evaluation.
        Explicit as_of_ms makes ages reproducible. Books remain RAM-only.
        """
        key=(network,coin)
        if key not in self.engine.assets:raise KeyError("unknown flow asset")
        as_of=time.time_ns()//1_000_000 if as_of_ms is None else as_of_ms
        data=l2_view(self.books.get(key),as_of_ms=as_of,received_at_ms=self._book_receipts.get(key),
                     network=network,symbol=coin,book_epoch=self._book_epochs.get(key),
                     current_epoch=self._connection_epoch,n_sig_figs=5,
                     conflicting_timestamp_ms=self._book_conflicts.get(key))
        return LiveSnapshot(freeze(data))

    def read_all(self):
        return {network+":"+coin:self.read(network,coin).to_dict() for network,coin in sorted(self.engine.assets)}


def recovery_check(journal,engine):
    verification=journal.verify()
    restored=journal.recover()
    matches=(set(engine.assets)==set(restored.assets) and all(
        engine.export_asset(*key,include_seen=False)==restored.export_asset(*key,include_seen=False) for key in engine.assets))
    return {**verification,"state_matches":matches,"recovered_assets":len(restored.assets)}


async def capture(args):
    tracemalloc.start()
    started=time.perf_counter()
    with FlowStore(args.db,max_journal_rows=args.max_journal_rows,
                   retention_ms=args.retention_seconds*1000 if args.retention_seconds is not None else None,
                   max_db_bytes=args.max_db_mib*1024*1024 if args.max_db_mib is not None else None) as journal:
        live=LiveStore(journal)
        client=PublicWSClient(args.symbols,network=getattr(args,"network","mainnet"))
        task=asyncio.create_task(client.run(args.seconds))
        try:
            while not task.done():
                live.sync_ws(client,now_ms=time.time_ns()//1_000_000)
                await asyncio.sleep(0.1)
            await task
        finally:
            client.stop()
            if not task.done(): task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            while client.trade_queue:
                live.sync_ws(client,now_ms=time.time_ns()//1_000_000)
            live.sync_ws(client,now_ms=time.time_ns()//1_000_000)
        views=live.read_all()
        # Summary only: full L2 levels remain available through LiveStore.read.
        for view in views.values():
            if view["l2_book"]:
                view["l2_book"]={"time_ms":view["l2_book"]["time_ms"],"levels_per_side":[len(view["l2_book"]["bids"]),len(view["l2_book"]["asks"])]}
        report={"kind":"order_flow_capture","read_only":True,"execution_authorized":False,
                "side_semantics":SIDE_SEMANTICS,"transport":client.summary(),"assets":views,
                "storage":journal.status(),"recovery":recovery_check(journal,live.engine)}
        report["status"]=("ok" if client.counts["connections"] and not client.errors
                          and client.confirmed==client.desired and report["recovery"]["state_matches"] else "degraded")
    _,peak=tracemalloc.get_traced_memory(); tracemalloc.stop()
    report["performance"]={"elapsed_seconds":round(time.perf_counter()-started,3),"peak_traced_bytes":peak}
    report["performance"].update(process_memory())
    try:
        import resource
        report["performance"]["rusage_maxrss_kib"]=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except ImportError:
        report["performance"]["rusage_maxrss_kib"]=None
    report["storage"]["wal_bytes_after_close"]=Path(str(args.db)+"-wal").stat().st_size if Path(str(args.db)+"-wal").exists() else 0
    return report


def replay(count,path):
    """Accelerated synthetic stream, 20 matches/s; no sleeps or network."""
    if type(count) is not int or not 1<=count<=1_000_000: raise ValueError("invalid replay size")
    if Path(path).exists() and Path(path).stat().st_size:raise ValueError("synthetic replay requires a fresh database")
    tracemalloc.start(); started=time.perf_counter()
    start=2_000_000_000_000
    engine=OrderFlow(max_seen=512)
    with FlowStore(path,max_journal_rows=5000) as journal:
        journal.write(engine,[Operation("connect","synthetic",coin,start) for coin in ("ETH","HYPE")])
        batch=[]
        for index in range(count):
            coin="ETH" if index%2==0 else "HYPE"
            stamp=start+(index//20)*1000+(index%20)*40
            trade=Trade(coin,"B" if index%4<2 else "A",Decimal(10 if coin=="ETH" else 20),Decimal(1),stamp,index)
            batch.append(Operation("trade","synthetic",coin,stamp+2000,trade))
            if len(batch)>=256:
                batch += [Operation("advance","synthetic",name,stamp+2000,contact_ms=stamp+2000) for name in ("ETH","HYPE")]
                journal.write(engine,batch); batch=[]
        batch += [Operation("advance","synthetic",name,stamp+3000,contact_ms=stamp+3000) for name in ("ETH","HYPE")]
        journal.write(engine,batch)
        metrics={coin:engine.snapshot("synthetic",coin) for coin in ("ETH","HYPE")}
        check=recovery_check(journal,engine)
        storage=journal.status()
    _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
    elapsed=time.perf_counter()-started
    return {"kind":"synthetic_flow_replay","read_only":True,"execution_authorized":False,
            "input_trades":count,"elapsed_seconds":round(elapsed,3),"trades_per_second":round(count/elapsed,1),
            "peak_traced_bytes":peak,"storage":storage,"recovery":check,"assets":metrics}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols",nargs="*",default=["ETH","HYPE"])
    parser.add_argument("--seconds",type=float,default=60)
    parser.add_argument("--db",type=Path)
    parser.add_argument("--network",choices=("mainnet","testnet"),default="mainnet")
    parser.add_argument("--max-journal-rows",type=int,help="override stored limit (new DB default 100000)")
    parser.add_argument("--retention-seconds",type=int,help="override stored retention (new DB default 86400)")
    parser.add_argument("--max-db-mib",type=int,help="override stored page limit (new DB default 64)")
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument("--recover-only",action="store_true")
    modes.add_argument("--replay-trades",type=int)
    parser.add_argument("--json",action="store_true")
    args=parser.parse_args(argv)
    if args.db is None:
        args.db=Path(__file__).resolve().parents[2]/"data"/"order_flow"/(args.network+".sqlite3")
    try:
        if args.replay_trades:
            if args.db.name in ("mainnet.sqlite3","testnet.sqlite3"): parser.error("choose a separate --db for synthetic replay")
            report=replay(args.replay_trades,args.db)
        elif args.recover_only:
            if not args.db.exists(): parser.error("recovery database does not exist")
            with FlowStore(args.db) as journal:
                live=LiveStore(journal)
                report={"kind":"flow_recovery","read_only":True,"execution_authorized":False,
                        "storage":journal.status(),"verification":journal.verify(),"assets":live.read_all()}
        else:
            args.symbols=args.symbols or ["ETH","HYPE"]
            report=asyncio.run(capture(args))
    except (ValueError,FlowStoreError):
        print("order flow operation failed validation/persistence; no financial action")
        return 1
    except KeyboardInterrupt:
        return 130
    print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False) if args.json else json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False))
    return 0 if report.get("recovery",{}).get("state_matches",True) and report.get("status","ok")=="ok" else 1


if __name__=="__main__": raise SystemExit(main())
