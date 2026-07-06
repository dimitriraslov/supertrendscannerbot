# Supertrend 1D Flip Scanner

Runs every 4 hours on GitHub Actions. Sends Telegram alerts when any ticker flips bull or bear on the daily Supertrend (ATR 10, Factor 3.0).

## Setup (one time, takes 10 minutes)

### Step 1 — Create a new GitHub repository

1. Go to github.com → click **New repository**
2. Name it `supertrend-scanner`
3. Set it to **Private** (keeps your watchlist and API keys safe)
4. Check **Add a README file**
5. Click **Create repository**

### Step 2 — Upload these files

Upload all 4 files to the root of your repository:
- `scanner.py`
- `watchlist.json`
- `state.json`
- `README.md`

Then create this folder structure for the workflow:
- Create folder `.github/workflows/`
- Upload `scanner.yml` inside that folder

### Step 3 — Add your secrets

Go to your repository → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add these 3 secrets exactly as named:

| Secret name | Value |
|-------------|-------|
| `TWELVE_DATA_KEY` | Your Twelve Data API key |
| `TELEGRAM_TOKEN` | Your Telegram bot token |
| `TELEGRAM_CHAT` | Your Telegram chat ID |

### Step 4 — Test it

1. Go to **Actions** tab in your repo
2. Click **Supertrend 1D Scanner**
3. Click **Run workflow** → **Run workflow**
4. Watch it run — check your Telegram for a summary message

### Step 5 — It runs automatically from now on

The scanner runs every 4 hours automatically. You will receive:
- An **individual alert** the moment any ticker flips
- A **summary message** at the end of each scan (only if there were flips or errors)

## Adding new tickers

Edit `watchlist.json` and add to any narrative section:

```json
{"sym": "TICKER", "name": "Company Name", "td": "TICKER"}
```

For crypto use the format: `"td": "BTC/USD"`

## Credit usage

- ~106 assets × 1 daily candle request = 106 credits per scan
- 6 scans per day = 636 credits
- Free tier limit = 800 credits/day
- Leaves ~164 credits for your dashboard ✅

## Adjusting scan frequency

Edit `scanner.yml` and change the cron schedule:
- Every 4h: `0 0,4,8,12,16,20 * * *`
- Every 6h: `0 0,6,12,18 * * *` (saves credits)
- Every 8h: `0 0,8,16 * * *` (most conservative)
