"""
List and delete Helius webhooks.

Usage:
    set HELIUS_API_KEY=your-key
    python delete_helius_webhooks.py              # lists all webhooks
    python delete_helius_webhooks.py --all        # deletes ALL webhooks (prompts first)
    python delete_helius_webhooks.py <webhookID>  # deletes one specific webhook
"""

import os
import sys

import requests

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
BASE_URL       = "https://api.helius.xyz/v0"
SEP            = "-" * 60


def list_webhooks() -> list[dict]:
    resp = requests.get(
        f"{BASE_URL}/webhooks",
        params={"api-key": HELIUS_API_KEY},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def delete_webhook(webhook_id: str) -> bool:
    resp = requests.delete(
        f"{BASE_URL}/webhooks/{webhook_id}",
        params={"api-key": HELIUS_API_KEY},
        timeout=10,
    )
    if resp.status_code == 200:
        return True
    print(f"  ❌ Delete failed {resp.status_code}: {resp.text[:200]}")
    return False


def print_webhook(wh: dict) -> None:
    print(f"  ID        : {wh.get('webhookID')}")
    print(f"  URL       : {wh.get('webhookURL')}")
    print(f"  Type      : {wh.get('webhookType')}")
    addrs = wh.get("accountAddresses", [])
    print(f"  Addresses : {len(addrs)} — {', '.join(addrs)}")


def main() -> None:
    print(SEP)
    print("HELIUS WEBHOOK MANAGER")
    print(SEP)

    if not HELIUS_API_KEY:
        print("❌ HELIUS_API_KEY is not set")
        sys.exit(1)

    print(f"API key: {HELIUS_API_KEY[:8]}...{HELIUS_API_KEY[-4:]}\n")

    try:
        webhooks = list_webhooks()
    except requests.HTTPError as exc:
        print(f"❌ Failed to list webhooks: {exc}")
        sys.exit(1)

    if not webhooks:
        print("No webhooks found.")
        return

    print(f"Found {len(webhooks)} webhook(s):\n")
    for wh in webhooks:
        print_webhook(wh)
        print()

    args = sys.argv[1:]
    if not args:
        return  # list only

    # ── Delete all ────────────────────────────────────────────────────────────
    if args[0] == "--all":
        print(SEP)
        confirm = input(f"Delete ALL {len(webhooks)} webhook(s)? Type YES to confirm: ").strip()
        if confirm != "YES":
            print("Aborted.")
            return
        for wh in webhooks:
            wid = wh.get("webhookID", "")
            if delete_webhook(wid):
                print(f"  ✅ Deleted {wid}")
        return

    # ── Delete specific ID ────────────────────────────────────────────────────
    target_id = args[0]
    known_ids = [wh.get("webhookID") for wh in webhooks]
    if target_id not in known_ids:
        print(f"❌ Webhook ID '{target_id}' not found in your account.")
        print(f"   Known IDs: {', '.join(known_ids)}")
        sys.exit(1)

    print(SEP)
    if delete_webhook(target_id):
        print(f"✅ Deleted webhook {target_id}")


if __name__ == "__main__":
    main()
