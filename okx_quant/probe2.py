import time, requests, pandas as pd
S = requests.Session(); S.headers.update({"User-Agent":"Mozilla/5.0","Accept":"application/json"})
B="https://api.gateio.ws/api/v4"
# 试 to 参数翻页能走多远
cur=int(time.time())//14400*14400
allt=set(); page=0
while page<15:
    r=S.get(B+"/futures/usdt/contract_stats",params={"contract":"BTC_USDT","interval":"4h","limit":1000,"to":cur},timeout=20)
    r.raise_for_status(); d=r.json()
    if not d: print("empty at page",page); break
    ts=[int(x["time"]) for x in d]
    new=len(set(ts)-allt); allt|=set(ts)
    print(f"page{page}: got {len(d)} new {new} range {pd.to_datetime(min(ts),unit='s')} ~ {pd.to_datetime(max(ts),unit='s')}")
    if new==0: break
    cur=min(ts)-14400; page+=1; time.sleep(0.2)
print("total",len(allt),"?", pd.to_datetime(min(allt),unit='s'), pd.to_datetime(max(allt),unit='s'))
# funding 历史
cur=int(time.time()); a=set(); p=0
while p<15:
    r=S.get(B+"/futures/usdt/funding_rate",params={"contract":"BTC_USDT","limit":1000,"to":cur},timeout=20)
    d=r.json()
    if not d: break
    ts=[int(x["t"]) for x in d]; new=len(set(ts)-a); a|=set(ts)
    print(f"fund p{p}: {len(d)} new {new} {pd.to_datetime(min(ts),unit='s')}")
    if new==0: break
    cur=min(ts)-1; p+=1; time.sleep(0.2)
print("funding total",len(a), pd.to_datetime(min(a),unit='s'), pd.to_datetime(max(a),unit='s'))
