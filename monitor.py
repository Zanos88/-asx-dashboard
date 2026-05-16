"""
Holder concentration monitor — run standalone or via GitHub Actions cron.
Env vars: HELIUS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import json
import os
import requests
from datetime import datetime

HELIUS_API_KEY     = os.environ.get("HELIUS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "snapshots")

TOKENS = {
    "ALON": "8XtRWb4uAAJFMP4QQhoYYCWR6XXb7ybcCdiqPwz9s5WS",
}

# Alert thresholds
MOVE_THRESHOLD_PCT = 1.0   # alert if wallet's % of supply changes by this much


def fetch_holders(token_address):
    url     = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    payload = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  "getTokenLargestAccounts",
        "params":  [token_address],
    }
    resp = requests.post(url, json=payload, timeout=15)
    return resp.json().get("result", {}).get("value", [])


def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  ⚠️  Telegram not configured — skipping alert")
        return
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
        timeout=10,
    )
    if resp.ok:
        print("  ✅ Telegram alert sent")
    else:
        print(f"  ❌ Telegram error: {resp.text}")


def load_snapshot(symbol):
    path = os.path.join(SNAPSHOT_DIR, f"{symbol}_holders.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def save_snapshot(symbol, holders):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOT_DIR, f"{symbol}_holders.json")
    with open(path, "w") as f:
        json.dump(
            {"timestamp": datetime.utcnow().isoformat(), "holders": holders},
            f, indent=2,
        )


def get_amt(h):
    ui = h.get("uiAmount")
    return float(ui) if ui is not None else float(h.get("amount", 0))


def compare_holders(old_holders, new_holders):
    old_map   = {h["address"]: h for h in old_holders}
    new_map   = {h["address"]: h for h in new_holders}
    old_total = sum(get_amt(h) for h in old_holders) or 1
    new_total = sum(get_amt(h) for h in new_holders) or 1

    changes = []

    for addr, h in new_map.items():
        if addr not in old_map:
            pct = get_amt(h) / new_total * 100
            changes.append({"type": "NEW", "address": addr, "old_pct": None, "new_pct": pct, "delta": pct})

    for addr, h in old_map.items():
        if addr not in new_map:
            pct = get_amt(h) / old_total * 100
            changes.append({"type": "EXIT", "address": addr, "old_pct": pct, "new_pct": None, "delta": -pct})

    for addr in set(old_map) & set(new_map):
        old_pct = get_amt(old_map[addr]) / old_total * 100
        new_pct = get_amt(new_map[addr]) / new_total * 100
        delta   = new_pct - old_pct
        if abs(delta) >= MOVE_THRESHOLD_PCT:
            changes.append({"type": "MOVE", "address": addr, "old_pct": old_pct, "new_pct": new_pct, "delta": delta})

    return changes


def format_message(symbol, changes, snapshot_ts):
    lines = [
        f"🚨 <b>Holder Alert — {symbol}</b>",
        f"📅 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        f"vs snapshot: {snapshot_ts[:16].replace('T', ' ')} UTC\n",
    ]
    for c in changes:
        addr = f"{c['address'][:6]}...{c['address'][-4:]}"
        if c["type"] == "NEW":
            lines.append(f"🆕 <b>NEW</b> wallet entered top 20\n   <code>{addr}</code> → {c['new_pct']:.2f}%")
        elif c["type"] == "EXIT":
            lines.append(f"🚪 <b>EXIT</b> wallet left top 20\n   <code>{addr}</code> was {c['old_pct']:.2f}%")
        else:
            arrow = "📈" if c["delta"] > 0 else "📉"
            lines.append(
                f"{arrow} <b>MOVE</b>  <code>{addr}</code>\n"
                f"   {c['old_pct']:.2f}% → {c['new_pct']:.2f}% ({c['delta']:+.2f}%)"
            )
    return "\n".join(lines)


def run():
    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}] Starting holder monitor")

    for symbol, address in TOKENS.items():
        print(f"\n── {symbol} ({address[:8]}...)")

        current = fetch_holders(address)
        if not current:
            print("  No holder data returned — skipping")
            continue
        print(f"  Fetched {len(current)} holders")

        snapshot = load_snapshot(symbol)

        if snapshot:
            changes = compare_holders(snapshot["holders"], current)
            if changes:
                print(f"  {len(changes)} change(s) detected")
                msg = format_message(symbol, changes, snapshot["timestamp"])
                send_telegram(msg)
            else:
                print("  No significant changes")
        else:
            print("  No previous snapshot — creating baseline")
            send_telegram(
                f"📸 <b>{symbol}</b> — baseline snapshot created.\n"
                f"{len(current)} holders tracked.\n"
                f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
            )

        save_snapshot(symbol, current)
        print(f"  Snapshot saved → snapshots/{symbol}_holders.json")


if __name__ == "__main__":
    run()
