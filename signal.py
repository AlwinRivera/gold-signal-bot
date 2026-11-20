"""
gold-signal-bot — XAU/USD EMA Pullback Signal Scanner
Runs hourly via GitHub Actions, sends Telegram alert when setup fires.
"""

import os
import math
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

TD_KEY     = os.environ["TD_KEY"]
TG_TOKEN   = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
SHEET_URL    = os.environ.get("SHEET_URL", "")
ACCOUNT_SIZE = float(os.environ.get("ACCOUNT_SIZE", "10000"))
RISK_PCT     = float(os.environ.get("RISK_PCT", "1.0"))

PHT = timezone(timedelta(hours=8))

def utc_hour(utc_str):
    dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.hour

def in_session(h):
    return (6 <= h <= 9) or (12 <= h <= 15)

def fetch_news_events():
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        events = []
        for event in root.findall("event"):
            currency = event.findtext("country", "")
            impact   = event.findtext("impact", "")
            if currency not in ("USD", "XAU") or impact != "High":
                continue
            date_str = event.findtext("date", "")
            time_str = event.findtext("time", "")
            if not date_str or not time_str or time_str in ("All Day", "Tentative"):
                continue
            try:
                # The feed's times are already UTC (checked: US Retail Sales, 8:30 AM New York,
                # is listed as 1:30pm in November). No daylight-saving conversion needed.
                naive = datetime.strptime(date_str + " " + time_str, "%m-%d-%Y %I:%M%p")
                dt_utc = naive.replace(tzinfo=timezone.utc)
                events.append(dt_utc)
                print("News: " + event.findtext("title", "") + " at " + dt_utc.strftime("%a %H:%M UTC"))
            except Exception:
                continue
        return events
    except Exception as e:
        print("News filter unavailable: " + str(e))
        return []

def is_near_news(candle_time_str, events, buffer_minutes=30):
    if not events:
        return False
    # Entry happens at the candle's close, one hour after its start
    entry_dt = datetime.strptime(candle_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) + timedelta(hours=1)
    buf = timedelta(minutes=buffer_minutes)
    for ev in events:
        if abs(entry_dt - ev) <= buf:
            return True
    return False

def market_open(dt):
    """Same rule as the backtest: gold is shut Saturday and Sunday before 22:00 UTC."""
    return not (dt.weekday() == 5 or (dt.weekday() == 6 and dt.hour < 22))

def calc_ema(closes, period):
    k = 2 / (period + 1)
    ema = [None] * len(closes)
    if len(closes) < period:
        return ema
    ema[period - 1] = sum(closes[:period]) / period
    for i in range(period, len(closes)):
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k)
    return ema

def fetch_candles(n=120, interval="1h"):
    url = ("https://api.twelvedata.com/time_series"
           "?symbol=XAU/USD&interval=" + interval + "&outputsize=" + str(n) + "&timezone=UTC&apikey=" + TD_KEY)
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "error":
        raise Exception("Twelve Data error: " + data.get("message", ""))
    candles = list(reversed(data["values"]))
    out = [{"t": c["datetime"], "o": float(c["open"]), "h": float(c["high"]),
            "l": float(c["low"]), "c": float(c["close"])} for c in candles]
    # Drop the still-forming candle: only trade on fully closed candles
    span = timedelta(hours=4) if interval == "4h" else timedelta(hours=1)
    last_start = datetime.strptime(out[-1]["t"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    if last_start + span > datetime.now(timezone.utc):
        out = out[:-1]
    # Twelve Data includes weekend quotes no broker fills; drop them (the backtest does too)
    return [c for c in out
            if market_open(datetime.strptime(c["t"], "%Y-%m-%d %H:%M:%S"))]

def get_4h_trend():
    """Returns 'bull', 'bear', or 'neutral' based on 4H 20/50 EMA alignment."""
    try:
        candles_4h = fetch_candles(100, interval="4h")[-60:]
        closes_4h = [c["c"] for c in candles_4h]
        e20_4h = calc_ema(closes_4h, 20)
        e50_4h = calc_ema(closes_4h, 50)
        idx = len(candles_4h) - 1
        if e20_4h[idx] is None or e50_4h[idx] is None:
            return "neutral"
        separation = abs(e20_4h[idx] - e50_4h[idx]) / e50_4h[idx]
        if separation < 0.001:  # less than 0.1% apart = too close to call
            return "neutral"
        if e20_4h[idx] > e50_4h[idx]:
            return "bull"
        elif e20_4h[idx] < e50_4h[idx]:
            return "bear"
        return "neutral"
    except Exception as e:
        print("4H trend fetch failed: " + str(e))
        return "neutral"  # fail open: don't block signals if 4H data unavailable

def detect_signal(candles, news_events):
    closes = [c["c"] for c in candles]
    e20 = calc_ema(closes, 20)
    e50 = calc_ema(closes, 50)
    idx = len(candles) - 1
    cur = candles[idx]
    if e20[idx] is None or e50[idx] is None:
        return None
    h = utc_hour(cur["t"])
    if not in_session(h):
        return None
    # Twelve Data includes weekend quotes for gold; the real market is shut Sat to Sun ~22:00 UTC
    if datetime.strptime(cur["t"], "%Y-%m-%d %H:%M:%S").weekday() >= 5:
        return None
    if is_near_news(cur["t"], news_events):
        print("Skipping - news within 30 min of " + cur["t"])
        return None
    # 4H trend filter: only trade in direction of higher timeframe trend
    htf_trend = get_4h_trend()
    ev = e20[idx]; e5 = e50[idx]
    bull_trend = ev > e5; bear_trend = ev < e5
    if htf_trend == "bear" and bull_trend:
        print("Skipping BUY setup - 4H trend is bearish")
        return None
    if htf_trend == "bull" and bear_trend:
        print("Skipping SELL setup - 4H trend is bullish")
        return None
    if not bull_trend and not bear_trend:
        return None
    candle_size = cur["h"] - cur["l"]
    body = abs(cur["c"] - cur["o"])
    if candle_size < 2 or body / candle_size < 0.35:
        return None
    tol = ev * 0.001
    direction = None
    if bull_trend and cur["l"] <= ev + tol and cur["c"] > ev and cur["c"] > cur["o"]:
        direction = "BUY"
    if bear_trend and cur["h"] >= ev - tol and cur["c"] < ev and cur["c"] < cur["o"]:
        direction = "SELL"
    if not direction:
        return None
    sl_buf = max(candle_size * 0.7, 4.0)
    sl = ev - sl_buf if direction == "BUY" else ev + sl_buf
    if abs(cur["c"] - sl) < 6.0:
        return None
    r1 = abs(cur["c"] - sl)
    tp = cur["c"] + r1 * 4.0 if direction == "BUY" else cur["c"] - r1 * 4.0
    trend_label = "20 EMA > 50 EMA (uptrend)" if bull_trend else "20 EMA < 50 EMA (downtrend)"
    htf_label = "4H " + htf_trend.upper() if htf_trend != "neutral" else "4H neutral"
    stop_dist = abs(cur["c"] - sl)
    risk_amt = ACCOUNT_SIZE * (RISK_PCT / 100)
    lot_size = math.floor(risk_amt / (stop_dist * 100) * 100 + 1e-9) / 100  # round down
    lot_size = max(0.01, lot_size)  # minimum 0.01 lots
    real_risk = lot_size * 100 * stop_dist  # can exceed risk_amt when the stop is wide
    return {"direction": direction, "entry": cur["c"], "sl": sl, "tp": tp,
            "sl_pts": round(stop_dist, 1), "ema20": ev,
            "time": cur["t"], "trend": trend_label, "htf": htf_label,
            "lot_size": lot_size, "risk_amt": round(risk_amt, 2), "real_risk": round(real_risk, 2)}

def send_telegram(text):
    api = "https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage"
    r = requests.post(api, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
                      timeout=10)
    if r.status_code == 400:
        # Markdown parse failure: resend as plain text so the message still arrives
        r = requests.post(api, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
    if not r.ok:
        raise Exception("Telegram send failed: HTTP " + str(r.status_code))

def track_results(candles_raw, news_events=None):
    """Rebuild recent signals with the backtest engine (same rules, 4R) and resolve them.
    Stateless: every run recomputes the last few days from candle history."""
    import backtest as bt
    def conv(cs):
        out = [{"t": datetime.strptime(c["t"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
                "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]} for c in cs]
        return [c for c in out if bt.market_open(c["t"])]
    c1 = conv(candles_raw)
    c4 = conv(fetch_candles(400, interval="4h"))
    # One position at a time, exactly like the backtest. News-skipped candles never open a trade.
    skip = lambda t: is_near_news(t.strftime("%Y-%m-%d %H:%M:%S"), news_events or [])
    return bt.run(c1, c4, "live", "both", 4.0, None, True, include_open=True, skip=skip)

def send_result(t, losses_today):
    risk_amt = ACCOUNT_SIZE * (RISK_PCT / 100)
    head = {"WIN": "\u2705 WIN", "LOSS": "\u274c LOSS"}.get(t["result"], "\u23f1 TIME LIMIT: CLOSE THIS TRADE NOW")
    usd = t["r"] * risk_amt
    msg = (head + " - XAU/USD " + t["dir"] + "\n"
           "Signal candle: " + t["time"].strftime("%Y-%m-%d %H:%M") + " UTC\n"
           "Entry " + f"{t['entry']:.2f}" + "  SL " + f"{t['sl']:.2f}" + "  TP " + f"{t['tp']:.2f}" + "\n"
           "Result: " + ("+" if t["r"] >= 0 else "") + f"{t['r']:.2f}" + "R (about " +
           ("+" if usd >= 0 else "-") + "$" + f"{abs(usd):.0f}" + ")\n"
           "Losses today: " + str(losses_today) + " of 2")
    send_telegram(msg)

def close_by(candle_time_str, bars=72):
    """Time limit used in the backtest: 72 market-open hourly candles after entry.
    Returns the PHT time when the trade should be closed if neither SL nor TP was hit."""
    t = datetime.strptime(candle_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    n = 0
    while n < bars:
        t += timedelta(hours=1)
        if market_open(t):
            n += 1
    return (t + timedelta(hours=1)).astimezone(PHT)

def send_signal(signal, skip_reason=None):
    arrow = "\U0001f7e2 \u25b2" if signal["direction"] == "BUY" else "\U0001f534 \u25bc"
    stop = ("\u26d4 " + skip_reason + " DO NOT TAKE THIS ONE.\n" if skip_reason else "")
    msg = (stop + arrow + " *XAU/USD " + signal["direction"] + " SETUP*\n"
           "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
           "\U0001f4cd Entry:  `" + f"{signal['entry']:.2f}" + "`\n"
           "\U0001f6d1 Stop:   `" + f"{signal['sl']:.2f}" + "` (" + str(signal["sl_pts"]) + " pts)\n"
           "\U0001f3af TP:     `" + f"{signal['tp']:.2f}" + "` (4R, hold up to 3 days)\n"
           "\u23f1 Close by: " + close_by(signal["time"]).strftime("%a %d %b, %I:%M %p") + " PHT if SL/TP not hit\n"
           "💰 Size:   `" + str(signal["lot_size"]) + " lots` ($" + str(signal["risk_amt"]) + " risk)\n" +
           ("Note: wide stop. 0.01 lots is the smallest size, so this trade risks about $" +
            f"{signal['real_risk']:.0f}" + " (" + f"{signal['real_risk'] / ACCOUNT_SIZE * 100:.2f}" + "% of the account).\n"
            if signal["real_risk"] > signal["risk_amt"] * 1.05 else "") +
           "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
           "\U0001f4ca 20 EMA: `" + f"{signal['ema20']:.2f}" + "`\n"
           "\U0001f4c8 Trend:  " + signal["trend"] + "\n"
           "\U0001f550 Candle: `" + signal["time"] + " UTC`\n"
           "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
           "_Strategy: EMA Pullback · 1H · XAUPullbackBot_")
    send_telegram(msg)
    print("Signal sent: " + signal["direction"] + " @ " + f"{signal['entry']:.2f}")

def log_to_sheet(signal):
    if not SHEET_URL:
        return
    try:
        payload = {
            "timestamp":   datetime.now(PHT).strftime("%Y-%m-%d %H:%M PHT"),
            "direction":   signal["direction"],
            "entry":       signal["entry"],
            "sl":          signal["sl"],
            "tp":          signal["tp"],
            "ema20":       signal["ema20"],
            "trend":       signal["trend"],
            "htf_trend":   signal["htf"],
            "lot_size":    signal["lot_size"],
            "risk_amt":    signal["risk_amt"],
            "candle_time": signal["time"]
        }
        requests.post(SHEET_URL, json=payload, timeout=10)
        print("Logged to Google Sheet.")
    except Exception as e:
        print("Sheet log failed: " + str(e))

def send_daily_summary():
    today_pht = datetime.now(PHT).strftime("%Y-%m-%d")
    msg = ("\U0001f4ca *Daily Summary \u2014 " + today_pht + "*\n"
           "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
           "\U0001f550 NY session closed\n"
           "\U0001f4cb Check your signal log sheet for today\'s trades\n"
           "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
           "_XAUPullbackBot · EMA Pullback 1H_")
    send_telegram(msg)
    print("Daily summary sent.")

def main():
    now_pht = datetime.now(PHT).strftime("%Y-%m-%d %H:%M PHT")
    print("Scanning XAU/USD 1H " + now_pht)
    candles_all = fetch_candles(1000)  # ~6 weeks, so the trade tracker has a stable history
    candles = candles_all[-120:]  # detection uses the same 120-candle window as before
    latest = candles[-1]["t"]
    print("Fetched " + str(len(candles)) + " candles. Latest: " + latest)
    news_events = fetch_news_events()
    print("Found " + str(len(news_events)) + " high-impact news events this week.")

    # Freshness check: warn if candle older than 2 hours
    now_utc = datetime.now(timezone.utc)
    latest_dt = datetime.strptime(latest, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    age_hours = (now_utc - latest_dt).total_seconds() / 3600
    # count only hours the market was open, so the Sunday reopen does not trigger a warning
    open_hours = sum(1 for k in range(1, int(age_hours) + 1)
                     if market_open(latest_dt + timedelta(hours=k)))
    if open_hours > 2 and market_open(now_utc):
        stale_msg = ("WARNING: Stale candle data\n"
                     "Latest candle is " + str(round(age_hours, 1)) + "h old\n"
                     "`" + latest + " UTC`\n"
                     "_Twelve Data may be delayed_")
        send_telegram(stale_msg)
        print("Warning: stale candle - " + str(round(age_hours, 1)) + "h old")

    # Results of earlier signals + daily stop (2 losses in a UTC day)
    losses_today = 0
    in_trade = None
    try:
        tracked = track_results(candles_all, news_events)
        for t in tracked[-3:]:
            print("Tracked: " + t["time"].strftime("%Y-%m-%d %H:%M") + " " + t["dir"] + " " + t["result"] +
                  " " + str(round(t["r"], 2)) + "R" +
                  (", closed " + t["exit"].strftime("%Y-%m-%d %H:%M") if t["exit"] else ", still open"))
        closed = [t for t in tracked if t["exit"]]
        losses_today = sum(1 for t in closed if t["exit"].date() == latest_dt.date() and t["r"] < 0)
        for t in closed:
            if t["exit"] == latest_dt:
                send_result(t, losses_today)
        # A trade from an earlier candle still running means: no new position
        still_open = [t for t in tracked if t["exit"] is None and t["time"] < latest_dt]
        if still_open:
            in_trade = still_open[-1]
    except Exception as e:
        print("Result tracking failed: " + type(e).__name__ + " " + str(e)[:100])

    signal = detect_signal(candles, news_events)
    h = utc_hour(latest)

    if signal:
        reason = None
        if in_trade:
            reason = ("ALREADY IN A TRADE (" + in_trade["dir"] + " from " +
                      in_trade["time"].strftime("%H:%M") + " UTC is still open).")
        elif losses_today >= 2:
            reason = "DAILY STOP: 2 losses today."
        send_signal(signal, reason)
        if not reason:
            log_to_sheet(signal)  # the sheet only gets trades you should actually take
        status = "Signal fired (skip)" if reason else "Signal fired!"
    elif not market_open(now_utc):
        status = "Market closed (weekend)"
    elif in_session(h):
        status = "In session, no setup"
    else:
        status = "Outside session"

    # Heartbeat every hour
    hb = ("\U0001f916 *XAUPullbackBot*\n"
          "Status: " + status + "\n"
          "\U0001f550 `" + latest + " UTC`\n"
          "\U0001f4c5 " + now_pht + "\n"
          "_Bot is running \u2705_")
    send_telegram(hb)
    print("Heartbeat sent.")

    if h == 15:
        send_daily_summary()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import requests as _r
        err = "\u26a0\ufe0f Bot error: " + str(e).replace(TG_TOKEN, "***").replace(TD_KEY, "***")
        print(err)
        try:
            _r.post("https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage",
                    json={"chat_id": TG_CHAT_ID, "text": err}, timeout=10)
        except Exception:
            pass
