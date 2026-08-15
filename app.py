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
import json, os, re

import streamlit as st

from screener_fetch import fetch_one

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
ALL_STOCKS_PATH = os.path.join(CACHE_DIR, "all_stocks.json")

FY_LABEL_RE = re.compile(r"^(Mar|Jun|Sep|Dec)\s+\d{4}$")
ARRAY_FIELDS = ["revenue", "revenue_growth_pct", "expenses", "operating_profit", "opm_pct",
                "other_income", "interest", "depreciation", "pbt", "tax_pct", "net_profit",
                "pat_growth_pct", "eps", "shares_cr"]

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

def clean_stock(raw):
    """Strips a trailing "TTM" (or other non-"Mon YYYY") column Screener
    sometimes appends when quarterly data has outpaced the last fiscal
    year-end — left in, it breaks anything reading the latest actual
    year off years[-1]. Same fix as the earlier ValuationTool skill
    build (2026-08-13), ported here since this repo doesn't import that
    code."""
    years = raw["years"]
    fy_idx = [i for i, y in enumerate(years) if FY_LABEL_RE.match(y)]
    stock = {k: v for k, v in raw.items() if k not in ARRAY_FIELDS and k != "years"}
    stock["years"] = [years[i] for i in fy_idx]
    for f in ARRAY_FIELDS:
        stock[f] = [raw[f][i] for i in fy_idx]
    return stock


def load_all_stocks():
    if not os.path.exists(ALL_STOCKS_PATH):
        return {}
    with open(ALL_STOCKS_PATH) as f:
        raw = json.load(f)
    return {ticker: clean_stock(v) for ticker, v in raw.items()}


def load_raw_all_stocks():
    """Unclean (TTM column, if any, intact) — only save_all_stocks()
    should write this shape back out, so a re-clean on next load stays
    idempotent regardless of how many times a stock gets refreshed."""
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
      .vl-pos { color: var(--vl-good) !important; font-weight: 600; }
      .vl-neg { color: var(--vl-bad) !important; font-weight: 600; }

      .vl-empty { background: var(--vl-surface); border: 1px dashed var(--vl-border); border-radius: 12px;
                  padding: 40px 24px; text-align: center; color: var(--vl-muted); margin: 18px 0; }

      .vl-stat-row { display: flex; flex-wrap: wrap; gap: 10px; margin: 6px 0 14px; }
      .vl-stat { background: var(--vl-surface); border: 1px solid var(--vl-border); border-radius: 10px;
                 padding: 10px 16px; min-width: 110px; }
      .vl-stat-label { display: block; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.08em;
                        color: var(--vl-faint) !important; margin-bottom: 3px; }
      .vl-stat-value { font-size: 19px; font-weight: 700; color: var(--vl-ink) !important; }

      /* Slim icon buttons (the summary row's Open/Remove) */
      div[data-testid="stHorizontalBlock"] button[kind="secondary"] { padding: 2px 10px; }
    </style>
    """, unsafe_allow_html=True)


def render_stat_row(items):
    cards = "".join(f'<div class="vl-stat"><span class="vl-stat-label">{lab}</span>'
                     f'<span class="vl-stat-value">{val}</span></div>' for lab, val in items)
    st.markdown(f'<div class="vl-stat-row">{cards}</div>', unsafe_allow_html=True)


def hist_cell_html(v, digits, suffix, colorize, bold):
    text = fmt(v, digits, suffix)
    cls = ""
    if colorize and v is not None:
        cls = "vl-pos" if v >= 0 else "vl-neg"
    weight = "font-weight:700;" if bold else ""
    span = f'<span class="{cls}" style="{weight}">{text}</span>' if cls else f'<span style="{weight}">{text}</span>'
    return f'<div style="text-align:right;">{span}</div>'


# ───────────────────────── Summary page ─────────────────────────

def page_summary(all_stocks):
    if not all_stocks:
        st.markdown('<div class="vl-empty">No companies yet — retrieve one from Screener.in below.</div>',
                    unsafe_allow_html=True)
        return

    session_id = get_session_id()
    if st.button("🔄 Refresh all now", disabled=not session_id,
                  help="Re-fetches every company below from Screener.in live"):
        raw = load_raw_all_stocks()
        progress = st.progress(0.0, text="Refreshing…")
        tickers = list(raw.keys())
        for i, t in enumerate(tickers):
            data, err = fetch_one(t, session_id)
            if not err:
                raw[t] = data
            progress.progress((i + 1) / len(tickers), text=f"Refreshed {t}")
        save_all_stocks(raw)
        progress.empty()
        st.success(f"Refreshed {len(tickers)} companies.")
        st.rerun()

    header = st.columns([3, 1, 1, 1.2, 1.2, 0.6, 0.5])
    for col, label in zip(header, ["Company", "Price", "P/E", "Mkt Cap", "52W High", "", ""]):
        col.markdown(f"**{label}**")
    st.markdown('<hr style="margin:2px 0 8px;border-color:var(--vl-border);">', unsafe_allow_html=True)

    for ticker, stock in all_stocks.items():
        cols = st.columns([3, 1, 1, 1.2, 1.2, 0.6, 0.5])
        cols[0].markdown(f"**{stock['name']}**  \n<span class='vl-sub'>{ticker}</span>", unsafe_allow_html=True)
        cols[1].write(f"₹{fmt(stock.get('current_price'))}")
        cols[2].write(f"{fmt(stock.get('pe_ratio'), 1)}x")
        cols[3].write(f"₹{fmt(stock.get('market_cap_cr'))} Cr")
        cols[4].write(f"₹{fmt(stock.get('week52_high'))}")
        if cols[5].button("Open", key=f"open_{ticker}"):
            st.session_state["_jump_to"] = ticker
            st.rerun()
        if cols[6].button("🗑️", key=f"remove_{ticker}", help=f"Remove {stock['name']} from tracking"):
            raw = load_raw_all_stocks()
            raw.pop(ticker, None)
            save_all_stocks(raw)
            st.rerun()


# ───────────────────────── Detail page (fundamentals) ─────────────────────────

def page_detail(stock, ticker):
    if st.button("← Back to Summary"):
        st.session_state["_jump_to"] = None
        st.rerun()

    st.subheader(f"{stock['name']} ({ticker})")
    render_stat_row([
        ("Price", f"₹{fmt(stock.get('current_price'))}"),
        ("P/E", f"{fmt(stock.get('pe_ratio'), 1)}x"),
        ("Mkt Cap", f"₹{fmt(stock.get('market_cap_cr'))} Cr"),
        ("52W High", f"₹{fmt(stock.get('week52_high'))}"),
    ])

    session_id = get_session_id()
    if st.button(f"🔄 Refresh {ticker} now", disabled=not session_id):
        with st.spinner("Fetching…"):
            data, err = fetch_one(ticker, session_id)
        if err:
            st.error(err)
        else:
            raw = load_raw_all_stocks()
            raw[ticker] = data
            save_all_stocks(raw)
            st.success("Refreshed.")
            st.rerun()

    st.divider()
    st.caption("Fundamentals (Screener.in, consolidated) — annual Profit & Loss")

    n = len(stock["years"])
    col_widths = [2.2] + [1] * n

    def row(label, vals, digits=0, suffix="", colorize=False, bold=False):
        cols = st.columns(col_widths)
        cols[0].markdown(("**" + label + "**") if bold else label)
        for j, v in enumerate(vals):
            cols[1 + j].markdown(hist_cell_html(v, digits, suffix, colorize, bold), unsafe_allow_html=True)

    hdr = st.columns(col_widths)
    hdr[0].markdown("**Financial Year**")
    for j, y in enumerate(stock["years"]):
        hdr[1 + j].markdown(f"<div style='text-align:right;color:var(--vl-faint);font-size:11px;'>{y}</div>",
                             unsafe_allow_html=True)

    row("Revenue Cr", stock["revenue"], bold=True)
    row("Revenue Growth %", stock["revenue_growth_pct"], 1, "%", colorize=True)
    row("Expenses Cr", stock["expenses"])
    row("Operating Profit Cr", stock["operating_profit"], bold=True)
    row("OPM %", stock["opm_pct"], 1, "%", colorize=True)
    row("Other Income Cr", stock["other_income"])
    row("Interest Expense Cr", stock["interest"])
    row("Depreciation Cr", stock["depreciation"])
    row("PBT Cr", stock["pbt"], bold=True)
    row("Tax %", stock["tax_pct"], 1, "%")
    row("PAT Cr", stock["net_profit"], bold=True)
    row("PAT Growth %", stock["pat_growth_pct"], 1, "%", colorize=True)
    row("Number of Shares Cr", stock["shares_cr"], 3)
    row("EPS ₹", stock["eps"], 2, bold=True)


# ───────────────────────── Retrieve ─────────────────────────

def section_retrieve(all_stocks):
    st.divider()
    st.subheader("📥 Retrieve from Screener")

    session_id = get_session_id()
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
            # different symbol/code than what was typed, and the storage
            # key needs to be the real symbol for later refreshes to work.
            raw = load_raw_all_stocks()
            raw[data["ticker"]] = data
            save_all_stocks(raw)
            st.success(f"Retrieved **{data['name']}** ({data['ticker']}) — price ₹{fmt(data['current_price'])}, "
                       f"PE {fmt(data['pe_ratio'], 1)}x.")
            st.session_state["_jump_to"] = data["ticker"]
            st.rerun()
    elif submitted:
        st.warning("Type a ticker or company name first.")


# ───────────────────────── Main ─────────────────────────

def main():
    inject_css()
    st.title("🧮 Valuation Ledger")

    all_stocks = load_all_stocks()
    jump_to = st.session_state.get("_jump_to")

    if jump_to and jump_to in all_stocks:
        page_detail(all_stocks[jump_to], jump_to)
        return

    st.caption("Summary — retrieve companies live from Screener.in")
    page_summary(all_stocks)
    section_retrieve(all_stocks)


if __name__ == "__main__":
    main()
