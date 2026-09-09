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
UNIVERSE_SIZE = 30      # rule was validated on the top 30 KRW markets by traded value
VALUE_WINDOW_DAYS = 30  # rank on a 30-day average, NOT a live 24h snapshot (see top_universe)
MIN_HISTORY_DAYS = 67   # the backtest required >=400 4h bars of history; new listings were excluded
TEST_ALERT = os.environ.get('TEST_ALERT', '').lower() == 'true'
CATCHUP_BARS = 3        # re-check the last N completed bars to cover skipped runs
SENT_FILE = 'sent.json'  # committed back to the repo so dedup survives each run


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


def btc_regime_by_bar(btc):
    """strong_bull per BAR, keyed by that bar's UTC timestamp.

    The catch-up window re-checks up to CATCHUP_BARS old bars, and the backtest
    gated every entry on the regime AT THAT BAR. A single latest-bar flag applied
    to all of them would admit entries the backtest never took -- if BTC turned
    strong_bull only this bar, a signal from 12h ago would still pass."""
    c = [x['trade_price'] for x in btc]
    m200, m50 = sma(c, 200), sma(c, 50)
    return {btc[i]['candle_date_time_utc']:
            bool(m200[i] is not None and m50[i] is not None
                 and c[i] > m200[i] and m50[i] > m200[i])
            for i in range(len(btc))}


def top_universe(markets):
    """The top UNIVERSE_SIZE KRW markets by AVERAGE traded value over the last
    VALUE_WINDOW_DAYS days -- deliberately not the live 24h figure.

    Ranking on a 24h snapshot lets a single day's pump into the set: measured
    2026-09-09, a 24h ranking shared only 17 of 30 names with the universe the
    rule was validated on, so the scanner would have been alerting on a different
    universe than the 75.9% win rate came from. A 30-day average, plus the same
    listing-age floor the backtest used, reproduces 29 of those 30 names.

    Costs ~285 daily-candle requests (about a minute) and replaces a full scan of
    284 markets (about ten), so the run gets faster, not slower."""
    today_kst = datetime.now(KST).strftime('%Y-%m-%d')
    scored = []
    too_new = failed = 0
    for m in markets:
        try:
            rows = fetch(f"{BASE}/candles/days?market={m}&count={MIN_HISTORY_DAYS + 1}")
        except Exception:
            failed += 1                       # counted, not swallowed -- see the check in main()
            continue
        # Drop today's candle only if it IS today's. A market with no trades yet
        # today simply has no partial candle, and slicing it off blindly would
        # throw away a completed day and push the coin under the history floor.
        if rows and rows[0]['candle_date_time_kst'][:10] == today_kst:
            rows = rows[1:]
        if len(rows) < MIN_HISTORY_DAYS:
            too_new += 1                      # listed too recently to be in the backtest
            continue
        window = rows[:VALUE_WINDOW_DAYS]
        scored.append((sum(r['candle_acc_trade_price'] for r in window) / len(window), m))
    scored.sort(reverse=True)
    print(f"Ranked {len(scored)} markets by {VALUE_WINDOW_DAYS}d average value "
          f"({too_new} skipped: less than {MIN_HISTORY_DAYS} days listed, "
          f"{failed} fetch failures)")
    return [m for _, m in scored[:UNIVERSE_SIZE]], failed


def load_sent():
    """Keys of signals already alerted, so the catch-up overlap doesn't repeat them."""
    try:
        with open(SENT_FILE, encoding='utf-8') as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_sent(keys):
    """Keep only recent keys; the file is committed back by the workflow."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        kept = sorted(k for k in keys if k.split('@', 1)[-1] >= cutoff)
        with open(SENT_FILE, 'w', encoding='utf-8') as f:
            json.dump(kept, f, indent=0)
        print(f"sent.json updated ({len(kept)} keys)")
    except Exception as e:
        print(f"could not write {SENT_FILE}: {e}")


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
    regime = btc_regime_by_bar(btc)
    i = len(bc) - 1
    # Keep the bar's timestamp in its own name: `i` gets reused as a loop counter
    # further down (the scan loop), which silently pointed this at the wrong bar.
    btc_bar = btc[i]['candle_date_time_utc']
    print(f"BTC bar {kst_str(btc_bar)} close={bc[i]:,.0f} "
          f"MA200={m200[i]:,.0f} MA50={m50[i]:,.0f} -> strong_bull={regime[btc_bar]}")

    # The gate is per-bar from here on; skip the whole run only when NONE of the
    # bars the catch-up window can still alert on were strong_bull.
    window = [x['candle_date_time_utc'] for x in btc[-CATCHUP_BARS:]]
    if not any(regime.get(t) for t in window):
        print(f"Regime gate CLOSED on all of the last {CATCHUP_BARS} bars. Exiting quietly.")
        return

    # ---- 2. scan ----
    mk = fetch(f"{BASE}/market/all?isDetails=false")
    krw_all = [m['market'] for m in mk if m['market'].startswith('KRW-')
               and not any(s in m['market'] for s in ('USDT', 'USDC', 'DAI', 'USDG'))]

    # Scan ONLY the universe the rule was validated on. A full-market backtest
    # (261 markets, 3 years, same rule) showed the edge does not survive outside it:
    #   top 30   WR 75.9%  payoff 0.42  expectancy +0.54%   <- breakeven WR is 70.4%
    #   31-100   WR 63.3%  payoff 0.50  expectancy -0.26%
    #   101+     WR 59.7%  payoff 0.39  expectancy -1.19%
    #   all 261  WR 58.9%  payoff 0.42  expectancy -1.03%
    # The payoff ratio is roughly constant across tiers -- what collapses is the win
    # rate, and with a +3%/-9% exit the breakeven win rate is 70.4%, so only the top
    # tier clears it. Widening the universe turns +0.54% into -1.03% per trade.
    krw, rank_failures = top_universe(krw_all)
    # Same principle as the BTC bar shortage above: a universe we could not build
    # is a data failure, not a quiet 'no signal'. Without this the run would scan
    # nothing, print 0 signals and report success.
    if len(krw) < UNIVERSE_SIZE:
        print(f"ERROR: ranked only {len(krw)} markets ({rank_failures} fetch failures).")
        send_telegram(f"⚠️ 업비트 스캐너 오류: 거래대금 순위를 {len(krw)}종밖에 못 만들었습니다"
                      f"(조회 실패 {rank_failures}건). 대상 선정 불가로 이번 회차 건너뜀.")
        sys.exit(1)
    print(f"Universe: top {len(krw)} of {len(krw_all)} KRW markets (the validated set)")
    print(f"Scanning on last completed bar ...")

    already_sent = load_sent()
    new_keys = []
    hits = []
    failures = 0
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
            # Check the last CATCHUP_BARS completed bars, not just the newest one.
            # GitHub's scheduled runs are best-effort: they get delayed by hours and
            # sometimes skipped entirely. A signal is fixed at its bar's close, so a
            # skipped run would silently lose it. `already_sent` prevents the overlap
            # from re-alerting the same signal on the next run.
            for j in range(len(close) - CATCHUP_BARS, len(close)):
                if j < 1 or ma60[j] is None or k[j] is None or k[j - 1] is None:
                    continue
                if not (k[j - 1] < 30 <= k[j]):
                    continue
                if not (mh[j - 1] < 0 <= mh[j]):
                    continue
                if not (close[j] > ma60[j]):
                    continue
                ts = c[j]['candle_date_time_utc']
                if not regime.get(ts):
                    continue        # BTC was not strong_bull on THAT bar
                key = f"{mkt}@{ts}"
                if key in already_sent:
                    print(f"  (skip, already alerted) {key}")
                    continue
                new_keys.append(key)
                hits.append({
                    'market': mkt, 'price': close[j], 'k': k[j],
                    'ma60_gap': (close[j] - ma60[j]) / ma60[j] * 100,
                    'bar': ts,
                })
                print(f"  SIGNAL {mkt} @ {close[j]} (bar {ts})")
        except Exception as e:
            failures += 1

    print(f"Scan done: {len(hits)} signals, {failures} fetch failures")

    if not hits:
        print("No signals this bar. No alert sent.")
        if TEST_ALERT:
            # Diagnostic ping: proves the Secrets are wired even on a quiet bar.
            # Without this there is no way to tell "no signal" apart from "broken".
            send_telegram(
                "🩺 진단 실행 (수동)\n\n"
                f"BTC 국면: 기준봉 strong_bull={regime[btc_bar]} (봉별로 판정)\n"
                f"검사 대상: 원화마켓 상위 {len(krw)}종 (거래대금 30일 평균)\n"
                f"기준봉: {kst_str(btc_bar)} 마감\n"
                f"결과: 조건 충족 0건 → 평상시라면 알림 없음\n\n"
                "이 메시지가 보이면 GitHub Actions → 텔레그램 연결이 정상입니다.\n"
                "이 규칙은 연 22건(2~3주에 1회) 수준이라 조용한 날이 대부분입니다."
            )
        return

    # ---- 3. alert ----
    hits.sort(key=lambda x: (x['bar'], -x['ma60_gap']))
    lines = [f"🔔 업비트 신호 {len(hits)}건", ""]
    for h in hits:
        tk = h['market'].replace('KRW-', '')
        p = h['price']
        lines.append(f"{tk}  {fmt_price(p)}원   ({kst_str(h['bar'])} 봉)")
        lines.append(f"   익절 {fmt_price(p * (1 + TP_PCT / 100))} / "
                     f"손절 {fmt_price(p * (1 - SL_PCT / 100))} / 최대 5일")
        lines.append(f"   %K {h['k']:.0f} · MA60대비 {h['ma60_gap']:+.1f}%")
    lines += [
        "",
        "규칙: 강세장(BTC>MA200 & MA50>MA200) + 스토캐스틱30돌파 + MACD0돌파 + 종가>MA60",
        "대상: 거래대금 상위 30종(30일 평균) — 규칙이 검증된 범위",
        "이 범위 3년 백테스트: 승률 75.9% (클러스터 54건), PF 1.34, 기대값 +0.54%/거래",
        "※ 구간 분류에 현재 거래대금 순위를 과거에 소급 적용 — 순수 표본외 성적은 아님",
        "⚠️ 2024-06~2025-03 구간에선 손실(-2.8%)을 낸 규칙. 조건 충족 사실 전달일 뿐 투자 조언 아님.",
    ]
    if send_telegram("\n".join(lines)):
        save_sent(already_sent | set(new_keys))
    else:
        print("Telegram failed - NOT marking these signals as sent, so the next run retries.")


if __name__ == "__main__":
    main()
