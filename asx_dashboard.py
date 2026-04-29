"""
ASX Portfolio Dashboard — Hedge Fund Grade + AI Recommendations
Built with Python, Streamlit, yfinance, Plotly, and Anthropic SDK
Run: streamlit run dashboard.py
Deploy: Push to GitHub → link to share.streamlit.io
Secrets: Add ANTHROPIC_API_KEY and ADMIN_PASSWORD to Streamlit Cloud secrets
"""

import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, date
import anthropic
import google.generativeai as genai
import json
import urllib.parse
import warnings
warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="ASX Portfolio Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ─────────────────────────────────────────────
# THEME
# ─────────────────────────────────────────────
st.markdown("""
<style>
    html, body, [class*="css"] {
        background-color: #0d1117; color: #e6edf3; font-family: 'Inter', sans-serif;
    }
    .stApp { background-color: #0d1117; }
    section[data-testid="stSidebar"] { background-color: #161b22; border-right: 1px solid #30363d; }
    section[data-testid="stSidebar"] * { color: #e6edf3 !important; }
    div[data-testid="metric-container"] {
        background-color: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px;
    }
    div[data-testid="metric-container"] label { color: #adbac7 !important; font-size: 12px; text-transform: uppercase; }
    div[data-testid="metric-container"] div[data-testid="stMetricValue"] { color: #ffffff !important; font-size: 22px !important; font-weight: 700 !important; }
    div[data-testid="metric-container"] div[data-testid="stMetricDelta"] { color: #adbac7 !important; }
    p, li, span, div { color: #e6edf3; }
    h1, h2, h3, h4 { color: #ffffff !important; }
    h1 { border-bottom: 1px solid #30363d; padding-bottom: 12px; }
    .stDataFrame { background-color: #161b22; }
    .stDataFrame * { color: #e6edf3 !important; }
    thead tr th { background-color: #21262d !important; color: #adbac7 !important; font-size: 11px !important; text-transform: uppercase !important; }
    tbody tr td { color: #e6edf3 !important; }
    tbody tr:hover { background-color: #1c2128 !important; }
    button[data-baseweb="tab"] { color: #adbac7 !important; }
    button[data-baseweb="tab"][aria-selected="true"] { color: #58a6ff !important; border-bottom: 2px solid #58a6ff !important; }
    details summary { color: #58a6ff !important; font-weight: 600; }
    details { background-color: #161b22 !important; border: 1px solid #30363d !important; border-radius: 8px; }
    .stButton button { background-color: #21262d !important; color: #e6edf3 !important; border: 1px solid #30363d !important; border-radius: 6px !important; }
    .stButton button:hover { background-color: #30363d !important; border-color: #58a6ff !important; color: #ffffff !important; }
    .stSelectbox div, .stRadio div { color: #e6edf3 !important; }
    .stRadio label { color: #e6edf3 !important; }
    .stAlert { background-color: #161b22 !important; color: #e6edf3 !important; border-color: #30363d !important; }
    .stCaption, small { color: #adbac7 !important; }
    .rec-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 12px; color: #e6edf3; }
    .rec-section-title { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }
    .signal-badge { display: inline-block; padding: 6px 18px; border-radius: 20px; font-weight: 700; font-size: 15px; letter-spacing: 0.08em; }
    .pill { display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; margin-right: 6px; margin-bottom: 4px; }
    .streamlit-expanderContent p { color: #e6edf3 !important; }
    .streamlit-expanderContent { color: #e6edf3 !important; }
    hr { border-color: #30363d; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# CONSTANTS
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
SIGNAL_COLORS = {
    "STRONG BUY": "#3fb950",
    "ACCUMULATE": "#58a6ff",
    "HOLD":       "#e3b341",
    "SELL":       "#f85149",
    "AVOID":      "#8b949e",
}
PLOTLY_LAYOUT = dict(
    paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
    font=dict(color="#c9d1d9", family="Inter"),
    xaxis=dict(gridcolor="#21262d", showgrid=True, zeroline=False),
    yaxis=dict(gridcolor="#21262d", showgrid=True, zeroline=False),
    legend=dict(bgcolor="#161b22", bordercolor="#30363d", borderwidth=1),
    margin=dict(l=0, r=0, t=40, b=0),
)

# ─────────────────────────────────────────────
# URL PARAMETER HELPERS
# ─────────────────────────────────────────────
def encode_holdings(holdings: dict) -> str:
    payload = {t: {"s": h["shares"], "e": h["avg_entry"], "n": h.get("name", t)}
               for t, h in holdings.items()}
    return urllib.parse.quote(json.dumps(payload, separators=(",", ":")))

def decode_holdings(encoded: str) -> dict:
    try:
        payload = json.loads(urllib.parse.unquote(encoded))
        return {t: {"shares": v["s"], "avg_entry": v["e"], "name": v.get("n", t)}
                for t, v in payload.items()}
    except:
        return {}

# ─────────────────────────────────────────────
# INITIALISE SESSION STATE
# ─────────────────────────────────────────────
def init_holdings():
    if "holdings" not in st.session_state:
        url_param = st.query_params.get("portfolio", "")
        if url_param:
            decoded = decode_holdings(url_param)
            if decoded:
                st.session_state.holdings = decoded
                return
        st.session_state.holdings = {
            t: {"shares": m["shares"], "avg_entry": m["avg_entry"], "name": m["name"]}
            for t, m in ACTUAL_HOLDINGS.items()
        }
    if "catalyst_data" not in st.session_state:
        st.session_state.catalyst_data = None
    if "catalyst_error" not in st.session_state:
        st.session_state.catalyst_error = None
    if "catalyst_generated_at" not in st.session_state:
        st.session_state.catalyst_generated_at = None

init_holdings()

# ─────────────────────────────────────────────
# ANTHROPIC CLIENT
# ─────────────────────────────────────────────
@st.cache_resource
def get_gemini_client():
    try:
        api_key = st.secrets["GEMINI_API_KEY"]
        genai.configure(api_key=api_key)
        # Use GenerativeModel without tools — we'll prompt for search behaviour instead
        model = genai.GenerativeModel(model_name="gemini-2.0-flash-exp")
        return model
    except Exception as e:
        st.error(f"Gemini init error: {e}")
        return None


    try:
        api_key = st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        api_key = None
    if not api_key:
        return None
    return anthropic.Anthropic(api_key=api_key)

# ─────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────
@st.cache_data(ttl=300)
def fetch_ticker(ticker, period="6mo"):
    for _ in range(3):
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period=period, interval="1d")
            info = {}
            try:
                info = t.info
            except:
                pass
            if not hist.empty:
                return hist, info
        except:
            pass
    return pd.DataFrame(), {}

@st.cache_data(ttl=300)
def fetch_comparison(tickers, start):
    try:
        data = yf.download(list(tickers), start=start, auto_adjust=True)["Close"]
        if isinstance(data, pd.Series):
            data = data.to_frame()
        return (data / data.iloc[0]) * 100
    except:
        return pd.DataFrame()

# ─────────────────────────────────────────────
# TECHNICAL INDICATORS
# ─────────────────────────────────────────────
def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calc_obv(close, volume):
    obv = [0]
    for i in range(1, len(close)):
        if close.iloc[i] > close.iloc[i-1]:   obv.append(obv[-1] + volume.iloc[i])
        elif close.iloc[i] < close.iloc[i-1]: obv.append(obv[-1] - volume.iloc[i])
        else:                                  obv.append(obv[-1])
    return pd.Series(obv, index=close.index)

def calc_fisher(high, low, period=10):
    hl2     = (high + low) / 2
    highest = hl2.rolling(period).max()
    lowest  = hl2.rolling(period).min()
    value   = (2 * ((hl2 - lowest) / (highest - lowest + 1e-10)) - 1).clip(-0.999, 0.999)
    return (0.5 * np.log((1 + value) / (1 - value))).rolling(3).mean()

def calc_vwap(df):
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    return (typical * df["Volume"]).cumsum() / df["Volume"].cumsum()

def calc_macd(series, fast=12, slow=26, signal=9):
    macd = series.ewm(span=fast).mean() - series.ewm(span=slow).mean()
    sig  = macd.ewm(span=signal).mean()
    return macd, sig, macd - sig

def liquidity_ratio(volume, window=30):
    avg = volume.rolling(window).mean().iloc[-1]
    return round(volume.iloc[-1] / avg, 2) if avg else 0

def compute_signal(hist):
    try:
        close  = hist["Close"].dropna()
        volume = hist["Volume"].dropna()
        if len(close) < 5:
            return "HOLD", 0, {"rsi": 50, "obv_rising": False, "above_ma20": False,
                                "above_ma50": False, "liq_ratio": 1.0, "day_chg": 0, "score": 0}
        price = close.iloc[-1]
        prev  = close.iloc[-2] if len(close) > 1 else price
        try:
            rsi_val = calc_rsi(close).iloc[-1]
            rsi_val = round(float(rsi_val), 1) if not np.isnan(rsi_val) else 50.0
        except: rsi_val = 50.0
        try:
            obv_s = calc_obv(close, volume)
            obv_rising = obv_s.iloc[-1] > obv_s.iloc[-6] if len(obv_s) >= 6 else False
        except: obv_rising = False
        try:
            ma20 = close.rolling(20).mean().iloc[-1]
            above_ma20 = bool(price > ma20) if not np.isnan(ma20) else False
        except: above_ma20 = False
        try:
            ma50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else np.nan
            above_ma50 = bool(price > ma50) if not np.isnan(ma50) else False
        except: above_ma50 = False
        try:
            liq = float(liquidity_ratio(volume))
            liq = liq if not np.isnan(liq) else 1.0
        except: liq = 1.0
        day_chg = round((price - prev) / prev * 100, 2) if prev else 0
        score = 0
        score += 2 if rsi_val < 30 else 1 if rsi_val < 45 else -2 if rsi_val > 70 else -1 if rsi_val > 60 else 0
        score += 1 if obv_rising else -1
        score += 1 if above_ma20 else -1
        score += 1 if above_ma50 else -1
        score += 1 if liq > 1.5 else -1 if liq < 0.5 else 0
        score += 1 if day_chg > 3 else -1 if day_chg < -3 else 0
        label = ("STRONG BUY" if score >= 4 else "ACCUMULATE" if score >= 2
                 else "HOLD" if score >= -1 else "SELL" if score >= -3 else "AVOID")
        return label, score, {"rsi": rsi_val, "obv_rising": obv_rising,
            "above_ma20": above_ma20, "above_ma50": above_ma50,
            "liq_ratio": liq, "day_chg": day_chg, "score": score}
    except:
        return "HOLD", 0, {"rsi": 50, "obv_rising": False, "above_ma20": False,
                            "above_ma50": False, "liq_ratio": 1.0, "day_chg": 0, "score": 0}

# ─────────────────────────────────────────────
# AI RECOMMENDATION
# ─────────────────────────────────────────────
@st.cache_data(ttl=1800, show_spinner=False)
def get_ai_recommendation(ticker, name, price, signal_label, score, indicators):
    client = get_anthropic_client()
    if not client:
        return None, "ANTHROPIC_API_KEY not configured."
    prompt = f"""You are a senior equities analyst specialising in ASX small-cap and micro-cap stocks.

Ticker: {ticker} ({name})
Current Price: ${price:.4f} AUD
Rule-based Signal: {signal_label} (score: {score}/6)
RSI (14): {indicators['rsi']}
OBV Trend: {"Rising — accumulation" if indicators['obv_rising'] else "Falling — distribution"}
Price vs MA20: {"Above" if indicators['above_ma20'] else "Below"}
Price vs MA50: {"Above" if indicators['above_ma50'] else "Below"}
Liquidity Ratio (vs 30-day avg vol): {indicators['liq_ratio']}x
Day Change: {indicators['day_chg']:+.2f}%

Use your web search tool to find:
1. Recent news, broker commentary, or sentiment for {ticker}
2. Any ASX substantial holder notices (Form 603/604) or known institutional activity
3. Macro or sector tailwinds/headwinds relevant to this stock

Respond ONLY with a valid JSON object — no markdown fences, no preamble:
{{
  "signal": "{signal_label}",
  "rationale": "2-3 sentence technical rationale referencing the indicators",
  "sentiment": "Current market sentiment, recent news, broker views, sector tailwinds/headwinds",
  "holders": "Top holder activity — substantial holder notices, institutional buying/selling, or note if data is limited",
  "risks": "1-2 key risks to the thesis",
  "sources": ["source 1", "source 2"],
  "disclaimer": "Not financial advice. For informational purposes only."
}}"""
    try:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1200,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in response.content if hasattr(b, "text"))
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        start = cleaned.find("{"); end = cleaned.rfind("}") + 1
        if start == -1 or end == 0:
            return None, "Could not parse AI response."
        return json.loads(cleaned[start:end]), None
    except Exception as e:
        return None, f"API error: {str(e)}"

# ─────────────────────────────────────────────
# AI CATALYST PIPELINE (GEMINI 2.0 FLASH)
# ─────────────────────────────────────────────
@st.cache_data(ttl=7200, show_spinner=False)
def get_ai_catalysts(tickers_tuple):
    model = get_gemini_client()
    if not model:
        # Diagnostic — check what secrets are available
        available = list(st.secrets.keys()) if hasattr(st, "secrets") else []
        return None, f"Gemini client could not be initialised. Keys found in secrets: {available}"

    tickers    = list(tickers_tuple)
    ticker_str = ", ".join(tickers)

    prompt = f"""You are an ASX equities analyst. Search the web and find upcoming catalysts, 
announcements, and key events for these ASX-listed stocks: {ticker_str}.

Find:
1. Recent ASX announcements and quarterly reports for each ticker
2. Upcoming scheduled events (AGMs, drilling results, resource estimates, capital raises, regulatory decisions)
3. Any analyst coverage, broker notes, or sentiment shifts
4. Macro or sector-level events that could materially impact these stocks

Today's date is {date.today().strftime("%d %B %Y")}.

Respond ONLY with a valid JSON array — no markdown fences, no preamble, no explanation:
[
  {{
    "date": "Month Year or specific date if known",
    "ticker": "AKN.AX",
    "event": "Clear description of the event or catalyst",
    "impact": "High|Medium|Low",
    "source": "Source name e.g. ASX announcement, Company website"
  }}
]

Include 3-6 events per ticker where data is available. Order by date ascending."""

    try:
        response = model.generate_content(
            prompt,
            tools=[{"google_search": {}}],
        )
        raw     = response.text
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        start   = cleaned.find("[")
        end     = cleaned.rfind("]") + 1
        if start == -1 or end == 0:
            return None, "Could not parse Gemini response."
        return json.loads(cleaned[start:end]), None
    except Exception as e:
        return None, f"Gemini API error: {str(e)}"

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def signal_badge_html(label):
    color = SIGNAL_COLORS.get(label, "#8b949e")
    return (f'<span class="signal-badge" style="background:{color}22;color:{color};'
            f'border:2px solid {color};">{label}</span>')

def pill_html(text, color):
    return f'<span class="pill" style="background:{color}22;color:{color};border:1px solid {color};">{text}</span>'

# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ Controls")
    st.markdown("---")
    view = st.radio("View", ["Portfolio Overview", "Stock Deep-Dive", "Comparison Chart", "Catalyst Pipeline"])
    st.markdown("---")
    if view == "Stock Deep-Dive":
        all_tickers    = list(st.session_state.holdings.keys()) + list(WATCHLIST.keys())
        selected_ticker = st.selectbox("Select Ticker", all_tickers)
        timeframe       = st.selectbox("Timeframe", ["1mo", "3mo", "6mo", "1y", "2y"], index=2)
    else:
        selected_ticker = list(st.session_state.holdings.keys())[0]
        timeframe       = "6mo"

    st.markdown("---")
    st.markdown("### ✏️ Edit Portfolio")
    st.caption("Update values then click **✅ Apply Changes**.")

    if "form_holdings" not in st.session_state:
        st.session_state.form_holdings = {
            t: {"shares": h["shares"], "avg_entry": h["avg_entry"], "name": h.get("name", t)}
            for t, h in st.session_state.holdings.items()
        }

    tickers_list = list(st.session_state.form_holdings.keys())
    for i, ticker in enumerate(tickers_list):
        h = st.session_state.form_holdings[ticker]
        st.markdown(f"**{ticker}**")
        s_val = st.number_input("Shares", min_value=0, step=1000,
                                value=int(h["shares"]), key=f"fs_{i}")
        e_val = st.number_input("Avg Entry $", min_value=0.0001, step=0.001,
                                format="%.4f", value=float(h["avg_entry"]), key=f"fe_{i}")
        st.session_state.form_holdings[ticker]["shares"]    = s_val
        st.session_state.form_holdings[ticker]["avg_entry"] = e_val
        st.markdown("")

    if st.button("✅ Apply Changes", use_container_width=True, type="primary"):
        st.session_state.holdings = {t: dict(h) for t, h in st.session_state.form_holdings.items()}
        encoded = encode_holdings(st.session_state.holdings)
        st.query_params["portfolio"] = encoded
        st.success("✅ Portfolio updated!")
        st.rerun()

    st.markdown("---")
    st.markdown("**➕ Add New Ticker**")
    new_t = st.text_input("Ticker symbol", placeholder="e.g. BHP.AX", key="add_ticker_input")
    new_s = st.number_input("Shares", min_value=0, step=1000, value=0, key="add_shares")
    new_e = st.number_input("Avg Entry $", min_value=0.0001, step=0.001,
                            format="%.4f", value=0.010, key="add_entry")
    if st.button("➕ Add Ticker", use_container_width=True):
        if new_t.strip():
            t = new_t.strip().upper()
            if not t.endswith(".AX"): t += ".AX"
            if t not in st.session_state.form_holdings:
                st.session_state.form_holdings[t] = {"shares": new_s, "avg_entry": new_e, "name": t}
                st.session_state.holdings = {t2: dict(h) for t2, h in st.session_state.form_holdings.items()}
                encoded = encode_holdings(st.session_state.holdings)
                st.query_params["portfolio"] = encoded
                st.success(f"Added {t}!")
                st.rerun()
            else:
                st.warning(f"{t} is already in your portfolio.")
        else:
            st.warning("Enter a ticker symbol first.")

    st.markdown("---")
    st.markdown("**🗑 Remove Ticker**")
    remove_opts = ["— select to remove —"] + list(st.session_state.form_holdings.keys())
    to_remove   = st.selectbox("", remove_opts, key="remove_sel", label_visibility="collapsed")
    if st.button("Remove Selected", use_container_width=True):
        if to_remove != "— select to remove —":
            if len(st.session_state.form_holdings) > 1:
                del st.session_state.form_holdings[to_remove]
                st.session_state.holdings = {t: dict(h) for t, h in st.session_state.form_holdings.items()}
                encoded = encode_holdings(st.session_state.holdings)
                st.query_params["portfolio"] = encoded
                st.success(f"Removed {to_remove}")
                st.rerun()
            else:
                st.warning("Must keep at least one holding.")

    st.markdown("---")
    if st.button("↺ Reset to Defaults", use_container_width=True):
        st.session_state.holdings = {
            t: {"shares": m["shares"], "avg_entry": m["avg_entry"], "name": m["name"]}
            for t, m in ACTUAL_HOLDINGS.items()
        }
        st.session_state.form_holdings = {t: dict(h) for t, h in st.session_state.holdings.items()}
        st.query_params.clear()
        st.rerun()

    st.markdown("---")
    st.markdown("**🔄 Refresh Market Data**")
    refresh_pw = st.text_input("Admin password", type="password", key="refresh_pw",
                               placeholder="Enter password to refresh")
    if st.button("🔄 Refresh Market Data", use_container_width=True):
        admin_pw = st.secrets.get("ADMIN_PASSWORD", "")
        if admin_pw and refresh_pw == admin_pw:
            st.cache_data.clear()
            st.success("Cache cleared — reloading prices...")
            st.rerun()
        elif not admin_pw:
            st.cache_data.clear()
            st.rerun()
        else:
            st.error("Incorrect password.")

    st.markdown("---")
    st.markdown("**🔗 Share Portfolio URL**")
    st.caption("Anyone with this link sees your portfolio pre-loaded.")
    encoded   = encode_holdings(st.session_state.holdings)
    share_url = f"https://nhe5bwk4jecpb5xjrkgnia.streamlit.app/?portfolio={encoded}"
    st.code(share_url, language=None)
    st.caption(f"Last update: {datetime.now().strftime('%H:%M:%S AEST')}")

# ─────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────
col_title, col_refresh = st.columns([5, 1])
with col_title:
    st.markdown("# 📈 ASX Portfolio Dashboard")
    st.markdown(f"<span style='color:#8b949e'>Institutional-grade analytics · {date.today().strftime('%A, %d %B %Y')}</span>",
                unsafe_allow_html=True)
with col_refresh:
    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("🔄 Refresh", help="Clear cache and reload all live data"):
        st.cache_data.clear()
        st.rerun()
st.markdown("---")

# ─────────────────────────────────────────────
# VIEW: PORTFOLIO OVERVIEW
# ─────────────────────────────────────────────
if view == "Portfolio Overview":
    st.subheader("🗂 Actual Holdings")
    total_value, total_cost = 0, 0

    for ticker, holding in st.session_state.holdings.items():
        hist, info = fetch_ticker(ticker)
        if hist.empty:
            st.warning(f"Could not load data for {ticker}. Click 'Refresh Market Data' to retry.")
            continue

        shares    = holding["shares"]
        avg_entry = holding["avg_entry"]
        name      = holding.get("name", ticker)
        price     = hist["Close"].iloc[-1]
        prev      = hist["Close"].iloc[-2] if len(hist) > 1 else price
        day_chg   = (price - prev) / prev * 100
        cost      = avg_entry * shares
        value     = price * shares
        unreal    = value - cost
        unreal_p  = (unreal / cost * 100) if cost else 0
        be_dist   = (avg_entry - price) / price * 100
        mktcap    = info.get("marketCap", None)

        sig_label, sig_score, indicators = compute_signal(hist)
        total_value += value
        total_cost  += cost

        with st.container():
            st.markdown(f"""
            <div class="rec-card">
              <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
                <div>
                  <span style="color:#58a6ff;font-weight:700;font-size:20px;">{ticker}</span>
                  <span style="color:#8b949e;font-size:14px;margin-left:10px;">{name}</span>
                </div>
                {signal_badge_html(sig_label)}
              </div>
            </div>
            """, unsafe_allow_html=True)

        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Avg Entry",   f"${avg_entry:.4f}")
        c2.metric("Last Price",  f"${price:.4f}", f"{day_chg:+.2f}%")
        c3.metric("Shares",      f"{shares:,}" if shares else "—")
        c4.metric("Unrealised",  f"${unreal:+,.0f}" if shares else "—",
                                 f"{unreal_p:+.1f}%" if shares else None)
        c5.metric("BE Distance", f"{be_dist:+.1f}%")
        c6.metric("Mkt Cap",     f"${mktcap/1e6:.1f}M" if mktcap else "N/A")

        pills = [
            (f"RSI {indicators['rsi']}", "#3fb950" if indicators['rsi'] < 30 else "#f85149" if indicators['rsi'] > 70 else "#8b949e"),
            (f"OBV {'↑ Accum.' if indicators['obv_rising'] else '↓ Distrib.'}", "#3fb950" if indicators['obv_rising'] else "#f85149"),
            (f"MA20 {'✓' if indicators['above_ma20'] else '✗'}", "#3fb950" if indicators['above_ma20'] else "#f85149"),
            (f"MA50 {'✓' if indicators['above_ma50'] else '✗'}", "#3fb950" if indicators['above_ma50'] else "#f85149"),
            (f"Liq {indicators['liq_ratio']}x", "#3fb950" if indicators['liq_ratio'] > 1.5 else "#f85149" if indicators['liq_ratio'] < 0.5 else "#8b949e"),
        ]
        st.markdown(" ".join(pill_html(t, c) for t, c in pills), unsafe_allow_html=True)

        with st.expander(f"🤖 AI Recommendation · Market Sentiment · Holder Activity — {ticker}"):
            if st.button(f"Generate Analysis for {ticker}", key=f"btn_{ticker}"):
                with st.spinner(f"Searching market data and analysing {ticker}…"):
                    rec, err = get_ai_recommendation(
                        ticker, name, price, sig_label, sig_score, indicators
                    )
                if err:
                    st.error(err)
                elif rec:
                    ai_sig   = rec.get("signal", sig_label)
                    ai_color = SIGNAL_COLORS.get(ai_sig, SIGNAL_COLORS[sig_label])
                    st.markdown(f"**AI Signal:** {signal_badge_html(ai_sig)}", unsafe_allow_html=True)
                    st.markdown("")
                    col_a, col_b = st.columns(2)
                    with col_a:
                        st.markdown(f"<div class='rec-section-title' style='color:#58a6ff;'>📊 Technical Rationale</div>", unsafe_allow_html=True)
                        st.markdown(rec.get("rationale", ""))
                        st.markdown(f"<div class='rec-section-title' style='color:#e3b341;margin-top:14px;'>🌐 Market Sentiment</div>", unsafe_allow_html=True)
                        st.markdown(rec.get("sentiment", ""))
                    with col_b:
                        st.markdown(f"<div class='rec-section-title' style='color:#bc8cff;'>🏦 Top Holder Activity</div>", unsafe_allow_html=True)
                        st.markdown(rec.get("holders", ""))
                        st.markdown(f"<div class='rec-section-title' style='color:#f85149;margin-top:14px;'>⚠️ Key Risks</div>", unsafe_allow_html=True)
                        st.markdown(rec.get("risks", ""))
                    if rec.get("sources"):
                        st.markdown("**Sources:** " + " · ".join(rec["sources"]))
                    st.caption(rec.get("disclaimer", "Not financial advice."))
            else:
                st.caption("Click the button above to run AI analysis with live web search.")

        st.markdown("---")

    pnl   = total_value - total_cost
    pnl_p = (pnl / total_cost * 100) if total_cost else 0
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Portfolio Value", f"${total_value:,.0f}")
    c2.metric("Total Cost Basis",      f"${total_cost:,.0f}")
    c3.metric("Total Unrealised P&L",  f"${pnl:+,.0f}", f"{pnl_p:+.1f}%")
    c4.metric("Positions",             len(st.session_state.holdings))

    st.markdown("---")
    st.subheader("🎯 AKN Valuation Milestones")
    hist_akn, info_akn = fetch_ticker("AKN.AX")
    if not hist_akn.empty:
        akn_price  = hist_akn["Close"].iloc[-1]
        shares_out = info_akn.get("sharesOutstanding", 1_000_000_000)
        current_cap= akn_price * shares_out
        fig_ms = go.Figure()
        fig_ms.add_hline(y=current_cap/1e6, line_color="#58a6ff", line_dash="dash",
                         annotation_text=f"Current: ${current_cap/1e6:.1f}M", annotation_font_color="#58a6ff")
        for ms in AKN_MILESTONES:
            implied = ms["target_cap_m"] * 1e6 / shares_out
            fig_ms.add_hline(y=ms["target_cap_m"], line_color="#3fb950", line_dash="dot",
                             annotation_text=f"{ms['stage']} ${ms['target_cap_m']}M → ${implied:.4f}/sh",
                             annotation_font_color="#3fb950")
        fig_ms.update_layout(title="AKN.AX Market Cap Milestone Tracker (A$M)",
                             yaxis_title="Market Cap ($M)", height=300, **PLOTLY_LAYOUT)
        st.plotly_chart(fig_ms, use_container_width=True)

    st.markdown("---")
    st.subheader("👁 Watchlist / Prospect Portfolio")
    watch_rows = []
    for ticker, meta in WATCHLIST.items():
        hist, info = fetch_ticker(ticker)
        if hist.empty:
            watch_rows.append({"Ticker": ticker, "Name": meta["name"], "Signal": "N/A",
                                "Last Price": "N/A", "Day Chg%": "N/A", "Mkt Cap": "N/A"})
            continue
        price   = hist["Close"].iloc[-1]
        prev    = hist["Close"].iloc[-2] if len(hist) > 1 else price
        day_chg = (price - prev) / prev * 100
        mktcap  = info.get("marketCap", None)
        sig_label, _, _ = compute_signal(hist)
        watch_rows.append({
            "Ticker":     ticker, "Name": meta["name"], "Signal": sig_label,
            "Last Price": f"${price:.4f}", "Day Chg%": f"{day_chg:+.2f}%",
            "Mkt Cap":    f"${mktcap/1e6:.1f}M" if mktcap else "N/A",
        })
    st.dataframe(pd.DataFrame(watch_rows), use_container_width=True, hide_index=True)

# ─────────────────────────────────────────────
# VIEW: DEEP DIVE
# ─────────────────────────────────────────────
elif view == "Stock Deep-Dive":
    hist, info = fetch_ticker(selected_ticker, period=timeframe)
    if hist.empty:
        st.error(f"Could not load data for {selected_ticker}.")
        st.stop()

    price   = hist["Close"].iloc[-1]
    prev    = hist["Close"].iloc[-2] if len(hist) > 1 else price
    day_chg = (price - prev) / prev * 100
    rsi_val = calc_rsi(hist["Close"]).iloc[-1]
    liq     = liquidity_ratio(hist["Volume"])
    obv_s   = calc_obv(hist["Close"], hist["Volume"])
    obv_trend = "Accumulation 📈" if obv_s.iloc[-1] > obv_s.iloc[-6] else "Distribution 📉"
    macd, sig, macd_h = calc_macd(hist["Close"])
    vwap    = calc_vwap(hist)
    ma20    = hist["Close"].rolling(20).mean()
    ma50    = hist["Close"].rolling(50).mean()
    ma200   = hist["Close"].rolling(200).mean()
    fisher  = calc_fisher(hist["High"], hist["Low"])
    name    = info.get("longName", selected_ticker)
    mktcap  = info.get("marketCap", None)

    st.subheader(f"{name} ({selected_ticker})")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Last Price",      f"${price:.4f}", f"{day_chg:+.2f}%")
    c2.metric("Mkt Cap",         f"${mktcap/1e6:.1f}M" if mktcap else "N/A")
    c3.metric("RSI (14)",        f"{rsi_val:.1f}", "Overbought" if rsi_val > 70 else "Oversold" if rsi_val < 30 else "Neutral")
    c4.metric("Liquidity Ratio", f"{liq}x")
    c5.metric("OBV Trend",       obv_trend)
    st.markdown("---")

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.03)
    fig.add_trace(go.Candlestick(x=hist.index, open=hist["Open"], high=hist["High"],
        low=hist["Low"], close=hist["Close"], name="Price",
        increasing_line_color="#3fb950", decreasing_line_color="#f85149",
        increasing_fillcolor="#1a3a2a", decreasing_fillcolor="#3a1a1a"), row=1, col=1)
    for ma, color, label in [(ma20,"#58a6ff","MA20"),(ma50,"#e3b341","MA50"),(ma200,"#bc8cff","MA200")]:
        fig.add_trace(go.Scatter(x=hist.index, y=ma, line=dict(color=color, width=1), name=label), row=1, col=1)
    fig.add_trace(go.Scatter(x=hist.index, y=vwap, line=dict(color="#ff7b72", width=1, dash="dot"), name="VWAP"), row=1, col=1)
    colors = ["#3fb950" if c >= o else "#f85149" for c, o in zip(hist["Close"], hist["Open"])]
    fig.add_trace(go.Bar(x=hist.index, y=hist["Volume"], marker_color=colors, name="Volume", opacity=0.6), row=2, col=1)
    fig.update_layout(xaxis_rangeslider_visible=False, height=550, **PLOTLY_LAYOUT)
    st.plotly_chart(fig, use_container_width=True)

    t1, t2, t3, t4 = st.tabs(["OBV", "Fisher Transform", "MACD", "RSI"])
    with t1:
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=hist.index, y=obv_s, line=dict(color="#58a6ff"), name="OBV"))
        fig2.update_layout(title="On-Balance Volume", height=280, **PLOTLY_LAYOUT)
        st.plotly_chart(fig2, use_container_width=True)
    with t2:
        fig3 = go.Figure()
        fig3.add_trace(go.Scatter(x=hist.index, y=fisher, line=dict(color="#e3b341"), name="Fisher"))
        fig3.add_hline(y=1.5, line_color="#f85149", line_dash="dash")
        fig3.add_hline(y=-1.5, line_color="#3fb950", line_dash="dash")
        fig3.update_layout(title="Ehlers Fisher Transform (10-day)", height=280, **PLOTLY_LAYOUT)
        st.plotly_chart(fig3, use_container_width=True)
    with t3:
        fig4 = go.Figure()
        hc = ["#3fb950" if v >= 0 else "#f85149" for v in macd_h]
        fig4.add_trace(go.Bar(x=hist.index, y=macd_h, marker_color=hc, name="Histogram"))
        fig4.add_trace(go.Scatter(x=hist.index, y=macd, line=dict(color="#58a6ff"), name="MACD"))
        fig4.add_trace(go.Scatter(x=hist.index, y=sig, line=dict(color="#e3b341", dash="dot"), name="Signal"))
        fig4.update_layout(title="MACD (12/26/9)", height=280, **PLOTLY_LAYOUT)
        st.plotly_chart(fig4, use_container_width=True)
    with t4:
        rsi_s = calc_rsi(hist["Close"])
        fig5  = go.Figure()
        fig5.add_trace(go.Scatter(x=hist.index, y=rsi_s, line=dict(color="#bc8cff"), name="RSI"))
        fig5.add_hline(y=70, line_color="#f85149", line_dash="dash")
        fig5.add_hline(y=30, line_color="#3fb950", line_dash="dash")
        fig5.update_layout(title="RSI (14)", height=280, **PLOTLY_LAYOUT)
        st.plotly_chart(fig5, use_container_width=True)

# ─────────────────────────────────────────────
# VIEW: COMPARISON
# ─────────────────────────────────────────────
elif view == "Comparison Chart":
    st.subheader(f"📊 Normalised Performance — Base 100 from {COMPARISON_START}")
    all_tickers = tuple(list(st.session_state.holdings.keys()) + list(WATCHLIST.keys()))
    normed = fetch_comparison(all_tickers, COMPARISON_START)
    if normed.empty:
        st.warning("No comparison data available."); st.stop()
    colors = {"AKN.AX":"#58a6ff","XST.AX":"#3fb950","G11.AX":"#e3b341","VRC.AX":"#bc8cff","RNX.AX":"#ff7b72"}
    fig = go.Figure()
    for col in normed.columns:
        fig.add_trace(go.Scatter(x=normed.index, y=normed[col], name=col,
                                 line=dict(color=colors.get(col, "#8b949e"), width=2)))
    fig.add_hline(y=100, line_color="#30363d", line_dash="dash")
    fig.update_layout(title=f"Normalised Return since {COMPARISON_START}",
                      yaxis_title="Indexed Return", height=500, **PLOTLY_LAYOUT)
    st.plotly_chart(fig, use_container_width=True)
    summary = [{"Ticker": c, "Return since Jan 1 2026": f"{normed[c].dropna().iloc[-1]-100:+.1f}%",
                "Last Index": f"{normed[c].dropna().iloc[-1]:.1f}"}
               for c in normed.columns if not normed[c].dropna().empty]
    st.dataframe(pd.DataFrame(summary), use_container_width=True, hide_index=True)

# ─────────────────────────────────────────────
# VIEW: CATALYST PIPELINE (AI ON-DEMAND)
# ─────────────────────────────────────────────
elif view == "Catalyst Pipeline":
    st.subheader("⚡ Catalyst Pipeline")
    st.caption("AI-generated via live web search across your portfolio tickers.")

    portfolio_tickers = tuple(st.session_state.holdings.keys())

    if st.session_state.catalyst_generated_at:
        st.caption(f"Last generated: {st.session_state.catalyst_generated_at}")

    if st.button("🔍 Generate / Refresh Catalyst Pipeline", type="primary"):
        with st.spinner("🔍 Searching ASX announcements and upcoming catalysts..."):
            catalysts, err = get_ai_catalysts(portfolio_tickers)
            st.session_state.catalyst_data         = catalysts
            st.session_state.catalyst_error        = err
            st.session_state.catalyst_generated_at = datetime.now().strftime("%d %b %Y %H:%M AEST")
            get_ai_catalysts.clear()

    st.markdown("---")

    if st.session_state.catalyst_error:
        st.error(f"Could not load catalysts: {st.session_state.catalyst_error}")
    elif st.session_state.catalyst_data:
        catalysts     = st.session_state.catalyst_data
        impact_colors = {"High": "#f85149", "Medium": "#e3b341", "Low": "#58a6ff"}

        high_count   = sum(1 for c in catalysts if c.get("impact") == "High")
        medium_count = sum(1 for c in catalysts if c.get("impact") == "Medium")
        low_count    = sum(1 for c in catalysts if c.get("impact") == "Low")

        cc1, cc2, cc3, cc4 = st.columns(4)
        cc1.metric("Total Events",   len(catalysts))
        cc2.metric("🔴 High Impact", high_count)
        cc3.metric("🟡 Medium",      medium_count)
        cc4.metric("🔵 Low",         low_count)
        st.markdown("---")

        for cat in catalysts:
            impact = cat.get("impact", "Medium")
            ticker = cat.get("ticker", "")
            col    = impact_colors.get(impact, "#8b949e")
            source = cat.get("source", "")
            c1, c2, c3, c4, c5 = st.columns([1.2, 1, 3.5, 1, 1.2])
            c1.markdown(f"**{cat.get('date', '—')}**")
            c2.markdown(pill_html(ticker, "#58a6ff"), unsafe_allow_html=True)
            c3.markdown(cat.get("event", ""))
            c4.markdown(pill_html(impact.upper(), col), unsafe_allow_html=True)
            c5.markdown(f"<span style='color:#8b949e;font-size:11px;'>{source}</span>",
                        unsafe_allow_html=True)
            st.markdown("<hr style='margin:4px 0;border-color:#21262d;'>", unsafe_allow_html=True)

        st.caption("⚠️ AI-generated from public sources. Verify against official ASX announcements before making investment decisions.")
    else:
        st.info("👆 Click **Generate / Refresh Catalyst Pipeline** above to search for upcoming catalysts and ASX announcements across your holdings.")

# ─────────────────────────────────────────────
# FOOTER
# ─────────────────────────────────────────────
st.markdown("---")
st.caption("⚠️ This dashboard is for informational purposes only and does not constitute financial advice. "
           "Data sourced from Yahoo Finance. AI analysis powered by Anthropic Claude with web search. "
           "Prices may be delayed. Past performance is not indicative of future results.")
