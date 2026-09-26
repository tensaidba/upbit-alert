"""
Upbit signal scanner -> Telegram alert.

RULE (walk-forward validated, 3 years, cluster-adjusted, after 0.2% costs):
  Entry (all three on the SAME completed 4h bar):
    - Stochastic %K(14) crosses UP through 30
    - MACD histogram (12,26,9) crosses UP through 0
    - close > MA60
  Regime gate: BTC 4h close > MA200 AND MA50 > MA200  (strong_bull only)
  Exit: TP +3% / SL -9% / max 30 bars (5 days)

SCOPE: every eligible KRW market is scanned, but signals are reported in two tiers,
  because the edge is confined to the top of the liquidity ranking (see tier_of):
    top 30  -> 🔔 validated    WR 75.9%, expectancy +0.54%  (breakeven WR 70.2%)
    31+     -> 🔎 reference    WR 59.7-63.3%, expectancy negative
  The unvalidated ones are shown because the user asked to see them, and labelled
  because acting on them the same way is what the backtest says loses money.

  Backtest (top 30): n=54 events, WR 75.9%, PF 1.34, expectancy +0.54%/trade.
  KNOWN WEAKNESS: lost money in the 2024-06~2025-03 window (50.0% WR, -2.84%).

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
MIN_RANKED = 100        # fewer than this many ranked markets means the data pull broke
VALUE_WINDOW_DAYS = 30  # rank on a 30-day average, NOT a live 24h snapshot (see ranked_markets)
MAX_REFERENCE_LINES = 12  # a market-wide turn can fire dozens of unvalidated signals at once
                          # and Telegram caps a message at 4096 chars; the rest are counted, not listed.
MIN_HISTORY_DAYS = 67   # the backtest required >=400 4h bars of history; new listings were excluded
TEST_ALERT = os.environ.get('TEST_ALERT', '').lower() == 'true'
# TEMPORARY: send a short confirmation on EVERY run, so the real schedule is visible
# instead of inferred. GitHub fires only 5-7 of 24 hourly schedules (measured
# 2026-09-13..25), and the daily report cannot show which hours were skipped.
# Turn off by setting the repo variable PING_EVERY_RUN to 'false'.
PING_EVERY_RUN = os.environ.get('PING_EVERY_RUN', 'false').lower() == 'true'
CATCHUP_BARS = 3        # re-check the last N completed bars to cover skipped runs
SENT_FILE = 'sent.json'  # committed back to the repo so dedup survives each run
HEARTBEAT_FILE = 'heartbeat.json'  # last date the daily status report was sent (KST)
RANKING_FILE = 'ranking.json'  # cached liquidity ranking, recomputed once per KST day
DAILY_REPORT_HOUR = 9   # KST hour from which the day's status report may go out


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


TIERS = (
    # (rank_max, icon, label, win_rate, profit_factor, expectancy, breakeven_wr)
    (UNIVERSE_SIZE, '🔔', '검증된 신호', 75.9, 1.34, +0.54, 70.2),
    (100,           '🔎', '참고용 (미검증)', 63.3, 0.87, -0.26, 66.6),
    (10 ** 9,       '🔎', '참고용 (미검증)', 59.7, 0.57, -1.19, 72.0),
)


def tier_of(rank):
    """Which liquidity tier a rank falls in. Tier 0 is the only validated one.

    Measured 2026-09-09 over all 261 KRW markets, 3 years, same rule and exit:
    the payoff ratio barely moves across tiers (0.39-0.50) -- what collapses is
    the win rate, 75.9% -> 59.7%. With a +3%/-9% exit the breakeven win rate is
    ~70%, so only the top tier clears it. That is why tier 1 and 2 signals go out
    labelled as unvalidated instead of as 🔔 signals."""
    for t, spec in enumerate(TIERS):
        if rank <= spec[0]:
            return t
    return len(TIERS) - 1


def ranked_markets(markets):
    """Every eligible KRW market, ordered by AVERAGE traded value over the last
    VALUE_WINDOW_DAYS days -- deliberately not the live 24h figure.

    Ranking on a 24h snapshot lets a single day's pump into the set: measured
    2026-09-09, a 24h ranking shared only 17 of 30 names with the universe the
    rule was validated on, so the scanner would have been alerting on a different
    universe than the 75.9% win rate came from. A 30-day average, plus the same
    listing-age floor the backtest used, reproduces 29 of those 30 names.

    The rank is recomputed every run, but the inputs are COMPLETED daily candles,
    so the result only actually changes once a day, at KST midnight; every run in
    between produces the identical ordering. And a 30-day mean moves slowly, so a
    name does not jump tiers on one day's volume -- except right at a boundary,
    where a coin genuinely sits on the line and flipping between 🔔 and 🔎 across a
    midnight is the honest display rather than a glitch.

    Returns [(rank, market)] with rank starting at 1, plus the fetch-failure count.
    Costs ~285 daily-candle requests (about a minute)."""
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
    return [(r, m) for r, (_, m) in enumerate(scored, 1)], failed


def cached_ranking(markets):
    """ranked_markets(), but computed once per KST day instead of once per run.

    The ranking averages COMPLETED daily candles, so its value cannot change until
    a new daily candle closes at KST midnight -- every run in between was paying
    ~285 requests (roughly 2 of the 3.5 local minutes) to recompute an identical
    list. Caching it is what makes a scan of all 262 markets cheap enough to run
    hourly. The file is committed back by the workflow, like the other ledgers.

    A cache miss (new day, missing or unreadable file) just recomputes, so a failed
    commit costs one slow run rather than a wrong universe."""
    today = datetime.now(KST).strftime('%Y-%m-%d')
    try:
        with open(RANKING_FILE, encoding='utf-8') as f:
            cached = json.load(f)
        if cached.get('date') == today and len(cached.get('markets', [])) >= MIN_RANKED:
            ranked = [(r, m) for r, m in enumerate(cached['markets'], 1)]
            print(f"Ranking: cached for {today} ({len(ranked)} markets, no re-fetch)")
            return ranked, 0, False
    except Exception:
        pass
    ranked, failed = ranked_markets(markets)
    if len(ranked) >= MIN_RANKED:
        try:
            with open(RANKING_FILE, 'w', encoding='utf-8') as f:
                json.dump({'date': today, 'markets': [m for _, m in ranked]}, f)
            print(f"ranking.json updated ({len(ranked)} markets for {today})")
        except Exception as e:
            print(f"could not write {RANKING_FILE}: {e}")
    return ranked, failed, True


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


def daily_report_due():
    """True once per KST day, on the first run at or after DAILY_REPORT_HOUR.

    Tracked in a committed file rather than its own daily cron on purpose:
    GitHub skips most scheduled runs (observed 2026-09-13: 7 fired out of 24),
    so a once-a-day cron would simply not fire on many days -- which is exactly
    the silence this report exists to rule out."""
    now = datetime.now(KST)
    if now.hour < DAILY_REPORT_HOUR:
        return False
    try:
        with open(HEARTBEAT_FILE, encoding='utf-8') as f:
            last = json.load(f).get('date')
    except Exception:
        last = None
    return last != now.strftime('%Y-%m-%d')


def mark_daily_report():
    """Record today, so the report goes out once and not on every later run."""
    try:
        with open(HEARTBEAT_FILE, 'w', encoding='utf-8') as f:
            json.dump({'date': datetime.now(KST).strftime('%Y-%m-%d')}, f)
        print('heartbeat.json updated')
    except Exception as e:
        print(f'could not write {HEARTBEAT_FILE}: {e}')


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


def send_run_ping(btc_bar, regime_ok, scanned, near, cached, funnel='', regime_status=''):
    """TEMPORARY per-run confirmation (see PING_EVERY_RUN).

    Deliberately carries the run's own clock time: the point is to show WHICH hours
    actually fired, which a bar timestamp cannot show (several runs share one bar).
    Returns True if it went out, so the caller can skip the daily report and not
    send two near-identical messages in the same run."""
    if not PING_EVERY_RUN:
        return False
    now = datetime.now(KST)
    if regime_ok:
        body = (f"BTC 국면: {regime_status}\n"
                f"검사 대상: {scanned}종{' (순위 캐시)' if cached else ' (순위 재계산)'}\n"
                f"기준봉: {kst_str(btc_bar)} 마감\n"
                f"{funnel}\n"
                f"  스토캐스틱 30돌파 {near['stoch']}건 / MACD 0돌파 {near['macd']}건 / "
                f"둘 다 같은 봉 {near['both']}건")
    else:
        body = (f"BTC 국면: strong_bull 아님 → 종목 스캔 안 함\n"
                f"기준봉: {kst_str(btc_bar)} 마감")
    return send_telegram(
        f"🧪 스캔 실행 확인 {now.strftime('%m-%d %H:%M')} KST\n\n"
        f"{body}\n\n"
        "※ 스케줄이 실제로 몇 시에 도는지 확인하려고 매 회차 보내는 임시 메시지입니다.\n"
        "실매매 신호가 아닙니다. 확인 끝나면 끕니다."
    )


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
        # The per-run ping already says the gate was closed, so it stands in for the
        # day's report rather than being sent alongside it.
        if send_run_ping(btc_bar, False, 0, None, False):
            if daily_report_due():
                mark_daily_report()
            return
        # Report once a day even here: a closed gate is a normal, common state, and
        # the point of the report is that silence never has to be interpreted.
        if daily_report_due():
            if send_telegram(
                "📋 업비트 스캐너 일일 점검\n\n"
                "BTC 국면: strong_bull 아님 → 알림 조건 닫힘\n"
                f"기준봉: {kst_str(btc_bar)} 마감\n"
                "결과: 국면 필터에서 막혀 종목 스캔 안 함\n\n"
                "이 메시지가 보이면 알림 경로는 정상입니다.\n"
                "규칙상 BTC가 강세장일 때만 신호를 찾습니다."):
                mark_daily_report()
        return

    # ---- 2. scan ----
    mk = fetch(f"{BASE}/market/all?isDetails=false")
    krw_all = [m['market'] for m in mk if m['market'].startswith('KRW-')
               and not any(s in m['market'] for s in ('USDT', 'USDC', 'DAI', 'USDG'))]

    # Scan EVERY eligible KRW market, but do not present them alike. A full-market
    # backtest (261 markets, 3 years, same rule and exit) measured 2026-09-09:
    #   top 30   WR 75.9%  payoff 0.42  expectancy +0.54%   <- breakeven WR is 70.4%
    #   31-100   WR 63.3%  payoff 0.50  expectancy -0.26%
    #   101+     WR 59.7%  payoff 0.39  expectancy -1.19%
    #   all 261  WR 58.9%  payoff 0.42  expectancy -1.03%
    # The payoff ratio is roughly constant across tiers -- what collapses is the win
    # rate, and with a +3%/-9% exit the breakeven win rate is 70.4%, so only the top
    # tier clears it. Treating all 261 as one pool turns +0.54% into -1.03% per trade,
    # so the tiers are kept visibly apart in the alert (see TIERS / tier_of).
    ranked, rank_failures, ranking_fresh = cached_ranking(krw_all)
    # Same principle as the BTC bar shortage above: a universe we could not build
    # is a data failure, not a quiet 'no signal'. Without this the run would scan
    # nothing, print 0 signals and report success.
    if len(ranked) < MIN_RANKED:
        print(f"ERROR: ranked only {len(ranked)} markets ({rank_failures} fetch failures).")
        send_telegram(f"⚠️ 업비트 스캐너 오류: 거래대금 순위를 {len(ranked)}종밖에 못 만들었습니다"
                      f"(조회 실패 {rank_failures}건). 대상 선정 불가로 이번 회차 건너뜀.")
        sys.exit(1)
    print(f"Universe: all {len(ranked)} eligible of {len(krw_all)} KRW markets "
          f"(top {UNIVERSE_SIZE} = validated tier)")
    print(f"Scanning on last completed bar ...")

    already_sent = load_sent()
    new_keys = []
    hits = []
    failures = 0
    checked = 0
    insufficient = 0
    # Funnel counters. "0 signals" has several very different causes -- nothing
    # matched, something matched but BTC was not strong_bull on that bar, or it
    # matched and was already alerted -- and collapsing them into one number is
    # what made an earlier silent failure look like a normal quiet day.
    raw_matches = regime_matches = duplicates = 0
    near = {'stoch': 0, 'macd': 0, 'both': 0}   # for the daily report
    for rank, mkt in ranked:
        try:
            c = completed_candles(mkt, 200)
            if len(c) < 100:
                insufficient += 1
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
                checked += 1
                c1 = k[j - 1] < 30 <= k[j]
                c2 = mh[j - 1] < 0 <= mh[j]
                c3 = close[j] > ma60[j]
                near['stoch'] += c1
                near['macd'] += c2
                near['both'] += c1 and c2
                if not (c1 and c2 and c3):
                    continue
                raw_matches += 1
                ts = c[j]['candle_date_time_utc']
                if not regime.get(ts):
                    continue        # BTC was not strong_bull on THAT bar
                regime_matches += 1
                key = f"{mkt}@{ts}"
                if key in already_sent:
                    duplicates += 1
                    print(f"  (skip, already alerted) {key}")
                    continue
                new_keys.append(key)
                hits.append({
                    'market': mkt, 'price': close[j], 'k': k[j],
                    'ma60_gap': (close[j] - ma60[j]) / ma60[j] * 100,
                    'bar': ts, 'rank': rank, 'tier': tier_of(rank),
                })
                print(f"  SIGNAL {mkt} (rank {rank}, tier {tier_of(rank)}) "
                      f"@ {close[j]} (bar {ts})")
        except Exception as e:
            failures += 1

    print(f"Scan done: {len(hits)} signals, {failures} fetch failures")

    funnel = (f"조건 충족 {raw_matches}건 → BTC 필터 통과 {regime_matches}건 → "
              f"기발송 제외 {duplicates}건 → 신규 {len(hits)}건\n"
              f"조회 실패 {failures}종 / 이력 부족 {insufficient}종 / 순위 실패 {rank_failures}건")
    print(funnel)

    # A scan that evaluated NOTHING is a data failure, not a quiet day. Without this
    # the run prints "0 signals", exits 0 and the workflow goes green -- the same
    # pattern as the MA200 shortage and the empty-universe bug before it.
    if checked == 0:
        print("ERROR: no evaluable market bars; cannot conclude 'no signal'.")
        send_telegram("⚠️ 업비트 스캐너: 검사 가능한 종목 봉이 0개라 신호 유무를 "
                      "판정할 수 없습니다.\n\n" + funnel)
        sys.exit(1)

    # The gate opens if ANY of the last CATCHUP_BARS bars was strong_bull, so the
    # newest bar can be non-bull while the run still scans. Saying "strong_bull ✅"
    # in that case is simply false.
    latest_bull = regime[btc_bar]
    regime_status = ("strong_bull ✅ (알림 조건 열림)" if latest_bull else
                     "기준봉은 strong_bull 아님 · 최근 검사봉 중 강세장이 있어 스캔함")
    incomplete = bool(failures or insufficient or rank_failures)
    result_status = ("검사 불완전 · 조회 성공 범위에서 신규 0건" if incomplete else
                     "검사 완료 · 신규 0건")

    if not hits:
        print(result_status)
        if send_run_ping(btc_bar, True, len(ranked), near, not ranking_fresh,
                         funnel, regime_status):
            if daily_report_due():
                mark_daily_report()
            return
        if daily_report_due():
            if send_telegram(
                "📋 업비트 스캐너 일일 점검\n\n"
                f"BTC 국면: {regime_status}\n"
                f"검사 대상: 원화마켓 {len(ranked)}종 전체 "
                f"(상위 {UNIVERSE_SIZE}종만 🔔 검증된 신호)\n"
                f"기준봉: {kst_str(btc_bar)} 마감\n"
                f"결과: {result_status}\n{funnel}\n\n"
                f"근접 상황 (최근 {CATCHUP_BARS}봉 · {checked}개 조합)\n"
                f"  스토캐스틱 30돌파 {near['stoch']}건\n"
                f"  MACD 0돌파 {near['macd']}건\n"
                f"  둘 다 같은 봉 {near['both']}건 ← 여기가 0이면 신호 없음\n\n"
                "이 메시지가 보이면 알림 경로가 정상입니다.\n"
                "🔔 검증된 신호는 2~3주에 1회, 🔎 참고용은 하루 1건 안팎입니다."):
                mark_daily_report()
        if TEST_ALERT:
            # Diagnostic ping: proves the Secrets are wired even on a quiet bar.
            # Without this there is no way to tell "no signal" apart from "broken".
            send_telegram(
                "🩺 진단 실행 (수동)\n\n"
                f"BTC 국면: 기준봉 strong_bull={regime[btc_bar]} (봉별로 판정)\n"
                f"검사 대상: 원화마켓 {len(ranked)}종 전체 (거래대금 30일 평균 순위)\n"
                f"  🔔 검증된 신호: 상위 {UNIVERSE_SIZE}종\n"
                f"  🔎 참고용: {UNIVERSE_SIZE + 1}위 이하\n"
                f"기준봉: {kst_str(btc_bar)} 마감\n"
                f"결과: {result_status}\n{funnel}\n\n"
                "이 메시지가 보이면 GitHub Actions → 텔레그램 연결이 정상입니다."
            )
        return

    # ---- 3. alert ----
    if send_telegram(build_alert(hits, funnel if incomplete or duplicates else '')):
        save_sent(already_sent | set(new_keys))
        # A real alert already proves the channel works, so skip today's report.
        if daily_report_due():
            mark_daily_report()
    else:
        print("Telegram failed - NOT marking these signals as sent, so the next run retries.")


def build_alert(hits, funnel=''):
    """Render the alert text. Separate from main() so the layout can be checked
    without hitting the network or sending anything.

    `funnel` is appended only when the scan was incomplete or suppressed duplicates,
    so a normal alert stays clean but a partial one never looks complete."""
    # Two tiers, deliberately not interleaved. The same three conditions fired for
    # every name here, but only the top tier's win rate clears the +3%/-9% breakeven,
    # so mixing them into one list would lend the unvalidated ones a 75.9% that was
    # never measured for them.
    hits = sorted(hits, key=lambda x: (x['tier'], x['bar'], -x['ma60_gap']))
    verified = [h for h in hits if h['tier'] == 0]
    reference = [h for h in hits if h['tier'] != 0]

    # The summary line only earns its place when both kinds are present; with one
    # kind it just repeats the section header below it.
    lines = []
    if verified and reference:
        lines += [f"🔔 검증된 신호 {len(verified)}건 · 🔎 참고용 {len(reference)}건", ""]

    if verified:
        spec = TIERS[0]
        lines.append(f"🔔 검증된 신호 {len(verified)}건 — 거래대금 상위 {UNIVERSE_SIZE}종")
        for h in verified:
            p = h['price']
            lines.append(f"{h['market'].replace('KRW-', '')}  {fmt_price(p)}원   "
                         f"({kst_str(h['bar'])} 봉 · {h['rank']}위)")
            lines.append(f"   익절 {fmt_price(p * (1 + TP_PCT / 100))} / "
                         f"손절 {fmt_price(p * (1 - SL_PCT / 100))} / 최대 5일")
            lines.append(f"   %K {h['k']:.0f} · MA60대비 {h['ma60_gap']:+.1f}%")
        lines.append(f"→ 이 구간 3년 성적: 승률 {spec[3]}%, PF {spec[4]}, "
                     f"기대값 {spec[5]:+.2f}% (손익분기 {spec[6]}%)")
        lines.append("")

    if reference:
        lines.append(f"🔎 참고용 {len(reference)}건 — 검증범위 밖, 백테스트가 보증하지 않음")
        for h in reference[:MAX_REFERENCE_LINES]:
            p = h['price']
            lines.append(f"{h['market'].replace('KRW-', '')}  {fmt_price(p)}원  "
                         f"{h['rank']}위 · {kst_str(h['bar'])} 봉 · MA60 {h['ma60_gap']:+.0f}%")
        if len(reference) > MAX_REFERENCE_LINES:
            lines.append(f"   … 외 {len(reference) - MAX_REFERENCE_LINES}건 (생략)")
        # One stats line per tier actually present, so the numbers shown always
        # belong to the names listed above them.
        for t in sorted({h['tier'] for h in reference}):
            spec = TIERS[t]
            lo = TIERS[t - 1][0] + 1
            rng = f"{lo}~{spec[0]}위" if spec[0] < 10 ** 8 else f"{lo}위 이하"
            lines.append(f"→ {rng} 3년 성적: 승률 {spec[3]}%, PF {spec[4]}, "
                         f"기대값 {spec[5]:+.2f}% (손익분기 {spec[6]}%) ⚠️ 마이너스")
        lines.append("")

    lines += [
        "규칙: 강세장(BTC>MA200 & MA50>MA200) + 스토캐스틱30돌파 + MACD0돌파 + 종가>MA60",
        "순위: 일봉 30일 평균 거래대금 (KST 자정에 하루 한 번 갱신)",
        "※ 구간 분류에 현재 거래대금 순위를 과거에 소급 적용 — 순수 표본외 성적은 아님",
        "⚠️ 2024-06~2025-03 구간에선 상위 30종도 손실(-2.8%). 조건 충족 사실 전달일 뿐 투자 조언 아님.",
    ]
    if funnel:
        lines += ["", funnel]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
