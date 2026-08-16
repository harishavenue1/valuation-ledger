#!/usr/bin/env python3
"""
Valuation Ledger — self-contained Streamlit app.

Runs identically local or deployed (Streamlit Community Cloud via
GitHub): no absolute local paths, no import from any other project's
scripts (see screener_fetch.py, bundled alongside this file).

No secrets required to fetch data (verified 2026-08-15) — Screener's
search API, top-ratios, and full P&L table all work fully anonymously;
SCREENER_SESSION_ID is optional and currently unused by any fetch this
app makes (see get_session_id() / fetch_one()'s docstring). GITHUB_TOKEN
is the one secret worth setting once deployed — see the "GitHub-backed
sync" section below for why. Both come from st.secrets — Streamlit's
own mechanism, which transparently reads .streamlit/secrets.toml
locally and the dashboard-configured secrets when deployed, so the same
code path works in both places with zero branching.

Setup (local): none required to run. Optionally copy
.streamlit/secrets.toml.example to .streamlit/secrets.toml and fill in
GITHUB_TOKEN if you want scenario edits synced (git-ignored, never
commit the real value).

Setup (Streamlit Community Cloud): push this repo to GitHub, deploy at
share.streamlit.io, then optionally add GITHUB_TOKEN under the app's
Settings -> Secrets in the same TOML format.

Run locally:
    streamlit run app.py
"""
import hmac, json, os, re
from datetime import date

import streamlit as st

from screener_fetch import fetch_one

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
GUIDANCE_DIR = os.path.join(CACHE_DIR, "guidance")
SCENARIOS_PATH = os.path.join(CACHE_DIR, "scenarios.json")
ALL_STOCKS_PATH = os.path.join(CACHE_DIR, "all_stocks.json")
LAST_REFRESH_PATH = os.path.join(CACHE_DIR, "last_refresh.json")
GUIDANCE_TRACKER_PATH = os.path.join(CACHE_DIR, "guidance_tracker.json")
GUIDANCE_TAGS = ["", "Beat", "Neutral", "Miss"]
GUIDANCE_TAG_CLASS = {"Beat": "vl-tag-beat", "Neutral": "vl-tag-neutral", "Miss": "vl-tag-miss"}

FY_LABEL_RE = re.compile(r"^(Mar|Jun|Sep|Dec)\s+\d{4}$")
ARRAY_FIELDS = ["revenue", "revenue_growth_pct", "expenses", "operating_profit", "opm_pct",
                "other_income", "interest", "depreciation", "pbt", "tax_pct", "net_profit",
                "pat_growth_pct", "eps", "shares_cr"]

CASES = ["base", "bull", "bear", "mgmt"]
CASE_LABEL = {"base": "Base Case", "bull": "Bull Case", "bear": "Bear Case", "mgmt": "Management Case"}
CASE_COLOR = {"base": "#B7C0BB", "bull": "#63C46E", "bear": "#E3776A", "mgmt": "#C4A8E8"}
N_EST_YEARS = 3

# Management Case is dropped from the Future Projections grid and the
# Summary page table (explicit request, 2026-08-15) — it's never
# guidance-seeded anyway and stayed blank/unused in both most of the
# time. It's still fully available via each company's Detail-page chip
# row and Key Assumptions. Scenario data already saved for "mgmt" is
# left untouched — nothing here deletes it, this constant just controls
# which cases get columns/chips in these two views.
GRID_CASES = [c for c in CASES if c != "mgmt"]

st.set_page_config(page_title="Valuation Ledger", page_icon="🧮", layout="wide")


# ───────────────────────── Access gate (via st.secrets) ─────────────────────────

def check_password():
    """True if the app is unlocked for this browser session — either an
    APP_PASSWORD secret isn't configured (open access, same posture as
    before this existed — matches the optional-secret pattern used
    elsewhere in this file), or the visitor already entered it correctly
    this session, or they just did on this run.

    Why this exists (added 2026-08-16): every write in this app — Retrieve,
    Refresh, editing scenario inputs, Remove — runs server-side using the
    app's own embedded GITHUB_TOKEN. There's no per-visitor distinction
    otherwise: anyone who reaches the deployed URL can drive all of it,
    including deleting a tracked company, with zero trace of who did it.
    A public GitHub repo doesn't grant randoms write access on its own
    (see README) — but a public *app URL* with no gate functionally does,
    since the app acts as a privileged proxy for whoever's using it."""
    try:
        app_password = st.secrets["APP_PASSWORD"]
    except (KeyError, FileNotFoundError):
        return True

    if st.session_state.get("authenticated"):
        return True

    st.title("🧮 Valuation Ledger")
    pwd = st.text_input("Password", type="password", key="_pwd_input")
    if st.button("Unlock"):
        # Constant-time compare — trivial to add, avoids a timing side
        # channel on password length/prefix for what little it's worth.
        if hmac.compare_digest(pwd, app_password):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False


# ───────────────────────── Session cookie (via st.secrets) ─────────────────────────

def get_session_id():
    """Optional (verified 2026-08-15) — Screener's search API, top-ratios,
    and full P&L table all work fully anonymously; see fetch_one()'s
    docstring in screener_fetch.py. Returns None if not configured,
    which is the normal case now; callers no longer gate on this, they
    just pass it through to fetch_one() where it's a no-op when falsy."""
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


def _local_load_raw_all_stocks():
    if not os.path.exists(ALL_STOCKS_PATH):
        return {}
    with open(ALL_STOCKS_PATH) as f:
        return json.load(f)


def _local_save_raw_all_stocks(data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(ALL_STOCKS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def load_raw_all_stocks():
    """Unclean (TTM column, if any, intact) — only save_all_stocks()
    should write this shape back out, so a re-clean on next load stays
    idempotent regardless of how many times a stock gets refreshed.
    GitHub-synced the same way as scenarios (see _github_synced_load) —
    company/price data is "all the data" too, not just scenario edits,
    so it needs to survive a redeploy and match across devices the same
    way."""
    cfg = get_github_config()
    path = cfg["stocks_path"] if cfg else "data/all_stocks.json"
    return _github_synced_load("all_stocks_raw_cache", "all_stocks_sha", path,
                                _local_load_raw_all_stocks, _local_save_raw_all_stocks, "company data")


def save_all_stocks(data):
    cfg = get_github_config()
    path = cfg["stocks_path"] if cfg else "data/all_stocks.json"
    _github_synced_save(data, "all_stocks_raw_cache", "all_stocks_sha", path,
                         _local_save_raw_all_stocks, "company data")


def load_all_stocks():
    raw = load_raw_all_stocks()
    return {ticker: clean_stock(v) for ticker, v in raw.items()}


def _local_load_last_refresh():
    if not os.path.exists(LAST_REFRESH_PATH):
        return {}
    with open(LAST_REFRESH_PATH) as f:
        return json.load(f)


def _local_save_last_refresh(data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(LAST_REFRESH_PATH, "w") as f:
        json.dump(data, f, indent=2)


def load_last_refresh():
    """{"date": "YYYY-MM-DD"} of the last successful auto-refresh — kept
    in GitHub too (not just local) so a visit from any device marks the
    day done for every device, not just the one that happened to open
    first."""
    return _github_synced_load("last_refresh_cache", "last_refresh_sha", "data/last_refresh.json",
                                _local_load_last_refresh, _local_save_last_refresh, "refresh timestamp")


def save_last_refresh(data):
    _github_synced_save(data, "last_refresh_cache", "last_refresh_sha", "data/last_refresh.json",
                         _local_save_last_refresh, "refresh timestamp")


def load_guidance(ticker):
    p = os.path.join(GUIDANCE_DIR, f"{ticker}.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None


def _local_load_scenarios():
    if not os.path.exists(SCENARIOS_PATH):
        return {}
    with open(SCENARIOS_PATH) as f:
        return json.load(f)


def _local_save_scenarios(data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(SCENARIOS_PATH, "w") as f:
        json.dump(data, f, indent=2)


# ── GitHub-backed sync (so scenario edits survive a cloud redeploy and
# are the same regardless of which computer opens the app) ──
#
# cache/ is git-ignored (personal, regenerable-ish local cache) so it
# does NOT survive a Streamlit Community Cloud redeploy on its own. To
# make scenario edits durable and consistent across devices, they're
# also mirrored to a real tracked file (GITHUB_DATA_PATH, default
# "data/scenarios.json") in this repo via the GitHub Contents API,
# authenticated with a Personal Access Token in st.secrets. The local
# file stays as a fast read + offline-safe fallback: every save writes
# both; every load prefers GitHub when configured, and falls back to
# local (with a one-time warning) if the API call fails for any reason
# (no token configured, bad token, network hiccup, rate limit).
#
# Cached in st.session_state for the lifetime of the browser session so
# a Streamlit rerun (fires on every widget interaction) doesn't refetch
# from the API each time — only load_scenarios()'s first call per
# session hits the network; every save updates the cache + the stored
# sha (GitHub requires the current blob sha to update a file).

import base64
import requests


def get_github_config():
    """None if GITHUB_TOKEN isn't set — callers fall back to local-only."""
    try:
        token = st.secrets["GITHUB_TOKEN"]
    except (KeyError, FileNotFoundError):
        return None
    repo = st.secrets.get("GITHUB_REPO", "harishavenue1/valuation-ledger")
    return {
        "token": token,
        "repo": repo,
        "scenarios_path": st.secrets.get("GITHUB_DATA_PATH", "data/scenarios.json"),
        "stocks_path": st.secrets.get("GITHUB_STOCKS_PATH", "data/all_stocks.json"),
        "guidance_tracker_path": st.secrets.get("GITHUB_GUIDANCE_TRACKER_PATH", "data/guidance_tracker.json"),
    }


def _github_headers(token):
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


def github_fetch_json(cfg, path):
    """Returns (data, sha). sha is None both when the file doesn't exist
    yet (first-ever save will create it) and when the fetch failed
    outright — callers distinguish those via the raised/caught error
    happening at the call site, not here."""
    url = f"https://api.github.com/repos/{cfg['repo']}/contents/{path}"
    resp = requests.get(url, headers=_github_headers(cfg["token"]), timeout=10)
    if resp.status_code == 404:
        return {}, None
    resp.raise_for_status()
    body = resp.json()
    content = base64.b64decode(body["content"]).decode("utf-8")
    return json.loads(content), body["sha"]


def github_put_json(cfg, path, data, sha, message):
    """Returns the new sha. Raises on failure — caller decides how to
    surface that (local save already succeeded by the time this runs,
    so a failure here never loses data, just skips the cross-device
    sync for this edit)."""
    url = f"https://api.github.com/repos/{cfg['repo']}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(json.dumps(data, indent=2).encode("utf-8")).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    resp = requests.put(url, headers=_github_headers(cfg["token"]), json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()["content"]["sha"]


def _github_synced_load(cache_key, sha_key, github_path, local_loader, local_saver, label):
    """Shared load pattern for any GitHub-synced JSON store: session-cached,
    GitHub-preferred, local-file fallback with a one-time warning."""
    if cache_key in st.session_state:
        return st.session_state[cache_key]

    cfg = get_github_config()
    if cfg:
        try:
            data, sha = github_fetch_json(cfg, github_path)
            st.session_state[sha_key] = sha
            st.session_state[cache_key] = data
            local_saver(data)  # keep local mirror fresh as offline fallback
            return data
        except Exception as e:
            st.warning(f"⚠️ Couldn't reach GitHub for {label} sync ({e}) — using local cache. "
                       "Edits will still save locally but won't sync until the next successful load.")

    data = local_loader()
    st.session_state[cache_key] = data
    st.session_state[sha_key] = None
    return data


def _github_synced_save(data, cache_key, sha_key, github_path, local_saver, label, max_attempts=4):
    """Shared save pattern: local write always succeeds first (never lose
    data), then best-effort push to GitHub — retried with a freshly
    fetched sha up to max_attempts times, not just once.

    Why more than one retry: Streamlit fires one script rerun per widget
    commit, and typing through several driver fields in a row (growth%,
    OPM%, tax%, PE, ...) can genuinely produce reruns close enough
    together that more than one save is in flight against the same sha
    — observed live (2026-08-16): a single retry still hit a second 409.
    Each loop iteration re-fetches the current sha immediately before
    retrying, so it keeps re-aiming at whatever the latest remote state
    actually is rather than replaying the same stale guess.

    Not data-lossy on this device even if every attempt fails: every
    save writes the FULL current dict (see set_case_state()), so the
    very next successful save — for this field or any other — carries
    this one's change forward too. The real residual risk is narrower:
    a redeploy, or another device reading GitHub, in the gap before
    that next successful save."""
    local_saver(data)
    st.session_state[cache_key] = data

    cfg = get_github_config()
    if not cfg:
        return
    message = f"Update {os.path.basename(github_path)} via Valuation Ledger app"
    sha = st.session_state.get(sha_key)
    last_err = None
    for attempt in range(max_attempts):
        try:
            new_sha = github_put_json(cfg, github_path, data, sha, message)
            st.session_state[sha_key] = new_sha
            return
        except Exception as e:
            last_err = e
            try:
                _, sha = github_fetch_json(cfg, github_path)
            except Exception:
                break  # can't even read now (network down) — no point retrying further

    st.warning(f"⚠️ Saved locally, but GitHub sync of {label} failed after {max_attempts} attempts "
               f"({last_err}). This device's copy is safe; the next successful save will carry this "
               "change forward too — but until then, a redeploy or another device reading GitHub may miss it.")


def load_scenarios():
    cfg = get_github_config()
    path = cfg["scenarios_path"] if cfg else "data/scenarios.json"
    return _github_synced_load("scenarios_cache", "scenarios_sha", path,
                                _local_load_scenarios, _local_save_scenarios, "scenarios")


def save_scenarios(data):
    cfg = get_github_config()
    path = cfg["scenarios_path"] if cfg else "data/scenarios.json"
    _github_synced_save(data, "scenarios_cache", "scenarios_sha", path, _local_save_scenarios, "scenarios")


def _local_load_guidance_tracker():
    if not os.path.exists(GUIDANCE_TRACKER_PATH):
        return {"quarters": [], "tracked": [], "cells": {}}
    with open(GUIDANCE_TRACKER_PATH) as f:
        return json.load(f)


def _local_save_guidance_tracker(data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(GUIDANCE_TRACKER_PATH, "w") as f:
        json.dump(data, f, indent=2)


def load_guidance_tracker():
    """{"quarters": [labels...], "tracked": [tickers...], "cells": {ticker:
    {quarter_label: {"note": str, "tag": ""|"Beat"|"Neutral"|"Miss"}}}}.
    "tracked" is this page's OWN row list — deliberately separate from
    all_stocks.json's full company set (explicit request, 2026-08-16:
    "in this page we can row wise add more companies", read as this
    page having its own add/remove independent of the Summary page's
    list) — though adding a company here still fetches into the shared
    all_stocks.json via the same fetch_one()/save_all_stocks() path, so
    price data isn't duplicated, only tracked-here membership is."""
    cfg = get_github_config()
    path = cfg["guidance_tracker_path"] if cfg else "data/guidance_tracker.json"
    return _github_synced_load("guidance_tracker_cache", "guidance_tracker_sha", path,
                                _local_load_guidance_tracker, _local_save_guidance_tracker, "guidance tracker")


def save_guidance_tracker(data):
    cfg = get_github_config()
    path = cfg["guidance_tracker_path"] if cfg else "data/guidance_tracker.json"
    _github_synced_save(data, "guidance_tracker_cache", "guidance_tracker_sha", path,
                         _local_save_guidance_tracker, "guidance tracker")


def last_actual(arr):
    """Walk backward for the first non-null value — defensive against a
    company whose latest FY had a one-off null (e.g. a loss year with no
    meaningful tax %)."""
    for v in reversed(arr or []):
        if v is not None:
            return v
    return None


def default_case_state(stock, ticker, case):
    """Revenue Growth % seeds from cache/guidance/<TICKER>.json when
    present (Base=range midpoint, Bull=upper, Bear=lower — never for
    Management Case, which stays open for the user's own "if management
    is right" modelling). OPM%/Tax% always carry forward the last actual
    value regardless of guidance — computeModel() needs both non-null
    before it can produce PBT/PAT/EPS at all."""
    guidance = load_guidance(ticker)
    guided_growth = None
    if guidance and case != "mgmt":
        guided_growth = guidance.get("revenue_growth", {}).get(case)
    drivers = []
    for _ in range(N_EST_YEARS):
        drivers.append({
            "revGrowth": guided_growth,
            "opm": last_actual(stock.get("opm_pct")),
            "tax": last_actual(stock.get("tax_pct")),
            "other_income": last_actual(stock.get("other_income")),
            "interest": last_actual(stock.get("interest")),
            "depreciation": last_actual(stock.get("depreciation")),
            "shares": last_actual(stock.get("shares_cr")),
            "pe": None,
        })
    assumptions = ""
    if guided_growth is not None and guidance and guidance.get("source_text"):
        which = ("guidance range midpoint" if case == "base"
                 else "guidance range upper end" if case == "bull" else "guidance range lower end")
        assumptions = (
            f"[Auto-filled from management guidance research]\n\n"
            f"Revenue Growth % ({CASE_LABEL[case]}): {guided_growth}% — {which}.\n\n"
            f"Guidance: {guidance['source_text']}\n\n"
            f"Confidence: {guidance.get('confidence', '')}\n\n"
            f"Sources: {', '.join(guidance.get('source_urls', []))}\n\n"
            f"As of: {guidance.get('as_of', '')}"
        )
    return {"drivers": drivers, "assumptions": assumptions}


def get_case_state(scenarios, stock, ticker, case):
    saved = scenarios.get(ticker, {}).get(case)
    if saved and "drivers" in saved:
        return saved
    return default_case_state(stock, ticker, case)


def set_case_state(scenarios, ticker, case, state):
    scenarios.setdefault(ticker, {})[case] = state
    save_scenarios(scenarios)


# ───────────────────────── Compute engine ─────────────────────────
# Mirrors the Artifact's computeModel()/headlineCagr() exactly — same
# formulas, same sequencing. Keep in sync if either changes.

def compute_model(stock, state):
    revenue_prev = stock["revenue"][-1]
    pat_prev = stock["net_profit"][-1]
    rows = []
    for dr in state["drivers"]:
        revenue = (revenue_prev * (1 + dr["revGrowth"] / 100)
                   if dr.get("revGrowth") is not None and revenue_prev is not None else None)
        op = revenue * dr["opm"] / 100 if revenue is not None and dr.get("opm") is not None else None
        expenses = revenue - op if revenue is not None and op is not None else None
        oi = dr.get("other_income") or 0
        interest = dr.get("interest") or 0
        dep = dr.get("depreciation") or 0
        pbt = (op + oi - interest - dep) if op is not None else None
        pat = pbt * (1 - dr["tax"] / 100) if pbt is not None and dr.get("tax") is not None else None
        pat_growth = ((pat - pat_prev) / abs(pat_prev) * 100
                      if pat is not None and pat_prev not in (None, 0) else None)
        shares = dr.get("shares") or stock["shares_cr"][-1]
        eps = pat / shares if pat is not None and shares else None
        fwd_pe = (stock["current_price"] / eps
                  if eps not in (None, 0) and stock.get("current_price") else None)
        rows.append(dict(revenue=revenue, operating_profit=op, expenses=expenses, other_income=oi,
                          interest=interest, depreciation=dep, pbt=pbt, pat=pat, pat_growth=pat_growth,
                          shares=shares, eps=eps, forward_pe=fwd_pe))
        revenue_prev = revenue if revenue is not None else revenue_prev
        pat_prev = pat if pat is not None else pat_prev
    return rows


def days_until(year):
    return (date(year, 3, 31) - date.today()).days


def cagr_for(current_price, share_price, days):
    if not current_price or not share_price or share_price <= 0 or days is None or days <= 0:
        return None
    return (pow(share_price / current_price, 365 / days) - 1) * 100


def headline_cagr(stock, state):
    """The chip/summary-column CAGR — nearest estimate year that has a PE
    Multiple filled in (walk forward, not backward: a user modelling only
    FY2027 expects the Base Case chip to show FY2027, not silently skip
    ahead to FY2029 just because it happens to be checked first)."""
    model = compute_model(stock, state)
    last_year = int(stock["years"][-1].split(" ")[1])
    for i in range(N_EST_YEARS):
        dr = state["drivers"][i]
        eps = model[i]["eps"]
        if dr.get("pe") and eps is not None:
            year = last_year + i + 1
            share_price = eps * dr["pe"]
            cagr = cagr_for(stock["current_price"], share_price, days_until(year))
            # growth is never None here: eps only computes (above) when
            # revGrowth was set, since revenue — and everything downstream
            # of it, including eps — depends on it in compute_model().
            return dict(cagr=cagr, share_price=share_price, year=year,
                        growth=dr.get("revGrowth"), pe=dr["pe"])
    return None


# ───────────────────────── Format helpers ─────────────────────────

def as_float(x):
    """st.number_input requires value/step/min/max to all share one numeric
    type — mixing an int (e.g. a guidance JSON's plain `35`) with a float
    step (e.g. 0.5) raises StreamlitMixedNumericTypesError."""
    return float(x) if x is not None else None


def fmt(v, digits=0, suffix=""):
    if v is None:
        return "—"
    return f"{v:,.{digits}f}{suffix}"


def fmt_signed(v, digits=1, suffix="%"):
    if v is None:
        return "—"
    return f"{'+' if v >= 0 else ''}{v:,.{digits}f}{suffix}"


def est_year_label(stock, i, fy=False):
    last_year = int(stock["years"][-1].split(" ")[1])
    return f"FY{last_year + i + 1}" if fy else f"Mar {last_year + i + 1}"


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

      .vl-chip-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px;
                     margin: 6px 0 18px; }
      .vl-chip { background: var(--vl-surface); border: 1px solid var(--vl-border);
                 border-left: 4px solid var(--chip-color, var(--vl-accent)); border-radius: 10px;
                 padding: 12px 16px; }
      .vl-chip-label { display: block; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.07em;
                        color: var(--vl-faint) !important; margin-bottom: 3px; }
      .vl-chip-cagr { font-size: 22px; font-weight: 700; color: var(--chip-color, var(--vl-accent)) !important; }
      .vl-chip-sub { display: block; font-size: 12px; color: var(--vl-muted) !important; margin-top: 2px; }

      .vl-badge { display: inline-block; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.06em;
                  font-weight: 700; padding: 3px 9px; border-radius: 20px; margin-left: 8px; vertical-align: middle; }
      .vl-badge-guidance { color: var(--vl-brass) !important; background: rgba(224,179,77,0.14); }

      /* Management Guidance Tracker page — small Beat/Neutral/Miss pill
       * rendered under each quarter's tag selectbox (same .vl-badge
       * shape as vl-badge-guidance above, not a widget-wrapper hack —
       * simpler and matches how CAGR +/- is already color-coded via
       * plain markdown spans elsewhere, e.g. cagr_html()). */
      .vl-tag-beat { color: var(--vl-good) !important; background: rgba(99,196,110,0.14); }
      .vl-tag-miss { color: var(--vl-bad) !important; background: rgba(227,119,106,0.14); }
      .vl-tag-neutral { color: var(--vl-brass) !important; background: rgba(224,179,77,0.14); }

      /* Guidance Tracker's Company x Quarter x [+] grid — fixed pixel
       * widths + horizontal scroll instead of the normal shrink-to-fit
       * st.columns() behavior, so quarter columns stay comfortably
       * writable/readable no matter how many accumulate (2026-08-16
       * request). :first-child / :last-child are structural, not a
       * fixed count, so this works regardless of how many quarters
       * exist — column order is always [Company (with its own remove
       * button inside), q1..qN, ➕] with nothing after ➕ (Price/Remove
       * both dropped from being separate trailing columns, same
       * request), so ➕ can keep appending columns rightward forever.
       * Every st.columns() row inside this one container shares the
       * same scroll position since none of them have their own overflow
       * context — only the outer container does.
       *
       * 2026-08-16 (later same day): the grid now paginates quarters
       * 3-at-a-time ("slides", see page_guidance_tracker's PAGE_SIZE) so
       * horizontal scroll is now the fallback, not the norm — bumped
       * every fixed width up (200→220, 260→340, 55→60) and the note
       * st.text_area's height (see the row-render loop) since 3 visible
       * columns leaves plenty of spare width/height that used to need
       * rationing across however many quarters had piled up. */
      .st-key-vl_guidance_grid { overflow-x: auto; padding-bottom: 10px; }
      .st-key-vl_guidance_grid div[data-testid="stHorizontalBlock"] { flex-wrap: nowrap; min-width: max-content; }
      .st-key-vl_guidance_grid div[data-testid="stHorizontalBlock"] > div:first-child {
        flex: 0 0 auto !important; width: 220px !important; min-width: 220px !important; }
      .st-key-vl_guidance_grid div[data-testid="stHorizontalBlock"] > div:last-child {
        flex: 0 0 auto !important; width: 60px !important; min-width: 60px !important; }
      .st-key-vl_guidance_grid div[data-testid="stHorizontalBlock"] > div:not(:first-child):not(:last-child) {
        flex: 0 0 auto !important; width: 340px !important; min-width: 340px !important; }

      /* Slim icon buttons (the summary row's Remove) */
      div[data-testid="stHorizontalBlock"] button[kind="secondary"] { padding: 2px 10px; }

      /* Summary row's company name — a tertiary st.button styled to read
         as a link (opens that company's Detail page) rather than a button,
         replacing the old standalone 🔍 icon column. */
      div[data-testid="stHorizontalBlock"] button[kind="tertiary"] {
        padding: 0; font-weight: 700; font-size: 14.5px; color: var(--vl-ink) !important;
      }
      div[data-testid="stHorizontalBlock"] button[kind="tertiary"]:hover { color: var(--vl-accent) !important; }
      div[data-testid="stHorizontalBlock"] button[kind="tertiary"] p { text-decoration: underline;
        text-decoration-color: transparent; transition: text-decoration-color 0.15s; }
      div[data-testid="stHorizontalBlock"] button[kind="tertiary"]:hover p { text-decoration-color: currentColor; }

      /* Financial Modelling table's per-cell number_input widgets — made
         to look like inline table cells rather than standalone form
         fields. Critical-input rows (Revenue Growth %/OPM %/Tax %) get a
         brass underline via :has() + the widget's own aria-label. */
      div[data-testid="stNumberInput"] { margin-bottom: 0 !important; }
      div[data-testid="stNumberInput"] input {
        background: transparent !important; border: none !important;
        border-bottom: 1px dashed var(--vl-faint) !important; border-radius: 0 !important;
        color: var(--vl-ink) !important; font-size: 13px !important;
        text-align: right !important; padding: 2px 4px !important; height: 30px !important;
      }
      div[data-testid="stNumberInput"] button { display: none !important; }
      div[data-testid="stNumberInput"]:has(input[aria-label="Revenue Growth %"]) input,
      div[data-testid="stNumberInput"]:has(input[aria-label="OPM %"]) input,
      div[data-testid="stNumberInput"]:has(input[aria-label="Tax %"]) input {
        border-bottom: 1.5px solid var(--vl-brass) !important;
        background: rgba(224,179,77,0.10) !important; font-weight: 600 !important;
      }

      /* Future Projections grid — one vertical rule after each FY's 3
       * case columns (Base/Bull/Bear), so the 9-column Year×Case grid
       * reads as 3 visually separated FY groups instead of one
       * undifferentiated block. Scoped to this grid's own st.container
       * key (not global) so it can't bleed into the summary table or
       * the fundamentals table, which use unrelated column counts. */
      .st-key-vl_projections_grid div[data-testid="stHorizontalBlock"] > div:nth-child(4),
      .st-key-vl_projections_grid div[data-testid="stHorizontalBlock"] > div:nth-child(7) {
        border-right: 1px solid var(--vl-border);
        padding-right: 6px;
      }

      /* Row labels (Revenue Growth %, Interest Expense Cr, Number of
       * Shares Cr, ...) at default body font size wrap to 2 lines in
       * that narrow first column on real-world (non-maximized) window
       * widths, breaking the "each metric is one row" layout this grid
       * is built around — shrink + force single line so column width
       * math never has to guess the exact pixel budget. */
      .st-key-vl_projections_grid div[data-testid="stHorizontalBlock"] > div:first-child p {
        font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
    </style>
    """, unsafe_allow_html=True)


def render_stat_row(items):
    cards = "".join(f'<div class="vl-stat"><span class="vl-stat-label">{lab}</span>'
                     f'<span class="vl-stat-value">{val}</span></div>' for lab, val in items)
    st.markdown(f'<div class="vl-stat-row">{cards}</div>', unsafe_allow_html=True)


def render_chip_row(chips):
    """chips: list of (label, color, cagr_text, sub_text) tuples."""
    cards = "".join(
        f'<div class="vl-chip" style="--chip-color:{color}"><span class="vl-chip-label">{label} CAGR</span>'
        f'<span class="vl-chip-cagr">{cagr_text}</span><span class="vl-chip-sub">{sub_text}</span></div>'
        for label, color, cagr_text, sub_text in chips
    )
    st.markdown(f'<div class="vl-chip-row">{cards}</div>', unsafe_allow_html=True)


def cagr_html(cagr, sub):
    if cagr is None:
        return '<span class="vl-sub">—</span>'
    cls = "vl-pos" if cagr >= 0 else "vl-neg"
    return f'<span class="{cls}">{fmt_signed(cagr, 1)}</span><br><span class="vl-sub">{sub}</span>'


def ema_yn_html(price, ema_value):
    """Y = price currently above that EMA (bullish), N = at/below, plus
    the signed % gap between price and that EMA underneath (added
    2026-08-16, request: "easy to track ones closer to ema") — small
    magnitude either direction means price is sitting right on the EMA
    (support/resistance zone, or an imminent crossover), which the Y/N
    letter alone can't distinguish from one that's decisively cleared
    it. The sign always agrees with Y/N by construction (Y ⟺ positive),
    so this isn't contradictory information, just the magnitude Y/N
    alone throws away. "—" when either input is missing (e.g. a company
    added before this existed and not yet refreshed, or the EMA fetch
    failed for that company on last refresh — see fetch_one()'s
    degrade-to-None handling in screener_fetch.py)."""
    if price is None or ema_value is None or ema_value == 0:
        return '<div style="text-align:center;"><span class="vl-sub">—</span></div>'
    is_above = price > ema_value
    cls = "vl-pos" if is_above else "vl-neg"
    label = "Y" if is_above else "N"
    pct = (price - ema_value) / ema_value * 100
    return (f'<div style="text-align:center;"><span class="{cls}" style="font-weight:700;">{label}</span>'
            f'<br><span class="{cls}" style="font-size:11px;">{fmt_signed(pct)}</span></div>')


def hist_cell_html(v, digits, suffix, colorize, bold):
    text = fmt(v, digits, suffix)
    cls = ""
    if colorize and v is not None:
        cls = "vl-pos" if v >= 0 else "vl-neg"
    weight = "font-weight:700;" if bold else ""
    span = f'<span class="{cls}" style="{weight}">{text}</span>' if cls else f'<span style="{weight}">{text}</span>'
    return f'<div style="text-align:right;">{span}</div>'


# ───────────────────────── Summary page ─────────────────────────

def refresh_all_stocks(session_id, show_progress=True):
    """Re-fetches every tracked ticker from Screener.in and saves (which
    also GitHub-syncs, see save_all_stocks). Shared by the manual
    "Refresh all now" button and the once-a-day auto-trigger in
    maybe_auto_refresh() — same code path either way. Returns the count
    refreshed."""
    raw = load_raw_all_stocks()
    tickers = list(raw.keys())
    if not tickers:
        return 0
    progress = st.progress(0.0, text="Refreshing…") if show_progress else None
    for i, t in enumerate(tickers):
        data, err = fetch_one(t, session_id)
        if not err:
            raw[t] = data
        if progress:
            progress.progress((i + 1) / len(tickers), text=f"Refreshed {t}")
    save_all_stocks(raw)
    if progress:
        progress.empty()
    return len(tickers)


def maybe_auto_refresh():
    """Once per calendar day, silently refreshes every tracked ticker's
    price/P&L the first time the app is opened that day — no button
    click needed, no growth/PE inputs required. The "day is done" flag
    (data/last_refresh.json) is GitHub-synced like everything else, so
    whichever device happens to open the app first each day satisfies
    it for every device, not just that one.

    Blocking: this runs synchronously before the page renders, so the
    first visitor of the day waits on N sequential Screener.in fetches.
    Fine for a personal watchlist of a handful of tickers; would need
    revisiting (e.g. a real scheduler) if the tracked list grows large.

    Gated by st.session_state so it only ever attempts once per browser
    session — main() (and therefore this) re-runs on every Streamlit
    rerun, i.e. every widget interaction, not just the initial load."""
    if st.session_state.get("auto_refresh_checked"):
        return
    st.session_state["auto_refresh_checked"] = True

    session_id = get_session_id()  # optional — fetch_one() works fine with None
    today = date.today().isoformat()
    if load_last_refresh().get("date") == today:
        return

    raw = load_raw_all_stocks()
    if raw:
        with st.spinner(f"Refreshing {len(raw)} tracked companies for today…"):
            n = refresh_all_stocks(session_id, show_progress=False)
        if n:
            st.toast(f"🔄 Auto-refreshed {n} companies for {today}")
    save_last_refresh({"date": today})


def page_summary(all_stocks, scenarios):
    if st.button("📋 Management Guidance Tracker →", help="Track quarterly management guidance and Beat/Neutral/Miss per company"):
        st.session_state["_view"] = "guidance_tracker"
        st.rerun()

    if not all_stocks:
        st.markdown('<div class="vl-empty">No companies yet — retrieve one from Screener.in below.</div>',
                    unsafe_allow_html=True)
        return

    if st.button("🔄 Refresh all now", help="Re-fetches every company below from Screener.in live"):
        n = refresh_all_stocks(get_session_id())
        st.success(f"Refreshed {n} companies.")
        st.rerun()

    # GRID_CASES (module-level, excludes "mgmt") drives the case columns —
    # see its definition for why. EMA_COLS: Y = price currently above that
    # EMA, N = at/below (see ema_yn_html()) — Close-based via Screener's
    # own chart API, not OHLC4 (explicit trade-off, 2026-08-16, over a
    # second data source like yfinance that has no coverage for SME/
    # small-cap names this app otherwise supports).
    EMA_COLS = [("ema20d", "20D"), ("ema50d", "50D"), ("ema33w", "33W")]
    col_widths = [2.2, 0.8, 0.6] + [0.5] * len(EMA_COLS) + [1.05, 1.05, 1.05, 0.55]
    header_labels = (["Company", "Price", "P/E"] + [lbl for _, lbl in EMA_COLS]
                      + [CASE_LABEL[c].replace(" Case", "") for c in GRID_CASES] + [""])
    header = st.columns(col_widths)
    for col, label in zip(header, header_labels):
        col.markdown(f"**{label}**")
    st.markdown('<hr style="margin:2px 0 8px;border-color:var(--vl-border);">', unsafe_allow_html=True)

    n_ema_cols = len(EMA_COLS)
    case_start = 3 + n_ema_cols
    n_case_cols = len(GRID_CASES)
    remove_col = case_start + n_case_cols
    for ticker, stock in all_stocks.items():
        cols = st.columns(col_widths)
        # Name itself opens the Detail page (replaces the old standalone 🔍
        # icon column, 2026-08-15) — styled via the tertiary-button CSS
        # above to read as a link, not a button.
        if cols[0].button(stock["name"], key=f"open_{ticker}", type="tertiary", help=f"Open {stock['name']}"):
            st.session_state["_jump_to"] = ticker
            st.rerun()
        cols[0].markdown(f"<span class='vl-sub' style='display:block;margin-top:-10px;'>{ticker}</span>",
                          unsafe_allow_html=True)
        cols[1].write(f"₹{fmt(stock.get('current_price'))}")
        cols[2].write(f"{fmt(stock.get('pe_ratio'), 1)}x")
        for i, (ema_key, _) in enumerate(EMA_COLS):
            cols[3 + i].markdown(ema_yn_html(stock.get("current_price"), stock.get(ema_key)),
                                  unsafe_allow_html=True)
        for i, case in enumerate(GRID_CASES):
            state = get_case_state(scenarios, stock, ticker, case)
            h = headline_cagr(stock, state)
            # Growth% and PE are the two assumptions that actually drove
            # this CAGR (added on request, 2026-08-15) — shown above the
            # price/year line so it's legible at a glance which case grew
            # revenue how fast at what exit multiple, not just the result.
            sub = (f"{fmt(h['growth'], 1)}% gr · {fmt(h['pe'], 1)}x PE<br>"
                   f"₹{fmt(h['share_price'])} · FY{h['year']}") if h and h["cagr"] is not None else "fill PE"
            cols[case_start + i].markdown(cagr_html(h["cagr"] if h else None, sub), unsafe_allow_html=True)
        if cols[remove_col].button("🗑️", key=f"remove_{ticker}", help=f"Remove {stock['name']} from tracking", use_container_width=True):
            raw = load_raw_all_stocks()
            raw.pop(ticker, None)
            save_all_stocks(raw)
            st.rerun()


# ───────────────────────── Management Guidance Tracker page ─────────────────────────

def page_guidance_tracker(all_stocks):
    """Company (static, left, with its own remove button) x Quarter
    (grows sideways via the ➕ column at the end, middle/right) grid —
    track what management guided each quarter, free-text notes plus a
    Beat/Neutral/Miss tag, added 2026-08-16 per explicit request. No
    Price column here (dropped on request, 2026-08-16 — already on the
    Summary page, redundant here, and kept ➕ from being anything but the
    literal last column so it can keep appending indefinitely). Quarter
    columns and this page's own tracked-company list are both
    user-controlled (add/remove/rename), not automatic — see
    load_guidance_tracker()'s docstring for why "tracked" here is
    deliberately separate from all_stocks.json's full company set even
    though adding a company here still fetches into that same shared
    store (no duplicated price data, only tracked-here membership)."""
    if st.button("← Back to Summary", key="gt_back"):
        st.session_state["_view"] = "summary"
        st.rerun()

    st.subheader("📋 Management Guidance Tracker")
    st.caption("Company (left) × Quarter (grows rightward — ➕ at the end adds another).")

    tracker = load_guidance_tracker()
    quarters = tracker.setdefault("quarters", [])
    tracked = tracker.setdefault("tracked", [])
    cells = tracker.setdefault("cells", {})

    # ── Add a company (fetches into the shared all_stocks.json, same as
    # Retrieve on Summary — only this page's "tracked" membership is new) ──
    with st.form("gt_add_company", clear_on_submit=True):
        c1, c2 = st.columns([4, 1])
        with c1:
            new_ticker = st.text_input("Add a company to this tracker",
                                        placeholder="e.g. TITAN, or a numeric BSE code for SME names").strip()
        with c2:
            st.write("")
            add_co_submitted = st.form_submit_button("Add", type="primary", use_container_width=True)
    if add_co_submitted and new_ticker:
        with st.spinner(f"Fetching {new_ticker} from Screener.in…"):
            data, err = fetch_one(new_ticker.upper(), get_session_id())
        if err:
            st.error(f"Couldn't fetch **{new_ticker}**: {err}")
        else:
            raw = load_raw_all_stocks()
            raw[data["ticker"]] = data
            save_all_stocks(raw)
            if data["ticker"] not in tracked:
                tracked.append(data["ticker"])
                save_guidance_tracker(tracker)
            st.success(f"Added **{data['name']}** ({data['ticker']}).")
            st.rerun()
    elif add_co_submitted:
        st.warning("Type a ticker or company name first.")

    if not tracked:
        st.markdown('<div class="vl-empty">No companies tracked here yet — add one above.</div>',
                    unsafe_allow_html=True)
        return

    st.divider()

    # Quarters paginate 3-at-a-time ("slides") instead of all piling up in
    # one horizontally-scrolling row (explicit request, 2026-08-16, later
    # same day as the fixed-width change below) — with only 3 columns ever
    # on screen at once, each can afford to be wider/taller than when an
    # arbitrary number had to share the row. gt_slide is the 0-indexed
    # slide number; session_state persists it across reruns (tag/note
    # edits rerun this page) but NOT across a fresh page load, so opening
    # the tracker always starts on the most recent slide.
    PAGE_SIZE = 3
    total_slides = max(1, -(-len(quarters) // PAGE_SIZE))  # ceil div
    if "gt_slide" not in st.session_state:
        st.session_state["gt_slide"] = total_slides - 1
    st.session_state["gt_slide"] = max(0, min(st.session_state["gt_slide"], total_slides - 1))
    slide = st.session_state["gt_slide"]
    slide_start = slide * PAGE_SIZE
    visible_quarters = quarters[slide_start:slide_start + PAGE_SIZE]

    if total_slides > 1:
        nav = st.columns([1, 3, 1])
        with nav[0]:
            if st.button("◀ Older", key="gt_slide_prev", disabled=slide == 0, use_container_width=True):
                st.session_state["gt_slide"] = slide - 1
                st.rerun()
        with nav[1]:
            st.markdown(f"<div style='text-align:center;color:var(--vl-muted);padding-top:6px;'>"
                        f"Quarters {slide_start + 1}–{min(slide_start + PAGE_SIZE, len(quarters))} of "
                        f"{len(quarters)}</div>", unsafe_allow_html=True)
        with nav[2]:
            if st.button("Newer ▶", key="gt_slide_next", disabled=slide >= total_slides - 1,
                         use_container_width=True):
                st.session_state["gt_slide"] = slide + 1
                st.rerun()

    # Fixed pixel widths + horizontal scroll (explicit request, 2026-08-16:
    # "set the width to each quarter to at least sizeable to write and
    # read easily") instead of Streamlit's normal st.columns() behavior,
    # which proportionally SHRINKS every column to fit the container —
    # scroll is now just a fallback for narrow viewports since pagination
    # above caps this at 3 quarter columns regardless of how many exist
    # overall. Scoped to this one container (.st-key-vl_guidance_grid, see
    # inject_css()) so it can't affect the Summary table or the
    # Fundamentals/Projections grids on the Detail page, which all still
    # want the normal shrink-to-fit behavior. Structural :first-child /
    # :nth-last-child selectors (not nth-child(N) counting from the
    # start) so the CSS doesn't need to know how many quarter columns
    # are visible — Company stays wide-fixed on the left, ➕ stays
    # narrow-fixed on the right, and every visible quarter in between
    # gets the same comfortable fixed width. The relative ratios still
    # passed to st.columns() below are moot once this CSS's
    # `flex: 0 0 auto` overrides them — kept only because st.columns()
    # requires *some* ratio list matching the column count.
    with st.container(key="vl_guidance_grid"):
        # ➕ is the LAST column, full stop (explicit request, 2026-08-16:
        # "we dont need price col at end... let + button be end of the
        # col, so it can keep appending the cols") — Price dropped from
        # this grid entirely (already on the Summary page, redundant
        # here), and the per-row remove button moved into the Company
        # cell itself so nothing trails after ➕ either. New quarters
        # just keep extending the list rightward with ➕ always at the
        # tip of the full list — adding one auto-jumps to the slide that
        # now contains it, since it's by definition the most recent.
        col_widths = [2.0] + [1.4] * len(visible_quarters) + [0.5]
        add_q_col = 1 + len(visible_quarters)  # == last index

        # ── Header row ── (remove button stacked under the label, not a
        # nested st.columns(), so the fixed-width CSS above — which
        # targets every stHorizontalBlock inside this container — has no
        # nested row to accidentally also force wide)
        header = st.columns(col_widths)
        header[0].markdown("**Company**")
        for vi, q in enumerate(visible_quarters):
            i = slide_start + vi  # real index into the full quarters list
            with header[1 + vi]:
                # Keyed by the quarter's own label, not its index — an
                # index-based key would collide with stale session_state
                # left over from whatever quarter used to sit at that
                # position before an earlier removal shifted the list,
                # showing/renaming the wrong quarter's label.
                new_label = st.text_input("Quarter label", value=q, key=f"gt_qlabel_{q}",
                                           label_visibility="collapsed")
                if new_label != q and new_label.strip() and new_label not in quarters:
                    quarters[i] = new_label.strip()
                    cells_for_q = {t: cells[t].pop(q) for t in cells if q in cells[t]}
                    for t, cell in cells_for_q.items():
                        cells[t][new_label.strip()] = cell
                    save_guidance_tracker(tracker)
                    st.rerun()
                if st.button("✕ remove", key=f"gt_rmq_{q}", help=f"Remove the \"{q}\" column (all companies)"):
                    quarters.remove(q)
                    for t in cells:
                        cells[t].pop(q, None)
                    save_guidance_tracker(tracker)
                    st.rerun()
        with header[add_q_col]:
            st.write("")
            if st.button("➕", key="gt_add_quarter_btn", help="Add a new quarter column"):
                n = len(quarters) + 1
                label = f"Quarter {n}"
                while label in quarters:
                    n += 1
                    label = f"Quarter {n}"
                quarters.append(label)
                save_guidance_tracker(tracker)
                st.session_state["gt_slide"] = max(0, -(-len(quarters) // PAGE_SIZE) - 1)
                st.rerun()
        st.markdown('<hr style="margin:2px 0 8px;border-color:var(--vl-border);">', unsafe_allow_html=True)

        # ── One row per tracked company ──
        for ticker in list(tracked):
            stock = all_stocks.get(ticker)
            row_cells = cells.setdefault(ticker, {})
            cols = st.columns(col_widths)
            with cols[0]:
                st.markdown(
                    f"**{stock['name'] if stock else ticker}**  \n<span class='vl-sub'>{ticker}</span>",
                    unsafe_allow_html=True)
                if st.button("🗑️ remove", key=f"gt_rm_{ticker}", help=f"Remove {ticker} from this tracker only "
                             "(doesn't touch the main company list)"):
                    tracked.remove(ticker)
                    save_guidance_tracker(tracker)
                    st.rerun()

            for vi, q in enumerate(visible_quarters):
                cell = row_cells.setdefault(q, {"note": "", "tag": ""})
                with cols[1 + vi]:
                    tag = st.selectbox("Tag", GUIDANCE_TAGS,
                                        index=GUIDANCE_TAGS.index(cell["tag"]) if cell["tag"] in GUIDANCE_TAGS else 0,
                                        key=f"gt_{ticker}_{q}_tag", label_visibility="collapsed")
                    # Always render the badge line, hidden (not omitted)
                    # when there's no tag yet — reserves the same height
                    # either way, so every row's note box starts at the
                    # same y position regardless of which cells happen to
                    # have a tag set (misalignment reported 2026-08-16:
                    # tagged cells' notes sat lower than untagged ones').
                    badge_html = (f"<span class='vl-badge {GUIDANCE_TAG_CLASS[tag]}'>{tag}</span>" if tag
                                  else "<span class='vl-badge' style='visibility:hidden;'>—</span>")
                    st.markdown(badge_html, unsafe_allow_html=True)
                    # height bumped 90→140 (2026-08-16, same request as the
                    # column-width bump above) — 3-per-slide pagination
                    # freed up enough width that taller notes read better
                    # than wider-but-shorter ones.
                    note = st.text_area("Note", value=cell["note"], key=f"gt_{ticker}_{q}_note",
                                         label_visibility="collapsed", height=140,
                                         placeholder="Guidance / commentary…")
                    if tag != cell["tag"] or note != cell["note"]:
                        row_cells[q] = {"note": note, "tag": tag}
                        save_guidance_tracker(tracker)


# ───────────────────────── Detail page (fundamentals) ─────────────────────────

def page_detail(stock, ticker, scenarios):
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

    if st.button(f"🔄 Refresh {ticker} now"):
        with st.spinner("Fetching…"):
            data, err = fetch_one(ticker, get_session_id())
        if err:
            st.error(err)
        else:
            raw = load_raw_all_stocks()
            raw[ticker] = data
            save_all_stocks(raw)
            st.success("Refreshed.")
            st.rerun()

    chips = []
    for case in CASES:
        state = get_case_state(scenarios, stock, ticker, case)
        h = headline_cagr(stock, state)
        color = CASE_COLOR[case]
        if h and h["cagr"] is not None:
            chips.append((CASE_LABEL[case], color, fmt_signed(h["cagr"], 1), f"₹{fmt(h['share_price'])} · FY{h['year']}"))
        else:
            chips.append((CASE_LABEL[case], color, "—", "fill PE to compute"))
    render_chip_row(chips)

    st.divider()
    # Reflects the actual per-stock fetch result, not assumed consolidated
    # — screener_fetch.py falls back to standalone whenever consolidated
    # has too few years (e.g. a company whose consolidated reporting only
    # started recently despite decades of standalone history), so this
    # varies stock to stock and the label needs to say which it got.
    basis = "consolidated" if stock.get("consolidated") else "standalone"
    st.caption(f"Fundamentals (Screener.in, {basis}) — annual Profit & Loss")

    n = len(stock["years"])
    hist_col_widths = [2.2] + [1] * n

    def hist_row(label, vals, digits=0, suffix="", colorize=False, bold=False):
        cols = st.columns(hist_col_widths)
        cols[0].markdown(("**" + label + "**") if bold else label)
        for j, v in enumerate(vals):
            cols[1 + j].markdown(hist_cell_html(v, digits, suffix, colorize, bold), unsafe_allow_html=True)

    hdr = st.columns(hist_col_widths)
    hdr[0].markdown("**Financial Year**")
    for j, y in enumerate(stock["years"]):
        hdr[1 + j].markdown(f"<div style='text-align:right;color:var(--vl-faint);font-size:11px;'>{y}</div>",
                             unsafe_allow_html=True)

    hist_row("Revenue Cr", stock["revenue"], bold=True)
    hist_row("Revenue Growth %", stock["revenue_growth_pct"], 1, "%", colorize=True)
    hist_row("Expenses Cr", stock["expenses"])
    hist_row("Operating Profit Cr", stock["operating_profit"], bold=True)
    hist_row("OPM %", stock["opm_pct"], 1, "%", colorize=True)
    hist_row("Other Income Cr", stock["other_income"])
    hist_row("Interest Expense Cr", stock["interest"])
    hist_row("Depreciation Cr", stock["depreciation"])
    hist_row("PBT Cr", stock["pbt"], bold=True)
    hist_row("Tax %", stock["tax_pct"], 1, "%")
    hist_row("PAT Cr", stock["net_profit"], bold=True)
    hist_row("PAT Growth %", stock["pat_growth_pct"], 1, "%", colorize=True)
    hist_row("Number of Shares Cr", stock["shares_cr"], 3)
    hist_row("EPS ₹", stock["eps"], 2, bold=True)

    st.divider()
    render_projections_grid(stock, ticker, scenarios)


def render_projections_grid(stock, ticker, scenarios):
    """Base/Bull/Bear/Management × all 3 estimate years flattened into one
    grid — explicit correction, 2026-08-15: an earlier version repeated a
    full 4-case header + ~11 rows per year (3 separate blocks), which
    meant scrolling past a lot of vertical space that the page's width
    wasn't using ("input boxes are very wide enough" — plenty of spare
    horizontal room). One metric per row, with all Year×Case combinations
    as columns in that same row, uses the width instead of the scroll."""
    st.subheader("🎯 Future Projections & CAGR — all cases, all years, one grid")

    # GRID_CASES (module-level) excludes "mgmt" here too — see its
    # definition for why. The persist loop below only writes back
    # drivers for the cases actually rendered in this grid.

    guidance = load_guidance(ticker)
    if guidance:
        st.info(f"📋 **Guidance-seeded** — Base/Bull/Bear Revenue Growth % pre-filled from management guidance "
                f"research where available (as of {guidance.get('as_of', 'unknown')}). Edit any case freely.",
                icon="📋")

    case_states = {c: get_case_state(scenarios, stock, ticker, c) for c in GRID_CASES}

    EDITABLE_FIELDS = ["revGrowth", "opm", "tax", "other_income", "interest", "depreciation", "shares"]
    FIELD_STEP = {"revGrowth": 0.5, "opm": 0.5, "tax": 0.5, "other_income": 1.0,
                  "interest": 1.0, "depreciation": 1.0, "shares": 0.01}
    FIELD_DIGITS = {"revGrowth": 1, "opm": 1, "tax": 1, "other_income": 1,
                     "interest": 1, "depreciation": 1, "shares": 3}
    FIELD_LABEL = {"revGrowth": "Revenue Growth %", "opm": "OPM %", "tax": "Tax %",
                   "other_income": "Other Income Cr", "interest": "Interest Expense Cr",
                   "depreciation": "Depreciation Cr", "shares": "Number of Shares Cr"}

    def widget_key(case, field, i):
        return f"{ticker}_{case}_{field}_{i}"

    # Two-pass, same trick as before but across all rendered cases: Pass
    # 1 reads every case's current widget values straight out of
    # session_state (a case's "Revenue Cr" needs that case's own Revenue
    # Growth %, whose input widget renders in the same grid) and
    # computes each case's full 3-year model up front. Pass 2 renders.
    effective = {}
    for case in GRID_CASES:
        state = case_states[case]
        eff_list = []
        for i in range(N_EST_YEARS):
            dr = state["drivers"][i]
            eff = {f: st.session_state.get(widget_key(case, f, i), as_float(dr[f])) for f in EDITABLE_FIELDS}
            eff["pe"] = st.session_state.get(f"{ticker}_{case}_pe_{i}", as_float(dr["pe"]))
            eff_list.append(eff)
        effective[case] = eff_list
    models = {case: compute_model(stock, {"drivers": effective[case]}) for case in GRID_CASES}

    last_year = int(stock["years"][-1].split(" ")[1])
    n_cases = len(GRID_CASES)
    grid_widths = [1.5] + [0.95] * (N_EST_YEARS * n_cases)

    def col_index(i, c):
        return 1 + i * n_cases + c

    def header_row():
        cols = st.columns(grid_widths)
        cols[0].markdown("&nbsp;", unsafe_allow_html=True)
        for i in range(N_EST_YEARS):
            year = last_year + i + 1
            for c, case in enumerate(GRID_CASES):
                cols[col_index(i, c)].markdown(
                    f"<div style='text-align:center;font-size:10px;line-height:1.3;'>"
                    f"<b>FY{year}</b><br><span style='color:{CASE_COLOR[case]};font-weight:700;'>"
                    f"{CASE_LABEL[case].replace(' Case', '')}</span></div>", unsafe_allow_html=True)

    def input_row(label, field):
        cols = st.columns(grid_widths)
        cols[0].markdown(label)
        for i in range(N_EST_YEARS):
            for c, case in enumerate(GRID_CASES):
                with cols[col_index(i, c)]:
                    st.number_input(FIELD_LABEL[field], value=effective[case][i][field],
                                     step=FIELD_STEP[field], format=f"%.{FIELD_DIGITS[field]}f",
                                     key=widget_key(case, field, i), label_visibility="collapsed")

    def computed_row(label, field, digits=0, suffix=""):
        cols = st.columns(grid_widths)
        cols[0].markdown(f"*{label}*")
        for i in range(N_EST_YEARS):
            for c, case in enumerate(GRID_CASES):
                v = models[case][i][field]
                cols[col_index(i, c)].markdown(
                    f"<div style='text-align:center;color:var(--vl-muted);font-style:italic;font-size:12px;'>"
                    f"{fmt(v, digits, suffix)}</div>", unsafe_allow_html=True)

    # Keyed container so the FY-separator CSS rule (nth-child on this
    # grid's own st.columns() rows) can't bleed into any other table on
    # the page — see inject_css()'s .st-key-vl_projections_grid rule.
    with st.container(key="vl_projections_grid"):
        header_row()
        input_row("Revenue Growth %", "revGrowth")
        input_row("OPM %", "opm")
        input_row("Tax %", "tax")
        input_row("Other Income Cr", "other_income")
        input_row("Interest Expense Cr", "interest")
        input_row("Depreciation Cr", "depreciation")
        input_row("Number of Shares Cr", "shares")
        computed_row("Revenue Cr", "revenue")
        computed_row("PAT Cr", "pat")
        computed_row("EPS ₹", "eps", 2)

        pe_cols = st.columns(grid_widths)
        pe_cols[0].markdown("**PE Multiple**")
        for i in range(N_EST_YEARS):
            for c, case in enumerate(GRID_CASES):
                with pe_cols[col_index(i, c)]:
                    st.number_input("PE Multiple", value=as_float(case_states[case]["drivers"][i].get("pe")),
                                     step=0.5, format="%.1f", key=f"{ticker}_{case}_pe_{i}",
                                     label_visibility="collapsed", placeholder="PE")

        cagr_cols = st.columns(grid_widths)
        cagr_cols[0].markdown("**CAGR**")
        for i in range(N_EST_YEARS):
            year = last_year + i + 1
            for c, case in enumerate(GRID_CASES):
                eps = models[case][i]["eps"]
                pe_val = st.session_state.get(f"{ticker}_{case}_pe_{i}")
                share_price = eps * pe_val if eps is not None and pe_val else None
                cagr = cagr_for(stock["current_price"], share_price, days_until(year))
                sub = f"₹{fmt(share_price)}" if share_price is not None else ""
                with cagr_cols[col_index(i, c)]:
                    st.markdown(f"<div style='text-align:center;font-size:11px;'>{cagr_html(cagr, sub)}</div>",
                                unsafe_allow_html=True)

    st.markdown('<div style="font-size:11px;color:var(--vl-faint);margin-top:6px;">'
                'plain = editable estimate (carried forward by default) · <i>italic</i> = auto-computed</div>',
                unsafe_allow_html=True)

    # ── Persist any edits across the cases rendered here (Mgmt's saved
    # state, if any, is untouched — it has no widgets in this grid) ──
    for case in GRID_CASES:
        state = case_states[case]
        new_drivers = []
        for i in range(N_EST_YEARS):
            nd = {f: st.session_state.get(widget_key(case, f, i)) for f in EDITABLE_FIELDS}
            nd["pe"] = st.session_state.get(f"{ticker}_{case}_pe_{i}")
            new_drivers.append(nd)
        if new_drivers != state["drivers"]:
            state["drivers"] = new_drivers
            set_case_state(scenarios, ticker, case, state)

    # ── Key assumptions — one narrow text area per case ──
    st.divider()
    st.caption("Key Assumptions")
    assum_cols = st.columns(len(GRID_CASES))
    for c, case in enumerate(GRID_CASES):
        with assum_cols[c]:
            st.markdown(f"<span style='color:{CASE_COLOR[case]};font-weight:600;font-size:12.5px;'>"
                        f"{CASE_LABEL[case]}</span>", unsafe_allow_html=True)
            val = st.text_area(f"{case} assumptions", value=case_states[case]["assumptions"], height=110,
                                key=f"{ticker}_{case}_assumptions", label_visibility="collapsed", max_chars=5000)
            if val != case_states[case]["assumptions"]:
                case_states[case]["assumptions"] = val
                set_case_state(scenarios, ticker, case, case_states[case])
            if st.button("Clear estimates", key=f"{ticker}_{case}_clear", use_container_width=True):
                scenarios.get(ticker, {}).pop(case, None)
                save_scenarios(scenarios)
                st.rerun()


# ───────────────────────── Retrieve ─────────────────────────

def section_retrieve(all_stocks):
    st.divider()
    st.subheader("📥 Retrieve from Screener")

    with st.form("retrieve_form", clear_on_submit=True):
        col1, col2 = st.columns([4, 1])
        with col1:
            new_ticker = st.text_input("NSE/BSE ticker or company name",
                                        placeholder="e.g. TITAN, or a numeric BSE code for SME names").strip()
        with col2:
            st.write("")
            submitted = st.form_submit_button("Retrieve", type="primary", use_container_width=True)

    if submitted and new_ticker:
        with st.spinner(f"Fetching {new_ticker} from Screener.in…"):
            data, err = fetch_one(new_ticker.upper(), get_session_id())
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
    inject_css()  # runs before the password gate too, so the lock screen gets the same dark theme
    if not check_password():
        return

    st.title("🧮 Valuation Ledger")

    maybe_auto_refresh()  # before load_all_stocks(), so a same-day refresh renders fresh, not stale

    all_stocks = load_all_stocks()
    scenarios = load_scenarios()
    jump_to = st.session_state.get("_jump_to")

    if jump_to and jump_to in all_stocks:
        page_detail(all_stocks[jump_to], jump_to, scenarios)
        return

    if st.session_state.get("_view") == "guidance_tracker":
        page_guidance_tracker(all_stocks)
        return

    st.caption("Summary — retrieve companies live from Screener.in")
    page_summary(all_stocks, scenarios)
    section_retrieve(all_stocks)


if __name__ == "__main__":
    main()
