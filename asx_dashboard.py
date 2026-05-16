"""
ASX + Solana Meme Coin Portfolio Dashboard
Secrets required: ANTHROPIC_API_KEY, ADMIN_PASSWORD
Optional:         HELIUS_API_KEY, BIRDEYE_API_KEY
"""

import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, date
import anthropic
import json
import urllib.parse
import requests
import warnings

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Portfolio Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────
# THEME
# ─────────────────────────────────────────────
st.markdown("""
<style>
    html, body, [class*="css"] {
        background-color: #0d1117;
        color: #f0f6fc;
        font-family: 'Inter', sans-serif;
    }
    .stApp { background-color: #0d1117; }

    /* Sidebar */
    section[data-testid="stSidebar"] {
        background-color: #161b22;
        border-right: 2px solid #30363d;
    }
    section[data-testid="stSidebar"] * { color: #f0f6fc !important; }
    section[data-testid="stSidebar"] label { color: #f0f6fc !important; font-weight: 600; }

    /* Headings */
    h1, h2, h3, h4, h5, h6 { color: #f0f6fc !important; font-weight: 700; }
    p, li, span, div { color: #d1d9e0; }

    /* Buttons */
    .stButton > button {
        background-color: #238636;
        color: #ffffff;
        border: 1px solid #2ea043;
        border-radius: 6px;
        font-weight: 600;
    }
    .stButton > button:hover {
        background-color: #2ea043;
        border-color: #3fb950;
        color: #ffffff;
    }

    /* Inputs */
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

    /* Metric cards */
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
    div[data-testid="metric-container"] div[data-testid="stMetricDelta"] {
        font-size: 13px;
        font-weight: 600;
    }

    /* Expanders */
    details { background-color: #1c2128; border: 1px solid #444c56; border-radius: 8px; }
    details summary { color: #f0f6fc !important; font-weight: 600; font-size: 15px; }

    /* Dataframes */
    .stDataFrame {
        border: 1px solid #444c56;
        border-radius: 8px;
    }

    /* Radio / checkboxes */
    .stRadio label { color: #d1d9e0 !important; font-size: 14px; }
    .stRadio div[role="radiogroup"] label[data-baseweb="radio"] { color: #f0f6fc !important; }

    /* Captions */
    .stCaption, small { color: #adbac7 !important; }

    /* Warning / info / success boxes */
    div[data-testid="stAlert"] { border-radius: 8px; }

    /* Badges */
    .signal-badge {
        padding: 4px 12px;
        border-radius: 12px;
        font-size: 13px;
        font-weight: 700;
        letter-spacing: 0.5px;
    }
    .pill {
        padding: 3px 10px;
        border-radius: 10px;
        font-size: 12px;
        font-weight: 600;
    }
    .warn-flag { color: #ff7b72; font-size: 13px; font-weight: 600; }
    .safe-flag { color: #56d364; font-size: 13px; font-weight: 600; }
    hr { border-color: #30363d; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# CONSTANTS — ASX
# ─────────────────────────────────────────────
ACTUAL_HOLDINGS = {
    "AKN.AX": {"avg_entry": 0.043, "shares": 212782, "name": "AuKing Mining"},
    "XST.AX": {"avg_entry": 0.120, "shares": 0,      "name": "Xstate Resources"},
}

WATCHLIST = {
    "G11.AX": {"name": "Group 11 Technologies"},
    "VRC.AX": {"name": "Volt Resources"},
    "RNX.AX": {"name": "Renegade Exploration"},
}

AKN_MILESTONES = [
    {"stage": "Discovery",  "target_cap_m": 55},
    {"stage": "Resource",   "target_cap_m": 100},
    {"stage": "Developer",  "target_cap_m": 250},
]

COMPARISON_START = "2026-01-01"

# ─────────────────────────────────────────────
# CONSTANTS — CRYPTO
# ─────────────────────────────────────────────
DEFAULT_TOKENS = {
    "ALON": {
        "address": "8XtRWb4uAAJFMP4QQhoYYCWR6XXb7ybcCdiqPwz9s5WS",
        "name":    "Alon",
        "emoji":   "🤖",
    },
}

SIGNAL_COLORS = {
    "STRONG BUY":  "#3fb950",
    "BUY":         "#7ee787",
    "HOLD":        "#d29922",
    "SELL":        "#f85149",
    "STRONG SELL": "#da3633",
    "WATCH":       "#58a6ff",
}

PLOT_TEMPLATE = dict(
    paper_bgcolor="#0d1117",
    plot_bgcolor="#0d1117",
    font=dict(color="#e6edf3", size=12),
    xaxis=dict(color="#8b949e", showgrid=True, gridcolor="#21262d", zeroline=False),
    yaxis=dict(color="#8b949e", showgrid=True, gridcolor="#21262d", zeroline=False),
    legend=dict(bgcolor="#161b22", bordercolor="#30363d", borderwidth=1),
    margin=dict(l=0, r=0, t=40, b=0),
)

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def signal_badge_html(label):
    color = SIGNAL_COLORS.get(label, "#8b949e")
    return (
        f'<span class="signal-badge" style="background:{color}22;color:{color};'
        f'border:2px solid {color};">{label}</span>'
    )

def pill_html(text, color):
    return (
        f'<span class="pill" style="background:{color}22;color:{color};'
        f'border:1px solid {color};">{text}</span>'
    )

def shorten_addr(addr):
    return f"{addr[:4]}...{addr[-4:]}" if addr else "—"

def fmt_usd(v):
    if v is None:
        return "—"
    v = float(v)
    if abs(v) >= 1e9:
        return f"${v/1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v/1e6:.2f}M"
    if abs(v) >= 1e3:
        return f"${v/1e3:.2f}K"
    return f"${v:.6f}"

def fmt_aud(v):
    if v is None:
        return "—"
    v = float(v)
    if abs(v) >= 1e6:
        return f"A${v/1e6:.2f}M"
    if abs(v) >= 1e3:
        return f"A${v/1e3:.1f}K"
    return f"A${v:.4f}"

def pct_color(v):
    if v is None:
        return "#8b949e"
    return "#3fb950" if v >= 0 else "#f85149"

# ─────────────────────────────────────────────
# URL HELPERS
# ─────────────────────────────────────────────
def encode_holdings(holdings):
    payload = {
        t: {"s": h["shares"], "e": h["avg_entry"], "n": h.get("name", t)}
        for t, h in holdings.items()
    }
    return urllib.parse.quote(json.dumps(payload, separators=(",", ":")))

def decode_holdings(encoded):
    try:
        payload = json.loads(urllib.parse.unquote(encoded))
        return {
            t: {"shares": v["s"], "avg_entry": v["e"], "name": v.get("n", t)}
            for t, v in payload.items()
        }
    except Exception:
        return {}

# ─────────────────────────────────────────────
# SESSION STATE INIT
# ─────────────────────────────────────────────
def init_state():
    if "holdings" not in st.session_state:
        url_param = st.query_params.get("portfolio", "")
        if url_param:
            st.session_state.holdings = decode_holdings(url_param)
        else:
            st.session_state.holdings = dict(ACTUAL_HOLDINGS)
    if "tokens" not in st.session_state:
        st.session_state.tokens = dict(DEFAULT_TOKENS)
    for key in ["catalyst_data", "catalyst_error", "catalyst_generated_at"]:
        if key not in st.session_state:
            st.session_state[key] = None

init_state()

# ─────────────────────────────────────────────
# ANTHROPIC CLIENT
# ─────────────────────────────────────────────
@st.cache_resource
def get_anthropic_client():
    try:
        api_key = st.secrets["ANTHROPIC_API_KEY"]
        return anthropic.Anthropic(api_key=api_key)
    except Exception:
        return None

# ─────────────────────────────────────────────
# DATA FETCHING — ASX
# ─────────────────────────────────────────────
@st.cache_data(ttl=300)
def fetch_ticker(ticker):
    try:
        t    = yf.Ticker(ticker)
        hist = t.history(period="1y")
        info = t.info
        return hist, info
    except Exception:
        return pd.DataFrame(), {}

@st.cache_data(ttl=300)
def fetch_comparison(tickers, start):
    result = {}
    for t in tickers:
        try:
            hist = yf.Ticker(t).history(start=start)
            if not hist.empty:
                result[t] = hist["Close"]
        except Exception:
            pass
    return result

# ─────────────────────────────────────────────
# DATA FETCHING — CRYPTO
# ─────────────────────────────────────────────
@st.cache_data(ttl=60)
def fetch_dexscreener(token_address):
    try:
        url  = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        pairs = data.get("pairs", [])
        if not pairs:
            return None
        # Pick highest-liquidity Solana pair
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not sol_pairs:
            sol_pairs = pairs
        sol_pairs.sort(
            key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0),
            reverse=True,
        )
        return sol_pairs[0]
    except Exception:
        return None

@st.cache_data(ttl=120)
def fetch_helius_token_holders(token_address):
    try:
        api_key = st.secrets.get("HELIUS_API_KEY", "")
        if not api_key:
            return None, "no_key"
        url     = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
        payload = {
            "jsonrpc": "2.0",
            "id":      1,
            "method":  "getTokenLargestAccounts",
            "params":  [token_address],
        }
        resp = requests.post(url, json=payload, timeout=15)
        data = resp.json()
        holders = data.get("result", {}).get("value", [])
        return holders, None
    except Exception as e:
        return None, str(e)

@st.cache_data(ttl=120)
def fetch_helius_transactions(token_address, limit=100):
    try:
        api_key = st.secrets.get("HELIUS_API_KEY", "")
        if not api_key:
            return None, "no_key"
        url  = (
            f"https://api.helius.xyz/v0/addresses/{token_address}/transactions"
            f"?api-key={api_key}&limit={limit}"
        )
        resp = requests.get(url, timeout=15)
        return resp.json(), None
    except Exception as e:
        return None, str(e)

# ─────────────────────────────────────────────
# AI ANALYSIS
# ─────────────────────────────────────────────
def run_ai_analysis(prompt, client):
    try:
        msg = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
    except Exception as e:
        return f"Error: {e}"

# ─────────────────────────────────────────────
# CHART HELPERS
# ─────────────────────────────────────────────
def candlestick_chart(hist, ticker):
    fig = go.Figure(data=[go.Candlestick(
        x=hist.index,
        open=hist["Open"],
        high=hist["High"],
        low=hist["Low"],
        close=hist["Close"],
        name=ticker,
        increasing_line_color="#3fb950",
        decreasing_line_color="#f85149",
    )])
    fig.update_layout(**PLOT_TEMPLATE, title=ticker, height=400)
    return fig

def line_chart(series_dict, title=""):
    fig    = go.Figure()
    colors = ["#58a6ff", "#3fb950", "#f85149", "#d29922", "#bc8cff"]
    for i, (name, series) in enumerate(series_dict.items()):
        norm = (series / series.iloc[0] - 1) * 100
        fig.add_trace(go.Scatter(
            x=norm.index, y=norm.values,
            name=name, mode="lines",
            line=dict(color=colors[i % len(colors)], width=2),
        ))
    fig.update_layout(**PLOT_TEMPLATE, title=title, height=350, yaxis_ticksuffix="%")
    return fig

# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ Navigation")
    dashboard_mode = st.radio(
        "Dashboard",
        ["🇦🇺 ASX Portfolio", "🪙 Solana Meme"],
        label_visibility="collapsed",
    )
    st.markdown("---")

    if dashboard_mode == "🇦🇺 ASX Portfolio":
        view = st.radio(
            "View",
            ["Portfolio Overview", "Watchlist", "AKN Analysis", "Price Charts", "AI Analysis"],
        )
    else:
        view = st.radio(
            "View",
            ["Token Overview", "On-Chain Health", "Whale Detection", "Manage Tokens"],
        )

    st.markdown("---")
    st.markdown("### 🔐 AI Access")
    st.text_input(
        "Admin password", type="password", key="refresh_pw",
        placeholder="Enter to unlock AI",
    )

# ─────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────
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
    if st.button("🔄 Refresh", help="Reload latest data"):
        st.cache_data.clear()
        st.rerun()
st.markdown("---")

# ─────────────────────────────────────────────
# AI UNLOCK CHECK
# ─────────────────────────────────────────────
admin_pw    = st.secrets.get("ADMIN_PASSWORD", "")
typed_pw    = st.session_state.get("refresh_pw", "")
ai_unlocked = (not admin_pw) or (typed_pw == admin_pw)

# ═══════════════════════════════════════════════
# ASX DASHBOARD VIEWS
# ═══════════════════════════════════════════════
if dashboard_mode == "🇦🇺 ASX Portfolio":

    # ── Portfolio Overview ──────────────────────
    if view == "Portfolio Overview":
        st.subheader("🗂 Actual Holdings")
        total_value, total_cost = 0.0, 0.0

        for ticker, holding in st.session_state.holdings.items():
            hist, info = fetch_ticker(ticker)
            if hist.empty:
                st.warning(f"Could not load {ticker}.")
                continue

            shares    = holding["shares"]
            avg_entry = holding["avg_entry"]
            name      = holding.get("name", ticker)
            price     = hist["Close"].iloc[-1]
            value     = shares * price
            cost      = shares * avg_entry
            pnl       = value - cost
            pnl_pct   = (pnl / cost * 100) if cost else 0
            mktcap    = info.get("marketCap", None)

            total_value += value
            total_cost  += cost

            with st.expander(f"**{ticker}** — {name}", expanded=True):
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Price",  fmt_aud(price))
                c2.metric("Shares", f"{shares:,}")
                c3.metric("Value",  fmt_aud(value))
                c4.metric(
                    "P&L",
                    f"{fmt_aud(pnl)} ({pnl_pct:+.1f}%)",
                    delta=f"{pnl_pct:+.1f}%",
                )
                if mktcap:
                    st.caption(
                        f"Market cap: {fmt_aud(mktcap)}  •  Avg entry: A${avg_entry:.4f}"
                    )

        st.markdown("---")
        total_pnl     = total_value - total_cost
        total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0
        tc1, tc2, tc3 = st.columns(3)
        tc1.metric("Total Portfolio Value", fmt_aud(total_value))
        tc2.metric("Total Cost Basis",      fmt_aud(total_cost))
        tc3.metric(
            "Total P&L",
            f"{fmt_aud(total_pnl)} ({total_pnl_pct:+.1f}%)",
            delta=f"{total_pnl_pct:+.1f}%",
        )

        # Shareable link
        st.markdown("---")
        encoded = encode_holdings(st.session_state.holdings)
        st.caption(f"Share link: `?portfolio={encoded}`")

    # ── Watchlist ───────────────────────────────
    elif view == "Watchlist":
        st.subheader("👁 Watchlist")
        rows = []
        for ticker, meta in WATCHLIST.items():
            hist, info = fetch_ticker(ticker)
            if hist.empty:
                continue
            price   = hist["Close"].iloc[-1]
            prev    = hist["Close"].iloc[-2] if len(hist) > 1 else price
            chg_pct = (price - prev) / prev * 100
            vol     = hist["Volume"].iloc[-1]
            rows.append({
                "Ticker": ticker,
                "Name":   meta["name"],
                "Price":  fmt_aud(price),
                "1D %":   f"{chg_pct:+.2f}%",
                "Volume": f"{int(vol):,}",
            })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("No watchlist data available.")

    # ── AKN Analysis ────────────────────────────
    elif view == "AKN Analysis":
        st.subheader("⛏ AuKing Mining (AKN.AX) — Milestone Targets")
        hist, info = fetch_ticker("AKN.AX")
        if not hist.empty:
            price           = hist["Close"].iloc[-1]
            shares_on_issue = info.get("sharesOutstanding", None)

            if shares_on_issue:
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
                st.dataframe(
                    pd.DataFrame(milestone_rows),
                    use_container_width=True,
                    hide_index=True,
                )

                holding = st.session_state.holdings.get("AKN.AX", {})
                shares  = holding.get("shares", 0)
                if shares:
                    st.markdown("#### Portfolio value at each milestone")
                    val_rows = []
                    for ms in AKN_MILESTONES:
                        target_price = ms["target_cap_m"] * 1e6 / shares_on_issue
                        val_rows.append({
                            "Stage": ms["stage"],
                            "Value": fmt_aud(shares * target_price),
                            "Gain":  fmt_aud(shares * (target_price - price)),
                        })
                    st.dataframe(
                        pd.DataFrame(val_rows),
                        use_container_width=True,
                        hide_index=True,
                    )
            else:
                st.info("Shares outstanding not available from Yahoo Finance.")
        else:
            st.error("Could not load AKN.AX data.")

    # ── Price Charts ────────────────────────────
    elif view == "Price Charts":
        st.subheader("📊 Price Charts")
        all_tickers = list(st.session_state.holdings.keys()) + list(WATCHLIST.keys())
        selected    = st.selectbox("Select ticker", all_tickers)
        chart_type  = st.radio("Chart type", ["Candlestick", "Line"], horizontal=True)

        hist, _ = fetch_ticker(selected)
        if not hist.empty:
            if chart_type == "Candlestick":
                st.plotly_chart(candlestick_chart(hist, selected), use_container_width=True)
            else:
                st.plotly_chart(
                    line_chart({selected: hist["Close"]}, title=selected),
                    use_container_width=True,
                )

        st.markdown("---")
        st.subheader(f"📈 Relative Performance (since {COMPARISON_START})")
        comp_tickers = list(st.session_state.holdings.keys())
        comp_data    = fetch_comparison(comp_tickers, COMPARISON_START)
        if comp_data:
            st.plotly_chart(
                line_chart(comp_data, "Relative Return (%)"),
                use_container_width=True,
            )

    # ── AI Analysis ─────────────────────────────
    elif view == "AI Analysis":
        st.subheader("🤖 AI Portfolio Analysis")
        if not ai_unlocked:
            st.warning("Enter admin password in the sidebar to unlock AI features.")
        else:
            client = get_anthropic_client()
            if not client:
                st.error("ANTHROPIC_API_KEY not configured in secrets.")
            else:
                context_lines = []
                for ticker, holding in st.session_state.holdings.items():
                    hist, info = fetch_ticker(ticker)
                    if not hist.empty:
                        price   = hist["Close"].iloc[-1]
                        pnl_pct = (price / holding["avg_entry"] - 1) * 100
                        context_lines.append(
                            f"{ticker} ({holding.get('name', ticker)}): "
                            f"price A${price:.4f}, entry A${holding['avg_entry']:.4f}, "
                            f"P&L {pnl_pct:+.1f}%, shares {holding['shares']:,}"
                        )
                context = "\n".join(context_lines)

                analysis_type = st.selectbox(
                    "Analysis type",
                    ["Portfolio Summary", "Risk Assessment", "Catalyst Watch", "Exit Strategy"],
                )
                prompts = {
                    "Portfolio Summary": (
                        f"Analyse this ASX small-cap portfolio and provide a concise summary "
                        f"of current positioning, key risks, and near-term catalysts:\n\n{context}"
                    ),
                    "Risk Assessment": (
                        f"Assess the key risks in this ASX portfolio. Focus on concentration "
                        f"risk, sector exposure, and downside scenarios:\n\n{context}"
                    ),
                    "Catalyst Watch": (
                        f"For each stock in this portfolio, identify the most important upcoming "
                        f"catalysts (drilling results, resource estimates, government decisions, "
                        f"etc.) that could move the price:\n\n{context}"
                    ),
                    "Exit Strategy": (
                        f"Suggest exit strategy guidelines for each position in this portfolio, "
                        f"including target prices and stop-loss levels:\n\n{context}"
                    ),
                }

                if st.button("🚀 Run Analysis"):
                    with st.spinner("Analysing with Claude..."):
                        result = run_ai_analysis(prompts[analysis_type], client)
                    st.markdown(result)

# ═══════════════════════════════════════════════
# SOLANA MEME DASHBOARD VIEWS
# ═══════════════════════════════════════════════
else:

    # ── Token Overview ──────────────────────────
    if view == "Token Overview":
        st.subheader("🪙 Solana Token Overview")
        if not st.session_state.tokens:
            st.info("No tokens added yet. Go to **Manage Tokens** to add some.")
        else:
            for symbol, token in st.session_state.tokens.items():
                addr = token["address"]
                name = token.get("name", symbol)
                pair = fetch_dexscreener(addr)

                with st.expander(
                    f"**{symbol}** — {name}  `{shorten_addr(addr)}`", expanded=True
                ):
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
                    buys24    = pair.get("txns", {}).get("h24", {}).get("buys",  0) or 0
                    sells24   = pair.get("txns", {}).get("h24", {}).get("sells", 0) or 0
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

    # ── On-Chain Health ─────────────────────────
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

            pair = fetch_dexscreener(addr)
            if pair:
                liq       = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                mktcap    = float(pair.get("marketCap") or pair.get("fdv") or 0)
                liq_ratio = liq / mktcap * 100 if mktcap else 0

                st.markdown("#### 💧 Liquidity Health")
                l1, l2, l3 = st.columns(3)
                l1.metric("Liquidity (USD)", fmt_usd(liq))
                l2.metric("Market Cap",      fmt_usd(mktcap))
                l3.metric("Liq / MCap",      f"{liq_ratio:.1f}%")

                if liq_ratio < 2:
                    st.markdown(
                        '<span class="warn-flag">⚠️ Very low liquidity ratio — rug risk elevated</span>',
                        unsafe_allow_html=True,
                    )
                elif liq_ratio < 5:
                    st.markdown(
                        '<span class="warn-flag">⚠️ Low liquidity ratio — use caution</span>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        '<span class="safe-flag">✅ Healthy liquidity ratio</span>',
                        unsafe_allow_html=True,
                    )

                st.markdown("#### 📊 Buy / Sell Pressure")
                txns    = pair.get("txns", {})
                periods = [("5m", "m5"), ("1h", "h1"), ("6h", "h6"), ("24h", "h24")]
                pressure_rows = []
                for label, key in periods:
                    b     = txns.get(key, {}).get("buys",  0) or 0
                    s     = txns.get(key, {}).get("sells", 0) or 0
                    total = b + s
                    ratio = b / total * 100 if total else 50
                    pressure_rows.append({
                        "Period": label,
                        "Buys":   b,
                        "Sells":  s,
                        "Buy %":  f"{ratio:.0f}%",
                    })
                st.dataframe(
                    pd.DataFrame(pressure_rows), use_container_width=True, hide_index=True
                )

            st.markdown("---")
            st.markdown("#### 👥 Holder Concentration (Helius)")
            h_data, h_err = fetch_helius_token_holders(addr)

            if h_data and isinstance(h_data, list) and len(h_data) > 0:
                # uiAmount is decimal-adjusted; fall back to raw amount string if absent
                def get_amt(h):
                    ui = h.get("uiAmount")
                    if ui is not None:
                        return float(ui)
                    return float(h.get("amount", 0))

                total_supply = sum(get_amt(h) for h in h_data)
                top3         = sum(get_amt(h) for h in h_data[:3])
                top3_pct     = top3 / total_supply * 100 if total_supply else 0

                holder_rows = []
                for i, h in enumerate(h_data[:10], 1):
                    amt = get_amt(h)
                    pct = amt / total_supply * 100 if total_supply else 0
                    holder_rows.append({
                        "Rank":     i,
                        "Address":  shorten_addr(h.get("address", "—")),
                        "Amount":   f"{amt:,.2f}",
                        "% Supply": f"{pct:.2f}%",
                    })
                st.dataframe(
                    pd.DataFrame(holder_rows), use_container_width=True, hide_index=True
                )

                if top3_pct > 50:
                    st.markdown(
                        f'<span class="warn-flag">⚠️ Top 3 holders: {top3_pct:.1f}% '
                        f'— high concentration risk</span>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f'<span class="safe-flag">✅ Top 3 holders: {top3_pct:.1f}% '
                        f'— reasonable distribution</span>',
                        unsafe_allow_html=True,
                    )
            elif h_err == "no_key":
                st.caption("Add HELIUS_API_KEY to secrets for holder data.")
            else:
                st.info("No holder data available.")

    # ── Whale Detection ─────────────────────────
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
                min_value=1_000,
                max_value=100_000,
                value=10_000,
                step=1_000,
                format="$%d",
            )

            pair      = fetch_dexscreener(addr)
            price_usd = float(pair.get("priceUsd", 0) or 0) if pair else 0

            txns, t_err = fetch_helius_transactions(addr, limit=100)

            if txns and isinstance(txns, list):
                whales = []
                for tx in txns:
                    if not isinstance(tx, dict):
                        continue
                    token_transfers = tx.get("tokenTransfers", []) or []
                    timestamp       = tx.get("timestamp", 0)
                    tx_type         = tx.get("type", "UNKNOWN")
                    sig             = tx.get("signature", "")

                    for tt in token_transfers:
                        try:
                            amount  = float(tt.get("tokenAmount", 0) or 0)
                            usd_val = amount * price_usd
                            if usd_val >= whale_threshold:
                                whales.append({
                                    "Time":  (
                                        datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
                                        if timestamp else "—"
                                    ),
                                    "Type":  tx_type,
                                    "Value": fmt_usd(usd_val),
                                    "From":  shorten_addr(tt.get("fromUserAccount", "")),
                                    "To":    shorten_addr(tt.get("toUserAccount", "")),
                                    "Sig":   sig[:8] + "..." if sig else "—",
                                })
                        except Exception:
                            continue

                if whales:
                    st.markdown(
                        f"**{len(whales)} whale transaction(s) detected "
                        f"(≥ {fmt_usd(whale_threshold)})**"
                    )
                    st.dataframe(pd.DataFrame(whales), use_container_width=True, hide_index=True)
                else:
                    st.success(
                        f"No whale transactions ≥ {fmt_usd(whale_threshold)} in recent history."
                    )
            elif t_err == "no_key":
                st.caption("Add HELIUS_API_KEY to secrets for transaction data.")
            else:
                st.info("No transaction data available.")

    # ── Manage Tokens ───────────────────────────
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
            submitted = st.form_submit_button("Add Token")
            if submitted:
                if sym and addr:
                    st.session_state.tokens[sym.upper()] = {
                        "address": addr,
                        "name":    name or sym,
                    }
                    st.success(f"Added {sym.upper()}.")
                    st.rerun()
                else:
                    st.error("Symbol and address are required.")

# ─────────────────────────────────────────────
# FOOTER
# ─────────────────────────────────────────────
st.markdown("---")
st.caption(
    "⚠️ Not financial advice. Meme coins are extremely high risk. "
    "ASX data via Yahoo Finance. Crypto data via DexScreener + Helius. "
    "AI analysis via Anthropic Claude with web search."
)
