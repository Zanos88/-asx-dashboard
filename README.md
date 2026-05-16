# ASX + Solana Meme Coin Portfolio Dashboard

A production-grade Streamlit dashboard for tracking ASX small-cap stocks and Solana meme coin
positions, with real-time on-chain data, whale detection, holder concentration monitoring,
Telegram alerts, and X/Grok sentiment analysis.

---

## Architecture

| Component | Platform | Purpose |
|---|---|---|
| Streamlit dashboard | Streamlit Community Cloud | UI — portfolio, charts, sentiment |
| Webhook + cron API | Vercel (serverless) | Real-time Helius webhooks + 6h cron |
| Holder diff monitor | GitHub Actions (hourly) | Snapshot diff alerts with persistence |

> **Why two backend services?**  
> Vercel functions are stateless (no disk), so they send current state snapshots.  
> GitHub Actions has a file system, so it handles diff-based alerts.  
> Together they cover both real-time and scheduled monitoring.

---

## Secrets reference

| Key | Where to set | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Streamlit Cloud | Claude AI analysis |
| `ADMIN_PASSWORD` | Streamlit Cloud | Locks AI features |
| `HELIUS_API_KEY` | Streamlit Cloud + Vercel + GitHub Actions | On-chain data |
| `XAI_API_KEY` | Streamlit Cloud + Vercel + GitHub Actions | Grok / X sentiment |
| `TELEGRAM_BOT_TOKEN` | Streamlit Cloud + Vercel + GitHub Actions | Telegram delivery |
| `TELEGRAM_CHAT_ID` | Streamlit Cloud + Vercel + GitHub Actions | Target chat / channel |
| `HELIUS_WEBHOOK_SECRET` | Vercel | HMAC signature verification |
| `WHALE_THRESHOLD_USD` | Vercel | Min USD for whale alert (default 10000) |
| `SENTIMENT_TOKENS` | Vercel | Comma-separated symbols e.g. `ALON` |
| `CRON_SECRET` | Vercel | Optional protection for `/api/cron` |

---

## Step 1 — Deploy the Streamlit dashboard

1. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**.
2. Select your repo, branch `main`, main file: `asx_dashboard.py`.
3. Click **Advanced settings → Secrets** and paste:

```toml
ANTHROPIC_API_KEY  = "sk-ant-..."
ADMIN_PASSWORD     = "your-password"
HELIUS_API_KEY     = "your-helius-key"
XAI_API_KEY        = "xai-..."
TELEGRAM_BOT_TOKEN = "123456:ABC..."
TELEGRAM_CHAT_ID   = "-100123456789"
```

4. Click **Deploy**. Note your app URL (e.g. `https://yourapp.streamlit.app`).

---

## Step 2 — Deploy the Vercel API (webhook + cron)

### 2a. Install Vercel CLI and link

```bash
npm install -g vercel
vercel login
vercel link   # run from the repo root, select your project
```

### 2b. Set environment variables in Vercel

```bash
vercel env add TELEGRAM_BOT_TOKEN
vercel env add TELEGRAM_CHAT_ID
vercel env add HELIUS_API_KEY
vercel env add XAI_API_KEY
vercel env add HELIUS_WEBHOOK_SECRET
vercel env add WHALE_THRESHOLD_USD     # e.g. 10000
vercel env add SENTIMENT_TOKENS        # e.g. ALON
vercel env add CRON_SECRET             # any random string
```

Or add them in the Vercel dashboard: **Project → Settings → Environment Variables**.

### 2c. Deploy

```bash
vercel --prod
```

Note your deployment URL (e.g. `https://your-project.vercel.app`).

> **Vercel cron requires a Pro plan** ($20/month). On the free Hobby plan,
> call `/api/cron` from GitHub Actions instead (see Step 4b).

---

## Step 3 — Set up Helius webhook

Helius fires a POST to your Vercel URL on every matching transaction in real-time.

1. Go to [dev.helius.xyz](https://dev.helius.xyz) → **Webhooks** → **New Webhook**.
2. Fill in the form:

| Field | Value |
|---|---|
| **Webhook URL** | `https://your-project.vercel.app/webhook/helius` |
| **Transaction types** | `TOKEN_TRANSFER`, `SWAP` |
| **Account addresses** | Your token mint address(es), one per line |
| **Auth header** | Same value you set for `HELIUS_WEBHOOK_SECRET` |

3. Add your token mint addresses (one per line):
   ```
   8XtRWb4uAAJFMP4QQhoYYCWR6XXb7ybcCdiqPwz9s5WS
   ```
4. Click **Create Webhook**.
5. Click **Test** — you should receive a Telegram whale alert within seconds.

> **Tip:** You can monitor multiple token mints in a single webhook. Helius fires
> for any transaction touching any of the listed addresses.

---

## Step 4 — Set up GitHub Actions (hourly holder diff monitor)

GitHub Actions runs `monitor.py` every hour. Unlike Vercel, it persists snapshots
to disk and commits them back to the repo, enabling diff-based alerts.

### 4a. Add secrets to GitHub

Go to your repo → **Settings → Secrets → Actions** → **New repository secret**:

```
HELIUS_API_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
XAI_API_KEY
```

### 4b. Optional — trigger Vercel cron from GitHub Actions (free alternative)

If you are on Vercel's free Hobby plan, add this step to `.github/workflows/monitor.yml`
to call the cron endpoint every 6 hours from GitHub Actions instead:

```yaml
- name: Trigger Vercel cron
  env:
    CRON_SECRET: ${{ secrets.CRON_SECRET }}
  run: |
    curl -X GET "https://your-project.vercel.app/api/cron" \
      -H "x-cron-secret: $CRON_SECRET"
```

Add `CRON_SECRET` to GitHub Actions secrets (same value as Vercel env var).

---

## Step 5 — Set up Telegram bot

1. Message `@BotFather` on Telegram → `/newbot` → follow the prompts → copy the token.
2. Start a chat with your bot (or add it to a group or channel).
3. Get your chat ID:
   - **Personal chat:** message `@userinfobot`
   - **Group:** add `@userinfobot` to the group
   - **Channel:** forward a channel message to `@userinfobot`
4. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` everywhere listed in the secrets table.

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `POST` | `/webhook/helius` | Helius real-time transaction webhook |
| `POST` | `/trigger/sentiment` | Manually trigger X sentiment digest |
| `GET` | `/api/cron` | Vercel/GitHub Actions cron — holder status + sentiment |

### Test the webhook manually

```bash
curl -X POST https://your-project.vercel.app/trigger/sentiment \
  -H "x-api-key: your-HELIUS_WEBHOOK_SECRET"
```

### Test the cron manually

```bash
curl https://your-project.vercel.app/api/cron \
  -H "x-cron-secret: your-CRON_SECRET"
```

---

## Local development

```bash
# Install dependencies
pip install -r requirements.txt

# Create Streamlit secrets
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

# Run API locally (webhook receiver)
TELEGRAM_BOT_TOKEN=... HELIUS_API_KEY=... uvicorn api.index:app --reload --port 8000

# Run monitor manually
HELIUS_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python monitor.py
```

---

## File structure

```
.
├── asx_dashboard.py              # Streamlit dashboard (Streamlit Community Cloud)
├── monitor.py                    # Holder diff monitor + sentiment (GitHub Actions)
├── api/
│   └── index.py                  # Vercel serverless API (webhook + cron)
├── vercel.json                   # Vercel routing + cron schedule
├── requirements.txt              # Python dependencies
├── snapshots/                    # Holder snapshots committed by GitHub Actions
│   └── ALON_holders.json
├── .github/
│   └── workflows/
│       └── monitor.yml           # Hourly cron job
└── .streamlit/
    └── secrets.toml              # Local secrets (never committed)
```

---

## Disclaimer

Not financial advice. Meme coins are extremely high risk. All data is provided for informational purposes only.
