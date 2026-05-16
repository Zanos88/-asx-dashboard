"""
Create (or update) the Helius webhook for all tokens in config.json.

Usage:
    set HELIUS_API_KEY=your-key
    set VERCEL_URL=https://your-project.vercel.app
    set HELIUS_WEBHOOK_SECRET=your-secret   (optional — same value as in Vercel env)
    python setup_helius_webhook.py

What it does:
    1. Reads token mint addresses from config.json.
    2. Lists existing Helius webhooks so you can see what's already configured.
    3. Creates a new enhanced webhook pointing at /webhook/helius on your Vercel URL.
       (Does NOT delete existing webhooks — use delete_helius_webhooks.py for that.)
"""

import json
import os
import sys

import requests

HELIUS_API_KEY        = os.environ.get("HELIUS_API_KEY", "")
VERCEL_URL            = os.environ.get("VERCEL_URL", "").rstrip("/")
HELIUS_WEBHOOK_SECRET = os.environ.get("HELIUS_WEBHOOK_SECRET", "")

BASE_URL    = "https://api.helius.xyz/v0"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
SEP         = "-" * 60


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"❌ Could not read config.json: {exc}")
        sys.exit(1)


def list_webhooks() -> list[dict]:
    resp = requests.get(
        f"{BASE_URL}/webhooks",
        params={"api-key": HELIUS_API_KEY},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def create_webhook(addresses: list[str], webhook_url: str, auth_header: str) -> dict:
    body: dict = {
        "webhookURL":       webhook_url,
        "transactionTypes": ["TOKEN_TRANSFER", "SWAP"],
        "accountAddresses": addresses,
        "webhookType":      "enhanced",
    }
    if auth_header:
        body["authHeader"] = auth_header

    resp = requests.post(
        f"{BASE_URL}/webhooks",
        params={"api-key": HELIUS_API_KEY},
        json=body,
        timeout=10,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError:
        print(f"❌ Helius API error {resp.status_code}: {resp.text[:400]}")
        sys.exit(1)

    return resp.json()


def main() -> None:
    print(SEP)
    print("HELIUS WEBHOOK SETUP")
    print(SEP)

    # ── Validate env ──────────────────────────────────────────────────────────
    if not HELIUS_API_KEY:
        print("❌ HELIUS_API_KEY is not set")
        sys.exit(1)
    print(f"API key : {HELIUS_API_KEY[:8]}...{HELIUS_API_KEY[-4:]}")

    # ── Load tokens from config.json ──────────────────────────────────────────
    cfg    = load_config()
    tokens = cfg.get("solana_tokens", {})

    if not tokens:
        print("❌ No tokens found in config.json → solana_tokens")
        sys.exit(1)

    print(f"\nTokens in config.json:")
    addresses = []
    for sym, info in tokens.items():
        print(f"  {sym:10s}  {info['address']}")
        addresses.append(info["address"])

    # ── List existing webhooks ────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("EXISTING WEBHOOKS")
    print(SEP)
    try:
        existing = list_webhooks()
    except requests.HTTPError as exc:
        print(f"❌ Failed to list webhooks: {exc}")
        sys.exit(1)

    if not existing:
        print("  (none)")
    else:
        for wh in existing:
            print(f"  ID      : {wh.get('webhookID')}")
            print(f"  URL     : {wh.get('webhookURL')}")
            print(f"  Type    : {wh.get('webhookType')}")
            addrs = wh.get("accountAddresses", [])
            print(f"  Addresses ({len(addrs)}): {', '.join(addrs)}")
            print()

    # ── Create new webhook ────────────────────────────────────────────────────
    print(SEP)
    print("CREATE WEBHOOK")
    print(SEP)

    if not VERCEL_URL:
        print("⚠️  VERCEL_URL is not set — skipping webhook creation.")
        print("   Set VERCEL_URL=https://your-project.vercel.app and re-run.")
        return

    webhook_url = f"{VERCEL_URL}/webhook/helius"
    print(f"  Webhook URL : {webhook_url}")
    print(f"  Auth header : {'set (' + HELIUS_WEBHOOK_SECRET[:4] + '...)' if HELIUS_WEBHOOK_SECRET else 'not set'}")
    print(f"  Addresses   : {len(addresses)} token(s)")
    print()

    result = create_webhook(addresses, webhook_url, HELIUS_WEBHOOK_SECRET)

    print(f"✅ Webhook created successfully")
    print(f"  ID        : {result.get('webhookID')}")
    print(f"  URL       : {result.get('webhookURL')}")
    print(f"  Type      : {result.get('webhookType')}")
    print(f"  Addresses : {result.get('accountAddresses')}")
    print()
    print("Next step: paste the webhook ID above into your Helius dashboard")
    print("and set the same HELIUS_WEBHOOK_SECRET in your Vercel env vars.")


if __name__ == "__main__":
    main()
