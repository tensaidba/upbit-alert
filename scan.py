import json
import os
import time
import urllib.request


def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    time.sleep(0.05)
    return data


def sma_series(series, period):
    out = [None] * len(series)
    for i in range(len(series)):
        if i + 1 >= period:
            out[i] = sum(series[i + 1 - period:i + 1]) / period
    return out


def midpoint(high, low, period, idx):
    if idx + 1 < period:
        return None
    h = max(high[idx + 1 - period:idx + 1])
    l = min(low[idx + 1 - period:idx + 1])
    return (h + l) / 2


def has_recent_signal(candles):
    data = list(reversed(candles))
    close = [d['trade_price'] for d in data]
    high = [d['high_price'] for d in data]
    low = [d['low_price'] for d in data]
    vol = [d['candle_acc_trade_volume'] for d in data]
    n = len(close)
    if n < 100:
        return False
    ma10 = sma_series(close, 10); ma20 = sma_series(close, 20); ma60 = sma_series(close, 60)
    vol_ma20 = sma_series(vol, 20)
    tenkan = [midpoint(high, low, 9, i) for i in range(n)]
    kijun = [midpoint(high, low, 26, i) for i in range(n)]
    senkouA_raw = [None if a is None or b is None else (a + b) / 2 for a, b in zip(tenkan, kijun)]
    senkouB_raw = [midpoint(high, low, 52, i) for i in range(n)]
    senkouA = [None] * (n + 26); senkouB = [None] * (n + 26)
    for i in range(n):
        if senkouA_raw[i] is not None: senkouA[i + 26] = senkouA_raw[i]
        if senkouB_raw[i] is not None: senkouB[i + 26] = senkouB_raw[i]
    ma_aligned = [(ma10[i] is not None and ma20[i] is not None and ma60[i] is not None and ma10[i] > ma20[i] > ma60[i]) for i in range(n)]

    events = {}

    def add(idx, name):
        events.setdefault(idx, set()).add(name)

    for i in range(1, n):
        if ma_aligned[i] and not ma_aligned[i - 1]:
            add(i, "MA")
        if i < len(senkouA) and i < len(senkouB) and senkouA[i] is not None and senkouB[i] is not None and senkouA[i - 1] is not None and senkouB[i - 1] is not None:
            top_prev = max(senkouA[i - 1], senkouB[i - 1]); top_cur = max(senkouA[i], senkouB[i])
            if close[i - 1] <= top_prev and close[i] > top_cur:
                add(i, "ICHI")
        if vol_ma20[i - 1] is not None and vol_ma20[i] is not None and vol[i - 1] <= vol_ma20[i - 1] * 2.5 and vol[i] > vol_ma20[i] * 2.5:
            add(i, "VOL")

    last_idx = n - 1
    recent = {i: s for i, s in events.items() if i >= last_idx - 10}
    if not recent:
        return False
    all_sigs = set()
    for s in recent.values():
        all_sigs |= s
    return len(all_sigs) >= 2


def check_full_state(candles, momentum_bars=None):
    data = list(reversed(candles))
    close = [d['trade_price'] for d in data]
    high = [d['high_price'] for d in data]
    low = [d['low_price'] for d in data]
    n = len(close)
    if n < 90:
        return None
    ma10 = sma_series(close, 10); ma20 = sma_series(close, 20); ma60 = sma_series(close, 60)
    tenkan = [midpoint(high, low, 9, i) for i in range(n)]
    kijun = [midpoint(high, low, 26, i) for i in range(n)]
    senkouA_raw = [None if a is None or b is None else (a + b) / 2 for a, b in zip(tenkan, kijun)]
    senkouB_raw = [midpoint(high, low, 52, i) for i in range(n)]
    senkouA = [None] * (n + 26); senkouB = [None] * (n + 26)
    for i in range(n):
        if senkouA_raw[i] is not None: senkouA[i + 26] = senkouA_raw[i]
        if senkouB_raw[i] is not None: senkouB[i + 26] = senkouB_raw[i]
    i = n - 1
    ma_ok = ma10[i] is not None and ma20[i] is not None and ma60[i] is not None and ma10[i] > ma20[i] > ma60[i]
    cloud_top = max(senkouA[i], senkouB[i]) if (senkouA[i] is not None and senkouB[i] is not None) else None
    above = cloud_top is not None and close[i] > cloud_top
    mom = None
    if momentum_bars and n > momentum_bars:
        mom = (close[i] - close[i - momentum_bars]) / close[i - momentum_bars] * 100
    return {'ma_aligned': ma_ok, 'above_cloud': above, 'momentum': mom, 'price': close[i]}


def send_slack(message):
    webhook = os.environ.get('SLACK_WEBHOOK_URL')
    if not webhook:
        print("SLACK_WEBHOOK_URL not set, skipping notification. Message was:")
        print(message)
        return
    payload = json.dumps({'text': message}).encode('utf-8')
    req = urllib.request.Request(webhook, data=payload, headers={'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req, timeout=10)
        print("Slack notification sent.")
    except Exception as e:
        print(f"Slack send failed: {e}")


def main():
    markets_data = fetch("https://api.upbit.com/v1/market/all?isDetails=false")
    krw_markets = [m['market'] for m in markets_data if m['market'].startswith('KRW-')]

    candidates = []
    for market in krw_markets:
        try:
            c15 = fetch(f"https://api.upbit.com/v1/candles/minutes/15?market={market}&count=200")
            if has_recent_signal(c15):
                candidates.append(market)
        except Exception:
            pass

    qualifying = []
    for market in candidates:
        try:
            c30 = fetch(f"https://api.upbit.com/v1/candles/minutes/30?market={market}&count=200")
            c4h = fetch(f"https://api.upbit.com/v1/candles/minutes/240?market={market}&count=100")
            cday = fetch(f"https://api.upbit.com/v1/candles/days?market={market}&count=31")

            s30 = check_full_state(c30)
            s4h = check_full_state(c4h, momentum_bars=42)
            if not s30 or not s4h:
                continue

            dday = list(reversed(cday))
            d30 = (dday[-1]['trade_price'] - dday[0]['trade_price']) / dday[0]['trade_price'] * 100

            if (s30['ma_aligned'] and s30['above_cloud'] and
                    s4h['ma_aligned'] and s4h['above_cloud'] and
                    s4h['momentum'] is not None and s4h['momentum'] > 0 and
                    d30 < 70):
                qualifying.append({
                    'market': market,
                    'price': s4h['price'],
                    'momentum_7d': round(s4h['momentum'], 1),
                    'change_30d': round(d30, 1),
                })
        except Exception:
            pass

    qualifying.sort(key=lambda x: -x['momentum_7d'])
    print(f"SCANNED: {len(krw_markets)} markets, {len(candidates)} candidates, {len(qualifying)} qualifying")

    if qualifying:
        tickers = [q['market'].replace('KRW-', '') for q in qualifying]
        top = qualifying[0]
        ticker_str = ','.join(tickers[:5]) + (f" 외{len(tickers) - 5}종" if len(tickers) > 5 else "")
        top_ticker = top['market'].replace('KRW-', '')
        msg = (f"🔔 [업비트 신호] {ticker_str}\n"
               f"1위: {top_ticker} {top['price']}원, 4h 7일모멘텀 {top['momentum_7d']}%, 30일변동 {top['change_30d']}%\n"
               f"(투자 조언 아님, 조건 충족 사실만 전달)")
        send_slack(msg)
    else:
        print("No qualifying signals this run.")


if __name__ == "__main__":
    main()
