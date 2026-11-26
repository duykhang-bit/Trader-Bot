#!/usr/bin/env python3
"""Reproduce the fixed Intraday Paper validation from Binance public data.

No parameter search occurs here. It downloads the fixed UTC interval used for
validation, applies the production paper rules, and prints portfolio/per-symbol
results after modeled VIP0 futures fees and slippage.

Validated interval: 2025-11-26 16:08 UTC through 2026-11-26 16:06 UTC.
Run: python3 tools/intraday_paper_backtest.py [--cache path.json]
"""
from __future__ import annotations
import argparse, json, time, urllib.parse, urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

SYMBOLS=("BTCUSDT","ETHUSDT","SOLUSDT")
START=int(datetime(2025,11,26,16,8,tzinfo=timezone.utc).timestamp()*1000)
END=int(datetime(2026,11,26,16,7,tzinfo=timezone.utc).timestamp()*1000)
ENTRY_COST=STOP_COST=TIME_COST=.0006; TP_COST=.0002

def fetch():
 out={}
 for sym in SYMBOLS:
  rows=[];cur=START
  while cur<END:
   url="https://fapi.binance.com/fapi/v1/klines?"+urllib.parse.urlencode(dict(symbol=sym,interval="5m",startTime=cur,endTime=END,limit=1500))
   with urllib.request.urlopen(url,timeout=30) as r:data=json.loads(r.read())
   if not data:break
   rows += [[x[0],float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])] for x in data]
   cur=data[-1][0]+300_000;time.sleep(.3)
  out[sym]=rows;print(sym,"bars",len(rows),flush=True)
 return out

def ema(x,n):
 o=np.empty_like(x);a=2/(n+1);o[0]=x[0]
 for i in range(1,len(x)):o[i]=a*x[i]+(1-a)*o[i-1]
 return o

def resample(a,n):
 k=len(a)//n*n;z=a[:k].reshape(-1,n,6)
 return np.column_stack([z[:,0,0],z[:,0,1],z[:,:,2].max(1),z[:,:,3].min(1),z[:,-1,4],z[:,:,5].sum(1)])

def atr(h,l,c,n=14):
 pc=np.roll(c,1);pc[0]=c[0];tr=np.maximum(h-l,np.maximum(abs(h-pc),abs(l-pc)))
 return ema(tr,2*n-1)

def trend4h(b):
 z=resample(b,48);tr=ema(z[:,4],20)-ema(z[:,4],50);idx=np.clip(np.arange(len(b))//48-1,0,len(z)-1);return tr[idx]

def run(b):
 t,o,h,l,c=b[:,0],b[:,1],b[:,2],b[:,3],b[:,4];tr=trend4h(b);a=atr(h,l,c);out=[];day_ms=86_400_000
 first=int((t[0]-13*3_600_000)//day_ms);last=int((t[-1]-13*3_600_000)//day_ms)
 for day in range(first,last+1):
  st=day*day_ms+13*3_600_000;i0=int(np.searchsorted(t,st));i1=i0+12
  if i0<250 or i1+2>=len(b):continue
  rh=h[i0:i1].max();rl=l[i0:i1].min();d=0;si=None
  # 14:00-17:00 UTC, closed-bar breakout in closed 4h trend.
  for i in range(i1,min(len(b)-1,i1+36)):
   buffer=.1*a[i]
   if c[i]>rh+buffer and tr[i]>0:d=1;si=i;break
   if c[i]<rl-buffer and tr[i]<0:d=-1;si=i;break
  if not d:continue
  entry=o[si+1];stop=rl if d>0 else rh;risk=d*(entry-stop)
  if risk<=0 or risk/entry>.03:continue
  target=entry+d*2*risk;res=None
  for j in range(si+1,min(len(b),si+1+24)):
   hit_sl=l[j]<=stop if d>0 else h[j]>=stop;hit_tp=h[j]>=target if d>0 else l[j]<=target
   if hit_sl:res=(stop,j,STOP_COST,"SL");break
   if hit_tp:res=(target,j,TP_COST,"TP");break
  if res is None:j=min(len(b)-1,si+24);res=(c[j],j,TIME_COST,"TIME")
  x,j,fee,why=res;out.append((int(t[j]),d*(x-entry)/entry-ENTRY_COST-fee,why,d))
 return out

def stats(rows,weight=1):
 r=np.array([x[1] for x in rows])/weight;eq=np.cumsum(r);gross=r[r>0].sum();loss=-r[r<0].sum();months=defaultdict(float)
 for x,y,*_ in rows:months[datetime.fromtimestamp(x/1000,timezone.utc).strftime("%Y-%m")]+=y/weight*100
 return dict(trades=len(r),ret=r.sum()*100,win=(r>0).mean()*100,pf=gross/loss,dd=(np.maximum.accumulate(eq)-eq).max()*100,months=dict(months))

def main():
 p=argparse.ArgumentParser();p.add_argument("--cache",default="intraday_backtest_data.json");a=p.parse_args();cache=Path(a.cache)
 if cache.exists():data=json.loads(cache.read_text())
 else:data=fetch();cache.write_text(json.dumps(data))
 all_rows=[]
 for sym in SYMBOLS:
  rows=run(np.array(data[sym],float));all_rows+=rows;s=stats(rows);print(sym,s)
 print("PORTFOLIO equal-weight",stats(sorted(all_rows),len(SYMBOLS)))
 print("Expected reference: BTC -3.03%, ETH +9.62%, SOL +19.31%, equal-weight portfolio +8.63% (small boundary differences possible)")
if __name__=="__main__":main()
