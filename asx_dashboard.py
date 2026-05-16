"""
ASX + Solana Meme Coin Portfolio Dashboard
==========================================
A Streamlit dashboard for tracking ASX small-cap stocks and Solana meme coin
positions, with on-chain health metrics, whale detection, holder concentration
monitoring, X/Grok sentiment analysis, and AI-powered portfolio analysis.

Secrets required (Streamlit secrets.toml or Community Cloud):
    ANTHROPIC_API_KEY   Claude AI for portfolio analysis
    ADMIN_PASSWORD      Locks AI features behind a password
    HELIUS_API_KEY      On-chain data (holders, transactions)
    XAI_API_KEY         Grok / X real-time sentiment analysis
    TELEGRAM_BOT_TOKEN  Telegram alert delivery
    TELEGRAM_CHAT_ID    Telegram target chat or channel
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import warnings
from datetime import date, datetime
from typing import Any

import anthropic
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf

warnings.filterwarnings("ignore")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

# ── Page config (must be first Streamlit call) ────────────────────────────────
st.set_page_config(
    page_title="Portfolio Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Theme ─────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    html, body, [class*="css"] {
        background-color: #0d1117;
        color: #f0f6fc;
        font-family: 'Inter', sans-serif;
    }
    .stApp { background-color: #0d1117; }
    section[data-testid="stSidebar"] {
        background-color: #161b22;
        border-right: 2px solid #30363d;
    }
    section[data-testid="stSidebar"] * { color: #f0f6fc !important; }
    section[data-testid="stSidebar"] label { font-weight: 600; }
    h1, h2, h3, h4, h5, h6 { color: #f0f6fc !important; font-weight: 700; }
    p, li, span, div { color: #d1d9e0; }
    .stButton > button {
        background-color: #238636;
        color: #ffffff;
        border: 1px solid #2ea043;
        border-radius: 6px;
        font-weight: 600;
    }
    .stButton > button:hover { background-color: #2ea043; border-color: #3fb950; }
    .stTextInput > div > div > input {
        background-color: #21262d;
        color: #f0f6fc;
        border: 1px solid #484f58;
    }
    .stSelectbox > div > div {
        background-color: #21262d;
        color: #f0f6fc;
        border: 1px solid #484f58;
    }
    div[data-testid="metric-container"] {
        background-color: #1c2128;
        border: 1px solid #444c56;
        border-radius: 8px;
        padding: 14px;
    }
    div[data-testid="metric-container"] label {
        color: #adbac7 !important;
        font-size: 13px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    div[data-testid="metric-container"] div[data-testid="stMetricValue"] {
        color: #f0f6fc !important;
        font-size: 22px;
        font-weight: 700;
    }
    details { background-color: #1c2128; border: 1px solid #444c56; border-radius: 8px; }
    details summary { color: #f0f6fc !important; font-weight: 600; font-size: 15px; }
    .stDataFrame { border: 1px solid #444c56; border-radius: 8px; }
    .stRadio label { color: #d1d9e0 !important; font-size: 14px; }
    .stCaption, small { color: #adbac7 !important; }
    .signal-badge {
        padding: 4px 12px;
        border-radius: 12px;
        font-size: 13px;
        font-weight: 700;
        letter-spacing: 0.5px;
    }
    .pill { padding: 3px 10px; border-radius: 10px; font-size: 12px; font-weight: 600; }
    .warn-flag { color: #ff7b72; font-size: 13px; font-weight: 600; }
    .safe-flag { color: #56d364; font-size: 13px; font-weight: 600; }
    hr { border-color: #30363d; }
</style>
""", unsafe_allow_html=True)

# ── Config (config.json — editable from GitHub mobile app) ───────────────────
def _load_config() -> dict[str, Any]:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not load config.json (%s) — using defaults", exc)
        return {}

_cfg = _load_config()

# ── Constants — ASX ───────────────────────────────────────────────────────────
ACTUAL_HOLDINGS: dict[str, dict[str, Any]] = _cfg.get("asx_holdings") or {
    "AKN.AX": {"avg_entry": 0.043, "shares": 212782, "name": "AuKing Mining"},
    "XST.AX": {"avg_entry": 0.120, "shares": 0,      "name": "Xstate Resources"},
}

WATCHLIST: dict[str, dict[str, Any]] = _cfg.get("asx_watchlist") or {
    "G11.AX": {"name": "Group 11 Technologies"},
    "VRC.AX": {"name": "Volt Resources"},
    "RNX.AX": {"name": "Renegade Exploration"},
}

AKN_MILESTONES: list[dict[str, Any]] = _cfg.get("akn_milestones") or [
    {"stage": "Discovery",  "target_cap_m": 55},
    {"stage": "Resource",   "target_cap_m": 100},
    {"stage": "Developer",  "target_cap_m": 250},
]

COMPARISON_START = "2026-01-01"

# ── Constants — Crypto ────────────────────────────────────────────────────────
DEFAULT_TOKENS: dict[str, dict[str, Any]] = _cfg.get("solana_tokens") or {
    "ALON": {
        "address": "8XtRWb4uAAJFMP4QQhoYYCWR6XXb7ybcCdiqPwz9s5WS",
        "name":    "Alon",
        "emoji":   "🤖",
    },
}

SIGNAL_COLORS: dict[str, str] = {
    "STRONG BUY":  "#3fb950",
    "BUY":         "#7ee787",
    "HOLD":        "#d29922",
    "SELL":        "#f85149",
    "STRONG SELL": "#da3633",
    "WATCH":       "#58a6ff",
}

PLOT_TEMPLATE: dict[str, Any] = dict(
    paper_bgcolor="#0d1117",
    plot_bgcolor="#0d1117",
    font=dict(color="#e6edf3", size=12),
    xaxis=dict(color="#8b949e", showgrid=True, gridcolor="#21262d", zeroline=False),
    yaxis=dict(color="#8b949e", showgrid=True, gridcolor="#21262d", zeroline=False),
    legend=dict(bgcolor="#161b22", bordercolor="#30363d", borderwidth=1),
    margin=dict(l=0, r=0, t=40, b=0),
)

SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "snapshots")


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmt_usd(v: float | None) -> str:
    """Format a float as a compact USD string (e.g. $1.23M, $456.00K)."""
    if v is None:
        return "—"
    v = float(v)
    if abs(v) >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.2f}K"
    return f"${v:.6f}"


def fmt_aud(v: float | None) -> str:
    """Format a float as a compact AUD string (e.g. A$1.23M)."""
    if v is None:
        return "—"
    v = float(v)
    if abs(v) >= 1_000_000:
        return f"A${v / 1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"A${v / 1_000:.1f}K"
    return f"A${v:.4f}"


def signal_badge_html(label: str) -> str:
    """Return an HTML span styled as a coloured signal badge."""
    color = SIGNAL_COLORS.get(label, "#8b949e")
    return (
        f'<span class="signal-badge" style="background:{color}22;color:{color};'
        f'border:2px solid {color};">{label}</span>'
    )


def pill_html(text: str, color: str) -> str:
    """Return an HTML span styled as a small coloured pill."""
    return (
        f'<span class="pill" style="background:{color}22;color:{color};'
        f'border:1px solid {color};">{text}</span>'
    )


def shorten_addr(addr: str) -> str:
    """Shorten a Solana wallet address to ``XXXX...XXXX`` form."""
    return f"{addr[:4]}...{addr[-4:]}" if addr else "—"


# ── URL helpers ───────────────────────────────────────────────────────────────

def encode_holdings(holdings: dict[str, dict[str, Any]]) -> str:
    """
    Encode a holdings dict as a URL-safe query-parameter string.

    Args:
        holdings: Dict mapping ticker → {shares, avg_entry, name}.

    Returns:
        URL-encoded JSON string suitable for use in a ``?portfolio=`` param.
    """
    payload = {
        t: {"s": h["shares"], "e": h["avg_entry"], "n": h.get("name", t)}
        for t, h in holdings.items()
    }
    return urllib.parse.quote(json.dumps(payload, separators=(",", ":")))


def decode_holdings(encoded: str) -> dict[str, dict[str, Any]]:
    """
    Decode a URL-encoded holdings string back to a holdings dict.

    Args:
        encoded: URL-encoded JSON string from ``encode_holdings()``.

    Returns:
        Holdings dict, or an empty dict on parse failure.
    """
    try:
        payload = json.loads(urllib.parse.unquote(encoded))
        return {
            t: {"shares": v["s"], "avg_entry": v["e"], "name": v.get("n", t)}
            for t, v in payload.items()
        }
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        log.warning("Failed to decode portfolio URL param: %s", exc)
        return {}


# ── Session state ─────────────────────────────────────────────────────────────

def init_state() -> None:
    """
    Initialise Streamlit session state with defaults.

    Reads a ``?portfolio=`` URL query param to pre-populate holdings on first load.
    All keys are idempotent — safe to call on every rerun.
    """
    if "holdings" not in st.session_state:
        url_param = st.query_params.get("portfolio", "")
        st.session_state.holdings = (
            decode_holdings(url_param) if url_param else dict(ACTUAL_HOLDINGS)
        )
    if "tokens" not in st.session_state:
        st.session_state.tokens = dict(DEFAULT_TOKENS)
    for key in ("catalyst_data", "catalyst_error", "catalyst_generated_at"):
        if key not in st.session_state:
            st.session_state[key] = None


init_state()


# ── Anthropic client ──────────────────────────────────────────────────────────

@st.cache_resource
def get_anthropic_client() -> anthropic.Anthropic | None:
    """
    Return a cached Anthropic API client.

    Returns:
        Anthropic client instance, or None if the API key is not configured.
    """
    try:
        return anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
    except (KeyError, Exception) as exc:
        log.warning("Anthropic client unavailable: %s", exc)
        return None


# ── ASX data fetching ─────────────────────────────────────────────────────────

def _yf_fetch(ticker: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Inner fetch for a yfinance ticker with up to 3 retry attempts.

    Args:
        ticker: ASX ticker symbol (e.g. "AKN.AX").

    Returns:
        Tuple of (history DataFrame, info dict). Both may be empty on failure.
    """
    for attempt in range(3):
        try:
            t    = yf.Ticker(ticker)
            hist = t.history(period="1y")
            info = t.info
            return hist, info
        except Exception as exc:
            if attempt == 2:
                log.error("yfinance fetch failed for %s after 3 attempts: %s", ticker, exc)
                return pd.DataFrame(), {}
            time.sleep(2 ** attempt)
    return pd.DataFrame(), {}


@st.cache_data(ttl=300)
def fetch_ticker(ticker: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Fetch 1-year price history and metadata for an ASX ticker.

    Results are cached for 5 minutes to avoid hammering Yahoo Finance.

    Args:
        ticker: ASX ticker symbol (e.g. "AKN.AX").

    Returns:
        Tuple of (OHLCV DataFrame, info dict). Both may be empty on failure.
    """
    return _yf_fetch(ticker)


@st.cache_data(ttl=300)
def fetch_comparison(tickers: list[str], start: str) -> dict[str, pd.Series]:
    """
    Fetch closing price series for multiple tickers from a start date.

    Tickers that fail to load are silently skipped so the chart still renders.

    Args:
        tickers: List of ASX ticker symbols.
        start:   ISO date string (e.g. "2026-01-01").

    Returns:
        Dict mapping ticker → closing price Series.
    """
    result: dict[str, pd.Series] = {}
    for ticker in tickers:
        for attempt in range(3):
            try:
                hist = yf.Ticker(ticker).history(start=start)
                if not hist.empty:
                    result[ticker] = hist["Close"]
                break
            except Exception as exc:
                if attempt == 2:
                    log.warning("Comparison fetch failed for %s: %s", ticker, exc)
                time.sleep(2 ** attempt)
    return result


# ── Crypto data fetching ──────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def fetch_dexscreener(token_address: str) -> dict[str, Any] | None:
    """
    Fetch the highest-liquidity Solana trading pair for a token from DexScreener.

    Results are cached for 60 seconds. Returns None on failure rather than
    raising so callers can gracefully show a warning.

    Args:
        token_address: Solana token mint address.

    Returns:
        DexScreener pair dict, or None if the token is not found or on error.
    """
    for attempt in range(3):
        try:
            url  = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            pairs = resp.json().get("pairs", [])
            if not pairs:
                return None
            sol_pairs = [p for p in pairs if p.get("chainId") == "solana"] or pairs
            sol_pairs.sort(
                key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0),
                reverse=True,
            )
            return sol_pairs[0]
        except requests.Timeout:
            log.warning("DexScreener timeout for %s (attempt %d/3)", token_address[:8], attempt + 1)
        except requests.HTTPError as exc:
            log.error("DexScreener HTTP error for %s: %s", token_address[:8], exc)
            return None
        except (requests.ConnectionError, ValueError) as exc:
            log.warning("DexScreener error for %s (attempt %d/3): %s", token_address[:8], attempt + 1, exc)
        if attempt < 2:
            time.sleep(2 ** attempt)
    return None


@st.cache_data(ttl=120)
def fetch_helius_token_holders(
    token_address: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Fetch the top-20 token holders via the Helius ``getTokenLargestAccounts`` RPC.

    Args:
        token_address: Solana token mint address.

    Returns:
        Tuple of (holders list, error_code).
        ``error_code`` is "no_key" if HELIUS_API_KEY is missing, or an error
        string on failure, or None on success.
    """
    api_key = st.secrets.get("HELIUS_API_KEY", "")
    if not api_key:
        return [], "no_key"

    for attempt in range(3):
        try:
            url  = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
            resp = requests.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id":      1,
                    "method":  "getTokenLargestAccounts",
                    "params":  [token_address],
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                log.error("Helius RPC error: %s", data["error"])
                return [], str(data["error"])
            return data.get("result", {}).get("value", []), None
        except requests.Timeout:
            log.warning("Helius holder timeout (attempt %d/3)", attempt + 1)
        except requests.HTTPError as exc:
            log.error("Helius HTTP error: %s", exc)
            return [], str(exc)
        except (requests.ConnectionError, ValueError) as exc:
            log.warning("Helius holder error (attempt %d/3): %s", attempt + 1, exc)
        if attempt < 2:
            time.sleep(2 ** attempt)

    return [], "timeout"


@st.cache_data(ttl=120)
def fetch_helius_transactions(
    token_address: str,
    limit: int = 100,
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Fetch recent transactions for a token address via the Helius REST API.

    Args:
        token_address: Solana token mint address.
        limit:         Maximum number of transactions to return (max 100).

    Returns:
        Tuple of (transactions list, error_code or None).
    """
    api_key = st.secrets.get("HELIUS_API_KEY", "")
    if not api_key:
        return [], "no_key"

    for attempt in range(3):
        try:
            url  = (
                f"https://api.helius.xyz/v0/addresses/{token_address}/transactions"
                f"?api-key={api_key}&limit={limit}"
            )
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            return resp.json(), None
        except requests.Timeout:
            log.warning("Helius tx timeout for %s (attempt %d/3)", token_address[:8], attempt + 1)
        except requests.HTTPError as exc:
            log.error("Helius tx HTTP error: %s", exc)
            return [], str(exc)
        except (requests.ConnectionError, ValueError) as exc:
            log.warning("Helius tx error (attempt %d/3): %s", attempt + 1, exc)
        if attempt < 2:
            time.sleep(2 ** attempt)

    return [], "timeout"


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(msg: str) -> tuple[bool, str]:
    """
    Send an HTML-formatted message to Telegram via the Bot API.

    Retries up to 3 times with exponential backoff on transient network errors.

    Args:
        msg: HTML-formatted message body.

    Returns:
        Tuple of (success: bool, error_description: str).
    """
    token   = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False, "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in secrets"

    for attempt in range(3):
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
            resp.raise_for_status()
            return True, ""
        except requests.Timeout:
            log.warning("Telegram timeout (attempt %d/3)", attempt + 1)
        except requests.HTTPError as exc:
            log.error("Telegram HTTP error: %s", exc)
            return False, str(exc)
        except requests.ConnectionError as exc:
            log.warning("Telegram connection error (attempt %d/3): %s", attempt + 1, exc)
        if attempt < 2:
            time.sleep(2 ** attempt)

    return False, "Failed after 3 attempts"


# ── Grok / X Sentiment ────────────────────────────────────────────────────────

def fetch_grok_sentiment(prompt: str) -> tuple[str | None, str | None]:
    """
    Query the xAI / Grok API for real-time X (Twitter) sentiment.

    Uses Grok's live search parameters to retrieve up-to-date X posts.
    Retries up to 3 times with exponential backoff on transient errors.

    Args:
        prompt: Full user prompt to send to the Grok model.

    Returns:
        Tuple of (response_text, error_code).
        ``error_code`` is "no_key" when XAI_API_KEY is absent, or an error
        string on failure, or None on success.
    """
    api_key = st.secrets.get("XAI_API_KEY", "")
    if not api_key:
        return None, "no_key"

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {
        "model": "grok-3",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a financial sentiment analyst with real-time access to X (Twitter). "
                    "Analyse recent X posts and provide structured, actionable insights. "
                    "Be concise and flag risks clearly."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 1200,
        "search_parameters": {
            "mode": "auto",
            "return_citations": True,
            "sources": [{"type": "x"}, {"type": "news"}],
        },
    }

    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.x.ai/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=45,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"], None
        except requests.Timeout:
            log.warning("Grok timeout (attempt %d/3)", attempt + 1)
        except requests.HTTPError as exc:
            log.error("Grok HTTP %s: %s", resp.status_code, resp.text[:200])
            return None, f"HTTP {resp.status_code}"
        except (requests.ConnectionError, KeyError, ValueError) as exc:
            log.warning("Grok error (attempt %d/3): %s", attempt + 1, exc)
        if attempt < 2:
            time.sleep(2 ** attempt)

    return None, "timeout"


# ── Snapshot helpers ──────────────────────────────────────────────────────────

def load_snapshot(symbol: str) -> dict[str, Any] | None:
    """
    Load the most recent holder snapshot from disk for a given token.

    Args:
        symbol: Token symbol (e.g. "ALON").

    Returns:
        Snapshot dict with ``timestamp`` and ``holders`` keys, or None.
    """
    path = os.path.join(SNAPSHOT_DIR, f"{symbol}_holders.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not load snapshot for %s: %s", symbol, exc)
        return None


def save_snapshot(symbol: str, holders: list[dict[str, Any]]) -> None:
    """
    Persist the current holder list as a timestamped JSON snapshot.

    Args:
        symbol:  Token symbol (e.g. "ALON").
        holders: Holder list from ``fetch_helius_token_holders()``.
    """
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOT_DIR, f"{symbol}_holders.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {"timestamp": datetime.utcnow().isoformat(), "holders": holders},
                fh, indent=2,
            )
    except OSError as exc:
        log.error("Could not save snapshot for %s: %s", symbol, exc)


def _get_amt(holder: dict[str, Any]) -> float:
    """
    Extract the decimal-adjusted amount from a Helius holder dict.

    Prefers ``uiAmount``; falls back to raw ``amount``.
    """
    ui = holder.get("uiAmount")
    return float(ui) if ui is not None else float(holder.get("amount", 0))


def compare_holders(
    old_holders: list[dict[str, Any]],
    new_holders: list[dict[str, Any]],
    symbol: str,
) -> list[dict[str, Any]]:
    """
    Diff two holder snapshots and return significant changes.

    Args:
        old_holders: Holder list from the previous snapshot.
        new_holders: Current holder list.
        symbol:      Token symbol (used only for logging).

    Returns:
        List of change dicts with keys: type, address, old_pct, new_pct, delta.
    """
    old_map   = {h["address"]: h for h in old_holders}
    new_map   = {h["address"]: h for h in new_holders}
    old_total = sum(_get_amt(h) for h in old_holders) or 1.0
    new_total = sum(_get_amt(h) for h in new_holders) or 1.0
    changes:  list[dict[str, Any]] = []

    for addr, h in new_map.items():
        if addr not in old_map:
            pct = _get_amt(h) / new_total * 100
            changes.append({"type": "NEW", "address": addr, "old_pct": None, "new_pct": pct, "delta": pct})

    for addr, h in old_map.items():
        if addr not in new_map:
            pct = _get_amt(h) / old_total * 100
            changes.append({"type": "EXIT", "address": addr, "old_pct": pct, "new_pct": None, "delta": -pct})

    for addr in set(old_map) & set(new_map):
        old_pct = _get_amt(old_map[addr]) / old_total * 100
        new_pct = _get_amt(new_map[addr]) / new_total * 100
        delta   = new_pct - old_pct
        if abs(delta) >= 1.0:
            changes.append({"type": "MOVE", "address": addr,
                            "old_pct": old_pct, "new_pct": new_pct, "delta": delta})

    log.info("compare_holders(%s): %d change(s) found", symbol, len(changes))
    return changes


def format_alert_message(
    symbol: str,
    changes: list[dict[str, Any]],
    snapshot_ts: str,
) -> str:
    """
    Build an HTML-formatted Telegram message summarising holder changes.

    Args:
        symbol:      Token symbol.
        changes:     Output of ``compare_holders()``.
        snapshot_ts: ISO timestamp of the previous snapshot.

    Returns:
        HTML string ready to send via the Telegram Bot API.
    """
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


# ── AI analysis ───────────────────────────────────────────────────────────────

def run_ai_analysis(prompt: str, client: anthropic.Anthropic) -> str:
    """
    Run a prompt through the Claude API and return the response text.

    Args:
        prompt: User prompt string.
        client: Authenticated Anthropic client.

    Returns:
        Response text, or an error string on failure.
    """
    try:
        msg = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
    except anthropic.APIStatusError as exc:
        log.error("Claude API error: %s", exc)
        return f"API error: {exc}"
    except anthropic.APIConnectionError as exc:
        log.error("Claude connection error: %s", exc)
        return "Connection error — please retry."


# ── Chart helpers ─────────────────────────────────────────────────────────────

def candlestick_chart(hist: pd.DataFrame, ticker: str) -> go.Figure:
    """
    Build a Plotly candlestick chart from a yfinance history DataFrame.

    Args:
        hist:   OHLCV DataFrame from ``fetch_ticker()``.
        ticker: Ticker label for the chart title.

    Returns:
        Plotly Figure object.
    """
    fig = go.Figure(data=[go.Candlestick(
        x=hist.index,
        open=hist["Open"], high=hist["High"],
        low=hist["Low"],   close=hist["Close"],
        name=ticker,
        increasing_line_color="#3fb950",
        decreasing_line_color="#f85149",
    )])
    fig.update_layout(**PLOT_TEMPLATE, title=ticker, height=400)
    return fig


def line_chart(series_dict: dict[str, pd.Series], title: str = "") -> go.Figure:
    """
    Build a Plotly normalised return line chart for multiple price series.

    Each series is normalised to percentage return from its first value.

    Args:
        series_dict: Dict mapping name → closing price Series.
        title:       Chart title.

    Returns:
        Plotly Figure object.
    """
    fig    = go.Figure()
    colors = ["#58a6ff", "#3fb950", "#f85149", "#d29922", "#bc8cff"]
    for i, (name, series) in enumerate(series_dict.items()):
        norm = (series / series.iloc[0] - 1) * 100
        fig.add_trace(go.Scatter(
            x=norm.index, y=norm.values, name=name, mode="lines",
            line=dict(color=colors[i % len(colors)], width=2),
        ))
    fig.update_layout(**PLOT_TEMPLATE, title=title, height=350, yaxis_ticksuffix="%")
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ Navigation")
    dashboard_mode: str = st.radio(
        "Dashboard",
        ["🇦🇺 ASX Portfolio", "🪙 Solana Meme"],
        label_visibility="collapsed",
    )
    st.markdown("---")

    if dashboard_mode == "🇦🇺 ASX Portfolio":
        view: str = st.radio(
            "View",
            ["Portfolio Overview", "Watchlist", "AKN Analysis",
             "Price Charts", "AI Analysis", "X Sentiment"],
        )
    else:
        view = st.radio(
            "View",
            ["Token Overview", "On-Chain Health", "Whale Detection",
             "Holder Alerts", "X Sentiment", "Manage Tokens"],
        )

    st.markdown("---")
    st.markdown("### 🔐 AI Access")
    st.text_input(
        "Admin password", type="password", key="refresh_pw",
        placeholder="Enter to unlock AI",
    )

# ─────────────────────────────────────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────────────────────────────────────
col_title, col_ref = st.columns([5, 1])
with col_title:
    if dashboard_mode == "🇦🇺 ASX Portfolio":
        st.markdown("# 📈 ASX Portfolio Dashboard")
    else:
        st.markdown("# 🪙 Solana Meme Dashboard")
    st.markdown(
        f"<span style='color:#adbac7;font-size:15px;'>{date.today().strftime('%A, %d %B %Y')}</span>",
        unsafe_allow_html=True,
    )
with col_ref:
    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("🔄 Refresh", help="Clear cache and reload latest data"):
        st.cache_data.clear()
        st.rerun()
st.markdown("---")

# ── AI unlock check ───────────────────────────────────────────────────────────
admin_pw: str    = st.secrets.get("ADMIN_PASSWORD", "")
typed_pw: str    = st.session_state.get("refresh_pw", "")
ai_unlocked: bool = (not admin_pw) or (typed_pw == admin_pw)


# ═════════════════════════════════════════════════════════════════════════════
# ASX PORTFOLIO VIEWS
# ═════════════════════════════════════════════════════════════════════════════
if dashboard_mode == "🇦🇺 ASX Portfolio":

    # ── Portfolio Overview ────────────────────────────────────────────────────
    if view == "Portfolio Overview":
        st.subheader("🗂 Actual Holdings")
        total_value, total_cost = 0.0, 0.0

        for ticker, holding in st.session_state.holdings.items():
            try:
                hist, info = fetch_ticker(ticker)
                if hist.empty:
                    st.warning(f"No data returned for {ticker}.")
                    continue

                shares    = holding["shares"]
                avg_entry = holding["avg_entry"]
                name      = holding.get("name", ticker)
                price     = float(hist["Close"].iloc[-1])
                value     = shares * price
                cost      = shares * avg_entry
                pnl       = value - cost
                pnl_pct   = (pnl / cost * 100) if cost else 0.0
                mktcap    = info.get("marketCap")

                total_value += value
                total_cost  += cost

                with st.expander(f"**{ticker}** — {name}", expanded=True):
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Price",  fmt_aud(price))
                    c2.metric("Shares", f"{shares:,}")
                    c3.metric("Value",  fmt_aud(value))
                    c4.metric("P&L",    f"{fmt_aud(pnl)} ({pnl_pct:+.1f}%)", delta=f"{pnl_pct:+.1f}%")
                    if mktcap:
                        st.caption(f"Market cap: {fmt_aud(mktcap)}  •  Avg entry: A${avg_entry:.4f}")
            except Exception as exc:
                log.error("Portfolio overview error for %s: %s", ticker, exc)
                st.error(f"Error loading {ticker} — check logs.")
                continue  # never crash on a single ticker

        st.markdown("---")
        total_pnl     = total_value - total_cost
        total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0.0
        tc1, tc2, tc3 = st.columns(3)
        tc1.metric("Total Portfolio Value", fmt_aud(total_value))
        tc2.metric("Total Cost Basis",      fmt_aud(total_cost))
        tc3.metric("Total P&L", f"{fmt_aud(total_pnl)} ({total_pnl_pct:+.1f}%)", delta=f"{total_pnl_pct:+.1f}%")

        st.markdown("---")
        encoded = encode_holdings(st.session_state.holdings)
        st.caption(f"Shareable link param: `?portfolio={encoded}`")

    # ── Watchlist ─────────────────────────────────────────────────────────────
    elif view == "Watchlist":
        st.subheader("👁 Watchlist")
        rows: list[dict[str, Any]] = []
        for ticker, meta in WATCHLIST.items():
            try:
                hist, info = fetch_ticker(ticker)
                if hist.empty:
                    continue
                price   = float(hist["Close"].iloc[-1])
                prev    = float(hist["Close"].iloc[-2]) if len(hist) > 1 else price
                chg_pct = (price - prev) / prev * 100
                rows.append({
                    "Ticker": ticker,
                    "Name":   meta["name"],
                    "Price":  fmt_aud(price),
                    "1D %":   f"{chg_pct:+.2f}%",
                    "Volume": f"{int(hist['Volume'].iloc[-1]):,}",
                })
            except Exception as exc:
                log.error("Watchlist error for %s: %s", ticker, exc)
                continue

        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("No watchlist data available.")

    # ── AKN Analysis ─────────────────────────────────────────────────────────
    elif view == "AKN Analysis":
        st.subheader("⛏ AuKing Mining (AKN.AX) — Milestone Targets")
        try:
            hist, info      = fetch_ticker("AKN.AX")
            shares_on_issue = info.get("sharesOutstanding")

            if not hist.empty and shares_on_issue:
                price       = float(hist["Close"].iloc[-1])
                current_cap = price * shares_on_issue / 1e6

                mc1, mc2 = st.columns(2)
                mc1.metric("Current Price",      fmt_aud(price))
                mc2.metric("Current Market Cap", f"A${current_cap:.1f}M")
                st.markdown("---")

                milestone_rows = []
                for ms in AKN_MILESTONES:
                    target_price = ms["target_cap_m"] * 1e6 / shares_on_issue
                    upside       = (target_price / price - 1) * 100
                    milestone_rows.append({
                        "Stage":        ms["stage"],
                        "Target Cap":   f"A${ms['target_cap_m']}M",
                        "Target Price": f"A${target_price:.4f}",
                        "Upside":       f"{upside:+.0f}%",
                    })
                st.dataframe(pd.DataFrame(milestone_rows), use_container_width=True, hide_index=True)

                holding = st.session_state.holdings.get("AKN.AX", {})
                shares  = holding.get("shares", 0)
                if shares:
                    st.markdown("#### Portfolio value at each milestone")
                    val_rows = []
                    for ms in AKN_MILESTONES:
                        tp = ms["target_cap_m"] * 1e6 / shares_on_issue
                        val_rows.append({
                            "Stage": ms["stage"],
                            "Value": fmt_aud(shares * tp),
                            "Gain":  fmt_aud(shares * (tp - price)),
                        })
                    st.dataframe(pd.DataFrame(val_rows), use_container_width=True, hide_index=True)
            else:
                st.info("Shares outstanding not available — Yahoo Finance may be throttling.")
        except Exception as exc:
            log.error("AKN analysis error: %s", exc)
            st.error("Failed to load AKN.AX data.")

    # ── Price Charts ──────────────────────────────────────────────────────────
    elif view == "Price Charts":
        st.subheader("📊 Price Charts")
        all_tickers = list(st.session_state.holdings.keys()) + list(WATCHLIST.keys())
        selected    = st.selectbox("Select ticker", all_tickers)
        chart_type  = st.radio("Chart type", ["Candlestick", "Line"], horizontal=True)

        try:
            hist, _ = fetch_ticker(selected)
            if not hist.empty:
                if chart_type == "Candlestick":
                    st.plotly_chart(candlestick_chart(hist, selected), use_container_width=True)
                else:
                    st.plotly_chart(line_chart({selected: hist["Close"]}, title=selected), use_container_width=True)
            else:
                st.warning(f"No chart data for {selected}.")
        except Exception as exc:
            log.error("Chart error for %s: %s", selected, exc)
            st.error("Failed to render chart.")

        st.markdown("---")
        st.subheader(f"📈 Relative Performance (since {COMPARISON_START})")
        try:
            comp_data = fetch_comparison(list(st.session_state.holdings.keys()), COMPARISON_START)
            if comp_data:
                st.plotly_chart(line_chart(comp_data, "Relative Return (%)"), use_container_width=True)
            else:
                st.info("No comparison data available for the selected period.")
        except Exception as exc:
            log.error("Comparison chart error: %s", exc)

    # ── AI Analysis ───────────────────────────────────────────────────────────
    elif view == "AI Analysis":
        st.subheader("🤖 AI Portfolio Analysis")
        if not ai_unlocked:
            st.warning("Enter admin password in the sidebar to unlock AI features.")
        else:
            client = get_anthropic_client()
            if not client:
                st.error("ANTHROPIC_API_KEY not configured in secrets.")
            else:
                context_lines: list[str] = []
                for ticker, holding in st.session_state.holdings.items():
                    try:
                        hist, _ = fetch_ticker(ticker)
                        if not hist.empty:
                            price   = float(hist["Close"].iloc[-1])
                            pnl_pct = (price / holding["avg_entry"] - 1) * 100
                            context_lines.append(
                                f"{ticker} ({holding.get('name', ticker)}): "
                                f"price A${price:.4f}, entry A${holding['avg_entry']:.4f}, "
                                f"P&L {pnl_pct:+.1f}%, shares {holding['shares']:,}"
                            )
                    except Exception as exc:
                        log.warning("Context build failed for %s: %s", ticker, exc)
                        continue

                context = "\n".join(context_lines)
                analysis_type = st.selectbox(
                    "Analysis type",
                    ["Portfolio Summary", "Risk Assessment", "Catalyst Watch", "Exit Strategy"],
                )
                prompts: dict[str, str] = {
                    "Portfolio Summary": (
                        f"Analyse this ASX small-cap portfolio and provide a concise summary of "
                        f"current positioning, key risks, and near-term catalysts:\n\n{context}"
                    ),
                    "Risk Assessment": (
                        f"Assess the key risks in this ASX portfolio. Focus on concentration risk, "
                        f"sector exposure, and downside scenarios:\n\n{context}"
                    ),
                    "Catalyst Watch": (
                        f"For each stock in this portfolio, identify the most important upcoming catalysts "
                        f"(drilling results, resource estimates, government decisions, etc.) that could move "
                        f"the price:\n\n{context}"
                    ),
                    "Exit Strategy": (
                        f"Suggest exit strategy guidelines for each position, including target prices and "
                        f"stop-loss levels:\n\n{context}"
                    ),
                }
                if st.button("🚀 Run Analysis", type="primary"):
                    with st.spinner("Analysing with Claude..."):
                        result = run_ai_analysis(prompts[analysis_type], client)
                    st.markdown(result)

    # ── X Sentiment (ASX) ─────────────────────────────────────────────────────
    elif view == "X Sentiment":
        st.subheader("𝕏 X / Grok Sentiment Analysis")
        xai_key = st.secrets.get("XAI_API_KEY", "")
        if not xai_key:
            st.error("XAI_API_KEY not set in secrets. Get an API key at console.x.ai")
        else:
            all_tickers = list(st.session_state.holdings.keys()) + list(WATCHLIST.keys())
            selected    = st.selectbox("Select ticker", all_tickers)
            name        = (
                st.session_state.holdings.get(selected, {}).get("name")
                or WATCHLIST.get(selected, {}).get("name")
                or selected
            )
            period = st.radio("Lookback period", ["24 hours", "7 days", "30 days"], index=1, horizontal=True)

            prompt = (
                f"Search X (Twitter) for posts about {selected} ({name}) ASX stock from the last {period}.\n\n"
                f"Provide a structured analysis:\n\n"
                f"**1. Sentiment Score** — Bullish / Neutral / Bearish with confidence %\n"
                f"**2. Key Themes** — main narratives and catalysts being discussed\n"
                f"**3. Community Signals** — discussion volume, sentiment shifts, coordinated activity\n"
                f"**4. Catalysts & News** — announcements, drill results, regulatory news\n"
                f"**5. Red Flags** — FUD, warnings, or concerns raised\n"
                f"**6. Summary** — 2-3 sentence verdict for an investor holding this stock\n"
            )
            if st.button("🔍 Analyse X Sentiment", type="primary"):
                with st.spinner(f"Grok is searching X for {selected} sentiment..."):
                    result, err = fetch_grok_sentiment(prompt)
                if result:
                    st.markdown(result)
                elif err == "no_key":
                    st.error("XAI_API_KEY not configured.")
                else:
                    st.error(f"Grok API error: {err}")


# ═════════════════════════════════════════════════════════════════════════════
# SOLANA MEME DASHBOARD VIEWS
# ═════════════════════════════════════════════════════════════════════════════
else:

    # ── Token Overview ────────────────────────────────────────────────────────
    if view == "Token Overview":
        st.subheader("🪙 Solana Token Overview")
        if not st.session_state.tokens:
            st.info("No tokens added yet. Go to **Manage Tokens** to add some.")
        else:
            for symbol, token in st.session_state.tokens.items():
                try:
                    addr = token["address"]
                    name = token.get("name", symbol)
                    pair = fetch_dexscreener(addr)

                    with st.expander(f"**{symbol}** — {name}  `{shorten_addr(addr)}`", expanded=True):
                        if not pair:
                            st.warning("No DexScreener data found for this address.")
                            continue

                        price_usd = float(pair.get("priceUsd", 0) or 0)
                        mktcap    = pair.get("marketCap") or pair.get("fdv")
                        liq       = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                        vol24     = float(pair.get("volume", {}).get("h24", 0) or 0)
                        chg5m     = float(pair.get("priceChange", {}).get("m5",  0) or 0)
                        chg1h     = float(pair.get("priceChange", {}).get("h1",  0) or 0)
                        chg24h    = float(pair.get("priceChange", {}).get("h24", 0) or 0)
                        buys24    = int(pair.get("txns", {}).get("h24", {}).get("buys",  0) or 0)
                        sells24   = int(pair.get("txns", {}).get("h24", {}).get("sells", 0) or 0)
                        dex_url   = pair.get("url", "")

                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("Price (USD)",  fmt_usd(price_usd))
                        c2.metric("Market Cap",   fmt_usd(float(mktcap)) if mktcap else "—")
                        c3.metric("Liquidity",    fmt_usd(liq))
                        c4.metric("24h Volume",   fmt_usd(vol24))

                        c5, c6, c7, c8 = st.columns(4)
                        c5.metric("5m",             f"{chg5m:+.2f}%")
                        c6.metric("1h",             f"{chg1h:+.2f}%")
                        c7.metric("24h",            f"{chg24h:+.2f}%")
                        c8.metric("Buys/Sells 24h", f"{buys24} / {sells24}")

                        if dex_url:
                            st.markdown(f"[View on DexScreener ↗]({dex_url})")
                except Exception as exc:
                    log.error("Token overview error for %s: %s", symbol, exc)
                    st.error(f"Error loading {symbol}.")
                    continue

    # ── On-Chain Health ───────────────────────────────────────────────────────
    elif view == "On-Chain Health":
        st.subheader("🔬 On-Chain Health")
        if not st.session_state.tokens:
            st.info("No tokens added. Go to **Manage Tokens** first.")
        else:
            selected_sym = st.selectbox("Select token", list(st.session_state.tokens.keys()))
            token = st.session_state.tokens[selected_sym]
            addr  = token["address"]
            st.markdown(f"**Mint address:** `{addr}`")
            st.markdown("---")

            try:
                pair = fetch_dexscreener(addr)
                if pair:
                    liq       = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                    mktcap    = float(pair.get("marketCap") or pair.get("fdv") or 0)
                    liq_ratio = liq / mktcap * 100 if mktcap else 0.0

                    st.markdown("#### 💧 Liquidity Health")
                    l1, l2, l3 = st.columns(3)
                    l1.metric("Liquidity (USD)", fmt_usd(liq))
                    l2.metric("Market Cap",      fmt_usd(mktcap))
                    l3.metric("Liq / MCap",      f"{liq_ratio:.1f}%")

                    if liq_ratio < 2:
                        st.markdown('<span class="warn-flag">⚠️ Very low liquidity ratio — rug risk elevated</span>', unsafe_allow_html=True)
                    elif liq_ratio < 5:
                        st.markdown('<span class="warn-flag">⚠️ Low liquidity ratio — use caution</span>', unsafe_allow_html=True)
                    else:
                        st.markdown('<span class="safe-flag">✅ Healthy liquidity ratio</span>', unsafe_allow_html=True)

                    st.markdown("#### 📊 Buy / Sell Pressure")
                    txns     = pair.get("txns", {})
                    periods  = [("5m", "m5"), ("1h", "h1"), ("6h", "h6"), ("24h", "h24")]
                    p_rows   = []
                    for label, key in periods:
                        b     = int(txns.get(key, {}).get("buys",  0) or 0)
                        s     = int(txns.get(key, {}).get("sells", 0) or 0)
                        total = b + s
                        ratio = b / total * 100 if total else 50.0
                        p_rows.append({"Period": label, "Buys": b, "Sells": s, "Buy %": f"{ratio:.0f}%"})
                    st.dataframe(pd.DataFrame(p_rows), use_container_width=True, hide_index=True)
            except Exception as exc:
                log.error("On-chain health DexScreener error for %s: %s", selected_sym, exc)
                st.error("Failed to load DexScreener data.")

            st.markdown("---")
            st.markdown("#### 👥 Holder Concentration (Helius)")
            try:
                h_data, h_err = fetch_helius_token_holders(addr)
                if h_data:
                    total = sum(_get_amt(h) for h in h_data) or 1.0
                    top3  = sum(_get_amt(h) for h in h_data[:3])
                    top3_pct = top3 / total * 100

                    holder_rows = []
                    for i, h in enumerate(h_data[:10], 1):
                        amt = _get_amt(h)
                        holder_rows.append({
                            "Rank":     i,
                            "Address":  shorten_addr(h.get("address", "—")),
                            "Amount":   f"{amt:,.2f}",
                            "% Supply": f"{amt / total * 100:.2f}%",
                        })
                    st.dataframe(pd.DataFrame(holder_rows), use_container_width=True, hide_index=True)

                    flag = (
                        f'<span class="warn-flag">⚠️ Top 3 holders: {top3_pct:.1f}% — high concentration risk</span>'
                        if top3_pct > 50
                        else f'<span class="safe-flag">✅ Top 3 holders: {top3_pct:.1f}% — reasonable distribution</span>'
                    )
                    st.markdown(flag, unsafe_allow_html=True)
                elif h_err == "no_key":
                    st.caption("Add HELIUS_API_KEY to secrets for holder data.")
                else:
                    st.info(f"No holder data available ({h_err}).")
            except Exception as exc:
                log.error("Holder concentration error for %s: %s", selected_sym, exc)
                st.error("Failed to load holder data.")

    # ── Whale Detection ───────────────────────────────────────────────────────
    elif view == "Whale Detection":
        st.subheader("🐳 Whale Detection")
        if not st.session_state.tokens:
            st.info("No tokens added. Go to **Manage Tokens** first.")
        else:
            selected_sym = st.selectbox("Select token", list(st.session_state.tokens.keys()))
            token = st.session_state.tokens[selected_sym]
            addr  = token["address"]

            whale_threshold = st.slider(
                "Whale threshold (USD)",
                min_value=1_000, max_value=100_000,
                value=10_000, step=1_000, format="$%d",
            )

            try:
                pair      = fetch_dexscreener(addr)
                price_usd = float(pair.get("priceUsd", 0) or 0) if pair else 0.0

                txns, t_err = fetch_helius_transactions(addr, limit=100)
                if txns and isinstance(txns, list):
                    whales: list[dict[str, Any]] = []
                    for tx in txns:
                        if not isinstance(tx, dict):
                            continue
                        timestamp = tx.get("timestamp", 0)
                        tx_type   = tx.get("type", "UNKNOWN")
                        sig       = tx.get("signature", "")
                        for tt in tx.get("tokenTransfers", []):
                            try:
                                amount  = float(tt.get("tokenAmount", 0) or 0)
                                usd_val = amount * price_usd
                                if usd_val >= whale_threshold:
                                    whales.append({
                                        "Time":  datetime.fromtimestamp(timestamp).strftime("%H:%M:%S") if timestamp else "—",
                                        "Type":  tx_type,
                                        "Value": fmt_usd(usd_val),
                                        "From":  shorten_addr(tt.get("fromUserAccount", "")),
                                        "To":    shorten_addr(tt.get("toUserAccount", "")),
                                        "Sig":   sig[:8] + "..." if sig else "—",
                                    })
                            except (ValueError, TypeError, KeyError) as exc:
                                log.warning("Whale parse error: %s", exc)
                                continue

                    if whales:
                        st.markdown(f"**{len(whales)} whale transaction(s) ≥ {fmt_usd(whale_threshold)}**")
                        st.dataframe(pd.DataFrame(whales), use_container_width=True, hide_index=True)
                    else:
                        st.success(f"No whale transactions ≥ {fmt_usd(whale_threshold)} in recent history.")
                elif t_err == "no_key":
                    st.caption("Add HELIUS_API_KEY to secrets for transaction data.")
                else:
                    st.info("No transaction data available.")
            except Exception as exc:
                log.error("Whale detection error for %s: %s", selected_sym, exc)
                st.error("Failed to load transaction data.")

    # ── Holder Alerts ─────────────────────────────────────────────────────────
    elif view == "Holder Alerts":
        st.subheader("🔔 Holder Concentration Alerts")
        if not st.session_state.tokens:
            st.info("No tokens added. Go to **Manage Tokens** first.")
        else:
            selected_sym = st.selectbox("Select token", list(st.session_state.tokens.keys()))
            token = st.session_state.tokens[selected_sym]
            addr  = token["address"]

            try:
                current_holders, h_err = fetch_helius_token_holders(addr)
                snapshot = load_snapshot(selected_sym)

                col_a, col_b = st.columns(2)

                with col_a:
                    st.markdown("#### 📸 Snapshot")
                    if snapshot:
                        ts = snapshot["timestamp"][:16].replace("T", " ")
                        st.caption(f"Last saved: {ts} UTC  •  {len(snapshot['holders'])} holders")
                    else:
                        st.caption("No snapshot saved yet.")

                    if st.button("💾 Save current as baseline"):
                        if current_holders:
                            save_snapshot(selected_sym, current_holders)
                            st.success("Snapshot saved.")
                            st.rerun()
                        else:
                            st.error("Could not fetch current holders.")

                with col_b:
                    st.markdown("#### 📡 Telegram")
                    tg_ok = bool(st.secrets.get("TELEGRAM_BOT_TOKEN") and st.secrets.get("TELEGRAM_CHAT_ID"))
                    st.markdown(
                        pill_html("Configured ✓", "#3fb950") if tg_ok else pill_html("Not configured", "#f85149"),
                        unsafe_allow_html=True,
                    )
                    if not tg_ok:
                        st.caption("Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to secrets.")

                    if st.button("🧪 Send test message"):
                        ok, err = send_telegram(f"✅ <b>Test alert</b> — {selected_sym} monitoring is active.")
                        st.success("Sent!") if ok else st.error(f"Failed: {err}")

                st.markdown("---")

                if current_holders and snapshot:
                    try:
                        changes = compare_holders(snapshot["holders"], current_holders, selected_sym)
                    except Exception as exc:
                        log.error("compare_holders failed: %s", exc)
                        changes = []

                    if changes:
                        st.markdown(f"### ⚠️ {len(changes)} change(s) since last snapshot")
                        rows = []
                        for c in changes:
                            rows.append({
                                "Type":    {"NEW": "🆕 New", "EXIT": "🚪 Exit", "MOVE": "📊 Move"}[c["type"]],
                                "Address": shorten_addr(c["address"]),
                                "Old %":   f"{c['old_pct']:.2f}%" if c["old_pct"] is not None else "—",
                                "New %":   f"{c['new_pct']:.2f}%" if c["new_pct"] is not None else "—",
                                "Δ":       f"{c['delta']:+.2f}%",
                            })
                        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

                        if st.button("📤 Send alert to Telegram"):
                            msg  = format_alert_message(selected_sym, changes, snapshot["timestamp"])
                            ok, resp = send_telegram(msg)
                            st.success("Alert sent!") if ok else st.error(f"Failed: {resp}")
                    else:
                        st.success("✅ No significant changes since last snapshot.")
                elif not snapshot:
                    st.info("Save a baseline snapshot first, then return after some time to see changes.")
                elif h_err == "no_key":
                    st.caption("Add HELIUS_API_KEY to secrets.")

                st.markdown("---")
                st.markdown("#### Current top 20 holders")
                if current_holders:
                    total = sum(_get_amt(h) for h in current_holders) or 1.0
                    ref_rows = [{
                        "Rank":     i,
                        "Address":  shorten_addr(h.get("address", "—")),
                        "Amount":   f"{_get_amt(h):,.2f}",
                        "% Supply": f"{_get_amt(h) / total * 100:.2f}%",
                    } for i, h in enumerate(current_holders, 1)]
                    st.dataframe(pd.DataFrame(ref_rows), use_container_width=True, hide_index=True)
                elif h_err == "no_key":
                    st.caption("Add HELIUS_API_KEY to secrets.")
            except Exception as exc:
                log.error("Holder alerts error for %s: %s", selected_sym, exc)
                st.error("Failed to load holder data.")

    # ── X Sentiment (Solana) ──────────────────────────────────────────────────
    elif view == "X Sentiment":
        st.subheader("𝕏 X / Grok Sentiment Analysis")
        xai_key = st.secrets.get("XAI_API_KEY", "")
        if not xai_key:
            st.error("XAI_API_KEY not set in secrets. Get an API key at console.x.ai")
        elif not st.session_state.tokens:
            st.info("No tokens added. Go to **Manage Tokens** first.")
        else:
            selected_sym = st.selectbox("Select token", list(st.session_state.tokens.keys()))
            token        = st.session_state.tokens[selected_sym]
            name         = token.get("name", selected_sym)
            addr         = token["address"]
            period       = st.radio("Lookback period", ["24 hours", "7 days", "30 days"], index=0, horizontal=True)

            price_ctx = ""
            try:
                pair = fetch_dexscreener(addr)
                if pair:
                    p     = float(pair.get("priceUsd", 0) or 0)
                    chg   = float(pair.get("priceChange", {}).get("h24", 0) or 0)
                    mcap  = pair.get("marketCap") or pair.get("fdv")
                    price_ctx = (
                        f"Price: ${p:.6f} USD | 24h: {chg:+.1f}% | "
                        f"MCap: {fmt_usd(float(mcap)) if mcap else 'unknown'}"
                    )
            except Exception as exc:
                log.warning("Price context fetch failed for %s: %s", selected_sym, exc)

            prompt = (
                f"Search X (Twitter) for posts about {selected_sym} ({name}), a Solana meme coin, "
                f"from the last {period}.\n"
                f"{f'Live market context: {price_ctx}' if price_ctx else ''}\n\n"
                f"**1. Sentiment Score** — Bullish / Neutral / Bearish with confidence %\n"
                f"**2. Community & Hype Level** — organic vs coordinated, activity level\n"
                f"**3. Key Narratives** — utility claims, meme themes, dev activity\n"
                f"**4. KOL / Influencer Activity** — notable accounts, bullish or bearish\n"
                f"**5. Risk Signals** — rug concerns, whale dump warnings, honeypot chatter\n"
                f"**6. Momentum Assessment** — growing, peaking, or fading?\n"
                f"**7. Summary** — 2-3 sentence verdict for a speculative trader\n"
            )
            if st.button("🔍 Analyse X Sentiment", type="primary"):
                with st.spinner(f"Grok is searching X for {selected_sym} sentiment..."):
                    result, err = fetch_grok_sentiment(prompt)
                if result:
                    if price_ctx:
                        st.caption(f"📊 Market snapshot: {price_ctx}")
                    st.markdown(result)
                elif err == "no_key":
                    st.error("XAI_API_KEY not configured.")
                else:
                    st.error(f"Grok API error: {err}")

    # ── Manage Tokens ─────────────────────────────────────────────────────────
    elif view == "Manage Tokens":
        st.subheader("⚙️ Manage Tokens")
        st.markdown("#### Current tokens")
        if st.session_state.tokens:
            for sym, tok in list(st.session_state.tokens.items()):
                c1, c2 = st.columns([4, 1])
                c1.markdown(f"**{sym}** — {tok.get('name', sym)}  \n`{tok['address']}`")
                if c2.button("Remove", key=f"rm_{sym}"):
                    del st.session_state.tokens[sym]
                    st.rerun()
        else:
            st.info("No tokens added yet.")

        st.markdown("---")
        st.markdown("#### Add a new token")
        with st.form("add_token"):
            sym  = st.text_input("Symbol (e.g. ALON)")
            name = st.text_input("Name (e.g. Alon)")
            addr = st.text_input("Solana mint address")
            if st.form_submit_button("Add Token"):
                if sym and addr:
                    st.session_state.tokens[sym.upper()] = {"address": addr, "name": name or sym}
                    st.success(f"Added {sym.upper()}.")
                    st.rerun()
                else:
                    st.error("Symbol and address are required.")

# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown("---")
st.caption(
    "⚠️ Not financial advice. Meme coins are extremely high risk. "
    "ASX data via Yahoo Finance. Crypto data via DexScreener + Helius. "
    "Sentiment via Grok / X. AI analysis via Anthropic Claude."
)
