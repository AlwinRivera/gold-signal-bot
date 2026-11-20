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


def fetch_page(interval, end=None, symbol="XAU/USD"):
    url = ("https://api.twelvedata.com/time_series?symbol=" + symbol + "&interval=" + interval +
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


def fetch(interval, pages=1, symbol="XAU/USD"):
    """Page backwards through history; free tier allows 8 requests/minute."""
    out = []
    end = None
    for p in range(pages):
        if p:
            time.sleep(9)
        page = fetch_page(interval, end, symbol)
        if out:
            page = [c for c in page if c["t"] < out[0]["t"]]
        if not page:
            break
        out = page + out
        end = out[0]["t"] - timedelta(minutes=1)
        print(symbol + " " + interval + " page " + str(p + 1) + ": back to " + out[0]["t"].strftime("%Y-%m-%d"))
    # drop the still-forming last candle, like the live bot
    span = {"4h": timedelta(hours=4), "15min": timedelta(minutes=15)}.get(interval, timedelta(hours=1))
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


def in_session(h, sessions="both"):
    london = 6 <= h <= 9
    ny = 12 <= h <= 15
    return london if sessions == "london" else (ny if sessions == "ny" else london or ny)


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


# Strategy settings (live bot values). The robustness check changes one at a time.
P = {"ema_fast": 20, "ema_slow": 50, "tol": 0.001, "body": 0.35, "sl_mult": 0.7,
     "sl_min": 4.0, "min_risk": 6.0, "sep": 0.001}


def detect(window, htf_mode, c4, sessions="both", rr=1.5, daily=None):
    """Mirror of signal.py detect_signal on a 120-candle window (news filter excluded)."""
    closes = [c["c"] for c in window]
    e20 = calc_ema(closes, P["ema_fast"])
    e50 = calc_ema(closes, P["ema_slow"])
    cur = window[-1]
    ev, e5 = e20[-1], e50[-1]
    if ev is None or e5 is None:
        return None
    if not in_session(cur["t"].hour, sessions):
        return None
    bull, bear = ev > e5, ev < e5

    if htf_mode != "off":
        sep = P["sep"] if htf_mode == "live" else 0.0
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
    if size < 2 or body / size < P["body"]:
        return None
    tol = ev * P["tol"]
    d = None
    if bull and cur["l"] <= ev + tol and cur["c"] > ev and cur["c"] > cur["o"]:
        d = "BUY"
    if bear and cur["h"] >= ev - tol and cur["c"] < ev and cur["c"] < cur["o"]:
        d = "SELL"
    if not d:
        return None
    if daily is not None:
        want = "BUY" if daily.get(cur["t"].date()) == "bull" else ("SELL" if daily.get(cur["t"].date()) == "bear" else None)
        if d != want:
            return None
    buf = max(size * P["sl_mult"], P["sl_min"])
    sl = ev - buf if d == "BUY" else ev + buf
    if abs(cur["c"] - sl) < P["min_risk"]:
        return None
    r1 = abs(cur["c"] - sl)
    tp = cur["c"] + rr * r1 if d == "BUY" else cur["c"] - rr * r1
    return {"dir": d, "entry": cur["c"], "sl": sl, "tp": tp, "risk": r1, "rr": rr}


def resolve(sig, future):
    """Walk forward bar by bar. If SL and TP are both inside one bar, count it as a loss."""
    mb = sig.get("max_bars", MAX_BARS)
    for j, b in enumerate(future[:mb]):
        if sig["dir"] == "BUY":
            hit_sl, hit_tp = b["l"] <= sig["sl"], b["h"] >= sig["tp"]
        else:
            hit_sl, hit_tp = b["h"] >= sig["sl"], b["l"] <= sig["tp"]
        if hit_sl:
            return "LOSS", -1.0, j + 1, b["t"]
        if hit_tp:
            return "WIN", sig.get("rr", 1.5), j + 1, b["t"]
    if len(future) < mb:
        return "OPEN", 0.0, 0, None  # not resolved yet and the time limit has not passed
    last = future[min(mb, len(future)) - 1]
    move = last["c"] - sig["entry"] if sig["dir"] == "BUY" else sig["entry"] - last["c"]
    return "TIMEOUT", move / sig["risk"], min(mb, len(future)), last["t"]


def run(c1, c4, htf_mode, sessions="both", rr=1.5, daily=None, one_at_a_time=True,
        include_open=False, skip=None, flat_hour=None, max_bars=None, daily_stop=None):
    trades = []
    busy_until = None
    for i in range(120, len(c1)):
        window = c1[i - 119:i + 1]
        if one_at_a_time and busy_until is not None and window[-1]["t"] < busy_until:
            continue  # one position at a time
        if skip is not None and skip(window[-1]["t"]):
            continue
        if daily_stop is not None:
            entry_t = window[-1]["t"] + timedelta(hours=1)
            lost = sum(1 for t in trades if t["exit"] is not None and t["r"] < 0
                       and t["exit"].date() == entry_t.date() and t["exit"] + timedelta(hours=1) <= entry_t)
            if lost >= daily_stop:
                continue
        sig = detect(window, htf_mode, c4, sessions, rr, daily)
        if not sig:
            continue
        if max_bars is not None:
            sig["max_bars"] = max_bars
        if flat_hour is not None:  # day trading: close at flat_hour UTC the same day
            sig["max_bars"] = max(1, flat_hour - (window[-1]["t"].hour + 1))
        res, r, bars, exit_t = resolve(sig, c1[i + 1:])
        if res == "OPEN":
            if include_open:
                trades.append({"time": window[-1]["t"], "dir": sig["dir"], "entry": round(sig["entry"], 2),
                               "sl": round(sig["sl"], 2), "tp": round(sig["tp"], 2),
                               "risk_pts": round(sig["risk"], 2), "result": "OPEN", "r": 0.0,
                               "bars": 0, "exit": None})
                busy_until = datetime.max.replace(tzinfo=timezone.utc)
            continue
        r -= SPREAD_PTS / sig["risk"]  # spread cost, in R
        busy_until = exit_t
        trades.append({"time": window[-1]["t"], "dir": sig["dir"], "entry": round(sig["entry"], 2),
                       "sl": round(sig["sl"], 2), "tp": round(sig["tp"], 2),
                       "risk_pts": round(sig["risk"], 2), "result": res, "r": round(r, 3),
                       "bars": bars, "exit": exit_t})
    return trades


def daily_bias(c1):
    """For each trading day: 'bull' if the previous day closed above its daily 50 EMA, else 'bear'."""
    closes_by_day = {}
    for c in c1:
        closes_by_day[c["t"].date()] = c["c"]  # last 1H close of the UTC day
    days = sorted(closes_by_day)
    closes = [closes_by_day[d] for d in days]
    ema = calc_ema(closes, 50)
    bias = {}
    for k in range(len(days) - 1):
        if ema[k] is not None:
            bias[days[k + 1]] = "bull" if closes[k] > ema[k] else "bear"
    return bias


def run_breakout(c1, rr=1.5):
    """London breakout of the Asian range.
    Asian range = high/low of the 00:00-05:00 UTC candles. First London candle (06-09 UTC)
    that closes outside it is the entry; stop at the middle of the range; target rr x risk;
    anything still open at 20:00 UTC is closed at market. One trade per day."""
    trades = []
    by_day = defaultdict(list)
    for i, c in enumerate(c1):
        by_day[c["t"].date()].append(i)
    for day in sorted(by_day):
        idx = by_day[day]
        asia = [c1[i] for i in idx if c1[i]["t"].hour <= 5]
        if len(asia) < 5:
            continue
        hi = max(c["h"] for c in asia)
        lo = min(c["l"] for c in asia)
        mid = (hi + lo) / 2
        for i in idx:
            c = c1[i]
            if not 6 <= c["t"].hour <= 9:
                continue
            d = "BUY" if c["c"] > hi else ("SELL" if c["c"] < lo else None)
            if not d:
                continue
            risk = abs(c["c"] - mid)
            if risk < 2:
                break
            tp = c["c"] + rr * risk if d == "BUY" else c["c"] - rr * risk
            sig = {"dir": d, "entry": c["c"], "sl": mid, "tp": tp, "risk": risk, "rr": rr,
                   "max_bars": max(1, 20 - (c["t"].hour + 1))}
            res, r, bars, exit_t = resolve(sig, c1[i + 1:])
            if res != "OPEN":
                r -= SPREAD_PTS / risk
                trades.append({"time": c["t"], "dir": d, "entry": round(c["c"], 2), "sl": round(mid, 2),
                               "tp": round(tp, 2), "risk_pts": round(risk, 2), "result": res,
                               "r": round(r, 3), "bars": bars})
            break
    return trades


def stats(trades):
    n = len(trades)
    if n == 0:
        return None
    wins = sum(1 for t in trades if t["r"] > 0)
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


def robustness(c1, c4, split):
    global SPREAD_PTS
    o = []
    o.append("## Robustness check of untested settings (4R, 72h)")
    o.append("")
    o.append("Rule decided before running: a setting changes only if the live value is fragile or clearly "
               "worse than its neighbours in BOTH periods. Live value marked with *.")
    o.append("")
    o.append("| Setting | Value | Trades | Per trade | PF before 2026 | PF 2026 | Max DD |")
    o.append("|---|---|---|---|---|---|---|")
    live_P = dict(P)
    sweeps = [("ema_fast", [10, 15, 20, 30]), ("ema_slow", [30, 40, 50, 75, 100]),
              ("tol", [0.0, 0.0005, 0.001, 0.002, 0.003]), ("body", [0.2, 0.25, 0.35, 0.45, 0.55]),
              ("sl_mult", [0.4, 0.55, 0.7, 0.85, 1.0]), ("sl_min", [2.0, 3.0, 4.0, 6.0, 8.0]),
              ("min_risk", [3.0, 4.5, 6.0, 8.0, 10.0]), ("sep", [0.0, 0.0005, 0.001, 0.002, 0.004])]
    def row(name, val, ts, star):
        a = stats([t for t in ts if t["time"] < split])
        b = stats([t for t in ts if t["time"] >= split])
        al = stats(ts)
        if not (a and b and al):
            return "| " + name + " | " + str(val) + star + " | too few | | | | |"
        return ("| " + name + " | " + str(val) + star + " | " + str(al["n"]) + " | " + str(round(al["exp"], 2)) +
                "R | " + str(round(a["pf"], 2)) + " | " + str(round(b["pf"], 2)) + " | " + str(round(al["dd"], 1)) + "R |")
    for key, vals in sweeps:
        for v in vals:
            P.update(live_P)
            P[key] = v
            o.append(row(key, v, run(c1, c4, "live", "both", 4.0), " *" if v == live_P[key] else ""))
    P.update(live_P)
    for ds in [None, 1, 2, 3]:
        o.append(row("daily stop (losses)", "off" if ds is None else ds,
                       run(c1, c4, "live", "both", 4.0, daily_stop=ds), " *" if ds == 2 else ""))
    base_spread = SPREAD_PTS
    for sp in [0.2, 0.3, 0.5, 0.8, 1.2]:
        SPREAD_PTS = sp
        o.append(row("cost per trade ($)", sp, run(c1, c4, "live", "both", 4.0), " *" if sp == base_spread else ""))
    SPREAD_PTS = base_spread
    o.append("")

    return o


def main():
    global SPREAD_PTS
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

    # Out-of-sample check: judge on 2024-2025, confirm on 2026 (data the idea was not picked from)
    split = datetime(2026, 1, 1, tzinfo=timezone.utc)
    oos = [("Current: London + NY", "live", "both"),
           ("London only", "live", "london"),
           ("NY only", "live", "ny")]
    out.append("## Out-of-sample check")
    out.append("")
    for period, keep in [("2024-2025 (in-sample)", lambda t: t["time"] < split),
                         ("2026 (out-of-sample)", lambda t: t["time"] >= split)]:
        out.append("**" + period + "**")
        out.append("")
        out.append("| Variant | Trades | Win rate | Avg R | Net | Profit factor | Max DD | Worst losing streak | Worst day |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for label, mode, sess in oos:
            ts = [t for t in run(c1, c4, mode, sess) if keep(t)]
            span = 23.0 if "2024" in period else max((end - split).days / 30.4, 0.1)
            out.append(fmt_row(label, stats(ts), span))
        out.append("")

    # Strategy shootout: three ideas fixed in advance, judged on both periods
    bias = daily_bias(c1)
    ideas = [("Current bot (EMA pullback 1.5R)", results["live"]),
             ("A: London breakout of Asian range", run_breakout(c1)),
             ("B: EMA pullback, 2R target", run(c1, c4, "live", "both", 2.0)),
             ("EMA pullback, 2.5R target", run(c1, c4, "live", "both", 2.5)),
             ("EMA pullback, 3R target", run(c1, c4, "live", "both", 3.0)),
             ("EMA pullback, 4R target", run(c1, c4, "live", "both", 4.0)),
             ("EMA pullback, 5R target", run(c1, c4, "live", "both", 5.0)),
             ("EMA pullback, 6R target", run(c1, c4, "live", "both", 6.0)),
             ("EMA pullback, 7R target", run(c1, c4, "live", "both", 7.0)),
             ("EMA pullback, 8R target", run(c1, c4, "live", "both", 8.0)),
             ("EMA pullback, 9R target", run(c1, c4, "live", "both", 9.0)),
             ("EMA pullback, 10R target", run(c1, c4, "live", "both", 10.0)),
             ("C: EMA pullback, daily trend only", run(c1, c4, "live", "both", 1.5, bias))]
    out.append("## Strategy shootout")
    out.append("")
    out.append("Pass = profit factor >= 1.2 in BOTH periods.")
    out.append("")
    out.append("| Strategy | 2024-25 trades | 2024-25 win% | 2024-25 net | 2024-25 PF | 2026 trades | 2026 win% | 2026 net | 2026 PF | Max DD (all) | Risk/trade to keep DD under 8% |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for label, ts in ideas:
        a = stats([t for t in ts if t["time"] < split])
        b = stats([t for t in ts if t["time"] >= split])
        allst = stats(ts)
        if not (a and b and allst):
            out.append("| " + label + " | not enough trades |")
            continue
        safe = round(8.0 / abs(allst["dd"]), 2) if allst["dd"] < 0 else 2.0
        ok = a["pf"] >= 1.2 and b["pf"] >= 1.2
        out.append("| " + label + (" **PASS**" if ok else "") + " | " + str(a["n"]) + " | " + str(round(a["wr"], 1)) +
                   "% | " + str(round(a["net"], 1)) + "R | " + str(round(a["pf"], 2)) + " | " + str(b["n"]) + " | " +
                   str(round(b["wr"], 1)) + "% | " + str(round(b["net"], 1)) + "R | " + str(round(b["pf"], 2)) +
                   " | " + str(round(allst["dd"], 1)) + "R | " + str(safe) + "% |")
    out.append("")

    # FTMO 2-Step odds for the live 4R strategy at different risk levels
    import random as _rnd
    import research as _rs
    t4 = run(c1, c4, "live", "both", 4.0)
    for edge_label, haircut in [("full backtest edge", 0.0), ("HALF the backtest edge", 0.5)]:
      cut = haircut * sum(t["r"] for t in t4) / len(t4)
      weeks = _rs.weeks_of_daily_r([dict(t, r=t["r"] - cut) for t in t4])
      out.append("## FTMO odds, live 4R strategy, " + edge_label + " (5,000 simulated challenges per row)")
      out.append("")
      out.append("Re-shuffles real weeks of backtest results. Max loss 10%, daily loss 5%, Phase 1 +10%, "
                 "Phase 2 +5%. Backtest edge assumed real (live results are usually worse).")
      out.append("")
      out.append("| Risk per trade | Funded within 6 months | Funded within 12 months | Funded eventually | Failed | Median months to funded |")
      out.append("|---|---|---|---|---|---|")
      for risk in [0.4, 0.5, 0.6, 0.8, 1.0, 1.25]:
          rng = _rnd.Random(11)
          n, w6, w12, ok, fail, dur = 5000, 0, 0, 0, 0, []
          for _ in range(n):
              r1, a1 = _rs.ftmo_phase(weeks, risk, 10.0, rng, 260)
              if r1 != "pass":
                  fail += r1 == "fail"
                  continue
              r2, a2 = _rs.ftmo_phase(weeks, risk, 5.0, rng, 260)
              if r2 != "pass":
                  fail += r2 == "fail"
                  continue
              ok += 1
              wk = a1 + a2
              dur.append(wk)
              w6 += wk <= 26
              w12 += wk <= 52
          dur.sort()
          med = str(round(dur[len(dur) // 2] / 4.33, 1)) if dur else "-"
          pc = lambda x: str(round(x / n * 100)) + "%"
          out.append("| " + str(risk) + "% | " + pc(w6) + " | " + pc(w12) + " | " + pc(ok) + " | " + pc(fail) + " | " + med + " |")
      out.append("")

    # Stop sizes of the live 4R strategy (for small-account lot sizing)
    t4 = run(c1, c4, "live", "both", 4.0)
    rk = sorted(t["risk_pts"] for t in t4)
    if rk:
        out.append("## Stop distance, live 4R strategy")
        out.append("")
        out.append("Median $" + str(round(rk[len(rk) // 2], 1)) + ", 90th percentile $" +
                   str(round(rk[int(len(rk) * 0.9)], 1)) + ", max $" + str(round(rk[-1], 1)) + ".")
        for cap in (20, 40, 50, 80):
            out.append("- Stop above $" + str(cap) + ": " + str(sum(1 for x in rk if x > cap)) + " of " +
                       str(len(rk)) + " trades (" + str(round(sum(1 for x in rk if x > cap) / len(rk) * 100)) + "%)")
        out.append("")

    if os.environ.get("ROBUST") == "1":
        out += robustness(c1, c4, split)

    out.append("## Time limit check (4R target)")
    out.append("")
    out.append("| Time limit | Trades | Closed by time | Net | Per trade | PF before 2026 | PF 2026 | Max DD |")
    out.append("|---|---|---|---|---|---|---|---|")
    for mb in [24, 48, 72, 96, 120]:
        ts = run(c1, c4, "live", "both", 4.0, None, True, max_bars=mb)
        a = stats([t for t in ts if t["time"] < split])
        b = stats([t for t in ts if t["time"] >= split])
        al = stats(ts)
        if not (a and b and al):
            continue
        tpct = str(round(sum(1 for t in ts if t["result"] == "TIMEOUT") / len(ts) * 100)) + "%"
        out.append("| " + str(mb) + " market hours (~" + str(round(mb / 24, 1)) + " days) | " + str(al["n"]) + " | " +
                   tpct + " | " + str(round(al["net"], 1)) + "R | " + str(round(al["exp"], 2)) + "R | " +
                   str(round(a["pf"], 2)) + " | " + str(round(b["pf"], 2)) + " | " + str(round(al["dd"], 1)) + "R |")
    out.append("")
    out.append("## Target size: how trades end")
    out.append("")
    out.append("| Version | Trades | Hit target | Hit stop | Closed by time | Net (all) | PF before 2026 | PF 2026 |")
    out.append("|---|---|---|---|---|---|---|---|")
    for rr in [2.0, 3.0, 4.0, 6.0, 8.0, 10.0]:
        for label, fh in [("hold up to 3 days", None), ("day trade, flat 20:00 UTC", 20)]:
            ts = run(c1, c4, "live", "both", rr, None, True, flat_hour=fh)
            if not ts:
                continue
            n = len(ts)
            pct = lambda k: str(round(sum(1 for t in ts if t["result"] == k) / n * 100)) + "%"
            a = stats([t for t in ts if t["time"] < split])
            b = stats([t for t in ts if t["time"] >= split])
            out.append("| " + str(int(rr)) + "R, " + label + " | " + str(n) + " | " + pct("WIN") + " | " + pct("LOSS") +
                       " | " + pct("TIMEOUT") + " | " + str(round(sum(t["r"] for t in ts), 1)) + "R | " +
                       (str(round(a["pf"], 2)) if a else "-") + " | " + (str(round(b["pf"], 2)) if b else "-") + " |")
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
