"""
Strategy research bench. Runs the pre-declared tests in RESEARCH.md:
five strategies, three markets, judged on 2024-25 and confirmed on 2026,
then combines the survivors and simulates FTMO 2-step challenges.

Run from GitHub Actions (research.yml). Needs TD_KEY.
"""

import os
import random
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import backtest as bt

MARKETS = [("XAU/USD", 0.30), ("EUR/USD", 0.00012), ("USD/JPY", 0.015)]
SPLIT = datetime(2026, 1, 1, tzinfo=timezone.utc)
FRESH = datetime(2026, 11, 17, tzinfo=timezone.utc)  # Round 2: only data after this counts
ROUND2 = [("S1 EMA pullback (bot)", "XAU/USD"), ("S1 EMA pullback 4R (bot from 20 Nov)", "XAU/USD"), ("S2 Prev-day breakout", "USD/JPY"),
          ("S2 Prev-day breakout", "XAU/USD"), ("S4 RSI(2) dip, daily trend", "XAU/USD")]
PAGES = int(os.environ.get("PAGES", "4"))
SIMS = int(os.environ.get("SIMS", "3000"))
RISKS = [0.25, 0.5, 0.75, 1.0, 1.25]


# ---------- indicators (all causal: value at i uses only candles <= i) ----------

def atr(c, n=14):
    out = [None] * len(c)
    trs = []
    for i, x in enumerate(c):
        if i == 0:
            tr = x["h"] - x["l"]
        else:
            pc = c[i - 1]["c"]
            tr = max(x["h"] - x["l"], abs(x["h"] - pc), abs(x["l"] - pc))
        trs.append(tr)
        if i == n - 1:
            out[i] = sum(trs) / n
        elif i >= n:
            out[i] = (out[i - 1] * (n - 1) + tr) / n
    return out


def rsi(closes, n=2):
    out = [None] * len(closes)
    g = l = 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        up, dn = max(ch, 0.0), max(-ch, 0.0)
        if i <= n:
            g += up
            l += dn
            if i == n:
                g /= n
                l /= n
                out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
        else:
            g = (g * (n - 1) + up) / n
            l = (l * (n - 1) + dn) / n
            out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def bbands(closes, n=20, k=2.0):
    mid, up, lo = [None] * len(closes), [None] * len(closes), [None] * len(closes)
    for i in range(n - 1, len(closes)):
        w = closes[i - n + 1:i + 1]
        m = sum(w) / n
        sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
        mid[i], up[i], lo[i] = m, m + k * sd, m - k * sd
    return mid, up, lo


def to_daily(c1):
    """Daily candles from 1H candles (UTC days). Returns (days, date -> index)."""
    days, idx = [], {}
    for c in c1:
        d = c["t"].date()
        if d not in idx:
            idx[d] = len(days)
            days.append({"d": d, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]})
        else:
            x = days[idx[d]]
            x["h"] = max(x["h"], c["h"])
            x["l"] = min(x["l"], c["l"])
            x["c"] = c["c"]
    return days, idx


# ---------- trade simulation ----------

def simulate(c1, i, d, entry, sl, tp, cost, max_bars, exit_fn=None):
    """Enter at close of candle i. Walk forward. Stop checked before target (conservative)."""
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    last = i + max_bars
    if last >= len(c1):
        return None  # would still be open at the end of the data
    for j in range(i + 1, last + 1):
        b = c1[j]
        if d == "BUY":
            hit_sl, hit_tp = b["l"] <= sl, tp is not None and b["h"] >= tp
        else:
            hit_sl, hit_tp = b["h"] >= sl, tp is not None and b["l"] <= tp
        if hit_sl:
            r, res = -1.0, "LOSS"
        elif hit_tp:
            r, res = abs(tp - entry) / risk, "WIN"
        elif j == last or (exit_fn and exit_fn(j)):
            move = b["c"] - entry if d == "BUY" else entry - b["c"]
            r, res = move / risk, "EXIT"
        else:
            continue
        return {"time": c1[i]["t"], "exit": b["t"], "dir": d, "r": r - cost / risk, "result": res}
    return None


def bars_until(hour_now, close_hour):
    """Bars from entry (close of the candle starting at hour_now) until close_hour UTC."""
    return max(1, close_hour - (hour_now + 1))


def weekday_ok(t):
    return t.weekday() < 5


# ---------- strategies ----------

def s2_prev_day_breakout(c1, days, didx, a, cost):
    trades, busy, done = [], None, set()
    for i, c in enumerate(c1):
        t = c["t"]
        if not weekday_ok(t) or not 6 <= t.hour <= 15 or a[i] is None:
            continue
        if t.date() in done or (busy and t < busy):
            continue
        k = didx[t.date()]
        if k == 0:
            continue
        p = days[k - 1]
        d = "BUY" if c["c"] > p["h"] else ("SELL" if c["c"] < p["l"] else None)
        if not d:
            continue
        sl = c["c"] - 1.5 * a[i] if d == "BUY" else c["c"] + 1.5 * a[i]
        tp = c["c"] + 3.0 * a[i] if d == "BUY" else c["c"] - 3.0 * a[i]  # 2R
        tr = simulate(c1, i, d, c["c"], sl, tp, cost, bars_until(t.hour, 20))
        done.add(t.date())
        if tr:
            trades.append(tr)
            busy = tr["exit"]
    return trades


def s3_asian_fade(c1, a, cost):
    closes = [c["c"] for c in c1]
    mid, up, lo = bbands(closes)
    trades, busy = [], None
    for i, c in enumerate(c1):
        t = c["t"]
        if not weekday_ok(t) or not 0 <= t.hour <= 5 or a[i] is None or mid[i] is None:
            continue
        if busy and t < busy:
            continue
        if c["c"] > up[i]:
            d, sl = "SELL", c["c"] + 1.5 * a[i]
        elif c["c"] < lo[i]:
            d, sl = "BUY", c["c"] - 1.5 * a[i]
        else:
            continue
        if abs(c["c"] - mid[i]) < 0.5 * abs(c["c"] - sl):
            continue
        tr = simulate(c1, i, d, c["c"], sl, mid[i], cost, bars_until(t.hour, 7))
        if tr:
            trades.append(tr)
            busy = tr["exit"]
    return trades


def s4_rsi_dip(c1, days, didx, a, cost):
    closes = [c["c"] for c in c1]
    r2 = rsi(closes, 2)
    dema = bt.calc_ema([x["c"] for x in days], 50)
    trades, busy = [], None
    for i, c in enumerate(c1):
        t = c["t"]
        if not weekday_ok(t) or t.hour > 20 or a[i] is None or r2[i] is None:
            continue
        if t.weekday() == 4 and t.hour >= 18:
            continue
        if busy and t < busy:
            continue
        k = didx[t.date()]
        if k == 0 or dema[k - 1] is None:
            continue
        bull = days[k - 1]["c"] > dema[k - 1]
        if bull and r2[i] < 10:
            d, sl = "BUY", c["c"] - 2 * a[i]
            ex = lambda j: r2[j] is not None and r2[j] > 60
        elif not bull and r2[i] > 90:
            d, sl = "SELL", c["c"] + 2 * a[i]
            ex = lambda j: r2[j] is not None and r2[j] < 40
        else:
            continue
        tr = simulate(c1, i, d, c["c"], sl, None, cost, 24, ex)
        if tr:
            trades.append(tr)
            busy = tr["exit"]
    return trades


def s5_donchian(c1, days, didx, cost):
    da = atr(days, 14)
    trades, busy = [], None
    for i, c in enumerate(c1):
        t = c["t"]
        if not weekday_ok(t) or t.hour > 20 or (t.weekday() == 4 and t.hour >= 18):
            continue
        if busy and t < busy:
            continue
        k = didx[t.date()]
        if k < 20 or da[k - 1] is None:
            continue
        hh = max(x["h"] for x in days[k - 20:k])
        ll = min(x["l"] for x in days[k - 20:k])
        if c["c"] > hh:
            d, sl = "BUY", c["c"] - 2 * da[k - 1]
        elif c["c"] < ll:
            d, sl = "SELL", c["c"] + 2 * da[k - 1]
        else:
            continue

        def ex(j, d=d):
            tj = c1[j]["t"]
            if tj.weekday() == 4 and tj.hour >= 19:
                return True  # flat before the weekend
            kj = didx[tj.date()]
            if kj < 10:
                return False
            if d == "BUY":
                return c1[j]["c"] < min(x["l"] for x in days[kj - 10:kj])
            return c1[j]["c"] > max(x["h"] for x in days[kj - 10:kj])

        tr = simulate(c1, i, d, c["c"], sl, None, cost, 24 * 6, ex)
        if tr:
            trades.append(tr)
            busy = tr["exit"]
    return trades


# ---------- FTMO Monte Carlo ----------

def weeks_of_daily_r(trades):
    """Group trades (R units) into weeks -> list of days -> total R that day (by entry date)."""
    daily = defaultdict(float)
    for t in trades:
        daily[t["time"].date()] += t["r"]
    weeks = defaultdict(list)
    for d in sorted(daily):
        weeks[d.isocalendar()[:2]].append(daily[d])
    all_weeks = []
    if daily:
        first, last = min(daily), max(daily)
        d = first - timedelta(days=first.weekday())
        while d <= last:
            all_weeks.append(weeks.get(d.isocalendar()[:2], []))  # empty weeks count too
            d += timedelta(days=7)
    return all_weeks


def ftmo_phase(weeks, risk, target, rng, max_weeks=104):
    """Returns ('pass'|'fail'|'timeout', weeks_used). Balance in % of the start."""
    bal = 100.0
    for w in range(max_weeks):
        for day_r in rng.choice(weeks):
            day_start = bal
            bal += day_r * risk
            if day_start - bal >= 5.0 or bal <= 90.0:
                return "fail", w + 1
            if bal >= 100.0 + target:
                return "pass", w + 1
    return "timeout", max_weeks


def ftmo_odds(trades, sims, seed=7):
    weeks = weeks_of_daily_r(trades)
    rows = []
    if not weeks:
        return rows
    for risk in RISKS:
        rng = random.Random(seed)
        both = p1 = fails = 0
        durations = []
        for _ in range(sims):
            r1, w1 = ftmo_phase(weeks, risk, 10.0, rng)
            if r1 == "fail":
                fails += 1
                continue
            if r1 != "pass":
                continue
            p1 += 1
            r2, w2 = ftmo_phase(weeks, risk, 5.0, rng)
            if r2 == "pass":
                both += 1
                durations.append(w1 + w2)
            elif r2 == "fail":
                fails += 1
        durations.sort()
        med = durations[len(durations) // 2] / 4.33 if durations else None
        rows.append((risk, p1 / sims * 100, both / sims * 100, fails / sims * 100, med))
    return rows


# ---------- reporting ----------

def fmt(x, nd=2):
    return "-" if x is None else str(round(x, nd))


def main():
    out = ["# Strategy research results", "",
           "Plan and pass rules: see RESEARCH.md (written before these tests ran).", ""]
    results = {}  # (strategy, market) -> trades

    for mi, (sym, cost) in enumerate(MARKETS):
        if mi:
            time.sleep(10)
        c1 = [c for c in bt.fetch("1h", PAGES, sym) if bt.market_open(c["t"])]
        days, didx = to_daily(c1)
        a = atr(c1, 14)
        results[("S2 Prev-day breakout", sym)] = s2_prev_day_breakout(c1, days, didx, a, cost)
        results[("S3 Asian fade", sym)] = s3_asian_fade(c1, a, cost)
        results[("S4 RSI(2) dip, daily trend", sym)] = s4_rsi_dip(c1, days, didx, a, cost)
        results[("S5 20-day breakout", sym)] = s5_donchian(c1, days, didx, cost)
        if sym == "XAU/USD":
            time.sleep(10)
            c4 = [c for c in bt.fetch("4h", 1, sym) if bt.market_open(c["t"])]
            s1 = bt.run(c1, c4, "live", "both", 2.0)
            s1b = bt.run(c1, c4, "live", "both", 4.0)
            results[("S1 EMA pullback 4R (bot from 20 Nov)", sym)] = [
                {"time": t["time"], "exit": t["time"], "dir": t["dir"], "r": t["r"], "result": t["result"]}
                for t in s1b]
            results[("S1 EMA pullback (bot)", sym)] = [
                {"time": t["time"], "exit": t["time"], "dir": t["dir"], "r": t["r"], "result": t["result"]}
                for t in s1]

    out.append("## Every strategy x market")
    out.append("")
    out.append("| Strategy | Market | 2024-25 trades | 2024-25 win% | 2024-25 net | 2024-25 PF | "
               "2026 trades | 2026 win% | 2026 net | 2026 PF | Max DD (all) | Result |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    survivors = []
    for (name, sym) in sorted(results):
        ts = results[(name, sym)]
        a = bt.stats([t for t in ts if t["time"] < SPLIT])
        b = bt.stats([t for t in ts if t["time"] >= SPLIT])
        al = bt.stats(ts)
        if not (a and b and al):
            out.append("| " + name + " | " + sym + " | not enough trades | | | | | | | | | FAIL |")
            continue
        ok = a["pf"] >= 1.2 and b["pf"] >= 1.2 and b["n"] >= 20
        if ok:
            survivors.append((name, sym))
        out.append("| " + name + " | " + sym + " | " + str(a["n"]) + " | " + fmt(a["wr"], 1) + "% | " +
                   fmt(a["net"], 1) + "R | " + fmt(a["pf"]) + " | " + str(b["n"]) + " | " + fmt(b["wr"], 1) +
                   "% | " + fmt(b["net"], 1) + "R | " + fmt(b["pf"]) + " | " + fmt(al["dd"], 1) + "R | " +
                   ("**PASS**" if ok else "fail") + " |")
    out.append("")

    out.append("## Survivors")
    out.append("")
    if not survivors:
        out.append("**None.** No strategy/market pair met the pass rules.")
    else:
        for s in survivors:
            out.append("- " + s[0] + " on " + s[1])
    out.append("")

    groups = [("Current bot alone (S1 gold)", [("S1 EMA pullback (bot)", "XAU/USD")])]
    if survivors:
        groups.append(("All survivors combined", survivors))
    for label, members in groups:
        trades = []
        for m in members:
            trades += results.get(m, [])
        trades.sort(key=lambda t: t["time"])
        st = bt.stats(trades)
        if not st:
            continue
        months = max((trades[-1]["time"] - trades[0]["time"]).days / 30.4, 0.1)
        out.append("## FTMO odds: " + label)
        out.append("")
        out.append(str(st["n"]) + " trades (" + fmt(st["n"] / months, 1) + "/month), net " + fmt(st["net"], 1) +
                   "R, profit factor " + fmt(st["pf"]) + ", max drawdown " + fmt(st["dd"], 1) +
                   "R, worst day " + fmt(st["worst_day"], 1) + "R.")
        out.append("")
        out.append("Simulated " + str(SIMS) + " challenges by re-shuffling real weeks. "
                   "Max loss 10%, daily loss 5%, Phase 1 +10%, Phase 2 +5%, give up after 2 years per phase.")
        out.append("")
        out.append("| Risk per trade | Pass Phase 1 | Pass both (funded) | Fail | Median months to funded |")
        out.append("|---|---|---|---|---|")
        for risk, p1, both, fails, med in ftmo_odds(trades, SIMS):
            out.append("| " + str(risk) + "% | " + fmt(p1, 1) + "% | " + fmt(both, 1) + "% | " + fmt(fails, 1) +
                       "% | " + fmt(med, 1) + " |")
        out.append("")

    if len(survivors) > 1:
        out.append("## Monthly correlation between survivors")
        out.append("")
        monthly = {}
        for m in survivors:
            mm = defaultdict(float)
            for t in results[m]:
                mm[t["time"].strftime("%Y-%m")] += t["r"]
            monthly[m] = mm
        keys = sorted(set(k for m in monthly.values() for k in m))
        names = [s[0].split(" ")[0] + " " + s[1] for s in survivors]
        out.append("| | " + " | ".join(names) + " |")
        out.append("|---" * (len(names) + 1) + "|")
        for x, nx in zip(survivors, names):
            row = []
            for y in survivors:
                xs = [monthly[x].get(k, 0.0) for k in keys]
                ys = [monthly[y].get(k, 0.0) for k in keys]
                mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
                cov = sum((p - mx) * (q - my) for p, q in zip(xs, ys))
                vx = sum((p - mx) ** 2 for p in xs) ** 0.5
                vy = sum((q - my) ** 2 for q in ys) ** 0.5
                row.append(fmt(cov / (vx * vy) if vx and vy else 0.0))
            out.append("| " + nx + " | " + " | ".join(row) + " |")
        out.append("")

    # Round 2: frozen near-misses, judged only on data after FRESH
    out.append("## Round 2: fresh data since " + FRESH.strftime("%Y-%m-%d"))
    out.append("")
    out.append("| Candidate | Trades | Win% | Net | PF | Status |")
    out.append("|---|---|---|---|---|---|")
    tg = ["XAUPullbackBot monthly research check", "Fresh data since " + FRESH.strftime("%d %b %Y"), ""]
    passed = []
    for m in ROUND2:
        start = datetime(2026, 11, 20, tzinfo=timezone.utc) if "4R" in m[0] else FRESH
        st = bt.stats([t for t in results.get(m, []) if t["time"] >= start])
        label = m[0] + " " + m[1]
        if not st:
            out.append("| " + label + " | 0 | - | - | - | waiting |")
            tg.append(label + ": 0 trades yet")
            continue
        status = "waiting" if st["n"] < 25 else ("PASS" if st["pf"] >= 1.2 else "fail")
        if status == "PASS":
            passed.append(m)
        out.append("| " + label + " | " + str(st["n"]) + " | " + fmt(st["wr"], 1) + "% | " + fmt(st["net"], 1) +
                   "R | " + fmt(st["pf"]) + " | " + status + " |")
        tg.append(label + ": " + str(st["n"]) + " trades, " + fmt(st["net"], 1) + "R, PF " + fmt(st["pf"]) +
                  " (" + status + ")")
    out.append("")
    if passed:
        trades = sorted([t for m in passed for t in results[m]], key=lambda t: t["time"])
        tg.append("")
        tg.append("Passed: " + ", ".join(m[0] + " " + m[1] for m in passed))
        out.append("**Passed so far:** " + ", ".join(m[0] + " " + m[1] for m in passed))
        out.append("")
        out.append("| Risk per trade | Pass Phase 1 | Pass both (funded) | Fail | Median months to funded |")
        out.append("|---|---|---|---|---|")
        for risk, p1, both, fails, med in ftmo_odds(trades, SIMS):
            out.append("| " + str(risk) + "% | " + fmt(p1, 1) + "% | " + fmt(both, 1) + "% | " + fmt(fails, 1) +
                       "% | " + fmt(med, 1) + " |")
            tg.append(str(risk) + "% risk: " + fmt(both, 0) + "% funded odds")
        out.append("")
    else:
        tg.append("")
        tg.append("Nothing has passed on fresh data yet. Keep forward testing, no paid challenge.")
    tg.append("Rules: need 25+ trades and PF 1.2+ on new data.")

    report = "\n".join(out)
    print(report)
    tok, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if tok and chat:
        try:
            import requests
            requests.post("https://api.telegram.org/bot" + tok + "/sendMessage",
                          json={"chat_id": chat, "text": "\n".join(tg)}, timeout=10)
        except Exception as e:
            print("Telegram summary failed: " + type(e).__name__)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()
