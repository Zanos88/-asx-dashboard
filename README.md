# ASX + Solana Meme Coin Portfolio Dashboard

A production-grade Streamlit dashboard for tracking ASX small-cap stocks and Solana meme coin positions. Includes real-time on-chain data, whale detection, holder concentration monitoring with Telegram alerts, and X/Grok sentiment analysis.

---

## Features

| Feature | Source |
|---|---|
| ASX portfolio P&L, charts, milestones | Yahoo Finance |
| Solana token price, liquidity, volume | DexScreener |
| Holder concentration (top 20 wallets) | Helius RPC |
| Whale transaction detection | Helius REST |
| Real-time whale alerts | Telegram Bot |
| Holder change monitoring (hourly cron) | GitHub Actions |
| X / Twitter sentiment analysis | Grok (xAI API) |
| AI portfolio analysis | Anthropic Claude |

---

## Secrets reference

| Key | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Claude AI (console.anthropic.com) |
| `ADMIN_PASSWORD` | Yes | Locks AI analysis behind a password |
| `HELIUS_API_KEY` | Yes | On-chain data (dev.helius.xyz) |
| `XAI_API_KEY` | Yes | Grok / X sentiment (console.x.ai) |
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | Chat or channel to receive alerts |
| `HELIUS_WEBHOOK_SECRET` | No | HMAC secret for webhook verification |
| `WHALE_THRESHOLD_USD` | No | Minimum USD for whale alert (default 10000) |

---

## Option A — Streamlit Community Cloud (simplest)

1. Fork or push this repo to GitHub.
2. Go to [share.streamlit.io](https://share.streamlit.io) and click **New app**.
3. Select your repo, branch `main`, and set **Main file path** to `asx_dashboard.py`.
4. Click **Advanced settings → Secrets** and paste:

```toml
ANTHROPIC_API_KEY  = "sk-ant-..."
ADMIN_PASSWORD     = "your-password"
HELIUS_API_KEY     = "your-helius-key"
XAI_API_KEY        = "xai-..."
TELEGRAM_BOT_TOKEN = "123456:ABC..."
TELEGRAM_CHAT_ID   = "-100123456789"
```

5. Click **Deploy**.

---

## Option B — Railway (recommended for webhook support)

Railway runs two services from the same repo:
- **Dashboard** — Streamlit app
- **Webhook** — FastAPI receiver for real-time Helius events

### Prerequisites

- [Railway account](https://railway.app) (free tier works)
- Railway CLI: `npm install -g @railway/cli` then `railway login`

---

### Step 1 — Deploy the Dashboard service

1. Go to [railway.app/new](https://railway.app/new) → **Deploy from GitHub repo**.
2. Select your repo.
3. Railway will auto-detect `railway.toml` and use it. If not, set manually:
   - **Start command:** `streamlit run asx_dashboard.py --server.port $PORT --server.address 0.0.0.0 --server.headless true`
4. Click **Add variables** and add all secrets from the table above.
5. Under **Settings → Networking**, click **Generate Domain** to get a public URL.
6. Note the URL (e.g. `https://asx-dashboard-production.up.railway.app`).

The `railway.toml` already sets:
- `restartPolicyType = "on_failure"` with 10 max retries
- Health check at `/_stcore/health`

---

### Step 2 — Deploy the Webhook service

The webhook service receives real-time Helius transaction notifications.

1. In Railway, open your project and click **New Service → GitHub Repo** (same repo).
2. Override the start command:
   ```
   uvicorn webhook:app --host 0.0.0.0 --port $PORT
   ```
3. Add the same environment variables as the dashboard, plus:
   ```
   WHALE_THRESHOLD_USD   = 10000
   HELIUS_WEBHOOK_SECRET = your-random-secret-string
   SENTIMENT_TOKENS      = ALON
   ```
4. Under **Settings → Networking**, click **Generate Domain**.
5. Note the webhook URL (e.g. `https://webhook-production.up.railway.app`).

To add a custom `railway.toml` for the webhook service, create `railway.webhook.toml`
and reference it in Railway's service settings, or configure via the UI.

---

### Step 3 — Set up Helius webhook

1. Go to [dev.helius.xyz](https://dev.helius.xyz) → **Webhooks** → **New Webhook**.
2. Fill in:
   - **Webhook URL:** `https://webhook-production.up.railway.app/webhook/helius`
   - **Transaction types:** Select `TOKEN_TRANSFER` (and optionally `SWAP`)
   - **Account addresses:** Paste the token mint address(es) to monitor:
     ```
     8XtRWb4uAAJFMP4QQhoYYCWR6XXb7ybcCdiqPwz9s5WS
     ```
   - **Auth header:** Paste the same value you set for `HELIUS_WEBHOOK_SECRET`
3. Click **Create**.
4. Test it: click **Test** in the Helius dashboard. You should receive a Telegram message.

> **Tip:** You can add multiple mint addresses in one webhook. Helius will fire the
> webhook for transactions involving any of the monitored addresses.

---

### Step 4 — Set up Telegram bot

1. Message `@BotFather` on Telegram → `/newbot` → follow prompts → copy the token.
2. Start a chat with your bot (or add it to a group/channel).
3. To get your chat ID:
   - For a personal chat: message `@userinfobot`.
   - For a group: add `@userinfobot` to the group and it will reply with the group ID.
   - For a channel: forward a message from the channel to `@userinfobot`.
4. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in both Railway services.

---

### Step 5 — Set up GitHub Actions (hourly holder monitor)

GitHub Actions runs `monitor.py` every hour independently of the Railway services.

1. In your GitHub repo → **Settings → Secrets → Actions**, add:
   ```
   HELIUS_API_KEY
   TELEGRAM_BOT_TOKEN
   TELEGRAM_CHAT_ID
   XAI_API_KEY
   ```
2. The workflow at `.github/workflows/monitor.yml` is already configured.
3. To trigger manually: **Actions → Holder Monitor → Run workflow**.
4. On first run it creates baseline snapshots; subsequent runs diff and alert.

To also receive X sentiment digests from GitHub Actions (not just the dashboard),
ensure `XAI_API_KEY` is set in GitHub secrets. The monitor sends a sentiment digest
after each holder check.

---

## Local development

```bash
# Clone
git clone https://github.com/Zanos88/-asx-dashboard.git
cd -asx-dashboard

# Install dependencies
pip install -r requirements.txt

# Create secrets file
mkdir -p .streamlit
cat > .streamlit/secrets.toml << 'EOF'
ANTHROPIC_API_KEY  = "sk-ant-..."
ADMIN_PASSWORD     = "your-password"
HELIUS_API_KEY     = "your-helius-key"
XAI_API_KEY        = "xai-..."
TELEGRAM_BOT_TOKEN = "123456:ABC..."
TELEGRAM_CHAT_ID   = "-100123456789"
EOF

# Run dashboard
streamlit run asx_dashboard.py

# Run webhook receiver (separate terminal)
uvicorn webhook:app --reload --port 8000

# Run monitor once manually
HELIUS_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python monitor.py
```

---

## File structure

```
.
├── asx_dashboard.py          # Streamlit dashboard (main app)
├── monitor.py                # Holder monitor + sentiment dispatcher (cron)
├── webhook.py                # FastAPI webhook receiver (Railway service)
├── railway.toml              # Railway config for dashboard service
├── requirements.txt          # Python dependencies
├── snapshots/                # Holder snapshot JSON files (committed by GH Actions)
│   └── ALON_holders.json
├── .github/
│   └── workflows/
│       └── monitor.yml       # Hourly GitHub Actions cron job
└── .streamlit/
    └── secrets.toml          # Local secrets (never committed)
```

---

## Disclaimer

Not financial advice. Meme coins are extremely high risk. All data is provided for informational purposes only.
