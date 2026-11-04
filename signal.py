"""
gold-signal-bot — XAU/USD EMA Pullback Signal Scanner
Runs hourly via GitHub Actions, sends Telegram alert when setup fires.

Strategy: EMA Pullback to 20 EMA
- 20 EMA above 50 EMA → look for BUYs
- 20 EMA below 50 EMA → look for SELLs
- Candle wick touches 20 EMA within 0.1%
- Candle closes on trend side, body >= 35%
- Session: London (PHT 15-17) or NY (PHT 21-23)
- SL: 0.7x candle size beyond 20 EMA, min $4
- TP: 1.5R
"""

import os
import requests
from datetime import datetime, timezone, timedelta

TD_KEY     = os.environ["TD_KEY"]
TG_TOKEN   = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]

PHT = timezone(timedelta(hours=8))

def pht_hour(utc_str):
    dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(PHT).hour

def utc_hour(utc_str):
    dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.hour

def in_session(h):
    # Adjust for DST: London (Mar-Oct) is UTC+1, NY (Mar-Nov) is UTC-4
    # We check the UTC hour directly against session windows
    # London: 07-09 UTC (winter) or 06-08 UTC (summer)
    # NY:     13-15 UTC (winter) or 12-14 UTC (summer)
    # Instead of PHT hour, use UTC hour from the candle timestamp directly
    # This function now accepts UTC hour
    london_winter = (7 <= h <= 9)    # UTC+0, Nov-Mar
    london_summer = (6 <= h <= 8)    # UTC+1, Mar-Oct
    ny_winter     = (13 <= h <= 15)  # UTC-5, Nov-Mar
    ny_summer     = (12 <= h <= 14)  # UTC-4, Mar-Nov
    return london_winter or london_summer or ny_winter or ny_summer

def calc_ema(closes, period):
    k = 2 / (period + 1)
    ema = [None] * len(closes)
    if len(closes) < period:
        return ema
    ema[period - 1] = sum(closes[:period]) / period
    for i in range(period, len(closes)):
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k)
    return ema

def fetch_candles(n=120):
    url = (
        f"https://api.twelvedata.com/time_series"
        f"?symbol=XAU/USD&interval=1h&outputsize={n}&apikey={TD_KEY}"
    )
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "error":
        raise Exception(f"Twelve Data error: {data.get('message')}")
    candles = list(reversed(data["values"]))
    return [{"t": c["datetime"], "o": float(c["open"]), "h": float(c["high"]),
             "l": float(c["low"]), "c": float(c["close"])} for c in candles]

def detect_signal(candles):
    closes = [c["c"] for c in candles]
    e20 = calc_ema(closes, 20)
    e50 = calc_ema(closes, 50)
    idx = len(candles) - 1
    cur = candles[idx]
    if e20[idx] is None or e50[idx] is None:
        return None
    h = pht_hour(cur["t"])
    if not in_session(h):
        return None
    ev = e20[idx]; e5 = e50[idx]
    bull_trend = ev > e5; bear_trend = ev < e5
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
    tp = cur["c"] + r1 * 1.5 if direction == "BUY" else cur["c"] - r1 * 1.5
    return {"direction": direction, "entry": cur["c"], "sl": sl, "tp": tp,
            "sl_pips": round(abs(cur["c"] - sl) / 0.01, 1), "ema20": ev,
            "time": cur["t"], "trend": "20 EMA > 50 EMA (uptrend)" if bull_trend else "20 EMA < 50 EMA (downtrend)"}

def send_telegram(signal):
    arrow = "🟢 ▲" if signal["direction"] == "BUY" else "🔴 ▼"
    msg = (
        f"{arrow} *XAU/USD {signal['direction']} SETUP*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📍 Entry:  `{signal['entry']:.2f}`\n"
        f"🛑 Stop:   `{signal['sl']:.2f}` ({signal['sl_pips']} pips)\n"
        f"🎯 TP:     `{signal['tp']:.2f}` (1.5R)\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 20 EMA: `{signal['ema20']:.2f}`\n"
        f"📈 Trend:  {signal['trend']}\n"
        f"🕐 Candle: `{signal['time']} UTC`\n"
        f"━━━━━━━━━━━━━━━\n"
        f"_Strategy: EMA Pullback · 1H · XAUPullbackBot_"
    )
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
                      timeout=10)
    r.raise_for_status()
    print(f"✅ Signal sent: {signal['direction']} @ {signal['entry']:.2f}")

def main():
    print(f"🔍 Scanning XAU/USD 1H — {datetime.now(PHT).strftime('%Y-%m-%d %H:%M PHT')}")
    candles = fetch_candles(120)
    print(f"📦 Fetched {len(candles)} candles. Latest: {candles[-1]['t']}")
    signal = detect_signal(candles)
    if signal:
        print(f"🎯 Setup found: {signal['direction']} @ {signal['entry']:.2f}")
        send_telegram(signal)
    else:
        h = utc_hour(candles[-1]["t"])
        if in_session(h):
            print("⏳ In session but no setup on this candle.")
        else:
            print(f"💤 Outside session (UTC hour: {h}). No scan needed.")

if __name__ == "__main__":
    main()
