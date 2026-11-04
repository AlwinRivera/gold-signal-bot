# gold-signal-bot 🥇

XAU/USD EMA Pullback signal scanner — runs hourly via GitHub Actions, sends Telegram alerts.

## Setup

### 1. Fork or create this repo on GitHub

### 2. Add GitHub Secrets
Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add these three secrets:

| Name | Value |
|------|-------|
| `TD_KEY` | Your Twelve Data API key |
| `TG_TOKEN` | Your Telegram bot token |
| `TG_CHAT_ID` | Your Telegram chat ID |

### 3. Enable Actions
Go to the **Actions** tab in your repo and click **"I understand my workflows, go ahead and enable them"** if prompted.

### 4. Test it
Click **Actions** → **XAU/USD Signal Scan** → **Run workflow** to trigger a manual scan.

## Strategy
- **EMA Pullback to 20 EMA** on XAU/USD 1H
- Sessions: London (PHT 15–17) and NY (PHT 21–23)
- Touch tolerance: 0.1% of EMA price
- Body filter: ≥35% of candle range
- SL: 0.7× candle size beyond 20 EMA
- TP: 1.5R
- Backtest: ~50% WR, +0.25R/trade expectancy
