"""
Upbit signal scanner -> Telegram alert.

RULE (walk-forward validated, 3 years, cluster-adjusted, after 0.2% costs):
  Entry (all three on the SAME completed 4h bar):
    - Stochastic %K(14) crosses UP through 30
    - MACD histogram (12,26,9) crosses UP through 0
    - close > MA60
  Regime gate: BTC 4h close > MA200 AND MA50 > MA200  (strong_bull only)
  Exit: TP +3% / SL -9% / max 30 bars (5 days)

  Backtest: n=67 events, win rate 71.6%, PF 1.31, expectancy +0.48%/trade, p=1.2e-5
  KNOWN WEAKNESS: lost money in the 2024-06~2025-03 window (44.4% WR, -2.80%).

IMPORTANT: only COMPLETED bars are evaluated. The in-progress bar is discarded,
because its stochastic/MACD values still change until close -- evaluating it would
not match the backtest and would produce signals that later vanish.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

BASE = "https://api.upbit.com/v1"
KST = timezone(timedelta(hours=9))
BAR_SECONDS = 4 * 3600
TP_PCT = 3.0
SL_PCT = 9.0
MAX_HOLD_BARS = 30
UNIVERSE_SIZE = 30      # rule was validated on the top 30 KRW markets by 24h value


# ------------------------------------------------------------------ http
def fetch(url, retries=4):
    last = None
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode('utf-8'))
            time.sleep(0.11)
            return data
        except Exception as e:
            last = e
            time.sleep(1.0 + a)
    raise last


# ------------------------------------------------------------ indicators
def sma(s, p):
    out = [None] * len(s); acc = 0.0
    for i in range(len(s)):
        acc += s[i]
        if i >= p:
            acc -= s[i - p]
        if i + 1 >= p:
            out[i] = acc / p
    return out


def ema(s, p):
    out = [None] * len(s); k = 2 / (p + 1); prev = None
    for i in range(len(s)):
        prev = s[i] if prev is None else s[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def stoch_k(h, l, c, period=14):
    n = len(c); out = [None] * n
    for i in range(n):
        if i + 1 >= period:
            hh = max(h[i + 1 - period:i + 1]); ll = min(l[i + 1 - period:i + 1])
            out[i] = 50.0 if hh == ll else (c[i] - ll) / (hh - ll) * 100
    return out


def macd_hist(c):
    e12, e26 = ema(c, 12), ema(c, 26)
    line = [a - b for a, b in zip(e12, e26)]
    sig = ema(line, 9)
    return [line[i] - sig[i] for i in range(len(c))]


# --------------------------------------------------------------- candles
def completed_candles(market, need=200):
    """Return at least `need` COMPLETED 4h candles, oldest->newest.

    Upbit caps count at 200 per request, and the newest bar returned is still
    forming -- so asking for 200 yields only 199 completed ones. MA200 needs 200,
    hence the pagination: without it the BTC regime check silently aborts every
    run (the workflow still reports 'success' while doing nothing)."""
    now = datetime.now(timezone.utc)
    collected = {}
    to = None
    for _ in range(5):
        url = f"{BASE}/candles/minutes/240?market={market}&count=200"
        if to:
            url += "&to=" + urllib.parse.quote(to)
        batch = fetch(url)
        if not batch:
            break
        for r in batch:
            collected[r['candle_date_time_utc']] = r
        to = min(batch, key=lambda x: x['candle_date_time_utc'])['candle_date_time_utc']
        done = sum(1 for ts in collected
                   if (now - datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)).total_seconds() >= BAR_SECONDS)
        if done >= need:
            break
    rows = sorted(collected.values(), key=lambda x: x['candle_date_time_utc'])
    return [r for r in rows
            if (now - datetime.fromisoformat(r['candle_date_time_utc']).replace(tzinfo=timezone.utc)).total_seconds() >= BAR_SECONDS]


def kst_str(utc_str):
    return (datetime.fromisoformat(utc_str).replace(tzinfo=timezone.utc)
            .astimezone(KST).strftime('%m-%d %H:%M'))


def fmt_price(p):
    if p >= 1000:
        return f"{p:,.0f}"
    if p >= 1:
        return f"{p:,.2f}"
    return f"{p:.6f}".rstrip('0')


# ------------------------------------------------------------- telegram
def send_telegram(text):
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - would have sent:\n" + text)
        return False
    payload = urllib.parse.urlencode({
        'chat_id': chat_id, 'text': text, 'disable_web_page_preview': 'true'
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = json.loads(r.read().decode()).get('ok')
        print(f"Telegram sent: {ok}")
        return bool(ok)
    except Exception as e:
        print(f"Telegram send FAILED: {e}")
        return False


# ------------------------------------------------------------------ main
def main():
    now_kst = datetime.now(KST).strftime('%Y-%m-%d %H:%M')
    print(f"=== scan start {now_kst} KST ===")

    # ---- 1. regime gate ----
    btc = completed_candles("KRW-BTC", need=200)
    if len(btc) < 200:
        # Loud, not quiet: this is a data problem, not a "no signal" outcome.
        print(f"ERROR: only {len(btc)} completed BTC bars, need 200 for MA200.")
        send_telegram(f"⚠️ 업비트 스캐너 오류: BTC 봉 데이터 부족({len(btc)}/200). 국면 판정 불가로 이번 회차 건너뜀.")
        sys.exit(1)
    bc = [x['trade_price'] for x in btc]
    m200, m50 = sma(bc, 200), sma(bc, 50)
    i = len(bc) - 1
    above200 = m200[i] is not None and bc[i] > m200[i]
    ma50_over = m50[i] is not None and m200[i] is not None and m50[i] > m200[i]
    strong_bull = above200 and ma50_over
    print(f"BTC bar {kst_str(btc[i]['candle_date_time_utc'])} close={bc[i]:,.0f} "
          f"MA200={m200[i]:,.0f} MA50={m50[i]:,.0f} -> strong_bull={strong_bull}")

    if not strong_bull:
        print("Regime gate CLOSED (not strong_bull). No alert. Exiting quietly.")
        return

    # ---- 2. scan ----
    mk = fetch(f"{BASE}/market/all?isDetails=false")
    krw_all = [m['market'] for m in mk if m['market'].startswith('KRW-')
               and not any(s in m['market'] for s in ('USDT', 'USDC', 'DAI', 'USDG'))]

    # Restrict to the universe the rule was actually validated on: the top N KRW
    # markets by 24h traded value. The 71.6% win rate was measured on those only.
    # Alerting on an illiquid coin outside this set would attach a win rate to a
    # signal that was never tested there, and slippage on thin books breaks the
    # 0.2% cost assumption the backtest ran on.
    tickers = []
    for i in range(0, len(krw_all), 80):
        chunk = krw_all[i:i + 80]
        tickers.extend(fetch(f"{BASE}/ticker?markets=" + urllib.parse.quote(','.join(chunk))))
    tickers.sort(key=lambda t: -t['acc_trade_price_24h'])
    krw = [t['market'] for t in tickers[:UNIVERSE_SIZE]]
    print(f"Universe: top {len(krw)} of {len(krw_all)} KRW markets by 24h value "
          f"(rule was validated on this set only)")
    print(f"Scanning on last completed bar ...")

    hits = []
    failures = 0
    bar_ts = None
    for mkt in krw:
        try:
            c = completed_candles(mkt, 200)
            if len(c) < 100:
                continue
            close = [x['trade_price'] for x in c]
            high = [x['high_price'] for x in c]
            low = [x['low_price'] for x in c]
            k = stoch_k(high, low, close)
            mh = macd_hist(close)
            ma60 = sma(close, 60)
            j = len(close) - 1
            if j < 1 or ma60[j] is None or k[j] is None or k[j - 1] is None:
                continue
            if not (k[j - 1] < 30 <= k[j]):
                continue
            if not (mh[j - 1] < 0 <= mh[j]):
                continue
            if not (close[j] > ma60[j]):
                continue
            bar_ts = bar_ts or c[j]['candle_date_time_utc']
            hits.append({
                'market': mkt, 'price': close[j], 'k': k[j],
                'ma60_gap': (close[j] - ma60[j]) / ma60[j] * 100,
                'bar': c[j]['candle_date_time_utc'],
            })
            print(f"  SIGNAL {mkt} @ {close[j]}")
        except Exception as e:
            failures += 1

    print(f"Scan done: {len(hits)} signals, {failures} fetch failures")

    if not hits:
        print("No signals this bar. No alert sent.")
        return

    # ---- 3. alert ----
    hits.sort(key=lambda x: -x['ma60_gap'])
    lines = [f"🔔 업비트 신호 {len(hits)}건  ({kst_str(hits[0]['bar'])} 봉 마감 기준)", ""]
    for h in hits:
        tk = h['market'].replace('KRW-', '')
        p = h['price']
        lines.append(f"▪ {tk}  {fmt_price(p)}원")
        lines.append(f"   익절 {fmt_price(p * (1 + TP_PCT / 100))} / "
                     f"손절 {fmt_price(p * (1 - SL_PCT / 100))} / 최대 5일")
        lines.append(f"   %K {h['k']:.0f} · MA60대비 {h['ma60_gap']:+.1f}%")
    lines += [
        "",
        "규칙: 강세장(BTC>MA200 & MA50>MA200) + 스토캐스틱30돌파 + MACD0돌파 + 종가>MA60",
        "백테스트 3년: 승률 71.6% (n=67), 손익비 1.31, 기대값 +0.48%/거래",
        "⚠️ 2024-06~2025-03 구간에선 손실(-2.8%)을 낸 규칙. 조건 충족 사실 전달일 뿐 투자 조언 아님.",
    ]
    send_telegram("\n".join(lines))


if __name__ == "__main__":
    main()
