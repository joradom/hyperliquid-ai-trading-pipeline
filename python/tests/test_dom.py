"""Offline immutable DOM fixtures, POSIX keyboard and lifecycle tests."""
import asyncio
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from rich.console import Console
except ImportError:
    Console = None

from src import dom
from src.dom import Controls, DOMFrame, DOMSession, RichRenderer, TerminalKeys, fresh_database, number, obi_percent, render_frame
from src.flow_store import FlowStore
from src.live_store import LiveStore, LiveSnapshot, freeze
from src.ws_client import PublicWSClient
from test_stream_types import NOW, ack, book, packet, trade
from test_ws_client import FakeSocket


class RecordingRenderer:
    def __init__(self):
        self.frames = []
        self.closed = False
    async def present(self, frame):
        self.frames.append(frame)
    async def close(self):
        self.closed = True


def wire_book(coin="ETH"):
    return {"coin":coin,"time":NOW+900,"levels":[
        [{"px":str(100-index/100),"sz":str(index+2),"n":1} for index in range(20)],
        [{"px":str(100.02+index/100),"sz":str(index+3),"n":1} for index in range(20)]]}


class DOMControlsTests(unittest.TestCase):
    def test_switch_wraps_and_level_limits(self):
        controls=Controls(("ETH","HYPE","SOL"))
        controls.key("\t");self.assertEqual(controls.selected,"HYPE")
        controls.key("\t");controls.key("\t");self.assertEqual(controls.selected,"ETH")
        for _ in range(40):controls.key("+")
        self.assertEqual(controls.levels,20)
        for _ in range(40):controls.key("-")
        self.assertEqual(controls.levels,1)

    def test_pause_resume_quit_and_unknown_key(self):
        controls=Controls(("ETH",))
        controls.key("x");self.assertEqual(controls.revision,0)
        controls.key("P");self.assertTrue(controls.paused)
        controls.key("p");self.assertFalse(controls.paused)
        controls.key("Q");self.assertTrue(controls.quit.is_set())

    def test_presentation_number_format_preserves_missing_and_zero(self):
        self.assertEqual(number(None),"—")
        self.assertEqual(number("0"),"0")
        self.assertEqual(number("100.000"),"100")
        self.assertEqual(number("1e-20"),"1e-20")
        self.assertEqual(number("NaN"),"—")
        self.assertEqual(number("12345",compact=True),"12.34k")
        self.assertEqual(number("1.234567"),"1.2346")
        self.assertEqual(number("0.005339",places=None),"0.005339")

    def test_obi_percent_rounds_only_presentation_and_handles_missing(self):
        cases=((None,"—"),("0","0.00%"),("0.777","77.70%"),
               ("-0.0416666666666666666666","-4.17%"),("1","100.00%"),
               ("-0.000001","0.00%"),("NaN","—"))
        for value,expected in cases:
            with self.subTest(value=value):self.assertEqual(obi_percent(value),expected)

    def test_optional_help_preserves_existing_control_state(self):
        controls=Controls(("ETH","HYPE"))
        controls.key("p");controls.key("\t");controls.key("+")
        controls.key("h");self.assertTrue(controls.show_help)
        self.assertTrue(controls.paused)
        self.assertEqual((controls.selected,controls.levels),("HYPE",11))
        controls.key("?");self.assertFalse(controls.show_help)

    def test_existing_database_and_sidecars_are_never_reused(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(dom,"DATA_ROOT",Path(directory)):
            path=Path(directory)/"old.sqlite3"
            path.write_bytes(b"existing evidence")
            with self.assertRaises(ValueError):fresh_database(path)
            self.assertEqual(path.read_bytes(),b"existing evidence")
            sidecar=Path(directory)/"new.sqlite3-wal";sidecar.write_bytes(b"WAL")
            with self.assertRaises(ValueError):fresh_database(Path(directory)/"new.sqlite3")
            self.assertEqual(sidecar.read_bytes(),b"WAL")

    def test_default_database_paths_are_unique_and_outside_path_rejected(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(dom,"DATA_ROOT",Path(directory)):
            first,second=fresh_database(),fresh_database()
            self.assertNotEqual(first,second)
            self.assertTrue(first.exists() and second.exists())
            with self.assertRaises(ValueError):fresh_database(Path(directory).parent/"outside.sqlite3")

    def test_invalid_cli_inputs_fail_before_database_or_network(self):
        cases=(["ETH","--levels","0"],["ETH","--levels","21"],
               ["ETH","--refresh","nan"],["ETH","--seconds","0"],["ETH/USDC"])
        for argv in cases:
            with self.subTest(argv=argv),patch("src.dom.foreground") as run,patch("src.dom.fresh_database") as db:
                with patch("sys.stderr",io.StringIO()),self.assertRaises(SystemExit) as error:dom.main(argv)
                self.assertEqual(error.exception.code,2)
                run.assert_not_called();db.assert_not_called()

    def test_missing_rich_fails_before_network_and_database(self):
        original=__import__
        def importing(name,*args,**kwargs):
            if name=="rich.console":raise ImportError("optional dependency unavailable")
            return original(name,*args,**kwargs)
        with patch("builtins.__import__",side_effect=importing),patch("sys.stderr",io.StringIO()) as error, \
             patch("src.dom.foreground") as run:
            self.assertEqual(dom.main(["ETH"]),2)
        self.assertIn("requirements-dom.txt",error.getvalue())
        run.assert_not_called()

    @unittest.skipIf(Console is None,"optional Rich dependency")
    def test_dumb_tty_fails_before_connecting_invisibly(self):
        with patch("rich.console.Console",return_value=SimpleNamespace(is_terminal=True,is_dumb_terminal=True)), \
             patch("sys.stderr",io.StringIO()) as error,patch("src.dom.foreground") as run:
            self.assertEqual(dom.main(["ETH"]),2)
        run.assert_not_called()
        self.assertIn("TERM=dumb",error.getvalue())


@unittest.skipIf(Console is None,"optional Rich presentation dependency not installed")
class DOMRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.journal=FlowStore(Path(self.temp.name)/"flow.sqlite3");self.addCleanup(self.journal.close)
        self.live=LiveStore(self.journal)
        self.client=PublicWSClient(("ETH","HYPE"),clock_ms=lambda:NOW+1000)
        self.client.connection_epoch=1;self.client.connected_at_ms=NOW
        self.client.pending={sub:100 for sub in self.client.desired}
        for sub in self.client.desired:self.client.handle_message(ack(sub))
        for coin in self.client.symbols:self.client.handle_message(packet("l2Book",wire_book(coin)))
        self.client.handle_message(packet("trades",[trade(time_ms=NOW+1000)]))
        self.live.sync_ws(self.client,now_ms=NOW+1000)
        self.session=DOMSession(self.client,self.live,RecordingRenderer(),clock_ms=lambda:NOW+1000)

    def text(self,frame=None,width=120,height=40):
        output=io.StringIO()
        console=Console(file=output,width=width,height=height,no_color=True,color_system=None)
        console.print(render_frame(frame or self.session.frame(),width,height))
        return output.getvalue()

    def test_full_render_has_prices_notionals_cumulative_depth_bands_and_aggressor(self):
        text=self.text()
        for value in ("READ-ONLY","ETH","Bid px","Ask px","quote","Σ base",
                      "OBI observado","OBI válido","PARTIAL","WARMUP","AGGRESSOR A/B","1m","5m","15m"):
            self.assertIn(value,text)
        self.assertNotIn("\x1b[",text)

    def test_ten_levels_initially_and_adaptive_height(self):
        text=self.text(width=80,height=24)
        self.assertIn("10/10 niveles",text)
        self.assertLessEqual(len(text.splitlines()),24)
        self.assertTrue(all(len(line)<=80 for line in text.splitlines()))
        short=self.text(width=60,height=16)
        self.assertIn("2/10 niveles",short)
        self.assertLessEqual(len(short.splitlines()),16)

    def test_tiny_terminal_and_null_values_are_legible(self):
        empty=replace(self.session.frame(),flow=None,l2=None)
        text=self.text(empty,width=32,height=10)
        for value in ("—","WARMUP","UNVERIFIED","Q","P"):
            self.assertIn(value,text)
        self.assertLessEqual(len(text.splitlines()),10)
        self.assertTrue(all(len(line)<=32 for line in text.splitlines()))

    def test_stale_book_and_tape_gap_are_displayed_separately(self):
        frame=self.session.frame()
        l2=self.live.read_l2("mainnet","ETH",as_of_ms=NOW+20000)
        transport=self.client.summary();transport["assets"]["ETH"]["continuity"]="gap_detected"
        frame=replace(frame,l2=l2,transport=freeze(transport))
        text=self.text(frame)
        self.assertIn("L2 STALE",text)
        self.assertIn("TAPE GAP",text)
        self.assertIn("AGGRESSOR A/B",text)
        self.assertIsNone(l2["metrics"]["obi_legacy"]["valid"])

    def test_renderer_uses_provided_obi_and_never_recalculates_or_mutates_views(self):
        frame=self.session.frame();data=frame.l2.to_dict()
        data["metrics"]["obi_legacy"].update(observed="0.777",valid=None)
        frozen=LiveSnapshot(freeze(data));before=json.dumps(frozen.to_dict(),sort_keys=True)
        frame=replace(frame,l2=frozen)
        with patch("src.orderbook.obi",side_effect=AssertionError("duplicate maths forbidden")), \
             patch("src.orderbook.calculate_book",side_effect=AssertionError("duplicate metrics forbidden")), \
             patch("subprocess.run",side_effect=AssertionError("execution forbidden")), \
             patch("urllib.request.urlopen",side_effect=AssertionError("network forbidden")):
            text=self.text(frame)
        self.assertIn("obs 77.70%",text)
        self.assertEqual(json.dumps(frozen.to_dict(),sort_keys=True),before)

    def test_width_is_capped_without_losing_quote_depth_fields(self):
        text=self.text(width=240,height=40)
        self.assertTrue(all(len(line)<=dom.MAX_RENDER_WIDTH for line in text.splitlines()))
        self.assertIn("Σ quote",text)
        self.assertIn("OBI válido",text)
        self.assertIn("H/?",text)

    def test_optional_help_explains_quality_and_displays_existing_gap_reasons(self):
        frame=self.session.frame()
        transport=self.client.summary()
        transport["assets"]["ETH"]["gap_reasons"]={"dedup_horizon_expired":419}
        text=self.text(replace(frame,show_help=True,transport=freeze(transport)))
        for value in ("READY","PARTIAL","GAP","UNVERIFIED","C / P / U","dedup_horizon_expired=419","H/? volver"):
            self.assertIn(value,text)
        self.assertNotIn("AYUDA",self.text())

    def test_ready_partial_and_unknown_signed_values_remain_distinct(self):
        frame=self.session.frame();data=frame.flow.to_dict()
        data.update(calculation_version="1.0",side_semantics="unverified",side_semantics_version=None)
        data["windows"]["1m"].update(ready=True,trades=7,volume_base="12.34567",volume_quote="1234.5678")
        data["windows"]["1m"]["quality_reasons"]=["side_semantics_unverified"]
        text=self.text(replace(frame,flow=LiveSnapshot(freeze(data))))
        self.assertIn("READY",text)
        self.assertIn("WARMUP PARTIAL",text)
        self.assertIn("UNVERIFIED",text)

    def test_paused_label_and_controls_survive_narrow_screen(self):
        frame=replace(self.session.frame(),paused=True)
        text=self.text(frame,width=80,height=24)
        self.assertIn("PAUSED",text)
        self.assertIn("ingesta activa",text)

    def test_switch_frame_selects_monitored_asset_without_new_subscriptions(self):
        first=self.session.frame()
        self.session.controls.key("\t")
        second=self.session.frame()
        self.assertEqual((first.symbol,second.symbol),("ETH","HYPE"))
        self.assertEqual(len(self.client.desired),4)
        self.assertEqual(first.l2["symbol"],"ETH")
        self.assertEqual(second.l2["symbol"],"HYPE")
        with self.assertRaises(TypeError):second.transport["state"]="modified"

    def test_verified_synthetic_signed_values_require_engine_availability(self):
        frame=self.session.frame();data=frame.flow.to_dict()
        data["network"]="synthetic"
        data["side_semantics"]="verified_synthetic_aggressor"
        data["cvd"].update(base="9.876",quote="987.6",available=True)
        data["windows"]["1m"].update(ready=True,signed_available=True,delta_base="1.234",cvd_window_base="1.234")
        text=self.text(replace(frame,flow=LiveSnapshot(freeze(data))))
        self.assertIn("VERIFIED SYNTHETIC",text)
        self.assertIn("9.876",text)
        self.assertIn("1.234",text)
        data["cvd"]["available"]=False
        text=self.text(replace(frame,flow=LiveSnapshot(freeze(data))))
        self.assertNotIn("9.876",text)

    def test_documented_real_delta_requires_versions_coverage_and_availability(self):
        for network in ("mainnet","testnet"):
            frame=self.session.frame();data=frame.flow.to_dict();data["network"]=network
            data["windows"]["1m"].update(ready=True,signed_available=True,delta_base="2.345",cvd_window_base="2.345")
            data["cvd"].update(available=True,base="7.654",quote="765.4")
            text=self.text(replace(frame,flow=LiveSnapshot(freeze(data))))
            self.assertIn("AGGRESSOR A/B",text)
            self.assertIn("2.345",text)
            self.assertIn("7.654",text)
            for mutate in (lambda d:d.update(side_semantics_version="unknown"),
                           lambda d:d.update(calculation_version="1.0"),
                           lambda d:d["connection"].update(fresh=False),
                           lambda d:d.update(recovered_not_live=True),
                           lambda d:d["windows"]["1m"].update(ready=False),
                           lambda d:d["windows"]["1m"].update(signed_available=False)):
                import copy
                invalid=copy.deepcopy(data);mutate(invalid)
                text=self.text(replace(frame,flow=LiveSnapshot(freeze(invalid))))
                self.assertNotIn("2.345",text)
                if not invalid["connection"]["fresh"] or invalid.get("recovered_not_live"):
                    self.assertNotIn("7.654",text)

    def test_legacy_unsigned_render_cannot_show_forged_directional_values(self):
        frame=self.session.frame();data=frame.flow.to_dict()
        data.update(calculation_version="1.0",side_semantics="unverified",side_semantics_version=None)
        data["windows"]["1m"].update(ready=True,signed_available=True,delta_base="2.345",cvd_window_base="2.345")
        data["cvd"].update(available=True,base="7.654")
        text=self.text(replace(frame,flow=LiveSnapshot(freeze(data))))
        self.assertIn("UNVERIFIED",text)
        self.assertNotIn("2.345",text);self.assertNotIn("7.654",text)

    def test_testnet_session_reads_testnet_views_and_identifies_network(self):
        client=PublicWSClient(("ETH",),network="testnet",clock_ms=lambda:NOW+1000)
        client.connection_epoch=1;client.connected_at_ms=NOW
        client.pending={sub:100 for sub in client.desired}
        for sub in client.desired:client.handle_message(ack(sub))
        client.handle_message(packet("l2Book",wire_book()))
        self.live.sync_ws(client,now_ms=NOW+1000)
        session=DOMSession(client,self.live,RecordingRenderer(),clock_ms=lambda:NOW+1000)
        frame=session.frame()
        self.assertEqual(frame.flow["network"],"testnet")
        self.assertEqual(frame.l2["network"],"testnet")
        text=self.text(frame)
        self.assertIn("TESTNET",text)
        self.assertIn("AGGRESSOR A/B",text)


class DOMLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.journal=FlowStore(Path(self.temp.name)/"flow.sqlite3");self.addCleanup(self.journal.close)
        self.live=LiveStore(self.journal)
        self.now=NOW+1000
        self.socket=FakeSocket()
        self.connections=[]
        async def connector(url,**kwargs):self.connections.append(url);return self.socket
        self.client=PublicWSClient(("ETH","HYPE"),connector=connector,clock_ms=lambda:self.now)
        self.renderer=RecordingRenderer();self.keys=asyncio.Queue()
        self.session=DOMSession(self.client,self.live,self.renderer,refresh=20,clock_ms=lambda:self.now,
                                key_source=self.keys.get,poll_seconds=0.001)

    async def until(self,predicate):
        async def observe():
            while not predicate():await asyncio.sleep(0.001)
        await asyncio.wait_for(observe(),3)

    async def ready(self):
        self.task=asyncio.create_task(self.session.run())
        await self.until(lambda:self.client.confirmed==self.client.desired and self.session.rendered_frames>0)

    async def quit(self):
        await self.keys.put("q")
        return await asyncio.wait_for(self.task,3)

    async def test_one_shared_connection_and_clean_quit_drain_audit_close(self):
        await self.ready()
        await self.socket.inbox.put(packet("l2Book",wire_book("ETH")))
        await self.socket.inbox.put(packet("trades",[trade(time_ms=self.now)]))
        await self.until(lambda:self.client.counts["trades_consumed"]==1)
        report=await self.quit()
        self.assertEqual(len(self.connections),1)
        self.assertTrue(self.socket.closed and self.journal.closed and self.renderer.closed)
        self.assertEqual(report["remaining_trade_queue"],0)
        self.assertTrue(report["recovery"]["state_matches"])
        self.assertEqual(report["observed_trades"]["ETH"],1)
        self.assertFalse(report["execution_authorized"])

    async def test_pause_freezes_render_but_ingestion_and_journal_continue(self):
        await self.ready()
        await self.keys.put("p")
        await self.until(lambda:self.renderer.frames[-1].paused)
        count=self.session.rendered_frames
        frozen=self.renderer.frames[-1]
        self.now+=1000
        await self.socket.inbox.put(packet("trades",[trade(time_ms=self.now)]))
        await self.until(lambda:self.client.counts["trades_consumed"]==1)
        await asyncio.sleep(0.08)
        self.assertEqual(self.session.rendered_frames,count)
        self.assertIs(self.renderer.frames[-1],frozen)
        self.assertEqual(self.live.read("mainnet","ETH")["observed_total"]["unique_trades"],1)
        await self.keys.put("p")
        await self.until(lambda:not self.renderer.frames[-1].paused)
        self.assertEqual(self.renderer.frames[-1].flow["observed_total"]["unique_trades"],1)
        await self.quit()

    async def test_help_does_not_pause_capture_and_can_be_closed(self):
        await self.ready()
        await self.keys.put("h")
        await self.until(lambda:self.renderer.frames[-1].show_help)
        self.assertFalse(self.session.controls.paused)
        self.now+=1000
        await self.socket.inbox.put(packet("trades",[trade(time_ms=self.now)]))
        await self.until(lambda:self.client.counts["trades_consumed"]==1)
        await self.keys.put("?")
        await self.until(lambda:not self.renderer.frames[-1].show_help)
        await self.quit()

    async def test_slow_renderer_does_not_block_reception_or_ingestion(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def blocked(frame):entered.set();await release.wait()
        self.renderer.present=blocked
        self.task=asyncio.create_task(self.session.run())
        await asyncio.wait_for(entered.wait(),3)
        await self.until(lambda:self.client.confirmed==self.client.desired)
        await self.socket.inbox.put(packet("trades",[trade(time_ms=self.now)]))
        await self.until(lambda:self.client.counts["trades_consumed"]==1)
        self.assertFalse(release.is_set())
        await self.quit()

    async def test_pause_during_in_flight_render_is_not_lost(self):
        entered=asyncio.Event();release=asyncio.Event()
        original=self.renderer.present
        calls=0
        async def delayed(frame):
            nonlocal calls
            calls+=1
            if calls==1:
                entered.set();await release.wait()
            await original(frame)
        self.renderer.present=delayed
        self.task=asyncio.create_task(self.session.run())
        await asyncio.wait_for(entered.wait(),3)
        await self.keys.put("p")
        await self.until(lambda:self.session.controls.paused)
        release.set()
        await self.until(lambda:len(self.renderer.frames)>=2 and self.renderer.frames[-1].paused)
        self.assertEqual(self.renderer.frames[0].as_of_ms,self.renderer.frames[-1].as_of_ms)
        await self.quit()

    async def test_cancellation_closes_socket_renderer_database_and_all_tasks(self):
        await self.ready();self.task.cancel()
        with self.assertRaises(asyncio.CancelledError):await self.task
        self.assertTrue(self.socket.closed and self.journal.closed and self.renderer.closed)
        self.assertFalse(any(t.get_name().startswith("dom-") for t in asyncio.all_tasks() if t is not asyncio.current_task()))

    async def test_early_quit_without_data_is_explicit_not_fake_market_state(self):
        self.session.controls.key("q")
        report=await self.session.run()
        self.assertEqual(report["recovery"]["journal_rows"],0)
        self.assertIsNone(report["recovery"]["state_matches"])
        self.assertTrue(self.journal.closed and self.renderer.closed)

    async def test_display_error_still_closes_resources(self):
        async def broken(frame):raise RuntimeError("simulated display failure")
        self.renderer.present=broken
        with self.assertRaises(RuntimeError):await self.session.run()
        self.assertTrue(self.journal.closed and self.renderer.closed and self.socket.closed)

    async def test_journal_failure_stops_instead_of_hiding_or_discarding_queue(self):
        await self.ready()
        with patch.object(self.journal,"write",side_effect=RuntimeError("simulated journal failure")):
            await self.socket.inbox.put(packet("trades",[trade(time_ms=self.now)]))
            with self.assertRaises(RuntimeError):await self.task
        self.assertTrue(self.journal.closed and self.socket.closed)
        self.assertEqual(len(self.client.trade_queue),1)

    async def test_terminal_keyboard_restores_original_settings(self):
        if os.name!="posix":self.skipTest("POSIX keyboard")
        import pty
        import termios
        master,slave=pty.openpty()
        self.addCleanup(os.close,master);self.addCleanup(os.close,slave)
        stream=os.fdopen(os.dup(slave),"r");self.addCleanup(stream.close)
        original=termios.tcgetattr(slave)
        with TerminalKeys(stream) as keyboard:
            self.assertTrue(keyboard.enabled)
            os.write(master,b"p\t+")
            self.assertEqual([await asyncio.wait_for(keyboard.get(),1) for _ in range(3)],["p","\t","+"])
        self.assertEqual(termios.tcgetattr(slave),original)

    @unittest.skipIf(Console is None,"optional Rich dependency")
    async def test_actual_rich_renderer_worker_uses_detached_frame_and_closes(self):
        output=io.StringIO()
        console=Console(file=output,width=80,height=24,no_color=True,color_system=None)
        renderer=RichRenderer(console)
        frame=DOMFrame("ETH",0,1,10,self.now,None,None,freeze({"state":"disconnected"}))
        await renderer.present(frame)
        await renderer.close()
        self.assertTrue(renderer.pool._shutdown)
        self.assertIn("READ-ONLY",output.getvalue())
        self.assertIn("UNVERIFIED",output.getvalue())


if __name__=="__main__":unittest.main()
