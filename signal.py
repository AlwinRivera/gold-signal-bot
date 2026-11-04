"""
gold-signal-bot — XAU/USD EMA Pullback Signal Scanner
Runs hourly via GitHub Actions, sends Telegram alert when setup fires.
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
                naive = datetime.strptime(date_str + " " + time_str, "%m-%d-%Y %I:%M%p")
                month = naive.month
                offset = timedelta(hours=-4) if 3 <= month <= 10 else timedelta(hours=-5)
                dt_utc = naive.replace(tzinfo=timezone(offset)).astimezone(timezone.utc)
                events.append(dt_utc)
            except Exception:
                continue
        return events
    except Exception as e:
        print("News filter unavailable: " + str(e))
        return []

def is_near_news(candle_time_str, events, buffer_minutes=30):
    if not events:
        return False
    candle_dt = datetime.strptime(candle_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    buf = timedelta(minutes=buffer_minutes)
    for ev in events:
        if abs(candle_dt - ev) <= buf:
            return True
    return False

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
    url = ("https://api.twelvedata.com/time_series"
           "?symbol=XAU/USD&interval=1h&outputsize=" + str(n) + "&apikey=" + TD_KEY)
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "error":
        raise Exception("Twelve Data error: " + data.get("message", ""))
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
    if is_near_news(cur["t"], news_events):
        print("Skipping - news within 30 min of " + cur["t"])
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
    trend_label = "20 EMA > 50 EMA (uptrend)" if bull_trend else "20 EMA < 50 EMA (downtrend)"
    return {"direction": direction, "entry": cur["c"], "sl": sl, "tp": tp,
            "sl_pips": round(abs(cur["c"] - sl) / 0.01, 1), "ema20": ev,
            "time": cur["t"], "trend": trend_label}

def send_telegram(text):
    r = requests.post("https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
                      timeout=10)
    r.raise_for_status()

def send_signal(signal):
    arrow = "\U0001f7e2 \u25b2" if signal["direction"] == "BUY" else "\U0001f534 \u25bc"
    msg = (arrow + " *XAU/USD " + signal["direction"] + " SETUP*\n"
           "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
           "\U0001f4cd Entry:  `" + f"{signal['entry']:.2f}" + "`\n"
           "\U0001f6d1 Stop:   `" + f"{signal['sl']:.2f}" + "` (" + str(signal["sl_pips"]) + " pips)\n"
           "\U0001f3af TP:     `" + f"{signal['tp']:.2f}" + "` (1.5R)\n"
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
    candles = fetch_candles(120)
    latest = candles[-1]["t"]
    print("Fetched " + str(len(candles)) + " candles. Latest: " + latest)
    news_events = fetch_news_events()
    print("Found " + str(len(news_events)) + " high-impact news events this week.")

    signal = detect_signal(candles, news_events)
    h = utc_hour(latest)

    if signal:
        send_signal(signal)
        log_to_sheet(signal)
        status = "Signal fired!"
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
        err = "\u26a0\ufe0f Bot error: " + str(e)
        print(err)
        try:
            _r.post("https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage",
                    json={"chat_id": TG_CHAT_ID, "text": err}, timeout=10)
        except Exception:
            pass
