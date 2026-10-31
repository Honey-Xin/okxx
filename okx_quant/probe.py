import requests, json
S = requests.Session(); S.headers.update({"User-Agent":"Mozilla/5.0","Accept":"application/json"})
tests = [
 ("funding", "https://api.gateio.ws/api/v4/futures/usdt/funding_rate?contract=BTC_USDT&limit=5"),
 ("contract_stats", "https://api.gateio.ws/api/v4/futures/usdt/contract_stats?contract=BTC_USDT&interval=1h&limit=5"),
 ("oi_hist", "https://api.gateio.ws/api/v4/futures/usdt/contracts/BTC_USDT"),
 ("longshort", "https://api.gateio.ws/api/v4/futures/usdt/contract_stats?contract=BTC_USDT&limit=3&interval=4h"),
]
for name,u in tests:
    try:
        r = S.get(u, timeout=15)
        print(f"--- {name} HTTP {r.status_code}")
        t = r.text[:600]
        print(t.replace("\n"," "))
    except Exception as e:
        print(f"--- {name} ERR {type(e).__name__} {e}")
