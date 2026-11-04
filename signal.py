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

Filters:
- News filter: skip signals 30 min before/after high-impact news (via ForexFactory XML)
- DST-aware session detection using UTC hours
"""

import os
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

TD_KEY     = os.environ["TD_KEY"]
TG_TOKEN   = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
SHEET_URL  = os.environ.get("SHEET_URL", "")

PHT = timezone(timedelta(hours=8))
ET_  = timezone(timedelta(hours=-5))  # US Eastern (winter); -4 in summer

# ── time helpers ─────────────────────────────────
def pht_hour(utc_str):
    dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(PHT).hour

def utc_hour(utc_str):
    dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.hour

def in_session(h):
    # UTC hours covering London + NY including DST shifts
    london_winter = (7 <= h <= 9)
    london_summer = (6 <= h <= 8)
    ny_winter     = (13 <= h <= 15)
    ny_summer     = (12 <= h <= 14)
    return london_winter or london_summer or ny_winter or ny_summer

# ── news filter ──────────────────────────────────
def fetch_news_events():
    """Fetch this week's high-impact USD/XAU events from ForexFactory XML."""
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        events = []
        for event in root.findall("event"):
            currency = event.findtext("country", "")
            impact   = event.findtext("impact", "")
            if currency not in ("USD", "XAU"):
                continue
            if impact != "High":
                continue
            date_str  = event.findtext("date", "")
            time_str  = event.findtext("time", "")
            if not date_str or not time_str or time_str == "All Day" or time_str == "Tentative":
                continue
            try:
                # ForexFactory times are US Eastern
                naive = datetime.strptime(f"{date_str} {time_str}", "%m-%d-%Y %I:%M%p")
                # Determine if DST is active (roughly Mar second Sun to Nov first Sun)
                month = naive.month
                dst_active = 3 <= month <= 10
                offset = timedelta(hours=-4) if dst_active else timedelta(hours=-5)
                et_tz  = timezone(offset)
                dt_et  = naive.replace(tzinfo=et_tz)
                dt_utc = dt_et.astimezone(timezone.utc)
                events.append(dt_utc)
            except Exception:
                continue
        return events
    except Exception as e:
        print(f"⚠️ News filter unavailable: {e}")
        return []

def is_near_news(candle_time_str, events, buffer_minutes=30):
    """Return True if candle is within buffer_minutes of a high-impact event."""
    if not events:
        return False
    candle_dt = datetime.strptime(candle_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    buf = timedelta(minutes=buffer_minutes)
    for ev in events:
        if abs(candle_dt - ev) <= buf:
            return True
    return False

# ── EMA + candle helpers ─────────────────────────
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
    # News filter
    if is_near_news(cur["t"], news_events):
        print(f"📰 Skipping — high-impact news within 30 min of {cur['t']}")
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

def log_to_sheet(signal):
    if not SHEET_URL:
        return
    try:
        payload = {
            "timestamp": datetime.now(PHT).strftime("%Y-%m-%d %H:%M PHT"),
            "direction": signal["direction"],
            "entry":     signal["entry"],
            "sl":        signal["sl"],
            "tp":        signal["tp"],
            "ema20":     signal["ema20"],
            "trend":     signal["trend"],
            "candle_time": signal["time"]
        }
        requests.post(SHEET_URL, json=payload, timeout=10)
        print("📋 Logged to Google Sheet.")
    except Exception as e:
        print(f"⚠️ Sheet log failed: {e}")

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
    news_events = fetch_news_events()
    print(f"📰 Found {len(news_events)} high-impact news events this week.")
    signal = detect_signal(candles, news_events)
    if signal:
        print(f"🎯 Setup found: {signal['direction']} @ {signal['entry']:.2f}")
        send_telegram(signal)
        log_to_sheet(signal)
    else:
        h = utc_hour(candles[-1]["t"])
        if in_session(h):
            print("⏳ In session but no setup on this candle.")
        else:
            print(f"💤 Outside session (UTC hour: {h}). No scan needed.")

if __name__ == "__main__":
    main()
