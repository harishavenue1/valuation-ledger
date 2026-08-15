#!/usr/bin/env python3
"""
Self-contained Screener.in fetch logic — no dependency on any other
local skill/script, so this repo runs standalone (local machine,
another machine, or Streamlit Community Cloud via GitHub).

Ported from ~/.claude/skills/ValuationTool/scripts/fetch_valuation_data.py
(2026-08-13/14 build) — same fetch/parse logic, same bug fixes (TTM
column stripping, standalone-fallback for companies with no real
consolidated financials, date-matched YoY unused here since this only
needs annual columns which are evenly spaced). Kept in sync by hand;
if you fix a bug in one, fix it in the other or retire one in favor of
the other.
"""
import re, time
import requests
from bs4 import BeautifulSoup

HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://www.screener.in/",
}


def parse_number(s):
    if not s:
        return None
    s = str(s).strip().replace(",", "").replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


def resolve_url(ticker, headers):
    r = requests.get("https://www.screener.in/api/company/search/",
                      params={"q": ticker}, headers=headers, timeout=15)
    if r.status_code != 200:
        return None
    results = r.json()
    if not results:
        return None
    tkr_low = ticker.lower()
    exact = next((x for x in results
                  if x["url"].strip("/").split("/")[1].lower() == tkr_low), None)
    chosen = exact or results[0]
    base = chosen["url"].rstrip("/")
    if base.endswith("/consolidated"):
        base = base[: -len("/consolidated")]
    return base, chosen["name"], chosen["id"]


def fetch_page(base_url, headers):
    r = requests.get(f"https://www.screener.in{base_url}/consolidated/", headers=headers, timeout=15)
    if r.status_code == 200:
        return r.text, True
    r = requests.get(f"https://www.screener.in{base_url}/", headers=headers, timeout=15)
    if r.status_code == 200:
        return r.text, False
    return None, None


def parse_top_ratios(soup):
    out = {"current_price": None, "pe_ratio": None, "market_cap_cr": None, "week52_high": None}
    top_ratios = soup.find("ul", id="top-ratios")
    if not top_ratios:
        return out
    for li in top_ratios.find_all("li"):
        label = li.get_text(" ", strip=True)
        nums = [parse_number(s.get_text(strip=True)) for s in li.find_all("span", class_="number")]
        nums = [n for n in nums if n is not None]
        if "Current Price" in label and nums:
            out["current_price"] = nums[0]
        elif "Stock P/E" in label and nums:
            out["pe_ratio"] = nums[0]
        elif "Market Cap" in label and nums:
            out["market_cap_cr"] = nums[0]
        elif "High" in label and "Low" in label and nums:
            out["week52_high"] = nums[0]
    return out


def parse_pl_section(soup):
    section = next((s for s in soup.find_all("section")
                     if s.find("h2") and "Profit" in s.find("h2").get_text(strip=True)), None)
    if not section:
        return None, None
    table = section.find("table")
    if not table:
        return None, None
    years = [th.get_text(strip=True) for th in table.find("thead").find_all("th")][1:]
    rows = {}
    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        label = cells[0].get_text(strip=True).rstrip("+").strip()
        vals = [parse_number(td.get_text(strip=True)) for td in cells[1:]]
        rows[label] = vals
    return years, rows


def find_row(rows, *keywords):
    for label, vals in rows.items():
        low = label.lower()
        if all(k in low for k in keywords):
            return vals
    return None


def yoy_series(vals):
    out = [None]
    for i in range(1, len(vals)):
        cur, prev = vals[i], vals[i - 1]
        if cur is None or prev is None or prev == 0:
            out.append(None)
            continue
        out.append(round((cur - prev) / abs(prev) * 100, 1))
    return out


def fetch_one(ticker, session_id=None):
    """Returns (data_dict, None) on success or (None, error_message) on
    failure. data_dict["ticker"] is the *resolved canonical* symbol,
    which can differ from the `ticker` argument if you passed free text
    (e.g. "Yash Highvoltage Ltd" resolves to BSE code "544310") —
    always use data["ticker"] as your storage key, not the input.

    session_id is optional (verified 2026-08-15): Screener's search API,
    top-ratios, and full multi-year P&L table all return complete data
    to a fully anonymous request — confirmed against both a large-cap
    (Titan, 12yr consolidated) and a micro-cap SME (Yash Highvoltage,
    7yr standalone fallback). No known feature this app uses is gated
    behind login. Kept as an optional param (unused when None/falsy)
    rather than removed outright, in case a future Screener change or
    an as-yet-untested company/data type turns out to need it."""
    headers = {**HEADERS_BASE}
    if session_id:
        headers["Cookie"] = f"sessionid={session_id}"
    resolved = resolve_url(ticker, headers)
    if not resolved:
        return None, f"could not resolve '{ticker}' via Screener's search API"
    base_url, company_name, company_id = resolved

    html, consolidated = fetch_page(base_url, headers)
    if html is None:
        return None, f"HTTP fetch failed for {base_url} (session cookie may be expired — check Settings)"
    soup = BeautifulSoup(html, "html.parser")

    top = parse_top_ratios(soup)
    pl_years, pl_rows = parse_pl_section(soup)
    MIN_USEFUL_YEARS = 5  # below this, multi-year trend/CAGR work is too thin to be useful
    if consolidated and len(pl_years or []) < MIN_USEFUL_YEARS:
        # /consolidated/ can return HTTP 200 with either a structurally
        # empty P&L table (blank header, no year columns — companies
        # that only publish standalone figures) or a short-but-nonempty
        # one (e.g. consolidation only recently started/became required,
        # so consolidated history is truncated even though the company
        # itself is old and standalone goes back much further — real
        # case: Yash Highvoltage, incorporated 2002, consolidated P&L
        # only from Mar 2025 while standalone has Mar 2020 onward).
        # Either way, more years of standalone beats fewer years of
        # consolidated for trend work, so prefer it whenever it's longer.
        r = requests.get(f"https://www.screener.in{base_url}/", headers=headers, timeout=15)
        if r.status_code == 200:
            standalone_soup = BeautifulSoup(r.text, "html.parser")
            standalone_years, standalone_rows = parse_pl_section(standalone_soup)
            if len(standalone_years or []) > len(pl_years or []):
                html, consolidated, soup = r.text, False, standalone_soup
                top = parse_top_ratios(soup)
                pl_years, pl_rows = standalone_years, standalone_rows
    if not pl_years:
        return None, "Profit & Loss section not found on this page (tried both consolidated and standalone)"

    revenue = find_row(pl_rows, "sales") or find_row(pl_rows, "revenue")
    expenses = find_row(pl_rows, "expenses")
    op_profit = find_row(pl_rows, "operating profit")
    opm = find_row(pl_rows, "opm")
    other_income = find_row(pl_rows, "other income")
    interest = find_row(pl_rows, "interest")
    depreciation = find_row(pl_rows, "depreciation")
    pbt = find_row(pl_rows, "profit before tax")
    tax_pct = find_row(pl_rows, "tax %")
    net_profit = find_row(pl_rows, "net profit")
    eps = find_row(pl_rows, "eps")

    if not (revenue and net_profit and eps):
        return None, "Sales/Net Profit/EPS rows not all found — Screener layout may differ for this ticker"

    n = len(pl_years)

    def pad(vals):
        if vals is None:
            return [None] * n
        return (vals + [None] * n)[:n]

    revenue, expenses, op_profit, opm = pad(revenue), pad(expenses), pad(op_profit), pad(opm)
    other_income, interest, depreciation = pad(other_income), pad(interest), pad(depreciation)
    pbt, tax_pct, net_profit, eps = pad(pbt), pad(tax_pct), pad(net_profit), pad(eps)

    shares_cr = []
    for npv, e in zip(net_profit, eps):
        if npv is None or e in (None, 0):
            shares_cr.append(None)
        else:
            shares_cr.append(round(npv / e, 3))

    canonical_ticker = base_url.rstrip("/").split("/")[-1]

    return {
        "ticker": canonical_ticker,
        "name": company_name,
        "base_url": base_url,
        "consolidated": consolidated,
        "current_price": top["current_price"],
        "pe_ratio": top["pe_ratio"],
        "market_cap_cr": top["market_cap_cr"],
        "week52_high": top["week52_high"],
        "years": pl_years,
        "revenue": revenue,
        "revenue_growth_pct": yoy_series(revenue),
        "expenses": expenses,
        "operating_profit": op_profit,
        "opm_pct": opm,
        "other_income": other_income,
        "interest": interest,
        "depreciation": depreciation,
        "pbt": pbt,
        "tax_pct": tax_pct,
        "net_profit": net_profit,
        "pat_growth_pct": yoy_series(net_profit),
        "eps": eps,
        "shares_cr": shares_cr,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, None
