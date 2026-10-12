"""Synthetic virtual-time integration fixture; no public API, sleeps or models."""
import sys, json, threading, math
from collections import Counter
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

from src.rest_policy import RESTConfig, RESTCoordinator, request_weight
from src.hl_client import HyperliquidInfoClient
from src.market_context import MarketContextProvider
from src.market_state import INTERVAL_MS
from src.research_pipeline import run_research, format_report
from src.research_config import ResearchConfig
from test_hl_client import Response
from test_analysis_pipeline import output

BASE=2_000_001_600_000  # synthetic UTC, aligned to 4h
BASE=BASE//14400000*14400000

class Engine:
    """One running simulated worker; virtual time advances only at quiescence."""
    def __init__(self):
        self.now=0.; self.cv=threading.Condition(); self.local=threading.local(); self.executors=[]; self.sequence=0; self.order=0
    def timer(self): return self.now
    def wall(self): return BASE+int(round(self.now*1000))
    def wait(self,seconds):
        task=getattr(self.local,'task',None)
        if task is None:
            self.now+=seconds; return
        with self.cv:
            task['wake']=self.now+seconds; task['state']='sleep'; self.cv.notify_all()
            while task['state']!='run': self.cv.wait()
    def executor(self,max_workers):
        return Executor(self,max_workers)

class SimFuture(Future):
    def __init__(self,executor): super().__init__(); self.executor=executor
    def result(self,timeout=None):
        self.executor.pump(lambda:self.done())
        return super().result(timeout=0)

class Executor:
    def __init__(self,engine,max_workers):
        self.e=engine; self.n=max_workers; self.tasks=[]; engine.executors.append(self)
    def __enter__(self): return self
    def __exit__(self,*args):
        self.pump(lambda:all(t['future'].done() for t in self.tasks))
        for t in self.tasks:
            if 'thread' in t: t['thread'].join(timeout=2)
    def submit(self,fn,*args,**kwargs):
        f=SimFuture(self)
        self.tasks.append({'fn':fn,'args':args,'kwargs':kwargs,'future':f,'state':'queued','id':self.e.sequence})
        self.e.sequence+=1
        return f
    def map(self,fn,items):
        futures=[self.submit(fn,item) for item in items]
        return (f.result() for f in futures)
    def worker(self,t):
        self.e.local.task=t
        with self.e.cv:
            while t['state']!='run': self.e.cv.wait()
        try: result=t['fn'](*t['args'],**t['kwargs']); t['future'].set_result(result)
        except BaseException as exc: t['future'].set_exception(exc)
        finally:
            with self.e.cv: t['state']='done'; self.e.cv.notify_all()
    def pump(self,done):
        with self.e.cv:
            guard=0
            while not done():
                guard+=1
                if guard>200000: raise AssertionError('simulation did not converge')
                # All overlapping pools share the same virtual scheduler.
                for executor in self.e.executors:
                    active=sum(t['state'] in ('ready','sleep','run') for t in executor.tasks)
                    for t in executor.tasks:
                        if active>=executor.n: break
                        if t['state']=='queued':
                            t['state']='ready'; active+=1
                            t['thread']=threading.Thread(target=executor.worker,args=(t,),daemon=True); t['thread'].start()
                tasks=[t for executor in self.e.executors for t in executor.tasks]
                ready=[t for t in tasks if t['state']=='ready' or t['state']=='sleep' and t['wake']<=self.e.now+1e-9]
                if not ready:
                    sleeping=[t for t in tasks if t['state']=='sleep']
                    if not sleeping: raise AssertionError('no simulated workers ready')
                    self.e.now=min(t['wake'] for t in sleeping)
                    continue
                ready.sort(key=lambda t:((t['id']-self.e.order)%max(1,self.e.sequence)))
                t=ready[0]; self.e.order=t['id']+1
                t['state']='run'; self.e.cv.notify_all()
                while t['state']=='run':
                    if not self.e.cv.wait(timeout=5): raise AssertionError('uncontrolled blocking in simulation')


def synthetic_value(payload, symbols, wall):
    """Shared synthetic REST fixture for virtual and real-thread benchmarks."""
    kind = payload['type']
    symbol = payload.get('coin', payload.get('req', {}).get('coin'))
    if kind == 'metaAndAssetCtxs':
        return [{'universe':[{'name':s,'szDecimals':2,'maxLeverage':3} for s in symbols]},
                [{'markPx':'104','midPx':'104.005','oraclePx':'104','prevDayPx':'103',
                  'funding':'0.00001','openInterest':'200000','dayNtlVlm':str(100000000-i*100000),
                  'premium':'0','impactPxs':['104','104.01']} for i,s in enumerate(symbols)]]
    if kind == 'candleSnapshot':
        r=payload['req']; d=INTERVAL_MS[r['interval']]; end=r['endTime']//d*d
        start=r['startTime']//d*d
        value=[]
        for i,t in enumerate(range(start,end,d)):
            c=100+i*.02
            value.append({'t':t,'T':t+d-1,'o':str(c-.01),'h':str(c+.2),'l':str(c-.2),'c':str(c),
                          'v':'300' if t==end-d else '100','n':10})
        return value
    if kind == 'l2Book':
        return {'coin':symbol,'time':wall(),'levels':[[{'px':'104','sz':'200','n':2}],[{'px':'104.01','sz':'200','n':2}]]}
    if kind == 'fundingHistory':
        return [{'time':wall()-i*3600000,'fundingRate':'0.00001'} for i in reversed(range(72))]
    raise AssertionError('unexpected request')


def scenario(mode="volume",pool=10,latency=0.02,*,incremental=True,max_admitted=3,model_latency=0):
    e=Engine(); calls=[]; captures=[]; model_calls=[]; weights=[]
    config=RESTConfig()
    coordinator=RESTCoordinator(config,timer=e.timer,wait=e.wait)
    symbols=['T'+str(i).zfill(2) for i in range(pool)]
    def transport(request,timeout):
        payload=json.loads(request.data); kind=payload['type']; symbol=payload.get('coin',payload.get('req',{}).get('coin'))
        calls.append((e.now,kind,symbol))
        if latency>=timeout:
            weights.append(request_weight(payload)); e.wait(timeout)
            raise TimeoutError('synthetic transport timeout')
        e.wait(latency)
        value=synthetic_value(payload, symbols, e.wall)
        weights.append(request_weight(payload,value))
        return Response(json.dumps(value))
    client=HyperliquidInfoClient(coordinator=coordinator,transport=transport,clock_ms=e.wall,rng=lambda:0)
    provider=MarketContextProvider(client,timer=e.timer,clock_ms=e.wall,wait=e.wait)
    from src.market_state import build_market_state
    def builder(symbol,**kwargs):
        start=e.now
        market=build_market_state(symbol,**kwargs)
        captures.append((symbol,start,e.now,market))
        return market
    def runner(command,**kwargs):
        snapshot=json.loads(kwargs['input']); model_calls.append((snapshot['symbol'],e.now,snapshot['valid_until_utc']))
        if model_latency: e.wait(model_latency)
        return SimpleNamespace(returncode=0,stdout=output(),stderr='')
    with patch('time.sleep',side_effect=AssertionError('real sleep forbidden')), \
         patch('urllib.request.urlopen',side_effect=AssertionError('real HTTP forbidden')), \
         patch('subprocess.run',side_effect=AssertionError('real process forbidden')), \
         patch('src.market_screener.ThreadPoolExecutor',e.executor), \
         patch('src.scanner.ThreadPoolExecutor',e.executor), \
         patch('src.analysis_pipeline.ThreadPoolExecutor',e.executor), \
         patch('src.scanner.build_market_state',builder):
        report=run_research(configuration=ResearchConfig(rest=config),top_n=pool,limit=10,sort_by=mode,
                            context_provider=provider,clock_ms=e.wall,timer=e.timer,runner=runner,
                            incremental=incremental,max_admitted=max_admitted,admission_wait=e.wait)
    assert report['scanner'] is not None, report['errors']
    rows=report['scanner']['results']; analysis=report['analysis']['results']
    complete=sum(not [i for i in market['data_quality']['issues'] if i['code']!='unknown_freshness'] for _,_,_,market in captures)
    details=[]
    for symbol,start,end,market in captures:
        issues=[i for i in market['data_quality']['issues'] if i['code']!='unknown_freshness']
        row=next(r for r in rows if r['symbol']==symbol)
        review=next(r for r in analysis if r['symbol']==symbol)['model_review']
        details.append({'symbol':symbol,'start':round(start,3),'duration':round(end-start,3),
                        'class':row['classification'],'issues':list(dict.fromkeys(i['detail'] for i in issues)),
                        'review':review['reason'],'invoked':review['invoked']})
    summary={'incremental':incremental,'rest_requests':len(calls),'accounted_rest_weight':sum(weights),'case':mode,'pool':pool,'latency':latency,'elapsed':round(e.now,3),
             'research_status':report['status'],'scanner_status':report['scanner']['status'],
             'complete_captures':complete,'returned_captures':len(captures),'classified':report['scanner']['counts'],
             'model_invocations':len(model_calls),'review_reasons':dict(Counter(r['model_review']['reason'] for r in analysis)),
             'calls_by_endpoint':dict(Counter(k for _,k,_ in calls)),
             'max_capture_duration':round(max(end-start for _,start,end,_ in captures),3),
             'budget_error_in_json':'budget_wait_exceeded' in json.dumps(report),
             'budget_error_in_text':'budget_wait_exceeded' in format_report(report),
             'expiry_in_text':'snapshot_expired' in format_report(report),'details':details}
    return summary,report
