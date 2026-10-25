# Wolf dashboard

A read-only window onto the bot. The bot keeps running on Railway; this
Next.js app runs on Vercel and reads the bot's API on the server, so the API
key never reaches the browser.

Pages: **Overview** (paper balance net of costs, the ledger line, Monte Carlo,
every week in the log) and **Sinyal aktif** (open signals with a chart and
their entry, stop and targets drawn on it).

## Setup, once

**Railway (the bot)**
1. Variables → add `API_KEY` = a long random string. Do this *before* step 2:
   with a public domain and no key, the API refuses everything (503).
2. Settings → Networking → **Generate Domain**. Copy the URL.
3. Settings → **Watch Paths**: `/**` and `!/dashboard/**`, so editing the
   dashboard does not restart the bot.

**Vercel (this folder)**
1. Add New → Project → import this repo.
2. **Root Directory**: `dashboard`.
3. Environment variables:
   - `WOLF_API_URL` = the Railway URL (`https://….up.railway.app`)
   - `WOLF_API_KEY` = the same value as the bot's `API_KEY`
   - `DASHBOARD_PASSWORD` = a password for this site (any username works)
4. Settings → Git → **Ignored Build Step**: `git diff --quiet HEAD^ HEAD -- .`
   so bot-only commits do not rebuild the site.

## Local

```bash
cp .env.example .env.local   # fill in the three values
npm install && npm run dev
```

Charts load candles from Binance's public data host in the viewer's browser;
a symbol Binance does not list shows a message instead of a chart.
