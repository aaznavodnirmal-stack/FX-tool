# Wise FX Watch

Checks Wise's SGD exchange rates every hour, updates an online dashboard, and emails you when there's a buy, sell or target-rate signal. It also sends a daily summary around 8am Singapore time. It runs free on GitHub, so your computer can stay off.

## Where the rates come from

The rates come from Wise's own public rate feed (`wise.com/rates/live` and `wise.com/rates/history+live`), the same one the Wise website uses for its converter and charts. You don't need an API token. Fees come from Wise's public price quotes. If those aren't available, the fee estimates in `watch.py` are used instead.

If Wise doesn't answer, the script falls back to ECB reference rates (Frankfurter). These are close to Wise's rates but not identical, so it emails you a warning when this happens. ECB has no LKR data, so LKR is skipped while the fallback is in use.

## Setup (about 15 minutes)

### 1. Gmail app password

1. Turn on 2-Step Verification for your Google account.
2. Go to <https://myaccount.google.com/apppasswords>, create an app password named "FX Watch", and copy the 16-character code.

### 2. GitHub repository

1. Sign in at github.com and click **New repository**. Name it `wise-fx-watch` and make it **Public** (free Pages and unlimited Actions minutes; only rates and your settings are visible, never your email or password). Create it.
2. Click **uploading an existing file** and drag in everything from this folder: `watch.py`, `dashboard_template.html`, `README.md`, `.gitignore` and the `.github` folder. Commit.
   - If the `.github` folder doesn't upload, click **Add file → Create new file** and name it `.github/workflows/watch.yml`. Paste in the contents of `watch.yml` and commit.

### 3. Secrets (so your password is never in the code)

Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Name | Value |
|---|---|
| `EMAIL_USER` | your Gmail address |
| `EMAIL_PASS` | the 16-character app password |
| `EMAIL_TO` | where alerts go (can be the same Gmail) |

### 4. Dashboard and first run

1. Go to **Settings → Pages** and set **Source** to **GitHub Actions**.
2. Go to **Actions**, click **FX Watch**, then **Run workflow**. After about a minute:
   - the dashboard is live at `https://<your-username>.github.io/wise-fx-watch/`
   - you get the first summary email (and any signals).

From then on, it runs every hour by itself.

## Telling it what you hold

Edit the `SETTINGS` block at the top of `watch.py`. In GitHub, open the file and click the pencil icon.

```python
HOLDINGS = [{"ccy": "USD", "amount": 15.44, "sgd": 20, "date": "2026-10-14"}]
TARGETS = {"LKR": 236.0}
```

- `HOLDINGS`: after you buy a currency in Wise, add it here. You get a **sell** email once converting it back would make at least `TAKE_PROFIT_PCT` after both fees. Remove it after you sell.
- `TARGETS`: you get an email when 1 SGD buys at least this much of that currency.
- `BUY_RANK`, `LOOKBACK_DAYS`, `COOLDOWN_HOURS` and `SUMMARY_HOUR_SGT` tune how picky and how chatty it is.

## What the signals mean

| Email | When |
|---|---|
| **Buy X with SGD** | Today's Wise rate beats `BUY_RANK` (85%) of the last 90 days and you don't already hold X |
| **Good day to send SGD to LKR/INR** | Same test, for currencies Wise can't hold |
| **Sell X back to SGD** | A holding is up at least `TAKE_PROFIT_PCT` after both fees |
| **X hit your target** | Rate reached a value in `TARGETS` |
| **Daily summary** | Every currency's rate, verdict, trend and likely 7-day range, with the dashboard attached |

The same signal isn't repeated within `COOLDOWN_HOURS`.

**Predictions.** For each currency, the script tests on the past year whether "the recent trend continues" or "the rate returns to its average" would have called 7-day moves correctly. It uses whichever was right at least 55% of the time. If neither was, it assumes a random walk. The "likely in 7 days" range is a 68% band. Exchange rates are mostly random over days and weeks, so treat signals as better or worse odds, not certainties.

## Running it on your own PC instead

Use this if Wise blocks GitHub's servers (you'll get the "ECB fallback" email), or if you'd rather keep everything local:

```
python watch.py --loop
```

To send emails from your PC, set `EMAIL_USER`, `EMAIL_PASS` and `EMAIL_TO` as Windows environment variables. To test without sending anything, run `python watch.py --demo --dry-run`; the emails are saved in `out/`.

## Notes

- GitHub can pause scheduled workflows in a repository with no activity for 60 days. The hourly data commits normally count as activity, but if emails stop, open **Actions** and re-enable the workflow.
- Rate history builds up in `data/rates.csv`, and every signal sent is logged in `data/signals.csv`.
