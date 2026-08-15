#!/usr/bin/env python3
"""
Valuation Ledger — self-contained Streamlit app.

Runs identically local or deployed (Streamlit Community Cloud via
GitHub): no absolute local paths, no import from any other project's
scripts (see screener_fetch.py, bundled alongside this file). The
Screener.in session cookie comes from st.secrets — Streamlit's own
secrets mechanism, which transparently reads .streamlit/secrets.toml
locally and the dashboard-configured secrets when deployed, so the same
code path works in both places with zero branching.

Setup (local): copy .streamlit/secrets.toml.example to
.streamlit/secrets.toml and fill in SCREENER_SESSION_ID (git-ignored,
never commit the real value).

Setup (Streamlit Community Cloud): push this repo to GitHub, deploy at
share.streamlit.io, then add SCREENER_SESSION_ID under the app's
Settings -> Secrets in the same TOML format.

Run locally:
    streamlit run app.py
"""
import json, os

import streamlit as st

from screener_fetch import fetch_one

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
ALL_STOCKS_PATH = os.path.join(CACHE_DIR, "all_stocks.json")

st.set_page_config(page_title="Valuation Ledger", page_icon="🧮", layout="wide")


# ───────────────────────── Session cookie (via st.secrets) ─────────────────────────

def get_session_id():
    """None if not configured — callers must handle that, not assume a
    value. Reads st.secrets, which Streamlit populates from
    .streamlit/secrets.toml locally or the Cloud dashboard's Secrets
    panel when deployed — same lookup, no environment-specific code."""
    try:
        return st.secrets["SCREENER_SESSION_ID"]
    except (KeyError, FileNotFoundError):
        return None


# ───────────────────────── Data I/O ─────────────────────────

def load_all_stocks():
    if not os.path.exists(ALL_STOCKS_PATH):
        return {}
    with open(ALL_STOCKS_PATH) as f:
        return json.load(f)


def save_all_stocks(data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(ALL_STOCKS_PATH, "w") as f:
        json.dump(data, f, indent=2)


# ───────────────────────── Format helpers ─────────────────────────

def fmt(v, digits=0, suffix=""):
    if v is None:
        return "—"
    return f"{v:,.{digits}f}{suffix}"


# ───────────────────────── Theme ─────────────────────────

def inject_css():
    st.markdown("""
    <style>
      :root {
        --vl-bg: #0F1512; --vl-surface: #171F19; --vl-surface-2: #2B2312;
        --vl-ink: #E8EEE9; --vl-muted: #9CAA9F; --vl-faint: #718074;
        --vl-border: #2B362F; --vl-accent: #52C29D; --vl-brass: #E0B34D;
        --vl-good: #63C46E; --vl-bad: #E3776A;
      }
      .stApp { font-variant-numeric: tabular-nums; }
      h1, h2, h3 { letter-spacing: 0.1px; }

      table.vl-table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
      table.vl-table th { text-align: left; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em;
                           color: var(--vl-faint); padding: 8px 10px; border-bottom: 1px solid var(--vl-border); }
      table.vl-table td { padding: 8px 10px; border-bottom: 1px solid var(--vl-border); color: var(--vl-ink); }
      table.vl-table tr:hover td { background: rgba(82,194,157,0.06); }
      table.vl-table td.vl-num { text-align: right; font-variant-numeric: tabular-nums; }
      .vl-sub { color: var(--vl-muted) !important; font-size: 11.5px; }

      .vl-empty { background: var(--vl-surface); border: 1px dashed var(--vl-border); border-radius: 12px;
                  padding: 40px 24px; text-align: center; color: var(--vl-muted); margin: 18px 0; }
    </style>
    """, unsafe_allow_html=True)


def render_summary_table(all_stocks):
    rows_html = ""
    for ticker, stock in all_stocks.items():
        rows_html += (
            f'<tr><td><strong>{stock["name"]}</strong><br><span class="vl-sub">{ticker}</span></td>'
            f'<td class="vl-num">₹{fmt(stock.get("current_price"))}</td>'
            f'<td class="vl-num">{fmt(stock.get("pe_ratio"), 1)}x</td>'
            f'<td class="vl-num">₹{fmt(stock.get("market_cap_cr"))} Cr</td>'
            f'<td class="vl-num">₹{fmt(stock.get("week52_high"))}</td></tr>'
        )
    st.markdown(f'''
    <div style="overflow-x:auto;">
    <table class="vl-table">
      <thead><tr>
        <th>Company</th><th style="text-align:right;">Price</th><th style="text-align:right;">P/E</th>
        <th style="text-align:right;">Mkt Cap</th><th style="text-align:right;">52W High</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
    </div>
    ''', unsafe_allow_html=True)


# ───────────────────────── Main ─────────────────────────

def main():
    inject_css()
    st.title("🧮 Valuation Ledger")
    st.caption("Summary — retrieve companies live from Screener.in")

    session_id = get_session_id()
    all_stocks = load_all_stocks()

    if all_stocks:
        if st.button("🔄 Refresh all now", disabled=not session_id,
                      help="Re-fetches every company below from Screener.in live"):
            progress = st.progress(0.0, text="Refreshing…")
            tickers = list(all_stocks.keys())
            for i, t in enumerate(tickers):
                data, err = fetch_one(t, session_id)
                if not err:
                    all_stocks[t] = data
                progress.progress((i + 1) / len(tickers), text=f"Refreshed {t}")
            save_all_stocks(all_stocks)
            progress.empty()
            st.success(f"Refreshed {len(tickers)} companies.")
            st.rerun()
        render_summary_table(all_stocks)
    else:
        st.markdown('<div class="vl-empty">No companies yet — retrieve one from Screener.in below.</div>',
                    unsafe_allow_html=True)

    st.divider()
    st.subheader("📥 Retrieve from Screener")

    if not session_id:
        st.error("No `SCREENER_SESSION_ID` configured — retrieval is disabled until you set it. "
                 "Local: copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill it in. "
                 "Deployed: add it under the app's Settings → Secrets on Streamlit Community Cloud.")

    with st.form("retrieve_form", clear_on_submit=True):
        col1, col2 = st.columns([4, 1])
        with col1:
            new_ticker = st.text_input("NSE/BSE ticker or company name",
                                        placeholder="e.g. TITAN, or a numeric BSE code for SME names",
                                        disabled=not session_id).strip()
        with col2:
            st.write("")
            submitted = st.form_submit_button("Retrieve", type="primary",
                                               use_container_width=True, disabled=not session_id)

    if submitted and new_ticker:
        with st.spinner(f"Fetching {new_ticker} from Screener.in…"):
            data, err = fetch_one(new_ticker.upper(), session_id)
        if err:
            st.error(f"Couldn't fetch **{new_ticker}**: {err}")
        else:
            # Store under the *resolved canonical* ticker (data["ticker"]),
            # not the raw typed text — free-text input can resolve to a
            # different symbol/code than what was typed (e.g. a company
            # name resolving to a numeric BSE/SME code), and later
            # refreshes need the real symbol as the storage key.
            all_stocks[data["ticker"]] = data
            save_all_stocks(all_stocks)
            st.success(f"Retrieved **{data['name']}** ({data['ticker']}) — price ₹{fmt(data['current_price'])}, "
                       f"PE {fmt(data['pe_ratio'], 1)}x.")
            st.rerun()
    elif submitted:
        st.warning("Type a ticker or company name first.")


if __name__ == "__main__":
    main()
