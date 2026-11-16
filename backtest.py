"""
Backtest for the XAU/USD EMA pullback bot.

Replays the exact entry/stop/target rules from signal.py over historical
1H candles (Twelve Data, UTC), one closed candle at a time, the same way
the live bot sees them. Run manually from GitHub Actions (backtest.yml).

Not simulated: the ForexFactory news filter (no historical feed), and
slippage beyond a fixed spread cost per trade.
"""

import os
import csv
import time
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

TD_KEY = os.environ["TD_KEY"]
SPREAD_PTS = float(os.environ.get("SPREAD_PTS", "0.30"))  # round-trip cost in $ per oz
MAX_BARS = 72        # give up on a trade after 3 days and close at market
RISK_PCT = 1.0
ACCOUNT = 10000.0


def parse(t):
    return datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def fetch_page(interval, end=None):
    url = ("https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=" + interval +
           "&outputsize=5000&timezone=UTC&apikey=" + TD_KEY)
    if end:
        url += "&end_date=" + end.strftime("%Y-%m-%d %H:%M:%S")
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "error":
        print("Twelve Data stopped paging: " + data.get("message", ""))
        return []
    rows = list(reversed(data["values"]))
    return [{"t": parse(c["datetime"]), "o": float(c["open"]), "h": float(c["high"]),
             "l": float(c["low"]), "c": float(c["close"])} for c in rows]


def fetch(interval, pages=1):
    """Page backwards through history; free tier allows 8 requests/minute."""
    out = []
    end = None
    for p in range(pages):
        if p:
            time.sleep(9)
        page = fetch_page(interval, end)
        if out:
            page = [c for c in page if c["t"] < out[0]["t"]]
        if not page:
            break
        out = page + out
        end = out[0]["t"] - timedelta(minutes=1)
        print(interval + " page " + str(p + 1) + ": back to " + out[0]["t"].strftime("%Y-%m-%d"))
    # drop the still-forming last candle, like the live bot
    span = timedelta(hours=4) if interval == "4h" else timedelta(hours=1)
    if out and out[-1]["t"] + span > datetime.now(timezone.utc):
        out = out[:-1]
    return out


# Same EMA as signal.py: SMA seed over the first `period` closes of the window
def calc_ema(closes, period):
    k = 2 / (period + 1)
    ema = [None] * len(closes)
    if len(closes) < period:
        return ema
    ema[period - 1] = sum(closes[:period]) / period
    for i in range(period, len(closes)):
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k)
    return ema


def market_open(t):
    """Gold trades Sun ~22:00 UTC to Fri ~21:00 UTC. Drop Saturday and Sunday before 22:00."""
    return not (t.weekday() == 5 or (t.weekday() == 6 and t.hour < 22))


def in_session(h):
    return (6 <= h <= 9) or (12 <= h <= 15)


def htf_trend(c4, as_of, sep_min):
    """4H trend from the last 60 fully closed 4H candles at time `as_of` (live bot uses 60)."""
    closed = [c for c in c4 if c["t"] + timedelta(hours=4) <= as_of]
    win = closed[-60:]
    if len(win) < 50:
        return "neutral"
    closes = [c["c"] for c in win]
    a = calc_ema(closes, 20)[-1]
    b = calc_ema(closes, 50)[-1]
    if abs(a - b) / b < sep_min:
        return "neutral"
    return "bull" if a > b else ("bear" if a < b else "neutral")


def detect(window, htf_mode, c4):
    """Mirror of signal.py detect_signal on a 120-candle window (news filter excluded)."""
    closes = [c["c"] for c in window]
    e20 = calc_ema(closes, 20)
    e50 = calc_ema(closes, 50)
    cur = window[-1]
    ev, e5 = e20[-1], e50[-1]
    if ev is None or e5 is None:
        return None
    if not in_session(cur["t"].hour):
        return None
    bull, bear = ev > e5, ev < e5

    if htf_mode != "off":
        sep = 0.001 if htf_mode == "live" else 0.0
        trend = htf_trend(c4, cur["t"] + timedelta(hours=1), sep)
        if trend == "bear" and bull:
            return None
        if trend == "bull" and bear:
            return None
        if htf_mode == "strict" and trend == "neutral":
            return None

    if not bull and not bear:
        return None
    size = cur["h"] - cur["l"]
    body = abs(cur["c"] - cur["o"])
    if size < 2 or body / size < 0.35:
        return None
    tol = ev * 0.001
    d = None
    if bull and cur["l"] <= ev + tol and cur["c"] > ev and cur["c"] > cur["o"]:
        d = "BUY"
    if bear and cur["h"] >= ev - tol and cur["c"] < ev and cur["c"] < cur["o"]:
        d = "SELL"
    if not d:
        return None
    buf = max(size * 0.7, 4.0)
    sl = ev - buf if d == "BUY" else ev + buf
    if abs(cur["c"] - sl) < 6.0:
        return None
    r1 = abs(cur["c"] - sl)
    tp = cur["c"] + 1.5 * r1 if d == "BUY" else cur["c"] - 1.5 * r1
    return {"dir": d, "entry": cur["c"], "sl": sl, "tp": tp, "risk": r1}


def resolve(sig, future):
    """Walk forward bar by bar. If SL and TP are both inside one bar, count it as a loss."""
    for j, b in enumerate(future[:MAX_BARS]):
        if sig["dir"] == "BUY":
            hit_sl, hit_tp = b["l"] <= sig["sl"], b["h"] >= sig["tp"]
        else:
            hit_sl, hit_tp = b["h"] >= sig["sl"], b["l"] <= sig["tp"]
        if hit_sl:
            return "LOSS", -1.0, j + 1, b["t"]
        if hit_tp:
            return "WIN", 1.5, j + 1, b["t"]
    if not future:
        return "OPEN", 0.0, 0, None
    last = future[min(MAX_BARS, len(future)) - 1]
    move = last["c"] - sig["entry"] if sig["dir"] == "BUY" else sig["entry"] - last["c"]
    return "TIMEOUT", move / sig["risk"], min(MAX_BARS, len(future)), last["t"]


def run(c1, c4, htf_mode):
    trades = []
    busy_until = None
    for i in range(120, len(c1)):
        window = c1[i - 119:i + 1]
        if busy_until is not None and window[-1]["t"] < busy_until:
            continue  # one position at a time
        sig = detect(window, htf_mode, c4)
        if not sig:
            continue
        res, r, bars, exit_t = resolve(sig, c1[i + 1:])
        if res == "OPEN":
            continue
        r -= SPREAD_PTS / sig["risk"]  # spread cost, in R
        busy_until = exit_t
        trades.append({"time": window[-1]["t"], "dir": sig["dir"], "entry": round(sig["entry"], 2),
                       "sl": round(sig["sl"], 2), "tp": round(sig["tp"], 2),
                       "risk_pts": round(sig["risk"], 2), "result": res, "r": round(r, 3),
                       "bars": bars})
    return trades


def stats(trades):
    n = len(trades)
    if n == 0:
        return None
    wins = sum(1 for t in trades if t["result"] == "WIN")
    net = sum(t["r"] for t in trades)
    eq = peak = dd = 0.0
    streak = worst_streak = 0
    for t in trades:
        eq += t["r"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
        streak = streak + 1 if t["r"] < 0 else 0
        worst_streak = max(worst_streak, streak)
    daily = defaultdict(float)
    for t in trades:
        daily[t["time"].date()] += t["r"]
    gross_w = sum(t["r"] for t in trades if t["r"] > 0)
    gross_l = -sum(t["r"] for t in trades if t["r"] < 0)
    return {"n": n, "wins": wins, "wr": wins / n * 100, "net": net, "exp": net / n,
            "dd": dd, "streak": worst_streak, "worst_day": min(daily.values()),
            "pf": gross_w / gross_l if gross_l else float("inf")}


def fmt_row(name, s, months):
    if not s:
        return "| " + name + " | 0 | - | - | - | - | - | - | - |"
    usd = s["net"] * ACCOUNT * RISK_PCT / 100
    return ("| " + name + " | " + str(s["n"]) + " (" + str(round(s["n"] / months, 1)) + "/mo) | " +
            str(round(s["wr"], 1)) + "% | " + str(round(s["exp"], 2)) + "R | " +
            str(round(s["net"], 1)) + "R ($" + str(round(usd)) + ") | " +
            str(round(s["pf"], 2)) + " | " + str(round(s["dd"], 1)) + "R | " +
            str(s["streak"]) + " | " + str(round(s["worst_day"], 1)) + "R |")


def main():
    pages = int(os.environ.get("PAGES", "4"))
    c1 = fetch("1h", pages)
    c4 = fetch("4h", max(1, pages // 3))
    wk = defaultdict(int)
    for c in c1:
        wk[c["t"].strftime("%a")] += 1
    print("1H candles by weekday (UTC): " + str(dict(wk)))
    sun = sorted(set(c["t"].hour for c in c1 if c["t"].weekday() == 6))
    print("Sunday hours present (UTC): " + str(sun))
    if os.environ.get("KEEP_WEEKENDS") != "1":
        c1 = [c for c in c1 if market_open(c["t"])]
        c4 = [c for c in c4 if market_open(c["t"])]
        print("After dropping weekend candles: " + str(len(c1)) + " 1H candles")
    start, end = c1[120]["t"], c1[-1]["t"]
    months = max((end - start).days / 30.4, 0.1)

    variants = [("Live rules (4H filter, 0.1% gap)", "live"),
                ("No 4H filter", "off"),
                ("4H must agree (no neutral)", "strict")]
    results = {}
    for label, mode in variants:
        results[mode] = run(c1, c4, mode)

    out = []
    out.append("# XAU/USD EMA Pullback Backtest")
    out.append("")
    out.append("Period: **" + start.strftime("%Y-%m-%d") + " to " + end.strftime("%Y-%m-%d") +
               "** (" + str(round(months, 1)) + " months, " + str(len(c1)) + " 1H candles). "
               "Spread cost " + str(SPREAD_PTS) + " pts/trade. News filter not simulated. "
               "If SL and TP are hit in the same candle, it counts as a loss.")
    out.append("")
    out.append("| Variant | Trades | Win rate | Avg R | Net | Profit factor | Max DD | Worst losing streak | Worst day |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for label, mode in variants:
        out.append(fmt_row(label, stats(results[mode]), months))
    out.append("")
    out.append("Break-even win rate at 1.5R is 40%. $ figures assume $100 risk per trade (1% of $10k).")
    out.append("")

    live = results["live"]
    if live:
        out.append("## Live rules: breakdown")
        out.append("")
        out.append("| Slice | Trades | Win rate | Net R |")
        out.append("|---|---|---|---|")
        slices = [
            ("London (06-09 UTC)", [t for t in live if t["time"].hour <= 9]),
            ("New York (12-15 UTC)", [t for t in live if t["time"].hour >= 12]),
            ("BUY", [t for t in live if t["dir"] == "BUY"]),
            ("SELL", [t for t in live if t["dir"] == "SELL"]),
        ]
        bymonth = defaultdict(list)
        for t in live:
            bymonth[str(t["time"].year) + "-Q" + str((t["time"].month - 1) // 3 + 1)].append(t)
        for m in sorted(bymonth):
            slices.append((m, bymonth[m]))
        for name, ts in slices:
            s = stats(ts)
            if s:
                out.append("| " + name + " | " + str(s["n"]) + " | " + str(round(s["wr"], 1)) +
                           "% | " + str(round(s["net"], 1)) + " |")
        out.append("")
        timeouts = sum(1 for t in live if t["result"] == "TIMEOUT")
        out.append("Trades closed by 72-bar timeout: " + str(timeouts))
        out.append("")
        out.append("## Last 10 trades (live rules)")
        out.append("")
        out.append("| Candle (UTC) | Dir | Entry | SL | TP | Result | R |")
        out.append("|---|---|---|---|---|---|---|")
        for t in live[-10:]:
            out.append("| " + t["time"].strftime("%Y-%m-%d %H:%M") + " | " + t["dir"] + " | " +
                       str(t["entry"]) + " | " + str(t["sl"]) + " | " + str(t["tp"]) + " | " +
                       t["result"] + " | " + str(t["r"]) + " |")

    report = "\n".join(out)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report + "\n")

    with open("backtest_trades.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["variant", "time_utc", "dir", "entry", "sl", "tp", "risk_pts", "result", "r", "bars"])
        for label, mode in variants:
            for t in results[mode]:
                w.writerow([mode, t["time"].strftime("%Y-%m-%d %H:%M"), t["dir"], t["entry"], t["sl"],
                            t["tp"], t["risk_pts"], t["result"], t["r"], t["bars"]])


if __name__ == "__main__":
    main()
