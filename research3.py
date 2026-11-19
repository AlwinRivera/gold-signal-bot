"""
Round 3 research: four day-trading ideas on 15-minute gold candles.
Rules are exactly as declared in RESEARCH.md (Round 3). Run from GitHub Actions.
"""

import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import backtest as bt
import research as rs

NY = ZoneInfo("America/New_York")
LDN = ZoneInfo("Europe/London")
COST = 0.30
MIN_RISK = 2.0
RR = 2.0
SPLIT = datetime(2026, 1, 1, tzinfo=timezone.utc)
PAGES = int(os.environ.get("PAGES15", "22"))
SIMS = int(os.environ.get("SIMS", "3000"))


def local(t, tz):
    return t.astimezone(tz)


def trade(c, i, d, entry, sl, close_at):
    """Simulate on 15M candles from i+1; stop first if both hit; flat at the first candle
    starting at or after close_at (UTC datetime). Returns trade dict or None."""
    risk = abs(entry - sl)
    if risk < MIN_RISK:
        return None
    tp = entry + RR * risk if d == "BUY" else entry - RR * risk
    for j in range(i + 1, len(c)):
        b = c[j]
        if b["t"] >= close_at:
            prev = c[j - 1]
            move = prev["c"] - entry if d == "BUY" else entry - prev["c"]
            return {"time": c[i]["t"], "exit": prev["t"], "dir": d, "r": move / risk - COST / risk,
                    "result": "EXIT"}
        hit_sl = b["l"] <= sl if d == "BUY" else b["h"] >= sl
        hit_tp = b["h"] >= tp if d == "BUY" else b["l"] <= tp
        if hit_sl:
            return {"time": c[i]["t"], "exit": b["t"], "dir": d, "r": -1.0 - COST / risk, "result": "LOSS"}
        if hit_tp:
            return {"time": c[i]["t"], "exit": b["t"], "dir": d, "r": RR - COST / risk, "result": "WIN"}
    return None  # still open at the end of the data


def by_day(c):
    days = defaultdict(list)
    for i, x in enumerate(c):
        days[x["t"].date()].append(i)
    return days


def utc_at(day, hh, mm, tz):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz).astimezone(timezone.utc)


# ---------- D1: NY opening range breakout ----------
def d1(c):
    out = []
    days = by_day(c)
    for day in sorted(days):
        if day.weekday() >= 5:
            continue
        o, r_end = utc_at(day, 9, 30, NY), utc_at(day, 10, 0, NY)
        last_entry, flat = utc_at(day, 12, 0, NY), utc_at(day, 15, 45, NY)
        rng = [c[i] for i in days[day] if o <= c[i]["t"] < r_end]
        if len(rng) < 2:
            continue
        hi, lo = max(x["h"] for x in rng), min(x["l"] for x in rng)
        for i in days[day]:
            t = c[i]["t"]
            if t < r_end or t + timedelta(minutes=15) > last_entry:
                continue
            d = "BUY" if c[i]["c"] > hi else ("SELL" if c[i]["c"] < lo else None)
            if not d:
                continue
            tr = trade(c, i, d, c[i]["c"], lo if d == "BUY" else hi, flat)
            if tr:
                out.append(tr)
            break
    return out


# ---------- D2: yesterday's high/low fake-out ----------
def d2(c):
    out = []
    days = by_day(c)
    order = sorted(days)
    for k in range(1, len(order)):
        day, prev = order[k], order[k - 1]
        if day.weekday() >= 5:
            continue
        ph = max(c[i]["h"] for i in days[prev])
        pl = min(c[i]["l"] for i in days[prev])
        flat = datetime(day.year, day.month, day.day, 20, 0, tzinfo=timezone.utc)
        for i in days[day]:
            x = c[i]
            if not 7 <= x["t"].hour <= 15:
                continue
            if x["h"] > ph and x["c"] < ph:
                d, sl = "SELL", x["h"] + 0.5
            elif x["l"] < pl and x["c"] > pl:
                d, sl = "BUY", x["l"] - 0.5
            else:
                continue
            tr = trade(c, i, d, x["c"], sl, flat)
            if tr:
                out.append(tr)
                break
    return out


# ---------- D3: 15M EMA pullback with 1H trend ----------
def to_1h(c):
    hours, idx = [], {}
    for x in c:
        key = x["t"].replace(minute=0)
        if key not in idx:
            idx[key] = len(hours)
            hours.append({"t": key, "o": x["o"], "h": x["h"], "l": x["l"], "c": x["c"], "n": 1})
        else:
            h = hours[idx[key]]
            h["h"], h["l"], h["c"], h["n"] = max(h["h"], x["h"]), min(h["l"], x["l"]), x["c"], h["n"] + 1
    return hours, idx


def d3(c):
    out = []
    closes = [x["c"] for x in c]
    e20, e50 = bt.calc_ema(closes, 20), bt.calc_ema(closes, 50)
    hours, hidx = to_1h(c)
    hc = [h["c"] for h in hours]
    h20, h50 = bt.calc_ema(hc, 20), bt.calc_ema(hc, 50)
    busy = None
    for i, x in enumerate(c):
        t = x["t"]
        if t.weekday() >= 5 or not (6 <= t.hour <= 9 or 12 <= t.hour <= 15):
            continue
        if busy and t < busy:
            continue
        if e20[i] is None or e50[i] is None:
            continue
        k = hidx[t.replace(minute=0)] - 1  # last fully closed hour
        if k < 0 or h20[k] is None or h50[k] is None:
            continue
        up1h, dn1h = h20[k] > h50[k], h20[k] < h50[k]
        ev, bull, bear = e20[i], e20[i] > e50[i], e20[i] < e50[i]
        size, body = x["h"] - x["l"], abs(x["c"] - x["o"])
        if size <= 0 or body / size < 0.35:
            continue
        tol = ev * 0.001
        d = None
        if bull and up1h and x["l"] <= ev + tol and x["c"] > ev and x["c"] > x["o"]:
            d = "BUY"
        if bear and dn1h and x["h"] >= ev - tol and x["c"] < ev and x["c"] < x["o"]:
            d = "SELL"
        if not d:
            continue
        sl = ev - 0.7 * size if d == "BUY" else ev + 0.7 * size
        flat = datetime(t.year, t.month, t.day, 20, 0, tzinfo=timezone.utc)
        tr = trade(c, i, d, x["c"], sl, flat)
        if tr:
            out.append(tr)
            busy = tr["exit"]
    return out


# ---------- D4: London open momentum ----------
def d4(c):
    out = []
    hours, hidx = to_1h(c)
    ha = rs.atr(hours, 14)
    closes = [x["c"] for x in c]
    e20 = bt.calc_ema(closes, 20)
    days = by_day(c)
    for day in sorted(days):
        if day.weekday() >= 5:
            continue
        o, h1 = utc_at(day, 8, 0, LDN), utc_at(day, 9, 0, LDN)
        last_entry, flat = utc_at(day, 11, 0, LDN), utc_at(day, 12, 0, LDN)
        first = [c[i] for i in days[day] if o <= c[i]["t"] < h1]
        if len(first) < 4:
            continue
        k = hidx.get(o.replace(minute=0))
        if k is None or k < 1 or ha[k - 1] is None:
            continue
        move = first[-1]["c"] - first[0]["o"]
        if abs(move) < ha[k - 1]:
            continue
        d = "BUY" if move > 0 else "SELL"
        for i in days[day]:
            x = c[i]
            if x["t"] < h1 or x["t"] + timedelta(minutes=15) > last_entry or e20[i] is None:
                continue
            if d == "BUY" and x["l"] <= e20[i] and x["c"] > e20[i]:
                tr = trade(c, i, d, x["c"], x["l"] - 0.5, flat)
            elif d == "SELL" and x["h"] >= e20[i] and x["c"] < e20[i]:
                tr = trade(c, i, d, x["c"], x["h"] + 0.5, flat)
            else:
                continue
            if tr:
                out.append(tr)
            break
    return out


def main():
    c = [x for x in bt.fetch("15min", PAGES, "XAU/USD") if bt.market_open(x["t"])]
    first, last = c[0]["t"], c[-1]["t"]
    out = ["# Round 3: day-trading batch (15M gold)", "",
           "Data: " + first.strftime("%Y-%m-%d") + " to " + last.strftime("%Y-%m-%d") + ", " +
           str(len(c)) + " candles. Rules and pass bar: RESEARCH.md, Round 3.", ""]
    if first > datetime(2024, 6, 1, tzinfo=timezone.utc):
        out.append("**Note:** history starts later than planned; the 2024-25 sample is shorter.")
        out.append("")
    results = {"D1 NY opening range breakout": d1(c), "D2 Yesterday's high/low fake-out": d2(c),
               "D3 15M EMA pullback, 1H trend": d3(c), "D4 London open momentum": d4(c)}
    out.append("| Strategy | 2024-25 trades | 2024-25 win% | 2024-25 net | 2024-25 PF | "
               "2026 trades | 2026 win% | 2026 net | 2026 PF | Max DD | Result |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    passed = []
    for name, ts in results.items():
        a = bt.stats([t for t in ts if t["time"] < SPLIT])
        b = bt.stats([t for t in ts if t["time"] >= SPLIT])
        al = bt.stats(ts)
        if not (a and b and al):
            out.append("| " + name + " | not enough trades | | | | | | | | | fail |")
            continue
        ok = a["pf"] >= 1.3 and b["pf"] >= 1.3 and b["n"] >= 30
        if ok:
            passed.append(name)
        f = rs.fmt
        out.append("| " + name + " | " + str(a["n"]) + " | " + f(a["wr"], 1) + "% | " + f(a["net"], 1) +
                   "R | " + f(a["pf"]) + " | " + str(b["n"]) + " | " + f(b["wr"], 1) + "% | " + f(b["net"], 1) +
                   "R | " + f(b["pf"]) + " | " + f(al["dd"], 1) + "R | " + ("**PASS**" if ok else "fail") + " |")
    out.append("")
    out.append("**Passed:** " + (", ".join(passed) if passed else "none"))
    out.append("")
    for name in passed:
        ts = sorted(results[name], key=lambda t: t["time"])
        out.append("### FTMO odds: " + name)
        out.append("")
        out.append("| Risk per trade | Pass Phase 1 | Pass both (funded) | Fail | Median months to funded |")
        out.append("|---|---|---|---|---|")
        for risk, p1, both, fails, med in rs.ftmo_odds(ts, SIMS):
            out.append("| " + str(risk) + "% | " + rs.fmt(p1, 1) + "% | " + rs.fmt(both, 1) + "% | " +
                       rs.fmt(fails, 1) + "% | " + rs.fmt(med, 1) + " |")
        out.append("")
    report = "\n".join(out)
    print(report)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")


if __name__ == "__main__":
    main()
