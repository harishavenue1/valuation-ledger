#!/usr/bin/env python3
"""
Self-contained Screener.in fetch logic — no dependency on any other
local skill/script, so this repo runs standalone (local machine,
another machine, or Streamlit Community Cloud via GitHub).

Ported from ~/.claude/skills/ValuationTool/scripts/fetch_valuation_data.py
(2026-08-13/14 build) — same fetch/parse logic, same bug fixes (TTM
column stripping, standalone-fallback for companies with no real
consolidated financials). Kept in sync by hand; if you fix a bug in one,
fix it in the other or retire one in favor of the other.

Quarterly Results support (2026-08-16, "can we also pull in quarterly
results") ported the date-matched YoY logic from
~/.claude/skills/SectorLeaderCompare/scripts/compare_to_leader.py
instead — annual columns are evenly spaced so fetch_one()'s own
yoy_series() (fixed -1 index) is fine for those, but the quarterly table
can have gaps (a company skipping/delaying a quarter), where a fixed
-4 offset silently pairs the wrong periods (confirmed there on Krishna
Defence & Allied Industries, missing a Jun-2025 column entirely).
"""
import re, time
from datetime import datetime
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


def fetch_price_series(company_id, headers, days):
    """Daily-close (date, price) pairs from Screener's own internal chart
    API — not a public/documented endpoint, found by inspecting the
    company page's own chart widget (data-company-id + a
    /api/company/<id>/chart/?q=Price&days=N request), but it's the only
    source that has price history for EVERY ticker this app supports,
    including SME/small-caps (verified: yfinance has zero coverage for
    Yash Highvoltage/544310, ruled out for that reason 2026-08-16).
    Trade-off accepted along with that choice: this only returns daily
    Close, not Open/High/Low, so EMAs computed from it are Close-based,
    not the OHLC4 convention used elsewhere in this user's tools.

    Screener silently changes granularity based on `days` — empirically
    daily up to ~400-ish, auto-downsampled to ~weekly (7-day gaps) once
    you ask for a long enough range. See DAILY_DAYS_FOR_EMA /
    WEEKLY_DAYS_FOR_EMA below for the specific values this app relies on
    to get each granularity — if Screener ever changes that threshold,
    the daily fetch silently becoming weekly (or vice versa) would skew
    every EMA quietly, so re-verify the gap pattern here if EMA values
    ever look implausible.

    Up to 2 retries with increasing backoff on failure (2026-08-16, "EMA
    distance missing for many companies", then "2 companies still not
    retrieved" after a first pass at just 1 retry wasn't quite enough)
    — this endpoint is hit twice per company
    (daily + weekly) with zero pacing between companies in
    refresh_all_stocks()'s bulk loop, and confirmed live: several
    companies came back with real quarterly data (parsed from the same
    already-fetched page, no extra request) but null EMAs (this separate
    chart-API call specifically failing) after a "Refresh all now",
    while a standalone re-fetch of the exact same tickers moments later
    succeeded — consistent with transient rate-limiting/timeouts under
    that rapid back-to-back load, not a real per-company data problem."""
    # 3 attempts, not 2 (2026-08-16, "2 companies still not retrieved" —
    # still failing occasionally for a couple of tickers with just one
    # retry; upped to two, with increasing backoff, and paired with the
    # inter-chart-call delay added in fetch_price_emas()).
    backoffs = [1.5, 3.0]
    for attempt in range(3):
        try:
            r = requests.get(f"https://www.screener.in/api/company/{company_id}/chart/",
                              params={"q": "Price", "days": days}, headers=headers, timeout=15)
        except requests.RequestException:
            r = None
        if r is not None and r.status_code == 200:
            try:
                raw_values = r.json()["datasets"][0]["values"]
            except (KeyError, IndexError, ValueError, TypeError):
                raw_values = None
            if raw_values:
                out = []
                for d, v in raw_values:
                    p = parse_number(v)
                    if p is not None:
                        out.append((d, p))
                return out
        if attempt < len(backoffs):
            time.sleep(backoffs[attempt])
    return []


def ema(values, period):
    """Standard EMA (seeded with the SMA of the first `period` values,
    then iterated forward), computed over whatever history is actually
    available even when that's less than `period` points — common for
    recently-listed stocks. Converges less precisely with thin history,
    but a rougher estimate beats refusing to show anything. Returns the
    EMA as of the LAST point in `values` (i.e. "today"), or None if
    `values` is empty."""
    if not values:
        return None
    n = min(period, len(values))
    e = sum(values[:n]) / n
    alpha = 2 / (period + 1)
    for v in values[n:]:
        e = v * alpha + e * (1 - alpha)
    return e


DAILY_DAYS_FOR_EMA = 400    # confirmed daily granularity (~271 trading-day points)
WEEKLY_DAYS_FOR_EMA = 3000  # confirmed weekly granularity (auto-downsampled by Screener)


def fetch_price_emas(company_id, headers):
    """20-day / 50-day EMA from daily closes, 33-week EMA from weekly
    closes — see fetch_price_series()'s docstring for the Close-only /
    SME-coverage trade-off this relies on.

    Small delay between the two chart-API calls (2026-08-16, "2
    companies still not retrieved") — these were still coming back None
    for a couple of companies even with fetch_price_series()'s own
    retry and refresh_all_stocks()'s inter-ticker pacing, confirmed live
    (standalone single-company calls for the exact same tickers
    succeeded instantly right after). The two chart requests for one
    company fired back-to-back with zero gap between THEM specifically
    (only between different companies, before this) was the remaining
    gap in the pacing — this closes it."""
    daily_closes = [p for _, p in fetch_price_series(company_id, headers, DAILY_DAYS_FOR_EMA)]
    time.sleep(0.5)
    weekly_closes = [p for _, p in fetch_price_series(company_id, headers, WEEKLY_DAYS_FOR_EMA)]
    return {
        "ema20d": ema(daily_closes, 20),
        "ema50d": ema(daily_closes, 50),
        "ema33w": ema(weekly_closes, 33),
    }


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


def parse_period_label(label):
    label = label.strip()
    for fmt in ("%b %Y", "%B %Y"):
        try:
            return datetime.strptime(label, fmt)
        except ValueError:
            continue
    return None


def months_between(later, earlier):
    return (later.year - earlier.year) * 12 + (later.month - earlier.month)


def find_year_ago_index(period_dates, idx, tolerance=1):
    """Index of the column ~12 months before period_dates[idx], matched by
    actual calendar date rather than a fixed -4 position offset — see this
    module's docstring for why (Screener's quarterly table can have gaps,
    e.g. a skipped/delayed quarter)."""
    target = period_dates[idx]
    if target is None:
        return None
    best_idx, best_diff = None, None
    for i, d in enumerate(period_dates):
        if d is None or i == idx:
            continue
        diff = abs(months_between(target, d) - 12)
        if diff <= tolerance and (best_diff is None or diff < best_diff):
            best_idx, best_diff = i, diff
    return best_idx


def yoy_series_by_date(vals, period_dates):
    """Per-column YoY % growth matched by calendar date (find_year_ago_index),
    not a fixed -4 index offset. None wherever there's no value or no
    matching prior-year column (a gap, or simply not enough history yet
    for that position)."""
    out = []
    for i, cur in enumerate(vals):
        prev_idx = find_year_ago_index(period_dates, i)
        prev = vals[prev_idx] if prev_idx is not None else None
        if cur is None or prev is None or prev == 0:
            out.append(None)
        else:
            out.append(round((cur - prev) / abs(prev) * 100, 1))
    return out


def parse_quarterly_section(soup):
    """Screener's "Quarterly Results" section (or "Half-Yearly Results"
    for some SME-listed companies that report semi-annually — kept
    generic here since callers only care about the row data/dates, not
    the reporting cadence itself). Same table shape as parse_pl_section()'s
    annual Profit & Loss, just period-labeled columns, plus the parsed
    calendar dates per column for yoy_series_by_date() to match on.
    Returns (labels, rows, period_dates) or (None, None, None)."""
    section = next((s for s in soup.find_all("section")
                     if s.find("h2") and ("Quarterly" in s.find("h2").get_text(strip=True)
                                           or "Half" in s.find("h2").get_text(strip=True))), None)
    if not section:
        return None, None, None
    table = section.find("table")
    if not table:
        return None, None, None
    labels = [th.get_text(strip=True) for th in table.find("thead").find_all("th")][1:]
    period_dates = [parse_period_label(lbl) for lbl in labels]
    rows = {}
    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        label = cells[0].get_text(strip=True).rstrip("+").strip()
        vals = [parse_number(td.get_text(strip=True)) for td in cells[1:]]
        rows[label] = vals
    return labels, rows, period_dates


def fetch_one(ticker, session_id=None):
    """Returns (data_dict, None) on success or (None, error_message) on
    failure. data_dict["ticker"] is the *resolved canonical* symbol,
    which can differ from the `ticker` argument if you passed free text
    (e.g. "Yash Highvoltage Ltd" resolves to BSE code "544310") —
    always use data["ticker"] as your storage key, not the input.

    session_id is optional (verified 2026-08-15): Screener's search API,
    top-ratios, and full multi-year P&L table all return complete data
    to a fully anonymous request — confirmed against both a large-cap
    (Titan, 12yr consolidated) and a micro-cap SME (Yash Highvoltage). No
    known feature this app uses is gated behind login. Kept as an
    optional param (unused when None/falsy) rather than removed
    outright, in case a future Screener change or an as-yet-untested
    company/data type turns out to need it.

    Consolidated is now kept as-is whenever it exists at all, however few
    years (2026-08-16 request: "use consolidated results always") —
    standalone is only substituted when consolidated is completely absent
    (see the not-pl_years check below). Yash Highvoltage is the concrete
    example of the trade-off this accepts: it only has 2 years of
    consolidated P&L (Mar 2025-2026) vs. 7 years standalone, so its
    consolidated CAGR/trend work is now thinner than it used to be —
    correct per this request, but worth knowing if a specific company's
    numbers look sparse."""
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
    if consolidated and not pl_years:
        # /consolidated/ returning HTTP 200 doesn't guarantee it actually
        # HAS consolidated financials — some companies only ever publish
        # standalone figures, and Screener still serves the /consolidated/
        # URL with a structurally empty P&L table (blank header, no year
        # columns) rather than a 404. This is the ONLY case that falls
        # back to standalone now (2026-08-16 request: "use consolidated
        # results always" — this used to also swap in standalone whenever
        # it simply had MORE years than a short-but-real consolidated
        # history; that preference is gone, consolidated is kept as-is
        # whenever it exists at all, however few years). Without this one
        # remaining fallback, a company with zero consolidated data would
        # fail to fetch entirely instead of degrading to standalone.
        r = requests.get(f"https://www.screener.in{base_url}/", headers=headers, timeout=15)
        if r.status_code == 200:
            standalone_soup = BeautifulSoup(r.text, "html.parser")
            standalone_years, standalone_rows = parse_pl_section(standalone_soup)
            if standalone_years:
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

    # EMAs are a best-effort add-on, not core to what this function has
    # always returned (price/P&L) — a hiccup fetching/parsing them
    # (network blip, Screener's chart API shape changing) degrades to
    # None fields rather than failing the whole company fetch.
    try:
        emas = fetch_price_emas(company_id, headers)
    except Exception:
        emas = {"ema20d": None, "ema50d": None, "ema33w": None}

    # Quarterly Results — same best-effort-add-on treatment as EMAs: a
    # missing/unparseable section (or a company that simply doesn't have
    # one) degrades to an empty "quarters" list rather than failing the
    # whole fetch, since this app's core has never depended on it.
    # 2026-08-16 request: "can we also pull in quarterly results".
    quarters, q_data = [], {}
    try:
        q_labels, q_rows, q_dates = parse_quarterly_section(soup)
        if q_labels and q_rows:
            nq = len(q_labels)

            def qpad(vals):
                if vals is None:
                    return [None] * nq
                return (vals + [None] * nq)[:nq]

            q_revenue = qpad(find_row(q_rows, "sales") or find_row(q_rows, "revenue"))
            q_expenses = qpad(find_row(q_rows, "expenses"))
            q_op_profit = qpad(find_row(q_rows, "operating profit"))
            q_opm = qpad(find_row(q_rows, "opm"))
            q_other_income = qpad(find_row(q_rows, "other income"))
            q_interest = qpad(find_row(q_rows, "interest"))
            q_depreciation = qpad(find_row(q_rows, "depreciation"))
            q_pbt = qpad(find_row(q_rows, "profit before tax"))
            q_tax_pct = qpad(find_row(q_rows, "tax %"))
            q_net_profit = qpad(find_row(q_rows, "net profit"))
            q_eps = qpad(find_row(q_rows, "eps"))
            # YoY (not QoQ) — computed over the FULL fetched history before
            # trimming below, so the earliest kept quarter still gets a
            # real figure instead of losing it to truncation; date-matched
            # (see module docstring), not a fixed -4 offset.
            q_revenue_growth_pct = yoy_series_by_date(q_revenue, q_dates)
            q_pat_growth_pct = yoy_series_by_date(q_net_profit, q_dates)

            # Last N only (2026-08-16 request: "last 8 quarters") —
            # trimmed after growth% above, not before.
            LAST_N_QUARTERS = 8

            def qtrim(vals):
                return vals[-LAST_N_QUARTERS:]

            quarters = qtrim(q_labels)
            q_data = {
                "q_revenue": qtrim(q_revenue), "q_revenue_growth_pct": qtrim(q_revenue_growth_pct),
                "q_expenses": qtrim(q_expenses), "q_operating_profit": qtrim(q_op_profit),
                "q_opm_pct": qtrim(q_opm), "q_other_income": qtrim(q_other_income),
                "q_interest": qtrim(q_interest), "q_depreciation": qtrim(q_depreciation),
                "q_pbt": qtrim(q_pbt), "q_tax_pct": qtrim(q_tax_pct),
                "q_net_profit": qtrim(q_net_profit), "q_pat_growth_pct": qtrim(q_pat_growth_pct),
                "q_eps": qtrim(q_eps),
            }
    except Exception:
        quarters, q_data = [], {}

    return {
        "ticker": canonical_ticker,
        "name": company_name,
        "base_url": base_url,
        "consolidated": consolidated,
        "current_price": top["current_price"],
        "pe_ratio": top["pe_ratio"],
        "market_cap_cr": top["market_cap_cr"],
        "week52_high": top["week52_high"],
        "ema20d": emas["ema20d"],
        "ema50d": emas["ema50d"],
        "ema33w": emas["ema33w"],
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
        "quarters": quarters,
        **q_data,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, None


def fetch_price_only(ticker, session_id=None):
    """Lightweight sibling of fetch_one() — current_price/pe_ratio/
    market_cap_cr/week52_high only, no P&L/quarterly/EMA. 2026-08-16
    request: "2 refresh for fundamental data & prices alone, as prices
    do need daily updates" — fundamentals (annual P&L, Quarterly
    Results) don't change day to day so refreshing them daily is wasted
    load, but EMA specifically is the expensive/fragile part (2 extra
    chart-API requests per company, already the confirmed source of
    transient failures under bulk load — see fetch_price_series()'s
    retry). Skipping it here roughly halves the request count per
    company (resolve + page, vs. resolve + page + 2x chart), so a daily
    all-tickers price refresh is both faster and lower-risk than running
    the full fetch_one() for every ticker every day.

    Same return shape as fetch_one() — (data_dict, None) or
    (None, error) — but data_dict only has ticker/name/current_price/
    pe_ratio/market_cap_cr/week52_high (+ fetched_at). Caller is
    responsible for merging this into an existing stored record rather
    than treating it as a full replacement (it deliberately has none of
    fetch_one()'s other fields)."""
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

    canonical_ticker = base_url.rstrip("/").split("/")[-1]
    return {
        "ticker": canonical_ticker,
        "name": company_name,
        "current_price": top["current_price"],
        "pe_ratio": top["pe_ratio"],
        "market_cap_cr": top["market_cap_cr"],
        "week52_high": top["week52_high"],
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, None
